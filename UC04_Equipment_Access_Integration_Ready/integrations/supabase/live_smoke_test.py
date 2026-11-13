"""LIVE smoke test of UC-04's Supabase connection with SYNTHETIC rows only (each marked UC04TEST-<id>). It writes to the real project, so it
needs an explicit flag:

    python live_smoke_test.py --yes-write-synthetic-rows

What it leaves behind: `audit_events` is append-only by design (the database refuses UPDATE and DELETE), so the audit rows of every run
stay, tagged with the run's tag. The synthetic case is soft-deleted (`is_deleted = true`); run rows are deleted. It touches no existing row.
"""

import sys
import uuid

import connect


def main() -> int:
    if "--yes-write-synthetic-rows" not in sys.argv:
        print("Refusing to write: pass --yes-write-synthetic-rows (see the docstring for what stays behind).")
        return 2
    client = connect.make_client()
    agent = connect.build_agent(state_mode="memory", client=client)  # fixes the identifier profile, then imports UC-04
    sys.path.insert(0, str(connect.UC04 / "tests"))
    from helpers import ALL, approval, make  # UC-04 synthetic test helpers

    tag = "UC04TEST-" + uuid.uuid4().hex[:10]
    case = str(uuid.uuid4())
    clients = client.select("client_configs", filters={"is_active": True}, columns=["client_id"], limit=1)
    client_id = clients[0]["client_id"]
    client.insert("onboarding_cases", {"case_id": case, "client_id": client_id, "employee_no": tag.replace("TEST", "EMP"), "candidate_name": tag,
                                       "candidate_email": f"{tag.lower()}@example.com", "hr_officer_id": "UC04TEST-HR", "manager_id": "UC04TEST-MGR",
                                       "role_id": "software_engineer", "joining_date": "2026-12-14", "work_location_type": "OFFICE",
                                       "onboarding_attributes": ["synthetic", tag], "status": "INTAKE_PENDING"})
    ok = False
    try:
        payload = make(case_id=case, employee_ref=tag.replace("TEST", "EMP"), client_id=client_id)
        payload["config"]["client_id"] = client_id
        payload["approval_records"] = [approval(k, client=client_id, request_id=f"{case}:SYN-REQ-{k}") for k in ALL]
        result = agent.evaluate(payload, actor_id="user:uc04-test", run_id=tag.lower(), idempotency_key=f"{tag.lower()}.1")
        events = client.select("audit_events", filters={"case_id": case}, columns=["action"])
        runs = client.select("agent_runs", filters={"case_id": case}, columns=["run_id", "status"])
        print("status:", result.overall_status.value, "| audit rows:", len(events), "| run rows:", len(runs))
        ok = result.overall_status.value == "IN_PROGRESS" and len(events) > 5 and len(runs) == 1
    finally:
        client.update("onboarding_cases", filters={"case_id": case}, values={"is_deleted": True})  # soft delete
        print("synthetic case soft-deleted; tag:", tag, "(its audit rows stay: append-only)")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
