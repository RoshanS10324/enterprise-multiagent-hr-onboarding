"""Environment check and first-time development setup for UC-02. Never prints a secret value, never writes to a database.

    python scripts/uc02_env.py check [--stage run|decide|send|documents|recover|setup|preflight|verify|full]
    python scripts/uc02_env.py setup

``check`` only reads: it loads ``.env`` then ``.env.local`` (the process environment always wins) and reports which required
settings are present, by name. ``setup`` is the explicit setup action: it adds ``UC02_STORE_MODE=supabase`` and
``UC02_ENV=development`` to ``.env.local`` if nothing defines them, and generates any of the five secrets that are missing into
``.env.local`` (git-ignored). Existing values are never changed. It refuses if ``UC02_ENV`` is anything but ``development``.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onboarding_task_management import envfile  # noqa: E402


def _dependencies() -> list[str]:
    missing = []
    for module, required in (("pydantic", True), ("fastapi", False), ("httpx", False)):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(f"{module}{'' if required else ' (optional: only the API and API tests need it)'}")
    return missing


def _print_status(status: envfile.EnvironmentStatus) -> None:
    for name, ok in status.present.items():
        print(f"  {'OK     ' if ok else 'MISSING'}  {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check", "setup"])
    parser.add_argument("--stage", default="full", choices=sorted(envfile.STAGE_SECRETS))
    args = parser.parse_args()

    report = envfile.load_environment()
    files = ", ".join(report.files_loaded) or "none found"
    print(f"UC-02 root: {envfile.PROJECT_ROOT}")
    print(f"Environment files loaded: {files} (the process environment takes precedence)")

    if args.command == "setup":
        added = envfile.set_local_defaults()
        if added:
            print(f"Added to .env.local: {', '.join(added)}")
        try:
            created = envfile.ensure_dev_secrets(create=True)
        except PermissionError as exc:
            print(f"STOPPED: {exc}")
            return 1
        for variable, state in created.items():
            print(f"  secret {variable}: {state}")
        print("Secrets are stored only in .env.local (git-ignored) and are never printed.")

    print(f"Stage requirements checked: {args.stage}")
    status = envfile.check_environment(args.stage)
    _print_status(status)
    missing_modules = _dependencies()
    for module in missing_modules:
        print(f"  MISSING  python module {module}")
    if status.problems:
        print("NOT READY:")
        for problem in status.problems:
            print(f"  - {problem}")
    ready = status.ok and not [m for m in missing_modules if "optional" not in m]
    print("READY: the UC-02 runtime configuration is complete." if ready else "NOT READY.")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
