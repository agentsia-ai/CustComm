"""RFC 5322 thread resolution.

Given a RawInboundMessage, decide which existing Thread it belongs to — or
return None to indicate "new thread". Resolution order:

  1. In-Reply-To header → a Message we've stored → that Message's thread_id
  2. References headers (left-to-right) → same
  3. Fallback: same customer_id + same normalized subject within the last
     `subject_fallback_days` days (default 90)
  4. Otherwise: None (new thread)
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from custcomm.crm.database import ThreadDatabase
from custcomm.models import RawInboundMessage

logger = logging.getLogger(__name__)


# Match any number of "Re:" / "Fwd:" / "Fw:" prefixes (with or without colons
# and trailing spaces), case-insensitive, at the start of a subject line.
_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fwd?|aw|sv)\s*:\s*)+", re.IGNORECASE
)

# Collapse runs of whitespace to single spaces.
_WS_RE = re.compile(r"\s+")


def normalize_subject(subject: str) -> str:
    """Strip leading Re:/Fwd:/etc. prefixes, collapse whitespace, lowercase.

    Used both to store a canonical thread subject and to match incoming
    messages to existing threads by subject.
    """
    if not subject:
        return ""
    stripped = _SUBJECT_PREFIX_RE.sub("", subject)
    return _WS_RE.sub(" ", stripped).strip().lower()


async def resolve_thread_id(
    raw: RawInboundMessage,
    customer_id: str,
    db: ThreadDatabase,
    subject_fallback_days: int = 90,
) -> Optional[str]:
    """Return the thread_id this inbound message belongs to, or None if it
    should start a new thread."""

    # 1. In-Reply-To
    if raw.in_reply_to_header:
        tid = await db.find_thread_by_message_header(raw.in_reply_to_header)
        if tid:
            logger.debug(f"Thread {tid} matched via In-Reply-To")
            return tid

    # 2. References (walk left-to-right; older → newer is the convention)
    for ref in raw.references_headers:
        tid = await db.find_thread_by_message_header(ref)
        if tid:
            logger.debug(f"Thread {tid} matched via References")
            return tid

    # 3. Subject fallback
    normalized = normalize_subject(raw.subject)
    if normalized:
        tid = await db.find_thread_by_subject(
            customer_id, normalized, within_days=subject_fallback_days
        )
        if tid:
            logger.debug(
                f"Thread {tid} matched via subject fallback ({normalized!r})"
            )
            return tid

    # 4. Miss — caller creates a new thread
    return None
