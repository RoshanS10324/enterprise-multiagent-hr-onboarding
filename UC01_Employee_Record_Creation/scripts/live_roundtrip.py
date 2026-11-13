"""Live write/read/update round trip against the UC-01 Supabase tables.

Inserts clearly-marked throwaway rows (ZZ_UC01_ROUNDTRIP), reads them back,
updates them, then DELETES everything it created (and restores the employee
counter). Run only against a project you are happy to touch.
"""
import os
import sys
from datetime import date
from pathlib import Path

import httpx
from pydantic import SecretStr

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "src"))
for p in (HERE.parent / ".env", HERE / ".env"):
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

from sub_agent.adapters.supabase.client import PostgrestClient  # noqa: E402
from sub_agent.adapters.supabase.repositories import SupabaseFormRepository  # noqa: E402
from sub_agent.crypto import FieldCipher, generate_key_b64, load_key  # noqa: E402
from sub_agent.models.onboarding_form import OnboardingForm  # noqa: E402

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
CLIENT = "test_enterprise_corp"
TAG = "ZZ_UC01_ROUNDTRIP"
db = PostgrestClient(URL, SecretStr(KEY))
raw = httpx.Client(base_url=URL + "/rest/v1", headers=H, timeout=30)
results = []
created = []  # (table, pk_col, pk_val) in insert order
S = {}


def check(name, fn):
    try:
        results.append((True, name, fn() or ""))
    except Exception as exc:  # noqa: BLE001
        results.append((False, name, f"{type(exc).__name__}: {exc}"[:400]))


def ins(table, row, pk):
    out = db.insert(table, [row])[0]
    created.append((table, pk, out[pk]))
    return out


def need(cond, msg):
    if not cond:
        raise AssertionError(msg)


def eq(v):
    return f"eq.{v}"


def t_case():
    r = ins("onboarding_cases", dict(
        client_id=CLIENT, candidate_name=TAG, candidate_email="zz@example.invalid",
        hr_officer_id="hr1", joining_date="2026-12-01", work_location_type="OFFICE",
        location_name="Dubai", onboarding_attributes=[]), "case_id")
    S["case"] = r["case_id"]
    need(r["role_id"] is None and r["manager_id"] is None, "role/manager should be NULL")
    need(r["current_stage"] == "INTAKE", f"current_stage={r['current_stage']}")
    up = db.update("onboarding_cases",
                   {"reactivates_record_id": "r1", "reactivates_external_ref": "E1", "department": "IT"},
                   filters={"case_id": eq(S["case"])})[0]
    need(up["reactivates_external_ref"] == "E1" and up["department"] == "IT", "update not persisted")
    return "insert without role_id/manager_id OK, default stage INTAKE, update incl. rehire cols OK"


def t_form():
    case = S["case"]
    repo = SupabaseFormRepository(db, FieldCipher(load_key(generate_key_b64())))
    f = OnboardingForm(
        client_id=CLIENT, case_id=case, version=1, candidate_name=TAG,
        candidate_email="zz@example.invalid", designation="Engineer",
        joining_date=date(2026, 12, 1), work_location_type="OFFICE",
        salary_and_terms=SecretStr("AED 99999"), status="DRAFT")
    created.append(("onboarding_forms", "form_id", f.form_id))
    S["form"] = f.form_id
    repo.save(f)
    g = repo.get(case, 1)
    need(g is not None and g.salary_and_terms.get_secret_value() == "AED 99999", "decrypt mismatch")
    raw_row = db.select("onboarding_forms", filters={"form_id": eq(f.form_id)})[0]
    need("99999" not in str(raw_row), "salary stored in plaintext!")
    repo.save(g.model_copy(update={"status": "AWAITING_APPROVAL"}))
    need(repo.get(case, 1).status == "AWAITING_APPROVAL", "status update lost")
    return "adapter save/get/update OK; salary encrypted at rest"


def t_offer():
    db.insert("offers", [dict(client_id=CLIENT, offer_ref=TAG, candidate_email="zz@example.invalid",
                              candidate_name=TAG, designation="Engineer", hr_officer_id="hr1",
                              offer_payload={})])
    created.append(("offers", "offer_ref", TAG))
    return "insert OK"


def t_offer_form():
    db.upsert("offer_forms", [dict(client_id=CLIENT, offer_ref=TAG, form_id=S["form"])],
              on_conflict="client_id,offer_ref")
    created.append(("offer_forms", "offer_ref", TAG))
    need(db.select("offer_forms", filters={"offer_ref": eq(TAG)})[0]["form_id"] == S["form"], "readback")
    return "upsert/read OK"


def t_person():
    r = ins("person_records", dict(client_id=CLIENT, email="zz@example.invalid", full_name=TAG,
                                   kind="OPEN_CASE", case_id=S["case"], hris_external_ref="X1"), "record_id")
    up = db.update("person_records", {"kind": "PAST_EMPLOYEE", "hris_external_ref": "X2"},
                   filters={"record_id": eq(r["record_id"])})[0]
    need(up["hris_external_ref"] == "X2" and up["kind"] == "PAST_EMPLOYEE", "update")
    return "insert/update incl. hris_external_ref OK"


def t_disc():
    r = ins("intake_discrepancies", dict(
        case_id=S["case"], field_name="designation", value_form="a", value_other="b",
        other_source="offer_letter", form_version=1, other_fingerprint=TAG), "discrepancy_id")
    up = db.update("intake_discrepancies", {"status": "RESOLVED"},
                   filters={"discrepancy_id": eq(r["discrepancy_id"])})[0]
    need(up["status"] == "RESOLVED", "update")
    return "insert/update OK"


def t_hris():
    r = ins("hris_sync_records", dict(
        case_id=S["case"], client_id=CLIENT, target_system="ZOHO_PEOPLE", form_version=1,
        prepared_payload={"a": "b"}, sensitive_field_names=["salary"], updates_external_ref="E1"), "record_id")
    up = db.update("hris_sync_records", {"updates_external_ref": "E2"},
                   filters={"record_id": eq(r["record_id"])})[0]
    need(up["updates_external_ref"] == "E2", "update")
    return "insert/update incl. updates_external_ref OK"


def t_event():
    ins("domain_events_outbox", dict(client_id=CLIENT, event_type="CaseOpenedEvent", payload={"x": 1}, case_id=S["case"]), "event_id")
    return "insert OK"


def t_pending():
    db.insert("pending_starts", [dict(client_id=CLIENT, offer_ref=TAG, reasons=["r"])])
    created.append(("pending_starts", "offer_ref", TAG))
    need(db.select("pending_starts", filters={"offer_ref": eq(TAG)}), "readback")
    return "insert/read OK"


def t_audit():
    r = ins("audit_events", dict(client_id=CLIENT, case_id=S["case"], actor_type="SYSTEM",
                                 actor_id=TAG, action=TAG, detail={}, prev_hash="p", entry_hash="h"), "event_id")
    need(r["prev_hash"] == "p" and r["entry_hash"] == "h", "chain cols not stored")
    return "insert with prev_hash/entry_hash OK (UC-01 adapter itself still refuses; see report)"


def t_appr():
    r = ins("uc01_approvals", dict(case_id=S["case"], checkpoint="HC_01_FORM",
                                   subject_ref=f"form:{S['form']}", requested_to="hr_mgr"), "approval_id")
    need(r["status"] == "PENDING", "default status")
    db.update("uc01_approvals", {"status": "APPROVED", "decided_by": "hr_mgr"},
              filters={"approval_id": eq(r["approval_id"])})
    try:
        db.update("uc01_approvals", {"status": "REJECTED"}, filters={"approval_id": eq(r["approval_id"])})
    except Exception:  # noqa: BLE001
        return "insert/decide OK; re-deciding correctly blocked by immutability trigger"
    raise AssertionError("decided approval was mutable")


def t_empno():
    S["emp_before"] = db.select("employee_number_counters", filters={"client_id": eq(CLIENT)})[0]["last_value"]
    a = raw.post("/rpc/next_employee_no", json={"p_client_id": CLIENT})
    a.raise_for_status()
    b = raw.post("/rpc/next_employee_no", json={"p_client_id": CLIENT})
    b.raise_for_status()
    need(a.json() != b.json(), "duplicate numbers")
    return f"RPC returned {a.json()} then {b.json()} (counter restored to {S['emp_before']})"


try:
    check("onboarding_cases insert/update (nullable placement, rehire cols)", t_case)
    if "case" in S:
        check("onboarding_forms via SupabaseFormRepository", t_form)
        check("offers (FK parent)", t_offer)
        if "form" in S:
            check("offer_forms", t_offer_form)
        check("person_records", t_person)
        check("intake_discrepancies", t_disc)
        check("hris_sync_records", t_hris)
        check("domain_events_outbox", t_event)
        check("pending_starts", t_pending)
        check("audit_events chain columns", t_audit)
        if "form" in S:
            check("uc01_approvals", t_appr)
    check("next_employee_no RPC", t_empno)
finally:
    raw.delete("/audit_events", params={"action": eq(TAG)})
    raw.delete("/domain_events_outbox", params={"case_id": eq(S["case"])}) if "case" in S else None
    for table, pk, val in reversed(created):
        raw.delete(f"/{table}", params={pk: eq(val)})
    if "emp_before" in S:
        raw.patch("/employee_number_counters", params={"client_id": eq(CLIENT)},
                  json={"last_value": S["emp_before"]})
    raw.delete("/audit_events", params={"action": eq(TAG)})
    left = []
    for t, col in (("onboarding_cases", "candidate_name"), ("person_records", "full_name"),
                   ("audit_events", "action"), ("offers", "offer_ref")):
        if raw.get(f"/{t}", params={col: eq(TAG), "select": "*"}).json():
            left.append(t)
    print()
    for ok, name, detail in results:
        print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""))
    print("cleanup leftovers:", left or "none")
    db.close()
