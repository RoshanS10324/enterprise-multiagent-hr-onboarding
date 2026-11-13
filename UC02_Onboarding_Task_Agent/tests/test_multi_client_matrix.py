"""Dynamic-checklist validation across 20 synthetic employees and 4 clients.

Why this module exists
----------------------

The existing suite proves each rule in isolation. What it did not prove is the
claim UC02-BR-02 actually makes: that **the checklist is selected per employee**
and that two employees of two different clients, differing only in the
dimensions the configuration filters on, get demonstrably different plans.

Nothing here re-implements a rule. Every plan is produced by the existing
:class:`OnboardingService.create_onboarding` ->
:func:`planner.build_tasks` -> :meth:`ClientOnboardingConfig.applicable` path,
and every explanation by the existing :meth:`OnboardingService.checklist`.

Independent expectations
------------------------

The expected values in :data:`CASES` are **hand-derived from the configuration
tables and the documented rules**, not produced by calling the generator. That
is the whole point: an expectation computed by the code under test proves only
that the code agrees with itself.

The same applies to due dates. :data:`DUE_BY_OFFSET` was worked out by counting
Monday-to-Friday days backwards from the joining date on a calendar, and
:data:`HOLIDAY_DUE_BY_OFFSET` does the same across the 1-3 December 2026 public
holidays. Neither calls :func:`calendar_uae.add_business_days`.

Two clients are defined here rather than in ``synthetic.py``: CLIENT-GAMMA and
CLIENT-DELTA exist only to exercise the ``nationality_groups``, ``grades`` and
``role_ids`` filter dimensions, which no shipped client configures. They are
test fixtures, so they live with the tests. CLIENT-ALPHA, CLIENT-BETA and
CLIENT-EMPTY are reused unchanged from ``synthetic.build_registry()``.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import date

from onboarding_task_management.calendar_uae import UAE_PUBLIC_HOLIDAYS
from onboarding_task_management.contracts import (
    Actor,
    ChecklistContext,
    OnboardingStatus,
    Role,
    TaskCategory,
    TaskStatus,
)
from onboarding_task_management.errors import (
    DataIsolationError,
    DuplicateOnboardingError,
    MissingConfigurationError,
    PreconditionError,
)
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    MilestoneTemplate,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import (
    OnboardingService,
    OnboardingTrigger,
)
from onboarding_task_management.synthetic import (
    ATTR_BV_REQUIRED,
    ATTR_FAMILY_INSURANCE,
    ATTR_HR_SYSTEM_ACTIVATION,
    ATTR_INSURANCE_REQUIRED,
    ATTR_NEEDS_ACCOMMODATION,
    ATTR_NEEDS_FOOD,
    ATTR_NEEDS_TRAVEL,
    ATTR_NON_LIABILITY_LETTER,
    ATTR_SALARY_TRANSFER_LETTER,
    ATTR_VISA_REQUIRED,
    CLIENT_ALPHA,
    CLIENT_BETA,
    CLIENT_EMPTY,
    GRADE_L2,
    GRADE_L3,
    GRADE_L4,
    LOCATION_ABU_DHABI,
    LOCATION_DUBAI,
    LOCATION_REMOTE,
    PROJECT_APOLLO,
    PROJECT_ZEPHYR,
    accepted_offer,
    approved_form,
    build_registry,
)

# ---------------------------------------------------------------------------
# Fixed dates. Explicit, because UC-02 never reads the clock in business logic.
# ---------------------------------------------------------------------------

AS_OF = date(2026, 10, 15)
JOINING = date(2026, 11, 16)          # a Monday, no holidays in its lead-in
HOLIDAY_JOINING = date(2026, 12, 7)   # a Monday behind 1-3 Dec 2026 holidays

#: Hand-counted business days back from Monday 2026-11-16. Weekends only -
#: there is no UAE public holiday between 2026-10-29 and 2026-11-18.
#: e.g. -10: Nov 13,12,11,10,9 then 6,5,4,3,2 -> 2026-11-02.
DUE_BY_OFFSET: dict[int, date] = {
    -12: date(2026, 10, 29),
    -10: date(2026, 11, 2),
    -9: date(2026, 11, 3),
    -8: date(2026, 11, 4),
    -7: date(2026, 11, 5),
    -6: date(2026, 11, 6),
    -5: date(2026, 11, 9),
    -4: date(2026, 11, 10),
    -3: date(2026, 11, 11),
    -2: date(2026, 11, 12),
    0: date(2026, 11, 16),
    2: date(2026, 11, 18),
}

#: Hand-counted back from Monday 2026-12-07, skipping Tue 1, Wed 2 and Thu 3
#: December 2026 (Commemoration Day / National Day) as well as weekends.
#: -1: Dec 4. -2: Dec 3/2/1 are holidays, so Nov 30. -3: Nov 27. -4: Nov 26.
#: -5: Nov 25. -6: Nov 24. -7: Nov 23.
HOLIDAY_DUE_BY_OFFSET: dict[int, date] = {
    -1: date(2026, 12, 4),
    -2: date(2026, 11, 30),
    -3: date(2026, 11, 27),
    -4: date(2026, 11, 26),
    -5: date(2026, 11, 25),
    -6: date(2026, 11, 24),
    -7: date(2026, 11, 23),
}

# ---------------------------------------------------------------------------
# Test-owned clients. These exercise the three filter dimensions that no
# shipped client configures: nationality_groups, grades and role_ids.
# ---------------------------------------------------------------------------

CLIENT_GAMMA = "CLIENT-GAMMA"
CLIENT_DELTA = "CLIENT-DELTA"

GROUP_EXPAT = "grp-expat"
GROUP_LOCAL = "grp-local"
GROUP_UNMAPPED = "grp-unmapped"   # matches no configured group

ROLE_FIELD_ENGINEER = "field_engineer"
ROLE_ANALYST = "analyst"

GAMMA_CONFIG = ClientOnboardingConfig(
    approval_status="approved",  # test fixture
    client_id=CLIENT_GAMMA,
    templates=(
        TaskTemplate(
            key="gamma_hr_record", category=TaskCategory.HR_RECORD,
            title="Create employee record", title_ar="إنشاء سجل الموظف",
            owner=Role.HR_OPS, offset_days=-8,
        ),
        TaskTemplate(
            key="gamma_visa_pack", category=TaskCategory.VISA,
            title="Assemble visa pack", title_ar="تجهيز ملف التأشيرة",
            owner=Role.PRO, offset_days=-6, depends_on=("gamma_hr_record",),
            nationality_groups=(GROUP_EXPAT,), evidence_required=True,
        ),
        TaskTemplate(
            key="gamma_local_registration", category=TaskCategory.HR_RECORD,
            title="Local labour registration", title_ar="التسجيل العمالي المحلي",
            owner=Role.HR_OFFICER, offset_days=-5,
            depends_on=("gamma_hr_record",), nationality_groups=(GROUP_LOCAL,),
        ),
        TaskTemplate(
            key="gamma_exec_briefing", category=TaskCategory.ORIENTATION,
            title="Executive briefing", title_ar="إحاطة تنفيذية",
            owner=Role.HR_LEAD, offset_days=-2,
            depends_on=("gamma_hr_record",), grades=(GRADE_L4,),
        ),
        TaskTemplate(
            key="gamma_security_clearance", category=TaskCategory.BACKGROUND_VERIFICATION,
            title="Security clearance", title_ar="التصريح الأمني",
            owner=Role.HR_OFFICER, offset_days=-4,
            depends_on=("gamma_hr_record",),
            nationality_groups=(GROUP_EXPAT,), grades=(GRADE_L4,),
            evidence_required=True,
        ),
    ),
    milestones=(
        MilestoneTemplate(
            key="gamma_record_ready", title="Record ready", title_ar="السجل جاهز",
            offset_days=-7, requires=("gamma_hr_record",),
        ),
    ),
)

DELTA_CONFIG = ClientOnboardingConfig(
    approval_status="approved",  # test fixture
    client_id=CLIENT_DELTA,
    templates=(
        TaskTemplate(
            key="delta_record", category=TaskCategory.HR_RECORD,
            title="Create employee record", title_ar="إنشاء سجل الموظف",
            owner=Role.HR_OPS, offset_days=-7,
        ),
        TaskTemplate(
            key="delta_equipment", category=TaskCategory.IT_PROVISIONING,
            title="Issue field equipment", title_ar="تسليم معدات الميدان",
            owner=Role.IT_MANAGER, offset_days=-5, depends_on=("delta_record",),
            role_ids=(ROLE_FIELD_ENGINEER,), evidence_required=True,
        ),
        TaskTemplate(
            key="delta_safety_training", category=TaskCategory.ORIENTATION,
            title="Site safety training", title_ar="تدريب السلامة بالموقع",
            owner=Role.HR_OFFICER, offset_days=-3,
            depends_on=("delta_equipment",), role_ids=(ROLE_FIELD_ENGINEER,),
            evidence_required=True,
        ),
        TaskTemplate(
            key="delta_desk", category=TaskCategory.WORKPLACE,
            title="Allocate desk", title_ar="تخصيص مكتب",
            owner=Role.FACILITIES, offset_days=-2, depends_on=("delta_record",),
            role_ids=(ROLE_ANALYST,),
        ),
    ),
    milestones=(
        MilestoneTemplate(
            key="delta_field_ready", title="Field ready", title_ar="جاهز للميدان",
            offset_days=-1,
            requires=("delta_equipment", "delta_safety_training"),
            role_ids=(ROLE_FIELD_ENGINEER,),
        ),
    ),
)


def matrix_registry():
    """The three shipped clients plus the two test-owned ones."""
    registry = build_registry()
    registry.register(GAMMA_CONFIG)
    registry.register(DELTA_CONFIG)
    return registry


# ---------------------------------------------------------------------------
# Expected owners, hand-copied from the configuration tables.
# ---------------------------------------------------------------------------

EXPECTED_OWNER: dict[str, Role] = {
    # CLIENT-ALPHA
    "hr_record_create": Role.HR_OPS,
    "collect_documents": Role.HR_OPS,
    "visa_application": Role.HR_OPS,
    "it_laptop": Role.IT_MANAGER,
    "it_accounts": Role.IT_MANAGER,
    "access_apollo": Role.IT_MANAGER,
    "access_zephyr": Role.IT_MANAGER,
    "desk_allocation": Role.FACILITIES,
    "remote_kit_shipping": Role.FACILITIES,
    "payroll_setup": Role.FINANCE,
    "orientation_schedule": Role.HR_OPS,
    "policy_ack": Role.EMPLOYEE,
    "background_verification": Role.HR_OFFICER,
    "medical_test": Role.HR_OPS,
    "biometrics_capture": Role.HR_OPS,
    "visa_pack_handover": Role.HR_OPS,
    "visa_processing": Role.PRO,
    "insurance_enrolment": Role.HR_OPS,
    "family_insurance_enrolment": Role.HR_OPS,
    "travel_booking": Role.HR_OPS,
    "accommodation_booking": Role.FACILITIES,
    "food_arrangement": Role.FACILITIES,
    "salary_transfer_letter": Role.HR_OFFICER,
    "non_liability_letter": Role.HR_OFFICER,
    "hr_system_activation": Role.HR_OPS,
    # CLIENT-GAMMA
    "gamma_hr_record": Role.HR_OPS,
    "gamma_visa_pack": Role.PRO,
    "gamma_local_registration": Role.HR_OFFICER,
    "gamma_exec_briefing": Role.HR_LEAD,
    "gamma_security_clearance": Role.HR_OFFICER,
    # CLIENT-DELTA
    "delta_record": Role.HR_OPS,
    "delta_equipment": Role.IT_MANAGER,
    "delta_safety_training": Role.HR_OFFICER,
    "delta_desk": Role.FACILITIES,
}

#: Offset per (client_id, template key), hand-copied from the configuration
#: tables. Keyed by client because CLIENT-BETA deliberately reuses two of
#: CLIENT-ALPHA's template names (``hr_record_create``, ``it_accounts``) with
#: its own shorter lead times -- which is itself a client-isolation property
#: worth asserting. Used with DUE_BY_OFFSET to assert exact due dates.
EXPECTED_OFFSET: dict[tuple[str, str], int] = {
    (CLIENT_ALPHA, "hr_record_create"): -10,
    (CLIENT_ALPHA, "collect_documents"): -8,
    (CLIENT_ALPHA, "visa_application"): -7,
    (CLIENT_ALPHA, "it_laptop"): -5,
    (CLIENT_ALPHA, "it_accounts"): -3,
    (CLIENT_ALPHA, "access_apollo"): -2,
    (CLIENT_ALPHA, "access_zephyr"): -2,
    (CLIENT_ALPHA, "desk_allocation"): -2,
    (CLIENT_ALPHA, "remote_kit_shipping"): -6,
    (CLIENT_ALPHA, "payroll_setup"): -4,
    (CLIENT_ALPHA, "orientation_schedule"): 0,
    (CLIENT_ALPHA, "policy_ack"): 2,
    (CLIENT_ALPHA, "background_verification"): -12,
    (CLIENT_ALPHA, "medical_test"): -9,
    (CLIENT_ALPHA, "biometrics_capture"): -8,
    (CLIENT_ALPHA, "visa_pack_handover"): -7,
    (CLIENT_ALPHA, "visa_processing"): -3,
    (CLIENT_ALPHA, "insurance_enrolment"): -5,
    (CLIENT_ALPHA, "family_insurance_enrolment"): -5,
    (CLIENT_ALPHA, "travel_booking"): -6,
    (CLIENT_ALPHA, "accommodation_booking"): -6,
    (CLIENT_ALPHA, "food_arrangement"): -4,
    (CLIENT_ALPHA, "salary_transfer_letter"): -4,
    (CLIENT_ALPHA, "non_liability_letter"): -4,
    (CLIENT_ALPHA, "hr_system_activation"): 0,
    # CLIENT-BETA: same two names, different lead times.
    (CLIENT_BETA, "hr_record_create"): -6,
    (CLIENT_BETA, "it_accounts"): -3,
    (CLIENT_GAMMA, "gamma_hr_record"): -8,
    (CLIENT_GAMMA, "gamma_visa_pack"): -6,
    (CLIENT_GAMMA, "gamma_local_registration"): -5,
    (CLIENT_GAMMA, "gamma_exec_briefing"): -2,
    (CLIENT_GAMMA, "gamma_security_clearance"): -4,
    (CLIENT_DELTA, "delta_record"): -7,
    (CLIENT_DELTA, "delta_equipment"): -5,
    (CLIENT_DELTA, "delta_safety_training"): -3,
    (CLIENT_DELTA, "delta_desk"): -2,
}

#: Template keys whose configuration sets ``evidence_required``.
EXPECTED_EVIDENCE_KEYS = frozenset({
    "background_verification", "medical_test", "biometrics_capture",
    "visa_pack_handover", "visa_processing", "insurance_enrolment",
    "family_insurance_enrolment", "travel_booking", "accommodation_booking",
    "salary_transfer_letter", "non_liability_letter", "hr_system_activation",
    "gamma_visa_pack", "gamma_security_clearance",
    "delta_equipment", "delta_safety_training",
})

# CLIENT-ALPHA building blocks, hand-derived from the template filters.
ALPHA_UNIVERSAL = {
    "hr_record_create", "collect_documents", "it_laptop", "it_accounts",
    "payroll_setup", "orientation_schedule", "policy_ack",
}
ALPHA_ONSITE = {"visa_application", "desk_allocation"}   # Dubai / Abu Dhabi
ALPHA_REMOTE = {"remote_kit_shipping"}                   # Remote-IN
ALPHA_VISA = {
    "medical_test", "biometrics_capture", "visa_pack_handover",
    "visa_processing",
}


@dataclass(frozen=True)
class Case:
    """One synthetic employee and the plan the rules say they should get."""

    employee_id: str
    client_id: str
    role_id: str
    grade: str
    location: str
    project: str
    nationality_group: str
    attributes: tuple[str, ...]
    expected_keys: frozenset[str]
    expected_ready: frozenset[str]
    expected_milestones: frozenset[str]
    note: str
    joining_date: date = JOINING
    #: Templates the client configured that this employee must NOT receive.
    expected_absent: frozenset[str] = field(default_factory=frozenset)


def alpha(keys: set[str]) -> frozenset[str]:
    return frozenset(keys)


CASES: tuple[Case, ...] = (
    # ---------------- CLIENT-ALPHA (8) ----------------
    Case("E-01", CLIENT_ALPHA, "backend_engineer", GRADE_L3, LOCATION_DUBAI,
         PROJECT_APOLLO, "", (),
         alpha(ALPHA_UNIVERSAL | ALPHA_ONSITE | {"access_apollo"}),
         frozenset({"hr_record_create"}),
         frozenset({"paperwork_complete", "workplace_ready", "day_one_ready",
                    "onboarding_complete"}),
         "Dubai / apollo, no extended activity",
         expected_absent=frozenset({"access_zephyr", "remote_kit_shipping",
                                    "travel_booking", "visa_processing"})),
    Case("E-02", CLIENT_ALPHA, "data_engineer", GRADE_L3, LOCATION_ABU_DHABI,
         PROJECT_ZEPHYR, "", (),
         alpha(ALPHA_UNIVERSAL | ALPHA_ONSITE | {"access_zephyr"}),
         frozenset({"hr_record_create"}),
         frozenset({"paperwork_complete", "workplace_ready", "day_one_ready",
                    "onboarding_complete"}),
         "Abu Dhabi / zephyr swaps the access task",
         expected_absent=frozenset({"access_apollo", "remote_kit_shipping"})),
    Case("E-03", CLIENT_ALPHA, "ml_engineer", GRADE_L3, LOCATION_REMOTE,
         PROJECT_APOLLO, "", (),
         alpha(ALPHA_UNIVERSAL | ALPHA_REMOTE | {"access_apollo"}),
         frozenset({"hr_record_create"}),
         frozenset({"paperwork_complete", "day_one_ready",
                    "onboarding_complete"}),
         "Remote-IN: no visa_application, no desk, workplace_ready not applicable",
         expected_absent=frozenset({"visa_application", "desk_allocation",
                                    "access_zephyr"})),
    Case("E-04", CLIENT_ALPHA, "devops_engineer", GRADE_L4, LOCATION_DUBAI,
         PROJECT_APOLLO, "", (ATTR_VISA_REQUIRED,),
         alpha(ALPHA_UNIVERSAL | ALPHA_ONSITE | {"access_apollo"} | ALPHA_VISA),
         frozenset({"hr_record_create", "medical_test"}),
         frozenset({"paperwork_complete", "workplace_ready", "day_one_ready",
                    "onboarding_complete", "work_authorisation_ready"}),
         "visa_required adds the four work-authorisation tasks",
         expected_absent=frozenset({"background_verification",
                                    "travel_booking"})),
    Case("E-05", CLIENT_ALPHA, "qa_engineer", GRADE_L2, LOCATION_DUBAI,
         PROJECT_ZEPHYR, "", (ATTR_BV_REQUIRED,),
         alpha(ALPHA_UNIVERSAL | ALPHA_ONSITE
               | {"access_zephyr", "background_verification"}),
         frozenset({"hr_record_create"}),
         frozenset({"paperwork_complete", "workplace_ready", "day_one_ready",
                    "onboarding_complete", "verification_cleared"}),
         "bv_required adds background verification and its milestone"),
    Case("E-06", CLIENT_ALPHA, "backend_engineer", GRADE_L4, LOCATION_DUBAI,
         PROJECT_APOLLO, "",
         (ATTR_INSURANCE_REQUIRED, ATTR_FAMILY_INSURANCE),
         alpha(ALPHA_UNIVERSAL | ALPHA_ONSITE
               | {"access_apollo", "insurance_enrolment",
                  "family_insurance_enrolment"}),
         frozenset({"hr_record_create", "insurance_enrolment"}),
         frozenset({"paperwork_complete", "workplace_ready", "day_one_ready",
                    "onboarding_complete"}),
         "family insurance depends on base insurance"),
    Case("E-07", CLIENT_ALPHA, "backend_engineer", GRADE_L4, LOCATION_DUBAI,
         PROJECT_APOLLO, "",
         (ATTR_BV_REQUIRED, ATTR_VISA_REQUIRED, ATTR_INSURANCE_REQUIRED,
          ATTR_FAMILY_INSURANCE, ATTR_NEEDS_TRAVEL, ATTR_NEEDS_ACCOMMODATION,
          ATTR_NEEDS_FOOD, ATTR_SALARY_TRANSFER_LETTER,
          ATTR_NON_LIABILITY_LETTER, ATTR_HR_SYSTEM_ACTIVATION),
         alpha(ALPHA_UNIVERSAL | ALPHA_ONSITE | {"access_apollo"} | ALPHA_VISA
               | {"background_verification", "insurance_enrolment",
                  "family_insurance_enrolment", "travel_booking",
                  "accommodation_booking", "food_arrangement",
                  "salary_transfer_letter", "non_liability_letter",
                  "hr_system_activation"}),
         frozenset({"hr_record_create", "medical_test", "insurance_enrolment",
                    "travel_booking", "accommodation_booking",
                    "food_arrangement"}),
         frozenset({"paperwork_complete", "workplace_ready", "day_one_ready",
                    "onboarding_complete", "verification_cleared",
                    "work_authorisation_ready", "relocation_ready"}),
         "full relocation: every extended activity"),
    Case("E-08", CLIENT_ALPHA, "qa_engineer", GRADE_L2, LOCATION_ABU_DHABI,
         PROJECT_APOLLO, "", (ATTR_NEEDS_TRAVEL, ATTR_NEEDS_ACCOMMODATION),
         alpha(ALPHA_UNIVERSAL | ALPHA_ONSITE
               | {"access_apollo", "travel_booking",
                  "accommodation_booking"}),
         frozenset({"hr_record_create", "travel_booking",
                    "accommodation_booking"}),
         frozenset({"paperwork_complete", "workplace_ready", "day_one_ready",
                    "onboarding_complete", "relocation_ready"}),
         "relocation_ready needs both travel and accommodation"),

    # ---------------- CLIENT-BETA (3) — minimal config ----------------
    Case("E-09", CLIENT_BETA, "backend_engineer", GRADE_L3, LOCATION_DUBAI,
         PROJECT_APOLLO, "", (),
         frozenset({"hr_record_create", "it_accounts"}),
         frozenset({"hr_record_create"}), frozenset(),
         "BETA configures two tasks; ALPHA's tasks must not leak in",
         expected_absent=frozenset({"collect_documents", "it_laptop",
                                    "payroll_setup", "desk_allocation"})),
    Case("E-10", CLIENT_BETA, "ml_engineer", GRADE_L4, LOCATION_REMOTE,
         PROJECT_ZEPHYR, "", (ATTR_VISA_REQUIRED, ATTR_NEEDS_TRAVEL),
         frozenset({"hr_record_create", "it_accounts"}),
         frozenset({"hr_record_create"}), frozenset(),
         "BETA has no attribute-gated templates, so flags change nothing",
         expected_absent=frozenset({"travel_booking", "visa_processing"})),
    Case("E-11", CLIENT_BETA, "analyst", GRADE_L2, LOCATION_ABU_DHABI,
         PROJECT_APOLLO, GROUP_EXPAT, (),
         frozenset({"hr_record_create", "it_accounts"}),
         frozenset({"hr_record_create"}), frozenset(),
         "BETA configures no nationality filter, so the group is ignored",
         expected_absent=frozenset({"gamma_visa_pack"})),

    # ---------------- CLIENT-GAMMA (5) — nationality + grade ----------------
    Case("E-12", CLIENT_GAMMA, "backend_engineer", GRADE_L3, LOCATION_DUBAI,
         PROJECT_APOLLO, GROUP_EXPAT, (),
         frozenset({"gamma_hr_record", "gamma_visa_pack"}),
         frozenset({"gamma_hr_record"}),
         frozenset({"gamma_record_ready"}),
         "expat group gets the visa pack, not local registration",
         expected_absent=frozenset({"gamma_local_registration",
                                    "gamma_exec_briefing",
                                    "gamma_security_clearance"})),
    Case("E-13", CLIENT_GAMMA, "backend_engineer", GRADE_L3, LOCATION_DUBAI,
         PROJECT_APOLLO, GROUP_LOCAL, (),
         frozenset({"gamma_hr_record", "gamma_local_registration"}),
         frozenset({"gamma_hr_record"}),
         frozenset({"gamma_record_ready"}),
         "local group gets registration, not the visa pack",
         expected_absent=frozenset({"gamma_visa_pack",
                                    "gamma_security_clearance"})),
    Case("E-14", CLIENT_GAMMA, "data_engineer", GRADE_L4, LOCATION_DUBAI,
         PROJECT_APOLLO, GROUP_EXPAT, (),
         frozenset({"gamma_hr_record", "gamma_visa_pack",
                    "gamma_exec_briefing", "gamma_security_clearance"}),
         frozenset({"gamma_hr_record"}),
         frozenset({"gamma_record_ready"}),
         "L4 expat: both dimensions must match for security clearance",
         expected_absent=frozenset({"gamma_local_registration"})),
    Case("E-15", CLIENT_GAMMA, "data_engineer", GRADE_L4, LOCATION_DUBAI,
         PROJECT_APOLLO, GROUP_LOCAL, (),
         frozenset({"gamma_hr_record", "gamma_local_registration",
                    "gamma_exec_briefing"}),
         frozenset({"gamma_hr_record"}),
         frozenset({"gamma_record_ready"}),
         "L4 local: grade matches, nationality does not -> no clearance",
         expected_absent=frozenset({"gamma_security_clearance",
                                    "gamma_visa_pack"})),
    Case("E-16", CLIENT_GAMMA, "qa_engineer", GRADE_L2, LOCATION_ABU_DHABI,
         PROJECT_ZEPHYR, GROUP_UNMAPPED, (),
         frozenset({"gamma_hr_record"}),
         frozenset({"gamma_hr_record"}),
         frozenset({"gamma_record_ready"}),
         "a group matching no configured set gets only unfiltered tasks",
         expected_absent=frozenset({"gamma_visa_pack",
                                    "gamma_local_registration",
                                    "gamma_exec_briefing",
                                    "gamma_security_clearance"})),

    # ---------------- CLIENT-DELTA (4) — role filtering ----------------
    Case("E-17", CLIENT_DELTA, ROLE_FIELD_ENGINEER, GRADE_L3, LOCATION_DUBAI,
         PROJECT_APOLLO, "", (),
         frozenset({"delta_record", "delta_equipment",
                    "delta_safety_training"}),
         frozenset({"delta_record"}),
         frozenset({"delta_field_ready"}),
         "field engineer: three-deep dependency chain",
         expected_absent=frozenset({"delta_desk"})),
    Case("E-18", CLIENT_DELTA, ROLE_ANALYST, GRADE_L3, LOCATION_DUBAI,
         PROJECT_APOLLO, "", (),
         frozenset({"delta_record", "delta_desk"}),
         frozenset({"delta_record"}),
         frozenset(),
         "analyst: desk only, and the field milestone is not applicable",
         expected_absent=frozenset({"delta_equipment",
                                    "delta_safety_training"})),
    Case("E-19", CLIENT_DELTA, ROLE_FIELD_ENGINEER, GRADE_L4,
         LOCATION_ABU_DHABI, PROJECT_ZEPHYR, GROUP_EXPAT, (),
         frozenset({"delta_record", "delta_equipment",
                    "delta_safety_training"}),
         frozenset({"delta_record"}),
         frozenset({"delta_field_ready"}),
         "DELTA filters on role only: grade, location and group are ignored",
         expected_absent=frozenset({"delta_desk"})),
    Case("E-20", CLIENT_DELTA, ROLE_ANALYST, GRADE_L2, LOCATION_DUBAI,
         PROJECT_APOLLO, "", (),
         frozenset({"delta_record", "delta_desk"}),
         frozenset({"delta_record"}),
         frozenset(),
         "second analyst, holiday-crossing joining date",
         joining_date=HOLIDAY_JOINING,
         expected_absent=frozenset({"delta_equipment"})),
)

CLIENTS_UNDER_TEST = frozenset(c.client_id for c in CASES)


def actor_for(case: Case, actor_id: str = "U-HR-1") -> Actor:
    return Actor(actor_id=actor_id, role=Role.HR_OPS, client_id=case.client_id)


def trigger_for(case: Case) -> OnboardingTrigger:
    """Build the trigger from the case. Reuses the shipped offer/form helpers."""
    return OnboardingTrigger(
        client_id=case.client_id,
        employee_id=case.employee_id,
        joining_date=case.joining_date,
        role_id=case.role_id,
        department="technology",
        manager_id=f"MGR-{case.client_id}",
        location=case.location,
        project=case.project,
        grade=case.grade,
        nationality_group=case.nationality_group,
        attributes=case.attributes,
        offer=accepted_offer(case.employee_id),
        form=approved_form(case.employee_id),
    )


class MatrixTestCase(unittest.TestCase):
    """One service holding all four clients, so isolation is observable."""

    def setUp(self) -> None:
        self.service = OnboardingService(matrix_registry())
        self.records = {}
        for case in CASES:
            record = self.service.create_onboarding(
                actor_for(case), trigger_for(case)
            )
            self.records[case.employee_id] = record

    def keys_for(self, case: Case) -> set[str]:
        return {t.template_key for t in self.records[case.employee_id].tasks}


# ---------------------------------------------------------------------------
# Coverage of the matrix itself
# ---------------------------------------------------------------------------


class MatrixCoverageTests(unittest.TestCase):
    def test_at_least_twenty_employees_across_at_least_four_clients(self) -> None:
        self.assertGreaterEqual(len(CASES), 20)
        self.assertGreaterEqual(len(CLIENTS_UNDER_TEST), 4)

    def test_employee_ids_are_unique(self) -> None:
        ids = [c.employee_id for c in CASES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_filter_dimension_is_exercised(self) -> None:
        """Each of the six dimensions must vary across the matrix."""
        for attribute in ("role_id", "grade", "location", "project",
                          "nationality_group"):
            with self.subTest(dimension=attribute):
                values = {getattr(c, attribute) for c in CASES}
                self.assertGreater(len(values), 1, attribute)
        self.assertGreater(len({c.attributes for c in CASES}), 1)


# ---------------------------------------------------------------------------
# Task selection
# ---------------------------------------------------------------------------


class TaskSelectionTests(MatrixTestCase):
    def test_every_employee_receives_exactly_the_expected_tasks(self) -> None:
        for case in CASES:
            with self.subTest(employee=case.employee_id, note=case.note):
                actual = self.keys_for(case)
                self.assertEqual(
                    actual, set(case.expected_keys),
                    f"{case.employee_id}: missing="
                    f"{sorted(set(case.expected_keys) - actual)} unexpected="
                    f"{sorted(actual - set(case.expected_keys))}",
                )

    def test_no_employee_receives_an_explicitly_excluded_task(self) -> None:
        for case in CASES:
            if not case.expected_absent:
                continue
            with self.subTest(employee=case.employee_id):
                self.assertEqual(
                    self.keys_for(case) & set(case.expected_absent), set()
                )

    def test_no_employee_has_a_duplicate_task(self) -> None:
        for case in CASES:
            with self.subTest(employee=case.employee_id):
                keys = [t.template_key for t in
                        self.records[case.employee_id].tasks]
                self.assertEqual(len(keys), len(set(keys)))
                ids = [t.task_id for t in self.records[case.employee_id].tasks]
                self.assertEqual(len(ids), len(set(ids)))

    def test_task_ids_are_derived_from_the_onboarding_and_template(self) -> None:
        for case in CASES:
            record = self.records[case.employee_id]
            with self.subTest(employee=case.employee_id):
                self.assertEqual(
                    record.onboarding_id,
                    f"ONB-{case.client_id}-{case.employee_id}",
                )
                for task in record.tasks:
                    self.assertEqual(
                        task.task_id,
                        f"{record.onboarding_id}#{task.template_key}",
                    )

    def test_two_employees_differing_only_in_one_dimension_differ_in_plan(self) -> None:
        """The checklist is selected, not fixed."""
        pairs = (
            ("E-01", "E-02", "project"),          # apollo vs zephyr
            ("E-01", "E-03", "location"),         # Dubai vs Remote-IN
            ("E-12", "E-13", "nationality_group"),
            ("E-13", "E-15", "grade"),
            ("E-17", "E-18", "role_id"),
        )
        by_id = {c.employee_id: c for c in CASES}
        for left, right, dimension in pairs:
            with self.subTest(pair=f"{left}/{right}", dimension=dimension):
                self.assertNotEqual(
                    self.keys_for(by_id[left]), self.keys_for(by_id[right])
                )


# ---------------------------------------------------------------------------
# Owners, due dates, dependencies, evidence
# ---------------------------------------------------------------------------


class OwnerAssignmentTests(MatrixTestCase):
    def test_every_task_carries_the_configured_owner(self) -> None:
        for case in CASES:
            for task in self.records[case.employee_id].tasks:
                with self.subTest(employee=case.employee_id,
                                  key=task.template_key):
                    self.assertEqual(task.owner,
                                     EXPECTED_OWNER[task.template_key])

    def test_no_task_is_owned_by_the_system_pseudo_role(self) -> None:
        for case in CASES:
            for task in self.records[case.employee_id].tasks:
                with self.subTest(employee=case.employee_id,
                                  key=task.template_key):
                    self.assertIsNot(task.owner, Role.SYSTEM)


class DueDateTests(MatrixTestCase):
    def test_due_dates_match_the_hand_counted_calendar(self) -> None:
        for case in CASES:
            if case.joining_date != JOINING:
                continue
            for task in self.records[case.employee_id].tasks:
                offset = EXPECTED_OFFSET[(case.client_id, task.template_key)]
                with self.subTest(employee=case.employee_id,
                                  key=task.template_key, offset=offset):
                    self.assertEqual(task.due_date, DUE_BY_OFFSET[offset])

    def test_the_same_template_name_uses_each_clients_own_lead_time(self) -> None:
        """CLIENT-BETA's hr_record_create is -6, CLIENT-ALPHA's is -10."""
        alpha = next(c for c in CASES if c.client_id == CLIENT_ALPHA)
        beta = next(c for c in CASES if c.client_id == CLIENT_BETA)
        alpha_due = self.records[alpha.employee_id].task_by_key(
            "hr_record_create").due_date
        beta_due = self.records[beta.employee_id].task_by_key(
            "hr_record_create").due_date
        self.assertEqual(alpha_due, DUE_BY_OFFSET[-10])
        self.assertEqual(beta_due, DUE_BY_OFFSET[-6])
        self.assertNotEqual(alpha_due, beta_due)

    def test_holiday_joining_date_skips_the_december_public_holidays(self) -> None:
        case = next(c for c in CASES if c.joining_date == HOLIDAY_JOINING)
        for task in self.records[case.employee_id].tasks:
            offset = EXPECTED_OFFSET[(case.client_id, task.template_key)]
            with self.subTest(key=task.template_key, offset=offset):
                self.assertEqual(task.due_date,
                                 HOLIDAY_DUE_BY_OFFSET[offset])

    def test_no_due_date_falls_on_a_weekend_or_public_holiday(self) -> None:
        for case in CASES:
            for task in self.records[case.employee_id].tasks:
                with self.subTest(employee=case.employee_id,
                                  key=task.template_key):
                    self.assertLess(task.due_date.weekday(), 5)
                    self.assertNotIn(task.due_date, UAE_PUBLIC_HOLIDAYS)


class DependencyAndReadinessTests(MatrixTestCase):
    def test_initial_ready_set_matches_the_tasks_with_no_prerequisite(self) -> None:
        for case in CASES:
            with self.subTest(employee=case.employee_id, note=case.note):
                ready = {
                    t.template_key for t in self.service.ready_tasks(
                        actor_for(case), self.records[case.employee_id].onboarding_id
                    )
                }
                self.assertEqual(ready, set(case.expected_ready))

    def test_every_non_ready_task_starts_blocked(self) -> None:
        for case in CASES:
            record = self.records[case.employee_id]
            for task in record.tasks:
                with self.subTest(employee=case.employee_id,
                                  key=task.template_key):
                    expected = (
                        TaskStatus.READY
                        if task.template_key in case.expected_ready
                        else TaskStatus.BLOCKED
                    )
                    self.assertIs(task.status, expected)

    def test_every_dependency_points_at_a_task_in_the_same_plan(self) -> None:
        for case in CASES:
            record = self.records[case.employee_id]
            ids = {t.task_id for t in record.tasks}
            for task in record.tasks:
                with self.subTest(employee=case.employee_id,
                                  key=task.template_key):
                    for dependency in task.depends_on:
                        self.assertIn(dependency, ids)

    def test_prerequisites_are_ordered_before_their_dependants(self) -> None:
        for case in CASES:
            record = self.records[case.employee_id]
            position = {t.task_id: i for i, t in enumerate(record.tasks)}
            for task in record.tasks:
                for dependency in task.depends_on:
                    with self.subTest(employee=case.employee_id,
                                      key=task.template_key):
                        self.assertLess(position[dependency],
                                        position[task.task_id])

    def test_the_delta_field_chain_is_three_layers_deep(self) -> None:
        case = next(c for c in CASES if c.employee_id == "E-17")
        batches = self.service.parallel_batches(
            actor_for(case), self.records["E-17"].onboarding_id
        )
        self.assertEqual(len(batches), 3)
        self.assertEqual([len(b) for b in batches], [1, 1, 1])


class EvidenceRequirementTests(MatrixTestCase):
    def test_evidence_flag_matches_the_configuration(self) -> None:
        for case in CASES:
            for task in self.records[case.employee_id].tasks:
                with self.subTest(employee=case.employee_id,
                                  key=task.template_key):
                    self.assertEqual(
                        task.evidence_required,
                        task.template_key in EXPECTED_EVIDENCE_KEYS,
                    )

    def test_an_evidence_task_cannot_complete_without_a_reference(self) -> None:
        """E-17's delta_equipment requires evidence; delta_record does not."""
        case = next(c for c in CASES if c.employee_id == "E-17")
        actor = actor_for(case)
        onboarding_id = self.records["E-17"].onboarding_id
        self.service.complete_task(
            actor, onboarding_id, f"{onboarding_id}#delta_record"
        )
        from onboarding_task_management.errors import EvidenceRequiredError

        with self.assertRaises(EvidenceRequiredError):
            self.service.complete_task(
                actor, onboarding_id, f"{onboarding_id}#delta_equipment"
            )
        task = self.service.complete_task(
            actor, onboarding_id, f"{onboarding_id}#delta_equipment",
            "DOC-EQUIP-17",
        )
        self.assertEqual(task.evidence_ref, "DOC-EQUIP-17")


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------


class MilestoneApplicabilityTests(MatrixTestCase):
    def test_applicable_milestones_match_expectation(self) -> None:
        for case in CASES:
            with self.subTest(employee=case.employee_id, note=case.note):
                states = self.service.milestones(
                    actor_for(case),
                    self.records[case.employee_id].onboarding_id,
                    AS_OF,
                )
                self.assertEqual({m.template_key for m in states},
                                 set(case.expected_milestones))

    def test_every_milestone_requires_only_tasks_in_the_plan(self) -> None:
        """A milestone requiring an out-of-scope task could never be achieved."""
        for case in CASES:
            record = self.records[case.employee_id]
            ids = {t.task_id for t in record.tasks}
            states = self.service.milestones(
                actor_for(case), record.onboarding_id, AS_OF
            )
            for state in states:
                with self.subTest(employee=case.employee_id,
                                  milestone=state.template_key):
                    self.assertEqual(set(state.required_task_ids) - ids, set())


# ---------------------------------------------------------------------------
# Client isolation
# ---------------------------------------------------------------------------


class ClientIsolationTests(MatrixTestCase):
    def test_no_task_leaks_across_clients(self) -> None:
        by_client: dict[str, set[str]] = {}
        for case in CASES:
            by_client.setdefault(case.client_id, set()).update(
                self.keys_for(case)
            )
        # The three configurations share no template key except the two names
        # CLIENT-BETA deliberately reuses from CLIENT-ALPHA.
        self.assertEqual(by_client[CLIENT_GAMMA] & by_client[CLIENT_DELTA],
                         set())
        self.assertEqual(by_client[CLIENT_ALPHA] & by_client[CLIENT_GAMMA],
                         set())
        self.assertEqual(by_client[CLIENT_ALPHA] & by_client[CLIENT_DELTA],
                         set())

    def test_every_record_and_task_carries_its_own_client_id(self) -> None:
        for case in CASES:
            record = self.records[case.employee_id]
            with self.subTest(employee=case.employee_id):
                self.assertEqual(record.client_id, case.client_id)
                for task in record.tasks:
                    self.assertEqual(task.client_id, case.client_id)

    def test_an_actor_cannot_read_another_clients_onboarding(self) -> None:
        gamma = next(c for c in CASES if c.client_id == CLIENT_GAMMA)
        intruder = Actor(actor_id="U-HR-X", role=Role.HR_OPS,
                         client_id=CLIENT_DELTA)
        with self.assertRaises(DataIsolationError):
            self.service.get(intruder, self.records[gamma.employee_id].onboarding_id)

    def test_a_cross_client_create_is_refused(self) -> None:
        case = next(c for c in CASES if c.client_id == CLIENT_GAMMA)
        intruder = Actor(actor_id="U-HR-X", role=Role.HR_OPS,
                         client_id=CLIENT_DELTA)
        with self.assertRaises(DataIsolationError):
            self.service.create_onboarding(intruder, trigger_for(case))

    def test_audit_events_are_scoped_to_their_own_client(self) -> None:
        for client_id in (CLIENT_ALPHA, CLIENT_BETA, CLIENT_GAMMA,
                          CLIENT_DELTA):
            actor = Actor(actor_id="U-AUD", role=Role.HR_OPS,
                          client_id=client_id)
            with self.subTest(client=client_id):
                events = self.service.audit.events_for(actor)
                self.assertTrue(events)
                self.assertEqual({e.client_id for e in events}, {client_id})


# ---------------------------------------------------------------------------
# Repeatability and idempotency
# ---------------------------------------------------------------------------


class RepeatabilityTests(unittest.TestCase):
    def test_generation_is_byte_identical_across_two_fresh_services(self) -> None:
        def plan(case: Case) -> list[tuple[str, str, str, str]]:
            service = OnboardingService(matrix_registry())
            record = service.create_onboarding(actor_for(case), trigger_for(case))
            return [
                (t.template_key, t.owner.value, t.due_date.isoformat(),
                 t.status.value)
                for t in record.tasks
            ]

        for case in CASES:
            with self.subTest(employee=case.employee_id):
                self.assertEqual(plan(case), plan(case))

    def test_a_repeated_trigger_is_refused_rather_than_duplicated(self) -> None:
        service = OnboardingService(matrix_registry())
        case = CASES[0]
        service.create_onboarding(actor_for(case), trigger_for(case))
        with self.assertRaises(DuplicateOnboardingError):
            service.create_onboarding(actor_for(case), trigger_for(case))
        records = service._client_records(case.client_id)
        self.assertEqual(len(records), 1)

    def test_a_repeat_does_not_reset_progress(self) -> None:
        service = OnboardingService(matrix_registry())
        case = next(c for c in CASES if c.employee_id == "E-17")
        record = service.create_onboarding(actor_for(case), trigger_for(case))
        service.complete_task(
            actor_for(case), record.onboarding_id,
            f"{record.onboarding_id}#delta_record",
        )
        with self.assertRaises(DuplicateOnboardingError):
            service.create_onboarding(actor_for(case), trigger_for(case))
        after = service.get(actor_for(case), record.onboarding_id)
        done = after.task_by_key("delta_record")
        self.assertIsNotNone(done)
        self.assertIs(done.status, TaskStatus.COMPLETE)

    def test_preview_agrees_with_the_generated_plan(self) -> None:
        """preview_checklist must predict what create_onboarding produces."""
        service = OnboardingService(matrix_registry())
        for case in CASES:
            context = ChecklistContext(
                client_id=case.client_id,
                employee_id=case.employee_id,
                joining_date=case.joining_date,
                location=case.location,
                project=case.project,
                role_id=case.role_id,
                grade=case.grade,
                nationality_group=case.nationality_group,
                attributes=sorted(case.attributes),
            )
            with self.subTest(employee=case.employee_id):
                preview = service.preview_checklist(actor_for(case), context)
                self.assertEqual(
                    {d.template_key for d in preview.included},
                    set(case.expected_keys),
                )


# ---------------------------------------------------------------------------
# Explanations
# ---------------------------------------------------------------------------


class ExplanationTests(MatrixTestCase):
    def test_every_included_task_carries_a_non_empty_reason(self) -> None:
        for case in CASES:
            view = self.service.checklist(
                actor_for(case), self.records[case.employee_id].onboarding_id,
                AS_OF,
            )
            for entry in view.entries:
                with self.subTest(employee=case.employee_id,
                                  key=entry.template_key):
                    self.assertTrue(entry.decision.reason.strip())
                    self.assertIn(entry.decision.reason_code,
                                  {"applies_to_all", "matched_all_filters"})

    def test_every_excluded_template_names_the_dimension_that_excluded_it(self) -> None:
        for case in CASES:
            view = self.service.checklist(
                actor_for(case), self.records[case.employee_id].onboarding_id,
                AS_OF,
            )
            for decision in view.excluded:
                with self.subTest(employee=case.employee_id,
                                  key=decision.template_key):
                    self.assertTrue(
                        decision.reason_code.startswith("excluded_by_")
                    )
                    self.assertTrue(decision.reason.strip())

    def test_the_nationality_exclusion_reason_names_the_group_code(self) -> None:
        """BR-02's nationality dimension, explained without storing a nationality."""
        case = next(c for c in CASES if c.employee_id == "E-13")
        view = self.service.checklist(
            actor_for(case), self.records["E-13"].onboarding_id, AS_OF
        )
        visa = next(d for d in view.excluded
                    if d.template_key == "gamma_visa_pack")
        self.assertEqual(visa.reason_code, "excluded_by_nationality_group")
        self.assertIn(GROUP_LOCAL, visa.reason)
        self.assertIn(GROUP_EXPAT, visa.reason)


# ---------------------------------------------------------------------------
# Negative and edge cases
# ---------------------------------------------------------------------------


class NegativeCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OnboardingService(matrix_registry())
        self.case = CASES[0]

    def test_a_client_with_no_templates_is_refused(self) -> None:
        actor = Actor(actor_id="U-HR-1", role=Role.HR_OPS,
                      client_id=CLIENT_EMPTY)
        trigger = trigger_for(self.case).model_copy(
            update={"client_id": CLIENT_EMPTY}
        )
        with self.assertRaises(MissingConfigurationError):
            self.service.create_onboarding(actor, trigger)

    def test_an_unknown_client_is_refused(self) -> None:
        actor = Actor(actor_id="U-HR-1", role=Role.HR_OPS,
                      client_id="CLIENT-NOT-CONFIGURED")
        trigger = trigger_for(self.case).model_copy(
            update={"client_id": "CLIENT-NOT-CONFIGURED"}
        )
        with self.assertRaises(MissingConfigurationError):
            self.service.create_onboarding(actor, trigger)

    def test_an_unaccepted_offer_is_refused_for_every_client(self) -> None:
        for client_id in sorted(CLIENTS_UNDER_TEST):
            case = next(c for c in CASES if c.client_id == client_id)
            trigger = trigger_for(case).model_copy(
                update={"offer": accepted_offer(case.employee_id).model_copy(
                    update={"accepted": False}
                )}
            )
            with self.subTest(client=client_id), \
                    self.assertRaises(PreconditionError):
                self.service.create_onboarding(actor_for(case), trigger)

    def test_an_unapproved_form_is_refused_for_every_client(self) -> None:
        for client_id in sorted(CLIENTS_UNDER_TEST):
            case = next(c for c in CASES if c.client_id == client_id)
            trigger = trigger_for(case).model_copy(
                update={"form": approved_form(case.employee_id).model_copy(
                    update={"approved": False}
                )}
            )
            with self.subTest(client=client_id), \
                    self.assertRaises(PreconditionError):
                self.service.create_onboarding(actor_for(case), trigger)

    def test_a_non_hr_actor_may_not_create_an_onboarding(self) -> None:
        from onboarding_task_management.errors import UnauthorizedActionError

        actor = Actor(actor_id="U-IT-1", role=Role.IT_MANAGER,
                      client_id=self.case.client_id)
        with self.assertRaises(UnauthorizedActionError):
            self.service.create_onboarding(actor, trigger_for(self.case))

    def test_a_joining_date_outside_calendar_coverage_is_refused(self) -> None:
        from onboarding_task_management.errors import CalendarRangeError

        trigger = trigger_for(self.case).model_copy(
            update={"joining_date": date(2028, 3, 1)}
        )
        with self.assertRaises(CalendarRangeError):
            self.service.create_onboarding(actor_for(self.case), trigger)

    def test_a_blocked_task_cannot_be_started(self) -> None:
        from onboarding_task_management.errors import PrerequisiteNotMetError

        case = next(c for c in CASES if c.employee_id == "E-17")
        record = self.service.create_onboarding(actor_for(case),
                                                trigger_for(case))
        with self.assertRaises(PrerequisiteNotMetError):
            self.service.start_task(
                actor_for(case), record.onboarding_id,
                f"{record.onboarding_id}#delta_safety_training",
            )

    def test_every_created_onboarding_starts_active(self) -> None:
        for case in CASES[:4]:
            record = self.service.create_onboarding(actor_for(case),
                                                    trigger_for(case))
            with self.subTest(employee=case.employee_id):
                self.assertIs(record.status, OnboardingStatus.ACTIVE)
                self.assertEqual(record.revision, 0)


# ---------------------------------------------------------------------------
# Agentic workflow, across clients
#
# The existing agent tests all run against CLIENT-ALPHA. These prove the same
# loop drives a client whose configuration it has never seen, using the
# shipped UC02Agent, UC02Tools and advisors -- no second agent is defined.
# ---------------------------------------------------------------------------


class AgenticWorkflowAcrossClientsTests(unittest.TestCase):
    #: One employee per client, covering all four.
    AGENT_EMPLOYEES = ("E-01", "E-09", "E-12", "E-17")

    def setUp(self) -> None:
        self.service = OnboardingService(matrix_registry())
        self.by_id = {c.employee_id: c for c in CASES}
        self.records = {}
        for employee_id in self.AGENT_EMPLOYEES:
            case = self.by_id[employee_id]
            self.records[employee_id] = self.service.create_onboarding(
                actor_for(case), trigger_for(case)
            )

    def _agent(self, employee_id: str, advisor=None):
        from onboarding_task_management.agent.loop import UC02Agent

        case = self.by_id[employee_id]
        return UC02Agent(self.service, actor_for(case), advisor=advisor)

    def test_the_offline_agent_acts_and_verifies_for_every_client(self) -> None:
        from onboarding_task_management.agent.loop import RunStatus

        for employee_id in self.AGENT_EMPLOYEES:
            case = self.by_id[employee_id]
            with self.subTest(employee=employee_id, client=case.client_id):
                result = self._agent(employee_id).run(
                    self.records[employee_id].onboarding_id, AS_OF
                )
                self.assertIs(result.status, RunStatus.ACTED)
                self.assertTrue(result.verified)
                self.assertEqual(result.advisor, "offline")
                self.assertIsNone(result.model)
                self.assertEqual(result.exceptions, [])
                self.assertEqual(len(result.actions_taken), 1)

    def test_the_agent_only_ever_calls_allowlisted_tools(self) -> None:
        from onboarding_task_management.agent.tools import ALL_TOOLS

        for employee_id in self.AGENT_EMPLOYEES:
            with self.subTest(employee=employee_id):
                result = self._agent(employee_id).run(
                    self.records[employee_id].onboarding_id, AS_OF
                )
                self.assertTrue(result.tool_calls)
                for tool in result.tool_calls:
                    self.assertIn(tool, ALL_TOOLS)

    def test_the_agent_starts_a_task_the_rules_engine_declared_ready(self) -> None:
        """The agent may only progress work the deterministic layer unblocked."""
        for employee_id in self.AGENT_EMPLOYEES:
            case = self.by_id[employee_id]
            record = self.records[employee_id]
            with self.subTest(employee=employee_id):
                result = self._agent(employee_id).run(
                    record.onboarding_id, AS_OF
                )
                started = result.actions_taken[0].rsplit(":", 1)[1]
                key = started.split("#", 1)[1]
                self.assertIn(key, case.expected_ready)
                after = self.service.get(actor_for(case), record.onboarding_id)
                started_task = after.task_by_key(key)
                self.assertIsNotNone(started_task)
                assert started_task is not None  # narrow for the type checker
                self.assertIs(started_task.status, TaskStatus.IN_PROGRESS)

    def test_the_fake_llm_drives_the_same_loop_and_is_labelled_as_fake(self) -> None:
        from onboarding_task_management.agent.loop import RunStatus
        from onboarding_task_management.sub_agent.fakes import make_fake_llm

        for employee_id in self.AGENT_EMPLOYEES:
            with self.subTest(employee=employee_id):
                result = self._agent(
                    employee_id, advisor=make_fake_llm("scripted")
                ).run(self.records[employee_id].onboarding_id, AS_OF)
                self.assertIs(result.status, RunStatus.ACTED)
                self.assertTrue(result.verified)
                self.assertTrue(result.advice.source.startswith("fake_llm:"))
                self.assertIn("fake/", result.advice.source)

    def test_a_gateway_failure_degrades_instead_of_inventing_advice(self) -> None:
        from onboarding_task_management.sub_agent.fakes import make_fake_llm

        employee_id = "E-12"
        result = self._agent(
            employee_id, advisor=make_fake_llm("error")
        ).run(self.records[employee_id].onboarding_id, AS_OF)
        self.assertTrue(result.degraded)
        self.assertIn("FakeLLMError", result.degraded_reason)
        # Degraded, but still a correct deterministic outcome.
        self.assertEqual(result.exceptions, [])

    def test_a_hallucinated_recommendation_is_overruled_by_the_record(self) -> None:
        from onboarding_task_management.sub_agent.fakes import make_fake_llm

        employee_id = "E-17"
        result = self._agent(
            employee_id, advisor=make_fake_llm("hallucinate")
        ).run(self.records[employee_id].onboarding_id, AS_OF)
        self.assertTrue(result.degraded)
        self.assertIn("overruled", result.degraded_reason)
        if result.advice.target_task_id is not None:
            self.assertTrue(
                result.advice.target_task_id.startswith(
                    self.records[employee_id].onboarding_id
                )
            )

    def test_the_agent_cannot_change_a_plan_without_human_approval(self) -> None:
        """uc02.request_approval raises a gate; it does not re-plan."""
        from onboarding_task_management.agent.tools import UC02Tools
        from onboarding_task_management.contracts import DecisionOutcome
        from onboarding_task_management.errors import ApprovalRequiredError

        case = self.by_id["E-01"]
        record = self.records["E-01"]
        tools = UC02Tools(self.service, actor_for(case))
        new_date = date(2026, 11, 30)

        raised = tools.request_approval(record.onboarding_id,
                                        joining_date=new_date)
        self.assertTrue(raised.ok)
        self.assertIs(raised.data.outcome, DecisionOutcome.PENDING)
        # Unchanged until a human decides.
        self.assertEqual(
            self.service.get(actor_for(case), record.onboarding_id).joining_date,
            case.joining_date,
        )
        with self.assertRaises(ApprovalRequiredError):
            self.service.apply_change(actor_for(case), raised.data.request_id)

        # The requester may not approve their own request.
        from onboarding_task_management.errors import UnauthorizedActionError

        with self.assertRaises(UnauthorizedActionError):
            self.service.decide_change(
                actor_for(case), raised.data.request_id,
                DecisionOutcome.APPROVED,
            )

        approver = actor_for(case, actor_id="U-HR-2")
        self.service.decide_change(approver, raised.data.request_id,
                                   DecisionOutcome.APPROVED)
        outcome = self.service.apply_change(actor_for(case),
                                            raised.data.request_id)
        self.assertEqual(outcome.revision, 1)
        self.assertEqual(
            self.service.get(actor_for(case), record.onboarding_id).joining_date,
            new_date,
        )

    def test_a_write_tool_refusal_is_reported_not_swallowed(self) -> None:
        """A blocked task cannot be started through the tool layer either."""
        from onboarding_task_management.agent.tools import UC02Tools

        case = self.by_id["E-17"]
        record = self.records["E-17"]
        tools = UC02Tools(self.service, actor_for(case))
        result = tools.update_task_state(
            record.onboarding_id,
            f"{record.onboarding_id}#delta_safety_training",
            action="start",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "POLICY_BREACH")
        after = self.service.get(actor_for(case), record.onboarding_id)
        blocked = after.task_by_key("delta_safety_training")
        self.assertIsNotNone(blocked)
        assert blocked is not None  # narrow for the type checker
        self.assertIs(blocked.status, TaskStatus.BLOCKED)

    def test_every_agent_run_is_audited_under_its_own_client(self) -> None:
        for employee_id in self.AGENT_EMPLOYEES:
            case = self.by_id[employee_id]
            with self.subTest(employee=employee_id, client=case.client_id):
                self._agent(employee_id).run(
                    self.records[employee_id].onboarding_id, AS_OF
                )
                actions = self.service.audit.actions_for(actor_for(case))
                for expected in ("agent.started", "agent.acted",
                                 "agent.finished"):
                    self.assertIn(expected, actions)

    def test_a_second_run_does_not_repeat_the_first_action(self) -> None:
        """The task is already IN_PROGRESS, so it is no longer ready."""
        employee_id = "E-12"
        agent = self._agent(employee_id)
        onboarding_id = self.records[employee_id].onboarding_id
        first = agent.run(onboarding_id, AS_OF)
        second = agent.run(onboarding_id, AS_OF)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(second.actions_taken, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
