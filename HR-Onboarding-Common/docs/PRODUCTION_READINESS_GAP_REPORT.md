# Production readiness: gaps and blockers

Date: 2026-11-04. **Verdict: not ready for production.** The LLM gateway and the Supabase orchestration state are ready for integration testing. The real UC agents cannot yet complete a case together, and every external system other than OpenRouter and Supabase is a mock.

## 1. Blockers (must be resolved before production)

| # | Blocker | Impact | Owner | Evidence |
|---|---|---|---|---|
| B-1 | **OpenRouter key leaked.** The real key was committed in `HR-Onboarding-Common/.env.example` (commit `2e9762b`, pushed to `origin/Prasanna` and `origin/Roshan`). It has been removed from the file, but it is still valid and still in git history. | Anyone with repository access can use the key | Key owner | `git log -S'sk-or-v1-' -- HR-Onboarding-Common/.env.example` |
| B-2 | **UC04 accepts synthetic ids only.** `Uc04Input.case_id` must match `^SYN-CASE-\d{3,}$`, and `employee_ref` is restricted the same way. UC01 issues uuid case ids. | UC04 rejects every real case (`INVALID_INPUT`, stage FAILED), so the real workflow cannot reach readiness | UC04 owner (contract decision) | `test_real_agents_plan_then_verify_documents_then_dispatch_both_branches_*`; `scripts/run_online_orchestration_check.py` |
| B-3 | **UC01's Supabase writes are paused** pending DBA decisions B1–B9: encryption key, audit storage, status vocabulary, atomic writes, `next_employee_no` concurrency and document storage. | No real `onboarding_cases` row is created by intake. Without one, `case_orchestration_state` cannot hold the case (foreign key) | DBA, UC01 owner | `UC01_Employee_Record_Creation/docs/supabase-integration-status.md` |
| B-4 | **Nothing delivers the offer-acceptance event to UC05's mailbox.** | Real UC05 blocks orientation (`UC05_OFFER_EVENT_NOT_FOUND`) | Integration owner (Outlook/HRIS) | `scripts/run_online_orchestration_check.py` output |
| B-5 | **External systems are mocks.** | No real calendar booking, email, Teams message, HRIS record or document storage | Platform | §3 |
| B-6 | **Production authentication and authorization are not wired for the orchestrator API.** Tests use header identity (`IDENTITY_MODE=insecure-dev-headers`). | — | Platform / security | UC05 `tests/conftest.py`; `platform/identity.py` |

## 2. Credentials and permissions

| Item | State |
|---|---|
| `OPENROUTER_API_KEY` | Present in `HR-Onboarding-Common/.env` and `UC05_Orientation-Schedule-Agent/.env` (both git-ignored). **Must be rotated (B-1).** |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Present only in `UC05_Orientation-Schedule-Agent/.env`. The orchestrator needs them in its own environment for `ORCHESTRATOR_STATE_STORE=supabase`. They were not copied into another file. |
| Service-role key | Bypasses RLS. Tenant isolation rests on the orchestrator's query filters (`client_id` on every read and write) and has been tested. A scoped database role with RLS on `case_orchestration_state` would add a second line of defence. |
| `UC01_FIELD_ENCRYPTION_KEY` | Missing (UC01 B1). |
| Microsoft Graph (Outlook) app registration | Not configured. UC02 has a real Graph connector that has never contacted Microsoft. |
| Teams, calendar, HRIS/Zoho, document storage | No credentials and no real connectors. |
| RLS policies, CHECK constraints, triggers on the shared tables | Not verifiable through PostgREST. Needs SQL access or a DBA (UC01 B5/B6). |

## 3. Integration classification

| Integration | Implementation | Verified how | Production-ready |
|---|---|---|---|
| OpenRouter | **Real**, through `LiteLLMGateway` | Live calls from all five UCs; real invalid-key and unknown-model failures | Yes, after key rotation (B-1) |
| Supabase `case_orchestration_state` | **Real** (`SupabaseCaseStateStore`) | Live end-to-end run: one row, versions 7→42, a CAS conflict refused by the live database, a cross-tenant write refused | For integration testing; production also needs B-3 |
| Supabase `onboarding_cases` (read) | **Real** | Live read of the authoritative tenant, employee number and joining date | Yes, read-only |
| UC01–UC05 application code | Real code | Offline suites (all pass) and the online run with real agents | No: B-2, B-3, B-4 |
| Teams | **Mock** (UC05 `connectors/teams/mock.py`) | — | No |
| Outlook | **Mock** in UC01, UC03, UC04 and UC05. UC02 has a real Graph connector, tested only against mocked responses | — | No |
| HRIS / Zoho | **Mock / in memory** (UC01) | — | No |
| Calendar | **Mock** (UC05 `connectors/calendar/mock.py`; `Connectors/calendar/fake.py`) | — | No |
| Email / notifications | **Simulated** (`MockNotificationOutbox`, `synthetic: true`, `transport: mock-notification`) | — | No |
| Document storage | **Local**: UC03 keeps references and metadata in SQLite or memory. UC01 found no Supabase Storage bucket (B4) | — | No |

## 4. Security review

| Topic | Finding |
|---|---|
| API key protection | Read only from the environment, by LiteLLM. Never logged; `redact` covers errors, and real failures were checked for key leakage. **B-1 is the exception.** |
| Tenant isolation | Every Supabase query carries `client_id`. A first write requires the `onboarding_cases` row in the same tenant. Another tenant's case is reported as "not found". Tested offline (F tests) and live. |
| Role-based access | Each operation checks its role allowlist before anything is read or changed (`require_role`). UC04 also checks its caller itself. |
| Approval enforcement | HITL gates: intake mismatch, day-one signoff (a second HR admin), document review, UC04 and UC05 approvals. A signoff for an earlier joining date is withdrawn and cannot be decided. |
| Model authority | Advisory or draft only, in every UC: UC01 `DRAFT_FOR_HUMAN`, UC02 advice validated with a rules fallback, UC03 never approves a document, UC04's narrative changes no decision field, UC05's availability and approvals are deterministic. |
| Prompt injection | UC03 scans document text before any model call (the model is never called when injection is detected; tested). UC05 policy Q&A detects injection. Inputs travel as `<input>` JSON blocks and prompts say the content is data. |
| Structured output | Every call has a Pydantic schema. Invalid output is retried, then `LLMError`, then the caller's deterministic fallback. |
| Reasoning traces | `reasoning.exclude=true` on single-turn calls. Reasoning text is never logged, returned or stored; only counts reach events. The `reasoning` field of UC03's name check is a one-sentence business explanation, capped at 300 characters. |
| Audit and observability | Stage outcomes carry execution, trace and correlation ids. LLM events carry counts only. |
| Data in `case_orchestration_state` | Ids, statuses, plan and document references, and document metadata (for example extracted name and expiry date) in `orchestrator_context.state.submitted_documents`. No document contents, salary or contact details: intake-only fields are never in state. **Review whether the document metadata should be minimised further before production.** |
| Idempotency | Stable per-stage keys; UC-side replay (UC04 BR-21, UC05 ledger); the notification outbox is idempotent by key. |
| Concurrency | Database-enforced optimistic concurrency per case row; branch writes reconcile; whole-state overwrites after another writer are refused. |
| Direct provider SDKs | None (enforced by test). |

## 5. Other gaps found

1. `cost_usd` is not reported for `deepseek/deepseek-v4.1-flash`: LiteLLM's local price map does not know the model. Add a price entry or read OpenRouter's usage endpoint.
2. A `LiteLLMGateway` constructed directly from `get_settings()` carries the built-in default fallback chain (`anthropic/claude-sonnet-5`). `build_llm` clears it online, and every UC uses `build_llm`. Only the legacy "factory missing" branches in UC01 and UC02 could construct it directly.
3. UC03 `src/config.py` still declares `gemini_api_key` and `azure_openai_*`. Nothing reads them since the migration; removing them is UC03's call.
4. UC05's suite showed six HTTP-test errors once, with an FK violation against the live database. They did not reproduce in three later runs. UC05's `conftest.py` forces `UC05_DATABASE_MODE=mock`; the root cause of that one run was not found.
5. Developer environments: UC01 and UC04 had no `.venv`, so the orchestrator's real-worker tests were silently **skipped** before this change. They now exist. CI should fail, not skip, when a worker interpreter is missing.
6. LangGraph checkpoints and the HITL inbox are still SQLite or memory per process. A multi-instance deployment needs them on a shared store, as `case_orchestration_state` now is.

## 6. Deployment requirements

1. Rotate the OpenRouter key; store it in a secret manager; set `OPENROUTER_API_KEY` in the orchestrator, UC02/UC03 (in process) and the UC05 worker only.
2. Set `LLM_MODE=online`, the `LLM_MODEL_*` tiers and, optionally, verified fallbacks (`scripts/openrouter_smoke_test.py --tier all --include-fallbacks`).
3. Set `ORCHESTRATOR_STATE_STORE=supabase` with `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (or a scoped role) and `SUPABASE_SCHEMA`.
4. Resolve B-2, B-3 and B-4, then run the live checks below with the real UC01 in Supabase mode.
5. Shared stores for the LangGraph checkpoints and the HITL inbox when more than one orchestrator instance runs.
6. Real connectors (Graph, calendar, Teams, HRIS, storage) behind the existing ports, each with its own approval.

## 7. Commands to reproduce

```powershell
# offline suites
cd HR-Onboarding-Common; $env:LLM_MODE="offline"; .\.venv\Scripts\python.exe -m pytest
# each UC: cd <UC dir>; $env:LLM_MODE="offline"; .\.venv\Scripts\python.exe -m pytest

# live OpenRouter (billable)
cd HR-Onboarding-Common
.\.venv\Scripts\python.exe scripts\openrouter_smoke_test.py --tier all
.\.venv\Scripts\python.exe scripts\openrouter_uc_live_check.py
.\.venv\Scripts\python.exe scripts\run_online_orchestration_check.py

# live Supabase (writes one case_orchestration_state row for the named synthetic case)
$env:ORCH_SUPABASE_LIVE="1"; $env:ORCH_LIVE_TEST_CLIENT_ID="SYN-UAE-TEST"
$env:ORCH_LIVE_TEST_CASE_ID="62e3b13e-4448-5959-9b2e-56908d33678b"
# plus SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / SUPABASE_SCHEMA in the environment
.\.venv\Scripts\python.exe -m pytest tests\orchestration\test_live_supabase_orchestration.py -s
```
