---
name: recommend_role_standard_items
use_case: UC-04
version: 1.0.0
type: advisory_skill
description: "Recommends the role's standard equipment and access package, from the case's own verified client and role configuration, for HR to review when nothing has been requested."
inputs:
  client_id:
    type: string
    required: true
    description: "The case's client (must equal the configuration's client_id)"
  role_id:
    type: string
    required: true
    description: "The case's role code, e.g. software_engineer or power_bi_developer (must equal the configuration's role_id)"
  role_equipment_matrix:
    type: config_block
    required: true
    description: "The role's equipment list from the case configuration (VERIFIED, VERIFIED_EMPTY or MISSING)"
  role_access_matrix:
    type: config_block
    required: true
    description: "The role's access list from the case configuration (VERIFIED, VERIFIED_EMPTY or MISSING)"
outputs:
  equipment:
    type: list
    description: "Recommended equipment item ids: the role's equipment list, or none when that list is missing"
  access:
    type: list
    description: "Recommended access item ids: the role's access list, or none when that list is missing"
  gaps:
    type: list
    description: "Why a kind has no recommendation (a configuration gap code)"
when_to_use:
  - "When the case has the blocker `NO_ITEMS_REQUESTED`: nothing has been requested yet and HR must say what the joiner needs."
  - "When HR asks which equipment and access are standard for a role at a client."
constraints:
  autonomy: "L1"
  write_access: false
  execution: "read_only"
  requests_items: false
  approves: false
knowledge_dependencies:
  - "brain/onboarding/uc04_equipment_access.md#client-and-role-eligibility-rules"
failure_behavior:
  on_scope_mismatch: "No recommendation at all: report `CONFIG_SCOPE_MISMATCH` (the configuration belongs to another client or role)."
  on_missing_matrix: "No recommendation for that kind: report `CONFIG_MISSING:role_equipment_matrix` or `CONFIG_MISSING:role_access_matrix`. Never an empty list or a guess."
  on_empty_matrix: "Recommend nothing for that kind: the client configured none for the role."
---

# Skill: recommend_role_standard_items

## Purpose
When `NO_ITEMS_REQUESTED` is raised, HR has to say which equipment and access the joiner needs (DL-17). The answer for
an ordinary joiner is the role's standard package, which is already in the client's configuration for that role. This
skill reads that package and hands it to HR as a recommendation, so HR reviews and confirms it instead of typing item
ids by hand.

## Method
1. Check scope: the configuration's `client_id` and `role_id` must equal the case's. Otherwise recommend nothing
   (`CONFIG_SCOPE_MISMATCH`).
2. Read `role_equipment_matrix` and `role_access_matrix` from the case configuration:
   - VERIFIED: recommend its items, in order, without duplicates;
   - VERIFIED_EMPTY: recommend nothing for that kind;
   - MISSING: no recommendation for that kind (`CONFIG_MISSING:role_equipment_matrix` / `CONFIG_MISSING:role_access_matrix`).
3. Return the recommendation with the configuration version it came from.

The recommendation is specific to the role because the configuration is: each client and role has its own file, so
`software_engineer` and `power_bi_developer` each get their own lists. This skill holds no item lists of its own.

## Limits
- Advisory only. It never records a request, approves an item or opens a ticket. HR records the request (the
  orchestrator's "Record requests" action), so the requester is always the HR user.
- Every recommended item comes from the role's matrix, so each still needs its standard approval
  (`hiring_manager` for client_alpha). An item HR adds outside the matrix is an exception (`exception_required=True`)
  and needs exception approval, as always.
- Implemented in `src/sub_agent/role_packages.py` (`recommend_standard_items`).
