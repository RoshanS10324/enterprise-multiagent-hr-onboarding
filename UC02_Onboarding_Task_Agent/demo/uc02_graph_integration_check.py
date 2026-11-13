"""Controlled, staged verification of the real Microsoft Graph connection. Run ONLY once Microsoft 365 access exists.

    python demo/uc02_graph_integration_check.py connection                    # read-only: token + mailbox Drafts read
    python demo/uc02_graph_integration_check.py draft --client-id C --role hr_officer
                                                                              # creates ONE clearly marked TEST draft
    python demo/uc02_graph_integration_check.py send --client-id C --role hr_officer --confirm-send TEST-KEY
                                                                              # sends that draft to the approved recipients

Rules: configuration comes from the environment (``UC02_GRAPH_*``, ``UC02_RECIPIENTS_FILE``); nothing is printed that could
be a secret, token or address; ``draft`` and ``send`` need ``UC02_OUTLOOK_CONNECTOR=graph`` and never fall back to the mock;
``send`` also needs ``--confirm-send`` equal to the test key. Use a test mailbox and a sink recipient you control (see
docs/UC02_RUNTIME_AND_ACTIVATION.md). This script has never been run against a real tenant.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onboarding_task_management.sub_agent.connectors.outlook.base import (  # noqa: E402
    OutlookConnectorError,
    OutlookMessage,
)
from onboarding_task_management.sub_agent.connectors.outlook.factory import build_outlook_connector  # noqa: E402
from onboarding_task_management.sub_agent.connectors.outlook.graph import GraphOutlookConnector  # noqa: E402

TEST_KEY = "UC02-GRAPH-INTEGRATION-TEST-001"


def load_env() -> dict[str, str]:
    from onboarding_task_management import envfile

    envfile.load_environment()
    return dict(os.environ)


def connector(env: dict[str, str]) -> GraphOutlookConnector:
    env = {**env, "UC02_OUTLOOK_CONNECTOR": "graph"}
    built = build_outlook_connector(env)  # raises on any missing setting; never falls back to the mock
    assert isinstance(built, GraphOutlookConnector)
    return built


def message(client_id: str, role: str, approval: str | None) -> OutlookMessage:
    return OutlookMessage(
        idempotency_key=TEST_KEY, onboarding_id="TEST", client_id=client_id, recipient_role=role,
        subject_en="[UC-02 INTEGRATION TEST] Please ignore", approval_id=approval,
        body_en="This is a controlled UC-02 integration test message. It contains no employee data.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["connection", "draft", "send"])
    parser.add_argument("--client-id")
    parser.add_argument("--role", default="hr_officer")
    parser.add_argument("--confirm-send")
    args = parser.parse_args()
    try:
        graph = connector(load_env())
        if args.step == "connection":
            for name, ok in graph.verify_connection().items():
                print(f"  {'PASS' if ok else 'FAIL'}  {name}")
            return 0
        if not args.client_id:
            print("--client-id is required for this step")
            return 2
        if args.step == "draft":
            receipt = graph.create_draft(message(args.client_id, args.role, None))
            print(f"  draft status: {receipt.status.value}")
            return 0
        if args.confirm_send != TEST_KEY:
            print(f"refusing to send: --confirm-send must be {TEST_KEY}")
            return 2
        receipt = graph.send(message(args.client_id, args.role, "INTEGRATION-TEST-APPROVAL"))
        print(f"  send status: {receipt.status.value} (accepted for sending; delivery is not confirmed)")
        return 0
    except OutlookConnectorError as exc:
        print(f"  FAIL  {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
