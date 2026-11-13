"""Command line for the central client configuration: validate the JSON profiles, dry-run against Supabase (read-only), seed, verify.

    python scripts/uc02_client_config.py validate                 # offline: schema + semantic checks of every profile
    python scripts/uc02_client_config.py dry-run                  # READ-ONLY against Supabase: what would be inserted
    python scripts/uc02_client_config.py verify                   # READ-ONLY read-back of what is stored
    python scripts/uc02_client_config.py seed --confirm-write <host>   # WRITES (insert-only); needs UC02_ENV=development and UC02_ALLOW_CONFIG_SEEDING=1

``dry-run`` and ``verify`` never write. ``seed`` is refused unless the dry run is clean and the target host is re-typed. Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onboarding_task_management import envfile  # noqa: E402
from onboarding_task_management.client_configuration import seeding  # noqa: E402
from onboarding_task_management.client_configuration.document import PROFILE_DIR, load_profiles, validate_document  # noqa: E402
from onboarding_task_management.persistence import PersistenceError  # noqa: E402
from onboarding_task_management.supabase_rest import SupabaseRestClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["validate", "dry-run", "verify", "seed"])
    parser.add_argument("--confirm-write", default="")
    parser.add_argument("--no-uc04-matrix", action="store_true", help="accepted for compatibility: UC-04's role_access_matrix is ALWAYS excluded (its mapping is unconfirmed)")
    parser.add_argument("--json", action="store_true", help="print the full machine-readable report")
    parser.add_argument("--set", choices=["synthetic", "existing"], default="synthetic",
                        help="synthetic = the SYN-CLIENT-A..D profiles; existing = the four existing mock clients (CLIENT_CANADA/DUBAI/INDIA/USA)")
    args = parser.parse_args()
    docs = load_profiles(PROFILE_DIR / "existing_mock_clients") if args.set == "existing" else load_profiles()
    if args.command == "validate":
        bad = 0
        for d in docs:
            problems = validate_document(d)
            bad += bool(problems)
            print(f"{d['client_master']['client_id']}: {'VALID' if not problems else 'INVALID'}")
            for p in problems[:10]:
                print("   -", p)
        return 1 if bad else 0
    envfile.load_environment()
    if (os.environ.get("UC02_STORE_MODE") or "").lower() != "supabase":
        print("STOPPED: set UC02_STORE_MODE=supabase (run scripts/setup_dev_environment.ps1 once).")
        return 1
    rest = SupabaseRestClient.from_env(dict(os.environ))
    if args.command in ("dry-run", "verify"):  # read-only commands: any non-GET request is refused before it leaves the process
        _call = rest._call

        def _get_only(method, *a, **k):
            if method != "GET":
                raise PersistenceError(f"read-only command attempted a {method} request; refused")
            return _call(method, *a, **k)

        rest._call = _get_only  # type: ignore[method-assign]
    matrix = False  # role_access_matrix is never projected until UC-04 confirms the mapping
    try:
        if args.command == "dry-run":
            report = seeding.dry_run(rest, docs, include_role_access_matrix=matrix)
            print(json.dumps(report.summary(), indent=2, default=str))
            return 0 if report.safe_to_apply else 1
        if args.command == "verify":
            problems = seeding.verify(rest, docs, include_role_access_matrix=matrix)
            print("READ-BACK:", "OK - the database holds exactly what the documents say" if not problems else f"{len(problems)} problem(s)")
            for p in problems[:30]:
                print("  -", p)
            return 1 if problems else 0
        result = seeding.apply(rest, docs, confirm_target=args.confirm_write, include_role_access_matrix=matrix)
        print(json.dumps(result, indent=2))
        return 0
    except PersistenceError as exc:
        print(f"STOPPED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
