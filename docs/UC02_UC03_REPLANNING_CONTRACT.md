# UC02 ↔ UC03 planning and replanning contract

This document defines the three messages exchanged between UC02 (planning) and UC03 (documents), how
plan versions work, and how errors are reported. The typed models live in
`HR-Onboarding-Common/src/hr_onboarding_common/orchestration/workflow_models.py`. UC02 and UC03 have
their own native forms of the same messages:

| Message | Orchestrator model | UC02 native | UC03 native |
|---|---|---|---|
| UC02 → UC03 document collection request | `DocumentCollectionRequest` (in `document_collection.request`) | `OnboardingRecord.documents` + `plan_version` | `src/sub_agent/collection.py` `DocumentCollectionRequest` |
| UC03 → UC02 validation feedback | `DocumentValidationFeedback` (`document_validation_feedback`) | `contracts.DocumentFeedback` (the changes only) | built by `adapters/uc03_documents.build_feedback` from UC03's results and `requirement_gaps` |
| UC02 → UC03 revised plan | `RevisedPlan` (`revised_plan`) + the new `onboarding_plan` | `contracts.DocumentReplanOutcome` | applied as the next `DocumentCollectionRequest` |

Every message carries ids, codes, statuses and dates only. Document contents never travel.

## 1. UC02 → UC03: document collection request

Built by `adapters/uc03_document_collection.collection_request` from the current `onboarding_plan`, then
opened in UC03 with `DocumentCollectionService.open_collection`.

| Field | Meaning |
|---|---|
| `case_id`, `tenant_id`, `employee_id`, `correlation_id` | Shared identifiers. |
| `plan_id` | UC02's onboarding id. |
| `plan_version` | The plan version these requirements belong to (≥ 1). |
| `joining_date` | The plan's effective joining date. |
| `required_documents` | Document types, in plan order. |
| `document_requirements` | Per document: `document_type`, `label`, `required` (mandatory), `due_date`, `reason`. |
| `mandatory_documents` | The subset that must be verified. |
| `validation_rules` | UC03's own rules per document type, from its catalog (read-only for UC02). |
| `due_dates` | Document type → the plan's document-collection deadline. |
| `metadata` | `source`, `data_origin: "synthetic"`. |

UC03's rules for this request:

- The same plan version with the same requirements is a no-op (no second request is sent).
- An **older** plan version is refused: `STALE_PLAN_VERSION`.
- The same version with **different** requirements is refused: `PLAN_VERSION_CONFLICT`. A changed plan
  always needs a new version.
- A newer version supersedes the open collection; `superseded_versions` keeps the earlier ones.

## 2. UC03 → UC02: document validation feedback

Built after every validation, and by the submission stage when HR changes the requirements.

| Field | Meaning |
|---|---|
| `feedback_id` | Deterministic: a hash of case, plan version, the requested changes and the HR change ids. UC02's replan is idempotent on it. |
| `case_id`, `tenant_id`, `employee_id`, `plan_id` | Identifiers. |
| `plan_version` | The version UC03 **validated against**: the collection it holds open. |
| `validation_status` | `verified`, `correction_required`, `hr_review_pending`, `blocked`, `replanning_required` or `awaiting_documents`. |
| `verified_documents`, `missing_documents`, `invalid_documents`, `expired_documents` | Per-document outcome lists. |
| `pending_hr_reviews` | Documents awaiting an authorized reviewer. |
| `correction_required` | True when something must be supplied or corrected. |
| `replanning_required`, `replanning_reasons` | True when the plan must change; the reasons are codes such as `NEW_MANDATORY_DOCUMENT:visa_documents`, `DOCUMENT_DERIVED_ATTRIBUTE:visa_required`, `HR_REQUIREMENT_CHANGE:RCR-…`. |
| `recommended_task_changes` | `[{action: add_document \| remove_document \| add_attribute, target, reason}]`. |
| `change_request_ids` | The HR requirement changes this feedback carries. |
| `document_revision` | Document type → the revision validated. |

When replanning is required:

- A mandatory document UC03's catalog requires for this employee, given what their documents show,
  that the plan does not list. A document HR removed (pending or applied change) is a waiver and is
  never re-added.
- An attribute a document establishes that the plan was not made with (`visa_required` maps to UC02's
  `visa_required`).
- A pending HR change to the requirements.

Replanning takes precedence. The UC03 stage is `BLOCKED` with `REPLANNING_REQUIRED` (canonical status
`PENDING`, category `dependency`), the corrections it found are kept, and they are asked for under the
new plan version.

## 3. UC02 → UC03: revised plan

`replan_tasks` calls `OnboardingService.replan_documents(actor, plan_id, DocumentFeedback)` and records
the result.

| Field | Meaning |
|---|---|
| `plan_id`, `plan_version`, `previous_plan_version` | The new version and the one it replaces. |
| `updated_required_documents` | The new requirement list (same shape as in §1). |
| `added_tasks`, `removed_tasks`, `modified_tasks` | UC02 task ids (for example `…#visa_processing`). |
| `updated_validation_requirements` | Reserved; UC03's rules come from UC03's catalog. |
| `effective_joining_date` | Unchanged by a document replan. |
| `replanning_reason` | The feedback's reason codes. |
| `feedback_id`, `duplicate_replay` | The feedback applied; `true` when UC02 had already applied it. |

`apply_revised_plan` then re-opens UC03's three stages. UC03:

1. opens the collection for the new version;
2. sends a request listing only documents not yet on file (`not_required` when there is none);
3. revalidates. An unchanged revision keeps its verdict and is never sent to UC03's validator twice; a
   new document is validated normally.

## 4. Plan versioning rules

1. Plan version 1 is the initial plan from `plan_tasks`. Every replan, and every joining-date change
   UC02 applies, creates the next version. A plan version is immutable.
2. `plan_history` records each version: `initial`, `replanned` or `date_change`, with the reason, the
   feedback id, and the documents and tasks added or removed. It is append-only.
3. UC03 states which version it validated against (`document_validation_feedback.plan_version`). A
   result for any other version than the current one is **stale**. `evaluate_document_result` re-opens
   UC03's stages instead of accepting it, and UC02 refuses stale feedback with `StalePlanVersionError`.
4. `verified_context.plan_version` records the version the documents were verified against. UC04 and
   UC05 receive it (`verified_plan_version`).

## 5. Correction and revalidation

| Situation | UC03 | UC02 |
|---|---|---|
| Missing document | `missing` → `supply`; the stage is `WAITING_FOR_INPUT` | Not involved |
| Expired, unreadable, wrong type, missing fields | `correction_required` → `resubmit` | Not involved |
| Corrected document submitted | A new revision; validated again (no `DUPLICATE_REQUEST`) | Not involved |
| Same document submitted again | `duplicate=True`; no new revision, nothing re-opened, no run | Not involved |
| Valid but needs HR approval | `hr_review_required`; only `document_review_roles` decide; a refused attempt consumes nothing | Not involved |
| HR rejects a revision | `hr_review_rejected` → `resubmit` | Not involved |
| New mandatory document, new attribute, HR change | `replanning_required` | Replans → the next version |

## 6. Errors and statuses

| Code | Stage | Status | Category | Recovery |
|---|---|---|---|---|
| `REPLANNING_REQUIRED` | `document_submission` / `validate_documents` | `BLOCKED` (canonical `PENDING`) | dependency | UC02 replans in the same run |
| `STALE_PLAN_VERSION` | `replan_tasks`, `document_collection` | `BLOCKED` | validation | Revalidate against the current version (automatic on the next pass) |
| `PLAN_VERSION_CONFLICT` | `document_collection` | `BLOCKED` | business | Reconcile the plan with UC02 |
| `REPLANNING_FAILED` | `replan_tasks` | `BLOCKED` | business | Resolve in UC02 (or withdraw the change); a resume retries |
| `UC02_REPLANNING_UNAVAILABLE` | `replan_tasks` | `RETRY_PENDING` | infrastructure | Resume or authorized `retry_stage`, bounded by `stage_max_attempts` |
| `REPLAN_LIMIT_REACHED` | `replan_tasks` | `BLOCKED` | business | A person reviews the plan, then resumes |
| `VALIDATION_FEEDBACK_MISSING` | `replan_tasks` | `BLOCKED` | validation | Revalidate in UC03 |
| `PLAN_DOCUMENTS_MISSING` | `document_collection` | `BLOCKED` | validation | Review the plan in UC02 |
| `UC03_COLLECTION_UNAVAILABLE` | `document_collection` | `RETRY_PENDING` | infrastructure | Retry |

UC04 and UC05 never start while any of these is open on a UC02 or UC03 stage.

## 7. HR requirement changes

`POST /v1/orchestration/cases/{case_id}/documents/requirements` takes
`{add_documents: [...], remove_documents: [...], reason: "..."}`. The role required is
`document_requirement_change_roles` (default `[hr_admin]`).

The request is refused, with nothing changed, in these cases:

- **403:** an unauthorized caller.
- **404:** another tenant's case.
- **409:** no plan yet, or the documents are already verified.
- **422:** a change that changes nothing, a type UC03 cannot validate, or removing a document the plan
  does not require.

The same change against the same plan version is recorded once. The change is applied by the next UC02
replan and marked `applied` with `applied_in_plan_version`.
