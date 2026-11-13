"""Ten synthetic employees through UC-01 -> UC-02 against an ISOLATED test database.

Dataset: data/mock/isolated_e2e/employees_10.json (client SYN-CLIENT-A, UC-02's synthetic profile).

Modes (safest first):

    validate      [--original PATH]
        OFFLINE. No network at all (every DNS lookup is refused). Checks the dataset (uniqueness,
        the client's vocabulary), builds each employee's populated form (from the real blank
        original if given, otherwise from the template stand-in the unit tests use), and runs
        UC-01 extraction + validation + budget on it with UC-01 in memory. Writes nothing
        anywhere except the report.

    check-config  --env-file PATH
        OFFLINE. Validates the test-database configuration without connecting: required names,
        https, and that neither the URL, the project ref nor the service key belongs to the
        shared project (read from the repository's .env files, compared by host/ref and by
        SHA-256 only - nothing is printed).

    preflight     --env-file PATH --confirm-host HOST
        READ-ONLY against the TEST database (plus UC-01's zero-write status probe, an insert
        that its FK always rejects). Verifies both UC-01 and UC-02 resolve to that host, the
        tables and columns exist, the case-status CHECK, the details/budget tables, the
        encryption key, the bucket, the employee-number counter, and UC-02's approved
        SYN-CLIENT-A configuration (including that no template filters by project).

    run           --env-file PATH --confirm-host HOST --original PATH --i-understand-this-writes
        WRITES to the TEST database: each employee through the existing UC-01 flow
        (uc01_document_live_run.run: intake, persistence, independent read-back) and the
        existing handoff to UC-02 (shared orchestrator adapter -> UC-02's own persistence).
        One failure is recorded and the run continues. A ledger is written after every
        employee. Re-running reuses employees (content-addressed documents), never duplicates.

    retry-handoff --env-file PATH --confirm-host HOST [--test-id EMP-TEST-00N ...]
        Re-runs ONLY the UC-02 handoff for ledger employees whose UC-01 record is verified and
        whose handoff did not pass. Creates no UC-01 records.

Every mode except `validate`/`check-config` refuses to start unless check-config passes, the
host is re-typed with --confirm-host, and every network lookup is limited to that host.
Labels are ISOLATED_DB_* - never LIVE_*, never MOCK_*.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import socket
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT / "src"))

DATASET = ROOT / "data" / "mock" / "isolated_e2e" / "employees_10.json"
DEFAULT_OUT = ROOT / "output" / "isolated-e2e"
#: SHA-256 of the blank 'Onboarding form - 2026' (recorded by the live pre-check, 2026-11-05).
ORIGINAL_SHA256 = "99939e02820c97716ac388856dc5971563b73eeb0708c5079a3a42e5132335dc"
#: Files that configure the SHARED project. Read only to learn what must NOT be used.
SHARED_ENV_FILES = (ROOT / ".env", REPO / ".env", REPO / "UC02_Onboarding_Task_Agent" / ".env",
                    REPO / "UC02_Onboarding_Task_Agent" / ".env.local")
REQUIRED_TEST_ENV = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "UC01_FIELD_ENCRYPTION_KEY",
                     "UC01_FIELD_ENCRYPTION_KEY_ID", "UC01_DOCUMENT_BUCKET", "ISOLATED_TEST_DATABASE")
#: Credentials and endpoints the modules could otherwise pick up from the shared .env files.
PINNED_EMPTY = ("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY", "DATABASE_URL",
                "UC01_FIELD_ENCRYPTION_PREVIOUS_KEYS", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AZURE_API_KEY",
                "AZURE_API_BASE", "OPENAI_API_KEY")
UC01_CASE_TABLES = ("onboarding_cases", "onboarding_forms", "person_records", "uc01_onboarding_form_details",
                    "uc01_onboarding_budget")
UC01_FORM_TABLES = ("uc01_onboarding_form_field_status", "uc01_onboarding_document_checklist",
                    "uc01_onboarding_budget_lines")
UC02_CASE_TABLES = ("onboarding_tasks", "plan_revisions", "approval_requests", "agent_runs", "audit_events")
PASS, FAIL, BLOCKED, ERROR, SKIPPED = "PASS", "FAIL", "BLOCKED", "ERROR", "SKIPPED"
LABELS = {"LIVE_PERSISTENCE_VERIFIED": "ISOLATED_DB_PERSISTENCE_VERIFIED",
          "LIVE_READBACK_VERIFIED": "ISOLATED_DB_READBACK_VERIFIED",
          "UC02_HANDOFF_VERIFIED": "ISOLATED_DB_UC02_HANDOFF_VERIFIED"}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ dataset


def load_dataset(path: Path = DATASET) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def attributes(emp: dict[str, Any]) -> list[str]:
    """The opt-in UC-02 attributes the form supports (UC-02 never infers them)."""
    if emp["scenario"] == "overseas":
        return ["location_status:overseas", "needs_travel", "needs_accommodation"]
    return ["location_status:in_country"]


def expected_plan(emp: dict[str, Any], ds: dict[str, Any]) -> dict[str, int]:
    oracle = ds["client_configuration"]["expected_tasks"]
    if emp["scenario"] == "in_country":
        return oracle["in_country"]
    return oracle["overseas_G3"] if emp["grade"] == "G3" else oracle["overseas_G4_plus"]


def validate_dataset(ds: dict[str, Any], today: date | None = None) -> list[str]:
    today = today or date.today()
    cfg, emps, problems = ds["client_configuration"], ds["employees"], []
    if ds.get("synthetic") is not True:
        problems.append("dataset is not marked synthetic")
    if len(emps) != 10:
        problems.append(f"expected 10 employees, found {len(emps)}")
    for field in ("test_id", "name", "email"):
        values = [e[field].lower() for e in emps]
        if len(set(values)) != len(values):
            problems.append(f"duplicate {field}")
    groups = cfg["nationality_groups"]
    for e in emps:
        tid = e["test_id"]
        if not e["email"].endswith(".invalid"):
            problems.append(f"{tid}: email must use the reserved .invalid domain")
        if "Synthetic" not in e["name"]:
            problems.append(f"{tid}: name must be visibly synthetic")
        if e["role_id"] not in cfg["roles"]:
            problems.append(f"{tid}: role {e['role_id']} not in the client's roles")
        if e["grade"] not in cfg["grades"]:
            problems.append(f"{tid}: grade {e['grade']} not in the client's grades")
        if e["nationality_code"] not in groups.get(e["nationality_group"], []):
            problems.append(f"{tid}: nationality {e['nationality_code']} is not in group {e['nationality_group']}")
        if not {a for a in attributes(e) if a != "location_status:in_country"} <= set(
                cfg["attributes_used_by_templates"]):
            problems.append(f"{tid}: attribute outside the configuration's vocabulary")
        if not e.get("project") or not e.get("manager_id"):
            problems.append(f"{tid}: project and manager are required")
        joining = date.fromisoformat(e["joining_date"])
        if joining <= today or joining.weekday() >= 5:
            problems.append(f"{tid}: joining date must be a future working day")
        b = e["budget"]
        needed = {"visa_fee", "insurance_employee", "ticket_employee"}
        needed |= {"insurance_family", "ticket_family"} if e["family"] else set()
        needed |= {"hotel_nights", "hotel_rate"} if e["scenario"] == "overseas" else set()
        if not needed <= set(b):
            problems.append(f"{tid}: budget missing {sorted(needed - set(b))}")
    return problems


def form_values(emp: dict[str, Any], ds: dict[str, Any]) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """What HR writes on the 2026 template for this employee (row label -> value lines)."""
    d, b, n = ds["defaults"], emp["budget"], emp["test_id"][-3:]
    overseas, family = emp["scenario"] == "overseas", emp["family"]

    def money(v: int) -> str:
        return f"{d['currency']} {v:,}"

    travel = (date.fromisoformat(emp["joining_date"]) - timedelta(days=2)).isoformat()
    # A hire already inside the country has the visa copy, cancellation paper and Emirates ID in hand
    # (UC-01 requires them received for that route); an overseas hire receives them after arrival.
    in_country_docs = "PENDING" if overseas else "RECEIVED"
    values = {
        "name of employee": [emp["name"]],
        "reporting to": [emp["reporting_to"]],
        "designation": [emp["designation"]],
        "r&r": [f"Delivers {emp['designation']} work on {emp['project']} (see Roles and Responsibilities)"],
        "final salary": [emp["salary"]],
        "doj": [emp["joining_date"]],
        "probation period (3 or 6 months)": [d["probation"]],
        "1 year or 2 years visa": [f"Selected: 2 years - {'outside' if overseas else 'inside'} country",
                                   f"Visa fee: {money(b['visa_fee'])}"],
        "visa - visit + entry permit": ["Not applicable"],
        "direct entry permit": [
            "Direct entry permit: Required - employer sponsored" if overseas
            else "Direct entry permit: Not required - inside country",
            "Passport: RECEIVED", "Passport cover: RECEIVED", f"Visa copy: {in_country_docs}",
            f"Cancellation paper: {in_country_docs}", f"Emirates ID: {in_country_docs}", "Signed offer letter: RECEIVED", "Photograph: RECEIVED",
            "Degree certificate: RECEIVED", f"Father's name: SYNTHETIC-FATHER-NAME-{n}",
            f"Mother's name: SYNTHETIC-MOTHER-NAME-{n}", f"Marital status: {'Married' if family else 'Single'}",
            f"Mobile number: SYNTHETIC-MOBILE-{n}"],
        "insurance": [d["insurance_plan"]],
        "annual ticket -employee": [f"Sector: {emp['home_sector']}", f"Travel dates: {travel}",
                                    f"Ticket cost: {money(b['ticket_employee'])}"],
        "annual ticket- family": ([f"Sector: {emp['home_sector']}", f"Travel dates: {travel}",
                                   "Travellers: spouse", f"Ticket cost: {money(b['ticket_family'])}"]
                                  if family else ["Not applicable"]),
        "insurance for employee": [f"Category: {d['insurance_plan']}", f"Premium: {money(b['insurance_employee'])}",
                                   "Census: submitted"],
        "insurance for family": (["Coverage: spouse", f"Premium: {money(b['insurance_family'])}"]
                                 if family else ["Not applicable"]),
        "hotel stay - how many days (only applicable for staff outside uae)": (
            [f"Duration: {b['hotel_nights']} nights", f"Budget: {money(b['hotel_rate'])} per night",
             "Location: Al Barsha, Dubai"] if overseas else ["Not applicable"]),
        "any other promises made to the candidate": [
            f"Relocation allowance (one-time): {money(b['relocation'])}", "Laptop provided on day one"],
    }
    supplementary = [("Employee Email", emp["email"]), ("Department", emp["department"]),
                     ("Nationality", emp["nationality"]), ("Work Location", d["location_name"]),
                     ("Grade", emp["grade"]), ("Employment Duration", "Permanent (unlimited contract)")]
    return values, supplementary


def intake_for(emp: dict[str, Any], ds: dict[str, Any]) -> dict[str, Any]:
    """Same shape as data/mock/synthetic_form_2026_intake.json (what uc01_document_live_run expects)."""
    d = ds["defaults"]
    return {"synthetic": True, "document": f"{emp['test_id']}.docx", "intake": {
        "client_id": ds["client_id"], "hr_officer_id": d["hr_officer_id"], "candidate_email": emp["email"],
        "offer_reference": f"SYN-OFFER-A-{emp['test_id'][-3:]}", "offer_accepted": d["offer_accepted"],
        "work_location_type": d["work_location_type"],
        # Project and nationality group are placement data: stored on the case by UC-01, and
        # identical to the UC-02 fields, so the handoff sees two agreeing sources.
        "placement": {"location_name": d["location_name"], "project_name": emp["project"],
                      "nationality_group": emp["nationality_group"]},
        "form_approval": d["form_approval"],
        "uc02": {"role_id": emp["role_id"], "manager_id": emp["manager_id"],
                 "location": ds["client_configuration"]["location"], "project": emp["project"],
                 "attributes": attributes(emp)}}}


def blank_stand_in() -> bytes:
    """The template's rows with nothing filled in (the stand-in tests/test_synthetic_onboarding_form uses)."""
    mock_docs = _load("mock_docs_isolated", ROOT / "scripts" / "mock_onboarding_documents.py")
    return mock_docs.build_docx({}, "Roles and Responsibilities (eg given below)",
                                ["Example duty one.", "Example duty two."])


def read_original(path: Path | None) -> tuple[bytes, str]:
    if path is None:
        return blank_stand_in(), "template stand-in (no --original given)"
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ORIGINAL_SHA256:
        raise SystemExit(f"--original is not the blank 2026 form (SHA-256 differs from {ORIGINAL_SHA256[:12]}...)")
    return data, "blank original (SHA-256 verified)"


def build_document(original: bytes, emp: dict[str, Any], ds: dict[str, Any]) -> bytes:
    gen = _load("synthetic_form_isolated", ROOT / "scripts" / "synthetic_onboarding_form.py")
    values, supplementary = form_values(emp, ds)
    return gen.populate_docx(original, values, supplementary)


# ------------------------------------------------------------------ isolation


def _host(url: str) -> str:
    """Lower-case host of a URL, or "". Never raises (a password with '[' breaks urlparse) and never
    puts the value in an error."""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        m = re.search(r"@([A-Za-z0-9.-]+)(?::\d+)?(?:/|$)", url) or re.match(r"^[a-z]+://([A-Za-z0-9.-]+)", url)
        return m.group(1).lower() if m else ""


def _project_ref(url: str) -> str | None:
    host = _host(url)
    m = re.match(r"^([a-z0-9]+)\.supabase\.(co|in)$", host)
    if m:
        return m.group(1)
    m = re.search(r"postgres\.([a-z0-9]+)[:@]", url)  # pooler URLs carry the ref in the user name
    return m.group(1) if m else None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def shared_fingerprint(files=SHARED_ENV_FILES) -> dict[str, set[str]]:
    """Hosts, project refs and key digests of the SHARED configuration. Never printed."""
    from dotenv import dotenv_values

    hosts, refs, keys = set(), set(), set()
    for f in files:
        if not f.is_file():
            continue
        for name, value in dotenv_values(f).items():
            if not value:
                continue
            if "URL" in name:
                host = _host(value)
                if host and "pooler" not in host:
                    hosts.add(host)
                if (ref := _project_ref(value)):
                    refs.add(ref)
            if name.endswith("_KEY") or "SECRET" in name:
                keys.add(_digest(value))
    return {"hosts": hosts, "refs": refs, "keys": keys}


def check_config(test_env: dict[str, str], shared: dict[str, set[str]], confirm_host: str | None = None) -> dict:
    """Static validation of the TEST configuration. Returns {"ok", "host", "problems", "checks"}."""
    problems: list[str] = []
    missing = [n for n in REQUIRED_TEST_ENV if not (test_env.get(n) or "").strip()]
    if missing:
        problems.append(f"missing in the test env file: {missing}")
    url = (test_env.get("SUPABASE_URL") or "").strip()
    host = _host(url)
    ref = _project_ref(url)
    checks = {
        "marked_isolated": test_env.get("ISOLATED_TEST_DATABASE") == "1",
        "https": url.startswith("https://"),
        "host_not_shared": bool(host) and host not in shared["hosts"],
        "project_ref_not_shared": ref is None or ref not in shared["refs"],
        "service_key_not_shared": _digest(test_env.get("SUPABASE_SERVICE_ROLE_KEY", "")) not in shared["keys"],
        "encryption_key_not_shared": _digest(test_env.get("UC01_FIELD_ENCRYPTION_KEY", "")) not in shared["keys"],
        "dev_encryption_key_id": (test_env.get("UC01_FIELD_ENCRYPTION_KEY_ID") or "").startswith("dev-"),
        "no_other_endpoint_points_at_shared": not any(
            (_project_ref(v) in shared["refs"]) for k, v in test_env.items() if v and "URL" in k),
    }
    if confirm_host is not None:
        checks["confirm_host_matches"] = confirm_host.strip().lower() == host
    problems += [f"check failed: {k}" for k, ok in checks.items() if not ok]
    if not checks["https"]:
        problems.append("UC-01 Settings accept only https:// (a local http stack needs the documented "
                        "localhost exemption first - docs/local-test-env.md 'Known gaps')")
    return {"ok": not problems, "host": host, "project_ref": ref, "checks": checks, "problems": problems}


def apply_environment(test_env: dict[str, str]) -> None:
    """Pin every name both modules read BEFORE either is imported: their .env loaders never
    overwrite a name that is already set, so the shared files cannot fill anything in."""
    url = test_env["SUPABASE_URL"].strip()
    pinned = {
        "SUPABASE_URL": url, "NEXT_PUBLIC_SUPABASE_URL": url,
        "SUPABASE_SERVICE_ROLE_KEY": test_env["SUPABASE_SERVICE_ROLE_KEY"],
        "UC01_FIELD_ENCRYPTION_KEY": test_env["UC01_FIELD_ENCRYPTION_KEY"],
        "UC01_FIELD_ENCRYPTION_KEY_ID": test_env["UC01_FIELD_ENCRYPTION_KEY_ID"],
        "UC01_DOCUMENT_BUCKET": test_env["UC01_DOCUMENT_BUCKET"],
        "APP_ENV": "dev", "UC01_SUPABASE_INTERIM_WRITES": "1",
        "UC02_STORE_MODE": "supabase", "UC02_ENV": "development", "UC02_ALLOW_SYNTHETIC_CONFIG": "1",
        "UC02_ALLOW_FIXTURE_WRITES": "0", "UC02_ALLOW_CONFIG_SEEDING": "0",
        "LLM_MODE": "offline", "ORCHESTRATOR_STORE_MODE": "memory",
        **{name: "" for name in PINNED_EMPTY},
    }
    os.environ.update(pinned)


def environment_leaks(shared: dict[str, set[str]]) -> list[str]:
    """Names in the process environment that still carry a shared host, ref or key (names only)."""
    leaks = []
    for name, value in os.environ.items():
        if not value:
            continue
        if _digest(value) in shared["keys"] or any(r in value for r in shared["refs"]) or any(
                h in value.lower() for h in shared["hosts"]):
            leaks.append(name)
    return sorted(leaks)


def network_guard(allowed_host: str | None) -> None:
    """Refuse every DNS lookup except the test host (None = no network at all)."""
    real = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host)
        if allowed_host is None or name.lower() != allowed_host:
            raise ConnectionRefusedError(f"isolated run: network lookup of {name!r} refused")
        return real(host, *args, **kwargs)

    socket.getaddrinfo = guarded  # type: ignore[assignment]


def resolved_targets() -> dict[str, str]:
    """The host each module will actually call, from its own configuration code."""
    from onboarding_task_management.supabase_rest import SupabaseRestClient

    from sub_agent.api.settings import Settings

    uc02 = SupabaseRestClient.from_env(dict(os.environ))
    return {"uc01": _host(Settings.from_env().supabase_url), "uc02": _host(getattr(uc02, "_base", ""))}


def isolate(env_file: Path, confirm_host: str) -> dict[str, Any]:
    """check-config + pin + leak scan + guard + target check. Exits on any failure."""
    from dotenv import dotenv_values

    shared = shared_fingerprint()
    test_env = {k: v or "" for k, v in dotenv_values(env_file).items()}
    cfg = check_config(test_env, shared, confirm_host)
    if not cfg["ok"]:
        raise SystemExit("REFUSED - test database configuration: " + "; ".join(cfg["problems"]))
    apply_environment(test_env)
    network_guard(cfg["host"])
    targets = resolved_targets()  # imports both modules: their .env loaders run now
    leaks = environment_leaks(shared)
    if leaks:
        raise SystemExit(f"REFUSED - shared-project values still in the environment: {leaks}")
    if set(targets.values()) != {cfg["host"]}:
        raise SystemExit(f"REFUSED - modules resolve to different hosts: {targets}")
    return {"host": cfg["host"], "checks": cfg["checks"], "targets": targets, "env_leaks": leaks}


# ------------------------------------------------------------------ database helpers


def _live_run():
    return _load("uc01_document_live_run_isolated", ROOT / "scripts" / "uc01_document_live_run.py")


def test_container():
    from sub_agent.api.container import build_container
    from sub_agent.api.settings import Settings

    settings = Settings.from_env()
    if settings.app_env != "dev" or not settings.persistence_enabled:
        raise SystemExit("refusing: the isolated run needs APP_ENV=dev and a configured test database")
    return build_container(settings)


def _columns_exist(db, table: str, columns: tuple[str, ...]) -> str | None:
    try:
        db.select(table, columns=",".join(columns), limit=1)
        return None
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return f"{table}: {type(exc).__name__}"


def preflight(container, ds: dict[str, Any]) -> dict[str, Any]:
    from datetime import date as _date

    from onboarding_task_management.sub_agent.runtime import build_service
    from onboarding_task_management.supabase_schema import REQUIRED

    from sub_agent.documents.supabase_store import FormDetailsStoreUnavailable

    lr = _live_run()
    out: dict[str, Any] = {}
    db = container.postgrest
    problems = [p for t in UC01_CASE_TABLES[:3] if (p := _columns_exist(db, t, ("case_id",)))]
    problems += [p for t, cols in REQUIRED.items() if (p := _columns_exist(db, t, tuple(cols)))]
    out["tables_and_columns"] = {"status": PASS if not problems else BLOCKED, "problems": problems}
    out["case_status_check_B5"] = lr.probe_case_status(db)
    try:
        container.form_details.ensure_ready()
        out["form_details_tables"] = {"status": PASS}
    except FormDetailsStoreUnavailable as exc:
        out["form_details_tables"] = {"status": BLOCKED, "detail": str(exc)}
    out["encryption"] = lr.check_encryption(container)
    bucket = os.environ["UC01_DOCUMENT_BUCKET"]
    try:
        container.storage.exists(bucket, f"{ds['client_id']}/uc01-intake/preflight-probe.docx")
        out["bucket"] = {"status": PASS, "bucket": bucket}
    except Exception as exc:  # noqa: BLE001
        out["bucket"] = {"status": BLOCKED, "bucket": bucket, "detail": type(exc).__name__}
    client = ds["client_id"]
    counters = db.select("employee_number_counters", columns="client_id", filters={"client_id": f"eq.{client}"})
    out["employee_number_counter"] = {"status": PASS if counters else BLOCKED,
                                      **({} if counters else {"detail": "seed with scripts/seed_employee_number_counters.py --apply"})}
    registry, configs = build_service().registry, {}
    for emp in ds["employees"]:
        try:
            cfg = registry.get(client, on=_date.fromisoformat(emp["joining_date"]))
            configs[emp["test_id"]] = cfg
        except Exception as exc:  # noqa: BLE001
            configs[emp["test_id"]] = f"{type(exc).__name__}: {exc}"
    bad = {k: v for k, v in configs.items() if isinstance(v, str)}
    if bad:
        out["uc02_configuration"] = {"status": BLOCKED, "problems": bad}
    else:
        cfg = next(iter(configs.values()))
        project_filters = sorted({p for t in cfg.templates for p in t.projects}
                                 | {p for d in cfg.document_requirements for p in getattr(d, "projects", ())})
        outside = sorted({e["project"] for e in ds["employees"]} - set(project_filters)) if project_filters else []
        out["uc02_configuration"] = {
            "status": PASS if cfg.approval_status.value == "approved" and not outside else BLOCKED,
            "client_id": cfg.client_id, "approval_status": cfg.approval_status.value,
            "template_count": len(cfg.templates), "project_filters": project_filters or "none (project-agnostic)",
            **({"projects_not_in_configuration": outside} if outside else {})}
    lookup = container.onboarding_deps.case_repo.case_ids_for_email
    existing = {e["test_id"]: lookup(client, e["email"]) for e in ds["employees"]}
    out["existing_dataset_employees"] = {"status": PASS, "reused_on_run": {k: v for k, v in existing.items() if v}}
    return out


def plan_counts(fresh, case_id: str) -> dict[str, Any]:
    e2e = _live_run()._e2e()
    plan = e2e.plan_in_database(fresh.postgrest, case_id)
    ids = {t["task_id"] for t in plan["tasks"]}
    return {"tasks": len(plan["tasks"]), "dependencies": len(plan["dependencies"]),
            "dependencies_within_plan": all(d["depends_on_task_id"] in ids for d in plan["dependencies"]),
            "tasks_have_owner_and_due_date": all(t["owner_role"] and t["due_date"] for t in plan["tasks"])}


def db_summary(fresh, ledger: dict[str, Any]) -> dict[str, Any]:
    """Row counts for exactly this run's employees, read through a fresh connection."""
    db = fresh.postgrest
    cases = [r["case_id"] for r in ledger["employees"].values() if r.get("case_id")]
    forms = [r["form_id"] for r in ledger["employees"].values() if r.get("form_id")]
    counts: dict[str, Any] = {}

    def count(table: str, column: str, ids: list[str]) -> int | str:
        if not ids:
            return 0
        try:
            return len(db.select(table, columns=column, filters={column: f"in.({','.join(ids)})"}))
        except Exception as exc:  # noqa: BLE001
            return f"unreadable ({type(exc).__name__})"

    for t in UC01_CASE_TABLES + UC02_CASE_TABLES:
        counts[t] = count(t, "case_id", cases)
    for t in UC01_FORM_TABLES:
        counts[t] = count(t, "form_id", forms)
    task_ids = [t["task_id"] for c in cases for t in _live_run()._e2e().plan_in_database(db, c)["tasks"]]
    counts["task_dependencies"] = count("task_dependencies", "task_id", task_ids)
    return {"employees": len(cases), "rows": counts}


# ------------------------------------------------------------------ run / retry


def _relabel(final: dict[str, str]) -> dict[str, str]:
    return {k: LABELS.get(v, v) for k, v in final.items()}


def run_employee(container, emp, ds, docs_dir: Path, original: bytes, pre, agent, deps) -> dict[str, Any]:
    lr = _live_run()
    doc = build_document(original, emp, ds)
    path = docs_dir / f"{emp['test_id']}.docx"
    path.write_bytes(doc)
    report: dict[str, Any] = {"precheck": pre, "steps": {}, "final": {}}
    try:
        lr.run(container, test_container, report, live=True, intake=intake_for(emp, ds), document=path,
               agent=agent, deps=deps)
    except Exception as exc:  # noqa: BLE001 - one employee's failure never stops the others
        report["error"] = f"{type(exc).__name__}: {exc}"
    ids = report.get("identifiers") or {}
    uc01 = report["steps"].get("uc01 intake", {})
    result = {"test_id": emp["test_id"], "case_id": ids.get("case_id") or uc01.get("case_id"),
              "form_id": ids.get("form_id") or uc01.get("form_id"),
              "employee_no": ids.get("employee_no") or uc01.get("employee_no"),
              "uc01_status": uc01.get("status"), "final": _relabel(report["final"]),
              "stopped": report.get("stopped"), "error": report.get("error"),
              "document_sha256": hashlib.sha256(doc).hexdigest(), "steps": report["steps"]}
    if result["case_id"] and report["final"].get("uc02") == "UC02_HANDOFF_VERIFIED":
        result.update(_plan_checks(emp, ds, result["case_id"]))
    return result


def _plan_checks(emp, ds, case_id: str) -> dict[str, Any]:
    fresh = test_container()
    counts = plan_counts(fresh, case_id)
    case = fresh.postgrest.select("onboarding_cases", columns="project_name,client_id,employee_no",
                                  filters={"case_id": f"eq.{case_id}"})
    expected = expected_plan(emp, ds)
    return {"plan_in_database": counts, "expected_plan": expected,
            "plan_matches_expectation": (counts["tasks"], counts["dependencies"]) == (
                expected["tasks"], expected["dependencies"]),
            "case_project_name": case[0]["project_name"] if case else None}


def write_ledger(out: Path, ledger: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "ledger.json").write_text(json.dumps(ledger, indent=1, default=str), encoding="utf-8")


def summary_table(ledger: dict[str, Any]) -> list[str]:
    rows = ["| Employee | Employee no | UC-01 | UC-02 | DB read-back | Tasks/deps (expected) |",
            "|---|---|---|---|---|---|"]
    for tid, r in ledger["employees"].items():
        f = r.get("final", {})
        plan = r.get("plan_in_database") or {}
        exp = r.get("expected_plan") or {}
        rows.append(f"| {tid} | {r.get('employee_no') or '-'} | {f.get('persistence', r.get('error') or '-')} | "
                    f"{f.get('uc02', '-')} | {f.get('readback', '-')} | "
                    f"{plan.get('tasks', '-')}/{plan.get('dependencies', '-')} "
                    f"({exp.get('tasks', '-')}/{exp.get('dependencies', '-')}) |")
    return rows


# ------------------------------------------------------------------ modes


def mode_validate(ds, original_path: Path | None) -> dict[str, Any]:
    network_guard(None)
    os.environ.update({"SUPABASE_URL": "", "LLM_MODE": "offline", "APP_ENV": "dev"})
    from sub_agent.documents.budget import budget_summary
    from sub_agent.documents.persistence import build_form_details
    from sub_agent.documents.service import DocumentIntakeService

    lr, e2e = _live_run(), None
    e2e = lr._e2e()
    original, origin = read_original(original_path)
    out: dict[str, Any] = {"mode": "validate", "network": "none", "original": origin,
                           "dataset_problems": validate_dataset(ds), "employees": {}}
    container = e2e.uc01_memory_container()
    service = DocumentIntakeService(container.onboarding_deps, details_repo=container.form_details)
    for emp in ds["employees"]:
        doc = build_document(original, emp, ds)
        res = service.process(doc, f"{emp['test_id']}.docx", lr.intake_context(intake_for(emp, ds), emp["test_id"]))
        x = res.extraction
        b = budget_summary(x.budget) if x is not None else {}
        rows = None
        if x is not None and res.form_id:
            # The rows UC-01's own details builder produces = what the details tables will receive.
            rec = build_form_details(x, form_id=res.form_id, case_id=res.case_id or "", client_id=ds["client_id"],
                                     review_confirmed_by=None, review_confirmed_at=None, source_document=None)
            rows = {"onboarding_cases": 1, "onboarding_forms": 1, "uc01_onboarding_form_details": 1,
                    "uc01_onboarding_budget": 1, "uc01_onboarding_form_field_status": len(rec.field_status),
                    "uc01_onboarding_document_checklist": len(rec.checklist),
                    "uc01_onboarding_budget_lines": len(rec.budget.lines)}
        out["employees"][emp["test_id"]] = {
            "uc01_in_memory": res.status.value, "errors": res.errors, "review_reasons": res.review_reasons,
            "completeness": x.metadata.completeness if x else None,
            "missing": x.metadata.missing_fields if x else None, "invalid": x.metadata.invalid_fields if x else None,
            "budget": {"status": b.get("status"), "currency": b.get("currency"), "total": b.get("budget_total")},
            "uc02_attributes": attributes(emp), "expected_plan": expected_plan(emp, ds), "expected_uc01_rows": rows,
            "document_sha256": hashlib.sha256(doc).hexdigest()}
    out["ok"] = not out["dataset_problems"] and all(
        e["uc01_in_memory"] == "CREATED" and e["budget"]["status"] == "COMPLETE" for e in out["employees"].values())
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=("validate", "check-config", "preflight", "run", "retry-handoff"))
    p.add_argument("--dataset", type=Path, default=DATASET)
    p.add_argument("--env-file", type=Path, help="the TEST database's own env file (never a shared .env)")
    p.add_argument("--confirm-host", help="re-type the test database host")
    p.add_argument("--original", type=Path, help="local copy of the blank 'Onboarding form - 2026' (.docx)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--test-id", action="append", help="retry-handoff: limit to these employees")
    p.add_argument("--i-understand-this-writes", action="store_true")
    a = p.parse_args(argv)
    ds = load_dataset(a.dataset)
    a.out.mkdir(parents=True, exist_ok=True)

    if a.mode == "validate":
        report = mode_validate(ds, a.original)
        (a.out / "validate.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        for tid, e in report["employees"].items():
            print(f"{tid}: UC-01(memory) {e['uc01_in_memory']:<10} budget {e['budget']['status']} "
                  f"{e['budget']['currency']} {e['budget']['total']}  expected plan {e['expected_plan']}")
        print("dataset problems:", report["dataset_problems"] or "none", "| OK" if report["ok"] else "| NOT OK")
        return 0 if report["ok"] else 1

    if a.env_file is None:
        p.error(f"`{a.mode}` needs --env-file (the TEST database's env file)")
    resolved = a.env_file.resolve()
    if resolved in {f.resolve() for f in SHARED_ENV_FILES}:
        p.error("--env-file is one of the SHARED project's .env files; refused")

    if a.mode == "check-config":
        from dotenv import dotenv_values

        cfg = check_config({k: v or "" for k, v in dotenv_values(a.env_file).items()}, shared_fingerprint(),
                           a.confirm_host)
        print(json.dumps({k: cfg[k] for k in ("ok", "host", "checks", "problems")}, indent=1))
        return 0 if cfg["ok"] else 1

    if not a.confirm_host:
        p.error(f"`{a.mode}` needs --confirm-host")
    isolation = isolate(a.env_file, a.confirm_host)
    container = test_container()
    pre = preflight(container, ds)
    if a.mode == "preflight":
        report = {"mode": "preflight", "isolation": isolation, "preflight": pre}
        (a.out / "preflight.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        for name, v in pre.items():
            print(f"[{v['status']:>7}] {name}")
        return 0 if all(v["status"] == PASS for v in pre.values()) else 1

    blocking = {k: v for k, v in pre.items() if v["status"] != PASS}
    if blocking:
        raise SystemExit(f"REFUSED - preflight not clean: {sorted(blocking)} (see `preflight`)")
    ledger_path = a.out / "ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else {
        "dataset_id": ds["dataset_id"], "client_id": ds["client_id"], "employees": {}}
    ledger["isolation"] = isolation
    agent, deps = _live_run()._e2e().orchestrator()
    by_id = {e["test_id"]: e for e in ds["employees"]}

    if a.mode == "run":
        if not a.i_understand_this_writes or a.original is None:
            p.error("`run` writes to the test database: pass --original and --i-understand-this-writes")
        original, _ = read_original(a.original)
        docs_dir = a.out / "documents"
        docs_dir.mkdir(parents=True, exist_ok=True)
        for emp in ds["employees"]:
            ledger["employees"][emp["test_id"]] = run_employee(container, emp, ds, docs_dir, original, pre, agent, deps)
            write_ledger(a.out, ledger)
            print(f"{emp['test_id']}: {ledger['employees'][emp['test_id']]['final']}")
    else:  # retry-handoff
        lr = _live_run()
        targets = a.test_id or list(ledger["employees"])
        for tid in targets:
            r = ledger["employees"].get(tid)
            if not r or r.get("final", {}).get("readback") != LABELS["LIVE_READBACK_VERIFIED"]:
                print(f"{tid}: {SKIPPED} (no verified UC-01 record in the ledger - use `run`)")
                continue
            if r["final"].get("uc02") == LABELS["UC02_HANDOFF_VERIFIED"]:
                print(f"{tid}: already handed off")
                continue
            step = lr.uc02_handoff(test_container(), r["case_id"], intake_for(by_id[tid], ds), agent=agent, deps=deps)
            r["steps"]["uc02 handoff + checklist"] = step
            r["final"]["uc02"] = LABELS["UC02_HANDOFF_VERIFIED"] if step["status"] == PASS else step["status"]
            if step["status"] == PASS:
                r.update(_plan_checks(by_id[tid], ds, r["case_id"]))
            write_ledger(a.out, ledger)
            print(f"{tid}: {r['final']['uc02']}")

    ledger["database_summary"] = db_summary(test_container(), ledger)
    ledger["finished_at"] = datetime.now(UTC).isoformat()
    write_ledger(a.out, ledger)
    print("\n".join(summary_table(ledger)))
    print(json.dumps(ledger["database_summary"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
