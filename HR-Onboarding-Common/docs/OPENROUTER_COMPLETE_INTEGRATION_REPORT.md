# OpenRouter integration through the Central LLM Gateway: UC01–UC05

Date: 2026-11-04. Branch: `Prasanna` (uncommitted working tree). Model: `deepseek/deepseek-v4.1-flash` via OpenRouter.

This report records what was verified by running code. A row says "verified" only where a test or a live call ran and its output is quoted below.

## 1. Summary

| Area | Status | Evidence |
|---|---|---|
| Central LLM Gateway (`hr_onboarding_common.llm.LiteLLMGateway`) | Working; the only model path in all five UCs | §2, §4; `tests/llm/test_provider_boundary.py` (6 tests) |
| OpenRouter, real API | Verified live on all three tiers | §3.1 smoke test |
| UC01 | Gateway path verified live; one bug fixed | §3.2, §4.1 |
| UC02 | Gateway path verified live, including through the orchestrator; wiring repaired | §3.2, §3.3, §4.2 |
| UC03 | **Migrated in this change.** It made direct Anthropic and Gemini calls before; all three call sites now use the gateway, verified live | §3.2, §4.3 |
| UC04 | Gateway path verified live; the decision logic is untouched | §3.2, §4.4 |
| UC05 | Gateway path verified live | §3.2, §4.5 |
| Direct provider SDK calls remaining | **None** in any UC source tree (enforced by test) | §5 |

## 2. Architecture

```
UC01 GatewayLlm ─────────────┐
UC02 LiteLLMAdvisor ─────────┤  (in the orchestrator process: AgentContext.llm)
UC03 integrations/gateway ───┤  (GatewayAdvisor via UC03's advisor port; in the orchestrator process: AgentContext.llm, injected)
UC04 GatewayModelPort ───────┤
UC05 runtime / services ─────┘
            │  structured(task, tier, system, user, schema)
            ▼
hr_onboarding_common.llm.build_llm()  ── LLM_MODE=offline → FakeLLM (deterministic, no network)
            │ LLM_MODE=online, after validate_online_config()
            ▼
LiteLLMGateway ── retries (tenacity), per-tier fallback chain, refusal handling, schema validation,
            │      reasoning {"enabled":true,"exclude":true}, redaction, LLMEvent callbacks
            ▼
LiteLLM  "openrouter/deepseek/deepseek-v4.1-flash"  → https://openrouter.ai/api/v1
```

UCs name a **task** and a **tier** (FRONTIER / WORKHORSE / FAST) and pass a Pydantic schema. They never name a model or read a key. Model ids, the key, the base URL, reasoning, timeouts and retries are configured once, in `HR-Onboarding-Common/.env`.

### Gateway configuration (actual variable names)

| Variable | Value in use | Purpose |
|---|---|---|
| `LLM_MODE` | `online` (`.env`), `offline` in tests | Selects the gateway or FakeLLM |
| `LLM_PROVIDER` | unset, so `openrouter` | Route model ids through OpenRouter |
| `OPENROUTER_API_KEY` | set (value never printed) | Read only from the environment, by LiteLLM |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Must be https; validated |
| `OPENROUTER_MODEL`, `LLM_MODEL_FRONTIER/WORKHORSE/FAST` | `deepseek/deepseek-v4.1-flash` | Model per tier |
| `LLM_REASONING_ENABLED` | `true` | Sends `reasoning.enabled`; single-turn calls also send `exclude: true`, so no reasoning text comes back |
| `LLM_TIMEOUT_S`, `LLM_MAX_ATTEMPTS`, `LLM_BACKOFF_*` | 120 / 3 / 1–30 s | Timeout and retry policy, owned by the gateway only |
| `OPENROUTER_REQUIRE_PARAMETERS` | default `true` | Route only to providers that support JSON-schema output and reasoning |

The prompt suggested `LLM_ENABLED=true`. The repository has no such variable: the switch is `LLM_MODE`. UC01's own advisory switch is `UC01_LLM_ENABLED`, off by default. No variable was renamed.

## 3. Real API verification (Mode 1, live OpenRouter)

### 3.1 Gateway smoke test (`scripts/openrouter_smoke_test.py --tier all`)

```
OPENROUTER_API_KEY: set        litellm: 1.106.4     provider: openrouter
base URL: https://openrouter.ai/api/v1   require_parameters: True
reasoning: {'enabled': True, 'effort': None, 'token_headroom': 2048}
[frontier]  OK route=openrouter/deepseek/deepseek-v4.1-flash latency=1686 ms tokens in/out=57/52 reasoning_tokens=38
[workhorse] OK route=openrouter/deepseek/deepseek-v4.1-flash latency=744 ms  tokens in/out=57/45 reasoning_tokens=31
[fast]      OK route=openrouter/deepseek/deepseek-v4.1-flash latency=811 ms  tokens in/out=57/35 reasoning_tokens=21
structured reply validated: ok=True echo='pong' (each tier)
```

The request reached OpenRouter. The latencies and token counts were returned by the provider, the reasoning tokens were counted, and the replies validated against the schema. `cost_usd` is `None`: LiteLLM's local price map does not know this model (a reporting gap, §6).

### 3.2 Every UC's own gateway path (`scripts/openrouter_uc_live_check.py`)

Each UC's own code path ran against a real `LiteLLMGateway` built by `build_llm`, with a recording callback attached. A row is "verified" only when the gateway emitted a `success` event.

| UC | Path exercised | Task | Real call | Latency | Tokens in / out / reasoning | Result |
|---|---|---|---|---|---|---|
| UC01 | `GatewayLlm.complete` | `uc01.discrepancy_summary` | **yes** | 9855 ms | 1263 / 438 / 314 | 521-char draft |
| UC02 | orchestrator `call_uc02_plan_tasks` → `UC02SubAgent(llm_client=gateway)` | `uc02.advise` | **yes** | 8235 ms | 1567 / 1545 / 1162 | plan COMPLETE, 10 tasks, `advice_source=litellm:deepseek/deepseek-v4.1-flash`, not degraded |
| UC03 | `name_agent` | `uc03.name_consistency` | **yes** | 10073 ms | 455 / 1762 / 1650 | father-name variant judged same person |
| UC03 | `LLMDocumentExtractor` | `uc03.document_extraction` | **yes** | 1656 ms | 377 / 97 / 19 | 5 fields, confidence 0.99 |
| UC03 | `comms.draft_communication` | `uc03.draft_communication` | **yes** | 2443 ms | 545 / 604 / 477 | 431-char draft for HR review |
| UC04 | `LlmAdvisor(GatewayModelPort)` on a real UC04 evaluation | `uc04.advisory` | **yes** | 6286 ms | 202 / 425 / 315 | 480-char narrative; decision fields come from the service |
| UC05 | `answer_policy_question` | `onboarding.policy_qa` | **yes** | 3139 ms | 1413 / 317 / 90 | answered, 1 citation |

All routes were `openrouter/deepseek/deepseek-v4.1-flash`. No retry, fallback or failure event occurred.

### 3.3 Through the orchestrator with the real agents (`scripts/run_online_orchestration_check.py`)

`build_deps` ran online, with the real UC01, UC04 and UC05 workers and UC02 and UC03 in-process. These events came from the orchestrator's gateway:

```
success task=uc02.advise           route=openrouter/deepseek/deepseek-v4.1-flash latency_ms=6145 tokens=1547/1400/876
success task=uc03.name_consistency route=openrouter/deepseek/deepseek-v4.1-flash latency_ms=1893 tokens=573/91/37
```

After the model judged the name variant consistent, UC03 still held the document at `awaiting_approval` until an HR review. The model did not approve it. What this run cannot show is in §6.

### 3.4 Real failure handling

These ran against the real endpoint, through the gateway:

| Case | Result |
|---|---|
| Invalid key | `LLMError` (`AuthenticationError`) in 3.9 s; neither the invalid nor the real key appears in the message |
| Model id that does not exist | `LLMError` (`BadRequestError`); no key in the message |

Timeouts, rate limits, transient 5xx, invalid structured output, refusal and fallback are covered by the gateway's unit tests against a stand-in transport (all pass): `tests/llm/test_gateway_openrouter.py`, `test_litellm_openrouter_transport.py` and `test_gateway_reasoning.py`.

## 4. Per-UC integration

### 4.1 UC01: employee record creation
- **Inspected:** `src/sub_agent/llm/{base,gateway_client,guard,fake}.py`, `api/container.py`, `brain/tasks/*.yaml`.
- **Invocation points:** `GatewayLlm.complete` for the draft tasks `discrepancy_summary`, `duplicate_explanation`, `intake_intent` and `responsibilities_review`. The authority is `DRAFT_FOR_HUMAN`, so the model never decides intake.
- **Direct provider calls:** none.
- **Changed:** `build_uc01_llm(environ=...)` decided the mode from the `environ` it was given, but then built the client from `os.environ`. It could report online while holding a `FakeLLM`, or validate a different key. It now passes the same mapping to `build_llm`. The tests were updated for OpenRouter routes.
- **Deterministic paths kept:** intake, duplicate detection and validation are untouched. A request without a placement is now held by UC01 itself (`location_name_required`); the orchestrator forwards a placement only when the caller states one.
- **Remaining mocks:** all persistence is in memory. UC01's Supabase writes are paused pending DBA decisions B1–B9.

### 4.2 UC02: onboarding task planning
- **Invocation point:** `LiteLLMAdvisor.advise`, task `uc02.advise`, tier WORKHORSE. Every recommendation is still validated by `validate_advice`; on any model failure UC02 falls back to its rules engine and marks the advice `degraded`.
- **Direct provider calls:** none. `agent/harness.py` contains `import litellm` only inside `litellm_available()`, an install probe that makes no call; the boundary test allow-lists it.
- **Found and fixed:**
  1. The staged edit to `adapters/uc02_tasks.py` had been made against an older copy. It deleted `DOCUMENT_TASK_KEYS`, `plan_record`, `catalog_requirements` and `record_initial_plan`, and with them the import of the whole orchestration package. HEAD's file was restored and the gateway wiring (`UC02_LLM_BEHAVIOUR="gateway"`, `llm_client=ctx.llm`, `advice_source`) was applied on top.
  2. The staged `bootstrap.py` always gave the orchestrator `FakeLLM`, even with `LLM_MODE=online`, so through the orchestrator UC02 never reached OpenRouter. It also referenced `FakeLLM` without importing it and dropped `uc03_document_store_path`. It now uses `build_llm(settings, offline=offline_llm)`: the gateway online, and FakeLLM with UC02's handler offline.
  3. The HR-Common virtualenv had a stale, non-editable copy of UC02, and `jsonschema` (a declared UC02 dependency) was missing. Both were reinstalled.
- **Replanning:** UC03's structured feedback reaches UC02 (`replan_tasks`). This is verified by `test_C_*` and `test_13_to_17_*` against UC02's real `OnboardingService`.

### 4.3 UC03: document collection and validation (migrated in this change)
- **Direct provider calls before this change:**
  - `comms.py`: `anthropic.Anthropic(...)`
  - `extraction.py`: `google.genai.Client(...)`
  - `name_agent.py`: Gemini first, then Anthropic

  Each read its own key (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`) and hard-coded its own model.
- **Superseded (2026-11-06 integration):** `src/sub_agent/llm_gateway.py`, described below, has been removed. UC03's only model path is now its advisor port (`src/sub_agent/advisor_port.py`), implemented by `integrations/gateway/GatewayAdvisor` over the orchestrator's shared client (`_uc03_advisor` in `adapters/uc03_documents.py`):
  - tasks `uc03.draft_communication` (WORKHORSE) and `uc03.name_check` (FAST), using placeholder names only;
  - low-confidence extraction no longer sends document text to any model; the document goes to HR review;
  - `tests/llm/test_provider_boundary.py::test_uc03_model_calls_go_through_its_advisor_port` enforces this.

  The rest of this section records the change as it was made.
- **At the time of this change:** one module, `src/sub_agent/llm_gateway.py`, was UC03's only model path. Each call site had a Pydantic schema:

  | Call site | Task | Tier | Schema |
  |---|---|---|---|
  | name consistency | `uc03.name_consistency` | FAST | `NameConsistencyOutput` |
  | low-confidence extraction | `uc03.document_extraction` | FAST | `ExtractionOutput`, mirroring `brain/onboarding/uc03_extraction.md`; fields as name/value pairs so strict JSON-schema output cannot drop keys |
  | reminder and replacement drafts | `uc03.draft_communication` | WORKHORSE | `DraftOutput` |

- **How the client is resolved:** an injected client wins. The orchestrator injects its own `AgentContext.llm` (`adapters/uc03_documents.py`). Otherwise UC03 calls `build_llm`. Offline, or without `hr_onboarding_common` installed, there is no model.
- **Deterministic fallbacks kept:**
  - no model → a name variant goes to HR review
  - low-confidence extraction → the local result
  - draft → the template
- **Model output never approves a document.** A consistent name only removes the `NAME_MISMATCH` finding; the document still needs HR's decision (`HUMAN_CONFIRMATION`). Two tests assert this, and the live run in §3.3 shows it.
- **Side fix:** the old Gemini `_parse` read a flat `confidence` key, but the prompt specifies `overall_confidence` and a nested `extracted_fields`. `_parse` now accepts the prompt's shape and still accepts the legacy flat shape.
- **Tests:** 210 pass (206 before). The fake Gemini clients were replaced by the real `LiteLLMGateway` over a stand-in transport, so the tests exercise schema validation, retries and degradation. New cases cover gateway failure, invalid structured output and a provider error leaking no secret.
- **Not removed:** `src/config.py` still declares `gemini_api_key` and `azure_openai_*` settings. No code reads them now; removing them is UC03's call.

### 4.4 UC04: equipment and access readiness
- **Invocation point:** `GatewayModelPort.complete`, task `uc04.advisory`, tier WORKHORSE, behind `LlmAdvisor`. It writes only the advisory narrative. All decisions, statuses and dates come from `Uc04Service`, and the model never provisions anything.
- **Direct provider calls:** none. Optional dependencies (`anthropic`, `langchain-openai`) are declared in the `integration` extra; none is imported.
- **Changed:** nothing in UC04. A `.venv` was created so the worker and the tests can run: 686 passed, 1 skipped.

### 4.5 UC05: orientation scheduling
- **Invocation points:** `services/policy_qa.py` (`onboarding.policy_qa`) and `services/welcome.py` (the personal note), both through `runtime.build_llm`.
- **Scheduling rules untouched:** availability, calendar checks and approvals are deterministic, and the model never confirms availability.
- **Joining date:** comes from the authoritative `onboarding_cases` row in Supabase mode (`db/repository.py`).
- **Direct provider calls:** none (boundary test).
- **Changed:** a contract test was updated for the shared HITL `Decision.WITHDRAWN` value.

## 5. Direct provider calls remaining

**None.** `tests/llm/test_provider_boundary.py` statically scans the source trees of HR-Onboarding-Common and UC01–UC05 and fails on:
- any import of `anthropic`, `openai`, `google.genai`, `google.generativeai`, `cohere` or `mistralai`;
- any `litellm` import outside the gateway, except UC02's install probe;
- UC03 reading a provider key itself.

## 6. Remaining gaps (details in PRODUCTION_READINESS_GAP_REPORT.md)

- **Leaked key:** the real OpenRouter key was committed in `HR-Onboarding-Common/.env.example` (commit `2e9762b`, pushed to `origin/Prasanna` and `origin/Roshan`). It has been removed from the file, but **it must be rotated**, and it remains in git history.
- **Cost reporting:** `cost_usd` is not reported for this model, because LiteLLM's local price map does not know it.
- **Default fallbacks:** a `LiteLLMGateway` constructed directly from `get_settings()` carries the built-in default fallbacks (`anthropic/claude-sonnet-5`). `build_llm`, which every UC uses, clears them online. Only the legacy "factory missing" branches in UC01 and UC02 could construct it directly.
- **Real agents end to end:** the full workflow cannot finish online. UC04 accepts only synthetic `SYN-CASE-…` ids, while UC01 issues UUIDs, and UC05 needs the offer-acceptance event in its mailbox connector.
- **Mock-only integrations:** Teams, Outlook, HRIS, calendar, email and document storage.
