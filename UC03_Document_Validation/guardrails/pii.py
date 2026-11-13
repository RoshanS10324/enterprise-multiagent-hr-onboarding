from __future__ import annotations

from enum import Enum

AUTHORIZED_ROLES_BY_FIELD: dict[str, set[str]] = {
    "medical": {"HR_USER"},
    "criminal": {"HR_USER"},
    "disability": {"HR_USER"},
    "salary": {"HR_USER", "PRO_USER"},
    "bank": {"HR_USER"},
}


class PIIField(str, Enum):
    MEDICAL = "medical"
    CRIMINAL = "criminal"
    DISABILITY = "disability"
    SALARY = "salary"
    BANK = "bank"


SENSITIVE_CATEGORIES: set[PIIField] = set(PIIField)


def mask_sensitive_label(field: PIIField) -> str:
    return "HR item"


def is_restricted_field(field_name: str, actor_role: str) -> bool:
    """Return True if the field is sensitive and the actor role is not authorised."""
    authorised = AUTHORIZED_ROLES_BY_FIELD.get(field_name.lower(), set())
    return bool(authorised) and actor_role not in authorised
