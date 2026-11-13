# UC-04 shared LLM gateway wiring (`integrations/gateway/`)

Optional, **advisory only**, and off by default. It lets UC-04 write its plain-language advisory narrative through the shared Enterprise HR Agent LLM gateway (`hr_onboarding_common.llm`, OpenRouter through LiteLLM). It does not change any decision.

## Where this comes from
It follows the pattern UC05 uses on the `origin/Prasanna` branch (reviewed at commit `de63b0c`, 2026-11-04): the shared client is built once by `hr_onboarding_common.llm.build_llm` from the central configuration; every call is `llm.structured(task=..., tier=..., schema=...)`; `LLM_MODE=offline` keeps a deterministic offline path; an invalid online configuration stops startup with a message that names variables, never values; tests run offline with `FakeLLM` and an injected `completion_fn`. Prasanna changed no UC-01..04 source: UC05 is the only use case wired to the gateway on that branch.

## How UC-04 differs from UC05, and why
| | UC05 | UC-04 |
|---|---|---|
| Dependency on Common | hard dependency, imported throughout `src/` | none in the core. The wiring is here, in `integrations/`, because `tests/test_packaging.py` keeps model layers out of `src/`. |
| Where the client is used | the agent's own LLM tasks | one task, `uc04.advisory`, behind the existing `AdvisorPort` / `LlmAdvisor` / `ModelPort` |
| `SanitizedLLM` wrapper | needed: Common's `BaseAgent` copies the error text into API responses | not needed: `LlmAdvisor` records only the error class name, never provider text |
| Offline LLM | `sub_agent.fakes.offline_llm` | `offline.offline_llm`, one handler for `uc04.advisory` |

## Files
| File | Purpose |
|---|---|
| `model_port.py` | `GatewayModelPort(llm)`: `ModelPort` over a shared `LLMClient`. Task `uc04.advisory`, tier `WORKHORSE`, response schema `AdvisoryNarrative` (one text field, 1-1500 characters). The input is a `facts` block of codes, states and counts only. |
| `factory.py` | `build_gateway_advisor(environ=None)`: offline mode returns `FakeLlmAdvisor`; online mode returns the gateway advisor after `build_llm` validated the configuration. `advisor_for_client(llm)`: for a shared client the caller already has (for example `AgentContext.llm`). |
| `offline.py` | `offline_llm()`: a `FakeLLM` for tests and local runs. |

## What stays deterministic
The model returns text for `narrative` only, behind the banner `[MODEL-GENERATED ADVISORY - NOT AN AUTHORITATIVE APPROVAL OR DECISION]` and after the privacy guard (contact details and pay text are replaced by a redaction marker). `overall_status`, approval guidance, risk analysis, readiness summary and every other decision field of the report come from the deterministic template; `Uc04Result` is never passed to or changed by the model. Instruction-like text from a model is not filtered: it stays inside the labelled narrative and has no effect.

## Failure behaviour
Any gateway error, refusal, deadline overrun (`AdvisoryTimeout`: the advisor waits at most 20 s, below the orchestrator's 30 s worker call timeout, whatever the gateway's own retries do), rejected output (empty, over 1500 characters, wrong schema) or configuration problem gives a report with `degraded=true`, `degraded_reason=<error class name>` and the unchanged decision fields. No provider text, key or employee data is recorded. Replacing the narrative with the template text happens only with `fallback_to_template=True`, and the report then says `provider="fake-llm"`, `fallback_from="litellm"`. An invalid online configuration raises `GatewayConfigurationError`; nothing silently falls back to offline.

## Configuration and dependencies
All configuration is the shared one (`HR-Onboarding-Common/.env`, `HR_ONBOARDING_ENV_FILE`): `LLM_MODE`, tier models, key, reasoning. UC-04 names no model, provider or key and never reads a credential.

Running it needs a Common that has `build_llm` (the Prasanna branch's), plus `tenacity`, `python-dotenv` and (online only) `litellm` in the process's environment.

The optional extra is in `pyproject.toml` and `uv.lock` is regenerated (38 packages added, none removed or changed):
```toml
[project.optional-dependencies]
gateway = ["hr-onboarding-common", "litellm>=1.83.0"]
[tool.uv.sources]
hr-onboarding-common = { path = "../HR-Onboarding-Common" }   # exact casing: case-sensitive on Linux and macOS
```
Install with `uv sync --extra gateway`. The default offline install is unaffected. The prepared worker environment under `%LOCALAPPDATA%\hr-onboarding-envs\uc04` has **not** been rebuilt, because that would install the outdated Common. Rebuild it after Prasanna's Common is merged.

## Tests
`tests/test_gateway_advisor.py`. It needs `hr_onboarding_common`: set `HR_ONBOARDING_COMMON_SRC` to a Common `src` folder, or keep `HR-Onboarding-Common` next to this folder. With a Common that lacks `build_llm`, the factory tests skip. Everything is offline; **no real gateway call has been made or verified.**

## Not connected: the orchestrator
On Prasanna's Common the UC-04 worker (`_Uc04Handler` in `uc_worker_runtime.py`) builds a plain `Uc04Agent()` and only has `evaluate_orchestrated`, which never calls the advisor. UC-04 workers also get no provider credentials by default (`DEFAULT_LLM_ACCESS["uc04"] = False`; turn on with `UC04_WORKER_LLM_ACCESS=1` or `llm_access: true` in `worker_config.yaml`, which needs no code change). Orchestrated runs therefore have no model narrative yet.

The proposed Common change is `docs/common_patches/04_partD_llm_advisory_worker.patch` (not applied; needs Common team approval): a separate, read-only `advise_orchestrated` operation and `llm_status()`, built lazily so evaluation never depends on a model. It was validated with a real worker process in a temporary copy, offline. The patch applies to Prasanna's Common, not on top of Part A, and Parts A and B need rebasing onto that Common. Where the orchestrator stores the advisory is the Common team's decision.
