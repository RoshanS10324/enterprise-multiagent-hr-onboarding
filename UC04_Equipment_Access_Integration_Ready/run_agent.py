"""Interactive CLI to run the UC-04 Equipment & Access Sub-Agent directly in the terminal.

No Swagger UI or web browser needed. Run:
    python run_agent.py
    python run_agent.py --scenario ready
    python run_agent.py --scenario at_risk
    python run_agent.py --scenario blocked
    python run_agent.py --scenario in_progress
    python run_agent.py --all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from sub_agent.agent import Uc04Agent  # noqa: E402
from sub_agent.fixtures import (  # noqa: E402
    ALL,
    approvals,
    delivery,
    make,
    ready_payload,
    ticket,
)


def get_scenario_payload(scenario_name: str) -> tuple[str, dict]:
    """Build representative scenario payloads."""
    name = scenario_name.lower().strip()

    if name in ("awaiting_approval", "pending", "default"):
        return (
            "Scenario: New Joiner Case Received (Awaiting Approval)",
            make(),
        )

    if name in ("in_progress", "submitted"):
        return (
            "Scenario: Hiring Manager Approved (Submitted to Mock IT Desk)",
            make(
                approval_records=approvals(ALL),
                mock_it_ticket_updates=[ticket(k) for k in ALL],
            ),
        )

    if name in ("at_risk", "delayed"):
        return (
            "Scenario: Hardware Delayed Past Start Date (At Risk)",
            make(
                approval_records=approvals(ALL),
                mock_it_ticket_updates=[ticket(k) for k in ALL],
                delivery_status=[delivery("201", "PENDING", expected="2026-11-20")],
            ),
        )

    if name in ("ready", "completed"):
        return (
            "Scenario: All Items Fulfilled & Evidenced (Ready for Day 1)",
            ready_payload(n=4),
        )

    if name in ("blocked", "start_gate"):
        return (
            "Scenario: Start Gate Not Cleared (Offer Letter Not Signed)",
            make(offer_accepted=False),
        )

    # fallback
    return (f"Scenario: Custom ({scenario_name})", make())


def print_banner(text: str, char: str = "=") -> None:
    line = char * len(text)
    print(f"\n{line}\n{text}\n{line}")


def run_single_scenario(agent: Uc04Agent, scenario_name: str) -> None:
    title, payload = get_scenario_payload(scenario_name)
    print_banner(title, "=")

    result, advisory = agent.evaluate_with_advisory(payload)

    status_str = result.overall_status.value
    print(f"Case ID     : {result.case_id}")
    print(f"Client ID   : {payload.get('client_id', 'unknown')}")
    print(f"Overall     : [{status_str}]")
    print(f"Readiness   : {result.readiness_reason}")

    print("\n--- Requested Items ---")
    for item in result.items:
        kind = item.item_kind.value if hasattr(item.item_kind, "value") else str(item.item_kind)
        state = item.state.value if hasattr(item.state, "value") else str(item.state)
        print(
            f"  - {item.request_key} | {item.item_id:<20} | {kind:<9} | "
            f"State: {state:<24} | Due: {item.due_date}"
        )

    if result.blockers:
        print("\n--- Active Blockers ---")
        for b in result.blockers:
            print(f"  [X] {b.code}: {b.reason} (Owner: {b.owner})")

    if result.risks:
        print("\n--- Detected Risks ---")
        for r in result.risks:
            print(f"  [!] {r.code}: {r.reason} (Owner: {r.owner})")

    if result.proposals:
        print("\n--- Workaround Proposals ---")
        for p in result.proposals:
            print(f"  [*] {p.proposal_type}: {p.description} (Requires: {p.requires_approval_by})")

    print("\n--- Simulated Advisory Explanation (Fake LLM & Brain) ---")
    print(advisory.formatted_explanation)

    print("\n--- Next Recommended Actions ---")
    if result.next_actions:
        for idx, act in enumerate(result.next_actions, 1):
            print(f"  {idx}. {act.action} [Assigned to: {act.owner}]")
    else:
        print("  All next actions completed. No further action needed.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the UC-04 Equipment & Access Agent in the terminal."
    )
    parser.add_argument(
        "--scenario",
        "-s",
        default="awaiting_approval",
        choices=["awaiting_approval", "in_progress", "at_risk", "ready", "blocked"],
        help="The onboarding scenario to evaluate.",
    )
    parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="Run all representative scenarios sequentially.",
    )
    args = parser.parse_args()

    agent = Uc04Agent()

    if args.all:
        for sc in ["awaiting_approval", "in_progress", "at_risk", "ready", "blocked"]:
            run_single_scenario(agent, sc)
    else:
        run_single_scenario(agent, args.scenario)


if __name__ == "__main__":
    main()
