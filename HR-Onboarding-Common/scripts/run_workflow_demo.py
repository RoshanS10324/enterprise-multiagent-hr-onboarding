"""Run the state-driven onboarding workflow end to end. SYNTHETIC DATA ONLY: no real-time API,
no credentials, no live service; every message and booking is simulated.

    python scripts/run_workflow_demo.py                    drive the whole workflow over the HTTP API
                                                           (in-process), printing each step
    python scripts/run_workflow_demo.py --serve [--port N] serve the orchestrator API on 127.0.0.1
                                                           (development identity headers) and drive it
                                                           yourself (docs/STATE_DRIVEN_ONBOARDING_WORKFLOW.md §9)
    python scripts/run_workflow_demo.py --real             the real UC01/UC04/UC05 workers, UC03 and UC02:
                                                           shows how far the real agents get today
    --persist-dir DIR                                      keep case states and checkpoints in SQLite
                                                           (a restarted --serve resumes the same cases)

What runs, by mode:
  default / --serve  the REAL orchestrator graph, routing, HITL gate (day-1 signoff), the REAL UC03
                     in-process -- document collection per plan version (its registry), validation
                     rules, HR review, and its requirement-gap detection that triggers UC02 replanning --
                     with the mock notifier. Stand-ins with each UC's output shape for UC01, UC02
                     planning/replanning, UC04 and UC05 -- no worker process is needed.
  --real             every UC for real: UC01/UC04/UC05 through their workers (UC0X_WORKER_PYTHON, or
                     the environments scripts/run_tests.ps1 builds under %LOCALAPPDATA%), UC02 and UC03
                     in-process.

The order is UC01 -> UC02 (initial planning) -> UC03 (collection, storage, validation) -> UC04 || UC05,
with UC03 -> UC02 replanning whenever UC03 finds the plan must change (docs/UPDATED_ONBOARDING_WORKFLOW.md).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

COMMON = Path(__file__).resolve().parents[1]
WORKSPACE = COMMON.parent
UC03_ROOT = WORKSPACE / "UC03_Document_Validation"
UC04_CONFIG = WORKSPACE / "UC04_Equipment_Access_Integration_Ready" / "data" / "client_alpha_config.json"

from hr_onboarding_common.agent import AgentContext  # noqa: E402
from hr_onboarding_common.config import Settings  # noqa: E402
from hr_onboarding_common.llm import FakeLLM  # noqa: E402
from hr_onboarding_common.orchestration.adapters._shared import with_stage_status  # noqa: E402
from hr_onboarding_common.orchestration.adapters.uc02_replanning import apply_revised_plan, check_feedback  # noqa: E402
from hr_onboarding_common.orchestration.adapters.uc02_tasks import (catalog_requirements, plan_record,  # noqa: E402
                                                                    record_initial_plan)
from hr_onboarding_common.orchestration.api import create_orchestrator_app  # noqa: E402
from hr_onboarding_common.orchestration.case_store import SqliteCaseStateStore  # noqa: E402
from hr_onboarding_common.orchestration.contracts import WorkerResponse  # noqa: E402
from hr_onboarding_common.orchestration.deps import OrchestratorDeps  # noqa: E402
from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent  # noqa: E402
from hr_onboarding_common.orchestration.state import Stage, StageStatus  # noqa: E402
from hr_onboarding_common.platform.identity import InsecureHeaderIdentityProvider  # noqa: E402

CLIENT = "CLIENT-TEST-A"
HR = {"X-User-Id": "user:hr-admin-01", "X-Client-Id": CLIENT, "X-User-Roles": "hr_admin"}
HR_2 = {"X-User-Id": "user:hr-admin-02", "X-Client-Id": CLIENT, "X-User-Roles": "hr_admin"}
# The authorized approver of an internal onboarding form (policy gate onboarding_form_approval): not the requester.
HR_LEAD = {"X-User-Id": "user:hr-lead-01", "X-Client-Id": CLIENT, "X-User-Roles": "hr_lead"}
SCHEDULER = {"X-User-Id": "svc:joining-day-scheduler", "X-Client-Id": CLIENT, "X-User-Roles": "onboarding_scheduler"}
CANDIDATE = "Alex Morgan"  # synthetic
START_BODY = {"offer_accepted": True, "offer_reference": "OFR-DEMO-001", "offer_acceptance_event_id": "EVT-001",
              "candidate_name": CANDIDATE, "candidate_email": "alex.morgan@example.com",
              "designation": "Software Engineer", "joining_date": "2026-12-01", "id_number": "ID-DEMO-001",
              "role_id": "software_engineer", "department": "Engineering", "manager_id": "TL-TEST-001",
              "location": "Dubai", "project": "platform", "uc04_client_config_ref": str(UC04_CONFIG),
              "offer_accepted_at": "2026-10-20T10:00:00Z"}
# A person's form is never approved by submitting it (no form_approved_at / form_approved_by_role here): it goes
# to the onboarding_form_approval gate, and the decision's facts are what UC01 and UC02 receive.


# ------------------------------------------------------------------ stand-ins (default / --serve)


_UC01_SRC = str(WORKSPACE / "UC01_Employee_Record_Creation" / "src")


def _uc01_extract_document(payload: dict) -> dict:
    """Run UC01's deterministic docx/pdf extractor directly (no worker process needed).
    Replicates uc_worker_runtime._extract_onboarding_document exactly."""
    import base64
    import hashlib
    from datetime import date
    from pydantic import SecretStr

    if _UC01_SRC not in sys.path:
        sys.path.insert(0, _UC01_SRC)

    from sub_agent.documents.pdf_reader import read_document  # type: ignore
    from sub_agent.documents.extraction import extract_onboarding_form  # type: ignore
    from sub_agent.documents.validation import validate_extraction  # type: ignore

    data = base64.b64decode(payload["content_base64"], validate=True)
    filename = str(payload["filename"])

    try:
        content = read_document(data, filename)
    except Exception as exc:
        code = getattr(exc, "code", "UNSUPPORTED_FORMAT")
        return {"status": "REJECTED", "code": code, "reason": str(exc).split(": ", 1)[-1][:300]}

    extraction = extract_onboarding_form(
        content, document_name=filename,
        sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))

    outcome = validate_extraction(
        extraction,
        candidate_email=payload.get("candidate_email"),
        work_location_type=payload.get("work_location_type"),
        today=date.today())

    md = extraction.metadata

    # Safe field view: status + source + note per field; value only for non-sensitive fields.
    fields: dict = {}
    for key, f in extraction.fields().items():
        entry: dict = {"status": f.status.value, "source": f.source}
        if f.note:
            entry["note"] = f.note
        if f.value is not None and not isinstance(f.value, SecretStr):
            entry["value"] = (f.value.isoformat() if hasattr(f.value, "isoformat")
                              else f.value.value if hasattr(f.value, "value") else f.value)
        fields[key] = entry

    supplementary: dict = {}
    for k, f in extraction.supplementary_fields().items():
        entry = {"status": f.status.value, "source": f.source}
        if f.value is not None and not isinstance(f.value, SecretStr):
            entry["value"] = f.value
        if f.note:
            entry["note"] = f.note
        supplementary[k] = entry

    return {
        "status": ("VALIDATION_FAILED" if outcome.errors
                   else "NEEDS_REVIEW" if outcome.review_reasons
                   else "VALID"),
        "errors": list(outcome.errors),
        "review_reasons": list(outcome.review_reasons),
        "outstanding_documents": list(outcome.outstanding_documents),
        "extraction": {
            "template_id": md.template_id,
            "template_recognised": md.template_recognised,
            "completeness": md.completeness,
            "fields": fields,
            "supplementary": supplementary,
            "missing_fields": md.missing_fields,
            "invalid_fields": md.invalid_fields,
            "uncertain_fields": md.uncertain_fields,
            "synthetic_document": md.synthetic_document,
            "responsibilities": {
                "items": list(extraction.roles.responsibilities),
                "is_example": extraction.roles.is_example,
                "source": extraction.roles.source,
            },
            "reference_charges": [c.model_dump() for c in extraction.reference_charges],
        },
        "source_document": {
            "sha256": md.source_sha256,
            "size_bytes": md.source_size_bytes,
            "template_id": md.template_id,
            "template_recognised": md.template_recognised,
            "extracted_at": md.extracted_at.isoformat(),
        },
    }


class _Uc01StandIn:
    """UC01's OpenCaseResult shape; the real UC01 adapter translates it. Reading an uploaded onboarding form
    (extract_onboarding_document) runs UC01's own deterministic extractor in-process (_uc01_extract_document)."""
    is_alive = True

    def call(self, op, payload, *, actor, run_id=None, idempotency_key=None, timeout_s=None) -> WorkerResponse:
        if op == "extract_onboarding_document":
            try:
                result = _uc01_extract_document(payload)
            except Exception as exc:
                return WorkerResponse(run_id=run_id or "r", ok=False,
                                      error_code=getattr(exc, "code", "UC01_UNAVAILABLE"),
                                      result=None)
            return WorkerResponse(run_id=run_id or "r", ok=True, result=result)
        # The joining date HR entered on the form, as the real UC01 records it (the fixed date only when none is given).
        joining = (payload.get("form") or {}).get("joining_date") or "2026-12-01"
        case = {"case_id": f"UC01-{payload['request_id']}", "employee_no": "EMP-TEST-001",
                "joining_date": str(joining), "client_id": payload["client_id"]}
        return WorkerResponse(run_id=run_id or "r", ok=True, result={"outcome": "CASE_OPENED", "case": case,
                                                                    "approvals_created": [], "reasons": []})


class _Uc04StandIn:
    """UC04's OrchestratorResponse shape; the real UC04 adapter translates it."""
    is_alive = True

    def call(self, op, payload, *, actor, run_id=None, idempotency_key=None, timeout_s=None) -> WorkerResponse:
        items = [{"request_id": "REQ-SYN-1", "item_id": "laptop_standard", "state": "FULFILLED", "ticket_id": "TKT-SYN-1"}]
        return WorkerResponse(run_id=run_id or "r", ok=True, result={"result": {
            "case_id": payload["input"]["case_id"], "overall_status": "READY", "items": items, "blockers": []},
            "proposals": []})


class _Uc05StandIn:
    """UC05's worker for the reads the orchestrator makes outside the stages (the stages themselves are stand-ins):
    the joining-day welcome preview, labelled synthetic. Anything else UC05 would do is refused, never faked."""
    is_alive = True

    def call(self, op, payload, *, actor, run_id=None, idempotency_key=None, timeout_s=None) -> WorkerResponse:
        if op == "welcome_message":
            return WorkerResponse(run_id=run_id or "r", ok=True, result={
                "status": "preview", "subject": "Welcome to the team!", "language": "en",
                "body": ("Hello,\n\nWelcome aboard! On your first day you will meet your manager and your onboarding "
                         "buddy, collect your equipment and join the induction session.\n\nWe are glad to have you "
                         "with us.\n\nHR Team\n\n[Synthetic demo template - not a client-approved message]"),
                "source": "demo stand-in (synthetic template)"})
        return WorkerResponse(run_id=run_id or "r", ok=False, error_code="NOT_AVAILABLE_IN_DEMO",
                              error_detail=f"UC05 '{op}' is not available in demo mode (UC05 runs as a stand-in)")


class _Pool:
    def __init__(self, **workers):
        self.workers = workers

    def get(self, uc_id):
        return self.workers[uc_id]

    def stop_all(self):
        for worker in self.workers.values():
            if hasattr(worker, "stop"):
                worker.stop()


def _plan_stand_in(state, deps):
    """UC02 initial planning's output shape: the task plan, and plan version 1 whose document
    requirements come from UC03's real catalog (what the real adapter records in UC02)."""
    new = with_stage_status(state, Stage.PLAN_TASKS, StageStatus.COMPLETE)
    new["task_plan"] = {"onboarding_id": "ONB-SYN-001", "created": True, "ready": ["hr_record_create"],
                        "blocked": [], "tasks": [{"task_id": "T-1", "template_key": "it_laptop", "owner": "it_manager",
                                                  "due_date": "2026-11-20", "status": "ready"}],
                        "plan_version": 1, "data_origin": "synthetic"}
    record_initial_plan(new, plan_record(state, plan_id="ONB-SYN-001", plan_version=1,
                                         required_documents=catalog_requirements(state, deps), due_date="2026-11-24",
                                         task_count=1, attributes=[], source="uc02_stand_in"))
    return new


def _replan_stand_in(state, deps):
    """UC02 replanning's output shape: the change UC03 recommended, as the next plan version (the real
    adapter's own feedback check and result recording)."""
    refused = check_feedback(state)
    if refused is not None:
        return refused
    feedback, plan = state["document_validation_feedback"], state["onboarding_plan"]
    changes = feedback["recommended_task_changes"]
    drop = {c["target"] for c in changes if c["action"] == "remove_document"}
    add = [c["target"] for c in changes if c["action"] == "add_document"]
    attributes = [c["target"] for c in changes if c["action"] == "add_attribute"]
    documents = [d for d in plan["required_documents"] if d["document_type"] not in drop] + [
        {"document_type": t, "label": t.replace("_", " ").title(), "required": True, "reason": "replanned"} for t in add]
    return apply_revised_plan(state, plan_version=plan["plan_version"] + 1, required_documents=documents,
                              added_tasks=[f"ONB-SYN-001#{a}_tasks" for a in attributes], removed_tasks=[],
                              modified_tasks=[], attributes=[*plan.get("attributes", []), *attributes],
                              task_count=plan.get("task_count", 0) + len(attributes), due_date="2026-11-24",
                              source="uc02_stand_in")


_STAND_IN_PLAN_ID = "ONB-SYN-001"
_STAND_IN_CATEGORIES = {"it_laptop": "it_provisioning"}  # the stand-in plan's one task (_plan_stand_in), UC-02's category


def _uc02_plan_stand_in(state, deps, onboarding_id):
    """Demo mode only: the UC-02 plan UC04 links to (adapters/uc04_uc02_plan.py) is the stand-in plan this demo's
    _plan_stand_in recorded on the case -- there is no UC-02 service here. Labelled synthetic, and only that plan:
    any other id is refused exactly as UC-02 would refuse an unknown plan."""
    from hr_onboarding_common.orchestration.adapters.uc04_uc02_plan import PLAN_MISSING, Uc02PlanError

    plan = state.get("task_plan") or {}
    if onboarding_id != _STAND_IN_PLAN_ID or plan.get("onboarding_id") != _STAND_IN_PLAN_ID:
        raise Uc02PlanError(PLAN_MISSING, f"the demo's UC-02 stand-in holds no onboarding plan {onboarding_id}")
    return {"onboarding_id": _STAND_IN_PLAN_ID, "case_id": state["case_id"], "client_id": state["client_id"],
            "employee_id": state.get("employee_id"), "plan_version": plan.get("plan_version", 1),
            "plan_status": "proposed", "revision": 0, "config_version": "", "data_origin": "synthetic",
            "source": "uc02_stand_in",
            "tasks": [{**t, "category": _STAND_IN_CATEGORIES.get(t.get("template_key"))} for t in plan.get("tasks", [])]}


def _uc05_stand_in(stage: Stage, result_key: str, result: dict[str, Any]):
    """A UC05 stage's output shape: complete, with a synthetic result."""
    def run(state, deps):
        new = with_stage_status(state, stage, StageStatus.COMPLETE)
        new[result_key] = {**result, "data_origin": "synthetic"}
        if "uc05_start_date" in result and state.get("joining_date"):
            # UC05 schedules from the case's joining date (a fixed date here would contradict it: JOINING_DATE_UNRECONCILED).
            new[result_key]["uc05_start_date"] = state["joining_date"]
        if stage == Stage.ORIENTATION_SCHEDULING:
            new["employee_ref"] = {"client_id": CLIENT, "employee_id": state["employee_id"],
                                   "employee_no": state["employee_id"], "source": "shared_scheme", "scheme": "shared"}
        return new
    return run


def _welcome_stand_in(state, deps):
    """UC05's joining-day welcome: waits for the joining-day event, then 'delivers' (simulated)."""
    if not state.get("joining_day_event"):
        new = with_stage_status(state, Stage.JOINING_DAY_WELCOME, StageStatus.WAITING)
        new["next_action"] = f"await_joining_day:{state.get('joining_date')}"
        return new
    new = with_stage_status(state, Stage.JOINING_DAY_WELCOME, StageStatus.COMPLETE)
    new["welcome_message_result"] = {"draft_id": "DRAFT-SYN-1", "delivery": "delivered (simulated)",
                                     "data_origin": "synthetic"}
    return new


def _persistence(persist_dir: Path | None) -> dict[str, Any]:
    if persist_dir is None:
        return {}
    from langgraph.checkpoint.sqlite import SqliteSaver

    persist_dir.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(str(persist_dir / "checkpoints.db"), check_same_thread=False))
    saver.setup()
    return {"case_states": SqliteCaseStateStore(persist_dir / "case_states.db"), "checkpointer": saver}


def build_synthetic_agent(persist_dir: Path | None) -> OnboardingOrchestratorAgent:
    adapters = {
        Stage.PLAN_TASKS.value: _plan_stand_in,
        Stage.REPLAN_TASKS.value: _replan_stand_in,
        Stage.ORIENTATION_SCHEDULING.value: _uc05_stand_in(Stage.ORIENTATION_SCHEDULING, "orientation_schedule_result", {
            "uc05_case": True, "session_id": "SES-SYN-1", "session_status": "confirmed",
            "uc05_start_date": "2026-12-01"}),
        Stage.BUDDY_ASSIGNMENT.value: _uc05_stand_in(Stage.BUDDY_ASSIGNMENT, "buddy_assignment_result",
                                                     {"buddy_id": "EMP-TEST-050", "status": "assigned"}),
        Stage.TEAM_INTRODUCTION.value: _uc05_stand_in(Stage.TEAM_INTRODUCTION, "team_introduction_result",
                                                      {"session_id": "SES-SYN-2", "session_status": "confirmed"}),
        Stage.JOINING_DAY_WELCOME.value: _welcome_stand_in,
    }
    deps = OrchestratorDeps(ctx=None, worker_pool=_Pool(uc01=_Uc01StandIn(), uc04=_Uc04StandIn(), uc05=_Uc05StandIn()),
                            uc03_repo_root=UC03_ROOT, stage_adapters=adapters, uc02_plan_reader=_uc02_plan_stand_in,
                            **_persistence(persist_dir))
    settings = Settings(agent_home=COMMON, **({"data_dir": persist_dir} if persist_dir else {}))
    ctx = AgentContext.build(agent_home=COMMON, llm=FakeLLM(), connector=deps, settings=settings,
                             persist=persist_dir is not None)
    deps.ctx = ctx
    return OnboardingOrchestratorAgent(ctx)


def build_real_agent(persist_dir: Path | None) -> OnboardingOrchestratorAgent:
    from hr_onboarding_common.orchestration.bootstrap import build_deps

    envs = Path(os.environ.get("LOCALAPPDATA", "")) / "hr-onboarding-envs"
    roots = {"uc01": "UC01_Employee_Record_Creation", "uc04": "UC04_Equipment_Access_Integration_Ready",
             "uc05": "UC05_Orientation-Schedule-Agent"}
    for uc, root in roots.items():  # explicitly configured values always win
        python = envs / uc / "Scripts" / "python.exe"
        if python.exists():
            os.environ.setdefault(f"{uc.upper()}_WORKER_PYTHON", str(python))
        os.environ.setdefault(f"{uc.upper()}_WORKER_SRC_DIR", str(WORKSPACE / root / "src"))
    ctx, deps = build_deps(agent_home=COMMON, uc03_repo_root=UC03_ROOT, persist_dir=persist_dir)
    return OnboardingOrchestratorAgent(ctx)


# ------------------------------------------------------------------ the walk-through


def _show(step: str, view: dict[str, Any]) -> dict[str, Any]:
    actions = ", ".join(f"{a['action']}({','.join(a['documents']) or a.get('reference') or a['stage']})"
                        for a in view.get("pending_actions", [])) or "-"
    print(f"\n== {step}\n   workflow_status={view.get('workflow_status')}  current_stage={view.get('current_stage')}")
    print(f"   pending: {actions}")
    return view


def _print_executed_steps(agent: OnboardingOrchestratorAgent, case_id: str) -> None:
    """The LangGraph supersteps the last request executed from plan_tasks on, read from the
    checkpoint history (the nodes that ran in each step; " + " joins nodes of the same step)."""
    steps = []
    for snapshot in agent.graph.get_state_history({"configurable": {"thread_id": case_id}}):  # newest first
        ran = sorted(t.name for t in snapshot.tasks if t.result is not None or t.error is not None)
        if ran:
            steps.append(" + ".join(ran))
        if snapshot.metadata.get("source") == "input":
            break
    steps.reverse()
    start = next((i for i, step in enumerate(steps) if step == "validate_documents"), None)
    if start is not None:
        print(f"   LangGraph steps: {' -> '.join(steps[start:start + 6])}")


def _print_fan_out_timing(view: dict[str, Any]) -> None:
    """When each stage of the fan-out ran, from the timestamps the orchestrator recorded per stage.
    The two branches run in the same LangGraph step on separate threads; with these instant
    stand-ins their calls may not overlap in time (test_execution_order.py proves that they can)."""
    outcomes = view.get("stage_outcomes") or {}
    for stage in ("validate_documents", "equipment_access", "orientation_scheduling"):
        outcome = outcomes.get(stage)
        if outcome:
            print(f"   {stage:<23} started {outcome['started_at'][11:26]}  finished {outcome['finished_at'][11:26]}"
                  f"  ({outcome['status']}, joining_date {outcome['inputs'].get('joining_date')})")
        else:
            print(f"   {stage:<23} not started")


def _ok(response):
    if response.status_code >= 400:
        sys.exit(f"request failed: {response.status_code} {response.text}")
    return response.json()


def _documents(types: list[str], *, tag: str = "1", expired: tuple[str, ...] = (),
               visa_required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"documents": [{"document_type": t, "document_ref": f"SYN-DOC-{t}-{tag}", "metadata": {
        "readable": True, "required_fields_present": True, "evidence_stored": True, "attestation_confirmed": True,
        "extracted_name": CANDIDATE, "expiry_date": "2020-01-01" if t in expired else "2031-01-01",
        **({"visa_required": True} if t in visa_required else {})}} for t in types]}


def _plan_line(view: dict[str, Any]) -> str:
    plan = view.get("onboarding_plan") or {}
    collection = view.get("document_collection") or {}
    return (f"   plan {plan.get('plan_id')} v{plan.get('plan_version')} ({len(plan.get('required_documents') or [])} "
            f"documents); UC03 collecting for v{collection.get('plan_version')}")


def walk_through(client, agent: OnboardingOrchestratorAgent, *, real: bool) -> None:
    base = "/v1/orchestration/cases"
    view = _show("0. HR submits the internal onboarding form: an approval request is raised for an authorized "
                 "approver; UC01 waits (it opens a case only from an approved form)",
                 _ok(client.post(base, json=START_BODY, headers=HR)))
    approval = view["form_approval"]
    print(f"   form approval {approval['approval_id']}: {approval['status']}, required role "
          f"{approval['required_approver_role']}, {approval['assignment']['status']}")
    view = _show("1. an HR lead (not the requester) approves the form: UC01 record -> UC02 plans (v1, with its "
                 "document requirements) -> UC03 opens the collection and sends the request (mock), then WAIT",
                 _ok(client.post(f"{base}/{view['case_id']}/approvals/{approval['approval_id']}/decision",
                                 json={"decision": "approved"}, headers=HR_LEAD)))
    print(_plan_line(view))
    if not view.get("document_collection"):  # UC02 could not plan: nothing is collected (the real agents today)
        for stage, error in view["stage_errors"].items():
            print(f"   {stage}: {error['code']} -- {' '.join(error['detail'].split())[:110]}")
        print("\nUC02 plans first; while it cannot, UC03 collects nothing and no branch starts. See "
              "docs/WORKFLOW_IMPLEMENTATION_REPORT.md (known blockers).")
        return
    case = f"{base}/{view['case_id']}"
    required = [d["document_type"] for d in view["document_collection"]["required_documents"]]

    view = _show("2. documents supplied, one passport expired: UC03 asks for a correction; no replanning, no branch",
                 _ok(client.post(f"{case}/documents", json=_documents(required, expired=("passport",)), headers=HR)))
    print(f"   correction: {[(r['document_type'], r['action'], r['reason_codes']) for r in view['correction_requirements'] if r['action'] != 'hr_review']}")

    view = _show("3. corrected passport (revision 2) shows the holder needs a visa: UC03 revalidates, reports that "
                 "the plan must change -> UC02 replans (v2) -> UC03 applies it and asks for the new document",
                 _ok(client.post(f"{case}/documents", json=_documents(["passport"], tag="2", visa_required=("passport",)),
                                 headers=HR)))
    print(_plan_line(view))
    feedback = view.get("document_validation_feedback") or {}
    print(f"   UC03 feedback (v{feedback.get('plan_version')}): {feedback.get('validation_status')}; "
          f"replanning reasons {feedback.get('replanning_reasons')}")
    added = [d["document_type"] for d in view["document_collection"]["required_documents"]
             if d["document_type"] not in required]

    if added:
        view = _show(f"4. the new document(s) {added} supplied: UC03 revalidates against plan v"
                     f"{(view.get('onboarding_plan') or {}).get('plan_version')}; HR review required",
                     _ok(client.post(f"{case}/documents", json=_documents(added), headers=HR)))

    reviews = {"reviews": [{"document_type": d["document_type"], "decision": "approved"}
                           for d in view["document_collection"]["required_documents"]
                           if (view.get("document_validation") or {}).get(d["document_type"], {}).get("classification")
                           == "hr_review_required"]}
    view = _show("5. HR reviewer approves: documents verified under the current plan version -> dispatch_branches "
                 "-> UC04 equipment_access + UC05 orientation_scheduling in parallel -> merge -> day-1 readiness",
                 _ok(client.post(f"{case}/documents/reviews", json=reviews, headers=HR)))
    print(f"   validate_documents: {view['stage_statuses']['validate_documents']}  verified against plan "
          f"v{(view.get('verified_context') or {}).get('plan_version')}  joining_date={view['joining_date']}")
    print(f"   branches (UC04, UC05): { {b: s['status'] for b, s in view['branch_statuses'].items()} }")
    _print_executed_steps(agent, view["case_id"])
    _print_fan_out_timing(view)
    if real:
        for stage, error in view["stage_errors"].items():
            print(f"   {stage}: {error['code']} -- {' '.join(error['detail'].split())[:110]}")
        print("\nThe real agents stop here today; see docs/WORKFLOW_IMPLEMENTATION_REPORT.md (known blockers).")
        return

    (signoff,) = [a["reference"] for a in view["pending_actions"] if a["action"] == "decide_approval"]
    view = _show("6. a second HR admin signs off day-1 readiness (the orchestrator's own HITL gate)",
                 _ok(client.post(f"{case}/approvals/{signoff}/decision", json={"decision": "approved"}, headers=HR_2)))

    view = _show("7. joining-day event (normally the joining-day scheduler): UC05 welcome -> COMPLETE",
                 _ok(client.post(f"{case}/joining-day", json={"as_of": "2026-12-01"}, headers=SCHEDULER)))
    print(f"   completed stages: {view['completed_stages']}")
    print(f"   document request: {view['document_collection']['notification']}")


def _declare_dev_identity_headers(app) -> None:
    """Swagger UI sends only headers the OpenAPI document declares. Declare the three development
    identity headers as security schemes so its Authorize button can send them. Documentation only:
    the identity provider still reads and checks the same headers on every request."""
    schema = app.openapi()  # generated once and cached by FastAPI; changed in place
    schemes = {"UserId": "X-User-Id", "ClientId": "X-Client-Id", "Roles": "X-User-Roles"}
    schema.setdefault("components", {})["securitySchemes"] = {
        name: {"type": "apiKey", "in": "header", "name": header} for name, header in schemes.items()}
    schema["security"] = [{name: [] for name in schemes}]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--real", action="store_true", help="use the real UC01/UC04/UC05 workers and UC02")
    parser.add_argument("--serve", action="store_true", help="serve the API instead of walking through it")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--persist-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    agent = build_real_agent(args.persist_dir) if args.real else build_synthetic_agent(args.persist_dir)
    # Development identity: trusts X-User-Id / X-Client-Id / X-User-Roles. Local use only.
    app = create_orchestrator_app(agent=agent, identity_provider=InsecureHeaderIdentityProvider(enabled=True))
    _declare_dev_identity_headers(app)
    print("SYNTHETIC DATA ONLY -- development identity headers are trusted; never expose this server.")
    try:
        if args.serve:
            import uvicorn

            uvicorn.run(app, host="127.0.0.1", port=args.port)
        else:
            from fastapi.testclient import TestClient

            walk_through(TestClient(app), agent, real=args.real)
    finally:
        if args.real:
            agent.deps.worker_pool.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
