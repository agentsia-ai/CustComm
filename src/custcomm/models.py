"""CustComm Core Data Models.

Central entities used across inbox, AI, outreach, scheduler, and CRM layers.
Every layer speaks in these objects — never raw dicts.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, EmailStr, Field

from custcomm._time import now_utc


# ── Enums ─────────────────────────────────────────────────────────────────────


class MessageDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageChannel(str, Enum):
    EMAIL = "email"
    # reserved: SMS, VOICE


class ThreadStatus(str, Enum):
    NEW = "new"                              # ingested, not yet triaged
    TRIAGED = "triaged"                      # classified, awaiting draft
    DRAFTED = "drafted"                      # reply drafted, awaiting approval
    APPROVED = "approved"                    # operator approved, awaiting send
    AWAITING_CUSTOMER = "awaiting_customer"  # we replied, waiting on them
    SNOOZED = "snoozed"                      # muted until snoozed_until
    ESCALATED = "escalated"                  # human-only
    CLOSED = "closed"                        # resolved
    SPAM = "spam"


class Intent(str, Enum):
    NEW_INQUIRY = "new_inquiry"
    FOLLOWUP_QUESTION = "followup_question"
    APPOINTMENT_REQUEST = "appointment_request"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    COMPLAINT = "complaint"
    UNRELATED = "unrelated"
    UNCERTAIN = "uncertain"

    @classmethod
    def values(cls) -> list[str]:
        return [i.value for i in cls]


class AppointmentStatus(str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class DraftStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    SENT = "sent"
    DISCARDED = "discarded"


# ── Nested value objects ──────────────────────────────────────────────────────


class AttachmentRef(BaseModel):
    """Metadata record for an attachment on an inbound message. CustComm v1
    never downloads or parses attachment content — we only record that it
    existed, so downstream review can surface it."""

    filename: str
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    stored: bool = False  # always False in v1


class IntentResult(BaseModel):
    intent: Intent = Intent.UNCERTAIN
    confidence: float = 0.0
    reasoning: str = ""
    classified_at: datetime = Field(default_factory=now_utc)


class TimeSlot(BaseModel):
    start_at: datetime
    end_at: datetime
    timezone: str = "UTC"


class AppointmentProposal(BaseModel):
    """Output of AppointmentHandler.propose() — what to offer the customer,
    plus the reply body containing those offers."""

    thread_id: str
    slots: list[TimeSlot] = []
    reply_subject: str = ""
    reply_body: str = ""


class AppointmentDecision(BaseModel):
    """Output of AppointmentHandler.interpret_customer_reply()."""

    kind: str                        # "confirm" | "reschedule" | "cancel" | "ambiguous"
    chosen_slot_index: Optional[int] = None  # when kind == "confirm"
    new_proposed_slots: list[TimeSlot] = []  # when kind == "reschedule"
    reasoning: str = ""


# ── Entities ──────────────────────────────────────────────────────────────────


class Customer(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    email: str                       # normalized lowercase (DB UNIQUE)
    display_name: Optional[str] = None
    phone: Optional[str] = None
    first_seen_at: datetime = Field(default_factory=now_utc)
    last_seen_at: datetime = Field(default_factory=now_utc)
    notes: str = ""
    tags: list[str] = []

    @staticmethod
    def normalize_email(raw: str) -> str:
        return raw.strip().lower()


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    thread_id: str
    customer_id: str
    direction: MessageDirection
    channel: MessageChannel = MessageChannel.EMAIL

    from_addr: str = ""
    to_addrs: list[str] = []
    cc_addrs: list[str] = []
    subject: str = ""

    body_text: str = ""
    body_html: Optional[str] = None

    # RFC 5322 identifiers — used for thread resolution.
    message_id_header: Optional[str] = None
    in_reply_to_header: Optional[str] = None
    references_headers: list[str] = []

    received_at: Optional[datetime] = None  # inbound
    sent_at: Optional[datetime] = None      # outbound

    raw_data: dict[str, Any] = {}           # full headers + provider metadata
    attachment_log: list[AttachmentRef] = []


class Draft(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    thread_id: str
    status: DraftStatus = DraftStatus.PENDING

    subject: str = ""
    body: str = ""

    generated_at: datetime = Field(default_factory=now_utc)
    generated_by: str = "ReplyDrafter"      # class name for audit
    intent_at_time_of_draft: Intent = Intent.UNCERTAIN

    approval_token: str = Field(default_factory=lambda: str(uuid4()))
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None       # free-form: "cli" / "mcp" / operator email

    sent_at: Optional[datetime] = None
    sent_message_id: Optional[str] = None   # the provider-assigned Message-ID header

    supersedes_draft_id: Optional[str] = None


class Appointment(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    thread_id: str
    customer_id: str
    status: AppointmentStatus = AppointmentStatus.PROPOSED

    start_at: datetime
    end_at: datetime
    timezone: str = "UTC"
    location_or_link: Optional[str] = None
    notes: str = ""

    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Thread(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    customer_id: str
    subject: str = ""                            # canonical, from the first message
    status: ThreadStatus = ThreadStatus.NEW

    intent: Optional[Intent] = None              # last classified intent
    intent_confidence: Optional[float] = None
    intent_reasoning: str = ""

    summary: str = ""                            # rolling compressed history

    snoozed_until: Optional[datetime] = None
    escalation_reason: str = ""

    last_inbound_at: Optional[datetime] = None
    last_outbound_at: Optional[datetime] = None
    next_followup_at: Optional[datetime] = None

    appointment_id: Optional[str] = None

    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    tags: list[str] = []
    notes: str = ""

    def touch(self) -> None:
        self.updated_at = now_utc()

    @property
    def needs_reply(self) -> bool:
        return self.status in (ThreadStatus.NEW, ThreadStatus.TRIAGED)


# ── Raw inbound (provider-neutral envelope) ───────────────────────────────────


class RawInboundMessage(BaseModel):
    """Provider-neutral inbound envelope produced by every InboxConnector.

    Kept deliberately flat so new connectors (IMAP, future Outlook Graph, etc.)
    can produce it without inventing their own intermediate shape.
    """

    provider: str                                # "gmail" | "imap" | ...
    provider_message_id: str                     # stable per-provider id
    message_id_header: Optional[str] = None
    in_reply_to_header: Optional[str] = None
    references_headers: list[str] = []

    from_addr: str
    from_name: Optional[str] = None
    to_addrs: list[str] = []
    cc_addrs: list[str] = []
    subject: str = ""

    body_text: str = ""
    body_html: Optional[str] = None

    received_at: datetime = Field(default_factory=now_utc)
    attachments: list[AttachmentRef] = []
    raw_headers: dict[str, Any] = {}


# Re-export EmailStr for downstream consumers (unused locally but common in
# persona repos that want typed email fields).
__all__ = [
    "MessageDirection",
    "MessageChannel",
    "ThreadStatus",
    "Intent",
    "AppointmentStatus",
    "DraftStatus",
    "AttachmentRef",
    "IntentResult",
    "TimeSlot",
    "AppointmentProposal",
    "AppointmentDecision",
    "Customer",
    "Message",
    "Draft",
    "Appointment",
    "Thread",
    "RawInboundMessage",
    "EmailStr",
]
