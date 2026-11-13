"""In-memory Outlook connector for demos, tests, and evals.

Talks to nothing: messages are supplied in the constructor or loaded from
the synthetic `.eml` fixtures in `demo/emails/`. Deterministic -- the
same inputs always yield the same order -- so eval runs stay repeatable.
"""

from email import message_from_string
from email.utils import parseaddr
from pathlib import Path
from typing import Iterable, Optional

from .base import MailMessage, OutlookConnector, OutlookConnectorError

DEFAULT_MAILBOX = "onboarding@enterprise.example.com"
_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "demo" / "emails"


def _message_from_raw(message_id: str, raw: str) -> MailMessage:
    parsed = message_from_string(raw)
    _, sender = parseaddr(parsed.get("From") or "")
    return MailMessage(
        message_id=message_id,
        raw=raw,
        sender=sender or None,
        subject=parsed.get("Subject") or None,
    )


class MockOutlookConnector(OutlookConnector):
    """A fake mailbox. `fail_next` makes it raise once, so callers can
    exercise their retry path without a real outage.
    """

    def __init__(
        self,
        messages: Optional[Iterable[MailMessage]] = None,
        mailbox: str = DEFAULT_MAILBOX,
    ) -> None:
        self.mailbox = mailbox
        self._messages: list[MailMessage] = list(messages or [])
        self._acknowledged: set[str] = set()
        self.fail_next = False

    @classmethod
    def from_fixtures(
        cls, directory: Optional[Path] = None, mailbox: str = DEFAULT_MAILBOX
    ) -> "MockOutlookConnector":
        """Load every `.eml` in `directory` (default: `demo/emails/`),
        sorted by filename so ordering is stable across platforms."""
        source = directory or _FIXTURES_DIR
        messages = [
            _message_from_raw(path.name, path.read_text(encoding="utf-8"))
            for path in sorted(source.glob("*.eml"))
        ]
        return cls(messages, mailbox=mailbox)

    def deliver(self, raw: str, message_id: Optional[str] = None) -> MailMessage:
        """Drop a new message into the fake mailbox."""
        new_id = message_id or f"mock-{len(self._messages) + 1}"
        message = _message_from_raw(new_id, raw)
        self._messages.append(message)
        return message

    def _check_mailbox(self, mailbox: str) -> None:
        if mailbox != self.mailbox:
            raise OutlookConnectorError(f"unknown mailbox {mailbox!r}")

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise OutlookConnectorError("simulated Outlook transport failure")

    def fetch_unprocessed(self, mailbox: str, limit: int = 50) -> list[MailMessage]:
        self._check_mailbox(mailbox)
        self._maybe_fail()
        pending = [m for m in self._messages if m.message_id not in self._acknowledged]
        return pending[:limit]

    def acknowledge(self, mailbox: str, message_id: str) -> None:
        self._check_mailbox(mailbox)
        self._maybe_fail()
        self._acknowledged.add(message_id)
