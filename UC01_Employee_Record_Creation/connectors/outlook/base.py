"""The contract every Outlook connector implements.

[Proposed] The email trigger itself is a stand-in (OQ-01/Q-11): in
production, offer acceptance should come from the recruitment system or
an e-signature flow, not a mailbox. This interface exists so the mock
used by demos and evals and any later real implementation are
interchangeable.

Boundaries that apply to any implementation of `OutlookConnector`:

- Microsoft Graph is a business system. A real implementation does not
  call Graph directly; it goes through a tool defined in `mcp/`
  (see CLAUDE.md, "Tooling boundaries"). Only the mock in `mock.py`,
  which talks to nothing, is exempt.
- Transport only. Do not parse offer references, classify intent, or
  decide anything about a case here. That belongs to
  `sub_agent.intake.email_intake`.
- Never log, store, or return message bodies beyond the single `raw`
  handoff to the intake parser, which reads From and Subject only.
- Nothing sent to a joiner goes out without a person's approval, so this
  interface is deliberately read-only: it has no send method.
"""

from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class OutlookConnectorError(RuntimeError):
    """Transport failure (auth, network, throttling). Raised by the
    connector so the caller can retry or park the trigger; it never means
    "this email was rejected" -- that is an intake decision."""


class MailMessage(BaseModel):
    """One inbound message, as transport sees it.

    `raw` is the RFC 5322 source, handed to the intake parser unmodified.
    `message_id` is the mailbox's stable identifier, used for
    acknowledgement and for at-least-once de-duplication upstream.
    """

    model_config = ConfigDict(frozen=True)

    message_id: str
    raw: str
    # Convenience copies of the only two headers UC-01 intake reads. Kept
    # optional so a transport that cannot cheaply pre-parse may omit them.
    sender: Optional[str] = None
    subject: Optional[str] = None


@runtime_checkable
class OutlookConnector(Protocol):
    def fetch_unprocessed(self, mailbox: str, limit: int = 50) -> list[MailMessage]:
        """Return up to `limit` messages not yet acknowledged, oldest first.

        Delivery is at least once: the same message may be returned again
        until `acknowledge` succeeds. Callers must stay idempotent, which
        UC-01 intake already is (duplicate acceptance is detected and
        ignored).
        """
        ...

    def acknowledge(self, mailbox: str, message_id: str) -> None:
        """Mark a message as processed so it is not returned again.

        Acknowledge only after the intake result is durable. Acknowledging
        an unknown or already-acknowledged id is a no-op, not an error.
        """
        ...
