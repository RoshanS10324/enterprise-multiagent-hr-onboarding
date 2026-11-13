from enum import StrEnum


class PolicyViolationCode(StrEnum):
    ROLE_NOT_ALLOWED = "ROLE_NOT_ALLOWED"
    SELF_APPROVAL = "SELF_APPROVAL"
    NOT_ASSIGNED_OFFICER = "NOT_ASSIGNED_OFFICER"
    REQUESTER_MISMATCH = "REQUESTER_MISMATCH"


class PolicyViolationError(ValueError):
    """A policy/authorization rule was violated.

    The message is generic and carries the code only, never an id: the
    code alone is enough for a caller to branch on, and ids belong in the
    audit entry (POLICY_DENIED/ACCESS_DENIED), not in an exception string
    that might end up in a log or an error page.
    """

    def __init__(self, code: PolicyViolationCode) -> None:
        self.code = code
        super().__init__(f"policy violation: {code.value}")


class HrisUnavailableError(Exception):
    """Raised by an HrisGateway when the target system can't be written to.

    Only this exception's class name is ever persisted (HrisRecord.last_error);
    the message is never stored, since it could carry response data.
    """


class CaseNotFoundError(ValueError):
    """A case doesn't exist, or the viewer isn't allowed to see it.

    Both situations raise this SAME exception with the SAME generic
    message, so a caller can never tell "doesn't exist" apart from
    "exists but you can't see it" (that distinction would itself leak
    whether the case exists).
    """

    def __init__(self) -> None:
        super().__init__("case not found")


class AccessDeniedError(ValueError):
    """The viewer's role isn't allowed to perform this operation."""

    def __init__(self) -> None:
        super().__init__("access denied")
