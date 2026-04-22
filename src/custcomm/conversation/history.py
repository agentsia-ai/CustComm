"""Thread history view builder.

Assembles the compressed-but-faithful view of a thread's conversation that
the drafter / classifier consume. Lives here (not in `ai/`) because it's
pure plumbing — no LLM calls — and both subclasses and the engine default
should see the same well-formed view.

Policy:
  - Last `full_messages_kept` messages are included verbatim.
  - Older messages are folded into a rolling `Thread.summary` via
    `ReplyDrafter.summarize_history()`. We only REGENERATE the summary
    when the total message count crosses `summarize_at_messages`.
  - The prompt text produced by `format_for_prompt()` is what lands in the
    drafter's user message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from custcomm.config.loader import CustCommConfig
from custcomm.crm.database import ThreadDatabase
from custcomm.models import Message, MessageDirection, Thread

logger = logging.getLogger(__name__)


@dataclass
class ThreadHistoryView:
    """What a drafter/classifier sees about a thread."""

    thread: Thread
    full_messages: list[Message] = field(default_factory=list)
    summary: str = ""

    def format_for_prompt(self, operator_name: str) -> str:
        """Human-readable block injected as the drafter's user message."""
        lines: list[str] = []
        lines.append(f"Thread subject: {self.thread.subject or '(no subject)'}")
        if self.thread.intent:
            lines.append(f"Latest classified intent: {self.thread.intent.value}")
        if self.summary:
            lines.append("")
            lines.append("Earlier conversation summary:")
            lines.append(self.summary)
        if self.full_messages:
            lines.append("")
            lines.append("Recent messages (oldest → newest):")
            for m in self.full_messages:
                speaker = (
                    operator_name
                    if m.direction == MessageDirection.OUTBOUND
                    else f"Customer <{m.from_addr}>"
                )
                ts = (m.received_at or m.sent_at or "").isoformat() if (
                    m.received_at or m.sent_at
                ) else ""
                lines.append(f"--- {speaker} @ {ts} ---")
                lines.append(m.body_text.strip())
        return "\n".join(lines)


class ThreadHistoryBuilder:
    """Builds the prompt-sized view of a thread. Single responsibility: it
    decides what's in the window and what's in the summary.

    The summarization LLM call lives on `ReplyDrafter.summarize_history`;
    this class invokes it when needed but doesn't implement it.
    """

    def __init__(self, config: CustCommConfig, db: ThreadDatabase) -> None:
        self.config = config
        self.db = db
        self.full_kept = config.history.full_messages_kept
        self.summarize_at = config.history.summarize_at_messages
        self.max_summary_chars = config.history.max_summary_chars

    async def build(
        self,
        thread: Thread,
        summarizer: "HistorySummarizer | None" = None,
    ) -> ThreadHistoryView:
        """Fetch messages, window them, and (optionally) refresh the summary.

        `summarizer` is anything exposing `async summarize_history(messages)`
        returning a string — in practice a `ReplyDrafter`. When omitted, we
        reuse whatever `thread.summary` already holds.
        """
        messages = await self.db.get_messages(thread.id)

        if len(messages) <= self.full_kept:
            return ThreadHistoryView(
                thread=thread, full_messages=messages, summary=thread.summary
            )

        older = messages[: -self.full_kept]
        recent = messages[-self.full_kept:]

        # Only regenerate the summary if we've newly crossed the threshold
        # OR the thread's stored summary is empty. (A "stale summary" policy
        # beyond this would need tracking which message IDs are covered; v1
        # trades that for simplicity — we always cover messages older than
        # the sliding window.)
        should_summarize = summarizer is not None and (
            not thread.summary or len(messages) >= self.summarize_at
        )
        summary = thread.summary
        if should_summarize:
            try:
                summary = await summarizer.summarize_history(older)
                if summary and len(summary) > self.max_summary_chars:
                    summary = summary[: self.max_summary_chars] + "…"
                thread.summary = summary
                await self.db.upsert_thread(thread)
            except Exception as exc:  # noqa: BLE001 — never fail a draft on summarization
                logger.warning(f"History summarization failed: {exc}")

        return ThreadHistoryView(
            thread=thread, full_messages=recent, summary=summary
        )


class HistorySummarizer:  # structural typing hint; not imported elsewhere
    """Protocol-ish type hint. Anything with an async `summarize_history`
    method can play this role."""

    async def summarize_history(self, messages: list[Message]) -> str: ...
