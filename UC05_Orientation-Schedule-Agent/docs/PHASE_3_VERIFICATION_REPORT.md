# Phase 3 Verification Report: HR-Onboarding-Agent

**Date:** 2026-10-27  **Version verified:** 0.3.0 (`employee-onboarding@v8`)  **Verdict:** not production-ready (see §12)

This report records what was executed and observed. Statements derived from reading code, rather
than from running it, are labelled *(code inspection)*. Nothing here certifies security or
production readiness.

Verification had two independent strands:
- **My own checks:** §§2–10.
- **A separate adversarial review agent** (§13). It was given the code but not my findings, and had to reproduce every defect with a script before reporting it.

It confirmed **14 defects** that my checks had missed or only partly covered. 12 are fixed, and the
fixes are verified three ways: the original reproduction scripts rerun, new regression tests, and a
mutation check. 2 remain open and documented. Summary in §13.

---

## 1. Scope and environment

| Item | Value |
|---|---|
| Repository | `C:\Studies\Projects\D-ACT\HR-Onboarding-Agent` (**not a git repository**) |
| Change-scope baseline | Pre-refactor snapshot `HR-Onboarding-Agent-before-refactor.tgz` (session scratchpad), taken before Phase 2 |
| OS / Python / uv | Windows 11 (10.0.26300) / Python 3.13.1 / uv 0.13.6 |
| Key packages | langgraph 1.2.16, langgraph-checkpoint 4.3.0, langgraph-checkpoint-sqlite 3.1.2, fastapi 0.157.0, pydantic 2.14.1, pytest 9.1.1, hr-onboarding-common 0.1.0 (path dependency) |
| LLM | Offline `FakeLLM` throughout. Live mode was **not** run, so no model-quality claim is made. |
| External systems | None contacted. No email, calendar event or message was sent anywhere. |

Because there is no git history, change scope was established by `diff -rq` against the snapshot.

---

## 2. Directory structure

All required paths exist (checked one by one):
- `connectors/{email,employee_directory,calendar,teams,notifications}`
- `context_layer/{skills,knowledge}`
- `evaluation/run_evals.py`
- `guardrails/policy.yaml`
- `src/sub_agent/{__init__,agent,api,fakes}.py`
- `tests/testcase.py`
- `.env.example`, `pyproject.toml`, `README.md`

**Additional files retained, and why:**
- `src/sub_agent/`: `services/` (business rules), `tool_registry.py` (the authorization and policy enforcement point), `models.py` and `repository.py` (split from the old `connectors/onboarding/base.py`), `signatures.py` (e-signature boundary).
- `tests/`: 18 focused test modules plus `conftest.py`.
- `docs/`: existing design records.

**Responsibilities:**
- `connectors/` holds interfaces only. Every in-memory double lives in `src/sub_agent/fakes.py`.
- `context_layer/` and `connectors/` never import the agent, the API or the services. Verified in fresh interpreters by `test_context_layer.py::test_context_layer_and_connectors_never_import_the_agent` (7 modules).
- The internal import graph has **no cycles** (AST check).

**Obsolete modules and old paths:**
- `src/onboarding_agent/` and `context_layer/data/` were removed in Phase 2.
- No source, test or evaluation file references `onboarding_agent`. The only remaining mentions are in the dated notes in `docs/`.

**Change scope against the snapshot:**
- Modified: `.env.example`, `README.md`, `pyproject.toml`, `uv.lock`, `guardrails/policy.yaml`, one SKILL.md, three docs, all 17 pre-existing test files.
- Added: `connectors/`, `context_layer/{__init__.py, knowledge/, skills/__init__.py, skills/hr-policy-answerer/}`, `evaluation/`, `src/sub_agent/`, four test files, and this report.
- Removed: the old package and `context_layer/data/`.
- A `.mypy_cache/` created by this phase's type-check was deleted.

**Other repositories:**
- The Recruitment Agent and `hr-recruitment-common` are not present on this machine.
- `hr-onboarding-common` was not modified: its newest file is dated 09:37, before this work began.
- **Observation, out of scope:** its source tree contains an uninstalled `src/hr_onboarding_common/Connectors/` package that duplicates the agent's connectors. Nothing references it, and it was not touched.

**Unexpected external edit:**
- **What happened:** at 14:47:06 a stray `.` was inserted into an import line of `src/sub_agent/fakes.py`, breaking every import.
- **Source:** neither this session nor the review agent made that change.
- **Action:** only that line was restored. The full suite passed before and after.

---

## 3. Packaging and imports

| Check | Command | Result |
|---|---|---|
| Lockfile consistency | `uv lock --check --offline` | Pass: 102 packages, lock up to date |
| Environment matches lock | `uv sync --locked --offline --dry-run` | Pass: "Would make no changes" |
| Compilation | `python -m compileall -q src connectors context_layer evaluation tests` | Pass: exit 0 |
| Wheel | `uv build --wheel --offline` | Pass: `connectors`, `context_layer`, `sub_agent`; 6 policy documents and 2 SKILL.md files included; no tests, docs or old package |
| Clean-environment install | new venv + `uv pip install <wheel> ../hr-onboarding-common`, run from an unrelated directory | Pass with `AGENT_HOME` set (`/healthz` reports `employee-onboarding@v8`; offer-accepted returns 200) |
| Same, without `AGENT_HOME` | as above | Pre-existing defect V1, now fixed: fails fast at startup (§11) |
| Lint (not configured by the project) | `uvx ruff check --select F,E9 src connectors context_layer evaluation tests` | Pass |
| Type check (not configured by the project) | `uvx mypy -p sub_agent -p connectors -p context_layer` | 1 error, pre-existing: `fakes.py:125`, in code moved verbatim from the original mock. The second pre-existing error (`intake.py:38`) was removed by the #1 fix. |

---

## 4. Application startup and API

**Startup and configuration**, from `tests/test_startup_and_config.py` (6 tests, each in a fresh interpreter):

| Scenario | Observed |
|---|---|
| Default offline configuration | `/healthz`, `/openapi.json`, a record read, `/v1/runs` and a workflow route all return 200 |
| `HR_AGENT_PERSIST=1` | `hitl.db`, `checkpoints.db` and `audit.jsonl` are created |
| `CONNECTOR_MODE=live` / `SIGNATURE_VERIFIER_MODE=live` | Startup fails ("… is not supported") |
| No `guardrails/` under `AGENT_HOME` | Startup fails with guidance |
| `HR_AGENT_ALLOW_INSECURE_HEADER_IDENTITY=0` | Protected routes return 401; only `/healthz` and `/openapi.json` stay open |

**API contract.** The pre-refactor app (from the snapshot) and the current app were compared on
their OpenAPI documents: method, path, body fields, required fields, parameters and response codes.

| | Count |
|---|---|
| Routes before / after | 26 / 31 |
| Removed | **0** |
| Changed | **0** |
| Added | 5 workflow routes |

The check was repeated after the §13 fixes, with the same result. `/v1/runs`'s original body still
drafts a welcome message; a missing `employee_id` or an unknown event still returns 422.

**Behaviour tightened behind unchanged routes (by the §13 fixes):**
- Naive datetimes (no timezone) in `start`, `induction_start` and `as_of` now return 422, where they used to be silently read in the server's local timezone.
- `POST /v1/onboarding/sessions/check-timeouts` now requires `hr_admin`.
- An RSVP for someone other than the caller now requires `hr_admin`.
- Cross-tenant 403 messages no longer name the owning client.

**Live service.** With `HR_AGENT_PERSIST=1`, a real `uvicorn` process was restarted mid-scenario. The
checkpoint survived, but the case returned 404, because the repository is in memory (§6).

---

## 5. Workflow verification

**Graph structure** (`tests/testcase.py`):
- All 25 nodes are reachable from START and every node reaches END (`test_every_node_is_reachable…`).
- Driving every branch family, every node executes and none writes an undeclared state key (`test_every_node_runs_and_writes_only_declared_state_keys`). This matters because LangGraph 1.2 was verified to *silently drop* undeclared keys.
- The review agent separately confirmed that every route value a node returns is in that node's path map.

**Stage matrix:**

| # | Stage | Implementation | Evidence (tests) | Status |
|---|---|---|---|---|
| 1 | Offer acceptance received and validated | `_validate_offer_event` → `services/intake.py` | valid / malformed / incomplete / unknown / duplicate / cross-tenant (also for an already-processed event, R01); `test_intake_service.py` | **PASS** (events enter through the API; there is no mailbox) |
| 2 | Employee details retrieved and validated | intake → `EmployeeDirectoryPort` (fake) | incomplete record flagged, no case; directory tenant checked | **PASS** against the fake |
| 3 | Induction drafted (participants, time, office hours, availability) | `draft_induction_meeting` → `find_available_slot` / `schedule_session` | joining-day 09:00 Dubai slot, 90 min, `[new hire, team lead]`; outside hours, no slot, busy participant, past start (R14), late joiner (R14) | **PASS** |
| 4 | HR approval requested | HITL item + `request_hr_approval` → `NotificationPort` | item created; role queue or the specific team lead notified | **PARTIAL**: the Teams/Outlook channel is `FakeNotifier` only |
| 5 | Pause and resume for approval | run ends at the wait; HITL listener; checkpoint resume | mid-run outage resume; restart resume (SQLite); decision retry after restart; the listener's thread cannot be hijacked (R02) | **PARTIAL**: onboarding records are in memory and lost on restart (§6) |
| 6 | Approved meetings scheduled; rejected not | `apply_meeting_decision` | confirmed only after approval; rejection releases the hold; late approval after timeout ignored; confirm failure retried; edited payload cannot redirect; withdrawn case not applied (R12); unapplied rejection released by the sweep (R06) | **PARTIAL**: a tentative hold is booked and invitations are recorded "sent" *before* approval *(code: `schedule_session`)* |
| 7 | Participant responses tracked | `record_participant_response` | accepted response satisfies attendance; non-invitee refused; cancelled session refused; only self or `hr_admin` may respond (R09) | **PASS** for recording. Out-of-order delivery is a gap (§9). |
| 8 | Conflicts routed to HR | decline → `attendee_declined` item + notification | `test_declined_rsvp_routes_to_an_hr_conflict_decision` | **PASS**, with one gap: a later "accept" leaves the HR item open (§9) |
| 9 | HR keep/cancel/replan validated and applied | `apply_conflict_decision` | keep, cancel, replan; malformed / naive / wrong-key replan routed to HR with the original meeting kept (R08); second decision on a replanned meeting books nothing (R07); policy approver role used (R07) | **PASS** |
| 10 | Buddy assignment follows the rules | `assign_buddy`, `apply_buddy_decision` | same-team; authorized pool; edited buddy restricted; no candidates; rejection; notification retry; one rejected notice does not block the other (R13) | **PASS** |
| 11 | Team intro prepared and scheduled | `prepare_team_intro` (the team lead on record approves) | drafted after buddy; only the team lead on record confirms; never in the past (R14) | **PARTIAL**: same tentative-hold caveat as stage 6 |
| 12 | Policy Q&A independently callable | `answer_policy_question` + `/v1/onboarding/policy-questions` | cited answer; injection refused before the model; model outage gives a safe no-answer; missing knowledge gives "no matching policy"; no side effects on other stages | **PASS** offline. Live quality **NOT VERIFIED**. |
| 13 | Joining-day welcome | `prepare_welcome_messages` + `POST …/workflow/joining-day` | branded template; language rules; approval required; delivered once; edited and partial-edit text sent (R08); duplicates prevented (R03); a failed run resumes (R10) | **PARTIAL**: **no scheduler**. Delivery is `FakeNotifier`. |

**Routes still without a test (NOT VERIFIED):**
- `validate_offer_event` → inactive case
- `draft_induction_meeting` → `exists`
- `prepare_team_intro` → `exists` and `failed`
- `assign_buddy` → `awaiting` and `assigned`
- `apply_buddy_decision` → `stale`
- `apply_welcome_decision` → `sent`
- `deliver_welcome` → duplicate-send
- `record_participant_response` → `reject`

**Newly covered in this phase:** `apply_conflict_decision → replan_failed` (R08), `load_decision → ignore` (R12), and the "late decision" path of `apply_conflict_decision` (R07).

**Independent triggers:** a policy question, an accepted RSVP and a joining-day run with no joiners
change no sessions, approvals, drafts or buddy assignments
(`test_independent_triggers_do_not_run_unrelated_stages`).

---

## 6. Approval lifecycle and checkpointing

| Case | Result | Evidence |
|---|---|---|
| Authorized approval | PASS | `test_approved_induction_is_confirmed…`, HTTP flow test |
| Unauthorized role | PASS (403) | `test_decision_by_an_unauthorized_role_is_403` |
| Wrong specific approver | PASS: never applied | `test_team_intro_decided_by_a_different_team_lead…` |
| Rejected approval | PASS | hold released; a rejection that could not be applied is released by the sweep (R06) |
| Duplicate decision | PASS (409) | `test_deciding_the_same_approval_twice_is_409` |
| Replayed callback / decision through `/v1/runs` | PASS | `retry_apply` refuses a clean item; a `/v1/runs` decision cannot complete, poison or resume the listener's thread (R02) |
| Other employee / draft / session | PASS | wrong-draft and wrong-employee welcome approvals refused; edited payload cannot redirect a booking |
| Invalid transitions | PASS | non-pending returns 409; late approval after a timeout is ignored; approval on a withdrawn case not applied (R12) |
| Resume after approval / failure after approval / retry | PASS | `test_calendar_failure_while_confirming…`, delivery and notification retry tests |
| Induction not confirmed before approval | PASS for confirmation | see stage 6 for the tentative hold |

**Checkpoint backend:**
- `SqliteSaver` at `<HR_AGENT_DATA_DIR>/checkpoints.db` when `HR_AGENT_PERSIST=1`, otherwise in-memory `MemorySaver`.
- Restart survival is shown by 2 tests, mutation-checked: both fail with the in-memory saver.

**Repository:** `MockOnboarding` is **in memory only**. On a real restart, approvals and checkpoints
survive but cases are gone (404). **Durable end-to-end resumption is not supported** until a
persistent repository exists.

---

## 7. Connectors and integrations

| Integration | Classification | Notes |
|---|---|---|
| Email offer-acceptance events | **Mocked** (`MockEmailAdapter`) | Outage and malformed cases tested; ingestion through the API only |
| Employee directory | **Mocked** | Incomplete-record and tenant cases tested |
| Calendar availability and creation | **Mocked** (`FakeCalendar`) | Conflict, outage, transient, confirm-failure and lost-response cases tested |
| Participant RSVPs | **Missing** as a provider integration | Recorded through the API |
| Teams messaging and bot | **Missing** | Contract documented in `connectors/teams/`; no adapter, no fake bot |
| HR approval notifications | **Mocked** (`FakeNotifier`, `simulated=True`) | Approval items themselves are real |
| Welcome delivery | **Mocked** | No joining-day scheduler |
| E-signature | **Mocked** (`FakeSignatureVerifier` in the demo); the default `NullSignatureVerifier` rejects everything | |
| Onboarding database | **Mocked** (in memory) | |
| LLM | Offline fake verified; live gateway **implemented but not integration-tested** | |

**Authentication and timeouts** are documented error contracts only (`connectors/*/base.py`).

- **Unconfigured connectors:** fail closed with `ConnectorNotConfiguredError`. This is tested at the registry level; its HTTP 503 mapping is configured but untested.
- **Calendar and email outages:** return 502, tested over HTTP.
- **Notification outages:** exercised only indirectly, through the retry tests.

---

## 8. Security findings

Original severities S1–S6 are from `docs/ONBOARDING_COMMON_ARCHITECTURE.md` Part B. P-items were
found in Phases 1–2, V-items in my Phase 3 checks, and R-items by the independent review (§13).

| ID | Finding | Severity | Verification | Status |
|---|---|---|---|---|
| S1 | Cross-tenant approval access and decisions | Critical | `test_security_fixes.py` (get/list/decide; HTTP 404); evaluation | **Fixed and verified** (in `hr-onboarding-common`) |
| S2 | Insufficient role authorization | High | role gate on signatures; HITL role checks; timeout sweep and on-behalf RSVPs now role-gated (R09); startup test with header identity disabled | **Partially fixed**: roles still come from **client-supplied headers** |
| S3 | Forged acknowledgement evidence | Critical | checklist and security tests; evaluation | **Partially fixed**: fails closed by default; the demo wires `FakeSignatureVerifier`; no real provider |
| S4 | Cross-tenant `/v1/runs` | Critical | 403/404 tests; instance-mismatch test; in-graph check | **Fixed and verified** |
| S5 | Decision consumed before its follow-up succeeds | High | apply_error / retry_apply; resume and restart tests; unapplied rejections released (R06) | **Fixed and verified** |
| S6 | Kill-switch bypass | High | runs, decisions, registry routes; **in-flight runs now stopped** (R11) | **Fixed; verified on representative paths** |
| P1 | `/v1/runs` acted on `input.employee_id`, not `instance` | Critical | instance-mismatch test | **Fixed and verified** |
| P2 | Decision side effects bypassed the gateway | High | kill-switch decision test | **Fixed and verified** |
| P3 | Welcome send defects (edits ignored, approval not bound, per-request dedupe) | High | registry and workflow tests | **Fixed and verified** |
| P4 | Silent fallback to mock connectors | Medium | fail-closed test | **Fixed and verified** |
| P5 | Unscreened policy questions | Medium | Q&A tests + evaluation | **Fixed and verified** |
| P6 | Registry relied on the repository's tenant filter | High | mutation-checked test | **Fixed and verified** |
| P7 | HR edit could assign an ineligible or other-tenant buddy | High | unit, workflow and evaluation tests | **Fixed and verified** |
| P8 | Forged RSVPs (non-invitee, cancelled session) | Medium | unit + HTTP 422 | **Fixed and verified**. Sender identity relies on headers (S2). |
| V1 | App started without its policy envelope | Low | startup test | **Fixed** (pre-existing) |
| V2 | Provider error text echoed to the caller | Medium | 2 tests | **Partially fixed**: the Q&A paths are generic; `BaseAgent` (library) still formats `"LLM failure: {e}"` for other model-backed runs |
| R01–R14 | Independent review findings | Critical to Low | see §13 | 12 fixed, 2 partially fixed or open |

**Other checks:**
- **Logs and audit:** a full scenario produced 71 log lines and 68 audit events with no name, email address, body or question text. With `HR_AGENT_PERSIST=1`, checkpoints do store a policy question's text *(code inspection)*.
- **Unauthorized tools:** a tool missing from the allowlist is refused (`test_tool_not_on_the_allowlist_is_a_policy_violation`).
- **HTTP routes:** every route goes through `IdentityProvider`, then the registry or the agent *(code inspection)*.

---

## 9. Idempotency and failure recovery

| Scenario | Result | Mechanism / evidence |
|---|---|---|
| Same offer event received repeatedly | **PASS** | duplicate detection, also after a restart; a *rejected* run no longer consumes the event (R02) |
| Meeting creation times out after the provider created it | **FAIL: known gap** | the retry books a second hold. `xfail(strict)` `test_booking_timeout_after_the_provider…` (observed: 2 bookings) |
| Approval callback delivered twice | **PASS** | HITL 409; `retry_apply` only after a recorded failure; `/v1/runs` cannot touch the listener's thread |
| RSVPs out of order | **Gap** | last write wins; no provider timestamp; decline then accept leaves the HR conflict item open |
| Buddy assignment retried | **PASS** | pending-proposal reuse; assignment not repeated; notifications per approval and recipient |
| Welcome delivery retried after an uncertain response | **FAIL: known gap** | re-sent. `xfail(strict)` `test_welcome_delivery_timeout…` (observed: 2 sends) |
| Duplicate or concurrent welcome drafting | **PASS, within one process** | an existing draft is reused, and welcome runs are serialized per tenant (R03; mutation-checked). **Not** across multiple worker processes. |
| Restart while waiting for HR approval | **PARTIAL** | approval and checkpoint survive (`HR_AGENT_PERSIST=1`); cases do not |
| Provider temporarily unavailable | **PASS** | resume at the failed node, including a failed joining-day notification run (R10) |
| Node fails after an earlier side effect | **PASS** | checkpoint resume; buddy assigned once |

**Exactly-once is not guaranteed for outbound side effects.** Duplicates are prevented on the
agent's own records, but not when a provider acted and its response was lost. That needs provider
idempotency keys on `CalendarPort.book_event` and `NotificationPort.send`: an interface change,
recommended separately.

---

## 10. Test commands and results

| Command | Result |
|---|---|
| Phase 1 baseline: `.venv\Scripts\python.exe -m pytest` | **232 passed** |
| End of Phase 2: `uv run pytest` | 312 passed |
| **Phase 3 final: `uv run --offline pytest -rxX`** | **352 passed, 2 xfailed** (the two known gaps in §9); exit 0 |
| Review reproductions after the fixes: the 21 `r*.py` scripts (§13) | every fixed defect now shows the corrected behaviour; #5 still reproduces, as documented |
| Mutation check (8 mutants, each undoing one fix in a throwaway copy) | **8/8 caught** by the regression tests |
| Evaluations: `uv run --offline python evaluation/run_evals.py` | **36 scenarios: 35 pass, 0 fail, 1 skip** (needs a live model). Policy Q&A: accuracy 1.0, citation precision 1.0, 0 forbidden citations. Exit 0. |
| Lint / types / packaging | see §3 |

No test was removed or weakened. Existing-test changes in Phase 2 were limited to imports and
fixture wiring. The two `xfail(strict=True)` tests assert the *correct* behaviour and fail today;
`--runxfail` shows the expected assertion.

Collected per file (354 total):
- `testcase.py`: 83
- `test_scheduling_service.py`: 29
- `test_api_routes.py`: 28
- `test_security_fixes.py`: 26
- `test_onboarding_repository.py`: 22
- `test_tool_registry.py`: 21
- `test_attendance_and_approver_routing.py`: 19
- `test_checklist_service.py`: 17
- `test_phase9_lifecycle_and_gaps.py`: 15
- `test_welcome_service.py`: 15
- `test_intake_service.py`: 13
- `test_policy_qa_service.py`: 13
- `test_context_layer.py`: 11
- `test_invitations_and_timeouts.py`: 8
- `test_meeting_time_validation.py`: 8
- `test_policy_knowledge_base.py`: 8
- `test_joining_trigger_service.py`: 7
- `test_startup_and_config.py`: 6
- `test_onboarding.py`: 5

The test world uses a fixed clock (2026-10-27). Tests that drive the shared API singleton use the
real clock, and their fixture dates (Nov–Dec 2026) must stay in the future.

---

## 11. Defects fixed during verification

**From my own checks:**

| Defect | Origin | Fix | Tests |
|---|---|---|---|
| V1: app started without its policy envelope | pre-existing | `api.py` fails fast with guidance to set `AGENT_HOME` | `test_missing_policy_envelope_fails_startup_with_guidance` |
| V2: model outage leaked provider text (workflow Q&A); bare 500 (individual Q&A route) | pre-existing | Q&A node returns a safe no-answer; `LLMError` maps to a generic 503 | 2 tests in `testcase.py` |
| Stray `.` in `fakes.py` | external edit | that line restored | full suite |

**From the independent review:** 12 defects fixed (table in §13).

**Test infrastructure added** (test doubles only):
- `FakeCalendar(fail_next_confirms=, lose_next_booking_responses=)` and `FakeNotifier(lose_next_responses=)`.
- `OnboardingDependencies.clock`: an injectable "now", so scheduling decisions are testable.

---

## 12. Remaining defects, blockers and next steps

**Blockers for any production use:**
1. **Identity.** Header-trust identity (S2): a real IdP or trusted gateway is required.
2. **Durability.** No persistent onboarding repository, so cases are lost on restart.
3. **Integrations.** No real email, HRMS, calendar, Teams/Outlook, e-signature or notification provider.
4. **Scheduling.** No joining-day scheduler.
5. **Scaling.** The workflow's locks are per process. More than one worker needs a shared checkpointer and cross-process locking (R03 holds only within one process).

**Defects to fix next:**
1. **Uncertain-outcome retries (R05):** add provider idempotency keys to `CalendarPort.book_event` and `NotificationPort.send`, then promote the 2 `xfail` tests.
2. **Invitations before approval:** decide whether tentative holds may notify attendees before approval, or book only after approval (stages 6 and 11).
3. **Existence check (R04 remainder):** cross-tenant 403 versus not-found 404 still reveals that a record exists; the approvals API answers 404 for both. Unifying them changes an asserted contract (`test_cross_tenant_read_is_403` and others).
4. **Stale conflict decisions:** an RSVP accepted after a decline should resolve or flag the open HR conflict decision; use provider timestamps for ordering.
5. **Past times on HR routes:** `POST /v1/onboarding/{id}/sessions` still accepts a timezone-aware start in the past. Only the workflow refuses past slots.
6. **Provider error text (V2 remainder):** handle it in `BaseAgent` (library change).
7. **Untested routes:** add tests for those listed in §5, and fix the remaining pre-existing `mypy` error.

**Housekeeping:**
- `docs/API_ENDPOINT_TEST_EXAMPLES.docx` is stale.
- Decide what to do about the duplicate `Connectors/` package in `hr-onboarding-common`.
- Confirm the placeholder meeting participants in `policy.yaml` with HR.

---

## 13. Independent review findings

**Method:**
- A separate review agent received the code paths to review and the categories to hunt, but none of my findings.
- It was told not to modify the repository. It complied: its scripts live in the session scratchpad `review/` directory, and it wrote only bytecode caches into the repo while importing.
- It reproduced each defect with a script (`r01`–`r19`, 21 files).
- I reran every script before the fixes (all reproduced; log `review/before_fixes.log`) and after them (`review/after_fixes.log`).
- I then added a regression test per defect and confirmed with a mutation check that the tests fail when their fix is undone.

**Origin:** *Refactor* means introduced by the Phase 2 workflow layer. *Pre-existing* means present in the original code.

| # | Finding (severity) | Origin | Before | After the fix | Regression test(s) | Status |
|---|---|---|---|---|---|---|
| R01 | Tenant A could read tenant B's full onboarding record by redelivering B's already-processed offer event id (**Critical**) | pre-existing (`intake.py`) | 200 with B's name, email, team, client | 403 "not accessible to this client" | `test_r01_…` (mutation-checked) | **Fixed** |
| R02 | A role-less `/v1/runs` `approval_decided` call shared the HITL listener's checkpoint thread: it could make a later HR decision a silent no-op, resume a failed decision outside the listener (sending the email), or make a real offer event look like a duplicate (**High**) | refactor | HR approval did nothing; email sent via `/v1/runs`; no case created | separate thread for non-listener decisions; rejected runs do not consume keys | 3 `test_r02_…` (mutation-checked) | **Fixed** |
| R03 | Duplicate welcome emails: `/v1/runs` re-drafted each time; concurrent runs both drafted (**Medium**) | pre-existing, extended by the refactor | 3 emails; 2 drafts | existing draft reused; welcome runs serialized per tenant | `test_r03_…` ×2 (both mutation-checked) | **Fixed, within one process** |
| R04 | 403 bodies named the other tenant's client (and ids); 403 vs 404 reveals existence (**Medium**) | pre-existing | "belongs to CLIENT-GLOBEX…" | generic "not accessible" text; status codes unchanged | `test_r04_…` | **Partially fixed**: the existence check is still open |
| R05 | A provider timeout after success is retried as a duplicate booking or email (**Medium**) | design gap | 2 holds; 2 emails | unchanged | 2 strict `xfail` tests | **Open** (interface change) |
| R06 | A rejection whose apply failed (outage or kill switch) could never be applied; the hold leaked (**Medium**) | refactor (kill switch) + pre-existing (outage) | session scheduled, hold tentative forever | the timeout sweep releases unapplied rejections | `test_r06_…` | **Fixed** |
| R07 | Two conflict decisions on one meeting booked two replacements; replan ignored the policy approver role (**Medium**) | pre-existing | 2 confirmed inductions; `hr_admin` instead of `team_lead` | already-resolved session ignored; policy role used | 2 `test_r07_…` | **Fixed** |
| R08 | Malformed edits stuck permanently: a bad replan cancelled the meeting first; a partial welcome edit could never be sent (**Medium**) | pre-existing (replan) + refactor (welcome) | original cancelled with no replacement; draft approved but never sent | replan validated before cancelling, failures routed to HR; partial edits merged, unusable content routed to HR | 3 `test_r08_…` (parametrised) | **Fixed** |
| R09 | The timeout sweep had no role check and used the caller's clock (naive value gave 500); anyone could RSVP for anyone (**Medium**) | pre-existing | role-less user cancelled all bookings; intern declined as the team lead | sweep needs `hr_admin` and a timezone (422); RSVP for oneself or by `hr_admin` | 2 `test_r09_…` + `test_r14_naive_…` | **Fixed** (identity still header-based, S2) |
| R10 | A notification outage mid-joining-day lost approval requests permanently (**Low/Medium**) | refactor | retry reported nothing to do | joining-day runs keyed by date; the retry resumes and notifies | `test_r10_…` | **Fixed** |
| R11 | The kill switch did not stop a run already in progress (**Low/Medium**) | refactor | 2 notifications after the switch | live policy checked before every node; run resumes when re-enabled | `test_r11_…` (mutation-checked) | **Fixed** |
| R12 | Approved decisions kept advancing a withdrawn case (**Low/Medium**) | refactor | team intro booked; welcome sent | approvals on inactive cases recorded but not applied; rejections still release holds | 2 `test_r12_…` (mutation-checked) | **Fixed** |
| R13 | A rejected buddy notice stopped the loop, so the new hire was never told (**Low**) | refactor | 0 notices to the new hire | each recipient attempted; rejections reported together | `test_r13_…` (mutation-checked) | **Fixed** |
| R14 | Meetings could be booked in the past; naive datetimes read in the server timezone (IST) (**Low**) | refactor (past slots) + pre-existing (naive) | team intro booked 2026-10-12 (past) | injectable clock; no slot before now; naive values refused (422 at the API, `InvalidMeetingTimeError` in services) | 3 `test_r14_…` | **Fixed** for workflow-drafted meetings; HR routes still accept a past aware time (§12) |

**Suspicions the review could not confirm (not run):**
- **Concurrent decisions for one employee:** a buddy approval and an induction re-confirmation at the same moment could each pass the "no live team intro" check. Same race class as R03, but on decision threads, which the welcome lock does not cover.
- **Corrected redelivery ignored:** a redelivered offer that resumes a failed run keeps the original input, so a corrected `induction_start` would be ignored *(code inspection)*.
- **Path from `client_id`:** the policy knowledge base builds a path from `client_id`, so a value such as `"."` would load every tenant's policies. This matters only if an identity provider could issue such an id.
- **`":"` in `client_id`:** checkpoint thread ids join `client_id` with `":"`, so an id containing `":"` could collide with another tenant's. Theoretical.

**Confirmed sound by the reviewer:**
- HITL tenant scoping.
- Registry tenant checks; the only gap was R01.
- Kill switch on new runs, decisions and registry calls; the exceptions were R06 and R11.
- Welcome-send binding.
- Buddy-edit restriction.
- Specific-approver gates.
- `/v1/runs` instance checks.
- Graph routing.
- Offer resume and duplicate detection.
- Policy Q&A tenant scoping and injection handling.
- Signature role and verifier.
- Notification retry deduplication.
