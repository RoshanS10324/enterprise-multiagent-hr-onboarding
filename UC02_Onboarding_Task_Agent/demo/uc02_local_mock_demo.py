"""UC-02 ISOLATED LOCAL MOCK DEMONSTRATION - the whole workflow in one command, touching no real system.

    python demo\\uc02_local_mock_demo.py              # compact report
    python demo\\uc02_local_mock_demo.py --verbose    # also dump every stage's full result

What is real and what is not
----------------------------
REAL: the actual UC-02 code - runtime gate, Supabase adapter, configuration reader, planner, Fake-LLM review, signed event/callback/document
channels, approval flow, document gate, task tracker, monitoring sweep, recovery. These are the same stage functions the Supabase
Development demo uses (``sub_agent/supabase_demo.py``); nothing here re-implements business logic.
MOCK: the database is an in-process stand-in for Supabase's REST API that enforces the live schema's columns, NOT NULLs, keys and CHECKs
(``tests/test_supabase_store.FakePostgrest``); employee, client configuration and documents are synthetic; the review comes from the Fake LLM;
Outlook, UC-03/UC-04/UC-05 are mocks. Every secret is generated in memory for this run and never stored or printed.

Isolation guarantees
--------------------
* ``.env`` / ``.env.local`` are NOT read (the loader is disabled) and the real Supabase variables are ignored: the environment is built here.
* Network access is blocked for the whole process, so a mistaken real call would fail instead of reaching anything.
* No file is written. Each run starts from an empty database, so it is repeatable.
Exit code 0 only if every check at the end passes.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
from datetime import date, timedelta
from pathlib import Path

os.environ["UC02_DISABLE_DOTENV"] = "1"  # the central loader becomes a no-op: no real .env or .env.local is read

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def _blocked(*_a, **_k):
    raise OSError("network access is blocked in the isolated local demo")


socket.socket.connect = _blocked  # type: ignore[method-assign]
socket.socket.connect_ex = _blocked  # type: ignore[method-assign]
socket.create_connection = _blocked  # type: ignore[assignment]

from onboarding_task_management.sub_agent import supabase_demo as demo  # noqa: E402
from onboarding_task_management.supabase_rest import SupabaseRestClient  # noqa: E402
from onboarding_task_management.supabase_synthetic import FixtureSpec  # noqa: E402
from tests.test_supabase_store import FakePostgrest  # noqa: E402

HOST = "local-mock.invalid"
ENV = {  # built here; the real environment is never consulted
    "UC02_STORE_MODE": "supabase", "UC02_ENV": "development", "UC02_ALLOW_FIXTURE_WRITES": "1",
    "SUPABASE_URL": f"https://{HOST}", "SUPABASE_SERVICE_ROLE_KEY": "local-mock-not-a-real-key",
    **{name: secrets.token_urlsafe(32) for name in (
        "UC02_APPROVAL_CALLBACK_SECRET", "UC02_EVENT_SECRET", "UC02_DOCUMENTS_SECRET", "UC02_UC04_STATUS_SECRET",
        "UC02_UC05_STATUS_SECRET")},
}
UC01_TABLES = ("onboarding_cases", "offers", "onboarding_forms", "offer_forms")
CHECKS: list[tuple[str, bool]] = []


def short(task_id: str) -> str:
    return task_id.split("#", 1)[-1]


def check(name: str, ok: bool) -> None:
    CHECKS.append((name, bool(ok)))


def stage(title: str) -> None:
    print(f"\n=== {title}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verbose", action="store_true", help="dump each stage's full result as JSON")
    args = parser.parse_args()

    def dump(label: str, result) -> None:
        if args.verbose:
            print(f"--- {label} (full result)\n{json.dumps(result, indent=2, default=str)}")

    db = FakePostgrest()
    rest = SupabaseRestClient(f"https://{HOST}", ENV["SUPABASE_SERVICE_ROLE_KEY"], transport=db)
    spec = FixtureSpec()
    print("UC-02 ISOLATED LOCAL MOCK DEMO - synthetic data, Fake LLM, mock Outlook, mock documents, mock UC-04/UC-05.")
    print("Database: in-process stand-in (no Supabase, no network, no files). Secrets: generated in memory, never shown.")

    stage("1. Synthetic fixture (UC-01 case, offer, form; client configuration; templates; holidays)")
    fixture = demo.setup(ENV, rest=rest, spec=spec, confirm_target=HOST)
    print("  rows created:", {t: v for t, v in fixture["fixture"].items()} if isinstance(fixture["fixture"], dict) else fixture["fixture"])
    uc01_before = {t: [dict(r) for r in rest.select(t, {})] for t in UC01_TABLES}

    stage("2. UC-01 event -> configuration -> dynamic checklist -> Fake LLM review -> HR review email (mock Outlook)")
    ran = demo.run(ENV, rest=rest, spec=spec, mock_downstream=True)
    wf = ran["workflow"]
    flow = demo.make_flow(ENV, rest=rest, mock_downstream=True)
    coordinator = flow._actor(spec.client_id)
    flow.service.hydrate(spec.client_id)
    record = flow.service.get(coordinator, spec.case_id)
    review = record.analyses.get("p1", {})
    print(f"  event: {ran['event']}; config version {wf['config_version']}; data origin: {wf['data_origin']}")
    print(f"  checklist: {len(record.tasks)} tasks generated from the client configuration; plan status: {wf['plan_status']}")
    print(f"  Fake LLM review: source={review.get('source')} advisory_only={review.get('advisory_only')}")
    print(f"    summary: {str(review.get('checklist_summary', ''))[:150]}")
    print(f"  HR review email prepared (mock Outlook): {any(m.kind == 'hrmail' for m in record.messages)}; nothing sent")
    dump("run", ran)
    check("event accepted and plan waits for HR (not auto-approved)", wf["plan_status"] == "submitted")
    check("Fake LLM review stored, labelled, advisory only", bool(review) and review.get("advisory_only") is True)
    check("HR review email draft prepared", any(m.kind == "hrmail" for m in record.messages))

    stage("3. HR decision through the SIGNED approval callback (simulated approver)")
    decided = demo.decide(ENV, rest=rest, spec=spec, decision="approved", mock_downstream=True)
    print(f"  decision approved -> callback outcome: {decided['outcome']}; plan status: {decided['workflow']['plan_status']};"
          f" assignments prepared: {decided['workflow']['assignments']}")
    print(f"  downstream after approval, before documents: {decided['workflow']['downstream']}")
    dump("decide", decided)
    check("approval applied and plan approved", decided["workflow"]["plan_status"] == "approved")
    check("team assignments prepared", decided["workflow"]["assignments"] > 3)
    check("downstream still waiting for documents (mandatory gate closed)",
          set(decided["workflow"]["downstream"].values()) == {"waiting_for_documents"})

    stage("4. Authorised sends through the connector (mock Outlook; nothing leaves this process)")
    sent = demo.send_authorised(ENV, rest=rest, spec=spec, mock_downstream=True)
    kinds: dict[str, int] = {}
    for m in sent["messages"].values():
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    print(f"  messages processed: {kinds}; connector: {next(iter(sent['messages'].values()))['connector']};"
          f" real: {any(m['real'] for m in sent['messages'].values())}")
    dump("send", sent)
    check("every message went through the MOCK connector", sent["messages"] and not any(m["real"] for m in sent["messages"].values()))

    stage("5. Mock document verification -> mandatory gate -> mock UC-04 / UC-05 triggers")
    docs = demo.documents(ENV, rest=rest, spec=spec, mock_downstream=True)
    print(f"  document states: {docs['workflow']['documents']}; gate open: {docs['gate'].get('open')}")
    print(f"  downstream: {docs['workflow']['downstream']}  (MOCK adapters)")
    dump("documents", docs)
    check("mandatory documents verified and gate open", docs["gate"].get("open") is True)
    check("UC-04 and UC-05 triggered once through mock adapters",
          docs["workflow"]["downstream"] == {"UC-04": "triggered", "UC-05": "triggered"})

    stage("6. Task execution: parallel waves, dependency unblocking, mock access provisioning")
    first = demo.tasks(ENV, rest=rest, spec=spec, mock_downstream=True, waves=2)
    for wave in first["waves"]:
        print(f"  wave {wave['wave']}: " + "; ".join(f"{short(t['task'])} [{t['via'].split(' status')[0]}]" for t in wave["parallel_tasks"]))
    print(f"  ready together now: {[short(t) for t in first['after']['ready']]}")
    print(f"  still blocked: { {short(k): [short(x) for x in v] for k, v in first['after']['blocked'].items()} }")
    dump("tasks (2 waves)", first)
    ids = [t["task"] for w in first["waves"] for t in w["parallel_tasks"]]
    check("independent tasks ran in parallel waves", any(len(w["parallel_tasks"]) > 1 for w in first["waves"]))
    check("completing prerequisites unblocked dependants", bool(first["newly_unblocked"]))
    check("dependants stayed blocked until prerequisites were done", all(k not in ids for k in first["after"]["blocked"]))
    check("access provisioning was a labelled MOCK UC-04 update",
          any("MOCK uc04" in t["via"] for w in first["waves"] for t in w["parallel_tasks"]))

    stage("7. Due dates, overdue detection and the monitoring sweep (reminder DRAFTS only)")
    far = max(date.fromisoformat(d) for d in first["due_dates"].values()) + timedelta(days=3)
    late = demo.tasks(ENV, rest=rest, spec=spec, mock_downstream=True, waves=0, as_of=far)
    again = demo.tasks(ENV, rest=rest, spec=spec, mock_downstream=True, waves=0, as_of=far)
    print(f"  simulated date {far}: overdue tasks {[short(t) for t in late['overdue_at_as_of']]}")
    print(f"  sweep: {late['sweep']['escalations']} escalation records, {late['sweep']['reminder_drafts']} reminder drafts; {late['sweep']['delivery']}")
    print(f"  repeating the sweep recorded {again['sweep']['newly_recorded_escalations'] + again['sweep']['newly_recorded_reminders']} new items")
    print("  There is NO scheduler: the sweep runs only when called; reminders are never sent automatically.")
    dump("overdue sweep", late)
    check("open tasks are reported overdue after their due dates", bool(late["overdue_at_as_of"]))
    check("sweep produced reminder drafts / escalations, nothing delivered",
          (late["sweep"]["escalations"] or late["sweep"]["reminder_drafts"]) and "nothing was sent" in late["sweep"]["delivery"])
    check("repeating the sweep recorded nothing new", again["sweep"]["newly_recorded_escalations"] == 0
          and again["sweep"]["newly_recorded_reminders"] == 0)

    stage("8. Run the remaining waves to completion (synthetic case only)")
    rest_of = demo.tasks(ENV, rest=rest, spec=spec, mock_downstream=True, waves=10)
    print(f"  further waves: {[[short(t['task']) for t in w['parallel_tasks']] for w in rest_of['waves']]}")
    print(f"  task states now: {rest_of['workflow']['tasks']}")
    dump("remaining waves", rest_of)
    check("every task completed, none blocked", rest_of["workflow"]["tasks"]["complete"] == len(rest_of["due_dates"])
          and not rest_of["after"]["blocked"])
    idle = demo.tasks(ENV, rest=rest, spec=spec, mock_downstream=True, waves=10)
    check("running the task stage again changes nothing", idle["waves"] == [])

    stage("9. Restart recovery and duplicate-event idempotency")
    before = demo.verify(ENV, rest=rest, spec=spec)
    recovered = demo.recover(ENV, rest=rest, spec=spec, mock_downstream=True)
    after = demo.verify(ENV, rest=rest, spec=spec)
    print(f"  new runtime reloaded the workflow: stage={recovered['recovered_workflow']['stage']}, tasks={recovered['recovered_workflow']['tasks']}")
    print(f"  replayed UC-01 event: {recovered['event_replay']}; tasks {recovered['tasks_before_after']}, approvals "
          f"{recovered['approvals_before_after']}, assignments {recovered['assignments_before_after']}")
    dump("recover", recovered)
    check("replayed event created nothing (duplicate)", recovered["event_replay"] == "duplicate" and not recovered["duplicate_tasks"])
    check("stored state identical after recovery", before["task_status_counts"] == after["task_status_counts"]
          and before["documents"] == after["documents"] and before["messages"] == after["messages"])

    stage("10. Read-back of what is stored (mock database)")
    print(f"  tasks stored: {after['tasks_stored']} {after['task_status_counts']}; dependencies: {after['dependencies_stored']}; "
          f"approvals: {after['approval_requests']}")
    print(f"  assignments: {after['assignments']}; audit events: {after['audit_events']}; state snapshots: {after['case_state_snapshots']}")
    uc01_after = {t: [dict(r) for r in rest.select(t, {})] for t in UC01_TABLES}
    check("UC-01 owned tables were never modified by UC-02", uc01_before == uc01_after)
    check("data is marked synthetic", after["data_origin"] == "synthetic")

    stage("CHECKS")
    for name, ok in CHECKS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    failed = [n for n, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed. Mocked: database stand-in, Fake LLM, Outlook, documents, UC-04/UC-05.")
    print("Nothing real was contacted, written or sent." if not failed else f"FAILED: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
