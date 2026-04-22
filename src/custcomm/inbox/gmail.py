"""Gmail API inbox connector.

Authentication: OAuth2 with a cached refresh token. On first use the
`google-auth-oauthlib` helper runs a local-server flow and opens a browser
for consent. The resulting token is persisted to `keys.gmail_token_path`
(default `./.gmail_token.json`, gitignored).

Scope: `gmail.modify` — we need to read inbound mail, mark it processed,
and send replies from the same account. Nothing broader.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, AsyncIterator

from custcomm.inbox.base import InboxConnector
from custcomm.models import AttachmentRef, RawInboundMessage

logger = logging.getLogger(__name__)

# Gmail read + send from the same authenticated account.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailInbox(InboxConnector):
    """Polls Gmail for messages matching `config.inbox.gmail.query`."""

    async def fetch_new(self) -> AsyncIterator[RawInboundMessage]:
        service = _build_gmail_service(
            credentials_path=self.keys.gmail_credentials_path,
            token_path=self.keys.gmail_token_path,
        )
        query = self.config.inbox.gmail.get("query", "in:inbox is:unread newer_than:7d")
        ignore_senders = [s.lower() for s in self.config.inbox.ignore_senders]

        resp = service.users().messages().list(
            userId="me", q=query, maxResults=100
        ).execute()
        ids = [m["id"] for m in resp.get("messages", [])]
        logger.info(f"Gmail query '{query}' matched {len(ids)} message(s).")

        for mid in ids:
            full = service.users().messages().get(
                userId="me", id=mid, format="full"
            ).execute()
            raw = _gmail_to_raw_inbound(full)
            if raw is None:
                continue
            if any(s in raw.from_addr.lower() for s in ignore_senders):
                logger.debug(f"Ignoring message from {raw.from_addr} (ignore_senders)")
                continue
            yield raw


# ── Gmail service + message decoding helpers ─────────────────────────────────


def _build_gmail_service(credentials_path: str, token_path: str) -> Any:
    """Build a Gmail API client. Performs the OAuth dance on first run."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_file = Path(token_path)
    creds: Any = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path:
                raise RuntimeError(
                    "GMAIL_CREDENTIALS_PATH is not set. Download OAuth client "
                    "credentials from Google Cloud Console and point "
                    "GMAIL_CREDENTIALS_PATH at the JSON file."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_path, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        logger.info(f"Cached Gmail OAuth token at {token_file}")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _gmail_to_raw_inbound(full: dict) -> RawInboundMessage | None:
    """Decode a Gmail message resource into a RawInboundMessage."""
    payload = full.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    # From / To / Cc
    from_addrs = getaddresses([headers.get("from", "")])
    if not from_addrs or not from_addrs[0][1]:
        logger.warning(f"Gmail message {full.get('id')} has no From header; skipping")
        return None
    from_name, from_email = from_addrs[0]

    to_addrs = [addr for _, addr in getaddresses([headers.get("to", "")]) if addr]
    cc_addrs = [addr for _, addr in getaddresses([headers.get("cc", "")]) if addr]

    # Received timestamp: Gmail's internalDate is ms-since-epoch; headers also
    # carry a Date. Prefer internalDate when available.
    received_at = datetime.utcnow()
    if "internalDate" in full:
        try:
            received_at = datetime.utcfromtimestamp(int(full["internalDate"]) / 1000.0)
        except (ValueError, TypeError):
            pass
    elif "date" in headers:
        try:
            parsed = parsedate_to_datetime(headers["date"])
            if parsed:
                received_at = parsed.replace(tzinfo=None)
        except (TypeError, ValueError):
            pass

    # References can be space- or comma-separated.
    refs_raw = headers.get("references", "")
    references = [r.strip() for r in refs_raw.replace(",", " ").split() if r.strip()]

    # Body parts
    body_text, body_html, attachments = _extract_body_and_attachments(payload)

    return RawInboundMessage(
        provider="gmail",
        provider_message_id=full.get("id", ""),
        message_id_header=headers.get("message-id"),
        in_reply_to_header=headers.get("in-reply-to"),
        references_headers=references,
        from_addr=from_email,
        from_name=from_name or None,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        subject=headers.get("subject", ""),
        body_text=body_text,
        body_html=body_html,
        received_at=received_at,
        attachments=attachments,
        raw_headers=headers,
    )


def _extract_body_and_attachments(
    payload: dict,
) -> tuple[str, str | None, list[AttachmentRef]]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentRef] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        filename = part.get("filename") or ""
        body = part.get("body") or {}
        data = body.get("data")
        sub = part.get("parts") or []

        if filename:
            attachments.append(
                AttachmentRef(
                    filename=filename,
                    mime_type=mime or "application/octet-stream",
                    size_bytes=body.get("size", 0) or 0,
                    stored=False,
                )
            )
        elif data and mime == "text/plain":
            text_parts.append(_b64url(data))
        elif data and mime == "text/html":
            html_parts.append(_b64url(data))

        for child in sub:
            walk(child)

    walk(payload)

    body_text = "\n\n".join(p for p in text_parts if p.strip())
    body_html = "\n\n".join(p for p in html_parts if p.strip()) or None

    if not body_text and body_html:
        # Crude text fallback — strip tags. Good enough for v1 classifier input;
        # proper HTML rendering is a post-v1 concern.
        import re

        body_text = re.sub(r"<[^>]+>", " ", body_html)
        body_text = re.sub(r"\s+", " ", body_text).strip()

    return body_text, body_html, attachments


def _b64url(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
