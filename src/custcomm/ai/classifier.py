"""CustComm Intent Classifier.

This is the generic engine implementation. To customize for a productized
agent (e.g. a named persona with a tuned triage rubric), either:
  1. Subclass `IntentClassifier` and override `SYSTEM_PROMPT` (and optionally
     `_build_user_prompt`), or
  2. Point `config.ai.intent_prompt_path` at an external prompt file.

See CLAUDE.md → Customization Patterns for details.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

import anthropic

from custcomm.config.loader import APIKeys, CustCommConfig
from custcomm.models import Intent, IntentResult, Message, Thread

logger = logging.getLogger(__name__)


DEFAULT_INTENT_PROMPT = """You are a customer-communications triage expert. Your job is to classify
a single inbound customer email into one of the following intent buckets:

  - new_inquiry          : a customer reaching out for the first time about a product/service
  - followup_question    : an existing customer asking for more info in an ongoing conversation
  - appointment_request  : asking to schedule/book/visit/meet
  - reschedule           : asking to move an existing appointment
  - cancel               : asking to cancel an appointment, subscription, or order
  - complaint            : expressing dissatisfaction or reporting a problem
  - unrelated            : spam, vendor pitch, notification, automated bounce, or otherwise
                           not something the operator needs to respond to
  - uncertain            : you cannot confidently choose any of the above

Return ONLY valid JSON — no preamble, no explanation outside the JSON — in this exact shape:
{
  "intent": "<one of the values above>",
  "confidence": <float between 0.0 and 1.0>,
  "reasoning": "<1–2 sentences explaining your choice>"
}

Be honest about confidence. If the message is ambiguous, short, or you lack context,
lower the confidence and use 'uncertain' when appropriate — the operator will review
anything flagged as uncertain."""


class IntentClassifier:
    """Classifies inbound messages against an intent taxonomy using Claude.

    Subclass this and override `SYSTEM_PROMPT` to define a tuned classifier
    with a custom rubric. Per-deployment overrides can also be supplied by
    setting `config.ai.intent_prompt_path` to a text file.
    """

    SYSTEM_PROMPT: str = DEFAULT_INTENT_PROMPT

    def __init__(self, config: CustCommConfig, keys: APIKeys) -> None:
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=keys.anthropic)
        self.model = config.ai.model
        self.min_confidence = config.ai.min_intent_confidence
        self._system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        """Resolve the active system prompt.

        Resolution order:
          1. `config.ai.intent_prompt_path` (if set and file exists)
          2. Class attribute `SYSTEM_PROMPT` (subclass-overridable)
        """
        override = self.config.ai.intent_prompt_path
        if override:
            path = Path(override)
            if path.exists():
                logger.info(f"{type(self).__name__} using prompt override: {path}")
                return path.read_text(encoding="utf-8")
            logger.warning(
                f"intent_prompt_path points at missing file: {path} — "
                f"falling back to {type(self).__name__}.SYSTEM_PROMPT"
            )
        return self.SYSTEM_PROMPT

    def _build_user_prompt(self, thread: Thread, message: Message) -> str:
        history_blurb = ""
        if thread.summary:
            history_blurb = f"\n\nEarlier-thread summary (for context):\n{thread.summary}"

        return f"""Classify the intent of this inbound customer email.

Thread subject: {thread.subject or "(no subject)"}
Thread status: {thread.status.value}
Message from: {message.from_addr}
Message subject: {message.subject}
Message body:
---
{message.body_text.strip()}
---{history_blurb}

Return only JSON."""

    async def classify(self, thread: Thread, message: Message) -> IntentResult:
        """Classify a single message. Never raises — parse failures fall through
        to Intent.UNCERTAIN."""
        prompt = self._build_user_prompt(thread, message)

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=400,
                system=self._system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text
            data = _parse_json_loosely(raw)

            intent = _coerce_intent(data.get("intent"))
            confidence = float(data.get("confidence", 0.0) or 0.0)
            if confidence < self.min_confidence and intent != Intent.UNCERTAIN:
                logger.info(
                    f"Classifier confidence {confidence:.2f} below floor "
                    f"{self.min_confidence} → forcing UNCERTAIN"
                )
                intent = Intent.UNCERTAIN

            return IntentResult(
                intent=intent,
                confidence=confidence,
                reasoning=str(data.get("reasoning") or ""),
                classified_at=datetime.utcnow(),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Classifier failed to parse response: {e}")
            return IntentResult(
                intent=Intent.UNCERTAIN,
                confidence=0.0,
                reasoning=f"Parse error: {e}",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Classifier API call failed: {e}")
            return IntentResult(
                intent=Intent.UNCERTAIN,
                confidence=0.0,
                reasoning=f"API error: {e}",
            )

    async def classify_batch(
        self, pairs: list[tuple[Thread, Message]]
    ) -> list[IntentResult]:
        """Classify in small batches (5 concurrent) to manage rate limits."""
        out: list[IntentResult] = []
        for i in range(0, len(pairs), 5):
            batch = pairs[i : i + 5]
            results = await asyncio.gather(
                *[self.classify(t, m) for t, m in batch]
            )
            out.extend(results)
        return out


# ── parsing helpers ──────────────────────────────────────────────────────────


def _parse_json_loosely(raw: str) -> dict:
    """Accept either a bare JSON object or a fenced ```json``` block."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    # Trim leading/trailing fences or language tags
    raw = raw.strip()
    return json.loads(raw)


def _coerce_intent(raw: object) -> Intent:
    if raw is None:
        return Intent.UNCERTAIN
    text = str(raw).strip().lower()
    try:
        return Intent(text)
    except ValueError:
        logger.warning(f"Classifier returned unknown intent {text!r} → UNCERTAIN")
        return Intent.UNCERTAIN
