"""UC-02 against the shared Supabase project: read-only preflight by default, one approved write on request.

    python demo/uc02_supabase_live.py --case-id <uuid>                       # preflight only, writes nothing
    python demo/uc02_supabase_live.py --case-id <uuid> --execute --confirm <uuid>   # the approved write

Requires ``SUPABASE_URL`` (or ``NEXT_PUBLIC_SUPABASE_URL``) and ``SUPABASE_SERVICE_ROLE_KEY`` in the
environment or in this folder's ``.env``; neither value is ever printed. ``UC02_STORE_MODE`` is forced
to ``supabase``: there is no memory or SQLite fallback.

What it never does: write ``onboarding_cases`` or any UC-01 / other-use-case row, create a case, delete
anything it does not own, or change schema, RLS or triggers.

The client configuration used is SYNTHETIC and in-process (``SYNTHETIC-UC02-LIVE-TEST``). It is not the
client's approved configuration; the live ``task_templates`` and ``client_*`` tables are empty. The offer
and form facts are supplied by this harness, because the live ``onboarding_forms`` table holds no row for
the case.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onboarding_task_management.agent import UC02Agent  # noqa: E402
from onboarding_task_management.contracts import (  # noqa: E402
    Actor,
    DecisionOutcome,
    Role,
    TaskCategory,
)
from onboarding_task_management.persistence import PersistenceError, store_from_env  # noqa: E402
from onboarding_task_management.sub_agent.checklist import (  # noqa: E402
    ClientOnboardingConfig,
    ConfigRegistry,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import OnboardingService, OnboardingTrigger  # noqa: E402
from onboarding_task_management.supabase_rest import SupabaseGateway, SupabaseRestClient  # noqa: E402
from onboarding_task_management.synthetic import accepted_offer, approved_form  # noqa: E402

CONFIG_LABEL = "SYNTHETIC-UC02-LIVE-TEST"
REQUESTER_ID = "uc02-test-hr-requester"
APPROVER_ID = "uc02-test-hr-approver"
UC02_TABLES = ("onboarding_tasks", "task_dependencies", "approval_requests", "audit_events", "agent_runs")


def load_env() -> dict[str, str]:
    from onboarding_task_management import envfile

    envfile.load_environment()
    env = dict(os.environ)
    env["UC02_STORE_MODE"] = "supabase"
    return env


def synthetic_config(client_id: str) -> ClientOnboardingConfig:
    def t(key, category, offset, deps=()):
        return TaskTemplate(key=key, category=category, title=f"[synthetic] {key}", title_ar="x",
                            owner=Role.HR_OPS, offset_days=offset, depends_on=deps)

    return ClientOnboardingConfig(
        client_id=client_id, version=CONFIG_LABEL, approval_status="approved",
        templates=(
            t("hr_record_create", TaskCategory.HR_RECORD, -5),
            t("background_verification", TaskCategory.BACKGROUND_VERIFICATION, -4, ("hr_record_create",)),
            t("medical_test", TaskCategory.MEDICAL, -4),
            t("laptop_request", TaskCategory.IT_PROVISIONING, -3, ("hr_record_create",)),
            t("orientation_slot", TaskCategory.ORIENTATION, -1, ("laptop_request",)),
        ))


def trigger_from_case(case: dict) -> OnboardingTrigger:
    employee = case["employee_no"]
    return OnboardingTrigger(
        client_id=case["client_id"], employee_id=employee, joining_date=date.fromisoformat(case["joining_date"]),
        role_id=case["role_id"], department=case["department"], manager_id=case["manager_id"],
        location=case["location_name"], project=case["project_name"], grade=case["grade"],
        nationality_group=case["nationality_group"], attributes=tuple(case["onboarding_attributes"] or ()),
        case_id=case["case_id"], offer=accepted_offer(employee), form=approved_form(employee))


def build_service(env: dict[str, str], client_id: str, store) -> OnboardingService:
    registry = ConfigRegistry()
    registry.register(synthetic_config(client_id))
    return OnboardingService(registry, store=store, require_plan_approval=True)


def preflight(env: dict[str, str], case_id: str) -> tuple[dict, list[str]]:
    rest = SupabaseRestClient.from_env(env)
    gateway = SupabaseGateway(rest)
    problems: list[str] = []
    cases = [c for c in rest.select("onboarding_cases", {"case_id": case_id}, columns="case_id,client_id")]
    if not cases:
        raise SystemExit(f"case {case_id} does not exist; nothing to do")
    client_id = cases[0]["client_id"]
    case = gateway.get_case(client_id, case_id)
    if not rest.select("client_configs", {"client_id": client_id}, columns="client_id"):
        problems.append(f"client {client_id} is not in client_configs")
    if not case.get("employee_no"):
        problems.append("the case has no employee_no (UC-02 needs it to prevent duplicates)")
    live_tasks = rest.select("onboarding_tasks", {"case_id": case_id}, columns="task_id,template_key,status_summary")
    foreign = [r for r in live_tasks if "uc02" not in (r.get("status_summary") or {})]
    keys = {t.key for t in synthetic_config(client_id).templates}
    clashes = sorted(r["template_key"] for r in foreign if r["template_key"] in keys)
    if clashes:
        problems.append(f"template keys clash with other use cases' tasks: {clashes}")
    if rest.select("audit_events", {"case_id": case_id, "action": "UC02_CASE_STATE"}, columns="event_id"):
        problems.append("UC-02 has already stored state for this case (a re-run would add to it)")
    return {"case": case, "foreign_tasks": len(foreign), "uc02_tasks": len(live_tasks) - len(foreign)}, problems


def describe(case: dict) -> None:
    """Plan offline (no network writes) so the exact operation can be shown before approval."""
    from onboarding_task_management.persistence import InMemoryTableClient, OnboardingStore

    store = OnboardingStore(InMemoryTableClient())
    service = build_service({}, case["client_id"], store)
    hr = Actor(actor_id=REQUESTER_ID, role=Role.HR_OPS, client_id=case["client_id"])
    record = service.create_onboarding(hr, trigger_from_case(case))
    edges = [(t.template_key, d.split("#")[-1]) for t in record.tasks for d in t.depends_on]
    print(f"  onboarding_tasks     : {len(record.tasks)} rows -> " + ", ".join(t.template_key for t in record.tasks))
    print(f"  task_dependencies    : {len(edges)} rows -> " + ", ".join(f"{a}<-{b}" for a, b in edges))
    print("  approval_requests    : 1 row (plan_approval, requested by requester, decided by a different approver)")
    print("  agent_runs           : 1 row (Fake LLM advisor, offline)")
    print("  audit_events         : the UC-02 audit events plus UC02_CASE_STATE snapshots (append-only)")


def execute(env: dict[str, str], case: dict) -> None:
    client_id, case_id = case["client_id"], case["case_id"]
    store = store_from_env(env)  # supabase only; raises, never falls back
    service = build_service(env, client_id, store)
    hr = Actor(actor_id=REQUESTER_ID, role=Role.HR_OPS, client_id=client_id)
    approver = Actor(actor_id=APPROVER_ID, role=Role.HR_OPS, client_id=client_id)
    today = date.today()

    record = service.create_onboarding(hr, trigger_from_case(case))
    print(f"[1] created onboarding {record.onboarding_id[:8]}... with {len(record.tasks)} tasks")
    request = service.submit_plan(hr, record.onboarding_id)
    service.decide_plan(approver, request.request_id, DecisionOutcome.APPROVED)
    print("[2] plan submitted by requester and approved by a different approver")
    first = service.get(hr, record.onboarding_id).task_by_key("hr_record_create")
    service.assign_owner(hr, record.onboarding_id, first.task_id, "uc02-test-owner")
    service.start_task(hr, record.onboarding_id, first.task_id)
    print("[3] assigned and started one task")
    run = UC02Agent(service, hr, store=store).run(record.onboarding_id, today)
    print(f"[4] agent run recorded: {run.run_id}")
    try:
        service.create_onboarding(hr, trigger_from_case(case))
        print("[5] DUPLICATE ACCEPTED (unexpected)")
    except Exception as exc:  # the expected outcome
        print(f"[5] duplicate create refused: {type(exc).__name__}")
    print("done; re-run with --verify to read back after a restart")


def verify(env: dict[str, str], case: dict) -> None:
    client_id, case_id = case["client_id"], case["case_id"]
    rest = SupabaseRestClient.from_env(env)
    store = store_from_env(env)
    fresh = build_service(env, client_id, store)  # a brand-new process-equivalent service
    hr = Actor(actor_id=REQUESTER_ID, role=Role.HR_OPS, client_id=client_id)
    count = fresh.hydrate(client_id)
    record = fresh.get(hr, case_id)
    print(f"restart: hydrated {count} onboarding(s); case {case_id[:8]}... plan_status={record.plan_status.value}, "
          f"{len(record.tasks)} tasks, {len(fresh.audit.events_for(hr))} audit events recovered")
    rows = {
        "onboarding_tasks": rest.select("onboarding_tasks", {"case_id": case_id}, columns="task_id,status"),
        "task_dependencies": [e for t in record.tasks for e in rest.select("task_dependencies", {"task_id": t.task_id})],
        "approval_requests": rest.select("approval_requests", {"case_id": case_id}, columns="request_id,outcome"),
        "agent_runs": rest.select("agent_runs", {"case_id": case_id}, columns="run_id,status"),
        "audit_events": rest.select("audit_events", {"case_id": case_id}, columns="event_id,action"),
    }
    for table, found in rows.items():
        print(f"  {table:18} {len(found)} rows in Supabase")
    ids = [r["task_id"] for r in rows["onboarding_tasks"]]
    print("  duplicate task rows:", len(ids) - len(set(ids)))
    print("  UC-01 case row untouched: this script never issues a write to onboarding_cases")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--execute", action="store_true", help="perform the approved write")
    parser.add_argument("--confirm", help="must equal --case-id when --execute is used")
    parser.add_argument("--verify", action="store_true", help="read back after a restart (read-only)")
    args = parser.parse_args()
    env = load_env()
    try:
        info, problems = preflight(env, args.case_id)
        case = info["case"]
        print(f"backend: supabase (no fallback) | case {case['case_id']} | client {case['client_id']} | "
              f"employee_no {case['employee_no']}")
        print(f"existing tasks on the case: {info['foreign_tasks']} owned by other use cases, "
              f"{info['uc02_tasks']} owned by UC-02")
        print(f"config: {CONFIG_LABEL} (synthetic, in-process)")
        if args.verify:
            verify(env, case)
            return 0
        print("planned writes (UC-02 tables only; nothing else is touched):")
        describe(case)
        if problems:
            print("BLOCKED:", *problems, sep="\n  - ")
            return 2
        if not args.execute:
            print("preflight clean; nothing written (dry run)")
            return 0
        if args.confirm != args.case_id:
            print("refusing to write: --confirm must equal --case-id")
            return 2
        execute(env, case)
        return 0
    except PersistenceError as exc:
        print(f"PERSISTENCE ERROR: {exc} {getattr(exc, 'context', '')}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
