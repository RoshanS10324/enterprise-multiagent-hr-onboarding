"""Static checks of the provider boundary: in HR-Onboarding-Common, UC05 and (below) every UC agent's source,
no provider SDK is imported anywhere and LiteLLM only by the gateway -- so no agent can create its
own client."""

from __future__ import annotations

import ast
from pathlib import Path

COMMON = Path(__file__).resolve().parents[2]
WORKSPACE = COMMON.parent
PROVIDER_SDKS = ("anthropic", "openai", "google.genai", "google.generativeai", "cohere", "mistralai")


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _sources(*roots: Path) -> list[Path]:
    return [p for root in roots for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _offenders(files: list[Path], prefixes: tuple[str, ...]) -> list[str]:
    return sorted(f"{p.relative_to(WORKSPACE)}: {name}" for p in files for name in _imports(p)
                  if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes))


def test_no_provider_sdk_is_imported_by_the_common_package_or_uc05():
    uc05 = WORKSPACE / "UC05_Orientation-Schedule-Agent"
    files = _sources(COMMON / "src", COMMON / "scripts", uc05 / "src", uc05 / "connectors", uc05 / "context_layer",
                     uc05 / "evaluation")
    assert files and _offenders(files, PROVIDER_SDKS) == []


def test_litellm_is_imported_only_by_the_gateway():
    uc05 = WORKSPACE / "UC05_Orientation-Schedule-Agent"
    users = _offenders(_sources(COMMON / "src", uc05 / "src"), ("litellm",))
    assert users == [str(Path("HR-Onboarding-Common/src/hr_onboarding_common/llm/gateway.py")) + ": litellm"]


def test_the_uc02_adapter_asks_for_a_behaviour_uc02_accepts():
    """UC02's accepted behaviours are read from its source (no import needed): its scripted
    FAKE_BEHAVIOURS plus GATEWAY_BEHAVIOUR. The orchestrator must ask for the gateway one, so UC02's
    advice goes through the orchestrator's central LLM client (LiteLLMGateway online) -- never a
    scripted fake in production."""
    from hr_onboarding_common.orchestration.adapters.uc02_tasks import UC02_LLM_BEHAVIOUR

    sub_agent = WORKSPACE / "UC02_Onboarding_Task_Agent" / "src" / "onboarding_task_management" / "sub_agent"

    def assigned(path, name):
        return [ast.literal_eval(node.value) for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets)]

    (behaviours,) = assigned(sub_agent / "fakes.py", "FAKE_BEHAVIOURS")
    (gateway,) = assigned(sub_agent / "adapter.py", "GATEWAY_BEHAVIOUR")
    assert UC02_LLM_BEHAVIOUR in (*behaviours, gateway)
    assert UC02_LLM_BEHAVIOUR == gateway


# The source trees of every UC agent (not their tests, which may fake a transport).
UC_SOURCE_DIRS = {
    "uc01": ("UC01_Employee_Record_Creation", ("src",)),
    "uc02": ("UC02_Onboarding_Task_Agent", ("src", "connectors")),
    "uc03": ("UC03_Document_Validation", ("src", "guardrails")),
    "uc04": ("UC04_Equipment_Access_Integration_Ready", ("src", "integrations", "connectors", "mcp")),
    "uc05": ("UC05_Orientation-Schedule-Agent", ("src", "connectors", "context_layer")),
}
# The only import of litellm outside the gateway: UC-02's `litellm_available()` probe (agent/harness.py)
# checks whether the package is installed. It makes no call and builds no client.
_LITELLM_PROBES = {str(Path("UC02_Onboarding_Task_Agent/src/onboarding_task_management/agent/harness.py")) + ": litellm"}


def _uc_sources() -> list[Path]:
    roots = [WORKSPACE / folder / sub for folder, subs in UC_SOURCE_DIRS.values() for sub in subs]
    return _sources(*[r for r in roots if r.is_dir()])


def test_no_uc_agent_imports_a_provider_sdk():
    """UC01-UC05 reach a model only through hr_onboarding_common.llm (the central gateway). UC03's former
    direct Anthropic/Gemini calls (comms.py, extraction.py, name_agent.py) go through it as well now."""
    files = _uc_sources()
    assert len({p.relative_to(WORKSPACE).parts[0] for p in files}) == len(UC_SOURCE_DIRS)
    assert _offenders(files, PROVIDER_SDKS) == []


def test_no_uc_agent_calls_litellm_itself():
    assert set(_offenders(_uc_sources(), ("litellm",))) <= _LITELLM_PROBES


def test_uc03_model_calls_go_through_its_advisor_port():
    """UC03's model use is advisory and goes through its port (src/sub_agent/advisor_port.py), implemented over
    the shared client in integrations/gateway/ (GatewayAdvisor, given hr_onboarding_common.llm's LLMClient by the
    orchestrator). Name checks and drafts use the port; low-confidence extraction sends no document text to any
    model. No UC03 file builds a client or reads a provider credential."""
    uc03 = WORKSPACE / "UC03_Document_Validation"
    sub_agent = uc03 / "src" / "sub_agent"
    for name in ("comms.py", "name_agent.py"):
        text = (sub_agent / name).read_text(encoding="utf-8")
        assert "advisor_port" in text, f"{name} must reach a model only through the advisor port"
    for path in (sub_agent / "comms.py", sub_agent / "extraction.py", sub_agent / "name_agent.py",
                 *sorted((uc03 / "integrations" / "gateway").glob("*.py"))):
        text = path.read_text(encoding="utf-8")
        assert "API_KEY" not in text, f"{path.name} must not read a provider credential itself"
    assert not (sub_agent / "llm_gateway.py").exists(), "a second UC03 model path would bypass the advisor port"
