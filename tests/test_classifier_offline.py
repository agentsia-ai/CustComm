"""IntentClassifier behavior tests with mocked Anthropic client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custcomm.ai.classifier import IntentClassifier
from custcomm.models import Intent, Message, MessageDirection, Thread


def _mock_anthropic_response(text: str) -> MagicMock:
    content_block = MagicMock()
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    return response


@pytest.mark.asyncio
async def test_classifier_parses_clean_json(test_config, test_keys) -> None:
    c = IntentClassifier(test_config, test_keys)
    c.client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(
            '{"intent":"new_inquiry","confidence":0.92,"reasoning":"clear ask"}'
        )
    )

    thread = Thread(customer_id="cust-1", subject="hi")
    msg = Message(
        thread_id=thread.id,
        customer_id="cust-1",
        direction=MessageDirection.INBOUND,
        from_addr="user@x.com",
        body_text="Can you tell me more about your services?",
    )
    result = await c.classify(thread, msg)
    assert result.intent == Intent.NEW_INQUIRY
    assert 0.9 < result.confidence <= 1.0


@pytest.mark.asyncio
async def test_classifier_low_confidence_collapses_to_uncertain(
    test_config, test_keys
) -> None:
    test_config.ai.min_intent_confidence = 0.8
    c = IntentClassifier(test_config, test_keys)
    c.client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(
            '{"intent":"appointment_request","confidence":0.5,"reasoning":"fuzzy"}'
        )
    )

    thread = Thread(customer_id="cust-1", subject="hi")
    msg = Message(
        thread_id=thread.id,
        customer_id="cust-1",
        direction=MessageDirection.INBOUND,
        from_addr="user@x.com",
        body_text="maybe we can talk sometime?",
    )
    result = await c.classify(thread, msg)
    assert result.intent == Intent.UNCERTAIN


@pytest.mark.asyncio
async def test_classifier_handles_fenced_json(test_config, test_keys) -> None:
    c = IntentClassifier(test_config, test_keys)
    c.client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(
            '```json\n{"intent":"complaint","confidence":0.88,"reasoning":"angry"}\n```'
        )
    )

    thread = Thread(customer_id="cust-1", subject="hi")
    msg = Message(
        thread_id=thread.id,
        customer_id="cust-1",
        direction=MessageDirection.INBOUND,
        from_addr="user@x.com",
        body_text="This is terrible service.",
    )
    result = await c.classify(thread, msg)
    assert result.intent == Intent.COMPLAINT


@pytest.mark.asyncio
async def test_classifier_bad_json_returns_uncertain(
    test_config, test_keys
) -> None:
    c = IntentClassifier(test_config, test_keys)
    c.client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response("this is not JSON at all")
    )

    thread = Thread(customer_id="cust-1", subject="hi")
    msg = Message(
        thread_id=thread.id,
        customer_id="cust-1",
        direction=MessageDirection.INBOUND,
        from_addr="user@x.com",
        body_text="hello",
    )
    result = await c.classify(thread, msg)
    assert result.intent == Intent.UNCERTAIN
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_classifier_unknown_intent_coerces_to_uncertain(
    test_config, test_keys
) -> None:
    c = IntentClassifier(test_config, test_keys)
    c.client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(
            '{"intent":"refund_request","confidence":0.9,"reasoning":"x"}'
        )
    )

    thread = Thread(customer_id="cust-1", subject="hi")
    msg = Message(
        thread_id=thread.id,
        customer_id="cust-1",
        direction=MessageDirection.INBOUND,
        from_addr="user@x.com",
        body_text="I want my money back",
    )
    result = await c.classify(thread, msg)
    # Not in our taxonomy → UNCERTAIN
    assert result.intent == Intent.UNCERTAIN
