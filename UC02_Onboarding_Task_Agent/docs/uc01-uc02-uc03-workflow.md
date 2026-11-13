# UC-01 → UC-02 → UC-03 — End-to-End Onboarding Workflow

**Verified against branch `logesh` @ `1c073df` on 2026-10-22.** Every UC-02 value below was produced by
executing the actual code, not copied from a summary. Fictional test data throughout.

Diagram sources: [`uc01-uc02-uc03-workflow.mmd`](uc01-uc02-uc03-workflow.mmd) (flowchart) ·
[`uc01-uc02-uc03-sequence.mmd`](uc01-uc02-uc03-sequence.mmd) (sequence).

---

## 0. Implementation reality — read this first

| Component | Status | Evidence |
|---|---|---|
| **UC-01 Employee Record Creation** | 🔴 **Does not exist** | No code, spec or document on any of the 4 branches, or in the documents folder |
| **Trigger 1 — UC-01 → UC-02** | 🔴 **PROPOSED — NOT YET INTEGRATED** | UC-02 has no API, webhook, queue or event listener |
| **UC-02 Onboarding Task Management** | 🟢 **Implemented** | 14 modules, 288 tests passing |
| **Trigger 2 — UC-02 → UC-03** | 🔴 **PROPOSED — NOT YET INTEGRATED** | UC-02 makes no outbound calls; no HTTP client anywhere in `src/` |
| **UC-03 Document Validation** | 🟡 **Implemented on another branch** | `origin/Roshan`: `src/api/uc03.py`, real FastAPI router — **not present on `logesh`** |
| **Trigger 3 — UC-03 → UC-02** | 🔴 **PROPOSED — NOT YET INTEGRATED** | No callback, no inbound endpoint |
| **Shared persistence** | 🔴 **None** | Both agents hold state in memory; no database |

> **In short:** the only implemented link in this chain is *inside* UC-02. All three
> inter-agent connections are proposed. The diagram shows the intended design and marks
> every unimplemented hop explicitly.

---

## 1. Sample employee (fictional)

| Field | Value | In UC-02's contract? |
|---|---|---|
| Employee name | Arun Kumar | ❌ **No — excluded by design (PII)** |
| Employee ID | EMP-001 | ✅ `employee_id` |
| Client ID | CLIENT-ALPHA | ✅ `client_id` |
| Role | Backend Engineer | ✅ `role_id = backend_engineer` |
| Department | Technology | ✅ `department` |
| Manager ID | MGR-CLIENT-ALPHA | ✅ `manager_id` |
| Joining date | 16 Nov 2026 | ✅ `joining_date` |
| Location | Dubai | ✅ `location` |
| Project | Apollo | ✅ `project = apollo` |
| Offer accepted | Yes | ✅ `offer.accepted` |
| Form submitted + HR approved | Yes | ✅ `form.submitted`, `form.approved` |

UC-02 deliberately carries **no name, grade or nationality**. Its audit log actively rejects
keys named `name`, `email`, `nationality`, `passport`.

---

## 2. Stage-by-stage

### Stage A — Offer acceptance · `PROPOSED`
The source/ATS system records acceptance: `employee_id=EMP-001`, `offer_reference=OFR-EMP-001`,
`accepted=true`, `accepted_at=2026-10-01T09:00:00Z`. No such system exists in this repository;
UC-02 receives these values as part of its input payload.

### Stage B — UC-01 processing · `PROPOSED — NO CODE EXISTS`
Four checks, then four branches:

| Check | Fail path |
|---|---|
| Offer accepted? | Remain **PENDING** |
| Form submitted **and** approved? | Remain **PENDING** |
| Duplicate ACTIVE case? | **Duplicate-handling rule — not documented anywhere; marked proposed** |
| Details consistent with offer/form? | Route to **HR review** |

Success → create `CASE-EMP-001`. All of this is proposed: I found no UC-01 implementation or
specification to describe.

### Stage C — Trigger 1 · `PROPOSED — NOT YET INTEGRATED`
There is no event name, topic or endpoint to cite. Today UC-02's only entry point is an
in-process Python call:

```python
service.create_onboarding(actor, trigger)   # actor: Actor, trigger: OnboardingTrigger
```

The mapping `CASE-EMP-001 → OnboardingTrigger` must **drop** name, grade and nationality.

### Stage D — UC-02 task generation · `IMPLEMENTED`

1. **Validate payload** — offer accepted (with timestamp), form submitted, complete, approved by an HR role → else `PreconditionError`.
2. **Load client configuration** → else `MissingConfigurationError`. Tasks are never invented.
3. **Select templates** by `location=Dubai`, `project=apollo`, `role_id=backend_engineer`.
4. **Generate checklist, assign owners, compute due dates** on the UAE business calendar.
5. **Apply dependencies**, compute **parallel readiness**, track status/risk/milestones.

**Real generated plan** — `ONB-CLIENT-ALPHA-EMP-001`, joining 2026-11-16:

| Task | Owner | Category | Due | Initial status | Depends on |
|---|---|---|---|---|---|
| `hr_record_create` | hr_ops | hr_record | 2026-11-02 | **READY** | — |
| `collect_documents` | hr_ops | document_collection | 2026-11-04 | BLOCKED | hr_record_create |
| `visa_application` | hr_ops | document_collection | 2026-11-05 | BLOCKED | collect_documents |
| `it_laptop` | it_manager | it_provisioning | 2026-11-09 | BLOCKED | hr_record_create |
| `payroll_setup` | finance | payroll | 2026-11-10 | BLOCKED | collect_documents |
| `it_accounts` | it_manager | it_provisioning | 2026-11-11 | BLOCKED | it_laptop |
| `access_apollo` | it_manager | access_management | 2026-11-12 | BLOCKED | it_accounts |
| `desk_allocation` | facilities | workplace | 2026-11-12 | BLOCKED | hr_record_create |
| `orientation_schedule` | hr_ops | orientation | 2026-11-16 | BLOCKED | hr_record_create |
| `policy_ack` | employee | policy_acknowledgement | 2026-11-18 | BLOCKED | orientation_schedule |

**Parallel execution layers:**
1. `hr_record_create`
2. `collect_documents`, `it_laptop`, `desk_allocation`, `orientation_schedule`
3. `visa_application`, `it_accounts`, `payroll_setup`, `policy_ack`
4. `access_apollo`

**Milestones** (all `pending` as at 2026-10-22): `paperwork_complete` (2026-11-05) ·
`workplace_ready` (2026-11-12) · `day_one_ready` (2026-11-13) · `onboarding_complete` (2026-11-18).

> The "Create email and project access" step in the brief maps to two real tasks here:
> `it_accounts` and `access_apollo`.

### Stage E — Trigger 2 · `PROPOSED — NOT YET INTEGRATED`
`collect_documents` cannot start until `hr_record_create` is COMPLETE — **this prerequisite is
enforced today**; attempting otherwise raises `PrerequisiteNotMetError`. Only once it is READY
would UC-02 call UC-03's real endpoint:

```
POST /onboarding/uc03/validate      → DocumentValidationResult
```

UC-03's `DocumentValidationRequest` supports `correlation_id`, and validates **one document per
call** — so UC-02's single `collect_documents` task would fan out into several requests.

### Stage F — UC-03 document validation · `IMPLEMENTED on origin/Roshan`
UC-03 first re-checks the same start conditions (offer accepted, form approved) and goes
`ON_HOLD` if unmet. Fictional results for this example:

| Document | `DocumentStatus` |
|---|---|
| Identity document | ACCEPTED |
| Education certificate | ACCEPTED |
| Signed offer | ACCEPTED |
| **Experience letter** | **MISSING** |

Supported outcomes: `REQUESTED / RECEIVED / MISSING / UNDER_REVIEW / REJECTED / ACCEPTED`, plus
`HR_REVIEW_REQUIRED` where human judgement is needed. **These are fictional illustrative results,
not production output.**

### Stage G — Trigger 3 · `PROPOSED — NOT YET INTEGRATED`
UC-03 returns the result with `outstanding_requirements = [experience_letter]`.

**Important accuracy note:** the brief asks to show UC-02 setting the task to *action-required*
or *pending*. **UC-02 has neither status.** Its only task statuses are
`blocked / ready / in_progress / complete / cancelled`. The faithful behaviour is:

- `collect_documents` is **not completed** — it stays `READY`/`IN_PROGRESS`.
- `visa_application` and `payroll_setup` stay **BLOCKED**, because their prerequisite is unmet.
- A reminder **draft** may be produced (`delivery_status = "draft_not_sent"`); nothing is sent.

Once HR supplies the missing letter, re-validation returns ACCEPTED and `collect_documents` can be
completed, releasing `visa_application` and `payroll_setup`. **No retry/re-validation mechanism
exists — that loop is proposed.**

### Stage H — Final outcome

- ✅ **Progress** — every required task COMPLETE and `day_one_ready` achieved → handover to UC-06.
- ⏸️ **Not onboarded** — any required document or approval missing → tasks remain pending/blocked
  or await HR action. The employee is **not** onboarded until the completion criteria are met.

---

## 3. Data-contract mismatches

| # | Mismatch | Impact |
|---|---|---|
| 1 | **`employee_name_form` is REQUIRED by UC-03** but does not exist in any UC-02 model | 🔴 **Blocker.** UC-02 cannot call UC-03 without either adding PII or changing UC-03's contract |
| 2 | UC-03 also expects `nationality`, `grade`, `role`; UC-02 has only `role_id` | 🟠 Degraded client-rule evaluation in UC-03 |
| 3 | Identifier naming: UC-02 `onboarding_id` vs UC-03/UC-01 `case_id` | 🟠 Mapping layer needed |
| 4 | `OnboardingStatus` exists in **both** with **incompatible values** — UC-02 `active/complete/cancelled`; UC-03 `INCOMPLETE/COMPLETE/ON_HOLD/ACTION_REQUIRED/...` | 🔴 Cannot share one enum or column |
| 5 | UC-03 validates **one document per call**; UC-02 models document collection as **one task** | 🟠 Fan-out plus result aggregation required |
| 6 | UC-02 has no `action_required`/`pending` task status | 🟠 UC-03 outcomes cannot be represented 1:1 |
| 7 | No shared persistence or correlation store | 🔴 Results cannot be durably tied back to a case |
| 8 | UC-02 is a library; UC-03 is a FastAPI service **on another branch** | 🔴 No runtime in which both exist today |

---

## 4. Legend

| Symbol / colour | Meaning |
|---|---|
| 🔵 Blue subgraph | UC-01 (proposed) |
| 🟡 Amber subgraph, dashed | Trigger / integration layer — **all proposed** |
| 🟣 Purple subgraph | UC-02 — implemented in this repository |
| 🟢 Teal subgraph | UC-03 — implemented on `origin/Roshan` |
| 🟠 Orange node | Pending / human review |
| 🔴 Red node | Error, blocked, or contract blocker |
| 🟩 Green node | Completed / ready |
| ◇ Diamond | Decision point |
| Dashed border | Not integrated / no code in this repository |
| `[IMPL]` | Implemented and test-covered here |
| `[IMPL-OB]` | Implemented on another branch, not integrated |
| `[PROPOSED]` | No implementation exists |

---

## 5. How to view the diagrams

**VS Code** — install *Markdown Preview Mermaid Support* (`bierner.markdown-mermaid`) and open
this `.md` file with `Ctrl+Shift+V`. For the `.mmd` files directly, install *Mermaid Editor*
(`tomoyukim.vscode-mermaid-editor`) or *Mermaid Preview* (`vstirbu.vscode-mermaid-preview`), then
open the file and use the preview button.

**GitHub** — `.mmd` files are not auto-rendered, but fenced ```mermaid blocks in Markdown are.
To render on GitHub, paste a `.mmd` file's contents into a fenced ```mermaid block.

**Browser, no install** — paste the `.mmd` contents into <https://mermaid.live>.

---

*Fictional test data only. No production system was accessed. UC-03 behaviour was read from
`origin/Roshan` without modification; no branch was switched, and nothing was committed.*
