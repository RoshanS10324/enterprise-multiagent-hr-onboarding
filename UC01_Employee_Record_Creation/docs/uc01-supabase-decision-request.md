# Decision request: UC-01 Supabase persistence (B1–B9)

**From:** UC-01 Employee Record Creation (Logesh)
**To:** team lead, DBA / platform owner, HR data owner, client-configuration owner
**Date:** 2026-11-04
**Status:** UC-01 can't save a single onboarding case to the dev Supabase
project until these are answered. UC-01 has made no Supabase writes. All
checks so far were read-only: connectivity, tables, row counts.

**Please reply with:** the option letter (or your own answer), plus a name
for who signs it off. Each item is independent unless it says otherwise.
**B1, B2 and B6 block everything else.**

| # | Decision | Owner | Blocks |
|---|---|---|---|
| B1 | One shared dev encryption key | Team lead | All writes |
| B2 | Where UC-01's audit trail lives | DBA | All writes |
| B3 | How UC-01 stores offers | HR data owner + DBA | Email-intake flow |
| B4 | What a "case folder" is | Product / platform owner | Case creation |
| B5 | Case `status` vocabulary | DBA | Case creation |
| B6 | All-or-nothing writes | DBA | Case creation |
| B7 | Employee-number function | DBA | Case creation |
| B8 | A synthetic client for UC-01 test data | Client-configuration owner | Test data |
| B9 | Where the onboarding-form details live | DBA + HR data owner | Document-extracted details |

## Tracking (updated 2026-11-04)

The database was re-checked read-only on 2026-11-04 and was unchanged.
**No decision has been answered yet.**

| # | Owner | Approval | UC-01 implementation | Migration | Evidence |
|---|---|---|---|---|---|
| B1 | Team lead | **Pending** | Key handling exists (`crypto.py`). No key on this machine. | None | Key confirmed missing (read-only) |
| B2 | DBA | **Pending** (coupled to B6) | Not started; `UnsupportedAuditLog` refuses | Draft: `uc01_audit_log` in `docs/schema/proposed/uc01_b2_b4_b7_b9_draft.sql` | None |
| B3 | HR + DBA | **Pending** | Not started; `UnsupportedOfferRepository` refuses | None (needs the HR answer) | None |
| B4 | Product / platform | **Pending** | Not started; source file not stored | Draft: private bucket + `candidate_documents` metadata | No buckets exist (read-only) |
| B5 | DBA | **Pending** | Not started; `UnsupportedCaseRepository` refuses | None (needs the constraint) | None |
| B6 | DBA | **Pending** (coupled to B2) | `PostgrestClient.rpc` added (one call = one transaction) | None | Fake-PostgREST tests only |
| B7 | DBA | **Pending** | `SupabaseEmployeeNumberGenerator` implemented, **not wired** | Reference function in the draft (comment only) | 10 fake-PostgREST tests; live safety **not** verified |
| B8 | Config owner | **Pending** | None | None | `SYN-UC01-DOC-TEST` and `CLIENT-ALPHA` absent. The 4 synthetic clients (`CLIENT_DUBAI` and others) exist, but their only config version is **draft**, so UC-02 refuses all of them (read-only, 2026-11-04). See `docs/supabase-dev-setup.md` §2. |
| B9 | DBA + HR | **Pending** | Extraction done; no persistence | Draft: three typed tables with RLS | Design only |

Also implemented: `SupabaseApprovalRepository` (`uc01_approvals`), covered
by 8 fake-PostgREST tests. It is **not wired** into the container: no flow
can complete until B2 is decided, so wiring it alone would add nothing.

**B2 and B6 must be decided together.** The existing audit-chain design
(`docs/schema/audit-chain-design.md` §3) computes each entry's hash in
Python while holding a Postgres advisory lock. That needs a direct database
connection (B6 option B). With PostgREST SQL functions (B6 option A), the
hash would have to be computed in SQL, which means re-implementing UC-01's
canonical digest there. The B6 recommendation below assumed the audit
question was separate. It is not.

---

## B1. One shared dev encryption key

**Owner:** team lead (and whoever created the row below)

**Question:** Which `UC01_FIELD_ENCRYPTION_KEY` is the agreed key for the
shared dev project, and how is it distributed to developers?

**Why it matters:**
- UC-01 encrypts `onboarding_forms.salary_and_terms_encrypted` with
  AES-256-GCM.
- One encrypted row already exists: client `SYN-UAE-TEST`, status APPROVED,
  created 2026-11-04 05:19 UTC, with a synthetic `example.invalid` email.
- That row was written with a key that is not on the UC-01 developer's
  machine.
- If a developer generates their own key, rows become unreadable to
  everyone else. Data is not lost, but decryption fails.

**Options:**
- **A.** Whoever created that row shares its key through the team's secret
  store. It becomes the single dev key. UC-01 checks it by decrypting the
  row in memory before writing anything; the value is never printed.
  *(Recommended.)*
- **B.** The team agrees a new key. The row's owner re-creates their row
  under it. UC-01 deletes nothing that it did not create.
- **C.** Each developer keeps a separate key and only touches their own
  synthetic clients. *(Not recommended: it hides real problems.)*

**Also needed:**
- Who backs the key up, and where (Key Vault or Supabase Vault).
- Rotation is not implemented. Changing the key makes existing rows
  unreadable.

**Answer:** ______  **Signed off by:** ______

---

## B2. Where UC-01's audit trail lives (decision D4)

**Owner:** DBA

**Question:** Should UC-01 write its hash-chained audit entries to the
shared `audit_events` table, or to a dedicated UC-01 table?

**Why it matters:**
- Every UC-01 flow writes an audit entry **first**, so this blocks all
  persistence.
- The live `audit_events` table is shared: it already holds 398 rows from
  other use cases. It requires a `client_id` (NOT NULL). UC-01's audit
  entries carry a case id, and request-level entries (for example
  `START_GATE_EVALUATED`) have no client id or case id.
- UC-01's audit chain needs a gap-free, strictly increasing `sequence`. The
  sequence must be assigned atomically, and PostgREST cannot do that alone.
- Background: `docs/uc01-supabase-decisions.md` D4 and
  `docs/schema/audit-chain-design.md`.

**Options:**
- **A.** A dedicated `uc01_audit_log` table, append-only (UPDATE, DELETE
  and TRUNCATE blocked by trigger). The sequence is allocated by a SQL
  function. *(Recommended; matches the earlier D4 recommendation.)*
- **B.** Use the shared `audit_events` table. UC-01 adds `client_id` to
  every entry, and the sequence is unique among UC-01 rows only (partial
  unique index plus an allocation function).
- **C.** No hash chain in Supabase. Audit rows are stored without
  tamper-evidence. *(Not recommended: it weakens an existing control.)*

**Also needed:** whether a migration may be applied to the dev project, and
by whom. UC-01 will supply the DDL for review and will not apply it itself.

**Answer:** ______  **Signed off by:** ______

---

## B3. How UC-01 stores offers

**Owner:** HR data owner + DBA

**Question:** The live `offers` table requires `candidate_name`,
`designation` and `offer_payload` (all NOT NULL). UC-01's offer record
carries only the client, reference, email, HR officer, status and
acceptance time. Where should the missing values come from, or should the
columns be optional for UC-01 rows?

**Options:**
- **A.** The real offer source supplies them. Please name the system and
  its fields. UC-01 extends its offer record to match.
- **B.** The three columns become nullable for UC-01-originated rows.
- **C.** A separate `uc01_offers` table holding exactly what UC-01 knows.

UC-01 will not fill these columns with placeholder values.

**Answer:** ______  **Signed off by:** ______

---

## B4. What a "case folder" is

**Owner:** product / platform owner

**Question:** When UC-01 opens a case it creates a case folder
(`DocumentStore.create_case_folder`) and stores its reference in
`onboarding_cases.folder_ref`. Where should that folder actually exist?

**Options:**
- **A.** A Supabase Storage bucket. Please give the bucket name, the path
  pattern and the access policy.
- **B.** An external document system (for example SharePoint). It needs its
  own integration and credentials.
- **C.** No physical folder. `folder_ref` is only a logical reference such
  as `uc01/<client>/<case>`, recorded and labelled as such.
  *(Recommended for now.)*

**Answer:** ______  **Signed off by:** ______

---

## B5. Case `status` vocabulary

**Owner:** DBA (together with the orchestration owner)

**Question:** Does `onboarding_cases.status` have a CHECK constraint or an
agreed vocabulary? UC-01 currently writes `PLAN_PENDING`, but the live
default is `INTAKE_PENDING`. The table is shared, with 12 rows from other
use cases.

**Please provide:**
- The constraint definition, if any. PostgREST does not show constraints.
- Which values UC-01 may write.

**Options:**
- **A.** UC-01 maps its statuses onto the shared vocabulary. Please provide
  the mapping.
- **B.** The column accepts UC-01's values (`PLAN_PENDING`, `ON_HOLD`, …).

**Answer:** ______  **Signed off by:** ______

---

## B6. All-or-nothing writes (decision D6)

**Owner:** DBA

**Question:** How may UC-01 make a case-opening atomic?

**Why it matters:**
- Opening a case writes to several tables: case, form link, person record,
  approvals and audit.
- PostgREST runs one statement per HTTP call. A failure part-way through
  leaves a half-created record.
- That breaks "never return success when persistence failed" and "no
  partially created employee records".

**Options:**
- **A.** A SQL function per operation (for example `uc01_open_case(...)`)
  called through `/rpc`. One transaction, and no new driver.
  *(Recommended.)*
- **B.** A direct Postgres connection (psycopg) with a restricted
  `uc01_app` role. This needs a correct `DATABASE_URL` for the dev project;
  the current one points at a different project.
- **C.** Accept non-atomic writes with compensating clean-up.
  *(Not recommended.)*

**Answer:** ______  **Signed off by:** ______

---

## B7. Employee-number function

**Owner:** DBA

**Question:** Is `public.next_employee_no(p_client_id text)` atomic and
safe under concurrent calls? Who may execute it?

**Please provide:**
- The function body. PostgREST shows only its signature.
- Whether it is a single `UPDATE … RETURNING` or uses row locking.
- The role that has `GRANT EXECUTE` on it.
- The prefix and padding to use for UC-01 test clients. The counter table
  defaults are `EMP-` and 6.

**Answer:** ______  **Signed off by:** ______

---

## B8. A synthetic client for UC-01 test data

**Owner:** client-configuration owner

**Question:** May UC-01 add one clearly synthetic client, `SYN-UC01-MOCK`,
to the dev project? Or should it reuse an existing synthetic client? Which
one?

**Why it matters:**
- `onboarding_cases.client_id` has a foreign key to `client_configs`.
- The client-configs table is shared configuration. A new row needs
  `company_name`, `config_hash`, `working_days` and `settings_payload`.
- An `employee_number_counters` row is also needed for B7.

**Options:**
- **A.** The configuration owner creates `SYN-UC01-MOCK` with the values
  they choose. *(Recommended.)*
- **B.** UC-01 uses an existing synthetic client such as `SYN-UAE-TEST`. It
  then shares that client's cases and employee numbers with other
  developers.

**Answer:** ______  **Signed off by:** ______

---

## B9. Where the onboarding-form details live

**Owner:** DBA + HR data owner

**Question:** UC-01 now extracts the 2026 onboarding form. Only these
fields fit live columns: name, email, designation, joining date, work
location and the encrypted salary (`onboarding_forms`). Where should the
rest be stored?

**The rest:** reporting line, probation, visa terms, the documents
checklist status, father's and mother's name, marital status, mobile
number, insurance, benefits, other promises, and the responsibilities.

**Options:**
- **A.** A new table, `uc01_onboarding_form_details`, keyed by `form_id`,
  with the personal details and mobile number encrypted like the salary.
  *(Recommended.)*
- **B.** Store only non-personal details. Personal details are not stored
  by UC-01 (UC-03 collects them with the documents).
- **C.** Do not store them. They are used for validation only.

The source file itself needs B4 (storage) before it can be kept anywhere.

**Answer:** ______  **Signed off by:** ______

---

## After the answers arrive

UC-01 will:
1. Implement the six missing repositories against the agreed design.
2. Test them against the fake PostgREST.
3. Share any DDL for DBA review; the DBA applies it.
4. Run the end-to-end acceptance test against the dev project: create a
   synthetic employee, read it back, restart, and read it again.
5. Seed `data/mock/employees.json` with `--target supabase`.

Nothing is deleted. Every row is tagged as synthetic.

## Related decisions outside B1–B8

- **HR data owner:** does the orchestrator's `location` mean the same as
  UC-01's `location_name`? See `docs/uc01-placement-orchestrator-handover.md`.
- **Product:** where should HR read the model's draft explanations, now
  that they are not stored in the audit log? One option is the approval
  record.
