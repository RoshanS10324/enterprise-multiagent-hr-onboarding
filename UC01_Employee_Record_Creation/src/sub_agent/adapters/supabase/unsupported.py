"""Ports with no compatible table in the confirmed Onboarding_Agent schema.

Each adapter here raises before touching the database. They exist so the
gap is explicit and testable, instead of being papered over by inventing
columns, fabricating values, or silently falling back to memory.

Every message names the exact missing capability and what decision is
needed. See docs/schema/live-schema-comparison.md for the full evidence.
"""

from __future__ import annotations

from typing import Optional

from ...models import Approval, AuditEntry, Case, OfferRecord


class SchemaGapError(RuntimeError):
    """The live schema cannot store this entity without a decision."""


_CASE_GAP = (
    "onboarding_cases cannot store a UC-01 Case yet.\n"
    "\n"
    "DECIDED (D1/D2, approved): role_id and manager_id are NO LONGER "
    "required for UC-01 case creation. UC-01 never produced them - they "
    "appear zero times in src/, specs/, skills/ and the "
    "POST /internal/onboarding-requests contract - so nothing in the "
    "application had to change. What remains is a DATABASE constraint: "
    "both columns are still NOT NULL with no default in the live "
    "onboarding_cases table. The smallest safe change is in "
    "docs/schema/proposed/d4_d5_additive.sql (D1/D2 section); it has NOT "
    "been applied.\n"
    "\n"
    "STILL BLOCKING (D3, open): these columns are also NOT NULL with no "
    "default, and UC-01 has no value for any of them:\n"
    "    department, grade, nationality_group, location_name,\n"
    "    project_name, current_stage, onboarding_attributes\n"
    "\n"
    "So resolving D1/D2 did NOT unblock case creation on its own. Each of "
    "the above still needs an owner decision - supplied upstream, derived "
    "by a stated rule, or made nullable - per "
    "docs/uc01-supabase-decisions.md D3.\n"
    "\n"
    "Fabricating values, inserting empty strings, or dropping NOT NULL "
    "without the owner's decision all remain excluded."
)

_AUDIT_GAP = (
    "audit_events cannot store a UC-01 AuditEntry without breaking audit "
    "integrity.\n"
    "\n"
    "AuditEntry is hash-chained (src/sub_agent/audit_integrity.py): every "
    "entry carries sequence, prev_hash and entry_hash, and chain_entry() "
    "links each row to its predecessor so a later edit, deletion or "
    "reordering is detectable. The live table has NONE of those three "
    "columns.\n"
    "\n"
    "Writing entries without them would produce an audit log that looks "
    "complete but whose integrity can never be verified - /audit/integrity "
    "would have nothing to check. That is a worse outcome than not "
    "persisting at all, so this adapter refuses.\n"
    "\n"
    "Additionally the live table requires client_id, actor_role, outcome and "
    "detail (all NOT NULL); AuditEntry has none of them. It also names the "
    "timestamp occurred_at and the correlation id correlation_id.\n"
    "\n"
    "To unblock: add sequence (int), prev_hash (text) and entry_hash (text) "
    "to audit_events, and decide how to populate client_id/actor_role/"
    "outcome/detail for UC-01 entries."
)

_APPROVAL_GAP = (
    "approval_requests is a different entity from UC-01's Approval.\n"
    "\n"
    "Live table: request_id (text PK), gate, approver_role, workflow_module, "
    "payload (jsonb NOT NULL), rationale (NOT NULL), confidence_score "
    "(numeric NOT NULL), outcome, sla_due_at (NOT NULL), decision_reason_code, "
    "final_payload, apply_error.\n"
    "UC-01 Approval: approval_id (uuid), checkpoint, subject_ref, "
    "requested_to, status, reason, decision_note.\n"
    "\n"
    "It is a cross-use-case orchestration approval record, not UC-01's "
    "human-checkpoint approval. rationale, confidence_score and sla_due_at "
    "are NOT NULL and UC-01 produces none of them; requested_to has no "
    "column (approver_role is a role, not a person).\n"
    "\n"
    "To unblock: confirm whether UC-01 should adopt the orchestration "
    "approval contract (and supply those fields), or whether UC-01 needs its "
    "own approvals table."
)

_OFFER_GAP = (
    "offers requires candidate_name, designation and offer_payload (jsonb), "
    "all NOT NULL. UC-01's OfferRecord carries only client_id, offer_ref, "
    "candidate_email, hr_officer_id, status and accepted_at.\n"
    "\n"
    "OfferRecord is also marked [Proposed] / dev-simulator-only in ports.py, "
    "so writing to a shared production table from it would be inappropriate "
    "regardless. Reads could be supported once the field mapping is agreed."
)

_COUNTER_GAP = (
    "employee_number_counters cannot be driven safely over PostgREST.\n"
    "\n"
    "Allocating a number is a read-modify-write (SELECT last_value, then "
    "UPDATE last_value + 1). PostgREST issues one statement per call with no "
    "transaction and no row lock, so two concurrent requests can both read "
    "the same last_value and mint the SAME employee number. The in-memory "
    "generator has the same race, but it is not shared across processes.\n"
    "\n"
    "The live table also requires prefix and padding (NOT NULL), while the "
    "'EMP-%06d' format currently lives in Python - two sources of truth.\n"
    "\n"
    "To unblock: expose a Postgres function (e.g. "
    "next_employee_no(client_id) doing UPDATE ... RETURNING, which is atomic) "
    "and call it via RPC, or provide a direct psycopg connection so the "
    "allocation can hold a row lock."
)


class _Refusing:
    def __init__(self, message: str) -> None:
        self._message = message

    def _fail(self) -> None:
        raise SchemaGapError(self._message)


class UnsupportedCaseRepository(_Refusing):
    """CaseRepository: blocked on onboarding_cases NOT NULL columns."""

    def __init__(self) -> None:
        super().__init__(_CASE_GAP)

    def save(self, case: Case) -> None:
        self._fail()

    def get(self, case_id: str) -> Optional[Case]:
        self._fail()
        return None

    def list_for_client(self, client_id: str) -> list[Case]:
        self._fail()
        return []


class UnsupportedAuditLog(_Refusing):
    """AuditLog: blocked on the missing hash-chain columns."""

    def __init__(self) -> None:
        super().__init__(_AUDIT_GAP)

    def append(self, entry: AuditEntry) -> None:
        self._fail()

    def entries(self) -> list[AuditEntry]:
        self._fail()
        return []

    def entries_for_case(self, case_id: str) -> list[AuditEntry]:
        self._fail()
        return []


class UnsupportedApprovalRepository(_Refusing):
    """ApprovalRepository: approval_requests models a different entity."""

    def __init__(self) -> None:
        super().__init__(_APPROVAL_GAP)

    def add(self, approval: Approval) -> None:
        self._fail()

    def get(self, approval_id: str) -> Optional[Approval]:
        self._fail()
        return None

    def list_for_case(self, case_id: str) -> list[Approval]:
        self._fail()
        return []

    def update(self, approval: Approval) -> None:
        self._fail()

    def list_pending_for(self, requested_to: str) -> list[Approval]:
        self._fail()
        return []


class UnsupportedOfferRepository(_Refusing):
    """OfferRepository: offers requires fields OfferRecord does not have."""

    def __init__(self) -> None:
        super().__init__(_OFFER_GAP)

    def get(self, client_id: str, offer_ref: str) -> Optional[OfferRecord]:
        self._fail()
        return None

    def find_by_ref(self, offer_ref: str) -> list[OfferRecord]:
        self._fail()
        return []

    def save(self, offer: OfferRecord) -> None:
        self._fail()


class UnsupportedEmployeeNumberGenerator(_Refusing):
    """EmployeeNumberGenerator: no atomic allocation over PostgREST."""

    def __init__(self) -> None:
        super().__init__(_COUNTER_GAP)

    def next(self, client_id: str) -> str:
        self._fail()
        return ""


class UnsupportedDocumentStore(_Refusing):
    """DocumentStore: no document-folder table in the live schema."""

    def __init__(self) -> None:
        super().__init__(
            "No table in the confirmed schema backs DocumentStore. "
            "create_case_folder() targets an external document system "
            "(SharePoint/Drive), which has no connector in UC-01 yet; "
            "candidate_documents stores validated documents, not case folders."
        )

    def create_case_folder(self, client_id: str, case_id: str) -> str:
        self._fail()
        return ""


class UnsupportedFormDetailsRepository(_Refusing):
    """FormDetailsRepository when the "form_details" port is not enabled.

    ensure_ready() refuses, and the document-intake service calls it BEFORE
    opening a case - so in Supabase mode a document is never turned into a
    case whose details and budget would then have nowhere to go."""

    def __init__(self) -> None:
        super().__init__(
            "The onboarding-document details and budget have no enabled store: the uc01_onboarding_* "
            "tables are PROPOSED (docs/schema/proposed/20261105120000_uc01_form_details_and_budget.sql) "
            "and the 'form_details' port is not enabled.")

    def ensure_ready(self) -> None:
        self._fail()

    def save(self, record: object) -> None:
        self._fail()

    def get(self, form_id: str) -> None:
        self._fail()

    def find_by_document(self, client_id: str, sha256: str) -> list[str]:
        self._fail()
        return []


__all__ = [
    "SchemaGapError",
    "UnsupportedApprovalRepository",
    "UnsupportedAuditLog",
    "UnsupportedCaseRepository",
    "UnsupportedDocumentStore",
    "UnsupportedEmployeeNumberGenerator",
    "UnsupportedFormDetailsRepository",
    "UnsupportedOfferRepository",
]
