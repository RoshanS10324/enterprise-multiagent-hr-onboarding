# Existing client configuration: retrieval audit

Date: 2026-11-04. Scope: UC-01..UC-05 contracts as they bear on UC-02 configuration retrieval. UC-06 is out of scope.

**Verdict: the four existing clients (`CLIENT_CANADA`, `CLIENT_DUBAI`, `CLIENT_INDIA`, `CLIENT_USA`) are NOT configured for onboarding.** Each has only a
client master row and a draft, unapproved version 1 marked `environment: mock`; none has a single task template, document requirement, service policy,
milestone or access-matrix row. UC-02's retrieval correctly refuses all four, so no dynamic checklist can be generated for them. Nothing was fabricated to
make them work. The retrieval and checklist mechanism itself is proven on the one fully configured client, `SYN-UAE-TEST` (synthetic).

## How the evidence was gathered

| Kind | What | Writes |
|---|---|---|
| **Live database, read-only** | PostgREST `GET` against the project (every HTTP call was intercepted and asserted to be `GET`: 45 calls for retrieval, 12 for checklist preview, plus the inventory). Ran the real `SupabaseConfigSource` reader and the real `OnboardingService.preview_checklist`. | none |
| **Local mock tests** | `tests/test_existing_clients_retrieval.py`: the schema-enforcing mock database loaded with a read-only snapshot of the live configuration rows (`tests/fixtures/existing_clients_config_snapshot.json`); state changes (inactive, expired, future, approved-but-empty) made to the in-memory copy only. | none to Supabase |
| **Actual live workflow** | Not run for this audit. (The earlier synthetic workflow case `62e3b13e-...` for `SYN-UAE-TEST` is inspected read-only below.) | none |

**Environment note.** You report this project is labelled PRODUCTION. Earlier in this work it was treated as Development (`UC02_ENV=development`). Synthetic UC-02 records for the synthetic client `SYN-UAE-TEST` and case `62e3b13e-4448-5959-9b2e-56908d33678b` exist in it: the synthetic fixture rows (inserted earlier, as you reported) and the workflow records of the `run` / `decide` / `send` (mock Outlook) / `documents` stages. In this session I ran `run` (reported duplicate, no new plan), `tasks` (two waves: 4 tasks completed through mock updates), the overdue sweep (`--as-of 2026-11-20`: escalation and reminder-draft audit records) and `recover` (replayed event; audit records). Those wrote UC-02 rows (task status changes, audit events, case-state snapshots, agent runs) for that synthetic case only; no client configuration, no UC-01 record and nothing for the four existing clients was written. **Nothing at all was written during this audit.** Whether the synthetic records should stay in a Production project is your decision.

## 1. Existing client inventory (live, read-only)

| Client | Active | Company | Timezone | Working days | Version | Hash | Templates | Documents | Policies | Milestones | Holidays | Access matrix |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| CLIENT_CANADA | yes | Canada Mock Technologies Inc | America/Toronto | Mon-Fri | v1 **draft**, no approver, from 2026-11-02 | `mock-canada-v1` | 0 | 0 | 0 | 0 | 0 | 0 |
| CLIENT_DUBAI | yes | Dubai Mock Technologies LLC | Asia/Dubai | Mon-Fri | v1 **draft**, no approver | `mock-dubai-v1` | 0 | 0 | 0 | 0 | 2 (`[SAMPLE] UAE National Day` 2026-12-02, -03) | 0 |
| CLIENT_INDIA | yes | India Mock Technologies Pvt Ltd | Asia/Kolkata | Mon-Sat | v1 **draft**, no approver | `mock-india-v1` | 0 | 0 | 0 | 0 | 0 | 0 |
| CLIENT_USA | yes | USA Mock Technologies Inc | America/New_York | Mon-Fri | v1 **draft**, no approver | `mock-usa-v1` | 0 | 0 | 0 | 0 | 0 | 0 |
| SYN-UAE-TEST (synthetic, not a primary scenario) | yes | SYNTHETIC TEST CLIENT | Asia/Dubai | Mon-Fri | v1 approved (`synthetic-fixture`), from 2026-01-01 | `synthetic-3e270b8c` | 18 | 6 | 14 (`uc02_assignment`) | 0 | 2 | 0 |
| test_enterprise_corp (not a scenario) | yes | Acme Global Cloud Solutions | Asia/Dubai | Mon-Fri | **none** | `cfg_hash_demo_2026` | 0 | 0 | 0 | 0 | 0 | 0 |

`settings_payload` of the four: `{"country": "...", "currency": "...", "environment": "mock"}` only. Integrity: no orphan rows (every child row's client exists; every
versioned row's version exists), no duplicate natural keys in any of the eight tables. The configuration hashes are labels (`mock-<country>-v1`), not content hashes, and
there is no source document to compare them with, so hash matching is **not verifiable**.

## 2. Configuration completeness (same for all four existing clients unless noted)

| Configuration category | CLIENT_CANADA | CLIENT_DUBAI | CLIENT_INDIA | CLIENT_USA | Evidence / missing fields |
|---|---|---|---|---|---|
| Client master | VERIFIED | VERIFIED | VERIFIED | VERIFIED | row present, active, company and timezone set |
| Version configuration | PARTIALLY_CONFIGURED | PARTIALLY_CONFIGURED | PARTIALLY_CONFIGURED | PARTIALLY_CONFIGURED | v1 exists but `draft`, no `approved_by`/`approved_at`; hash is a label |
| Employee attributes | MISSING | MISSING | MISSING | MISSING | no attribute-driven template or document exists |
| Country and location | PARTIALLY_CONFIGURED | PARTIALLY_CONFIGURED | PARTIALLY_CONFIGURED | PARTIALLY_CONFIGURED | country is free text in `settings_payload`; no supported locations |
| Role and grade | MISSING | MISSING | MISSING | MISSING | no role or grade filter anywhere |
| Nationality applicability | MISSING | MISSING | MISSING | MISSING | no nationality group anywhere |
| Employment type | MISSING | MISSING | MISSING | MISSING | no column or rule exists |
| Identity documents | MISSING | MISSING | MISSING | MISSING | 0 document requirements |
| Education documents | MISSING | MISSING | MISSING | MISSING | 0 document requirements |
| Employment verification | MISSING | MISSING | MISSING | MISSING | - |
| Background verification | MISSING | MISSING | MISSING | MISSING | 0 templates |
| Visa / work authorisation | MISSING | MISSING | MISSING | MISSING | 0 templates; legal requirements AWAITING_CONFIRMATION |
| Medical requirements | MISSING | MISSING | MISSING | MISSING | - |
| Insurance | MISSING | MISSING | MISSING | MISSING | - |
| Payroll and banking | MISSING | MISSING | MISSING | MISSING | - |
| Tax / statutory | MISSING | MISSING | MISSING | MISSING | - |
| Task templates | MISSING | MISSING | MISSING | MISSING | `template_count = 0`, no rows |
| Task dependencies | MISSING | MISSING | MISSING | MISSING | no templates to depend on |
| Milestones | MISSING | MISSING | MISSING | MISSING | `milestone_count = 0`, no rows |
| Working calendar | PARTIALLY_CONFIGURED | PARTIALLY_CONFIGURED | PARTIALLY_CONFIGURED | PARTIALLY_CONFIGURED | working days present (India Mon-Sat); not approved |
| Holidays | MISSING | PARTIALLY_CONFIGURED | MISSING | MISSING | Dubai: 2 `[SAMPLE]` dates only |
| Equipment | MISSING | MISSING | MISSING | MISSING | `role_access_matrix` empty |
| Approved system access | MISSING | MISSING | MISSING | MISSING | `role_access_matrix` empty |
| Orientation | MISSING | MISSING | MISSING | MISSING | - |
| Training | MISSING | MISSING | MISSING | MISSING | - |
| Policy acknowledgement | MISSING | MISSING | MISSING | MISSING | - |
| Approval requirements | MISSING | MISSING | MISSING | MISSING | no service policies, no approver mapping |

For comparison, `SYN-UAE-TEST` has templates, dependencies, documents, assignments, calendar and holidays (VERIFIED as synthetic data), but no milestones, no access
matrix, and no employment-type, tax or orientation/training catalogue (MISSING). All of its values are synthetic assumptions, not approved policies.

## 3. Retrieval service findings (live, read-only)

Service: `onboarding_task_management/supabase_config.py`, `SupabaseConfigSource.load(client_id, on_date)`, used by UC-02 through `SupabaseConfigRegistry` and
`SupabaseCalendars`. It issues `SELECT`s only (verified in code and by intercepting every HTTP call). Order of checks: client row exists -> client active -> synthetic
clients only if the runtime opts in -> clients marked mock (`environment: mock` or a `mock-` hash) refused -> versions newest first: approved, approver and timestamp,
hash, effective dates -> active templates of THAT version (at least one) -> milestones, documents, assignment policies of that version, each filtered by `client_id` and
`config_version`.

| Request | Actual result |
|---|---|
| CLIENT_CANADA / DUBAI / INDIA / USA | refused: `client configuration is marked mock and is not approved for real onboarding`. Version gate on its own: `v1 is 'draft', not approved`. Calendar refused the same way. |
| SYN-UAE-TEST, synthetic not allowed | refused: `this client is a synthetic test fixture and is not accepted by this runtime` |
| SYN-UAE-TEST, synthetic allowed | loaded: version 1, 18 templates, 6 documents, 14 assignment rules, 0 milestones; every row belongs to SYN-UAE-TEST; calendar = 2 holidays, weekend Sat/Sun, `approved = False` |
| test_enterprise_corp | refused: `client has no configuration version` |
| `CLIENT_MISSING_XYZ`, empty id, `client_dubai` (wrong case) | refused: `client is not in client_configs` (no fallback to any other client) |
| Inactive / expired / future-effective / approved-without-approver / missing version | refused with the matching reason (local mock tests on the snapshot) |
| Role / access configuration | not retrieved by UC-02 (`role_access_matrix` is UC-04's and is empty for every client) |

## 4. Employee attribute matching findings

Matching is done per template and per document by `TaskTemplate.dimension_verdicts` / `DocumentRequirement.applies_to`: an empty filter means "all"; a non-empty filter
must contain the employee's value; every `requires_attributes` entry must be present. Verified on the live SYN-UAE-TEST configuration:

* In-country vs overseas (DL-09) routes are mutually exclusive and chosen by `location_status:*`; grade filters exclude senior tasks for L2; a missing attribute is
  never treated as a match.
* **Gap 1:** an **unsupported role** (`astronaut`) or **unsupported location** (`Atlantis`) is accepted and produces the same 13-task checklist, because the templates
  have no role or location filter and UC-02 has no client role/location catalogue to validate against. (Recorded as `expectedFailure` tests.)
* **Gap 2:** with the **location status missing**, both visa routes are excluded and the preview reports no issue (`review_required = False`): a UAE hire with an
  unstated status would silently get no visa task. (Recorded as an `expectedFailure` test.)
* Employment type and work country are not inputs to the UC-02 matcher (no such filter in the stored configuration). Joining date drives version selection and due dates.

## 5. Persistence and audit (live, read-only, synthetic case `62e3b13e-...`)

13 tasks, all `client_id = SYN-UAE-TEST`, `case_id` set, `template_key` set on every row (traceable to `task_templates`); 12 dependencies, all inside the case; the
configuration version is recorded at plan level (case-state snapshot `config_version = 1`); audit trail present (`onboarding.create`, `document.update`, `task.status_update`,
`downstream.trigger`, `monitor.*`, 62 case-state snapshots); the event ledger shows the replayed event recorded once without a second plan.
**Gap 3:** the `onboarding_tasks.config_version` column is **NULL on all 13 tasks** (the adapter does not write it). `plan_revisions` holds 0 rows for the case (no re-plan happened).

## 6. Missing configuration and confirmations still needed

* For each of the four clients: an approved configuration version (approver, timestamp, content hash) and every category marked MISSING above, supplied and signed off
  by the client's HR / configuration owners. Who may approve a client configuration is open (Business Framework OQ-04).
* Country/legal: visa and work-authorisation, medical, insurance, payroll/tax requirements for UAE, India, Canada and the USA must come from the country compliance
  packs (Framework section 7) and the clients. None exists in the repository; none was assumed.
* UC-04: the access matrix vocabulary and the role/item mapping. UC-03 / UC-05: their document and orientation/policy configuration (see the requirements matrix).

## 7. Next steps

1. Decide whether this project is Development or Production, and whether the synthetic records for `SYN-UAE-TEST` may stay in it.
2. Obtain approved configurations for the four clients (or decide that they remain placeholders); load them only in a Development project, through the reviewed seed path.
3. UC-02 fixes (code, not data): validate role and location against a client catalogue (Gap 1); flag a missing location status for review (Gap 2); write
   `config_version` on each task row (Gap 3). Each is pinned by an `expectedFailure` test in `tests/test_existing_clients_retrieval.py`.
4. Re-run this audit; then the checklist tests for the four clients become meaningful.
