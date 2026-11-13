"""The populated SYNTHETIC copy of the 2026 onboarding form, and complete
extraction from it (.docx and text-based .pdf).

The real original lives in Supabase Storage and is not committed; the
generator is exercised on a blank stand-in with the same template rows
(mock_onboarding_documents.build_docx with no values). The committed
populated copy (data/mock/documents/SYNTHETIC_onboarding_form_2026_populated.*)
was generated from the real original by scripts/synthetic_onboarding_form.py."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sub_agent.documents.budget import AmountProvenance, BudgetCategory, BudgetStatus
from sub_agent.documents.docx_reader import read_docx
from sub_agent.documents.extraction import extract_onboarding_form
from sub_agent.documents.models import DocumentStatus, FieldStatus
from sub_agent.documents.pdf_reader import read_document
from sub_agent.documents.service import safe_extraction_view

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "data" / "mock" / "documents"
POPULATED_DOCX = DOCS / "SYNTHETIC_onboarding_form_2026_populated.docx"
POPULATED_PDF = DOCS / "SYNTHETIC_onboarding_form_2026_populated.pdf"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load("synthetic_form", ROOT / "scripts" / "synthetic_onboarding_form.py")
mock_docs = _load("mock_docs_for_synthetic", ROOT / "scripts" / "mock_onboarding_documents.py")


def blank_original() -> bytes:
    """Stand-in for the blank original: the template's rows, nothing filled in."""
    return mock_docs.build_docx({}, "Roles and Responsibilities (eg given below)",
                                ["Example duty one.", "Example duty two."])


def extract(data: bytes, name: str):
    return extract_onboarding_form(read_document(data, name), document_name=name,
                                   sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))


# ------------------------------------------------------------------ the generator


def test_the_generator_never_changes_the_original_and_copies_every_other_part_unchanged():
    original = blank_original()
    before = hashlib.sha256(original).hexdigest()
    populated = gen.populate_docx(original)
    assert hashlib.sha256(original).hexdigest() == before
    src, out = zipfile.ZipFile(io.BytesIO(original)), zipfile.ZipFile(io.BytesIO(populated))
    assert src.namelist() == out.namelist()
    for name in src.namelist():
        if name != "word/document.xml":
            assert src.read(name) == out.read(name), name


def test_the_generator_keeps_the_template_rows_and_labels_synthetic_data():
    content = read_docx(gen.populate_docx(blank_original()))
    original = read_docx(blank_original())
    form = content.tables[0]
    assert [r[0] for r in form.rows] == [r[0] for r in original.tables[0].rows]  # labels unchanged
    assert [r[1] for r in form.rows] == [r[1] for r in original.tables[0].rows]  # printed rate card unchanged
    assert content.paragraphs[0].text.startswith("SYNTHETIC TEST DATA - NOT A REAL EMPLOYEE")
    assert content.tables[1].rows[0][0][0].startswith("SYNTHETIC TEST DATA")
    assert not any("eg given below" in p.text for p in content.paragraphs)


def test_the_generator_refuses_to_overwrite_a_filled_value_cell():
    partly_filled = mock_docs.build_docx({"Name of Employee": "Someone Already Written"}, "Roles", [])
    with pytest.raises(ValueError, match="not empty"):
        gen.populate_docx(partly_filled)


def test_the_generator_refuses_an_already_populated_copy():
    with pytest.raises(ValueError, match="single table"):
        gen.populate_docx(gen.populate_docx(blank_original()))


def test_the_generator_is_deterministic():
    assert gen.populate_docx(blank_original()) == gen.populate_docx(blank_original())


def test_every_synthetic_value_is_fictional():
    text = " ".join(" ".join(v) for v in gen.VALUES.values()) + " ".join(v for _, v in gen.SUPPLEMENTARY)
    assert "example.invalid" in text and "SYNTHETIC-MOBILE" in text and "SYNTHETIC-FATHER-NAME" in text
    assert "Synthetic" in gen.VALUES["name of employee"][0]


# ------------------------------------------------------------------ complete extraction


@pytest.fixture(scope="module")
def populated():
    return extract(POPULATED_DOCX.read_bytes(), POPULATED_DOCX.name)


def test_the_committed_copy_is_marked_synthetic_and_fully_extracted(populated):
    md = populated.metadata
    assert md.synthetic_document and md.template_recognised and md.completeness == 1.0
    assert md.missing_fields == md.invalid_fields == md.uncertain_fields == md.unrecognised_rows == []


def test_employee_and_employment_fields(populated):
    e, emp = populated.employee, populated.employment
    assert e.employee_name.value == "Omar Synthetic" and e.designation.value == "Synthetic Data Governance Engineer"
    assert e.reporting_to.value == "SYN Head of Data Governance"
    assert e.joining_date.value == date(2027, 1, 17) and e.probation_months.value == 6
    assert e.final_salary.value.get_secret_value() == "SYNTHETIC: AED 22,000 per month (all inclusive)"
    assert emp.visa_duration_years.value == 2 and emp.visa_location_category.value == "OUTSIDE_COUNTRY"
    assert populated.offer.issuing_entity.value in ("ACME/GLOBAL", "ACME / GLOBAL")
    sup = {k.split(".")[1]: f.value for k, f in populated.supplementary_fields().items()}
    assert sup == {"employee_email": "omar.synthetic@example.invalid", "department": "Data & Analytics",
                   "nationality": "Indian", "work_location": "SYN Dubai Office", "grade": "L5",
                   "employment_duration": "Permanent (unlimited contract)"}
    assert len(populated.roles.responsibilities) == 17 and not populated.roles.is_example


def test_documents_checklist_and_personal_details(populated):
    d, p = populated.documents, populated.personal
    assert d.passport.value == DocumentStatus.RECEIVED and d.emirates_id.value == DocumentStatus.PENDING
    assert d.degree_certificate.value == DocumentStatus.RECEIVED
    assert p.marital_status.value == "MARRIED" and p.mobile_number.value.get_secret_value() == "SYNTHETIC-MOBILE-31"


def test_budget_from_the_populated_form(populated):
    b = populated.budget
    assert b.status == BudgetStatus.COMPLETE and b.currency == "AED" and b.budget_total == Decimal("21500.00")
    assert {c.value: b.category_total(c) for c in BudgetCategory} == {
        "VISA": Decimal("4800.00"), "INSURANCE": Decimal("8000.00"), "TRAVEL": Decimal("3550.00"),
        "ACCOMMODATION": Decimal("3150.00"), "OTHER": Decimal("2000.00")}
    assert len(b.reference_rates) == 12 and b.reference_differences == []
    # The salary is never a budget line.
    assert not any("22,000" in (x.money.original_text if x.money else "") for x in b.lines)
    assert b.line("accommodation.hotel.budget").provenance == AmountProvenance.CALCULATED


def test_the_pdf_copy_extracts_exactly_like_the_docx(populated):
    pdf = extract(POPULATED_PDF.read_bytes(), POPULATED_PDF.name)

    def view(x):
        v = safe_extraction_view(x)
        for f in v["fields"].values():
            f.pop("source", None)
        for f in v["supplementary"].values():
            f.pop("source", None)
        for line in v["budget"]["lines"] + v["budget"]["reference_rates"]:
            line.pop("source", None)
        for r in v["reference_charges"]:
            r.pop("source", None)
        v["responsibilities"].pop("source", None)
        return v

    assert view(pdf) == view(populated)


def test_the_safe_view_never_carries_the_salary_or_personal_secrets(populated):
    text = str(safe_extraction_view(populated))
    for secret in ("22,000", "SYNTHETIC-MOBILE-31", "SYNTHETIC-FATHER-NAME-31", "SYNTHETIC-MOTHER-NAME-31"):
        assert secret not in text


# ------------------------------------------------------------------ failure documents


def _variant(**changes) -> bytes:
    values = {k: list(v) for k, v in gen.VALUES.items()}
    for key, lines in changes.items():
        values[key] = lines
    return gen.populate_docx(blank_original(), values=values)


@pytest.mark.parametrize("label,field", [("name of employee", "employee.employee_name"),
                                         ("doj", "employee.joining_date"),
                                         ("final salary", "employee.final_salary")])
def test_a_missing_mandatory_value_is_missing_not_invented(label, field):
    x = extract(_variant(**{label: []}), "v.docx")
    assert x.fields()[field].status == FieldStatus.MISSING and field in x.metadata.missing_fields


def test_a_blank_form_has_no_values_and_no_budget():
    x = extract(blank_original(), "blank.docx")
    assert x.metadata.template_recognised and x.metadata.completeness < 0.1
    assert x.budget.status == BudgetStatus.UNAVAILABLE and x.budget.budget_total is None
    assert x.fields()["employee.employee_name"].status == FieldStatus.MISSING


def test_an_invalid_currency_is_kept_as_invalid():
    x = extract(_variant(**{"annual ticket -employee": ["Ticket cost: XYZ 1,250"]}), "v.docx")
    line = x.budget.line("travel.employee.ticket")
    assert line.provenance == AmountProvenance.INVALID and x.budget.status == BudgetStatus.PARTIAL
