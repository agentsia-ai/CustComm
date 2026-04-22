"""ThreadDatabase tests — CRUD, dedup, approve/send interlock."""

from __future__ import annotations

from datetime import datetime

import pytest

from custcomm.crm.database import ThreadDatabase
from custcomm.models import (
    AttachmentRef,
    Customer,
    Draft,
    DraftStatus,
    Intent,
    Message,
    MessageDirection,
    Thread,
    ThreadStatus,
)


@pytest.mark.asyncio
async def test_upsert_customer_deduplicates_by_email(
    initialized_db: ThreadDatabase,
) -> None:
    db = initialized_db
    c1 = await db.upsert_customer(
        Customer(email="A@Example.com", display_name="A")
    )
    c2 = await db.upsert_customer(
        Customer(email="a@example.com", display_name="Also A")
    )
    assert c1.id == c2.id  # merged by normalized email
    fetched = await db.get_customer_by_email("A@EXAMPLE.COM")
    assert fetched is not None
    assert fetched.id == c1.id


@pytest.mark.asyncio
async def test_insert_message_dedups_by_header(
    initialized_db: ThreadDatabase,
) -> None:
    db = initialized_db
    customer = await db.upsert_customer(Customer(email="x@example.com"))
    thread = Thread(customer_id=customer.id, subject="hi")
    await db.upsert_thread(thread)
    msg = Message(
        thread_id=thread.id,
        customer_id=customer.id,
        direction=MessageDirection.INBOUND,
        from_addr="x@example.com",
        body_text="hello",
        message_id_header="<m-1@example.com>",
        received_at=datetime.utcnow(),
    )
    assert await db.insert_message(msg) is True

    dup = msg.model_copy(update={"id": "different-id"})
    assert await db.insert_message(dup) is False


@pytest.mark.asyncio
async def test_list_and_count_by_status(initialized_db: ThreadDatabase) -> None:
    db = initialized_db
    customer = await db.upsert_customer(Customer(email="y@example.com"))
    for i, status in enumerate(
        [ThreadStatus.NEW, ThreadStatus.NEW, ThreadStatus.TRIAGED]
    ):
        t = Thread(customer_id=customer.id, subject=f"t{i}", status=status)
        await db.upsert_thread(t)

    counts = await db.count_threads_by_status()
    assert counts.get("new") == 2
    assert counts.get("triaged") == 1

    news = await db.list_threads(status=ThreadStatus.NEW)
    assert len(news) == 2


@pytest.mark.asyncio
async def test_approve_and_send_interlock(initialized_db: ThreadDatabase) -> None:
    db = initialized_db
    customer = await db.upsert_customer(Customer(email="z@example.com"))
    thread = Thread(customer_id=customer.id, subject="q")
    await db.upsert_thread(thread)

    draft = Draft(
        thread_id=thread.id,
        subject="Re: q",
        body="Hello!",
        intent_at_time_of_draft=Intent.NEW_INQUIRY,
    )
    await db.insert_draft(draft)

    # PENDING cannot be sent yet
    assert await db.mark_draft_sent(draft.id, draft.approval_token, "<m@x>") is False

    # Approve
    assert await db.approve_draft(draft.id, approved_by="cli") is True

    # Wrong token fails
    assert await db.mark_draft_sent(draft.id, "wrong-token", "<m@x>") is False

    # Correct token wins exactly once
    assert await db.mark_draft_sent(draft.id, draft.approval_token, "<m@x>") is True
    assert await db.mark_draft_sent(draft.id, draft.approval_token, "<m@x>") is False


@pytest.mark.asyncio
async def test_insert_draft_supersedes_prior_pending(
    initialized_db: ThreadDatabase,
) -> None:
    db = initialized_db
    customer = await db.upsert_customer(Customer(email="s@example.com"))
    thread = Thread(customer_id=customer.id, subject="q")
    await db.upsert_thread(thread)

    d1 = Draft(thread_id=thread.id, subject="v1", body="a")
    await db.insert_draft(d1)
    d2 = Draft(thread_id=thread.id, subject="v2", body="b")
    await db.insert_draft(d2, supersede_pending=True)

    d1_after = await db.get_draft(d1.id)
    d2_after = await db.get_draft(d2.id)
    assert d1_after is not None and d1_after.status == DraftStatus.DISCARDED
    assert d2_after is not None and d2_after.status == DraftStatus.PENDING
    assert d2_after.supersedes_draft_id == d1.id


@pytest.mark.asyncio
async def test_attachment_round_trip(initialized_db: ThreadDatabase) -> None:
    db = initialized_db
    customer = await db.upsert_customer(Customer(email="att@example.com"))
    thread = Thread(customer_id=customer.id, subject="q")
    await db.upsert_thread(thread)

    m = Message(
        thread_id=thread.id,
        customer_id=customer.id,
        direction=MessageDirection.INBOUND,
        from_addr="att@example.com",
        body_text="see file",
        message_id_header="<att-1@example.com>",
        received_at=datetime.utcnow(),
        attachment_log=[
            AttachmentRef(filename="doc.pdf", mime_type="application/pdf", size_bytes=1024)
        ],
    )
    await db.insert_message(m)
    msgs = await db.get_messages(thread.id)
    assert len(msgs) == 1
    assert msgs[0].attachment_log[0].filename == "doc.pdf"
