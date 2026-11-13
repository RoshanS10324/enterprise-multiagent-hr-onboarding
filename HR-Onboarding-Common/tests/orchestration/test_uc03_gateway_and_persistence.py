"""Orchestrated UC03 with the shared LLM client and UC03's store selection (offline: FakeLLM from build_llm, and a strict
in-memory table client standing in for Supabase). No network, no database.

What is proved here:
  * the orchestrator hands UC03 its OWN shared client (AgentContext.llm, built by build_llm) as an advisor;
  * every authoritative document result is identical with and without that client;
  * UC03's store selection is honoured by the orchestrator, and its persistence status reaches the orchestrator state.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from types import SimpleNamespace

import pytest

from _helpers import UC_ROOTS, require_uc
from hr_onboarding_common.config import Settings
from hr_onboarding_common.llm import FakeLLM, build_llm
from hr_onboarding_common.orchestration.adapters import uc03_documents
from hr_onboarding_common.orchestration.adapters.uc03_documents import call_uc03_validate_documents
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import new_state

GOOD = {"readable": True, "required_fields_present": True, "expiry_date": "2031-01-01", "evidence_stored": True,
        "extracted_name": "Arun Kumar", "attestation_confirmed": True}
TYPES = ("signed_offer_letter", "passport", "degree_certificate", "experience_letter", "payslips", "nda", "policy_acknowledgements")
AUTHORITATIVE = ("document_status", "compliance_item_status", "hr_decision", "evidence_status", "findings", "required_next_action")


def _state(case_id="C1", client_id="CL1", name="Arun Kumar"):
    state = new_state(case_id=case_id, client_id=client_id, correlation_id="corr1",
                      actor={"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"]},
                      intake_trigger={"candidate_name": name, "documents": {}},
                      uc02_role_context={"role_id": "engineer", "grade": "G1", "nationality": "IN", "location": "Dubai"})
    state["employee_id"] = "EMP-001"
    state["submitted_documents"] = {t: {"document_type": t, "document_ref": f"SYN-{t}", "revision": 1, "metadata": dict(GOOD)}
                                    for t in TYPES}
    return state


def _deps(llm=None):
    require_uc("uc03")
    ctx = SimpleNamespace(llm=llm) if llm is not None else None
    return OrchestratorDeps(ctx=ctx, worker_pool=None, uc03_repo_root=UC_ROOTS["uc03"])


def _decisions(result):
    return {t: {k: r[k] for k in AUTHORITATIVE} for t, r in result["document_validation_result"].items()}


def test_the_orchestrator_passes_its_shared_client_from_build_llm_into_uc03():
    llm = build_llm(Settings(), environ={"LLM_MODE": "offline"})  # the shared factory, offline mode
    assert isinstance(llm, FakeLLM)
    deps = _deps(llm)
    result = call_uc03_validate_documents(_state(), deps)
    assert {"uc03.draft_communication", "uc03.name_check"} <= set(llm.handlers)  # UC03's offline handlers added to the SHARED client
    advisor = uc03_documents._uc03_advisor(deps)
    assert type(advisor).__name__ == "GatewayAdvisor" and advisor._llm is llm
    assert result["document_validation_result"]


def test_authoritative_document_results_are_identical_with_and_without_the_shared_client():
    without = call_uc03_validate_documents(_state(), _deps())
    with_llm = call_uc03_validate_documents(_state(), _deps(build_llm(Settings(), environ={"LLM_MODE": "offline"})))
    assert _decisions(with_llm) == _decisions(without)


def test_a_failing_shared_client_never_changes_a_document_result():
    class Down(FakeLLM):
        def structured(self, **kwargs):
            from hr_onboarding_common.llm import LLMError
            raise LLMError("provider down")

    without = call_uc03_validate_documents(_state(name="Arun K Kumar"), _deps())
    with_llm = call_uc03_validate_documents(_state(name="Arun K Kumar"), _deps(Down()))
    assert _decisions(with_llm) == _decisions(without)


def test_without_a_context_uc03_runs_without_an_advisor():
    assert uc03_documents._uc03_advisor(_deps()) is None


# ------------------------------------------------------------------ UC03 store selection through the orchestrator
def _uc03_fake_tables():
    """UC03's strict table fake, loaded by file path (the name `tests` exists in both projects)."""
    path = UC_ROOTS["uc03"] / "tests" / "unit" / "fake_tables.py"
    spec = importlib.util.spec_from_file_location("uc03_fake_tables_for_orchestration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FakeTables()


@pytest.fixture
def supabase_backed(monkeypatch):
    require_uc("uc03")
    uc03_documents._ensure_uc03_importable(UC_ROOTS["uc03"])
    tables = _uc03_fake_tables()
    tables.seed("client_configs", client_id="CL1")
    case_id = str(uuid.UUID(int=42))
    tables.seed("onboarding_cases", case_id=case_id, client_id="CL1")
    runtime = sys.modules.get("integrations.supabase") or __import__("integrations.supabase", fromlist=["build_stores"])
    real = runtime.build_stores
    stores = real({"UC03_STORE_MODE": "supabase"}, tables=tables)
    monkeypatch.setattr(runtime, "build_stores", lambda *a, **k: stores)
    return tables, stores, case_id


def test_the_orchestrator_uses_uc03_database_stores_and_reports_persistence(supabase_backed):
    tables, stores, case_id = supabase_backed
    stores.document_registry.register(case_ref=case_id, client_id="CL1", document_type="passport", document_ref="SYN-passport",
                                      file_name="passport.pdf", file_size_bytes=10, mime_type="application/pdf",
                                      checksum_sha256="c" * 64)
    result = call_uc03_validate_documents(_state(case_id=case_id), _deps())
    statuses = {t: r["persistence_status"] for t, r in result["document_validation_result"].items()}
    assert statuses["passport"] == "PERSISTED"
    assert statuses["payslips"] == "NOT_PERSISTED:DOCUMENT_TYPE_NOT_IN_SCHEMA"  # no counterpart in the shared list, reported
    assert statuses["degree_certificate"] == "NOT_PERSISTED:DOCUMENT_NOT_REGISTERED"  # never registered, reported
    assert len(tables.rows["document_validations"]) == 1
    assert tables.rows["uc03_case_state"] and tables.rows["audit_events"]


def test_a_database_failure_during_orchestration_is_a_technical_failure_not_a_verdict(supabase_backed):
    tables, _, case_id = supabase_backed
    tables.fail.add(("insert", "uc03_case_state"))
    result = call_uc03_validate_documents(_state(case_id=case_id), _deps())
    from hr_onboarding_common.orchestration.state import Stage, StageStatus
    assert result["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] not in (StageStatus.COMPLETE.value,
                                                                             StageStatus.AWAITING_APPROVAL.value)
