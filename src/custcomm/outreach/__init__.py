"""Outbound reply senders.

Each concrete backend subclasses `ReplySender` and implements
`async def send(draft, thread, latest_inbound) -> str` returning the
provider-assigned Message-ID.
"""

from __future__ import annotations

from custcomm.config.loader import APIKeys, CustCommConfig
from custcomm.outreach.base import ReplySender, SendResult


def build_sender(config: CustCommConfig, keys: APIKeys) -> ReplySender:
    backend = config.outreach.backend.lower()
    if backend == "gmail":
        from custcomm.outreach.gmail import GmailSender
        return GmailSender(config, keys)
    if backend == "smtp":
        from custcomm.outreach.smtp import SMTPSender
        return SMTPSender(config, keys)
    raise ValueError(
        f"Unknown outreach backend: {backend!r}. Use 'gmail' or 'smtp'."
    )


__all__ = ["ReplySender", "SendResult", "build_sender"]
