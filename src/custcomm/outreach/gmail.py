"""Gmail API reply sender.

Reuses the same OAuth2 credentials as `GmailInbox`, so an operator who set
up `backend: gmail` for the inbox is already set up to send. From address
is pinned to `config.operator_email`.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from custcomm.inbox.gmail import _build_gmail_service
from custcomm.models import Draft, Message, Thread
from custcomm.outreach.base import ReplySender, SendResult

logger = logging.getLogger(__name__)


class GmailSender(ReplySender):
    async def send(
        self, draft: Draft, thread: Thread, latest_inbound: Message | None
    ) -> SendResult:
        service = _build_gmail_service(
            credentials_path=self.keys.gmail_credentials_path,
            token_path=self.keys.gmail_token_path,
        )

        email_msg = _compose_reply(
            subject=draft.subject,
            body=draft.body,
            from_email=self.config.operator_email,
            from_name=self.config.operator_name,
            to_email=(latest_inbound.from_addr if latest_inbound else ""),
            in_reply_to=(latest_inbound.message_id_header if latest_inbound else None),
            references=(latest_inbound.references_headers if latest_inbound else []),
        )

        raw = base64.urlsafe_b64encode(email_msg.as_bytes()).decode()
        sent = service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()

        message_id_header = email_msg["Message-ID"]
        logger.info(
            f"Sent Gmail reply for thread {thread.id}: "
            f"provider_id={sent.get('id')} message_id={message_id_header}"
        )
        return SendResult(
            message_id_header=message_id_header,
            provider_id=sent.get("id"),
        )


def _compose_reply(
    subject: str,
    body: str,
    from_email: str,
    from_name: str,
    to_email: str,
    in_reply_to: str | None,
    references: list[str] | None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    if to_email:
        msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        refs = list(references or [])
        if in_reply_to not in refs:
            refs.append(in_reply_to)
        if refs:
            msg["References"] = " ".join(refs)

    msg.set_content(body)
    return msg
