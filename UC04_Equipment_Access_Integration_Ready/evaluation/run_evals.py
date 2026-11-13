"""Scenario evaluation: actual UC-04 output is compared with hand-authored expectations in expected_outputs.json.

Every scenario runs through BOTH entry points (direct and parent-orchestrated) on fresh environments. Both must match the
expectation and each other. Exit code 1 on any mismatch. Offline, synthetic, no network.

    python evaluation/run_evals.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from sub_agent.entrypoints.direct import DirectEntry  # noqa: E402
from sub_agent.entrypoints.orchestrated import Actor, OrchestratedEntry, OrchestratorRequest, WorkflowRef  # noqa: E402
from sub_agent.fixtures import (ALL, CALLER_ID, CALLER_ROLES, approval, approvals, delivery, make, new_environment,  # noqa: E402
                           ready_payload, ticket)
from sub_agent.states import ParentCaseStatus  # noqa: E402

SITE_RULES = {"state": "VERIFIED", "value": {"required_items": [], "block_on_late_clearance": False,
                                              "owner_role": "account_manager"}}
EXTRA_103 = {"request_key": "SYN-REQ-103", "item_id": "admin-console-access", "item_kind": "ACCESS", "requested_by": "SYN-USER-201"}
MONITOR = {"request_key": "SYN-REQ-301", "item_id": "monitor-27", "item_kind": "EQUIPMENT", "requested_by": "SYN-USER-201"}
IN_101 = {"request_key": "SYN-REQ-101", "item_id": "email-teams-account", "item_kind": "ACCESS", "requested_by": "SYN-USER-201"}

SCENARIOS = {
    "S01_awaiting_approval": dict(payload=lambda: make()),
    "S02_start_gate_blocked": dict(payload=lambda: make(offer_accepted=False)),
    "S03_out_of_matrix_exception": dict(payload=lambda: make(
        requested_access=[IN_101, EXTRA_103], requested_equipment=[],
        approval_records=[approval("101"), approval("103", request_id="SYN-CASE-001:SYN-REQ-103", item="admin-console-access")])),
    "S04_missing_access_matrix": dict(payload=lambda: make(config={"role_access_matrix": {"state": "MISSING"}})),
    "S05_approved_not_ready": dict(payload=lambda: make(approval_records=approvals(ALL))),
    "S06_it_rejected": dict(payload=lambda: make(approval_records=approvals(["201"]), mock_it_ticket_updates=[
        ticket("201", "CREATED"), ticket("201", "REJECTED", reason="Wrong model requested")])),
    "S07_laptop_delayed": dict(payload=lambda: make(
        approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")],
        delivery_status=[delivery("201", "PENDING", expected="2026-11-20")])),
    "S08_ready": dict(payload=lambda: ready_payload()),
    "S09_clearance_late_flagged": dict(payload=lambda: make(
        client_site_required=True, config={"client_site_rules": SITE_RULES},
        client_site_clearance={"status": "PENDING", "expected_date": "2026-11-30"})),
    "S10_withdrawn": dict(payload=lambda: make(approval_records=approvals(ALL)), parent_status=ParentCaseStatus.WITHDRAWN),
    "S11_client_bravo_monitor": dict(payload=lambda: make("bravo", 2, requested_access=[], requested_equipment=[MONITOR])),
    "S12_client_alpha_monitor": dict(payload=lambda: make("alpha", 1, requested_access=[], requested_equipment=[MONITOR])),
}


def summarise(result) -> dict:
    return {
        "overall_status": result.overall_status.value,
        "items": {i.request_key: i.state.value for i in result.items},
        "exceptions": sorted(i.request_key for i in result.items if i.exception_required),
        "blockers": sorted({b.code for b in result.blockers}),
        "risks": sorted({r.code for r in result.risks}),
        "config_gaps": sorted(result.config_gaps),
    }


def run_one(spec: dict, path: str):
    service, cases, desk = new_environment()
    if spec.get("parent_status"):
        cases.set_status("SYN-CASE-001", spec["parent_status"])
    payload = spec["payload"]()
    if path == "direct":
        result = DirectEntry(service).evaluate(payload, actor_id=CALLER_ID, roles=CALLER_ROLES)
    else:
        request = OrchestratorRequest(
            run_id="eval-run", idempotency_key="eval-key",
            workflow=WorkflowRef(id="onboarding", instance=payload["case_id"], step="sub_agent"),
            actor=Actor(on_behalf_of=CALLER_ID, roles=["onboarding_orchestrator"]), input=payload)
        result = OrchestratedEntry(service).evaluate(request).result
    assert desk.calls == [] and cases.created == [], "an evaluation must never call the IT desk or create a case"
    return result


def main() -> int:
    expected_file = Path(__file__).resolve().parent / "expected_outputs.json"
    expected = {k: v for k, v in json.loads(expected_file.read_text("utf-8")).items()
                if not k.startswith("_")}
    assert set(expected) == set(SCENARIOS), "expected_outputs.json and the scenario list differ"
    failures = 0
    for name, spec in SCENARIOS.items():
        direct, parent = run_one(spec, "direct"), run_one(spec, "parent")
        ok_expected = summarise(direct) == expected[name] and summarise(parent) == expected[name]
        ok_equal = direct.business_decision() == parent.business_decision()
        status = "PASS" if ok_expected and ok_equal else "FAIL"
        print(f"{status}  {name:<32} {direct.overall_status.value}")
        if status == "FAIL":
            failures += 1
            print("   expected:", json.dumps(expected[name]))
            print("   direct  :", json.dumps(summarise(direct)))
            print("   parent  :", json.dumps(summarise(parent)))
            if not ok_equal:
                print("   direct and parent business decisions differ")
    total = len(SCENARIOS)
    print(f"\n{total - failures}/{total} scenarios match the hand-authored expected outputs (both entry points).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
