"""CustComm Reply Drafter.

This is the generic engine implementation. To customize for a productized
agent (e.g. a named persona with a distinctive voice), either:
  1. Subclass `ReplyDrafter` and override `SYSTEM_PROMPT` (and optionally
     `_build_user_prompt` / `summarize_history`), or
  2. Point `config.ai.reply_prompt_path` at an external prompt file.

See CLAUDE.md → Customization Patterns for details.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import anthropic

from custcomm.config.loader import APIKeys, CustCommConfig
from custcomm.conversation.history import ThreadHistoryView
from custcomm.models import Draft, DraftStatus, Intent, Message, MessageDirection

logger = logging.getLogger(__name__)


DEFAULT_REPLY_PROMPT = """You draft professional, human-sounding replies to customer emails on
behalf of a small-business operator.

Hard rules — you MUST follow all of these:
  - NEVER invent prices, availability, product features, discounts,
    guarantees, or commitments that aren't already present in the thread
    history or clearly implied by the operator's previous replies.
  - When something is uncertain (timing, pricing, scope, capacity),
    explicitly say the operator will confirm — don't guess.
  - Match the customer's tone. Formal customers get formal replies;
    casual customers get casual, friendly ones. Don't over-warm.
  - Keep it concise. Under 150 words whenever possible.
  - Don't start with "I hope this email finds you well" or any similar filler.
  - Don't restate everything the customer just said. Reply forward.
  - If the intent is a complaint, DON'T pretend to resolve it yourself —
    acknowledge, empathize, and say the operator will follow up personally.

Return ONLY valid JSON — no preamble — in this exact shape:
{
  "subject": "Re: <original or adjusted subject>",
  "body": "<the reply body, plain text, no signature — signature is added separately>"
}"""


DEFAULT_SUMMARY_PROMPT = """You compress earlier parts of a customer-support email thread so the
drafter can stay focused on the most recent messages without losing context.

Produce a faithful, non-editorializing summary. Preserve:
  - Any open commitments the operator has made (promised to follow up,
    promised a quote, agreed to a time, etc.)
  - Key facts the customer has shared (company, timeline, use case)
  - Any prior decisions (rescheduled from X to Y, declined option Z)

Return only the summary text — no JSON, no headers, no commentary.
Maximum 6 sentences."""


class ReplyDrafter:
    """Drafts customer replies using Claude.

    Subclass this and override `SYSTEM_PROMPT` to define a tuned drafter with
    a custom voice. Per-deployment overrides can also be supplied via
    `config.ai.reply_prompt_path`.
    """

    SYSTEM_PROMPT: str = DEFAULT_REPLY_PROMPT
    SUMMARY_SYSTEM_PROMPT: str = DEFAULT_SUMMARY_PROMPT

    def __init__(self, config: CustCommConfig, keys: APIKeys) -> None:
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=keys.anthropic)
        self.model = config.ai.model
        self.max_reply_chars = config.ai.max_reply_chars
        self._system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        """Resolve the active system prompt.

        Resolution order:
          1. `config.ai.reply_prompt_path` (if set and file exists)
          2. Class attribute `SYSTEM_PROMPT` (subclass-overridable)
        """
        override = self.config.ai.reply_prompt_path
        if override:
            path = Path(override)
            if path.exists():
                logger.info(f"{type(self).__name__} using prompt override: {path}")
                return path.read_text(encoding="utf-8")
            logger.warning(
                f"reply_prompt_path points at missing file: {path} — "
                f"falling back to {type(self).__name__}.SYSTEM_PROMPT"
            )
        return self.SYSTEM_PROMPT

    def _build_user_prompt(
        self,
        history_view: ThreadHistoryView,
        intent: Intent,
        guidance: str = "",
    ) -> str:
        operator_name = self.config.operator_name or self.config.client_name
        history_block = history_view.format_for_prompt(operator_name)

        guidance_block = ""
        if guidance:
            guidance_block = (
                "\n\nOperator guidance for THIS draft (overrides defaults when relevant):\n"
                f"{guidance.strip()}\n"
            )

        return f"""Draft a reply to the latest inbound message in this thread.

Classified intent: {intent.value}
Operator: {operator_name}
Operator email (your From): {self.config.operator_email}

=== THREAD ===
{history_block}
{guidance_block}
Write the reply now. JSON only."""

    async def draft(
        self,
        history_view: ThreadHistoryView,
        intent: Intent,
        guidance: str = "",
    ) -> Draft:
        """Generate a Draft for the thread in `history_view`.

        The returned draft is `status=PENDING` and carries a fresh
        `approval_token`. Persistence (and the supersede-prior-pending dance)
        is handled by the caller via `ThreadDatabase.insert_draft`.
        """
        prompt = self._build_user_prompt(history_view, intent, guidance=guidance)

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=800,
            system=self._system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
        data = _parse_json_loosely(raw)

        subject = str(data.get("subject") or history_view.thread.subject or "").strip()
        body = str(data.get("body") or "").strip()

        truncated = False
        if len(body) > self.max_reply_chars:
            body = body[: self.max_reply_chars].rstrip() + "…"
            truncated = True
            logger.warning(
                f"Draft for thread {history_view.thread.id} exceeded max_reply_chars; truncated."
            )

        body = _append_signature(body, self.config.outreach.signature, self.config)

        generator_label = type(self).__name__
        if truncated:
            generator_label += " (truncated)"

        return Draft(
            thread_id=history_view.thread.id,
            status=DraftStatus.PENDING,
            subject=subject,
            body=body,
            intent_at_time_of_draft=intent,
            generated_by=generator_label,
        )

    async def summarize_history(self, messages: list[Message]) -> str:
        """Compress a list of older messages into a plain-text summary.

        Separate LLM call from `draft()` so the compression step can use a
        tighter max_tokens budget. Override in a persona subclass only if
        the summarization voice needs to match — the default is deliberately
        plain.
        """
        if not messages:
            return ""
        rendered = _render_messages_for_summary(
            messages, operator_name=self.config.operator_name
        )
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=500,
            system=self.SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": rendered}],
        )
        return response.content[0].text.strip()


# ── helpers ───────────────────────────────────────────────────────────────────


def _parse_json_loosely(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def _render_messages_for_summary(messages: list[Message], operator_name: str) -> str:
    lines: list[str] = ["Summarize this email thread:", ""]
    for m in messages:
        who = (
            operator_name
            if m.direction == MessageDirection.OUTBOUND
            else f"Customer <{m.from_addr}>"
        )
        ts = (m.received_at or m.sent_at)
        ts_str = ts.isoformat() if ts else ""
        lines.append(f"--- {who} @ {ts_str} ---")
        lines.append(m.body_text.strip())
        lines.append("")
    return "\n".join(lines)


def _append_signature(body: str, template: str, config) -> str:
    if not template:
        return body
    sig = template.format(
        operator_name=config.operator_name,
        operator_title=config.operator_title,
        operator_email=config.operator_email,
    )
    return f"{body.strip()}\n\n{sig.strip()}"
