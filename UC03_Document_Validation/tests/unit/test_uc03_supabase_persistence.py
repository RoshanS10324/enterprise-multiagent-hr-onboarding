"""UC-03 persistence on Supabase (integrations/supabase/) against a strict in-memory table client
(tests/unit/fake_tables.py).

The fake enforces the live columns, required fields, keys, foreign keys, CHECK lists and append-only
audit, so these tests
fail when a mapping is wrong. They do NOT touch a real database: no live read or write happens here.
"""

from __future__ import annotations

import sys
import uuid
from datetime import date, timedelta

import pytest

from integrations.supabase import (
    DuplicateKey,
    SharedSupabaseTables,
    StorageError,
    StoreConfigurationError,
    build_stores,
    store_mode,
)
from integrations.supabase.stores import request_key
from src.state.enums import (
    ActorType,
    ApprovalStatus,
    OnboardingStatus,
    RequiredAction,
    SubmissionStatus,
    ValidationFinding,
)
from src.state.models import ApprovalRecord, AuditEvent, OnboardingCase
from src.sub_agent.checklist import ChecklistService
from src.sub_agent.extraction import MockDocumentExtractor
from src.sub_agent.models import DocumentValidationRequest
from src.sub_agent.validator import DocumentValidationService
from tests.unit.fake_tables import FakeTables

TODAY = date(2026, 11, 4)
CLIENT = "client-test"
ONBOARDING_CASE = str(uuid.UUID(int=7))  # a real (seeded) onboarding case


def _world():
    t = FakeTables()
    t.seed("client_configs", client_id=CLIENT)
    t.seed("onboarding_cases", case_id=ONBOARDING_CASE, client_id=CLIENT)
    return t, build_stores({"UC03_STORE_MODE": "supabase"}, tables=t)


def _req(case_id=ONBOARDING_CASE, **overrides) -> DocumentValidationRequest:
    data = dict(
        case_id=case_id,
        employee_id="emp-1",
        employee_name_form="Ahmed Al Rashidi",
        offer_accepted=True,
        onboarding_form_approved=True,
        document_type="passport",
        submission_status=SubmissionStatus.RECEIVED,
        extracted_name="Ahmed Al Rashidi",
        expiry_date=TODAY + timedelta(days=900),
        readable=True,
        required_fields_present=True,
        extraction_confidence=0.95,
        evidence_stored=False,
        client_id=CLIENT,
        document_ref="docs/passport-1.pdf",
        requesting_actor_id="hr-user-1",
        requesting_actor_type=ActorType.HR_USER,
        validation_date=TODAY,
    )
    data.update(overrides)
    return DocumentValidationRequest(**data)


def _service(stores):
    return DocumentValidationService(
        case_store=stores.case_store,
        audit_store=stores.audit_store,
        checklist_service=ChecklistService("default"),
        extractor=MockDocumentExtractor(),
        result_store=stores.result_store,
    )


def _register(stores, document_type="passport", ref="docs/passport-1.pdf", checksum="a" * 64):
    return stores.document_registry.register(
        case_ref=ONBOARDING_CASE,
        client_id=CLIENT,
        document_type=document_type,
        document_ref=ref,
        file_name="passport.pdf",
        file_size_bytes=2048,
        mime_type="application/pdf",
        checksum_sha256=checksum,
    )


# ------------------------------------------------------------------ 1. connection initialisation
# / mode selection
def test_memory_is_the_default_and_needs_no_credentials():
    stores = build_stores({})
    assert stores.mode == "memory" and stores.result_store is None
    assert type(stores.case_store).__name__ == "InMemoryCaseStore"


def test_an_unknown_mode_is_refused():
    with pytest.raises(StoreConfigurationError, match="UC03_STORE_MODE"):
        store_mode({"UC03_STORE_MODE": "sqlite"})


def test_supabase_mode_without_settings_stops_and_names_the_variables(monkeypatch):
    common = pytest.importorskip("hr_onboarding_common.platform.supabase_client")  # noqa: F841 - loads its .env; values cleared below
    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ANON_KEY"):
        monkeypatch.setenv(name, "")
    with pytest.raises(StoreConfigurationError) as exc:
        build_stores({"UC03_STORE_MODE": "supabase"})
    assert "SUPABASE_URL" in str(exc.value) and "SUPABASE_SERVICE_ROLE_KEY" in str(exc.value)


def test_importing_the_store_selector_does_not_load_the_shared_client_or_any_env_file():
    for name in [m for m in sys.modules if m.endswith("platform.supabase_client")]:
        del sys.modules[name]
    import importlib

    import integrations.supabase.runtime as runtime

    importlib.reload(runtime)
    runtime.build_stores({})  # memory mode
    assert not [m for m in sys.modules if m.endswith("platform.supabase_client")]


def _repo_env(tmp_path, text):
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    return env_file


def test_repo_env_selects_the_mode_for_any_entry_point(monkeypatch, tmp_path):
    from integrations.supabase.runtime import _adopt_repo_env

    for name in ("UC03_STORE_MODE", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.setenv(name, "")  # recorded, so teardown restores the original state
        monkeypatch.delenv(name)
    _adopt_repo_env(_repo_env(tmp_path, "UC03_STORE_MODE=supabase\nSUPABASE_URL=https://x.test\n"
                                        "SUPABASE_SERVICE_ROLE_KEY=fake\nOTHER=ignored\n"))
    import os

    assert os.environ["UC03_STORE_MODE"] == "supabase" and os.environ["SUPABASE_URL"] == "https://x.test"
    assert "OTHER" not in os.environ


def test_repo_env_never_overrides_the_real_environment_or_the_test_guard(monkeypatch, tmp_path):
    from integrations.supabase.runtime import _adopt_repo_env

    monkeypatch.setenv("UC03_STORE_MODE", "memory")
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.delenv("SUPABASE_URL")
    _adopt_repo_env(_repo_env(tmp_path, "UC03_STORE_MODE=supabase\nSUPABASE_URL=https://x.test\n"))
    import os

    assert os.environ["UC03_STORE_MODE"] == "memory"
    assert "SUPABASE_URL" not in os.environ  # credentials are taken only in supabase mode


def test_supabase_mode_builds_all_stores_over_one_table_client():
    t, stores = _world()
    assert stores.mode == "supabase"
    assert {
        type(s).__name__
        for s in (
            stores.case_store,
            stores.audit_store,
            stores.result_store,
            stores.document_registry,
        )
    } == {
        "SupabaseCaseStore",
        "SupabaseAuditStore",
        "SupabaseValidationResultStore",
        "SupabaseDocumentRegistry",
    }


# ------------------------------------------------------------------ 2/7/8. case creation,
# retrieval, update
async def test_a_case_is_created_linked_read_back_and_updated_with_a_version():
    t, stores = _world()
    case = OnboardingCase(
        case_id=ONBOARDING_CASE,
        employee_id="emp-1",
        client_id=CLIENT,
        role="",
        grade="",
        nationality="",
        location="",
        offer_accepted=True,
        onboarding_form_approved=True,
        employee_name_form="Ahmed Al Rashidi",
    )
    await stores.case_store.save_case(case)
    (row,) = t.rows["uc03_case_state"]
    assert (row["onboarding_case_id"], row["client_id"], row["version"]) == (
        ONBOARDING_CASE,
        CLIENT,
        1,
    )
    loaded = await stores.case_store.get_case(ONBOARDING_CASE)
    assert loaded == case
    await stores.case_store.save_case(
        case.model_copy(
            update={
                "status": OnboardingStatus.HR_REVIEW_REQUIRED,
                "received_documents": {"passport"},
            }
        )
    )
    (row,) = t.rows["uc03_case_state"]
    assert row["version"] == 2 and row["status"] == "HR_REVIEW_REQUIRED"
    assert (await stores.case_store.get_case(ONBOARDING_CASE)).received_documents == {"passport"}


async def test_a_standalone_uc03_case_is_stored_without_a_link():
    t, stores = _world()
    case = OnboardingCase(
        case_id="case-local-1",
        employee_id="emp-2",
        client_id=CLIENT,
        role="",
        grade="",
        nationality="",
        location="",
        offer_accepted=True,
        onboarding_form_approved=True,
        employee_name_form="X",
    )
    await stores.case_store.save_case(case)
    assert t.rows["uc03_case_state"][0]["onboarding_case_id"] is None
    assert t.rows["onboarding_cases"] == [
        {"case_id": ONBOARDING_CASE, "client_id": CLIENT}
    ]  # UC-01's table is only read


async def test_a_concurrent_case_change_is_detected_not_overwritten():
    t, stores = _world()
    case = OnboardingCase(
        case_id="case-c",
        employee_id="e",
        client_id=CLIENT,
        role="",
        grade="",
        nationality="",
        location="",
        offer_accepted=True,
        onboarding_form_approved=True,
        employee_name_form="X",
    )
    await stores.case_store.save_case(case)
    real_update = t.update
    t.update = lambda table, values, filters: (
        [] if table == "uc03_case_state" else real_update(table, values, filters)
    )
    with pytest.raises(StorageError) as exc:
        await stores.case_store.save_case(case)
    assert exc.value.code == "CONCURRENT_MODIFICATION"


# ------------------------------------------------------------------ 3. document records
def test_registering_a_document_is_idempotent_and_maps_the_type():
    t, stores = _world()
    first, again = _register(stores), _register(stores)
    assert first == again and len(t.rows["candidate_documents"]) == 1
    assert t.rows["candidate_documents"][0]["document_type"] == "PASSPORT"


def test_a_document_cannot_be_registered_for_a_case_that_is_not_an_onboarding_case():
    _, stores = _world()
    with pytest.raises(StorageError, match="CASE_NOT_AN_ONBOARDING_CASE"):
        stores.document_registry.register(
            case_ref="case-local",
            client_id=CLIENT,
            document_type="passport",
            document_ref="r",
            file_name="f",
            file_size_bytes=1,
            mime_type="application/pdf",
            checksum_sha256="b" * 64,
        )


# ------------------------------------------------------------------ 4/6. validation results and
# HR referrals
async def test_a_validation_result_is_persisted_with_the_deterministic_decision():
    t, stores = _world()
    _register(stores)
    result = await _service(stores).validate(_req())
    assert result.persistence_status == "PERSISTED"
    (row,) = t.rows["document_validations"]
    assert row["case_id"] == ONBOARDING_CASE and row["uc03_case_ref"] == ONBOARDING_CASE
    assert row["validation_findings"] == [f.value for f in result.validation_findings]
    assert row["required_next_action"] == result.required_next_action.value
    assert row["document_status"] == result.document_status.value
    assert row["uc03_compliance_status"] == result.compliance_item_status.value
    assert row["audit_reference"] == result.audit_reference
    assert row["advisory"] is None  # no advisor configured: nothing non-authoritative to keep
    assert t.rows["candidate_documents"][0]["status"] in ("VALIDATED", "REJECTED")


async def test_a_name_variation_is_persisted_as_an_hr_referral():
    t, stores = _world()
    _register(stores)
    result = await _service(stores).validate(_req(extracted_name="Ahmed Al Rashidi Khalid"))
    (row,) = t.rows["document_validations"]
    assert ValidationFinding.NAME_MISMATCH.value in row["validation_findings"]
    assert (
        row["required_next_action"]
        == RequiredAction.HR_REVIEW.value
        == result.required_next_action.value
    )
    assert row["hr_review_status"] == "PENDING"


@pytest.mark.parametrize(
    "case_id,document_type,register,expected",
    [
        ("case-local-1", "passport", False, "NOT_PERSISTED:CASE_NOT_AN_ONBOARDING_CASE"),
        (ONBOARDING_CASE, "payslips", False, "NOT_PERSISTED:DOCUMENT_TYPE_NOT_IN_SCHEMA"),
        (ONBOARDING_CASE, "passport", False, "NOT_PERSISTED:DOCUMENT_NOT_REGISTERED"),
    ],
)
async def test_a_result_the_shared_schema_cannot_hold_is_reported_not_faked(
    case_id, document_type, register, expected
):
    t, stores = _world()
    result = await _service(stores).validate(_req(case_id=case_id, document_type=document_type))
    assert result.persistence_status == expected and t.rows["document_validations"] == []


# ------------------------------------------------------------------ 5. audit events
async def test_audit_events_are_stored_append_only_and_read_back():
    t, stores = _world()
    _register(stores)
    result = await _service(stores).validate(_req())
    events = await stores.audit_store.get_events(ONBOARDING_CASE)
    assert result.audit_reference in [e.event_id for e in events]
    stored = [r for r in t.rows["audit_events"] if r["action"] == "DOCUMENT_VALIDATED"]
    assert stored and stored[0]["actor_type"] == "HUMAN" and stored[0]["actor_role"] == "hr_user"
    assert stored[0]["case_id"] == ONBOARDING_CASE and stored[0]["client_id"] == CLIENT
    with pytest.raises(StorageError, match="append-only"):
        t.update("audit_events", {"action": "x"}, {"event_id": stored[0]["event_id"]})


async def test_retrying_the_same_audit_event_does_not_duplicate_it():
    t, stores = _world()
    await stores.case_store.save_case(
        OnboardingCase(
            case_id="c1",
            employee_id="e",
            client_id=CLIENT,
            role="",
            grade="",
            nationality="",
            location="",
            offer_accepted=True,
            onboarding_form_approved=True,
            employee_name_form="X",
        )
    )
    ev = AuditEvent(
        event_id=str(uuid.uuid4()),
        case_id="c1",
        actor_id="a",
        actor_type=ActorType.SYSTEM,
        action="X",
        detail={},
    )
    await stores.audit_store.log(ev)
    await stores.audit_store.log(ev)
    assert len(t.rows["audit_events"]) == 1


# ------------------------------------------------------------------ approvals (approval_requests)
async def test_an_approval_round_trips_through_approval_requests():
    t, stores = _world()
    await stores.case_store.save_case(
        OnboardingCase(
            case_id=ONBOARDING_CASE,
            employee_id="e",
            client_id=CLIENT,
            role="",
            grade="",
            nationality="",
            location="",
            offer_accepted=True,
            onboarding_form_approved=True,
            employee_name_form="X",
        )
    )
    rec = ApprovalRecord(
        approval_id="apr-1",
        case_id=ONBOARDING_CASE,
        action_type="SEND_REMINDER",
        actor_id="hr-9",
        actor_type=ActorType.HR_USER,
        status=ApprovalStatus.APPROVED,
    )
    await stores.case_store.save_approval(rec)
    got = await stores.case_store.get_approval(ONBOARDING_CASE, "SEND_REMINDER")
    assert (got.approval_id, got.status, got.actor_id) == ("apr-1", ApprovalStatus.APPROVED, "hr-9")
    (row,) = t.rows["approval_requests"]
    assert (row["workflow_module"], row["outcome"], row["decided_by"], row["requested_by"]) == (
        "uc03",
        "APPROVED",
        "hr-9",
        "uc03-agent",
    )


async def test_an_approval_status_the_schema_cannot_hold_is_refused_not_guessed():
    _, stores = _world()
    await stores.case_store.save_case(
        OnboardingCase(
            case_id=ONBOARDING_CASE,
            employee_id="e",
            client_id=CLIENT,
            role="",
            grade="",
            nationality="",
            location="",
            offer_accepted=True,
            onboarding_form_approved=True,
            employee_name_form="X",
        )
    )
    rec = ApprovalRecord(
        approval_id="apr-2",
        case_id=ONBOARDING_CASE,
        action_type="X",
        actor_id="hr",
        actor_type=ActorType.HR_USER,
        status=ApprovalStatus.NOT_REQUIRED,
    )
    with pytest.raises(ValueError, match="no counterpart"):
        await stores.case_store.save_approval(rec)


# ------------------------------------------------------------------ 9. idempotency
async def test_the_same_request_twice_creates_one_validation_row():
    t, stores = _world()
    _register(stores)
    service = _service(stores)
    first = await service.validate(_req(submission_status=SubmissionStatus.UNDER_REVIEW))
    second = await service.validate(_req(submission_status=SubmissionStatus.UNDER_REVIEW))
    assert first.persistence_status == second.persistence_status == "PERSISTED"
    assert len(t.rows["document_validations"]) == 1
    assert t.rows["document_validations"][0]["request_key"] == request_key(
        _req(submission_status=SubmissionStatus.UNDER_REVIEW)
    )


# ------------------------------------------------------------------ 10/11. database failures are
# never hidden
@pytest.mark.parametrize(
    "op",
    [
        ("insert", "document_validations"),
        ("insert", "audit_events"),
        ("insert", "uc03_case_state"),
        ("select", "candidate_documents"),
    ],
)
async def test_a_database_failure_raises_instead_of_reporting_success(op):
    t, stores = _world()
    _register(stores)
    t.fail.add(op)
    with pytest.raises(StorageError) as exc:
        await _service(stores).validate(_req())
    assert exc.value.code == "DATABASE_ERROR"
    if op == ("insert", "document_validations"):
        assert t.rows["document_validations"] == []  # nothing half-written in that table


def test_the_shared_client_adapter_reduces_errors_to_codes_without_row_data():
    class Boom:
        def table(self, name):
            raise RuntimeError(
                'Supabase HTTP 409 error: {"code":"23505",'
                '"details":"Key (request_key)=(secret-ish) exists"}'
            )

    with pytest.raises(DuplicateKey) as dup:
        SharedSupabaseTables(Boom()).insert("document_validations", {"x": 1})
    assert "secret-ish" not in str(dup.value)

    class Down:
        def table(self, name):
            raise RuntimeError(
                "Supabase HTTP 503 error: upstream for "
                "https://example.invalid/rest/v1 with key sk-test"
            )

    with pytest.raises(StorageError) as err:
        SharedSupabaseTables(Down()).select("audit_events")
    assert (
        err.value.code == "DATABASE_ERROR"
        and "example.invalid" not in str(err.value)
        and "sk-test" not in str(err.value)
    )


# ------------------------------------------------------------------ 12. API integration with
# database-backed stores
def test_the_api_uses_the_configured_database_stores():
    from fastapi.testclient import TestClient

    from src.state.store import configure_stores

    t, stores = _world()
    _register(stores)
    configure_stores(stores.case_store, stores.audit_store, stores.result_store)
    try:
        from src.main import app

        configure_stores(
            stores.case_store, stores.audit_store, stores.result_store
        )  # main's import installs memory again
        client = TestClient(app)
        response = client.post("/onboarding/uc03/validate", json=_req().model_dump(mode="json"))
        assert response.status_code == 200, response.text
        assert response.json()["persistence_status"] == "PERSISTED"
        assert len(t.rows["document_validations"]) == 1 and t.rows["audit_events"]
        trail = client.get(
            f"/onboarding/uc03/audit/{ONBOARDING_CASE}", params={"requesting_actor_id": "hr-user-1"}
        )
        assert trail.status_code == 200 and any(
            e["action"] == "DOCUMENT_VALIDATED" for e in trail.json()
        )
    finally:
        from src.state.store import InMemoryAuditStore, InMemoryCaseStore

        configure_stores(InMemoryCaseStore(), InMemoryAuditStore(), None)


def test_the_api_reports_a_database_failure_as_an_error_not_a_success():
    from fastapi.testclient import TestClient

    from src.state.store import InMemoryAuditStore, InMemoryCaseStore, configure_stores

    t, stores = _world()
    _register(stores)
    t.fail.add(("insert", "document_validations"))
    try:
        from src.main import app

        configure_stores(stores.case_store, stores.audit_store, stores.result_store)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/onboarding/uc03/validate", json=_req().model_dump(mode="json"))
        assert response.status_code == 500 and "PERSISTED" not in response.text
    finally:
        configure_stores(InMemoryCaseStore(), InMemoryAuditStore(), None)
