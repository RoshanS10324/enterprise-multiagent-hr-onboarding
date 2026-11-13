"""Live check: UC02 in Supabase mode, through the real orchestration API, against the configured Supabase project.

DEVELOPMENT DATABASE ONLY. Every write needs --confirm-write <the Supabase host>. What it writes:
  UC02          onboarding_tasks, task_dependencies, plan_revisions, audit_events, agent_runs -- UC02's own rows
  orchestrator  case_orchestration_state -- one row per case it runs
It never writes onboarding_cases or any other UC01-owned table: it runs EXISTING cases UC01 stored earlier.

Real: the orchestration API routes (create_orchestrator_app, called in process), the orchestrator graph, contract layer
and SupabaseCaseStateStore; UC02's real package and adapters in UC02_STORE_MODE=supabase (configuration, the UC01 case
and its own tables in Supabase); UC03 (in process); the form-approval gate. Not real: UC01 -- its Supabase writes are
blocked on decision B1 (UC01 docs/supabase-integration-status.md), so intake is bound to the stored onboarding_cases
row (UC01's CASE_OPENED shape, with the row's own id, tenant, employee number and joining date); UC04 and UC05 are the
demo's stand-ins (data_origin synthetic). No model is called (offline LLM).

Each step is its own process, so a backend restart sits between them:
  preflight                                   read-only: settings, schema, the case and its client configuration
  plan    --case ID --confirm-write HOST      start the case through the API; UC02 plans (revision 1)
  resume  --case ID --confirm-write HOST      NEW process: reload from Supabase; replay UC02 (idempotency); documents ->
                                              UC03 -> replanning (revision 2) -> UC03 again -> UC04 || UC05 (UC04's
                                              real adapter: blocked for a client without a UC04 configuration)
  branches --case ID --confirm-write HOST     NEW process: resume; UC04 || UC05 (stage stand-ins) from the stored state
  negative --case ID --other-case ID --nocfg-case ID --confirm-write HOST
                                              wrong tenant; missing configuration (UC02 writes no task, revision)
  verify  --case ID                           read-only read-back of every row
Run from HR-Onboarding-Common with its .venv (the orchestrator's environment, UC02 installed there).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import run_workflow_demo as demo  # noqa: E402  (UC04/UC05 stand-ins, the demo's identity headers)

from hr_onboarding_common.agent import AgentContext  # noqa: E402
from hr_onboarding_common.config import Settings, load_environment  # noqa: E402
from hr_onboarding_common.orchestration.adapters import uc02_runtime  # noqa: E402
from hr_onboarding_common.orchestration.adapters.uc02_tasks import call_uc02_plan_tasks, uc02_sub_agent  # noqa: E402
from hr_onboarding_common.orchestration.api import create_orchestrator_app  # noqa: E402
from hr_onboarding_common.orchestration.bootstrap import offline_llm  # noqa: E402
from hr_onboarding_common.orchestration.case_store import InMemoryCaseStateStore  # noqa: E402
from hr_onboarding_common.orchestration.contracts import WorkerResponse  # noqa: E402
from hr_onboarding_common.orchestration.deps import OrchestratorDeps  # noqa: E402
from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent  # noqa: E402
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state  # noqa: E402
from hr_onboarding_common.orchestration.supabase_state_store import SupabaseCaseStateStore  # noqa: E402
from hr_onboarding_common.platform.identity import InsecureHeaderIdentityProvider  # noqa: E402

BASE = "/v1/orchestration/cases"
UC02_TABLES = ("onboarding_tasks", "task_dependencies", "plan_revisions")


def headers(client_id: str, user: str, role: str) -> dict[str, str]:
    return {"X-User-Id": user, "X-Client-Id": client_id, "X-User-Roles": role}


# ------------------------------------------------------------------ configuration and database access


def environment() -> dict[str, str]:
    load_environment()
    problems = []
    if (os.environ.get("ORCHESTRATOR_STATE_STORE") or "").strip().lower() != "supabase":
        problems.append("ORCHESTRATOR_STATE_STORE must be supabase")
    if uc02_runtime.configured_store_mode() != "supabase":
        problems.append("UC02_STORE_MODE must be supabase")
    if problems:
        sys.exit("not configured: " + "; ".join(problems) + " (HR-Onboarding-Common/.env)")
    return uc02_runtime.uc02_environment()


def rest():
    from onboarding_task_management.supabase_rest import SupabaseRestClient

    return SupabaseRestClient.from_env(environment())


def host() -> str:
    return urlparse(environment().get("SUPABASE_URL", "")).hostname or ""


def require_write(confirm: str | None) -> None:
    if confirm != host():
        sys.exit(f"refusing to write: pass --confirm-write {host()} (the Supabase host this run writes to)")


def case_row(case_id: str) -> dict[str, Any]:
    from onboarding_task_management.supabase_rest import CASE_COLUMNS

    rows = rest().select("onboarding_cases", {"case_id": case_id}, columns=CASE_COLUMNS)
    if not rows or rows[0].get("is_deleted"):
        sys.exit(f"case {case_id} is not an onboarding_cases row (or is deleted)")
    return rows[0]


def uc02_counts(case_id: str, client_id: str) -> dict[str, int]:
    r = rest()
    tasks = r.select("onboarding_tasks", {"case_id": case_id, "client_id": client_id}, columns="task_id,status_summary")
    ours = [t for t in tasks if "uc02" in (t.get("status_summary") or {})]
    edges = [e for t in ours for e in r.select("task_dependencies", {"task_id": t["task_id"], "client_id": client_id},
                                                columns="task_id")]
    revisions = r.select("plan_revisions", {"case_id": case_id, "client_id": client_id}, columns="revision")
    snapshots = r.select("audit_events", {"case_id": case_id, "client_id": client_id, "action": "UC02_CASE_STATE"},
                         columns="event_id")
    return {"uc02_tasks": len(ours), "dependencies": len(edges), "plan_revisions": len(revisions),
            "case_state_snapshots": len(snapshots)}


# ------------------------------------------------------------------ the orchestrator for this run


class Uc01CaseBinding:
    """UC01's CASE_OPENED answer for an onboarding_cases row UC01 already stored (its id, tenant, employee number and
    joining date, read from Supabase). Stands in for UC01, whose Supabase writes are blocked (decision B1); it creates
    nothing and refuses another tenant."""

    is_alive = True

    def __init__(self, case: dict[str, Any]):
        self.case = case

    def call(self, op, payload, *, actor, run_id=None, idempotency_key=None, timeout_s=None) -> WorkerResponse:
        if op != "open_onboarding_case" or payload["client_id"] != self.case["client_id"]:
            return WorkerResponse(run_id=run_id or "r", ok=False, error_code="TENANT_CONTEXT_CONFLICT",
                                  error_detail="the bound case belongs to another tenant")
        case = {k: self.case.get(k) for k in ("case_id", "client_id", "employee_no", "joining_date")}
        return WorkerResponse(run_id=run_id or "r", ok=True, result={"outcome": "CASE_OPENED", "case": case,
                                                                    "approvals_created": [], "reasons": []})


def _uc05_stand_in(stage: Stage, result_key: str, result: dict[str, Any]):
    """A UC05 stage's output shape (complete, synthetic result), in the CASE's tenant (the demo's stand-in is fixed to
    its own test client, which the contract layer rightly refuses for another tenant)."""
    from hr_onboarding_common.orchestration.adapters._shared import with_stage_status

    def run(state, deps):
        new = with_stage_status(state, stage, StageStatus.COMPLETE)
        new[result_key] = {**result, "data_origin": "synthetic"}
        if stage == Stage.ORIENTATION_SCHEDULING:
            new["employee_ref"] = {"client_id": state["client_id"], "employee_id": state["employee_id"],
                                   "employee_no": state["employee_id"], "source": "shared_scheme", "scheme": "shared"}
        return new
    return run


def _uc04_stage_stand_in(state, deps):
    """UC04 at stage level (synthetic READY). Used by the `branches` step only: the real UC04 adapter refuses a client
    without a UC04 equipment/access configuration (UC04_CONFIGURATION_MISSING), which SYN-UAE-TEST does not have."""
    from hr_onboarding_common.orchestration.adapters._shared import with_stage_status

    new = with_stage_status(state, Stage.EQUIPMENT_ACCESS, StageStatus.COMPLETE)
    new["equipment_access_result"] = {"overall_status": "READY", "items": [], "data_origin": "synthetic",
                                      "verified_plan_version": (state.get("verified_context") or {}).get("plan_version")}
    return new


def build(case: dict[str, Any] | None, *, uc04_stage_stand_in: bool = False):
    environment()
    adapters = {
        Stage.ORIENTATION_SCHEDULING.value: _uc05_stand_in(Stage.ORIENTATION_SCHEDULING, "orientation_schedule_result", {
            "uc05_case": True, "session_id": "SES-SYN-1", "session_status": "confirmed"}),
        Stage.BUDDY_ASSIGNMENT.value: _uc05_stand_in(Stage.BUDDY_ASSIGNMENT, "buddy_assignment_result",
                                                     {"buddy_id": "SYN-BUDDY-1", "status": "assigned"}),
        Stage.TEAM_INTRODUCTION.value: _uc05_stand_in(Stage.TEAM_INTRODUCTION, "team_introduction_result",
                                                      {"session_id": "SES-SYN-2", "session_status": "confirmed"}),
        Stage.JOINING_DAY_WELCOME.value: demo._welcome_stand_in,
    }  # no plan_tasks / replan_tasks entry: UC02 is the real one
    if uc04_stage_stand_in:
        adapters[Stage.EQUIPMENT_ACCESS.value] = _uc04_stage_stand_in
    states = SupabaseCaseStateStore.from_env(os.environ, provisional=InMemoryCaseStateStore())
    deps = OrchestratorDeps(ctx=None, worker_pool=demo._Pool(uc01=Uc01CaseBinding(case or {}), uc04=demo._Uc04StandIn()),
                            uc03_repo_root=demo.UC03_ROOT, stage_adapters=adapters, case_states=states)
    ctx = AgentContext.build(agent_home=demo.COMMON, llm=offline_llm(), connector=deps,
                             settings=Settings(agent_home=demo.COMMON), persist=False)
    deps.ctx = ctx
    agent = OnboardingOrchestratorAgent(ctx)
    from fastapi.testclient import TestClient

    app = create_orchestrator_app(agent=agent, identity_provider=InsecureHeaderIdentityProvider(enabled=True),
                                  runtime_info={"mode": "uc02-supabase-verification",
                                                "state_store": type(states).__name__,
                                                "uc02_store": uc02_runtime.configured_store_mode()})
    return agent, TestClient(app)


def ok(response) -> dict[str, Any]:
    if response.status_code >= 400:
        sys.exit(f"request failed: {response.status_code} {response.text[:400]}")
    return response.json()


def report(title: str, view: dict[str, Any]) -> None:
    plan, task_plan = view.get("onboarding_plan") or {}, view.get("task_plan") or {}
    summary = (view.get("stage_outcomes") or {}).get("plan_tasks", {}).get("result_summary")
    print(f"\n== {title}")
    print(f"   case {view['case_id']}  workflow {view.get('workflow_status')}  state version {view.get('state_version')}")
    print("   stages: " + ", ".join(f"{s}={v}" for s, v in view["stage_statuses"].items() if v not in ("pending",)))
    print(f"   plan v{plan.get('plan_version')}  tasks {task_plan.get('task_count')}  required "
          f"{task_plan.get('required_task_count')}  dependencies {task_plan.get('dependency_count')}  store "
          f"{task_plan.get('store')}  config v{task_plan.get('config_version')}")
    if summary:
        print(f"   stage_outcomes.plan_tasks.result_summary: {json.dumps(summary)[:300]}")
    for blocker in view.get("blockers") or []:
        print(f"   blocker {blocker['stage']}: {blocker['code']} -- {blocker['reason'][:140]}")


# ------------------------------------------------------------------ steps


def preflight(args) -> int:
    from onboarding_task_management.supabase_schema import verify_schema

    env = environment()
    r = rest()
    print(f"Supabase host {host()}; UC02 store {uc02_runtime.configured_store_mode()}, UC02_ENV {env.get('UC02_ENV')}, "
          f"synthetic configuration allowed: {env.get('UC02_ALLOW_SYNTHETIC_CONFIG') == '1'}")
    problems = verify_schema(r.schema())
    print("schema:", "OK" if not problems else problems)
    for case_id in filter(None, (args.case, args.other_case, args.nocfg_case)):
        case = case_row(case_id)
        versions = r.select("client_config_versions", {"client_id": case["client_id"]}, columns="version,status")
        state = SupabaseCaseStateStore.from_env(os.environ, provisional=InMemoryCaseStateStore())._row(case_id, None)
        print(f"case {case_id}: client {case['client_id']}, employee {case['employee_no']}, joining "
              f"{case['joining_date']}; config versions {versions}; UC02 rows {uc02_counts(case_id, case['client_id'])}; "
              f"case_orchestration_state {'present v' + str(state['version']) if state else 'none'}")
    return 0 if not problems else 1


def plan(args) -> int:
    require_write(args.confirm_write)
    case = case_row(args.case)
    client = case["client_id"]
    from onboarding_task_management import synthetic_profiles as sp

    profile = sp.PROFILES["uae_inside"]
    agent, api = build(case)
    body = {
        "offer_accepted": True, "offer_reference": f"OFR-UC02-VERIFY-{args.case[:8]}",
        "offer_acceptance_event_id": f"EVT-UC02-VERIFY-{args.case[:8]}", "candidate_name": "SYNTHETIC CANDIDATE",
        "candidate_email": "synthetic.candidate@example.invalid", "designation": "SYNTHETIC ENGINEER",
        "joining_date": case["joining_date"], "id_number": f"SYN-ID-{args.case[:8]}", "role_id": profile.role_id,
        "department": case.get("department") or "Engineering", "manager_id": "SYN-MGR-001",
        "location": profile.location, "project": case.get("project_name") or "synthetic-project",
        "grade": profile.grade, "nationality_group": profile.nationality_group,
        "attributes": [*profile.attributes, f"location_status:{profile.current_location_status}"],
        "offer_accepted_at": "2026-10-20T10:00:00Z",
    }
    print(f"before: UC02 rows {uc02_counts(args.case, client)}")
    view = ok(api.post(BASE, json=body, headers=headers(client, "user:hr-admin-uc02-verify", "hr_admin")))
    approval = view["form_approval"]["approval_id"]
    view = ok(api.post(f"{BASE}/{view['case_id']}/approvals/{approval}/decision", json={"decision": "approved"},
                       headers=headers(client, "user:hr-lead-uc02-verify", "hr_lead")))
    report("plan: form approved -> UC01 (bound case) -> UC02 plans in Supabase -> UC03 collection", view)
    print(f"after: UC02 rows {uc02_counts(args.case, client)}")
    return 0 if view["stage_statuses"]["plan_tasks"] == "complete" else 2


def resume(args) -> int:
    require_write(args.confirm_write)
    case = case_row(args.case)
    client = case["client_id"]
    hr = headers(client, "user:hr-admin-uc02-verify", "hr_admin")
    agent, api = build(case)
    view = ok(api.get(f"{BASE}/{args.case}", headers=hr))
    report("resume (new process): the case as Supabase holds it", view)
    assert agent.deps._uc02_sub_agent is None, "a fresh process has no UC02 in memory"

    # Duplicate execution: the plan_tasks stage replayed against the persisted state.
    before = uc02_counts(args.case, client)
    state = agent.get_case(args.case, client_id=client)
    replay = call_uc02_plan_tasks(state, agent.deps)
    after = uc02_counts(args.case, client)
    print(f"\n== duplicate UC02 execution: status {replay['stage_statuses']['plan_tasks']}, created "
          f"{(replay.get('task_plan') or {}).get('created')}; rows before {before} after {after} "
          f"-> {'NO DUPLICATES' if before['uc02_tasks'] == after['uc02_tasks'] and before['plan_revisions'] == after['plan_revisions'] and before['dependencies'] == after['dependencies'] else 'DUPLICATED'}")

    # Documents: a passport showing the holder needs a visa -> UC03 asks UC02 to replan -> revision 2.
    required = [d["document_type"] for d in view["document_collection"]["required_documents"]]
    view = ok(api.post(f"{BASE}/{args.case}/documents", json=demo._documents(required, visa_required=("passport",)),
                       headers=hr))
    report("documents (passport needs a visa): UC03 -> UC02 replans -> UC03 collects for the new revision", view)
    added = [d["document_type"] for d in view["document_collection"]["required_documents"] if d["document_type"] not in required]
    if added:
        view = ok(api.post(f"{BASE}/{args.case}/documents", json=demo._documents(added), headers=hr))
    reviews = [{"document_type": d["document_type"], "decision": "approved"}
               for d in view["document_collection"]["required_documents"]
               if (view.get("document_validation") or {}).get(d["document_type"], {}).get("classification") == "hr_review_required"]
    if reviews:
        view = ok(api.post(f"{BASE}/{args.case}/documents/reviews", json={"reviews": reviews}, headers=hr))
    report("after HR review: documents verified -> UC04 || UC05", view)
    print(f"   branches: { {b: s['status'] for b, s in view['branch_statuses'].items()} }  verified against plan "
          f"v{(view.get('verified_context') or {}).get('plan_version')}")
    print(f"   UC02 rows {uc02_counts(args.case, client)}")
    return 0


def branches(args) -> int:
    """NEW process: resume the case; UC04 || UC05 run from the persisted, verified state (stage stand-ins)."""
    require_write(args.confirm_write)
    case = case_row(args.case)
    client = case["client_id"]
    agent, api = build(case, uc04_stage_stand_in=True)
    view = ok(api.post(f"{BASE}/{args.case}/resume", headers=headers(client, "user:hr-admin-uc02-verify", "hr_admin")))
    report("branches (new process, resumed from Supabase): UC04 || UC05 -> merge -> day-one readiness", view)
    print(f"   branches: { {b: s['status'] for b, s in view['branch_statuses'].items()} }")
    for stage in ("equipment_access", "orientation_scheduling"):
        inputs = (view["stage_outcomes"].get(stage) or {}).get("inputs") or {}
        print(f"   {stage} received from the persisted state: plan v{inputs.get('plan_version')}, verified plan "
              f"v{inputs.get('verified_plan_version')}, documents verified {inputs.get('documents_verified')}, "
              f"task plan available {inputs.get('task_plan_available')}")
    print(f"   day_one_readiness: {view['stage_statuses'].get('day_one_readiness')}  pending actions: "
          f"{[a['action'] for a in view.get('pending_actions') or []]}")
    return 0


def negative(args) -> int:
    require_write(args.confirm_write)
    case = case_row(args.case)
    client = case["client_id"]
    agent, api = build(case)

    # Wrong tenant: another client's actor cannot read the workflow state or UC02's plan.
    other = case_row(args.other_case)
    response = api.get(f"{BASE}/{args.case}", headers=headers(other["client_id"], "user:hr-admin-other", "hr_admin"))
    print(f"\n== wrong tenant: GET as {other['client_id']} -> HTTP {response.status_code}")
    from onboarding_task_management.contracts import Actor, Role
    from onboarding_task_management.errors import DataIsolationError

    try:
        uc02_sub_agent(agent.deps).service.get(Actor(actor_id="u", role=Role.HR_OPS, client_id=other["client_id"]), args.case)
        print("   UC02 read across tenants: ALLOWED (unexpected)")
    except DataIsolationError as exc:
        print(f"   UC02 read across tenants: refused ({exc.message})")
    # A case of another tenant presented in this tenant's workflow is never planned.
    state = _state(args.other_case, client, "SYN-EMP-VERIFY-TENANT", case["joining_date"])
    before = uc02_counts(args.other_case, other["client_id"])
    result = call_uc02_plan_tasks(state, agent.deps)
    print(f"   {other['client_id']} case in a {client} workflow: plan_tasks {result['stage_statuses']['plan_tasks']} "
          f"{(result.get('stage_errors') or {}).get('plan_tasks', {}).get('code')}; UC02 rows of that case "
          f"{before} -> {uc02_counts(args.other_case, other['client_id'])}")

    # Missing configuration: a client with no approved configuration version.
    nocfg = case_row(args.nocfg_case)
    state = _state(args.nocfg_case, nocfg["client_id"], "SYN-EMP-VERIFY-NOCFG", nocfg["joining_date"] or "2026-12-01")
    before = uc02_counts(args.nocfg_case, nocfg["client_id"])
    result = call_uc02_plan_tasks(state, agent.deps)
    blockers = [b["code"] for b in result["blockers"] if b["stage"] == "plan_tasks"]
    print(f"\n== missing configuration ({nocfg['client_id']}): plan_tasks {result['stage_statuses']['plan_tasks']}, "
          f"error {result.get('error_code')}, blockers {blockers}")
    print(f"   detail: {(result.get('stage_errors') or {}).get('plan_tasks', {}).get('detail', '')[:200]}")
    print(f"   UC02 rows of that case {before} -> {uc02_counts(args.nocfg_case, nocfg['client_id'])} (nothing fabricated)")
    return 0


def _state(case_id: str, client_id: str, employee_id: str, joining: str) -> dict[str, Any]:
    """A workflow state whose intake completed (the facts UC01 verified), for running the plan_tasks stage itself."""
    actor = {"on_behalf_of": "user:hr-admin-uc02-verify", "roles": ["hr_admin"], "client_id": client_id}
    state = new_state(case_id=case_id, client_id=client_id, correlation_id=f"uc02-verify-{case_id[:8]}", actor=actor,
                      intake_trigger={"offer_accepted": True}, uc02_role_context={
                          "role_id": "eng", "department": "Engineering", "manager_id": "SYN-MGR-001",
                          "location": "Dubai", "project": "synthetic-project", "offer_accepted_at": "2026-10-20T10:00:00Z",
                          "form_approved_at": "2026-10-21T10:00:00Z", "approved_by_role": "hr_ops"})
    state.update(employee_id=employee_id, joining_date=str(joining)[:10])
    state["stage_statuses"][Stage.INTAKE.value] = StageStatus.COMPLETE.value
    return state


def verify(args) -> int:
    case = case_row(args.case)
    client = case["client_id"]
    r = rest()
    print(f"case {args.case} ({client}): UC02 rows {uc02_counts(args.case, client)}")
    for rev in r.select("plan_revisions", {"case_id": args.case, "client_id": client}, order_by="revision"):
        summary = rev.get("change_summary") or {}
        print(f"   revision {rev['revision']}: {summary.get('trigger_source')} (previous {summary.get('previous_revision')}),"
              f" added tasks {len(rev.get('added_task_ids') or [])}, applied by {rev.get('applied_by')}, "
              f"correlation {rev.get('correlation_id')}")
    events = r.select("audit_events", {"case_id": args.case, "client_id": client}, columns="action,outcome",
                      extra=[("action", "like.orchestration.*")])
    print(f"   UC02 orchestration audit: {[(e['action'], e['outcome']) for e in events]}")
    runs = r.select("agent_runs", {"case_id": args.case, "client_id": client}, columns="run_id,status,tool_calls,error_summary")
    print(f"   agent_runs: {[(x['run_id'][:24], x['status'], x['tool_calls'], x['error_summary']) for x in runs]}")
    row = SupabaseCaseStateStore.from_env(os.environ, provisional=InMemoryCaseStateStore())._row(args.case, client)
    if row:
        print(f"   case_orchestration_state v{row['version']} {row['workflow_status']}: "
              + ", ".join(f"{s}={v}" for s, v in row["stage_statuses"].items() if v != "pending"))
        for stage in ("plan_tasks", "replan_tasks"):
            outcome = (row.get("stage_outcomes") or {}).get(stage) or {}
            print(f"   stage_outcomes.{stage}.result_summary: {json.dumps(outcome.get('result_summary'))[:400]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=("preflight", "plan", "resume", "branches", "negative", "verify"))
    parser.add_argument("--case")
    parser.add_argument("--other-case")
    parser.add_argument("--nocfg-case")
    parser.add_argument("--confirm-write")
    args = parser.parse_args(argv)
    if args.step != "preflight" and not args.case:
        parser.error("--case is required")
    return {"preflight": preflight, "plan": plan, "resume": resume, "branches": branches, "negative": negative,
            "verify": verify}[args.step](args)


if __name__ == "__main__":
    sys.exit(main())
