"""The ONE place UC-02 loads its local environment files, and the secret bootstrap for local development.

Which files, from where
-----------------------
Two optional files in the UC-02 project root (the folder that holds ``pyproject.toml``), found from this package's own location so
the working directory does not matter:

1. ``.env``        - project configuration (for example the Supabase URL and keys).
2. ``.env.local``  - local overrides and the generated development secrets. Written only by the explicit setup command.

Both are git-ignored (``.env`` and ``.env.*``); ``.env.example`` is the tracked template and contains placeholders only.

Precedence (highest first): the process environment (what PowerShell set, or the shell exported), then ``.env.local``, then
``.env``. A value already in the process environment is NEVER overwritten by a file, so an explicit override always wins.

Who calls :func:`load_environment`: the entry points only (the demo CLI, the API when it first needs the runtime, the setup and
check scripts). Library functions take an explicit mapping and never read files, so unit tests and embedded use are unaffected.
Set ``UC02_DISABLE_DOTENV=1`` to make the loader a no-op (the test suite does this, so a developer's real files can never leak
into a test run).

Secrets
-------
:func:`ensure_dev_secrets` can create the five required secrets with ``secrets.token_urlsafe`` and append them to ``.env.local``.
It only runs when asked (``create=True``), only creates what is missing, never overwrites, refuses unless ``UC02_ENV`` is
``development``, and never returns or prints a value (only names and whether each is present). Production secrets come from the
deployment's secret manager and are never generated here.
"""

from __future__ import annotations

import os
import secrets as _secrets
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILES = (".env", ".env.local")
DISABLE_FLAG = "UC02_DISABLE_DOTENV"

#: The five secrets the full workflow needs, by the channel they protect.
SECRET_VARIABLES: dict[str, str] = {
    "approval_callback": "UC02_APPROVAL_CALLBACK_SECRET",
    "events": "UC02_EVENT_SECRET",
    "documents": "UC02_DOCUMENTS_SECRET",
    "uc04": "UC02_UC04_STATUS_SECRET",
    "uc05": "UC02_UC05_STATUS_SECRET",
}
SUPABASE_URL_VARIABLES = ("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY_VARIABLE = "SUPABASE_SERVICE_ROLE_KEY"

#: What each demo stage needs beyond Supabase itself. ``preflight`` and ``verify`` only read, so they need no secret.
STAGE_SECRETS: dict[str, tuple[str, ...]] = {
    "preflight": (), "verify": (), "setup": (),
    "run": ("approval_callback", "events"),
    "decide": ("approval_callback",),
    "send": ("approval_callback",),
    "documents": ("approval_callback", "documents"),
    "tasks": ("approval_callback",),
    "recover": ("approval_callback", "events"),
    "full": tuple(SECRET_VARIABLES),
}


@dataclass
class LoadReport:
    files_loaded: list[str] = field(default_factory=list)
    variables_set: list[str] = field(default_factory=list)  # names only, never values
    skipped_already_set: list[str] = field(default_factory=list)
    disabled: bool = False


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal dotenv parser: ``KEY=VALUE`` lines, ``#`` comments, optional ``export``, optional matching quotes. No expansion."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def load_environment(root: Path | None = None, environ: MutableMapping[str, str] | None = None) -> LoadReport:
    """Load ``.env`` then ``.env.local`` into ``environ`` (default ``os.environ``) without overwriting anything already set."""
    env = os.environ if environ is None else environ
    report = LoadReport()
    if env.get(DISABLE_FLAG) == "1":
        report.disabled = True
        return report
    base = root or PROJECT_ROOT
    merged: dict[str, str] = {}
    for name in ENV_FILES:  # .env.local is read last, so it overrides .env (but never the process environment)
        path = base / name
        if path.is_file():
            merged.update(parse_env_file(path))
            report.files_loaded.append(name)
    for key, value in merged.items():
        if key in env:
            report.skipped_already_set.append(key)
        else:
            env[key] = value
            report.variables_set.append(key)
    return report


# ---------------------------------------------------------------------------------------------------------------------------


@dataclass
class EnvironmentStatus:
    ok: bool
    problems: list[str]
    present: dict[str, bool]  # name -> set (never the value)


def check_environment(stage: str = "full", environ: MutableMapping[str, str] | None = None) -> EnvironmentStatus:
    """Which required settings are missing for ``stage``. Reads only; returns names, never values."""
    env = os.environ if environ is None else environ
    problems: list[str] = []
    present: dict[str, bool] = {}
    mode = (env.get("UC02_STORE_MODE") or env.get("PERSISTENCE_BACKEND") or "").strip().lower()
    present["UC02_STORE_MODE=supabase"] = mode == "supabase"
    if mode != "supabase":
        problems.append("UC02_STORE_MODE must be 'supabase' (memory and sqlite are not allowed for the agent runtime)")
    url_ok = any(env.get(v) for v in SUPABASE_URL_VARIABLES)
    present["SUPABASE_URL"] = url_ok
    if not url_ok:
        problems.append("SUPABASE_URL (or NEXT_PUBLIC_SUPABASE_URL) is not set")
    key_ok = bool(env.get(SUPABASE_KEY_VARIABLE))
    present[SUPABASE_KEY_VARIABLE] = key_ok
    if not key_ok:
        problems.append(f"{SUPABASE_KEY_VARIABLE} is not set")
    present["UC02_ENV"] = bool(env.get("UC02_ENV"))
    for channel in STAGE_SECRETS.get(stage, STAGE_SECRETS["full"]):
        variable = SECRET_VARIABLES[channel]
        ok = bool(env.get(variable))
        present[variable] = ok
        if not ok:
            problems.append(f"{variable} is not set")
    return EnvironmentStatus(ok=not problems, problems=problems, present=present)


def ensure_dev_secrets(*, create: bool, root: Path | None = None,
                       environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Report (and with ``create=True``, persist) the five development secrets. Values are never returned.

    Returns ``{variable: "present" | "missing" | "created"}``. With ``create=True`` the missing ones are generated and APPENDED to
    ``.env.local`` (created if absent, never rewritten), and also set in ``environ`` so the current process sees them. Refused
    unless ``UC02_ENV`` is ``development``: a production secret must come from the deployment's secret store.
    """
    env = os.environ if environ is None else environ
    status = {variable: ("present" if env.get(variable) else "missing") for variable in SECRET_VARIABLES.values()}
    if not create:
        return status
    if (env.get("UC02_ENV") or "") != "development":
        raise PermissionError("development secrets are generated only when UC02_ENV=development; "
                              "production secrets are managed externally")
    missing = [v for v, s in status.items() if s == "missing"]
    if not missing:
        return status
    path = (root or PROJECT_ROOT) / ".env.local"
    generated = {variable: _secrets.token_urlsafe(32) for variable in missing}
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    block = (f"{prefix}# Generated by the UC-02 development setup. Local only; never commit; never share.\n"
             + "".join(f"{variable}={value}\n" for variable, value in generated.items()))
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(block)
    try:
        os.chmod(path, 0o600)  # best effort; Windows only honours the read-only bit
    except OSError:
        pass
    for variable, value in generated.items():
        env[variable] = value
        status[variable] = "created"
    return status


def set_local_defaults(*, root: Path | None = None, environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Persist the non-secret development settings (``UC02_STORE_MODE=supabase``, ``UC02_ENV=development``) in ``.env.local``
    if they are not already defined anywhere. Returns the names added. Used only by the explicit setup command."""
    env = os.environ if environ is None else environ
    wanted = {"UC02_STORE_MODE": "supabase", "UC02_ENV": "development"}
    path = (root or PROJECT_ROOT) / ".env.local"
    defined = set(env) | (set(parse_env_file(path)) if path.exists() else set())
    added = [k for k in wanted if k not in defined]
    if added:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(prefix + "".join(f"{k}={wanted[k]}\n" for k in added))
        for k in added:
            env[k] = wanted[k]
    return added


__all__ = ["ENV_FILES", "PROJECT_ROOT", "SECRET_VARIABLES", "STAGE_SECRETS", "EnvironmentStatus", "LoadReport",
           "check_environment", "ensure_dev_secrets", "load_environment", "parse_env_file", "set_local_defaults"]
