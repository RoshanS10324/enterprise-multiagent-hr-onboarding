"""Synthetic client configurations and triggers.

Everything here is invented. There are no real employees, no real client names
and no contact details — personas are opaque IDs plus a scenario label, which is
stricter than the review's §6.1 table (that listed fictional names; the contracts
in this prototype have no name field at all).

Two clients exist so that data-isolation behaviour can be exercised, and one
deliberately empty client so that missing-configuration handling can be tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from onboarding_task_management.contracts import (
    ChangeDriver,
    OfferAcceptance,
    OnboardingForm,
    Role,
    TaskCategory,
)
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    ConfigRegistry,
    MilestoneTemplate,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import OnboardingTrigger

CLIENT_ALPHA = "CLIENT-ALPHA"
CLIENT_BETA = "CLIENT-BETA"
CLIENT_EMPTY = "CLIENT-EMPTY"

#: Fixed clock anchor. Tests pass explicit ``as_of`` dates so runs are reproducible.
ANCHOR_TODAY = date(2026, 10, 15)
DEFAULT_JOINING_DATE = date(2026, 11, 16)

LOCATION_DUBAI = "Dubai"
LOCATION_ABU_DHABI = "Abu Dhabi"
LOCATION_REMOTE = "Remote-IN"

PROJECT_APOLLO = "apollo"
PROJECT_ZEPHYR = "zephyr"

_JD = ChangeDriver.JOINING_DATE
_LOC = ChangeDriver.LOCATION
_PRJ = ChangeDriver.PROJECT


# ---------------------------------------------------------------------------
# Applicability flags (UC02-BR-07 / UC02-BR-08).
#
# Every extended activity below is OPT-IN. UC-02 never infers that an employee
# needs a visa, travel, accommodation, food, family insurance or a banking
# letter — the caller declares it. The rule is "track ... only where
# applicable", so an employee without the flag simply has no such task.
# ---------------------------------------------------------------------------
ATTR_BV_REQUIRED = "bv_required"
ATTR_VISA_REQUIRED = "visa_required"
ATTR_INSURANCE_REQUIRED = "insurance_required"
ATTR_FAMILY_INSURANCE = "family_insurance"
ATTR_NEEDS_TRAVEL = "needs_travel"
ATTR_NEEDS_ACCOMMODATION = "needs_accommodation"
ATTR_NEEDS_FOOD = "needs_food"
ATTR_SALARY_TRANSFER_LETTER = "salary_transfer_letter"
ATTR_NON_LIABILITY_LETTER = "non_liability_letter"
ATTR_HR_SYSTEM_ACTIVATION = "hr_system_activation"

GRADE_L2 = "L2"
GRADE_L3 = "L3"
GRADE_L4 = "L4"

#: Extended activities required by UC02-BR-07, UC02-BR-08 and HR-system
#: activation. Approver roles follow Business Framework §8 "Systems and
#: actions": visa/travel/insurance -> HR operations, with HR manager or admin
#: for spend; background verification -> HR officer. No approver was invented;
#: an action with no documented approver carries ``None``.
EXTENDED_TEMPLATES: tuple[TaskTemplate, ...] = (
    TaskTemplate(
        key="background_verification",
        category=TaskCategory.BACKGROUND_VERIFICATION,
        title="Raise background verification and track result",
        title_ar="طلب التحقق من الخلفية ومتابعة النتيجة",
        owner=Role.HR_OFFICER,
        offset_days=-12,
        depends_on=("collect_documents",),
        sensitive_to=(_JD,),
        requires_attributes=(ATTR_BV_REQUIRED,),
        evidence_required=True,
        approval_role=Role.HR_OFFICER,
    ),
    TaskTemplate(
        key="medical_test",
        category=TaskCategory.MEDICAL,
        title="Complete pre-employment medical test",
        title_ar="إجراء الفحص الطبي قبل التوظيف",
        owner=Role.HR_OPS,
        offset_days=-9,
        sensitive_to=(_JD, _LOC),
        requires_attributes=(ATTR_VISA_REQUIRED,),
        evidence_required=True,
        approval_role=Role.HR_OPS,
    ),
    TaskTemplate(
        key="biometrics_capture",
        category=TaskCategory.BIOMETRICS,
        title="Complete biometrics capture",
        title_ar="استكمال تسجيل البصمة الحيوية",
        owner=Role.HR_OPS,
        offset_days=-8,
        depends_on=("medical_test",),
        sensitive_to=(_JD, _LOC),
        requires_attributes=(ATTR_VISA_REQUIRED,),
        evidence_required=True,
        approval_role=Role.HR_OPS,
    ),
    TaskTemplate(
        key="visa_pack_handover",
        category=TaskCategory.VISA,
        title="Hand visa pack to PRO",
        title_ar="تسليم ملف التأشيرة إلى مندوب العلاقات الحكومية",
        owner=Role.HR_OPS,
        offset_days=-7,
        depends_on=("collect_documents",),
        sensitive_to=(_JD, _LOC),
        requires_attributes=(ATTR_VISA_REQUIRED,),
        evidence_required=True,
        approval_role=Role.HR_OPS,
    ),
    # NOTE: visa processing deliberately does NOT depend on
    # background_verification. Business Framework §15 OQ-02 — "Does the visa
    # wait for background verification?" — is an OPEN QUESTION owned by the
    # Process SME. A client that has decided may declare the dependency in its
    # own configuration; UC-02 does not assume it in either direction.
    TaskTemplate(
        key="visa_processing",
        category=TaskCategory.VISA,
        title="Track visa processing with PRO",
        title_ar="متابعة إجراءات التأشيرة مع المندوب",
        owner=Role.PRO,
        offset_days=-3,
        depends_on=("visa_pack_handover",),
        sensitive_to=(_JD, _LOC),
        requires_attributes=(ATTR_VISA_REQUIRED,),
        evidence_required=True,
        approval_role=Role.HR_OPS,
    ),
    TaskTemplate(
        key="insurance_enrolment",
        category=TaskCategory.INSURANCE,
        title="Enrol employee in medical insurance",
        title_ar="تسجيل الموظف في التأمين الصحي",
        owner=Role.HR_OPS,
        offset_days=-5,
        sensitive_to=(_JD,),
        requires_attributes=(ATTR_INSURANCE_REQUIRED,),
        evidence_required=True,
        approval_role=Role.HR_MANAGER,
    ),
    TaskTemplate(
        key="family_insurance_enrolment",
        category=TaskCategory.INSURANCE,
        title="Enrol dependants in medical insurance",
        title_ar="تسجيل أفراد الأسرة في التأمين الصحي",
        owner=Role.HR_OPS,
        offset_days=-5,
        depends_on=("insurance_enrolment",),
        sensitive_to=(_JD,),
        requires_attributes=(ATTR_FAMILY_INSURANCE,),
        evidence_required=True,
        approval_role=Role.HR_MANAGER,
    ),
    TaskTemplate(
        key="travel_booking",
        category=TaskCategory.TRAVEL,
        title="Book inbound travel",
        title_ar="حجز تذاكر السفر",
        owner=Role.HR_OPS,
        offset_days=-6,
        sensitive_to=(_JD, _LOC),
        requires_attributes=(ATTR_NEEDS_TRAVEL,),
        evidence_required=True,
        approval_role=Role.HR_MANAGER,
    ),
    TaskTemplate(
        key="accommodation_booking",
        category=TaskCategory.ACCOMMODATION,
        title="Confirm accommodation ready before joining",
        title_ar="تأكيد جاهزية السكن قبل الالتحاق",
        owner=Role.FACILITIES,
        offset_days=-6,
        sensitive_to=(_JD, _LOC),
        requires_attributes=(ATTR_NEEDS_ACCOMMODATION,),
        evidence_required=True,
        approval_role=Role.HR_MANAGER,
    ),
    TaskTemplate(
        key="food_arrangement",
        category=TaskCategory.FOOD,
        title="Arrange food allowance or catering",
        title_ar="ترتيب بدل الإعاشة أو التموين",
        owner=Role.FACILITIES,
        offset_days=-4,
        sensitive_to=(_JD, _LOC),
        requires_attributes=(ATTR_NEEDS_FOOD,),
        approval_role=Role.HR_MANAGER,
    ),
    # Banking letters are PREPARED by HR. UC-02 never contacts a bank and
    # never initiates a transaction — it tracks issuance of the letter only.
    TaskTemplate(
        key="salary_transfer_letter",
        category=TaskCategory.BANKING_LETTER,
        title="Issue salary transfer letter",
        title_ar="إصدار خطاب تحويل الراتب",
        owner=Role.HR_OFFICER,
        offset_days=-4,
        depends_on=("hr_record_create",),
        sensitive_to=(_JD,),
        requires_attributes=(ATTR_SALARY_TRANSFER_LETTER,),
        evidence_required=True,
        approval_role=Role.HR_OFFICER,
    ),
    TaskTemplate(
        key="non_liability_letter",
        category=TaskCategory.BANKING_LETTER,
        title="Issue non-liability letter",
        title_ar="إصدار خطاب عدم الممانعة",
        owner=Role.HR_OFFICER,
        offset_days=-4,
        depends_on=("hr_record_create",),
        sensitive_to=(_JD,),
        requires_attributes=(ATTR_NON_LIABILITY_LETTER,),
        evidence_required=True,
        approval_role=Role.HR_OFFICER,
    ),
    # Activation in the configured HR system (e.g. Zoho People) ON joining.
    # There is NO live integration: no approved API contract, credentials
    # mechanism or integration requirement exists, so this is tracked as a
    # task with a human owner and completion confirmation only.
    TaskTemplate(
        key="hr_system_activation",
        category=TaskCategory.HR_SYSTEM_ACTIVATION,
        title="Activate employee record in the configured HR system",
        title_ar="تفعيل سجل الموظف في نظام الموارد البشرية المعتمد",
        owner=Role.HR_OPS,
        offset_days=0,
        depends_on=("hr_record_create",),
        sensitive_to=(_JD,),
        requires_attributes=(ATTR_HR_SYSTEM_ACTIVATION,),
        evidence_required=True,
        approval_role=Role.HR_OPS,
    ),
)

EXTENDED_MILESTONES: tuple[MilestoneTemplate, ...] = (
    MilestoneTemplate(
        key="verification_cleared",
        title="Background verification cleared",
        title_ar="اكتمال التحقق من الخلفية",
        offset_days=-10,
        requires=("background_verification",),
        requires_attributes=(ATTR_BV_REQUIRED,),
    ),
    MilestoneTemplate(
        key="work_authorisation_ready",
        title="Work authorisation ready",
        title_ar="جاهزية تصريح العمل",
        offset_days=-2,
        requires=("medical_test", "biometrics_capture", "visa_processing"),
        requires_attributes=(ATTR_VISA_REQUIRED,),
    ),
    MilestoneTemplate(
        key="relocation_ready",
        title="Relocation logistics ready",
        title_ar="جاهزية ترتيبات الانتقال",
        offset_days=-3,
        requires=("travel_booking", "accommodation_booking"),
        requires_attributes=(ATTR_NEEDS_TRAVEL, ATTR_NEEDS_ACCOMMODATION),
    ),
)


ALPHA_CONFIG = ClientOnboardingConfig(
    approval_status="approved",  # SYNTHETIC fixture: not a real client approval
    client_id=CLIENT_ALPHA,
    templates=(
        TaskTemplate(
            key="hr_record_create",
            category=TaskCategory.HR_RECORD,
            title="Create HRIS employee record",
            title_ar="إنشاء سجل الموظف في نظام الموارد البشرية",
            owner=Role.HR_OPS,
            offset_days=-10,
            sensitive_to=(_JD,),
        ),
        TaskTemplate(
            key="collect_documents",
            category=TaskCategory.DOCUMENT_COLLECTION,
            title="Collect and verify joining documents",
            title_ar="جمع وثائق الالتحاق والتحقق منها",
            owner=Role.HR_OPS,
            offset_days=-8,
            depends_on=("hr_record_create",),
            sensitive_to=(_JD,),
        ),
        TaskTemplate(
            key="visa_application",
            category=TaskCategory.DOCUMENT_COLLECTION,
            title="Submit employment visa application",
            title_ar="تقديم طلب تأشيرة العمل",
            owner=Role.HR_OPS,
            offset_days=-7,
            depends_on=("collect_documents",),
            sensitive_to=(_JD, _LOC),
            locations=(LOCATION_DUBAI, LOCATION_ABU_DHABI),
        ),
        TaskTemplate(
            key="it_laptop",
            category=TaskCategory.IT_PROVISIONING,
            title="Provision laptop and standard IT kit",
            title_ar="تجهيز الحاسوب المحمول ومعدات تقنية المعلومات",
            owner=Role.IT_MANAGER,
            offset_days=-5,
            depends_on=("hr_record_create",),
            sensitive_to=(_JD,),
        ),
        TaskTemplate(
            key="it_accounts",
            category=TaskCategory.IT_PROVISIONING,
            title="Create email and directory accounts",
            title_ar="إنشاء حسابات البريد الإلكتروني والدليل",
            owner=Role.IT_MANAGER,
            offset_days=-3,
            depends_on=("it_laptop",),
            sensitive_to=(_JD,),
        ),
        TaskTemplate(
            key="access_apollo",
            category=TaskCategory.ACCESS_MANAGEMENT,
            title="Grant Apollo project system access",
            title_ar="منح صلاحيات الوصول لمشروع أبولو",
            owner=Role.IT_MANAGER,
            offset_days=-2,
            depends_on=("it_accounts",),
            sensitive_to=(_JD, _PRJ),
            projects=(PROJECT_APOLLO,),
        ),
        TaskTemplate(
            key="access_zephyr",
            category=TaskCategory.ACCESS_MANAGEMENT,
            title="Grant Zephyr project system access",
            title_ar="منح صلاحيات الوصول لمشروع زفير",
            owner=Role.IT_MANAGER,
            offset_days=-2,
            depends_on=("it_accounts",),
            sensitive_to=(_JD, _PRJ),
            projects=(PROJECT_ZEPHYR,),
        ),
        TaskTemplate(
            key="desk_allocation",
            category=TaskCategory.WORKPLACE,
            title="Allocate desk and building access card",
            title_ar="تخصيص مكتب وبطاقة دخول المبنى",
            owner=Role.FACILITIES,
            offset_days=-2,
            depends_on=("hr_record_create",),
            sensitive_to=(_JD, _LOC),
            locations=(LOCATION_DUBAI, LOCATION_ABU_DHABI),
        ),
        TaskTemplate(
            key="remote_kit_shipping",
            category=TaskCategory.WORKPLACE,
            title="Ship remote working kit",
            title_ar="شحن معدات العمل عن بعد",
            owner=Role.FACILITIES,
            offset_days=-6,
            depends_on=("hr_record_create",),
            sensitive_to=(_JD, _LOC),
            locations=(LOCATION_REMOTE,),
        ),
        TaskTemplate(
            key="payroll_setup",
            category=TaskCategory.PAYROLL,
            title="Register employee in payroll and WPS",
            title_ar="تسجيل الموظف في الرواتب ونظام حماية الأجور",
            owner=Role.FINANCE,
            offset_days=-4,
            depends_on=("collect_documents",),
            sensitive_to=(_JD,),
        ),
        TaskTemplate(
            key="orientation_schedule",
            category=TaskCategory.ORIENTATION,
            title="Schedule day-1 orientation session",
            title_ar="جدولة جلسة التعريف في اليوم الأول",
            owner=Role.HR_OPS,
            offset_days=0,
            depends_on=("hr_record_create",),
            sensitive_to=(_JD,),
        ),
        TaskTemplate(
            key="policy_ack",
            category=TaskCategory.POLICY_ACKNOWLEDGEMENT,
            title="Acknowledge employee handbook and policies",
            title_ar="الإقرار بدليل الموظف والسياسات",
            owner=Role.EMPLOYEE,
            offset_days=2,
            depends_on=("orientation_schedule",),
            sensitive_to=(_JD,),
        ),
    )
    + EXTENDED_TEMPLATES,
    milestones=(
        MilestoneTemplate(
            key="paperwork_complete",
            title="Paperwork complete",
            title_ar="اكتمال المعاملات الورقية",
            offset_days=-7,
            requires=("hr_record_create", "collect_documents"),
        ),
        MilestoneTemplate(
            key="workplace_ready",
            title="Workplace ready",
            title_ar="جاهزية مكان العمل",
            offset_days=-2,
            requires=("desk_allocation",),
            locations=(LOCATION_DUBAI, LOCATION_ABU_DHABI),
        ),
        MilestoneTemplate(
            key="day_one_ready",
            title="Day-1 ready",
            title_ar="الجاهزية لليوم الأول",
            offset_days=-1,
            requires=(
                "hr_record_create",
                "collect_documents",
                "it_laptop",
                "it_accounts",
                "payroll_setup",
            ),
        ),
        MilestoneTemplate(
            key="onboarding_complete",
            title="Onboarding complete",
            title_ar="اكتمال التأهيل",
            offset_days=2,
            requires=("orientation_schedule", "policy_ack"),
        ),
    )
    + EXTENDED_MILESTONES,
)


BETA_CONFIG = ClientOnboardingConfig(
    approval_status="approved",  # SYNTHETIC fixture: not a real client approval
    client_id=CLIENT_BETA,
    templates=(
        TaskTemplate(
            key="hr_record_create",
            category=TaskCategory.HR_RECORD,
            title="Create HRIS employee record",
            title_ar="إنشاء سجل الموظف في نظام الموارد البشرية",
            owner=Role.HR_OPS,
            offset_days=-6,
            sensitive_to=(_JD,),
        ),
        TaskTemplate(
            key="it_accounts",
            category=TaskCategory.IT_PROVISIONING,
            title="Create email and directory accounts",
            title_ar="إنشاء حسابات البريد الإلكتروني والدليل",
            owner=Role.IT_MANAGER,
            offset_days=-3,
            depends_on=("hr_record_create",),
            sensitive_to=(_JD,),
        ),
    ),
)


#: Registered but intentionally empty — exercises missing-configuration handling.
EMPTY_CONFIG = ClientOnboardingConfig(
    client_id=CLIENT_EMPTY, templates=(), approval_status="approved"  # SYNTHETIC fixture
)


def build_registry() -> ConfigRegistry:
    registry = ConfigRegistry()
    for config in (ALPHA_CONFIG, BETA_CONFIG, EMPTY_CONFIG):
        registry.register(config)
    return registry


@dataclass(frozen=True)
class Persona:
    """A synthetic onboarding scenario. IDs and attributes only — no names."""

    employee_id: str
    client_id: str
    role_id: str
    department: str
    location: str
    project: str
    scenario: str
    #: UC02-BR-02 grade filtering. Default keeps existing personas unchanged.
    grade: str = GRADE_L3
    #: UC02-BR-07/08 applicability flags. Default empty: no extended activity
    #: applies unless the scenario explicitly declares it.
    attributes: tuple[str, ...] = ()
    #: UC02-BR-02 nationality dimension, as an OPAQUE GROUP CODE (e.g.
    #: "grp-expat") — never a nationality value. Default empty, which is
    #: "unrestricted", so every existing persona is unchanged. Without this
    #: field ``make_trigger`` could not set the dimension at all, so the
    #: nationality filter in ``TaskTemplate.dimension_verdicts`` was
    #: unreachable through the standard fixture path.
    nationality_group: str = ""


SYNTHETIC_PERSONAS: tuple[Persona, ...] = (
    Persona("EMP-001", CLIENT_ALPHA, "backend_engineer", "technology",
            LOCATION_DUBAI, PROJECT_APOLLO, "happy path"),
    Persona("EMP-002", CLIENT_ALPHA, "data_engineer", "technology",
            LOCATION_ABU_DHABI, PROJECT_ZEPHYR, "document delay"),
    Persona("EMP-003", CLIENT_ALPHA, "ml_engineer", "technology",
            LOCATION_REMOTE, PROJECT_APOLLO, "remote worker, no visa task"),
    Persona("EMP-004", CLIENT_ALPHA, "devops_engineer", "technology",
            LOCATION_DUBAI, PROJECT_APOLLO, "joining date moved"),
    Persona("EMP-005", CLIENT_ALPHA, "qa_engineer", "technology",
            LOCATION_DUBAI, PROJECT_ZEPHYR, "cancelled onboarding"),
    Persona("EMP-006", CLIENT_BETA, "backend_engineer", "technology",
            LOCATION_DUBAI, PROJECT_APOLLO, "second client, isolation"),
    # Full-service relocation hire: exercises every extended activity.
    Persona("EMP-007", CLIENT_ALPHA, "backend_engineer", "technology",
            LOCATION_DUBAI, PROJECT_APOLLO,
            "relocation hire: visa, BV, travel, accommodation, insurance",
            GRADE_L4,
            (
                ATTR_BV_REQUIRED,
                ATTR_VISA_REQUIRED,
                ATTR_INSURANCE_REQUIRED,
                ATTR_FAMILY_INSURANCE,
                ATTR_NEEDS_TRAVEL,
                ATTR_NEEDS_ACCOMMODATION,
                ATTR_NEEDS_FOOD,
                ATTR_SALARY_TRANSFER_LETTER,
                ATTR_NON_LIABILITY_LETTER,
                ATTR_HR_SYSTEM_ACTIVATION,
            )),
    # Local hire needing none of the extended activities.
    Persona("EMP-008", CLIENT_ALPHA, "qa_engineer", "technology",
            LOCATION_DUBAI, PROJECT_APOLLO,
            "local hire: no visa, travel, accommodation or family insurance",
            GRADE_L2),
    # Cohort: three hires sharing one joining date (UC02-TC cohort scenario).
    Persona("EMP-010", CLIENT_ALPHA, "backend_engineer", "technology",
            LOCATION_DUBAI, PROJECT_APOLLO, "cohort member 1"),
    Persona("EMP-011", CLIENT_ALPHA, "data_engineer", "technology",
            LOCATION_DUBAI, PROJECT_APOLLO, "cohort member 2"),
    Persona("EMP-012", CLIENT_ALPHA, "qa_engineer", "technology",
            LOCATION_DUBAI, PROJECT_APOLLO, "cohort member 3"),
)

#: Employees joining together on one date - used for cohort tests.
COHORT_EMPLOYEE_IDS = ("EMP-010", "EMP-011", "EMP-012")


def accepted_offer(employee_id: str) -> OfferAcceptance:
    return OfferAcceptance(
        employee_id=employee_id,
        accepted=True,
        accepted_at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
        offer_reference=f"OFR-{employee_id}",
    )


def approved_form(employee_id: str) -> OnboardingForm:
    return OnboardingForm(
        employee_id=employee_id,
        submitted=True,
        approved=True,
        approved_by_role=Role.HR_OPS,
        approved_at=datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc),
    )


def make_trigger(
    persona: Persona,
    *,
    joining_date: date = DEFAULT_JOINING_DATE,
    offer: OfferAcceptance | None = None,
    form: OnboardingForm | None = None,
) -> OnboardingTrigger:
    """Build a trigger for a persona, defaulting to a valid, approved state."""
    return OnboardingTrigger(
        client_id=persona.client_id,
        employee_id=persona.employee_id,
        joining_date=joining_date,
        role_id=persona.role_id,
        department=persona.department,
        manager_id=f"MGR-{persona.client_id}",
        location=persona.location,
        project=persona.project,
        grade=persona.grade,
        nationality_group=persona.nationality_group,
        attributes=persona.attributes,
        offer=offer or accepted_offer(persona.employee_id),
        form=form or approved_form(persona.employee_id),
    )


def persona(employee_id: str) -> Persona:
    for item in SYNTHETIC_PERSONAS:
        if item.employee_id == employee_id:
            return item
    raise KeyError(employee_id)
