"""Local demo of UC-04 for one synthetic onboarding case. Everything is simulated: no network, no real ticket, no message.

    python run_demo.py

The same case is walked through the direct entry point, then the same facts are sent through the parent-orchestrated
entry point on a second environment to show both paths reach the same business decision.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from sub_agent.entrypoints.direct import DirectEntry  # noqa: E402
from sub_agent.entrypoints.orchestrated import Actor, OrchestratedEntry, OrchestratorRequest, WorkflowRef  # noqa: E402
from sub_agent.fixtures import (ACCESS_KEYS, ALL, CALLER_ID, CALLER_ROLES, EQUIPMENT_KEYS, approvals, collection, delivery,  # noqa: E402
                           make, new_environment, provisioning, ticket)


def show(title: str, result) -> None:
    print(f"\n=== {title}")
    print(f"mode={result.implementation_mode}  case={result.case_id}  status={result.overall_status.value}")
    print(f"why: {result.readiness_reason}")
    for i in result.items:
        print(f"  {i.request_key}  {i.item_id:<24} {i.state.value:<36} due={i.due_date}  approver={i.required_approver}")
    for b in result.blockers:
        print(f"  BLOCKER {b.code}: owner={b.owner}")
    for r in result.risks:
        print(f"  RISK    {r.code}: owner={r.owner}")
    for p in result.proposals:
        print(f"  PROPOSAL (not executed) {p.proposal_type}: needs {p.requires_approval_by}")
    for a in result.next_actions[:3]:
        print(f"  next: {a.action} [{a.owner}]")


def main() -> None:
    service, cases, desk = new_environment()
    direct = DirectEntry(service)
    run = lambda payload, **kw: direct.evaluate(payload, actor_id=CALLER_ID, roles=CALLER_ROLES, **kw)  # noqa: E731

    show("1. Case received: nothing approved yet", run(make()))
    show("2. Hiring manager approvals recorded (eligible only, nothing submitted)", run(make(approval_records=approvals(ALL))))
    print(f"   IT desk calls so far: {len(desk.calls)}")

    report = direct.submit_eligible(make(approval_records=approvals(ALL)), actor_id=CALLER_ID, roles=CALLER_ROLES)
    show("3. Simulated submission to the MOCK IT desk", report.result)
    print(f"   submitted={len(report.submitted)}  mock tickets={len(desk.tickets)}  real side effects={desk.real_side_effects}")

    delayed = make(approval_records=approvals(ALL), mock_it_ticket_updates=[ticket(k) for k in ALL],
                   delivery_status=[delivery("201", "PENDING", expected="2026-11-20")])
    show("4. Ticket complete but laptop expected after joining date", run(delayed))

    ready = make(approval_records=approvals(ALL), mock_it_ticket_updates=[ticket(k) for k in ALL],
                 provisioning_status=[provisioning(k) for k in ACCESS_KEYS], delivery_status=[delivery(k) for k in EQUIPMENT_KEYS],
                 collection_status=[collection(k) for k in EQUIPMENT_KEYS], as_of_date="2026-10-20")
    final = run(ready)
    show("5. Authoritative completion evidence for every item", final)

    # Like-for-like comparison: two fresh environments, equivalent input, one per entry point.
    fresh_direct_service, _, _ = new_environment()
    fresh_direct = DirectEntry(fresh_direct_service).evaluate(ready, actor_id=CALLER_ID, roles=CALLER_ROLES)
    parent_service, parent_cases, parent_desk = new_environment()
    request = OrchestratorRequest(run_id="demo-run", idempotency_key="demo-key",
                                  workflow=WorkflowRef(id="onboarding", instance="SYN-CASE-001", step="sub_agent"),
                                  actor=Actor(on_behalf_of=CALLER_ID, roles=["onboarding_orchestrator"]), input=ready)
    parent = OrchestratedEntry(parent_service).evaluate(request)
    identical = parent.result.business_decision() == fresh_direct.business_decision()
    print(f"\n=== 6. Same facts via the parent-orchestrated path: status={parent.result.overall_status.value}  "
          f"identical business decision={identical}")
    print(f"   cases created by UC-04: {parent_cases.created}  IT desk calls: {len(parent_desk.calls)}")


if __name__ == "__main__":
    main()
