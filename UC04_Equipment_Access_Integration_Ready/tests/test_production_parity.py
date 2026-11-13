"""The edge cases give the SAME results whether UC-04 runs on the mock wiring or on production-style wiring.

Mock wiring:        MockCaseContext + in-memory CaseStore + in-memory audit list.
Production wiring:  DatabaseCaseContext (reads onboarding_cases) + DatabaseCaseRepository over TableStateBackend (the UC-04 state tables)
                    + DatabaseAuditSink (audit_events) + DatabaseAgentRunRecorder (agent_runs).

The "database" here is an in-memory table client that enforces the real column names and required columns (from the audited schema), so a
payload the real tables would refuse is refused here. No network and no Supabase: this proves the business logic does not depend on the
mock, and that moving to a live database only swaps the injected adapters. Run in a subprocess because the production wiring needs the
PLATFORM identifier profile (uuid case ids), which is fixed once per process.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import sys, json, uuid
from datetime import datetime, timezone
sys.path[:0] = [r"%(src)s", r"%(tests)s", r"%(root)s"]
import guardrails.identity as identity
identity.configure("PLATFORM")
from helpers import ALL, approval, approvals, collection, delivery, make, ticket
from sub_agent.adapters.case_context_mock import MockCaseContext
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.agent import Uc04Agent
from sub_agent.persistence import (DatabaseAgentRunRecorder, DatabaseAuditSink, DatabaseCaseContext, DatabaseCaseRepository, InMemoryTableClient,
                                   TableSchema, TableStateBackend)
from sub_agent.ports import AdapterTimeout, ParentCase
from sub_agent.states import ParentCaseStatus
from test_client_change import alpha_round
import itertools
import sub_agent.fixtures as fixtures


def fresh(fn, *args):
    """The helpers number approval ids from a global counter: restart it so both wirings are given byte-identical payloads."""
    fixtures._apr = itertools.count(1)
    return fn(*args)


CLOCK = lambda: datetime(2026, 11, 4, tzinfo=timezone.utc)
FIX = json.load(open(r"%(fixture)s", encoding="utf-8"))["tables"]
SCHEMAS = {t: TableSchema(frozenset(v["columns"]), frozenset(v["required"]), tuple(v["primary_key"])) for t, v in FIX.items()}
SCHEMAS["uc04_case_state"] = TableSchema(frozenset({"case_id", "client_id", "employee_ref", "version", "record", "updated_at"}),
                                         frozenset({"case_id", "client_id", "employee_ref", "version", "record"}), ("case_id",))
SCHEMAS["uc04_replay_results"] = TableSchema(frozenset({"case_id", "idempotency_key", "payload_hash", "store_version", "parent_status", "result", "created_at"}),
                                             frozenset({"case_id", "idempotency_key", "payload_hash", "store_version", "parent_status", "result"}),
                                             ("case_id", "idempotency_key"))
CLIENT = "client_alpha"


def case_uuid(n=1):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"uc04-parity-{n}"))


def retarget(payload, n=1):
    text = json.dumps(payload).replace(f"SYN-CASE-{n:03d}", case_uuid(n))
    return json.loads(text)


def agents(case_numbers, desk_factory=MockITServiceDesk):
    parents = [(case_uuid(n), f"SYN-EMP-{n:03d}") for n in case_numbers]
    mock = Uc04Agent(cases=MockCaseContext([ParentCase(case_id=c, employee_ref=e, client_id=CLIENT) for c, e in parents]), it_desk=desk_factory(), clock=CLOCK)
    table = InMemoryTableClient(SCHEMAS, {"onboarding_cases": [{"case_id": c, "employee_no": e, "client_id": CLIENT, "status": "PROVISIONING_ACTIVE"} for c, e in parents]})
    prod = Uc04Agent(cases=DatabaseCaseContext(table), it_desk=desk_factory(), clock=CLOCK, store=DatabaseCaseRepository(TableStateBackend(table)),
                     audit_sink=DatabaseAuditSink(table), run_recorder=DatabaseAgentRunRecorder(table))
    return mock, prod, table


def ev(agent, payload, key="k"):
    return agent.evaluate(payload, actor_id="user:hr-admin-01", idempotency_key=key).model_dump(mode="json")


def sb(agent, payload):
    return agent.submit_eligible(payload, actor_id="user:hr-admin-01").model_dump(mode="json")


SITE = {"state": "VERIFIED", "value": {"required_items": [], "block_on_late_clearance": True, "owner_role": "account_manager"}}
COUNTRY = {"state": "VERIFIED", "value": {"country_code": "ZZ", "courier_extra_days": 5, "remote_first_day_required": True, "timezone": "Asia/Dubai",
                                          "weekend_days": [4, 5], "holidays": ["2026-11-02"]}}
EXTRA = {"request_key": "SYN-REQ-103", "item_id": "production-database-access", "item_kind": "ACCESS", "requested_by": "SYN-USER-201"}
TRANSFER = {"transfer_ref": "TRF-0001", "previous_role_id": "support_analyst",
            "held_access": [{"item_id": "email-teams-account", "item_kind": "ACCESS"}, {"item_id": "legacy-crm-access", "item_kind": "ACCESS"}],
            "held_equipment": [{"item_id": "laptop-standard", "item_kind": "EQUIPMENT", "asset_ref": "SYN-AST-0007"},
                               {"item_id": "old-scanner", "item_kind": "EQUIPMENT", "asset_ref": "SYN-AST-0008"}]}


def scenario_ec29(a):  return [ev(a, retarget(make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")], delivery_status=[delivery("201", "PENDING", expected="2026-12-01")])))]
def scenario_ec30(a):  return [ev(a, retarget(make(approval_records=approvals(["101"]), mock_it_ticket_updates=[ticket("101", "CREATED"), ticket("101", "REJECTED", reason="Licence not available")])))]
def scenario_ec31(a):
    std, exc = approval("103", role="hiring_manager", item="production-database-access"), approval("103", role="exception_approver", typ="EXCEPTION", item="production-database-access")
    p = lambda recs: retarget(make(requested_access=[EXTRA], requested_equipment=[], approval_records=recs))
    return [ev(a, p([]), "k1"), ev(a, p([std]), "k2"), ev(a, p([std, exc]), "k3"), sb(a, p([std, exc]))]
def scenario_ec32(a):  return [ev(a, retarget(make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")], delivery_status=[delivery("201")], collection_status=[collection("201", "MISSED_SLOT", next_slot="2026-11-09", evidence=None)])))]
def scenario_ec33(a):
    d = {**delivery("201", "FAULTY"), "asset_ref": "SYN-AST-0042", "assigned_model": "Model A", "reported_model": "Model A", "fault_note": "screen flickers"}
    return [ev(a, retarget(make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")], delivery_status=[d])))]
def scenario_ec34(a):  return [ev(a, retarget(make(client_site_required=True, config={"client_site_rules": SITE}, as_of_date="2026-11-09", client_site_clearance={"status": "PENDING"})))]
def scenario_ec35(a):
    first, second = alpha_round(), alpha_round()
    first["project_id"], second["project_id"] = "PRJ-ONE", "PRJ-TWO"
    return [ev(a, retarget(first), "p1"), ev(a, retarget(second), "p2")]
def scenario_ec36(a):  return [ev(a, retarget(make(work_mode="REMOTE", config={"country_rules": COUNTRY}, approval_records=approvals(ALL), as_of_utc="2026-11-05T21:00:00+00:00")))]
def scenario_ec42(a):
    return [ev(a, retarget(make(transfer=TRANSFER, internal_transfer=True, requested_equipment=[], approval_records=approvals(["102"]),
                                requested_access=[{"request_key": "SYN-REQ-102", "item_id": "source-control-access", "item_kind": "ACCESS", "requested_by": "SYN-USER-201"}])))]
def scenario_submit(a):
    payload = retarget(make(approval_records=approvals(ALL)))
    return [ev(a, payload, "k1"), sb(a, payload), sb(a, payload), ev(a, payload, "k2")]
def scenario_adapter_failure(a):
    payload = retarget(make(approval_records=approvals(["201"])))
    first, second = sb(a, payload), sb(a, payload)
    from sub_agent.orchestrated import Actor, CaseOperationRequest, WorkflowRef
    op = CaseOperationRequest(run_id="ack-1", workflow=WorkflowRef(id="employee-onboarding", instance=case_uuid(1), step="equipment_access"),
                              actor=Actor(on_behalf_of="user:hr-admin-01", roles=["hr_onboarding_officer"]), client_id=CLIENT,
                              request_id=case_uuid(1) + ":SYN-REQ-201")
    ack = a.acknowledge_orchestrated(op).model_dump(mode="json")  # the real entry point: it carries the actor into the audit event
    a.it_desk.fail_with = None
    return [first, second, ack, sb(a, payload)]


SCENARIOS = {"EC-29": scenario_ec29, "EC-30": scenario_ec30, "EC-31": scenario_ec31, "EC-32": scenario_ec32, "EC-33": scenario_ec33,
             "EC-34": scenario_ec34, "EC-35": scenario_ec35, "EC-36": scenario_ec36, "EC-42": scenario_ec42, "submit-replay": scenario_submit}
out = {}
for name, fn in SCENARIOS.items():
    mock, prod, table = agents([1])
    same = fresh(fn, mock) == fresh(fn, prod)
    out[name] = {"same": same, "audit_rows": len(table.tables.get("audit_events", [])), "runs": len(table.tables.get("agent_runs", [])),
                 "state_rows": len(table.tables.get("uc04_case_state", []))}

mock, prod, table = agents([1], desk_factory=lambda: MockITServiceDesk(fail_with=AdapterTimeout("x")))
out["adapter-failure-ack-retry"] = {"same": fresh(scenario_adapter_failure, mock) == fresh(scenario_adapter_failure, prod), "audit_rows": len(table.tables.get("audit_events", [])),
                                    "runs": len(table.tables.get("agent_runs", [])), "state_rows": len(table.tables.get("uc04_case_state", []))}

mock, prod, table = agents([1])
def withdrawn(a, is_prod):
    first = ev(a, retarget(make(approval_records=approvals(ALL))), "w1")
    sb(a, retarget(make(approval_records=approvals(ALL))))
    if is_prod:
        table.tables["onboarding_cases"][0]["status"] = "WITHDRAWN"
    else:
        a.cases.set_status(case_uuid(1), ParentCaseStatus.WITHDRAWN)
    return [first, ev(a, retarget(make(approval_records=approvals(ALL))), "w2")]
out["withdrawal"] = {"same": fresh(withdrawn, mock, False) == fresh(withdrawn, prod, True), "audit_rows": len(table.tables.get("audit_events", [])),
                     "runs": len(table.tables.get("agent_runs", [])), "state_rows": len(table.tables.get("uc04_case_state", []))}

# EC-40: a cohort of 6 people, one case each
numbers = list(range(1, 7))
mock, prod, table = agents(numbers)
def cohort(a):
    payloads = [retarget(make("alpha", n=i, approval_records=approvals(ALL, n=i)), n=i) for i in numbers]
    payloads[2]["offer_accepted"] = False
    return a.evaluate_cohort("COH-PARITY", payloads, actor_id="user:hr-admin-01").model_dump(mode="json")
out["EC-40"] = {"same": fresh(cohort, mock) == fresh(cohort, prod), "audit_rows": len(table.tables.get("audit_events", [])), "runs": len(table.tables.get("agent_runs", [])),
                "state_rows": len(table.tables.get("uc04_case_state", []))}
print(json.dumps(out))
'''


def test_every_edge_case_gives_identical_results_on_mock_and_production_style_wiring():
    script = SCRIPT % {"src": ROOT / "src", "tests": ROOT / "tests", "root": ROOT, "fixture": ROOT / "tests" / "fixtures" / "live_schema_subset.json"}
    proc = subprocess.run([sys.executable, "-c", textwrap.dedent(script)], capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert proc.returncode == 0, proc.stderr[-1500:]
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    assert set(results) == {"EC-29", "EC-30", "EC-31", "EC-32", "EC-33", "EC-34", "EC-35", "EC-36", "EC-42", "EC-40", "submit-replay",
                            "adapter-failure-ack-retry", "withdrawal"}
    different = [name for name, r in results.items() if not r["same"]]
    assert different == [], different
    for name, r in results.items():  # the production wiring really persisted: audit rows, run rows and the UC-04 state row
        assert r["audit_rows"] > 0 and r["runs"] > 0 and r["state_rows"] >= 1, (name, r)
    assert results["EC-40"]["state_rows"] == 5  # one stored case per person who progressed, never a shared one; the person held at the start gate stored nothing
