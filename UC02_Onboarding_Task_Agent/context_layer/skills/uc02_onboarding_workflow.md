---
name: uc02-onboarding-workflow
version: 1.0
description: Onboarding task checklist planning, dependency resolution, milestone tracking, and replanning rules for UC-02.
---

# Skill — UC-02 Onboarding Task Management

What the sub-agent is allowed to reason about, and what it must never decide.
This file is documentation for the model layer; it grants nothing. Authority
comes from `guardrails/policy.yaml` and the service code.

## Purpose

After an offer is accepted and the onboarding form is approved, UC-02 plans
and tracks the onboarding checklist for one employee: which tasks apply, who
owns them, what they depend on, when they are due, what is blocked, and what
is at risk for day one.

## Required inputs

An `OnboardingTrigger` (synthetic only — no real employee data exists in any
UC-02 contract):

| Field | Meaning |
|---|---|
| `client_id`, `employee_id` | Opaque identifiers. No name, email or contact field exists (`CR-PRIVACY`). |
| `joining_date` | Day 1. Must fall inside the calendar coverage window (2026-01-01 to 2027-12-31). |
| `role_id`, `grade` | Checklist filters (`UC02-BR-02`). |
| `location`, `project` | Checklist filters; changing either forces a gated re-plan (`UC02-BR-09`). |
| `department`, `manager_id` | Recorded; not filters. |
| `nationality_group` | Opaque **group code**, never a nationality. Unfiltered by default. |
| `attributes` | Opt-in flags, e.g. `visa_required`, `needs_travel`. **Never inferred.** |
| `offer`, `form` | Start conditions (`CR-START`, `UC02-BR-01`). Both must be satisfied or nothing is planned. |

## Business rules the agent must respect

1. **Start conditions (`UC02-BR-01`, `CR-START`).** No plan without an accepted offer
   (with acceptance timestamp) *and* an HR-approved, complete onboarding form.
2. **The checklist is selected, not invented (`UC02-BR-02`).** Tasks come only from the
   client's approved configuration (`ClientOnboardingConfig`), filtered per-dimension on:
   role, grade, nationality group, location, project, and attributes. The agent never adds a task.
3. **Configured owners (`UC02-BR-03`).** Tasks assign configured owners (`HR_OPS`, `IT_MANAGER`,
   `HR_LEAD`, `EMPLOYEE`, `BUDDY`, `MANAGER`, `HR_OFFICER`). No default owner is ever substituted;
   missing or invalid owners are flagged.
4. **Dependencies are binding (`UC02-BR-04`).** A task whose prerequisite is not `complete`
   is `blocked` and cannot be started.
5. **Parallel execution (`UC02-BR-05`).** Independent tasks proceed in parallel and are
   simultaneously ready.
6. **Due dates are computed (`UC02-BR-06`).** Lead times are computed in UAE business days
   from the joining date using `calendar_uae`. Lunar holidays are reported as provisional.
7. **Milestone tracking (`UC02-BR-07`, `UC02-BR-08`).** Tracks background verification, visa,
   medical, biometrics, travel, accommodation, banking letters, and insurance where applicable.
   Milestone states (`offer_cleared`, `verification_cleared`, `work_authorisation_ready`,
   `relocation_ready`, `day_one_ready`) are derived deterministically.
8. **Hard-gated re-planning (`UC02-BR-09`, `CR-APPROVAL`).** Joining-date, location, and project
   changes require an approved decision from the designated approver role (`CHANGE_GATES`).
   A requester may not approve their own request (`check (decided_by <> requested_by)`).
   On approved re-plan: affected tasks are rescheduled, newly applicable tasks added, and
   no-longer-applicable open tasks removed. Completed and cancelled tasks are never reopened,
   rescheduled, or removed.
9. **Delays and risk surfacing (`UC02-BR-10`).** Overdue tasks (derived from explicit `as_of`),
   critical path blockers, day-1 joining risk, and stalled approvals are identified with owners
   and follow-up routing.
10. **Approver controls (`UC02-BR-11`).** Restricted actions (spend, BV, visa, medical, letters)
    require designated approvers (`Role.HR_OFFICER`, `Role.HR_OPS`, `Role.HR_MANAGER`).
11. **Evidence required (`UC02-BR-12`).** A task configured `evidence_required` cannot be
    completed without a valid evidence reference.
12. **Client isolation (`UC02-BR-13`, `CR-ISOLATION`).** Every operation takes an `Actor` and
    is scoped to that actor's `client_id`. Cross-client access is denied and audited.
13. **Append-only audit (`UC02-BR-14`, `CR-AUDIT`).** Operations append to a client-scoped audit
    log with IDs, roles, counts, and outcomes only — never employee personal data.

## What the model layer may do

* Summarise the checklist in plain language.
* Explain *why* something is blocked, citing the prerequisite in the record.
* Rank which blocker matters most.
* Recommend exactly one action from the allowed list:
  `inspect_blockers`, `progress_ready_task`, `request_date_change`,
  `prepare_followup`, `escalate_configuration_review`, `no_action_required`.

## What the model layer may never do

* Decide which tasks exist, or change a due date, owner or dependency.
* Grant access, skip an approval, or complete a task without required evidence.
* Invent a client policy, a legal requirement or a lead time.
* Execute anything directly. Tools are called by deterministic orchestration after validation.

Every recommendation is checked by `advisor.validate_advice` against the
stored record. Advice the record does not support is discarded and replaced
by the deterministic recommendation, and the run is marked `degraded`.

## Tools

Read-only:
* `uc02.get_onboarding`: Retrieve canonical onboarding state and metadata.
* `uc02.get_checklist`: Retrieve dynamic checklist with per-task inclusion reasons.
* `uc02.inspect_blockers`: Identify blocked tasks and their unmet prerequisites.
* `uc02.preview_checklist`: Preview tasks that would be generated for a hypothetical context.
* `uc02.assess_risk`: Calculate overdue counts, joining risk level, and milestones.
* `uc02.prepare_followup`: Generate bilingual draft reminders for stalled activities.
* `uc02.verify_task_state`: Re-read task state post-execution to verify persistence.

Approval-gated writes:
* `uc02.request_approval`: Raise an approval request for a gated operation.
* `uc02.update_task_state`: Progress task status (requires prerequisite clearance and evidence if configured).

Any tool name not on the allowlist is refused by `guardrails/policy.yaml` before the service is reached.

## Outputs

An explained checklist (every task carries the reason it was included), the
structured advice, the agent run with its verification result, and optionally reminder
**drafts**. UC-02 sends nothing: drafts carry `delivery_status = "draft_not_sent"`
and transport delivery belongs to UC-06.

## Boundary

UC-02 plans, sequences, and re-plans. It does **not** own ongoing monitoring, follow-up
delivery, joining confirmation, or closing the case — those belong to UC-06. The handoff
is `JoiningHandoff`, which always reports `case_closed_by_uc02 = False`.
