"""Test world for the state-driven workflow tests: the REAL orchestrator graph, offline and
deterministic, with no UC worker process:

  UC01    FakeUc01Worker (_lifecycle.py): UC01's OpenCaseResult shape; the real UC01 adapter translates it.
  UC02    PlanDouble: initial planning in the real adapter's output shape -- `task_plan` and a versioned
          `onboarding_plan` whose document requirements come from UC03's REAL catalog; or the real
          adapter (real_uc02=True). ReplanDouble: UC02 replanning in the real adapter's output shape
          (it uses the real adapter's own `check_feedback` / `apply_revised_plan`); _uc02_real.py runs
          the REAL UC02 OnboardingService for both.
  UC03    the REAL document-collection adapter (UC03's real DocumentCollectionService: collection per
          plan version, stored revisions) and the orchestration's MockNotificationOutbox (synthetic --
          nothing is sent). Validation: Uc03Double, per-document verdicts in the real adapter's projection
          shape, aggregated by the real adapter's own `apply_validation_results` with UC03's REAL
          requirement-gap detection; or the REAL UC03 in-process (real_uc03=True).
  UC04    FakeUc04Worker (_lifecycle.py): UC04's OrchestratorResponse shape; the real UC04 adapter
          translates it.
  UC05    Uc05Double: the output shape of the UC05 adapter's stage functions. The real UC05 worker is
          exercised by test_uc05_lifecycle.py / test_orchestrator_api.py.

All data is synthetic. Nothing here is presented as a real UC01-UC05 integration.
"""

from __future__ import annotations

import collections
import copy
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from _helpers import UC_ROOTS
from _lifecycle import FakeUc01Worker, FakeUc04Worker, Pool
from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.config import Settings
from hr_onboarding_common.llm import FakeLLM
from hr_onboarding_common.orchestration.adapters._shared import add_pending_approval, record_stage_error, \
    with_stage_status
from hr_onboarding_common.orchestration.adapters.uc02_replanning import apply_revised_plan, check_feedback
from hr_onboarding_common.orchestration.adapters.uc02_tasks import catalog_requirements, plan_record, \
    record_initial_plan
from hr_onboarding_common.orchestration.adapters.uc03_documents import (apply_validation_results,
                                                                        requirement_gaps_for, validation_failed)
from hr_onboarding_common.orchestration.adapters.uc05_orientation import UC05_AGENT
from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.notifications import MockNotificationOutbox
from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent
from hr_onboarding_common.orchestration.state import Stage, StageStatus
from hr_onboarding_common.platform.hitl import Decision

COMMON_HOME = Path(__file__).resolve().parents[2]
CLIENT = "CLIENT-TEST-A"
CANDIDATE = "Alex Morgan"  # synthetic
HR = {"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": CLIENT}
HR_2 = {"on_behalf_of": "user:hr-admin-02", "roles": ["hr_admin"], "client_id": CLIENT}
OFFICER = {"on_behalf_of": "user:hr-officer-01", "roles": ["hr_onboarding_officer"], "client_id": CLIENT}
HR_OPS = {"on_behalf_of": "user:hr-ops-01", "roles": ["hr_ops"], "client_id": CLIENT}
OTHER_TENANT_HR = {"on_behalf_of": "user:hr-admin-b", "roles": ["hr_admin"], "client_id": "CLIENT-TEST-B"}
SCHEDULER = {"on_behalf_of": "svc:joining-day-scheduler", "roles": ["onboarding_scheduler"], "client_id": CLIENT}
JOINING = date(2026, 12, 1)
# The entry stages of the two parallel branches (state.Branch): the only stages after document
# verification. plan_tasks (UC02) now runs before the documents.
BRANCH_STAGES = (Stage.EQUIPMENT_ACCESS, Stage.ORIENTATION_SCHEDULING)
POST_VERIFICATION_STAGES = BRANCH_STAGES


class SimulatedCrash(BaseException):
    """A process stopping mid-run. Not an Exception, so no `except Exception` in the orchestrator or
    an adapter turns it into a stage status -- like a kill, it leaves only what was written through."""


def synthetic_document(doc_type: str, *, tag: str = "1", extracted_name: str = CANDIDATE, **meta: Any) -> dict:
    """A synthetic document reference with extraction metadata that UC03 accepts as valid."""
    metadata = {"readable": True, "required_fields_present": True, "expiry_date": "2031-01-01",
                "evidence_stored": True, "extracted_name": extracted_name, "attestation_confirmed": True}
    metadata.update(meta)
    return {"document_type": doc_type, "document_ref": f"SYN-DOC-{doc_type}-{tag}", "metadata": metadata}


# --------------------------------------------------------------------------- doubles

_VERDICTS = {  # the real UC03 adapter's projection for each classification (UC03's enum values)
    "verified": ("UNDER_REVIEW", "COMPLETE", ["VALID"], "NONE", "APPROVED"),
    "hr_review_required": ("UNDER_REVIEW", "PENDING_APPROVAL", ["VALID"], "HUMAN_CONFIRMATION", "PENDING"),
    "correction_required": ("REJECTED", "BLOCKED", ["EXPIRED_DOCUMENT"], "SEND_REPLACEMENT_REQUEST", "PENDING"),
    "missing": ("MISSING", "BLOCKED", ["MISSING_DOCUMENT"], "SEND_REMINDER", "PENDING"),
    "blocked": ("RECEIVED", "BLOCKED", ["VALID"], "PREPARE_VISA_PACK", "PENDING"),
}


class Uc03Double:
    """UC03 stand-in at the adapter boundary: per-document verdicts, aggregated by the real
    adapter. `fail_next` makes the next n calls a technical failure."""

    def __init__(self, default: str = "verified"):
        self.default, self.verdicts, self.fail_next, self.calls = default, {}, 0, 0
        self.seen: list[dict[str, int]] = []
        self.plan_versions: list[int | None] = []  # the collection's plan version each call validated against

    def __call__(self, state, deps):
        self.calls += 1
        if self.fail_next:
            self.fail_next -= 1
            return validation_failed(state, ConnectionError("UC03 case store unreachable (double)"))
        required = [d["document_type"] for d in state["document_collection"]["required_documents"] if d["required"]]
        submitted = state["submitted_documents"]
        self.seen.append({t: submitted[t]["revision"] for t in required if t in submitted})
        self.plan_versions.append(state["document_collection"].get("plan_version"))
        results = {}
        for doc_type in required:
            verdict = self.verdicts.get(doc_type, self.default)
            doc_status, compliance, findings, action, hr = _VERDICTS[verdict]
            results[doc_type] = {"document_status": doc_status, "compliance_item_status": compliance,
                                 "hr_decision": hr, "evidence_status": "STORED", "findings": findings,
                                 "required_next_action": action, "audit_reference": f"AUD-{doc_type}",
                                 "detail": f"Document type: {doc_type} | Findings: {', '.join(findings)} (double)",
                                 "revision": submitted[doc_type]["revision"], "review_decision": None,
                                 "classification": verdict}
        gaps, flags = requirement_gaps_for(state, deps)  # UC03's real requirement-gap detection
        return apply_validation_results(state, results, requirement_gaps=gaps, flags=flags)


class _Gate:
    """Instrumentation shared by the plan_tasks, UC04 and UC05 doubles, for proving execution order
    and concurrency from what actually ran:

      events      (stage, "enter" | "exit", perf_counter_ns, thread id) of every call, in order
      violations  a branch that entered while plan_tasks was still running, or before it had finished
      barrier     optional; only the two branches wait on it (BrokenBarrierError after its timeout if
                  they are serialized)
      delay       optional seconds a stage holds its call open, so overlapping intervals are visible
    """

    BRANCHES = ("equipment_access", "orientation_scheduling")

    def __init__(self):
        self.barrier: threading.Barrier | None = None
        self.threads: dict[str, int] = {}
        self.delay: dict[str, float] = {}
        self.events: list[tuple[str, str, int, int]] = []
        self.violations: list[str] = []
        self.uc03_gated = False  # set when the UC03 double records its calls here (the real UC03 does not)
        self._lock = threading.Lock()

    def _record(self, name: str, kind: str) -> None:
        with self._lock:
            if kind == "enter" and name in self.BRANCHES:
                for upstream, required in (("plan_tasks", True), ("validate_documents", self.uc03_gated)):
                    calls = [e for e in self.events if e[0] == upstream]
                    if (required and not calls) or (calls and calls[-1][1] != "exit"):
                        self.violations.append(f"{name} entered while {upstream} had not finished")
                if any(e[0] == "replan_tasks" and e[1] == "enter" for e in self.events) and \
                        [e for e in self.events if e[0] in ("replan_tasks", "validate_documents")][-1][0] != \
                        "validate_documents":
                    self.violations.append(f"{name} entered before UC03 revalidated after the last replan")
            self.events.append((name, kind, time.perf_counter_ns(), threading.get_ident()))

    def wait(self, name: str) -> None:
        """A stage's UC call begins. `threads` keeps the branches' threads (every call's is in `events`)."""
        if name in self.BRANCHES:
            self.threads[name] = threading.get_ident()
        self._record(name, "enter")
        if self.barrier is not None and name in self.BRANCHES:
            self.barrier.wait()
        if self.delay.get(name):
            time.sleep(self.delay[name])

    def done(self, name: str) -> None:
        """A stage's UC call ends."""
        self._record(name, "exit")

    def interval(self, name: str, occurrence: int = -1) -> tuple[int, int]:
        """(enter, exit) perf_counter_ns of one call of `name` (the last by default)."""
        enters = [e[2] for e in self.events if e[0] == name and e[1] == "enter"]
        exits = [e[2] for e in self.events if e[0] == name and e[1] == "exit"]
        return enters[occurrence], exits[occurrence]

    def entered(self, name: str) -> int:
        return sum(1 for e in self.events if e[0] == name and e[1] == "enter")


class PlanDouble:
    """UC02 task planning stand-in (adapter-output shape of call_uc02_plan_tasks). Runs sequentially
    before equipment_access/orientation_scheduling; its call is recorded on the shared gate."""

    def __init__(self, gate: _Gate | None = None):
        self.status, self.calls = StageStatus.COMPLETE, 0
        self.received: list[dict] = []
        self.gate = gate or _Gate()

    def __call__(self, state, deps):
        self.gate.wait("plan_tasks")
        try:
            return self._plan(state, deps)
        finally:
            self.gate.done("plan_tasks")

    def _plan(self, state, deps):
        self.calls += 1
        self.received.append({"employee_id": state.get("employee_id"), "joining_date": state.get("joining_date"),
                              "documents_seen": sorted(state.get("submitted_documents") or {})})
        if self.status == StageStatus.COMPLETE:
            new = with_stage_status(state, Stage.PLAN_TASKS, StageStatus.COMPLETE)
            new["task_plan"] = {"onboarding_id": "ONB-SYN-001", "created": True, "ready": ["hr_record_create"],
                                "blocked": ["it_laptop"], "plan_version": 1, "tasks": [
                                    {"task_id": "T-1", "template_key": "it_laptop", "owner": "it_manager",
                                     "due_date": "2026-11-20", "status": "blocked"}]}
            record_initial_plan(new, plan_record(
                state, plan_id="ONB-SYN-001", plan_version=1, required_documents=catalog_requirements(state, deps),
                due_date="2026-11-24", task_count=1, attributes=[], source="plan_double"))
            return new
        new = with_stage_status(state, Stage.PLAN_TASKS, self.status)
        code = "UC02_PACKAGE_UNIMPORTABLE" if self.status == StageStatus.FAILED else "MISSING_DATA"
        record_stage_error(new, Stage.PLAN_TASKS, code=code, detail=f"{code} (double)",
                           retryable=self.status != StageStatus.FAILED)
        new["next_action"] = f"hr_review:plan_tasks:{code.lower()}"
        return new


class ReplanDouble:
    """UC02 replanning stand-in in the real adapter's output shape: the real adapter's own
    `check_feedback` (missing or stale feedback is refused) and `apply_revised_plan`, with the plan change
    UC03 recommended applied the way UC02's `replan_documents` does (documents added/dropped, attributes
    added, next version, idempotent by feedback id). `status` simulates a refused or failed replan."""

    def __init__(self, gate: _Gate | None = None):
        self.status, self.calls, self.gate = StageStatus.COMPLETE, 0, gate or _Gate()
        self.received: list[dict] = []
        self.applied: dict[str, int] = {}  # feedback id -> plan version it produced (UC02's ledger)

    def __call__(self, state, deps):
        self.gate.wait("replan_tasks")
        try:
            return self._replan(state)
        finally:
            self.gate.done("replan_tasks")

    def _replan(self, state):
        self.calls += 1
        refused = check_feedback(state)
        if refused is not None:
            return refused
        feedback, plan = state["document_validation_feedback"], state["onboarding_plan"]
        self.received.append(copy.deepcopy(feedback))
        if self.status != StageStatus.COMPLETE:
            new = with_stage_status(state, Stage.REPLAN_TASKS, self.status)
            code = "REPLANNING_FAILED" if self.status == StageStatus.BLOCKED else "UC02_REPLANNING_UNAVAILABLE"
            record_stage_error(new, Stage.REPLAN_TASKS, code=code, detail=f"{code} (double)", retryable=True)
            new["next_action"] = f"hr_review:replan_tasks:{code.lower()}"
            return new
        changes = feedback["recommended_task_changes"]
        add = [c["target"] for c in changes if c["action"] == "add_document"]
        drop = {c["target"] for c in changes if c["action"] == "remove_document"}
        attrs = [c["target"] for c in changes if c["action"] == "add_attribute"]
        version = self.applied.setdefault(feedback["feedback_id"], plan["plan_version"] + 1)
        documents = [d for d in plan["required_documents"] if d["document_type"] not in drop] + [
            {"document_type": t, "label": t, "required": True, "reason": "replan (double)"} for t in add]
        added_tasks = [f"ONB-SYN-001#{a}_task" for a in attrs]
        return apply_revised_plan(state, plan_version=version, required_documents=documents, added_tasks=added_tasks,
                                  removed_tasks=[], modified_tasks=[], attributes=[*plan.get("attributes", []), *attrs],
                                  task_count=plan.get("task_count", 0) + len(added_tasks), due_date="2026-11-24",
                                  source="replan_double")


class GatedUc03Double(Uc03Double):
    def __init__(self, gate: _Gate):
        super().__init__()
        self.gate = gate
        gate.uc03_gated = True

    def __call__(self, state, deps):
        self.gate.wait("validate_documents")
        try:
            return super().__call__(state, deps)
        finally:
            self.gate.done("validate_documents")


class GatedUc04Worker(FakeUc04Worker):
    def __init__(self, overall_status: str = "READY", gate: _Gate | None = None):
        super().__init__(overall_status)
        self.gate = gate or _Gate()

    def call(self, op, payload, **kwargs):
        self.gate.wait("equipment_access")
        try:
            return super().call(op, payload, **kwargs)
        finally:
            self.gate.done("equipment_access")


class Uc05Double:
    """UC05 stand-in at the adapter boundary (shape of call_uc05_orientation / _buddy_assignment /
    _team_introduction / _joining_day_welcome results), with the same prerequisites.

    UC05's start date for the case: `start_date=None` (the default) models a UC05 whose record holds
    the case's current joining date. A date string models UC05 still holding that date -- UC05 fixes
    a case's start date at its intake and has no operation to change it -- and orientation then
    answers like the real adapter: BLOCKED, JOINING_DATE_MISMATCH. `received` lists the joining date
    each orientation call was handed."""

    def __init__(self, gate: _Gate | None = None):
        self.status = {"orientation": StageStatus.COMPLETE, "buddy": StageStatus.COMPLETE,
                       "team_intro": StageStatus.COMPLETE}
        self.calls: collections.Counter[str] = collections.Counter()
        self.gate = gate or _Gate()
        self.start_date: str | None = None
        self.received: list[str | None] = []

    def _stage(self, state, stage: Stage, key: str, result_key: str, result: dict, prerequisite: Stage | None):
        self.calls[key] += 1
        if prerequisite is not None and state["stage_statuses"][prerequisite.value] != StageStatus.COMPLETE.value:
            new = with_stage_status(state, stage, StageStatus.BLOCKED)
            record_stage_error(new, stage, code="PREREQUISITE_NOT_MET", detail="prerequisite (double)", retryable=True)
            return new
        status = self.status[key]
        new = with_stage_status(state, stage, status)
        new[result_key] = {**result, "data_origin": "synthetic"}
        if status == StageStatus.AWAITING_APPROVAL:
            add_pending_approval(new, agent=UC05_AGENT, reference=f"apr-double-{key}", role="hr_admin",
                                 description=f"UC05 {key} (double)", stage=stage, gate="scheduling_approval")
            new["next_action"] = f"hr_review:{stage.value}:awaiting_approval"
        elif status != StageStatus.COMPLETE:
            record_stage_error(new, stage, code=f"{key.upper()}_DOUBLE_{status.value.upper()}",
                               detail=f"{key} {status.value} (double)", retryable=status != StageStatus.FAILED)
        return new

    def orientation(self, state, deps):
        self.gate.wait("orientation_scheduling")
        try:
            self.received.append(state.get("joining_date"))
            held = self.start_date or state["joining_date"]
            if held != state["joining_date"]:
                self.calls["orientation"] += 1
                stage = Stage.ORIENTATION_SCHEDULING
                new = with_stage_status(state, stage, StageStatus.BLOCKED)
                new["orientation_schedule_result"] = {**(state.get("orientation_schedule_result") or {}),
                                                      "uc05_case": True, "uc05_start_date": held}
                record_stage_error(new, stage, code="JOINING_DATE_MISMATCH", retryable=True,
                                   detail=f"UC05 holds start date {held} (double)")
                new["next_action"] = "hr_review:orientation_scheduling:joining_date_mismatch"
                return new
            new = self._stage(state, Stage.ORIENTATION_SCHEDULING, "orientation", "orientation_schedule_result",
                              {"uc05_case": True, "session_id": "SES-SYN-1", "session_status": "confirmed",
                               "uc05_start_date": held}, None)
            new["employee_ref"] = {"client_id": CLIENT, "employee_id": state["employee_id"],
                                   "employee_no": state["employee_id"], "source": "shared_scheme", "scheme": "shared"}
            return new
        finally:
            self.gate.done("orientation_scheduling")

    def buddy(self, state, deps):
        return self._stage(state, Stage.BUDDY_ASSIGNMENT, "buddy", "buddy_assignment_result",
                           {"buddy_id": "EMP-SYN-050", "status": "assigned"}, Stage.ORIENTATION_SCHEDULING)

    def team_intro(self, state, deps):
        return self._stage(state, Stage.TEAM_INTRODUCTION, "team_intro", "team_introduction_result",
                           {"session_id": "SES-SYN-2", "session_status": "confirmed"}, Stage.BUDDY_ASSIGNMENT)

    def welcome(self, state, deps):
        self.calls["welcome"] += 1
        stage = Stage.JOINING_DAY_WELCOME
        if state["stage_statuses"][Stage.DAY_ONE_READINESS.value] != StageStatus.COMPLETE.value:
            new = with_stage_status(state, stage, StageStatus.BLOCKED)
            record_stage_error(new, stage, code="PREREQUISITE_NOT_MET", detail="readiness (double)", retryable=True)
            return new
        if not state.get("joining_day_event"):
            new = with_stage_status(state, stage, StageStatus.WAITING)
            new["next_action"] = f"await_joining_day:{state.get('joining_date')}"
            return new
        new = with_stage_status(state, stage, StageStatus.COMPLETE)
        new["welcome_message_result"] = {"draft_id": "DRAFT-SYN-1", "delivery": "delivered", "data_origin": "synthetic"}
        return new


# --------------------------------------------------------------------------- the world


class Flow:
    def __init__(self, *, uc04_status: str = "READY", real_uc02: bool = False, real_uc03: bool = False,
                 plan_adapter: Any | None = None, notifier: MockNotificationOutbox | None = None, case_states=None,
                 checkpointer=None, persist_dir: Path | None = None, real_replan: bool = False):
        self.gate = _Gate()
        self.uc01, self.uc04 = FakeUc01Worker(), GatedUc04Worker(uc04_status, self.gate)
        self.plan, self.uc05 = plan_adapter or PlanDouble(self.gate), Uc05Double(self.gate)
        self.uc03 = Uc03Double() if real_uc03 else GatedUc03Double(self.gate)
        self.replan = ReplanDouble(self.gate)
        self.notifier = notifier or MockNotificationOutbox()
        adapters: dict[str, Any] = {Stage.ORIENTATION_SCHEDULING.value: self.uc05.orientation,
                                    Stage.BUDDY_ASSIGNMENT.value: self.uc05.buddy,
                                    Stage.TEAM_INTRODUCTION.value: self.uc05.team_intro,
                                    Stage.JOINING_DAY_WELCOME.value: self.uc05.welcome}
        if not real_uc02:
            adapters[Stage.PLAN_TASKS.value] = self.plan
        if not (real_uc02 or real_replan):
            adapters[Stage.REPLAN_TASKS.value] = self.replan
        if not real_uc03:
            adapters[Stage.VALIDATE_DOCUMENTS.value] = self.uc03
        kwargs: dict[str, Any] = {} if case_states is None else {"case_states": case_states}
        self.deps = OrchestratorDeps(ctx=None, worker_pool=Pool(uc01=self.uc01, uc04=self.uc04),
                                     uc03_repo_root=UC_ROOTS["uc03"], checkpointer=checkpointer,
                                     stage_adapters=adapters, notifier=self.notifier,
                                     uc03_document_store_path=persist_dir / "uc03_documents.db" if persist_dir else None,
                                     **kwargs)
        settings = Settings(agent_home=COMMON_HOME, **({"data_dir": persist_dir} if persist_dir else {}))
        self.ctx = AgentContext.build(agent_home=COMMON_HOME, llm=FakeLLM(), connector=self.deps, settings=settings,
                                      persist=persist_dir is not None)
        self.deps.ctx = self.ctx
        self.agent = OnboardingOrchestratorAgent(self.ctx)

    def case_input(self, **over) -> OnboardingCaseInput:
        base = dict(client_id=CLIENT, hr_officer_id=HR["on_behalf_of"], offer_accepted=True,
                    offer_reference="OFR-WF-001", offer_acceptance_event_id="EVT-001", candidate_name=CANDIDATE,
                    candidate_email="alex.morgan@example.com", designation="Software Engineer",
                    joining_date="2026-12-01", id_number="ID-WF-001", role_id="software_engineer",
                    department="Engineering", manager_id="TL-TEST-001", location="Dubai", project="platform",
                    uc04_client_config_ref=str(UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"))
        base.update(over)
        return OnboardingCaseInput(**base)

    def start(self, **over):
        return self.agent.advance_case(self.case_input(**over), case_id=None, actor=HR)

    @staticmethod
    def outstanding(state) -> list[str]:
        return [o["document_type"] for o in (state.get("document_collection") or {}).get("outstanding", [])]

    def supply(self, state, doc_types: list[str] | None = None, *, actor=HR, tag: str = "1", **meta):
        documents = [synthetic_document(t, tag=tag, **meta) for t in (doc_types or self.outstanding(state))]
        return self.agent.submit_documents(state["case_id"], documents=documents, actor=actor)

    def to_branches(self):
        return self.supply(self.start())

    def approve_signoff(self, state, actor=HR_2):
        (item,) = self.ctx.hitl.list(status=Decision.PENDING, gate="day_one_signoff",
                                     workflow_instance=state["case_id"], client_id=CLIENT)
        return self.agent.decide_approval(state["case_id"], item.id, decision="approved", actor=actor)

    def stored(self, case_id: str) -> dict:
        return copy.deepcopy(self.deps.case_states[case_id])


def status(state, stage: Stage) -> str:
    return state["stage_statuses"][stage.value]


def code(state, stage: Stage) -> str | None:
    return (state["stage_errors"].get(stage.value) or {}).get("code")
