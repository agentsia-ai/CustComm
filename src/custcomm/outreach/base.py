"""Outbound reply sender ABC."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from custcomm.config.loader import APIKeys, CustCommConfig
from custcomm.models import Draft, Message, Thread


@dataclass
class SendResult:
    """What a sender returns after a successful send."""

    message_id_header: str          # the Message-ID assigned to our outbound mail
    provider_id: Optional[str] = None  # provider-native id (e.g. Gmail message id)


class ReplySender(ABC):
    """Abstract base for outbound-mail senders.

    The engine's send loop is responsible for:
      - Checking `draft.status == APPROVED` before calling `send()`.
      - Calling `ThreadDatabase.mark_draft_sent` with the result's
        message_id_header for the double-send interlock.

    A `ReplySender` should NOT mutate the draft itself — it just sends.
    """

    def __init__(self, config: CustCommConfig, keys: APIKeys) -> None:
        self.config = config
        self.keys = keys

    @abstractmethod
    async def send(
        self, draft: Draft, thread: Thread, latest_inbound: Message | None
    ) -> SendResult:
        """Send a reply.

        `latest_inbound` provides In-Reply-To / References data for proper
        threading in the recipient's client. Pass `None` only when the
        draft is genuinely the first message (rare in CustComm — almost
        every send is a reply).
        """
        ...
