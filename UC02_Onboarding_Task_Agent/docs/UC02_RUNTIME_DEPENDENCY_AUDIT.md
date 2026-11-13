# UC-02 runtime dependency audit (CSV, mock, fake, fixture, synthetic)

Date: 2026-11-06. Scope: the code that runs when the orchestrator executes UC-02. That is
`UC02_Onboarding_Task_Agent/src/**`, `HR-Onboarding-Common/src/**` and `HR-Onboarding-Common/scripts/**`, with
tests summarised at the end.

Terms searched: `database/data`, `csv`, `read_csv` / `DictReader`, `mock`, `fake`, `fixture`, `sample`,
`synthetic`, `hardcoded`, `task_templates`, `onboarding_tasks`.

Classification:
- **SAFE — test only**
- **SAFE — documentation**
- **SAFE — fixture / dev tooling** (opt-in, labelled, never on the Supabase runtime path)
- **REQUIRES CHANGE** (runtime dependency)
- **REQUIRES REVIEW** (ambiguous, or outside UC-02's scope)

## 1. The CSV export (`database/data`)

| Search | Result | Classification |
|---|---|---|
| `database/data`, `database\data` in any `.py/.ts/.tsx/.ps1/.toml/.yaml/.json` | **No reference** | SAFE: not a runtime dependency |
| `csv`, `read_csv`, `csv.reader`, `DictReader` in runtime code | **No reference** | SAFE |
| Root `client_config_versions_rows.csv` | Not read by any code | SAFE — documentation / reference |

**Verified, not assumed.** `database/` was renamed to `database_backup/` while each of these ran:

- the live preflight
- `plan` (process 1)
- `resume` (process 2)
- `branches` (process 3)
- `negative`
- `verify`
- the real backend (`serve_orchestrator.py`, with real UC01, UC04 and UC05 workers)
- both full test suites

All of them succeeded with the folder absent, and the folder was restored afterwards. See
`UC02_SUPABASE_IMPLEMENTATION_REPORT.md` §9.

## 2. Runtime mock dependency found and removed

| Location | Finding | Classification | Resolution |
|---|---|---|---|
| `HR-Onboarding-Common/.../adapters/uc02_tasks.py` (before) | The orchestrator built UC-02 with `build_service(require_plan_approval=False)` and no store setting. UC-02 therefore ran on an **in-memory store with the synthetic configuration registry** (`synthetic.build_registry()`). Plans were lost on restart, and the configuration came from code, not Supabase. | **REQUIRES CHANGE: done** | `uc02_tasks.uc02_sub_agent` builds UC-02 from `UC02_STORE_MODE` through `adapters/uc02_runtime.build_service`. In `supabase` mode it uses `runtime.build_runtime_service`, with the configuration from `SupabaseConfigSource` and the store `SupabaseTableClient`. It fails closed and never falls back. |
| `uc02_replanning.py`, `uc02_tasks.request_uc02_date_change` / `relay_uc02_decision` (before) | After a restart `deps._uc02_sub_agent` was `None`, so replanning and date changes returned "UC02 is not available". | **REQUIRES CHANGE: done** | The sub-agent is rebuilt on demand over the same store. In Supabase mode the plan is reloaded from the database. |
| `serve_orchestrator.py` | Nothing stopped a Supabase-backed workflow state from being combined with an in-memory UC-02. | **REQUIRES CHANGE: done** | In real mode, the server refuses to start with `ORCHESTRATOR_STATE_STORE=supabase` unless UC-02's store mode is `supabase` (`UC02_WORKER_ENV__UC02_STORE_MODE`). |

## 3. Remaining matches in runtime packages, classified

| File(s) | Match | Classification | Why |
|---|---|---|---|
| UC02 `synthetic.py`, `synthetic_profiles.py`, `seed.py`, `supabase_synthetic.py`, `sub_agent/supabase_demo.py`, `sub_agent/mock_handoff.py` | synthetic registry, profiles and fixture rows | SAFE — fixture / dev tooling | Used by unit tests, the demos and the guarded `setup` stage (`--confirm-write`, `UC02_ALLOW_FIXTURE_WRITES`, development only). Not imported on the orchestrator's Supabase path. |
| UC02 `sub_agent/runtime.py` → `build_registry()` | the synthetic registry in `memory` / `sqlite` mode | SAFE — test only | Reached only when `UC02_STORE_MODE` is unset, `memory` or `sqlite`. `task_plan.store` then reads `memory` (shown as "process memory (not durable)"), and production (`UC02_ENV=production`) refuses those modes. |
| UC02 `sub_agent/adapter.py`: default `OnboardingService(build_registry())`, `load_fixture`, `data/mock/*` | defaults and fixture loading | SAFE — fixture | The orchestrator always passes its service and never passes `fixture`. Fixtures are used only by UC-02's own demo API route. |
| UC02 `sub_agent/adapter.py`: `make_mock_outlook()` | mock Outlook object | SAFE | The orchestrator path sets `prepare_reminders=False`, so nothing is sent through it. UC-02's own RuntimeFlow uses the connector factory (mock unless `UC02_OUTLOOK_CONNECTOR=graph`). |
| UC02 `agent/fake_llm.py`, `sub_agent/fakes.py`, `agent/advisor.py` | Fake LLM | SAFE (labelled) | The orchestrator asks for `llm_behaviour="gateway"`, so it uses the orchestrator's own LLM: FakeLLM offline, LiteLLM/OpenRouter online. Advice is advisory only and validated, and a degraded result is marked as such. |
| UC02 `supabase_config.py`: mock/synthetic acceptance | `allow_synthetic` | **REQUIRES REVIEW** (deployment) | Gated by `config_safety`: only with `UC02_ENV=development|test` **and** `UC02_ALLOW_SYNTHETIC_CONFIG=1`, and refused in staging and production. Every client in the development database is marked mock or synthetic today, so the local `.env` opts in. A production deployment must not set it, and needs client configurations approved for real use. |
| UC02 `calendar_uae.py` | built-in UAE holidays | SAFE | Used only when no client calendar is required (memory mode). Supabase mode requires `client_holidays` and never assumes a calendar. |
| UC02 `providers.py` | "mock/local adapters" | SAFE — not imported | Nothing in `src/` imports it (legacy integration seams). |
| UC02 `client_configuration/*` (`task_templates`, `onboarding_tasks` names) | reading and seeding configuration documents | SAFE — dev tooling | Seeding and retrieval tools for configuration owners. They are not on the planning path. |
| UC02 `supabase_rest.py`, `supabase_store.py`, `supabase_schema.py` (`task_templates`, `onboarding_tasks`) | live table names | SAFE | The Supabase implementation itself. |
| UC02 `sub_agent/flow.py`, `flow_runtime.py`, `status_updates.py`, `sub_agent/api.py` | mock connectors, fixtures in comments | SAFE | UC-02's own signed-event runtime (Supabase-only, separate from the orchestrator path). Its mock pieces are opt-in and labelled. |
| Common `workflow_models.DATA_ORIGIN = "synthetic"` (`StageOutcome.data_origin`) | every outcome labelled `synthetic` | **REQUIRES REVIEW** | This is an existing contract (`Literal["synthetic"]`), and it now also labels UC-02's real Supabase-backed outcomes. Changing it is a shared-contract decision, so it was **not changed**. `task_plan.store` / `result_summary.store` say `supabase` for a real plan. |
| Common `orchestration/notifications.py` (`MockNotificationOutbox`) | mock document-request transport | REQUIRES REVIEW (UC-03, out of scope) | UC-03's document request is not sent anywhere real. It does not affect UC-02. |
| Common `Connectors/*/mock.py`, `fake.py`, `workers/uc_worker_runtime.py` (mock/sample) | UC-01, UC-04 and UC-05 connectors and workers | REQUIRES REVIEW (other UCs, out of scope) | Not UC-02. |
| Common `scripts/run_workflow_demo.py` | stand-ins for UC01, UC02, UC04 and UC05 | SAFE — fixture | Demo mode (`--mode demo`), labelled `data_origin=synthetic`. |
| Common `scripts/verify_uc02_supabase_workflow.py` | UC01 binding, UC04/UC05 stand-ins | SAFE — verification tool | Opt-in live check. UC-02 is real there, and the stand-ins are labelled. |
| Common `orchestrator_agent.py`, `workers/config.py`, `context_layer/hr_policy.py` ("hardcoded") | comments that say nothing is hard-coded | SAFE — documentation | — |

## 4. Tests

| Location | Matches | Classification |
|---|---|---|
| `UC02_Onboarding_Task_Agent/tests/**` | fakes (`FakePostgrest`), fixtures (`tests/fixtures/live_schema.json`, `existing_clients_config_snapshot.json`), synthetic data | SAFE — test only |
| `HR-Onboarding-Common/tests/**` | `_fake_postgrest.py`, `_fake_uc02_supabase.py`, `_workflow.py` doubles, synthetic data | SAFE — test only. `conftest.py` pins `UC02_*` / `ORCHESTRATOR_STATE_STORE` empty, so no test reaches a live database. |
| `UC02_Onboarding_Task_Agent/data/mock/*.json`, `data/UC02_Synthetic_Onboarding_Test_Data.json` | fixtures | SAFE — fixture (demo API / tests) |

## 5. Frontend

`onboarding-ui-prototype/src/data/mockData.ts` is used only by the `demo/*` routes (`UC02Detail` and the other demo
pages). The Control Center's case pages read `/v1/orchestration/cases/*`, which is the persisted
`case_orchestration_state`. Classification: **SAFE — demo pages**.

`onboarding-ui-prototype/.env` holds `SUPABASE_SERVICE_ROLE_KEY`. Vite exposes only `VITE_*` variables, and the
built bundle was checked and **does not** contain the key. The key is not needed there and should be removed from
that file (**REQUIRES REVIEW**: it is the owner's local file, so it was not edited).
