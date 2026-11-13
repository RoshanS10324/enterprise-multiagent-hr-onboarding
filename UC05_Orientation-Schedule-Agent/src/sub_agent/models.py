"""Onboarding domain models, enums and errors: the vocabulary shared by the repository, services,
tool registry, workflow and API. No behaviour beyond model validation lives here.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from hr_onboarding_common.contracts import new_id, now_utc


# --------------------------------------------------------------------------- enums

class TaskStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SessionType(StrEnum):
    ORIENTATION = "orientation"
    INDUCTION = "induction"
    TEAM_INTRO = "team_intro"
    BUDDY_INTRO = "buddy_intro"


class SessionStatus(StrEnum):
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class ResponseStatus(StrEnum):
    ACCEPTED = "accepted"
    TENTATIVE = "tentative"
    DECLINED = "declined"
    NO_RESPONSE = "no_response"


class InvitationStatus(StrEnum):
    PENDING = "pending"  # attendee is on the tentative hold but has not been invited yet (pre-approval)
    SENT = "sent"
    FAILED = "failed"


class OperationStatus(StrEnum):
    PENDING = "pending"  # recorded before the provider call; the outcome is not known yet
    SUCCEEDED = "succeeded"
    UNCERTAIN = "uncertain"  # the provider call failed in a way that may still have taken effect
    REJECTED = "rejected"  # the provider definitively refused; nothing happened
    RELEASED = "released"  # the side effect was later undone (e.g. the hold was cancelled)


class CommunicationStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"  # delivered once through NotificationPort; a SENT draft can never be sent again


class CaseStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    WITHDRAWN = "withdrawn"


# --------------------------------------------------------------------------- models

class OnboardingRecord(BaseModel):
    employee_id: str
    full_name: str
    email: str
    role_title: str
    department: str
    team: str
    team_lead_id: str
    client_id: str
    start_date: date
    case_status: CaseStatus = CaseStatus.ACTIVE
    buddy_id: str | None = None


class OnboardingTask(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    employee_id: str
    client_id: str
    task_type: str  # the catalog of required task types is a business rule (services/checklist.py)
    status: TaskStatus = TaskStatus.PENDING
    due_date: date | None = None
    created_at: datetime = Field(default_factory=now_utc)
    completed_at: datetime | None = None


class OnboardingSession(BaseModel):
    id: str = Field(default_factory=lambda: new_id("session"))
    employee_id: str
    client_id: str
    session_type: SessionType
    scheduled_start: datetime
    scheduled_end: datetime
    status: SessionStatus = SessionStatus.SCHEDULED
    attendees: list[str] = Field(default_factory=list)
    external_event_ref: str | None = None  # calendar booking reference
    booking_operation_key: str | None = None  # the OperationRecord that created the calendar hold

    @model_validator(mode="after")
    def _end_after_start(self) -> "OnboardingSession":
        if self.scheduled_end <= self.scheduled_start:
            raise ValueError(f"scheduled_end ({self.scheduled_end}) must be after scheduled_start "
                             f"({self.scheduled_start})")
        return self


class ParticipantResponse(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rsvp"))
    session_id: str
    participant_id: str
    response: ResponseStatus
    responded_at: datetime = Field(default_factory=now_utc)


class Invitation(BaseModel):
    """Whether the calendar provider was asked to invite a participant, and whether that send
    itself succeeded -- distinct from ParticipantResponse, which is the recipient's RSVP. A
    booking can succeed overall while one attendee's invite individually fails to send."""

    id: str = Field(default_factory=lambda: new_id("invite"))
    session_id: str
    participant_id: str
    status: InvitationStatus
    sent_at: datetime = Field(default_factory=now_utc)


class BrandingConfig(BaseModel):
    sender_name: str
    logo_url: str | None = None
    primary_color: str | None = None


class MessageTemplate(BaseModel):
    template_id: str
    channel: str = "email"
    language: str
    subject: str
    body: str


class ClientConfig(BaseModel):
    client_id: str
    name: str
    enabled_languages: list[str] = Field(default_factory=lambda: ["en"])
    default_language: str = "en"
    branding: BrandingConfig
    templates: dict[str, MessageTemplate] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_language_is_enabled(self) -> "ClientConfig":
        if self.default_language not in self.enabled_languages:
            raise ValueError(f"default_language '{self.default_language}' must be one of "
                             f"enabled_languages {self.enabled_languages}")
        return self


class CommunicationDraft(BaseModel):
    id: str = Field(default_factory=lambda: new_id("comm"))
    employee_id: str
    client_id: str
    template_id: str
    language: str
    subject: str
    body: str
    status: CommunicationStatus = CommunicationStatus.DRAFT
    created_at: datetime = Field(default_factory=now_utc)


class BuddyCandidate(BaseModel):
    """The authorized source of eligible buddies for a client. Buddy assignment picks from this
    list -- never a name invented or suggested by a model."""

    employee_id: str
    full_name: str
    client_id: str
    team: str
    active: bool = True


class Notification(BaseModel):
    """The durable record of a notification delivered through NotificationPort (connectors/
    notifications): who was told what, over which channel, with the provider's delivery_ref in
    `detail`. Written only after the port accepted the message."""

    id: str = Field(default_factory=lambda: new_id("notif"))
    employee_id: str  # whose onboarding this notification concerns
    recipient_id: str
    kind: str  # e.g. "buddy_assignment"
    sent_at: datetime = Field(default_factory=now_utc)
    detail: dict = Field(default_factory=dict)


class OperationRecord(BaseModel):
    """Durable idempotency record for one external side effect (a calendar booking, a message).

    `key` names the logical operation (e.g. "welcome:<client>:<draft id>") and is unique.
    `operation_id` is the idempotency key sent to the provider: it is created once, before the
    first provider call, and reused by every retry of the same logical operation, so a provider
    that honours idempotency keys can never act twice. Once an operation is RELEASED (its effect
    undone), the same logical key gets a fresh operation_id."""

    key: str
    operation_id: str = Field(default_factory=lambda: new_id("op"))
    kind: str  # "calendar_booking" | "notification"
    client_id: str
    status: OperationStatus = OperationStatus.PENDING
    provider_ref: str | None = None
    detail: dict = Field(default_factory=dict)  # ids and statuses needed to resume or reconcile; no message bodies
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class JobStatus(StrEnum):
    RUNNING = "running"  # claimed; `lease_until` says when another worker may take it over
    DONE = "done"
    FAILED = "failed"  # retried on a later tick until max attempts


class JoiningDayJob(BaseModel):
    """One joining-day run for one tenant and one local date. The (client_id, run_date) pair is
    unique, so a date is processed once per tenant no matter how many workers or restarts."""

    client_id: str
    run_date: date
    status: JobStatus
    attempts: int = 0
    lease_until: datetime | None = None
    last_error: str | None = None
    updated_at: datetime = Field(default_factory=now_utc)


# --------------------------------------------------------------------------- errors

class DuplicateRecordError(ValueError):
    pass


class DuplicateTaskError(DuplicateRecordError):
    pass


class DuplicateSessionError(DuplicateRecordError):
    pass


class DuplicateOnboardingCaseError(DuplicateRecordError):
    pass


class TenantMismatchError(PermissionError):
    pass


class UnsupportedLanguageError(ValueError):
    pass


class ClientConfigurationError(KeyError):
    """A tenant's welcome configuration (ClientConfig: languages, branding, templates) is missing
    or unusable. A KeyError, so every existing "not found" path (BaseAgent's MISSING_DATA, the API's
    404 mapping) still applies; its message is plain text and names no record contents."""

    def __str__(self) -> str:  # KeyError would otherwise show the message as a quoted repr
        return str(self.args[0]) if self.args else self.__class__.__name__


class MissingClientConfigError(ClientConfigurationError):
    """No ClientConfig is set up for the tenant."""


class InvalidClientConfigError(ClientConfigurationError):
    """The tenant's ClientConfig exists but cannot produce a welcome (no such template, a language
    not enabled, an empty subject/body, or a placeholder the drafting code does not fill)."""


class ClientCalendarUnavailableError(ClientConfigurationError):
    """No usable scheduling calendar for the tenant: no active client configuration, or no approved
    configuration version effective today. Scheduling stops (routed to HR) rather than falling back to
    a default working week or holiday list."""


class ClientCalendar(BaseModel):
    """A tenant's authoritative scheduling calendar (supabase mode: client_configs, client_holidays and
    the effective client_config_versions row). working_days uses 1 = Monday .. 7 = Sunday, the
    convention of client_configs.working_days."""

    client_id: str
    timezone: str
    working_days: list[int]
    holidays: list[date] = Field(default_factory=list)
    config_version: int | None = None

    @property
    def weekend_weekdays(self) -> list[int]:
        """The non-working days in Python's weekday() numbering (Mon=0 .. Sun=6), the form
        services/scheduling.py's settings use."""
        working = {d - 1 for d in self.working_days}
        return [d for d in range(7) if d not in working]

    def scheduling_settings(self, base: dict) -> dict:
        """`base` (policy.yaml settings) with this tenant's timezone, weekend and holidays."""
        return {**base, "timezone": self.timezone, "weekend_weekdays": self.weekend_weekdays,
                "holidays": sorted(h.isoformat() for h in self.holidays)}


class BuddyCapacityExceededError(ValueError):
    """Assigning this buddy would exceed their max_active_buddies. Nothing is assigned."""


class OnboardingCaseNotFoundError(KeyError):
    """No authoritative onboarding case exists for the employee in this tenant. UC05 never creates
    one: the case is opened upstream (UC01)."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else self.__class__.__name__


class CaseOwnershipError(PermissionError):
    """A write to a record another use case owns (onboarding_cases belongs to UC01)."""


class TenantScopeRequiredError(PermissionError):
    """A tenant-scoped lookup was attempted without a verified tenant (client_id)."""
