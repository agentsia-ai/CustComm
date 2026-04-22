"""Inbox connector ABC."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from custcomm.config.loader import APIKeys, CustCommConfig
from custcomm.models import RawInboundMessage


class InboxConnector(ABC):
    """Abstract base for inbound-mail connectors."""

    def __init__(self, config: CustCommConfig, keys: APIKeys) -> None:
        self.config = config
        self.keys = keys

    @abstractmethod
    def fetch_new(self) -> AsyncIterator[RawInboundMessage]:
        """Yield new messages since the last poll.

        Implementations decide what "new" means (e.g. Gmail's `is:unread`
        query, or an IMAP UIDVALIDITY watermark). The engine-side ingest
        logic dedups by `message_id_header`, so returning duplicates is
        safe but wasteful.
        """
        ...
