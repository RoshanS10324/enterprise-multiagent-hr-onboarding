# OpenRouter DeepSeek V4.1 Flash Integration Report

**Date:** 2026-11-02 · **Model:** `deepseek/deepseek-v4.1-flash` on OpenRouter
(`https://openrouter.ai/api/v1`), with reasoning, for every tier of the orchestrator and UC05.
**Boundary:** changes only in HR-Onboarding-Common and UC05. UC01–UC04 were inspected, not modified
(§9). `hr-recruitment-common` was not touched.

Related:
- how to configure and run it: [OPENROUTER_INTEGRATION_GUIDE.md](OPENROUTER_INTEGRATION_GUIDE.md);
- the earlier OpenRouter step: [OPENROUTER_INTEGRATION_REPORT.md](OPENROUTER_INTEGRATION_REPORT.md);
- what UC01–UC04 owners need to do: [UC_LLM_GATEWAY_MIGRATION_GUIDE.md](UC_LLM_GATEWAY_MIGRATION_GUIDE.md).

## Definition of done

| Item | Status | Where |
|---|---|---|
| DeepSeek V4.1 Flash is the common model | done | `.env.example`; §2, §3 |
| All three tiers use it | done | `OPENROUTER_MODEL` / `LLM_MODEL_*`; tests in §12 |
| OpenRouter only through the central `LiteLLMGateway` | done | §4 |
| The orchestrator receives the gateway | done | `bootstrap.build_deps`; `test_llm_wiring.py` |
| UC05 uses the gateway online | done | `sub_agent/runtime.build_context`; §7, §8 |
| UC05 supports the reasoning configuration | done | central in the gateway; UC05 tests in §12 |
| Multi-turn reasoning metadata preserved | done (new `LLMConversation`); no agent is multi-turn today | §6 |
| FakeLLM still works offline | done | default `LLM_MODE=offline` in tests; §12 |
| UC01–UC04 inspected and unchanged | done | checksums and environments, §9 |
| Missing UC01–UC04 integrations documented | done | §9, §10, migration guide |
| API key secure | done, with one finding: a key-shaped value was in `.env.example` and was removed | §2, §14 |
| Automated tests pass | done: common 553/553; UC05 544/545, the one failure pre-existing and unrelated | §12, §14 |
| Live smoke test documented | done; **not run here** (no key, by design) | §13 |
| Existing workflows and guardrails functional | done: all workflow suites pass unchanged | §12 |

---

## 1. Existing LLM architecture

What was in place before this change (from the earlier OpenRouter step):

| Piece | File | Role |
|---|---|---|
| `LLMClient`, `BaseLLM`, `FakeLLM` | `llm/base.py` | the one interface: `structured(task, tier, system, user, schema, max_tokens)` returns a validated Pydantic object. `FakeLLM` answers offline |
| `LiteLLMGateway` | `llm/gateway.py` | the only code that calls a model. Bounded retries (tenacity), fallback chains, final refusals, Pydantic validation, events, redacted errors |
| `build_llm`, `validate_online_config` | `llm/factory.py` | `LLM_MODE=offline` → the caller's offline LLM; `online` → the gateway, after a startup check |
| `load_environment` | `config.py` | one `.env` (`HR-Onboarding-Common/.env`); real environment variables win; its path is exported so workers read the same file |
| Orchestrator wiring | `orchestration/bootstrap.py` | `build_deps` → `build_llm` → `AgentContext.llm` |
| Worker credentials | `orchestration/workers/config.py` | only workers with LLM access (UC05) receive the provider key |
| UC05 | `sub_agent/runtime.py` | `build_llm(settings, offline=offline_llm)`, wrapped in `SanitizedLLM` |

What that did not cover for this model, found during inspection:

1. **Model ids.** The configured string went to LiteLLM unchanged and had to be written as
   `openrouter/<provider>/<model>`. The bare OpenRouter id `deepseek/deepseek-v4.1-flash` would have
   been sent to LiteLLM's own `deepseek` provider, i.e. DeepSeek's API with a different key.
2. **No reasoning parameter, no base-URL setting, no provider-routing constraint.**
3. **Single-turn only.** Nowhere to keep `reasoning_details` between turns.
4. **LiteLLM's own debug logs** carry whole request and response bodies: prompts, answers and
   reasoning text. They appeared as soon as the application logged at `DEBUG` (found while testing;
   fixed, §4).

## 2. OpenRouter configuration

All settings live in the one `.env`, `HR-Onboarding-Common/.env`; the template is
[`.env.example`](../.env.example). It holds exactly the configuration requested:

```env
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=deepseek/deepseek-v4.1-flash
LLM_MODEL_FRONTIER=deepseek/deepseek-v4.1-flash
LLM_MODEL_WORKHORSE=deepseek/deepseek-v4.1-flash
LLM_MODEL_FAST=deepseek/deepseek-v4.1-flash
LLM_MODE=online
LLM_REASONING_ENABLED=true
```

| Variable | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | none | read only from the environment, by LiteLLM. Never printed, logged or stored by our code |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | sent as LiteLLM `api_base`. Must be `https://`, without credentials, query or fragment |
| `OPENROUTER_MODEL` | none | OpenRouter model id for every tier whose `LLM_MODEL_*` is unset |
| `LLM_MODEL_FRONTIER` / `_WORKHORSE` / `_FAST` | `OPENROUTER_MODEL` | per-tier override; changing the model never touches agent code |
| `LLM_REASONING_ENABLED` | `true` | §5 |
| `LLM_REASONING_EFFORT` | unset | optional effort, §5 |
| `LLM_REASONING_TOKEN_HEADROOM` | `2048` | §5 |
| `OPENROUTER_REQUIRE_PARAMETERS` | `true` | §3, §4 |
| `LLM_FALLBACKS_*` | unset | none configured. No second model has been verified, so none is set (§14) |
| `LLM_PROVIDER` | `openrouter` | `litellm` keeps the earlier behaviour: models are LiteLLM routes, each prefix with its own key |

**Startup validation** (`validate_online_config`, `llm/factory.py`). In online mode, startup stops
with one message listing every problem. It checks:
- every tier has a model;
- every model and explicit fallback is a `<provider>/<model>` id;
- no placeholder is left;
- the key is present, without quotes or whitespace;
- the base URL is valid (rules above);
- the flags and effort have known values;
- an effort is not set while reasoning is off, and the headroom is not negative;
- reasoning is not requested for a route outside OpenRouter;
- timeout, attempts and backoff are valid.

Messages name variables, never values. The check runs:
- in the orchestrator (`build_deps`), before any worker starts;
- in UC05 (`build_context`), raised as `RuntimeConfigurationError`;
- in the UC05 worker, on its first request.

Offline mode needs no key.

**Precedence.** A real environment variable always overrides `.env` (`load_dotenv(override=False)`).
Workers inherit `HR_ONBOARDING_ENV_FILE` and load the same file.

**`.gitignore`.** `.env` and `.env.*` are ignored in HR-Onboarding-Common and in UC05;
`!.env.example` keeps the example tracked.

**Security finding.** At the start of this work, `HR-Onboarding-Common/.env.example` contained a
value shaped like a real OpenRouter key (`sk-or-v1-` followed by 64 hex characters) instead of a
placeholder. That file is deliberately *not* git-ignored, so it would have been committed. It was
replaced by an empty `OPENROUTER_API_KEY=`, and the copy in this session's local scratch snapshot was
scrubbed. The value is not reproduced anywhere. No other file in the workspace contains it, and
neither `HR-Onboarding-Common/.env` nor `UC05_Orientation-Schedule-Agent/.env` exists yet.
**If that was a live key, rotate it in OpenRouter** and put the new one in `.env` only.

## 3. Selected DeepSeek model and identifier mapping

**Catalog facts**, from OpenRouter's public `GET /api/v1/models` and the model's `/endpoints`
listing, retrieved 2026-11-02. Nothing below is assumed beyond what the catalog states:

| Property | Value |
|---|---|
| Id / canonical slug | `deepseek/deepseek-v4.1-flash` / `deepseek/deepseek-v4.1-flash-20260910` |
| Context length | 1,048,576 tokens; top provider's max completion 943,718 |
| Modalities | text + image → text |
| Supported parameters (model level) | include `response_format`, `structured_outputs`, `reasoning`, `reasoning_effort`, `include_reasoning`, `max_tokens`, `tools`, `tool_choice`, `seed`, `temperature` |
| Reasoning | `mandatory: false`, `default_enabled: true`, `supported_efforts: [max, high, low, none]`, `default_effort: high` |
| Price (catalog) | input $0.045 / M tokens, output $0.50 / M tokens, cache read $0.006 / M |
| Endpoints | 40 providers. **33** list `response_format`, `structured_outputs` and `reasoning`. **7** do not: StreamLake, GMICloud, Novita, Phala, SiliconFlow and Alibaba lack `structured_outputs`; BaseTen lacks `response_format` |

What the application needs from the model is JSON-schema structured output on every call, with
reasoning. Tool calling is not used by any task, so the `tools` support is irrelevant here. Because
seven endpoints cannot honour the schema, the gateway sends `provider.require_parameters: true`
(§4). OpenRouter then routes only to the 33 providers that support every parameter in the request.
Without it, a request could land on a provider that ignores `response_format`. That would show up
as `LLMOutputError` retries.

**Identifier mapping** (`llm/routing.py`, the only place that knows both):

| Configured (OpenRouter model id) | LiteLLM route (what the gateway calls) | Sent to OpenRouter as `model` |
|---|---|---|
| `deepseek/deepseek-v4.1-flash` | `openrouter/deepseek/deepseek-v4.1-flash` | `deepseek/deepseek-v4.1-flash` |
| `openrouter/deepseek/deepseek-v4.1-flash` (prefix accepted) | same | same |
| `openrouter/auto` (OpenRouter's own id: one slash, kept) | `openrouter/openrouter/auto` | `openrouter/auto` |

Status reports keep the two apart. The UC05 worker's `__llm_status__` and the smoke test print
`models` (ids) and `routes` (LiteLLM) separately, via `llm_summary`.

## 4. LiteLLM gateway integration

Installed versions: LiteLLM **1.106.4** (HR-Onboarding-Common `.venv`, Python 3.13) and **1.106.2**
(UC05's environment, Python 3.12). Both were verified with the real LiteLLM and only HTTP replaced
(`httpx.MockTransport`).

What one call sends (`LiteLLMGateway._request`):

| Request field | Value |
|---|---|
| `model` | the LiteLLM route, e.g. `openrouter/deepseek/deepseek-v4.1-flash` |
| `api_base` | `OPENROUTER_BASE_URL` (OpenRouter routes only) |
| `messages` | system, then any earlier turns of an `LLMConversation` (§6), then user |
| `response_format` | the Pydantic schema; LiteLLM turns it into `{"type": "json_schema", ...}` |
| `max_tokens` | the caller's budget, plus `LLM_REASONING_TOKEN_HEADROOM` while reasoning is on |
| `extra_body.provider` | `{"require_parameters": true}` unless `OPENROUTER_REQUIRE_PARAMETERS=false` |
| `extra_body.reasoning` | §5 |
| `num_retries`, `max_retries` | `0`: the gateway is the only retry layer |

Verified on the wire, in both environments:
- the URL `https://openrouter.ai/api/v1/chat/completions`, or a configured base URL;
- `Authorization: Bearer <key from the environment>`;
- `model` without the route prefix;
- `reasoning` and `provider` forwarded unchanged inside the JSON body;
- the `json_schema` response format;
- the raised `max_tokens`;
- a 429 retried by the gateway alone, one HTTP request per attempt.

`provider` and `reasoning` travel in LiteLLM's `extra_body`, the equivalent of the OpenAI-client
example. No OpenAI or OpenRouter client is created anywhere. `litellm.drop_params=True` does not
affect `extra_body`.

Unchanged from before: bounded exponential backoff with jitter on transient errors (rate limit,
connection, timeout, 5xx, bad gateway, schema-invalid output); fallback chains of explicitly
configured models only; refusals final; Pydantic validation; failures through `redact` and raised
`from None`.

New in the gateway:
- **events** carry `reasoning_tokens`, a count only;
- **reasoning that consumes the whole budget** (`finish_reason=length`, no content, reasoning tokens
  reported) gives a specific `LLMError` naming `LLM_REASONING_TOKEN_HEADROOM` and
  `LLM_REASONING_EFFORT`;
- **LiteLLM's own loggers** (`LiteLLM`, `LiteLLM Router`, `LiteLLM Proxy`) are set to `CRITICAL` when
  the gateway loads LiteLLM, whatever the application's log level (§1, point 4). Before this, a
  root logger at `DEBUG` printed prompts and reasoning text from LiteLLM. The key itself never
  appeared, since LiteLLM does not log the authorization header. A test now runs the real LiteLLM at
  `DEBUG` and asserts that neither prompts nor reasoning text nor the key reach the logs.

**Cost.** LiteLLM's local price map has no entry for this model, so the gateway reports
`cost_usd=None` for it. The catalog prices are in §3.

## 5. Reasoning configuration and compatibility

| Setting | Sent on OpenRouter routes |
|---|---|
| `LLM_REASONING_ENABLED=true` (default) | `reasoning: {"enabled": true}` |
| plus `LLM_REASONING_EFFORT=<e>` | `reasoning: {"effort": "<e>"}` instead |
| single-turn call (every call today) | also `"exclude": true` |
| `LLM_REASONING_ENABLED=false` | `reasoning: {"enabled": false}` |

- **`exclude` on single-turn calls.** The model still reasons, but OpenRouter does not return the
  reasoning text. Nothing to hide ever reaches the application. Multi-turn calls omit it (§6).
- **Off is explicit.** The catalog says this model reasons by default (`default_enabled: true`).
  OpenRouter's documentation describes `default_enabled` as the state "when the user has not set
  `reasoning.enabled`". Leaving the field out would therefore not turn reasoning off; the gateway
  sends `enabled: false`. For a model whose catalog entry says reasoning is `mandatory`, keep it on.
- **Efforts.** Validation accepts OpenRouter's values: `max`, `xhigh`, `high`, `medium`, `low`,
  `minimal`, `none`. Which ones a model takes is in its catalog entry; for this model, `max`, `high`,
  `low` and `none`. The documentation says `enabled: true` uses a "medium" default, while this
  model's catalog lists `default_effort: high` and has no `medium`. Which one applies is not stated,
  and was not tested here. Set `LLM_REASONING_EFFORT` to pin it.
- **Token budget.** OpenRouter counts reasoning tokens against `max_tokens`. UC05's welcome note asks
  for 300 tokens and its policy Q&A for 4000. With reasoning on, the gateway sends 300 + 2048 and
  4000 + 2048, so reasoning cannot use up the answer's budget. Callers keep stating only what their
  answer needs. If reasoning still runs out of budget, the error says so (§4).
- **Reasoning on other routes.** With `LLM_PROVIDER=litellm` and a direct provider route, no
  OpenRouter field is sent. Asking for reasoning there explicitly is refused at startup rather than
  silently dropped.
- **Compatibility verified offline:**
  - LiteLLM forwards `reasoning` unchanged;
  - it exposes the reasoning text as `message.reasoning_content`;
  - it exposes `reasoning_details` in `message.provider_specific_fields`;
  - it reports `usage.completion_tokens_details.reasoning_tokens`;
  - it forwards an assistant message's `reasoning_details` unchanged.

  **Not verifiable without a key:** that OpenRouter's providers for this model honour
  `json_schema` and reasoning together, and the exact shape of the `reasoning_details` they
  return. The smoke test (§13) checks both.

**Never exposed.** The gateway returns only the validated schema object. Events and logs carry
counts, never reasoning text. UC05's `SanitizedLLM` replaces every error message with "the language
model is unavailable". Tests feed reasoning text and `reasoning_details` through the gateway, UC05
and the smoke test, and assert that none of it reaches a result, an event, a log, an error or the
smoke test's output.

## 6. Multi-turn reasoning metadata handling

Inspection result: the earlier message abstraction was a single `system` + `user` pair. It could not
carry an assistant turn, much less its `reasoning_details`. The extension lives only in the common
package:

- **`LLMConversation`** (`llm/conversation.py`):
  - created with a non-empty scope, e.g. `(client_id, case_id, session_id)`;
  - keeps each successful turn: the user message, and the assistant content with its
    `reasoning_details` deep-copied exactly as returned (never edited, merged, reconstructed or
    invented; an answer without them is stored without them);
  - `history()` returns a copy.
- **Gateway** (`structured(..., conversation=, scope=)`):
  - checks the scope first, and raises `ConversationScopeError` before anything is sent if it
    differs, so one employee's, tenant's or case's history cannot travel with another's request;
  - sends the history, omits `exclude`;
  - reads `reasoning_details` from the answer and records the turn only if it validated.
- **`FakeLLM`** honours the same contract offline, without metadata. `SanitizedLLM` in UC05 passes
  both arguments through.
- **Isolation by construction.** A conversation lives in the memory of the run that created it. It
  is never written to case state, checkpoints or audit, never logged (its repr shows scope and turn
  count only) and never returned by an API. Its metadata leaves it only to go back to the provider.

**Current use.** No agent is multi-turn today: every UC05 call (and every call proposed for UC01–UC04)
is single-turn, so all of them send `exclude: true`. The extension is ready for the first agent that
needs it.

**Limitations:**
- A conversation cannot survive a process restart or a LangGraph interrupt/resume, because persisting
  provider reasoning metadata would need its own security design. That is a decision for when such an
  agent exists (§14).
- With fallbacks configured, a turn answered by a fallback model keeps *that* model's
  `reasoning_details`, and the next turn starts again at the primary model. Whether a provider accepts
  another model's reasoning metadata is not documented for this model. A multi-turn agent should use
  a tier without fallbacks, which is the current configuration.

## 7. UC01–UC05 status

| UC | Production model calls | Through the gateway | DeepSeek + reasoning | Status |
|---|---|---|---|---|
| UC01 Employee Record Creation | none: deterministic `FakeLlm`, `UC01_LLM_ENABLED=false` | n/a; worker started without the key | n/a | no model use; optional migration, guide §1 |
| UC02 Onboarding Task Management | none reachable: `UC02SubAgent.run` always uses its fake advisor; `advisor.py:1` is a `SyntaxError`; the real advisor imports `hr_recruitment_common` (not installed) | **missing / incorrect** | n/a until migrated | guide §2 |
| UC03 Document Validation | **direct SDK calls**: Anthropic in `comms.py`; Gemini and Anthropic in `name_agent.py`; Gemini in `extraction.py`. Model ids hard-coded | **missing**: bypasses the gateway | not applied: these calls would ignore the central model and need their own keys. Not active here (SDKs not installed in the orchestrator env, keys not configured), so UC03 runs its template and HR-review fallbacks | guide §3 |
| UC04 Equipment and Access Readiness | none: deterministic `FakeLlmAdvisor` | n/a; worker started without the key | n/a | none required; optional, guide §4 |
| UC05 Orientation Scheduling | `onboarding.policy_qa` (WORKHORSE, 4000 tokens) and `onboarding.welcome_note` (FAST, 300) | **yes**: `build_context` → `build_llm` → `LiteLLMGateway` → `SanitizedLLM`, for its API, scheduler, evaluation and orchestrator worker | **yes**: every tier `deepseek/deepseek-v4.1-flash`, reasoning on, verified offline (§12) | integrated |

UC05's entry points all build the LLM in one place, `sub_agent/runtime.build_context`. Its API and
joining-day scheduler call it directly; the orchestrator's UC05 worker calls the same function; its
evaluation runner uses the same `build_llm`. None initializes a provider SDK. The worker's
`__llm_status__` now reports the provider, base URL, routes and reasoning settings.

## 8. UC05 files modified

In this step:

| File | Change |
|---|---|
| `src/sub_agent/runtime.py` | `SanitizedLLM.structured` accepts and passes `conversation` and `scope`; docstring names the central DeepSeek configuration. No business logic touched |
| `.env.example` | LLM section: OpenRouter model ids, DeepSeek defaults, reasoning variables |
| `tests/conftest.py` | also clears the model and reasoning variables, so tests start from the defaults |
| `tests/test_llm_runtime.py` | 6 new tests (§12) |

From the earlier OpenRouter step, unchanged now: `src/sub_agent/api.py` and `scheduler.py` load the
common `.env`; `evaluation/run_evals.py` builds its LLM with `build_llm`; `.gitignore`; README
configuration section.

HR-Onboarding-Common files in this step:
- new: `llm/routing.py`, `llm/conversation.py`, `tests/llm/test_gateway_reasoning.py`, this report;
- changed: `config.py`, `llm/base.py`, `llm/callbacks.py`, `llm/gateway.py`, `llm/factory.py`,
  `llm/__init__.py`, `orchestration/workers/uc_worker_runtime.py` (UC05 status), `.env.example`,
  `scripts/openrouter_smoke_test.py`, `tests/conftest.py`,
  `tests/llm/test_llm_configuration.py`, `test_litellm_openrouter_transport.py`,
  `test_llm_wiring.py`, `tests/orchestration/test_llm_worker_configuration.py`;
- docs: `OPENROUTER_INTEGRATION_GUIDE.md`, `UC_LLM_GATEWAY_MIGRATION_GUIDE.md`, and a pointer in
  `OPENROUTER_INTEGRATION_REPORT.md`.

## 9. UC01–UC04 missing implementation

UC01–UC04 were inspected and left untouched:
- **Source, configuration and tests:** all 483 files under the four UC folders match their SHA-256
  checksums from before the OpenRouter work, and no file was added.
- **Installed environments:** `uc01`, `uc02-test`, `uc03-test`, `uc04` and `uc04-test` have the same
  package lists as before.

| UC | Missing or incorrect | Needed for DeepSeek specifically |
|---|---|---|
| UC01 | nothing required; no real model client exists | nothing beyond the optional gateway client |
| UC02 | `advisor.py:1` `SyntaxError`; advisor imports `hr_recruitment_common`; the sub-agent never accepts a real client | nothing extra: once on the gateway it gets the central model |
| UC03 | three direct provider SDK call sites, hard-coded models, own key reads | nothing extra: once on the gateway it gets the central model; budgets stay as proposed (headroom is central) |
| UC04 | nothing required | nothing |

## 10. Migration instructions for UC01–UC04 owners

The exact code, file by file, is in
[UC_LLM_GATEWAY_MIGRATION_GUIDE.md](UC_LLM_GATEWAY_MIGRATION_GUIDE.md). The new §0a covers the
central model. In short:

**Every UC.** Call `llm.structured(task=..., tier=..., system=..., user=input_block(...),
schema=..., max_tokens=...)` on an injected `LLMClient`, and catch `LLMError` for your deterministic
path. Do not:
- name a model, id or route;
- send `reasoning`, `extra_body` or provider routing;
- create a provider client or read a key;
- inflate `max_tokens` for reasoning.

A schema field named `reasoning` is answer text, not the provider's trace; consider calling it
`rationale`. Multi-turn, if ever needed, goes through `LLMConversation` with a case or session scope.

| UC | Steps | Acceptance criteria |
|---|---|---|
| UC01 (optional) | add `src/sub_agent/llm/gateway_client.py` (`GatewayLlm` adapting UC01's `LlmClient` to the gateway); choose it in `src/sub_agent/api/container.py` `build_container` when `UC01_LLM_ENABLED`; add `hr-onboarding-common` and `litellm` to `pyproject.toml`; ask for worker LLM access | off: nothing changes; on: every call through the gateway with UC01's guard first; an outage never blocks a case; offline suite passes |
| UC02 | delete the stray `UC` at `src/onboarding_task_management/agent/advisor.py:1`; import `hr_onboarding_common.llm` instead of `hr_recruitment_common` (`default_tier`, `input_block`, `llm_configured`, `build_advisor`); let `UC02SubAgent.__init__`/`run()` in `sub_agent/adapter.py` accept a client (fake stays the default); update `pyproject.toml` | the package imports; online, every UC02 call goes through the gateway; offline unchanged; planning rules, approval gates and the degraded flag unchanged; `plan_tasks` stops failing with `UC02_PACKAGE_UNIMPORTABLE` |
| UC03 | replace the SDK calls in `src/sub_agent/comms.py` (`_llm_draft`), `name_agent.py` (`_llm_name_check`, `_call_gemini`, `_call_claude`) and `extraction.py` (`HybridDocumentExtractor._call_llm`) with `structured(...)` on FAST (`uc03.name_check`, `uc03.extract_fields`) and WORKHORSE (`uc03.draft_communication`); thread `llm` through `validator.py` and `workflow.build_graph`; remove the SDK imports, key reads and dependencies | no direct SDK call remains; online, all calls through the gateway; offline results identical; every conservative fallback kept (name check routes to HR when unsure or unavailable) |
| UC04 (optional) | a gateway-backed advisor beside `FakeLlmAdvisor` in `src/sub_agent/advisor.py`, injected via `Uc04Agent.__init__(advisor=...)` | deterministic readiness and authorization rules unchanged; the advisory stays advisory; offline suite unchanged |

After each lands, the HR-Onboarding-Common team will:
- switch on worker LLM access (UC01/UC04);
- pass `AgentContext.llm` into UC02/UC03;
- extend the provider-boundary test;
- run the smoke test with `--tier all`.

## 11. Environment setup

1. `HR-Onboarding-Common/.env.example` → copy to `HR-Onboarding-Common/.env` (git-ignored).
2. Put your key after `OPENROUTER_API_KEY=` in `.env`: no quotes, no spaces. Nowhere else.
3. Leave the model and reasoning lines as they are. The example already sets DeepSeek for every tier,
   reasoning on, and `LLM_MODE=online`.
4. Check, then send one request (§13):
   `.venv\Scripts\python.exe scripts\openrouter_smoke_test.py --check-only`, then without the flag.
5. Run the system as usual. The orchestrator loads `.env` and passes its path to every worker. For
   UC05 on its own, set `HR_ONBOARDING_ENV_FILE` to that `.env` (guide §8a).
6. After changing HR-Onboarding-Common, refresh UC05's installed copy (from the workspace root):
   `uv pip install --python "$LOCALAPPDATA/hr-onboarding-envs/uc05/Scripts/python.exe" --reinstall --no-deps ./HR-Onboarding-Common`.

To run without the model, set `LLM_MODE=offline` in `.env`, or `UC05_WORKER_LLM_MODE=offline` for
UC05's worker only.

## 12. Testing commands and results

No test makes a network call. Both `conftest.py` files force `LLM_MODE=offline`, point the `.env` away,
and remove keys and model variables. Online tests set fake values and replace either the completion
function or HTTP.

```bash
cd HR-Onboarding-Common
export OPENBLAS_NUM_THREADS=1
E="$LOCALAPPDATA/hr-onboarding-envs"
export UC01_WORKER_PYTHON="$E/uc01/Scripts/python.exe" UC04_WORKER_PYTHON="$E/uc04/Scripts/python.exe" \
       UC05_WORKER_PYTHON="$E/uc05/Scripts/python.exe"
for f in $(find tests -name "test_*.py" | sort); do .venv/Scripts/python.exe -m pytest "$f" -q -p no:cacheprovider; done
cd ../UC05_Orientation-Schedule-Agent && "$E/uc05/Scripts/python.exe" -m pytest tests -q -p no:cacheprovider
```

(One file per process keeps memory within this machine's limits.)

**Results** (2026-11-02, after all changes; counted from JUnit XML):

| Suite | Tests | Passed | Failed | Skipped |
|---|---|---|---|---|
| HR-Onboarding-Common: 33 files; real UC01, UC04 and UC05 worker processes | **553** | **553** | 0 | 0 |
| UC05_Orientation-Schedule-Agent | **545** | **544** | 1 | 0 |

The one UC05 failure is pre-existing and unrelated (§14, item 7):
`test_status_vocabularies_match_what_the_orchestrator_validates` expects the HITL `Decision` values
without `withdrawn`. Before this step the suites stood at 502 passing (common), and 538 passing plus
that same failure (UC05). The difference is the tests added here; no existing test was removed.

Every onboarding workflow test passes unchanged. That includes:
- the orchestration graph, routing and execution order;
- the UC adapters and their contracts;
- joining-date reconciliation, durability and recovery;
- RSVP and sensitive data;
- UC05's scheduling, approvals, guardrails and evaluation fixtures.

Tests added or extended in this step:

| File | Tests | Covers |
|---|---|---|
| `tests/llm/test_llm_configuration.py` | 44 (was 18) | key loading; base URL; `OPENROUTER_MODEL` filling every tier, and per-tier override; DeepSeek id vs route; reasoning on/off/effort; invalid ids, URLs, flags, efforts, provider; explicit fallbacks validated; offline needs no key; status summary without the key |
| `tests/llm/test_gateway_reasoning.py` | 18 (new) | route + `api_base` + `extra_body` per setting; headroom; budget used up by reasoning; reasoning text never in result, events or logs; conversation round trip with `reasoning_details` unchanged and copied; none invented; scope refusal before sending; failed turn not recorded; `FakeLLM` parity |
| `tests/llm/test_gateway_openrouter.py` | 15 (was 14) | adds: a provider timeout is retried, with the configured timeout on every attempt. Already there: schema validation, retries, fallback, refusals, redaction |
| `tests/llm/test_litellm_openrouter_transport.py` | 7 (was 3) | real LiteLLM 1.106.4 on the wire: DeepSeek body, `reasoning`/`provider`, base URL and effort, explicit off, two-turn `reasoning_details` round trip; LiteLLM at `DEBUG` leaks no prompt, reasoning or key |
| `tests/llm/test_llm_wiring.py` | 6 (was 5) | the orchestrator's `build_deps` gets the gateway with every tier on the DeepSeek route |
| `tests/orchestration/test_llm_worker_configuration.py` | 7 (was 6) | the real UC05 worker reports DeepSeek ids, routes, base URL, reasoning, no fallbacks, no key; UC01/UC04 workers receive no key |
| UC05 `tests/test_llm_runtime.py` | 12 (was 6) | UC05 online on DeepSeek for every tier; policy Q&A sends reasoning and returns no reasoning text; welcome-note budget + headroom; budget exhaustion → generic error; conversation through `SanitizedLLM` and scope refusal; UC05's own LiteLLM 1.106.2 on the wire |

The smoke test was also run against a mocked OpenRouter answering like a reasoning model
(`--check-only --tier all`, then `--multi-turn --include-fallbacks`): exit code 0, ids and routes
reported, `reasoning_details` sent back on turn 2, and neither the key nor reasoning text in its
output.

## 13. Live smoke-test instructions

Opt-in and billable; never run by pytest. After step 2 of §11, from `HR-Onboarding-Common`:

```powershell
.venv\Scripts\python.exe scripts\openrouter_smoke_test.py --check-only        # validate; sends nothing
.venv\Scripts\python.exe scripts\openrouter_smoke_test.py                     # FAST tier, reasoning on
.venv\Scripts\python.exe scripts\openrouter_smoke_test.py --tier all          # each tier
.venv\Scripts\python.exe scripts\openrouter_smoke_test.py --multi-turn        # 2 turns, reasoning_details sent back
.venv\Scripts\python.exe scripts\openrouter_smoke_test.py --tier all --include-fallbacks   # verify each fallback
```

It reads the key from `.env` and validates the configuration as the agents do. It then prints the
provider, base URL, `require_parameters`, the reasoning settings, and per tier the model id and the
LiteLLM route. It sends `{"ok": true, "echo": "pong"}` as a structured request through the
production gateway (`max_tokens` 300 + headroom). Expected, per tier:

```text
[fast] OK  route=openrouter/deepseek/deepseek-v4.1-flash  latency=… ms  tokens in/out=…/…  reasoning_tokens=…  cost_usd=None
    structured reply validated: ok=True echo='pong'
```

- `reasoning_tokens` is the provider's count. If it reports none, the line says so; nothing is
  inferred.
- `--multi-turn` adds the number and types of `reasoning_details` entries returned, and the second
  turn's validated reply.
- It prints whether the key is set, never the key, and never reasoning text.
- Exit codes: `0` ok, `1` a request failed (sanitized error and retry/fallback events shown),
  `2` invalid configuration.

Each request costs a fraction of a cent at the catalog prices (§3).

## 14. Remaining blockers

1. **No live verification yet.** Everything above was verified offline against the installed LiteLLM
   versions. Run the smoke test (§13) once the key is in `.env`, ideally with `--tier all --multi-turn`.
   It answers what cannot be checked without a key:
   - providers honour JSON-schema output with reasoning (via `require_parameters`);
   - the actual `reasoning_details` shape;
   - which default effort `enabled: true` gives.
2. **Possible key exposure.** A key-shaped value was in `HR-Onboarding-Common/.env.example`, which is
   tracked (§2). It has been removed. Rotate it in OpenRouter if it was a live key.
3. **No fallback model.** Every tier is the same model with no fallback. If it or OpenRouter is
   unavailable, every model call fails: UC05 answers 503 "the language model is unavailable", and
   deterministic paths continue. Adding a fallback needs a second model verified with
   `--include-fallbacks`; none was configured, by instruction.
4. **UC02 and UC03 are not on the gateway** (§7, §10). Until their owners migrate, UC02 cannot import
   and UC03's model features stay on their fallbacks. Neither uses DeepSeek.
5. **Multi-turn is in-memory only** (§6). No current agent needs more; a persistent conversation would
   need its own security design.
6. **Cost not reported** for this model: LiteLLM's local price map lacks it (§4).
7. **Pre-existing, unrelated:** UC05's contract test
   `test_status_vocabularies_match_what_the_orchestrator_validates` fails because the orchestrator's
   HITL `Decision` vocabulary gained `withdrawn` in the earlier orchestration work. It awaits your
   decision on whether UC05 should adopt `withdrawn` or the orchestrator should keep it internal.
