# Common-side patches for UC-04 (proposals, NOT applied)

Nothing in `HR-Onboarding-Common` was changed. These are reviewable diffs, made and tested in a temporary copy of the workspace. Apply them only after the Common team approves. Diffs ignore line endings (the repository files use CRLF; re-check on apply).

## Part A: `01_partA_common_adapter_and_worker.patch` (non-breaking)

| Change | Where |
|---|---|
| Worker operations `submit_eligible`, `acknowledge_adapter_error`, `get_case_status` (authorisation stays inside UC-04's own policy) | `workers/uc_worker_runtime.py` |
| Opt-in identity profile: `UC04_IDENTITY_PROFILE` is read by the worker only and fixed before `sub_agent` is imported. Off by default, so the synthetic profile stays until the team approves PLATFORM | same |
| `AT_RISK` is reachable (BLOCKED plus an `AT_RISK` blocker); `CANCELLED` is terminal (FAILED, `CASE_CANCELLED`); an unknown UC-04 status is FAILED, never success | `adapters/uc04_equipment_access.py` |
| Client configuration accepted inline from UC02 (`uc02_role_context["uc04_client_config"]`, PROPOSED key name, pending D-1); the server-side path is still read only as an interim fallback | same |
| `config_version` and `config_digest` kept in the stage projection | same |
| Tests for all of the above, including the real worker process | `tests/orchestration/test_uc04_adapter_mapping.py` (new) |

Validation (temporary copy, with the temporary source forced first on the import path): the whole Common suite gives the **same 4 failures as the untouched working tree**, and no others. Those 4 are `test_real_agents_verify_documents_then_fan_out...`, `test_one_branch_failing_preserves_the_other_branches_results`, `test_known_import_defect_surfaces_as_failed_not_a_fake_success` and `test_real_uc02_defect_fails_task_planning_while_uc04_and_uc05_still_run`. They assert that UC02's `advisor.py` is still broken, and the uncommitted UC02 fix makes UC02 importable, so they fail with or without these patches. The 14 new tests pass.

Kept as today in Part A on purpose: `IN_PROGRESS` still maps to COMPLETE, and `offer_accepted`, `onboarding_form_approved` and `form_version` are still constants.

## Part B: `02_partB_strict_inputs_status_and_api.patch` (breaking, needs decisions)

| Change | Why it is separate |
|---|---|
| `IN_PROGRESS` maps to the in-progress stage status, not COMPLETE | The workflow has no later trigger that re-evaluates UC-04, so the case would wait. Needs the stage-status decision (F-06) and a status-sync trigger |
| `offer_accepted` and `onboarding_form_approved` come from `offer_accepted_at` and `form_approved_at` in the UC02 context; `form_version` is required and never defaulted | Existing test fixtures do not carry these fields |
| The API no longer accepts `uc04_client_config_ref` | Needs a server-side config source first (D-1, D-2); the existing API tests start cases with it |

Measured effect of the full Part B (tested in the temporary copy): about 55 existing Common tests fail, because their fixtures lack the parent context fields and their workflows rely on `IN_PROGRESS` meaning complete. `02_partB_tests_test_uc04_adapter_mapping_strict.py.txt` holds the stricter tests (all 15 pass against the Part B adapter). Fixtures must be updated together with Part B.

## Decisions these patches wait on

1. D-1/D-2: the key and path by which UC02 supplies the configuration, and whether UC-04 verifies it by version or hash.
2. F-06: the parent stage status for `IN_PROGRESS`, and what re-evaluates UC-04 later.
3. Approval to enable the PLATFORM identity profile (`UC04_IDENTITY_PROFILE`), and the approver role names.
4. IT: the channel for allocation requests and status updates, and the response fields (asset id, model, technician reference). Not built.

## Not part of these patches

No production IT Service Desk adapter, no MCP server and no database client were built. UC-04 reads no configuration from Supabase.

## Part C: `03_partC_supabase_state/` (Supabase as the only persistent store; proposals, NOT applied)
UC-04's own state is stored in two UC-04-owned tables, `uc04_case_state` and `uc04_replay_results` (`docs/proposed_migrations/uc04_state_tables.sql`, written for review and **not run**; the live project has no `uc04_*` tables yet). The code is `TableStateBackend`; it is tested over an in-memory table client only.
* `postgrest_table_client.py.txt`: a `TableClient` for Common over the PostgREST API, including the compare-and-swap `update`. The service-role key stays in Common.
* `worker_hook.py.txt`: `UC04_STATE_MODE=supabase` makes the worker inject the Supabase-backed repository, case context, audit sink and run recorder. Default stays in-memory, so nothing changes until the team approves.
SQLite is not used by UC-04. Removing it from Common (`SqliteCaseStateStore`, `--persist-dir`) and UC-05 was not done (scope decision pending).

## Part D: `04_partD_llm_advisory_worker.patch` (LLM gateway advisory; non-breaking; proposal, NOT applied)
Written against **`origin/Prasanna` (`de63b0c`) Common**, the version that has `build_llm`. It applies cleanly there (`git apply --check`) and **does not apply after Part A**: both change `_Uc04Handler` near the unknown-operation line. Parts A and B were written against the older Common and no longer apply to Prasanna's (his `adapters/uc04_equipment_access.py` changed), so they need rebasing when that Common is merged.

| Change | Where |
|---|---|
| New worker operation `advise_orchestrated`: explains a result the caller already holds (`payload["result"]`). Read-only: it re-evaluates nothing and changes no state, so it cannot change a decision. Evaluation (`evaluate_orchestrated`) is untouched and never calls the advisor. | `workers/uc_worker_runtime.py`, `_Uc04Handler` |
| The advisor agent `Uc04Agent(advisor=build_gateway_advisor())` is built on first use, so a bad LLM configuration affects only the advisory operation and `__llm_status__`, never evaluation | same |
| `llm_status()` for `__llm_status__`: implementation, provider and model name; a configuration problem is reported (names the variable, never a value), not raised | same |

**Not in the patch, on purpose**
* *Worker LLM access.* Prasanna's mechanism already works for UC-04 with no code change: set `UC04_WORKER_LLM_ACCESS=1`, or `llm_access: true` for `uc04` in `worker_config.yaml`. Changing `DEFAULT_LLM_ACCESS["uc04"]` to `True` would also break his own test `tests/llm/test_llm_wiring.py` (it asserts the default is `False`), so that is left to the Common team.
* *The orchestrator calling the new operation.* Where an advisory is stored in the case state, and who sees it, is a Common design decision (state schema, HITL display). Until that is decided, orchestrated runs carry no narrative.
* *Worker timeout.* UC-04's advisory deadline (`LlmAdvisor` 20 s, enforced in `integrations/gateway/model_port.py`) is below the worker's default call timeout (30 s), so no change is required. A longer `UC04_WORKER_TIMEOUT_S` is only needed if the advisory deadline is raised.

**Dependencies for the UC-04 worker interpreter.** `pip install` / `uv sync --extra gateway` from UC-04 (the extra is in `pyproject.toml`, the lock is regenerated). The prepared worker environment under `%LOCALAPPDATA%\hr-onboarding-envs\uc04` was **not** rebuilt, because that would install the outdated Common; rebuild it after Common is merged. The worker also needs `hr_onboarding_common` to be Prasanna's version.

**Validation (temporary copy of Prasanna's Common, UC-04 sources, a scratch environment built from the new lock; nothing in the repository was touched).** A real worker process was started through Common's own `UcWorker`, with no network call (offline mode, and online mode with no key, which stops at configuration validation):
* the provider key reaches the UC-04 worker only with access on; default access strips it; online mode with access off fails visibly (`GatewayConfigurationError`), never silently offline;
* offline: `advise_orchestrated` returns the template advisory (`provider: fake-llm`, `degraded: false`); the advised status equals the evaluated status;
* `evaluate_orchestrated` output is **byte-identical** with and without the patch, and an evaluation replay after the advisory call is unchanged;
* online with no key: the advisory operation errors with `GatewayConfigurationError` while evaluation keeps working; an unknown operation is rejected.
**Not validated:** a real model call.
