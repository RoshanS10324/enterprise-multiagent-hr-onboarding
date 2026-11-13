"""Onboarding-document intake: reader, extraction, validation, case creation,
advisory model and API. In-memory only; no network, no paid model."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from sub_agent.documents.docx_reader import DocumentReadError, read_docx
from sub_agent.documents.models import DocumentStatus, FieldStatus
from sub_agent.documents.service import (
    DocumentIntakeContext,
    DocumentIntakeService,
    DocumentIntakeStatus,
    safe_extraction_view,
)
from sub_agent.documents.validation import validate_extraction
from sub_agent.llm import FakeLlm, load_all_tasks

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("mock_docs", ROOT / "scripts" / "mock_onboarding_documents.py")
mock_docs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mock_docs)

TODAY = mock_docs.RUN_TODAY
SALARY_TEXT = "SYNTHETIC: AED 18,500 per month"


def _scenario(name):
    return next(s for s in mock_docs.SCENARIOS if s["name"] == name)


def _doc(name) -> bytes:
    return mock_docs.scenario_document(_scenario(name))


def _service(llm=None, deps=None):
    from factories import make_deps

    deps = deps or make_deps()
    return DocumentIntakeService(deps, llm=llm, today=lambda: TODAY), deps


def _extract(name):
    service, _ = _service()
    return service.extract(_doc(name), "x.docx")


def _ctx(name, **over):
    return DocumentIntakeContext(**{**_scenario(name)["context"], **over})


# ---------------------------------------------------------------- reader


def test_reader_keeps_tables_and_paragraphs_in_order():
    content = read_docx(_doc("complete_employee"), "a.docx")
    assert len(content.tables) == 1
    labels = [row[0][0] for row in content.tables[0].rows[1:]]
    assert labels[1] == "Name of Employee" and labels[-1].startswith("Any other promises")
    assert content.paragraphs[0].text == "Roles and Responsibilities"


@pytest.mark.parametrize("data,filename,code", [
    (b"", "a.docx", "EMPTY_FILE"),
    (b"plain text, not a zip", "a.docx", "NOT_A_DOCX"),
    (b"%PDF-1.7", "a.pdf", "UNSUPPORTED_FORMAT"),
])
def test_reader_rejects_bad_input(data, filename, code):
    with pytest.raises(DocumentReadError) as exc:
        read_docx(data, filename)
    assert exc.value.code == code


def _zip(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return buf.getvalue()


def test_reader_rejects_a_zip_without_a_document_part():
    with pytest.raises(DocumentReadError) as exc:
        read_docx(_zip({"other.xml": b"<x/>"}), "a.docx")
    assert exc.value.code == "NOT_A_DOCX"


def test_reader_refuses_a_dtd_entity_expansion_document():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    with pytest.raises(DocumentReadError) as exc:
        read_docx(_zip({"word/document.xml": bomb}), "a.docx")
    assert exc.value.code == "UNSAFE_XML"


def test_reader_refuses_oversized_files(monkeypatch):
    from sub_agent.documents import docx_reader

    monkeypatch.setattr(docx_reader, "MAX_FILE_BYTES", 100)
    with pytest.raises(DocumentReadError) as exc:
        read_docx(_doc("complete_employee"), "a.docx")
    assert exc.value.code == "FILE_TOO_LARGE"


# ---------------------------------------------------------------- extraction


def test_complete_document_extracts_every_section():
    x = _extract("complete_employee")
    assert x.metadata.template_recognised
    assert x.employee.employee_name.value == "Aisha Synthetic"
    assert x.employee.designation.value == "Synthetic Data Engineer"
    assert x.employee.joining_date.value == date(2027, 1, 11)
    assert x.employee.probation_months.value == 6
    assert isinstance(x.employee.final_salary.value, SecretStr)
    assert x.employee.final_salary.value.get_secret_value() == SALARY_TEXT
    assert x.employment.visa_duration_years.value == 2
    assert x.employment.visa_location_category.value == "OUTSIDE_COUNTRY"
    assert x.documents.passport.value == DocumentStatus.RECEIVED
    assert x.personal.marital_status.value == "SINGLE"
    assert isinstance(x.personal.mobile_number.value, SecretStr)
    assert x.benefits.hotel_stay_days.value == 7
    assert x.insurance.plan.value == "WN"
    assert x.roles.responsibilities == mock_docs._RR_SPECIFIC and not x.roles.is_example
    assert x.employee.final_salary.source.startswith("table[0].row[")


def test_template_charges_are_reference_data_never_salary():
    x = _extract("missing_salary_and_joining_date")
    assert x.employee.final_salary.status == FieldStatus.MISSING  # the AED price lines did not fill it
    texts = " ".join(c.text for c in x.reference_charges)
    assert "4700" in texts and "380" in texts and "1000" in texts
    assert {c.section for c in x.reference_charges} == {
        "employment.visa", "employment.visit_visa_entry_permit", "insurance.plan"}


def test_a_blank_template_extracts_nothing_and_invents_nothing():
    blank = mock_docs.build_docx({}, mock_docs._RR_EXAMPLE_HEADING, mock_docs._RR_SPECIFIC)
    service, _ = _service()
    x = service.extract(blank, "blank.docx")
    present = [k for k, f in x.fields().items() if f.status == FieldStatus.PRESENT]
    assert present == ["offer.issuing_entity"]  # printed by the template itself
    assert x.roles.is_example  # "eg given below": an example, not confirmed duties
    assert x.employee.final_salary.value is None and x.employee.joining_date.value is None


def test_two_different_values_in_one_row_are_uncertain_not_picked():
    x = _extract("conflicting_fields")
    assert x.employee.joining_date.status == FieldStatus.UNCERTAIN
    assert x.employee.joining_date.value is None
    assert "2 different values" in x.employee.joining_date.note


def test_an_ambiguous_date_is_uncertain():
    values = {**_scenario("complete_employee")["values"], "DOJ": "05/06/2027"}
    service, _ = _service()
    x = service.extract(mock_docs.build_docx(values, "Roles and Responsibilities", ["x"]), "d.docx")
    assert x.employee.joining_date.status == FieldStatus.UNCERTAIN
    assert x.employee.joining_date.value == date(2027, 6, 5)  # day-first, flagged for a person


def test_probation_outside_policy_is_invalid():
    assert _extract("conflicting_fields").employee.probation_months.status == FieldStatus.INVALID


def test_missing_is_distinguished_from_invalid():
    x = _extract("missing_employee_details")
    assert x.employee.employee_name.status == FieldStatus.MISSING
    values = {**_scenario("complete_employee")["values"], "Final Salary": "to be confirmed"}
    service, _ = _service()
    bad = service.extract(mock_docs.build_docx(values, "Roles and Responsibilities", ["x"]), "s.docx")
    assert bad.employee.final_salary.status == FieldStatus.INVALID


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("scenario,error", [
    ("missing_employee_details", "missing:employee.employee_name"),
    ("missing_salary_and_joining_date", "missing:employee.final_salary"),
    ("missing_salary_and_joining_date", "missing:employee.joining_date"),
])
def test_mandatory_fields_fail_validation(scenario, error):
    out = validate_extraction(_extract(scenario), candidate_email="a@example.invalid",
                              work_location_type="OFFICE", today=TODAY)
    assert error in out.errors


def test_missing_designation_fails_validation():
    values = {k: v for k, v in _scenario("complete_employee")["values"].items() if k != "Designation"}
    service, _ = _service()
    x = service.extract(mock_docs.build_docx(values, "Roles and Responsibilities", ["x"]), "d.docx")
    out = validate_extraction(x, candidate_email="a@example.invalid", work_location_type="OFFICE", today=TODAY)
    assert "missing:employee.designation" in out.errors


def test_what_the_document_never_contains_must_come_from_the_caller():
    out = validate_extraction(_extract("complete_employee"), candidate_email=None, work_location_type=None,
                              today=TODAY)
    assert {"missing:context.candidate_email", "missing:context.work_location_type"} <= set(out.errors)


def test_outstanding_documents_go_to_review():
    out = validate_extraction(_extract("missing_required_documents"), candidate_email="a@example.invalid",
                              work_location_type="OFFICE", today=TODAY)
    assert out.ok and "documents_outstanding" in out.review_reasons
    assert {"passport", "passport_cover", "photograph", "degree_certificate"} <= set(out.outstanding_documents)


def test_inside_country_visa_also_requires_visa_documents():
    values = {**_scenario("complete_employee")["values"], "1 year or 2 years visa": "1 year - inside country"}
    service, _ = _service()
    x = service.extract(mock_docs.build_docx(values, "Roles and Responsibilities", ["x"]), "v.docx")
    out = validate_extraction(x, candidate_email="a@example.invalid", work_location_type="OFFICE", today=TODAY)
    assert {"visa_copy", "cancellation_paper", "emirates_id"} <= set(out.outstanding_documents)


def test_a_past_joining_date_goes_to_review():
    out = validate_extraction(_extract("complete_employee"), candidate_email="a@example.invalid",
                              work_location_type="OFFICE", today=date(2027, 6, 1))
    assert "joining_date_in_past" in out.review_reasons


# ---------------------------------------------------------------- case creation


def test_every_synthetic_scenario_reaches_its_expected_status():
    report = mock_docs.run_scenarios()
    assert [r["scenario"] for r in report if not r["ok"]] == []


def test_a_complete_document_creates_one_case_with_an_employee_number():
    service, deps = _service()
    result = service.process(_doc("complete_employee"), "c.docx", _ctx("complete_employee"))
    assert result.status == DocumentIntakeStatus.CREATED
    assert result.employee_no and result.case_id
    case = deps.case_repo.get(result.case_id)
    assert case.candidate_name == "Aisha Synthetic" and case.location_name == "SYN Dubai Office"
    assert deps.form_repo.latest_approved(result.case_id).salary_and_terms.get_secret_value() == SALARY_TEXT
    assert result.source_document_stored is False


def test_extraction_never_approves_the_form():
    """A fully extracted document with no approval attestation is HELD by the start gate."""
    service, deps = _service()
    result = service.process(_doc("complete_employee"), "c.docx",
                             _ctx("complete_employee", form_status="DRAFT", approved_by=None, approved_at=None))
    assert result.status == DocumentIntakeStatus.HELD and "form not approved" in result.reasons
    assert deps.case_repo.list_for_client(mock_docs.CLIENT_ID) == []


def test_offer_not_accepted_is_held():
    service, _ = _service()
    result = service.process(_doc("complete_employee"), "c.docx", _ctx("complete_employee", offer_accepted=False))
    assert result.status == DocumentIntakeStatus.HELD and "offer not accepted" in result.reasons


def test_review_is_cleared_only_by_an_hr_user_and_is_audited():
    service, deps = _service()
    held = service.process(_doc("missing_required_documents"), "m.docx", _ctx("missing_required_documents"))
    assert held.status == DocumentIntakeStatus.NEEDS_REVIEW
    confirmed = service.process(_doc("missing_required_documents"), "m.docx",
                                _ctx("missing_required_documents", review_confirmed_by="SYN-HR-MANAGER-01"))
    assert confirmed.status == DocumentIntakeStatus.CREATED
    entry = next(e for e in deps.audit_log.entries() if e.action == "DOCUMENT_REVIEW_CONFIRMED")
    assert entry.actor_id == "SYN-HR-MANAGER-01" and "documents_outstanding" in entry.after["review_reasons"]


def test_employee_numbers_are_unique_and_duplicates_link():
    service, deps = _service()
    first = service.process(_doc("complete_employee"), "c.docx", _ctx("complete_employee"))
    second = service.process(_doc("complete_but_no_placement"), "n.docx",
                             _ctx("complete_but_no_placement", placement={"location_name": "SYN Dubai Office"}))
    again = service.process(_doc("complete_employee"), "c.docx", _ctx("complete_employee"))
    assert first.employee_no != second.employee_no
    assert again.status == DocumentIntakeStatus.LINKED_EXISTING and again.existing_case_id == first.case_id
    assert len(deps.case_repo.list_for_client(mock_docs.CLIENT_ID)) == 2


def test_a_rehire_document_opens_a_case_with_a_variant_approval():
    from sub_agent.models import PersonRecord, PersonRecordKind

    service, deps = _service()
    deps.person_directory.add(PersonRecord(client_id=mock_docs.CLIENT_ID, email="aisha.synthetic@example.invalid",
                                           full_name="Aisha Synthetic", kind=PersonRecordKind.PAST_EMPLOYEE))
    result = service.process(_doc("complete_employee"), "c.docx", _ctx("complete_employee"))
    assert result.status == DocumentIntakeStatus.CREATED and len(result.approvals_created) == 1
    assert deps.case_repo.get(result.case_id).variants.is_rehire


def test_a_storage_failure_is_never_reported_as_success():
    from factories import make_deps

    from sub_agent.adapters.supabase.unsupported import UnsupportedCaseRepository

    deps = make_deps()
    deps.case_repo = UnsupportedCaseRepository()
    service, _ = _service(deps=deps)
    result = service.process(_doc("complete_employee"), "c.docx", _ctx("complete_employee"))
    assert result.status == DocumentIntakeStatus.PERSISTENCE_FAILED
    assert result.case_id is None and result.employee_no is None


def test_audit_entries_carry_no_extracted_values_or_file_name():
    service, deps = _service()
    service.process(_doc("complete_employee"), "Aisha Synthetic offer.docx", _ctx("complete_employee"))
    dumped = json.dumps([e.model_dump(mode="json") for e in deps.audit_log.entries()
                         if e.action.startswith("DOCUMENT_")])
    for secret in ("Aisha", "18,500", "SYNTHETIC-MOBILE-01", "SYNTHETIC-FATHER", "aisha.synthetic@"):
        assert secret not in dumped


def test_the_safe_view_withholds_sensitive_values():
    view = json.dumps(safe_extraction_view(_extract("complete_employee")), default=str)
    for secret in ("18,500", "SYNTHETIC-MOBILE-01", "SYNTHETIC-FATHER-NAME-01", "SYNTHETIC-MOTHER-NAME-01"):
        assert secret not in view
    fields = safe_extraction_view(_extract("complete_employee"))["fields"]
    assert fields["employee.final_salary"]["status"] == "PRESENT" and "value" not in fields["employee.final_salary"]


# ---------------------------------------------------------------- advisory model


def test_offline_model_passes_specific_duties_and_flags_example_text():
    llm = FakeLlm(load_all_tasks())
    service, deps = _service(llm=llm)
    ok = service.process(_doc("complete_employee"), "c.docx", _ctx("complete_employee"))
    assert ok.status == DocumentIntakeStatus.CREATED
    values = _scenario("complete_employee")["values"]
    generic = mock_docs.build_docx(values, "Roles and Responsibilities", ["Example duties, e.g. as required."])
    flagged = service.process(generic, "g.docx", _ctx("complete_employee", candidate_email="g@example.invalid"))
    assert flagged.status == DocumentIntakeStatus.NEEDS_REVIEW
    assert "model_flagged_responsibilities" in flagged.review_reasons


def test_the_model_never_sees_salary_names_or_contact_details():
    hr_llm = pytest.importorskip("hr_onboarding_common.llm")
    from sub_agent.llm import GatewayLlm, load_persona

    seen = {}

    def handler(system, user, schema):
        seen["user"] = user
        return schema(label="MATCHES_DESIGNATION", confidence=0.9)

    gw = hr_llm.FakeLLM().on("uc01.responsibilities_review", handler)
    service, _ = _service(llm=GatewayLlm(gw, load_all_tasks(), load_persona()))
    result = service.process(_doc("complete_employee"), "c.docx", _ctx("complete_employee"))
    assert result.status == DocumentIntakeStatus.CREATED
    for secret in ("Aisha", "18,500", "SYNTHETIC-MOBILE", "SYNTHETIC-FATHER", "aisha.synthetic@", "SYN Head"):
        assert secret not in seen["user"]
    assert "Synthetic Data Engineer" in seen["user"]


def _stand_in_gateway(completion):
    hr_llm = pytest.importorskip("hr_onboarding_common.llm")
    from hr_onboarding_common.config import get_settings

    from sub_agent.llm import GatewayLlm, load_persona

    gw = hr_llm.LiteLLMGateway(get_settings(), callbacks=[], completion_fn=completion, sleep=lambda _s: None)
    return GatewayLlm(gw, load_all_tasks(), load_persona())


def _reply(content, finish_reason="stop"):
    def completion(**_kw):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
                                                        message=SimpleNamespace(content=content))], usage=None)
    return completion


def test_online_stand_in_model_can_escalate_to_review():
    llm = _stand_in_gateway(_reply(json.dumps({"label": "LOOKS_GENERIC_OR_EXAMPLE", "confidence": 0.7})))
    service, _ = _service(llm=llm)
    result = service.process(_doc("complete_employee"), "c.docx", _ctx("complete_employee"))
    assert result.status == DocumentIntakeStatus.NEEDS_REVIEW
    assert result.review_reasons == ["model_flagged_responsibilities"]


@pytest.mark.parametrize("completion,action", [
    (_reply("", finish_reason="content_filter"), "LLM_MODEL_REFUSED"),
    (_reply("not json"), "LLM_UNAVAILABLE"),
    (_reply(json.dumps({"label": "APPROVE_EMPLOYEE", "confidence": 1.0})), "LLM_UNAVAILABLE"),
])
def test_model_failures_leave_the_deterministic_outcome_unchanged(completion, action):
    service, deps = _service(llm=_stand_in_gateway(completion))
    result = service.process(_doc("complete_employee"), "c.docx", _ctx("complete_employee"))
    assert result.status == DocumentIntakeStatus.CREATED
    assert action in [e.action for e in deps.audit_log.entries()]


def test_provider_error_and_timeout_leave_the_outcome_unchanged():
    Timeout = type("Timeout", (Exception,), {})
    for exc in (RuntimeError("provider 500"), Timeout("timed out")):
        def completion(**_kw):
            raise exc
        service, deps = _service(llm=_stand_in_gateway(completion))
        assert service.process(_doc("complete_employee"), "c.docx",
                               _ctx("complete_employee")).status == DocumentIntakeStatus.CREATED
        assert "LLM_UNAVAILABLE" in [e.action for e in deps.audit_log.entries()]


def test_personal_numbers_in_responsibilities_are_refused_before_the_model():
    hr_llm = pytest.importorskip("hr_onboarding_common.llm")
    from sub_agent.llm import GatewayLlm, load_persona

    gw = hr_llm.FakeLLM().on("uc01.responsibilities_review", lambda *a: pytest.fail("must not be called"))
    service, deps = _service(llm=GatewayLlm(gw, load_all_tasks(), load_persona()))
    values = _scenario("complete_employee")["values"]
    doc = mock_docs.build_docx(values, "Roles and Responsibilities", ["Call the manager on +971 50 000 0000 daily."])
    result = service.process(doc, "p.docx", _ctx("complete_employee"))
    assert gw.calls == []
    assert result.status == DocumentIntakeStatus.CREATED  # deterministic path unchanged
    assert "LLM_INPUT_REFUSED" in [e.action for e in deps.audit_log.entries()]


# ---------------------------------------------------------------- API


def _api():
    from fastapi.testclient import TestClient

    from sub_agent.api.app import create_app
    from sub_agent.api.container import build_container
    from sub_agent.api.settings import Settings

    container = build_container(Settings(app_env="dev", dedup_hmac_key="k", system_api_key="doc-test-key"))
    return TestClient(create_app(container)), container


def _body(name, **over):
    ctx = dict(_scenario(name)["context"])
    placement = ctx.pop("placement") or {}
    body = {"filename": "SYNTHETIC.docx", "content_base64": base64.b64encode(_doc(name)).decode(),
            **ctx, **{k: v for k, v in placement.items()}}
    body.update(over)
    return body


def test_api_creates_a_case_and_withholds_sensitive_values():
    client, container = _api()
    response = client.post("/internal/onboarding-documents", json=_body("complete_employee"),
                           headers={"X-System-Key": "doc-test-key"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "CREATED" and body["employee_no"]
    assert body["source_document_stored"] is False
    for secret in ("18,500", "SYNTHETIC-MOBILE-01", "SYNTHETIC-FATHER-NAME-01"):
        assert secret not in response.text


def test_api_requires_the_system_key():
    client, _ = _api()
    assert client.post("/internal/onboarding-documents", json=_body("complete_employee")).status_code == 401


def test_api_rejects_invalid_base64_and_unsupported_files():
    client, _ = _api()
    headers = {"X-System-Key": "doc-test-key"}
    bad = client.post("/internal/onboarding-documents", json=_body("complete_employee", content_base64="@@@"),
                      headers=headers)
    assert bad.status_code == 422
    # .pdf is supported now (text-based PDFs); a .docx renamed to .pdf is still refused.
    pdf = client.post("/internal/onboarding-documents", json=_body("complete_employee", filename="form.pdf"),
                      headers=headers)
    assert pdf.json()["status"] == "REJECTED" and pdf.json()["rejection_code"] == "NOT_A_PDF"
    txt = client.post("/internal/onboarding-documents", json=_body("complete_employee", filename="form.txt"),
                      headers=headers)
    assert txt.json()["status"] == "REJECTED" and txt.json()["rejection_code"] == "UNSUPPORTED_FORMAT"


def test_api_holds_without_placement():
    client, _ = _api()
    body = _body("complete_but_no_placement")
    response = client.post("/internal/onboarding-documents", json=body, headers={"X-System-Key": "doc-test-key"})
    assert response.json()["status"] == "HELD" and "location_name_required" in response.json()["reasons"]
