"""Scope regressions after the orchestration changes: NDA/HR-policy acknowledgement and signature
tracking stays retired, and HR Policies Q&A stays an independent, reachable capability."""

import importlib.util
from pathlib import Path

import yaml

from sub_agent import api
from sub_agent.agent import AGENT_HOME, WorkflowEvent

RETIRED = ("acknowledg", "signature")


def _routes():
    return {route.path for route in api.app.routes}


def test_acknowledgement_and_signature_tracking_stays_retired():
    assert [p for p in _routes() if any(word in p for word in RETIRED)] == []
    assert importlib.util.find_spec("sub_agent.signatures") is None
    assert [e for e in WorkflowEvent if any(word in e.value for word in RETIRED)] == []
    tools = yaml.safe_load((Path(AGENT_HOME) / "guardrails" / "policy.yaml").read_text(encoding="utf-8"))["tools"]
    assert [t for group in tools.values() for t in group if any(word in t for word in RETIRED)] == []


def test_hr_policies_qa_stays_reachable():
    assert {"/v1/onboarding/policy-questions", "/v1/onboarding/workflow/policy-questions"} <= _routes()
    assert WorkflowEvent.POLICY_QUESTION == "policy_question"
