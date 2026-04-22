"""Engine service layer.

Thin coordination functions used by both the CLI and the MCP server. Each
function takes an already-constructed config + keys + db + class triple so
the caller controls lifecycle. The service layer never owns state.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from custcomm.ai.classifier import IntentClassifier
from custcomm.ai.drafter import ReplyDrafter
from custcomm.config.loader import APIKeys, CustCommConfig
from custcomm.conversation.history import ThreadHistoryBuilder
from custcomm.conversation.threading import normalize_subject, resolve_thread_id
from custcomm.crm.database import ThreadDatabase
from custcomm.inbox import build_inbox
from custcomm.models import (
    Customer,
    Draft,
    DraftStatus,
    Intent,
    Message,
    MessageDirection,
    RawInboundMessage,
    Thread,
    ThreadStatus,
)
from custcomm.outreach import build_sender

logger = logging.getLogger(__name__)


# Intents that we never auto-draft regardless of config (engine-level floor).
_NEVER_AUTO_DRAFT = {Intent.UNCERTAIN, Intent.UNRELATED, Intent.COMPLAINT, Intent.CANCEL}


# ── Poll ──────────────────────────────────────────────────────────────────────


async def poll_inbox(
    config: CustCommConfig, keys: APIKeys, db: ThreadDatabase
) -> dict[str, int]:
    """Pull new inbound messages and persist them, matched to threads.

    Returns a summary dict: {messages_ingested, messages_skipped_duplicate,
    new_threads, threads_matched}.
    """
    inbox = build_inbox(config, keys)
    ingested = 0
    duplicates = 0
    new_threads = 0
    matched = 0

    async for raw in inbox.fetch_new():
        result = await _ingest_inbound(raw, db)
        if result is None:
            duplicates += 1
            continue
        message, is_new_thread = result
        ingested += 1
        if is_new_thread:
            new_threads += 1
        else:
            matched += 1

    return {
        "messages_ingested": ingested,
        "messages_skipped_duplicate": duplicates,
        "new_threads": new_threads,
        "threads_matched": matched,
    }


async def _ingest_inbound(
    raw: RawInboundMessage, db: ThreadDatabase
) -> Optional[tuple[Message, bool]]:
    # Dedup by header before anything else — cheap, avoids upsert churn.
    if raw.message_id_header:
        existing_tid = await db.find_thread_by_message_header(raw.message_id_header)
        if existing_tid:
            return None

    customer = Customer(
        email=raw.from_addr,
        display_name=raw.from_name,
        last_seen_at=raw.received_at or datetime.utcnow(),
    )
    customer = await db.upsert_customer(customer)

    thread_id = await resolve_thread_id(raw, customer.id, db)
    is_new_thread = thread_id is None

    if is_new_thread:
        thread = Thread(
            customer_id=customer.id,
            subject=normalize_subject(raw.subject) or raw.subject or "(no subject)",
            status=ThreadStatus.NEW,
            last_inbound_at=raw.received_at,
        )
        await db.upsert_thread(thread)
        thread_id = thread.id
    else:
        thread = await db.get_thread(thread_id)  # type: ignore[arg-type]
        if thread is not None:
            # Keep original subject; only update if empty.
            if not thread.subject:
                thread.subject = normalize_subject(raw.subject) or raw.subject
            # A new inbound on an existing thread always pulls it back into the
            # triage flow — even if we were AWAITING_CUSTOMER or ESCALATED.
            if thread.status in (
                ThreadStatus.AWAITING_CUSTOMER,
                ThreadStatus.SNOOZED,
                ThreadStatus.CLOSED,
            ):
                thread.status = ThreadStatus.NEW
            thread.last_inbound_at = raw.received_at or datetime.utcnow()
            await db.upsert_thread(thread)

    message = Message(
        thread_id=thread_id,  # type: ignore[arg-type]
        customer_id=customer.id,
        direction=MessageDirection.INBOUND,
        from_addr=raw.from_addr,
        to_addrs=raw.to_addrs,
        cc_addrs=raw.cc_addrs,
        subject=raw.subject,
        body_text=raw.body_text,
        body_html=raw.body_html,
        message_id_header=raw.message_id_header,
        in_reply_to_header=raw.in_reply_to_header,
        references_headers=raw.references_headers,
        received_at=raw.received_at,
        raw_data={"provider": raw.provider, "provider_id": raw.provider_message_id},
        attachment_log=raw.attachments,
    )
    inserted = await db.insert_message(message)
    if not inserted:
        return None
    return message, is_new_thread


# ── Triage ────────────────────────────────────────────────────────────────────


async def triage_threads(
    config: CustCommConfig,
    keys: APIKeys,
    db: ThreadDatabase,
    classifier_cls: type[IntentClassifier] = IntentClassifier,
    thread_ids: Optional[list[str]] = None,
) -> dict[str, int]:
    """Classify intent for NEW threads (or a specific set)."""
    classifier = classifier_cls(config, keys)

    if thread_ids:
        threads = [t for t in (await db.get_thread(tid) for tid in thread_ids) if t]
    else:
        threads = await db.list_threads(status=ThreadStatus.NEW, limit=200)

    if not threads:
        return {"triaged": 0}

    counts: dict[str, int] = {}
    escalated = 0
    for thread in threads:
        messages = await db.get_messages(thread.id, limit=50)
        latest_inbound = next(
            (m for m in reversed(messages) if m.direction == MessageDirection.INBOUND),
            None,
        )
        if latest_inbound is None:
            continue
        result = await classifier.classify(thread, latest_inbound)
        thread.intent = result.intent
        thread.intent_confidence = result.confidence
        thread.intent_reasoning = result.reasoning

        if result.intent in {Intent.COMPLAINT, Intent.CANCEL}:
            thread.status = ThreadStatus.ESCALATED
            thread.escalation_reason = (
                f"Auto-escalated due to intent={result.intent.value}"
            )
            escalated += 1
        else:
            thread.status = ThreadStatus.TRIAGED

        await db.upsert_thread(thread)
        counts[result.intent.value] = counts.get(result.intent.value, 0) + 1

    counts["_escalated"] = escalated
    counts["triaged"] = sum(
        v for k, v in counts.items() if k not in {"_escalated", "triaged"}
    )
    return counts


# ── Draft ─────────────────────────────────────────────────────────────────────


async def draft_replies(
    config: CustCommConfig,
    keys: APIKeys,
    db: ThreadDatabase,
    drafter_cls: type[ReplyDrafter] = ReplyDrafter,
    thread_ids: Optional[list[str]] = None,
    guidance: str = "",
) -> list[Draft]:
    """Draft replies for eligible TRIAGED threads (or a specific set).

    Eligibility rules (never auto-drafts):
      - intent in config.outreach.auto_draft_intents
      - intent not in {UNCERTAIN, UNRELATED, COMPLAINT, CANCEL} (engine floor)
      - thread has at least one inbound message
    """
    drafter = drafter_cls(config, keys)
    builder = ThreadHistoryBuilder(config, db)

    if thread_ids:
        threads = [t for t in (await db.get_thread(tid) for tid in thread_ids) if t]
    else:
        threads = await db.list_threads(status=ThreadStatus.TRIAGED, limit=100)

    allowed = {i for i in config.outreach.auto_draft_intents}
    out: list[Draft] = []
    for thread in threads:
        if not thread.intent or thread.intent in _NEVER_AUTO_DRAFT:
            logger.info(
                f"Skipping thread {thread.id}: intent={thread.intent} not auto-draftable"
            )
            continue
        if thread.intent.value not in allowed:
            logger.info(
                f"Skipping thread {thread.id}: intent={thread.intent.value} "
                "not in outreach.auto_draft_intents"
            )
            continue

        messages = await db.get_messages(thread.id)
        if not any(m.direction == MessageDirection.INBOUND for m in messages):
            logger.warning(f"Thread {thread.id} has no inbound; can't draft")
            continue

        history_view = await builder.build(thread, summarizer=drafter)
        draft = await drafter.draft(history_view, thread.intent, guidance=guidance)
        await db.insert_draft(draft)
        thread.status = ThreadStatus.DRAFTED
        await db.upsert_thread(thread)
        out.append(draft)

    return out


# ── Approve / Send ────────────────────────────────────────────────────────────


async def approve_draft(
    db: ThreadDatabase, thread_id: str, approved_by: str = "cli"
) -> Optional[Draft]:
    """Approve the thread's pending draft. Returns the updated draft or None
    if there's nothing pending."""
    draft = await db.get_pending_draft_for_thread(thread_id)
    if not draft:
        return None
    ok = await db.approve_draft(draft.id, approved_by=approved_by)
    if not ok:
        return None
    # Transition thread status too.
    thread = await db.get_thread(thread_id)
    if thread:
        thread.status = ThreadStatus.APPROVED
        await db.upsert_thread(thread)
    return await db.get_draft(draft.id)


async def send_approved(
    config: CustCommConfig,
    keys: APIKeys,
    db: ThreadDatabase,
    thread_id: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Send all APPROVED drafts, or one specific thread's approved draft.

    Enforces:
      - `config.outreach.require_approval` (refuses to run if False in code)
      - `config.outreach.daily_send_limit` (skips once reached)
      - Per-draft atomic token-guarded update (no double-send)
    """
    if not config.outreach.require_approval:
        raise RuntimeError(
            "config.outreach.require_approval is False — refusing to send. "
            "The engine only supports the human-approval flow."
        )

    sender = build_sender(config, keys) if not dry_run else None
    approved = await db.list_drafts(status=DraftStatus.APPROVED, limit=500)
    if thread_id:
        approved = [d for d in approved if d.thread_id == thread_id]

    sent_today = await db.count_sent_today()
    remaining = max(0, config.outreach.daily_send_limit - sent_today)

    sent = 0
    skipped_limit = 0
    skipped_race = 0
    errors = 0

    for draft in approved:
        if sent >= remaining and not dry_run:
            skipped_limit += 1
            continue

        thread = await db.get_thread(draft.thread_id)
        if not thread:
            continue
        messages = await db.get_messages(draft.thread_id)
        latest_inbound = next(
            (m for m in reversed(messages) if m.direction == MessageDirection.INBOUND),
            None,
        )

        if dry_run:
            logger.info(f"[DRY-RUN] Would send draft {draft.id} on thread {thread.id}")
            sent += 1
            continue

        try:
            result = await sender.send(draft, thread, latest_inbound)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001
            logger.error(f"Send failed for draft {draft.id}: {e}")
            errors += 1
            continue

        owned = await db.mark_draft_sent(
            draft.id, draft.approval_token, result.message_id_header
        )
        if not owned:
            skipped_race += 1
            logger.warning(
                f"Draft {draft.id} was already sent by another caller — "
                "skipping status update."
            )
            continue

        outbound = Message(
            thread_id=thread.id,
            customer_id=thread.customer_id,
            direction=MessageDirection.OUTBOUND,
            from_addr=config.operator_email,
            to_addrs=[latest_inbound.from_addr] if latest_inbound else [],
            subject=draft.subject,
            body_text=draft.body,
            message_id_header=result.message_id_header,
            in_reply_to_header=(
                latest_inbound.message_id_header if latest_inbound else None
            ),
            references_headers=(
                latest_inbound.references_headers if latest_inbound else []
            ),
            sent_at=datetime.utcnow(),
            raw_data={"provider_id": result.provider_id} if result.provider_id else {},
        )
        await db.insert_message(outbound)

        thread.status = ThreadStatus.AWAITING_CUSTOMER
        thread.last_outbound_at = datetime.utcnow()
        await db.upsert_thread(thread)
        sent += 1

    return {
        "sent": sent,
        "skipped_daily_limit": skipped_limit,
        "skipped_double_send": skipped_race,
        "errors": errors,
    }
