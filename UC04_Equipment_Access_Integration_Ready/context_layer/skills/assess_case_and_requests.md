---
name: assess_case_and_requests
use_case: UC-04
version: 1.0.0
type: advisory_skill
description: "Assesses overall case status, start gate conditions, and item eligibility without altering lifecycle states or business logic."
inputs:
  case_id:
    type: string
    required: true
    description: "Synthetic case identifier (e.g., SYN-CASE-001)"
  offer_accepted:
    type: boolean
    required: true
    description: "Start gate: must be True to proceed"
  items:
    type: list
    required: true
    description: "List of requested equipment and access items"
outputs:
  status_assessment:
    type: string
    description: "Evaluated case status (BLOCKED, AWAITING_APPROVAL, IN_PROGRESS, AT_RISK, READY, NEEDS_HUMAN_REVIEW, CANCELLED)"
  eligibility_summary:
    type: string
    description: "Summary of items eligible for simulated submission or pending review"
  action_summary:
    type: string
    description: "Recommended next action according to deterministic service rules"
when_to_use:
  - "When a case is received or re-evaluated to understand its current state."
  - "When explaining why a case is blocked at the start gate."
constraints:
  autonomy: "L1 (observe and propose only)"
  write_access: false
  execution: "read_only"
knowledge_dependencies:
  - "brain/onboarding/uc04_equipment_access.md#case-lifecycle"
  - "brain/onboarding/uc04_equipment_access.md#operational-boundaries"
failure_behavior:
  on_missing_input: "Return safe fallback assessment indicating missing required fields."
  on_blocked: "Explain deterministic blocker reason (e.g. START_GATE_OFFER_NOT_ACCEPTED)."
---

# Skill: assess_case_and_requests

## Purpose
Explains the current deterministic evaluation of a case and its requested items without duplicating or overriding the underlying `Uc04Service` business rules.

## Method
1. Inspect the `overall_status` and `readiness_reason` computed by `Uc04Service`.
2. Map the active state against the canonical case lifecycle in brain knowledge.
3. Formulate a plain-English explanation of why the case is in its current state.
4. Clearly state whether the start gate (offer acceptance) has been satisfied.
