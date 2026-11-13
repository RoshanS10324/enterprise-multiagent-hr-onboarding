"""Supabase adapter tests.

Every test here runs against an in-process fake PostgREST served through
an httpx MockTransport. No socket is opened and the real project is
never contacted, so these are safe to run anywhere - the isolation the
brief requires between test adapters and the real database.

A separate, opt-in read-only check against the live project lives in
test_supabase_live.py.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sub_agent.adapters.supabase import (
    PostgrestClient,
    SchemaGapError,
    SupabaseDiscrepancyRepository,
    SupabaseEventPublisher,
    SupabaseFormRepository,
    SupabaseHolidayCalendar,
    SupabaseHrisRecordRepository,
    SupabaseOnboardingFormSource,
    SupabasePendingStartRepository,
    SupabasePersonDirectory,
    UniqueViolation,
    UnsupportedAuditLog,
    UnsupportedCaseRepository,
    UnsupportedEmployeeNumberGenerator,
    UnsupportedFieldError,
)
from sub_agent.api.container import build_container
from sub_agent.api.settings import Settings
from sub_agent.crypto import FieldCipher, FieldEncryptionError, generate_key_b64, load_key
from sub_agent.models import (
    CaseOpenedEvent,
    Discrepancy,
    DiscrepancyStatus,
    FormStatus,
    HrisRecord,
    HrisSystem,
    OnboardingForm,
    PendingStart,
    PersonRecord,
    PersonRecordKind,
    VariantFlags,
    WorkLocationType,
)

BASE = "https://example-project.supabase.co"


# --------------------------------------------------------------- fake DB


def _like_to_regex(pattern: str) -> str:
    r"""Postgres LIKE semantics: % = any run, _ = one char, \ escapes.

    The fake has to model the escape character, otherwise a test for
    "underscores are escaped" would pass even against an adapter that
    does not escape them.
    """
    out = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    return "".join(out)


class FakePostgrest:
    """Just enough PostgREST: eq/ilike filters, insert, upsert, patch.

    Not a Postgres emulator - it exists so the adapters' URL building,
    column mapping and encoding can be exercised round-trip.
    """

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.unique: dict[str, tuple[str, ...]] = {}
        # GENERATED ALWAYS columns: {table: {column: fn(row) -> value}}.
        # Like Postgres, a write that supplies one is rejected.
        self.generated: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []

    def _generated_violation(self, table: str, row: dict[str, Any], verb: str):
        for column in self.generated.get(table, {}):
            if column in row:
                message = (
                    f'cannot insert a non-DEFAULT value into column "{column}"'
                    if verb == "insert"
                    else f'column "{column}" can only be updated to DEFAULT'
                )
                return httpx.Response(
                    400, json={"code": "428C9", "message": message, "details": None}
                )
        return None

    # -- helpers
    def _rows(self, table: str) -> list[dict[str, Any]]:
        return self.tables.setdefault(table, [])

    @staticmethod
    def _matches(row: dict[str, Any], params: dict[str, str]) -> bool:
        for key, raw in params.items():
            if key in ("select", "order", "limit", "on_conflict"):
                continue
            op, _, wanted = raw.partition(".")
            actual = row.get(key)
            if op == "eq":
                if str(actual).lower() != wanted.lower():
                    return False
            elif op == "ilike":
                if actual is None or not re.match(
                    _like_to_regex(wanted), str(actual), re.IGNORECASE
                ):
                    return False
            else:  # pragma: no cover - unused operator
                raise AssertionError(f"fake does not implement operator {op!r}")
        return True

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        table = request.url.path.rsplit("/", 1)[-1]
        params = dict(request.url.params)

        if request.method == "GET":
            if table == "" or request.url.path.endswith("/rest/v1/"):
                return httpx.Response(200, json={"paths": {}})
            rows = [r for r in self._rows(table) if self._matches(r, params)]
            if order := params.get("order"):
                col, _, direction = order.partition(".")
                rows = sorted(rows, key=lambda r: r[col], reverse=direction == "desc")
            if limit := params.get("limit"):
                rows = rows[: int(limit)]
            return httpx.Response(200, json=rows)

        body = json.loads(request.content)
        prefer = request.headers.get("Prefer", "")

        if request.method == "POST":
            stored = []
            for row in body:
                if (rejected := self._generated_violation(table, row, "insert")) is not None:
                    return rejected
                row = dict(row)
                for column, derive in self.generated.get(table, {}).items():
                    row[column] = derive(row)
                key_cols = self.unique.get(table)
                existing = None
                if key_cols:
                    existing = next(
                        (
                            r
                            for r in self._rows(table)
                            if all(r.get(c) == row.get(c) for c in key_cols)
                        ),
                        None,
                    )
                if existing is not None:
                    if "merge-duplicates" in prefer:
                        existing.update(row)
                        stored.append(existing)
                        continue
                    if "ignore-duplicates" in prefer:
                        continue
                    return httpx.Response(
                        409,
                        json={"code": "23505", "message": "duplicate key", "details": "x"},
                    )
                self._rows(table).append(dict(row))
                stored.append(row)
            if "return=minimal" in prefer:
                return httpx.Response(201)
            return httpx.Response(201, json=stored)

        if request.method == "PATCH":
            if (rejected := self._generated_violation(table, body, "update")) is not None:
                return rejected
            touched = []
            for row in self._rows(table):
                if self._matches(row, params):
                    row.update(body)
                    touched.append(row)
            return httpx.Response(200, json=touched)

        raise AssertionError(f"unexpected method {request.method}")  # pragma: no cover


@pytest.fixture
def fake() -> FakePostgrest:
    db = FakePostgrest()
    db.unique["onboarding_forms"] = ("form_id",)
    db.unique["offer_forms"] = ("client_id", "offer_ref")
    db.unique["hris_sync_records"] = ("idempotency_key",)
    # Live (verified 2026-11-04): GENERATED ALWAYS AS
    # (case_id::text || ':' || target_system || ':' || form_version::text).
    db.generated["hris_sync_records"] = {
        "idempotency_key": lambda r: f"{r['case_id']}:{r['target_system']}:{r['form_version']}"
    }
    return db


@pytest.fixture
def client(fake: FakePostgrest) -> PostgrestClient:
    return PostgrestClient(
        BASE,
        SecretStr("test-service-key"),
        transport=httpx.MockTransport(fake.handler),
    )


@pytest.fixture
def cipher() -> FieldCipher:
    return FieldCipher(base64.b64decode(generate_key_b64()))


def _form(**over: Any) -> OnboardingForm:
    base: dict[str, Any] = dict(
        client_id="acme",
        case_id="11111111-1111-4111-8111-111111111111",
        version=1,
        candidate_name="Alex Doe",
        candidate_email="alex@example.com",
        designation="Engineer",
        joining_date=date(2026, 3, 1),
        work_location_type=WorkLocationType.OFFICE,
        salary_and_terms=SecretStr("AED 30,000/month + housing"),
    )
    base.update(over)
    return OnboardingForm(**base)


# ------------------------------------------------------------- crypto


def test_encrypt_decrypt_round_trips(cipher: FieldCipher):
    sealed = cipher.encrypt("AED 30,000", aad="form-1")
    assert cipher.decrypt(sealed, aad="form-1") == "AED 30,000"


def test_ciphertext_does_not_contain_the_plaintext(cipher: FieldCipher):
    sealed = cipher.encrypt("AED 30,000", aad="form-1")
    assert b"30,000" not in sealed
    assert b"AED" not in sealed


def test_same_plaintext_encrypts_differently_each_time(cipher: FieldCipher):
    # A deterministic ciphertext would leak that two forms share a salary.
    assert cipher.encrypt("same", aad="f") != cipher.encrypt("same", aad="f")


def test_ciphertext_bound_to_its_row_cannot_be_moved(cipher: FieldCipher):
    sealed = cipher.encrypt("AED 30,000", aad="form-1")
    with pytest.raises(FieldEncryptionError):
        cipher.decrypt(sealed, aad="form-2")


def test_tampered_ciphertext_is_rejected(cipher: FieldCipher):
    sealed = bytearray(cipher.encrypt("AED 30,000", aad="form-1"))
    sealed[-1] ^= 0x01
    with pytest.raises(FieldEncryptionError):
        cipher.decrypt(bytes(sealed), aad="form-1")


def test_wrong_key_is_rejected(cipher: FieldCipher):
    sealed = cipher.encrypt("AED 30,000", aad="form-1")
    other = FieldCipher(secrets.token_bytes(32))
    with pytest.raises(FieldEncryptionError):
        other.decrypt(sealed, aad="form-1")


@pytest.mark.parametrize("bad", ["", "   ", "not-base64!!", base64.b64encode(b"short").decode()])
def test_load_key_rejects_bad_keys(bad: str):
    with pytest.raises(FieldEncryptionError):
        load_key(bad)


def test_key_errors_never_echo_the_key_value():
    secret = base64.b64encode(b"x" * 16).decode()  # right format, wrong length
    with pytest.raises(FieldEncryptionError) as err:
        load_key(secret)
    assert secret not in str(err.value)


# -------------------------------------------------------- form repository


def test_form_round_trips_through_the_database(client, cipher, fake):
    repo = SupabaseFormRepository(client, cipher)
    form = _form()
    repo.save(form)

    loaded = repo.get(form.case_id, 1)
    assert loaded is not None
    assert loaded.form_id == form.form_id
    assert loaded.candidate_name == "Alex Doe"
    assert loaded.joining_date == date(2026, 3, 1)
    assert loaded.work_location_type is WorkLocationType.OFFICE
    # The secret survives the round trip but is still a SecretStr.
    assert loaded.salary_and_terms.get_secret_value() == "AED 30,000/month + housing"
    assert "30,000" not in repr(loaded)


def test_salary_is_stored_encrypted_not_in_clear(client, cipher, fake):
    SupabaseFormRepository(client, cipher).save(_form())
    stored = fake.tables["onboarding_forms"][0]["salary_and_terms_encrypted"]
    assert stored.startswith("\\x")
    assert "30,000" not in stored
    assert "AED" not in bytes.fromhex(stored[2:]).decode("latin-1")


def test_approved_at_keeps_its_timezone(client, cipher):
    repo = SupabaseFormRepository(client, cipher)
    when = datetime(2026, 2, 1, 9, 30, tzinfo=timezone.utc)
    form = _form(status=FormStatus.APPROVED, approved_by="hr-1", approved_at=when)
    repo.save(form)
    loaded = repo.get(form.case_id, 1)
    assert loaded.approved_at == when
    assert loaded.approved_at.tzinfo is not None


def test_latest_approved_picks_the_highest_version(client, cipher):
    repo = SupabaseFormRepository(client, cipher)
    when = datetime(2026, 2, 1, tzinfo=timezone.utc)
    for v in (1, 2):
        repo.save(
            _form(version=v, status=FormStatus.APPROVED, approved_by="hr", approved_at=when)
        )
    assert repo.latest_approved("11111111-1111-4111-8111-111111111111").version == 2


def test_latest_approved_ignores_non_approved(client, cipher):
    repo = SupabaseFormRepository(client, cipher)
    repo.save(_form(status=FormStatus.DRAFT))
    assert repo.latest_approved("11111111-1111-4111-8111-111111111111") is None


def test_save_is_idempotent_on_form_id(client, cipher, fake):
    repo = SupabaseFormRepository(client, cipher)
    form = _form()
    repo.save(form)
    repo.save(form.with_case(form.case_id))
    assert len(fake.tables["onboarding_forms"]) == 1


def test_data_is_visible_to_a_brand_new_repository(client, cipher):
    """Standing in for 'survives a restart': a second repository object,
    sharing no Python state, reads back what the first one wrote."""
    first = SupabaseFormRepository(client, cipher)
    form = _form()
    first.save(form)

    second = SupabaseFormRepository(client, cipher)
    assert second.get(form.case_id, 1).form_id == form.form_id


def test_a_different_encryption_key_cannot_read_the_salary(client, fake):
    SupabaseFormRepository(client, FieldCipher(secrets.token_bytes(32))).save(_form())
    other = SupabaseFormRepository(client, FieldCipher(secrets.token_bytes(32)))
    with pytest.raises(FieldEncryptionError):
        other.get("11111111-1111-4111-8111-111111111111", 1)


# ----------------------------------------------------- person directory


def test_person_round_trip_and_case_insensitive_email(client):
    repo = SupabasePersonDirectory(client)
    repo.add(
        PersonRecord(
            client_id="acme",
            email="Alex@Example.com",
            full_name="Alex Doe",
            kind=PersonRecordKind.OPEN_CASE,
        )
    )
    assert len(repo.find_by_email("acme", "alex@example.com")) == 1
    assert len(repo.find_by_name("acme", "ALEX DOE")) == 1


def test_person_lookup_is_scoped_to_the_client(client):
    repo = SupabasePersonDirectory(client)
    repo.add(
        PersonRecord(
            client_id="acme", email="a@x.com", full_name="A", kind=PersonRecordKind.OPEN_CASE
        )
    )
    assert repo.find_by_email("other-tenant", "a@x.com") == []


@pytest.mark.parametrize(
    "stored,looked_up",
    [
        ("johnXdoe@example.com", "john_doe@example.com"),  # _ is an ilike wildcard
        ("aNYTHINGb@example.com", "a%b@example.com"),      # % is an ilike wildcard
    ],
)
def test_email_lookup_is_exact_not_a_wildcard_pattern(client, stored, looked_up):
    """PostgREST ilike treats _ and % as wildcards. Underscores are
    ordinary in email addresses, so an unescaped lookup would match a
    DIFFERENT person and duplicate detection could link a new candidate
    to an unrelated open case."""
    repo = SupabasePersonDirectory(client)
    repo.add(
        PersonRecord(
            client_id="acme",
            email=stored,
            full_name="Other Person",
            kind=PersonRecordKind.OPEN_CASE,
        )
    )
    assert repo.find_by_email("acme", looked_up) == []


def test_email_lookup_still_matches_the_same_address_case_insensitively(client):
    repo = SupabasePersonDirectory(client)
    repo.add(
        PersonRecord(
            client_id="acme",
            email="john_doe@example.com",
            full_name="John Doe",
            kind=PersonRecordKind.OPEN_CASE,
        )
    )
    assert len(repo.find_by_email("acme", "JOHN_DOE@example.com")) == 1


# person_records.hris_external_ref (text, nullable) exists live (verified
# read-only 2026-11-04). The old refusal guarded against the link being
# silently DROPPED; storing it - and reading it back - is the stronger form
# of that guarantee.


def test_person_hris_ref_is_written_and_read_back(client, fake):
    repo = SupabasePersonDirectory(client)
    record = PersonRecord(client_id="acme", email="a@x.com", full_name="A",
                          kind=PersonRecordKind.PAST_EMPLOYEE, hris_external_ref="ZP-55")
    repo.add(record)
    assert fake.tables["person_records"][0]["hris_external_ref"] == "ZP-55"
    (found,) = repo.find_by_email("acme", "a@x.com")
    assert found.hris_external_ref == "ZP-55" and found.record_id == record.record_id


def test_person_without_hris_ref_stores_an_explicit_null(client, fake):
    repo = SupabasePersonDirectory(client)
    repo.add(PersonRecord(client_id="acme", email="b@x.com", full_name="B", kind=PersonRecordKind.OPEN_CASE))
    row = fake.tables["person_records"][0]
    assert "hris_external_ref" in row and row["hris_external_ref"] is None  # NULL, not omitted
    (found,) = repo.find_by_email("acme", "b@x.com")
    assert found.hris_external_ref is None


def test_person_read_tolerates_a_row_without_the_column(client, fake):
    """Rows written before the column existed come back as None, not an error."""
    fake.tables["person_records"] = [{"record_id": "r1", "client_id": "acme", "email": "c@x.com",
                                      "id_number": None, "full_name": "C", "kind": "PAST_EMPLOYEE",
                                      "case_id": None}]
    (found,) = SupabasePersonDirectory(client).find_by_email("acme", "c@x.com")
    assert found.hris_external_ref is None


# --------------------------------------------------------- discrepancies


def test_discrepancy_maps_field_to_field_name(client, fake):
    repo = SupabaseDiscrepancyRepository(client)
    repo.add(
        Discrepancy(
            case_id="c1",
            field="candidate_name",
            value_form="A",
            value_other="B",
            other_source="offer_letter",
            form_version=1,
            other_fingerprint="fp1",
        )
    )
    row = fake.tables["intake_discrepancies"][0]
    assert row["field_name"] == "candidate_name"
    assert "field" not in row
    assert repo.list_for_case("c1")[0].field == "candidate_name"


def test_discrepancy_without_fingerprint_is_refused(client):
    with pytest.raises(UnsupportedFieldError, match="other_fingerprint"):
        SupabaseDiscrepancyRepository(client).add(
            Discrepancy(
                case_id="c1",
                field="designation",
                value_form="A",
                value_other="B",
                other_source="s",
                form_version=1,
            )
        )


def test_discrepancy_update_persists_the_decision(client):
    repo = SupabaseDiscrepancyRepository(client)
    d = Discrepancy(
        case_id="c1",
        field="designation",
        value_form="A",
        value_other="B",
        other_source="s",
        form_version=1,
        other_fingerprint="fp",
    )
    repo.add(d)
    d.status = DiscrepancyStatus.RESOLVED
    repo.update(d)
    assert repo.get(d.discrepancy_id).status is DiscrepancyStatus.RESOLVED


# ---------------------------------------------------------- hris records


def _hris(**over: Any) -> HrisRecord:
    base: dict[str, Any] = dict(
        case_id="11111111-1111-4111-8111-111111111111",
        client_id="acme",
        system=HrisSystem.ZOHO_PEOPLE,
        form_version=1,
    )
    base.update(over)
    return HrisRecord(**base)


def test_hris_record_maps_system_to_target_system(client, fake):
    SupabaseHrisRecordRepository(client).add(_hris())
    assert fake.tables["hris_sync_records"][0]["target_system"] == "ZOHO_PEOPLE"


def _hris_bodies(fake: FakePostgrest, method: str) -> list[Any]:
    return [
        json.loads(r.content)
        for r in fake.requests
        if r.method == method and r.url.path.endswith("/hris_sync_records")
    ]


def test_hris_insert_lets_the_database_generate_idempotency_key(client, fake):
    """Live idempotency_key is GENERATED ALWAYS: Postgres rejects any
    insert that supplies it (SQLSTATE 428C9), so the adapter must not."""
    record = _hris()
    SupabaseHrisRecordRepository(client).add(record)
    (body,) = _hris_bodies(fake, "POST")
    assert all("idempotency_key" not in row for row in body)
    # The database's value equals the model's derived key, byte for byte.
    assert fake.tables["hris_sync_records"][0]["idempotency_key"] == record.idempotency_key


def test_hris_update_never_sends_idempotency_key(client, fake):
    """A PATCH that includes a generated column is rejected too."""
    repo = SupabaseHrisRecordRepository(client)
    record = _hris()
    repo.add(record)
    record.external_ref = "ZP-123"
    repo.update(record)
    (body,) = _hris_bodies(fake, "PATCH")
    assert "idempotency_key" not in body
    stored = repo.get(record.record_id)
    assert stored.external_ref == "ZP-123"
    assert stored.idempotency_key == record.idempotency_key


def test_hris_duplicate_idempotency_key_raises_unique_violation(client):
    repo = SupabaseHrisRecordRepository(client)
    repo.add(_hris())
    with pytest.raises(UniqueViolation):
        repo.add(_hris())


# hris_sync_records.updates_external_ref (text, nullable) exists live
# (verified read-only 2026-11-04): it is what makes an HRIS update target the
# existing employee, so it is stored and read back rather than refused.


def test_hris_updates_external_ref_is_written_and_read_back(client, fake):
    repo = SupabaseHrisRecordRepository(client)
    record = _hris(updates_external_ref="ZP-1")
    repo.add(record)
    assert fake.tables["hris_sync_records"][0]["updates_external_ref"] == "ZP-1"
    assert repo.get(record.record_id).updates_external_ref == "ZP-1"


def test_hris_without_updates_external_ref_stores_an_explicit_null(client, fake):
    repo = SupabaseHrisRecordRepository(client)
    record = _hris()
    repo.add(record)
    row = fake.tables["hris_sync_records"][0]
    assert "updates_external_ref" in row and row["updates_external_ref"] is None
    assert repo.get(record.record_id).updates_external_ref is None


def test_hris_update_keeps_the_external_ref(client, fake):
    repo = SupabaseHrisRecordRepository(client)
    record = _hris(updates_external_ref="ZP-1")
    repo.add(record)
    repo.update(record.model_copy(update={"last_error": "retry"}))
    assert repo.get(record.record_id).updates_external_ref == "ZP-1"


def test_hris_comparison_result_is_still_refused(client):
    """No live column: refused, never dropped."""
    with pytest.raises(UnsupportedFieldError, match="comparison_result"):
        SupabaseHrisRecordRepository(client).add(_hris(comparison_result={"x": 1}))


# ------------------------------------------------------- outbox / misc


def test_event_publisher_writes_type_and_payload(client, fake):
    SupabaseEventPublisher(client).publish(
        CaseOpenedEvent(
            case_id="c1",
            client_id="acme",
            form_version=1,
            joining_date=date(2026, 3, 1),
            work_location_type=WorkLocationType.OFFICE,
            variants=VariantFlags(),
        )
    )
    row = fake.tables["domain_events_outbox"][0]
    assert row["event_type"] == "CaseOpenedEvent"
    assert row["client_id"] == "acme"
    assert row["payload"]["form_version"] == 1
    assert "client_id" not in row["payload"]


def test_holiday_calendar_only_counts_observed_days(client, fake):
    fake.tables["client_holidays"] = [
        {"client_id": "acme", "holiday_date": "2026-12-02", "is_observed": True},
        {"client_id": "acme", "holiday_date": "2026-12-03", "is_observed": False},
    ]
    cal = SupabaseHolidayCalendar(client)
    assert cal.is_holiday("acme", date(2026, 12, 2)) is True
    assert cal.is_holiday("acme", date(2026, 12, 3)) is False


def test_pending_start_waiting_lookup_and_update(client):
    repo = SupabasePendingStartRepository(client)
    p = PendingStart(client_id="acme", offer_ref="OFF-1", reasons=["no_form"])
    repo.add(p)
    assert repo.get_waiting("acme", "OFF-1").pending_id == p.pending_id
    p.status = "STARTED"
    repo.update(p)
    assert repo.get_waiting("acme", "OFF-1") is None


def test_form_source_links_offer_to_form(client, cipher):
    forms = SupabaseFormRepository(client, cipher)
    source = SupabaseOnboardingFormSource(client, forms)
    form = _form()
    source.save_for_offer("acme", "OFF-1", form)
    assert source.latest_for_offer("acme", "OFF-1").form_id == form.form_id
    assert source.latest_for_offer("acme", "OFF-404") is None


# -------------------------------------------------- unsupported adapters


def test_case_repository_refuses_with_an_actionable_message():
    with pytest.raises(SchemaGapError) as err:
        UnsupportedCaseRepository().save(object())  # type: ignore[arg-type]
    message = str(err.value)
    # D1/D2 are decided: role_id and manager_id no longer block UC-01.
    assert "NO LONGER" in message
    # ...but D3 still does, and the message must say which columns.
    for column in [
        "department",
        "grade",
        "nationality_group",
        "location_name",
        "project_name",
    ]:
        assert column in message, f"D3 blocker {column} not named"
    assert "D3" in message


def test_case_refusal_does_not_claim_d1_d2_unblocked_creation():
    """Resolving D1/D2 removed two requirements but did NOT make case
    creation possible - five other NOT NULL columns remain. The message
    must not let a reader conclude otherwise."""
    message = str(
        pytest.raises(SchemaGapError, UnsupportedCaseRepository().save, object()).value  # type: ignore[arg-type]
    )
    assert "STILL BLOCKING" in message


def test_audit_log_refuses_because_the_hash_chain_cannot_be_stored():
    with pytest.raises(SchemaGapError) as err:
        UnsupportedAuditLog().append(object())  # type: ignore[arg-type]
    for column in ("sequence", "prev_hash", "entry_hash"):
        assert column in str(err.value)


def test_employee_number_generator_refuses_rather_than_racing():
    with pytest.raises(SchemaGapError, match="SAME employee number"):
        UnsupportedEmployeeNumberGenerator().next("acme")


def test_every_unsupported_method_refuses_and_never_returns_quietly():
    """A gap adapter whose method returned None instead of raising would
    look like a successful no-op to the caller - cases silently not
    saved, audit entries silently dropped. Every public method must
    raise, so this sweeps all of them rather than spot-checking."""
    import inspect

    from sub_agent.adapters.supabase import unsupported as mod

    adapters = [
        getattr(mod, name)()
        for name in mod.__all__
        if name.startswith("Unsupported")
    ]
    assert adapters, "no unsupported adapters found"

    checked = 0
    for adapter in adapters:
        for name, method in inspect.getmembers(adapter, inspect.ismethod):
            if name.startswith("_"):
                continue
            args = [None] * (len(inspect.signature(method).parameters))
            with pytest.raises(SchemaGapError):
                method(*args)
            checked += 1
    # 20 = Case 3 + Audit 3 + Approval 5 + Offer 3 + EmployeeNumber 1 +
    # DocumentStore 1 + FormDetails 4. Pinned so deleting a port method is noticed.
    assert checked == 20, f"expected to sweep every gap method, swept {checked}"


# ------------------------------------------------------------ container


def _supabase_settings(**over: Any) -> Settings:
    base: dict[str, Any] = dict(
        app_env="dev",
        supabase_url=BASE,
        supabase_service_role_key=SecretStr("k"),
        field_encryption_key=SecretStr(generate_key_b64()),
    )
    base.update(over)
    return Settings(**base)


def test_dev_without_supabase_url_uses_in_memory():
    container = build_container(Settings(app_env="dev"))
    assert container.postgrest is None
    assert type(container.deps.case_repo).__name__ == "InMemoryCaseRepository"


def test_non_dev_without_supabase_url_refuses_to_start():
    with pytest.raises(RuntimeError, match="in-memory adapters outside dev"):
        build_container(Settings(app_env="prod", dedup_hmac_key="x", system_api_key="y"))


def test_supabase_settings_wire_the_durable_adapters(client):
    container = build_container(_supabase_settings(), postgrest=client)
    assert type(container.deps.form_repo).__name__ == "SupabaseFormRepository"
    assert type(container.deps.discrepancy_repo).__name__ == "SupabaseDiscrepancyRepository"
    # and the gaps stay loudly unsupported rather than silently in-memory
    assert type(container.deps.case_repo).__name__ == "UnsupportedCaseRepository"
    assert type(container.deps.audit_log).__name__ == "UnsupportedAuditLog"


def test_supabase_url_without_encryption_key_is_rejected_before_any_connection():
    with pytest.raises(ValueError, match="UC01_FIELD_ENCRYPTION_KEY"):
        Settings(app_env="dev", supabase_url=BASE, supabase_service_role_key=SecretStr("k"))


def test_supabase_url_without_service_role_key_is_rejected():
    with pytest.raises(ValueError, match="SUPABASE_SERVICE_ROLE_KEY"):
        Settings(
            app_env="dev",
            supabase_url=BASE,
            field_encryption_key=SecretStr(generate_key_b64()),
        )


def test_supabase_url_must_be_https():
    with pytest.raises(ValueError, match="https"):
        _supabase_settings(supabase_url="http://insecure.example.com")


def test_deployment_injected_env_vars_beat_the_root_dotenv_file():
    """A checked-out .env must never redirect a deployed process.

    Run in a subprocess because the "was this set before import?"
    snapshot is taken when settings.py is first imported.
    """
    import os
    import subprocess
    import sys

    env = dict(os.environ, SUPABASE_SCHEMA="injected_by_deployment")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from sub_agent.api.settings import Settings;"
            "print(Settings.from_env().supabase_schema)",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout.strip() == "injected_by_deployment"


def test_settings_never_render_their_secrets():
    rendered = repr(_supabase_settings(supabase_service_role_key=SecretStr("super-secret")))
    assert "super-secret" not in rendered


def test_startup_fails_when_the_database_is_unreachable():
    """No silent fallback: an unreachable database stops the app."""
    from fastapi.testclient import TestClient

    from sub_agent.api.app import create_app

    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    dead = PostgrestClient(
        BASE, SecretStr("k"), transport=httpx.MockTransport(refuse)
    )
    app = create_app(build_container(_supabase_settings(), postgrest=dead))
    with pytest.raises(httpx.ConnectError):
        with TestClient(app):
            pass  # pragma: no cover - startup raises before the body runs


def test_service_role_key_is_sent_as_a_header_not_a_query_parameter(fake):
    c = PostgrestClient(
        BASE, SecretStr("tok"), transport=httpx.MockTransport(fake.handler)
    )
    c.select("person_records", columns="record_id")
    request = fake.requests[-1]
    assert "tok" not in str(request.url)
    assert request.headers["apikey"] == "tok"
