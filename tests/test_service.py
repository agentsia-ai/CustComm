"""End-to-end service tests — using mocked Anthropic, fake inbox, and a
real temp SQLite DB. Validates the whole poll → triage → draft → approve →
send flow in a single pass."""

from __future__ import annotations

from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from custcomm._time import now_utc
from custcomm.crm.database import ThreadDatabase
from custcomm.inbox.base import InboxConnector
from custcomm.models import (
    DraftStatus,
    Intent,
    MessageDirection,
    RawInboundMessage,
    ThreadStatus,
)
from custcomm.outreach.base import ReplySender, SendResult
from custcomm.service import (
    approve_draft,
    draft_replies,
    poll_inbox,
    send_approved,
    triage_threads,
)


class FakeInbox(InboxConnector):
    """Inbox that yields a fixed list of RawInboundMessage objects."""

    def __init__(self, config, keys, messages):
        super().__init__(config, keys)
        self._messages = messages

    async def fetch_new(self) -> AsyncIterator[RawInboundMessage]:
        for m in self._messages:
            yield m


class FakeSender(ReplySender):
    async def send(self, draft, thread, latest_inbound) -> SendResult:
        return SendResult(
            message_id_header=f"<fake-{draft.id}@test>", provider_id=f"prov-{draft.id}"
        )


def _anthropic_mock(responses: list[str]) -> MagicMock:
    """Produce a mock AsyncAnthropic.messages.create that returns queued
    responses in order."""
    it = iter(responses)

    async def side_effect(*_args, **_kwargs):
        block = MagicMock()
        block.text = next(it)
        resp = MagicMock()
        resp.content = [block]
        return resp

    return AsyncMock(side_effect=side_effect)


@pytest.mark.asyncio
async def test_end_to_end_happy_path(
    initialized_db: ThreadDatabase, test_config, test_keys, monkeypatch
) -> None:
    db = initialized_db

    inbound = RawInboundMessage(
        provider="fake",
        provider_message_id="p-1",
        message_id_header="<inb-1@example.com>",
        from_addr="cust@example.com",
        from_name="Cust",
        subject="Can I get pricing?",
        body_text="Hi, interested in your services. What does it cost?",
        received_at=now_utc(),
    )

    monkeypatch.setattr(
        "custcomm.service.build_inbox",
        lambda c, k: FakeInbox(c, k, [inbound]),
    )

    poll_result = await poll_inbox(test_config, test_keys, db)
    assert poll_result["messages_ingested"] == 1
    assert poll_result["new_threads"] == 1

    threads = await db.list_threads(status=ThreadStatus.NEW)
    assert len(threads) == 1

    # Triage — build a factory that mocks the classifier's Anthropic call.
    # NOTE: do NOT monkeypatch the module-level IntentClassifier here —
    # the factory would recursively call itself. We just pass the factory
    # as classifier_cls; service.triage_threads uses exactly what it's given.
    from custcomm.ai.classifier import IntentClassifier as RealClassifier

    def _patched_classifier(config, keys):
        c = RealClassifier(config, keys)
        c.client.messages.create = _anthropic_mock(
            ['{"intent":"new_inquiry","confidence":0.9,"reasoning":"pricing question"}']
        )
        return c

    counts = await triage_threads(
        test_config, test_keys, db, classifier_cls=_patched_classifier
    )
    assert counts.get("new_inquiry") == 1

    threads = await db.list_threads(status=ThreadStatus.TRIAGED)
    assert len(threads) == 1
    assert threads[0].intent == Intent.NEW_INQUIRY

    # Draft — same pattern: capture the real class, wrap it in a factory.
    from custcomm.ai.drafter import ReplyDrafter as RealDrafter

    def _patched_drafter(config, keys):
        d = RealDrafter(config, keys)
        d.client.messages.create = _anthropic_mock(
            ['{"subject":"Re: Can I get pricing?","body":"Happy to chat — our pricing varies."}']
        )
        return d

    drafts = await draft_replies(test_config, test_keys, db, drafter_cls=_patched_drafter)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.status == DraftStatus.PENDING
    assert "pricing" in d.body.lower()

    # Approve
    approved = await approve_draft(db, d.thread_id, approved_by="test")
    assert approved is not None
    assert approved.status == DraftStatus.APPROVED

    # Send — monkey-patch outreach builder to return the FakeSender
    monkeypatch.setattr(
        "custcomm.service.build_sender",
        lambda c, k: FakeSender(c, k),
    )
    result = await send_approved(test_config, test_keys, db)
    assert result["sent"] == 1
    assert result["errors"] == 0

    # Thread is now AWAITING_CUSTOMER, draft is SENT, and the outbound
    # message was persisted.
    thread = await db.get_thread(d.thread_id)
    assert thread is not None and thread.status == ThreadStatus.AWAITING_CUSTOMER
    final_draft = await db.get_draft(d.id)
    assert final_draft is not None and final_draft.status == DraftStatus.SENT

    messages = await db.get_messages(d.thread_id)
    assert any(m.direction == MessageDirection.OUTBOUND for m in messages)


@pytest.mark.asyncio
async def test_send_refuses_when_require_approval_disabled(
    initialized_db, test_config, test_keys
) -> None:
    test_config.outreach.require_approval = False
    with pytest.raises(RuntimeError):
        await send_approved(test_config, test_keys, initialized_db)


@pytest.mark.asyncio
async def test_draft_skips_uncertain_and_complaint(
    initialized_db, test_config, test_keys
) -> None:
    db = initialized_db
    from custcomm.models import Customer, Message, Thread

    customer = await db.upsert_customer(Customer(email="u@example.com"))
    for intent in (Intent.UNCERTAIN, Intent.COMPLAINT, Intent.UNRELATED):
        t = Thread(
            customer_id=customer.id,
            subject=f"t-{intent.value}",
            status=ThreadStatus.TRIAGED,
            intent=intent,
        )
        await db.upsert_thread(t)
        await db.insert_message(
            Message(
                thread_id=t.id,
                customer_id=customer.id,
                direction=MessageDirection.INBOUND,
                from_addr="u@example.com",
                body_text="hi",
                message_id_header=f"<m-{intent.value}@x>",
                received_at=now_utc(),
            )
        )

    drafts = await draft_replies(test_config, test_keys, db)
    assert drafts == []
