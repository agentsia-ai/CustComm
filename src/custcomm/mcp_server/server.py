"""CustComm MCP Server.

Exposes CustComm as an MCP tool server so Claude Desktop (or any MCP client)
can triage, draft, approve, and send customer replies conversationally.

Usage:
    custcomm mcp
    # or: python -m custcomm.mcp

See docs/MCP_SETUP.md for the Claude Desktop configuration block.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from custcomm.ai.appointments import AppointmentHandler
from custcomm.ai.classifier import IntentClassifier
from custcomm.ai.drafter import ReplyDrafter
from custcomm.config.loader import load_api_keys, load_config
from custcomm.crm.database import ThreadDatabase
from custcomm.models import (
    Intent,
    MessageDirection,
    ThreadStatus,
    TimeSlot,
)
from custcomm.service import (
    approve_draft,
    draft_replies,
    poll_inbox,
    send_approved,
    triage_threads,
)

logger = logging.getLogger(__name__)

# ── Server init ───────────────────────────────────────────────────────────────

app = Server("custcomm")

# Config / keys / db are initialized in main() rather than at import time so
# the cwd has had a chance to be set by the caller. When a productized agent
# runtime like agentsia-core's AgentContext.activate() chdir's into
# agents/<agent>/ before invoking main(), loading config here ensures the
# engine sees the client-specific config.yaml. Loading at module import would
# pick up whatever happened to be cwd when Python first imported the module.
config = None  # type: ignore[assignment]
keys = None    # type: ignore[assignment]
db = None      # type: ignore[assignment]


# ── Pluggable class seam ──────────────────────────────────────────────────────
# Defaults = generic engine classes. Downstream productized agents (e.g.
# agentsia-core's ARIA) override by passing *_cls= kwargs to main(), which
# sets the module-level globals BEFORE the server starts handling tool calls.
# Tool handlers below read these globals at request time so injection takes
# effect for every subsequent call.
INTENT_CLASSIFIER_CLASS: type[IntentClassifier] = IntentClassifier
REPLY_DRAFTER_CLASS: type[ReplyDrafter] = ReplyDrafter
APPOINTMENT_HANDLER_CLASS: type[AppointmentHandler] = AppointmentHandler


# ── Tool definitions ──────────────────────────────────────────────────────────


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="pipeline_summary",
            description=(
                "Summary of the CustComm pipeline: thread counts by status, "
                "thread counts by intent, and any drafts pending approval."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="list_threads",
            description=(
                "List threads filtered by status and/or intent. Use this to "
                "find work: e.g. status='triaged' to see what needs drafting."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Thread status filter"},
                    "intent": {"type": "string", "description": "Intent filter"},
                    "customer_email": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max rows (default 25)"},
                },
            },
        ),
        Tool(
            name="get_thread",
            description="Get a thread's full detail: messages, pending draft, and metadata.",
            inputSchema={
                "type": "object",
                "properties": {"thread_id": {"type": "string"}},
                "required": ["thread_id"],
            },
        ),
        Tool(
            name="poll_inbox",
            description=(
                "Pull new inbound messages from the configured inbox backend "
                "and persist them (matched to existing threads where possible)."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="triage_thread",
            description=(
                "Classify intent for NEW threads. With thread_ids provided, "
                "classify only those. Without, classify all NEW threads."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        Tool(
            name="draft_reply",
            description=(
                "Generate a reply draft for TRIAGED thread(s) whose intent is "
                "auto-draftable. Drafts status=PENDING until approved. Does NOT send."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_ids": {"type": "array", "items": {"type": "string"}},
                    "guidance": {
                        "type": "string",
                        "description": "Optional operator guidance for this draft run.",
                    },
                },
            },
        ),
        Tool(
            name="regenerate_draft",
            description=(
                "Discard a thread's pending draft and generate a new one. "
                "Optionally pass guidance to steer the rewrite."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "guidance": {"type": "string"},
                },
                "required": ["thread_id"],
            },
        ),
        Tool(
            name="approve_reply",
            description=(
                "Approve a thread's pending draft. Does NOT send — use "
                "send_approved to actually deliver."
            ),
            inputSchema={
                "type": "object",
                "properties": {"thread_id": {"type": "string"}},
                "required": ["thread_id"],
            },
        ),
        Tool(
            name="send_approved",
            description=(
                "Send drafts whose status is APPROVED. With thread_id, sends "
                "just that thread. Without, sends all approved drafts, up to "
                "the daily_send_limit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                },
            },
        ),
        Tool(
            name="update_thread_status",
            description="Change a thread's status (snoozed, closed, spam, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "status": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["thread_id", "status"],
            },
        ),
        Tool(
            name="propose_appointment",
            description=(
                "Propose appointment slot(s) as a reply to the customer. Stores "
                "the appointment as PROPOSED and drafts the proposal reply."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "slots": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start_at": {"type": "string", "description": "ISO datetime"},
                                "end_at": {"type": "string", "description": "ISO datetime"},
                                "timezone": {"type": "string"},
                            },
                            "required": ["start_at", "end_at"],
                        },
                    },
                    "location_or_link": {"type": "string"},
                },
                "required": ["thread_id", "slots"],
            },
        ),
        Tool(
            name="confirm_appointment",
            description="Mark a PROPOSED appointment CONFIRMED.",
            inputSchema={
                "type": "object",
                "properties": {"appointment_id": {"type": "string"}},
                "required": ["appointment_id"],
            },
        ),
        Tool(
            name="reschedule_appointment",
            description="Move an appointment to a new time.",
            inputSchema={
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "new_start_at": {"type": "string"},
                    "new_end_at": {"type": "string"},
                },
                "required": ["appointment_id", "new_start_at", "new_end_at"],
            },
        ),
        Tool(
            name="escalate_to_operator",
            description="Force a thread into status=ESCALATED (no auto-drafts).",
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["thread_id"],
            },
        ),
        Tool(
            name="get_customer",
            description="Look up a customer by email or id, with all their threads.",
            inputSchema={
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "email": {"type": "string"},
                },
            },
        ),
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────


def _json(obj: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(obj, indent=2, default=str))]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    await db.init()

    if name == "pipeline_summary":
        status_counts = await db.count_threads_by_status()
        intent_counts = await db.count_threads_by_intent()
        pending = len(await db.list_drafts(status=None, limit=500))
        pending_approval = [
            d for d in await db.list_drafts(status=None, limit=500)
            if d.status.value == "pending"
        ]
        return _json(
            {
                "status_counts": status_counts,
                "intent_counts": intent_counts,
                "drafts_total": pending,
                "pending_approval": len(pending_approval),
            }
        )

    elif name == "list_threads":
        status = ThreadStatus(arguments["status"]) if arguments.get("status") else None
        intent = Intent(arguments["intent"]) if arguments.get("intent") else None
        customer_id = None
        if arguments.get("customer_email"):
            cust = await db.get_customer_by_email(arguments["customer_email"])
            customer_id = cust.id if cust else "_nomatch_"
        threads = await db.list_threads(
            status=status,
            intent=intent,
            customer_id=customer_id,
            limit=arguments.get("limit", 25),
        )
        return _json(
            [
                {
                    "id": t.id,
                    "status": t.status.value,
                    "intent": t.intent.value if t.intent else None,
                    "subject": t.subject,
                    "last_inbound_at": t.last_inbound_at,
                    "last_outbound_at": t.last_outbound_at,
                    "customer_id": t.customer_id,
                }
                for t in threads
            ]
        )

    elif name == "get_thread":
        thread = await db.get_thread(arguments["thread_id"])
        if not thread:
            return _json({"error": "Thread not found"})
        messages = await db.get_messages(thread.id)
        draft = await db.get_pending_draft_for_thread(thread.id)
        customer = await db.get_customer(thread.customer_id)
        return _json(
            {
                "thread": thread.model_dump(),
                "customer": customer.model_dump() if customer else None,
                "messages": [m.model_dump() for m in messages],
                "pending_draft": draft.model_dump() if draft else None,
            }
        )

    elif name == "poll_inbox":
        result = await poll_inbox(config, keys, db)
        return _json(result)

    elif name == "triage_thread":
        counts = await triage_threads(
            config, keys, db,
            classifier_cls=INTENT_CLASSIFIER_CLASS,
            thread_ids=arguments.get("thread_ids"),
        )
        return _json(counts)

    elif name == "draft_reply":
        drafts = await draft_replies(
            config, keys, db,
            drafter_cls=REPLY_DRAFTER_CLASS,
            thread_ids=arguments.get("thread_ids"),
            guidance=arguments.get("guidance", ""),
        )
        return _json(
            [
                {
                    "draft_id": d.id,
                    "thread_id": d.thread_id,
                    "subject": d.subject,
                    "body": d.body,
                    "intent": d.intent_at_time_of_draft.value,
                }
                for d in drafts
            ]
        )

    elif name == "regenerate_draft":
        thread_id = arguments["thread_id"]
        drafts = await draft_replies(
            config, keys, db,
            drafter_cls=REPLY_DRAFTER_CLASS,
            thread_ids=[thread_id],
            guidance=arguments.get("guidance", ""),
        )
        if not drafts:
            return _json({"error": "Thread is not eligible for auto-drafting."})
        d = drafts[0]
        return _json(
            {
                "draft_id": d.id,
                "thread_id": d.thread_id,
                "subject": d.subject,
                "body": d.body,
                "supersedes_draft_id": d.supersedes_draft_id,
            }
        )

    elif name == "approve_reply":
        draft = await approve_draft(
            db, arguments["thread_id"], approved_by="mcp"
        )
        if not draft:
            return _json({"error": "No pending draft to approve on that thread."})
        return _json(
            {
                "approved": True,
                "draft_id": draft.id,
                "thread_id": draft.thread_id,
            }
        )

    elif name == "send_approved":
        result = await send_approved(
            config, keys, db,
            thread_id=arguments.get("thread_id"),
            dry_run=bool(arguments.get("dry_run", False)),
        )
        return _json(result)

    elif name == "update_thread_status":
        thread = await db.get_thread(arguments["thread_id"])
        if not thread:
            return _json({"error": "Thread not found"})
        thread.status = ThreadStatus(arguments["status"])
        if arguments.get("notes"):
            thread.notes = (thread.notes + "\n" + arguments["notes"]).strip()
        await db.upsert_thread(thread)
        return _json({"updated": True, "status": thread.status.value})

    elif name == "propose_appointment":
        from custcomm.scheduler.appointments import AppointmentStore

        thread = await db.get_thread(arguments["thread_id"])
        if not thread:
            return _json({"error": "Thread not found"})
        store = AppointmentStore(config, db)
        created = []
        for slot in arguments.get("slots", []):
            appt = await store.propose(
                thread_id=thread.id,
                customer_id=thread.customer_id,
                start_at=datetime.fromisoformat(slot["start_at"]),
                end_at=datetime.fromisoformat(slot["end_at"]),
                timezone=slot.get("timezone", "UTC"),
                location_or_link=arguments.get("location_or_link"),
            )
            created.append(appt.model_dump())
        # Attach the first proposed appointment to the thread for convenience.
        if created:
            thread.appointment_id = created[0]["id"]
            await db.upsert_thread(thread)
        return _json({"proposed": len(created), "appointments": created})

    elif name == "confirm_appointment":
        from custcomm.scheduler.appointments import AppointmentStore

        store = AppointmentStore(config, db)
        appt = await store.confirm(arguments["appointment_id"])
        if not appt:
            return _json({"error": "Appointment not found"})
        return _json({"confirmed": True, "appointment": appt.model_dump()})

    elif name == "reschedule_appointment":
        from custcomm.scheduler.appointments import AppointmentStore

        store = AppointmentStore(config, db)
        appt = await store.reschedule(
            arguments["appointment_id"],
            datetime.fromisoformat(arguments["new_start_at"]),
            datetime.fromisoformat(arguments["new_end_at"]),
        )
        if not appt:
            return _json({"error": "Appointment not found"})
        return _json({"rescheduled": True, "appointment": appt.model_dump()})

    elif name == "escalate_to_operator":
        thread = await db.get_thread(arguments["thread_id"])
        if not thread:
            return _json({"error": "Thread not found"})
        thread.status = ThreadStatus.ESCALATED
        thread.escalation_reason = arguments.get("reason", "manual")
        await db.upsert_thread(thread)
        return _json({"escalated": True})

    elif name == "get_customer":
        cust = None
        if arguments.get("customer_id"):
            cust = await db.get_customer(arguments["customer_id"])
        elif arguments.get("email"):
            cust = await db.get_customer_by_email(arguments["email"])
        if not cust:
            return _json({"error": "Customer not found"})
        threads = await db.list_threads(customer_id=cust.id, limit=100)
        return _json(
            {
                "customer": cust.model_dump(),
                "threads": [
                    {
                        "id": t.id,
                        "subject": t.subject,
                        "status": t.status.value,
                        "intent": t.intent.value if t.intent else None,
                    }
                    for t in threads
                ],
            }
        )

    return _json({"error": f"Unknown tool: {name}"})


# ── Entry point ───────────────────────────────────────────────────────────────


async def main(
    intent_classifier_cls: type[IntentClassifier] | None = None,
    reply_drafter_cls: type[ReplyDrafter] | None = None,
    appointment_handler_cls: type[AppointmentHandler] | None = None,
) -> None:
    """Start the MCP server.

    Optionally inject persona-specific subclasses (e.g. AriaIntentClassifier,
    AriaReplyDrafter). Defaults use the generic engine classes.
    """
    global INTENT_CLASSIFIER_CLASS, REPLY_DRAFTER_CLASS, APPOINTMENT_HANDLER_CLASS
    global config, keys, db

    if intent_classifier_cls is not None:
        INTENT_CLASSIFIER_CLASS = intent_classifier_cls
        logger.info(
            f"MCP intent classifier overridden: "
            f"{intent_classifier_cls.__module__}.{intent_classifier_cls.__name__}"
        )
    if reply_drafter_cls is not None:
        REPLY_DRAFTER_CLASS = reply_drafter_cls
        logger.info(
            f"MCP reply drafter overridden: "
            f"{reply_drafter_cls.__module__}.{reply_drafter_cls.__name__}"
        )
    if appointment_handler_cls is not None:
        APPOINTMENT_HANDLER_CLASS = appointment_handler_cls
        logger.info(
            f"MCP appointment handler overridden: "
            f"{appointment_handler_cls.__module__}.{appointment_handler_cls.__name__}"
        )

    # Load config/keys/db now that cwd is final. Doing this here (instead of
    # at module import) is what lets an agent runtime chdir into a client-
    # specific directory before the engine reads config.yaml.
    config = load_config()
    keys = load_api_keys()
    db = ThreadDatabase(config.database.sqlite_path)

    # Route ALL logs to stderr so stdout stays sacred for JSON-RPC frames.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    logger.info("Starting CustComm MCP server...")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


# Silence TimeSlot import-unused warning from our own typing hint (mypy quirk).
_ = TimeSlot  # noqa: F401


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
