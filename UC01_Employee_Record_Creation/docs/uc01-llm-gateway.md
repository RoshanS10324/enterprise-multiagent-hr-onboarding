# UC-01 and the central LLM gateway

UC-01's advisory model now reaches a model only through the shared gateway
in `hr_onboarding_common.llm`, the same gateway UC-05 and UC-02 use. UC-01
keeps its own small LLM contract and guard. The new code changes only
*where* a guarded prompt is sent.

## What the model may do

Nothing in UC-01 is decided by a model. There are three tasks, defined in
`brain/tasks/*.yaml`:

| Task | Authority | Caller | When the model fails or refuses |
|---|---|---|---|
| `intake_intent` | escalate only | `intake/email_intake.py` | The deterministic result stands |
| `duplicate_explanation` | draft for a human | `open_case.py` | The case and its HC_12 approval are created anyway |
| `discrepancy_summary` | draft for a human | `compare_sources.py` | The discrepancy and its HC_04 approval are flagged anyway |

The model can never approve, reject, create a record, or clear a review that
the rules raised. Tests: `tests/test_llm_gateway.py` and `tests/test_llm.py`.

## How a call flows

```
caller -> LlmRequest(task, named inputs)
       -> GatewayLlm.complete            src/sub_agent/llm/gateway_client.py
            1. guard.build_prompt        refuses undeclared, salary/bank/loan, identity or
                                         contact names, email addresses in values, and masked
                                         secrets. Raises LlmInputRefused (names only). The
                                         gateway is never called.
            2. gateway.structured(task="uc01.<task>", tier=FAST | WORKHORSE,
                                  system=brain/persona.md, user=<guarded prompt>,
                                  schema=_Label | _Draft)
            3. validate the reply        label must be in the task's output_values;
                                         a draft must be 1..2000 characters
            4. map errors                LLMRefusal -> LlmModelRefused
                                         LLMError, schema mismatch -> LlmUnavailable
                                         (generic message; provider text is dropped)
       <- LlmResponse(label | text, confidence, model=gateway.model_for(tier))
```

Callers catch `LlmError` and record `audit_action_for(exc)`, one of
`LLM_INPUT_REFUSED`, `LLM_MODEL_REFUSED` or `LLM_UNAVAILABLE`. The entry
carries the task name only.

The model's draft text is **never** written to the audit log. A drafted
entry records `draft_chars` and `draft_sha256` instead. The guardrails rule
`llm.audited_without_content` was previously violated by `"draft": text`.

## Configuration

| Variable | Values | Effect |
|---|---|---|
| `UC01_LLM_ENABLED` | `true` / `false` (code default `false`) | Attaches the advisory model at all |
| `LLM_MODE` | `offline` (default) / `online` | `offline`: the deterministic `FakeLlm`, which calls nothing. `online`: `GatewayLlm` over the central gateway. This is the same rule UC-05 and UC-02 use. |

Model ids, fallbacks, retries, timeouts and provider credentials all belong
to the **shared gateway's** settings (`hr_onboarding_common.config`, with
`LLM_MODEL_*` and related variables). UC-01 names no model, imports no
provider SDK and reads no provider key. A test scans `src/` for all three.

`build_uc01_llm` selects the client in this order:

1. An injected gateway (`build_container(..., llm_gateway=...)`), used as given.
2. `LLM_MODE=offline` gives `FakeLlm`.
3. `LLM_MODE=online` gives `GatewayLlm`. The gateway is built with
   `hr_onboarding_common.llm.build_llm(get_settings())` when the shared
   package provides `build_llm` (it validates the configuration and owns
   provider routing). Otherwise it is built with
   `LiteLLMGateway(get_settings())`.

## Dependency decision

`hr_onboarding_common` remains an **optional** import for UC-01. It is
guarded, and `GATEWAY_AVAILABLE` reports whether it is installed. It is not
declared in `pyproject.toml`, for three reasons:

- UC-01 must keep running on its own dependencies. CI and the Docker image
  run on them, and `LLM_MODE=offline` needs nothing more.
- `uv` is not available here to re-lock. Changing `pyproject.toml` without
  re-locking would break `uv sync --frozen`.
- No cycle is involved. HR-Onboarding-Common's dev dependencies list UC-02,
  not UC-01. So declaring the shared package later, for example as a path
  dependency, is safe once it can be locked.

With `LLM_MODE=online` and the shared package missing, `build_uc01_llm`
raises an error that says so. It never pretends to be online.

UC-01 runs in the orchestrator as its **own worker process**, so the
orchestrator's `AgentContext.llm` cannot be passed in. UC-01 builds its
client from the same shared settings. This is the "worker UC" pattern in
HR-Onboarding-Common's migration guide. In-process callers and tests can
still inject one.

## Comparison with UC-05 and UC-02

| | UC-05 (reference) | UC-02 | UC-01 |
|---|---|---|---|
| Mode switch | `LLM_MODE` (offline default) | `LLM_MODE` (offline default) | `LLM_MODE` (offline default), behind `UC01_LLM_ENABLED` |
| Offline client | common `FakeLLM` with handlers | UC-02's scripted fake, or common `FakeLLM` plus `register_fake_handler` | UC-01's deterministic `FakeLlm` (unchanged) |
| Online client | `LiteLLMGateway(get_settings())` | `build_llm` if present, else `LiteLLMGateway` | same as UC-02 |
| Where the client comes from | `runtime.build_context` | injected `AgentContext.llm` (in-process) | built in the worker, or injected |
| Provider text in errors | `SanitizedLLM` swaps in a generic message | `degraded_reason_for` | `GatewayLlm` re-raises with a generic message |
| Output validation | per service | `validate_advice` | `output_values`, draft length, the caller's escalate-only rule |

## Testing without a provider

`tests/test_llm_gateway.py` (28 tests) drives the **real** `LiteLLMGateway`
through its injectable `completion_fn`, and drives the common `FakeLLM`. It
covers:

- offline and online selection
- success
- provider error
- timeout, which the gateway retries
- refusal, which is not retried
- invalid JSON
- a label outside `output_values`
- bad draft length
- refusal of each sensitive input before any call
- no secrets or subjects in the gateway logs
- intake and case-creation outcomes that do not change when the model fails
- no draft text in the audit log
- no provider SDK imports

No test makes a network call. The suite was also run with outbound sockets
blocked, and no connection was attempted.
