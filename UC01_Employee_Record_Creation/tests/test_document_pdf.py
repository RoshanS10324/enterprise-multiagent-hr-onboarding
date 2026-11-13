"""Text-based PDF onboarding forms: same extraction and same downstream path as .docx."""

from __future__ import annotations

import builtins
import importlib.util
import io
import json
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("pypdf")

from sub_agent.documents.docx_reader import DocumentReadError, read_docx  # noqa: E402
from sub_agent.documents.extraction import extract_onboarding_form  # noqa: E402
from sub_agent.documents.pdf_reader import read_document, read_pdf  # noqa: E402
from sub_agent.documents.providers import PdfExtractionProvider  # noqa: E402
from sub_agent.documents.service import (  # noqa: E402
    DocumentIntakeContext,
    DocumentIntakeService,
    DocumentIntakeStatus,
    safe_extraction_view,
)

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("mock_docs", ROOT / "scripts" / "mock_onboarding_documents.py")
mock_docs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mock_docs)
PDF_FIXTURE = ROOT / "data" / "mock" / "documents" / "SYNTHETIC_onboarding_complete.pdf"


def _view(content):
    v = json.loads(json.dumps(safe_extraction_view(
        extract_onboarding_form(content, document_name="x", sha256="0", size_bytes=1)), default=str))
    v.pop("metadata", None)
    for f in v["fields"].values():
        f.pop("source", None)
    for r in v["reference_charges"]:
        r.pop("source", None)
    v["responsibilities"].pop("source", None)
    # Budget and supplementary rows: same rule - the values, not the cell positions.
    for line in v["budget"]["lines"] + v["budget"]["reference_rates"]:
        line.pop("source", None)
    for f in v["supplementary"].values():
        f.pop("source", None)
    return v


@pytest.mark.parametrize("scenario", [s for s in mock_docs.SCENARIOS if not s.get("reuse")], ids=lambda s: s["name"])
def test_a_pdf_extracts_exactly_like_the_same_docx(scenario):
    args = (scenario["values"], scenario["rr_heading"], scenario["rr"])
    assert _view(read_pdf(mock_docs.build_pdf(*args), "x.pdf")) == _view(read_docx(mock_docs.build_docx(*args), "x.docx"))


@pytest.mark.parametrize("name", ["SYNTHETIC_sample_employee.pdf", "SYNTHETIC_sample_employee.docx"])
def test_the_sample_employee_document_extracts_exactly_like_its_mock_fixture(name):
    from sub_agent.documents.providers import MockExtractionProvider

    data = (ROOT / "data" / "mock" / "documents" / name).read_bytes()
    from_document = _view(read_document(data, name))
    from_mock = json.loads(json.dumps(safe_extraction_view(MockExtractionProvider(mock_docs.SAMPLE_EMPLOYEE).extract()),
                                      default=str))
    from_mock.pop("metadata", None)
    for f in from_mock["fields"].values():
        f.pop("source", None)
    from_mock["responsibilities"].pop("source", None)
    assert from_document["fields"] == from_mock["fields"]  # wrapped PDF values are unwrapped, items kept apart
    assert from_document["responsibilities"] == from_mock["responsibilities"]


def test_the_committed_pdf_fixture_is_the_complete_synthetic_form():
    x = PdfExtractionProvider(PDF_FIXTURE.read_bytes(), PDF_FIXTURE.name).extract()
    assert x.metadata.template_recognised and x.employee.employee_name.value == "Aisha Synthetic"
    assert x.employee.final_salary.value.get_secret_value().startswith("SYNTHETIC")
    assert x.employee.joining_date.value == date(2027, 1, 11)


def test_a_pdf_goes_through_the_same_intake_path_and_is_created(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "")
    from sub_agent.api.container import build_container
    from sub_agent.api.settings import Settings

    container = build_container(Settings(app_env="dev", dedup_hmac_key="t", system_api_key="t"))
    service = DocumentIntakeService(container.onboarding_deps, today=lambda: date(2026, 11, 4))
    scenario = mock_docs.SCENARIOS[0]
    result = service.process(PDF_FIXTURE.read_bytes(), "form.pdf", DocumentIntakeContext(**scenario["context"]))
    assert result.status == DocumentIntakeStatus.CREATED, (result.errors, result.review_reasons)
    case = container.onboarding_deps.case_repo.get(result.case_id)
    assert case.candidate_name == "Aisha Synthetic"
    audit = json.dumps([e.model_dump(mode="json") for e in container.onboarding_deps.audit_log.entries()])
    assert "18,500" not in audit and "SYNTHETIC-MOBILE" not in audit  # no document text in the audit


@pytest.mark.parametrize("data,name,code", [
    (b"", "a.pdf", "EMPTY_FILE"),
    (b"not a pdf at all", "a.pdf", "NOT_A_PDF"),
    (b"%PDF-1.4\ngarbage that is not a pdf body", "a.pdf", "CORRUPT_PDF"),
    (b"%PDF-1.4", "a.txt", "UNSUPPORTED_FORMAT"),
])
def test_invalid_documents_are_rejected_with_a_code(data, name, code):
    with pytest.raises(DocumentReadError) as exc:
        read_document(data, name)
    assert exc.value.code == code


def test_a_pdf_without_a_text_layer_is_rejected_not_guessed():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(DocumentReadError) as exc:
        read_pdf(buf.getvalue(), "scan.pdf")
    assert exc.value.code == "NO_TEXT_LAYER"


def test_an_encrypted_pdf_is_refused():
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(PDF_FIXTURE.read_bytes())).pages:
        writer.add_page(page)
    writer.encrypt("synthetic-password")
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(DocumentReadError) as exc:
        read_pdf(buf.getvalue(), "locked.pdf")
    assert exc.value.code == "ENCRYPTED_PDF"


def test_too_many_pages_are_refused(monkeypatch):
    from sub_agent.documents import pdf_reader

    monkeypatch.setattr(pdf_reader, "MAX_PAGES", 0)
    with pytest.raises(DocumentReadError) as exc:
        read_pdf(PDF_FIXTURE.read_bytes(), "form.pdf")
    assert exc.value.code == "TOO_MANY_PAGES"


def test_a_blank_form_is_recognised_but_has_no_employee_details():
    blank = mock_docs.build_pdf({}, "Roles and Responsibilities", [])
    x = extract_onboarding_form(read_pdf(blank, "blank.pdf"), document_name="blank.pdf", sha256="0", size_bytes=1)
    assert x.metadata.template_recognised and "employee.employee_name" in x.metadata.missing_fields


def test_an_unrelated_pdf_is_not_recognised_as_the_form():
    letter = mock_docs.render_pdf([[(30.0, 800.0, "Dear SYNTHETIC reader,"),
                                    (30.0, 780.0, "This letter is not an onboarding form.")]])
    x = extract_onboarding_form(read_pdf(letter, "letter.pdf"), document_name="letter.pdf", sha256="0", size_bytes=1)
    assert not x.metadata.template_recognised
    assert x.employee.employee_name.status.value == "MISSING"  # nothing invented


def test_without_pypdf_a_pdf_is_rejected_and_docx_still_works(monkeypatch):
    real_import = builtins.__import__

    def no_pypdf(name, *args, **kwargs):
        if name == "pypdf" or name.startswith("pypdf."):
            raise ImportError("pypdf not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pypdf)
    with pytest.raises(DocumentReadError) as exc:
        read_pdf(PDF_FIXTURE.read_bytes(), "form.pdf")
    assert exc.value.code == "PDF_SUPPORT_UNAVAILABLE"
    assert read_document((ROOT / "data/mock/documents/SYNTHETIC_onboarding_complete.docx").read_bytes(), "f.docx").tables
