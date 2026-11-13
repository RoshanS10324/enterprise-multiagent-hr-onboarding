# UC-01: Employee Record Creation — handover

**Audience: engineering team lead and process SME.** This is a short brief
on what's built, how to run it, what needs your sign-off, and what's left
before this can run against real systems.

## 1. What is built

UC-01 is deterministic Python (no AI, no network calls) implementing the
employee-record-creation flow from the offer-accepted event through to a
written HRIS/payroll record: a start gate (approval/freeze/offer checks),
duplicate and rehire/variant detection, case and form-version creation,
discrepancy comparison against other HR sources, HRIS record preparation
and write with a human-approval checkpoint, and a read-side status/audit
API. It runs today entirely on in-memory adapters with synthetic data; no
real HRIS, database, or identity provider is wired in yet. A FastAPI HTTP
layer wraps the whole thing.

| Step | What it does | Module | Test file |
| --- | --- | --- | --- |
| U1 | Start gate: offer-accepted, hiring-freeze, form-approval checks | `start_gate.py` | `tests/test_start_gate.py` |
| U2 | Duplicate / rehire / same-name detection | `duplicate_check.py` | `tests/test_duplicate_check.py` |
| U3 | Case + form-version-1 + variant-approval creation | `open_case.py` | `tests/test_open_case.py` |
| U4 | Case-opened event (`publish_case_opened`) | `case_events.py` | `tests/test_open_case.py`, `tests/test_onboarding_entry.py` |
| Variant decisions (HC-12) | Deciding a variant approval (rehire/transfer/minor/duplicate notice) | `variants.py` | `tests/test_variants.py` |
| U5 | Comparing form vs. the offer and other HR-record snapshots, flagging discrepancies (R6). Identity-document checks are out of UC-01 scope. | `compare_sources.py` | `tests/test_compare_sources.py` |
| U6 | Preparing HRIS/payroll records for write, with the HC_05 approval gate | `hris_prepare.py` | `tests/test_hris_prepare.py` |
| U7 | Executing the HRIS write (post approval) | `hris_write.py` | `tests/test_hris_write.py` |
| U8 | Proposing and deciding a form-version change | `form_changes.py` | `tests/test_form_changes.py` |
| U9 | Case status, audit trail, pending-approvals worklist (read-only) | `case_status.py`, `access.py` | `tests/test_case_status.py`, `tests/test_access.py` |

`service.py` (`CaseService`) is the single entry point that wires policy
checks in front of U4-U9; `onboarding_entry.py` is the orchestrator for
U1-U3. `policy.py` + `policy/uc01_policy.toml` enforce role and
separation-of-duties rules on every decision. `api/` exposes all of this
over HTTP.

## 2. How to run

```
uv sync
uv run pytest
uv run python -m evaluation.uc01.runner
```

`uv run pytest` already runs the eval scenarios in-process
(`tests/test_evals.py`), so a plain test run covers both.

```
uv sync
cp .env.example .env   # APP_ENV=dev works out of the box
uv run uvicorn sub_agent.api.app:app --host 0.0.0.0 --port 8000
```

(Run a single worker, without `--reload`, when exercising a flow across
multiple requests — see docs/known-gaps.md #27: in-memory state lives
inside one process.) Full endpoint list and auth headers are in
[README.md](../README.md).

## 3. Decisions marked [Proposed] — need your sign-off

| Item | Where | Current behaviour |
| --- | --- | --- |
| Every role, autonomy level, and separation-of-duties flag | `policy/uc01_policy.toml` (whole file) | Drafted by engineering, not yet confirmed as real process. |
| HR_OFFICER-must-be-assigned rule | `policy/uc01_policy.toml`, known-gaps #19 | Only the HR officer assigned to a case may act on it; unassigned officers get 404. |
| HC_05_HRIS_WRITE separation of duties | `policy/uc01_policy.toml`, known-gaps #20 | The preparer of an HRIS batch cannot also approve it. A client with only one HR_MANAGER cannot complete a write under this rule. |
| Rejecting an HC_12_VARIANT decision (incl. possible-duplicate notices) | `variants.py`, known-gaps #21 | Always puts the case `ON_HOLD`, with no "reopen" path built yet — effectively terminal today. |
| Rejection of an HRIS write is terminal for that form version | `hris_prepare.py`, known-gaps #10, #16 | HR must raise a new form version and prepare again; no re-approval of the same batch. |
| Role capability sets and client-scoping rules for read access | `access.py` | Drafted by engineering (who can see status/audit/worklist, and PII limits), not confirmed. |
| `Viewer.client_ids` as a set (one HR user may serve several clients) | `models/viewer.py` | Built this way on an assumption; confirm it matches real HR staffing. |

## 4. Open questions blocking integration

- **OQ-01/Q-11** — Who actually creates and approves the onboarding form,
  and how does the offer-accepted event reach UC-01? Today the API
  (`POST /internal/onboarding-requests`) takes the form's status and, if
  the caller asserts APPROVED, the approver and timestamp as given —
  UC-01 never approves a form itself (see `api/schemas.py`,
  `OnboardingRequestBody`). We need to know the real upstream system and
  event.
- **Q-12** — What is the full onboarding form field list, and where does
  it live at rest? UC-01's `OnboardingForm` only has the fields this use
  case needed; it is not the system of record for the form.
- **Q-13** — Does the agent write to the HRIS directly, or does HR
  Operations enter the data after the agent prepares it? This decides
  whether `HrisGateway` is a real write path or just a staging/preview
  step.
- **Q-21** — What are the real employee-number generation rules (format,
  sequence, per-client vs. global)? `EmployeeNumberGenerator` is a stub
  today.
- **Q-23** — How is a rehire or duplicate actually identified in
  production? Today `duplicate_check.py` matches on email or id_number
  within the same client; a same-name-only match is never a merge, it
  only raises a non-blocking HR notice (EC-39) for review.
- **Field mapping** — Zoho (assumed source system in this build) vs. the
  real target(s), Workday/ADP or otherwise: `hris_mapping.py` is a
  placeholder mapping, not validated against any real schema.
- **Identity** — UC-01's auth is dev-only HTTP headers
  (`X-User-Id`/`X-User-Role`/`X-Client-Ids`), a stand-in for Microsoft
  Entra ID. No token validation exists yet (known-gaps #24).

## 5. Known gaps

**HIGH**
- No update-existing-record flow: a form change approved after HRIS
  records are already WRITTEN has nothing that pushes the change to the
  target system (known-gaps #11, #13).
- Multi-step writes (`open_onboarding_case`, `approve_form_change`) are
  not atomic/transactional; a crash partway through can leave partial
  state (known-gaps #1, #7).
- Rehire creates a new case and a new people record. The framework
  expects UC-01 to propose reactivating the existing employee record
  (EC-43). Against a real HRIS this would create a second record for the
  same person; case flow passes on stubs, but record reactivation is not
  built (known-gaps #28).

**MEDIUM**
- A HELD request is not stored, so there's no automatic re-check once a
  form is later approved (known-gaps #2).
- No concurrency lock around HRIS write status (known-gaps #14).
- `PersonRecord.full_name` doesn't update on a name-changing form change
  (known-gaps #9).
- Generic 409 `{"error": "conflict"}` for most `ValueError`s — no
  structured code to tell cases apart yet (known-gaps #26).
- EC-44 (equipment still held, expired documents on rehire) not built;
  depends on UC-03/UC-04 data (known-gaps #29).

**LOW**
- No file-watch/reload for `policy/uc01_policy.toml` (known-gaps #17).
- A hook-level `VERSION_NOT_IN_FORCE` check is currently unreachable
  through the normal call path (known-gaps #22).
- Eval runner's scenario format is a small bespoke interpreter, fine for
  16 scenarios but would need a richer design to scale (known-gaps #23).

## 6. Integration plan

All of the following are `Protocol` ports in `ports.py` with in-memory
adapters in `adapters/memory.py` today; each needs a real adapter before
this runs against live systems.

| Port | Target | What's needed from platform team |
| --- | --- | --- |
| `CaseRepository`, `FormRepository`, `ApprovalRepository`, `DiscrepancyRepository`, `HrisRecordRepository` | Cosmos DB | Containers/partition keys, connection/identity (managed identity preferred), and a decision on the transactional-write gap in section 5. |
| `HrisGateway` | An MCP tool, called behind API Management | The actual Zoho/Workday/ADP write contract, API Management route + auth, and the idempotency-key behaviour the gateway must honour. |
| `EventPublisher` | Real event bus (`FormChangedEvent`, `EmployeeRecordCreatedEvent`, etc.) | Topic/queue names and downstream consumers so events aren't just logged in memory. |
| `DocumentStore` | Real document storage for onboarding artifacts | Storage account/container and retention policy. |
| `EmployeeNumberGenerator` | Real numbering scheme | The answer to Q-21. |
| `HolidayCalendar` | Real calendar source | Per-client or global calendar, and who maintains it. |
| `PersonDirectory` | Real directory for duplicate/rehire checks | The answer to Q-23, and which system is authoritative (HRIS vs. a separate people directory). |
| Identity (`api/identity.py`) | Microsoft Entra ID | App registration, token validation library, and the real role claims to map onto `Role`. |

## 7. Acceptance status

| Criterion | Status | Proof |
| --- | --- | --- |
| T-01 (happy path, open case through RECORD_CREATED) | Passing on stubs | `evaluation/uc01/scenarios/t01_happy_path.json` |
| EC-19 (offer not accepted) | Passing on stubs | `evaluation/uc01/scenarios/ec19_offer_not_accepted.json` |
| EC-20 (hiring freeze) | Passing on stubs | `evaluation/uc01/scenarios/ec20_hiring_freeze.json` |
| EC-21 (declared minor rejected) | Passing on stubs | `evaluation/uc01/scenarios/ec21_minor_rejected.json` |
| EC-38 (repeat request links existing case) | Passing on stubs | `evaluation/uc01/scenarios/ec38_repeat_request.json` |
| EC-39 (two people, same name) | Passing on stubs | `evaluation/uc01/scenarios/ec39_two_people_same_name.json` |
| EC-41 (agency contractor variant) | Passing on stubs | `evaluation/uc01/scenarios/ec41_agency_contractor.json` |
| EC-42 (internal transfer variant) | Passing on stubs | `evaluation/uc01/scenarios/ec42_internal_transfer.json` |
| EC-43 (rehire variant) | Passing on stubs; case flow passes on stubs, record reactivation not built | `evaluation/uc01/scenarios/ec43_rehire.json` |
| EC-44 (equipment/documents on rehire) | Not built. Needs equipment and document data from UC-03/UC-04. | — |
| EC-11/35 (joining-date change via form version) | Passing on stubs | `evaluation/uc01/scenarios/ec11_ec35_joining_date_change.json` |
| GATEWAY-01 (idempotent retry, GR-14) | Passing on stubs | `evaluation/uc01/scenarios/gateway_failure_retry.json` |
| T-05 (HRIS record preparation) | Passing on stubs | `tests/test_hris_prepare.py`, `evaluation/uc01/scenarios/t01_happy_path.json` |
| AC-15 (field-for-field payload check against the prepared mapping) | Passing on stubs | `tests/test_hooks.py` (`PAYLOAD_MISMATCH` test), `hooks.py` `before_hris_write` |
| AC-02 (every approval and status change is in the audit trail) | Passing on stubs | `audit_actions_present` checks in `t01_happy_path.json` and the other scenarios, the `MISSING_CASE_OPENED_AUDIT` invariant, `tests/test_service.py` actor checks |
| GR-18 (salary/PII never leaves HR scopes) | Passing on stubs | The leak checks in `evaluation/uc01/scenarios/t01_happy_path.json` and `salary_change_no_leak.json`, `tests/test_api.py::test_no_response_body_in_full_flow_contains_salary_email_or_discrepancy_value` |
| AC-15 against the real HRIS field mapping | Needs real HRIS | `hris_mapping.py` is only validated against the synthetic mapping; needs the real Zoho/Workday/ADP schema from section 4. |
