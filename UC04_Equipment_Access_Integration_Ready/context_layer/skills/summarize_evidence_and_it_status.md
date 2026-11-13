---
name: summarize_evidence_and_it_status
use_case: UC-04
version: 1.0.0
type: advisory_skill
description: "Summarizes validated completion evidence, mock IT ticket progress, and physical delivery/collection milestones."
inputs:
  it_tickets:
    type: list
    required: true
    description: "List of simulated IT service desk ticket records"
  items:
    type: list
    required: true
    description: "List of item evaluation records"
outputs:
  ticket_status_summary:
    type: string
    description: "Status overview of IT tickets (CREATED, IN_PROGRESS, COMPLETED, REJECTED)"
  evidence_status_summary:
    type: string
    description: "Itemized verification of provisioning, delivery, and collection evidence"
  it_rejections:
    type: list
    description: "Any rejected tickets requiring specification correction"
when_to_use:
  - "When overall_status is IN_PROGRESS or NEEDS_HUMAN_REVIEW."
  - "When tracking equipment delivery or software account provisioning."
constraints:
  autonomy: "L1"
  write_access: false
  execution: "read_only"
knowledge_dependencies:
  - "brain/onboarding/uc04_equipment_access.md#it-mock-support"
  - "brain/onboarding/uc04_equipment_access.md#evidence-requirements-for-readiness"
failure_behavior:
  on_rejection: "Highlight IT rejection reasons and direct HR to correct the specification."
---

# Skill: summarize_evidence_and_it_status

## Purpose
Provides a consolidated view of IT service desk ticket progress and authoritative completion evidence across all hardware and software items.

## Method
1. Inspect `it_tickets` to verify ticket creation and fulfillment milestones.
2. Verify authoritative completion evidence:
   - Access: account provisioning confirmation.
   - Hardware: delivery tracking receipt or employee collection signature.
3. Flag any tickets in state `REJECTED` and report the recorded rejection reason.
4. Confirm whether any item has only partial evidence (e.g., ticket closed but hardware not yet signed for).
