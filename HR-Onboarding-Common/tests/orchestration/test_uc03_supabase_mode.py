"""UC03 in Supabase mode, embedded in the orchestrator: no network, no database.

UC03's own strict table fake (UC03 tests/unit/fake_tables.py: the live columns and required flags, the shared CHECK
lists, primary/unique/foreign keys, append-only audit) stands in for Supabase. What is proved:
  * the orchestrator hands UC03 its own table client (never a bare SUPABASE_URL) and UC03's case state, audit,
    validation results AND its document collection live in Supabase;
  * the collection and its revisions survive a restart (a new orchestrator on the same tables);
  * a submitted document whose result cannot be stored blocks the stage (no NOT_PERSISTED success);
  * another tenant's stored UC03 case is refused (FAILED, TENANT_CONTEXT_CONFLICT);
  * the upload path writes the table's vocabulary (UPLOADED, UC03's type mapping) and, when Supabase is
    authoritative, refuses a type the table cannot hold before anything is stored;
  * the PostgREST client raises UC03's errors and never puts the key in a message.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from _helpers import UC_ROOTS, require_uc
from hr_onboarding_common.orchestration.adapters import uc03_documents
from hr_onboarding_common.orchestration.adapters.uc03_documents import (NOT_PERSISTED_CODE, call_uc03_validate_documents,
                                                                         collection_service)
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.documents import (DocumentIntakeService, DocumentUploadError,
                                                          InMemoryCandidateDocuments, InMemoryObjectStorage,
                                                          uc03_document_types)
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state
from hr_onboarding_common.orchestration.uc03_supabase import Uc03PostgrestTables, Uc03StoreConfigurationError

GOOD = {"readable": True, "required_fields_present": True, "expiry_date": "2031-01-01", "evidence_stored": True,
        "extracted_name": "Arun Kumar", "attestation_confirmed": True}
CLIENT, OTHER = "CL1", "CL2"
CASE = str(uuid.UUID(int=4242))


def _fake_tables():
    require_uc("uc03")
    uc03_documents._ensure_uc03_importable(UC_ROOTS["uc03"])
    path = UC_ROOTS["uc03"] / "tests" / "unit" / "fake_tables.py"
    spec = importlib.util.spec_from_file_location("uc03_fake_tables_supabase_mode", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tables = module.FakeTables()
    tables.seed("client_configs", client_id=CLIENT)
    tables.seed("client_configs", client_id=OTHER)
    tables.seed("onboarding_cases", case_id=CASE, client_id=CLIENT)
    return tables


def _deps(tables):
    return OrchestratorDeps(ctx=None, worker_pool=None, uc03_repo_root=UC_ROOTS["uc03"], uc03_store_mode="supabase",
                            uc03_tables_factory=lambda: tables)


def _state(documents, *, client_id=CLIENT):
    state = new_state(case_id=CASE, client_id=client_id, correlation_id="corr-1",
                      actor={"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"]},
                      intake_trigger={"candidate_name": "Arun Kumar", "documents": {}},
                      uc02_role_context={"role_id": "engineer", "grade": "G1", "nationality": "IN", "location": "Dubai"})
    state["employee_id"] = "EMP-000101"
    state["document_collection"] = {"plan_version": 1, "required_documents": [
        {"document_type": t, "required": True} for t in documents]}
    state["submitted_documents"] = {t: {"document_type": t, "document_ref": f"storage:Doc_Collection/{t}.pdf",
                                        "revision": 1, "metadata": dict(GOOD)} for t in documents}
    return state


def _register(tables, document_type, schema_type):
    return tables.insert("candidate_documents", {
        "case_id": CASE, "client_id": CLIENT, "document_type": schema_type,
        "document_ref": f"storage:Doc_Collection/{document_type}.pdf", "file_name": f"{document_type}.pdf",
        "file_size_bytes": 10, "mime_type": "application/pdf", "checksum_sha256": document_type[0] * 64})


# ------------------------------------------------------------------------------------------------ validation
def test_validation_results_case_state_and_audit_are_stored_in_supabase():
    tables = _fake_tables()
    _register(tables, "passport", "PASSPORT")
    deps = _deps(tables)
    result = call_uc03_validate_documents(_state(["passport"]), deps)

    assert deps._uc03_resolved_mode == "supabase"
    assert result["document_validation_result"]["passport"]["persistence_status"] == "PERSISTED"
    (row,) = tables.rows["document_validations"]
    assert row["case_id"] == CASE and row["uc03_case_ref"] == CASE
    # UC03's exact values in its own columns; the shared columns hold the CHECK list's mapped value
    assert row["uc03_compliance_status"] in ("COMPLETE", "INCOMPLETE", "BLOCKED", "PENDING_APPROVAL")
    assert row["compliance_item_status"] in ("COMPLIANT", "INCOMPLETE", "NON_COMPLIANT")
    assert row["hr_review_status"] in ("PENDING", "ACCEPTED", "REJECTED", "NOT_REQUIRED")
    state_rows = {r["case_ref"]: r for r in tables.rows["uc03_case_state"]}
    assert state_rows[CASE]["client_id"] == CLIENT and state_rows[CASE]["onboarding_case_id"] == CASE
    assert any(r.get("reference") == f"uc03:{CASE}" for r in tables.rows["audit_events"])
    assert result["stage_errors"].get(Stage.VALIDATE_DOCUMENTS.value, {}).get("code") != NOT_PERSISTED_CODE


def test_a_submitted_document_whose_result_cannot_be_stored_blocks_the_stage():
    tables = _fake_tables()
    _register(tables, "passport", "PASSPORT")
    # payslips: UC03 type with no value in candidate_documents.document_type; nda: mappable but never uploaded
    result = call_uc03_validate_documents(_state(["passport", "payslips", "nda"]), _deps(tables))

    statuses = {t: r["persistence_status"] for t, r in result["document_validation_result"].items()}
    assert statuses["passport"] == "PERSISTED"
    assert statuses["payslips"] == "NOT_PERSISTED:DOCUMENT_TYPE_NOT_IN_SCHEMA"
    assert statuses["nda"] == "NOT_PERSISTED:DOCUMENT_NOT_REGISTERED"
    assert result["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.BLOCKED.value
    assert result["stage_errors"][Stage.VALIDATE_DOCUMENTS.value]["code"] == NOT_PERSISTED_CODE
    reasons = {b["reason"]: b for b in result["blockers"] if b["code"] == NOT_PERSISTED_CODE}
    assert set(reasons) == {"payslips: DOCUMENT_TYPE_NOT_IN_SCHEMA", "nda: DOCUMENT_NOT_REGISTERED"}
    assert reasons["nda: DOCUMENT_NOT_REGISTERED"]["owner"] == "hr_admin"


def test_a_missing_document_is_not_a_persistence_gap():
    tables = _fake_tables()
    _register(tables, "passport", "PASSPORT")
    state = _state(["passport", "degree_certificate"])
    del state["submitted_documents"]["degree_certificate"]  # mandatory, never supplied
    result = call_uc03_validate_documents(state, _deps(tables))
    assert result["stage_errors"][Stage.VALIDATE_DOCUMENTS.value]["code"] != NOT_PERSISTED_CODE


def test_another_tenants_stored_uc03_case_is_refused():
    tables = _fake_tables()
    _register(tables, "passport", "PASSPORT")
    call_uc03_validate_documents(_state(["passport"]), _deps(tables))  # UC03's case is stored under CL1
    result = call_uc03_validate_documents(_state(["passport"], client_id=OTHER), _deps(tables))
    assert result["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.FAILED.value
    assert result["stage_errors"][Stage.VALIDATE_DOCUMENTS.value]["code"] == "TENANT_CONTEXT_CONFLICT"


def test_a_database_failure_is_a_technical_failure_not_a_verdict():
    tables = _fake_tables()
    tables.fail.add(("select", "uc03_case_state"))
    result = call_uc03_validate_documents(_state(["passport"]), _deps(tables))
    assert result["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.RETRY_PENDING.value
    assert not tables.rows["document_validations"]


# ------------------------------------------------------------------------------------- document collection
def _open(service, *, tenant=CLIENT, version=1, types=("passport",)):
    from src.sub_agent.collection import DocumentCollectionRequest, RequiredDocumentSpec  # type: ignore

    return service.open_collection(DocumentCollectionRequest(
        case_id=CASE, tenant_id=tenant, employee_id="EMP-000101", plan_id="PLAN-1", plan_version=version,
        required_documents=[RequiredDocumentSpec(document_type=t, label=t, mandatory=True) for t in types]))


def test_the_document_collection_and_its_revisions_live_in_supabase_and_survive_a_restart():
    tables = _fake_tables()
    service = collection_service(_deps(tables))
    _open(service)
    first = service.submit(case_id=CASE, tenant_id=CLIENT, document_type="passport", document_ref="storage:b/p1.pdf",
                           submitted_by="user:hr-admin-01", metadata={"readable": True})
    again = service.submit(case_id=CASE, tenant_id=CLIENT, document_type="passport", document_ref="storage:b/p1.pdf",
                           submitted_by="user:hr-admin-01", metadata={"readable": True})
    second = service.submit(case_id=CASE, tenant_id=CLIENT, document_type="passport", document_ref="storage:b/p2.pdf",
                            submitted_by="user:hr-admin-01", metadata={"readable": True})
    assert (first.revision, again.duplicate, second.revision) == (1, True, 2)
    (row,) = [r for r in tables.rows["uc03_case_state"] if r["case_ref"] == f"collection:{CASE}"]
    assert row["client_id"] == CLIENT and row["onboarding_case_id"] == CASE and row["version"] == 3

    restarted = collection_service(_deps(tables))  # a new orchestrator process on the same database
    assert restarted.collection(CASE).plan_version == 1
    assert [d.revision for d in restarted.history(CASE, "passport")] == [1, 2]
    assert restarted.latest(CASE)["passport"].document_ref == "storage:b/p2.pdf"


def test_the_collection_refuses_another_tenant():
    from src.sub_agent.collection import CollectionError  # type: ignore

    tables = _fake_tables()
    service = collection_service(_deps(tables))
    _open(service)
    with pytest.raises(CollectionError):
        _open(service, tenant=OTHER, version=2)
    with pytest.raises(CollectionError):
        service.submit(case_id=CASE, tenant_id=OTHER, document_type="passport", document_ref="storage:b/x.pdf",
                       submitted_by="user:x")


def test_a_concurrent_collection_change_is_refused_not_overwritten():
    from integrations.supabase.collection_registry import SupabaseCollectionRegistry  # type: ignore
    from integrations.supabase.tables import StorageError  # type: ignore

    tables = _fake_tables()
    service = collection_service(_deps(tables))
    _open(service)
    stale = tables.select("uc03_case_state", filters={"case_ref": f"collection:{CASE}"})[0]
    _open(service, version=2)  # someone else moved the collection on
    registry = SupabaseCollectionRegistry(tables)
    with pytest.raises(StorageError) as exc:
        registry._update(stale, {"collection": {}, "documents": {}})
    assert exc.value.code == "CONCURRENT_MODIFICATION"
    assert registry.get_collection(CASE).plan_version == 2


# ---------------------------------------------------------------------------------------------- upload path
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class _Agent:
    def __init__(self, required):
        self.required, self.submitted = required, []

    def require_role(self, actor, key):
        return None

    def get_case(self, case_id, *, client_id):
        return {"case_id": case_id, "client_id": client_id, "document_collection": {
            "plan_version": 1, "required_documents": [{"document_type": t} for t in self.required]}}

    def submit_documents(self, case_id, *, documents, actor):
        self.submitted.extend(documents)
        return {"case_id": case_id}


async def _extract(path, document_type):
    return SimpleNamespace(extracted_name="Arun Kumar", confidence=0.9, injection_suspected=False,
                           extracted_fields={}), "test"


def _upload_service(known_cases, *, strict):
    require_uc("uc03")
    storage = InMemoryObjectStorage("Doc_Collection")
    repo = InMemoryCandidateDocuments(known_cases=known_cases, document_types=uc03_document_types(UC_ROOTS["uc03"]))
    return storage, repo, DocumentIntakeService(storage=storage, repository=repo, extractor=_extract, strict=strict)


ACTOR = {"client_id": CLIENT, "on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"]}


def test_upload_writes_the_tables_vocabulary():
    storage, repo, service = _upload_service({(CLIENT, CASE)}, strict=True)
    agent = _Agent(["signed_offer_letter"])
    _, receipt = service.upload(agent, case_id=CASE, document_type="signed_offer_letter", data=PDF,
                                content_type="application/pdf", file_name="offer.pdf", actor=ACTOR)
    row = repo.rows[receipt.document_id]
    assert row["document_type"] == "OFFER_LETTER" and row["status"] == "UPLOADED"
    assert receipt.metadata_persisted is True and agent.submitted


def test_strict_upload_refuses_a_type_the_table_cannot_hold_before_storing_anything():
    storage, repo, service = _upload_service({(CLIENT, CASE)}, strict=True)
    agent = _Agent(["payslips"])
    with pytest.raises(DocumentUploadError) as exc:
        service.upload(agent, case_id=CASE, document_type="payslips", data=PDF, content_type="application/pdf",
                       file_name="p.pdf", actor=ACTOR)
    assert (exc.value.status, exc.value.code) == (422, "DOCUMENT_TYPE_NOT_IN_SCHEMA")
    assert storage.uploads == 0 and not repo.rows and not agent.submitted


def test_strict_upload_without_an_onboarding_case_row_is_refused_and_not_submitted():
    storage, repo, service = _upload_service(set(), strict=True)
    agent = _Agent(["passport"])
    with pytest.raises(DocumentUploadError) as exc:
        service.upload(agent, case_id=CASE, document_type="passport", data=PDF, content_type="application/pdf",
                       file_name="p.pdf", actor=ACTOR)
    assert (exc.value.status, exc.value.code) == (409, "CASE_NOT_IN_DATABASE")
    assert not agent.submitted


def test_non_strict_upload_still_reports_the_skip():
    storage, repo, service = _upload_service(set(), strict=False)
    agent = _Agent(["passport"])
    _, receipt = service.upload(agent, case_id=CASE, document_type="passport", data=PDF,
                                content_type="application/pdf", file_name="p.pdf", actor=ACTOR)
    assert receipt.metadata_persisted is False and receipt.metadata_reason == "CASE_HAS_NO_ONBOARDING_CASES_ROW"


# ------------------------------------------------------------------------------------------ PostgREST client
SECRET = "service-role-secret-value"


def _client(handler):
    require_uc("uc03")
    uc03_documents._ensure_uc03_importable(UC_ROOTS["uc03"])
    from integrations.supabase.tables import DuplicateKey, StorageError  # type: ignore

    return Uc03PostgrestTables(url="https://example.supabase.co", service_role_key=SECRET, transport=httpx.MockTransport(handler),
                               duplicate_error=DuplicateKey, storage_error=StorageError), DuplicateKey, StorageError


def test_the_client_filters_inserts_and_updates_over_postgrest():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, request.url.path, dict(request.url.params), request.headers.get("Prefer")))
        if request.method == "GET":
            return httpx.Response(200, json=[{"case_ref": "x"}])
        return httpx.Response(201 if request.method == "POST" else 200, json=[json.loads(request.content)])

    client, _, _ = _client(handler)
    assert client.select("uc03_case_state", columns="case_ref", filters={"case_ref": "collection:a", "version": 2},
                         limit=1) == [{"case_ref": "x"}]
    assert client.insert("uc03_case_state", {"case_ref": "a"}) == {"case_ref": "a"}
    assert client.update("uc03_case_state", {"version": 3}, {"case_ref": "a", "version": 2}) == [{"version": 3}]
    get, post, patch = seen
    assert get[2] == {"select": "case_ref", "case_ref": "eq.collection:a", "version": "eq.2", "limit": "1"}
    assert post[3] == "return=representation" and patch[2] == {"case_ref": "eq.a", "version": "eq.2"}


@pytest.mark.parametrize("status,code,expected", [(409, "23505", "duplicate"), (409, "23503", "FOREIGN_KEY_VIOLATION"),
                                                  (400, "23514", "DATABASE_ERROR"), (503, "", "DATABASE_UNAVAILABLE")])
def test_the_client_raises_uc03s_errors_without_the_key(status, code, expected):
    client, duplicate, storage_error = _client(lambda request: httpx.Response(status, json={"code": code, "message": SECRET}))
    with pytest.raises(storage_error) as exc:
        client.insert("candidate_documents", {"document_id": "x"})
    assert (isinstance(exc.value, duplicate) and expected == "duplicate") or exc.value.code == expected
    assert SECRET not in str(exc.value)


def test_supabase_mode_without_settings_names_the_variable_and_never_falls_back():
    require_uc("uc03")
    uc03_documents._ensure_uc03_importable(UC_ROOTS["uc03"])
    from integrations.supabase.tables import DuplicateKey, StorageError  # type: ignore

    with pytest.raises(Uc03StoreConfigurationError, match="ORCHESTRATOR_SUPABASE_URL"):
        Uc03PostgrestTables.from_env({}, duplicate_error=DuplicateKey, storage_error=StorageError)
    deps = OrchestratorDeps(ctx=None, worker_pool=None, uc03_repo_root=UC_ROOTS["uc03"], uc03_store_mode="supabase")
    with pytest.raises(Exception):
        asyncio.run(uc03_documents._validate_all(_state(["passport"]), deps))
    assert deps._uc03_resolved_mode is None  # nothing was built in memory instead
