# UC-03 shared LLM gateway wiring (`integrations/gateway/`)

Optional and **advisory only**. It lets UC-03 use the shared Enterprise HR Agent LLM gateway (`hr_onboarding_common.llm`) for two narrow tasks. Every validation decision stays in UC-03's deterministic rules.

## What changed in UC-03
Before this migration, UC-03 called provider SDKs directly, read `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, and sent data a model should not see:
- **Communication drafting** (`comms.py`) sent the employee's name to Gemini, Claude or OpenAI.
- **Name checking** (`name_agent.py`) sent both full names to Gemini or Claude, and a model's "same person" answer **cleared** a name mismatch.
- **Low-confidence extraction** (`extraction.py`) sent up to 4,000 characters of passport, ID, visa or degree text to Gemini.

Now no provider SDK is imported, no key is read, and nothing above leaves UC-03.

## Architecture
```
UC-03 core (src/)                              integrations/gateway/        HR-Onboarding-Common
  validator ─┬─ comms.draft_communication_outcome ─┐
             └─ name_agent.check_name_consistency ─┼─ UC03AdvisorPort ─▶ GatewayAdvisor ─▶ LLMClient.structured(...)
                (deterministic decision first)     │  (advisor_port.py)    (advisor.py)      (LiteLLMGateway / FakeLLM)
```
* `src/sub_agent/advisor_port.py`: the port the core calls (`UC03AdvisorPort`), plus the privacy helpers (`pseudonymize_names`, `structural_hint`, `clean_note`) and the 20-second deadline. The core never imports this folder or the shared package.
* `advisor.py`: `GatewayAdvisor(llm)`, the port over any shared `LLMClient`.
* `offline.py`: `offline_llm()`, a `FakeLLM` with handlers for both tasks, for tests and demos.

| Task | Name | Tier | Response schema | What the model receives |
|---|---|---|---|---|
| Draft an HR message | `uc03.draft_communication` | WORKHORSE | `DraftReply(body)` | action code, outstanding document **labels**, finding codes, and `[EMPLOYEE_NAME]` as a placeholder |
| Note on a name variation | `uc03.name_check` | FAST | `NameConcernReply(level, note)` | anonymous labels only (`T1`, `INITIAL_OF_T1`, ...) |

The task names come from `HR-Onboarding-Common/docs/UC_LLM_GATEWAY_MIGRATION_GUIDE.md` section 3. The model per tier, retries, fallbacks and redaction belong to the shared gateway. UC-03 names no model, provider or key.

## Deterministic safeguards
* **UC-03 decides whether to send a message**; the model only words it. The real name is substituted locally after the call. A draft is rejected if it is empty, over 4,000 characters, lacks the placeholder (the model may have invented a name), or mentions a sensitive category. The HR-approval footer is always enforced.
* **Any name variation is `NAME_MISMATCH` and goes to HR** before the advisor is consulted. The advisor can only append an "advisory only" note. An unknown level, a failure or a timeout leaves the finding as it is.
* **The adapter refuses** to send anything that isn't a short code or an anonymous label (a second check behind the core).
* **No document text reaches any model.** Low-confidence documents get safe defaults and go to HR.
* **Audit:** with an advisor configured, the audit records the provider, whether a model note was added, the message source (`advisor` or `template`) and, on fallback, only the error class name. The default audit is unchanged.

## How the client reaches UC-03 (wired end to end)
```
orchestration/bootstrap.py: load_environment(); build_llm(settings)   (shared factory, LLM_MODE)
        ▼
AgentContext.llm            (FakeLLM offline, LiteLLMGateway online; the orchestrator's ONE client)
        ▼
adapters/uc03_documents.py: GatewayAdvisor(deps.ctx.llm) ──▶ build_uc03_graph(..., advisor=...)
        ▼
UC03AdvisorPort ──▶ GatewayAdvisor ──▶ llm.structured(task, tier, schema) ──▶ configured provider (OpenRouter)
```
* **Offline (`LLM_MODE=offline`, the default):** `build_llm` returns the shared `FakeLLM`. The adapter adds UC-03's offline handlers to that same client, for UC-03's two task names only, so orchestrated runs exercise the full gateway path with no network.
* **Online (`LLM_MODE=online`):** `build_llm` validates the shared configuration (key, tier models) at start-up and returns the shared `LiteLLMGateway`.
* **Standalone UC-03 / tests:** pass any shared client, e.g. `GatewayAdvisor(build_llm(Settings(), offline=offline_llm))`. Without `advisor=` everything is deterministic.

## Worker LLM access
That mechanism (`DEFAULT_LLM_ACCESS`, `UC0X_WORKER_LLM_ACCESS`) applies to the **subprocess workers** UC01, UC04 and UC05, which need a provider key in their own process. UC-03 is not a worker: it runs inside the orchestrator and gets only the injected client. It never reads a key itself, which a test enforces.

## Configuration and dependencies
* `anthropic` and `google-genai` were removed from UC-03's core dependencies. Nothing imports them any more.
* Optional extra `gateway`: `hr-onboarding-common` (path source `../HR-Onboarding-Common`, exact casing) and `litellm>=1.83.0`, both limited to Python 3.12+ because the shared package requires 3.12 while UC-03 supports 3.11.
* `uv.lock` was regenerated: 6 packages removed (the two SDKs and their helpers), 35 added for the optional extra, 1 existing package bumped (`certifi`). `uv lock --check` passes.

## Tests
`tests/unit/test_uc03_gateway_advisor.py` (49 tests, offline): offline fake, success on the right tasks and tiers, timeout, provider failure and refusal (only the class name is recorded), malformed and rejected replies, privacy (no name parts, no document numbers sent), visible fallback in the audit, identical authoritative results with and without the gateway across 8 scenarios and 3 advisor behaviours, the "same person" advisor that cannot clear a mismatch, and the no-SDK, no-key and no-hard-coded-model boundaries.
It needs `hr_onboarding_common`: set `HR_ONBOARDING_COMMON_SRC`, or keep `HR-Onboarding-Common` next to this folder. The in-tree source is preferred over any installed copy, so a stale installed Common cannot be tested by mistake. Otherwise it skips.
End to end with the shared factory, the real gateway class and the database stores: `tests/unit/test_uc03_end_to_end.py`. Orchestrated: `HR-Onboarding-Common/tests/orchestration/test_uc03_gateway_and_persistence.py`.

## Known limitations and pending work
* **No real gateway call has been made or verified.** Everything ran offline. The real `LiteLLMGateway` class was exercised with only its HTTP call replaced (`tests/unit/test_uc03_end_to_end.py`), which checks routing, tiers and that no name, path or key is sent. To verify online, set `LLM_MODE=online` and the shared key in `HR-Onboarding-Common/.env`, then run Common's `scripts/openrouter_smoke_test.py` (on `origin/Roshan`; not ported here) and one UC-03 validation.
* The shared factory was ported into this branch's Common from `origin/Roshan` (identical to Prasanna's). See `HR-Onboarding-Common/docs/UC03_INTEGRATION_COMMON_CHANGES.md`.
* A timed-out call keeps running on its worker thread until the gateway's own timeout. Its answer is discarded.
* **Behaviour change for HR:** name variations that a model used to clear (father names, initials, ordering) now always go to HR review.
* **Lint:** some edits from the earlier session use lines longer than the project's 100 characters (rule E501), and `workflow.py` had unused imports before this migration. Both were left as they are.
