"""Synthetic onboarding-form documents for UC-01 (template onboarding-form-2026).

    python scripts/mock_onboarding_documents.py generate   # write data/mock/documents/*.docx (+ the complete form as .pdf)
                                                           # and the sample employee (data/mock/sample_employee_record.json)
                                                           # as SYNTHETIC_sample_employee.pdf / .docx
    python scripts/mock_onboarding_documents.py run        # process every scenario, report
    python scripts/mock_onboarding_documents.py run FILE.docx   # extract one document, report

Every document is built with the SAME table layout and template text as the
2026 onboarding form, with the HR value column filled in with clearly
synthetic data: client SYN-UC01-DOC-TEST, .invalid email addresses, and
labelled placeholders ("SYNTHETIC-MOBILE-01") instead of realistic phone,
passport or Emirates ID numbers. Nothing here is a real person.

`run` processes the scenarios through the real DocumentIntakeService and
the unchanged open_onboarding_case flow against a fresh IN-MEMORY container.
Nothing is persisted and nothing leaves the process. Exit 0 only when every
scenario produced its expected status.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
OUT_DIR = ROOT / "data" / "mock" / "documents"
CLIENT_ID = "SYN-UC01-DOC-TEST"
#: Fixed "today" for the run, so a scenario's outcome never drifts with the calendar.
RUN_TODAY = date(2026, 11, 4)

# The template's own rows: (label, text the template prints in column 1).
TEMPLATE_ROWS: list[tuple[str, list[str]]] = [
    ("Offer Letter", ["ACME/GLOBAL"]),
    ("Name of Employee", []),
    ("Reporting to", []),
    ("Designation", []),
    ("R&R", []),
    ("Final Salary", []),
    ("DOJ", []),
    ("Probation period (3 or 6 months)", []),
    ("1 year or 2 years visa", ["1 YR – INSIDE COUNTRY: AED 4700/-", "1 YR – OUTSIDE COUNTRY: AED 4200/-",
                                "2 YRS – INSIDE COUNTRY: AED 5100/-",
                                "2 YRS – OUTSIDE COUNTRY: AED 4800/- NON SPONSORED ID CARD: AED 1100/-"]),
    ("Visa – visit + entry permit", ["VISIT VISA : AED 380/-", "DUMMY RETURN TICKET: AED 100/-"]),
    ("Direct entry permit", ["DOCS REQUIRED:", "PASSPORT (INCLUDING SCANNED PASSPORT COVER)",
                             "VISA COPY / CANCELLATION PAPER", "EMIRATES ID", "SIGNED OFFER LETTER",
                             "CLEAR PHOTO AS PER ICA REQUIREMENTS", "ATTESTED DEGREE CERTIFICATE",
                             "FATHER’S NAME/MOTHER’S NAME/MARITAL STATUS/MOBILE NUMBER"]),
    ("Insurance", ["BASIC: AED 1000/-", "WN: AED 3500/- + 1000/- FOR MATERNITY RN: AED 4500/- + 1900/- FOR MATERNITY",
                   "NOTE: INDICATIVE PREMIUM DEPENDS ON PRE EXISTING CONDITION AND AGE",
                   "NOTE: 10% TO 15% INCREASE ON PREMIUM EVERY YEAR"]),
    ("Annual ticket –Employee", ["SECTORS AND TRAVEL DATES"]),
    ("Annual ticket- Family", []),
    ("Insurance for Employee", ["CENSUS TO BE FILLED ALONG WITH REQUIRED", "DOCUMENTS"]),
    ("Insurance for Family", []),
    ("Hotel stay – how many days (only applicable for staff outside UAE)",
     ["BUDGET & AREA OR LOCATION ALSO TO BE MENTIONED"]),
    ("Any other promises made to the candidate", []),
]

_ALL_DOCS = ["Passport: RECEIVED", "Passport cover: RECEIVED", "Signed offer letter: RECEIVED",
             "Photograph: RECEIVED", "Degree certificate: RECEIVED"]
_PERSONAL = ["Father's name: SYNTHETIC-FATHER-NAME-01", "Mother's name: SYNTHETIC-MOTHER-NAME-01",
             "Marital status: Single", "Mobile number: SYNTHETIC-MOBILE-01"]


def _complete(name: str, designation: str) -> dict[str, Any]:
    """HR value column for a fully filled-in form (outside-country visa)."""
    return {
        "Name of Employee": name,
        "Reporting to": "SYN Head of Data Platforms",
        "Designation": designation,
        "R&R": f"Owns data pipelines as {designation}",
        "Final Salary": "SYNTHETIC: AED 18,500 per month",
        "DOJ": "2027-01-11",
        "Probation period (3 or 6 months)": "6 months",
        "1 year or 2 years visa": "2 years - outside country",
        "Visa – visit + entry permit": "Entry permit via employer",
        "Direct entry permit": "\n".join(["Direct entry permit: Not required", *_ALL_DOCS, *_PERSONAL]),
        "Insurance": "WN",
        "Annual ticket –Employee": "DXB-BOM, December",
        "Annual ticket- Family": "Not applicable",
        "Insurance for Employee": "Census submitted",
        "Insurance for Family": "Not applicable",
        "Hotel stay – how many days (only applicable for staff outside UAE)": "7 days, AED 450/night, Al Barsha",
        "Any other promises made to the candidate": "None",
    }


_RR_SPECIFIC = [
    "Build and operate the data pipelines owned by the Synthetic Data Engineer role.",
    "Maintain data quality checks for the synthetic onboarding datasets.",
    "Document pipeline runbooks and hand-over notes for the data platform team.",
]
_RR_EXAMPLE_HEADING = "Roles and Responsibilities (eg given below)"


SAMPLE_EMPLOYEE = ROOT / "data" / "mock" / "sample_employee_record.json"
_DOC_LABELS = {"passport": "Passport", "passport_cover": "Passport cover", "visa_copy": "Visa copy",
               "cancellation_paper": "Cancellation paper", "emirates_id": "Emirates ID",
               "signed_offer_letter": "Signed offer letter", "photograph": "Photograph",
               "degree_certificate": "Degree certificate"}
_PERSONAL_LABELS = {"father_name": "Father's name", "mother_name": "Mother's name",
                    "marital_status": "Marital status", "mobile_number": "Mobile number"}


def form_from_fixture(fixture: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
    """(values, rr_heading, rr) for build_docx/build_pdf from a fixture's 'extracted' section.

    Only what the fixture states is written; a null stays an empty cell."""
    x = fixture["extracted"]
    e, emp, ben = x["employee"], x["employment"], x["benefits"]
    where = {"OUTSIDE_COUNTRY": "outside country", "INSIDE_COUNTRY": "inside country"}[emp["visa_location_category"]]
    years = emp["visa_duration_years"]
    lines = [f"Direct entry permit: {emp['direct_entry_permit']}"]
    lines += [f"{_DOC_LABELS[k]}: {v}" for k, v in x["documents"].items() if v]
    lines += [f"{_PERSONAL_LABELS[k]}: {str(v).title() if k == 'marital_status' else v}"
              for k, v in x["personal"].items() if v]
    values = {
        "Name of Employee": e["employee_name"], "Reporting to": e["reporting_to"], "Designation": e["designation"],
        "R&R": x["roles"]["summary"], "Final Salary": e["final_salary"], "DOJ": e["joining_date"],
        "Probation period (3 or 6 months)": f"{e['probation_months']} months",
        "1 year or 2 years visa": f"{years} {'year' if years == 1 else 'years'} - {where}",
        "Visa – visit + entry permit": emp["visit_visa_entry_permit"],
        "Direct entry permit": "\n".join(lines),
        "Insurance": x["insurance"]["plan"], "Annual ticket –Employee": ben["annual_employee_ticket"],
        "Annual ticket- Family": ben["annual_family_ticket"],
        "Insurance for Employee": x["insurance"]["employee_insurance"],
        "Insurance for Family": x["insurance"]["family_insurance"],
        "Hotel stay – how many days (only applicable for staff outside UAE)":
            f"{ben['hotel_stay_days']} days, {ben['hotel_budget_and_location']}",
        "Any other promises made to the candidate": x["offer"]["other_promises"],
    }
    return {k: v for k, v in values.items() if v}, "Roles and Responsibilities", list(x["roles"]["responsibilities"])


def _without(values: dict[str, Any], *labels: str) -> dict[str, Any]:
    return {k: v for k, v in values.items() if k not in labels}


def _ctx(email: str, **over: Any) -> dict[str, Any]:
    ctx = {
        "client_id": CLIENT_ID, "hr_officer_id": "SYN-HR-OFFICER-01", "candidate_email": email,
        "offer_accepted": True, "work_location_type": "OFFICE",
        "placement": {"location_name": "SYN Dubai Office", "department": "Data"},
        # The INTERNAL form approval is the caller's attestation, never
        # something read out of the document.
        "form_status": "APPROVED", "approved_by": "SYN-HR-MANAGER-01",
        "approved_at": datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc).isoformat(),
    }
    ctx.update(over)
    return ctx


SCENARIOS: list[dict[str, Any]] = [
    {"name": "complete_employee", "file": "SYNTHETIC_onboarding_complete.docx",
     "values": _complete("Aisha Synthetic", "Synthetic Data Engineer"),
     "rr_heading": "Roles and Responsibilities", "rr": _RR_SPECIFIC,
     "context": _ctx("aisha.synthetic@example.invalid"), "expected": "CREATED"},
    {"name": "missing_employee_details", "file": "SYNTHETIC_onboarding_missing_details.docx",
     "values": _without(_complete("", "Synthetic Data Engineer"), "Name of Employee", "Reporting to"),
     "rr_heading": "Roles and Responsibilities", "rr": _RR_SPECIFIC,
     "context": _ctx("ben.synthetic@example.invalid"), "expected": "VALIDATION_FAILED"},
    {"name": "missing_required_documents", "file": "SYNTHETIC_onboarding_missing_documents.docx",
     "values": {**_complete("Carla Synthetic", "Synthetic Data Engineer"),
                "Direct entry permit": "\n".join(["Passport: PENDING", "Signed offer letter: RECEIVED", *_PERSONAL])},
     "rr_heading": "Roles and Responsibilities", "rr": _RR_SPECIFIC,
     "context": _ctx("carla.synthetic@example.invalid"), "expected": "NEEDS_REVIEW"},
    {"name": "missing_salary_and_joining_date", "file": "SYNTHETIC_onboarding_missing_salary_doj.docx",
     "values": _without(_complete("Dev Synthetic", "Synthetic Data Engineer"), "Final Salary", "DOJ"),
     "rr_heading": "Roles and Responsibilities", "rr": _RR_SPECIFIC,
     "context": _ctx("dev.synthetic@example.invalid"), "expected": "VALIDATION_FAILED"},
    {"name": "conflicting_fields", "file": "SYNTHETIC_onboarding_conflicting.docx",
     # Two different dates in two cells, an out-of-policy probation and the
     # template's example R&R: every one of these must go to HR review.
     "values": {**_complete("Elena Synthetic", "Synthetic Data Engineer"),
                "DOJ": ["07/12/2026", "12/07/2026"], "Probation period (3 or 6 months)": "4 months"},
     "rr_heading": _RR_EXAMPLE_HEADING, "rr": _RR_SPECIFIC,
     "context": _ctx("elena.synthetic@example.invalid"), "expected": "NEEDS_REVIEW"},
    {"name": "complete_but_no_placement", "file": "SYNTHETIC_onboarding_complete_no_placement.docx",
     "values": _complete("Faris Synthetic", "Synthetic Data Engineer"),
     "rr_heading": "Roles and Responsibilities", "rr": _RR_SPECIFIC,
     "context": _ctx("faris.synthetic@example.invalid", placement=None), "expected": "HELD",
     "expected_reasons": ["location_name_required"]},
    {"name": "form_not_yet_approved", "file": "SYNTHETIC_onboarding_complete_unapproved.docx",
     "values": _complete("Gaurav Synthetic", "Synthetic Data Engineer"),
     "rr_heading": "Roles and Responsibilities", "rr": _RR_SPECIFIC,
     "context": _ctx("gaurav.synthetic@example.invalid", form_status="AWAITING_APPROVAL", approved_by=None,
                     approved_at=None), "expected": "HELD", "expected_reasons": ["form not approved"]},
    {"name": "duplicate_submission", "file": "SYNTHETIC_onboarding_complete.docx", "reuse": "complete_employee",
     "context": _ctx("aisha.synthetic@example.invalid"), "expected": "LINKED_EXISTING"},
]


# ------------------------------------------------------------------ DOCX writer

_CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
       '<Default Extension="xml" ContentType="application/xml"/>'
       '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
       'wordprocessingml.document.main+xml"/></Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
         'officeDocument" Target="word/document.xml"/></Relationships>')


def _p(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def _cell(lines: list[str]) -> str:
    return "<w:tc>" + ("".join(_p(t) for t in lines) or "<w:p/>") + "</w:tc>"


def build_docx(values: dict[str, Any], rr_heading: str, rr: list[str]) -> bytes:
    """A .docx with the template's rows; `values[label]` is the HR value
    (a string for column 2, or [col1_value, col2_value] to fill both)."""
    rows = ["<w:tr>" + _cell(["SYNTHETIC TEST DOCUMENT - NOT A REAL EMPLOYEE"]) + _cell([]) + _cell([]) + "</w:tr>"]
    for label, printed in TEMPLATE_ROWS:
        value = values.get(label)
        col1, col2 = list(printed), []
        if isinstance(value, list):
            col1 = col1 + [value[0]]
            col2 = [value[1]]
        elif value:
            col2 = value.split("\n")
        rows.append("<w:tr>" + _cell([label]) + _cell(col1) + _cell(col2) + "</w:tr>")
    body = (f"<w:tbl>{''.join(rows)}</w:tbl>" + _p(rr_heading) + "".join(_p(t) for t in rr))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body>{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def _pdf_text(text: str) -> str:
    """A PDF string literal in WinAnsi (the en dash the template prints is kept)."""
    raw = text.replace("\u2013", "\x96").replace("\u2019", "\x92")
    out = raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return "".join(c if ord(c) < 128 else f"\\{ord(c):03o}" for c in out)


def build_pdf(values: dict[str, Any], rr_heading: str, rr: list[str]) -> bytes:
    """The same form as build_docx, as a text-based PDF: the three table
    columns at fixed x positions (label 30, template 170, value 340), cell
    text wrapped to its column width like a real PDF, then the Roles and
    Responsibilities section."""
    import textwrap

    widths = (30, 40, 52)  # characters per column line at 7pt Helvetica
    rows = [(["SYNTHETIC TEST DOCUMENT - NOT A REAL EMPLOYEE"], [], [])]
    for label, printed in TEMPLATE_ROWS:
        value = values.get(label)
        col1, col2 = list(printed), []
        if isinstance(value, list):
            col1, col2 = col1 + [value[0]], [value[1]]
        elif value:
            col2 = value.split("\n")
        rows.append(([label], col1, col2))
    rows = [tuple([w for line in cell for w in (textwrap.wrap(line, width) or [""])] for cell, width in zip(r, widths))
            for r in rows]
    pages: list[list[tuple[float, float, str]]] = [[]]
    y = 800.0

    def put(x: float, text: str, at: float) -> None:
        pages[-1].append((x, at, text))

    for cells in rows:
        height = max(len(c) for c in cells) or 1
        if y - 11 * height < 40:
            pages.append([])
            y = 800.0
        for x, cell in zip((30.0, 170.0, 340.0), cells):
            for i, line in enumerate(cell):
                put(x, line, y - 11 * i)
        y -= 11 * height + 4
    for line in [rr_heading] + [f"\u2022 {t}" for t in rr]:
        if y < 40:
            pages.append([])
            y = 800.0
        put(30.0, line.replace("\u2022", "-"), y)
        y -= 13
    return render_pdf(pages)


def render_pdf(pages: list[list[tuple[float, float, str]]]) -> bytes:
    """A minimal PDF: one Helvetica text run per (x, y, text), one list per page."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for runs in pages:
        stream = "".join(f"BT /F1 7 Tf 1 0 0 1 {x:.1f} {yy:.1f} Tm ({_pdf_text(t)}) Tj ET\n" for x, yy, t in runs)
        data = stream.encode("latin-1")
        objects.append(f"<< /Length {len(data)} >>\nstream\n" + data.decode("latin-1") + "endstream")
        content_no = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1")
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode("latin-1")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("latin-1")
    return bytes(out)


def scenario_document(scenario: dict[str, Any]) -> bytes:
    source = next(s for s in SCENARIOS if s["name"] == scenario["reuse"]) if scenario.get("reuse") else scenario
    return build_docx(source["values"], source["rr_heading"], source["rr"])


# ------------------------------------------------------------------ run


def _container():
    # A preset empty SUPABASE_URL wins over .env (api/settings.py): the run
    # is in-memory no matter what the local .env says.
    os.environ["SUPABASE_URL"] = ""
    from sub_agent.api.container import build_container
    from sub_agent.api.settings import Settings
    from sub_agent.documents.service import DocumentIntakeService

    container = build_container(Settings(app_env="dev", dedup_hmac_key="mock-only", system_api_key="mock-only"))
    return container, DocumentIntakeService(container.onboarding_deps, llm=container.onboarding_deps.llm,
                                            today=lambda: RUN_TODAY)


def run_scenarios() -> list[dict[str, Any]]:
    from sub_agent.documents.service import DocumentIntakeContext

    _, service = _container()
    report = []
    for s in SCENARIOS:
        result = service.process(scenario_document(s), s["file"], DocumentIntakeContext(**s["context"]))
        ok = result.status.value == s["expected"] and all(r in result.reasons for r in s.get("expected_reasons", []))
        report.append({"scenario": s["name"], "expected": s["expected"], "status": result.status.value, "ok": ok,
                       "employee_no": result.employee_no, "case_id": result.case_id,
                       "errors": result.errors, "review_reasons": result.review_reasons,
                       "outstanding_documents": result.outstanding_documents, "reasons": result.reasons,
                       "source_document_stored": result.source_document_stored})
    return report


def extract_one(path: Path) -> dict[str, Any]:
    from sub_agent.documents.service import safe_extraction_view

    _, service = _container()
    return safe_extraction_view(service.extract(path.read_bytes(), path.name))


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("generate", "run"):
        print(__doc__)
        return 2
    if argv[0] == "generate":
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for s in SCENARIOS:
            if not s.get("reuse"):
                (OUT_DIR / s["file"]).write_bytes(scenario_document(s))
                print("wrote", (OUT_DIR / s["file"]).relative_to(ROOT))
        complete = SCENARIOS[0]
        pdf = OUT_DIR / Path(complete["file"]).with_suffix(".pdf").name
        pdf.write_bytes(build_pdf(complete["values"], complete["rr_heading"], complete["rr"]))
        print("wrote", pdf.relative_to(ROOT))
        sample = form_from_fixture(json.loads(SAMPLE_EMPLOYEE.read_text(encoding="utf-8")))
        for suffix, build in ((".pdf", build_pdf), (".docx", build_docx)):
            path = OUT_DIR / f"SYNTHETIC_sample_employee{suffix}"
            path.write_bytes(build(*sample))
            print("wrote", path.relative_to(ROOT))
        return 0
    if len(argv) > 1:
        print(json.dumps(extract_one(Path(argv[1])), indent=1, default=str))
        return 0
    report = run_scenarios()
    for r in report:
        detail = r["employee_no"] or ", ".join(r["errors"] or r["reasons"] or r["review_reasons"][:3])
        print(f"[{'OK ' if r['ok'] else 'BAD'}] {r['scenario']:32s} {r['status']:18s} {detail}")
    print("NOTE: in-memory only - nothing persisted; source documents are not stored anywhere.")
    ok = all(r["ok"] for r in report)
    print("RESULT:", "ALL SCENARIOS AS EXPECTED" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
