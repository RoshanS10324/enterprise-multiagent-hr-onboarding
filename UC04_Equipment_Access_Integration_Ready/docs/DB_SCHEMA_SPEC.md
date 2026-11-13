# UC-04 proposed database schema (field-level specification)

**Status: proposal for team review. Not approved. Not verified against the shared Supabase schema or any approved Enterprise HR Agent schema** (neither was available). This page contains **no SQL and no migration**; nothing here connects to or changes a database.
Prepared 2026-10-27 by checking every proposed field against the UC-04 code (`src/sub_agent/contracts.py`, `states.py`, `store.py`, `audit.py`, `evidence.py`) and the Business Framework.

The persistence choice itself is open: the Enterprise HR Agent deck names Azure Cosmos DB for NoSQL for case state and audit records, and Supabase (PostgreSQL) is proposed for the team's testing setup. This spec is written as relational tables; a document store would need a different design.

## 1. Rules used to decide what stays
1. A field stays only if the code stores or needs it, or a documented requirement does. Anything else was removed, not "kept just in case".
2. Values the code recomputes on every call (eligibility, approval state, provisioning state, delivery state, due-date arithmetic, next actions) are **not stored** as independent fields, so they cannot contradict their sources.
3. Per-case input (work mode, client-site flag, joining date) is not client configuration.
4. Free text that could carry personal or sensitive data is avoided; the audit uses a fixed field list on purpose (GR-06, GR-18).
5. Every status column is a `TEXT` with a CHECK constraint built from the existing enum, so the database cannot hold a state the code does not know.

Result: **132 proposed fields in 10 tables became 100 fields in 10 tables.** Access and equipment tables are merged (the code has one item model), and two tables the code needs were added (`uc04_evidence`, `uc04_idempotency`). About 75 proposed fields were removed or relocated to config, evidence or derived values.

## 2. Tables and fields

### 2.1 `uc04_cases` (10 fields): UC-04 state for an existing onboarding case
Replaces `store.CaseRecord` (fingerprint) and the headline of `Uc04Result`.
| Field | Type and constraint | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `onboarding_case_id` | UUID, FK to the parent case, UNIQUE | The parent owns the case; UC-04 never creates one (BR-24). The UNIQUE key prevents a second UC-04 record |
| `status` | TEXT, CHECK in the 7 `CaseStatus` values | BLOCKED, NEEDS_HUMAN_REVIEW, AWAITING_APPROVAL, IN_PROGRESS, AT_RISK, READY, CANCELLED |
| `readiness_reason` | TEXT | Sanitised sentence from the result |
| `config_version` | TEXT | Version of the configuration used (the code's versions are strings such as "alpha-v1") |
| `evaluated_snapshot` | JSONB with fixed keys | `client_id`, `role_id`, `work_location`, `work_mode`, `client_site_required`, `joining_date`, `form_version` at the last evaluation. Needed to detect a change (EC-11, EC-35, TC-22). Could be typed columns instead |
| `state_version` | INTEGER | Bumped on every state change; with the parent status it decides whether a stored replay is still valid |
| `last_evaluated_at` | TIMESTAMPTZ | |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

### 2.2 `uc04_client_configs` (9 fields, plus 2 recommended): configuration per client, role and version
Replaces `contracts.CaseConfig`.
| Field | Type and constraint | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `client_id` | UUID, FK to `clients` | |
| `role_code` | TEXT | The scope is client **and** role |
| `version` | TEXT | UNIQUE(`client_id`, `role_code`, `version`) |
| `status` | TEXT, CHECK (ACTIVE, INACTIVE) | Chooses the version to use; not in the code, needed once configuration lives in a database |
| `access_matrix_state`, `equipment_matrix_state` | TEXT, CHECK (MISSING, VERIFIED_EMPTY, VERIFIED) | The code must tell "missing" from "verified empty" (BR-03); an empty set of rule rows cannot |
| `rules_config` | JSONB | Blocks, each with its own state: approvers and delegates per class (ACCESS, EQUIPMENT, EXCEPTION), lead times per kind (EQUIPMENT, ACCESS, CLIENT_SITE_CLEARANCE), working calendar, handover rules, evidence roles, country rules, client-site rules |
| `created_at` | TIMESTAMPTZ | |
| *recommended:* `verified_by`, `verified_at` | TEXT, TIMESTAMPTZ | "VERIFIED" in the code means "supplied", not "confirmed by the business". Record who confirmed it |

### 2.3 `uc04_role_item_rules` (5 fields): the allowlists
Replaces `role_access_matrix` and `role_equipment_matrix`, which are both lists of item codes. Keeping two tables is acceptable; each would then have 4 fields (no `item_kind`).
| Field | Type and constraint | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `config_id` | UUID, FK to `uc04_client_configs` | Client, role and version come from the config |
| `item_kind` | TEXT, CHECK (ACCESS, EQUIPMENT) | |
| `item_code` | TEXT | Free text code; whether a catalogue table is needed is open |
| `created_at` | TIMESTAMPTZ | UNIQUE(`config_id`, `item_kind`, `item_code`) |

### 2.4 `uc04_items` (14 fields): one row per equipment or access request
Replaces `store.ItemRecord` and the stored part of `ItemResult`. Merges the two proposed request tables, so approvals, tickets, evidence and risks have a single foreign key.
| Field | Type and constraint | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `uc04_case_id` | UUID, FK | |
| `request_key` | TEXT | The business key used for duplicate detection (BR-14); UNIQUE(`uc04_case_id`, `request_key`) |
| `item_kind` | TEXT, CHECK (ACCESS, EQUIPMENT) | |
| `item_code` | TEXT | |
| `state` | TEXT, CHECK in the 16 `ItemState` values | DRAFT_PREPARED to ITEM_COMPLETED, BLOCKED, CANCELLED |
| `client_site` | BOOLEAN | Needed for EC-35: a client change cancels old client-site requests |
| `exception_required` | BOOLEAN | True when the item is outside the role's allowlist |
| `requested_by` | TEXT, nullable | User reference, subject to the identity design |
| `due_date` | DATE, nullable | The code works in dates |
| `adapter_error` | TEXT, nullable | Error class name only; blocks a blind retry until a person acknowledges it |
| `superseded_at` | TIMESTAMPTZ, nullable | Set when a request is replaced (corrected request or client change); state is then CANCELLED |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

### 2.5 `uc04_approvals` (11 fields): approval decisions
Replaces `contracts.ApprovalRecord`. A pending approval is simply the absence of a row; the code has no PENDING record.
| Field | Type and constraint | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `item_id` | UUID, FK to `uc04_items` | One request, not two nullable foreign keys |
| `approval_type` | TEXT, CHECK (STANDARD, EXCEPTION) | An out-of-matrix item needs both |
| `approver_ref` | TEXT | User reference. Not a foreign key until the identity design is settled |
| `approver_role` | TEXT | Must match the configured approver or delegate for the class |
| `decision` | TEXT, CHECK (APPROVED, REJECTED) | |
| `decided_on` | DATE | The code compares dates (a future-dated approval is refused) |
| `client_id` | UUID, FK | The scope of the approval |
| `role_code` | TEXT | The scope of the approval |
| `client_site_required` | BOOLEAN | The scope of the approval; a mismatch is `STALE_SCOPE` |
| `created_at` | TIMESTAMPTZ | |

### 2.6 `uc04_it_tickets` (9 fields): tickets raised for items
Replaces `contracts.TicketUpdate` and the mock ticket. A row exists only after a successful submission; a failed attempt is recorded on the item (`adapter_error`).
| Field | Type and constraint | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `item_id` | UUID, FK | |
| `provider` | TEXT | MOCK for now, so mock and real tickets cannot be confused |
| `external_ticket_id` | TEXT | UNIQUE(`provider`, `external_ticket_id`) |
| `ticket_status` | TEXT, CHECK | The code's list is synthetic (CREATED, IN_PROGRESS, REJECTED, COMPLETED). The real list is an open question |
| `status_reason` | TEXT, nullable | Sanitised rejection reason only |
| `submitted_at` | TIMESTAMPTZ, nullable | |
| `status_updated_on` | DATE | |
| `created_at` | TIMESTAMPTZ | |

### 2.7 `uc04_evidence` (10 fields): completion evidence (new table)
Readiness depends on evidence (BR-17, GR-07), and the proposal had no place for it. Replaces `ProvisioningRecord`, `DeliveryRecord`, `CollectionRecord` and `ClientSiteClearance`.
| Field | Type and constraint | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `uc04_case_id` | UUID, FK | |
| `item_id` | UUID, nullable FK | Null for client-site clearance, which belongs to the case |
| `evidence_type` | TEXT, CHECK (PROVISIONING, DELIVERY, COLLECTION, CLIENT_SITE_CLEARANCE) | |
| `status` | TEXT | By type: provisioning (CONFIRMED_ACTIVE, FAILED); delivery (PENDING, DELIVERED, FAULTY, WRONG_MODEL); collection (COLLECTED, MISSED_SLOT, NOT_COLLECTED); clearance (PENDING, CONFIRMED, DENIED) |
| `evidence_ref` | TEXT | Reference to the evidence stored in the case |
| `asserted_by_role` | TEXT | Counts only if the configured evidence roles include it |
| `expected_date` | DATE, nullable | Expected delivery, next collection slot or expected clearance; the delay risk (EC-29) depends on it |
| `event_date` | DATE, nullable | When it happened (for example delivered on) |
| `created_at` | TIMESTAMPTZ | The code uses the last record listed per request; evidence has no timestamp today, which is an open question |

### 2.8 `uc04_risks` (12 fields, optional): history of risks and blockers
Risks and blockers are recomputed on every evaluation, so the service itself needs nothing stored here: persisting them is only for history and dashboards. Replaces `contracts.Risk` and `contracts.Blocker`.
The audit trail covers blockers only partly and risks hardly at all: each blocker is audited individually (`blocker.raised`, with code, rule, owner and next action), but risks are audited only as a count (`risk.calculated`, for example "1 risk(s), 0 blocker(s)"), without naming the risk. If the business needs to know which risk existed when (for example "laptop delayed beyond joining, detected on this date"), this table is required, or the service must start auditing each risk.
| Field | Type and constraint | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `uc04_case_id` | UUID, FK | |
| `item_id` | UUID, nullable FK | |
| `kind` | TEXT, CHECK (RISK, BLOCKER) | |
| `code` | TEXT | For example EQUIPMENT_DELAYED_BEYOND_JOINING |
| `severity` | TEXT, nullable, CHECK (BLOCKING, REVIEW) | Blockers only; risks have no severity in the code |
| `reason` | TEXT | Sanitised |
| `owner` | TEXT | Every blocker and risk names an owner (BR-19) |
| `next_action` | TEXT | Sanitised |
| `rule_id` | TEXT | Traceability to the requirement |
| `detected_at` | TIMESTAMPTZ | |
| `resolved_at` | TIMESTAMPTZ, nullable | Open or resolved is derived from this |

### 2.9 `uc04_audit_events` (13 fields): append-only audit trail
Replaces `audit.AuditLog`, whose fields are `timestamp, case_id, request_id, rule_id, event_type, previous_status, new_status, reason, next_action, owner`.
| Field | Type and constraint | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `uc04_case_id` | UUID, FK | |
| `item_id` | UUID, nullable | The request the event concerns |
| `rule_id` | TEXT, nullable | |
| `event_type` | TEXT | |
| `previous_status`, `new_status` | TEXT, nullable | |
| `reason` | TEXT, nullable | Sanitised, at most 300 characters |
| `next_action` | TEXT, nullable | Sanitised |
| `owner` | TEXT, nullable | |
| `event_timestamp` | TIMESTAMPTZ | |
| `correlation_id` | TEXT, nullable | The run id |
| `actor_reference` | TEXT, nullable | **New**: the code does not record the caller today, but GR-09 asks for every action to be logged |

### 2.10 `uc04_idempotency` (7 fields): stored results for replays (new table)
Replaces `CaseRecord.results`.
| Field | Type and constraint | Notes |
|---|---|---|
| `uc04_case_id`, `idempotency_key` | UUID FK, TEXT | Together the primary key. The key belongs to a call, not to an item |
| `payload_hash` | TEXT | The same key with a different payload is a conflict |
| `state_version` | INTEGER | |
| `parent_status` | TEXT | A replay is served only while hash, state version and parent status all still match |
| `result` | JSONB | The sanitised stored result |
| `created_at` | TIMESTAMPTZ | |

## 3. Proposed fields removed, and why
| Proposed field | Decision | Reason |
|---|---|---|
| `uc04_cases.client_id`, `employee_reference` | Removed | The parent case owns them; client at last evaluation is in the snapshot |
| `uc04_cases.readiness_status` | Removed | Duplicate of `status` (READY and AT_RISK are case statuses) |
| `uc04_cases.risk_level`, `next_action` | Removed | Not in the code; next actions are a list with owners, derived on each call |
| `uc04_cases.started_at`, `completed_at` | Removed | Not modelled; completion is the READY status |
| `uc04_client_configs.work_mode`, `client_site_required` | Removed | Per-case input, not client configuration |
| `uc04_client_configs.country_code`, `site_requirements` | Moved | Inside `rules_config` (country rules, client-site rules) |
| `*.effective_from`, `effective_until` | Removed | No source defines effective dating; versions are used instead |
| `uc04_role_access_rules.access_level`, `requires_mfa` | Removed | Not modelled and no source asks for them |
| `uc04_role_access_rules.is_allowed` | Removed | The matrix is an allowlist; presence means allowed |
| `*.requires_approval`, `approval_required` | Removed | Every item needs approval; approvers are configured per class |
| `uc04_role_equipment_rules.equipment_name`, `required_quantity`, `quantity` | Removed | Names and quantities are not modelled |
| `uc04_role_equipment_rules.lead_time_days` | Moved | Lead times are per kind in `rules_config` |
| `uc04_role_equipment_rules.collection_required` | Moved | Collection is a per-client handover rule in `rules_config` |
| `uc04_role_equipment_rules.allow_after_joining` | Removed | No source allows post-joining equipment or access (AC-07 open question) |
| `*.rule_version` | Removed | Carried by the config version |
| `access_requests.requested_access_level`, `preparation_status` | Removed | Not modelled |
| `*.eligibility_status`, `approval_status`, `provisioning_status`, `delivery_status`, `collection_status` | Removed | Derived from the item state, approvals and evidence |
| `access_requests.exception_reason` | Replaced | The code uses a constant code, so `exception_required` is a boolean |
| `access_requests.idempotency_key` | Moved | Idempotency is per call, not per item (`uc04_idempotency`) |
| `*.client_id` on request tables | Removed | Derivable through the case; keep it only if row-level security performance requires it |
| `equipment_requests.delivered_at`, `collected_at` | Moved | `uc04_evidence` |
| `uc04_approvals.access_request_id`, `equipment_request_id`, `uc04_case_id` | Replaced | One `item_id` |
| `uc04_approvals.approval_scope` | Replaced | Three explicit scope columns |
| `uc04_approvals.decision_reason` | Removed | Not in the code; free text can leak sensitive data |
| `uc04_approvals.requested_at`, `decided_at` | Removed | Pending is the absence of a row; the code uses a date |
| `uc04_it_tickets.ticket_type` | Removed | Equals the item kind |
| `uc04_it_tickets.submission_status` | Removed | A row exists only after success; failures are on the item |
| `uc04_it_tickets.last_synced_at` | Removed | Nothing polls the desk (status arrives as input) |
| `uc04_it_tickets.evidence_reference` | Moved | `uc04_evidence` |
| `uc04_risks.status`, `resolution_notes` | Removed | Status is derived from `resolved_at`; free text is not in the code |
| `uc04_audit_events.entity_type`, `entity_id` | Replaced | `item_id` |
| `uc04_audit_events.actor_type`, `outcome` | Removed | Not in the code; the status change is already recorded |
| `uc04_audit_events.details` (JSONB) | Removed | The code deliberately uses a fixed field list so nothing sensitive can slip in |

## 4. Relationships and constraints in one view
`onboarding_cases` 1 to 1 `uc04_cases` 1 to many `uc04_items`; each item has many `uc04_approvals`, `uc04_it_tickets`, `uc04_evidence` rows and optionally many `uc04_risks`. `uc04_cases` also has many `uc04_audit_events`, `uc04_evidence` (case-level clearance) and `uc04_idempotency` rows. `clients` 1 to many `uc04_client_configs` 1 to many `uc04_role_item_rules`.
Required constraints: UNIQUE(`onboarding_case_id`); UNIQUE(`uc04_case_id`, `request_key`); UNIQUE(`provider`, `external_ticket_id`); PK(`uc04_case_id`, `idempotency_key`); CHECK on every status column from the existing enums; foreign keys with a defined delete behaviour (restrict for audit and evidence).

## 5. Security notes
- Row-level security must derive the permitted client and user from trusted identity, never from a caller-supplied `client_id`. The service-role key must never reach a frontend.
- Audit and evidence tables are append-only for the application role: revoke UPDATE and DELETE. Row-level security alone does not guarantee immutability.
- Approvals must come from the approval system. A stored approver id does not prove authorisation, and the code can only check the consistency of records it is given.
- Store only synthetic data in development. Never store tokens, credentials or free-form personal data in any column.

## 6. Open decisions before a migration
1. The database: Azure Cosmos DB (per the deck) or Supabase for testing. A document store changes this design.
2. The real `clients` and `onboarding_cases` types and primary keys, to confirm the foreign keys.
3. Whether `item_code` needs a catalogue table.
4. Whether `evaluated_snapshot` should be typed columns.
5. The real ticket-status list, and whether the parent supplies evidence timestamps.
6. Whether `uc04_risks` is persisted at all.
7. Whether row-level security needs `client_id` on the request tables for performance.
8. The identity design for `approver_ref`, `requested_by` and `actor_reference`.

Next step after review: finalise names and constraints against the real shared schema, then prepare one version-controlled SQL migration for the team to review before anything is applied.
