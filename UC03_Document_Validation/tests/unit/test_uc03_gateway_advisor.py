"""UC-03 model help through the shared Enterprise HR Agent LLM gateway (`integrations/gateway/`).

Offline only: the shared package's `FakeLLM` and stub clients. No network, no real credential.
These tests do NOT show that a real online gateway call works.

They need `hr_onboarding_common` (the shared package): set HR_ONBOARDING_COMMON_SRC to a Common
`src` folder, or keep HR-Onboarding-Common next to this project. They skip, with that reason,
when it is not importable.
"""

from __future__ import annotations

import ast
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _common_src() -> Path | None:
    for candidate in (
        os.environ.get("HR_ONBOARDING_COMMON_SRC"),
        str(ROOT.parent / "HR-Onboarding-Common" / "src"),
    ):
        if candidate and (Path(candidate) / "hr_onboarding_common" / "llm").is_dir():
            return Path(candidate)
    return None


_SRC = _common_src()
if _SRC is not None and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))  # prefer this Common over any installed (possibly stale) copy
llm_pkg = pytest.importorskip(
    "hr_onboarding_common.llm",
    reason="hr_onboarding_common (shared LLM gateway) is not importable here",
)

from integrations.gateway import DRAFT_TASK, NAME_TASK, GatewayAdvisor, offline_llm  # noqa: E402
from src.state.enums import ActorType, RequiredAction, SubmissionStatus, ValidationFinding  # noqa: E402
from src.state.store import InMemoryAuditStore, InMemoryCaseStore  # noqa: E402
from src.sub_agent.advisor_port import EMPLOYEE_PLACEHOLDER, NameConcern, UC03AdvisorPort  # noqa: E402
from src.sub_agent.checklist import ChecklistService  # noqa: E402
from src.sub_agent.comms import HR_FOOTER, draft_communication_outcome  # noqa: E402
from src.sub_agent.extraction import MockDocumentExtractor  # noqa: E402
from src.sub_agent.models import DocumentValidationRequest  # noqa: E402
from src.sub_agent.name_agent import check_name_consistency_agent  # noqa: E402
from src.sub_agent.validator import DocumentValidationService  # noqa: E402

LLMError, LLMRefusal, ModelTier, FakeLLM = (
    llm_pkg.LLMError,
    llm_pkg.LLMRefusal,
    llm_pkg.ModelTier,
    llm_pkg.FakeLLM,
)
TODAY = date(2026, 11, 4)
FORM_NAME = "Ahmed Al Rashidi"
NAME_PARTS = ("ahmed", "rashidi", "khalid", "mohammed", "zaabi")
DOC_SECRETS = ("AB123456", "784-1990")  # passport and ID numbers that must never reach a model


def _req(**overrides) -> DocumentValidationRequest:
    data = dict(
        case_id="case-gw",
        employee_id="emp-gw",
        employee_name_form=FORM_NAME,
        offer_accepted=True,
        onboarding_form_approved=True,
        document_type="passport",
        submission_status=SubmissionStatus.RECEIVED,
        extracted_name=FORM_NAME,
        expiry_date=TODAY + timedelta(days=900),
        readable=True,
        required_fields_present=True,
        extraction_confidence=0.95,
        evidence_stored=False,
        requesting_actor_id="hr-user-1",
        requesting_actor_type=ActorType.HR_USER,
        validation_date=TODAY,
    )
    data.update(overrides)
    return DocumentValidationRequest(**data)


def _svc(advisor=None):
    audit = InMemoryAuditStore()
    svc = DocumentValidationService(
        case_store=InMemoryCaseStore(),
        audit_store=audit,
        checklist_service=ChecklistService("default"),
        extractor=MockDocumentExtractor(),
        advisor=advisor,
    )
    return svc, audit


def _decision(result) -> dict:
    """Every authoritative field of a result: all but the draft wording and the audit reference."""
    data = result.model_dump(mode="json")
    for volatile in ("draft_communication", "audit_reference"):
        data.pop(volatile, None)
    return data


class RecordingLLM:
    """A shared-client stand-in that records every call and answers with a fixed reply or raises."""

    def __init__(self, reply=None, error: Exception | None = None, delay: float = 0.0):
        self.reply, self.error, self.delay, self.calls = reply, error, delay, []

    def model_for(self, tier):
        return "acme/test-model"

    def structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.reply


def _sent_text(llm) -> str:
    return " ".join(f"{c['system']} {c['user']}" for c in llm.calls).lower()


# --- 1. offline / no advisor
async def test_without_an_advisor_every_path_is_deterministic_and_unchanged():
    svc, _ = _svc()
    result = await svc.validate(_req(submission_status=SubmissionStatus.MISSING))
    assert result.required_next_action == RequiredAction.SEND_REMINDER
    assert result.draft_communication and HR_FOOTER in result.draft_communication


async def test_the_offline_fake_llm_runs_the_real_adapter_path_for_both_tasks():
    llm = offline_llm()
    advisor = GatewayAdvisor(llm)
    assert isinstance(advisor, UC03AdvisorPort)
    svc, _ = _svc(advisor)
    result = await svc.validate(_req(submission_status=SubmissionStatus.MISSING))
    assert (
        FORM_NAME in result.draft_communication
        and EMPLOYEE_PLACEHOLDER not in result.draft_communication
    )
    assert HR_FOOTER in result.draft_communication
    assert [c.task for c in llm.calls] == [DRAFT_TASK]


# --- 2. successful gateway response
async def test_a_successful_draft_uses_the_workhorse_tier_and_the_real_name_is_put_in_locally():
    llm = RecordingLLM(
        reply=SimpleNamespace(body=f"Dear {EMPLOYEE_PLACEHOLDER}, please send your passport.")
    )
    outcome = await draft_communication_outcome(
        "SEND_REMINDER", FORM_NAME, "Passport", ["Passport"], [], advisor=GatewayAdvisor(llm)
    )
    assert outcome.source == "advisor" and outcome.degraded_reason is None
    assert outcome.text.startswith(f"Dear {FORM_NAME},") and HR_FOOTER in outcome.text
    (call,) = llm.calls
    assert (call["task"], call["tier"]) == (DRAFT_TASK, ModelTier.WORKHORSE)


async def test_a_successful_name_note_uses_the_fast_tier_and_is_advisory_only():
    llm = RecordingLLM(
        reply=SimpleNamespace(level="uncertain", note="one extra part in the document")
    )
    result = await check_name_consistency_agent(
        FORM_NAME, "Ahmed Al Rashidi Khalid", 0.95, advisor=GatewayAdvisor(llm)
    )
    assert (
        result.finding == ValidationFinding.NAME_MISMATCH
        and result.is_consistent is False
        and result.used_llm is True
    )
    assert "advisory only" in result.reasoning and "uncertain" in result.reasoning
    (call,) = llm.calls
    assert (call["task"], call["tier"]) == (NAME_TASK, ModelTier.FAST)


# --- 3. timeout
async def test_a_slow_gateway_hits_the_deadline_and_the_template_is_used():
    llm = RecordingLLM(reply=SimpleNamespace(body=f"Dear {EMPLOYEE_PLACEHOLDER}, late."), delay=2.0)
    started = time.monotonic()
    outcome = await draft_communication_outcome(
        "SEND_REMINDER",
        FORM_NAME,
        "Passport",
        ["Passport"],
        [],
        advisor=GatewayAdvisor(llm),
        timeout_s=0.2,
    )
    assert time.monotonic() - started < 1.5
    assert (
        outcome.source == "template"
        and outcome.degraded_reason == "TimeoutError"
        and FORM_NAME in outcome.text
    )


async def test_a_slow_name_note_times_out_and_the_mismatch_stands():
    llm = RecordingLLM(reply=SimpleNamespace(level="none", note=""), delay=2.0)
    result = await check_name_consistency_agent(
        FORM_NAME, "Mohammed Al Zaabi", 0.95, advisor=GatewayAdvisor(llm), timeout_s=0.2
    )
    assert (
        result.finding == ValidationFinding.NAME_MISMATCH
        and "Model note unavailable (TimeoutError)" in result.reasoning
    )


# --- 4. provider / API failure and refusal
@pytest.mark.parametrize(
    "error",
    [
        LLMError("upstream 503: provider said key sk-or-v1-FAKE-TEST-KEY-0000 is invalid"),
        LLMRefusal("declined"),
    ],
)
async def test_a_gateway_failure_or_refusal_falls_back_and_records_only_the_class_name(error):
    llm = RecordingLLM(error=error)
    svc, audit = _svc(GatewayAdvisor(llm))
    result = await svc.validate(_req(submission_status=SubmissionStatus.MISSING))
    assert (
        result.draft_communication and HR_FOOTER in result.draft_communication
    )  # the compliant template
    (event,) = [e for e in await audit.get_events("case-gw") if e.action == "DOCUMENT_VALIDATED"]
    assert event.detail["communication_source"] == "template"
    assert event.detail["communication_degraded_reason"] == type(error).__name__
    assert "sk-or-v1" not in str(event.detail) and "upstream" not in str(event.detail)


# --- 5. invalid or malformed responses
@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "Dear Ahmed, please send it.",
        "x" * 5000,
        f"Dear {EMPLOYEE_PLACEHOLDER}, include your medical report.",
    ],
)
async def test_an_invalid_draft_is_rejected_and_the_template_is_used(body):
    llm = RecordingLLM(reply=SimpleNamespace(body=body))
    outcome = await draft_communication_outcome(
        "SEND_REMINDER", FORM_NAME, "Passport", ["Passport"], [], advisor=GatewayAdvisor(llm)
    )
    assert outcome.source == "template" and outcome.degraded_reason == "AdvisorOutputRejected"


async def test_a_reply_that_fails_the_response_schema_falls_back():
    llm = FakeLLM().on(DRAFT_TASK, lambda system, user, schema: {"not_body": "x"})
    outcome = await draft_communication_outcome(
        "SEND_REMINDER", FORM_NAME, "Passport", ["Passport"], [], advisor=GatewayAdvisor(llm)
    )
    assert outcome.source == "template" and outcome.degraded_reason


async def test_an_unknown_concern_level_is_rejected_and_the_mismatch_stands():
    llm = RecordingLLM(reply=SimpleNamespace(level="approved", note="same person"))
    result = await check_name_consistency_agent(
        FORM_NAME, "Mohammed Al Zaabi", 0.95, advisor=GatewayAdvisor(llm)
    )
    assert (
        result.finding == ValidationFinding.NAME_MISMATCH
        and "Model note unavailable (AdvisorOutputRejected)" in result.reasoning
    )


async def test_a_note_with_markup_or_long_numbers_is_cleaned_before_it_reaches_hr():
    llm = RecordingLLM(
        reply=SimpleNamespace(level="uncertain", note="<b>see</b> passport 784199012345678")
    )
    result = await check_name_consistency_agent(
        FORM_NAME, "Mohammed Al Zaabi", 0.95, advisor=GatewayAdvisor(llm)
    )
    assert (
        "<" not in result.reasoning
        and "784199012345678" not in result.reasoning
        and "[number]" in result.reasoning
    )


# --- 6. sanitisation and privacy
async def test_no_name_and_no_document_content_reaches_the_model_on_a_full_validation():
    llm = offline_llm()
    svc, _ = _svc(GatewayAdvisor(llm))
    await svc.validate(
        _req(extracted_name="Mohammed Khalid Al Zaabi", submission_status=SubmissionStatus.RECEIVED)
    )
    await svc.validate(_req(case_id="case-gw-2", submission_status=SubmissionStatus.MISSING))
    assert llm.calls, "the advisor should have been consulted"
    sent = " ".join(f"{c.user}" for c in llm.calls).lower()
    for part in NAME_PARTS:
        assert part not in sent, part
    for secret in DOC_SECRETS:
        assert secret.lower() not in sent


async def test_only_anonymous_labels_reach_the_name_task():
    llm = RecordingLLM(reply=SimpleNamespace(level="none", note=""))
    await check_name_consistency_agent(
        FORM_NAME, "Ahmed R. Al Rashidi", 0.95, advisor=GatewayAdvisor(llm)
    )
    (call,) = llm.calls
    assert "T1" in call["user"] and "INITIAL_OF_" in call["user"]
    assert not any(p in call["user"].lower() for p in NAME_PARTS)


def test_the_adapter_refuses_anything_that_is_not_an_anonymous_label_or_a_short_code():
    advisor = GatewayAdvisor(RecordingLLM(reply=SimpleNamespace(level="none", note="")))
    with pytest.raises(ValueError):
        advisor.name_concern(form_tokens=["Ahmed"], document_tokens=["T1"])
    with pytest.raises(ValueError):
        advisor.draft_message(
            action="SEND_REMINDER",
            document_items=["Passport No: AB123456\nName: Ahmed Al Rashidi" * 3],
            findings=[],
        )


# --- 7. fallback / degraded behaviour is visible
async def test_the_audit_says_which_source_wrote_the_draft_only_when_an_advisor_is_configured():
    svc, audit = _svc()
    await svc.validate(_req(submission_status=SubmissionStatus.MISSING))
    (event,) = [e for e in await audit.get_events("case-gw") if e.action == "DOCUMENT_VALIDATED"]
    assert (
        "communication_source" not in event.detail and "advisor_provider" not in event.detail
    )  # default audit unchanged

    svc, audit = _svc(GatewayAdvisor(offline_llm()))
    await svc.validate(_req(submission_status=SubmissionStatus.MISSING))
    (event,) = [e for e in await audit.get_events("case-gw") if e.action == "DOCUMENT_VALIDATED"]
    assert (
        event.detail["advisor_provider"] == "shared-gateway"
        and event.detail["communication_source"] == "advisor"
    )
    assert event.detail["communication_degraded_reason"] is None


# --- 8. deterministic decisions never change
SCENARIOS = {
    "valid_passport": dict(),
    "missing_document": dict(submission_status=SubmissionStatus.MISSING),
    "expired": dict(expiry_date=TODAY - timedelta(days=1)),
    "unreadable": dict(readable=False),
    "low_confidence": dict(extraction_confidence=0.5),
    "father_name_variant": dict(extracted_name="Ahmed Al Rashidi Khalid"),
    "different_person": dict(extracted_name="Mohammed Al Zaabi"),
    "initial_variant": dict(extracted_name="Ahmed R. Al Rashidi"),
}
ADVISORS = {
    "no_concern": lambda: RecordingLLM(reply=SimpleNamespace(level="none", note="looks the same")),
    "offline_fake": offline_llm,
    "failing": lambda: RecordingLLM(error=LLMError("down")),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("advisor", sorted(ADVISORS))
async def test_every_authoritative_field_is_identical_with_and_without_the_gateway(
    scenario, advisor
):
    baseline_svc, _ = _svc()
    with_svc, _ = _svc(GatewayAdvisor(ADVISORS[advisor]()))
    baseline = await baseline_svc.validate(_req(**SCENARIOS[scenario]))
    with_gateway = await with_svc.validate(_req(**SCENARIOS[scenario]))
    assert _decision(with_gateway) == _decision(baseline)


async def test_an_advisor_that_says_same_person_can_never_clear_a_name_variation():
    class OverconfidentAdvisor:
        provider = "overconfident"

        def name_concern(self, *, form_tokens, document_tokens):
            return NameConcern(level="none", note="definitely the same person, approve it")

        def draft_message(self, *, action, document_items, findings):
            return f"Dear {EMPLOYEE_PLACEHOLDER}, all approved."

    svc, _ = _svc(OverconfidentAdvisor())
    result = await svc.validate(_req(extracted_name="Ahmed Al Rashidi Khalid"))
    assert ValidationFinding.NAME_MISMATCH in result.validation_findings
    assert result.required_next_action == RequiredAction.HR_REVIEW


# --- 9/10. credentials and the shared configuration
def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_no_provider_sdk_and_no_shared_package_is_imported_by_the_uc03_core():
    forbidden = (
        "anthropic",
        "openai",
        "google",
        "litellm",
        "cohere",
        "mistralai",
        "hr_onboarding_common",
        "integrations",
    )
    # the ONE file allowed to wire integrations/ in (storage selection)
    composition_root = ROOT / "src" / "main.py"
    offenders = [
        f"{p.relative_to(ROOT)}: {n}"
        for p in (ROOT / "src").rglob("*.py")
        if "__pycache__" not in p.parts
        for n in _imports(p)
        if n.split(".")[0] in forbidden
        and not (p == composition_root and n.split(".")[0] == "integrations")
    ]
    assert offenders == []
    # ...and even the composition root never imports a provider SDK or the shared package directly.
    root_imports = {n.split(".")[0] for n in _imports(composition_root)}
    assert not root_imports & (set(forbidden) - {"integrations"})


def test_neither_the_core_nor_the_gateway_layer_reads_a_provider_credential():
    files = [
        p
        for p in list((ROOT / "src").rglob("*.py")) + list((ROOT / "integrations").rglob("*.py"))
        if "__pycache__" not in p.parts
    ]
    readers = [
        str(p.relative_to(ROOT))
        for p in files
        if any(
            k in p.read_text("utf-8")
            for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
        )
    ]
    assert readers == []


def test_the_gateway_layer_imports_only_the_shared_package_for_model_access():
    forbidden = ("anthropic", "openai", "google", "litellm", "cohere", "mistralai")
    for path in (ROOT / "integrations" / "gateway").glob("*.py"):
        assert not [n for n in _imports(path) if n.split(".")[0] in forbidden], path.name


def test_the_model_is_chosen_by_the_shared_configuration_not_by_uc03():
    advisor = GatewayAdvisor(RecordingLLM())
    assert (
        advisor.model == "acme/test-model"
    )  # whatever the injected shared client is configured with
    source = (ROOT / "integrations" / "gateway" / "advisor.py").read_text("utf-8")
    for model_like in ("gemini-", "claude-", "gpt-", "deepseek", "openrouter/"):
        assert model_like not in source
