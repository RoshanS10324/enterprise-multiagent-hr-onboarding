# UC01–UC04: Moving to the Central LLM Gateway

**For:** the owners of UC01, UC02, UC03 and UC04. **From:** the HR-Onboarding-Common integration work
of 2026-11-02 (OpenRouter through LiteLLM). Updated the same day: every tier now runs on
`deepseek/deepseek-v4.1-flash` with reasoning (§0a). **Status:** proposals only. Nothing in UC01–UC04
was changed; every change below is yours to make, review and test.

The common side is in place: `hr_onboarding_common.llm` (gateway, mode selection, validation,
redaction), the orchestrator's wiring, and UC05, which already uses it. This guide lists, for each of
your UCs, what does not yet go through the gateway and exactly how to connect it. How the system is
configured and run: [OPENROUTER_INTEGRATION_GUIDE.md](OPENROUTER_INTEGRATION_GUIDE.md).

| UC | Production model calls today | Gateway integration | What is needed |
|---|---|---|---|
| UC01 | none (deterministic `FakeLlm` only, off by default) | not applicable yet | optional: a gateway-backed `LlmClient`, to use a real model |
| UC02 | none reachable (fake advisor only; real path broken) | **missing / incorrect** | fix `advisor.py:1`; use `hr_onboarding_common` instead of `hr_recruitment_common`; let the sub-agent accept a client |
| UC03 | **direct Anthropic and Gemini SDK calls** | **missing** | replace three SDK call sites with the gateway, keeping every fallback |
| UC04 | none (deterministic `FakeLlmAdvisor`) | not applicable | none required; optional gateway-backed advisor |

---

## 0. What every UC uses (common to all sections)

**The one interface.** `hr_onboarding_common.llm.LLMClient`:

```python
from hr_onboarding_common.llm import LLMClient, LLMError, LLMRefusal, ModelTier, input_block

result: MySchema = llm.structured(
    task="ucXX.some_task",      # stable name: appears in logs, traces, FakeLLM handlers
    tier=ModelTier.FAST,        # FRONTIER | WORKHORSE | FAST -- never a model id
    system=PREAMBLE,            # stable system prompt
    user=input_block({...}),    # structured input; untrusted text wrapped as data
    schema=MySchema,            # Pydantic model; the reply is validated against it
    max_tokens=800,
)
```

- Never import `litellm`, `anthropic`, `openai` or `google.genai`, and never read a provider key.
  The gateway does both.
- `LLMError` means every configured model failed, after retries and fallbacks. Its message is
  already redacted. Catch it and take your deterministic path. `LLMRefusal` (a subclass) means
  the model declined on content grounds; it is not retried.
- **Building the client** happens once per process: `build_llm(get_settings(), offline=<your
  offline LLM>)`. With `LLM_MODE=offline` you get your offline LLM. With `LLM_MODE=online` you get
  the `LiteLLMGateway`, after the configuration (key, tiers) has been validated.
- **Tests:** `hr_onboarding_common.llm.FakeLLM().on("ucXX.some_task", handler)` returns deterministic
  output through the same `structured()` call. Inject a `LiteLLMGateway(settings, completion_fn=...)`
  to test the real gateway without a network.
- **Tiers:** FRONTIER complex decisions, WORKHORSE drafting and reasoning, FAST extraction and
  classification. The model behind each tier is set in `HR-Onboarding-Common/.env`
  (`OPENROUTER_MODEL`, or `LLM_MODEL_*` per tier), never in UC code.

**Worker UCs (UC01, UC04)** run in their own processes with their own interpreters. To use the gateway
there:
1. Add `hr-onboarding-common` (path dependency, as UC05 does:
   `hr-onboarding-common = { path = "../hr-onboarding-common" }`) and `litellm` to your
   `pyproject.toml`, and re-lock.
2. Tell the HR-Onboarding-Common team. The orchestrator starts your worker *without* provider keys
   until your UC calls a model. They will switch it on: `DEFAULT_LLM_ACCESS` in
   `orchestration/workers/config.py`, or `UC0X_WORKER_LLM_ACCESS=1` per deployment.
3. Your worker inherits `LLM_MODE`, the tier variables and `HR_ONBOARDING_ENV_FILE` from the
   orchestrator. `get_settings()` loads that same `.env`.

**In-process UCs (UC02, UC03)** run inside the orchestrator. Accept an `LLMClient` from your caller
(dependency injection) rather than building one. The orchestrator passes its `AgentContext.llm`, which
`build_llm` already chose by mode.

## 0a. The central model: DeepSeek V4.1 Flash with reasoning

The shared configuration (`HR-Onboarding-Common/.env.example`) runs every tier on the OpenRouter
model `deepseek/deepseek-v4.1-flash`, with reasoning on. Details and evidence:
[OPENROUTER_DEEPSEEK_INTEGRATION_REPORT.md](OPENROUTER_DEEPSEEK_INTEGRATION_REPORT.md). For a UC this
means **nothing to add**. The gateway handles all of it, and the code in §1–§4 is unchanged:

- **Model ids.** Configuration holds OpenRouter model ids (`deepseek/deepseek-v4.1-flash`). The gateway
  turns them into the LiteLLM route (`openrouter/deepseek/deepseek-v4.1-flash`). Never name a model,
  an id or a route in UC code. Never write the bare id where a LiteLLM route is expected: LiteLLM
  would call DeepSeek's own API.
- **Reasoning is central.**
  - Do not send `reasoning`, `reasoning_effort`, `extra_body` or provider routing yourself, and do
    not create an OpenAI or OpenRouter client. `structured(...)` has no such parameters, on purpose.
  - The gateway sends `reasoning` and `provider.require_parameters`, and returns only your validated
    schema object.
  - Single-turn calls ask OpenRouter to exclude reasoning text, so nothing to filter reaches you.
- **Token budgets.** The `max_tokens` you pass is for your answer. While reasoning is on, the gateway
  adds `LLM_REASONING_TOKEN_HEADROOM` (2048) for the model's reasoning, so small budgets such as UC03's
  `max_tokens=200` name check still leave room for the answer. Do not inflate your budgets for
  reasoning.
- **A schema field called `reasoning` is answer text,** not the provider's reasoning trace. UC03's
  proposed `_NameVerdict.reasoning` (§3) is the model's stated justification, returned and stored like
  any other field. Consider naming it `rationale` so readers do not confuse the two.
- **Multi-turn, if a UC ever needs it.** Every call in §1–§4 is single-turn. To continue a
  conversation, create one `LLMConversation` per case or session and pass it with the same scope on
  every turn:

  ```python
  from hr_onboarding_common.llm import LLMConversation

  scope = (client_id, case_id, session_id)
  conversation = LLMConversation(scope=scope)
  first = llm.structured(task="ucXX.chat", tier=ModelTier.WORKHORSE, system=PREAMBLE,
                         user=input_block({...}), schema=Reply, max_tokens=600,
                         conversation=conversation, scope=scope)
  # the next turn: the earlier turns, with OpenRouter's reasoning_details unchanged, are sent too
  second = llm.structured(..., conversation=conversation, scope=scope)
  ```

  The gateway refuses a conversation under any other scope (`ConversationScopeError`) before
  anything is sent, and adds a turn only when it succeeds. Keep the conversation in memory for that
  run only. Never put it in case state, a checkpoint, a log or an API response: it carries the
  provider's reasoning metadata. `FakeLLM` honours the same contract, so offline tests need nothing
  extra.
- **Errors.** A reasoning budget used up before any answer is an `LLMError` like any other. Your
  existing `except LLMError` paths already cover it.

---

## 1. UC01 — Employee Record Creation

**Affected files**
- `src/sub_agent/api/container.py`: `build_container`, around lines 94–97, where the LLM is chosen
- `src/sub_agent/api/settings.py`: `Settings.llm_enabled` (`UC01_LLM_ENABLED`)
- `src/sub_agent/llm/`: a new `gateway_client.py`; `base.py`, `guard.py` and `brain.py` stay unchanged
- `pyproject.toml`: dependencies

**Existing implementation**
- UC01's own interface: `LlmClient.complete(LlmRequest) -> LlmResponse` (`llm/base.py`).
- The guard `guard.build_prompt(spec, inputs)` refuses undeclared or secret inputs before any model
  work.
- The only implementation is the deterministic `FakeLlm`. It is attached only when
  `UC01_LLM_ENABLED=true` and off by default.
- Tasks come from `brain/tasks/*.yaml`:

| Task | Authority | Caller | Fallback on `LlmError` |
|---|---|---|---|
| `discrepancy_summary` | `DRAFT_FOR_HUMAN` | `compare_sources.py` | audit `LLM_UNAVAILABLE`, flag kept |
| `duplicate_explanation` | `DRAFT_FOR_HUMAN` | duplicate handling | deterministic text |
| `intake_intent` | `ADVISORY_ESCALATE_ONLY` | `intake/email_intake.py` | audit `LLM_UNAVAILABLE`, no escalation |

**Missing integration:** no production client exists, so nothing reaches a model, and the worker
environment has neither `hr_onboarding_common` nor `litellm`. Nothing is *incorrect*: there is simply
no gateway path yet.

**Proposed change.** Add an adapter that keeps UC01's interface and guard, and calls the gateway:

```python
# src/sub_agent/llm/gateway_client.py
from pydantic import BaseModel, Field

from hr_onboarding_common.llm import LLMClient as GatewayClient, LLMError, ModelTier

from .base import LlmAuthority, LlmRequest, LlmResponse, LlmTaskSpec, LlmUnavailable
from .guard import build_prompt


class _Draft(BaseModel):
    text: str
    confidence: float = Field(ge=0.0, le=1.0)


class _Label(BaseModel):
    label: str
    confidence: float = Field(ge=0.0, le=1.0)


class GatewayLlm:
    """UC01's LlmClient over the central gateway. The guard still runs first."""

    def __init__(self, gateway: GatewayClient, tasks: dict[str, LlmTaskSpec], persona: str) -> None:
        self._gateway, self._tasks, self._persona = gateway, dict(tasks), persona

    def complete(self, request: LlmRequest) -> LlmResponse:
        spec = self._tasks.get(request.task)
        if spec is None:
            raise LlmUnavailable(f"no task spec loaded for {request.task!r}")
        prompt = build_prompt(spec, request.inputs)  # raises LlmInputRefused before any model call
        draft = spec.authority == LlmAuthority.DRAFT_FOR_HUMAN
        tier = ModelTier.WORKHORSE if draft else ModelTier.FAST
        try:
            out = self._gateway.structured(task=f"uc01.{spec.task}", tier=tier, system=self._persona,
                                           user=prompt, schema=_Draft if draft else _Label, max_tokens=800)
        except LLMError as exc:
            raise LlmUnavailable(type(exc).__name__) from None  # callers take the deterministic path
        if not draft and out.label not in spec.output_values:
            raise LlmUnavailable("label outside the task's declared output_values")
        return LlmResponse(task=spec.task, task_version=spec.version, text=out.text if draft else None,
                           label=None if draft else out.label, confidence=out.confidence,
                           model=self._gateway.model_for(tier))
```

```python
# src/sub_agent/api/container.py (replaces the FakeLlm line)
from hr_onboarding_common.config import get_settings
from hr_onboarding_common.llm import ONLINE, build_llm, llm_mode
from ..llm.brain import load_persona
from ..llm.gateway_client import GatewayLlm

llm = None
if settings.llm_enabled:
    tasks = load_all_tasks()
    llm = GatewayLlm(build_llm(get_settings()), tasks, load_persona()) if llm_mode() == ONLINE else FakeLlm(tasks)
```

**Imports and dependencies:** `hr-onboarding-common` (path dependency) and `litellm` in
`pyproject.toml`; nothing else.

**Configuration:**
- `UC01_LLM_ENABLED=true` and `LLM_MODE=online` (inherited from the orchestrator);
- tiers WORKHORSE (drafts) and FAST (`intake_intent`) from `HR-Onboarding-Common/.env`;
- orchestrator side: UC01 LLM access switched on (§0).

**Tests**
- A secret or undeclared input is refused before the gateway is called; assert 0 gateway calls with
  a `FakeLLM`.
- A gateway `LLMError` becomes `LlmUnavailable`. Each caller still audits `LLM_UNAVAILABLE` and
  completes deterministically.
- A label outside `output_values` is refused.
- `LLM_MODE=offline` builds `FakeLlm`; `online` builds `GatewayLlm`.
- No module imports a provider SDK or `litellm`.

**Acceptance criteria**
- With `UC01_LLM_ENABLED=false` nothing changes.
- With it on, every model call goes through `GatewayLlm` → `LiteLLMGateway`, and the guard always
  runs first.
- A model outage never blocks a case.
- The UC01 suite passes offline.

---

## 2. UC02 — Onboarding Task Management

**Affected files**
- `src/onboarding_task_management/agent/advisor.py`:
  - **line 1** (`SyntaxError`);
  - `default_tier()` (~297), `input_block()` (~305), `llm_configured()` (~421), `build_advisor()`
    (~453);
  - the module docstring's references to `hr_recruitment_common`.
- `src/onboarding_task_management/sub_agent/adapter.py`: `UC02SubAgent.__init__` (~168), `run()` (~194–213)
- `src/onboarding_task_management/agent/harness.py`: optional (it binds UC02 to the
  `hr_recruitment_common` harness)
- `pyproject.toml`

**Existing implementation**
- `LiteLLMAdvisor` makes one call: task `uc02.advise`, tier WORKHORSE, schema `AgentAdvice`.
- `advise_with_fallback` degrades to the deterministic `OfflineAdvisor` on any gateway failure and
  records `degraded=True` with a `degraded_reason`. Model recommendations that are not allowed are
  replaced and marked degraded (the validation around lines 370–410). These guards must stay.
- `FakeLLMAdvisor` / `FakeLLMClient` are used for tests.

**Missing or incorrect integration**
1. `advisor.py` line 1 starts with a stray `UC` before the docstring: `SyntaxError: invalid syntax`.
   The whole `agent` package, including `UC02SubAgent`, cannot be imported. This blocks the
   orchestrator's real task planning (`UC02_PACKAGE_UNIMPORTABLE`).
2. The real advisor imports `hr_recruitment_common.llm.gateway.LiteLLMGateway`. That is a different
   gateway, configured separately, and **not installed in any environment of this workspace**, so the
   real path can never be taken.
3. `llm_configured()` looks only for `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `AZURE_API_KEY` and
   `OPENAI_API_KEY`. With OpenRouter it always reports "not configured".
4. `UC02SubAgent.run()` always builds the fake advisor (`make_fake_llm(request.llm_behaviour)`).
   Callers have no way to pass a real client.

**Proposed changes**

```python
# advisor.py, line 1: delete the two characters "UC" so the file starts with the docstring's """.

# advisor.py: use the onboarding system's gateway
def default_tier() -> object:
    from hr_onboarding_common.llm import ModelTier
    return ModelTier.WORKHORSE


def input_block(payload: dict) -> str:
    from hr_onboarding_common.llm import input_block as common_input_block
    return common_input_block(payload)


def llm_configured() -> bool:
    if os.environ.get("UC02_LLM_OFFLINE", "0") == "1":
        return False
    from hr_onboarding_common.llm import ONLINE, llm_mode
    return llm_mode() == ONLINE  # key and tiers are validated by build_llm, which fails clearly


def build_advisor(client: object | None = None, *, fake: bool | None = None) -> Advisor:
    if client is not None:
        return LiteLLMAdvisor(client)
    if fake or (fake is None and fake_llm_requested()):
        from onboarding_task_management.agent.fake_llm import build_fake_advisor
        return build_fake_advisor()
    if not llm_configured():
        return OfflineAdvisor()
    from hr_onboarding_common.config import get_settings
    from hr_onboarding_common.llm import build_llm
    return LiteLLMAdvisor(build_llm(get_settings()))
```

```python
# sub_agent/adapter.py: let the caller pass the gateway (keep the fake as the default)
class UC02SubAgent:
    def __init__(self, service=None, outlook=None, llm_client: object | None = None) -> None:
        self.service = service or OnboardingService(build_registry())
        self.outlook = outlook or make_mock_outlook()
        self.llm_client = llm_client

    def run(self, request: WorkflowRequest) -> WorkflowResult:
        ...
        advisor = (LiteLLMAdvisor(self.llm_client) if self.llm_client is not None
                   else make_fake_llm(request.llm_behaviour))
        agent = UC02Agent(self.service, actor, advisor=advisor)
```

**Imports and dependencies:**
- replace `hr_recruitment_common` with `hr_onboarding_common` (path dependency) in the advisor and
  in `pyproject.toml`;
- `litellm` comes with the gateway;
- for `harness.py`, the onboarding equivalents are `hr_onboarding_common.platform.policy.PolicyStore`
  and `hr_onboarding_common.platform.tools.ToolGateway`.

**Configuration:** tier WORKHORSE for `uc02.advise`; no UC02-specific key. Orchestrator side: once
`UC02SubAgent` accepts `llm_client`, the HR-Onboarding-Common team will pass the orchestrator's
`AgentContext.llm` in `adapters/uc02_tasks.call_uc02_plan_tasks`. That adapter already requests the
valid fake behaviour `scripted` (`UC02_LLM_BEHAVIOUR`).

**Tests**
- `python -c "from onboarding_task_management.sub_agent.adapter import UC02SubAgent"` succeeds.
- `build_advisor` with `LLM_MODE=online` returns `LiteLLMAdvisor` over the common gateway. With an
  injected `completion_fn`, the call uses task `uc02.advise` and the WORKHORSE model.
- A gateway failure gives `degraded=True` with a redacted `degraded_reason` (no key in it).
- A recommendation outside the allowed set is still replaced (unchanged behaviour).
- `UC02SubAgent(llm_client=FakeLLM...)` uses that client; without it, the fake advisor as before.
- No `hr_recruitment_common` import remains in the agent package.

**Acceptance criteria**
- The package imports.
- Online, every UC02 model call goes through `hr_onboarding_common`'s gateway; offline and test
  behaviour is unchanged.
- Task-planning rules, approval gates and the degraded indicator are unchanged.
- The orchestrator's `plan_tasks` stops failing with `UC02_PACKAGE_UNIMPORTABLE`.

---

## 3. UC03 — Document Validation

**Affected files**
- `src/sub_agent/comms.py`: `draft_communication`, `_llm_draft`
- `src/sub_agent/name_agent.py`: `check_name_consistency_agent`, `_llm_name_check`, `_call_gemini`,
  `_call_claude`
- `src/sub_agent/extraction.py`: `HybridDocumentExtractor` (`_MODEL`, `__init__(client=...)`,
  `_call_llm`), `get_extractor`
- `src/sub_agent/validator.py`: the call sites (~202, ~301)
- `src/sub_agent/workflow.py`: `build_graph(..., extractor=...)`
- `pyproject.toml`: `anthropic`, `google-genai`

**Existing implementation (direct provider SDK calls)**

| Function | SDK and model (hard-coded) | Key read from | Fallback without a model |
|---|---|---|---|
| `comms._llm_draft` | `anthropic.Anthropic().messages.create`, `claude-haiku-4-5-20251001`, 600 tokens | `ANTHROPIC_API_KEY` | `_template_draft` |
| `name_agent._call_gemini` | `google.genai.Client().models.generate_content`, `gemini-2.0-flash`, JSON | `GEMINI_API_KEY` | next provider |
| `name_agent._call_claude` | `anthropic`, `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` | conservative `NAME_MISMATCH`, routed to HR |
| `HybridDocumentExtractor._call_llm` | `google.genai`, `gemini-2.0-flash` (`_MODEL`) | `GEMINI_API_KEY` (or an injected client) | the local extraction result |

Today these paths are dormant in the orchestrator. The SDKs aren't installed in its environment, and
only `OPENROUTER_API_KEY` is configured, so UC03 always takes its conservative fallbacks. They become
active as soon as someone sets `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` in an environment with those
SDKs. That would bypass the gateway entirely: no OpenRouter, no retries, no redaction, no tier
configuration.

**Missing integration:** all four call sites. Each must call `LLMClient.structured` instead.

**Proposed changes.** Inject the client, keep every fallback, validate with Pydantic. Note that the
gateway is synchronous; call it with `asyncio.to_thread` from UC03's async code.

```python
# name_agent.py
import asyncio
from pydantic import BaseModel, Field
from hr_onboarding_common.llm import LLMClient, LLMError, ModelTier, input_block


class _NameVerdict(BaseModel):
    is_same_person: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


async def _llm_name_check(name_from_form: str, extracted_name: str, llm: LLMClient | None) -> NameCheckResult:
    if llm is not None:
        try:
            v = await asyncio.to_thread(
                llm.structured, task="uc03.name_check", tier=ModelTier.FAST, system=_SYSTEM_PROMPT,
                user=input_block({"form_name": name_from_form, "document_name": extracted_name}),
                schema=_NameVerdict, max_tokens=200)
            return NameCheckResult(is_consistent=v.is_same_person, confidence=v.confidence,
                                   reasoning=v.reasoning, used_llm=True,
                                   finding=None if v.is_same_person else ValidationFinding.NAME_MISMATCH)
        except LLMError:
            pass  # fall through to the conservative result
    return NameCheckResult(is_consistent=False, confidence=0.0, finding=ValidationFinding.NAME_MISMATCH,
                           reasoning="No LLM available; name variation could not be verified -- routed to HR.",
                           used_llm=False)
```

```python
# comms.py
class _Draft(BaseModel):
    body: str

async def draft_communication(action, employee_name, document_label, outstanding_labels,
                              llm: LLMClient | None = None) -> str:
    items = outstanding_labels or [document_label]
    if llm is not None:
        try:
            draft = await asyncio.to_thread(
                llm.structured, task="uc03.draft_communication", tier=ModelTier.WORKHORSE,
                system=_PROMPT_PATH.read_text(encoding="utf-8") if _PROMPT_PATH.exists() else "",
                user=input_block({"employee_name_form": employee_name, "action": action, "outstanding_items": items}),
                schema=_Draft, max_tokens=600)
            if draft.body.strip():
                return draft.body
        except LLMError:
            pass
    return _template_draft(employee_name, items)
```

```python
# extraction.py: HybridDocumentExtractor takes the common client instead of a Gemini client
class _Extraction(BaseModel):
    extracted_name: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    injection_suspected: bool = False
    language: str | None = None
    supported_language: bool = True
    fields: dict[str, str | None] = Field(default_factory=dict)

def __init__(self, max_text: int = MAX_TEXT_FOR_LLM, llm: LLMClient | None = None) -> None: ...

async def _call_llm(self, text: str, document_type: str) -> dict[str, Any]:
    if self._llm is None:
        return {}  # caller uses the local result, as today
    try:
        out = await asyncio.to_thread(
            self._llm.structured, task="uc03.extract_fields", tier=ModelTier.FAST,
            system=self._system_prompt or "", schema=_Extraction, max_tokens=800,
            user=input_block({"document_type": document_type}) + "\n<document_text>\n" + text + "\n</document_text>")
        return {**out.fields, **out.model_dump(exclude={"fields"})}
    except LLMError:
        return {}
```

Thread `llm` through:
- `validator.py`: pass `llm` to `check_name_consistency_agent` and `draft_communication`;
- `workflow.build_graph(..., llm=None)`: build `HybridDocumentExtractor(llm=llm)`;
- delete the `os.environ.get("…_API_KEY")` reads and the `anthropic` / `google.genai` imports;
- remove both SDKs from `pyproject.toml` if nothing else uses them.

**Imports and dependencies:** `hr_onboarding_common.llm` only. The orchestrator already provides
the gateway and LiteLLM.

**Configuration:**
- tiers FAST (`uc03.name_check`, `uc03.extract_fields`) and WORKHORSE (`uc03.draft_communication`);
- no UC03-specific keys; `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` are no longer read.

Orchestrator side: the HR-Onboarding-Common team will pass `deps.ctx.llm` into UC03 in
`adapters/uc03_documents.py` once these parameters exist.

**Must not change**
- The name check still routes to HR whenever the model is unavailable, unsure, or says "not the
  same person".
- Low extraction confidence or `injection_suspected` keep their current handling.
- No validation rule is relaxed because a model call failed.
- The document scope is unchanged, and NDA/policy signature tracking stays excluded.

**Tests**
- Each of the three functions with `llm=None` returns exactly today's fallback.
- With `FakeLLM` handlers for `uc03.name_check`, `uc03.extract_fields` and
  `uc03.draft_communication`, the results map as before.
- A gateway `LLMError` → the conservative fallback (HR review / template / local extraction).
- An invalid model output (schema mismatch) → the same fallback.
- No module imports `anthropic` or `google.genai`; no module reads `*_API_KEY`.

**Acceptance criteria**
- No direct provider SDK call remains in UC03.
- Online, all UC03 model calls go through the gateway on the configured tiers.
- Offline and test results are identical to today's.
- Every conservative fallback is preserved.

---

## 4. UC04 — Equipment and Access Integration Readiness

**Affected files (only if you choose to add a model):** `src/sub_agent/advisor.py` (`FakeLlmAdvisor`),
`src/sub_agent/agent.py` (`Uc04Agent.__init__(advisor=...)`), `pyproject.toml`.

**Existing implementation:** a deterministic, read-only `FakeLlmAdvisor.generate_advisory(result,
input_data) -> AdvisoryReport`, carrying the simulated-advisory banner. It cannot approve, decide or
change state. There are no model calls and no provider SDKs.

**Gateway integration:** nothing to migrate. UC04 has no production LLM calls, so it already meets
the requirement that all production calls go through the gateway.

**Optional: a model-written advisory.** If you want one, keep the contract and fall back to the fake:

```python
from hr_onboarding_common.llm import LLMClient, LLMError, ModelTier, input_block


class _AdvisoryText(BaseModel):
    status_explanation: str
    approval_guidance: str
    risk_analysis: str
    readiness_summary: str
    action_recommendations: list[str] = Field(default_factory=list)


class GatewayAdvisor(FakeLlmAdvisor):
    """Same contract; the explanatory text comes from the gateway, the decision from code only."""

    def __init__(self, llm: LLMClient, **kw) -> None:
        super().__init__(**kw)
        self._llm = llm

    def generate_advisory(self, result, input_data=None) -> AdvisoryReport:
        base = super().generate_advisory(result, input_data)  # deterministic facts and banner
        if result is None:
            return base
        try:
            text = self._llm.structured(task="uc04.advisory", tier=ModelTier.WORKHORSE, system=ADVISORY_PREAMBLE,
                                        user=input_block(base.model_dump(include={"case_id", "overall_status"})),
                                        schema=_AdvisoryText, max_tokens=1200)
        except LLMError:
            return base
        return base.model_copy(update={**text.model_dump(), "is_simulated": False})
```

The banner, the read-only guarantee and `overall_status` come from code, never from the model.

**Dependencies and configuration:** only if adopted.
- `hr-onboarding-common` and `litellm` in `pyproject.toml`;
- tier WORKHORSE;
- orchestrator side: UC04 LLM access switched on (§0);
- the orchestrator's UC04 handler (`uc_worker_runtime._Uc04Handler`) would construct
  `Uc04Agent(advisor=GatewayAdvisor(build_llm(get_settings())))` when `LLM_MODE=online`.

**Tests:**
- readiness, authorization and connector rules are byte-for-byte unchanged with and without the
  gateway advisor;
- an `LLMError` returns the deterministic report;
- the model cannot change `overall_status` or the banner.

**Acceptance criteria:** deterministic readiness and authorization rules unchanged; the advisory stays
advisory; the offline UC04 suite passes unchanged.

---

## 5. After a UC lands its change

Tell the HR-Onboarding-Common team. On their side:
- switch on that worker's LLM access (UC01/UC04);
- pass `AgentContext.llm` into the in-process UC (UC02 `call_uc02_plan_tasks`, UC03
  `call_uc03_validate_documents`);
- extend `tests/llm/test_provider_boundary.py` to cover your source tree;
- run `scripts/openrouter_smoke_test.py --tier all` with the shared configuration (add
  `--multi-turn` if your UC uses `LLMConversation`).
