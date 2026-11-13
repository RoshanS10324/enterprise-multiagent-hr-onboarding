"""Typed errors for UC-02.

``code`` values mirror ``hr_recruitment_common.contracts.ExceptionCode`` so they
can be mapped onto the shared contract without renaming when the harness lands.
"""

from __future__ import annotations


class OnboardingError(Exception):
    """Base class for all UC-02 domain errors."""

    code = "INTERNAL_ERROR"

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:  # pragma: no cover - trivial
        if not self.context:
            return self.message
        detail = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({detail})"


class MissingConfigurationError(OnboardingError):
    """No client configuration, or the configuration yields no applicable tasks."""

    code = "MISSING_DATA"


class PreconditionError(OnboardingError):
    """Offer acceptance or onboarding-form approval is missing/invalid."""

    code = "MISSING_DATA"


class EmployeeNotEligibleError(PreconditionError):
    """The employee's role, grade, location, nationality group or employment type is not one the client's configuration supports.

    Raised only when the client's configuration declares an eligibility catalogue; UC-02 never guesses a catalogue."""

    code = "MISSING_DATA"


class DuplicateOnboardingError(OnboardingError):
    """An onboarding record already exists for this client/employee pair."""

    code = "DUPLICATE"


class PrerequisiteNotMetError(OnboardingError):
    """A task was started or completed while its dependencies were unmet."""

    code = "POLICY_BREACH"


class UnauthorizedActionError(OnboardingError):
    """The acting role may not perform this action on this task or gate."""

    code = "POLICY_BREACH"


class DataIsolationError(OnboardingError):
    """An actor from one client attempted to reach another client's data."""

    code = "POLICY_BREACH"


class ApprovalRequiredError(OnboardingError):
    """A hard-gated action was attempted without an approved gate decision."""

    code = "POLICY_BREACH"


class InvalidStateError(OnboardingError):
    """The onboarding or task is in a state that forbids this transition."""

    code = "POLICY_BREACH"


class StalePlanVersionError(InvalidStateError):
    """Feedback or a request was made against a plan version that is no longer current."""

    code = "STALE_PLAN_VERSION"


class EvidenceRequiredError(OnboardingError):
    """UC02-BR-12. A task requiring evidence was completed without it."""

    code = "MISSING_DATA"


class PlanNotApprovedError(OnboardingError):
    """Team execution was attempted before HR approved the onboarding plan."""

    code = "POLICY_BREACH"


class CalendarConfigurationError(OnboardingError):
    """A client's working calendar is required but not configured, or is invalid."""

    code = "MISSING_DATA"


class CalendarRangeError(OnboardingError):
    """A date falls outside the verified UAE holiday calendar coverage.

    Silently treating an uncovered year as holiday-free produces due dates that
    look valid but are wrong (finding L3).
    """

    code = "MISSING_DATA"
