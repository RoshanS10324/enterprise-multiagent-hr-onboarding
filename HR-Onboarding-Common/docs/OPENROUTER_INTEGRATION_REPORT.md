# OpenRouter Integration Report

**Date:** 2026-11-02 · **Scope:** route every production LLM call through `LiteLLMGateway` to
OpenRouter. **Boundary:** changes in HR-Onboarding-Common and UC05 only. UC01–UC04 were not touched:
not their source, configuration, tests, or installed dependencies (verified in §10). What they need is
in [UC_LLM_GATEWAY_MIGRATION_GUIDE.md](UC_LLM_GATEWAY_MIGRATION_GUIDE.md). How to configure and run it:
[OPENROUTER_INTEGRATION_GUIDE.md](OPENROUTER_INTEGRATION_GUIDE.md).

> **Later change (same day).** [OPENROUTER_DEEPSEEK_INTEGRATION_REPORT.md](OPENROUTER_DEEPSEEK_INTEGRATION_REPORT.md)
> configures `deepseek/deepseek-v4.1-flash` with reasoning. Model variables now hold OpenRouter model
> ids (`<provider>/<model>`); the gateway builds the `openrouter/...` LiteLLM route itself. The
> `openrouter/<provider>/<model>` form below is still accepted. Configuration checks and test counts
> in this report are as of that earlier step.

## Status at a glance

| Component | Status | Summary |
|---|---|---|
| Common gateway and configuration | **Implemented** | `LLM_MODE`; startup validation; OpenRouter routes; one `.env`; redaction; refusal and transient handling |
| Central orchestrator | **Implemented** | `build_deps` selects the LLM by mode, no longer a hard-coded `FakeLLM`. It makes no model calls itself today |
| UC05 Orientation | **Implemented** | `build_context` uses `build_llm`: offline = its offline LLM, online = the gateway. Worker follows `LLM_MODE` |
| UC01 Employee Record | **Missing (optional)** | no production model calls exist (deterministic `FakeLlm`, off by default), so nothing bypasses the gateway. A real model needs the adapter in the migration guide |
| UC02 Task Management | **Blocked** | the package doesn't import (`advisor.py:1`); its real advisor uses a different, uninstalled gateway; its sub-agent can't accept a client. Owner changes needed |
| UC03 Document Validation | **Missing** | 4 direct Anthropic/Gemini SDK call sites with hard-coded models. Dormant here (no SDKs installed in the orchestrator environment, no keys configured), so its conservative fallbacks apply. Owner changes needed |
| UC04 Equipment and Access | **Missing (optional)** | no model calls (deterministic advisor). Nothing to migrate; an optional gateway advisor is described |
| Live OpenRouter connectivity | **Not verified** | needs your key; `scripts/openrouter_smoke_test.py` does it (§12). Everything up to the HTTP request is verified offline against the installed LiteLLM |

---

## 1. Existing LLM architecture (findings before the change)

| Location | What was there |
|---|---|
| `hr_onboarding_common/llm/gateway.py` | `LiteLLMGateway`: tenacity retries, fallback chains, refusal on `content_filter`, Pydantic `response_format`, callbacks. Final errors embedded the raw provider message and chained the provider exception |
| `hr_onboarding_common/config.py` | `Settings` from the environment, model defaults `anthropic/claude-*`, no `LLM_MODE`. A bare `load_dotenv()` searched upward from the calling file |
| `orchestration/bootstrap.py:52` | `FakeLLM()` hard-coded, even for real runs |
| `orchestration/workers/uc_worker_client.py` | workers inherited the **whole** parent environment, so every worker would have received every key |
| `orchestration/workers/uc_worker_runtime.py` | the UC05 worker was forced offline (`UC05_WORKER_LLM_MODE`, default `offline`) |
| `orchestration/adapters/uc02_tasks.py` | requested `llm_behaviour="offline"`, a value UC02 rejects (`ValueError`): a latent bug once UC02 imports |
| UC05 `src/sub_agent/runtime.py:104` | gateway when `LLM_MODE != offline`, with no validation; `SanitizedLLM` wrapper |
| UC05 `api.py`, `scheduler.py` | bare `load_dotenv()` |
| UC01 | own `LlmClient` protocol and guard, `FakeLlm` only, `UC01_LLM_ENABLED=false` |
| UC02 | `LiteLLMAdvisor` over `hr_recruitment_common` (not installed anywhere); `UC02SubAgent.run` always uses the fake; `advisor.py:1` SyntaxError |
| UC03 | `comms.py` (Anthropic), `name_agent.py` (Gemini, then Anthropic), `extraction.py` (Gemini): direct SDKs, keys read from the environment, hard-coded model ids |
| UC04 | `FakeLlmAdvisor` only |
| Installed LiteLLM | 1.106.4 (common), 1.106.2 (UC05 env). The `openrouter` provider accepts `response_format`. Its local map reports `supports_response_schema=False` for every OpenRouter id checked |

## 2. Provider integration changes

- **OpenRouter via LiteLLM:** no OpenRouter client was written. The gateway passes
  `openrouter/<provider>/<model>` to LiteLLM, which routes it.
- **Verified offline against the installed LiteLLM with a mocked HTTP transport:**
  - the endpoint `https://openrouter.ai/api/v1/chat/completions`;
  - the model id with the prefix stripped;
  - `Authorization: Bearer` taken from `OPENROUTER_API_KEY`;
  - the Pydantic schema sent as `json_schema`;
  - exactly one HTTP request per attempt.
- **Mode selection** (`llm/factory.py`, new): `llm_mode()`, `validate_online_config()`,
  `build_llm()`, `online_fallbacks()`.
- **Gateway** (`llm/gateway.py`):
  - final failures pass through `redact()` and are raised `from None`;
  - `ContentPolicyViolationError` → `LLMRefusal` (no retry, no fallback);
  - `BadGatewayError` is transient;
  - `max_retries=0` alongside `num_retries=0`.
- **Redaction** (`llm/redact.py`, new): configured secret values, `sk-…`, `Bearer …` and
  `api_key=…`-style text are masked, whitespace collapsed, length capped at 300.

## 3. Status per use case

### UC01 — Employee Record Creation: missing (optional), compliant today
- **Calls:** none in production. `FakeLlm` serves `discrepancy_summary`, `duplicate_explanation`
  and `intake_intent`, only when `UC01_LLM_ENABLED=true`.
- **Gateway:** not used. No call bypasses it.
- **Worker:** started **without** provider keys. Verified with the real worker: `__llm_status__`
  reports `implementation: null` and `OPENROUTER_API_KEY` absent.
- **Next:** migration guide §1 (`GatewayLlm` adapter, tiers WORKHORSE/FAST, the guard still first).

### UC02 — Onboarding Task Management: blocked
- **Calls:** none reachable. The sub-agent entry point always uses the fake advisor. The real
  advisor imports `hr_recruitment_common`, which isn't installed. The package itself fails to import.
- **Common-side fix done:** our adapter now requests UC02's valid deterministic behaviour,
  `scripted`. A test guards it by reading UC02's `FAKE_BEHAVIOURS` from source.
- **Next:** migration guide §2: fix line 1; switch to `hr_onboarding_common`; add `llm_client` to
  `UC02SubAgent`. Then we pass the orchestrator's LLM in `call_uc02_plan_tasks`.

### UC03 — Document Validation: missing
- **Calls:** 4 direct SDK call sites (§4). In this deployment they are dormant: neither `anthropic`
  nor `google-genai` is installed in the orchestrator environment UC03 runs in, and only
  `OPENROUTER_API_KEY` is configured. UC03 therefore uses its template draft, routes name variations
  to HR review, and uses its local extraction.
- **Next:** migration guide §3: inject `LLMClient`; tasks `uc03.name_check` (FAST),
  `uc03.extract_fields` (FAST), `uc03.draft_communication` (WORKHORSE); every fallback kept.

### UC04 — Equipment and Access: missing (optional), compliant today
- **Calls:** none. `FakeLlmAdvisor` is deterministic and read-only.
- **Worker:** started without provider keys (verified with the real worker).
- **Next:** none required. Optional `GatewayAdvisor` in migration guide §4.

### UC05 — Orientation Scheduling: implemented
- `runtime.build_context`:
  - builds the LLM with `build_llm(settings, offline=offline_llm, environ=env)`;
  - turns a configuration error into `RuntimeConfigurationError`, naming variables only;
  - wraps the result in `SanitizedLLM`, as before.
- `api.py` and `scheduler.py` load the onboarding system's `.env` first, then UC05's own local one.
  The evaluations use `build_llm`, and `live` is accepted as `online`.
- **Calls:**
  - `onboarding.policy_qa` on WORKHORSE: the **HR Policies Q&A, preserved**. A test runs the real
    service and its citation grounding through the real gateway on an OpenRouter route.
  - `onboarding.welcome_note` on FAST.
- **Worker:** follows the orchestrator's `LLM_MODE` (`UC05_WORKER_LLM_MODE` still overrides it) and
  receives the key. Verified with the real worker: online it reports `LiteLLMGateway` with the
  configured tier models; with a bad configuration it fails at startup with
  `RuntimeConfigurationError` naming the variables.

### Central orchestrator: implemented
`bootstrap.build_deps` does three things, in order:
1. loads the one `.env`;
2. builds its LLM by mode, validating online configuration **before any worker starts**;
3. starts each worker with credentials only where `llm_access` is set.

It makes no model calls itself; the demo's synthetic mode keeps its explicit `FakeLLM`.

## 4. Direct SDK calls: removed or remaining

| Location | SDK | Status |
|---|---|---|
| HR-Onboarding-Common (all of `src/`, `scripts/`) | none | none present. Enforced by `tests/llm/test_provider_boundary.py` (LiteLLM imported only by `llm/gateway.py`) |
| UC05 (`src/`, `connectors/`, `context_layer/`, `evaluation/`) | none | none present (same test). `runtime.py` no longer constructs `LiteLLMGateway` itself |
| UC03 `src/sub_agent/comms.py::_llm_draft` | `anthropic` | **remaining** (owner) |
| UC03 `src/sub_agent/name_agent.py::_call_gemini` | `google.genai` | **remaining** (owner) |
| UC03 `src/sub_agent/name_agent.py::_call_claude` | `anthropic` | **remaining** (owner) |
| UC03 `src/sub_agent/extraction.py::HybridDocumentExtractor._call_llm` | `google.genai` | **remaining** (owner) |
| UC02 `agent/advisor.py::build_advisor` | none (another gateway: `hr_recruitment_common`) | **remaining** (owner) |

## 5. Gateway and dependency-injection changes

| File | Function | Change |
|---|---|---|
| `src/hr_onboarding_common/config.py` | `project_env_file`, `load_environment` (new); `Settings.llm_mode`; `get_settings` | one `.env`, explicit path, exported in `HR_ONBOARDING_ENV_FILE`, never overriding the environment |
| `src/hr_onboarding_common/llm/factory.py` (new) | `llm_mode`, `validate_online_config`, `online_fallbacks`, `build_llm`, `LLMConfigurationError`, `PROVIDER_CREDENTIALS`, `TIER_VARIABLES` | mode selection and startup validation |
| `src/hr_onboarding_common/llm/redact.py` (new) | `redact`, `secret_values`, `SECRET_ENV_VARS` | redaction |
| `src/hr_onboarding_common/llm/gateway.py` | `structured`, `_call_once`, `TRANSIENT`, `REFUSAL` | redacted final error; provider refusals; `max_retries=0` |
| `src/hr_onboarding_common/llm/__init__.py` | exports | the factory and `redact` |
| `src/hr_onboarding_common/orchestration/bootstrap.py` | `build_deps` | `load_environment()`, then `build_llm(settings)` before the worker pool |
| `src/hr_onboarding_common/orchestration/workers/config.py` | `DEFAULT_LLM_ACCESS`, `WorkerConfig.llm_access`, `has_llm_access`, `child_environment`, `load_worker_config` | per-worker credential scoping (`UC0X_WORKER_LLM_ACCESS`, yaml `llm_access`) |
| `src/hr_onboarding_common/orchestration/workers/uc_worker_client.py` | `UcWorker.start` | `Popen(env=config.child_environment())` |
| `src/hr_onboarding_common/orchestration/workers/uc_worker_runtime.py` | `_Uc05Handler._build`, `_Uc05Handler.llm_status`, `_llm_status`, `main` | UC05 follows `LLM_MODE`; `__llm_status__` diagnostic (names and booleans only) |
| `src/hr_onboarding_common/orchestration/adapters/uc02_tasks.py` | `UC02_LLM_BEHAVIOUR`, `call_uc02_plan_tasks` | `"offline"` → `"scripted"` (UC02's valid deterministic behaviour) |
| UC05 `src/sub_agent/runtime.py` | `build_context` | `build_llm`; `LLMConfigurationError` → `RuntimeConfigurationError` |
| UC05 `src/sub_agent/api.py` | module start | `load_environment()` before `load_dotenv()` |
| UC05 `src/sub_agent/scheduler.py` | `main` | same |
| UC05 `evaluation/run_evals.py` | `LIVE`, `build_world` | `llm_mode()`, `build_llm` |

## 6. Environment configuration

- **One file:** `HR-Onboarding-Common/.env`. The template, `HR-Onboarding-Common/.env.example`,
  holds placeholders only. `HR_ONBOARDING_ENV_FILE` can point elsewhere.
- **Precedence:** real environment > that file > UC05's local `.env` (UC05-only settings).
- **UC05 run on its own** (not as the orchestrator's worker) uses an installed copy of the common
  package and loads that file only when `HR_ONBOARDING_ENV_FILE` points to it. That was verified by
  starting the real UC05 server both ways; the steps are in guide §8a.
- **Child processes:** they get the file's path, and the environment minus credentials unless they
  have LLM access.
- **Variables:** `LLM_MODE`, `OPENROUTER_API_KEY`, `LLM_MODEL_{FRONTIER,WORKHORSE,FAST}`,
  `LLM_FALLBACKS_*`, `LLM_TIMEOUT_S`, `LLM_MAX_ATTEMPTS`, `LLM_BACKOFF_INITIAL_S`,
  `LLM_BACKOFF_MAX_S`. Optional: `OR_APP_NAME`, `OR_SITE_URL`, `UC05_WORKER_LLM_MODE`,
  `UC0X_WORKER_LLM_ACCESS`.
- **Online validation** (at startup) checks:
  - every tier is set;
  - every route is a supported provider in the `openrouter/<provider>/<model>` form;
  - no placeholders remain;
  - the key for each provider used is present, unquoted and without spaces;
  - the timeout, attempts and backoff values are valid.

  Unset fallbacks never fall back to the built-in `anthropic/…` labels.
- **No `.env` exists yet.** None was created; you will add the key yourself.

## 7. Model-tier mapping

| Task | Tier | Status |
|---|---|---|
| UC05 `onboarding.policy_qa` (HR Policies Q&A) | WORKHORSE | live in code |
| UC05 `onboarding.welcome_note` | FAST | live in code |
| UC02 `uc02.advise` | WORKHORSE | after the UC02 migration |
| UC03 `uc03.name_check`, `uc03.extract_fields` | FAST | after the UC03 migration (proposed) |
| UC03 `uc03.draft_communication` | WORKHORSE | after the UC03 migration (proposed) |
| UC01 `uc01.discrepancy_summary`, `uc01.duplicate_explanation` / `uc01.intake_intent` | WORKHORSE / FAST | optional (proposed) |
| UC04 `uc04.advisory` | WORKHORSE | optional (proposed) |
| FRONTIER | — | no caller today; reserved for orchestration-level decisions |

The model behind each tier is yours to choose (`LLM_MODEL_*`). No model id appears in agent code.
LiteLLM can't tell which OpenRouter models honour a JSON schema, so confirm each choice with the
smoke test.

## 8. Security changes

- The key is read only from the environment, by LiteLLM. Our code tests only whether it is present.
  Validation messages name variables, never values.
- **Least privilege:** UC01 and UC04 worker processes start without any provider key (verified with
  the real workers).
- **Redaction:** in gateway errors (`redact`, `from None`); UC05 adds its generic `SanitizedLLM`
  message. Verified that a key embedded in a provider error appears neither in the raised error nor
  in log output.
- **Logs:** events carry no prompt or completion text (tested).
- **Secrets not committed:**
  - `.gitignore` added to HR-Onboarding-Common and UC05 (`.env`, `.env.*`, `!.env.example`); the
    workspace `.gitignore` already ignores `.env`;
  - `.env.example` files hold placeholders only. UC05's previously had an empty
    `ANTHROPIC_API_KEY=` line, now replaced by a commented `OPENROUTER_API_KEY` placeholder.
- **Test isolation:** both suites force `LLM_MODE=offline`, ignore the real `.env` and drop
  provider keys, so a developer's online `.env` can never make the suite spend credits.
- **No real external integrations added:** email, Teams, calendar, HRIS and ITSM stay
  mocks/simulated. NDA/policy signature tracking stays excluded.

## 9. Tests executed and exact results

Environment: Windows; one test file per process; `OPENBLAS_NUM_THREADS=1`; worker interpreters from
`%LOCALAPPDATA%\hr-onboarding-envs`. The common package was reinstalled into the UC05 environment
(`uv pip install --reinstall --no-deps`), as `scripts/run_tests.ps1` does.

| Suite | Result |
|---|---|
| HR-Onboarding-Common, all 32 files (final run) | **502 passed, 0 failed, 0 skipped** (453 before + 49 new) |
| — `tests/llm/test_llm_configuration.py` | 18 passed |
| — `tests/llm/test_gateway_openrouter.py` | 14 passed |
| — `tests/llm/test_litellm_openrouter_transport.py` (real LiteLLM, mocked HTTP) | 3 passed |
| — `tests/llm/test_llm_wiring.py` | 5 passed |
| — `tests/llm/test_provider_boundary.py` | 3 passed |
| — `tests/orchestration/test_llm_worker_configuration.py` (**real UC01/UC04/UC05 workers**) | 6 passed |
| UC05 own suite (final run) | **538 passed, 1 failed** (533 before + 6 new) |
| — `tests/test_llm_runtime.py` (new) | 6 passed |
| — `tests/test_orchestration_contract.py::test_status_vocabularies_match_what_the_orchestrator_validates` | **failed, not caused by this task** (§11.4) |
| UC05 evaluations, offline | 35 scenarios: 34 pass, 0 fail, 1 skip (model-dependent; runs only online) |
| `scripts/openrouter_smoke_test.py --check-only`, nothing configured | exit 2; lists the three tiers and `OPENROUTER_API_KEY` as not set |
| Smoke test, end to end with real LiteLLM and mocked HTTP (dummy key; 429 on the first frontier attempt) | all 3 tiers OK, one retry on frontier, 4 HTTP requests in total, exit 0 |

What the new tests cover:
- the key from the environment;
- a missing key, tier or placeholder fails clearly;
- offline needs no key;
- tiers are read correctly;
- the environment overrides `.env`;
- the OpenRouter id is passed unchanged;
- schema validation;
- bounded retry and backoff;
- fallback;
- no retry on refusals;
- sanitized errors, and no key in logs;
- one HTTP request per attempt;
- the orchestrator's LLM by mode;
- invalid configuration stops startup before any worker;
- per-worker credential scoping;
- the UC05 worker's mode, override and validation;
- UC05 policy Q&A through the gateway on WORKHORSE;
- a UC05 failure surfacing as a generic error;
- no provider SDK in our code.

## 10. Files modified

There is no git repository, so files were compared with a copy taken before the work.

| File | +/− |
|---|---|
| `HR-Onboarding-Common/.env.example` (new) | +45 |
| `HR-Onboarding-Common/.gitignore` (new) | +12 |
| `HR-Onboarding-Common/docs/ONBOARDING_COMMON_ARCHITECTURE.md` | +10 −5 |
| `HR-Onboarding-Common/docs/OPENROUTER_INTEGRATION_GUIDE.md` (new) | +244 |
| `HR-Onboarding-Common/docs/UC_LLM_GATEWAY_MIGRATION_GUIDE.md` (new) | +516 |
| `HR-Onboarding-Common/docs/OPENROUTER_INTEGRATION_REPORT.md` (new) | this file |
| `HR-Onboarding-Common/scripts/openrouter_smoke_test.py` (new) | +89 |
| `HR-Onboarding-Common/src/hr_onboarding_common/config.py` | +41 −3 |
| `HR-Onboarding-Common/src/hr_onboarding_common/llm/__init__.py` | +11 −4 |
| `HR-Onboarding-Common/src/hr_onboarding_common/llm/factory.py` (new) | +128 |
| `HR-Onboarding-Common/src/hr_onboarding_common/llm/gateway.py` | +29 −13 |
| `HR-Onboarding-Common/src/hr_onboarding_common/llm/redact.py` (new) | +39 |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/adapters/uc02_tasks.py` | +6 −1 |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/bootstrap.py` | +12 −5 |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/workers/config.py` | +29 −1 |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/workers/uc_worker_client.py` | +1 |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/workers/uc_worker_runtime.py` | +32 −3 |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/workers/worker_config.example.yaml` | +4 |
| `HR-Onboarding-Common/tests/conftest.py` (new) | +21 |
| `HR-Onboarding-Common/tests/llm/` (5 new files) | +546 |
| `HR-Onboarding-Common/tests/orchestration/test_llm_worker_configuration.py` (new) | +93 |
| `UC05_Orientation-Schedule-Agent/.env.example` | +13 −6 |
| `UC05_Orientation-Schedule-Agent/.gitignore` (new) | +11 |
| `UC05_Orientation-Schedule-Agent/evaluation/run_evals.py` | +3 −3 |
| `UC05_Orientation-Schedule-Agent/src/sub_agent/api.py` | +5 −1 |
| `UC05_Orientation-Schedule-Agent/src/sub_agent/runtime.py` | +11 −2 |
| `UC05_Orientation-Schedule-Agent/src/sub_agent/scheduler.py` | +2 |
| `UC05_Orientation-Schedule-Agent/tests/conftest.py` | +7 |
| `UC05_Orientation-Schedule-Agent/tests/test_llm_runtime.py` (new) | +107 |

**Untouched, verified:**
- UC01–UC04: 483 files byte-identical (SHA-256, before and after).
- The UC01, UC02, UC03 and UC04 environments have the same installed packages as before.
- `hr-recruitment-common`: not present in the workspace; not touched.

**Environment change:** HR-Onboarding-Common reinstalled into the UC05 worker environment (UC05's
own dependency).

## 11. Remaining blockers

1. **UC02 (owner):** the `advisor.py:1` SyntaxError, plus the gateway migration (guide §2). Until
   then, task planning can't run for real and no UC02 model call is possible.
2. **UC03 (owner):** the four direct SDK call sites (guide §3). Harmless here today, but they would
   bypass the gateway as soon as `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` and those SDKs were
   present.
3. **UC01 and UC04 (owners, optional):** no model calls today. Adapters are proposed if one is
   wanted.
4. **UC05 contract test, needs your decision:**
   `test_status_vocabularies_match_what_the_orchestrator_validates` pins the shared HITL `Decision`
   enum to four values. My previous task added `withdrawn` to it, for withdrawing a superseded
   day-1 signoff.
   - It went unnoticed then because UC05's environment held an older copy of the common package.
     Refreshing that copy for this task exposed it.
   - The value is harmless to UC05: UC05 never withdraws, and "withdrawn" can't be given as a
     decision.
   - Updating that UC05 test is outside this task's LLM scope, so it is left failing.
5. **Model capabilities:** whether each chosen OpenRouter model honours JSON-schema output can only
   be confirmed live (smoke test). LiteLLM's local map doesn't say.
6. **Cost figures:** `cost_usd` appears only for models in LiteLLM's local price map. Many OpenRouter
   ids show `None`.
7. **UC05's environment holds a copy** of hr-onboarding-common. After any change to the common
   package, reinstall it there (`run_tests.ps1` does this).
8. **Live connectivity is unverified** until you run the smoke test with your key.

## 12. Adding the key and running the smoke test

1. Copy `HR-Onboarding-Common/.env.example` to `HR-Onboarding-Common/.env`. It is git-ignored.
2. In `.env`:
   - set `OPENROUTER_API_KEY` to your key, without quotes;
   - set the three `LLM_MODEL_*` values to OpenRouter ids that support structured output;
   - set `LLM_FALLBACKS_*`, or delete those lines;
   - leave `LLM_MODE=offline` for now.
3. Check, then test (from `HR-Onboarding-Common`):

   ```powershell
   .venv\Scripts\python.exe scripts\openrouter_smoke_test.py --check-only   # validates; sends nothing
   .venv\Scripts\python.exe scripts\openrouter_smoke_test.py                # one billable request (FAST)
   .venv\Scripts\python.exe scripts\openrouter_smoke_test.py --tier all     # one per tier
   ```

   It prints only whether the key is set. On failure it prints a sanitized error, and exits 2 for an
   invalid configuration or 1 for a failed request.
4. When every tier passes, set `LLM_MODE=online`. The orchestrator validates at startup, UC05 and
   its worker follow, and UC01/UC04 keep running without the key.
5. Troubleshooting: guide §10.
