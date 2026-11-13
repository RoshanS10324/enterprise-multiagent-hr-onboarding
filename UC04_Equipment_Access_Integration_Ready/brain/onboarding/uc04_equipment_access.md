# UC-04 Equipment & Approved Access Management — Brain Knowledge

## 1. Overview and Purpose
UC-04 is a deterministic sub-agent capability inside the Enterprise HR Agent Employee Onboarding Agent.
It governs equipment (laptops, monitors, headsets) and system access (email and Teams, source control, CI pipeline, VPN) for accepted joiners.
The capability verifies requests against approved client and role catalogues, tracks approvals and simulated IT service-desk tickets, and reports overall case readiness, risks, responsible owners, and next actions.

> **Source**: `specs/UC04_SPEC.md`, `specs/TRACEABILITY.md`, `README.md`

## 2. Operational Boundaries & Separation of Responsibilities
- **Local Simulation**: All IT tickets, delivery updates, and case records are synthetic (`SYN-...`). No real network calls, emails, or LLM tokens are expended by the core engine.
- **Deterministic Authority**: The deterministic business logic in `Uc04Service` is authoritative. Any Fake LLM, prompt, or advisory layer is strictly read-only and advisory.
- **Advisory Non-Authority**: The advisory layer cannot grant approvals, transition case/item states, create IT tickets, or bypass guardrails.
- **Never Creates Onboarding Cases**: UC-04 operates strictly within an existing parent onboarding case (`case_id`). It never originates a new onboarding case.

## 3. Case Lifecycle and Allowed Case States
A UC-04 case evaluates to exactly one of seven deterministic states:
1. `BLOCKED`: The start gate failed (`START_GATE_OFFER_NOT_ACCEPTED`, `START_GATE_FORM_NOT_APPROVED`, `CASE_ON_HOLD`) or essential configuration is missing (`CONFIG_MISSING:...`).
2. `AWAITING_APPROVAL`: One or more items are awaiting required standard or exception approvals (`APPROVAL_PENDING`, `EXCEPTION_PENDING`).
3. `IN_PROGRESS`: Approvals are recorded (`APPROVAL_RECEIVED`, `EXCEPTION_RECEIVED`), and items are eligible for submission or progressing through simulated IT fulfillment.
4. `AT_RISK`: An active risk was detected (e.g. `EQUIPMENT_DELAYED_BEYOND_JOINING`, `CLIENT_SITE_CLEARANCE_LATE`, `APPROVAL_STALLED`).
5. `READY`: Every required access and equipment item has authoritative completion evidence on file (`ITEM_COMPLETED`), and all site-specific rules are satisfied.
6. `NEEDS_HUMAN_REVIEW`: The IT service desk rejected a ticket (`REJECTED_CORRECTION_REQUIRED`) or internal transfer review is needed (`INTERNAL_TRANSFER_REVIEW`), or an approver rejected the request, evidence did not come from an allowed role, no approver or delegate is available, equipment is faulty or wrong, an adapter call failed, or an illegal state change was refused.
7. `CANCELLED`: The parent onboarding case was cancelled or withdrawn (`WITHDRAWN`). All pending items are cancelled.

## 4. Item Lifecycle and Allowed Item States
Each requested item follows a strict state progression:
- `DRAFT_PREPARED`: Initial item state upon parsing the request.
- `APPROVAL_PENDING`: Standard approval required from the configured approver (or delegate).
- `EXCEPTION_PENDING`: Request is out-of-matrix; requires elevated exception approver signoff.
- `APPROVAL_RECEIVED`: Required standard approval has been validated and recorded.
- `EXCEPTION_RECEIVED`: Required exception approval has been validated and recorded.
- `ELIGIBLE_FOR_SIMULATED_SUBMISSION`: All approval gates passed; eligible for dispatch to mock IT desk.
- `MOCK_TICKET_CREATED`: Dispatched to mock IT service desk; assigned a synthetic ticket ID.
- `IT_IN_PROGRESS`: IT desk is provisioning the access or fulfilling the hardware. A ticket that is complete without accepted evidence also stays here: a completed ticket is not proof of delivery or activation.
- `REJECTED_CORRECTION_REQUIRED`: IT desk rejected the request (e.g. model discontinued or invalid spec).
- `PROVISIONING_CONFIRMED`: Access granted, account created, or license assigned with valid evidence.
- `EQUIPMENT_DELIVERED`: Delivery evidence was accepted (status DELIVERED, a stored reference, from an allowed role).
- `EQUIPMENT_COLLECTED`: Collection evidence was accepted. This applies only when the client's handover rule is COLLECT.
- `CLIENT_SITE_CLEARANCE_CONFIRMED`: The client-site clearance was confirmed with authoritative evidence from an allowed role.
- `ITEM_COMPLETED`: Authoritative completion evidence verified for this item.
- `BLOCKED`: Item blocked by missing or invalid configuration, a rejected approval, prior equipment still held, or a ticket without a valid approval.
- `CANCELLED`: Cancelled due to case withdrawal or superseding request.

## 5. Client and Role Eligibility Rules
- **Access Matrix**: For a client and role, the list of access item codes that are allowed (synthetic: `software_engineer` for client_alpha and client_bravo).
- **Equipment Matrix**: For a client and role, the list of equipment item codes that are allowed. Quantities are not modelled. The work mode (`OFFICE`, `REMOTE`, `OVERSEAS`) does not change the list; remote or overseas equipment needs verified country rules and adds courier days to the lead time.
- **Client Configurations**: Clients (e.g. `client_alpha`, `client_bravo`) define specific rules:
  - Approvers per request class: client_alpha uses `hiring_manager` (delegate `hiring_manager_delegate`) and client_bravo uses `line_manager`; both use `exception_approver` for exceptions. The source documents do not say who approves exceptions (an open question).
  - Client-site rules: `client_site_rules` list the required client-site items, whether a late clearance blocks, and the owner role; the clearance lead time is part of the lead times.
  - Working day calendar: weekend days and public holidays.
  - Handover rules: mode (`COLLECT`, `DELIVER`, `READY_AT_DESK`) and missed slot retention.
- **Out-of-Matrix Exceptions**: Any item requested outside the role's approved matrix must be flagged as an exception (`exception_required=True`) and routed for explicit exception approval.

## 6. Approvals and Routing
- Standard items route to the designated role approver (default: `hiring_manager`).
- Exceptions route to the configured `exception_approver`.
- Approvals must match the exact `request_key` and item ID.
- Blanket approvals across unapproved items are rejected.
- When an approver is unavailable, requests route to their configured delegate.

## 7. IT Service Desk Integration (Mock)
- Mock tool: `it_service_desk.submit_request`.
- Only items in state `ELIGIBLE_FOR_SIMULATED_SUBMISSION` can be submitted.
- Submissions require policy approval (`write_requires_approval`).
- IT desk returns synthetic ticket records (`ticket_id`, `request_id`).

## 8. Evidence Requirements for Readiness
Authoritative completion evidence is required for every item to reach `READY`:
- **ACCESS**: Authoritative confirmation of account creation or license grant (`PROVISIONING_CONFIRMED`).
- **EQUIPMENT (Delivery)**: Delivery evidence with status DELIVERED, a stored evidence reference and an allowed asserting role (`EQUIPMENT_DELIVERED`). A FAULTY or WRONG_MODEL delivery is not assigned.
- **EQUIPMENT (Collection)**: Collection evidence, required only when the client's handover rule is COLLECT (`EQUIPMENT_COLLECTED`).
- **CLIENT-SITE CLEARANCE**: Clearance record with status `CONFIRMED` (`CLIENT_SITE_CLEARANCE_CONFIRMED`).

> **Rule**: An approval, a ticket, and delivery are never evidence of one another. An item is only completed when its own completion evidence exists. Evidence counts only with a stored reference, from a role configured for that evidence type, and after the ticket is complete.

## 9. Risk and Blocker Detection
- `EQUIPMENT_DELAYED_BEYOND_JOINING`: Delivery ETA is after the employee's joining date.
  - Mitigation Proposal: `LOAN_DEVICE` (requires `HR_OFFICER`).
  - Owner: `HR_OFFICER`.
  - Next Action: Flag to hiring manager and HR. Draft employee message for HR approval.
- `CLIENT_SITE_CLEARANCE_LATE`: Client site clearance expected date is after joining date or lead time overdue.
  - Mitigation Proposal: `OFFICE_START_OR_LATER_DEPLOYMENT` (requires `HR_OFFICER`).
  - Owner: `ACCOUNT_MANAGER` (or configured client site owner).
  - Next Action: Flag at plan approval; HR and account manager decide office start or later deployment.
- `APPROVAL_STALLED`: Item approval is outstanding past the due date.
  - Owner: `HR_OFFICER`.
  - Next Action: Escalate to HR officer, then HR operations lead after one working day.
- `REQUEST_NOT_RAISED_BY_DUE_DATE`: The request was not raised in time for the configured lead time.
  - Owner: `HR_OFFICER`.
  - Next Action: Re-plan and decide a workaround with hiring manager.
- `JOINING_DATE_REACHED_INCOMPLETE`: Joining date is reached and this item is not complete.
  - Owner: `HR_OFFICER`.
  - Next Action: Escalate to HR now.
- `COLLECTION_SLOT_MISSED`: Employee missed the booked collection slot.
  - Owner: `HR_OFFICER`.
  - Next Action: Draft reminder to employee and manager; offer next slot.
- `JOINING_DATE_NON_WORKING_DAY`: Joining date falls on a weekend or public holiday.
  - Mitigation Proposal: `NEXT_WORKING_DAY_SUGGESTION` (requires `HR_OFFICER`).
  - Owner: `HR_OFFICER`.
  - Next Action: Flag to HR. Joining date is never changed automatically.

## 10. Privacy & Security Guardrails
- **Fail-Closed Privacy**: Scans for 10 sensitive categories (compensation, banking, medical, criminal records, currency amounts, emails, phone numbers, national IDs, passports, API secrets).
- **Redaction**: Disallowed personal content is redacted to `[REDACTED: HR item, see HR]`.
- **Untrusted Input Protection**: Free-text fields (such as item notes) are untrusted and never executed as instructions; instruction-like text is treated as data: it is flagged in the audit by label (`untrusted_text.flagged`), never acted on and never echoed, and the case carries on.
- **Idempotency**: The same idempotency key with the same payload returns the stored result while the case state and the parent status are unchanged; a different payload under the same key is a conflict. Duplicate request keys and items are linked, and the mock desk creates one ticket per request.
