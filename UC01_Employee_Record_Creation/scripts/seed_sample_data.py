"""Seed sample UC-01 data into the live Supabase project (idempotent).

Fixed IDs + upsert, so re-running never duplicates. All sample emails use
@sample.invalid so they are easy to find and remove. audit_events is NOT
seeded (it is append-only live, so rows could never be deleted again).
onboarding_forms is seeded only if UC01_FIELD_ENCRYPTION_KEY is set.

    python scripts/seed_sample_data.py          # seed
    python scripts/seed_sample_data.py --remove # delete the seeded rows
"""
import os
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "src"))
for p in (HERE.parent / ".env", HERE / ".env"):
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
c = httpx.Client(base_url=URL + "/rest/v1", timeout=30, headers={
    "apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})

CLIENT = "CLIENT_DUBAI"


def cid(n):
    return f"5eed0000-0000-4000-8000-{n:012d}"


CASES = [
    (1, "Aisha Khan", "aisha.khan", "Software Engineer", "2026-12-01", "OFFICE", "Dubai HQ", "INTAKE_PENDING"),
    (2, "Rahul Mehta", "rahul.mehta", "Data Analyst", "2026-12-08", "CLIENT_SITE", "Client Site - Jebel Ali", "INTAKE_PENDING"),
    (3, "Sara Al Mansoori", "sara.almansoori", "HR Business Partner", "2026-12-15", "OFFICE", "Dubai HQ", "INTAKE_PENDING"),
]


def put(table, rows, conflict):
    r = c.post(f"/{table}", params={"on_conflict": conflict}, json=rows,
               headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    status = "OK " if r.status_code < 300 else "FAIL"
    print(f"{status} {table}: {len(rows)} row(s)" + ("" if r.status_code < 300 else f" -> {r.status_code} {r.text[:200]}"))


def remove():
    ids = ",".join(cid(n) for n, *_ in CASES)
    for t, col, val in [
        ("hris_sync_records", "case_id", f"in.({ids})"),
        ("intake_discrepancies", "case_id", f"in.({ids})"),
        ("domain_events_outbox", "case_id", f"in.({ids})"),
        ("person_records", "email", "like.*@sample.invalid"),
        ("pending_starts", "offer_ref", "like.SAMPLE-*"),
        ("offers", "offer_ref", "like.SAMPLE-*"),
        ("client_holidays", "name", "like.[SAMPLE]*"),
        ("onboarding_cases", "case_id", f"in.({ids})"),
    ]:
        r = c.delete(f"/{t}", params={col: val})
        print(t, r.status_code)


def seed():
    put("onboarding_cases", [dict(
        case_id=cid(n), client_id=CLIENT, candidate_name=name, candidate_email=f"{mail}@sample.invalid",
        hr_officer_id="hr.officer.1", joining_date=jd, work_location_type=wl, location_name=loc,
        status=st, onboarding_attributes=[], department="Engineering" if n == 1 else None)
        for n, name, mail, _d, jd, wl, loc, st in CASES], "case_id")

    put("person_records", [dict(
        record_id=cid(100 + n), client_id=CLIENT, email=f"{mail}@sample.invalid", full_name=name,
        kind="OPEN_CASE", case_id=cid(n), id_number=None, hris_external_ref=None) for n, name, mail, *_ in CASES]
        + [dict(record_id=cid(110), client_id=CLIENT, email="omar.farouk@sample.invalid", full_name="Omar Farouk",
                kind="PAST_EMPLOYEE", case_id=None, id_number="784-1990-0000000-1", hris_external_ref="ZP-10042")], "record_id")

    put("offers", [dict(
        client_id=CLIENT, offer_ref=f"SAMPLE-OFFER-{n}", candidate_email=f"{mail}@sample.invalid",
        candidate_name=name, designation=des, hr_officer_id="hr.officer.1", status="ACCEPTED",
        offer_payload={"sample": True}) for n, name, mail, des, *_ in CASES], "client_id,offer_ref")

    put("pending_starts", [dict(
        pending_id=cid(200), client_id=CLIENT, offer_ref="SAMPLE-OFFER-3",
        reasons=["possible_duplicate"], status="WAITING")], "pending_id")

    put("intake_discrepancies", [dict(
        discrepancy_id=cid(300), case_id=cid(2), field_name="designation", value_form="Data Analyst",
        value_other="Senior Data Analyst", other_source="offer_letter", form_version=1,
        other_fingerprint="sample-fp-1", status="OPEN")], "discrepancy_id")

    put("hris_sync_records", [dict(
        record_id=cid(400), case_id=cid(1), client_id=CLIENT, target_system="ZOHO_PEOPLE", form_version=1,
        prepared_payload={"full_name": "Aisha Khan", "designation": "Software Engineer"},
        sensitive_field_names=["salary_and_terms"], status="PREPARED")], "record_id")

    put("domain_events_outbox", [dict(
        event_id=cid(500 + n), client_id=CLIENT, case_id=cid(n), event_type="CaseOpenedEvent",
        payload={"case_id": cid(n)}) for n, *_ in CASES], "event_id")

    put("client_holidays", [
        dict(client_id=CLIENT, holiday_date="2026-12-02", name="[SAMPLE] UAE National Day", is_observed=True),
        dict(client_id=CLIENT, holiday_date="2026-12-03", name="[SAMPLE] UAE National Day (2nd)", is_observed=True)],
        "client_id,holiday_date")

    if os.environ.get("UC01_FIELD_ENCRYPTION_KEY"):
        from datetime import date

        from pydantic import SecretStr

        from sub_agent.adapters.supabase.client import PostgrestClient
        from sub_agent.adapters.supabase.repositories import SupabaseFormRepository
        from sub_agent.crypto import FieldCipher
        from sub_agent.models.onboarding_form import OnboardingForm

        with PostgrestClient(URL, SecretStr(KEY)) as db:
            repo = SupabaseFormRepository(db, FieldCipher.from_env())
            for n, name, mail, des, jd, wl, *_ in CASES:
                repo.save(OnboardingForm(
                    form_id=cid(600 + n), client_id=CLIENT, case_id=cid(n), version=1, candidate_name=name,
                    candidate_email=f"{mail}@sample.invalid", designation=des, joining_date=date.fromisoformat(jd),
                    work_location_type=wl, client_site_id="SITE-JA-01" if wl == "CLIENT_SITE" else None,
                    salary_and_terms=SecretStr("SAMPLE: AED 15,000 basic"), status="DRAFT"))
            print(f"OK  onboarding_forms: {len(CASES)} row(s) (encrypted)")
    else:
        print("SKIP onboarding_forms: UC01_FIELD_ENCRYPTION_KEY not set")


if __name__ == "__main__":
    remove() if "--remove" in sys.argv else seed()
