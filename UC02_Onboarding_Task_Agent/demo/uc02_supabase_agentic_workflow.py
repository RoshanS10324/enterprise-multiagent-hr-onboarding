"""UC-02 complete synthetic workflow on SUPABASE ONLY. There is no memory or SQLite mode in this demo.

    python demo/uc02_supabase_agentic_workflow.py preflight                      # read-only: backend, target, schema, fixture
    python demo/uc02_supabase_agentic_workflow.py setup --confirm-write <host>   # writes the SYNTHETIC fixture (insert-if-missing)
    python demo/uc02_supabase_agentic_workflow.py run                            # UC-01 event -> ... -> HR email draft (mock Outlook)
    python demo/uc02_supabase_agentic_workflow.py decide --decision approved     # simulated HR decision via the signed callback
    python demo/uc02_supabase_agentic_workflow.py decide --decision rejected --reason "..."
    python demo/uc02_supabase_agentic_workflow.py decide --decision changes_requested --reason "..."
    python demo/uc02_supabase_agentic_workflow.py send                           # HR lead authorises the prepared messages
    python demo/uc02_supabase_agentic_workflow.py documents [--reject <key>]     # signed document status; gate; triggers
    python demo/uc02_supabase_agentic_workflow.py tasks [--waves N] [--as-of YYYY-MM-DD]  # parallel tasks, unblocking, overdue
    python demo/uc02_supabase_agentic_workflow.py verify                         # read-only: what is stored in Supabase
    python demo/uc02_supabase_agentic_workflow.py recover                        # new runtime: reload, replay event, no duplicates

Add ``--mock-downstream`` to trigger UC-04/UC-05 through labelled MOCK adapters; without it they are recorded as NOT CONFIGURED.

Configuration: loaded from ``.env`` then ``.env.local`` in the UC-02 folder (the process environment wins). Run
``scripts\\setup_dev_environment.ps1`` once to write UC02_STORE_MODE / UC02_ENV and generate the five secrets into ``.env.local``;
``scripts\\check_environment.ps1`` reports what is missing. Each stage checks only what it needs (preflight and verify need no
secret; run needs the callback and event secrets; documents needs the callback and documents secrets). ``setup`` additionally
needs UC02_ALLOW_FIXTURE_WRITES=1. Nothing secret is ever printed.
Everything written is marked synthetic. Fake LLM and mock Outlook are labelled as such. A missing variable, an unreachable
database or an incomplete schema stops the run with a clear message.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onboarding_task_management import envfile  # noqa: E402
from onboarding_task_management.errors import OnboardingError  # noqa: E402
from onboarding_task_management.persistence import PersistenceError  # noqa: E402
from onboarding_task_management.sub_agent import supabase_demo as demo  # noqa: E402
from onboarding_task_management.supabase_synthetic import FixtureSpec  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["preflight", "setup", "run", "decide", "send", "documents", "tasks", "verify", "recover"])
    parser.add_argument("--confirm-write", default="")
    parser.add_argument("--decision", default="approved", choices=["approved", "rejected", "changes_requested"])
    parser.add_argument("--reason", default="")
    parser.add_argument("--reject", default=None, help="document key to reject (documents stage)")
    parser.add_argument("--mock-downstream", action="store_true")
    parser.add_argument("--waves", type=int, default=1, help="tasks stage: how many dependency waves to progress")
    parser.add_argument("--as-of", default=None, help="tasks stage: date (YYYY-MM-DD) for overdue detection and the sweep")
    parser.add_argument("--employee-no", default="SYN-EMP-0001")
    args = parser.parse_args()
    envfile.load_environment()  # .env then .env.local; the process environment always wins; no value is printed
    status = envfile.check_environment(args.stage)
    if not status.ok:
        print(f"STOPPED: the environment is not ready for the '{args.stage}' stage:")
        for problem in status.problems:
            print(f"  - {problem}")
        print("Run scripts\\check_environment.ps1 for a full report, or scripts\\setup_dev_environment.ps1 once to set up.")
        return 1
    spec = FixtureSpec(employee_no=args.employee_no)
    try:
        if args.stage == "preflight":
            result = demo.preflight(spec=spec)
        elif args.stage == "setup":
            result = demo.setup(spec=spec, confirm_target=args.confirm_write)
        elif args.stage == "run":
            result = demo.run(spec=spec, mock_downstream=args.mock_downstream)
        elif args.stage == "decide":
            result = demo.decide(spec=spec, decision=args.decision, reason=args.reason, mock_downstream=args.mock_downstream)
        elif args.stage == "send":
            result = demo.send_authorised(spec=spec, mock_downstream=args.mock_downstream)
        elif args.stage == "documents":
            result = demo.documents(spec=spec, mock_downstream=args.mock_downstream, reject=args.reject,
                                    verify_all=args.reject is None)
        elif args.stage == "tasks":
            result = demo.tasks(spec=spec, mock_downstream=args.mock_downstream, waves=args.waves,
                                as_of=date.fromisoformat(args.as_of) if args.as_of else None)
        elif args.stage == "verify":
            result = demo.verify(spec=spec)
        else:
            result = demo.recover(spec=spec, mock_downstream=args.mock_downstream)
    except (OnboardingError, PersistenceError) as exc:
        print(f"STOPPED: {exc}")
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
