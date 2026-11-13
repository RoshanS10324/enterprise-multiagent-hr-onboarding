# UC-02 final architecture and verified status

> **Update 2026-11-06.** The orchestrated path (UC-02 inside the central orchestrator) now also runs on Supabase:
> `UC02_STORE_MODE=supabase`, plan revisions per plan version, tenant-scoped queries, and the result persisted to
> `case_orchestration_state`. See `UC02_SUPABASE_ARCHITECTURE.md`, `UC02_SUPABASE_DATABASE_MAPPING.md`,
> `UC02_RUNTIME_DEPENDENCY_AUDIT.md` and `UC02_SUPABASE_IMPLEMENTATION_REPORT.md`. Where those differ from this
> document, they are current.

As of 2026-11-04, branch `logesh`. This document supersedes older statements elsewhere that UC-02 has "no Supabase", "no LLM", "484 tests" or
"works only against InMemoryTableClient". Where a historical document disagrees with this one, this one is right.

## 1. Architecture

```
UC-01 (event, case, offer, form)           HR approval portal            UC-03 / UC-04 / UC-05
          |  signed event                        | signed callback              ^ signed triggers / status
          v                                      v                              |
   RuntimeFlow (flow_runtime.py)  --- OnboardingFlow (flow.py): ledger, gates, document state machine, downstream
          |                                   |
   OnboardingService (service.py) -- planner / selection / checklist / tracker / reminders / status_updates
          |                                   |
   Fake LLM review (agent/)           Outlook connector (mock default | graph)
          |
   SupabaseTableClient (supabase_store.py) -- SupabaseRestClient (stdlib HTTP) --> Supabase Development
   SupabaseConfigSource (supabase_config.py): approved client configuration (read-only)
```

* Runtime selection (`runtime.py`): `UC02_STORE_MODE=supabase` is required; `memory` and `sqlite` are rejected, in production always. `InMemoryTableClient` and
  `SqliteTableClient` exist only for unit tests that inject them. Nothing falls back silently.
* Environment (`envfile.py`): `.env` then `.env.local` from the UC-02 folder, the process environment wins; five signing secrets; per-stage validation.
* The HTTP API (`sub_agent/api.py`) builds the same Supabase-backed runtime lazily and answers 503 with a reason if it cannot.

## 2. Actual workflow sequence (verified live on the Development database with the synthetic case)

1. `run`: signed UC-01 event -> case/offer/form read -> approved (synthetic) configuration -> checklist (13 tasks, 12 dependencies) -> Fake LLM review -> plan
   `submitted` -> HR review email draft (mock Outlook) -> approval request `plan_approval`.
2. `decide`: signed callback `approved` | `rejected` (reason required) | `changes_requested` (new plan version, new request, new review). Approval prepares the
   assignments (9), document requirements and notices; nothing is sent.
3. `send`: a different HR lead authorises each prepared message; it goes through the connector (mock by default, `real=False`).
4. `documents`: the document source reports `submitted -> under_review -> verified | rejected` through the signed channel; the mandatory-document gate decides
   when UC-04/UC-05 may be triggered; triggers are mock (`--mock-downstream`) or `not_configured`.
5. `tasks`: owners / mock external use cases progress tasks wave by wave; `--as-of` drives overdue detection and the monitoring sweep (reminder drafts and
   escalation records only).
6. `recover`: a new runtime reloads everything from Supabase; replaying the event creates nothing.
7. `verify`: read-only read-back of what is stored.

The same stage functions run end to end, in memory and with nothing real, in `demo/uc02_local_mock_demo.py` (isolated local demo; 22 checks). The Supabase stages above are
the Development integration demo. Verified Development task state (read back 2026-11-04): 13 tasks, 4 complete, 7 ready, 2 blocked; the onboarding is in progress, not complete.
Blocked: `medical_test` waits for `visa_status_change`; `biometrics` waits for `medical_test`. Ready tasks need their owner or the external use case to report; the `tasks` stage can
progress them and is safe to repeat. Overdue monitoring and reminders: derived from the clock; drafts only; no scheduler exists (future platform/UC-06 integration).

## 3. Database persistence mapping

Written by UC-02 (synthetic case only in the demo): `onboarding_tasks` (`status_summary.uc02` marks UC-02-owned rows), `task_dependencies`,
`plan_revisions`, `approval_requests`, `agent_runs`, `audit_events` (events, plus the append-only `UC02_CASE_STATE` snapshot in `after_state` carrying plan
status, assignments, documents, downstream tracks, messages with fingerprints, analyses, the idempotency ledger, delay counters).
Read only: `onboarding_cases`, `offers`, `onboarding_forms`, `offer_forms` (UC-01), `client_configs`, `client_config_versions`, `task_templates`,
`client_document_requirements`, `client_service_policies`, `client_holidays`, `client_milestone_templates`.
No migration or RLS change was made. Detailed column mapping: `UC02_SUPABASE_MAPPING_AND_PROPOSED_MIGRATION.md` (its "not implemented" lines are historical;
the mapping client is built and live-verified).

## 4. Responsibilities

* **Fake LLM** (`agent/fake_llm.py`, `analysis.py`): deterministic, labelled synthetic, summarises the plan and flags concerns for HR. It cannot approve, reject,
  send or change a plan; the decision exists only in a signed callback from an authorised role. No real model call has ever been made.
* **HR approval:** `ApprovalCallback` signature, role, plan-version and self-approval checks; decision, approver, timestamp, reason persisted and audited.
* **Documents:** state machine with mandatory gate; resubmission after `rejected` supported; statuses are reported by a document source. No OCR, antivirus or identity
  verification exists and none is claimed.
* **Tasks:** stored states `blocked`, `ready`, `in_progress`, `complete`, `cancelled`; `overdue` is derived from the due date and the clock; failed external
  outcomes keep the task open with a reason and dependants blocked. Independent tasks are ready together (`parallel_batches`).
* **Due dates and reminders:** from lead times, joining date, dependencies and the approved calendar; the sweep records each escalation once; reminders are drafts.
* **Access provisioning:** UC-02 only triggers and tracks. In the demo the access tasks are completed by labelled MOCK UC-04 status updates.
* **Retries:** sends and triggers retry up to 3 times with write-ahead intents; Graph adds capped backoff.

## 5. Verified results

| Check | Result |
|---|---|
| Full test suite | **1462 passed, 1 skipped, 2521 subtests passed** |
| Evaluation suite | **37 / 37 scenarios passed** |
| Live preflight (Development) | schema OK, 10 fixture groups present, `ready_to_run true` |
| Live `run` replay | event reported `duplicate`; workflow already existed |
| Live `tasks --waves 2` | wave 1: `hr_record_create`; wave 2 in parallel: `collect_documents`, `laptop_request`, `orientation_slot`; 7 tasks became ready; `medical_test`, `biometrics` stayed blocked |
| Live read-back (`verify`) | 13 tasks (4 complete, 7 ready, 2 blocked), 12 dependencies, approval APPROVED, 9 assignments, documents 4 mandatory verified, both downstream `triggered` (mock), 128 audit events, 62 snapshots, `uc01_tables_written_by_uc02 = 0` |
| Live overdue sweep (as-of 2026-11-20) | 6 overdue, 6 escalations, 5 reminder drafts, then 0 new on repeat |
| Live `recover` | replay `duplicate`; tasks 13/13, approvals 1/1, assignments 9/9; task progress intact in the new runtime |

Live checks used the Development project, synthetic data, mock Outlook and mock downstream. They do **not** show real email delivery, real UC-04/UC-05
integration, real document verification or real approved client configuration.

## 6. Test results

Final run on 2026-11-04 (`python -m pytest tests -o addopts="" -p no:cacheprovider -q`): **1462 passed, 1 skipped, 2521 subtests passed**. Evaluation (`python evaluation
un_evals.py`): **37/37**.
New this round: `tests/test_envfile.py` (31), `tests/test_task_lifecycle_supabase.py` (9: parallel waves, dependency order, completion read back from the database, mock access
provisioning, due dates, overdue, sweep once-only, restart recovery, no UC-01 writes), plus the Supabase-only workflow tests added earlier. All use a mocked Supabase transport.
**Static typing:** `python -m mypy src` reports 39 findings in 10 files (mostly `calendar_uae.py` attribute typing and `**kwargs` typing in older modules; none is a runtime failure
observed in tests). The older "mypy clean" statement applied to 16 modules and no longer holds repo-wide; this is an open quality item.

## 7. Known external dependencies

See `UC02_INTEGRATION_CHECKLIST.md` sections 3 and 4.
