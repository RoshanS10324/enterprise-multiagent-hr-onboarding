# UC-04 session handover (written 2026-11-04) — read this first

Owner: Roshan (UC-04 Equipment & Approved Access Management). Branch `Roshan`, HEAD `ad4d5a6`. **Nothing is committed or pushed.** Do not commit, push, reset, rebase, switch branches or stash without explicit approval. Do not touch the uncommitted UC-02 `advisor.py` fix. Never print `.env` contents, keys or URLs.

## Working style the owner wants
- UC-04 only. Do not connect, change or remove anything in Common, UC-01/02/03/05 unless asked. Put UC-04 work inside `UC04_Equipment_Access_Integration_Ready/`, not in new top-level folders.
- Plain language answers. Say clearly what is verified, what is mock, what is assumed. Do not invent business rules, columns or contracts: document the decision needed instead.
- Edge cases must be built against the mock and work unchanged on live/production (ports and adapters). Do not push the Supabase project into live use unasked.
- Autonomy L1: UC-04 detects, assesses, drafts and proposes; people approve; IT executes. Nothing is sent, ordered, assigned, reserved, booked, removed or cancelled by UC-04.

## Commands (PowerShell, repo root; interpreter `.\.venv\Scripts\python.exe`)
- Tests: `cd UC04_Equipment_Access_Integration_Ready; ..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -W ignore -q` -> with the older Common in the working tree: 680 passed, 7 skipped; with Prasanna's Common (`HR_ONBOARDING_COMMON_SRC` set): 686 passed, 1 skipped, 0 failed. The skips are the gateway factory tests (older Common) or the older-Common-only test (Prasanna Common). History: 639 at commit `6e36039`, 648 at `341b743`.
- Evals: `..\.venv\Scripts\python.exe evaluation\run_evals.py` -> 12/12. Lint: `..\.venv\Scripts\python.exe -m ruff check --select F,E9 src connectors mcp policy guardrails evaluation`. Demo: `run_demo.py`, `run_agent.py`.
- Multi-agent demo: `cd HR-Onboarding-Common; & "$env:LOCALAPPDATA\hr-onboarding-envs\common\Scripts\python.exe" scripts\run_workflow_demo.py` (add `--real` for real workers). Prepared envs: `%LOCALAPPDATA%\hr-onboarding-envs` (common, uc01, uc04, uc05).
- Common tests (untouched tree): 280 pass, 4 fail. The 4 assert UC-02 is still broken; the uncommitted UC-02 fix makes them fail.
- Write long edits with the Edit/Write tools; shell heredocs with nested quotes broke more than once.

## What was built this session (all uncommitted, all in UC-04)
- **Repository/audit/run interfaces** (`store.py`, `ports.py`, `audit.py`, `runs.py`) and **persistence package** `src/sub_agent/persistence/` (DatabaseCaseRepository, DatabaseCaseContext, DatabaseAuditSink, DatabaseAgentRunRecorder, TableStateBackend, serialization). Audit entries recorded before the verified client is known are held and flushed once it is bound (`AuditLog(defer_unattributed=True)`); those that never get a client stay in memory (`unpersisted`).
- **Platform identity profile** `guardrails/identity.py` (uuid case ids), opt-in via `identity.configure("PLATFORM")` before importing `sub_agent`. Role aliases / acknowledge roles in policy (empty by default).
- **Recovery:** submission intent recorded before the IT call (`SUBMISSION_IN_DOUBT`), adapter errors block blind retry, `acknowledge_orchestrated`, `status_orchestrated`, `submit_orchestrated`.
- **Advisors:** `AdvisorPort`, `FakeLlmAdvisor` (default, offline), `LlmAdvisor` over an injected `ModelPort` (shared LiteLLM gateway supplied from outside), `build_advisor()`. Model text only goes into `narrative`; decisions never change.
- **Config:** `config_version` + `config_digest` on every result; a config change triggers reassessment. UC-04 reads no config path.
- **Withdrawal / client change:** UC-04 stops local progress and drafts a notice to IT (`NOTIFY_IT_WITHDRAWAL`, `NOTIFY_IT_SUPERSEDED_REQUEST`); it cancels nothing.
- **Edge cases EC-29..36, EC-40, EC-42** (see `specs/UC04_SPEC.md` sections 15-16 and `tests/test_scenarios_complete.py`): structured message drafts, IT request drafts (loan device, collection slot, reservation, exchange, courier, access removal, equipment change), `project_id`, `deployment_date`, country calendar/timezone in `country_rules`, `evaluate_cohort` + `plan_slots` (`cohort.py`), `transfer.py`. Proposals carry a structured `payload`, `executed` is always false. EC-41 is excluded (not assigned to Roshan).
- **Production parity:** `tests/test_production_parity.py` runs 13 scenarios through mock wiring and production-style wiring (database case context + state tables + audit sink + run recorder over a fake table client enforcing real columns); results are identical.
- **Supabase connector** (optional, off by default): `integrations/supabase/` (PostgREST table client, `connect.build_agent`, `check_connection.py`, guarded `live_smoke_test.py` / `run_uc04_and_store.py`, 12 offline tests). The packaging test now allows network code only in `integrations/` and checks `src/` never imports it.
- **Proposed, NOT applied:** `docs/proposed_migrations/uc04_state_tables.sql` (tables `uc04_case_state`, `uc04_replay_results`); `docs/common_patches/` Part A (worker ops, AT_RISK/CANCELLED mapping, inline config, opt-in identity profile set at worker start), Part B (strict inputs, IN_PROGRESS mapping, remove config path from API), Part C (Supabase state wiring). Part A was validated in a temp copy of the workspace (same 4 baseline Common failures, no new ones).

## Supabase facts (verified read-only; do not assume they are still true)
- Live project is reachable; 38 tables at the last check. Shared tables used by UC-04 adapters exist. `uc04_*` tables do **not** exist.
- `audit_events` is append-only (trigger refuses UPDATE/DELETE). ~184 synthetic audit rows tagged `UC04TEST-...` from my test runs remain and cannot be removed without DB admin. The 4 synthetic cases are soft-deleted (`is_deleted = true`); my `agent_runs` rows were deleted. Never hard-delete cases (the cascade hits the append-only table).
- `onboarding_cases` holds candidate name/email: always select named non-personal columns, never `*`. `onboarding_attributes` is a text array. `agent_runs.status` check allows `proposed`/`exception`, not `failed`. `client_config_versions` currently has only `draft` rows.
- Live `role_access_matrix`, `provisioning_*`, `equipment_handovers` were empty; real cases lack an approved onboarding form.
- The production audit sink needs an actor and a client on every event; the real entry points set them, a direct `service.acknowledge_adapter_error` call does not.

## Known integration blockers (not UC-04 bugs)
Real UC-01 makes uuid case ids -> UC-04 needs the PLATFORM profile on. Demo HR users have role `hr_admin`, not in UC-04's allowed roles (alias needs governance). The parent never sends requested items or approvals to UC-04. HR approves/rejects proposals manually (owner decision), so no UC-04 decision input is planned. UC-02 and UC-05 fail on their own issues in `--real`.

## Decisions still needed (owners in brackets)
D-1/D-2 how UC-02 supplies config and whether UC-04 verifies version/hash [Common/UC-02]; D-3 shared-table extension vs UC-04-owned tables (owner chose UC-04-owned tables; migration unapplied) [Common]; D-5 vocabulary (`approval_class`, item kinds, approver role names, HC-08 exception approver) [governance]; D-6 timezone use; D-7 lead-time rule; D-8 config hash format and re-approval after a config change; F-06 stage status for IN_PROGRESS and what re-evaluates UC-04; IT channel and response fields (asset id, model, technician ref); project-change and cohort/transfer contracts; whether to remove SQLite from Common/UC-05 (asked, unanswered).

## Status update (2026-11-04, later the same day)
- UC-04 work committed on `Roshan` (`6e36039`), not pushed. Traceability and spec brought in line with the code (EC-40, EC-42, EC-35 project change).
- **HR approve/reject is manual, done by HR outside UC-04** (owner decision). No approve/reject loop is to be built in UC-04; UC-04 only reads recorded approvals.
- UC-04's edge cases from the Business Framework: EC-10, 11, 12, 13, 19, 20, 22, 23, 29-40, 42, 44, 51, 52 (23 total). All built and tested against mocks. EC-41 excluded; the rest belong to other stages.
- Case-id alignment: real UC-01 uuids are accepted via the PLATFORM profile. `UC01-REQ-...` exists only in the demo's fake UC-01, so it is not accepted on purpose.
- UC-04 code work is complete for what can be built without other owners. Everything left needs someone else (below).

## LLM gateway wiring (added after the commits above, uncommitted)
- `integrations/gateway/` (`model_port.py`, `factory.py`, `offline.py`, README) plugs the shared `LLMClient` into the existing `LlmAdvisor`. Pattern from UC05 on `origin/Prasanna` (`de63b0c`); Prasanna changed no UC01-04 source. Core change: `AdvisoryReport.degraded` / `degraded_reason` (additive). Tests: `tests/test_gateway_advisor.py` (needs a Common with `build_llm`; set `HR_ONBOARDING_COMMON_SRC`, otherwise the factory tests skip).
- **Not live-verified.** Offline only. `pyproject.toml` has the optional `gateway` extra (path source `../HR-Onboarding-Common`, exact casing) and `uv.lock` is regenerated (+38 packages, none changed). Prasanna's Common is **not merged** into Roshan or Orchestration-Branch; the prepared worker env was NOT rebuilt (it would install the outdated Common). `GatewayModelPort` now enforces the advisor's 20 s deadline (`AdvisoryTimeout`), below the worker's 30 s call timeout.
- Common-side proposal (not applied, needs Common team approval): `docs/common_patches/04_partD_llm_advisory_worker.patch` (separate read-only `advise_orchestrated` op + `llm_status()`); worker key access via `UC04_WORKER_LLM_ACCESS=1` (existing mechanism). Parts A and B no longer apply to Prasanna's Common and conflict with Part D; they need rebasing.

## Remaining, all blocked on someone outside UC-04
1. A person runs `docs/proposed_migrations/uc04_state_tables.sql`; then `build_agent(state_mode="supabase")` and a live check with `integrations/supabase/check_connection.py`.
2. Common owner applies `docs/common_patches/` Part A (then B, C) after governance approval.
3. IT supplies the service-desk channel and response fields -> then a production `ITServiceDeskPort` adapter.
4. Governance answers D-1..D-8, F-06, HC-08, EC-33 fields, EC-35 `project_id` source.
