"""ThreadHistoryBuilder tests."""

from __future__ import annotations

from datetime import timedelta
from typing import List
from unittest.mock import AsyncMock

import pytest

from custcomm._time import now_utc
from custcomm.conversation.history import ThreadHistoryBuilder
from custcomm.crm.database import ThreadDatabase
from custcomm.models import (
    Customer,
    Message,
    MessageDirection,
    Thread,
)


async def _seed(db: ThreadDatabase, n_messages: int) -> tuple[Thread, List[Message]]:
    customer = await db.upsert_customer(Customer(email="h@example.com"))
    thread = Thread(customer_id=customer.id, subject="history")
    await db.upsert_thread(thread)
    msgs: list[Message] = []
    base = now_utc() - timedelta(days=30)
    for i in range(n_messages):
        m = Message(
            thread_id=thread.id,
            customer_id=customer.id,
            direction=(
                MessageDirection.INBOUND if i % 2 == 0 else MessageDirection.OUTBOUND
            ),
            from_addr="h@example.com",
            subject="history",
            body_text=f"message {i}",
            message_id_header=f"<m-{i}@example.com>",
            received_at=base + timedelta(hours=i),
        )
        await db.insert_message(m)
        msgs.append(m)
    return thread, msgs


@pytest.mark.asyncio
async def test_history_under_window_includes_all(
    initialized_db: ThreadDatabase, test_config
) -> None:
    db = initialized_db
    test_config.history.full_messages_kept = 5
    test_config.history.summarize_at_messages = 10
    builder = ThreadHistoryBuilder(test_config, db)
    thread, _msgs = await _seed(db, 3)
    view = await builder.build(thread, summarizer=None)
    assert len(view.full_messages) == 3
    assert view.summary == ""


@pytest.mark.asyncio
async def test_history_over_window_slices_and_invokes_summarizer(
    initialized_db: ThreadDatabase, test_config
) -> None:
    db = initialized_db
    test_config.history.full_messages_kept = 3
    test_config.history.summarize_at_messages = 5
    builder = ThreadHistoryBuilder(test_config, db)
    thread, _msgs = await _seed(db, 7)

    summarizer = AsyncMock()
    summarizer.summarize_history = AsyncMock(return_value="SUMMARIZED")

    view = await builder.build(thread, summarizer=summarizer)
    assert len(view.full_messages) == 3
    assert view.summary == "SUMMARIZED"
    # Assert we summarized the 4 older messages
    call_args = summarizer.summarize_history.call_args
    assert len(call_args.args[0]) == 4


@pytest.mark.asyncio
async def test_history_view_format_for_prompt(
    initialized_db: ThreadDatabase, test_config
) -> None:
    db = initialized_db
    thread, _ = await _seed(db, 2)
    builder = ThreadHistoryBuilder(test_config, db)
    view = await builder.build(thread, summarizer=None)
    prompt = view.format_for_prompt(operator_name="Alex")
    assert "Thread subject" in prompt
    assert "Recent messages" in prompt
    assert "message 0" in prompt
