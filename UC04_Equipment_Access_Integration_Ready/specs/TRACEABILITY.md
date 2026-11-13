# UC-04 traceability

Legend. Source: **Framework** = Onboarding Agent Business Framework v0.1 (ID cited); **Deck** = Enterprise HR Agent introduction (page cited);
**Ref** = verified code in the HR sample reference repository (file and symbol); **Local** = local design decision.
Status: *Implemented* = code plus a passing test in this project; *Partial*; *Outside UC-04* = belongs to the parent onboarding workflow or another stage and is not implemented here.
Tests live in `tests/`. A test is cited by id (TC-nn / EC-nn); `tests/test_traceability.py` checks that every TC id and every in-scope EC id is referenced by a test.

## 1. Business rules BR-01 to BR-24 (from the assignment brief)
| BR | Implementation | Tests |
|---|---|---|
| BR-01 start gate | `rules.start_gate` | TC-01, TC-02, EC-19, EC-20 (test_rules) |
| BR-02 correct client configuration | `rules.check_config` scope check, `service._resolve_case` | TC-19, AC-10 test (test_rules) |
| BR-03 missing configuration blocks | `rules.check_config`, `config_state.ConfigBlock` | TC-07, TC-08, TC-09, TC-16, TC-27, EC-52 |
| BR-04 access allowlist | `items.evaluate_item` | TC-04, TC-07 |
| BR-05 exception plus standard approval | `approvals.assess`, `items.evaluate_item` | TC-04, TC-05, TC-06 |
| BR-06 equipment allowlist | `items.evaluate_item` | TC-08, EC-31 |
| BR-07 distinct states | `states.ItemState`, `lifecycle` | TC-14, TC-30 |
| BR-08 no external side effects | `ports`, mock adapters, `policy.ToolGateway` | TC-06, TC-21, TC-39 |
| BR-09 rejection handling | `evidence.collect_ticket_facts`, `items._progress` | TC-11, TC-29, EC-30 |
| BR-10 delayed equipment, proposal only | `risk.item_risks` | TC-12, TC-28, EC-29 |
| BR-11 faulty or wrong equipment | `items._progress`, `risk.item_risks` | EC-33 |
| BR-12 client-site prerequisites | `risk.assess_client_site` | TC-15, EC-34 |
| BR-13 remote or overseas rules | `rules.check_config`, `items._due`, `risk.remote_plan` | TC-16, EC-36 |
| BR-14 deduplication by id | `service._dedupe`, `store` | TC-17, TC-18, EC-38, EC-39; test_duplicate_request_key_creates_one_item (API) |
| BR-15 privacy and audit | `audit`, `guardrails` | TC-24, TC-32 |
| BR-16 approval validation | `approvals._invalid_reason` | TC-10 |
| BR-17 readiness needs evidence | `evidence.is_authoritative`, `readiness.derive_status` | TC-13, TC-14, TC-25, TC-26 |
| BR-18 fail closed | `service.parse_input`, `service.submit_eligible`, `service.evaluate` rollback (`store.snapshot` / `store.restore`) | TC-20, TC-21; tests/test_failure_atomicity.py (D2) |
| BR-19 owner for every blocker or risk | `Blocker.owner`, `Risk.owner`, `Owner.OWNER_CONFIG_MISSING` | TC-09, test_every_blocker_and_risk_names_an_owner |
| BR-20 deterministic dates | `calendar`, `items._due` | TC-22, TC-27, calendar test |
| BR-21 idempotency | `store`, `service.evaluate` (replay valid only while case state and parent status are unchanged) | TC-31; tests/test_failure_atomicity.py (D1) |
| BR-22 cancellation | `service._cancelled` | TC-23, TC-37; tests/test_failure_atomicity.py (replay after WITHDRAWN) |
| BR-23 one shared service | `entrypoints/*` call `Uc04Service` only | TC-33, TC-35, TC-36 |
| BR-24 parent case reused | `CaseContextPort`, `service._resolve_case` | TC-34 |

## 2. Decision rules DL-01 to DL-17
| ID | Status | Where / note |
|---|---|---|
| DL-01 start condition | Implemented | `rules.start_gate` (TC-01, TC-02) |
| DL-02 conflicting records | Partial | A changed form version must be re-approved (TC-22 and related test); form-versus-offer conflict detection is a parent responsibility |
| DL-03 document requests | Outside UC-04 | |
| DL-04 document arrival | Outside UC-04 | |
| DL-05 passport-name mismatch | Outside UC-04 | |
| DL-06 parallel activities | Implemented | Each item is evaluated independently from its own prerequisites |
| DL-07 joining-date risk | Implemented | `risk.item_risks` (TC-12, EC-12) |
| DL-08 visa vs background check | Outside UC-04 | |
| DL-09 UAE vs overseas visa route | Outside UC-04 | (UC-04 covers remote/overseas equipment only, EC-36) |
| DL-10 entitlements | Implemented | Matrix allowlist (TC-04) |
| DL-11 Zoho data | Outside UC-04 | |
| DL-12 salary transfer letter | Outside UC-04 | |
| DL-13 client site | Implemented | TC-15, EC-34 |
| DL-14 no response | Partial | Only a drafted reminder for a missed collection slot (EC-32); the reminder schedule is a parent concern |
| DL-15 withdrawal | Implemented | TC-23, TC-37 |
| DL-16 salary and contract questions | Outside UC-04 | UC-04 has no question-answering surface and no pay data |
| DL-17 missing information | Implemented | Configuration gaps ask HR (TC-07 to TC-09, TC-27, EC-52) |

## 3. Guardrails GR-01 to GR-21
| ID | Status | Where / note |
|---|---|---|
| GR-01 no unapproved joiner message | Implemented | UC-04 has no messaging port; any employee message is only mentioned as a draft for HR approval |
| GR-02 no request without checkpoint | Implemented | `ToolGateway`, approval gate (TC-03, TC-06, TC-35) |
| GR-03 access beyond matrix is an exception | Implemented | TC-04, TC-05, EC-31 |
| GR-04 no cross-joiner disclosure | Implemented | Case-scoped request ids, labels never echoed (TC-18) |
| GR-05 no pay or contract answers | Implemented | No such surface or field |
| GR-06 special-category privacy | Implemented | `guardrails.sanitize_free_text` (TC-24) |
| GR-07 evidence before completion | Implemented | Provisioning, delivery, collection, clearance evidence rules (TC-13, TC-14, TC-26) |
| GR-08 cite policy sources | Outside UC-04 | No policy answers |
| GR-09 audit trail | Implemented | `audit.AuditLog` |
| GR-10 untrusted text is data | Implemented | EC-10 test |
| GR-11 escalate withdrawal, complaints | Partial | Withdrawal handled via parent status; complaints and distress are outside UC-04 |
| GR-12 escalate date risk, stalled approvals | Implemented | `risk.item_risks` |
| GR-13 synthetic data only | Implemented | Identifier patterns, fixtures, TC-39 |
| GR-14 confirmation before system-of-record writes | Implemented | No system-of-record writes exist |
| GR-15 no unrequested data collection | Implemented | UC-04 collects nothing; only listed inputs are accepted |
| GR-16 client isolation | Implemented | TC-19, AC-10 test |
| GR-17 guardrails over client instructions | Implemented | Client overrides ignored, logged, HR admin notice (EC-51) |
| GR-18 restricted financial data | Implemented | No such fields (test_gr18), TC-24 |
| GR-19 no direct external submission | Implemented | No external ports |
| GR-20 spend needs approval at HC-08 | Implemented (by absence) | UC-04 raises no spend; a loan device is only proposed. HC-08 is undefined in the framework (open question, spec section 12); pinned by tests/test_integration_boundaries.py |
| GR-21 approved record is the source of truth | Partial | Inputs come from the approved form version; conflict detection is a parent concern |

## 4. Edge cases EC-01 to EC-52
**Implemented and tested in UC-04:** EC-10 (test_ec10), EC-11, EC-12, EC-13, EC-19, EC-20, EC-22 (equipment release and request cancellation drafts), EC-23 (IT-request part only),
EC-29 (message drafts and loan-device IT request draft), EC-30 (manager message draft quoting IT's reason), EC-31, EC-32 (reminder drafts, collection-slot and reservation request drafts), EC-33 (exchange request draft),
EC-34 (`deployment_date`), EC-35 (client change, tests/test_client_change.py; project change via optional `project_id`, tests/test_scenarios_complete.py), EC-36 (country calendar and timezone, courier draft, remote first-day plan),
EC-37, EC-38, EC-39, EC-40 (`Uc04Agent.evaluate_cohort` and `plan_slots`, `cohort.py`), EC-42 (internal transfer plan, `transfer.py`), EC-44, EC-51, EC-52.
EC-29 to EC-36, EC-40 and EC-42 are detailed in spec section 16 and verified by tests/test_scenarios_complete.py. All are verified against mock adapters only; live integration is pending.
**Partial:** none. Fields still marked PROPOSED pending business or IT confirmation: EC-33 `asset_ref`, `assigned_model`, `reported_model`, `fault_note`; EC-35 `project_id`; EC-31 exception approver role names (HC-08).
**Outside UC-04 (expected behaviour recorded by the framework; no UC-04 code):** EC-01 to EC-09 (documents), EC-14 to EC-18 and EC-21 (visa, medical, background check, minors),
EC-24 (no-show; UC-04 never activates a record), EC-25 to EC-28 (travel, insurance, banking), EC-41 (agency contractor; not assigned to UC-04), EC-43 (rehire reactivation),
EC-45 to EC-50 (salary and policy questions, privacy requests, language).
**Approvals:** HR approves or rejects UC-04 proposals manually outside UC-04; UC-04 only reads recorded approvals and never decides them.
UC-04 does not claim that all 52 edge cases behave as expected (AC-05); only its own subset is tested.

## 5. Acceptance criteria AC-01 to AC-15 (targets, not results)
| AC | Classification for UC-04 |
|---|---|
| AC-01 right checklist and activities | Wider onboarding product |
| AC-02 approvals before messages, records, requests, spend | Directly applicable: tested for UC-04 requests (TC-03, TC-06, TC-35) |
| AC-03 cited policy answers | Wider product |
| AC-04 nothing outside the role list unless an exception | Directly applicable (TC-04, EC-31) |
| AC-05 all 52 edge cases | UC-04 subset only; rest in the wider product |
| AC-06 no medical or other people's data in outputs | Directly applicable for UC-04 outputs (TC-24, TC-32); wider product for other outputs |
| AC-07 due dates honour lead times, items allowed after joining, calendar | Partly applicable. Lead times and calendar: verified, including boundaries (TC-22, TC-27; tests/test_calendar_boundaries.py). "Items allowed after joining": **blocked by an unanswered requirement**. The Framework allows it only for documents (EC-08); for IT, equipment and access it says they must be ready by day one, so no post-joining item is modelled and open items at or after joining are flagged (spec section 12) |
| AC-08 HR rates drafts usable | Needs pilot and business sign-off |
| AC-09 document reading accuracy | Wider product |
| AC-10 same employee, different plan per client | Directly applicable (test_ac10) |
| AC-11 client settings never override guardrails, no cross-client view | Directly applicable (TC-19, EC-51) |
| AC-12 new client by configuration only | Directly applicable in simulation (test_new_client_needs_only_configuration_not_code); needs pilot client sign-off |
| AC-13 activities start when ready; date risk flagged early | Partly applicable: UC-04 items only (TC-12, TC-15) |
| AC-14 no pay, loan or bank data in team, IT, facilities, client-site outputs | Directly applicable for UC-04 outputs (test_gr18, TC-24) |
| AC-15 Zoho records match the approved form | Wider product; needs real integration |
Passing UC-04's own tests does not satisfy any wider onboarding criterion.

## 6. Test ids (TC-01 to TC-39) to test files
| TC | File |
|---|---|
| 01 to 09, 16, 19, 27 | `test_rules.py` |
| 10 | `test_approvals.py` |
| 11, 13, 14, 25, 26, 29 | `test_readiness.py` |
| 12, 15, 22, 28, 38 (service level) | `test_risk.py` |
| 17, 18, 20, 21, 23, 31, 35 (authorisation) | `test_failures.py` |
| 24, 32 | `test_privacy.py` |
| 30 | `test_lifecycle.py` |
| 33, 34, 35, 36, 37, 38 | `test_two_paths.py` |
| 39 | `test_packaging.py` |
TC-01 to TC-32 follow the assignment brief; TC-33 to TC-38 follow the architecture brief; TC-39 is a local addition (no source documents, git data, network or secrets in the project).
Edge-case tests: `test_edge_cases.py` and the ids above. Evaluations: `evaluation/run_evals.py` (12 scenarios, both entry points).

## 7. Reference-repository evidence used
`hr_recruitment_common/contracts.py` (AgentRequest, WorkflowRef, Actor, AgentResponse, Proposal, AgentException, Autonomy); `platform/policy.py` (Policy, ToolsDef, GateDef, PolicyViolation);
`platform/tools.py` (ToolGateway); `platform/hitl.py` (ApprovalItem, HITLInbox); `platform/audit.py` (AuditLog); `platform/guardrails.py` (injection, PII and pay-figure patterns);
`connectors/ats/base.py` and `mock.py` (port plus mock pattern); `hr-recruitment-candidate-matching-agent` (deterministic agent with thresholds in policy and a zero-model-call test).
Gaps found and handled differently in UC-04: no idempotency in `BaseAgent.run`; `ToolGateway._require_approval` accepts an approval with no subject for any subject; `ApprovalItem.is_approved` also accepts edited approvals;
`AuditLog` has no injectable clock. The reference repository has no orchestrator, no onboarding agent, no MCP server and no IT service-desk connector.

## 8. Pre-integration review additions (2026-10-27)
Defects found by the audit and fixed, each with a regression test that was shown to fail before the fix:
| Id | Requirement | Defect | Fix | Tests |
|---|---|---|---|---|
| D1 | BR-21, BR-22, EC-20 | An idempotent replay returned the old answer after the parent case became WITHDRAWN or ON_HOLD | Replay is served only while case state and parent status both match | test_failure_atomicity.py (5 tests) |
| D2 | BR-18, BR-15 | A failed evaluation (privacy guard, unexpected error) left half-applied state; a retry lost the cancellation drafts | Stored state is restored on failure and audited as `state.rolled_back` | test_failure_atomicity.py (5 tests) |
| D3 | EC-35, DL-02 | After a client change the old client-site requests were orphaned: not cancelled, not reported, no draft | Cancel and archive them, propose draft cancellations, refuse reuse of their keys | test_client_change.py (15 tests) |
| D4 | input safety | `dev_reset_first` accepted "yes"; any orchestrator `contract_version` was accepted | Strict boolean; only contract version 1.0 accepted | test_api_hardening.py (15 tests) |

Verified without a code change: AC-07 lead-time and calendar boundaries (tests/test_calendar_boundaries.py, 20 tests); port and mock signatures, no external
imports in business modules, one gated write tool, no spend (tests/test_integration_boundaries.py, 9 tests).
Unresolved, recorded for the business owner in spec section 12: HC-08, AC-07 post-joining items, EC-35 `project_id` source (detection is implemented), separation of duties,
evidence timestamps, withdrawn single requests, orchestrator envelope fields.

## 9. UC-02 onboarding plan link (orchestrator side, 2026-11-10)
No UC-04 core rule changed: `Uc04Input`, `Uc04Service` and the item configuration are as before. Implemented in `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/adapters/uc04_uc02_plan.py`, called by `adapters/uc04_equipment_access.py`; spec section 15.
| Requirement | Implementation | Tests (`HR-Onboarding-Common/tests/orchestration/test_uc04_uc02_plan_link.py`) |
|---|---|---|
| UC-02 is the source of the plan; read-only, no plan created | `service_plan_reader` (UC-02 `OnboardingService.get`) | test_the_cases_own_plan_is_read_from_uc02_with_its_service_tasks, test_reading_the_plan_creates_nothing_in_uc02 |
| Never another case's, client's or employee's plan | `read_plan` identity check; UC-02 `DataIsolationError` | test_another_cases_plan_for_the_same_employee_is_never_attached, test_another_clients_plan_is_refused_by_uc02_isolation, test_a_plan_of_another_employee_is_refused, test_uc04_is_blocked_without_being_called_when_the_plan_is_another_cases |
| Missing plan or failed read blocks, no fallback | `UC02_PLAN_MISSING`, `UC02_PLAN_UNAVAILABLE` | test_no_plan_on_the_case_is_missing, test_a_plan_uc02_does_not_hold_is_missing, test_a_uc02_failure_is_unavailable_never_replaced, test_uc02_unbuildable_is_unavailable, test_uc04_is_blocked_when_the_case_has_no_plan |
| Service tasks reach UC-04's stage; advisory checks never block | `advisory_checks` -> `equipment_access_result.uc02_plan` | test_the_service_data_reaches_uc04s_stage_and_warnings_do_not_change_its_status, test_no_service_tasks_is_a_warning, test_config_version_mismatch_match_and_not_comparable, test_due_date_before_uc04_lead_time_is_a_warning_and_workplace_is_not_compared |
| Demo mode uses a labelled stand-in, never silently | `run_workflow_demo._uc02_plan_stand_in` | test_the_demo_stand_in_reader_is_labelled_and_refuses_any_other_plan |
