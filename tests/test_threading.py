"""Thread resolution + subject normalization tests."""

from __future__ import annotations

import pytest

from custcomm._time import now_utc
from custcomm.conversation.threading import normalize_subject, resolve_thread_id
from custcomm.crm.database import ThreadDatabase
from custcomm.models import (
    Customer,
    Message,
    MessageDirection,
    RawInboundMessage,
    Thread,
    ThreadStatus,
)


def test_normalize_subject_strips_prefixes() -> None:
    assert normalize_subject("Re: Hello") == "hello"
    assert normalize_subject("RE: re: Fwd:   Hello World") == "hello world"
    assert normalize_subject("Hello World") == "hello world"
    assert normalize_subject("") == ""


def test_normalize_subject_collapses_whitespace() -> None:
    assert normalize_subject("  Re:  multiple   spaces  ") == "multiple spaces"


@pytest.mark.asyncio
async def test_resolve_thread_by_in_reply_to(initialized_db: ThreadDatabase) -> None:
    db = initialized_db
    customer = await db.upsert_customer(
        Customer(email="jane@example.com", display_name="Jane")
    )
    thread = Thread(
        customer_id=customer.id, subject="Order inquiry", status=ThreadStatus.NEW
    )
    await db.upsert_thread(thread)
    await db.insert_message(
        Message(
            thread_id=thread.id,
            customer_id=customer.id,
            direction=MessageDirection.INBOUND,
            from_addr="jane@example.com",
            subject="Order inquiry",
            body_text="hi",
            message_id_header="<msg-1@example.com>",
            received_at=now_utc(),
        )
    )

    raw = RawInboundMessage(
        provider="gmail",
        provider_message_id="g-2",
        message_id_header="<msg-2@example.com>",
        in_reply_to_header="<msg-1@example.com>",
        from_addr="jane@example.com",
        subject="Re: Order inquiry",
    )
    tid = await resolve_thread_id(raw, customer.id, db)
    assert tid == thread.id


@pytest.mark.asyncio
async def test_resolve_thread_falls_back_to_subject(
    initialized_db: ThreadDatabase,
) -> None:
    db = initialized_db
    customer = await db.upsert_customer(
        Customer(email="bob@example.com")
    )
    thread = Thread(
        customer_id=customer.id,
        subject="product question",
        status=ThreadStatus.AWAITING_CUSTOMER,
    )
    await db.upsert_thread(thread)

    raw = RawInboundMessage(
        provider="gmail",
        provider_message_id="g-5",
        from_addr="bob@example.com",
        subject="Re:  Product Question",
    )
    tid = await resolve_thread_id(raw, customer.id, db)
    assert tid == thread.id


@pytest.mark.asyncio
async def test_resolve_thread_returns_none_for_brand_new(
    initialized_db: ThreadDatabase,
) -> None:
    db = initialized_db
    customer = await db.upsert_customer(Customer(email="new@example.com"))

    raw = RawInboundMessage(
        provider="gmail",
        provider_message_id="g-99",
        from_addr="new@example.com",
        subject="Something totally new",
    )
    tid = await resolve_thread_id(raw, customer.id, db)
    assert tid is None
