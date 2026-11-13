---
name: retrieve_access_equipment_requirements
use_case: UC-04
version: 1.0.0
type: advisory_skill
description: "Retrieves applicable access and equipment requirements from approved catalogues and client configurations."
inputs:
  role_id:
    type: string
    required: true
    description: "Employee role key (e.g., software_engineer, data_engineer)"
  client_id:
    type: string
    required: false
    description: "Client organization identifier (e.g., client_alpha, client_bravo)"
  delivery_mode:
    type: string
    required: false
    description: "Work mode: OFFICE, REMOTE, or OVERSEAS"
outputs:
  standard_access:
    type: list
    description: "Standard access packages approved for this role"
  standard_equipment:
    type: list
    description: "Standard equipment items approved for this role and delivery mode"
  client_site_requirements:
    type: list
    description: "Client-specific site access, clearance or badge requirements"
when_to_use:
  - "When checking whether requested items conform to the role matrix."
  - "When identifying out-of-matrix items that will require exception approval."
  - "When evaluating case in BLOCKED status due to configuration gaps or missing matrix."
  - "When evaluating case in AWAITING_APPROVAL status with out-of-matrix items."
constraints:
  autonomy: "L1"
  write_access: false
  execution: "read_only"
knowledge_dependencies:
  - "brain/onboarding/uc04_equipment_access.md#client-and-role-eligibility-rules"
failure_behavior:
  on_missing_config: "Flag missing role access matrix or configuration gap."
---

# Skill: retrieve_access_equipment_requirements

## Purpose
Retrieves role-based access entitlements and standard equipment packages from the active configuration matrix, referencing approved client rules.

## Method
1. Consult the verified role access matrix in `data/` or case configuration.
2. Cross-reference requested items against the role's allowed item IDs.
3. Identify items present in the request that are missing from the role matrix.
4. Output the standard catalogue and flag any out-of-matrix items.
