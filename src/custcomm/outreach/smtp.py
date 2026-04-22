"""SMTP reply sender — fallback for deployments not using the Gmail API."""

from __future__ import annotations

import logging
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import aiosmtplib

from custcomm.models import Draft, Message, Thread
from custcomm.outreach.base import ReplySender, SendResult

logger = logging.getLogger(__name__)


class SMTPSender(ReplySender):
    async def send(
        self, draft: Draft, thread: Thread, latest_inbound: Message | None
    ) -> SendResult:
        from_email = (
            self.keys.smtp_from_email or self.config.operator_email
        )
        from_name = self.keys.smtp_from_name or self.config.operator_name

        msg = EmailMessage()
        msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
        to_email = latest_inbound.from_addr if latest_inbound else ""
        if to_email:
            msg["To"] = to_email
        msg["Subject"] = draft.subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()

        if latest_inbound and latest_inbound.message_id_header:
            msg["In-Reply-To"] = latest_inbound.message_id_header
            refs = list(latest_inbound.references_headers or [])
            if latest_inbound.message_id_header not in refs:
                refs.append(latest_inbound.message_id_header)
            if refs:
                msg["References"] = " ".join(refs)

        msg.set_content(draft.body)

        await aiosmtplib.send(
            msg,
            hostname=self.keys.smtp_host,
            port=self.keys.smtp_port,
            username=self.keys.smtp_username,
            password=self.keys.smtp_password,
            start_tls=True,
        )

        message_id_header = msg["Message-ID"]
        logger.info(f"Sent SMTP reply for thread {thread.id}: {message_id_header}")
        return SendResult(message_id_header=message_id_header)
