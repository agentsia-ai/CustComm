"""CustComm Appointment Handler.

Separate from the general ReplyDrafter because slot-matching and confirmation
phrasing are distinct product concerns. Booking is where voice matters most,
and where careful guardrails matter most (never invent availability).

Customize by subclassing and overriding `SYSTEM_PROMPT`, or by pointing at
an external prompt file via `config.ai.appointment_prompt_path`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import anthropic

from custcomm.config.loader import APIKeys, CustCommConfig
from custcomm.models import (
    AppointmentDecision,
    AppointmentProposal,
    Message,
    Thread,
    TimeSlot,
)

logger = logging.getLogger(__name__)


DEFAULT_APPOINTMENT_PROMPT = """You handle appointment-scheduling conversations on behalf of a small-business
operator. You do TWO kinds of tasks; the user message tells you which.

TASK A — Propose slots:
Given the thread context and a list of available time slots, write a reply
that offers the customer the slot(s). Rules:
  - NEVER offer times that aren't in the provided slot list.
  - Present at most 3 slots — don't overwhelm.
  - Be concise and friendly. Ask the customer to confirm which one works
    (or propose an alternative if none do).

Output JSON:
{
  "chosen_slot_indexes": [<indexes into the provided slots, 0-based>],
  "reply_subject": "<subject line>",
  "reply_body": "<reply body, plain text, no signature>"
}

TASK B — Interpret customer's reply to a prior proposal:
Given the customer's reply and the current proposal's slots, decide whether
the customer is confirming one, asking to reschedule, cancelling, or
ambiguous.

Output JSON:
{
  "kind": "confirm" | "reschedule" | "cancel" | "ambiguous",
  "chosen_slot_index": <int or null>,
  "reasoning": "<1 sentence>"
}

Return ONLY valid JSON. No preamble."""


class AppointmentHandler:
    """Proposes appointment slots and interprets customer confirmations.

    Subclass to override `SYSTEM_PROMPT` for a persona-specific voice, or
    use the config path override.
    """

    SYSTEM_PROMPT: str = DEFAULT_APPOINTMENT_PROMPT

    def __init__(self, config: CustCommConfig, keys: APIKeys) -> None:
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=keys.anthropic)
        self.model = config.ai.model
        self._system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        """Resolution order:
          1. `config.ai.appointment_prompt_path` (if set and file exists)
          2. Class attribute `SYSTEM_PROMPT` (subclass-overridable)
        """
        override = self.config.ai.appointment_prompt_path
        if override:
            path = Path(override)
            if path.exists():
                logger.info(f"{type(self).__name__} using prompt override: {path}")
                return path.read_text(encoding="utf-8")
            logger.warning(
                f"appointment_prompt_path points at missing file: {path} — "
                f"falling back to {type(self).__name__}.SYSTEM_PROMPT"
            )
        return self.SYSTEM_PROMPT

    async def propose(
        self,
        thread: Thread,
        message: Message,
        available_slots: list[TimeSlot],
    ) -> AppointmentProposal:
        """Pick slot(s) from `available_slots` and draft the proposal reply."""
        if not available_slots:
            raise ValueError("propose() requires at least one available slot")

        slots_rendered = "\n".join(
            f"[{i}] {s.start_at.isoformat()} – {s.end_at.isoformat()} "
            f"({s.timezone})"
            for i, s in enumerate(available_slots)
        )

        user_prompt = f"""TASK A — Propose slots.

Thread subject: {thread.subject or "(no subject)"}
Customer message:
---
{message.body_text.strip()}
---

Available slots (pick up to 3 by index):
{slots_rendered}

Return JSON."""

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=500,
            system=self._system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        data = _parse_json_loosely(response.content[0].text)

        raw_idxs = data.get("chosen_slot_indexes") or []
        chosen: list[TimeSlot] = []
        for i in raw_idxs:
            try:
                chosen.append(available_slots[int(i)])
            except (ValueError, IndexError):
                logger.warning(f"AppointmentHandler returned invalid slot index {i}")

        return AppointmentProposal(
            thread_id=thread.id,
            slots=chosen,
            reply_subject=str(data.get("reply_subject") or thread.subject or "").strip(),
            reply_body=str(data.get("reply_body") or "").strip(),
        )

    async def interpret_customer_reply(
        self,
        thread: Thread,
        message: Message,
        current_proposal: AppointmentProposal,
    ) -> AppointmentDecision:
        """Parse a customer's reply to a prior proposal."""
        slots_rendered = "\n".join(
            f"[{i}] {s.start_at.isoformat()} – {s.end_at.isoformat()} "
            f"({s.timezone})"
            for i, s in enumerate(current_proposal.slots)
        )

        user_prompt = f"""TASK B — Interpret the customer's reply.

Thread subject: {thread.subject or "(no subject)"}
Current proposal offered these slots:
{slots_rendered}

Customer's reply:
---
{message.body_text.strip()}
---

Return JSON."""

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=300,
            system=self._system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        data = _parse_json_loosely(response.content[0].text)

        kind = str(data.get("kind") or "ambiguous").strip().lower()
        if kind not in {"confirm", "reschedule", "cancel", "ambiguous"}:
            kind = "ambiguous"

        idx = data.get("chosen_slot_index")
        chosen_idx = int(idx) if isinstance(idx, (int, float)) else None

        return AppointmentDecision(
            kind=kind,
            chosen_slot_index=chosen_idx,
            reasoning=str(data.get("reasoning") or ""),
        )


# ── helpers ───────────────────────────────────────────────────────────────────


def _parse_json_loosely(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())
