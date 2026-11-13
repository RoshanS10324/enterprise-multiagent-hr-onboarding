# OpenRouter Integration Guide

How the HR onboarding agents reach a language model through OpenRouter: configuring it, switching
between online and offline, checking that it works, and fixing it when it does not.

Status per use case and evidence: [OPENROUTER_INTEGRATION_REPORT.md](OPENROUTER_INTEGRATION_REPORT.md).
The configured model (DeepSeek V4.1 Flash with reasoning), in detail:
[OPENROUTER_DEEPSEEK_INTEGRATION_REPORT.md](OPENROUTER_DEEPSEEK_INTEGRATION_REPORT.md).
What UC01–UC04 owners need to change: [UC_LLM_GATEWAY_MIGRATION_GUIDE.md](UC_LLM_GATEWAY_MIGRATION_GUIDE.md).

---

## 1. Architecture overview

```text
                 OpenRouter API  (OPENROUTER_BASE_URL + /chat/completions)
                        ^
                        |  one HTTP request per attempt; key from OPENROUTER_API_KEY
                     LiteLLM  (route "openrouter/<OpenRouter model id>")
                        ^
          LiteLLMGateway  -- the only code that calls a model
          hr_onboarding_common/llm/gateway.py: model id -> route (llm/routing.py), reasoning options,
          retries, fallbacks, refusals, Pydantic validation, callbacks, redaction of failures
                        ^
          build_llm()  -- hr_onboarding_common/llm/factory.py
          LLM_MODE=offline -> the agent's FakeLLM;  LLM_MODE=online -> validated LiteLLMGateway
                        ^
     +------------------+---------------------------------------------+
     |                                                                |
  Orchestrator (bootstrap.build_deps)                     UC05 (sub_agent/runtime.build_context)
  - makes no model calls itself today                     - onboarding.policy_qa   (WORKHORSE)
  - starts the UC worker processes                        - onboarding.welcome_note (FAST)
     |
     +-- UC05 worker  ---> builds its own gateway from the same configuration (has LLM access)
     +-- UC01 worker  ---> no model calls; never receives the key
     +-- UC04 worker  ---> no model calls; never receives the key
     +-- UC02, UC03 (in-process) ---> not yet on the gateway: see the migration guide
```

Agents name a **tier** and a **Pydantic schema**; they never name a model or a provider. Which model a
tier means is configuration.

## 2. OpenRouter through LiteLLM

Two identifiers, kept apart:

| | Example | Where |
|---|---|---|
| **OpenRouter model id** | `deepseek/deepseek-v4.1-flash` | what you configure (`OPENROUTER_MODEL`, `LLM_MODEL_*`, `LLM_FALLBACKS_*`) |
| **LiteLLM route** | `openrouter/deepseek/deepseek-v4.1-flash` | what the gateway passes to LiteLLM; built only by `llm/routing.py` |

They are not interchangeable. Given the bare id, LiteLLM would use its own `deepseek` provider
(DeepSeek's API, with another key) instead of OpenRouter. A configured `openrouter/` prefix is
accepted and removed, so both spellings mean the same model.

LiteLLM 1.106 (installed here) handles the route like this. This was verified offline against the
installed version with a mocked HTTP transport (`tests/llm/test_litellm_openrouter_transport.py`):

| Step | What is sent |
|---|---|
| Endpoint | `POST <OPENROUTER_BASE_URL>/chat/completions`. The gateway passes `OPENROUTER_BASE_URL` (default `https://openrouter.ai/api/v1`) as `api_base` |
| Model id | the route without its `openrouter/` prefix: `deepseek/deepseek-v4.1-flash` |
| Key | `Authorization: Bearer <OPENROUTER_API_KEY>`, read by LiteLLM from the environment |
| Structured output | the Pydantic schema becomes `response_format: {"type": "json_schema", "json_schema": {"name": <Schema>, ...}}` |
| Provider routing | `provider: {"require_parameters": true}`: OpenRouter only picks providers of the model that support every parameter sent. Turn off with `OPENROUTER_REQUIRE_PARAMETERS=false` |
| Reasoning | with `LLM_REASONING_ENABLED=true`: `reasoning: {"enabled": true}`, or `{"effort": "<LLM_REASONING_EFFORT>"}`. A single-turn call adds `"exclude": true`, so no reasoning text comes back. With `false`: `reasoning: {"enabled": false}` |
| `max_tokens` | the caller's budget plus `LLM_REASONING_TOKEN_HEADROOM` (2048) while reasoning is on. Reasoning tokens count against `max_tokens` |
| Attribution | `X-Title` / `HTTP-Referer` headers from `OR_APP_NAME` / `OR_SITE_URL` (defaults `liteLLM` / `https://litellm.ai`) |
| Retries | none of its own: the gateway passes `num_retries=0` and `max_retries=0` |

`provider` and `reasoning` are OpenRouter request fields. They go through LiteLLM's `extra_body`,
which it forwards unchanged; no other client or SDK is involved.

**Multi-turn.** Every model call today is single-turn. When an agent needs to continue a
conversation, it passes an `LLMConversation` (`llm/conversation.py`) to `structured(...)`, together
with the scope it was created for, e.g. `(client_id, case_id, session_id)`. The gateway then:
- leaves out `exclude`;
- sends the earlier turns, each assistant turn with the `reasoning_details` OpenRouter returned for
  it, unchanged;
- adds the new turn only if it succeeds;
- refuses the call before sending anything if the scope differs.

A conversation lives only in the memory of the run that created it. It is never persisted, logged
or returned by an API.

The gateway then:
- validates the reply against the schema (`LLMOutputError` if it doesn't match);
- retries transient errors (rate limit, connection, timeout, 5xx, bad gateway, invalid output) with
  bounded exponential backoff;
- moves to the tier's fallback models;
- treats a refusal (content filter or a provider content-policy error) as final;
- reports failures without credentials or long provider text.

## 3. Environment variables

All of these go in **one file**, `HR-Onboarding-Common/.env`. The template is
[`HR-Onboarding-Common/.env.example`](../.env.example). A real environment variable always overrides
the file.

| Variable | Required | Meaning |
|---|---|---|
| `LLM_MODE` | no (default `offline`) | `offline` or `online` (`live` is accepted as `online`). The example file sets `online` |
| `OPENROUTER_API_KEY` | online | your OpenRouter key; read only from the environment |
| `OPENROUTER_BASE_URL` | no (`https://openrouter.ai/api/v1`) | OpenRouter API base; must be `https://`, without credentials, query or fragment |
| `OPENROUTER_MODEL` | online, unless all three tiers are set | OpenRouter model id used by every tier whose `LLM_MODEL_*` is unset; `deepseek/deepseek-v4.1-flash` |
| `LLM_MODEL_FRONTIER` | see `OPENROUTER_MODEL` | OpenRouter model id `<provider>/<model>`; overrides `OPENROUTER_MODEL` for that tier |
| `LLM_MODEL_WORKHORSE` | see `OPENROUTER_MODEL` | as above |
| `LLM_MODEL_FAST` | see `OPENROUTER_MODEL` | as above |
| `LLM_FALLBACKS_FRONTIER`, `_WORKHORSE`, `_FAST` | no | comma-separated OpenRouter model ids tried in order after the tier's model fails. Unset = no fallback (the built-in offline labels are never used online) |
| `LLM_REASONING_ENABLED` | no (`true`) | `true`: `reasoning: {"enabled": true}` on OpenRouter calls. `false`: `{"enabled": false}`, sent explicitly because DeepSeek V4.1 Flash reasons by default (catalog `default_enabled: true`). Keep `true` for a model whose catalog entry says reasoning is `mandatory` |
| `LLM_REASONING_EFFORT` | no (unset = provider default) | one of `max`, `xhigh`, `high`, `medium`, `low`, `minimal`, `none`. Which ones a model accepts is in its catalog entry (DeepSeek V4.1 Flash: `max`, `high`, `low`, `none`) |
| `LLM_REASONING_TOKEN_HEADROOM` | no (2048) | tokens added to each call's `max_tokens` while reasoning is on |
| `OPENROUTER_REQUIRE_PARAMETERS` | no (`true`) | send `provider.require_parameters` (§2) |
| `LLM_PROVIDER` | no (`openrouter`) | `litellm` makes every model a LiteLLM route used as given, each prefix with its own key; reasoning options are sent on `openrouter/` routes only |
| `LLM_TIMEOUT_S` | no (120) | per-request timeout |
| `LLM_MAX_ATTEMPTS` | no (3) | attempts per model, including the first |
| `LLM_BACKOFF_INITIAL_S`, `LLM_BACKOFF_MAX_S` | no (1, 30) | exponential backoff with jitter, capped at the max |
| `OR_APP_NAME`, `OR_SITE_URL` | no | attribution headers sent to OpenRouter |
| `HR_ONBOARDING_ENV_FILE` | no | use another `.env` file; set automatically for child processes |
| `UC05_WORKER_LLM_MODE` | no | run the UC05 worker in another mode than the rest |
| `UC0X_WORKER_LLM_ACCESS` | no | whether worker UC0X receives the key (default: UC05 yes, UC01/UC04 no) |
| `LITELLM_CALLBACKS` | no | LiteLLM-native callbacks, e.g. `otel` |

**How the `.env` is found.** `load_environment()` (`hr_onboarding_common/config.py`) loads
`HR_ONBOARDING_ENV_FILE` if set, else `HR-Onboarding-Common/.env`. It never overrides a variable that
is already set. It then exports the file's path in `HR_ONBOARDING_ENV_FILE`, so every UC worker
started afterwards reads the same file. A worker never goes searching for a different `.env`.

**UC05 on its own** (its API or scheduler, not started by the orchestrator) runs from its own
environment, which holds an *installed copy* of hr-onboarding-common. That copy has no project folder
to look in, so it loads `HR-Onboarding-Common/.env` **only when `HR_ONBOARDING_ENV_FILE` points to it**.
Otherwise it reads only the real environment and a `UC05_Orientation-Schedule-Agent/.env`, if one
exists. With `LLM_MODE=online` and nothing found, startup stops with the list of missing variables.
See §8a.

## 4. Model tiers

| Tier | Used for | Today's callers |
|---|---|---|
| `FRONTIER` | complex orchestration and decision-making | none yet (reserved) |
| `WORKHORSE` | reasoning, drafting, task-execution support | UC05 `onboarding.policy_qa` (HR Policies Q&A); UC02 `uc02.advise` once migrated |
| `FAST` | extraction, classification, lightweight drafting | UC05 `onboarding.welcome_note`; UC03/UC01 tasks once migrated |

**The configured model.** Every tier uses `deepseek/deepseek-v4.1-flash`. Its catalog facts, and
why `require_parameters` matters for it, are in
[OPENROUTER_DEEPSEEK_INTEGRATION_REPORT.md](OPENROUTER_DEEPSEEK_INTEGRATION_REPORT.md) §2.

**Choosing models.** Every call asks for a JSON-schema response. Pick models whose OpenRouter catalog
page lists structured outputs / `response_format` among their supported parameters.

LiteLLM's local capability map does not mark OpenRouter models as supporting response schemas (it
reports `False` for every OpenRouter id checked), so it can't be used to vet them. Check each
configured model with the smoke test (§9). A model that ignores the schema will produce
`LLMOutputError`, which is retried and then sent to the fallback model.

Give each fallback the same capability. Fallbacks may be from another provider on OpenRouter; they
all use the same key. None is configured by default. Verify one with
`--include-fallbacks` (§9) before relying on it.

## 5. Adding your API key

1. Copy `HR-Onboarding-Common/.env.example` to `HR-Onboarding-Common/.env`. The `.env` is
   git-ignored (`HR-Onboarding-Common/.gitignore` and the workspace `.gitignore`).
2. In `.env`, put your key after `OPENROUTER_API_KEY=`. Use no quotes and no spaces.
3. The model lines already name `deepseek/deepseek-v4.1-flash`. Change them only to use another
   OpenRouter model id. Fallbacks are commented out; uncomment one only after verifying it (§9).
4. Run `scripts/openrouter_smoke_test.py --check-only`, then the smoke test itself (§9). The example
   sets `LLM_MODE=online`. Set `offline` in `.env` until the smoke test passes if you want the agents
   to keep running without the model meanwhile.

Never put the key in `.env.example`, in code, in a test, in a command line you share, or in chat.

## 6. Online mode

Set `LLM_MODE=online` in `.env` (or in the environment). At startup the configuration is validated
(`validate_online_config`). Startup stops with one message listing every problem if:
- a tier has neither `LLM_MODEL_<TIER>` nor `OPENROUTER_MODEL`;
- a model or an explicit fallback is not an OpenRouter model id `<provider>/<model>` (with
  `LLM_PROVIDER=litellm`: not a supported LiteLLM route);
- a placeholder is still in place;
- the key is missing or contains quotes or spaces;
- `OPENROUTER_BASE_URL` is not `https://`, or holds credentials, a query or a fragment;
- `LLM_PROVIDER`, `LLM_REASONING_ENABLED`, `OPENROUTER_REQUIRE_PARAMETERS` or `LLM_REASONING_EFFORT`
  has a value outside its list;
- an effort is set while reasoning is off, or the headroom is negative;
- reasoning is requested explicitly for a route outside OpenRouter;
- the timeout, attempts or backoff values are invalid.

The message names variables, never their values. This happens:
- in the orchestrator, when `build_deps` runs, before any worker starts;
- in UC05, in `build_context`, where it is raised as `RuntimeConfigurationError`;
- in the UC05 worker, on its first request.

## 7. Offline mode

`LLM_MODE=offline` (the default): each agent uses its deterministic offline LLM. That is `FakeLLM`
in the orchestrator, and UC05's `offline_llm()` with its policy-Q&A and welcome-note handlers. It
makes no network calls and needs no key. The ordinary test suites always run offline:
- `HR-Onboarding-Common/tests/conftest.py` and `UC05_Orientation-Schedule-Agent/tests/conftest.py`
  force `LLM_MODE=offline`;
- they point `HR_ONBOARDING_ENV_FILE` away from your real `.env`;
- they remove provider keys from the test process.

An online failure is never turned into a fabricated answer. UC05 surfaces it as an error
(`SanitizedLLM`: "the language model is unavailable"). UC02's advisor, once migrated, keeps its
deterministic fallback with `degraded=True`.

## 8. How each part receives the gateway

| Part | How | Status |
|---|---|---|
| Orchestrator | `bootstrap.build_deps` → `build_llm(settings)` → `AgentContext.llm` | online-ready; it makes no model calls today |
| UC05 (API, scheduler, worker) | `sub_agent/runtime.build_context` → `build_llm(settings, offline=offline_llm)` → `SanitizedLLM` | **implemented** |
| UC05 worker | `uc_worker_runtime._Uc05Handler`: `LLM_MODE` inherited (or `UC05_WORKER_LLM_MODE`), key inherited (`llm_access`) | **implemented** |
| UC01, UC04 workers | no model calls; the worker process is started without provider keys | nothing to route; see the migration guide to add model use |
| UC02 | its sub-agent entry point always uses its fake advisor; its real advisor imports a different gateway (`hr_recruitment_common`, not installed) and its package does not import (`advisor.py:1`) | **missing**; migration guide |
| UC03 (in-process) | direct Anthropic and Gemini SDK calls, hard-coded model ids | **missing**; migration guide. Not active here: the SDKs are not installed in the orchestrator environment and only `OPENROUTER_API_KEY` is configured, so UC03 uses its template and HR-review fallbacks |

Check what a running worker uses: send it `__llm_status__`
(`tests/orchestration/test_llm_worker_configuration.py`). It returns:
- the mode and the LLM class;
- the tier model ids;
- for the gateway: the provider, base URL, LiteLLM routes, fallback routes and reasoning settings;
- *whether* each credential is present, never its value.

### 8a. Running UC05 on its own against OpenRouter

From `UC05_Orientation-Schedule-Agent`, in PowerShell. Use the working environment under
`%LOCALAPPDATA%\hr-onboarding-envs\uc05`; there is no `.venv` inside the UC05 folder.

```powershell
$env:HR_ONBOARDING_ENV_FILE = "C:\Studies\Projects\HR Onboarding Agent\HR-Onboarding-Common\.env"
$env:LLM_MODE = "online"                      # or LLM_MODE=online in that .env
$env:HR_AGENT_ENV = "development"                 # in-memory synthetic data; production refuses fake connectors
$env:IDENTITY_MODE = "insecure-dev-headers"   # development only: X-User-Id / X-Client-Id / X-User-Roles headers
$env:HR_AGENT_PERSIST = "0"
& "$env:LOCALAPPDATA\hr-onboarding-envs\uc05\Scripts\python.exe" -m uvicorn sub_agent.api:app --port 8010
```

The configuration is validated when the app starts. A missing key or tier stops it before it serves.
Then ask a policy question; this makes one billable WORKHORSE call:

```powershell
$h = @{ "X-User-Id" = "user:hr-admin-01"; "X-Client-Id" = "CLIENT-ACME"; "X-User-Roles" = "hr_admin" }
$b = '{"question": "Can I share confidential information?", "as_of": "2026-11-01", "employee_id": "EMP-001"}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8010/v1/onboarding/policy-questions -Headers $h `
  -ContentType "application/json" -Body $b
```

Expected: `answer` text and `citations` (for this question, `POL-CONDUCT-001`). A model failure
returns `503` with UC05's generic `{"detail": "the language model is unavailable", "error_type":
"LLMError"}`. The provider's message is never echoed; the error type is in the server log. `POST /v1/onboarding/workflow/policy-questions` (same body) runs the same question through
UC05's workflow and reports `workflow.stage = policy_answered`.

UC05's model-dependent evaluation runs the same way:
`& "$env:LOCALAPPDATA\hr-onboarding-envs\uc05\Scripts\python.exe" evaluation\run_evals.py`
with `LLM_MODE=online` and `HR_ONBOARDING_ENV_FILE` set. It makes several billable calls.

## 9. The connectivity smoke test (opt-in, billable)

`scripts/openrouter_smoke_test.py` isn't part of the test suite and never runs automatically.

```powershell
cd "HR-Onboarding-Common"
.venv\Scripts\python.exe scripts\openrouter_smoke_test.py --check-only         # validate only; sends nothing
.venv\Scripts\python.exe scripts\openrouter_smoke_test.py                      # one request on the FAST tier
.venv\Scripts\python.exe scripts\openrouter_smoke_test.py --tier all           # one request per tier
.venv\Scripts\python.exe scripts\openrouter_smoke_test.py --multi-turn         # two turns, reasoning_details sent back
.venv\Scripts\python.exe scripts\openrouter_smoke_test.py --tier all --include-fallbacks   # every configured model on its own
```

It reads `.env`, validates the configuration as the agents do, and prints:
- the provider, base URL, `require_parameters` and reasoning settings;
- per tier, the OpenRouter model id and the LiteLLM route it is called with.

It then sends one tiny structured request (`{"ok": true, "echo": "pong"}`, `max_tokens=300` plus the
reasoning headroom) per selected tier through the production gateway. For each one it prints:
- the route that answered;
- latency;
- tokens in and out, and the reasoning-token count;
- any retry or fallback events.

`--multi-turn` also reports how many `reasoning_details` entries came back, and their types, then
sends them with a second turn. It never prints their content.

On failure it prints the sanitized error. It prints only whether the key is set, never the key, and
never reasoning text. Exit codes: `0` success, `1` a request failed, `2` invalid configuration.

`cost_usd` is shown only when LiteLLM's *local* price map knows the model. The gateway never
downloads the price map, so for many OpenRouter ids it shows `None`.

## 10. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `online LLM configuration is invalid: OPENROUTER_API_KEY is not set` | `.env` not where expected, or the key line missing | check the `env file` line of `--check-only`; set `HR_ONBOARDING_ENV_FILE` or move the file to `HR-Onboarding-Common/.env` |
| `... still holds the placeholder` | value copied from `.env.example` | replace it |
| `AuthenticationError` in the smoke test | wrong, revoked or quoted key | create a new key in OpenRouter; paste it without quotes or spaces |
| `RateLimitError` retries, then fallback or failure | OpenRouter or upstream rate limit, or no credits | lower the volume, add a fallback model, check your OpenRouter credits and limits |
| `NotFoundError` / `BadRequestError` for one model | model id misspelled, retired, or not available to your account | copy the id from the catalog; ids are `<provider>/<model>`, e.g. `deepseek/deepseek-v4.1-flash` |
| `NotFoundError` mentioning providers / parameters | with `require_parameters`, no provider of the model supports every parameter sent | check the model's catalog entry; as a last resort set `OPENROUTER_REQUIRE_PARAMETERS=false` and watch for `LLMOutputError` |
| `LLMOutputError ... does not match <Schema>` | the model ignored the JSON schema | choose a model that supports structured outputs (§4); configure a fallback that does |
| `reasoning used the whole token budget` | reasoning tokens consumed `max_tokens` before any answer | raise `LLM_REASONING_TOKEN_HEADROOM`, or set a lower `LLM_REASONING_EFFORT` (DeepSeek V4.1 Flash: `low`) |
| `output truncated at max_tokens` | the answer itself was longer than the budget | raise the caller's `max_tokens` or the headroom |
| `ConversationScopeError` | an `LLMConversation` was used under another scope | by design; start a new conversation for that case or tenant |
| `LLMRefusal` | the model or provider declined on content grounds | not retried by design; review the request content |
| UC05 answers "the language model is unavailable" | any model failure (UC05 hides provider text) | run the smoke test on the WORKHORSE/FAST tiers; read the worker's stderr log for the error type |
| Worker reports `credentials_present.OPENROUTER_API_KEY: false` | that worker has no LLM access | intended for UC01/UC04; set `UC0X_WORKER_LLM_ACCESS=1` only when that UC really calls the gateway |

## 11. Security precautions

- **One source for the key:** the environment (`.env` or a secret manager that sets the variable).
  No code reads it except LiteLLM; the configuration check only tests whether it is present.
- **Never committed:** `.env` and `.env.*` are git-ignored (`!.env.example` stays). The examples
  contain placeholders only.
- **Least privilege:** worker processes without LLM access (UC01, UC04) are started without any
  provider key (`WorkerConfig.child_environment`).
- **Redaction:** every failure the gateway reports passes through `llm/redact.py`. That masks
  configured key values wherever they appear, plus anything shaped like `sk-…`, `Bearer …` or
  `api_key=…`, and truncates the text. The provider's exception is not chained into tracebacks.
  UC05 adds `SanitizedLLM`, which replaces the message with a generic one.
- **No prompt or reasoning text in logs:** gateway events carry task, tier, route, attempt, latency,
  token counts (including the reasoning-token count) and the error type. They never carry prompts,
  completions or reasoning.
- **Reasoning stays inside the gateway:**
  - single-turn calls ask OpenRouter to exclude reasoning text;
  - the gateway returns only the validated schema object;
  - `reasoning_details` are kept only inside an `LLMConversation`, which is never persisted, logged
    (its repr shows scope and turn count) or returned by an API.
- **LiteLLM's own logs are off:** its debug output contains whole request and response bodies,
  including prompts and reasoning text. The gateway sets its loggers (`LiteLLM`, `LiteLLM Router`,
  `LiteLLM Proxy`) to `CRITICAL`, whatever level the application logs at, and sets
  `suppress_debug_info`.
- **Rotate** the key in OpenRouter if it was ever pasted somewhere it should not be.

## 12. Test commands

```bash
cd HR-Onboarding-Common
export OPENBLAS_NUM_THREADS=1
# LLM configuration, gateway, reasoning and conversations, LiteLLM-over-OpenRouter transport, wiring,
# provider boundary (no workers)
.venv/Scripts/python.exe -m pytest tests/llm -p no:cacheprovider
# Real UC01/UC04/UC05 workers: who gets which LLM and which credentials
E="$LOCALAPPDATA/hr-onboarding-envs"
export UC01_WORKER_PYTHON="$E/uc01/Scripts/python.exe" UC04_WORKER_PYTHON="$E/uc04/Scripts/python.exe" \
       UC05_WORKER_PYTHON="$E/uc05/Scripts/python.exe"
.venv/Scripts/python.exe -m pytest tests/orchestration/test_llm_worker_configuration.py -p no:cacheprovider

# UC05 (its own environment holds a copy of hr-onboarding-common: refresh it after changing the common package)
uv pip install --python "$E/uc05/Scripts/python.exe" --reinstall --no-deps ./HR-Onboarding-Common   # from the workspace root
cd ../UC05_Orientation-Schedule-Agent && "$E/uc05/Scripts/python.exe" -m pytest tests/test_llm_runtime.py -p no:cacheprovider
```

None of these touch the network. The live check is §9.
