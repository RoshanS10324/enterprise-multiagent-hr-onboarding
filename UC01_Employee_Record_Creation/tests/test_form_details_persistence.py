"""Persistence of the document's details and budget, the source file in
Storage, and the full intake flow with read-back verification.

MOCKED, NOT LIVE: the "Supabase" here is FakePostgrest (an in-process
PostgREST stand-in) and an httpx MockTransport standing in for Storage. These
tests prove mapping, encryption, idempotency, ordering and failure handling;
they are not evidence that anything was written to the real project. The
live run is scripts/uc01_document_live_run.py.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from sub_agent.adapters.supabase.client import PostgrestClient, StorageError, SupabaseStorage
from sub_agent.adapters.supabase.storage_paths import UnsafeObjectPath, intake_object_path, source_document_id
from sub_agent.api.container import build_container
from sub_agent.api.settings import Settings
from sub_agent.crypto import FieldCipher, FieldEncryptionError, generate_key_b64, load_key
from sub_agent.documents.budget import BudgetStatus
from sub_agent.documents.persistence import (
    DataOrigin,
    FormDetailsConflict,
    InMemoryFormDetailsRepository,
    build_form_details,
    verify_saved,
)
from sub_agent.documents.service import DocumentIntakeContext, DocumentIntakeService, DocumentIntakeStatus
from sub_agent.documents.supabase_store import (
    TABLES,
    FormDetailsStoreUnavailable,
    InMemorySourceDocumentStore,
    SupabaseFormDetailsRepository,
    SupabaseSourceDocumentStore,
)
from test_supabase_adapters import BASE, FakePostgrest

ROOT = Path(__file__).resolve().parent.parent
POPULATED = ROOT / "data" / "mock" / "documents" / "SYNTHETIC_onboarding_form_2026_populated.docx"
TODAY = date(2026, 11, 5)
CLIENT = "SYN-UAE-TEST"
SALARY = "SYNTHETIC: AED 22,000 per month (all inclusive)"

_spec = importlib.util.spec_from_file_location("synthetic_form_p", ROOT / "scripts" / "synthetic_onboarding_form.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)
_spec2 = importlib.util.spec_from_file_location("mock_docs_p", ROOT / "scripts" / "mock_onboarding_documents.py")
mock_docs = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(mock_docs)


def _doc(**changes) -> bytes:
    if not changes:
        return POPULATED.read_bytes()
    values = {k: list(v) for k, v in gen.VALUES.items()}
    values.update(changes)
    blank = mock_docs.build_docx({}, "Roles and Responsibilities (eg given below)", ["Example duty."])
    return gen.populate_docx(blank, values=values)


def _ctx(**over) -> DocumentIntakeContext:
    base = dict(request_id="REQ-DOC-1", client_id=CLIENT, hr_officer_id="SYN-HR-OFFICER-31",
                candidate_email="omar.synthetic@example.invalid", offer_accepted=True, work_location_type="OFFICE",
                placement={"location_name": "SYN Dubai Office"}, form_status="APPROVED",
                approved_by="SYN-HR-MANAGER-31", approved_at=datetime(2026, 11, 1, 9, tzinfo=timezone.utc))
    base.update(over)
    return DocumentIntakeContext(**base)


def _memory(details=None, source_store=None):
    import os

    os.environ["SUPABASE_URL"] = ""
    container = build_container(Settings(app_env="dev", dedup_hmac_key="t", system_api_key="t"))
    details = details if details is not None else container.form_details
    service = DocumentIntakeService(container.onboarding_deps, details_repo=details, source_store=source_store,
                                    today=lambda: TODAY)
    return service, container, details


def _cipher() -> FieldCipher:
    return FieldCipher(load_key(generate_key_b64()), key_id="dev-test-1")


class DetailsFake(FakePostgrest):
    """FakePostgrest with the proposed tables' natural keys."""

    def __init__(self, missing: tuple[str, ...] = (), refuse: dict[str, str] | None = None) -> None:
        super().__init__()
        self.unique.update({
            "uc01_onboarding_form_details": ("form_id",), "uc01_onboarding_form_field_status": ("form_id", "field_key"),
            "uc01_onboarding_document_checklist": ("form_id", "item"), "uc01_onboarding_budget": ("form_id",),
            "uc01_onboarding_budget_lines": ("form_id", "line_key")})
        self.missing, self.refuse = set(missing), dict(refuse or {})

    def handler(self, request):
        table = request.url.path.rsplit("/", 1)[-1]
        if table in self.missing:  # what PostgREST answers for a table that does not exist
            self.requests.append(request)
            return httpx.Response(404, json={"code": "PGRST205", "message": "not found"})
        if request.method == "POST" and table in self.refuse:
            self.requests.append(request)
            return httpx.Response(400, json={"code": self.refuse[table], "message": "rejected", "details": "x"})
        return super().handler(request)


def _rest(fake) -> PostgrestClient:
    return PostgrestClient(BASE, SecretStr("k"), transport=httpx.MockTransport(fake.handler))


def _extraction(data: bytes | None = None):
    service, _, _ = _memory()
    return service.extract(data or _doc(), "SYNTHETIC.docx")


def _record(**over):
    x = _extraction()
    args = dict(form_id="11111111-1111-4111-8111-111111111111", case_id="22222222-2222-4222-8222-222222222222",
                client_id=CLIENT, review_confirmed_by=None, review_confirmed_at=None, source_document=None)
    args.update(over)
    return build_form_details(x, **args)


# ------------------------------------------------------------------ record building


def test_the_record_maps_every_section_and_keeps_secrets_secret():
    r = _record()
    assert r.data_origin == DataOrigin.SYNTHETIC and r.template_id == "onboarding-form-2026"
    assert (r.reporting_to, r.probation_months, r.visa_duration_years, r.visa_location_category) == (
        "SYN Head of Data Governance", 6, 2, "OUTSIDE_COUNTRY")
    assert r.nationality == "Indian" and r.employment_duration.startswith("Permanent")
    assert r.marital_status == "MARRIED" and len(r.responsibilities) == 17
    personal = json.loads(r.personal_details.get_secret_value())
    assert personal == {"father_name": "SYNTHETIC-FATHER-NAME-31", "mother_name": "SYNTHETIC-MOTHER-NAME-31",
                        "mobile_number": "SYNTHETIC-MOBILE-31"}
    assert "SYNTHETIC-MOBILE-31" not in repr(r) and SALARY not in r.model_dump_json()
    assert {c.item: c.status for c in r.checklist}["emirates_id"] == "PENDING"
    assert r.budget.budget_total == Decimal("21500.00")
    keys = {f.field_key for f in r.field_status}
    assert "employee.final_salary" in keys and "supplementary.employee_email" in keys


# ------------------------------------------------------------------ in-memory repository


def test_in_memory_saves_are_idempotent_and_a_different_record_is_refused():
    repo, r = InMemoryFormDetailsRepository(), _record()
    repo.save(r)
    repo.save(r)  # retry: no-op
    assert verify_saved(r, repo.get(r.form_id)) == []
    changed = r.model_copy(update={"reporting_to": "Someone else"})
    with pytest.raises(FormDetailsConflict):
        repo.save(changed)
    assert repo.get(r.form_id).reporting_to == "SYN Head of Data Governance"
    assert repo.find_by_document(CLIENT, r.source_document_sha256) == [r.form_id]


# ------------------------------------------------------------------ Supabase repository (FAKE PostgREST)


def test_supabase_round_trip_reads_back_exactly_what_was_written():
    fake, cipher = DetailsFake(), _cipher()
    repo, r = SupabaseFormDetailsRepository(_rest(fake), cipher), _record()
    repo.save(r)
    assert verify_saved(r, repo.get(r.form_id)) == []
    row = fake.tables["uc01_onboarding_form_details"][0]
    assert "SYNTHETIC-MOBILE-31" not in json.dumps(row)  # personal details only as ciphertext
    budget = fake.tables["uc01_onboarding_budget"][0]
    assert budget["total_onboarding_budget"] == "21500.00" and budget["budget_currency"] == "AED"
    assert (budget["visa_budget_amount"], budget["insurance_budget_amount"], budget["travel_budget_amount"],
            budget["accommodation_budget_amount"], budget["other_expenses_amount"]) == (
        "4800.00", "8000.00", "3550.00", "3150.00", "2000.00")
    assert budget["budget_status"] == "COMPLETE" and len(budget["reference_rates"]) == 12
    lines = fake.tables["uc01_onboarding_budget_lines"]
    assert len(lines) == len(r.budget.lines) and all(isinstance(x["amount"], (str, type(None))) for x in lines)
    assert not any(SALARY in json.dumps(t) for t in fake.tables.values())


def test_supabase_save_is_idempotent_and_refuses_changed_financial_data():
    fake = DetailsFake()
    repo, r = SupabaseFormDetailsRepository(_rest(fake), _cipher()), _record()
    repo.save(r)
    counts = {t: len(fake.tables.get(t, [])) for t in TABLES}
    repo.save(r)
    assert {t: len(fake.tables.get(t, [])) for t in TABLES} == counts  # no duplicate rows
    with pytest.raises(FormDetailsConflict):
        repo.save(r.model_copy(update={"budget": r.budget.model_copy(update={"budget_total": Decimal("1.00")})}))


def test_personal_details_cannot_be_read_with_another_key():
    fake = DetailsFake()
    SupabaseFormDetailsRepository(_rest(fake), _cipher()).save(_record())
    with pytest.raises(FieldEncryptionError):
        SupabaseFormDetailsRepository(_rest(fake), _cipher()).get("11111111-1111-4111-8111-111111111111")


def test_a_missing_table_is_reported_before_anything_is_written():
    fake = DetailsFake(missing=("uc01_onboarding_budget",))
    repo = SupabaseFormDetailsRepository(_rest(fake), _cipher())
    with pytest.raises(FormDetailsStoreUnavailable):
        repo.ensure_ready()
    assert not any(r.method == "POST" for r in fake.requests)


# ------------------------------------------------------------------ Storage (FAKE transport)


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads = 0
        self.tamper = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/storage/v1", 1)[1]
        if path.startswith("/object/list/"):
            body = json.loads(request.content)
            names = [k.rsplit("/", 1)[1] for k in self.objects if k.rsplit("/", 1)[0].endswith(body["prefix"])]
            return httpx.Response(200, json=[{"name": n, "id": "x"} for n in names if n == body["search"]])
        if request.method == "POST" and path.startswith("/object/"):
            key = path[len("/object/"):]
            if key in self.objects:
                return httpx.Response(400, json={"error": "Duplicate"})
            self.objects[key] = request.content
            self.uploads += 1
            return httpx.Response(200, json={"Key": key})
        if request.method == "GET" and path.startswith("/object/authenticated/"):
            key = path[len("/object/authenticated/"):]
            if key not in self.objects:
                return httpx.Response(404)
            return httpx.Response(200, content=self.objects[key] + (b"x" if self.tamper else b""))
        return httpx.Response(405)


def _storage(fake: FakeStorage) -> SupabaseSourceDocumentStore:
    return SupabaseSourceDocumentStore(SupabaseStorage(BASE, SecretStr("k"), transport=httpx.MockTransport(
        fake.handler)), "Doc_Collection")


def test_storage_is_content_addressed_idempotent_and_verified():
    fake, data = FakeStorage(), _doc()
    store = _storage(fake)
    ref, already = store.store(CLIENT, data, "docx", "application/x")
    assert not already and fake.uploads == 1
    assert ref.object_path == f"{CLIENT}/uc01-intake/{ref.document_id}.docx" and "Omar" not in ref.object_path
    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    again, already = store.store(CLIENT, data, "docx", "application/x")
    assert already and again == ref and fake.uploads == 1  # duplicate upload avoided
    fake.tamper = True
    with pytest.raises(StorageError):
        store.store(CLIENT, data, "docx", "application/x")


def test_object_paths_never_carry_unsafe_text():
    with pytest.raises(UnsafeObjectPath):
        intake_object_path("../x", source_document_id(CLIENT, "a" * 64), "docx")
    with pytest.raises(UnsafeObjectPath):
        intake_object_path(CLIENT, "not-a-uuid", "docx")
    with pytest.raises(UnsafeObjectPath):
        intake_object_path(CLIENT, source_document_id(CLIENT, "a" * 64), "exe")
    assert source_document_id(CLIENT, "a" * 64) == source_document_id(CLIENT, "a" * 64)
    assert source_document_id(CLIENT, "a" * 64) != source_document_id("OTHER", "a" * 64)


# ------------------------------------------------------------------ the full flow (in-memory)


def test_a_populated_form_becomes_a_verified_employee_record_with_its_budget():
    store = InMemorySourceDocumentStore("Doc_Collection")
    service, container, details = _memory(source_store=store)
    result = service.process(_doc(), "SYNTHETIC_onboarding.docx", _ctx())
    assert result.status == DocumentIntakeStatus.CREATED, (result.errors, result.review_reasons)
    assert result.persistence_verified and result.readback == {
        "form_details": True, "budget": True, "source_document": True, "employee_record": True}
    assert result.source_document_stored and result.source_document.bucket == "Doc_Collection"
    case = container.onboarding_deps.case_repo.get(result.case_id)
    assert case.department == "Data & Analytics" and case.grade == "L5"  # completed from the document
    form = container.onboarding_deps.form_repo.get(result.case_id, 1)
    assert form.salary_and_terms.get_secret_value() == SALARY and form.form_id == result.form_id
    saved = details.get(result.form_id)
    assert saved.budget.status == BudgetStatus.COMPLETE and saved.budget.budget_total == Decimal("21500.00")
    assert saved.source_document == result.source_document
    actions = [e.action for e in container.onboarding_deps.audit_log.entries()]
    for action in ("DOCUMENT_RECEIVED", "DOCUMENT_STORED", "DOCUMENT_EXTRACTED", "BUDGET_CALCULATED", "CASE_OPENED",
                   "PLACEMENT_COMPLETED_FROM_DOCUMENT", "FORM_DETAILS_SAVED", "BUDGET_RECORDED",
                   "EMPLOYEE_RECORD_VERIFIED"):
        assert action in actions, action
    audit_text = json.dumps([e.model_dump(mode="json") for e in container.onboarding_deps.audit_log.entries()])
    for secret in ("22,000", "SYNTHETIC-MOBILE-31", "SYNTHETIC-FATHER-NAME-31", "Omar Synthetic"):
        assert secret not in audit_text
    assert '"budget_total": "21500.00"' in audit_text


def test_the_same_document_twice_links_to_the_first_case_and_writes_nothing_new():
    service, container, details = _memory(source_store=InMemorySourceDocumentStore())
    first = service.process(_doc(), "a.docx", _ctx())
    second = service.process(_doc(), "a.docx", _ctx(request_id="REQ-DOC-2"))
    assert second.status == DocumentIntakeStatus.LINKED_EXISTING and second.duplicate_document
    assert second.existing_case_id == first.case_id and second.reasons == ["duplicate_document"]
    assert len(container.onboarding_deps.case_repo.list_for_client(CLIENT)) == 1
    assert details.find_by_document(CLIENT, first.document_sha256) == [first.form_id]


def test_a_corrected_document_for_the_same_employee_links_and_never_replaces_the_budget():
    service, container, details = _memory()
    first = service.process(_doc(), "a.docx", _ctx())
    corrected = _doc(**{"annual ticket -employee": ["Ticket cost: AED 1,400"]})
    second = service.process(corrected, "b.docx", _ctx(request_id="REQ-DOC-2"))
    assert second.status == DocumentIntakeStatus.LINKED_EXISTING and not second.duplicate_document
    assert details.get(first.form_id).budget.budget_total == Decimal("21500.00")  # unchanged
    assert len(container.onboarding_deps.case_repo.list_for_client(CLIENT)) == 1


def test_the_email_may_come_from_the_document_but_a_differing_caller_email_is_a_conflict():
    service, _, _ = _memory()
    result = service.process(_doc(), "a.docx", _ctx(candidate_email=None))
    assert result.status == DocumentIntakeStatus.CREATED
    service, _, _ = _memory()
    result = service.process(_doc(), "a.docx", _ctx(candidate_email="someone.else@example.invalid"))
    assert result.status == DocumentIntakeStatus.NEEDS_REVIEW
    assert "conflict:supplementary.employee_email" in result.review_reasons


def test_a_caller_department_that_differs_from_the_document_needs_review():
    service, _, _ = _memory()
    result = service.process(_doc(), "a.docx", _ctx(placement={"location_name": "SYN Dubai Office",
                                                               "department": "Finance"}))
    assert result.status == DocumentIntakeStatus.NEEDS_REVIEW
    assert "conflict:supplementary.department" in result.review_reasons


# ------------------------------------------------------------------ failure scenarios


@pytest.mark.parametrize("label,error", [("name of employee", "missing:employee.employee_name"),
                                         ("doj", "missing:employee.joining_date"),
                                         ("final salary", "missing:employee.final_salary")])
def test_a_missing_mandatory_value_stops_before_anything_is_written(label, error):
    service, container, details = _memory()
    result = service.process(_doc(**{label: []}), "a.docx", _ctx())
    assert result.status == DocumentIntakeStatus.VALIDATION_FAILED and error in result.errors
    assert container.onboarding_deps.case_repo.list_for_client(CLIENT) == []
    assert details.find_by_document(CLIENT, result.document_sha256) == []


def test_a_blank_form_fails_validation_and_reports_no_budget():
    service, container, _ = _memory()
    blank = mock_docs.build_docx({}, "Roles and Responsibilities (eg given below)", [])
    result = service.process(blank, "blank.docx", _ctx())
    assert result.status == DocumentIntakeStatus.VALIDATION_FAILED
    assert result.extraction.budget.status == BudgetStatus.UNAVAILABLE
    assert container.onboarding_deps.case_repo.list_for_client(CLIENT) == []


def test_a_form_without_any_budget_still_creates_the_employee_and_stores_an_unavailable_budget():
    empty = {k: [] for k in ("1 year or 2 years visa", "visa - visit + entry permit", "insurance",
                             "annual ticket -employee", "annual ticket- family", "insurance for employee",
                             "insurance for family", "any other promises made to the candidate",
                             "hotel stay - how many days (only applicable for staff outside uae)")}
    service, _, details = _memory()
    result = service.process(_doc(**empty), "a.docx", _ctx())
    assert result.status == DocumentIntakeStatus.CREATED and result.persistence_verified
    assert details.get(result.form_id).budget.status == BudgetStatus.UNAVAILABLE


def test_an_invalid_currency_goes_to_hr_review():
    service, container, _ = _memory()
    result = service.process(_doc(**{"annual ticket -employee": ["Ticket cost: XYZ 1,250"]}), "a.docx", _ctx())
    assert result.status == DocumentIntakeStatus.NEEDS_REVIEW
    assert "budget:invalid:travel.employee.ticket" in result.review_reasons
    assert container.onboarding_deps.case_repo.list_for_client(CLIENT) == []


@pytest.mark.parametrize("data,code", [(b"%PDF-1.4 broken", "CORRUPT_PDF"), (b"not a pdf at all", "NOT_A_PDF")])
def test_a_malformed_pdf_is_rejected_and_never_stored(data, code):
    store = InMemorySourceDocumentStore()
    service, _, _ = _memory(source_store=store)
    result = service.process(data, "x.pdf", _ctx())
    assert result.status == DocumentIntakeStatus.REJECTED and result.rejection_code in ("CORRUPT_PDF", "NOT_A_PDF")
    assert store.objects == {}


def test_an_unavailable_details_store_stops_before_the_case_is_created():
    fake = DetailsFake(missing=TABLES)
    service, container, _ = _memory(details=SupabaseFormDetailsRepository(_rest(fake), _cipher()))
    result = service.process(_doc(), "a.docx", _ctx())
    assert result.status == DocumentIntakeStatus.PERSISTENCE_FAILED
    assert result.errors == ["persistence:form_details_unavailable:FormDetailsStoreUnavailable"]
    assert container.onboarding_deps.case_repo.list_for_client(CLIENT) == []  # nothing half-created


def test_supabase_unreachable_is_a_persistence_failure_not_a_crash():
    def down(request):
        raise httpx.ConnectError("unreachable", request=request)

    repo = SupabaseFormDetailsRepository(PostgrestClient(BASE, SecretStr("k"), transport=httpx.MockTransport(down)),
                                         _cipher())
    service, container, _ = _memory(details=repo)
    result = service.process(_doc(), "a.docx", _ctx())
    assert result.status == DocumentIntakeStatus.PERSISTENCE_FAILED
    assert container.onboarding_deps.case_repo.list_for_client(CLIENT) == []


def test_a_database_constraint_failure_is_reported_and_never_shown_as_success():
    fake = DetailsFake(refuse={"uc01_onboarding_budget_lines": "23514"})
    service, container, _ = _memory(details=SupabaseFormDetailsRepository(_rest(fake), _cipher()))
    result = service.process(_doc(), "a.docx", _ctx())
    assert result.status == DocumentIntakeStatus.PERSISTENCE_FAILED and not result.persistence_verified
    assert "persistence:form_details:CheckViolation" in result.errors
    actions = [e.action for e in container.onboarding_deps.audit_log.entries()]
    assert "DOCUMENT_PERSISTENCE_FAILED" in actions and "EMPLOYEE_RECORD_VERIFIED" not in actions


def test_a_read_back_mismatch_is_a_persistence_failure():
    class Lossy(InMemoryFormDetailsRepository):
        def get(self, form_id):
            found = super().get(form_id)
            return found.model_copy(update={"hotel_stay_days": 99}) if found else None

    service, _, _ = _memory(details=Lossy())
    result = service.process(_doc(), "a.docx", _ctx())
    assert result.status == DocumentIntakeStatus.PERSISTENCE_FAILED
    assert "readback:hotel_stay_days" in result.errors and result.readback["form_details"] is False


def test_a_storage_failure_stops_the_intake():
    class Broken(InMemorySourceDocumentStore):
        def store(self, *args, **kwargs):
            raise StorageError("down")

    service, container, _ = _memory(source_store=Broken())
    result = service.process(_doc(), "a.docx", _ctx())
    assert result.status == DocumentIntakeStatus.PERSISTENCE_FAILED and result.errors == [
        "persistence:storage:StorageError"]
    assert container.onboarding_deps.case_repo.list_for_client(CLIENT) == []


def test_the_encryption_key_is_required_whenever_supabase_is_configured():
    with pytest.raises(ValueError, match="UC01_FIELD_ENCRYPTION_KEY"):
        Settings(app_env="dev", supabase_url=BASE, supabase_service_role_key=SecretStr("k"))


def test_supabase_mode_without_the_details_port_refuses_before_a_case():
    from test_supabase_interim_storage import LiveLikeFake, _container

    from sub_agent.adapters.supabase import SchemaGapError

    container = _container(LiveLikeFake(), supabase_interim_writes=False)
    with pytest.raises(SchemaGapError, match="form_details"):
        container.form_details.ensure_ready()


# ------------------------------------------------------------------ interim Supabase mode (FAKE PostgREST)


def test_interim_supabase_mode_persists_and_reads_back_the_whole_record():
    from test_supabase_interim_storage import LiveLikeFake, _container

    fake = LiveLikeFake()
    fake.unique.update(DetailsFake().unique)
    container = _container(fake)
    service = DocumentIntakeService(container.onboarding_deps, details_repo=container.form_details,
                                    today=lambda: TODAY)
    result = service.process(_doc(), "a.docx", _ctx())
    assert result.status == DocumentIntakeStatus.CREATED and result.persistence_verified, result.errors
    assert len(fake.tables["onboarding_cases"]) == 1 and len(fake.tables["uc01_onboarding_budget"]) == 1
    assert fake.tables["onboarding_cases"][0]["department"] == "Data & Analytics"
    assert fake.deletes == []


# ------------------------------------------------------------------ API


def test_the_api_returns_the_budget_and_verification_without_secrets():
    import os

    from fastapi.testclient import TestClient

    from sub_agent.api.app import create_app

    os.environ["SUPABASE_URL"] = ""
    container = build_container(Settings(app_env="dev", dedup_hmac_key="t", system_api_key="doc-key"))
    client = TestClient(create_app(container))
    body = {"filename": "SYNTHETIC.docx", "content_base64": base64.b64encode(_doc()).decode(), "client_id": CLIENT,
            "hr_officer_id": "SYN-HR-OFFICER-31", "candidate_email": "omar.synthetic@example.invalid",
            "offer_accepted": True, "work_location_type": "OFFICE", "location_name": "SYN Dubai Office",
            "form_status": "APPROVED", "approved_by": "SYN-HR-MANAGER-31",
            "approved_at": "2026-11-01T09:00:00+00:00"}
    response = client.post("/internal/onboarding-documents", json=body, headers={"X-System-Key": "doc-key"})
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["status"] == "CREATED" and out["persistence_verified"] is True and out["form_id"]
    assert out["extraction"]["budget"]["budget_total"] == "21500.00"
    assert out["extraction"]["budget"]["status"] == "COMPLETE"
    for secret in ("22,000", "SYNTHETIC-MOBILE-31", "SYNTHETIC-FATHER-NAME-31"):
        assert secret not in response.text
