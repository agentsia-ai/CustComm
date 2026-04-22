"""IMAP inbox connector — planned; stub in v1.

Left here with the right shape so a downstream contributor can fill it in
without restructuring the package. See the module-level TODO for the
intended approach.
"""

from __future__ import annotations

from typing import AsyncIterator

from custcomm.inbox.base import InboxConnector
from custcomm.models import RawInboundMessage


class IMAPInbox(InboxConnector):
    """Stub — raises NotImplementedError.

    Intended implementation:
      - Use `aioimaplib` or `imaplib` in a thread executor.
      - Track UIDVALIDITY + last seen UID in a small state file so we
        don't replay messages across restarts.
      - Translate the stored `(UIDVALIDITY, UID)` pair into
        `raw.provider_message_id` so dedup can include the case where
        `message_id_header` is missing.
    """

    async def fetch_new(self) -> AsyncIterator[RawInboundMessage]:
        raise NotImplementedError(
            "IMAPInbox is not implemented in v1. Set `inbox.backend: gmail` "
            "in config.yaml, or contribute the connector upstream."
        )
        yield  # pragma: no cover - required for AsyncIterator typing
