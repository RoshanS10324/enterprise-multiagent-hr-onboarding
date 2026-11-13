# UC-02 — Onboarding Task Management: Business Rules and Traceability

Business rules verified 2026-10-27 (484 tests at that time). **Current test totals and the Supabase/Fake-LLM status are in `UC02_FINAL_ARCHITECTURE_AND_STATUS.md`.**

This document maps the **approved** UC-02 business requirements to the implementation and
its automated tests. It does **not** restate or amend the requirements — the approved
`UC-02_Onboarding_Task_Management` document remains the source of truth. Identifiers here
follow the project convention (`UC02-BR-nn`, `UC02-TC-nnn`) and correspond one-to-one with
`BR-nn` and `TC-nnn` in that document.

---

## 1. Common rules reference

These are **shared platform behaviours**, defined once here and not redefined per rule:

| Ref | Common rule |
|---|---|
| `CR-START` | No active plan is generated unless the offer is accepted **and** the onboarding form is submitted, complete and approved by an HR role. |
| `CR-APPROVAL` | Hard-gated actions require an approved decision from the gate's approver role. A requester may not decide their own request; an approval is valid only for its own gate and only while the onboarding is active. |
| `CR-PRIVACY` | Records carry opaque identifiers only. No name, email, nationality, passport or contact field exists in any UC-02 model, and the audit log rejects those keys. |
| `CR-ISOLATION` | Every operation takes an `Actor` and is scoped to that actor's `client_id`. Cross-client access is denied and audited against the calling client. |
| `CR-AUDIT` | Task creation, status changes, re-planning decisions, approvals, reads and handoffs are appended to a client-scoped, append-only log carrying IDs, roles, counts and outcomes only. |

---

## 2. Responsibility boundary — UC-02 and UC-06

| Owned by **UC-02** | Owned by **UC-06** |
|---|---|
| Generate the onboarding plan and checklist | Ongoing monitoring |
| Assign configured owners, dependencies, due dates | Follow-up execution and delivery |
| Determine readiness and parallel activities | Joining confirmation |
| Re-plan after approved joining-date / location / project changes | Closing the onboarding case |
| Produce planning outputs, outstanding-activity information and **planning-time** joining risk | |

**Interface:** `OnboardingService.joining_handoff(actor, onboarding_id, as_of) -> JoiningHandoff`

`JoiningHandoff` carries the plan revision, planning-time `risk`, `outstanding` activities
(each with owner and required follow-up), `milestones`, `owner_issues` and `calendar`
status. It always reports `case_closed_by_uc02 = False` — **UC-02 never closes the case**.

UC-02 retains its planning-time risk calculation; that is a planning output, not ongoing
monitoring. UC-02 prepares reminder **drafts** (`delivery_status = "draft_not_sent"`) and
has no transport — delivery belongs to UC-06.

---

## 3. Business rules

| ID | Rule (abbreviated) | Status | Implementation | Tests |
|---|---|---|---|---|
| `UC02-BR-01` | Start only after offer accepted and form approved | **Completed** | `service.validate_preconditions` | `PreconditionTests` |
| `UC02-BR-02` | Checklist by client config, role, grade, nationality, location | **Completed (boundary)** | `TaskTemplate.dimension_verdicts` (single rule evaluation) reduced by `applies_to`; `selection.py` turns the same verdicts into per-task inclusion/exclusion reasons; `OnboardingService.checklist` / `preview_checklist` | `GradeFilteringTests`, `NationalityGroupTests`, `SelectionRuleTests`, Scenario A–G in `test_dynamic_checklist.py` |
| `UC02-BR-03` | Assign configured owner; flag missing/invalid | **Completed** | typed `owner` + `ClientOnboardingConfig.owner_issues()`; no default is ever substituted | `OwnerValidationTests` |
| `UC02-BR-04` | No dependent task starts before prerequisites | **Completed** | `planner.unmet_prerequisites` | `BlockedPrerequisiteTests` |
| `UC02-BR-05` | Independent activities proceed in parallel | **Completed** | `planner.ready_tasks`, `parallel_batches` | `ParallelExecutionTests`, `CohortTests` |
| `UC02-BR-06` | Due dates from lead times, joining date, dependencies, calendar | **Completed** (data provisional) | `calendar_uae`; `calendar_status()` reports `approved=False` for built-in data; out-of-range dates raise | `CalendarAuthorityTests`, `CalendarCoverageTests` |
| `UC02-BR-07` | Track BV, visa, medical, biometrics, insurance where applicable | **Completed** | 5 categories + templates, opt-in via applicability flags; milestones `verification_cleared`, `work_authorisation_ready` | `WorkAuthorisationTests` |
| `UC02-BR-08` | Track travel, accommodation, food, banking-letter dependencies | **Completed** | 4 categories + templates, opt-in; milestone `relocation_ready` | `TravelAndBankingTests` |
| `UC02-BR-09` | Re-plan on change, after required approval | **Completed** | `planner.replan` behind `CHANGE_GATES` | `ApprovalControlledReplanTests`, `JoiningDateChangeTests`, `LocationChangeTests`, `ProjectChangeTests` |
| `UC02-BR-10` | Identify overdue/at-risk **with owner and follow-up** | **Completed** | `tracker.outstanding_activities` + **`tracker.escalations`** (GR-12 triggers, Framework §10 routing) and `service._stalled_approvals` | `ResponsibilityBoundaryTests`, `TravelAndBankingTests`, `EscalationTests` |
| `UC02-BR-11` | No sending / restricted change / spend without approval | **Completed** | per-task `approval_role` from Framework §8; drafts never sent | `ExplicitApproverTests` |
| `UC02-BR-12` | No completion without required evidence | **Completed** | `TaskTemplate.evidence_required` + `EvidenceRequiredError` | `CompletionEvidenceTests`, `HrSystemActivationTests` |
| `UC02-BR-13` | Restrict to authorised users; separate each case | **Completed** | `CR-ISOLATION` | `DataIsolationTests`, `IsolationAndAuditForNewFeaturesTests` |
| `UC02-BR-14` | Record creation, assignment, status, re-planning, approvals | **Completed** | `CR-AUDIT`, 13 event types incl. `handoff.uc06` | `AuditLogTests`, `IsolationAndAuditForNewFeaturesTests` |

### Approver mapping (`UC02-BR-11`)

Sourced from Business Framework §8 "Systems and actions". **No approver was invented**; an
action with no documented approver carries `None`.

| Action | Approver | Role constant |
|---|---|---|
| Background verification | HR officer | `Role.HR_OFFICER` |
| Visa, medical, biometrics | HR operations | `Role.HR_OPS` |
| Travel, accommodation, food, insurance (spend) | HR manager / admin | `Role.HR_MANAGER` |
| Banking letters | HR officer | `Role.HR_OFFICER` |
| HR-system activation | HR operations | `Role.HR_OPS` |
| Joining-date / location change | HR operations | gate `date_change_approval` / `location_change_approval` |
| Project change | Hiring manager | gate `project_change_approval` |

### Dynamic checklist selection and explanation

Added 2026-10-27. Purely additive: no existing selection behaviour changed.

| Capability | Where | Notes |
|---|---|---|
| Per-dimension rule evaluation | `TaskTemplate.dimension_verdicts` | The **single** place the six filters are evaluated. `applies_to` is its boolean reduction, so a decision and its stated reason cannot drift apart (pinned by `test_decision_agrees_with_applies_to_on_every_combination`). |
| Inclusion / exclusion reasons | `selection.evaluate_template` | Reason text is assembled only from configured filter values and supplied context values. No policy, legal requirement or client rule is inferred. |
| Structured checklist | `selection.build_view` → `OnboardingService.checklist(actor, onboarding_id, as_of)` | Entries with reason, prerequisite blockers, overdue/approval state; ready / blocked / in-progress / complete / cancelled / overdue / awaiting-approval buckets; parallel batches; milestones; issues. Derived, never stored. |
| Selection without creating anything | `selection.preview` → `OnboardingService.preview_checklist(actor, context)` | Answers "what would this context produce?"; creates no record. `PreviewMatchesPlanTests` pins preview == generated plan for four personas. |
| Static configuration conflicts | `ClientOnboardingConfig.dependency_scope_conflicts()` → `OnboardingService.configuration_issues(actor, client_id)` | A dependent whose filters are wider than its prerequisite's. Reported, never repaired. |

**`awaiting_approval` is a statement about configuration, not a claim about state.**
It means the task is open *and* its template names an `approval_role` (UC02-BR-11) — i.e.
the action may not be taken unilaterally. Approval requests that actually exist are listed
separately in `pending_gate_request_ids`.

#### Issue codes and severity

| Code | Severity | Raised when |
|---|---|---|
| `no_applicable_tasks` | blocking | No configured template matches the context. `create_onboarding` also raises `MissingConfigurationError`. |
| `dropped_prerequisite` | review_required | An in-scope task's configured prerequisite is out of scope, so the planner drops the dependency instead of enforcing it. |
| `milestone_unreachable` | review_required | An applicable milestone requires a task this context will not get. |
| `owner_invalid` | review_required | An in-scope template's owner is the `SYSTEM` pseudo-role. No default owner is substituted. |
| `calendar_unapproved` | informational | Due dates came from the unapproved built-in holiday data (D-5). |

`review_required = True` when any issue is `review_required` or `blocking`.

**Known configuration conflict in the synthetic data (reported, not fixed):**
`family_insurance_enrolment` depends on `insurance_enrolment`, but the former requires only
`family_insurance` while the latter requires `insurance_required`. A context declaring
family insurance alone therefore gets the dependant without its prerequisite. Whether base
insurance should be implied is an **HR Ops decision** (new open decision **D-10**), so
UC-02 reports it rather than inventing the rule.

#### Agentic AI readiness — current seams

| Layer | Status | Where |
|---|---|---|
| Rules engine | **Implemented** | `selection.py`, `planner.py`, `tracker.py`, `calendar_uae.py` — deterministic, pure, callable by an agent as a tool. |
| LLM layer | **Fake LLM implemented (synthetic, labelled); no real model** | `agent/fake_llm.py`, `agent/analysis.py`. It may phrase or summarise a `ChecklistView`; it must not change which tasks are in it, and it never approves. |
| Tools / integrations | **Mock adapters only** | `providers.py` Protocols. No endpoint, credential or retry policy is approved. |
| Verification | **Implemented for local operations** | Every state change returns the stored outcome; the view is re-derived from the record, so success is never asserted without the record showing it. |
| Human approval | **Implemented** | `approvals.py` hard gates; `approval_role` per task; `review_required` on the checklist. |

---

## 4. Test-case traceability

Every spec test case now maps to at least one automated test.

| ID | Scenario | Covering tests |
|---|---|---|
| `UC02-TC-001` | Generate task plan | `CreateOnboardingTests` |
| `UC02-TC-002` | Offer not accepted | `PreconditionTests.test_offer_not_accepted_is_rejected` |
| `UC02-TC-003` | Form not approved | `PreconditionTests.test_unapproved_form_is_rejected` |
| `UC02-TC-004` | Missing configuration | `MissingConfigurationTests` |
| `UC02-TC-005` | Assign task owner | `CreateOnboardingTests.test_tasks_carry_owner_status_due_date_and_dependencies` |
| `UC02-TC-006` | Missing / invalid task owner | `OwnerValidationTests` |
| `UC02-TC-007` | Independent tasks in parallel | `ParallelExecutionTests` |
| `UC02-TC-008` | Unmet prerequisite | `BlockedPrerequisiteTests` |
| `UC02-TC-009` | Calculate due dates | `CreateOnboardingTests.test_due_dates_are_business_days_before_joining`, `CalendarAuthorityTests` |
| `UC02-TC-010` | Overdue task | `OverdueDetectionTests`, `JoiningRiskTests` |
| `UC02-TC-011` | Delayed milestone | `WorkAuthorisationTests`, `TravelAndBankingTests` |
| `UC02-TC-012` | Joining date changed | `JoiningDateChangeTests`, `ApprovalControlledReplanTests` |
| `UC02-TC-013` | Location or project changed | `LocationChangeTests`, `ProjectChangeTests` |
| `UC02-TC-014` | Duplicate request | `DuplicateOnboardingTests` |
| `UC02-TC-015` | Missing completion evidence | `CompletionEvidenceTests` |
| `UC02-TC-016` | Approval pending | `GateEnforcementTests`, `ApprovalControlledReplanTests` |
| `UC02-TC-017` | Unauthorized case access | `DataIsolationTests`, `IsolationAndAuditForNewFeaturesTests` |
| `UC02-TC-018` | All applicable tasks complete | `CreateOnboardingTests.test_happy_path_completes_every_task` |

### Dynamic-checklist scenarios (`tests/test_dynamic_checklist.py`, 68 tests)

| Scenario | Covering class | What it pins |
|---|---|---|
| A — local hire | `ScenarioALocalHireTests` | Common tasks generated; no relocation/visa tasks without the approved flags; every inclusion and exclusion carries a reason |
| B — relocation hire | `ScenarioBRelocationHireTests` | Relocation, visa and BV tasks added; `collect_documents` blocks `visa_pack_handover`; local-hire task set is a strict subset |
| C — role / grade | `ScenarioCRoleAndGradeTests`, `ScenarioCAuthorizationTests` | Grade- and role-filtered equipment/access; a role match plans the task but does not grant access — owner authorisation, approver and evidence still apply |
| D — missing documents | `ScenarioDMissingDocumentsTests` | Dependants BLOCKED, the missing prerequisite named, READY only after it completes, evidence recorded |
| E — joining date changed | `ScenarioEJoiningDateChangeTests`, `ScenarioELocationChangeTests` | Gate required before applying; open due dates recalculated; completed work preserved and not rescheduled; revision and audit history intact; a retained completed task is explained |
| F — client-specific | `ScenarioFClientSpecificTests` | CLIENT-BETA gets only what BETA configured; isolation and authorisation on preview; an added client template is additive only |
| G — missing / conflicting config | `ScenarioGMissingConfigurationTests`, `ScenarioGConflictingConfigurationTests` | Unregistered/empty client raises; "matches nothing" is blocking, not an empty plan; dependency-scope conflict, unreachable milestone and invalid owner reported |

**Additional edge cases** (beyond the spec list): weekend/holiday joining date
(`CalendarAuthorityTests`), cohort joining (`CohortTests`), owner unavailable
(`OwnerAvailabilityTests`), accommodation not ready (`TravelAndBankingTests`), visa pack
handover and subsequent tracking (`WorkAuthorisationTests`), family insurance applicability
(`WorkAuthorisationTests`), banking letters required/not required (`TravelAndBankingTests`),
HR-system activation blocked (`HrSystemActivationTests`), stale/rejected approval
(`ApprovalControlledReplanTests`, `StaleApprovalTests`), UC-06 boundary
(`ResponsibilityBoundaryTests`).

---

## 5. Canonical synthetic data

| Field | Value |
|---|---|
| Employee ID | `EMP-001` |
| Client ID | `CLIENT-ALPHA` |
| Role | `backend_engineer` · Grade `L3` |
| Department | `technology` · Manager `MGR-CLIENT-ALPHA` |
| Location / Project | `Dubai` / `apollo` |
| Joining date | **2026-11-16** |

This matches `data/UC02_Synthetic_Onboarding_Test_Data.json` and is unchanged by this pass.
Additional personas were **added, not substituted**: `EMP-007` (relocation hire, all
extended activities, grade L4), `EMP-008` (local hire, none), `EMP-010/011/012` (cohort).

> **Mismatch to resolve:** the Business Framework and UC-03 model an onboarding case with
> `grade`, `nationality` and an employee name. UC-02 carries `grade` but deliberately
> carries no name or nationality. A canonical cross-use-case test fixture has not been
> agreed; UC-02 has not changed any other use case's data.

---

## 6. Open decisions

| # | Decision required | Owner | Effect while unresolved |
|---|---|---|---|
| D-1 | Nationality -> group mapping policy | Governance / Legal | The **mechanism** exists (opaque `nationality_group`, unfiltered by default). No client declares a group, and UC-02 never stores a nationality value. |
| D-2 | Does visa processing wait for background verification? (Framework OQ-02) | Process SME | No dependency assumed in either direction; clients may declare their own |
| D-3 | Outcome taxonomy for a **failed** background verification | HR Ops | Only "pending / not complete" is modelled |
| D-4 | Leave-aware owner re-assignment | HR Ops | Admin-role cover path only; no leave calendar |
| D-5 | Approved UAE public-holiday data | Legal / HR | Calendar reports `approved=False`; coverage 2026-01-01 → 2027-12-31 |
| D-6 | Approval matrix beyond Framework §8 | HR Ops | Actions outside §8 carry no approver |
| D-7 | Reminder **delivery** channel and retries | HR Ops / Architecture | Drafts only; no delivery or retry. Escalation detection is implemented (GR-12); delivery of escalations is UC-06's. |
| D-8 | HR-system (Zoho) API contract, credentials, integration requirements | Architecture owner | Activation is a tracked task only; no live integration |
| D-9 | Shared database architecture (Supabase / Cosmos DB) | Architecture owner | In-memory only; no persistence |
| D-10 | Does declaring family insurance imply base medical insurance? | HR Ops | `family_insurance_enrolment` can be planned without `insurance_enrolment`; reported as `dependency_scope_conflict`, never auto-corrected |

---

*Synthetic data only. No live integration, database, notification transport or production
behaviour exists in UC-02.*


---

## 7. Mock-data layer and integration boundaries

Added in the mock-completion pass. **All adapters are mock/local.**

| Boundary (`providers.py`) | Mock adapter | Production adapter must |
|---|---|---|
| `EmployeeDataProvider` | `JsonEmployeeDataProvider` | return the `OnboardingTrigger` shape; never names/nationalities/contacts |
| `ClientConfigurationProvider` | `RegistryConfigurationProvider` | raise `MissingConfigurationError` rather than invent tasks |
| `CalendarProvider` | `BuiltInCalendarProvider`, `JsonCalendarProvider` | declare `approved` and `source`; unapproved data stays provisional |
| `OnboardingRepository` | `InMemoryOnboardingRepository`, `JsonFileOnboardingRepository` | enforce client partitioning at the storage layer; idempotent writes |
| `NotificationProvider` | `DraftOnlyNotificationProvider` | refuse to send until the approval gate has passed (GR-01) |
| `UC06HandoffProvider` | `LocalUC06HandoffProvider` | own monitoring, follow-up, confirmation and closure |

Endpoint URLs, authentication, credential storage and retry policy are
**deliberately absent** — none has been approved.

`JsonFileOnboardingRepository` writes atomically (temp file + `os.replace`) and
raises `ProviderDataError` on a corrupt store. It is **not** a production
database: Supabase, PostgreSQL and Cosmos DB remain unapproved and unused.

### Scenario fixtures

`data/mock/` holds 11 trigger payloads plus calendar, approval-decision and
evidence-reference fixtures, indexed in `data/mock/README.md`. The canonical
fixture is unmodified (`md5 efd40f765faba77d0305721dfad7c0c8`).
