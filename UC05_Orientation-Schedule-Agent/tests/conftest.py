import os

# Tests run as HR_AGENT_ENV=test with header identity (IDENTITY_MODE=insecure-dev-headers), which
# sub_agent.identity refuses in any other environment. Set before sub_agent.api is imported.
# tests/test_identity.py covers the JWT mode and the production refusals in fresh interpreters.
os.environ.setdefault("HR_AGENT_ENV", "test")
os.environ.setdefault("IDENTITY_MODE", "insecure-dev-headers")
# Never a real model: the onboarding system's .env (HR-Onboarding-Common/.env) may hold LLM_MODE=online and
# a real OPENROUTER_API_KEY. Point it away, force offline, drop provider credentials. Tests of online
# configuration set fake values themselves (tests/test_llm_runtime.py).
os.environ["HR_ONBOARDING_ENV_FILE"] = os.path.join(os.path.dirname(__file__), "no-such-dir", ".env")
os.environ["LLM_MODE"] = "offline"
# Never the live database either: sub_agent.api loads the local .env at import, and a developer's .env may
# select UC05_DATABASE_MODE=supabase with HR_AGENT_PERSIST=1. Set here, before that import, so .env (which
# never overrides an existing variable) cannot switch the suite to Supabase or on-disk stores. The opt-in
# tests/test_supabase_live.py builds its own Supabase connection from SUPABASE_* and is unaffected.
os.environ["UC05_DATABASE_MODE"] = "mock"
os.environ["HR_AGENT_PERSIST"] = "0"
os.environ["HR_AGENT_ENV"] = "test"
os.environ["IDENTITY_MODE"] = "insecure-dev-headers"
os.environ["CONNECTOR_MODE"] = "offline"
for _name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "AZURE_API_KEY"):
    os.environ.pop(_name, None)
# ...and the model and reasoning settings, so every test starts from the defaults.
for _name in ("LLM_PROVIDER", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL", "OPENROUTER_REQUIRE_PARAMETERS",
              "LLM_MODEL_FRONTIER", "LLM_MODEL_WORKHORSE", "LLM_MODEL_FAST", "LLM_FALLBACKS_FRONTIER",
              "LLM_FALLBACKS_WORKHORSE", "LLM_FALLBACKS_FAST", "LLM_REASONING_ENABLED", "LLM_REASONING_EFFORT",
              "LLM_REASONING_TOKEN_HEADROOM"):
    os.environ.pop(_name, None)

from datetime import datetime, timezone

import pytest

from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.contracts import Actor, AgentRequest, Purpose, Scope, WorkflowRef
from sub_agent.agent import AGENT_HOME, OnboardingAgent
from sub_agent.fakes import build_fake_dependencies, offline_llm

# client_id is CLIENT-ACME because every make_request() caller in this conftest's test files uses
# EMP-001/EMP-002 (both CLIENT-ACME) with this fixture; tests touching CLIENT-GLOBEX employees
# build their own Actor with the matching client_id instead of reusing this one.
HR_ADMIN = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
                 client_id="CLIENT-ACME")

# The synthetic world's joining dates are fixed (2026-11-02 ... 2026-12-01). A session may not be booked
# in the past, so the tests run on a fixed "now" before all of them -- the same instant testcase.py's
# workflow suite injects -- instead of the wall clock, which would make them fail as real time passes.
TEST_NOW = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_default_clock(monkeypatch):
    """Pins the DEFAULT clock only; a test that passes its own clock (e.g. FakeClock) still uses it."""
    import sub_agent.tool_registry as tool_registry
    monkeypatch.setattr(tool_registry, "utc_now", lambda: TEST_NOW)


@pytest.fixture
def deps():
    """A fresh OnboardingDependencies bundle of fakes (repository, calendar, email, directory,
    notifier, signature verifier, in-memory checkpointer) -- what AgentContext.connector holds."""
    return build_fake_dependencies()


@pytest.fixture
def llm():
    return offline_llm()


@pytest.fixture
def ctx(deps, llm):
    return AgentContext.build(agent_home=AGENT_HOME, llm=llm, connector=deps)


@pytest.fixture
def agent(ctx):
    return OnboardingAgent(ctx)


@pytest.fixture
def make_request():
    def _make_request(agent: OnboardingAgent, inp, *, employee_id: str) -> AgentRequest:
        return AgentRequest[agent.input_model](
            workflow=WorkflowRef(id="test", instance=employee_id, step="test"), actor=HR_ADMIN,
            scope=Scope(subject_ids=[employee_id]), input=inp)

    return _make_request
