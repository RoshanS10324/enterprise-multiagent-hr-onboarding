"""UC-01 on the central LLM gateway (hr_onboarding_common.llm).

Nothing here reaches a network or a paid model. The online path drives the
real `LiteLLMGateway` through its injectable `completion_fn`; the offline
gateway path uses the common `FakeLLM`. Every scenario also checks the
property that matters most for UC-01: the model stays advisory, and a model
failure never changes a deterministic outcome.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

hr_llm = pytest.importorskip("hr_onboarding_common.llm")

from hr_onboarding_common.config import get_settings  # noqa: E402
from hr_onboarding_common.llm import FakeLLM, LiteLLMGateway, extract_input_block, litellm_route  # noqa: E402

from sub_agent.api.container import build_container  # noqa: E402
from sub_agent.api.settings import Settings  # noqa: E402
from sub_agent.llm import (  # noqa: E402
    FakeLlm,
    GatewayLlm,
    LlmError,
    LlmInputRefused,
    LlmModelRefused,
    LlmRequest,
    LlmUnavailable,
    audit_action_for,
    build_uc01_llm,
    load_all_tasks,
    load_persona,
)
from sub_agent.llm import gateway_client  # noqa: E402

SECRET = "sk-or-v1-SECRET-0123456789"
SUBJECT = "Re: Your offer [OFFER-9101] - I accept"
SRC = Path(__file__).resolve().parent.parent / "src" / "sub_agent"


@pytest.fixture
def tasks():
    return load_all_tasks()


@pytest.fixture
def persona():
    return load_persona()


def _reply(content: str, finish_reason: str = "stop"):
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason, message=message)], usage=None)


def _completion(content: str, calls: list, finish_reason: str = "stop"):
    def completion(**kwargs):
        calls.append(kwargs)
        return _reply(content, finish_reason)

    return completion


def _raising(exc: Exception, calls: list):
    def completion(**kwargs):
        calls.append(kwargs)
        raise exc

    return completion


def _gateway(completion_fn, callbacks=()) -> LiteLLMGateway:
    return LiteLLMGateway(get_settings(), callbacks=list(callbacks), completion_fn=completion_fn,
                          sleep=lambda _s: None)


def _label(label: str, confidence: float = 0.9) -> str:
    return json.dumps({"label": label, "confidence": confidence})


def _intent(llm) -> object:
    return llm.complete(LlmRequest(task="intake_intent", inputs={"subject": SUBJECT}))


# ----------------------------------------------------------------- selection


def test_offline_mode_builds_the_deterministic_fake(tasks, persona):
    assert isinstance(build_uc01_llm(tasks, persona, environ={"LLM_MODE": "offline"}), FakeLlm)
    assert isinstance(build_uc01_llm(tasks, persona, environ={}), FakeLlm)  # offline is the default


def test_online_mode_builds_the_central_gateway(tasks, persona):
    # A complete online environment: the factory validates it (key shaped like a key, never sent).
    environ = {"LLM_MODE": "online", "OPENROUTER_API_KEY": "sk-or-v1-" + "0" * 64,
               "OPENROUTER_MODEL": "deepseek/deepseek-v4.1-flash"}
    llm = build_uc01_llm(tasks, persona, environ=environ)
    assert isinstance(llm, GatewayLlm)
    assert isinstance(llm._gateway, LiteLLMGateway)  # noqa: SLF001 -- the wiring is the point


def test_an_injected_gateway_is_used_as_given(tasks, persona):
    gw = FakeLLM()
    llm = build_uc01_llm(tasks, persona, gateway=gw, environ={"LLM_MODE": "offline"})
    assert isinstance(llm, GatewayLlm) and llm._gateway is gw  # noqa: SLF001


def test_online_without_the_shared_package_refuses_clearly(tasks, persona, monkeypatch):
    monkeypatch.setattr(gateway_client, "GATEWAY_AVAILABLE", False)
    with pytest.raises(LlmError, match="hr_onboarding_common is not installed"):
        build_uc01_llm(tasks, persona, environ={"LLM_MODE": "online"})


def test_container_wires_the_gateway_only_when_enabled():
    gw = FakeLLM()
    on = build_container(Settings(app_env="dev", llm_enabled=True, llm_mode="online"), llm_gateway=gw)
    assert isinstance(on.onboarding_deps.llm, GatewayLlm)
    assert on.intake_deps.llm is on.onboarding_deps.llm
    offline = build_container(Settings(app_env="dev", llm_enabled=True, llm_mode="offline"))
    assert isinstance(offline.onboarding_deps.llm, FakeLlm)
    off = build_container(Settings(app_env="dev", llm_enabled=False, llm_mode="online"), llm_gateway=gw)
    assert off.onboarding_deps.llm is None


def test_settings_read_llm_mode_from_the_environment():
    assert Settings.from_env({"LLM_MODE": "online"}).llm_mode == "online"
    assert Settings.from_env({}).llm_mode == "offline"


# ------------------------------------------------------------- successful calls


def test_common_fake_gateway_answers_through_the_shared_contract(tasks, persona):
    gw = FakeLLM().on("uc01.intake_intent", lambda system, user, schema: schema(label="LOOKS_LIKE_ACCEPTANCE",
                                                                                 confidence=0.8))
    response = _intent(GatewayLlm(gw, tasks, persona))
    assert response.label == "LOOKS_LIKE_ACCEPTANCE"
    assert response.task_version == tasks["intake_intent"].version
    call = gw.calls[-1]
    assert call.task == "uc01.intake_intent"
    assert call.tier == hr_llm.ModelTier.FAST
    assert response.model == get_settings().model_fast  # from shared settings, never from UC-01


def test_online_stand_in_completion_goes_through_the_real_gateway(tasks, persona):
    calls: list = []
    llm = GatewayLlm(_gateway(_completion(_label("LOOKS_LIKE_ACCEPTANCE"), calls)), tasks, persona)
    response = _intent(llm)
    assert response.label == "LOOKS_LIKE_ACCEPTANCE"
    assert len(calls) == 1
    assert calls[0]["model"] == litellm_route(get_settings().model_fast, get_settings())  # OpenRouter id -> "openrouter/<id>"
    assert calls[0]["metadata"]["task"] == "uc01.intake_intent"
    system = calls[0]["messages"][0]["content"]
    assert persona.strip()[:40] in (system if isinstance(system, str) else system[0]["text"])


def test_draft_tasks_use_the_workhorse_tier(tasks, persona):
    calls: list = []
    gw = _gateway(_completion(json.dumps({"text": "Two fields differ; see the form.", "confidence": 0.7}), calls))
    response = GatewayLlm(gw, tasks, persona).complete(LlmRequest(task="discrepancy_summary", inputs={
        "case_id": "c-1", "fields_in_conflict": "designation", "other_source": "hr_system", "form_version": "1"}))
    assert response.text == "Two fields differ; see the form."
    assert calls[0]["model"] == litellm_route(get_settings().model_workhorse, get_settings())


# ------------------------------------------------------------- failures


def test_provider_failure_becomes_unavailable_without_provider_text(tasks, persona):
    calls: list = []
    llm = GatewayLlm(_gateway(_raising(RuntimeError(f"401 invalid key {SECRET}"), calls)), tasks, persona)
    with pytest.raises(LlmUnavailable) as exc:
        _intent(llm)
    assert SECRET not in str(exc.value)
    assert audit_action_for(exc.value) == "LLM_UNAVAILABLE"


def test_timeout_is_retried_then_degrades(tasks, persona):
    Timeout = type("Timeout", (Exception,), {})  # the gateway retries exceptions of this name
    calls: list = []
    llm = GatewayLlm(_gateway(_raising(Timeout("read timed out"), calls)), tasks, persona)
    with pytest.raises(LlmUnavailable):
        _intent(llm)
    assert len(calls) >= get_settings().llm_max_attempts  # retried by the gateway, not by UC-01


def test_refusal_is_reported_as_a_refusal_and_not_retried(tasks, persona):
    calls: list = []
    llm = GatewayLlm(_gateway(_completion("", calls, finish_reason="content_filter")), tasks, persona)
    with pytest.raises(LlmModelRefused) as exc:
        _intent(llm)
    assert len(calls) == 1
    assert audit_action_for(exc.value) == "LLM_MODEL_REFUSED"


def test_invalid_json_from_the_model_degrades(tasks, persona):
    calls: list = []
    llm = GatewayLlm(_gateway(_completion("not json at all", calls)), tasks, persona)
    with pytest.raises(LlmUnavailable):
        _intent(llm)


def test_a_label_outside_the_declared_outputs_is_refused(tasks, persona):
    calls: list = []
    llm = GatewayLlm(_gateway(_completion(_label("APPROVE_THE_EMPLOYEE"), calls)), tasks, persona)
    with pytest.raises(LlmUnavailable, match="output_values"):
        _intent(llm)


@pytest.mark.parametrize("text", ["   ", "x" * (gateway_client.MAX_DRAFT_CHARS + 1)])
def test_an_empty_or_oversized_draft_is_refused(tasks, persona, text):
    calls: list = []
    gw = _gateway(_completion(json.dumps({"text": text, "confidence": 0.5}), calls))
    with pytest.raises(LlmUnavailable, match="length"):
        GatewayLlm(gw, tasks, persona).complete(LlmRequest(task="discrepancy_summary", inputs={
            "case_id": "c-1", "fields_in_conflict": "designation", "other_source": "hr", "form_version": "1"}))


# ------------------------------------------------------------- sanitisation


@pytest.mark.parametrize("inputs", [
    {"subject": SUBJECT, "salary_and_terms": "AED 99,999"},          # forbidden name
    {"subject": SUBJECT, "id_number": "784-1990-1234567-1"},           # personal name
    {"subject": "Re: [OFFER-1] from someone@example.test - I accept"},  # email inside a value
    {"subject": "**********"},                                         # a str()-ed SecretStr
])
def test_sensitive_inputs_are_refused_before_the_gateway_is_called(tasks, persona, inputs):
    gw = FakeLLM().on("uc01.intake_intent", lambda *a: pytest.fail("the gateway must not be called"))
    with pytest.raises(LlmInputRefused) as exc:
        GatewayLlm(gw, tasks, persona).complete(LlmRequest(task="intake_intent", inputs=inputs))
    assert gw.calls == []
    for value in inputs.values():
        if value not in ("**********",):
            assert value not in str(exc.value)  # names only, never values
    assert audit_action_for(exc.value) == "LLM_INPUT_REFUSED"


def test_nothing_sensitive_reaches_the_gateway_logs(tasks, persona, caplog):
    """The gateway's own logging callback records task, model and error type only."""
    calls: list = []
    gw = LiteLLMGateway(get_settings(), completion_fn=_raising(RuntimeError(SECRET), calls), sleep=lambda _s: None)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LlmUnavailable):
            _intent(GatewayLlm(gw, tasks, persona))
    logged = caplog.text + " ".join(str(getattr(r, "__dict__", "")) for r in caplog.records)
    assert SECRET not in logged
    assert SUBJECT not in logged


# ------------------------------------------------------------- business behaviour stays deterministic


def _intake_deps(llm):
    from factories import make_intake_deps

    from sub_agent.models import OfferRecord, OfferStatus

    deps = make_intake_deps()
    deps.llm = llm
    deps.offer_repo.save(OfferRecord(client_id="client-gw-001", offer_ref="OFFER-9101",
                                     candidate_email="gw.candidate@example.test",
                                     hr_officer_id="hr-officer-gw-1", status=OfferStatus.SENT))
    return deps


def _email(subject: str) -> str:
    return f"From: gw.candidate@example.test\nSubject: {subject}\n\nbody never read\n"


def test_a_gateway_outage_leaves_the_intake_outcome_deterministic(tasks, persona):
    from sub_agent.intake.email_intake import handle_acceptance_email

    baseline = handle_acceptance_email(_email(SUBJECT), _intake_deps(None))
    calls: list = []
    failing = GatewayLlm(_gateway(_raising(RuntimeError("down"), calls)), tasks, persona)
    deps = _intake_deps(failing)
    with_outage = handle_acceptance_email(_email(SUBJECT), deps)

    assert with_outage.outcome == baseline.outcome
    assert "LLM_UNAVAILABLE" in [e.action for e in deps.onboarding_deps.audit_log.entries()]


def test_the_gateway_can_escalate_but_never_clear_a_review(tasks, persona):
    from sub_agent.intake.email_intake import IntakeOutcome, handle_acceptance_email

    unsure = GatewayLlm(FakeLLM().on("uc01.intake_intent", lambda s, u, schema: schema(
        label="NO_ACCEPTANCE_SIGNAL", confidence=0.6)), tasks, persona)
    escalated = handle_acceptance_email(_email(SUBJECT), _intake_deps(unsure))
    assert escalated.outcome is IntakeOutcome.NEEDS_HR_REVIEW
    assert escalated.review_code == "MODEL_FLAGGED_FOR_REVIEW"

    confident = GatewayLlm(FakeLLM().on("uc01.intake_intent", lambda s, u, schema: schema(
        label="LOOKS_LIKE_ACCEPTANCE", confidence=0.99)), tasks, persona)
    decline = handle_acceptance_email(_email("Re: [OFFER-9101] - I must decline"), _intake_deps(confident))
    assert decline.outcome is IntakeOutcome.NEEDS_HR_REVIEW  # the model cannot clear the keyword review
    assert decline.review_code == "POSSIBLE_DECLINE_OR_NEGOTIATION"


def test_case_creation_is_unchanged_when_the_draft_task_fails(tasks, persona):
    """A name-only match raises an HC_12 notice; the explanation draft fails;
    the case and the approval are still created."""
    from factories import make_approved_form, make_deps, make_person, make_placement

    from sub_agent.adapters.memory import InMemoryPersonDirectory
    from sub_agent.models import PersonRecordKind
    from sub_agent.onboarding_entry import OpenCaseOutcome, OpenCaseRequest, open_onboarding_case

    directory = InMemoryPersonDirectory()
    directory.add(make_person(email="someone.else@example.test", full_name="Gateway Candidate",
                              kind=PersonRecordKind.PAST_EMPLOYEE))
    deps = make_deps(person_directory=directory)
    calls: list = []
    deps.llm = GatewayLlm(_gateway(_raising(RuntimeError("down"), calls)), tasks, persona)

    form = make_approved_form(candidate_name="Gateway Candidate", candidate_email="gw.case@example.test")
    result = open_onboarding_case(OpenCaseRequest(request_id="req-gw-1", client_id=form.client_id,
                                                  hr_officer_id="hr-officer-001", offer_accepted=True, form=form,
                                                  placement=make_placement()), deps)

    assert result.outcome == OpenCaseOutcome.CASE_OPENED
    assert result.approvals_created
    assert "LLM_UNAVAILABLE" in [e.action for e in deps.audit_log.entries()]


def test_draft_text_is_never_written_to_the_audit_log(tasks, persona):
    from factories import make_compare_deps, make_linked_case_and_form

    from sub_agent.compare_sources import compare_sources
    from sub_agent.models import SourceSnapshot

    draft = "DRAFT-TEXT-MUST-NOT-BE-AUDITED"
    gw = FakeLLM().on("uc01.discrepancy_summary", lambda s, u, schema: schema(text=draft, confidence=0.5))
    case, form = make_linked_case_and_form()
    deps = make_compare_deps()
    deps.llm = GatewayLlm(gw, tasks, persona)

    compare_sources(case, form, [SourceSnapshot(source="hr_system", values={"designation": "Other Title"})], deps)

    entries = [e for e in deps.audit_log.entries() if e.action == "DISCREPANCY_SUMMARY_DRAFTED"]
    assert len(entries) == 1
    assert draft not in str(entries[0].model_dump())
    assert entries[0].after["draft_chars"] == len(draft) and len(entries[0].after["draft_sha256"]) == 64
    # The model saw field names only, never the conflicting values.
    prompt = gw.calls[-1].user
    assert "Other Title" not in prompt and form.designation not in prompt


# ------------------------------------------------------------- boundaries


def test_no_uc01_module_imports_a_provider_sdk_or_reads_a_provider_key():
    sdk = re.compile(r"^\s*(import|from)\s+(litellm|anthropic|openai|google\.genai|google\.generativeai)\b", re.M)
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert sdk.search(text) is None, path
        assert not re.search(r"\b(ANTHROPIC|OPENAI|GEMINI|AZURE|OPENROUTER)_API_KEY\b", text), path


def test_the_offline_gateway_path_never_builds_a_provider_client(tasks, persona, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a real gateway must not be built offline")

    monkeypatch.setattr(gateway_client, "_build_gateway", boom)
    assert isinstance(build_uc01_llm(tasks, persona, environ={"LLM_MODE": "offline"}), FakeLlm)


def test_the_gateway_receives_the_guarded_prompt_only(tasks, persona):
    seen = {}

    def handler(system, user, schema):
        seen["user"] = user
        return schema(label="LOOKS_LIKE_ACCEPTANCE", confidence=0.9)

    _intent(GatewayLlm(FakeLLM().on("uc01.intake_intent", handler), tasks, persona))
    assert seen["user"].rstrip().endswith(f"subject: {SUBJECT}")
    assert extract_input_block(seen["user"]) == {}  # plain declared inputs, nothing smuggled in a block
