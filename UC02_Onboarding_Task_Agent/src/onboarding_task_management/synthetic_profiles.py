"""SYNTHETIC, TEST-ONLY client configurations and employee profiles for UAE and India onboarding.

These exist so the dynamic checklist, DL-09, DL-14 and the agent can be exercised end to end before the
real approved client configurations are supplied. They are **not** client policy, not legal advice and not
immigration rules: every task, offset, owner and route below is invented for testing. They must never be
written to the live database or used to onboard a real employee. The version label carries ``SYNTHETIC-TEST``
and ``is_synthetic()`` lets any caller refuse them.

Why they are shaped this way
* Routes are chosen by the client's own templates through attributes (DL-09: ``location_status:in_country`` /
  ``location_status:overseas``), exactly as a real configuration would. UC-02 contains no visa rule.
* India has no visa, biometrics or entry-permit template: the profile differs because the *configuration*
  differs, not because of a country rule in code.
* Owner roles use the roles UC-02 has. There is no "Admin/Travel", "Medical" or "Background verification"
  role, so those map to the nearest existing role and are listed in ``TEAM_LABELS`` as a synthetic label.
  Real team names are the clients' and are not invented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from onboarding_task_management.contracts import Role, TaskCategory
from onboarding_task_management.sub_agent.checklist import (
    AssignmentRule,
    DocumentRequirement,
    ClientOnboardingConfig,
    ConfigRegistry,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import OnboardingTrigger
from onboarding_task_management.synthetic import accepted_offer, approved_form

SYNTHETIC_PREFIX = "SYNTHETIC-TEST"
UAE_CLIENT = "SYN-UAE-TEST"
INDIA_CLIENT = "SYN-IND-TEST"
JOINING = date(2026, 11, 16)

IN_COUNTRY = "location_status:in_country"
OVERSEAS = "location_status:overseas"

#: Synthetic display labels per owner role. Not real team names.
TEAM_LABELS: dict[str, str] = {
    "hr_ops": "HR Operations (synthetic)",
    "hr_officer": "HR Onboarding Officer (synthetic)",
    "pro": "PRO / Visa (synthetic)",
    "facilities": "Admin / Travel Desk (synthetic)",
    "finance": "Payroll (synthetic)",
    "it_manager": "IT Service Desk (synthetic)",
    "manager": "Hiring Manager (synthetic)",
    "employee": "Employee",
}


def is_synthetic(config: ClientOnboardingConfig) -> bool:
    return config.version.startswith(SYNTHETIC_PREFIX)


def _t(key, category, owner, offset, deps=(), **kw) -> TaskTemplate:
    return TaskTemplate(key=key, category=category, title=f"[synthetic] {key}", title_ar=f"[synthetic] {key}",
                        owner=owner, offset_days=offset, depends_on=tuple(deps), **kw)


def _doc(key, mandatory=True, reason="synthetic requirement", **kw) -> DocumentRequirement:
    """A SYNTHETIC document requirement. The type names are placeholders, not real document lists: UC-03 and the
    client's configuration decide real ones."""
    return DocumentRequirement(key=key, document_type=f"synthetic_{key}", mandatory=mandatory, reason=reason, **kw)


def _rules(*triples) -> tuple[AssignmentRule, ...]:
    """``(category, team label key, use_case)`` -> rules, with the synthetic team labels."""
    return tuple(AssignmentRule(category=c, team=TEAM_LABELS[team], use_case=uc,
                                mode="use_case" if uc else "role", escalation_owner=Role.HR_OFFICER)
                 for c, team, uc in triples)


def uae_config() -> ClientOnboardingConfig:
    c = TaskCategory
    return ClientOnboardingConfig(
        client_id=UAE_CLIENT, version=f"{SYNTHETIC_PREFIX}-UAE-v1", approval_status="approved", data_origin="synthetic",
        templates=(
            _t("hr_record_create", c.HR_RECORD, Role.HR_OPS, -12, priority="high"),
            _t("collect_documents", c.DOCUMENT_COLLECTION, Role.HR_OPS, -10, ("hr_record_create",)),
            _t("background_verification", c.BACKGROUND_VERIFICATION, Role.HR_OFFICER, -9, ("collect_documents",),
               requires_attributes=("bv_required",)),
            # DL-09 routes: exactly one applies, chosen by the declared location status.
            _t("visa_status_change", c.VISA, Role.PRO, -8, ("collect_documents",), requires_attributes=(IN_COUNTRY,),
               priority="high"),
            _t("entry_permit", c.VISA, Role.PRO, -12, ("collect_documents",), requires_attributes=(OVERSEAS,),
               priority="critical"),
            _t("medical_test", c.MEDICAL, Role.PRO, -6, ("visa_status_change",), requires_attributes=(IN_COUNTRY,)),
            _t("medical_test_arrival", c.MEDICAL, Role.PRO, -3, ("entry_permit",), requires_attributes=(OVERSEAS,)),
            _t("biometrics", c.BIOMETRICS, Role.PRO, -4, ("medical_test",), requires_attributes=(IN_COUNTRY,)),
            _t("biometrics_arrival", c.BIOMETRICS, Role.PRO, -2, ("medical_test_arrival",),
               requires_attributes=(OVERSEAS,)),
            _t("flight_booking", c.TRAVEL, Role.FACILITIES, -7, ("entry_permit",),
               requires_attributes=(OVERSEAS, "needs_travel")),
            _t("accommodation", c.ACCOMMODATION, Role.FACILITIES, -5, ("flight_booking",),
               requires_attributes=("needs_accommodation",)),
            _t("insurance_enrolment", c.INSURANCE, Role.HR_OPS, -4, ("collect_documents",),
               requires_attributes=("insurance_required",)),
            _t("payroll_setup", c.PAYROLL, Role.FINANCE, -4, ("collect_documents",)),
            _t("laptop_request", c.IT_PROVISIONING, Role.IT_MANAGER, -5, ("hr_record_create",)),
            _t("system_access", c.ACCESS_MANAGEMENT, Role.IT_MANAGER, -2, ("laptop_request",), grades=("L4", "L5")),
            _t("orientation_slot", c.ORIENTATION, Role.HR_OPS, 0, ("hr_record_create",)),
            _t("policy_ack", c.POLICY_ACKNOWLEDGEMENT, Role.EMPLOYEE, 2, ("orientation_slot",)),
            _t("senior_briefing", c.ORIENTATION, Role.MANAGER, 1, ("orientation_slot",), grades=("L4", "L5")),
        ),
        assignments=_rules(
            (TaskCategory.HR_RECORD, "hr_ops", ""), (TaskCategory.DOCUMENT_COLLECTION, "hr_ops", "UC-03"),
            (TaskCategory.BACKGROUND_VERIFICATION, "hr_officer", ""), (TaskCategory.VISA, "pro", ""),
            (TaskCategory.MEDICAL, "pro", ""), (TaskCategory.BIOMETRICS, "pro", ""),
            (TaskCategory.TRAVEL, "facilities", ""), (TaskCategory.ACCOMMODATION, "facilities", ""),
            (TaskCategory.INSURANCE, "hr_ops", ""), (TaskCategory.PAYROLL, "finance", ""),
            (TaskCategory.IT_PROVISIONING, "it_manager", "UC-04"), (TaskCategory.ACCESS_MANAGEMENT, "it_manager", "UC-04"),
            (TaskCategory.ORIENTATION, "hr_ops", "UC-05"), (TaskCategory.POLICY_ACKNOWLEDGEMENT, "employee", "UC-05"),
        ),
        document_requirements=(
            _doc("identity", reason="synthetic: identity check for every employee"),
            _doc("education", reason="synthetic: qualification check for every employee"),
            _doc("status_change_pack", reason="synthetic: needed only for the in-country visa route",
                 requires_attributes=(IN_COUNTRY,)),
            _doc("entry_permit_pack", reason="synthetic: needed only for the overseas entry-permit route",
                 requires_attributes=(OVERSEAS,)),
            _doc("family_cover_form", mandatory=False, reason="synthetic: optional, only when insurance is requested",
                 requires_attributes=("insurance_required",), allow_not_applicable=True),
            _doc("senior_declaration", reason="synthetic: senior grades only", grades=("L4", "L5")),
        ),
    )


def india_config() -> ClientOnboardingConfig:
    c = TaskCategory
    return ClientOnboardingConfig(
        client_id=INDIA_CLIENT, version=f"{SYNTHETIC_PREFIX}-IND-v1", approval_status="approved", data_origin="synthetic",
        templates=(
            _t("hr_record_create", c.HR_RECORD, Role.HR_OPS, -10),
            _t("collect_documents", c.DOCUMENT_COLLECTION, Role.HR_OPS, -8, ("hr_record_create",)),
            _t("background_verification", c.BACKGROUND_VERIFICATION, Role.HR_OFFICER, -7, ("collect_documents",)),
            _t("medical_check", c.MEDICAL, Role.HR_OPS, -5, ("collect_documents",),
               requires_attributes=("medical_required",)),
            _t("insurance_enrolment", c.INSURANCE, Role.HR_OPS, -4, ("collect_documents",),
               requires_attributes=("insurance_required",)),
            _t("bank_account_setup", c.PAYROLL, Role.FINANCE, -4, ("collect_documents",)),
            _t("laptop_request", c.IT_PROVISIONING, Role.IT_MANAGER, -4, ("hr_record_create",)),
            _t("orientation_slot", c.ORIENTATION, Role.HR_OPS, 0, ("hr_record_create",)),
        ),
        # INSURANCE and MEDICAL are deliberately unmapped: a synthetic assignment gap for the tests.
        assignments=_rules(
            (TaskCategory.HR_RECORD, "hr_ops", ""), (TaskCategory.DOCUMENT_COLLECTION, "hr_ops", "UC-03"),
            (TaskCategory.BACKGROUND_VERIFICATION, "hr_officer", ""), (TaskCategory.PAYROLL, "finance", ""),
            (TaskCategory.IT_PROVISIONING, "it_manager", "UC-04"), (TaskCategory.ORIENTATION, "hr_ops", "UC-05"),
        ),
        document_requirements=(
            _doc("identity", reason="synthetic: identity check for every employee"),
            _doc("address_proof", reason="synthetic: address check for every employee"),
            _doc("medical_report", reason="synthetic: only when the profile declares a medical check",
                 requires_attributes=("medical_required",)),
        ),
    )


def registry() -> ConfigRegistry:
    reg = ConfigRegistry()
    reg.register(uae_config())
    reg.register(india_config())
    return reg


@dataclass(frozen=True)
class Profile:
    name: str
    client_id: str
    employee_id: str
    role_id: str
    grade: str
    location: str
    nationality_group: str
    attributes: tuple[str, ...]
    joining_date: date = JOINING
    current_location_status: str = ""


PROFILES: dict[str, Profile] = {
    "uae_inside": Profile("UAE, already in the UAE, senior", UAE_CLIENT, "EMP-SYN-U1", "eng", "L4", "Dubai", "grp-a",
                          ("bv_required", "insurance_required"), current_location_status="in_country"),
    "uae_overseas": Profile("UAE, overseas, junior, needs travel and housing", UAE_CLIENT, "EMP-SYN-U2", "eng", "L2",
                            "Dubai", "grp-b", ("bv_required", "needs_travel", "needs_accommodation"),
                            current_location_status="overseas"),
    "uae_unstated": Profile("UAE, location not stated", UAE_CLIENT, "EMP-SYN-U3", "eng", "L3", "Dubai", "grp-a", ()),
    "india_plain": Profile("India, no extras", INDIA_CLIENT, "EMP-SYN-I1", "eng", "L3", "Pune", "grp-a", ()),
    "india_full": Profile("India, medical and insurance", INDIA_CLIENT, "EMP-SYN-I2", "eng", "L4", "Pune", "grp-a",
                          ("medical_required", "insurance_required")),
}


def trigger_for(profile: Profile, *, case_id: str | None = None, joining_date: date | None = None) -> OnboardingTrigger:
    return OnboardingTrigger(
        client_id=profile.client_id, employee_id=profile.employee_id, joining_date=joining_date or profile.joining_date,
        role_id=profile.role_id, department="Engineering", manager_id=f"MGR-{profile.client_id}",
        location=profile.location, project="synthetic-project", grade=profile.grade,
        nationality_group=profile.nationality_group, attributes=profile.attributes,
        current_location_status=profile.current_location_status, case_id=case_id,
        offer=accepted_offer(profile.employee_id), form=approved_form(profile.employee_id))


__all__ = ["INDIA_CLIENT", "PROFILES", "Profile", "SYNTHETIC_PREFIX", "TEAM_LABELS", "UAE_CLIENT", "india_config",
           "is_synthetic", "registry", "trigger_for", "uae_config"]
