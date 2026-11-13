# Workflow implementation report: UC02 planning first, UC03 owns documents, replanning loop

Date: 2026-11-04. Branch: `Prasanna`, uncommitted working tree.

This change implements the revised workflow
`UC01 → UC02 (plan) → UC03 (collect, store, validate) ⇄ UC02 (replan) → UC04 ∥ UC05`. Design and
behaviour are described in [`UPDATED_ONBOARDING_WORKFLOW.md`](UPDATED_ONBOARDING_WORKFLOW.md),
[`UC02_UC03_RESPONSIBILITY_MIGRATION.md`](UC02_UC03_RESPONSIBILITY_MIGRATION.md) and
[`UC02_UC03_REPLANNING_CONTRACT.md`](UC02_UC03_REPLANNING_CONTRACT.md).

Authorization: changes to UC02 and UC03 were explicitly authorized. UC01, UC04 and UC05 sources are
unchanged (`git status` shows no change under those three folders). `hr-recruitment-common` is not in
this repository and was not touched.

## 1. Files changed

**Orchestrator (`HR-Onboarding-Common/src/hr_onboarding_common/orchestration/`)**

| File | Change |
|---|---|
| `state.py` | Stage order `intake → plan_tasks → document_collection → document_submission → validate_documents → replan_tasks → …`. New conditional stage `replan_tasks` (`NOT_REQUESTED` until asked for). `DOCUMENT_STAGES`. `REPLANNING_WAIT_CODE`. New fields: `onboarding_plan`, `plan_history`, `revised_plan`, `requirement_change_requests`, `document_validation_feedback`, `replan_round`, `document_route`. Back-fill for older cases. |
| `orchestrator_agent.py` | New graph wiring; `evaluate_document_result` (replan, stale, verified, wait routes, loop bound); `apply_revised_plan`; `await_replanning`. `submit_documents` stores through UC03 and is idempotent for duplicates. New `request_document_requirement_change`. `verified_context` records the plan id and version. |
| `adapters/uc03_document_collection.py` | **New.** UC03 collection for the plan version: builds the UC02 → UC03 request, opens it in UC03, sends one request per plan version (`…:r{version}`). |
| `adapters/uc02_replanning.py` | **New.** UC02 replanning stage: feedback check (missing or stale), `replan_documents`, and the `onboarding_plan`, `plan_history` and `revised_plan` records. |
| `adapters/uc02_tasks.py` | Planning defines the plan's document requirements in UC02 and builds `onboarding_plan`. An applied date change keeps the plan version in step with UC02. |
| `adapters/uc03_documents.py` | Validates against the plan's requirements. Passes `document_revision` and UC03's derived flags (`visa_required`, `sponsorship_applicable`). Builds `DocumentValidationFeedback`; replanning takes precedence. HR waivers are never re-added. UC03 collection-service accessor. |
| `adapters/uc02_document_collection.py` | Reduced to a deprecated re-export of the UC03 adapter. |
| `workflow_models.py` | `OnboardingPlanRecord`, `PlanHistoryEntry`, `DocumentCollectionRequest`, `DocumentValidationFeedback`, `RecommendedTaskChange`, `RevisedPlan`, `RequirementChangeRequest`. Extended `RequiredDocument`, `DocumentCollectionRecord`, `SubmittedDocument`, `DocumentMetadata` and `WorkflowStateModel`. |
| `dependencies.py` | `plan_tasks` needs intake only. Collection needs the plan's documents. Validation needs UC03's collection. `replan_tasks` needs the plan. Branches unchanged (documents verified and task plan). |
| `state_sync.py` | Ownership: UC02 owns the plan fields; UC03 stages own the collection, validation and feedback fields. |
| `agent_contract.py` | `USE_CASE_OF`: collection and submission are UC03, `replan_tasks` is UC02. New error codes and their categories. `plan_id` and `plan_version` in the shared context. |
| `workflow.py` | Stage operations and inputs for the new order (plan version handed to UC03 and to both branches). |
| `deps.py`, `bootstrap.py` | `uc03_document_store_path` (SQLite under `persist_dir`) and `max_replan_rounds`. |
| `api.py` | `POST /cases/{id}/documents/requirements`. Case view exposes plan and feedback fields. The start body takes UC01's approval facts. |
| `guardrails/policy.yaml` | `document_requirement_change_roles: [hr_admin]`. |
| `scripts/run_workflow_demo.py` | Stand-ins for planning and replanning; the walk-through shows the replanning loop. |

**UC02 (`UC02_Onboarding_Task_Agent/src/onboarding_task_management/`)**:
`service.py` (`define_document_requirements`, `replan_documents`), `planner.py` (`replan(...,
add_attributes=)`), `contracts.py` (`DocumentSpec`, `DocumentFeedback`, `DocumentReplanOutcome`),
`errors.py` (`StalePlanVersionError`), `sub_agent/adapter.py` (`WorkflowResult.plan_version`).

**UC03 (`UC03_Document_Validation/src/`)**: `sub_agent/collection.py` (**new**), `sub_agent/models.py`
(`document_revision`), `state/models.py` (`received_revisions`), `sub_agent/validator.py`
(revision-aware duplicate check).

**Tests**: new `HR-Onboarding-Common/tests/orchestration/test_uc02_uc03_replanning_workflow.py` (28
tests), `UC02_Onboarding_Task_Agent/tests/test_document_replanning.py` (7) and
`UC03_Document_Validation/tests/unit/test_uc03_collection.py` (6). Test helpers (`_workflow.py`,
`_lifecycle.py`, `_uc02_real.py`) and 13 existing orchestration test modules were updated where they
asserted the old order (planning after verification, collection labelled UC02, UC02's long-fixed
import defect).

**Docs**: the four documents in `docs/` listed above. `README.md` (workflow section, demo steps, known
issues, links). A "superseded" note at the top of `docs/ORCHESTRATION_WORKFLOW.md` and
`HR-Onboarding-Common/docs/STATE_DRIVEN_ONBOARDING_WORKFLOW.md`.

## 2. Graph changes

- `intake_mismatch_proposal → plan_tasks` (was `→ document_collection`).
- `plan_tasks → document_collection | await_plan_tasks` (was `→ dispatch_branches`).
- `document_submission → validate_documents | evaluate_document_result | await_documents`.
- `validate_documents → evaluate_document_result → record_verification | replan_tasks |
  document_collection | await_document_correction | await_document_review | await_validation_retry |
  await_replanning`.
- `replan_tasks → apply_revised_plan | await_replanning`; `apply_revised_plan → document_collection`.
- `record_verification → dispatch_branches` (was `→ plan_tasks`). The fan-out, merge, readiness,
  signoff and welcome are unchanged.

The graph now has two bounded cycles: re-dispatch (`MAX_DISPATCH_ROUNDS`) and replanning
(`max_replan_rounds`, default 3, per invocation).

## 3. State changes

The new fields and their owners are listed in
[`UPDATED_ONBOARDING_WORKFLOW.md` §6](UPDATED_ONBOARDING_WORKFLOW.md#6-state-ownership-and-persistence).
`submitted_documents` remains, now as the projection of UC03's registry (with `storage_id` and
`plan_version`). Document contents are never stored. References and the extraction metadata UC03
needs are stored, as before; the metadata is still excluded from every API response.

## 4. Tests and exact results

Environment: Windows 11, Python 3.12.16 via `uv` 0.13.7. UC01, UC04 and UC05 worker environments were
built by `scripts/run_tests.ps1` under `%LOCALAPPDATA%\hr-onboarding-envs`, so the real-worker tests
ran rather than being skipped.

| Suite | Before the change | After the change |
|---|---|---|
| HR-Onboarding-Common (real workers) | 556 passed, 6 failed. `test_orchestrator_e2e.py` could not be collected: unresolved merge-conflict markers were committed in it. | **602 passed, 0 failed, 0 skipped** (`test_orchestrator_e2e.py` resolved and included) |
| UC05 (`run_tests.ps1 -Suite uc05`) | 544 passed, 1 failed | 544 passed, 1 failed: the same test (`test_status_vocabularies_match_what_the_orchestrator_validates`, pre-existing, `withdrawn` decision vocabulary) |
| UC02 | 2 failed on the original source (`test_flow_supabase…recovered_after_a_restart`, `test_a_stalled_approval_escalates_past_its_gate_sla`) | 1369 passed, 3 failed, 10 skipped: the same 2, plus `test_the_audit_trail_is_persisted_and_survives_a_restart`, which is order-dependent (same-timestamp audit ids) and passes when re-run in isolation |
| UC03 | not run separately before the change | 206 passed (200 existing + 6 new) |

The six pre-existing common failures were all old-order or UC02-import-defect assumptions; they are
rewritten for the new order and pass. The demo (`uv run python scripts/run_workflow_demo.py`) runs to
completion with the real UC03, including a replan round.

Coverage of the 31 required test areas. `T` is `test_uc02_uc03_replanning_workflow.py`; other files are
named.

| # | Area | Where |
|---|---|---|
| 1–2 | Intake triggers UC02; invalid intake prevents it | T `test_01`, `test_02`; `test_execution_order.py` (checkpoint supersteps) |
| 3–5 | Plan defines documents; UC03 gets plan and version; UC02 neither collects nor validates | T `test_03`–`test_05`; `test_uc02_real_orchestration.py` (real UC02) |
| 6–12 | Collection, storage with metadata, duplicate idempotency, expired, missing, revalidation, HR review (unauthorized attempt consumes nothing) | T `test_06`–`test_12` (real UC03 for 9 and 11) |
| 13–17 | UC03 reports, UC02 receives and versions, UC03 applies and revalidates | T `test_13_to_17` (real UC02 and UC03 collection; node trace asserted) |
| 18–19 | Failed replan blocks downstream; no replan for a plain resubmission | T `test_18`, `test_19`, plus technical-failure and loop-bound tests |
| 20–24 | Branches only after validation; concurrent; same context; UC03 failure or HR review blocks both | T `test_20_to_22` (barrier, gate events), `test_23`, `test_24`; `test_execution_order.py` |
| 25–28 | Duplicate events; restart restores plan version and corrections; replan not repeated after a crash | T `test_25`, `test_26_to_28` (SQLite case store and UC03 registry), `test_28_…crash` (real UC02 ledger) |
| 29 | Day-one readiness and signoff after a replan | T `test_29`; existing readiness tests |
| 30 | HR Policies Q&A | `test_orchestrator_e2e.py::test_uc05_policy_qa_still_works…`, `test_uc05_lifecycle.py` (20) |
| 31 | No NDA/policy signature tracking added | T `test_31`; existing `test_nda_or_signature_tracking_not_reintroduced` |

Also covered: stale results after a plan change (T, a real UC02 date change); HR requirement changes
(a real UC02 replan, the waiver respected); refused requirement changes; the graph's edge set has no
bypass to the fan-out (`test_state_driven_workflow.py`).

## 5. Known blockers

1. **The real UC02 cannot plan for tenants without UC02 configuration.** UC02's synthetic registry has
   only `CLIENT-ALPHA`, `CLIENT-BETA` and `CLIENT-EMPTY`. Because UC02 now plans first, such a case
   stops at `plan_tasks` (`MISSING_DATA`) before any document is requested. The demo's `--real` mode
   shows this for `CLIENT-TEST-A`. UC02's owner decides the production configuration source.
2. **The real UC02 needs UC01's approval facts.** UC02 refuses to plan without `offer_accepted_at`,
   `form_approved_at` and `form_approved_by_role`. The API now accepts them. Callers (UC01's event
   integration) must send them.
3. **The real UC04 refuses non-`SYN-` case ids** (GR-13, unchanged). Real runs that reach the branches
   stop there.
4. **UC03's catalog lists an NDA and policy acknowledgements** as onboarding documents. This predates
   the change and is not UC05 signature tracking. Whether they stay is a UC03 owner decision.
5. **UC02 test flakiness:** order-dependent audit-id tests, pre-existing (see §4).

## 6. Remaining work

- Commit the change, after review; nothing was committed.
- Decide the production UC02 configuration source (blocker 1) and wire UC01's approval facts from
  UC01's event (blocker 2).
- UC02's standalone `OnboardingFlow` document gate and downstream trigger are deprecated for
  orchestrated mode but still present. Remove or guard them once no standalone caller remains.
- `updated_validation_requirements` in `RevisedPlan` is reserved: UC03's rules come from its own
  catalog and do not change with a plan version today.
- UC02's own store defaults to memory. Durable UC02 replan history across orchestrator restarts needs
  `UC02_STORE_MODE=sqlite` (or Supabase) in deployment.
- Older docs (`docs/ORCHESTRATION_WORKFLOW.md`,
  `HR-Onboarding-Common/docs/STATE_DRIVEN_ONBOARDING_WORKFLOW.md`, `docs/UC_INTEGRATION_CONTRACTS.md`)
  still describe the old order in their body. They carry a pointer to the new document but have not
  been rewritten.
- Note: during this session the merge-conflict block in `STATE_DRIVEN_ONBOARDING_WORKFLOW.md` was
  resolved on disk by someone other than this change, and an untracked root `.env.example` appeared.
  Neither was touched here.
