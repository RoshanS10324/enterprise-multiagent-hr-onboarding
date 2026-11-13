"""Test world for the full-lifecycle orchestration tests: the REAL orchestrator graph and the REAL
UC05 worker (UC05's own code, its simulated connectors, FakeLLM), with contract-compatible doubles
in front of UC01-UC04:

  UC01  FakeUc01Worker -- answers `open_onboarding_case` with UC01's OpenCaseResult shape; the real
        UC01 adapter translates it (only the UC01 process is replaced).
  UC02  initial task planning: a stage double in the real adapter's output shape (`task_plan` and a
        versioned `onboarding_plan` whose documents come from UC03's real catalog). The real UC02 is
        covered by test_uc02_real_orchestration.py and test_uc02_uc03_replanning_workflow.py. This is
        NOT a UC02 integration result.
  UC03  document collection: the REAL adapter (UC03's real DocumentCollectionService) and the mock
        notification outbox. Validation: a stage double -- per-document verdicts aggregated by the
        real UC03 adapter's `apply_validation_results`; the real UC03 is covered by
        test_uc03_adapter.py and test_state_driven_workflow.py. Valid/invalid is switchable to test
        waiting and correction.
  UC04  FakeUc04Worker -- answers `evaluate_orchestrated` with UC04's OrchestratorResponse shape;
        the real UC04 adapter translates it.

`World.start()` starts a case and, once it waits for documents, supplies a synthetic set -- the
state-driven workflow's document step -- so the UC05 tests begin where the branches fan out.
Nothing here is presented as a real end-to-end integration of UC01-UC04.
"""

from __future__ import annotations

import copy
import os
from datetime import date
from pathlib import Path
from typing import Any

from _helpers import UC_ROOTS, require_uc, venv_python
from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.config import Settings
from hr_onboarding_common.llm import FakeLLM
from hr_onboarding_common.orchestration.adapters._shared import with_stage_status
from hr_onboarding_common.orchestration.adapters.uc02_tasks import catalog_requirements, plan_record, \
    record_initial_plan
from hr_onboarding_common.orchestration.adapters.uc03_documents import apply_validation_results
from hr_onboarding_common.orchestration.contracts import Actor, OnboardingCaseInput, WorkerResponse
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent
from hr_onboarding_common.orchestration.state import Stage, StageStatus
from hr_onboarding_common.orchestration.tenant_config import TenantConfig
from hr_onboarding_common.orchestration.workers.config import WorkerConfig
from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker
from hr_onboarding_common.platform.hitl import Decision

COMMON_HOME = Path(__file__).resolve().parents[2]
CLIENT = "CLIENT-TEST-A"
EMPLOYEE, TEAM_LEAD_ID, BUDDY_ID = "EMP-TEST-001", "TL-TEST-001", "EMP-TEST-050"  # UC05's synthetic directory
JOINING = date(2026, 12, 1)

HR = {"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": CLIENT}
HR_2 = {"on_behalf_of": "user:hr-admin-02", "roles": ["hr_admin"], "client_id": CLIENT}
TEAM_LEAD = {"on_behalf_of": TEAM_LEAD_ID, "roles": ["team_lead"], "client_id": CLIENT}
BUDDY = {"on_behalf_of": BUDDY_ID, "roles": ["buddy"], "client_id": CLIENT}
SCHEDULER = {"on_behalf_of": "svc:joining-day-scheduler", "roles": ["onboarding_scheduler"], "client_id": CLIENT}
OTHER_TENANT_HR = {"on_behalf_of": "user:hr-admin-b", "roles": ["hr_admin"], "client_id": "CLIENT-TEST-B"}
BUDDY_CANDIDATE = {"employee_id": BUDDY_ID, "full_name": "Robin Park", "client_id": CLIENT, "team": "Platform"}
# UC05's synthetic seed configures welcome templates/branding for CLIENT-ACME and CLIENT-GLOBEX only;
# without one, UC05's joining-day run stops with MISSING_DATA (pinned by a test of its own).
CLIENT_CONFIG = {"client_id": CLIENT, "name": "Test Client A", "enabled_languages": ["en"], "default_language": "en",
                 "branding": {"sender_name": "Test A People Team"},
                 "templates": {"welcome": {"template_id": "welcome", "language": "en",
                                           "subject": "Welcome to {sender_name}, {full_name}!",
                                           "body": "Dear {full_name},\n\nWelcome aboard as {role_title}, "
                                                   "starting {start_date}."}}}


def start_uc05_worker(**env: str) -> UcWorker:
    """A real UC05 worker. `env` is applied to its process only (e.g. UC05_WORKER_DATA_DIR)."""
    require_uc("uc05")
    os.environ["HR_ORCH_WORKER_TEST_HOOKS"] = "1"  # TEST ONLY: enables __test_hooks__ on UC05's simulated connectors
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        worker = UcWorker(WorkerConfig(uc_id="uc05", python_executable=venv_python(UC_ROOTS["uc05"]),
                                       src_dir=UC_ROOTS["uc05"] / "src"))
        worker.start()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k)
            else:
                os.environ[k] = v
    return worker


class FakeUc01Worker:
    is_alive = True

    def __init__(self, *, employee_id: str | None = EMPLOYEE, joining_date: str | None = "2026-12-01"):
        self.employee_id, self.joining_date, self.calls = employee_id, joining_date, 0
        self.payloads: list[dict] = []

    def call(self, op, payload, *, actor, run_id=None, idempotency_key=None, timeout_s=None) -> WorkerResponse:
        assert op == "open_onboarding_case"
        self.calls += 1
        self.payloads.append(payload)
        if not payload["offer_accepted"]:  # UC01 holds an unverified acceptance
            return WorkerResponse(run_id=run_id or "r", ok=True, result={
                "outcome": "HELD", "case": None, "approvals_created": [], "reasons": ["offer acceptance unverified"]})
        case = {"case_id": f"UC01-{payload['request_id']}", "employee_no": self.employee_id,
                "joining_date": self.joining_date, "client_id": payload["client_id"]}
        return WorkerResponse(run_id=run_id or "r", ok=True, result={
            "outcome": "CASE_OPENED", "case": case, "approvals_created": [], "reasons": []})


class FakeUc04Worker:
    """UC04's `evaluate_orchestrated` contract as UC04's service implements it (BR-21): the same
    idempotency key with the same payload replays the stored result while nothing it depended on has
    changed -- here `overall_status` stands for UC04's own case state, so changing it is a change --;
    the same key with a different payload is refused (IDEMPOTENCY_CONFLICT); a later evaluation of a
    case whose joining date changed reports UC04's EC-11 reassessment. `calls` counts every call,
    `evaluations` only those that evaluated (UC04's side effects), `payloads`/`keys` what each carried."""

    is_alive = True

    def __init__(self, overall_status: str = "READY"):
        self.overall_status, self.calls, self.evaluations = overall_status, 0, 0
        self.payloads: list[dict] = []
        self.keys: list[str | None] = []
        self._results: dict[str, tuple[str, str, dict]] = {}
        self._joining: dict[str, str] = {}

    def call(self, op, payload, *, actor, run_id=None, idempotency_key=None, timeout_s=None) -> WorkerResponse:
        assert op == "evaluate_orchestrated"
        self.calls += 1
        self.payloads.append(payload)
        self.keys.append(idempotency_key)
        fingerprint = repr(sorted((k, v) for k, v in payload["input"].items() if k != "config"))
        if idempotency_key in self._results:
            stored_fingerprint, stored_state, stored = self._results[idempotency_key]
            if stored_fingerprint != fingerprint:
                return WorkerResponse(run_id=run_id or "r", ok=False, error_code="IDEMPOTENCY_CONFLICT",
                                      error_detail="The idempotency key was already used with a different payload.")
            if stored_state == self.overall_status:
                return WorkerResponse(run_id=run_id or "r", ok=True, result=copy.deepcopy(stored))
        self.evaluations += 1
        result = self._evaluate(payload)
        if idempotency_key is not None:
            self._results[idempotency_key] = (fingerprint, self.overall_status, copy.deepcopy(result))
        return WorkerResponse(run_id=run_id or "r", ok=True, result=result)

    def _evaluate(self, payload) -> dict:
        case_id, joining = payload["input"]["case_id"], payload["input"]["joining_date"]
        previous = self._joining.get(case_id)
        self._joining[case_id] = joining
        reassessment = ({"required": True, "changed_fields": ["joining_date"], "previous_joining_date": previous}
                        if previous not in (None, joining) else {"required": False, "changed_fields": []})
        blockers = [] if self.overall_status == "READY" else [
            {"code": "APPROVAL_REQUIRED", "reason": "laptop request needs the hiring manager", "owner": "hiring_manager",
             "next_action": "Hiring manager approves the request"}]
        proposals = [] if self.overall_status == "READY" else [
            {"subject_id": "REQ-1", "requires_approval_by": "hiring_manager", "rationale": "laptop approval"}]
        return {"result": {"case_id": case_id, "overall_status": self.overall_status, "items": [],
                           "blockers": blockers, "reassessment": reassessment}, "proposals": proposals}


class Pool:
    def __init__(self, **workers):
        self.workers = workers

    def get(self, uc_id):
        return self.workers[uc_id]


class World:
    """One orchestrator over the shared UC05 worker, with switchable doubles and call counters."""

    def __init__(self, uc05_worker, *, case_states=None, checkpointer=None, persist_dir: Path | None = None,
                 uc01: FakeUc01Worker | None = None, uc04: FakeUc04Worker | None = None, real_uc02: bool = False,
                 tenants: TenantConfig | None = None):
        self.uc05 = uc05_worker
        self.uc01 = uc01 or FakeUc01Worker()
        self.uc04 = uc04 or FakeUc04Worker()
        self.plan_calls = self.docs_calls = 0
        self.plan_ok, self.docs_valid = True, True
        adapters = {Stage.VALIDATE_DOCUMENTS.value: self._docs_double}
        if not real_uc02:
            adapters[Stage.PLAN_TASKS.value] = self._plan_double
        kwargs: dict[str, Any] = {} if case_states is None else {"case_states": case_states}
        if tenants is not None:
            kwargs["tenants"] = tenants
        self.deps = OrchestratorDeps(ctx=None, worker_pool=Pool(uc01=self.uc01, uc04=self.uc04, uc05=uc05_worker),
                                     uc03_repo_root=UC_ROOTS["uc03"], checkpointer=checkpointer,
                                     stage_adapters=adapters, **kwargs)
        settings = Settings(agent_home=COMMON_HOME, data_dir=persist_dir) if persist_dir else Settings(agent_home=COMMON_HOME)
        self.ctx = AgentContext.build(agent_home=COMMON_HOME, llm=FakeLLM(), connector=self.deps, settings=settings,
                                      persist=persist_dir is not None)
        self.deps.ctx = self.ctx
        self.agent = OnboardingOrchestratorAgent(self.ctx)

    # ---- contract-compatible stage doubles (adapter-output shape of UC02 / UC03)
    def _plan_double(self, state, deps):
        self.plan_calls += 1
        if not self.plan_ok:
            new = with_stage_status(state, Stage.PLAN_TASKS, StageStatus.BLOCKED)
            new["error_code"], new["error_detail"] = "MISSING_DATA", "UC02 precondition failed (double)"
            new["next_action"] = "hr_review:plan_tasks:precondition_failed"
            return new
        new = with_stage_status(state, Stage.PLAN_TASKS, StageStatus.COMPLETE)
        new["task_plan"] = {"onboarding_id": "ONB-DOUBLE", "created": True, "tasks": [], "ready": [], "blocked": [],
                            "plan_version": 1}
        record_initial_plan(new, plan_record(state, plan_id="ONB-DOUBLE", plan_version=1,
                                             required_documents=catalog_requirements(state, deps),
                                             due_date=state.get("joining_date"), task_count=0, attributes=[],
                                             source="plan_double"))
        return new

    def _docs_double(self, state, deps):
        """Every submitted document verified (UC03: compliance COMPLETE after HR approval), or -- when
        docs_valid is False -- the passport expired (UC03: REJECTED, SEND_REPLACEMENT_REQUEST)."""
        self.docs_calls += 1
        results = {}
        for doc_type, doc in state["submitted_documents"].items():
            invalid = doc_type == "passport" and not self.docs_valid
            results[doc_type] = {
                "document_status": "REJECTED" if invalid else "UNDER_REVIEW",
                "compliance_item_status": "BLOCKED" if invalid else "COMPLETE",
                "findings": ["EXPIRED_DOCUMENT"] if invalid else ["VALID"],
                "required_next_action": "SEND_REPLACEMENT_REQUEST" if invalid else "NONE",
                "classification": "correction_required" if invalid else "verified",
                "revision": doc["revision"], "review_decision": None,
                "detail": f"Document type: {doc_type} (double)"}
        return apply_validation_results(state, results)

    # ---- driving
    def case_input(self, **over) -> OnboardingCaseInput:
        base = dict(client_id=CLIENT, hr_officer_id=HR["on_behalf_of"], offer_accepted=True,
                    offer_reference="OFR-T-001", offer_acceptance_event_id="EVT-001", candidate_name="Alex Morgan",
                    candidate_email="alex.morgan@example.com", designation="Software Engineer",
                    joining_date="2026-12-01", id_number="ID-T-001", role_id="software_engineer",
                    department="Engineering", manager_id=TEAM_LEAD_ID, location="Dubai", project="platform",
                    uc04_client_config_ref=str(UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"))
        base.update(over)
        return OnboardingCaseInput(**base)

    def start(self, **over):
        """Start (or re-start) a case; if it then waits for documents, supply a synthetic set."""
        state = self.agent.advance_case(self.case_input(**over), case_id=None, actor=HR)
        if state["stage_statuses"][Stage.DOCUMENT_SUBMISSION.value] == StageStatus.WAITING_FOR_INPUT.value:
            state = self.supply_documents(state)
        return state

    def supply_documents(self, state, doc_types: list[str] | None = None, *, tag: str = "1", actor=HR):
        doc_types = doc_types or [o["document_type"] for o in state["document_collection"]["outstanding"]]
        documents = [{"document_type": t, "document_ref": f"SYN-DOC-{t}-{tag}",
                      "metadata": {"readable": True, "required_fields_present": True, "expiry_date": "2031-01-01",
                                   "evidence_stored": True, "attestation_confirmed": True}} for t in doc_types]
        return self.agent.submit_documents(state["case_id"], documents=documents, actor=actor)

    def pending(self, state, stage: Stage) -> str:
        (ref,) = [p["reference"] for p in state["pending_approvals"] if p.get("stage") == stage.value]
        return ref

    def gate_item(self, case_id: str, gate: str) -> str:
        (item,) = self.ctx.hitl.list(status=Decision.PENDING, gate=gate, workflow_instance=case_id, client_id=CLIENT)
        return item.id

    def decide(self, state, stage: Stage, decision="approved", actor=HR, **fields):
        if decision in ("rejected", "edited"):
            fields.setdefault("reason_code", "timing")
        return self.agent.decide_approval(state["case_id"], self.pending(state, stage), decision=decision,
                                          actor=actor, **fields)

    def hook(self, action: str, **fields) -> dict:
        response = self.uc05.call("__test_hooks__", {"action": action, **fields},
                                  actor=Actor(on_behalf_of="test", roles=["hr_admin"], client_id=CLIENT))
        assert response.ok, response.error_detail
        return response.result

    def uc05_snapshot(self) -> dict:
        response = self.uc05.call("case_snapshot", {"employee_id": EMPLOYEE},
                                  actor=Actor(on_behalf_of="test", roles=["hr_admin"], client_id=CLIENT))
        assert response.ok, response.error_detail
        return response.result

    # ---- the happy path, step by step
    def to_buddy_pending(self):
        return self.decide(self.start(), Stage.ORIENTATION_SCHEDULING)

    def to_team_intro_pending(self):
        return self.decide(self.to_buddy_pending(), Stage.BUDDY_ASSIGNMENT)

    def to_signoff_pending(self):
        return self.decide(self.to_team_intro_pending(), Stage.TEAM_INTRODUCTION, actor=TEAM_LEAD)

    def to_welcome_waiting(self):
        state = self.to_signoff_pending()
        return self.agent.decide_approval(state["case_id"], self.gate_item(state["case_id"], "day_one_signoff"),
                                          decision="approved", actor=HR_2)

    def to_welcome_pending(self):
        state = self.to_welcome_waiting()
        return self.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=SCHEDULER)


def status(state, stage: Stage) -> str:
    return state["stage_statuses"][stage.value]


def code(state, stage: Stage) -> str | None:
    return (state["stage_errors"].get(stage.value) or {}).get("code")
