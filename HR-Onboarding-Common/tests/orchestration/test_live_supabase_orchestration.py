"""OPT-IN, LIVE: the orchestration workflow persisted in the real Supabase `case_orchestration_state`.

Skipped unless ORCH_SUPABASE_LIVE=1. Needs SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY (SUPABASE_SCHEMA
optional) and an authorized synthetic test case: ORCH_LIVE_TEST_CLIENT_ID + ORCH_LIVE_TEST_CASE_ID, an
existing `onboarding_cases` row of that tenant. It WRITES one `case_orchestration_state` row for that case
(a previous row is deleted first only if this orchestrator wrote it -- any other row makes the test skip).

What is live and what is not:
  live    Supabase: every orchestration-state read and write (SupabaseCaseStateStore over HTTPS), the
          authoritative `onboarding_cases` row (tenant, employee number, joining date) read before intake
  double  UC01 is a link to that authoritative row (UC01's own Supabase writes are paused pending DBA
          decisions B1-B9, UC01 docs/supabase-integration-status.md); UC02-UC05 are the contract-shaped
          doubles of _workflow.py; the LLM is a FakeLLM. No OpenRouter call is made here.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from _helpers import UC_ROOTS
from _lifecycle import FakeUc01Worker, Pool
from _workflow import GatedUc03Double, GatedUc04Worker, PlanDouble, ReplanDouble, Uc05Double, _Gate, status
from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.config import Settings
from hr_onboarding_common.llm import FakeLLM
from hr_onboarding_common.orchestration.case_store import (CaseOwnershipError, InMemoryCaseStateStore,
                                                           StateVersionConflict)
from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.notifications import MockNotificationOutbox
from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent
from hr_onboarding_common.orchestration.state import Stage, StageStatus, WorkflowStatus
from hr_onboarding_common.orchestration.supabase_state_store import FORMAT, SupabaseCaseStateStore, row_to_state
from hr_onboarding_common.orchestration.workflow_models import validate_workflow_state
from hr_onboarding_common.platform.hitl import Decision

S, ST, W = Stage, StageStatus, WorkflowStatus
LIVE = os.environ.get("ORCH_SUPABASE_LIVE") == "1"
TENANT = os.environ.get("ORCH_LIVE_TEST_CLIENT_ID", "")
CASE = os.environ.get("ORCH_LIVE_TEST_CASE_ID", "")
pytestmark = pytest.mark.skipif(
    not (LIVE and TENANT and CASE and os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY")),
    reason="opt-in live test: ORCH_SUPABASE_LIVE=1, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, "
           "ORCH_LIVE_TEST_CLIENT_ID, ORCH_LIVE_TEST_CASE_ID")
COMMON_HOME = Path(__file__).resolve().parents[2]
HR = {"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": TENANT}
HR_2 = {"on_behalf_of": "user:hr-admin-02", "roles": ["hr_admin"], "client_id": TENANT}
SCHEDULER = {"on_behalf_of": "svc:joining-day-scheduler", "roles": ["onboarding_scheduler"], "client_id": TENANT}
OTHER_TENANT = "SYN-OTHER-TENANT"


class Rest:
    """Direct, read-mostly REST access for verification (never through the store under test)."""

    def __init__(self):
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        schema = os.environ.get("SUPABASE_SCHEMA") or "public"
        self.client = httpx.Client(base_url=os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1", timeout=20,
                                   headers={"apikey": key, "Authorization": f"Bearer {key}",
                                            "Accept-Profile": schema, "Content-Profile": schema})

    def case(self) -> dict | None:
        rows = self.client.get("/onboarding_cases", params={
            "select": "case_id,client_id,employee_no,joining_date,is_deleted", "case_id": f"eq.{CASE}",
            "client_id": f"eq.{TENANT}"}).json()
        return rows[0] if rows else None

    def row(self) -> dict | None:
        rows = self.client.get("/case_orchestration_state", params={"select": "*", "case_id": f"eq.{CASE}"}).json()
        return rows[0] if rows else None

    def delete_own_row(self) -> None:
        self.client.delete("/case_orchestration_state", params={
            "case_id": f"eq.{CASE}", "client_id": f"eq.{TENANT}", "orchestrator_context->>format": f"eq.{FORMAT}"})


class LinkToAuthoritativeCase(FakeUc01Worker):
    """Intake linked to the authoritative onboarding_cases row: its uuid, employee number and joining date."""

    def __init__(self, case: dict):
        super().__init__(employee_id=case["employee_no"], joining_date=case["joining_date"])
        self.case = case

    def call(self, op, payload, **kwargs):
        response = super().call(op, payload, **kwargs)
        if response.result and response.result.get("case"):
            response.result["case"]["case_id"] = self.case["case_id"]
        return response


class TenantUc05Double(Uc05Double):
    def orientation(self, state, deps):
        new = super().orientation(state, deps)
        if new.get("employee_ref"):
            new["employee_ref"] = {**new["employee_ref"], "client_id": state["client_id"]}
        return new


def live_store() -> SupabaseCaseStateStore:
    return SupabaseCaseStateStore.from_env(os.environ, provisional=InMemoryCaseStateStore())


def orchestrator(case: dict, tmp_path: Path):
    """One orchestrator process over the live store."""
    gate = _Gate()
    uc05 = TenantUc05Double(gate)
    adapters = {S.ORIENTATION_SCHEDULING.value: uc05.orientation, S.BUDDY_ASSIGNMENT.value: uc05.buddy,
                S.TEAM_INTRODUCTION.value: uc05.team_intro, S.JOINING_DAY_WELCOME.value: uc05.welcome,
                S.PLAN_TASKS.value: PlanDouble(gate), S.REPLAN_TASKS.value: ReplanDouble(gate),
                S.VALIDATE_DOCUMENTS.value: GatedUc03Double(gate)}
    deps = OrchestratorDeps(ctx=None, worker_pool=Pool(uc01=LinkToAuthoritativeCase(case),
                                                       uc04=GatedUc04Worker("READY", gate)),
                            uc03_repo_root=UC_ROOTS["uc03"], stage_adapters=adapters,
                            notifier=MockNotificationOutbox(), case_states=live_store(),
                            uc03_document_store_path=tmp_path / "uc03_documents.db")
    ctx = AgentContext.build(agent_home=COMMON_HOME, llm=FakeLLM(), connector=deps,
                             settings=Settings(agent_home=COMMON_HOME, data_dir=tmp_path), persist=True)
    deps.ctx = ctx
    return OnboardingOrchestratorAgent(ctx), ctx


def case_input(case: dict) -> OnboardingCaseInput:
    return OnboardingCaseInput(
        client_id=TENANT, hr_officer_id=HR["on_behalf_of"], offer_accepted=True, offer_reference=f"OFR-LIVE-{CASE[:8]}",
        offer_acceptance_event_id=f"EVT-LIVE-{CASE[:8]}", candidate_name="Synthetic Candidate",
        candidate_email="synthetic.candidate@example.invalid", designation="Software Engineer",
        joining_date=case["joining_date"], id_number="SYN-ID-LIVE", role_id="software_engineer",
        department="Engineering", manager_id="TL-SYN-001", location="Dubai", project="platform",
        uc04_client_config_ref=str(UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"))


def documents(state) -> list[dict]:
    return [{"document_type": o["document_type"], "document_ref": f"SYN-DOC-{o['document_type']}-live",
             "metadata": {"readable": True, "required_fields_present": True, "expiry_date": "2031-01-01",
                          "evidence_stored": True, "extracted_name": "Synthetic Candidate",
                          "attestation_confirmed": True}}
            for o in state["document_collection"]["outstanding"]]


def test_live_workflow_persists_every_stage_in_case_orchestration_state(tmp_path):
    rest = Rest()
    case = rest.case()
    assert case is not None and not case["is_deleted"], "the authorized test case must exist in its tenant"
    existing = rest.row()
    if existing is not None:
        if (existing.get("orchestrator_context") or {}).get("format") != FORMAT:
            pytest.skip("the test case has an orchestration row this orchestrator did not write; not touching it")
        rest.delete_own_row()
    evidence: list[str] = []

    # Process 1: intake (linked to the authoritative case), UC02 planning, UC03 collection -> waits for documents.
    agent, _ = orchestrator(case, tmp_path)
    state = agent.advance_case(case_input(case), case_id=None, actor=HR)
    assert state["case_id"] == CASE and state["joining_date"] == case["joining_date"]
    row = rest.row()
    assert row["client_id"] == TENANT and row["orchestrator_context"]["format"] == FORMAT
    assert row["stage_statuses"]["intake"] == row["stage_statuses"]["plan_tasks"] == "complete"
    assert row["workflow_status"] == W.WAITING_FOR_INPUT.value
    evidence.append(f"after intake/plan/collection: version={row['version']} workflow_status={row['workflow_status']}")

    # Optimistic concurrency, enforced by the live database.
    a, b = live_store(), live_store()
    sa, sb = a.load(CASE), b.load(CASE)
    sb["state_version"] += 1
    b.save(sb)
    sa["state_version"] += 1
    with pytest.raises(StateVersionConflict):
        a.save(sa)
    evidence.append(f"CAS: second writer from version {sa['state_version'] - 1} refused; row version={rest.row()['version']}")

    # Tenant isolation, enforced in the queries.
    assert live_store().load(CASE, client_id=OTHER_TENANT) is None
    intruder = live_store()
    foreign = intruder.load(CASE)
    foreign["client_id"], foreign["state_version"] = OTHER_TENANT, foreign["state_version"] + 1
    before = rest.row()["version"]
    with pytest.raises(CaseOwnershipError):
        intruder.save(foreign)
    assert rest.row()["version"] == before and rest.row()["client_id"] == TENANT
    with pytest.raises(KeyError):
        agent.get_case(CASE, client_id=OTHER_TENANT)

    # Process 2: a restarted orchestrator continues from the committed row only.
    agent, _ = orchestrator(case, tmp_path)
    loaded = agent.get_case(CASE, client_id=TENANT)
    state = agent.submit_documents(CASE, documents=documents(loaded), actor=HR)
    row = rest.row()
    for stage in ("document_submission", "validate_documents", "equipment_access", "orientation_scheduling",
                  "buddy_assignment", "team_introduction"):
        assert row["stage_statuses"][stage] == "complete", stage
        assert row["stage_outcomes"][stage]["status"] == "complete", stage
    assert {b: s["status"] for b, s in row["branch_statuses"].items()} == {
        "uc04_equipment_access": "completed", "uc05_orientation": "completed"}
    assert row["stage_statuses"]["day_one_readiness"] == "awaiting_approval"
    evidence.append(f"after documents + parallel UC04/UC05: version={row['version']} "
                    f"workflow_status={row['workflow_status']}")

    # Process 3: signoff by a second HR admin, then the joining-day welcome on the authoritative date.
    agent, ctx = orchestrator(case, tmp_path)
    (item,) = ctx.hitl.list(status=Decision.PENDING, gate="day_one_signoff", workflow_instance=CASE, client_id=TENANT)
    agent.decide_approval(CASE, item.id, decision="approved", actor=HR_2)
    from datetime import date

    state = agent.trigger_joining_day(CASE, as_of=date.fromisoformat(case["joining_date"]), actor=SCHEDULER)
    row = rest.row()
    assert (row["workflow_status"], row["overall_status"]) == ("completed", "complete")
    assert row["pending_actions"] == [] and row["version"] == state["state_version"]
    assert status(row_to_state(row), S.JOINING_DAY_WELCOME) == ST.COMPLETE.value
    validate_workflow_state(row_to_state(row))
    evidence.append(f"completed: version={row['version']} workflow_status={row['workflow_status']} "
                    f"completed_stages={len(row['completed_stages'])}")
    print("\nLIVE SUPABASE EVIDENCE (case " + CASE + ", tenant " + TENANT + "):\n  " + "\n  ".join(evidence))
