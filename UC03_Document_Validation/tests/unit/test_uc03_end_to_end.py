"""UC-03 end to end: deterministic validation + shared LLM gateway (advisory) + Supabase-backed
stores (strict fake tables).

Offline only. The shared gateway is either the shared FakeLLM (from build_llm in offline mode) or
the REAL LiteLLMGateway
class with only its HTTP call replaced; the database is tests/unit/fake_tables.py. These tests do
NOT prove real provider
or real database connectivity.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    os.environ.get("HR_ONBOARDING_COMMON_SRC"),
    str(ROOT.parent / "HR-Onboarding-Common" / "src"),
):
    if (
        candidate
        and (Path(candidate) / "hr_onboarding_common" / "llm").is_dir()
        and candidate not in sys.path
    ):
        sys.path.insert(0, candidate)  # prefer this Common over any installed (possibly stale) copy
        break
llm_pkg = pytest.importorskip("hr_onboarding_common.llm")
if not hasattr(llm_pkg, "build_llm"):
    pytest.skip("this Common has no shared factory (build_llm)", allow_module_level=True)

from hr_onboarding_common.config import Settings  # noqa: E402
from hr_onboarding_common.llm import LiteLLMGateway, LLMError, ModelTier, build_llm  # noqa: E402

from integrations.gateway import DRAFT_TASK, NAME_TASK, GatewayAdvisor, offline_llm  # noqa: E402
from integrations.supabase import StorageError, build_stores  # noqa: E402
from src.state.enums import ActorType, RequiredAction, SubmissionStatus, ValidationFinding  # noqa: E402
from src.sub_agent.checklist import ChecklistService  # noqa: E402
from src.sub_agent.extraction import MockDocumentExtractor  # noqa: E402
from src.sub_agent.models import DocumentValidationRequest  # noqa: E402
from src.sub_agent.validator import DocumentValidationService  # noqa: E402
from tests.unit.fake_tables import FakeTables  # noqa: E402

TODAY = date(2026, 11, 4)
CLIENT = "client-e2e"
CASE = str(uuid.UUID(int=99))
FAKE_KEY = "sk-or-v1-FAKE-TEST-KEY-0000"
AUTHORITATIVE = (
    "document_status",
    "validation_findings",
    "outstanding_requirements",
    "evidence_status",
    "hr_decision",
    "compliance_item_status",
    "visa_pack_status",
    "required_next_action",
    "approval_status",
    "injection_detected",
)


def _req(**overrides) -> DocumentValidationRequest:
    data = dict(
        case_id=CASE,
        employee_id="emp-e2e",
        employee_name_form="Ahmed Al Rashidi",
        offer_accepted=True,
        onboarding_form_approved=True,
        document_type="passport",
        submission_status=SubmissionStatus.RECEIVED,
        extracted_name="Ahmed Al Rashidi",
        expiry_date=TODAY + timedelta(days=900),
        readable=True,
        required_fields_present=True,
        extraction_confidence=0.95,
        evidence_stored=False,
        client_id=CLIENT,
        document_ref="docs/p.pdf",
        requesting_actor_id="hr-1",
        requesting_actor_type=ActorType.HR_USER,
        validation_date=TODAY,
    )
    data.update(overrides)
    return DocumentValidationRequest(**data)


def _world(advisor=None):
    t = FakeTables()
    t.seed("client_configs", client_id=CLIENT)
    t.seed("onboarding_cases", case_id=CASE, client_id=CLIENT)
    stores = build_stores({"UC03_STORE_MODE": "supabase"}, tables=t)
    stores.document_registry.register(
        case_ref=CASE,
        client_id=CLIENT,
        document_type="passport",
        document_ref="docs/p.pdf",
        file_name="p.pdf",
        file_size_bytes=100,
        mime_type="application/pdf",
        checksum_sha256="d" * 64,
    )
    svc = DocumentValidationService(
        case_store=stores.case_store,
        audit_store=stores.audit_store,
        checklist_service=ChecklistService("default"),
        extractor=MockDocumentExtractor(),
        advisor=advisor,
        result_store=stores.result_store,
    )
    return t, svc


def _decision(result):
    d = result.model_dump(mode="json")
    return {k: d[k] for k in AUTHORITATIVE}


def _shared_offline_advisor():
    return GatewayAdvisor(
        build_llm(Settings(), offline=offline_llm, environ={"LLM_MODE": "offline"})
    )


SCENARIOS = {
    "valid": dict(),
    "invalid_expired": dict(expiry_date=TODAY - timedelta(days=1)),
    "missing": dict(submission_status=SubmissionStatus.MISSING),
    "low_confidence": dict(extraction_confidence=0.5),
    "name_variation": dict(extracted_name="Ahmed Al Rashidi Khalid"),
    "unreadable": dict(readable=False),
}


# ------------------------------------------------------------------ scenarios: LLM + database
# both available
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_each_scenario_is_persisted_and_identical_with_and_without_the_gateway(name):
    t_plain, plain = _world()
    t_llm, with_llm = _world(_shared_offline_advisor())
    a = await plain.validate(_req(**SCENARIOS[name]))
    b = await with_llm.validate(_req(**SCENARIOS[name]))
    assert _decision(a) == _decision(b)  # the model never changes a decision
    assert a.persistence_status == b.persistence_status == "PERSISTED"
    row_plain, row_llm = (
        t_plain.rows["document_validations"][0],
        t_llm.rows["document_validations"][0],
    )
    for col in (
        "validation_findings",
        "required_next_action",
        "document_status",
        "uc03_compliance_status",
        "hr_review_status",
    ):
        assert row_plain[col] == row_llm[col], col
    assert (
        row_plain["advisory"] is None
        and row_llm["advisory"]["advisor_provider"] == "shared-gateway"
    )


async def test_a_name_variation_is_an_hr_referral_with_a_separate_advisory_note():
    t, svc = _world(_shared_offline_advisor())
    result = await svc.validate(_req(extracted_name="Ahmed Al Rashidi Khalid"))
    assert (
        ValidationFinding.NAME_MISMATCH in result.validation_findings
        and result.required_next_action == RequiredAction.HR_REVIEW
    )
    row = t.rows["document_validations"][0]
    assert (
        row["required_next_action"] == "HR_REVIEW"
        and row["advisory"]["name_check_model_note"] is True
    )
    assert "Ahmed" not in json.dumps(row["advisory"]) and "Khalid" not in json.dumps(
        row["advisory"]
    )


# ------------------------------------------------------------------ the REAL shared gateway
# class, only HTTP replaced
@pytest.fixture
def online_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HR_ONBOARDING_ENV_FILE", str(tmp_path / "none.env"))
    for name in (
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "LLM_PROVIDER",
        "OPENROUTER_MODEL",
        "LLM_REASONING_ENABLED",
        "LLM_FALLBACKS_FRONTIER",
        "LLM_FALLBACKS_WORKHORSE",
        "LLM_FALLBACKS_FAST",
    ):
        monkeypatch.delenv(name, raising=False)
    env = {
        "LLM_MODE": "online",
        "OPENROUTER_API_KEY": FAKE_KEY,
        "LLM_MODEL_FRONTIER": "acme/frontier-1",
        "LLM_MODEL_WORKHORSE": "acme/workhorse-1",
        "LLM_MODEL_FAST": "acme/fast-1",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


async def test_the_real_shared_gateway_routes_uc03_tasks_to_the_configured_tiers(online_env):
    seen = []

    def completion(**kwargs):
        seen.append(kwargs)
        schema = kwargs["response_format"]
        content = (
            {"body": "Dear [EMPLOYEE_NAME], please resend the document."}
            if "body" in schema.model_fields
            else {"level": "uncertain", "note": "one extra name part"}
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content=json.dumps(content))
                )
            ],
            usage=None,
        )

    assert (
        type(build_llm(Settings(), environ=online_env)).__name__ == "LiteLLMGateway"
    )  # online mode, validated configuration
    gateway = LiteLLMGateway(Settings(), completion_fn=completion, sleep=lambda s: None)
    t, svc = _world(GatewayAdvisor(gateway))
    # Unreadable passport (first submission) -> a replacement request is drafted -> the draft task.
    drafted = await svc.validate(_req(readable=False))
    # A second requested document type (a re-sent passport would be a duplicate) with a name
    # variation -> the name task.
    t.insert(
        "candidate_documents",
        {
            "case_id": CASE,
            "client_id": CLIENT,
            "document_type": "EDUCATIONAL_CERTIFICATE",
            "document_ref": "docs/d.pdf",
            "file_name": "d.pdf",
            "file_size_bytes": 100,
            "mime_type": "application/pdf",
            "checksum_sha256": "e" * 64,
        },
    )
    named = await svc.validate(
        _req(
            document_type="degree_certificate",
            document_ref="docs/d.pdf",
            extracted_name="Ahmed Al Rashidi Khalid",
            attestation_confirmed=True,
        )
    )
    routes = {c.task: (c.tier, c.model) for c in gateway.calls}
    assert routes[NAME_TASK][0] == ModelTier.FAST and routes[DRAFT_TASK][0] == ModelTier.WORKHORSE
    assert {k["model"] for k in seen} == {"openrouter/acme/fast-1", "openrouter/acme/workhorse-1"}
    sent = json.dumps([k["messages"] for k in seen])
    assert (
        "Ahmed" not in sent
        and "Rashidi" not in sent
        and "docs/p.pdf" not in sent
        and FAKE_KEY not in sent
    )
    assert (
        named.required_next_action == RequiredAction.HR_REVIEW
    )  # the note never cleared the variation
    assert "Ahmed Al Rashidi" in (
        drafted.draft_communication or ""
    )  # the real name is inserted locally, after the call
    advisories = [r["advisory"] for r in t.rows["document_validations"]]
    assert any(a and a.get("communication_source") == "advisor" for a in advisories)


# ------------------------------------------------------------------ failure combinations
async def test_llm_unavailable_keeps_and_persists_the_deterministic_result():
    class Down:
        provider = "shared-gateway"

        def draft_message(self, **kwargs):
            raise LLMError("provider down")

        def name_concern(self, **kwargs):
            raise LLMError("provider down")

    t_ref, reference = _world()
    t, svc = _world(Down())
    expected = await reference.validate(
        _req(extracted_name="Ahmed Al Rashidi Khalid", readable=False)
    )
    result = await svc.validate(_req(extracted_name="Ahmed Al Rashidi Khalid", readable=False))
    assert _decision(result) == _decision(expected) and result.persistence_status == "PERSISTED"
    assert (
        t.rows["document_validations"][0]["advisory"]["communication_degraded_reason"] == "LLMError"
    )


async def test_database_unavailable_fails_loudly_even_when_the_llm_works():
    t, svc = _world(_shared_offline_advisor())
    t.fail.add(("insert", "document_validations"))
    with pytest.raises(StorageError):
        await svc.validate(_req())
    assert t.rows["document_validations"] == []


async def test_repeated_ingestion_of_the_same_request_is_stored_once():
    t, svc = _world(_shared_offline_advisor())
    first = await svc.validate(_req(submission_status=SubmissionStatus.UNDER_REVIEW))
    again = await svc.validate(_req(submission_status=SubmissionStatus.UNDER_REVIEW))
    assert first.persistence_status == again.persistence_status == "PERSISTED"
    assert len(t.rows["document_validations"]) == 1


def test_the_service_role_key_and_database_settings_never_reach_the_model(online_env, monkeypatch):
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-FAKE-0000")
    seen: list[str] = []
    llm = offline_llm()
    inner = llm.handlers[DRAFT_TASK]
    llm.handlers[DRAFT_TASK] = lambda system, user, schema: (
        seen.append(system + user),
        inner(system, user, schema),
    )[1]
    GatewayAdvisor(llm).draft_message(
        action="SEND_REMINDER", document_items=["Passport"], findings=["Missing Document"]
    )
    assert seen and all(
        "service-role" not in s and "SUPABASE" not in s for s in seen
    )  # system prompt AND user input checked
