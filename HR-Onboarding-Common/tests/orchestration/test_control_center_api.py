"""The routes the AI Onboarding Control Center uses beyond the original orchestrator API: case list and
profile (tenant-scoped, identity gated by role), document upload to object storage with UC03's real
extractor and the orchestrator's real UC03 path, signed URLs, CORS and the health report.

Agent: scripts/run_workflow_demo.build_synthetic_agent (the real graph, routing, gates and UC03;
contract-shaped stand-ins for UC01/UC02/UC04/UC05). Storage: InMemoryObjectStorage (same contract as
SupabaseObjectStorage)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import run_workflow_demo as demo  # noqa: E402

from hr_onboarding_common.orchestration.api import create_orchestrator_app  # noqa: E402
from hr_onboarding_common.orchestration.documents import (DocumentIntakeService, InMemoryCandidateDocuments,  # noqa: E402
                                                          InMemoryObjectStorage, metadata_from_extraction,
                                                          object_path, parse_document_ref, uc03_extractor,
                                                          UnsafeObjectPath)
from hr_onboarding_common.platform.identity import InsecureHeaderIdentityProvider  # noqa: E402


BASE = "/v1/orchestration/cases"
HR = demo.HR
HR_OPS = {"X-User-Id": "user:hr-ops-01", "X-Client-Id": demo.CLIENT, "X-User-Roles": "hr_ops"}
OTHER = {"X-User-Id": "user:hr-admin-b", "X-Client-Id": "CLIENT-TEST-B", "X-User-Roles": "hr_admin"}
BODY = {**demo.START_BODY, "grade": "G7", "nationality_group": "NG-1", "work_location_type": "OFFICE"}
MANAGER_B = {"X-User-Id": "user:hr-lead-b", "X-Client-Id": "CLIENT-TEST-B", "X-User-Roles": "hr_lead"}


def opened(client, body, headers=HR, approver=None) -> dict:
    """Submit the internal onboarding form, then an HR lead of the same tenant (never the requester)
    approves it: only then does UC01 open the case."""
    view = client.post(BASE, json=body, headers=headers).json()
    approval = view["form_approval"]
    approver = approver or (demo.HR_LEAD if headers.get("X-Client-Id") == demo.CLIENT else MANAGER_B)
    out = client.post(f"{BASE}/{view['case_id']}/approvals/{approval['approval_id']}/decision",
                      json={"decision": "approved"}, headers=approver)
    assert out.status_code == 200, out.text
    return out.json()


def pdf(lines: list[str]) -> bytes:
    """A small, valid one-page PDF with the lines as native text (built by hand: no PDF library needed)."""
    esc = [ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in lines]
    stream = "BT /F1 11 Tf 72 760 Td 14 TL " + " ".join(f"({ln}) '" for ln in esc) + " ET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               "/Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


FILLER = ["This document is issued for identification purposes and remains the property of the issuing authority."] * 3
PASSPORT = pdf(["PASSPORT", "Name: Alex Morgan", "Passport No: AB1234567", "Nationality: Testland",
                "Date of Birth: 01/02/1990", "Date of Expiry: 01/01/2031", *FILLER])


class RecordingExtractor:
    """Stands in for UC03's extractor and records the bytes it was given: the file read back from storage."""

    def __init__(self):
        self.seen: list[bytes] = []

    async def __call__(self, path: str, document_type: str):
        self.seen.append(Path(path).read_bytes())
        return SimpleNamespace(extracted_name=demo.CANDIDATE, confidence=0.9, injection_suspected=False,
                               extracted_fields={"passport_number": "AB1234567", "expiry_date": "01/01/2031"}), "test"


@pytest.fixture
def world():
    agent = demo.build_synthetic_agent(None)
    storage = InMemoryObjectStorage("Doc_Collection")
    repo = InMemoryCandidateDocuments()
    extractor = RecordingExtractor()
    service = DocumentIntakeService(storage=storage, repository=repo, extractor=extractor, max_bytes=200_000)
    service.extractor_spy = extractor
    app = create_orchestrator_app(agent=agent, identity_provider=InsecureHeaderIdentityProvider(enabled=True),
                                  documents=service, cors_origins=["http://localhost:5173"],
                                  runtime_info={"mode": "demo", "state_store": "InMemoryCaseStateStore"})
    app.state.documents = service
    client = TestClient(app)
    return client, storage, repo, opened(client, BODY)


def upload(client, case_id, doc_type, data, *, mime="application/pdf", headers=HR, name="scan.pdf"):
    return client.post(f"{BASE}/{case_id}/documents/{doc_type}/upload", content=data,
                       headers={**headers, "Content-Type": mime, "X-File-Name": name})


# ------------------------------------------------------------------ list, profile, start fields


def test_list_is_tenant_scoped_and_carries_the_intake_profile(world):
    client, _, _, view = world
    other = client.post(BASE, json={**BODY, "offer_reference": "OFR-B-1"}, headers=OTHER)
    assert other.status_code == 201 and other.json()["form_approval"]["status"] == "pending"
    listed = client.get(BASE, headers=HR).json()
    assert [c["case_id"] for c in listed["cases"]] == [view["case_id"]]
    summary = listed["cases"][0]
    assert summary["profile"]["candidate_name"] == demo.CANDIDATE
    assert summary["profile"]["designation"] == "Software Engineer"
    assert summary["profile"]["grade"] == "G7" and summary["profile"]["nationality_group"] == "NG-1"
    assert summary["profile"]["requested_by"] == "user:hr-admin-01"
    assert summary["stage_statuses"]["intake"] == "complete"
    assert "candidate_email" not in str(listed) and "ID-DEMO-001" not in str(listed)


def test_name_is_withheld_without_the_identity_role(world):
    client, _, _, view = world
    assert client.get(BASE, headers=HR_OPS).json()["cases"][0]["profile"]["candidate_name"] is None
    profile = client.get(f"{BASE}/{view['case_id']}/profile", headers=HR_OPS).json()
    assert profile["profile"]["candidate_name"] is None and profile["profile"]["department"] == "Engineering"


def test_profile_of_another_tenants_case_is_not_found(world):
    client, _, _, view = world
    assert client.get(f"{BASE}/{view['case_id']}/profile", headers=OTHER).status_code == 404


def test_repeated_start_does_not_create_a_second_case(world):
    client, _, _, view = world
    again = client.post(BASE, json=BODY, headers=HR)
    assert again.status_code == 201 and again.json()["case_id"] == view["case_id"]
    assert len(client.get(BASE, headers=HR).json()["cases"]) == 1


# ------------------------------------------------------------------ upload


def test_upload_stores_under_the_tenant_path_and_runs_uc03_on_the_stored_file(world):
    client, storage, repo, view = world
    response = upload(client, view["case_id"], "passport", PASSPORT)
    assert response.status_code == 200, response.text
    spy = client.app.state.documents.extractor_spy
    assert spy.seen == [PASSPORT]  # UC03 extracted the object read back from storage
    result = response.json()
    receipt, case = result["upload"], result["case"]
    assert receipt["storage_path"].startswith(f"{demo.CLIENT}/{view['case_id']}/")
    assert receipt["storage_path"] in storage.objects and storage.objects[receipt["storage_path"]][0] == PASSPORT
    assert receipt["document_ref"] == f"storage:Doc_Collection/{receipt['storage_path']}"
    assert receipt["extraction"]["readable"] is True and receipt["extraction"]["required_fields_present"] is True
    assert receipt["extraction"]["expiry_date"] == "2031-01-01"
    # No onboarding_cases row for this case: the metadata row is skipped, explicitly.
    assert receipt["metadata_persisted"] is False and receipt["metadata_reason"] == "CASE_HAS_NO_ONBOARDING_CASES_ROW"
    assert case["documents"]["passport"]["document_ref"] == receipt["document_ref"]
    assert case["documents"]["passport"]["revision"] == 1
    assert "passport" in case["document_validation"] or case["stage_statuses"]["document_submission"] != "complete"


def test_metadata_row_is_written_when_the_case_has_an_onboarding_cases_row(world):
    client, _, repo, view = world
    repo.known_cases.add((demo.CLIENT, view["case_id"]))
    receipt = upload(client, view["case_id"], "passport", PASSPORT).json()["upload"]
    assert receipt["metadata_persisted"] is True
    row = repo.rows[receipt["document_id"]]
    # The table's own status vocabulary (its CHECK accepts UPLOADED / VALIDATED / REJECTED / EXPIRED; RECEIVED is refused)
    assert row["checksum_sha256"] == receipt["checksum_sha256"] and row["status"] == "UPLOADED"
    assert row["file_name"] == "scan.pdf" and row["document_ref"] == receipt["document_ref"]


def test_the_same_file_twice_is_idempotent(world):
    client, storage, _, view = world
    first = upload(client, view["case_id"], "passport", PASSPORT).json()
    second = upload(client, view["case_id"], "passport", PASSPORT).json()
    assert second["upload"]["storage_status"] == "exists"
    assert second["upload"]["document_id"] == first["upload"]["document_id"]
    assert second["case"]["documents"]["passport"]["revision"] == 1
    assert len(storage.objects) == 1


def test_a_different_file_is_a_new_revision(world):
    client, storage, _, view = world
    upload(client, view["case_id"], "passport", PASSPORT)
    other = pdf(["PASSPORT", "Name: Alex Morgan", "Passport No: AB7654321", "Date of Expiry: 01/01/2032", *FILLER])
    case = upload(client, view["case_id"], "passport", other).json()["case"]
    assert case["documents"]["passport"]["revision"] == 2 and len(storage.objects) == 2


@pytest.mark.parametrize("data,mime,status,code", [
    (b"PK\x03\x04docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", 415,
     "UNSUPPORTED_FILE_TYPE"),
    (b"not really a pdf", "application/pdf", 415, "FILE_SIGNATURE_MISMATCH"),
    (b"", "application/pdf", 400, "EMPTY_FILE"),
    (b"%PDF-" + b"0" * 300_000, "application/pdf", 413, "FILE_TOO_LARGE"),
], ids=["docx", "bad-signature", "empty", "too-large"])
def test_unacceptable_files_are_refused_before_storage(world, data, mime, status, code):
    client, storage, _, view = world
    response = upload(client, view["case_id"], "passport", data, mime=mime)
    assert response.status_code == status and code in response.json()["detail"]
    assert storage.uploads == 0


def test_a_document_the_plan_does_not_require_is_refused(world):
    client, storage, _, view = world
    response = upload(client, view["case_id"], "emirates_id_unplanned", PASSPORT)
    assert response.status_code == 422 and "DOCUMENT_NOT_REQUIRED" in response.json()["detail"]
    assert storage.uploads == 0


def test_another_tenant_cannot_upload_to_the_case(world):
    client, storage, _, view = world
    assert upload(client, view["case_id"], "passport", PASSPORT, headers=OTHER).status_code == 404
    assert storage.uploads == 0


def test_a_reader_without_the_submitter_role_cannot_upload(world):
    client, storage, _, view = world
    assert upload(client, view["case_id"], "passport", PASSPORT, headers=HR_OPS).status_code == 403
    assert storage.uploads == 0


def test_no_storage_configured_answers_503():
    agent = demo.build_synthetic_agent(None)
    client = TestClient(create_orchestrator_app(agent=agent,
                                                identity_provider=InsecureHeaderIdentityProvider(enabled=True)))
    case_id = opened(client, BODY)["case_id"]
    response = upload(client, case_id, "passport", PASSPORT)
    assert response.status_code == 503 and "DOCUMENT_STORAGE_NOT_CONFIGURED" in response.json()["detail"]
    assert client.get("/healthz").json()["document_storage"] == {"configured": False}


# ------------------------------------------------------------------ signed URL


def test_signed_url_is_issued_for_the_latest_stored_revision(world):
    client, _, _, view = world
    upload(client, view["case_id"], "passport", PASSPORT)
    out = client.get(f"{BASE}/{view['case_id']}/documents/passport/url", headers=HR)
    assert out.status_code == 200 and out.json()["url"].startswith("memory://Doc_Collection/CLIENT-TEST-A/")
    assert out.json()["expires_in"] == 60


def test_signed_url_refused_cross_tenant_without_role_and_for_reference_only_documents(world):
    client, _, _, view = world
    upload(client, view["case_id"], "passport", PASSPORT)
    url = f"{BASE}/{view['case_id']}/documents/passport/url"
    assert client.get(url, headers=OTHER).status_code == 404
    assert client.get(url, headers=HR_OPS).status_code == 403
    client.post(f"{BASE}/{view['case_id']}/documents", headers=HR, json={"documents": [
        {"document_type": "nda", "document_ref": "SYN-DOC-nda-1", "metadata": {"readable": True}}]})
    response = client.get(f"{BASE}/{view['case_id']}/documents/nda/url", headers=HR)
    assert response.status_code == 409 and "DOCUMENT_NOT_IN_STORAGE" in response.json()["detail"]


# ------------------------------------------------------------------ app plumbing and helpers


def test_cors_preflight_and_health_report(world):
    client, _, _, _ = world
    preflight = client.options(BASE, headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                                              "Access-Control-Request-Headers": "x-user-id,content-type"})
    assert preflight.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert client.options(BASE, headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"}
                          ).headers.get("access-control-allow-origin") is None
    health = client.get("/healthz").json()
    assert health["document_storage"]["bucket"] == "Doc_Collection" and health["mode"] == "demo"
    assert "service_role" not in str(health).lower()


def test_object_paths_never_carry_unsafe_segments():
    with pytest.raises(UnsafeObjectPath):
        object_path("../x", "CASE-1", "0f8fad5b-d9cb-469f-a165-70867728950e", "pdf")
    with pytest.raises(UnsafeObjectPath):
        object_path("CLIENT", "CASE/1", "0f8fad5b-d9cb-469f-a165-70867728950e", "pdf")
    with pytest.raises(UnsafeObjectPath):
        object_path("CLIENT", "CASE-1", "not-a-uuid", "pdf")
    assert parse_document_ref("storage:B/../x") is None and parse_document_ref("SYN-DOC-1") is None


def test_injection_suspected_extraction_never_carries_confidence():
    class Result:
        extracted_fields = {"passport_number": "AB1234567"}
        confidence = 0.95
        injection_suspected = True
        extracted_name = "Alex Morgan"

    meta = metadata_from_extraction(Result())
    assert meta["extraction_confidence"] == 0.0 and meta["evidence_stored"] is True
    assert "attestation_confirmed" not in meta and "visa_required" not in meta


def test_uc03_real_extractor_reads_a_native_text_pdf():
    """UC03's own LocalDocumentExtractor on a real PDF. Needs PyMuPDF to load (on Windows: the Microsoft
    Visual C++ Redistributable)."""
    pytest.importorskip("fitz", reason="PyMuPDF cannot load in this environment", exc_type=ImportError)
    import asyncio
    import tempfile

    extract = uc03_extractor(demo.UC03_ROOT, online=False)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
        handle.write(PASSPORT)
    try:
        result, method = asyncio.run(extract(handle.name, "passport"))
    finally:
        Path(handle.name).unlink()
    meta = metadata_from_extraction(result)
    assert method == "uc03_local" and meta["readable"] is True
    assert meta["expiry_date"] == "2031-01-01" and result.extracted_fields["passport_number"] == "AB1234567"


def test_a_persisted_orchestrator_can_validate_documents_with_the_real_uc03(tmp_path):
    """Regression: with persistence the orchestrator's checkpointer is the synchronous SqliteSaver. UC03's
    graph ran as a checkpointed subgraph and every validation failed (NotImplementedError ->
    UC03_TECHNICAL_ERROR, retry_pending)."""
    agent = demo.build_synthetic_agent(tmp_path)
    client = TestClient(create_orchestrator_app(agent=agent,
                                                identity_provider=InsecureHeaderIdentityProvider(enabled=True)))
    view = opened(client, demo.START_BODY)
    required = [d["document_type"] for d in view["document_collection"]["required_documents"]]
    view = client.post(f"{BASE}/{view['case_id']}/documents", json=demo._documents(required), headers=HR).json()
    assert view["stage_statuses"]["validate_documents"] == "awaiting_approval", view["stage_errors"]
    assert "UC03_TECHNICAL_ERROR" not in str(view["stage_errors"])


def test_a_file_with_no_readable_text_is_never_sent_to_the_llm(tmp_path, monkeypatch):
    """Regression: UC03's hybrid extractor sent empty text to the LLM and defaulted the confidence to 0.5,
    so an unreadable document came back readable."""
    import asyncio

    extract = uc03_extractor(demo.UC03_ROOT, online=True)
    from src.sub_agent import extraction as uc03_extraction  # importable once uc03_extractor ran

    async def must_not_be_called(self, *args, **kwargs):
        raise AssertionError("the LLM must not be asked to read a document with no text")

    monkeypatch.setattr(uc03_extraction.LLMDocumentExtractor, "extract", must_not_be_called)
    path = tmp_path / "blank.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")  # opens to no text (or does not open at all)
    result, method = asyncio.run(extract(str(path), "passport"))
    meta = metadata_from_extraction(result)
    assert meta["readable"] is False and meta["extraction_confidence"] == 0.0
    assert method.startswith("uc03_local")


def _through_documents(client, view, headers=HR):
    required = [d["document_type"] for d in view["document_collection"]["required_documents"]]
    view = client.post(f"{BASE}/{view['case_id']}/documents", json=demo._documents(required), headers=headers).json()
    reviews = [{"document_type": t, "decision": "approved"} for t, r in view["document_validation"].items()
               if r.get("classification") == "hr_review_required"]
    return client.post(f"{BASE}/{view['case_id']}/documents/reviews", json={"reviews": reviews}, headers=headers).json()


def test_uc04_uses_the_tenants_configured_client_config_when_the_case_names_none():
    from hr_onboarding_common.orchestration.tenant_config import TenantConfig

    agent = demo.build_synthetic_agent(None)
    agent.deps.tenants = TenantConfig.load(Path(demo.COMMON) / "config" / "tenants.yaml")
    assert Path(agent.deps.tenants.uc04_config_ref_for(demo.CLIENT)).exists()
    client = TestClient(create_orchestrator_app(agent=agent,
                                                identity_provider=InsecureHeaderIdentityProvider(enabled=True)))
    body = {k: v for k, v in demo.START_BODY.items() if k != "uc04_client_config_ref"}
    view = _through_documents(client, opened(client, body))
    assert view["stage_statuses"]["equipment_access"] == "complete", view["stage_errors"]
    # A tenant without its own entry never receives another client's (or the default's) configuration.
    assert agent.deps.tenants.uc04_config_ref_for("CLIENT-UNCONFIGURED") is None
    other = opened(client, {**body, "offer_reference": "OFR-B-2"}, headers=OTHER)
    assert agent.deps.tenants.uc04_config_ref_for("CLIENT-TEST-B") is None
    other = _through_documents(client, other, headers=OTHER)
    assert other["stage_statuses"]["equipment_access"] == "blocked"
    assert other["stage_errors"]["equipment_access"]["code"] == "UC04_CONFIGURATION_MISSING"


def test_document_storage_has_its_own_configuration_and_workers_never_receive_its_key(tmp_path):
    """A bare SUPABASE_URL in the orchestrator's environment switches UC01's worker into its (blocked) Supabase
    mode; the dedicated names avoid that, and the storage key is withheld from every worker process."""
    from hr_onboarding_common.orchestration.documents import SupabaseObjectStorage
    from hr_onboarding_common.orchestration.workers.config import WorkerConfig

    env = {"DOCUMENT_STORAGE_BUCKET": "Doc_Collection", "DOCUMENT_STORAGE_SUPABASE_URL": "https://example.supabase.co",
           "DOCUMENT_STORAGE_SERVICE_ROLE_KEY": "service-role-test-value"}
    storage = SupabaseObjectStorage.from_env(env)
    assert storage is not None and storage.bucket == "Doc_Collection"
    assert SupabaseObjectStorage.from_env({}) is None  # no bucket: no storage, never a fallback
    for uc in ("uc01", "uc04", "uc05"):
        child = WorkerConfig(uc_id=uc, python_executable=tmp_path, src_dir=tmp_path).child_environment(
            {**env, "PATH": "x"})
        assert "DOCUMENT_STORAGE_SERVICE_ROLE_KEY" not in child and "SUPABASE_URL" not in child
