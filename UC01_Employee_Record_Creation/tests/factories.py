"""Synthetic data builders for model tests. No real personal data."""

from datetime import date, datetime, timezone

from sub_agent.adapters.memory import (
    InMemoryApprovalRepository,
    InMemoryAuditLog,
    InMemoryCaseRepository,
    InMemoryDiscrepancyRepository,
    InMemoryDocumentStore,
    InMemoryEmployeeNumberGenerator,
    InMemoryEventPublisher,
    InMemoryFormRepository,
    InMemoryHolidayCalendar,
    InMemoryHrisRecordRepository,
    InMemoryOfferRepository,
    InMemoryOnboardingFormSource,
    InMemoryPendingStartRepository,
    InMemoryPersonDirectory,
    StubHrisGateway,
)
from sub_agent.case_status import U9Deps
from sub_agent.compare_sources import CompareDeps
from sub_agent.deps import UC01Deps
from sub_agent.intake.email_intake import IntakeDeps
from sub_agent.models import (
    Case,
    CasePlacement,
    OnboardingForm,
    PersonRecord,
    PersonRecordKind,
    Role,
    VariantFlags,
    Viewer,
    WorkLocationType,
)
from sub_agent.onboarding_entry import OnboardingDeps
from sub_agent.policy import Policy, load_policy
from sub_agent.service import CaseService

# Fixed clock for U8/U6/U7 tests: 2026-01-15 is a Thursday.
TEST_TODAY = date(2026, 1, 15)


def make_form(**overrides) -> OnboardingForm:
    defaults = dict(
        client_id="client-synthetic-001",
        version=1,
        candidate_name="Test Candidate",
        candidate_email="test.candidate@example.test",
        designation="Software Engineer",
        joining_date=date(2026, 1, 5),
        work_location_type=WorkLocationType.OFFICE,
        salary_and_terms="Base: 100000 SYNTHETIC/yr; benefits: standard",
    )
    defaults.update(overrides)
    return OnboardingForm(**defaults)


def make_case(**overrides) -> Case:
    defaults = dict(
        client_id="client-synthetic-001",
        candidate_name="Test Candidate",
        candidate_email="test.candidate@example.test",
        hr_officer_id="hr-officer-001",
        joining_date=date(2026, 1, 5),
        work_location_type=WorkLocationType.OFFICE,
        variants=VariantFlags(),
        current_form_version=1,
        # location_name is required at case creation (approved
        # 2026-11-04). The other placement attributes are optional
        # or defaulted, so they are deliberately left unset here.
        location_name="Dubai Office",
    )
    defaults.update(overrides)
    return Case(**defaults)


def make_placement(**overrides) -> CasePlacement:
    """Placement attributes for a case.

    location_name is the only required one (approved 2026-11-04);
    the optional and defaulted fields are left unset on purpose so
    the defaults are exercised by every test that does not care.
    """
    defaults = dict(location_name="Synthetic Office, Dubai")
    defaults.update(overrides)
    return CasePlacement(**defaults)


def make_person(**overrides) -> PersonRecord:
    defaults = dict(
        client_id="client-synthetic-001",
        email="existing.person@example.test",
        full_name="Existing Person",
        kind=PersonRecordKind.OPEN_CASE,
    )
    defaults.update(overrides)
    return PersonRecord(**defaults)


def make_approved_form(**overrides) -> OnboardingForm:
    """A version-1 form already APPROVED, ready to open a case from."""
    form = make_form(**overrides)
    return form.approve(approved_by="hr-officer-001", approved_at=utc_timestamp())


def make_deps(person_directory: InMemoryPersonDirectory | None = None) -> OnboardingDeps:
    """A fresh bundle of in-memory adapters for U3/U4/orchestrator tests."""
    return OnboardingDeps(
        case_repo=InMemoryCaseRepository(),
        form_repo=InMemoryFormRepository(),
        approval_repo=InMemoryApprovalRepository(),
        person_directory=person_directory or InMemoryPersonDirectory(),
        document_store=InMemoryDocumentStore(),
        number_generator=InMemoryEmployeeNumberGenerator(),
        event_publisher=InMemoryEventPublisher(),
        audit_log=InMemoryAuditLog(),
    )


def make_linked_case_and_form(**form_overrides) -> tuple[Case, OnboardingForm]:
    """An APPROVED version-1 form linked to a matching Case (U5 tests)."""
    form = make_approved_form(**form_overrides)
    case = make_case(
        client_id=form.client_id,
        candidate_name=form.candidate_name,
        candidate_email=form.candidate_email,
        joining_date=form.joining_date,
        work_location_type=form.work_location_type,
        current_form_version=form.version,
    )
    linked_form = form.with_case(case.case_id)
    return case, linked_form


TEST_FINGERPRINT_KEY = b"test-fixed-fingerprint-key-not-for-prod"


def make_intake_deps(onboarding_deps: OnboardingDeps | None = None) -> IntakeDeps:
    """A fresh bundle of in-memory adapters for the [Proposed] email-intake
    simulator tests (OQ-01/Q-11)."""
    return IntakeDeps(
        offer_repo=InMemoryOfferRepository(),
        form_source=InMemoryOnboardingFormSource(),
        pending_repo=InMemoryPendingStartRepository(),
        onboarding_deps=onboarding_deps or make_deps(),
        # The test IS the caller here, so it supplies the required
        # location_name. Left unset, try_start correctly refuses to
        # open a case rather than inventing a location.
        placement=make_placement(),
    )


def make_compare_deps() -> CompareDeps:
    """A fresh bundle of in-memory adapters for U5 compare/resolve tests."""
    return CompareDeps(
        discrepancy_repo=InMemoryDiscrepancyRepository(),
        approval_repo=InMemoryApprovalRepository(),
        audit_log=InMemoryAuditLog(),
        fingerprint_key=TEST_FINGERPRINT_KEY,
    )


def make_change_deps(
    holiday_calendar: InMemoryHolidayCalendar | None = None,
    hris_record_repo: InMemoryHrisRecordRepository | None = None,
    hris_gateway: StubHrisGateway | None = None,
) -> UC01Deps:
    """A fresh bundle of in-memory adapters for U6/U7/U8 tests.

    Uses a fixed clock (TEST_TODAY) instead of the real date.today, so
    "joining date is in the past" tests are deterministic.
    """
    return UC01Deps(
        case_repo=InMemoryCaseRepository(),
        form_repo=InMemoryFormRepository(),
        approval_repo=InMemoryApprovalRepository(),
        discrepancy_repo=InMemoryDiscrepancyRepository(),
        hris_record_repo=hris_record_repo or InMemoryHrisRecordRepository(),
        hris_gateway=hris_gateway or StubHrisGateway(),
        audit_log=InMemoryAuditLog(),
        publisher=InMemoryEventPublisher(),
        holiday_calendar=holiday_calendar or InMemoryHolidayCalendar(),
        today=lambda: TEST_TODAY,
    )


def seed_linked_case_and_form(deps: UC01Deps, **form_overrides) -> tuple[Case, OnboardingForm]:
    """Build a linked Case/Form pair and save both into deps' repos."""
    case, form = make_linked_case_and_form(**form_overrides)
    deps.case_repo.save(case)
    deps.form_repo.save(form)
    return case, form


def make_viewer(**overrides) -> Viewer:
    defaults = dict(
        user_id="hr-officer-001",
        role=Role.HR_OFFICER,
        client_ids=frozenset({"client-synthetic-001"}),
    )
    defaults.update(overrides)
    return Viewer(**defaults)


def make_u9_deps(deps: UC01Deps) -> U9Deps:
    """A U9Deps view over the same repos as an existing UC01Deps bundle."""
    return U9Deps(
        case_repo=deps.case_repo,
        form_repo=deps.form_repo,
        approval_repo=deps.approval_repo,
        discrepancy_repo=deps.discrepancy_repo,
        hris_record_repo=deps.hris_record_repo,
        audit_log=deps.audit_log,
    )


_POLICY_CACHE: Policy | None = None


def make_policy() -> Policy:
    global _POLICY_CACHE
    if _POLICY_CACHE is None:
        _POLICY_CACHE = load_policy()
    return _POLICY_CACHE


def make_case_service(deps: UC01Deps | None = None) -> CaseService:
    deps = deps or make_change_deps()
    return CaseService(deps, TEST_FINGERPRINT_KEY, make_policy())


def utc_timestamp() -> datetime:
    return datetime.now(timezone.utc)
