# UC-02 integration contracts (implemented side only)

> **Superseded in part (2026-11-04).** Statements here that Supabase persistence is unbuilt, unverified or blocked, that no LLM layer exists, or that only in-memory tests pass are historical. The verified state is in `UC02_FINAL_ARCHITECTURE_AND_STATUS.md`, `UC02_TRACEABILITY_MATRIX.md` and `UC02_INTEGRATION_CHECKLIST.md`.

Status key: **IMPLEMENTED** = exists in code and is covered by in-memory tests.
**NEEDS TEAM CONFIRMATION** = no agreed contract exists; nothing was invented.
The persistence layer has since been exercised against the Supabase Development project (2026-11-04); the cross-use-case contracts below that name another team remain unagreed.

## 1. Orchestrator -> UC-02: plan tasks (IMPLEMENTED)

Entry point: `call_uc02_plan_tasks` (HR-Onboarding-Common `orchestration/adapters/uc02_tasks.py`)
-> `UC02SubAgent.run` -> `OnboardingService.create_onboarding`.

| Item | Detail |
|---|---|
| Input (UC-02 trigger) | `client_id`, `employee_id`, `joining_date`, `role_id`, `department`, `manager_id`, `location`, `project`, optional `grade`, `nationality_group` (opaque code), `attributes`; **`case_id`, `correlation_id`** (optional, echoed); `offer{employee_id, accepted, accepted_at}`; `form{employee_id, submitted, approved, approved_by_role, approved_at}` |
| Required | everything except grade, nationality_group, attributes, case_id, correlation_id |
| Verified facts | The adapter no longer hardcodes offer/form values. It requires UC-01 intake `COMPLETE`, `intake_trigger.offer_accepted`, and `uc02_role_context.{offer_accepted_at, form_approved_at, approved_by_role}` (new optional `OnboardingCaseInput` fields `offer_accepted_at`, `form_approved_at`, `form_approved_by_role`). Missing -> stage BLOCKED, never defaulted |
| Validation | `validate_preconditions`: same employee, offer accepted + timestamp, form submitted/complete/approved by an HR role |
| Authorization | Orchestrator boundary role `task_planning_trigger_roles`; UC-02 `create_onboarding` additionally requires an HR role and the actor's client to equal the trigger's |
| Output | `task_plan{onboarding_id, case_id, correlation_id, created, tasks, ready, blocked}` |
| Failure | UC-02 domain errors -> stage BLOCKED `MISSING_DATA`; import failure -> FAILED; intake not complete -> BLOCKED `PREREQUISITE_NOT_MET` |
| Idempotency | One live onboarding per employee per client; a repeat returns `created=false` with the existing record. Task ids are deterministic |
| Audit | `onboarding.create` events carry `correlation_id`, `case_id`, `config_version` |
| Not done | UC-02 still mints its own `onboarding_id` (`ONB-<client>-<employee>`); the shared `case_id` is stored beside it, not used as the key (see schema question 1) |

## 2. Any workflow -> UC-02: status update (UC-02 side IMPLEMENTED)

Entry point: `UC02SubAgent.apply_status_update(payload, *, client_id, actor_id, actor_role)` ->
`OnboardingService.apply_status_update` -> `status_updates.apply_status_update`.

| Item | Detail |
|---|---|
| Input | `StatusUpdate{update_id, source, client_id, onboarding_id, task_id, outcome, occurred_at, reference?, blocking_reason?, case_id?, correlation_id?}`; a `case_id` that differs from the onboarding's recorded one is rejected (`case_id_mismatch`) |
| Outcomes | `completed`, `valid`, `in_progress`, `invalid`, `requires_review`, `suspected_fraud` (UC-03 only), `delayed` |
| Authentication | `client_id` and `actor_id` are supplied by the integration layer, **not** read from the payload. The declared `source` is honoured only if the actor is registered for it in `OnboardingService(source_principals={source: {actor_ids}})`. **There is no fallback:** with no principals configured, every update is refused (`source_not_authenticated`). Every audit event records `source_verification` = `verified` / `rejected_missing_identity` / `rejected_no_principals_configured` / `rejected_unregistered_source` / `rejected_caller_not_registered_for_source`. **No real service identity exists yet, so production use of this entry point is BLOCKED** until the team provides them |
| Authorization | `SOURCE_CATEGORIES`: a source may only report on the task categories it performs |
| Validation | pydantic schema; resolving outcomes need evidence when the task requires it and met prerequisites; unresolved outcomes need a reason |
| Idempotency | `update_id -> task_id` ledger on the record; replay = no-op (`duplicate_replay`); same id for another task = conflict |
| Failure | Business rejections returned with a stable `rejection_code`; cross-client -> `DataIsolationError`; malformed payload -> `ValueError` before any effect |
| Audit | `task.status_update` with actor, task, update id, source, before/after status, rejection code, timestamp, correlation id |
| Retry | Callers may resend with the same `update_id`; UC-02 has no outbound retry |

## 3. Contracts that do NOT exist yet (need team confirmation)

1. **UC-03 -> UC-02.** No agreed payload or caller. The orchestrator never invokes
   `apply_status_update`. Also unresolved: `collect_documents` depends on `hr_record_create`,
   which nothing in the orchestrator completes, so a document report would be rejected
   `prerequisites_unmet` today. Who completes `hr_record_create` (UC-01 hand-off?) is a team decision.
2. **UC-04 -> UC-02.** UC-04 reports equipment *items*; UC-02 has several tasks
   (`it_laptop`, `it_accounts`, `access_*`, `desk_allocation`). The item -> task mapping, and whether
   UC-04 reports per item or per stage, is not agreed.
3. **UC-05 -> UC-02.** Same for `orientation_schedule` and `policy_ack`.
4. **Service identities.** What `actor_id` each workflow authenticates as (the `source_principals` values).
5. **Approvals.** UC-02 gates (date/location/project change, cancellation, joining confirmation) are not
   decidable through the orchestrator; whether they should be relayed is undecided.
6. **Reminder ownership and persistence** (who sends, where drafts are stored) - not implemented.
7. **Nationality group definition** and the **approved-configuration source/versioning authority**.

## 4. Supabase persistence plan (historical; implemented and live-verified on Development, see the final architecture document)

Keep everything behind `OnboardingStore` / `TableClient`. Do not swap the in-memory client until the
schema questions (case id, target schema, status vocabularies) are answered. Order:
1. working `DATABASE_URL` -> read-only metadata verification;
2. confirm target schema and table ownership;
3. migrations (additive; see the schema review);
4. a `TableClient` implementation, tested read-after-write on synthetic rows;
5. persist the new fields added here (`case_id`, `correlation_id`, `config_version` on the case;
   `correlation_id`, `task_id`, `before_status`, `after_status`, `approver_id` on audit events) -
   **these need columns**, they would be rejected by the current SQL.


## 5. Per-agent status contracts: interface PROPOSALS (not approved, no orchestrator call implemented)

Common fields for all three: authenticated service identity (supplied by the transport, never the body),
shared `case_id`, `correlation_id`, `client_id`, `onboarding_id` (or a case_id -> onboarding_id lookup,
see schema question 1), `task_id` (a UC-02 task reference), `update_id` (idempotency key, stable per
real-world event so a retry reuses it), `outcome`, `occurred_at`, `reference` (required when the task
requires evidence), `blocking_reason` (required for `invalid` / `requires_review` / `suspected_fraud` /
`delayed`). Audit: UC-02 records actor, source, verification result, before/after status, rejection code,
correlation id.

| Agent | UC-02 tasks it may report on (from `SOURCE_CATEGORIES`, UC-02 code only) | Open decisions |
|---|---|---|
| UC-03 document validation | `collect_documents`-type (document_collection), background_verification | Event that means "documents verified"; which UC-03 verdicts map to `valid` / `invalid` / `requires_review` / `suspected_fraud`; who owns the document-request email |
| UC-04 equipment/access | it_provisioning, access_management, workplace | UC-04 reports per item/request; UC-02 has several tasks (`it_laptop`, `it_accounts`, `access_*`, `desk_allocation`). The item -> task mapping is not defined anywhere |
| UC-05 orientation | orientation, policy_acknowledgement | Which UC-05 event completes `orientation_schedule` vs `policy_ack` |

The category tables are UC-02's own authorisation limits; they are not an agreement by UC-03/04/05. No
adapter call was added to the orchestrator because no event-to-task mapping is confirmed in the repository.

## 6. `hr_record_create` (traced; no event invented)

- Owner role `hr_ops`, category `hr_record`, `evidence_required=False`, no prerequisites; every other task
  in the synthetic plan depends on it, so `collect_documents` stays `blocked` until it is `complete`.
- Defined completion path: a human `hr_ops`/`hr_lead` actor calls `complete_task`
  (`docs/uc01-uc02-uc03-sequence.mmd` shows exactly that). The only other route in code is an
  `operations` status report, which no workflow is registered to send.
- UC-01 is **not** a registered reporting source and no UC-01 -> UC-02 completion event is defined; the
  orchestrator never completes it. The agent run may *start* it (ready -> in_progress), never complete it.
- **Decision needed:** should the UC-01 HRIS-write confirmation complete this task (new source + evidence
  reference), or stay a manual HR Ops step?

## 7. `approval_role` (NEEDS TEAM CONFIRMATION)

`approval_role` marks a task as `awaiting_approval` in the checklist. It is configuration, not an approval:
no per-task approval request exists, `pending_gate_request_ids` stays empty, and `complete_task` does not
check it (pinned by `test_approval_role_is_an_informational_flag_not_an_approval`). UC-02's approval inbox
covers only the change/cancellation/joining gates. Decision needed: should completing such a task require an
approved request, which gate name, and who the approver is. Until then it must not be shown as an approval.

## 8. Duplicate completion

Unchanged: a second completion is refused (`InvalidStateError`), the original `completed_at` and the single
`complete` audit event are untouched. Each refused attempt is audited as its own `denied` event (kept so
refused attempts leave a trace); say if you want repeat refusals not to be audited.
