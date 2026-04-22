"""CustComm CRM — async SQLite store.

Tables: customers, threads, messages, drafts, appointments.
JSON blobs hold list-valued fields; denormalized columns enable fast filtering.

The double-send interlock lives in `mark_draft_sent` — an atomic UPDATE
guarded by both status and approval_token. If a CLI and MCP race, exactly
one wins; the other affects 0 rows and is logged as skipped.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from custcomm.models import (
    Appointment,
    AppointmentStatus,
    AttachmentRef,
    Customer,
    Draft,
    DraftStatus,
    Intent,
    Message,
    MessageChannel,
    MessageDirection,
    Thread,
    ThreadStatus,
)

logger = logging.getLogger(__name__)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class ThreadDatabase:
    """Async SQLite-backed store for all CustComm entities."""

    def __init__(self, db_path: str = "./data/custcomm.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # ── schema ────────────────────────────────────────────────────────────────

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    phone TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    notes TEXT DEFAULT '',
                    tags_json TEXT DEFAULT '[]'
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    subject TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    intent TEXT,
                    intent_confidence REAL,
                    intent_reasoning TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    snoozed_until TEXT,
                    escalation_reason TEXT DEFAULT '',
                    last_inbound_at TEXT,
                    last_outbound_at TEXT,
                    next_followup_at TEXT,
                    appointment_id TEXT,
                    tags_json TEXT DEFAULT '[]',
                    notes TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_threads_customer ON threads(customer_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_threads_followup ON threads(next_followup_at)"
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'email',
                    from_addr TEXT DEFAULT '',
                    to_addrs_json TEXT DEFAULT '[]',
                    cc_addrs_json TEXT DEFAULT '[]',
                    subject TEXT DEFAULT '',
                    body_text TEXT DEFAULT '',
                    body_html TEXT,
                    message_id_header TEXT,
                    in_reply_to_header TEXT,
                    references_json TEXT DEFAULT '[]',
                    received_at TEXT,
                    sent_at TEXT,
                    raw_data_json TEXT DEFAULT '{}',
                    attachment_log_json TEXT DEFAULT '[]'
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id)"
            )
            # message_id_header is the dedup key. Not every provider hands
            # one over (e.g. some internal-relay bots), so we allow NULL
            # and enforce uniqueness conditionally.
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_header
                ON messages(message_id_header)
                WHERE message_id_header IS NOT NULL
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    subject TEXT DEFAULT '',
                    body TEXT DEFAULT '',
                    generated_at TEXT NOT NULL,
                    generated_by TEXT DEFAULT 'ReplyDrafter',
                    intent_at_time_of_draft TEXT,
                    approval_token TEXT NOT NULL,
                    approved_at TEXT,
                    approved_by TEXT,
                    sent_at TEXT,
                    sent_message_id TEXT,
                    supersedes_draft_id TEXT
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_drafts_thread ON drafts(thread_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status)"
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS appointments (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    timezone TEXT DEFAULT 'UTC',
                    location_or_link TEXT,
                    notes TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_appointments_thread ON appointments(thread_id)"
            )

            await db.commit()
        logger.info(f"Database initialized: {self.db_path}")

    # ── customers ─────────────────────────────────────────────────────────────

    async def upsert_customer(self, customer: Customer) -> Customer:
        """Insert or update a customer, merging by normalized email.

        Returns the persisted customer (with `id` from the existing row if
        the email was already known).
        """
        customer.email = Customer.normalize_email(customer.email)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM customers WHERE email = ?", (customer.email,)
            ) as cur:
                existing = await cur.fetchone()

            if existing:
                # Merge: keep existing id, update last_seen_at / display_name if present
                customer.id = existing["id"]
                display = customer.display_name or existing["display_name"]
                phone = customer.phone or existing["phone"]
                last_seen = max(
                    customer.last_seen_at,
                    _parse_iso(existing["last_seen_at"]) or customer.last_seen_at,
                )
                await db.execute(
                    """
                    UPDATE customers
                       SET display_name = ?, phone = ?, last_seen_at = ?, tags_json = ?
                     WHERE id = ?
                    """,
                    (
                        display,
                        phone,
                        last_seen.isoformat(),
                        json.dumps(customer.tags),
                        customer.id,
                    ),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO customers
                      (id, email, display_name, phone, first_seen_at, last_seen_at, notes, tags_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        customer.id,
                        customer.email,
                        customer.display_name,
                        customer.phone,
                        customer.first_seen_at.isoformat(),
                        customer.last_seen_at.isoformat(),
                        customer.notes,
                        json.dumps(customer.tags),
                    ),
                )
            await db.commit()
        return customer

    async def get_customer_by_email(self, email: str) -> Optional[Customer]:
        email = Customer.normalize_email(email)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM customers WHERE email = ?", (email,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_customer(row) if row else None

    async def get_customer(self, customer_id: str) -> Optional[Customer]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM customers WHERE id = ?", (customer_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_customer(row) if row else None

    # ── threads ───────────────────────────────────────────────────────────────

    async def upsert_thread(self, thread: Thread) -> None:
        thread.touch()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id FROM threads WHERE id = ?", (thread.id,)
            ) as cur:
                existing = await cur.fetchone()

            row = _thread_to_row(thread)

            if existing:
                await db.execute(
                    """
                    UPDATE threads SET
                      customer_id=?, subject=?, status=?, intent=?, intent_confidence=?,
                      intent_reasoning=?, summary=?, snoozed_until=?, escalation_reason=?,
                      last_inbound_at=?, last_outbound_at=?, next_followup_at=?,
                      appointment_id=?, tags_json=?, notes=?, updated_at=?
                    WHERE id=?
                    """,
                    row[1:] + (thread.id,),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO threads
                      (id, customer_id, subject, status, intent, intent_confidence,
                       intent_reasoning, summary, snoozed_until, escalation_reason,
                       last_inbound_at, last_outbound_at, next_followup_at,
                       appointment_id, tags_json, notes, updated_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row + (thread.created_at.isoformat(),),
                )
            await db.commit()

    async def get_thread(self, thread_id: str) -> Optional[Thread]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM threads WHERE id = ?", (thread_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_thread(row) if row else None

    async def list_threads(
        self,
        status: Optional[ThreadStatus] = None,
        intent: Optional[Intent] = None,
        customer_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[Thread]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status.value)
        if intent:
            where.append("intent = ?")
            params.append(intent.value)
        if customer_id:
            where.append("customer_id = ?")
            params.append(customer_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            q = (
                f"SELECT * FROM threads {clause} "
                f"ORDER BY updated_at DESC LIMIT ?"
            )
            async with db.execute(q, (*params, limit)) as cur:
                rows = await cur.fetchall()
        return [_row_to_thread(r) for r in rows]

    async def count_threads_by_status(self) -> dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM threads GROUP BY status"
            ) as cur:
                return {status: count for status, count in await cur.fetchall()}

    async def count_threads_by_intent(self) -> dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT intent, COUNT(*) FROM threads "
                "WHERE intent IS NOT NULL GROUP BY intent"
            ) as cur:
                return {intent: count for intent, count in await cur.fetchall()}

    # ── messages ──────────────────────────────────────────────────────────────

    async def insert_message(self, message: Message) -> bool:
        """Insert a message. Returns True on insert, False on dedup skip.

        Dedup is by `message_id_header` when provided; otherwise we always insert.
        """
        async with aiosqlite.connect(self.db_path) as db:
            if message.message_id_header:
                async with db.execute(
                    "SELECT id FROM messages WHERE message_id_header = ?",
                    (message.message_id_header,),
                ) as cur:
                    if await cur.fetchone():
                        logger.debug(
                            f"Skipping duplicate message {message.message_id_header}"
                        )
                        return False

            await db.execute(
                """
                INSERT INTO messages
                  (id, thread_id, customer_id, direction, channel, from_addr,
                   to_addrs_json, cc_addrs_json, subject, body_text, body_html,
                   message_id_header, in_reply_to_header, references_json,
                   received_at, sent_at, raw_data_json, attachment_log_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    message.thread_id,
                    message.customer_id,
                    message.direction.value,
                    message.channel.value,
                    message.from_addr,
                    json.dumps(message.to_addrs),
                    json.dumps(message.cc_addrs),
                    message.subject,
                    message.body_text,
                    message.body_html,
                    message.message_id_header,
                    message.in_reply_to_header,
                    json.dumps(message.references_headers),
                    _iso(message.received_at),
                    _iso(message.sent_at),
                    json.dumps(message.raw_data),
                    json.dumps([a.model_dump() for a in message.attachment_log]),
                ),
            )
            await db.commit()
        return True

    async def get_messages(self, thread_id: str, limit: int = 200) -> list[Message]:
        """Return messages for a thread, oldest-first."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM messages WHERE thread_id = ? "
                "ORDER BY COALESCE(received_at, sent_at) ASC LIMIT ?",
                (thread_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_message(r) for r in rows]

    async def find_thread_by_message_header(
        self, message_id_header: str
    ) -> Optional[str]:
        """Given an RFC 5322 Message-ID, return the thread_id we stored it under
        (or None if we've never seen it)."""
        if not message_id_header:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT thread_id FROM messages WHERE message_id_header = ?",
                (message_id_header,),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def find_thread_by_subject(
        self,
        customer_id: str,
        normalized_subject: str,
        within_days: int = 90,
    ) -> Optional[str]:
        """Fallback thread matching: same customer + same normalized subject
        within the last N days."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id FROM threads
                 WHERE customer_id = ?
                   AND LOWER(TRIM(subject)) = ?
                   AND status NOT IN ('closed', 'spam')
                   AND julianday('now') - julianday(updated_at) <= ?
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (customer_id, normalized_subject.lower().strip(), within_days),
            ) as cur:
                row = await cur.fetchone()
        return row["id"] if row else None

    # ── drafts ────────────────────────────────────────────────────────────────

    async def insert_draft(self, draft: Draft, supersede_pending: bool = True) -> None:
        """Insert a draft. If `supersede_pending` is True, any PENDING draft on
        the same thread is marked DISCARDED first, and the new draft records
        the superseded id."""
        async with aiosqlite.connect(self.db_path) as db:
            if supersede_pending:
                async with db.execute(
                    "SELECT id FROM drafts WHERE thread_id = ? AND status = 'pending'",
                    (draft.thread_id,),
                ) as cur:
                    prior = await cur.fetchone()
                if prior:
                    draft.supersedes_draft_id = prior[0]
                    await db.execute(
                        "UPDATE drafts SET status = 'discarded' WHERE id = ?",
                        (prior[0],),
                    )
            await db.execute(
                """
                INSERT INTO drafts
                  (id, thread_id, status, subject, body, generated_at, generated_by,
                   intent_at_time_of_draft, approval_token, approved_at, approved_by,
                   sent_at, sent_message_id, supersedes_draft_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.id,
                    draft.thread_id,
                    draft.status.value,
                    draft.subject,
                    draft.body,
                    draft.generated_at.isoformat(),
                    draft.generated_by,
                    draft.intent_at_time_of_draft.value,
                    draft.approval_token,
                    _iso(draft.approved_at),
                    draft.approved_by,
                    _iso(draft.sent_at),
                    draft.sent_message_id,
                    draft.supersedes_draft_id,
                ),
            )
            await db.commit()

    async def get_draft(self, draft_id: str) -> Optional[Draft]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_draft(row) if row else None

    async def get_pending_draft_for_thread(self, thread_id: str) -> Optional[Draft]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM drafts WHERE thread_id = ? AND status = 'pending' "
                "ORDER BY generated_at DESC LIMIT 1",
                (thread_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_draft(row) if row else None

    async def list_drafts(
        self, status: Optional[DraftStatus] = None, limit: int = 100
    ) -> list[Draft]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if status:
                q = (
                    "SELECT * FROM drafts WHERE status = ? "
                    "ORDER BY generated_at DESC LIMIT ?"
                )
                args: tuple[Any, ...] = (status.value, limit)
            else:
                q = "SELECT * FROM drafts ORDER BY generated_at DESC LIMIT ?"
                args = (limit,)
            async with db.execute(q, args) as cur:
                rows = await cur.fetchall()
        return [_row_to_draft(r) for r in rows]

    async def approve_draft(
        self, draft_id: str, approved_by: str = "cli"
    ) -> bool:
        """Atomic approve. Returns True if this call flipped the status."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE drafts
                   SET status = 'approved',
                       approved_at = ?,
                       approved_by = ?
                 WHERE id = ? AND status = 'pending'
                """,
                (now, approved_by, draft_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def mark_draft_sent(
        self, draft_id: str, approval_token: str, sent_message_id: str | None
    ) -> bool:
        """Atomic send-guard. Only flips the row if status='approved' AND
        approval_token matches. Returns True if this call owned the send."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE drafts
                   SET status = 'sent',
                       sent_at = ?,
                       sent_message_id = ?
                 WHERE id = ?
                   AND status = 'approved'
                   AND approval_token = ?
                """,
                (now, sent_message_id, draft_id, approval_token),
            )
            await db.commit()
            return cur.rowcount > 0

    async def count_sent_today(self) -> int:
        """Count drafts sent since UTC midnight — used by the daily-send-limit
        guardrail."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM drafts "
                "WHERE status = 'sent' AND date(sent_at) = date('now')"
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    # ── appointments ──────────────────────────────────────────────────────────

    async def upsert_appointment(self, appointment: Appointment) -> None:
        appointment.updated_at = datetime.utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id FROM appointments WHERE id = ?", (appointment.id,)
            ) as cur:
                existing = await cur.fetchone()

            if existing:
                await db.execute(
                    """
                    UPDATE appointments SET
                      thread_id=?, customer_id=?, status=?, start_at=?, end_at=?,
                      timezone=?, location_or_link=?, notes=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        appointment.thread_id,
                        appointment.customer_id,
                        appointment.status.value,
                        appointment.start_at.isoformat(),
                        appointment.end_at.isoformat(),
                        appointment.timezone,
                        appointment.location_or_link,
                        appointment.notes,
                        appointment.updated_at.isoformat(),
                        appointment.id,
                    ),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO appointments
                      (id, thread_id, customer_id, status, start_at, end_at,
                       timezone, location_or_link, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        appointment.id,
                        appointment.thread_id,
                        appointment.customer_id,
                        appointment.status.value,
                        appointment.start_at.isoformat(),
                        appointment.end_at.isoformat(),
                        appointment.timezone,
                        appointment.location_or_link,
                        appointment.notes,
                        appointment.created_at.isoformat(),
                        appointment.updated_at.isoformat(),
                    ),
                )
            await db.commit()

    async def get_appointment(self, appointment_id: str) -> Optional[Appointment]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_appointment(row) if row else None


# ── row <-> model helpers ─────────────────────────────────────────────────────


def _row_to_customer(row: aiosqlite.Row) -> Customer:
    return Customer(
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        phone=row["phone"],
        first_seen_at=_parse_iso(row["first_seen_at"]) or datetime.utcnow(),
        last_seen_at=_parse_iso(row["last_seen_at"]) or datetime.utcnow(),
        notes=row["notes"] or "",
        tags=json.loads(row["tags_json"] or "[]"),
    )


def _thread_to_row(t: Thread) -> tuple[Any, ...]:
    return (
        t.id,
        t.customer_id,
        t.subject,
        t.status.value,
        t.intent.value if t.intent else None,
        t.intent_confidence,
        t.intent_reasoning,
        t.summary,
        _iso(t.snoozed_until),
        t.escalation_reason,
        _iso(t.last_inbound_at),
        _iso(t.last_outbound_at),
        _iso(t.next_followup_at),
        t.appointment_id,
        json.dumps(t.tags),
        t.notes,
        t.updated_at.isoformat(),
    )


def _row_to_thread(row: aiosqlite.Row) -> Thread:
    intent = row["intent"]
    return Thread(
        id=row["id"],
        customer_id=row["customer_id"],
        subject=row["subject"] or "",
        status=ThreadStatus(row["status"]),
        intent=Intent(intent) if intent else None,
        intent_confidence=row["intent_confidence"],
        intent_reasoning=row["intent_reasoning"] or "",
        summary=row["summary"] or "",
        snoozed_until=_parse_iso(row["snoozed_until"]),
        escalation_reason=row["escalation_reason"] or "",
        last_inbound_at=_parse_iso(row["last_inbound_at"]),
        last_outbound_at=_parse_iso(row["last_outbound_at"]),
        next_followup_at=_parse_iso(row["next_followup_at"]),
        appointment_id=row["appointment_id"],
        tags=json.loads(row["tags_json"] or "[]"),
        notes=row["notes"] or "",
        created_at=_parse_iso(row["created_at"]) or datetime.utcnow(),
        updated_at=_parse_iso(row["updated_at"]) or datetime.utcnow(),
    )


def _row_to_message(row: aiosqlite.Row) -> Message:
    return Message(
        id=row["id"],
        thread_id=row["thread_id"],
        customer_id=row["customer_id"],
        direction=MessageDirection(row["direction"]),
        channel=MessageChannel(row["channel"]),
        from_addr=row["from_addr"] or "",
        to_addrs=json.loads(row["to_addrs_json"] or "[]"),
        cc_addrs=json.loads(row["cc_addrs_json"] or "[]"),
        subject=row["subject"] or "",
        body_text=row["body_text"] or "",
        body_html=row["body_html"],
        message_id_header=row["message_id_header"],
        in_reply_to_header=row["in_reply_to_header"],
        references_headers=json.loads(row["references_json"] or "[]"),
        received_at=_parse_iso(row["received_at"]),
        sent_at=_parse_iso(row["sent_at"]),
        raw_data=json.loads(row["raw_data_json"] or "{}"),
        attachment_log=[
            AttachmentRef(**a) for a in json.loads(row["attachment_log_json"] or "[]")
        ],
    )


def _row_to_draft(row: aiosqlite.Row) -> Draft:
    return Draft(
        id=row["id"],
        thread_id=row["thread_id"],
        status=DraftStatus(row["status"]),
        subject=row["subject"] or "",
        body=row["body"] or "",
        generated_at=_parse_iso(row["generated_at"]) or datetime.utcnow(),
        generated_by=row["generated_by"] or "ReplyDrafter",
        intent_at_time_of_draft=Intent(row["intent_at_time_of_draft"])
        if row["intent_at_time_of_draft"]
        else Intent.UNCERTAIN,
        approval_token=row["approval_token"],
        approved_at=_parse_iso(row["approved_at"]),
        approved_by=row["approved_by"],
        sent_at=_parse_iso(row["sent_at"]),
        sent_message_id=row["sent_message_id"],
        supersedes_draft_id=row["supersedes_draft_id"],
    )


def _row_to_appointment(row: aiosqlite.Row) -> Appointment:
    return Appointment(
        id=row["id"],
        thread_id=row["thread_id"],
        customer_id=row["customer_id"],
        status=AppointmentStatus(row["status"]),
        start_at=_parse_iso(row["start_at"]) or datetime.utcnow(),
        end_at=_parse_iso(row["end_at"]) or datetime.utcnow(),
        timezone=row["timezone"] or "UTC",
        location_or_link=row["location_or_link"],
        notes=row["notes"] or "",
        created_at=_parse_iso(row["created_at"]) or datetime.utcnow(),
        updated_at=_parse_iso(row["updated_at"]) or datetime.utcnow(),
    )
