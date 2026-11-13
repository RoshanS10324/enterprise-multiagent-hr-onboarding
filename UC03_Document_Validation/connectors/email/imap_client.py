"""IMAP and SMTP email connector for HR mailbox monitoring.

Uses Python standard library imaplib and email modules for zero-dependency,
universal IMAP/SSL support across Gmail, Microsoft 365, Outlook, and corporate mail servers.
"""

from __future__ import annotations

import email
import email.header
import email.message
import email.utils
import imaplib
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Sequence

from .base import EmailAttachment, EmailIngestionConnectorBase, IncomingOnboardingEmail

log = logging.getLogger("uc03.connectors.imap")


def _decode_header(raw_header: str | None) -> str:
    """Safely decode RFC 2047 MIME encoded headers."""
    if not raw_header:
        return ""
    parts: list[str] = []
    for text_bytes, charset in email.header.decode_header(raw_header):
        if isinstance(text_bytes, bytes):
            enc = charset or "utf-8"
            try:
                parts.append(text_bytes.decode(enc, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(text_bytes.decode("utf-8", errors="replace"))
        else:
            parts.append(str(text_bytes))
    return "".join(parts).strip()


class IMAPEmailConnector(EmailIngestionConnectorBase):
    """Production IMAP connector supporting SSL/TLS, attachment extraction, and message flags."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 993,
        mailbox: str = "INBOX",
        smtp_host: str | None = None,
        smtp_port: int = 587,
        use_ssl: bool = True,
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.mailbox = mailbox
        self.smtp_host = smtp_host or host
        self.smtp_port = smtp_port
        self.use_ssl = use_ssl

    def _connect_imap(self) -> imaplib.IMAP4:
        if self.use_ssl:
            client = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            client = imaplib.IMAP4(self.host, self.port)
        client.login(self.user, self.password)
        client.select(self.mailbox)
        return client

    async def fetch_unread_emails(self, limit: int = 20) -> list[IncomingOnboardingEmail]:
        """Fetch unread emails with attachments."""
        emails: list[IncomingOnboardingEmail] = []
        if not self.host or not self.user:
            log.warning("IMAP host or user not configured; skipping mailbox scan")
            return []

        try:
            client = self._connect_imap()
            status, response = client.search(None, "UNSEEN")
            if status != "OK" or not response or not response[0]:
                client.close()
                client.logout()
                return []

            msg_ids = response[0].split()[-limit:]
            for mid in msg_ids:
                res_fetch, data = client.fetch(mid, "(RFC822)")
                if res_fetch != "OK" or not data or not data[0]:
                    continue

                raw_email = data[0][1]
                msg: email.message.Message = email.message_from_bytes(raw_email)

                parsed = self._parse_mime_message(msg, mid.decode())
                if parsed.attachments:
                    emails.append(parsed)

            client.close()
            client.logout()
        except Exception as exc:
            log.error("Failed to fetch unread emails via IMAP: %s", exc)
        return emails

    def _parse_mime_message(self, msg: email.message.Message, internal_id: str) -> IncomingOnboardingEmail:
        subject = _decode_header(msg.get("Subject", ""))
        sender_raw = _decode_header(msg.get("From", ""))
        sender_name, sender_email = email.utils.parseaddr(sender_raw)
        message_id = msg.get("Message-ID", f"imap-{internal_id}").strip("<>")

        body_text = ""
        attachments: list[EmailAttachment] = []

        if msg.is_multipart():
            for part in msg.walk():
                content_disposition = str(part.get("Content-Disposition", ""))
                content_type = part.get_content_type()

                if "attachment" in content_disposition.lower() or part.get_filename():
                    filename = _decode_header(part.get_filename() or "attachment.dat")
                    raw_bytes = part.get_payload(decode=True) or b""
                    is_inline = "inline" in content_disposition.lower()
                    cid = part.get("Content-ID", "").strip("<>")

                    attachments.append(
                        EmailAttachment(
                            filename=filename,
                            content_type=content_type,
                            size_bytes=len(raw_bytes),
                            raw_bytes=raw_bytes,
                            is_inline=is_inline,
                            content_id=cid or None,
                        )
                    )
                elif content_type == "text/plain" and not body_text:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_text = payload.decode(charset, errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body_text = payload.decode(charset, errors="replace")

        return IncomingOnboardingEmail(
            message_id=message_id,
            sender_email=sender_email.lower().strip(),
            sender_name=sender_name or sender_email,
            subject=subject,
            body_text=body_text.strip(),
            received_at=datetime.now(timezone.utc),
            attachments=attachments,
            raw_headers=dict(msg.items()),
        )

    async def mark_email_processed(self, message_id: str) -> bool:
        r"""Mark email as \Seen on IMAP server."""
        try:
            client = self._connect_imap()
            status, response = client.search(None, f'HEADER Message-ID "{message_id}"')
            if status == "OK" and response and response[0]:
                for mid in response[0].split():
                    client.store(mid, "+FLAGS", r"\Seen")
            client.close()
            client.logout()
            return True
        except Exception as exc:
            log.warning("Could not mark email %s as seen on IMAP: %s", message_id, exc)
            return False

    async def send_email(self, to: Sequence[str], subject: str, body: str) -> bool:
        """Send an email via SMTP."""
        if not self.smtp_host or not self.user:
            log.warning("SMTP not configured; email send skipped")
            return False

        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = self.user
            msg["To"] = ", ".join(to)

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.user, self.password)
                server.sendmail(self.user, list(to), msg.as_string())
            return True
        except Exception as exc:
            log.error("Failed to send SMTP email to %s: %s", to, exc)
            return False
