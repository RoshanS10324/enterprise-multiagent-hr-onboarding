"""Value mappings between UC-03's enums and the shared schema's CHECK lists
(consolidated_database_schema.sql).

Where the shared list is narrower than UC-03's, the shared column gets the closest value and UC-03's
exact value is kept
in an additive column (see supabase/migrations/20261104120000_uc03_persistence.sql). A value with no
defensible
mapping raises `UnmappedValue`: it is never guessed.
"""

from __future__ import annotations

import uuid

# candidate_documents.document_type CHECK list. UC-03 types without a counterpart are not stored
# there (reported).
DOCUMENT_TYPES = {
    "passport": "PASSPORT",
    "emirates_id": "NATIONAL_ID",
    "national_id": "NATIONAL_ID",
    "visa": "VISA",
    "visa_application": "VISA",
    "visa_application_documents": "VISA",
    "degree_certificate": "EDUCATIONAL_CERTIFICATE",
    "educational_certificate": "EDUCATIONAL_CERTIFICATE",
    "signed_offer_letter": "OFFER_LETTER",
    "offer_letter": "OFFER_LETTER",
    "medical_fitness": "MEDICAL_FITNESS",
    "background_check": "BACKGROUND_CHECK",
    "policy_acknowledgements": "SIGNED_POLICY",
    "non_disclosure_agreement": "SIGNED_POLICY",
    "nda": "SIGNED_POLICY",
}
# No counterpart in the shared list (recorded as a schema gap): experience_letter, payslips,
# family_dependent_documents.

COMPLIANCE = {
    "COMPLETE": "COMPLIANT",
    "INCOMPLETE": "INCOMPLETE",
    "PENDING_APPROVAL": "INCOMPLETE",
    "BLOCKED": "NON_COMPLIANT",
}
VISA_PACK = {
    "NOT_APPLICABLE": "NOT_APPLICABLE",
    "OUTSTANDING_ITEMS": "INCOMPLETE",
    "READY_FOR_PRO": "READY",
    "SUBMITTED": "READY",
}
HR_REVIEW = {
    "PENDING": "PENDING",
    "APPROVED": "ACCEPTED",
    "REJECTED": "REJECTED",
    "NOT_REQUIRED": "NOT_REQUIRED",
}
ACTOR_TYPE = {
    "HR_USER": "HUMAN",
    "PRO_USER": "HUMAN",
    "CANDIDATE": "HUMAN",
    "AGENT": "AGENT",
    "SYSTEM": "SYSTEM",
}
APPROVAL_OUTCOME = {"PENDING": "PENDING", "APPROVED": "APPROVED", "REJECTED": "REJECTED"}
CANDIDATE_DOCUMENT_STATUS = {
    "REJECTED": "REJECTED"
}  # anything else that was validated -> "VALIDATED"


class UnmappedValue(ValueError):
    """A UC-03 value has no counterpart in the shared schema. Never guessed."""


def mapped(table: dict[str, str], value: object, what: str) -> str:
    key = getattr(value, "value", value)
    if key not in table:
        raise UnmappedValue(f"{what}: {key!r} has no counterpart in the shared schema")
    return table[key]


def as_uuid(value: str | None) -> str | None:
    """`value` as a canonical uuid string, or None when it is not a uuid (UC-03 standalone ids, e.g.
    'case-001').
    """
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def stable_uuid(value: str) -> str:
    """A uuid for an id that is not one (deterministic, so a retried write lands on the same primary
    key).
    """
    return as_uuid(value) or str(uuid.uuid5(uuid.NAMESPACE_URL, f"uc03:{value}"))
