"""UC-02 on the central LLM gateway (``hr_onboarding_common.llm``).

No test here makes a network call: the real ``LiteLLMGateway`` is driven
through its injectable ``completion_fn``, and the common ``FakeLLM`` stands in
for the orchestrator's offline ``AgentContext.llm``.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hr_onboarding_common.config import get_settings
from hr_onboarding_common.llm import (
    FakeLLM,
    LiteLLMGateway,
    LLMError,
    LLMRefusal,
    ModelTier,
)
from hr_onboarding_common.llm import input_block as common_input_block

from onboarding_task_management.agent import advisor as advisor_module
from onboarding_task_management.agent.advisor import (
    ADVISE_TASK,
    COMMON_LLM_AVAILABLE,
    RECOMMENDABLE_ACTIONS,
    LiteLLMAdvisor,
    OfflineAdvisor,
    advise_with_fallback,
    build_advisor,
    default_tier,
    degraded_reason_for,
    input_block,
    llm_configured,
)
from onboarding_task_management.agent.fake_llm import register_fake_handler
from onboarding_task_management.sub_agent.adapter import (
    GATEWAY_BEHAVIOUR,
    UC02SubAgent,
    WorkflowRequest,
)
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import build_registry, make_trigger, persona
from tests.support import HR_OPS

SECRET = "sk-live-SECRET-0123456789"
SRC = Path(__file__).resolve().parents[1] / "src" / "onboarding_task_management"


def _completion(content: str = "", finish_reason: str = "stop", calls: list | None = None):
    """A stand-in for ``litellm.completion`` returning one canned choice."""

    def completion(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=finish_reason, message=message)], usage=None
        )

    return completion


def _raising(exc: Exception, calls: list | None = None):
    def completion(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        raise exc

    return completion


def _gateway(completion_fn) -> LiteLLMGateway:
    return LiteLLMGateway(
        get_settings(), callbacks=[], completion_fn=completion_fn, sleep=lambda _s: None
    )


class GatewayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())
        record = self.service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.oid = record.onboarding_id
        self.view = self.service.checklist(HR_OPS, self.oid, record.joining_date)
        self.risk = self.service.joining_risk(HR_OPS, self.oid, record.joining_date)

    def valid_advice_json(self, **over) -> str:
        expected = OfflineAdvisor().advise(self.view, self.risk)
        body = {
            "summary": "s",
            "recommended_action": expected.recommended_action,
            "target_task_id": expected.target_task_id,
            "rationale": "r",
            "confidence": 0.8,
        }
        body.update(over)
        return json.dumps(body)


class CommonContractTests(unittest.TestCase):
    def test_the_common_gateway_package_is_used(self) -> None:
        self.assertTrue(COMMON_LLM_AVAILABLE)
        self.assertIs(advisor_module.ModelTier, ModelTier)
        self.assertIs(advisor_module.LLMError, LLMError)

    def test_advise_runs_on_the_workhorse_tier(self) -> None:
        self.assertIs(default_tier(), ModelTier.WORKHORSE)
        self.assertEqual(ADVISE_TASK, "uc02.advise")

    def test_input_block_is_the_common_one(self) -> None:
        payload = {"b": 1, "a": [1, 2]}
        self.assertEqual(input_block(payload), common_input_block(payload))

    def test_no_legacy_gateway_reference_remains(self) -> None:
        for name in ("agent/advisor.py", "agent/fake_llm.py", "sub_agent/adapter.py"):
            text = (SRC / name).read_text(encoding="utf-8")
            self.assertNotIn("hr_recruitment_common.llm", text, name)

    def test_no_provider_sdk_or_key_access(self) -> None:
        sdk = re.compile(r"^\s*(import|from)\s+(anthropic|openai|litellm|google\.generativeai)\b", re.M)
        for name in ("agent/advisor.py", "agent/fake_llm.py", "sub_agent/adapter.py"):
            text = (SRC / name).read_text(encoding="utf-8")
            self.assertIsNone(sdk.search(text), name)
            self.assertNotRegex(text, r"[A-Z]+_API_KEY", name)


class ModeSelectionTests(unittest.TestCase):
    def test_offline_mode_uses_the_deterministic_advisor(self) -> None:
        with mock.patch.dict(os.environ, {"LLM_MODE": "offline", "UC02_LLM_FAKE": "0"}):
            self.assertIsInstance(build_advisor(), OfflineAdvisor)

    def test_online_mode_builds_the_central_gateway(self) -> None:
        with mock.patch.dict(
            os.environ, {"LLM_MODE": "online", "UC02_LLM_OFFLINE": "0", "UC02_LLM_FAKE": "0"}
        ):
            advisor = build_advisor()
        self.assertIsInstance(advisor, LiteLLMAdvisor)
        self.assertIsInstance(advisor.client, LiteLLMGateway)
        self.assertIs(advisor.tier, ModelTier.WORKHORSE)
        # The model id comes from the common settings, not from UC-02.
        self.assertEqual(advisor.model(), get_settings().model_workhorse)

    def test_online_mode_prefers_the_common_build_llm_factory(self) -> None:
        import hr_onboarding_common.llm as common_llm

        built = register_fake_handler(FakeLLM())
        factory = mock.Mock(return_value=built)
        with mock.patch.dict(
            os.environ, {"LLM_MODE": "online", "UC02_LLM_OFFLINE": "0", "UC02_LLM_FAKE": "0"}
        ), mock.patch.object(common_llm, "build_llm", factory, create=True):
            advisor = build_advisor()
        factory.assert_called_once()
        self.assertIs(advisor.client, built)

    def test_an_injected_client_is_used_as_given(self) -> None:
        llm = FakeLLM()
        with mock.patch.dict(os.environ, {"LLM_MODE": "offline"}):
            advisor = build_advisor(llm)
        self.assertIsInstance(advisor, LiteLLMAdvisor)
        self.assertIs(advisor.client, llm)


class InjectedFakeLLMTests(GatewayTestCase):
    def test_common_fake_llm_answers_uc02_advise(self) -> None:
        llm = register_fake_handler(FakeLLM())
        advice = advise_with_fallback(LiteLLMAdvisor(llm), self.view, self.risk)
        self.assertFalse(advice.degraded, advice.degraded_reason)
        self.assertIn(advice.recommended_action, RECOMMENDABLE_ACTIONS)
        self.assertEqual(llm.calls[-1].task, ADVISE_TASK)
        self.assertEqual(llm.calls[-1].tier, ModelTier.WORKHORSE)
        self.assertIn("<input>", llm.calls[-1].user)

    def test_common_fake_llm_advice_is_labelled_fake(self) -> None:
        llm = register_fake_handler(FakeLLM())
        advice = advise_with_fallback(LiteLLMAdvisor(llm), self.view, self.risk)
        self.assertTrue(advice.source.startswith("fake_llm:"))

    def test_fake_llm_without_a_handler_degrades(self) -> None:
        advice = advise_with_fallback(LiteLLMAdvisor(FakeLLM()), self.view, self.risk)
        self.assertTrue(advice.degraded)
        self.assertEqual(advice.degraded_reason, "LLMError: the language model is unavailable")
        self.assertIn(advice.recommended_action, RECOMMENDABLE_ACTIONS)


class OnlineGatewayTests(GatewayTestCase):
    def test_online_advice_goes_through_the_gateway(self) -> None:
        calls: list = []
        gateway = _gateway(_completion(self.valid_advice_json(), calls=calls))
        advice = advise_with_fallback(LiteLLMAdvisor(gateway), self.view, self.risk)
        self.assertFalse(advice.degraded, advice.degraded_reason)
        self.assertEqual(len(calls), 1)
        # LiteLLM is called with the route (an OpenRouter model id becomes "openrouter/<id>"; llm/routing.py).
        self.assertEqual(calls[0]["model"], gateway.route_for(get_settings().model_workhorse))
        self.assertEqual(calls[0]["metadata"]["task"], ADVISE_TASK)
        self.assertEqual(advice.source, f"litellm:{get_settings().model_workhorse}")

    def test_provider_failure_degrades_without_leaking_text(self) -> None:
        gateway = _gateway(_raising(RuntimeError(f"401 invalid api_key={SECRET}")))
        advice = advise_with_fallback(LiteLLMAdvisor(gateway), self.view, self.risk)
        self.assertTrue(advice.degraded)
        self.assertEqual(advice.degraded_reason, "LLMError: the language model is unavailable")
        self.assertNotIn(SECRET, advice.model_dump_json())
        self.assertEqual(
            advice.recommended_action, OfflineAdvisor().advise(self.view, self.risk).recommended_action
        )

    def test_refusal_degrades_and_is_not_retried(self) -> None:
        calls: list = []
        gateway = _gateway(_completion("", finish_reason="content_filter", calls=calls))
        advice = advise_with_fallback(LiteLLMAdvisor(gateway), self.view, self.risk)
        self.assertTrue(advice.degraded)
        self.assertTrue(advice.degraded_reason.startswith("LLMRefusal:"))
        self.assertEqual(len(calls), 1)

    def test_invalid_recommendation_is_replaced(self) -> None:
        gateway = _gateway(_completion(self.valid_advice_json(recommended_action="grant_access")))
        advice = advise_with_fallback(LiteLLMAdvisor(gateway), self.view, self.risk)
        self.assertTrue(advice.degraded)
        self.assertIn("not an allowed action", advice.degraded_reason)
        self.assertIn(advice.recommended_action, RECOMMENDABLE_ACTIONS)

    def test_hallucinated_task_is_replaced(self) -> None:
        gateway = _gateway(_completion(self.valid_advice_json(target_task_id="NOPE#ghost")))
        advice = advise_with_fallback(LiteLLMAdvisor(gateway), self.view, self.risk)
        self.assertTrue(advice.degraded)
        self.assertNotEqual(advice.target_task_id, "NOPE#ghost")


class DegradedReasonTests(unittest.TestCase):
    def test_gateway_errors_carry_no_provider_text(self) -> None:
        for exc in (LLMError(SECRET), LLMRefusal(SECRET)):
            self.assertNotIn(SECRET, degraded_reason_for(exc))

    def test_local_errors_keep_a_bounded_message(self) -> None:
        reason = degraded_reason_for(RuntimeError("x" * 1000))
        self.assertTrue(reason.startswith("RuntimeError: "))
        self.assertLess(len(reason), 260)


def _request(llm_behaviour: str) -> WorkflowRequest:
    trigger = make_trigger(persona("EMP-001")).model_dump(mode="json")
    return WorkflowRequest(trigger=trigger, llm_behaviour=llm_behaviour)


class SubAgentInjectionTests(unittest.TestCase):
    """The orchestrator's ``AgentContext.llm`` reaches UC-02 through the adapter."""

    def test_injected_llm_is_used_for_gateway_requests(self) -> None:
        llm = register_fake_handler(FakeLLM())
        agent = UC02SubAgent(llm_client=llm)
        result = agent.run(_request(llm_behaviour=GATEWAY_BEHAVIOUR))
        self.assertTrue(llm.calls)
        self.assertEqual(llm.calls[-1].task, ADVISE_TASK)
        # The offline FakeLLM is labelled as a fake, never as a real call.
        self.assertTrue(result.advice["source"].startswith("fake_llm:"))
        self.assertFalse(result.advice["degraded"])

    def test_injected_gateway_is_used_online(self) -> None:
        from onboarding_task_management.agent.fake_llm import classify, respond
        from hr_onboarding_common.llm import extract_input_block

        def completion(**kwargs):
            payload = extract_input_block(kwargs["messages"][1]["content"])
            return _completion(respond(payload, classify(payload)).model_dump_json())(**kwargs)

        result = UC02SubAgent(llm_client=_gateway(completion)).run(_request(GATEWAY_BEHAVIOUR))
        self.assertEqual(result.advice["source"], f"litellm:{get_settings().model_workhorse}")
        self.assertFalse(result.advice["degraded"])

    def test_gateway_request_without_injection_follows_llm_mode(self) -> None:
        with mock.patch.dict(os.environ, {"LLM_MODE": "offline"}):
            result = UC02SubAgent().run(
                _request(llm_behaviour=GATEWAY_BEHAVIOUR)
            )
        self.assertEqual(result.advice["source"], "offline")

    def test_offline_requests_ignore_the_injected_llm(self) -> None:
        llm = register_fake_handler(FakeLLM())
        UC02SubAgent(llm_client=llm).run(_request(llm_behaviour="offline"))
        self.assertEqual(llm.calls, [])

    def test_agent_context_llm_can_be_injected(self) -> None:
        from dataclasses import fields

        from hr_onboarding_common.agent import AgentContext

        self.assertIn("llm", {f.name for f in fields(AgentContext)})
        ctx = SimpleNamespace(llm=register_fake_handler(FakeLLM()))
        result = UC02SubAgent(llm_client=ctx.llm).run(
            _request(llm_behaviour=GATEWAY_BEHAVIOUR)
        )
        self.assertFalse(result.advice["degraded"])


if __name__ == "__main__":
    unittest.main()
