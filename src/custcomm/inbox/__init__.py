"""Inbound mail connectors.

Each concrete backend subclasses `InboxConnector` and exposes a single
method: `async def fetch_new() -> AsyncIterator[RawInboundMessage]`.
"""

from __future__ import annotations

from custcomm.config.loader import APIKeys, CustCommConfig
from custcomm.inbox.base import InboxConnector


def build_inbox(config: CustCommConfig, keys: APIKeys) -> InboxConnector:
    """Factory — selects the concrete InboxConnector based on config."""
    backend = config.inbox.backend.lower()
    if backend == "gmail":
        from custcomm.inbox.gmail import GmailInbox
        return GmailInbox(config, keys)
    if backend == "imap":
        from custcomm.inbox.imap import IMAPInbox
        return IMAPInbox(config, keys)
    raise ValueError(
        f"Unknown inbox backend: {backend!r}. Use 'gmail' or 'imap'."
    )


__all__ = ["InboxConnector", "build_inbox"]
