"""Run UC-04 on a SYNTHETIC case and store what it produces in Supabase, through the UC-04 connector only.

    python run_uc04_and_store.py --yes-write-synthetic-rows

Stored in the database: the synthetic parent case (`onboarding_cases`), every UC-04 audit event (`audit_events`) and the run records
(`agent_runs`). The full result is also saved locally under ./output/. UC-04's own state tables (`uc04_case_state`, `uc04_replay_results`)
receive the full result and item states only once the reviewed migration is applied and the script is run with --state-mode supabase.
The case is left in place (tagged UC04TEST-..., synthetic) so the output can be seen; `audit_events` is append-only and cannot be cleaned up.
"""

import json
import sys
import uuid
from pathlib import Path

import connect


def main() -> int:
    if "--yes-write-synthetic-rows" not in sys.argv:
        print("Refusing to write: pass --yes-write-synthetic-rows")
        return 2
    mode = sys.argv[sys.argv.index("--state-mode") + 1] if "--state-mode" in sys.argv else "memory"
    client = connect.make_client()
    agent = connect.build_agent(state_mode=mode, client=client)
    sys.path.insert(0, str(connect.UC04 / "tests"))
    from helpers import ALL, approval, make  # UC-04 synthetic helpers

    tag = "UC04TEST-" + uuid.uuid4().hex[:10]
    case, emp = str(uuid.uuid4()), tag.replace("TEST", "EMP")
    client_id = client.select("client_configs", filters={"is_active": True}, columns=["client_id"], limit=1)[0]["client_id"]
    client.insert("onboarding_cases", {"case_id": case, "client_id": client_id, "employee_no": emp, "candidate_name": tag,
                                       "candidate_email": f"{tag.lower()}@example.com", "hr_officer_id": "UC04TEST-HR", "manager_id": "UC04TEST-MGR",
                                       "role_id": "software_engineer", "joining_date": "2026-12-14", "work_location_type": "OFFICE",
                                       "onboarding_attributes": ["synthetic", tag], "status": "INTAKE_PENDING"})
    payload = make(case_id=case, employee_ref=emp, client_id=client_id)
    payload["config"]["client_id"] = client_id
    payload["approval_records"] = [approval(k, client=client_id, request_id=f"{case}:SYN-REQ-{k}") for k in ALL]

    run = tag.lower()
    first = agent.evaluate(payload, actor_id="user:uc04-run", run_id=run, idempotency_key=f"{run}.1")
    submitted = agent.submit_eligible(payload, actor_id="user:uc04-run")
    final = agent.evaluate(payload, actor_id="user:uc04-run", run_id=run + "-after", idempotency_key=f"{run}.2")

    print(f"\nCASE   {case}   (client {client_id}, tag {tag})")
    print(f"RUN 1  overall={first.overall_status.value}  reason={first.readiness_reason[:90]}")
    print(f"SUBMIT {len(submitted.submitted)} request(s) sent to the MOCK IT desk; failed={len(submitted.failed)} skipped={len(submitted.skipped)}")
    for i in final.items:
        print(f"  {i.request_id.split(':')[1]:<14} {i.item_id:<22} {i.state.value:<28} ticket={i.ticket_id}")
    print(f"RUN 2  overall={final.overall_status.value}  proposals={[p.proposal_type for p in final.proposals]}")

    events = client.select("audit_events", filters={"case_id": case}, columns=["action", "outcome", "actor_type"])
    runs = client.select("agent_runs", filters={"case_id": case}, columns=["run_id", "status", "proposal_count", "action_count"])
    print(f"\nSTORED IN SUPABASE: {len(events)} audit events, {len(runs)} run records, 1 case row")
    for r in runs:
        print(f"  agent_runs  {r['run_id']:<42} status={r['status']} proposals={r['proposal_count']} actions={r['action_count']}")
    if mode == "supabase":
        state = client.select("uc04_case_state", filters={"case_id": case}, columns=["version"])
        print(f"  uc04_case_state version={state[0]['version']} ; uc04_replay_results rows="
              f"{len(client.select('uc04_replay_results', filters={'case_id': case}, columns=['idempotency_key']))}")

    out = Path(__file__).resolve().parent / "output"
    out.mkdir(exist_ok=True)
    (out / f"{tag}.json").write_text(json.dumps({"case_id": case, "client_id": client_id, "first": first.model_dump(mode="json"),
                                                  "after_submission": final.model_dump(mode="json")}, indent=1), encoding="utf-8")
    print(f"\nfull result saved: output/{tag}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
