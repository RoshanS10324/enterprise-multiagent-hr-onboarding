# UC02 / UC03 responsibility migration

This document records how document collection and storage moved to UC03 and how replanning was added
to UC02. It lists what changed in each codebase, what is deprecated, and what each owner must keep
compatible. The workflow itself is in [`UPDATED_ONBOARDING_WORKFLOW.md`](UPDATED_ONBOARDING_WORKFLOW.md),
and the contracts are in [`UC02_UC03_REPLANNING_CONTRACT.md`](UC02_UC03_REPLANNING_CONTRACT.md).

Changes to `UC02_Onboarding_Task_Agent` and `UC03_Document_Validation` were explicitly authorized for
this work. UC01, UC04 and UC05 sources are unchanged.

## 1. Before and after

| Capability | Before | After |
|---|---|---|
| When UC02 plans | After UC03 verified the documents | Right after UC01 intake, before any document |
| Who defines the required documents | Nobody explicitly: the orchestrator's `document_collection` stage read UC03's checklist directly (the stage was labelled UC02 in `USE_CASE_OF`) | UC02, on its plan (`define_document_requirements`), versioned with the plan. The list is picked from UC03's catalog of validatable documents |
| Document request | Orchestrator stage, labelled UC02 | UC03 stage (`adapters/uc03_document_collection.py`): opens UC03's collection for the plan version, then sends the request |
| Document storage and revisions | Orchestrator state only (`submitted_documents`, revision counted by the orchestrator) | UC03's `DocumentCollectionService` registry: every revision stored, duplicates recognised. The state keeps a projection |
| Revalidating a corrected document | UC03 flagged any second submission of a type as `DUPLICATE_REQUEST` (an HR review) | UC03 tracks revisions (`document_revision`). Only the same or an older revision is a duplicate; a corrected revision is validated again |
| What UC03 validates | Every document in UC03's checklist | Exactly what the current plan version requires |
| Feedback to UC02 | None | `DocumentValidationFeedback` for the plan version validated |
| Replanning on document outcomes | Not possible | `OnboardingService.replan_documents` (UC02), idempotent per feedback id, rejects stale plan versions |
| UC02's standalone document gate (`OnboardingFlow`) | UC02's own document request, document-status intake and mandatory document gate before triggering UC04/UC05 | Unchanged, but **not used in orchestrated mode** (see §4) |

## 2. Inventory of what existed

### UC02 (`UC02_Onboarding_Task_Agent/src/onboarding_task_management`)

| Function | Kind | Status after migration |
|---|---|---|
| `sub_agent/adapter.py` `UC02SubAgent.run` | task planning | **Kept.** Used by `plan_tasks`. Now also returns `plan_version`. |
| `sub_agent/service.py` `create_onboarding`, `checklist`, `request_change` / `decide_change` / `apply_change` | planning, date or location replanning | **Kept.** A date change now also bumps the orchestrator's plan projection (`relay_uc02_decision`). |
| `sub_agent/planner.py` `replan` | re-selection of task templates | **Kept**, plus `add_attributes` (switches on attribute-gated templates without moving dates). |
| `sub_agent/flow.py` `prepare_document_request` | document request to the collection team | **Deprecated for orchestrated mode.** UC03 sends the request. |
| `sub_agent/flow.py` `apply_document_update`, `_DOC_TRANSITIONS` | document status intake (HMAC-signed) | **Deprecated for orchestrated mode.** UC03 holds the document status. |
| `sub_agent/flow.py` `refresh_documents`, `document_gate` | mandatory document gate | **Deprecated for orchestrated mode.** The orchestrator's `evaluate_document_result` and UC03's verification replace it. |
| `sub_agent/flow.py` `trigger_downstream` | triggers UC04/UC05 after the gate | **Must not be used in orchestrated mode.** It would be a second, competing dispatch. Only `dispatch_branches` starts UC04/UC05. |
| `contracts.py` `DocumentItem`, `OnboardingRecord.documents` | UC02's list of required documents | **Reused** as the plan's document requirements (`key` = UC03 document type). |

None of these functions were deleted. The standalone `OnboardingFlow` keeps working for UC02's own
API and tests.

### UC03 (`UC03_Document_Validation/src`)

| Function | Status after migration |
|---|---|
| `sub_agent/workflow.py` `build_uc03_graph`, `sub_agent/validator.py` `DocumentValidationService.validate` | **Kept.** Validation is unchanged except for the revision-aware duplicate check. |
| `sub_agent/checklist.py` `ChecklistService`, `knowledge/checklists/default.yaml` | **Kept.** This is the catalog of validatable document types and their rules. |
| `state/models.py` `OnboardingCase.received_documents` | **Kept** (for callers without revisions); `received_revisions` added. |

### Orchestrator (`HR-Onboarding-Common/src/hr_onboarding_common/orchestration`)

| Module | Status after migration |
|---|---|
| `adapters/uc02_document_collection.py` | **Deprecated import path.** It only re-exports `call_uc03_document_collection` as `call_uc02_document_collection`. |
| `adapters/uc03_document_collection.py` | **New.** UC03 document collection for the plan version. |
| `adapters/uc02_replanning.py` | **New.** UC02 replanning stage. |
| `adapters/uc02_tasks.py` | Defines the plan's document requirements in UC02; builds `onboarding_plan`. |
| `adapters/uc03_documents.py` | Validates against the plan; builds the feedback; UC03 collection-service accessor. |

## 3. What was added

**UC03** `src/sub_agent/collection.py`:

- `DocumentCollectionService.open_collection(DocumentCollectionRequest)`: one collection per plan
  version. It is idempotent for the same version, refuses an older version (`STALE_PLAN_VERSION`), and
  refuses changed requirements under the same version (`PLAN_VERSION_CONFLICT`).
- `submit(...)`: stores the next revision. If the reference, source and metadata are identical to the
  latest revision, nothing new is stored (`duplicate=True`). It refuses a document the plan version
  does not require, and refuses another tenant.
- `latest`, `history`, `catalog`, `known_document_types`, `validation_rules`, `derived_context`
  (flags such as `visa_required` that a document establishes), and `requirement_gaps`.
- Two registries: `InMemoryDocumentRegistry` and `SqliteDocumentRegistry` (survives a restart).
- `DocumentValidationRequest.document_revision` and `OnboardingCase.received_revisions`, which make the
  duplicate check revision-aware.

**UC02** `sub_agent/service.py`, `contracts.py`, `errors.py`:

- `define_document_requirements(actor, onboarding_id, specs)`: records the initial requirements on the
  current plan version. Idempotent; a different list after that is a replan.
- `replan_documents(actor, onboarding_id, DocumentFeedback) -> DocumentReplanOutcome`: adds or drops
  documents, switches on templates for new attributes, and creates the next plan version.
  - Idempotent by `feedback_id`: the ledger is `flow_ledger["docfb:<id>"]`.
  - Raises `StalePlanVersionError` for another plan version.
  - Never drops a verified document and never reopens a completed task.
  - Refuses feedback that changes nothing.
- `DocumentSpec`, `DocumentFeedback`, `DocumentReplanOutcome`, `StalePlanVersionError`.

## 4. Data ownership after migration

- **UC02** owns the task plan and its document requirements, with the plan version. In the orchestrated
  workflow, `OnboardingRecord.documents[*].state` stays `pending`; UC02 does not track document status.
- **UC03** owns collections, stored revisions, validation results and the catalog with its rules.
- **The orchestrator** owns workflow execution state and keeps projections of both:
  `onboarding_plan` from UC02, `document_collection` and `submitted_documents` from UC03.

There is one source of truth per fact. `submitted_documents` is UC03's registry projected into the case
state for routing and resume. It is never written without a UC03 receipt.

## 5. Migration risks

| Risk | Mitigation |
|---|---|
| UC02 cannot plan for a tenant without UC02 configuration. Under the old order this surfaced only after the documents were verified; now it stops the case before any document is requested. | Register every tenant's configuration in UC02 before enabling it. The demo's `--real` mode shows this today for `CLIENT-TEST-A`. |
| The real UC02 refuses to plan without `offer_accepted_at` / `form_approved_at` / `form_approved_by_role`. | These fields are now accepted by the API start body. Callers must send them. |
| The UC02 package is installed non-editable in the common venv, so a UC02 change is not picked up until reinstalled. | `uv sync --reinstall-package onboarding-task-management` in `HR-Onboarding-Common`. |
| An in-memory UC03 registry loses revisions on a restart. | Pass `persist_dir` (bootstrap uses `uc03_documents.db`). The orchestrator re-opens a missing collection from its own record of the request, but revision history needs the SQLite registry. |
| Two competing gates: UC02's standalone `document_gate` / `trigger_downstream` could start UC04/UC05 outside the orchestrator. | Do not run `OnboardingFlow` against an orchestrated case. Only `dispatch_branches` dispatches. |
| UC03's catalog lists an NDA and policy acknowledgements as documents. | These are UC03 document types, not UC05 signature tracking. Nothing new was added; a UC03 owner decision if they should go. |

## 6. Backward compatibility

- **Cases persisted before this change:** `ensure_lifecycle_fields` back-fills the new fields. A case
  whose validation had completed keeps `document_collection` and `document_submission` complete. On
  resume, `plan_tasks` runs (if it had not) and the case continues to the fan-out. A case without an
  `onboarding_plan` is never compared against an empty plan: no gaps, no replanning.
- **Legacy collections** (`source: "uc03_checklist"`, no `plan_version`) are re-opened in UC03's
  registry as plan version 1 on the next submission.
- **API:** existing routes and fields are unchanged. The case view adds `onboarding_plan`,
  `plan_history`, `revised_plan`, `document_validation_feedback` and `requirement_change_requests`.
  There is a new route, `POST /cases/{id}/documents/requirements`, and the start body takes UC01's
  three approval facts.
- **Stage outcomes:** result keys are unchanged (`task_plan`, `document_validation_result`).
  `USE_CASE_OF` now reports `document_collection` and `document_submission` as UC03, and
  `replan_tasks` as UC02.
- **Imports:** `adapters.uc02_document_collection.call_uc02_document_collection` still resolves.
- **UC03 callers without `document_revision`** keep the old per-type duplicate check.
