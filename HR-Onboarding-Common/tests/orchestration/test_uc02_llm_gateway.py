"""UC-02 advice through the orchestrator's own LLM (`AgentContext.llm`).

The adapter passes `deps.ctx.llm` into UC02SubAgent with `llm_behaviour="gateway"`. Offline that
client is the bootstrap FakeLLM with UC-02's `uc02.advise` handler; online it is the central
LiteLLMGateway. No test here reaches a network: the online path drives the real gateway through its
injectable `completion_fn`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from _helpers import UC_ROOTS, make_actor
from hr_onboarding_common.config import get_settings
from hr_onboarding_common.llm import FakeLLM, LiteLLMGateway, extract_input_block
from hr_onboarding_common.orchestration.adapters.uc02_tasks import UC02_LLM_BEHAVIOUR, call_uc02_plan_tasks
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state

pytest.importorskip("onboarding_task_management")

from onboarding_task_management.agent.fake_llm import classify, register_fake_handler, respond  # noqa: E402

SECRET = "sk-or-v1-SECRET-0123456789"
_APPROVED_CTX = {
    "role_id": "backend_engineer", "department": "Engineering", "manager_id": "MGR-1",
    "location": "Dubai", "project": "apollo", "offer_accepted_at": "2026-10-20T10:00:00Z",
    "form_approved_at": "2026-10-21T10:00:00Z", "approved_by_role": "hr_ops",
}


class _Policy:
    settings = {"task_planning_trigger_roles": ["hr_admin"]}


class _Deps:
    """What the UC02 planning adapter reads: the policy, the cached UC02 sub-agent, UC03's root (the
    plan's document requirements come from UC03's real catalog) and, here, the orchestrator context."""

    policy = _Policy()
    uc03_repo_root = UC_ROOTS["uc03"]
    uc03_document_store_path = None
    _uc03_graph = _uc03_case_store = _uc03_audit_store = _uc03_checklist_service = _uc03_collection_service = None

    def __init__(self, llm=None, *, with_ctx=True):
        self._uc02_sub_agent = None
        if with_ctx:
            self.ctx = SimpleNamespace(llm=llm)


def _state():
    state = new_state(case_id="C1", client_id="CLIENT-ALPHA", correlation_id="corr1",
                      actor=make_actor(roles=("hr_admin",), client_id="CLIENT-ALPHA"), intake_trigger={},
                      uc02_role_context=dict(_APPROVED_CTX))
    state["employee_id"] = "EMP-001"
    state["joining_date"] = "2026-11-16"
    state["intake_trigger"] = {"offer_accepted": True}
    state["stage_statuses"] = {**state["stage_statuses"], Stage.INTAKE.value: StageStatus.COMPLETE.value}
    return state


def _scripted_completion(calls: list):
    """Stand-in for litellm.completion: answers from the prompt's <input> block, as a model would."""

    def completion(**kwargs):
        calls.append(kwargs)
        payload = extract_input_block(kwargs["messages"][1]["content"])
        message = SimpleNamespace(content=respond(payload, classify(payload)).model_dump_json())
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=message)], usage=None)

    return completion


def _failing_completion(**kwargs):
    raise RuntimeError(f"401 Unauthorized: invalid key {SECRET}")


def _gateway(completion_fn) -> LiteLLMGateway:
    return LiteLLMGateway(get_settings(), callbacks=[], completion_fn=completion_fn, sleep=lambda _s: None)


def test_the_adapter_requests_the_gateway_behaviour():
    assert UC02_LLM_BEHAVIOUR == "gateway"


def test_online_advice_goes_through_the_central_gateway():
    calls: list = []
    result = call_uc02_plan_tasks(_state(), _Deps(_gateway(_scripted_completion(calls))))
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value, result.get("error_detail")
    plan = result["task_plan"]
    assert plan["advice_source"].startswith("litellm:")
    assert plan["advice_source"] == f"litellm:{get_settings().model_workhorse}"
    assert plan["advice_degraded"] is False
    assert len(calls) == 1 and calls[0]["metadata"]["task"] == "uc02.advise"


def test_offline_fake_llm_answers_uc02_and_is_labelled_fake():
    llm = register_fake_handler(FakeLLM())
    result = call_uc02_plan_tasks(_state(), _Deps(llm))
    plan = result["task_plan"]
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value
    assert plan["advice_source"].startswith("fake_llm:")
    assert plan["advice_degraded"] is False
    assert [c.task for c in llm.calls] == ["uc02.advise"]


def test_a_gateway_failure_degrades_without_leaking_provider_text():
    result = call_uc02_plan_tasks(_state(), _Deps(_gateway(_failing_completion)))
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value
    assert result["task_plan"]["advice_degraded"] is True
    assert SECRET not in repr(result)


def test_a_fake_llm_without_the_uc02_handler_degrades_but_still_plans():
    result = call_uc02_plan_tasks(_state(), _Deps(FakeLLM()))
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value
    assert result["task_plan"]["advice_degraded"] is True
    assert len(result["task_plan"]["tasks"]) > 0


def test_without_an_orchestrator_context_uc02_follows_llm_mode_offline(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "offline")
    result = call_uc02_plan_tasks(_state(), _Deps(with_ctx=False))
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value
    assert result["task_plan"]["advice_source"] == "offline"
    assert result["task_plan"]["advice_degraded"] is False


def test_bootstrap_registers_the_uc02_handler_on_its_offline_llm():
    from hr_onboarding_common.orchestration.bootstrap import build_deps

    ctx, deps = build_deps(agent_home=Path(__file__).parent.parent.parent, uc03_repo_root=UC_ROOTS["uc03"], ucs=())
    try:
        assert isinstance(ctx.llm, FakeLLM)
        assert "uc02.advise" in ctx.llm.handlers
        assert deps.ctx is ctx
    finally:
        deps.worker_pool.stop_all()


# ---------------------------------------------------------------------- the real orchestrator


@pytest.fixture(params=["offline", "online"])
def wired_orchestrator(request):
    """The full orchestrator (real UC01/UC04/UC05 workers). Offline: the bootstrap FakeLLM. Online:
    the same context with its LLM replaced by the real gateway over a scripted transport."""
    from _helpers import require_uc

    for uc_id in ("uc01", "uc04", "uc05"):
        require_uc(uc_id)
    from hr_onboarding_common.orchestration.bootstrap import build_deps
    from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent

    ctx, deps = build_deps(agent_home=Path(__file__).parent.parent.parent, uc03_repo_root=UC_ROOTS["uc03"])
    calls: list = []
    if request.param == "online":
        ctx.llm = _gateway(_scripted_completion(calls))
    yield request.param, OnboardingOrchestratorAgent(ctx), calls
    deps.worker_pool.stop_all()


def test_uc02_advice_through_the_real_orchestrator(wired_orchestrator):
    from test_uc02_real_orchestration import APPROVAL_FACTS, _verified

    mode, orchestrator, calls = wired_orchestrator
    state = _verified(orchestrator, **APPROVAL_FACTS)
    assert state["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value, state.get("error_detail")
    plan = state["task_plan"]
    assert plan["advice_degraded"] is False
    if mode == "online":
        assert plan["advice_source"].startswith("litellm:")
        assert any(c["metadata"]["task"] == "uc02.advise" for c in calls)
    else:
        assert plan["advice_source"].startswith("fake_llm:")
