# Common changes made for the UC-03 gateway and persistence integration

Branch `document-information-validation`, on top of `be25b63`. All uncommitted. Each change is listed with its impact on other use cases.

## 1. Shared LLM gateway ported from `origin/Roshan`
The files were copied verbatim from `origin/Roshan`, whose Common LLM layer is identical to `origin/Prasanna` (`de63b0c`/`b5a7d0e`). Nothing was merged and nothing else was taken from those branches.

| File | Change |
|---|---|
| `src/hr_onboarding_common/llm/factory.py` | **new**: `build_llm`, `llm_mode` (`offline` / `online`), `validate_online_config`, `LLMConfigurationError` |
| `src/hr_onboarding_common/llm/routing.py`, `redact.py`, `conversation.py` | **new**: OpenRouter routing, redaction of provider text, multi-turn conversation support |
| `src/hr_onboarding_common/llm/__init__.py`, `base.py`, `gateway.py`, `callbacks.py` | updated to the shared versions (reasoning, routing, redaction; same `structured()` contract) |
| `src/hr_onboarding_common/config.py` | updated: `load_environment`, `ENV_FILE_VAR`, provider and reasoning settings |
| `src/hr_onboarding_common/orchestration/bootstrap.py` | `AgentContext.llm` now comes from `build_llm(settings)` (LLM_MODE) instead of a hard-coded `FakeLLM()`; the project `.env` is loaded first |
| `tests/conftest.py` | **new**: forces offline mode and removes provider keys for every Common test |
| `tests/llm/test_llm_configuration.py`, `test_gateway_openrouter.py`, `test_gateway_reasoning.py`, `test_litellm_openrouter_transport.py` | **new**: the shared gateway's own tests |

Not ported, on purpose: worker LLM-access configuration (`workers/config.py`), `test_llm_wiring.py` (depends on it), `test_provider_boundary.py` (also asserts UC-02 internals) and the smoke-test script.

**Impact.** Default behaviour is unchanged: `LLM_MODE` defaults to `offline`, which gives a `FakeLLM` exactly as before. UC-05 and the orchestrator keep working; see the regression results below. Online mode now validates its configuration at start-up and fails loudly when the key or models are missing.

## 2. UC-03 adapter: `src/hr_onboarding_common/orchestration/adapters/uc03_documents.py`
* Passes the orchestrator's own shared client into UC-03: `build_uc03_graph(..., advisor=GatewayAdvisor(deps.ctx.llm))`. Offline, UC-03's two offline handlers are added to the shared `FakeLLM` for UC-03's task names only (`setdefault`, so existing handlers are never replaced).
* Builds UC-03's stores through UC-03's own selector (`integrations.supabase.build_stores`, `UC03_STORE_MODE`, default `memory`). Previously it hard-coded in-memory stores.
* Projects `persistence_status` per document into the orchestrator state (additive key).
* Loads UC-03's `integrations.*` modules and verifies they come from UC-03's repo root (the top-level name is generic).

**Impact.** UC-03 only. With `UC03_STORE_MODE` unset and `LLM_MODE=offline`, every document result is identical to before; a test asserts this.

## 3. New Common test
`tests/orchestration/test_uc03_gateway_and_persistence.py`: the client from the shared factory reaches UC-03; document results are identical with and without it; a failing client changes nothing; database stores and persistence status work through the orchestrator; a database failure is a technical failure, not a verdict.

## Known Common issue (not changed)
`platform/supabase_client.py` calls `load_dotenv()` when imported, which loads the repository-root `.env` (including a live service-role key) into any process that imports it. UC-03 imports it only in supabase mode. Recommended for the Common owner: load configuration explicitly instead of at import time.

## Regression results (offline)
See the final report: the Common and UC-05 suites were run before and after these changes on the same machine.
