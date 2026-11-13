"""Durable adapters backed by the confirmed Onboarding_Agent schema.

Only ports whose live table actually supports the model are implemented
here. Where a model field has no column, the adapter REFUSES the write
rather than dropping the field silently - a silently discarded
`hris_external_ref` or `updates_external_ref` would corrupt the rehire
and form-change flows in ways no test would catch. The ports with no
usable table at all live in unsupported.py.

Every column name below was read from the live PostgREST schema, not
from docs/schema/uc01_draft.sql (which has drifted from both the models
and the database). Renames applied:

    draft/model            live table/column
    ---------------------  ----------------------------------
    discrepancies          intake_discrepancies
    Discrepancy.field      intake_discrepancies.field_name
    hris_records           hris_sync_records
    HrisRecord.system      hris_sync_records.target_system
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable, Mapping, Optional

from ...audit_integrity import GENESIS_HASH, chain_entry, check_entry
from ...audit_scope import current_audit_client
from ...models import (
    ActorType,
    Approval,
    AuditEntry,
    Case,
    CaseStatus,
    VariantFlags,
    Discrepancy,
    DomainEvent,
    HrisRecord,
    OnboardingForm,
    PendingStart,
    PersonRecord,
)
from ...crypto import FieldCipher
from .client import PostgrestClient, SupabaseError, UniqueViolation, decode_bytea, encode_bytea


def _eq(value: str) -> str:
    return f"eq.{value}"


def _ilike_exact(value: str) -> str:
    r"""A case-insensitive EXACT match, not a pattern match.

    PostgREST's `ilike` treats `%` and `_` as wildcards. Interpolating a
    raw value is wrong for the lookups UC-01 does: `john_doe@x.com`
    would match `johnXdoe@x.com`, so duplicate detection could link a
    new candidate to an unrelated person's open case. Underscores in
    email addresses are ordinary, so this is not a corner case.

    Escaping them with a backslash restores the exact-match semantics
    InMemoryPersonDirectory has (strip + lower + ==).
    """
    escaped = value.strip().replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
    return f"ilike.{escaped}"


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


class UnsupportedFieldError(RuntimeError):
    """A model carries data the live schema has nowhere to put."""


# ---------------------------------------------------------------- forms


class SupabaseFormRepository:
    """FormRepository against onboarding_forms.

    The only table that maps to its model with no gaps. salary_and_terms
    is AES-GCM encrypted into salary_and_terms_encrypted (bytea), bound
    to form_id as AAD so a ciphertext cannot be moved between rows.
    """

    TABLE = "onboarding_forms"
    _COLS = (
        "form_id,client_id,case_id,version,candidate_name,candidate_email,"
        "designation,joining_date,work_location_type,client_site_id,"
        "salary_and_terms_encrypted,status,approved_by,approved_at,change_reason"
    )

    def __init__(self, client: PostgrestClient, cipher: FieldCipher) -> None:
        self._c = client
        self._cipher = cipher

    def _to_row(self, form: OnboardingForm) -> dict[str, Any]:
        sealed = self._cipher.encrypt(
            form.salary_and_terms.get_secret_value(), aad=form.form_id
        )
        return {
            "form_id": form.form_id,
            "client_id": form.client_id,
            "case_id": form.case_id,
            "version": form.version,
            "candidate_name": form.candidate_name,
            "candidate_email": form.candidate_email,
            "designation": form.designation,
            "joining_date": form.joining_date.isoformat(),
            "work_location_type": str(form.work_location_type),
            "client_site_id": form.client_site_id,
            "salary_and_terms_encrypted": encode_bytea(sealed),
            "status": str(form.status),
            "approved_by": form.approved_by,
            "approved_at": _iso(form.approved_at),
            "change_reason": form.change_reason,
        }

    def _from_row(self, row: dict[str, Any]) -> OnboardingForm:
        blob = decode_bytea(row["salary_and_terms_encrypted"])
        assert blob is not None  # NOT NULL in the live schema
        return OnboardingForm(
            form_id=row["form_id"],
            client_id=row["client_id"],
            case_id=row["case_id"],
            version=row["version"],
            candidate_name=row["candidate_name"],
            candidate_email=row["candidate_email"],
            designation=row["designation"],
            joining_date=date.fromisoformat(row["joining_date"]),
            work_location_type=row["work_location_type"],
            client_site_id=row["client_site_id"],
            salary_and_terms=self._cipher.decrypt(blob, aad=row["form_id"]),
            status=row["status"],
            approved_by=row["approved_by"],
            approved_at=_dt(row["approved_at"]),
            change_reason=row["change_reason"],
        )

    def save(self, form: OnboardingForm) -> None:
        # The in-memory port is save-or-replace keyed on form_id; upsert
        # reproduces that in one statement.
        self._c.upsert(self.TABLE, [self._to_row(form)], on_conflict="form_id")

    def latest_approved(self, case_id: str) -> Optional[OnboardingForm]:
        rows = self._c.select(
            self.TABLE,
            columns=self._COLS,
            filters={"case_id": _eq(case_id), "status": _eq("APPROVED")},
            order="version.desc",
            limit=1,
        )
        return self._from_row(rows[0]) if rows else None

    def get(self, case_id: str, version: int) -> Optional[OnboardingForm]:
        rows = self._c.select(
            self.TABLE,
            columns=self._COLS,
            filters={"case_id": _eq(case_id), "version": _eq(str(version))},
            limit=1,
        )
        return self._from_row(rows[0]) if rows else None

    def list_versions(self, case_id: str) -> list[OnboardingForm]:
        rows = self._c.select(
            self.TABLE,
            columns=self._COLS,
            filters={"case_id": _eq(case_id)},
            order="version.asc",
        )
        return [self._from_row(r) for r in rows]


# -------------------------------------------------------- person records


class SupabasePersonDirectory:
    """PersonDirectory against person_records.

    `hris_external_ref` (text, nullable) drives rehire/internal-transfer
    reactivation (EC-43). The live column now exists (verified read-only
    2026-11-04), so it is written and read back like every other field;
    None is stored as SQL NULL, never dropped.
    """

    TABLE = "person_records"
    _COLS = "record_id,client_id,email,id_number,full_name,kind,case_id,hris_external_ref"

    def __init__(self, client: PostgrestClient) -> None:
        self._c = client

    @staticmethod
    def _from_row(row: dict[str, Any]) -> PersonRecord:
        return PersonRecord(
            record_id=row["record_id"],
            client_id=row["client_id"],
            email=row["email"],
            id_number=row["id_number"],
            full_name=row["full_name"],
            kind=row["kind"],
            case_id=row["case_id"],
            hris_external_ref=row.get("hris_external_ref"),
        )

    def add(self, record: PersonRecord) -> None:
        self._c.insert(
            self.TABLE,
            [
                {
                    "record_id": record.record_id,
                    "client_id": record.client_id,
                    "email": record.email,
                    "id_number": record.id_number,
                    "full_name": record.full_name,
                    "kind": str(record.kind),
                    "case_id": record.case_id,
                    "hris_external_ref": record.hris_external_ref,
                }
            ],
            returning=False,
        )

    def find_by_email(self, client_id: str, email: str) -> list[PersonRecord]:
        # Case-insensitive, matching InMemoryPersonDirectory's .strip().lower().
        rows = self._c.select(
            self.TABLE,
            columns=self._COLS,
            filters={"client_id": _eq(client_id), "email": _ilike_exact(email)},
        )
        return [self._from_row(r) for r in rows]

    def find_by_id_number(self, client_id: str, id_number: str) -> list[PersonRecord]:
        rows = self._c.select(
            self.TABLE,
            columns=self._COLS,
            filters={"client_id": _eq(client_id), "id_number": _eq(id_number.strip())},
        )
        return [self._from_row(r) for r in rows]

    def find_by_name(self, client_id: str, full_name: str) -> list[PersonRecord]:
        rows = self._c.select(
            self.TABLE,
            columns=self._COLS,
            filters={"client_id": _eq(client_id), "full_name": _ilike_exact(full_name)},
        )
        return [self._from_row(r) for r in rows]


# ---------------------------------------------------------- discrepancies


class SupabaseDiscrepancyRepository:
    """DiscrepancyRepository against intake_discrepancies (field -> field_name)."""

    TABLE = "intake_discrepancies"
    _COLS = (
        "discrepancy_id,case_id,field_name,value_form,value_other,other_source,"
        "form_version,status,decided_by,decided_at,other_fingerprint"
    )

    def __init__(self, client: PostgrestClient) -> None:
        self._c = client

    @staticmethod
    def _from_row(row: dict[str, Any]) -> Discrepancy:
        return Discrepancy(
            discrepancy_id=row["discrepancy_id"],
            case_id=row["case_id"],
            field=row["field_name"],
            value_form=row["value_form"],
            value_other=row["value_other"],
            other_source=row["other_source"],
            form_version=row["form_version"],
            status=row["status"],
            decided_by=row["decided_by"],
            decided_at=_dt(row["decided_at"]),
            other_fingerprint=row["other_fingerprint"],
        )

    @staticmethod
    def _to_row(d: Discrepancy) -> dict[str, Any]:
        if d.other_fingerprint is None:
            # NOT NULL live, and it is the dedup key - a NULL would make
            # every comparison re-raise the same discrepancy forever.
            raise UnsupportedFieldError(
                "intake_discrepancies.other_fingerprint is NOT NULL and is the "
                "de-duplication key; a Discrepancy without one cannot be stored."
            )
        return {
            "discrepancy_id": d.discrepancy_id,
            "case_id": d.case_id,
            "field_name": d.field,
            "value_form": d.value_form,
            "value_other": d.value_other,
            "other_source": d.other_source,
            "form_version": d.form_version,
            "status": str(d.status),
            "decided_by": d.decided_by,
            "decided_at": _iso(d.decided_at),
            "other_fingerprint": d.other_fingerprint,
        }

    def add(self, discrepancy: Discrepancy) -> None:
        self._c.insert(self.TABLE, [self._to_row(discrepancy)], returning=False)

    def get(self, discrepancy_id: str) -> Optional[Discrepancy]:
        rows = self._c.select(
            self.TABLE, columns=self._COLS,
            filters={"discrepancy_id": _eq(discrepancy_id)}, limit=1,
        )
        return self._from_row(rows[0]) if rows else None

    def list_for_case(self, case_id: str) -> list[Discrepancy]:
        rows = self._c.select(
            self.TABLE, columns=self._COLS, filters={"case_id": _eq(case_id)}
        )
        return [self._from_row(r) for r in rows]

    def update(self, discrepancy: Discrepancy) -> None:
        row = self._to_row(discrepancy)
        row.pop("discrepancy_id")
        self._c.update(
            self.TABLE, row, filters={"discrepancy_id": _eq(discrepancy.discrepancy_id)}
        )


# ----------------------------------------------------------- hris records


class SupabaseHrisRecordRepository:
    """HrisRecordRepository against hris_sync_records (system -> target_system).

    `updates_external_ref` (text, nullable) is written and read back: it is
    what makes an HRIS update target the existing employee instead of
    minting a second one. The live column now exists (verified read-only
    2026-11-04).

    GAP, still refused rather than dropped:
      * comparison_result - no live column (model field has no writer today)
    Also: live idempotency_key is GENERATED ALWAYS AS
    (case_id::text || ':' || target_system || ':' || form_version::text)
    with a UNIQUE constraint (verified against the live project
    2026-11-04). Postgres rejects any insert or update that supplies it
    (SQLSTATE 428C9), so _to_row never sends it; the database derives
    the same value HrisRecord._derive_idempotency_key does, and its
    UNIQUE constraint is what turns a repeat into UniqueViolation.
    """

    TABLE = "hris_sync_records"
    _COLS = (
        "record_id,case_id,client_id,employee_no,target_system,form_version,"
        "prepared_payload,sensitive_field_names,external_ref,last_error,status,"
        "approved_by,approved_at,idempotency_key,updates_external_ref"
    )

    def __init__(self, client: PostgrestClient) -> None:
        self._c = client

    @staticmethod
    def _to_row(r: HrisRecord) -> dict[str, Any]:
        if getattr(r, "comparison_result", None) is not None:
            raise UnsupportedFieldError(
                "hris_sync_records has no comparison_result column."
            )
        return {
            "record_id": r.record_id,
            "case_id": r.case_id,
            "client_id": r.client_id,
            "employee_no": r.employee_no,
            "target_system": str(r.system),
            "form_version": r.form_version,
            "prepared_payload": r.prepared_payload,
            "sensitive_field_names": list(r.sensitive_field_names),
            "external_ref": r.external_ref,
            "last_error": r.last_error,
            "status": str(r.status),
            "approved_by": r.approved_by,
            "approved_at": _iso(r.approved_at),
            "updates_external_ref": r.updates_external_ref,
            # idempotency_key is GENERATED ALWAYS in the database: never sent.
        }

    @staticmethod
    def _from_row(row: dict[str, Any]) -> HrisRecord:
        return HrisRecord(
            record_id=row["record_id"],
            case_id=row["case_id"],
            client_id=row["client_id"],
            employee_no=row["employee_no"],
            system=row["target_system"],
            form_version=row["form_version"],
            prepared_payload=row["prepared_payload"] or {},
            sensitive_field_names=row["sensitive_field_names"] or [],
            external_ref=row["external_ref"],
            last_error=row["last_error"],
            status=row["status"],
            approved_by=row["approved_by"],
            approved_at=_dt(row["approved_at"]),
            updates_external_ref=row.get("updates_external_ref"),
        )

    def add(self, record: HrisRecord) -> None:
        try:
            self._c.insert(self.TABLE, [self._to_row(record)], returning=False)
        except UniqueViolation as exc:
            raise UniqueViolation(
                "an HRIS record already exists for this case/system/form version "
                "(idempotency_key collision); the write was not duplicated"
            ) from exc

    def get(self, record_id: str) -> Optional[HrisRecord]:
        rows = self._c.select(
            self.TABLE, columns=self._COLS, filters={"record_id": _eq(record_id)}, limit=1
        )
        return self._from_row(rows[0]) if rows else None

    def list_for_case(self, case_id: str) -> list[HrisRecord]:
        rows = self._c.select(
            self.TABLE, columns=self._COLS, filters={"case_id": _eq(case_id)}
        )
        return [self._from_row(r) for r in rows]

    def update(self, record: HrisRecord) -> None:
        row = self._to_row(record)
        row.pop("record_id")
        self._c.update(self.TABLE, row, filters={"record_id": _eq(record.record_id)})


# ------------------------------------------------------------ outbox etc.


class SupabaseEventPublisher:
    """EventPublisher -> domain_events_outbox (staged, relayed separately)."""

    TABLE = "domain_events_outbox"

    def __init__(self, client: PostgrestClient) -> None:
        self._c = client

    def publish(self, event: DomainEvent) -> None:
        payload = event.model_dump(mode="json")
        event_id = payload.pop("event_id")
        client_id = payload.pop("client_id")
        case_id = payload.pop("case_id", None)
        occurred_at = payload.pop("occurred_at", None)
        self._c.insert(
            self.TABLE,
            [
                {
                    "event_id": event_id,
                    "case_id": case_id,
                    "client_id": client_id,
                    "event_type": type(event).__name__,
                    "payload": payload,
                    "occurred_at": occurred_at,
                    "publish_attempts": 0,
                }
            ],
            returning=False,
        )


class SupabaseHolidayCalendar:
    """HolidayCalendar -> client_holidays (read-only; observed days only)."""

    TABLE = "client_holidays"

    def __init__(self, client: PostgrestClient) -> None:
        self._c = client

    def is_holiday(self, client_id: str, day: date) -> bool:
        rows = self._c.select(
            self.TABLE,
            columns="holiday_date",
            filters={
                "client_id": _eq(client_id),
                "holiday_date": _eq(day.isoformat()),
                "is_observed": "eq.true",
            },
            limit=1,
        )
        return bool(rows)


class SupabasePendingStartRepository:
    """PendingStartRepository -> pending_starts."""

    TABLE = "pending_starts"
    _COLS = "pending_id,client_id,offer_ref,reasons,status,created_at"

    def __init__(self, client: PostgrestClient) -> None:
        self._c = client

    @staticmethod
    def _from_row(row: dict[str, Any]) -> PendingStart:
        return PendingStart(
            pending_id=row["pending_id"],
            client_id=row["client_id"],
            offer_ref=row["offer_ref"],
            reasons=row["reasons"] or [],
            status=row["status"],
            created_at=_dt(row["created_at"]),
        )

    def add(self, pending: PendingStart) -> None:
        self._c.insert(
            self.TABLE,
            [
                {
                    "pending_id": pending.pending_id,
                    "client_id": pending.client_id,
                    "offer_ref": pending.offer_ref,
                    "reasons": list(pending.reasons),
                    "status": str(pending.status),
                    "created_at": _iso(pending.created_at),
                }
            ],
            returning=False,
        )

    def get_waiting(self, client_id: str, offer_ref: str) -> Optional[PendingStart]:
        rows = self._c.select(
            self.TABLE,
            columns=self._COLS,
            filters={
                "client_id": _eq(client_id),
                "offer_ref": _eq(offer_ref),
                "status": _eq("WAITING"),
            },
            limit=1,
        )
        return self._from_row(rows[0]) if rows else None

    def list_waiting(self, client_id: str) -> list[PendingStart]:
        rows = self._c.select(
            self.TABLE,
            columns=self._COLS,
            filters={"client_id": _eq(client_id), "status": _eq("WAITING")},
        )
        return [self._from_row(r) for r in rows]

    def update(self, pending: PendingStart) -> None:
        self._c.update(
            self.TABLE,
            {"status": str(pending.status), "reasons": list(pending.reasons)},
            filters={"pending_id": _eq(pending.pending_id)},
        )


class SupabaseOnboardingFormSource:
    """OnboardingFormSource -> offer_forms bridge + onboarding_forms."""

    TABLE = "offer_forms"

    def __init__(self, client: PostgrestClient, forms: SupabaseFormRepository) -> None:
        self._c = client
        self._forms = forms

    def latest_for_offer(self, client_id: str, offer_ref: str) -> Optional[OnboardingForm]:
        rows = self._c.select(
            self.TABLE,
            columns="form_id",
            filters={"client_id": _eq(client_id), "offer_ref": _eq(offer_ref)},
            limit=1,
        )
        if not rows:
            return None
        form_rows = self._c.select(
            SupabaseFormRepository.TABLE,
            columns=SupabaseFormRepository._COLS,
            filters={"form_id": _eq(rows[0]["form_id"])},
            limit=1,
        )
        return self._forms._from_row(form_rows[0]) if form_rows else None

    def save_for_offer(self, client_id: str, offer_ref: str, form: OnboardingForm) -> None:
        # The form row must exist before the bridge can reference it.
        # Two statements, not one transaction - PostgREST cannot group
        # them. Ordered so a failure leaves an orphan form (harmless)
        # rather than a bridge row pointing at a missing form (an FK error).
        self._forms.save(form)
        self._c.upsert(
            self.TABLE,
            [{"client_id": client_id, "offer_ref": offer_ref, "form_id": form.form_id}],
            on_conflict="client_id,offer_ref",
        )


# ---------------------------------------------------------------- approvals


class SupabaseApprovalRepository:
    """ApprovalRepository against uc01_approvals.

    The live table's columns match the Approval model one for one
    (approval_id uuid PK, case_id uuid FK -> onboarding_cases), so no
    field is dropped or renamed. Behaviour mirrors InMemoryApprovalRepository,
    including KeyError when updating an approval that does not exist.

    NOT wired into the container yet: every UC-01 flow audits first, and
    the audit store (decision B2) is not decided, so wiring this alone could
    never complete a flow.
    """

    TABLE = "uc01_approvals"
    _COLS = (
        "approval_id,case_id,checkpoint,subject_ref,requested_to,created_at,"
        "requested_by,status,decided_by,decided_at,reason,decision_note"
    )

    def __init__(self, client: PostgrestClient) -> None:
        self._c = client

    @staticmethod
    def _to_row(a: Approval) -> dict[str, Any]:
        return {
            "approval_id": a.approval_id,
            "case_id": a.case_id,
            "checkpoint": str(a.checkpoint),
            "subject_ref": a.subject_ref,
            "requested_to": a.requested_to,
            "created_at": _iso(a.created_at),
            "requested_by": a.requested_by,
            "status": str(a.status),
            "decided_by": a.decided_by,
            "decided_at": _iso(a.decided_at),
            "reason": a.reason,
            "decision_note": a.decision_note,
        }

    @staticmethod
    def _from_row(row: dict[str, Any]) -> Approval:
        return Approval(
            approval_id=row["approval_id"],
            case_id=row["case_id"],
            checkpoint=row["checkpoint"],
            subject_ref=row["subject_ref"],
            requested_to=row["requested_to"],
            created_at=datetime.fromisoformat(row["created_at"]),  # NOT NULL live
            requested_by=row["requested_by"],
            status=row["status"],
            decided_by=row["decided_by"],
            decided_at=_dt(row["decided_at"]),
            reason=row["reason"],
            decision_note=row["decision_note"],
        )

    def add(self, approval: Approval) -> None:
        self._c.insert(self.TABLE, [self._to_row(approval)], returning=False)

    def get(self, approval_id: str) -> Optional[Approval]:
        rows = self._c.select(self.TABLE, columns=self._COLS,
                              filters={"approval_id": _eq(approval_id)}, limit=1)
        return self._from_row(rows[0]) if rows else None

    def list_for_case(self, case_id: str) -> list[Approval]:
        rows = self._c.select(self.TABLE, columns=self._COLS,
                              filters={"case_id": _eq(case_id)}, order="created_at.asc")
        return [self._from_row(r) for r in rows]

    def update(self, approval: Approval) -> None:
        row = self._to_row(approval)
        row.pop("approval_id")
        updated = self._c.update(self.TABLE, row, filters={"approval_id": _eq(approval.approval_id)})
        if not updated:
            raise KeyError(f"no approval with id {approval.approval_id!r} to update")

    def list_pending_for(self, requested_to: str) -> list[Approval]:
        rows = self._c.select(self.TABLE, columns=self._COLS,
                              filters={"requested_to": _eq(requested_to), "status": _eq("PENDING")},
                              order="created_at.asc")
        return [self._from_row(r) for r in rows]


# ---------------------------------------------------------- employee numbers


_EMPLOYEE_NO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


class SupabaseEmployeeNumberGenerator:
    """EmployeeNumberGenerator over rpc/next_employee_no(p_client_id).

    Allocation happens inside ONE Postgres function call (one transaction),
    so the prefix/padding and the counter live in the database, not in
    Python. Whether the live function is concurrency-safe (a single
    UPDATE ... RETURNING, or row locking) cannot be seen through PostgREST:
    that is decision B7, and this adapter is not wired into the container
    until it is answered.

    The reply must be one non-empty employee number (a bare string, or a
    one-row/one-column result). Anything else is refused, never guessed.
    """

    FUNCTION = "next_employee_no"

    def __init__(self, client: PostgrestClient) -> None:
        self._c = client

    def next(self, client_id: str) -> str:
        value = self._c.rpc(self.FUNCTION, {"p_client_id": client_id})
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if isinstance(value, dict) and len(value) == 1:
            value = next(iter(value.values()))
        if not isinstance(value, str) or not _EMPLOYEE_NO.match(value):
            raise SupabaseError(f"{self.FUNCTION} returned an unexpected result shape")
        return value


# ---------------------------------------------------------------- cases


#: VariantFlags field -> live onboarding_cases column. Note the one rename:
#: the model's is_uae_resident is the live variant_is_resident.
VARIANT_COLUMNS: dict[str, str] = {
    "is_rehire": "variant_is_rehire",
    "is_internal_transfer": "variant_is_internal_transfer",
    "is_agency_contractor": "variant_is_agency_contractor",
    "is_minor": "variant_is_minor",
    "is_uae_resident": "variant_is_resident",
    "is_remote": "variant_is_remote",
}


class CaseStatusMappingMissing(UnsupportedFieldError):
    """No approved UC-01 -> onboarding_cases.status mapping (decision B5)."""


class SupabaseCaseRepository:
    """CaseRepository against onboarding_cases. NOT wired into the container.

    Column mapping is complete and tested; what is NOT decided is the status
    vocabulary. The live column defaults to INTAKE_PENDING and is shared with
    other use cases, while UC-01 has its own CaseStatus values, and any CHECK
    constraint is invisible through PostgREST (decision B5). So:

    * `status_mapping` (UC-01 CaseStatus -> live status text) must be supplied
      explicitly; there is no default. Without it every write raises
      CaseStatusMappingMissing, and a read refuses a status it cannot map back.
    * Placement columns are always sent explicitly - None as SQL NULL - so the
      live column defaults ("General", "Internal Operations", "") can never
      fill in a value nobody supplied. onboarding_attributes is always an array.
    * role_id / manager_id are not written (D1/D2: not required, nullable).
    * Rows soft-deleted by another use case (is_deleted) are not returned.

    Saving a case is not atomic with its form, approvals and audit entries
    over PostgREST (decision B6); that is why this stays unwired.
    """

    TABLE = "onboarding_cases"
    _COLS = (
        "case_id,client_id,employee_no,candidate_name,candidate_email,hr_officer_id,joining_date,"
        "work_location_type,location_name,project_name,grade,department,nationality_group,"
        + ",".join(VARIANT_COLUMNS.values())
        + ",status,current_stage,current_form_version,folder_ref,opened_at,onboarding_attributes,"
        "reactivates_record_id,reactivates_external_ref"
    )

    def __init__(self, client: PostgrestClient, *, status_mapping: Optional[Mapping[CaseStatus, str]] = None) -> None:
        self._c = client
        self._to_db: Optional[dict[CaseStatus, str]] = dict(status_mapping) if status_mapping else None
        self._from_db: Optional[dict[str, CaseStatus]] = None
        if self._to_db is not None:
            self._from_db = {v: k for k, v in self._to_db.items()}
            if len(self._from_db) != len(self._to_db):
                raise ValueError("status_mapping must be one-to-one so a status reads back unchanged")

    def _db_status(self, status: CaseStatus) -> str:
        if self._to_db is None:
            raise CaseStatusMappingMissing(
                "no approved mapping from UC-01 CaseStatus to onboarding_cases.status (decision B5); "
                "the case is not written")
        if status not in self._to_db:
            raise CaseStatusMappingMissing(f"status {status.value} has no approved onboarding_cases value")
        return self._to_db[status]

    def _model_status(self, value: str) -> CaseStatus:
        if self._from_db is None or value not in self._from_db:
            raise CaseStatusMappingMissing(
                "onboarding_cases.status value has no approved UC-01 mapping (decision B5)")
        return self._from_db[value]

    def to_row(self, case: Case) -> dict[str, Any]:
        row: dict[str, Any] = {
            "case_id": case.case_id,
            "client_id": case.client_id,
            "employee_no": case.employee_no,
            "candidate_name": case.candidate_name,
            "candidate_email": case.candidate_email,
            "hr_officer_id": case.hr_officer_id,
            "joining_date": case.joining_date.isoformat(),
            "work_location_type": str(case.work_location_type),
            "location_name": case.location_name,
            # Explicit None -> NULL, so no live default stands in for a value.
            "project_name": case.project_name,
            "grade": case.grade,
            "department": case.department,
            "nationality_group": case.nationality_group,
            "status": self._db_status(case.status),
            "current_stage": case.current_stage,
            "current_form_version": case.current_form_version,
            "folder_ref": case.folder_ref,
            "opened_at": _iso(case.opened_at),
            "onboarding_attributes": list(case.onboarding_attributes or []),
            "reactivates_record_id": case.reactivates_record_id,
            "reactivates_external_ref": case.reactivates_external_ref,
        }
        flags = case.variants.model_dump()
        row.update({column: bool(flags[field]) for field, column in VARIANT_COLUMNS.items()})
        return row

    def from_row(self, row: dict[str, Any]) -> Case:
        return Case(
            case_id=row["case_id"],
            client_id=row["client_id"],
            employee_no=row["employee_no"],
            candidate_name=row["candidate_name"],
            candidate_email=row["candidate_email"],
            hr_officer_id=row["hr_officer_id"],
            joining_date=date.fromisoformat(row["joining_date"]),
            work_location_type=row["work_location_type"],
            variants=VariantFlags(**{field: bool(row[column]) for field, column in VARIANT_COLUMNS.items()}),
            status=self._model_status(row["status"]),
            current_form_version=row["current_form_version"],
            opened_at=datetime.fromisoformat(row["opened_at"]),
            folder_ref=row["folder_ref"],
            reactivates_record_id=row["reactivates_record_id"],
            reactivates_external_ref=row["reactivates_external_ref"],
            location_name=row["location_name"],
            department=row["department"],
            grade=row["grade"],
            nationality_group=row["nationality_group"],
            project_name=row["project_name"],
            current_stage=row["current_stage"],
            onboarding_attributes=list(row["onboarding_attributes"] or []),
        )

    def save(self, case: Case) -> None:
        self._c.upsert(self.TABLE, [self.to_row(case)], on_conflict="case_id")

    def get(self, case_id: str) -> Optional[Case]:
        rows = self._c.select(self.TABLE, columns=self._COLS,
                              filters={"case_id": _eq(case_id), "is_deleted": _eq("false")}, limit=1)
        return self.from_row(rows[0]) if rows else None

    def list_for_client(self, client_id: str) -> list[Case]:
        rows = self._c.select(self.TABLE, columns=self._COLS,
                              filters={"client_id": _eq(client_id), "is_deleted": _eq("false")},
                              order="opened_at.asc")
        return [self.from_row(r) for r in rows]

    def case_ids_for_email(self, client_id: str, email: str) -> list[tuple[str, str]]:
        """(case_id, raw status) of this client's cases for this email. Only the
        two columns are read and nothing is mapped, so another use case's rows in
        the shared table (statuses outside UC-01's vocabulary) never raise here."""
        rows = self._c.select(self.TABLE, columns="case_id,status",
                              filters={"client_id": _eq(client_id), "candidate_email": _ilike_exact(email),
                                       "is_deleted": _eq("false")})
        return [(r["case_id"], r["status"]) for r in rows]


#: INTERIM (approved by the UC-01 owner 2026-11-04, pending DBA decision B5):
#: UC-01 writes its own CaseStatus names unchanged. If the live column has a
#: CHECK constraint that rejects them, the insert fails and the flow reports
#: PERSISTENCE_FAILED - nothing is coerced into another use case's vocabulary.
INTERIM_STATUS_MAPPING: dict[CaseStatus, str] = {status: status.value for status in CaseStatus}


# ---------------------------------------------------------------- audit


#: Marker on every UC-01 row of the shared audit_events table, so the UC-01
#: hash chain is computed over UC-01 rows only - never other use cases'.
AUDIT_ORIGIN = "uc01"


class AuditClientUnknown(UnsupportedFieldError):
    """audit_events.client_id is NOT NULL and no client could be resolved."""


class SupabaseAuditLog:
    """AuditLog against the shared audit_events table. INTERIM design
    (approved by the UC-01 owner 2026-11-04, pending DBA decisions B2/B6).

    * The same guard and hash chain as the in-memory log: check_entry refuses
      sensitive values; chain_entry stamps sequence/prev_hash/entry_hash over
      the UC-01 rows (detail.origin = "uc01") only.
    * client_id (NOT NULL live) is resolved, never invented: the entry's own
      client_id, else its case's client, else the request scope
      (audit_scope.audit_client_scope). Otherwise the write is refused.
    * Columns UC-01 has no value for are labelled, not guessed:
      actor_role = "uc01:<actor type>", outcome = "RECORDED".

    LIMITATION (why B2/B6 still need the DBA): reading the chain tail and
    inserting are two PostgREST calls, not one transaction. Two UC-01
    processes appending at the same moment could chain from the same tail;
    verify_chain would then report it. Acceptable for one developer process,
    not for production.
    """

    TABLE = "audit_events"
    _COLS = ("event_id,case_id,client_id,actor_type,actor_id,actor_role,action,outcome,detail,"
             "before_state,after_state,reference,occurred_at,sequence,prev_hash,entry_hash")
    _ORIGIN_FILTER = {"detail->>origin": "eq." + AUDIT_ORIGIN}

    def __init__(self, client: PostgrestClient, *, case_client: Callable[[str], Optional[str]]) -> None:
        self._c = client
        self._case_client = case_client

    def _client_for(self, entry: AuditEntry) -> str:
        client = entry.client_id
        if client is None and entry.case_id is not None:
            client = self._case_client(entry.case_id)
        if client is None:
            client = current_audit_client()
        if not client:
            raise AuditClientUnknown(
                f"audit entry {entry.action!r} has no resolvable client_id (no client on the entry, no case "
                "client, no request scope); audit_events requires one, and none is invented")
        return client

    def _tail(self) -> tuple[int, str]:
        rows = self._c.select(self.TABLE, columns="sequence,entry_hash", filters=dict(self._ORIGIN_FILTER),
                              order="sequence.desc", limit=1)
        if not rows:
            return 0, GENESIS_HASH
        return int(rows[0]["sequence"]) + 1, rows[0]["entry_hash"]

    @staticmethod
    def _to_row(entry: AuditEntry, client_id: str) -> dict[str, Any]:
        return {
            "event_id": entry.log_id,
            "case_id": entry.case_id,
            "client_id": client_id,
            "actor_type": str(entry.actor_type),
            "actor_id": entry.actor_id,
            "actor_role": AUDIT_ORIGIN + ":" + entry.actor_type.value.lower(),
            "action": entry.action,
            "outcome": "RECORDED",
            "detail": {"origin": AUDIT_ORIGIN, "request_id": entry.request_id},
            "before_state": entry.before,
            "after_state": entry.after,
            "reference": entry.reference,
            "occurred_at": entry.timestamp.isoformat(),
            "sequence": entry.sequence,
            "prev_hash": entry.prev_hash,
            "entry_hash": entry.entry_hash,
        }

    @staticmethod
    def _from_row(row: dict[str, Any]) -> AuditEntry:
        detail = row.get("detail") or {}
        return AuditEntry(
            log_id=row["event_id"], case_id=row["case_id"], request_id=detail.get("request_id"),
            actor_type=ActorType(row["actor_type"]), actor_id=row["actor_id"], action=row["action"],
            before=row["before_state"], after=row["after_state"],
            timestamp=datetime.fromisoformat(row["occurred_at"]), reference=row["reference"],
            client_id=row["client_id"], sequence=row["sequence"], prev_hash=row["prev_hash"],
            entry_hash=row["entry_hash"],
        )

    def append(self, entry: AuditEntry) -> None:
        check_entry(entry)  # fail closed on sensitive values, as in memory
        client_id = self._client_for(entry)
        sequence, prev_hash = self._tail()
        chained = chain_entry(entry, sequence, prev_hash)
        self._c.insert(self.TABLE, [self._to_row(chained, client_id)], returning=False)

    def entries(self) -> list[AuditEntry]:
        rows = self._c.select(self.TABLE, columns=self._COLS, filters=dict(self._ORIGIN_FILTER),
                              order="sequence.asc")
        return [self._from_row(r) for r in rows]

    def entries_for_case(self, case_id: str) -> list[AuditEntry]:
        rows = self._c.select(self.TABLE, columns=self._COLS,
                              filters={**self._ORIGIN_FILTER, "case_id": _eq(case_id)}, order="sequence.asc")
        return [self._from_row(r) for r in rows]


# ---------------------------------------------------------------- case folders


class SupabaseLogicalDocumentStore:
    """DocumentStore that records a LOGICAL folder reference only (interim,
    approved by the UC-01 owner 2026-11-04, pending decision B4).

    No bucket exists, so nothing is created or uploaded. The reference is
    built from validated, server-controlled ids and is stored in
    onboarding_cases.folder_ref by the case flow.
    """

    PREFIX = "logical://uc01/"

    def create_case_folder(self, client_id: str, case_id: str) -> str:
        from .storage_paths import _CLIENT_ID, UnsafeObjectPath, _uuid

        if not _CLIENT_ID.match(client_id or ""):
            raise UnsafeObjectPath("client_id contains characters not allowed in a folder reference")
        return f"{self.PREFIX}{client_id}/{_uuid(case_id, 'case_id')}"
