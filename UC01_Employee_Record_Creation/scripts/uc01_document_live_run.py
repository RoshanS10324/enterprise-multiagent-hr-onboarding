"""UC-01 against the configured Supabase project, end to end:

    synthetic form in Supabase Storage -> UC-01 retrieval -> extraction -> validation
    -> budget -> encryption -> case / form / person -> details + budget -> audit
    -> INDEPENDENT read-back (a fresh container) -> UC-02 handoff -> UC-02 checklist

    python scripts/uc01_document_live_run.py check --out RUN_DIR         # read-only + zero-write probes
    python scripts/uc01_document_live_run.py run --live --out RUN_DIR    # the dev writes, read-back, UC-02

Final labels (only ever from the configured project when run with --live):

    LIVE_PERSISTENCE_VERIFIED  UC-01 wrote the case, form, person, details and budget and verified them
    LIVE_READBACK_VERIFIED     a NEW container read every record back and it matched the extraction
    UC02_HANDOFF_VERIFIED      UC-02 planned the checklist through the approved contract and it was verified
    BLOCKED                    a prerequisite (DBA migration, configuration) is missing; nothing was written for it

The same functions run in the tests against a FAKE PostgREST; there they are
labelled MOCK_* and never LIVE_*.

What `run --live` may write (dev project, synthetic client SYN-UAE-TEST, the
UC-01 owner's interim approval): the synthetic form ONCE to Storage
(content-addressed; it is already there, so it is not uploaded again); UC-01
audit rows; and - only when the pre-checks pass - the case, form, person,
details and budget rows, then UC-02's own tasks through UC-02. A re-run finds
the same employee (same document) and reuses it: no second employee, no second
budget, no second upload. Nothing is deleted or overwritten; the original form
is only read. No key, salary or personal detail is printed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT / "src"))

BUCKET = "Doc_Collection"
ORIGINAL_OBJECT = "CLIENT-TEST-A/Onboarding form - 2026 (1).docx"
INTAKE = ROOT / "data" / "mock" / "synthetic_form_2026_intake.json"
PASS, BLOCKED, FAIL, NOT_TRIGGERED = "PASS", "BLOCKED", "FAIL", "NOT TRIGGERED"


def labels(live: bool) -> dict[str, str]:
    prefix = "LIVE" if live else "MOCK"
    return {"persistence": f"{prefix}_PERSISTENCE_VERIFIED", "readback": f"{prefix}_READBACK_VERIFIED",
            "uc02": "UC02_HANDOFF_VERIFIED" if live else "MOCK_UC02_HANDOFF_VERIFIED"}


def load_intake(path: Path = INTAKE) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("synthetic") is not True:
        raise SystemExit("refusing an intake file that is not marked synthetic")
    return data


def _e2e():
    """The existing UC-01 -> UC-02 handoff helpers (contract, orchestrator adapter, UC-02 read-back)."""
    spec = importlib.util.spec_from_file_location("e2e_uc01_to_uc02", ROOT / "scripts" / "e2e_uc01_to_uc02.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ------------------------------------------------------------------ configuration


def configure_live_environment() -> None:
    """Dev-only switches for THIS process; no .env file is modified."""
    os.environ.setdefault("APP_ENV", "dev")
    os.environ["UC01_SUPABASE_INTERIM_WRITES"] = "1"
    os.environ.setdefault("UC01_DOCUMENT_BUCKET", BUCKET)
    # UC-02's own documented development settings (its files are not touched).
    from dotenv import dotenv_values

    for name, value in dotenv_values(REPO / "UC02_Onboarding_Task_Agent" / ".env").items():
        if value is not None:
            os.environ.setdefault(name, value)
    os.environ["UC02_STORE_MODE"] = "supabase"
    os.environ.setdefault("UC02_ENV", "development")
    os.environ.setdefault("UC02_ALLOW_SYNTHETIC_CONFIG", "1")
    os.environ.setdefault("LLM_MODE", "offline")


def live_container():
    from sub_agent.api.container import build_container
    from sub_agent.api.settings import Settings

    settings = Settings.from_env()
    if not settings.persistence_enabled:
        raise SystemExit("SUPABASE_URL is not configured")
    if settings.app_env != "dev":
        raise SystemExit("refusing: this run writes synthetic data and is dev-only")
    return build_container(settings)


# ------------------------------------------------------------------ pre-checks


def probe_case_status(client) -> dict[str, Any]:
    """Zero-write: an insert for a client that does not exist. CHECKs run before
    the FK, so 23514 = UC-01 status still rejected (B5 open), 23503 = accepted."""
    from sub_agent.adapters.supabase import CheckViolation, ForeignKeyViolation
    from sub_agent.models import CaseStatus

    rejected = []
    for status in CaseStatus:
        row = {"case_id": str(uuid.uuid4()), "client_id": "ZZ-UC01-PROBE-NO-SUCH-CLIENT", "candidate_name": "probe",
               "candidate_email": "probe@example.invalid", "hr_officer_id": "probe", "joining_date": "2030-01-01",
               "work_location_type": "OFFICE", "location_name": "probe", "variant_is_rehire": False,
               "variant_is_internal_transfer": False, "variant_is_agency_contractor": False,
               "variant_is_minor": False, "variant_is_resident": False, "variant_is_remote": False,
               "status": status.value, "current_stage": "INTAKE", "current_form_version": 1,
               "opened_at": "2026-11-05T00:00:00Z", "updated_at": "2026-11-05T00:00:00Z",
               "onboarding_attributes": [], "is_deleted": False}
        try:
            client.insert("onboarding_cases", [row], returning=False)
            return {"status": FAIL, "detail": "a probe row was ACCEPTED - investigate (it never should be)"}
        except ForeignKeyViolation:
            continue
        except CheckViolation:
            rejected.append(status.value)
    if rejected:
        return {"status": BLOCKED, "rejected_uc01_statuses": rejected,
                "detail": "onboarding_cases_status_check rejects UC-01 statuses; the DBA must apply "
                          "docs/schema/proposed/b5_allow_uc01_case_statuses.sql"}
    return {"status": PASS, "detail": "every UC-01 CaseStatus is accepted"}


def precheck(container) -> dict[str, Any]:
    from sub_agent.documents.supabase_store import FormDetailsStoreUnavailable

    out: dict[str, Any] = {}
    storage = container.storage
    if storage is None:
        out["storage"] = {"status": BLOCKED, "detail": "UC01_DOCUMENT_BUCKET not set"}
    else:
        found = storage.exists(BUCKET, ORIGINAL_OBJECT)
        original = storage.download(BUCKET, ORIGINAL_OBJECT) if found else b""
        out["original_form"] = {"status": PASS if found else FAIL, "object_path": f"{BUCKET}/{ORIGINAL_OBJECT}",
                                "size_bytes": len(original),
                                "sha256": hashlib.sha256(original).hexdigest() if original else None,
                                "note": "read-only; never written"}
    out["case_status_check_B5"] = probe_case_status(container.postgrest)
    try:
        container.form_details.ensure_ready()
        out["form_details_tables"] = {"status": PASS}
    except FormDetailsStoreUnavailable as exc:
        out["form_details_tables"] = {"status": BLOCKED, "detail": str(exc)}
    out["encryption"] = check_encryption(container)
    return out


def check_encryption(container) -> dict[str, Any]:
    """Round trip with the configured key; reports booleans and the key-id PREFIX only."""
    from sub_agent.crypto import FieldCipher, load_key, parse_previous_keys

    s = container.settings
    key = s.field_encryption_key.get_secret_value()
    if not key.strip():
        return {"status": BLOCKED, "detail": "UC01_FIELD_ENCRYPTION_KEY is not set"}
    cipher = FieldCipher(load_key(key), key_id=s.field_encryption_key_id or None,
                         previous=parse_previous_keys(s.field_encryption_previous_keys.get_secret_value()))
    blob = cipher.encrypt("uc01-encryption-probe", aad="probe")
    ok = cipher.decrypt(blob, aad="probe") == "uc01-encryption-probe"
    return {"status": PASS if ok else FAIL, "key_id_prefix": (s.field_encryption_key_id or "")[:4],
            "ciphertext_version": blob[:1].hex(), "key_id_bound": bool(s.field_encryption_key_id)}


# ------------------------------------------------------------------ UC-01


def fetch_document(container, local: Path, client_id: str) -> tuple[bytes, str]:
    """The synthetic form FROM Supabase Storage when it is there (verified by
    SHA-256); otherwise the committed file, which the intake then stores once."""
    from sub_agent.adapters.supabase.storage_paths import intake_object_path, source_document_id

    data = local.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    storage = getattr(container, "storage", None)
    if storage is None:
        return data, "local file (no Storage configured)"
    path = intake_object_path(client_id, source_document_id(client_id, sha), "docx")
    if not storage.exists(BUCKET, path):
        return data, "local file (not yet in Storage; the intake stores it once)"
    stored = storage.download(BUCKET, path)
    if hashlib.sha256(stored).hexdigest() != sha:
        raise RuntimeError("the stored synthetic form differs from the committed one (SHA-256)")
    return stored, f"Supabase Storage {BUCKET}/{path}"


def intake_context(intake: dict[str, Any], request_id: str):
    from sub_agent.documents.service import DocumentIntakeContext

    i, a = intake["intake"], intake["intake"]["form_approval"]
    return DocumentIntakeContext(
        request_id=request_id, client_id=i["client_id"], hr_officer_id=i["hr_officer_id"],
        candidate_email=i["candidate_email"], offer_accepted=i["offer_accepted"],
        work_location_type=i["work_location_type"], placement=i["placement"], form_status=a["form_status"],
        approved_by=a["approved_by"], approved_at=a["approved_at"])


def employee_reference(result) -> Optional[tuple[str, str]]:
    """(case_id, form_id) of THIS document's employee, created now or earlier."""
    status = result.status.value
    if status == "CREATED" and result.persistence_verified:
        return result.case_id, result.form_id
    if status == "LINKED_EXISTING" and result.persistence_verified:  # resumed an interrupted earlier run
        return result.case_id, result.form_id
    if status == "LINKED_EXISTING" and result.duplicate_document and result.form_id:  # this same file, earlier
        return result.existing_case_id, result.form_id
    return None


def verify_record(fresh, *, case_id: str, form_id: str, extraction, intake: dict[str, Any]) -> dict[str, Any]:
    """Read EVERYTHING back through a container that did not write it, and
    compare with the validated extraction. Field names only, never values of
    secrets."""
    from sub_agent.audit_integrity import verify_chain
    from sub_agent.documents.budget import BudgetCategory
    from sub_agent.documents.persistence import build_form_details, verify_saved

    deps, i = fresh.onboarding_deps, intake["intake"]
    checks: dict[str, bool] = {}
    case = deps.case_repo.get(case_id)
    form = deps.form_repo.get(case_id, 1)
    people = deps.person_directory.find_by_email(i["client_id"], i["candidate_email"])
    details = fresh.form_details.get(form_id)
    checks["case_found"], checks["form_found"], checks["details_found"] = (
        case is not None, form is not None, details is not None)
    if case is None or form is None or details is None:
        return {"ok": False, "checks": checks}
    e, x, sup = extraction.employee, extraction, extraction.supplementary
    salary = e.final_salary.value.get_secret_value()
    checks.update({
        "employee_name": case.candidate_name == form.candidate_name == e.employee_name.value,
        "email": case.candidate_email == form.candidate_email == i["candidate_email"],
        "employee_number": bool(case.employee_no),
        "designation": form.designation == e.designation.value,
        "department": case.department == sup.department.value,
        "grade": case.grade == sup.grade.value,
        "nationality": details.nationality == sup.nationality.value,
        "joining_date": case.joining_date == form.joining_date == e.joining_date.value,
        "reporting_manager": details.reporting_to == e.reporting_to.value,
        "work_location": case.location_name == i["placement"]["location_name"]
        and details.work_location_on_form == sup.work_location.value,
        "probation": details.probation_months == e.probation_months.value,
        "salary_decrypts_to_the_extracted_terms": form.salary_and_terms.get_secret_value() == salary,
        "visa": (details.visa_duration_years, details.visa_location_category) == (
            x.employment.visa_duration_years.value, x.employment.visa_location_category.value),
        "insurance": details.insurance_plan == x.insurance.plan.value,
        "travel": details.annual_employee_ticket == x.benefits.annual_employee_ticket.value,
        "accommodation": details.hotel_stay_days == x.benefits.hotel_stay_days.value,
        "form_linked_to_case": form.case_id == case_id and form.form_id == form_id,
        "person_linked_to_case": sum(1 for p in people if p.case_id == case_id) == 1,
        "details_linked_to_case": details.case_id == case_id and details.client_id == i["client_id"],
    })
    expected = build_form_details(extraction, form_id=form_id, case_id=case_id, client_id=i["client_id"],
                                  review_confirmed_by=details.review_confirmed_by,
                                  review_confirmed_at=details.review_confirmed_at,
                                  source_document=details.source_document)
    mismatches = verify_saved(expected, details)
    checks["details_and_budget_equal_extraction"] = not mismatches
    b, xb = details.budget, extraction.budget
    checks["budget_total_and_currency"] = (b.budget_total, b.currency, b.status) == (
        xb.budget_total, xb.currency, xb.status)
    checks["budget_categories"] = all(b.category_total(c) == xb.category_total(c) for c in BudgetCategory)

    # Duplicates and the relations, straight from the tables.
    lookup = getattr(deps.case_repo, "case_ids_for_email", None)
    if lookup is not None:
        checks["one_case_for_this_employee"] = len(lookup(i["client_id"], i["candidate_email"])) == 1
    checks["one_details_record_for_this_document"] = fresh.form_details.find_by_document(
        i["client_id"], x.metadata.source_sha256) == [form_id]
    db = fresh.postgrest
    if db is not None:
        form_row = db.select("onboarding_forms", columns="*", filters={"form_id": f"eq.{form_id}"})
        details_row = db.select("uc01_onboarding_form_details", columns="*", filters={"form_id": f"eq.{form_id}"})
        budget_rows = db.select("uc01_onboarding_budget", columns="form_id,case_id,client_id",
                                filters={"form_id": f"eq.{form_id}"})
        raw = json.dumps(form_row + details_row, default=str)
        secrets = [salary] + [str(f.value.get_secret_value()) for f in (
            x.personal.father_name, x.personal.mother_name, x.personal.mobile_number) if f.usable]
        checks["no_plaintext_secrets_in_rows"] = not any(s and s in raw for s in secrets)
        checks["one_budget_row_linked_to_case"] = (len(budget_rows) == 1 and budget_rows[0]["case_id"] == case_id
                                                   and budget_rows[0]["client_id"] == i["client_id"])
    ref = details.source_document
    storage = getattr(fresh, "storage", None)
    if ref is not None and storage is not None:
        checks["source_document_in_storage"] = hashlib.sha256(
            storage.download(ref.bucket, ref.object_path)).hexdigest() == ref.sha256
    actions = {a.action for a in deps.audit_log.entries_for_case(case_id)}
    checks["audit_records"] = {"CASE_OPENED", "FORM_DETAILS_SAVED", "BUDGET_RECORDED",
                               "EMPLOYEE_RECORD_VERIFIED"} <= actions
    checks["audit_chain_intact"] = not verify_chain(deps.audit_log.entries())
    return {"ok": all(checks.values()), "checks": checks, "details_mismatches": mismatches,
            "case_id": case_id, "form_id": form_id, "employee_no": case.employee_no,
            "client_id": case.client_id, "joining_date": case.joining_date.isoformat(),
            "source_document": ref.model_dump() if ref else None,
            "budget": {"status": b.status.value, "currency": b.currency,
                       "total": str(b.budget_total) if b.budget_total is not None else None,
                       "categories": {c.value: str(b.category_total(c)) for c in BudgetCategory}}}


# ------------------------------------------------------------------ UC-02


def uc02_handoff(fresh, case_id: str, intake: dict[str, Any], *, agent=None, deps=None) -> dict[str, Any]:
    """The EXISTING approved handoff: OnboardingCaseInput built from the
    read-back -> shared orchestrator adapter call_uc02_plan_tasks -> UC-02.
    UC-02 is not modified and not replaced."""
    e2e = _e2e()
    rb = e2e.read_back(fresh, case_id)
    out: dict[str, Any] = {"case_id": rb["case_id"], "client_id": rb["client_id"],
                           "employee_no": rb["employee_no"], "joining_date": rb["joining_date"]}
    try:
        config = e2e.uc02_configuration(rb["client_id"], rb["joining_date"])
    except Exception as exc:  # noqa: BLE001 - missing/draft config stops the handoff
        return {**out, "status": BLOCKED, "error": f"UC-02 configuration: {type(exc).__name__}: {exc}"}
    out["configuration"] = {k: config[k] for k in ("client_id", "version", "approval_status", "template_count")}
    try:
        inp = e2e.handoff(rb, intake)
    except e2e.HandoffInputRequired as exc:
        # Stopped in UC-01: UC-02 is not called, nothing is written, the case may be retried.
        return {**out, "status": BLOCKED, "error_code": "HR_INPUT_REQUIRED", "hr_input_required": exc.missing,
                "conflicts": exc.conflicts, "detail": str(exc), "uc02_called": False,
                "next_action": "HR supplies the field(s) for this case, then: "
                               "uc01_document_live_run.py handoff --live --case-id <case_id>"}
    _, out["uc02_field_sources"] = e2e.uc02_fields(rb, intake["intake"])
    if agent is None or deps is None:
        agent, deps = e2e.orchestrator()
    planned = e2e.plan_tasks(agent, deps, inp, rb)
    out["plan_tasks"] = {k: planned[k] for k in ("plan_tasks_status", "error_code", "error_detail")}
    if planned["plan_tasks_status"] != "complete":
        return {**out, "status": FAIL}
    onboarding_id = planned["task_plan"]["onboarding_id"]
    state = e2e.describe_uc02(deps._uc02_sub_agent.service, rb["client_id"], onboarding_id)
    checks = {
        "same_case_reference": planned["case_id"] == case_id,
        "same_client": state["client_id"] == rb["client_id"],
        "same_employee": state["employee_id"] == rb["employee_no"],
        "checklist_generated": bool(state["tasks"]),
        "every_task_has_owner_status_due_date": all(t["owner"] and t["status"] and t["due_date"]
                                                    for t in state["tasks"]),
        "templates_from_the_approved_configuration": {t["template_key"] for t in state["tasks"]}
        <= set(config["template_keys"]),
        "dependencies_point_at_tasks_in_the_plan": all(d in {t["task_id"] for t in state["tasks"]}
                                                       for t in state["tasks"] for d in t["depends_on"]),
    }
    if os.environ.get("UC02_STORE_MODE") == "supabase" and fresh.postgrest is not None:
        db = e2e.plan_in_database(fresh.postgrest, case_id)
        ids = {t["task_id"] for t in state["tasks"]}
        edges = {(t["task_id"], d) for t in state["tasks"] for d in t["depends_on"]}
        checks.update({
            "tasks_persisted_by_uc02": {t["task_id"] for t in db["tasks"]} == ids,
            "persisted_for_this_employee": all(t["employee_id"] == rb["employee_no"] and t["client_id"]
                                               == rb["client_id"] for t in db["tasks"]),
            "dependencies_persisted": edges <= {(d["task_id"], d["depends_on_task_id"]) for d in db["dependencies"]},
        })
        out["persisted_task_rows"] = len(db["tasks"])
    out.update(status=PASS if all(checks.values()) else FAIL, checks=checks, onboarding_id=onboarding_id,
               uc02_created_new_plan=planned["task_plan"].get("created"), task_count=len(state["tasks"]),
               tasks=[{k: t[k] for k in ("template_key", "owner", "status", "due_date", "depends_on")}
                      for t in state["tasks"]])
    return out


# ------------------------------------------------------------------ the run


def run(container, fresh_factory: Callable[[], Any], report: dict[str, Any], *, live: bool,
        intake: dict[str, Any], document: Path, service=None, agent=None, deps=None) -> dict[str, Any]:
    from sub_agent.documents.budget import budget_summary

    names = labels(live)
    steps, final = report["steps"], report["final"]
    i = intake["intake"]
    data, origin = fetch_document(container, document, i["client_id"])
    steps["document retrieval"] = {"status": PASS, "source": origin, "sha256": hashlib.sha256(data).hexdigest()}
    request_id = f"uc01-doc-live-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}"
    service = service or container.documents
    result = service.process(data, document.name, intake_context(intake, request_id))
    x = result.extraction
    steps["storage"] = {"status": PASS if result.source_document_stored else ("SKIPPED" if not live else FAIL),
                        "already_stored": result.duplicate_document or None,
                        **({"object": f"{result.source_document.bucket}/{result.source_document.object_path}"}
                           if result.source_document else {})}
    if x is not None:
        md = x.metadata
        steps["extraction + validation"] = {
            "status": PASS if result.status.value not in ("REJECTED", "VALIDATION_FAILED") else FAIL,
            "completeness": md.completeness, "missing": md.missing_fields, "invalid": md.invalid_fields,
            "uncertain": md.uncertain_fields, "review_reasons": result.review_reasons}
        b = budget_summary(x.budget)
        steps["budget"] = {"status": PASS, "budget_status": b["status"], "currency": b["currency"],
                           "total": b["budget_total"], "categories": b["category_totals"]}
    steps["uc01 intake"] = {"status": result.status.value, "case_id": result.case_id or result.existing_case_id,
                            "employee_no": result.employee_no, "form_id": result.form_id, "errors": result.errors,
                            "reasons": result.reasons, "readback_in_service": result.readback,
                            "persistence_verified": result.persistence_verified}
    ref = employee_reference(result)
    pre = report.get("precheck", {})
    blockers = [k for k, v in pre.items() if v.get("status") == BLOCKED]
    if ref is None:
        final["persistence"] = BLOCKED if blockers or any("unavailable" in e for e in result.errors) else FAIL
        final["readback"] = final["uc02"] = BLOCKED if final["persistence"] == BLOCKED else NOT_TRIGGERED
        report["stopped"] = ("UC-01 did not create and verify the employee; UC-02 NOT triggered"
                             + (f" (blocked by: {', '.join(blockers)})" if blockers else ""))
        return report
    final["persistence"] = names["persistence"]
    case_id, form_id = ref

    rb = verify_record(fresh_factory(), case_id=case_id, form_id=form_id, extraction=x, intake=intake)
    steps["independent read-back"] = {"status": PASS if rb["ok"] else FAIL, **rb}
    if not rb["ok"]:
        final["readback"], final["uc02"] = FAIL, NOT_TRIGGERED
        report["stopped"] = "read-back did not match the extraction; UC-02 NOT triggered"
        return report
    final["readback"] = names["readback"]

    handoff = uc02_handoff(fresh_factory(), case_id, intake, agent=agent, deps=deps)
    steps["uc02 handoff + checklist"] = handoff
    final["uc02"] = names["uc02"] if handoff["status"] == PASS else handoff["status"]
    report["identifiers"] = {"client_id": rb["client_id"], "case_id": case_id, "form_id": form_id,
                             "employee_no": rb["employee_no"], "uc02_onboarding_id": handoff.get("onboarding_id")}
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("check", "run", "handoff"))
    parser.add_argument("--live", action="store_true", help="required for `run`/`handoff`: perform the dev writes")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case-id", help="`handoff`: the EXISTING verified case to hand to UC-02 (no UC-01 writes)")
    parser.add_argument("--intake", type=Path, default=INTAKE, help="intake-caller file (HR-supplied UC-02 fields)")
    args = parser.parse_args(argv)
    if args.mode in ("run", "handoff") and not args.live:
        parser.error(f"`{args.mode}` writes to the configured Supabase project; pass --live to confirm")
    if args.mode == "handoff" and not args.case_id:
        parser.error("`handoff` needs --case-id of an existing, verified UC-01 case")

    configure_live_environment()
    container = live_container()
    intake = load_intake(args.intake)
    if args.mode == "handoff":
        # Retry ONLY the handoff for an existing case: UC-01 creates nothing here.
        report = {"mode": "handoff", "client_id": intake["intake"]["client_id"], "as_of": date.today().isoformat(),
                  "steps": {"uc02 handoff + checklist": uc02_handoff(live_container(), args.case_id, intake)}}
        status = report["steps"]["uc02 handoff + checklist"]["status"]
        report["final"] = {"uc02": labels(True)["uc02"] if status == PASS else status}
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        step = report["steps"]["uc02 handoff + checklist"]
        print(f"FINAL uc02 {report['final']['uc02']}" + (f" - {step['detail']}" if step.get("detail") else ""))
        print(f"report: {args.out / 'report.json'}")
        return 0
    report: dict[str, Any] = {"mode": args.mode, "client_id": intake["intake"]["client_id"],
                              "as_of": date.today().isoformat(), "precheck": precheck(container), "steps": {},
                              "final": {}}
    if args.mode == "run":
        run(container, live_container, report, live=True, intake=intake,
            document=ROOT / "data" / "mock" / intake["document"])
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    for name, value in report["precheck"].items():
        print(f"[{value['status']:>13}] precheck: {name}" + (f" - {value['detail']}" if value.get("detail") else ""))
    for name, value in report["steps"].items():
        print(f"[{str(value.get('status')):>13}] {name}")
    for name, value in report["final"].items():
        print(f"FINAL {name:<12} {value}")
    if report.get("stopped"):
        print("STOPPED:", report["stopped"])
    print(f"report: {args.out / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
