"""ReplyDrafter behavior tests with mocked Anthropic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custcomm.ai.drafter import ReplyDrafter
from custcomm.conversation.history import ThreadHistoryView
from custcomm.models import Intent, Thread


def _mock_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


@pytest.mark.asyncio
async def test_drafter_returns_draft_with_signature(test_config, test_keys) -> None:
    d = ReplyDrafter(test_config, test_keys)
    d.client.messages.create = AsyncMock(
        return_value=_mock_response(
            '{"subject":"Re: hi","body":"Thanks for reaching out, I can help."}'
        )
    )

    thread = Thread(customer_id="c", subject="hi")
    view = ThreadHistoryView(thread=thread, full_messages=[], summary="")
    draft = await d.draft(view, Intent.NEW_INQUIRY)

    assert draft.subject == "Re: hi"
    assert "Thanks for reaching out" in draft.body
    assert "Thanks,\nTester" in draft.body  # signature applied
    assert draft.intent_at_time_of_draft == Intent.NEW_INQUIRY
    assert draft.status.value == "pending"
    assert draft.approval_token  # fresh token generated


@pytest.mark.asyncio
async def test_drafter_truncates_over_max_reply_chars(test_config, test_keys) -> None:
    test_config.ai.max_reply_chars = 50
    d = ReplyDrafter(test_config, test_keys)
    d.client.messages.create = AsyncMock(
        return_value=_mock_response(
            '{"subject":"Re: hi","body":"' + "x" * 500 + '"}'
        )
    )
    thread = Thread(customer_id="c", subject="hi")
    view = ThreadHistoryView(thread=thread, full_messages=[], summary="")
    draft = await d.draft(view, Intent.NEW_INQUIRY)
    assert "truncated" in draft.generated_by.lower()
    # Body length is trimmed to within the cap (signature appended after).
    # We only assert the truncation marker is present in the body.
    assert "…" in draft.body
