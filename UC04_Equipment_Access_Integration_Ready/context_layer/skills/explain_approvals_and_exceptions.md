---
name: explain_approvals_and_exceptions
use_case: UC-04
version: 1.0.0
type: advisory_skill
description: "Describes pending or required approvals, designated approver roles, and exception pathways using verified service results."
inputs:
  approvals_needed:
    type: list
    required: true
    description: "List of approval requirement objects from Uc04Result"
  items:
    type: list
    required: true
    description: "List of evaluated item records"
outputs:
  pending_approvals:
    type: list
    description: "Itemized list of items awaiting signoff with designated approver roles"
  exception_items:
    type: list
    description: "Items flagged as exceptions requiring elevated approval"
  approver_instructions:
    type: string
    description: "Clear routing instructions for HR and managers"
when_to_use:
  - "When overall_status is AWAITING_APPROVAL."
  - "When explaining why a request cannot yet be dispatched to IT."
constraints:
  autonomy: "L1"
  write_access: false
  execution: "read_only"
knowledge_dependencies:
  - "brain/onboarding/uc04_equipment_access.md#approvals-and-routing"
failure_behavior:
  on_unapproved: "List missing approvals explicitly without fabricating signoffs."
---

# Skill: explain_approvals_and_exceptions

## Purpose
Translates technical approval requirements and exception flags into clear, actionable routing instructions for the hiring manager and HR operations.

## Method
1. Inspect `approvals_needed` from the deterministic `Uc04Result`.
2. Determine whether each item requires standard approval (e.g. `hiring_manager`) or exception approval (e.g. `client_sponsor`).
3. Explain which specific items are holding up the workflow.
4. Highlight that approval records must be formally submitted through the API/service before any dispatch can occur.
