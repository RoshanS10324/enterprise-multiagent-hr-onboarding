"""Single source of truth for our-field -> target-system-field mapping.

Target field names are neutral placeholders: which HRIS/payroll product
this ultimately targets (Zoho vs Workday/ADP/other) is still an open
question. Only the shape (which of our fields map where, and which are
sensitive) is meant to be stable; the target names will change once that's
decided.
"""

from typing import NamedTuple, Optional

from pydantic import SecretStr

from .models import Case, HrisSystem, OnboardingForm


class FieldMapping(NamedTuple):
    our_field: str
    target_field: str
    sensitive: bool


# People record: identity and role info, nothing sensitive.
PEOPLE_FIELDS: tuple[FieldMapping, ...] = (
    FieldMapping("employee_no", "employee_number", False),
    FieldMapping("candidate_name", "full_name", False),
    FieldMapping("candidate_email", "work_email", False),
    FieldMapping("designation", "job_title", False),
    FieldMapping("joining_date", "start_date", False),
    FieldMapping("work_location_type", "work_location_type", False),
    FieldMapping("client_site_id", "site_reference", False),
)

# Payroll record: compensation. salary_and_terms is sensitive.
PAYROLL_FIELDS: tuple[FieldMapping, ...] = (
    FieldMapping("employee_no", "employee_number", False),
    FieldMapping("joining_date", "start_date", False),
    FieldMapping("designation", "job_title", False),
    FieldMapping("salary_and_terms", "compensation_terms", True),
)

HRIS_FIELD_MAPPING: dict[HrisSystem, tuple[FieldMapping, ...]] = {
    HrisSystem.ZOHO_PEOPLE: PEOPLE_FIELDS,
    HrisSystem.ZOHO_PAYROLL: PAYROLL_FIELDS,
}


def sensitive_field_names(system: HrisSystem) -> list[str]:
    """Target field NAMES only (never values) that are sensitive for this system."""
    return [f.target_field for f in HRIS_FIELD_MAPPING[system] if f.sensitive]


def _source_value(our_field: str, case: Optional[Case], form: OnboardingForm):
    if our_field == "employee_no":
        return case.employee_no if case is not None else None
    if our_field == "candidate_name":
        return form.candidate_name
    if our_field == "candidate_email":
        return form.candidate_email
    if our_field == "designation":
        return form.designation
    if our_field == "joining_date":
        return form.joining_date.isoformat()
    if our_field == "work_location_type":
        return form.work_location_type.value
    if our_field == "client_site_id":
        return form.client_site_id
    if our_field == "salary_and_terms":
        return form.salary_and_terms
    raise ValueError(f"unmapped source field: {our_field}")


def prepare_payloads(system: HrisSystem, case: Case, form: OnboardingForm) -> dict[str, str]:
    """Build the non-sensitive prepared_payload for one HRIS system.

    Fields whose source value is None (e.g. client_site_id on an OFFICE
    placement) are left out entirely rather than sent as empty/"None".
    Sensitive fields are skipped here entirely: see build_sensitive_payload,
    which is only ever called at write time, never stored.
    """
    mapping = HRIS_FIELD_MAPPING[system]
    prepared: dict[str, str] = {}

    for field in mapping:
        if field.sensitive:
            continue
        value = _source_value(field.our_field, case, form)
        if value is None:
            continue
        prepared[field.target_field] = str(value)

    return prepared


def build_sensitive_payload(system: HrisSystem, form: OnboardingForm) -> dict[str, SecretStr]:
    """Build the sensitive payload (salary) fresh from the form, at write time only.

    Never stored on HrisRecord: only built in-memory right before a
    gateway.write() call.
    """
    mapping = HRIS_FIELD_MAPPING[system]
    sensitive: dict[str, SecretStr] = {}

    for field in mapping:
        if not field.sensitive:
            continue
        value = _source_value(field.our_field, None, form)
        if value is None:
            continue
        sensitive[field.target_field] = value if isinstance(value, SecretStr) else SecretStr(value)

    return sensitive
