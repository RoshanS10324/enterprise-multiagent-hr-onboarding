-- ============================================================================
-- DRAFT - not applied, for review only.
--
-- 2026-11-03: corrected for drift. Seven fields that exist on the
-- Pydantic models and are used in src/ were missing from this file:
--   cases.reactivates_record_id, cases.reactivates_external_ref,
--   hris_records.updates_external_ref, person_records.hris_external_ref,
--   audit_log.sequence, audit_log.prev_hash, audit_log.entry_hash
-- The audit three are the hash chain; a table built from the previous
-- version of this file would have silently destroyed audit integrity.
-- See docs/schema/live-schema-comparison.md.
--
-- NOTE: this file describes the schema UC-01 WANTS. It is not the
-- schema that exists in the Onboarding_Agent project - that one differs
-- in table names and required columns. live-schema-comparison.md is the
-- authority on what is actually there.
--
-- UC-01 persistence schema, extracted from the current Pydantic models,
-- ports.py and adapters/memory.py (see docs/schema/uc01-schema.md for the
-- full field-by-field reasoning). Target: Postgres via Supabase.
--
-- [Proposed, needs lead / AI governance approval]: both the Postgres/
-- Supabase choice itself (docs/known-gaps.md #27 previously assumed
-- Cosmos DB) and the encryption approach for salary_and_terms below.
--
-- Every table/column/constraint here traces back to a specific model
-- field or port method. Where the code is ambiguous, look for a
-- "-- QUESTION:" comment instead of an invented field.
--
-- Enums use `text` + `CHECK` rather than native Postgres ENUM types -
-- see "Why text + CHECK" in uc01-schema.md (several enum values are
-- defined but never assigned by any code path read, and the whole
-- policy/role model is still [Proposed]).
-- ============================================================================

-- pgcrypto: gen_random_uuid() for PKs, pgp_sym_encrypt/decrypt for
-- salary_and_terms (see section 7 of the .md; Supabase enables this by
-- default).
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ============================================================================
-- cases  (Case, models/case.py)
-- ============================================================================
CREATE TABLE cases (
    case_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id             text NOT NULL,
    employee_no           text,                      -- Q-21: format is a placeholder (EMP-%06d)
    candidate_name        text NOT NULL,              -- PII
    candidate_email       text NOT NULL,              -- PII
    hr_officer_id         text NOT NULL,
    joining_date          date NOT NULL,
    work_location_type    text NOT NULL
        CHECK (work_location_type IN ('OFFICE', 'CLIENT_SITE')),

    -- VariantFlags flattened (see uc01-schema.md section 2 for why, not jsonb):
    variant_is_rehire             boolean NOT NULL DEFAULT false,
    variant_is_internal_transfer  boolean NOT NULL DEFAULT false,
    variant_is_agency_contractor  boolean NOT NULL DEFAULT false,
    variant_is_minor              boolean NOT NULL DEFAULT false,
    variant_is_uae_resident       boolean NOT NULL DEFAULT false,  -- declared only; never set by any code path read
    variant_is_remote             boolean NOT NULL DEFAULT false,  -- declared only; never set by any code path read
    -- Rejected alternative: variants jsonb NOT NULL DEFAULT '{}'::jsonb
    -- (kept as flattened booleans: individual flags are branched on in
    -- open_case.py / duplicate_check.py, so plain indexable booleans beat
    -- jsonb ->> comparisons for the only queries this needs today.)

    status                 text NOT NULL DEFAULT 'PLAN_PENDING'
        CHECK (status IN (
            'HELD', 'PLAN_PENDING', 'PLAN_APPROVED', 'IN_PROGRESS', 'AT_RISK',
            'READY_FOR_DAY_ONE', 'ONBOARDED', 'ON_HOLD', 'WITHDRAWN', 'CLOSED'
        )),
        -- Only PLAN_PENDING (default) and ON_HOLD are ever assigned by code
        -- read; WITHDRAWN/CLOSED are checked against (inactive-case guards)
        -- but never set; the rest are reserved for use cases outside UC-01.
    current_form_version   integer NOT NULL CHECK (current_form_version >= 1),
    opened_at               timestamptz NOT NULL DEFAULT now(),
    folder_ref              text,                      -- external DocumentStore reference, not itself a table

    -- Rehire / internal-transfer reactivation (EC-43). Added 2026-11-03:
    -- both are on the Case model and used in open_case.py /
    -- duplicate_check.py, but were missing from this draft. They point at
    -- the PersonRecord being reactivated and at how the target HRIS
    -- already identifies that person, so an update targets the existing
    -- employee instead of minting a second one.
    reactivates_record_id     text,
    reactivates_external_ref  text
);

CREATE INDEX cases_client_id_idx ON cases (client_id);
-- HR_OFFICER row visibility (access.case_visible) filters on both:
CREATE INDEX cases_client_hr_officer_idx ON cases (client_id, hr_officer_id);

-- employee_no is assigned by EmployeeNumberGenerator.next(client_id) and
-- is meant to identify one employee within a client; added per review
-- (fix #7) as a DB-enforced safety net alongside that generator.
CREATE UNIQUE INDEX cases_client_employee_no_uq
    ON cases (client_id, employee_no)
    WHERE employee_no IS NOT NULL;


-- ============================================================================
-- onboarding_forms  (OnboardingForm, models/onboarding_form.py)
-- ============================================================================
CREATE TABLE onboarding_forms (
    form_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           text NOT NULL,
    -- Nullable: a form is constructed before its Case exists (seeded via
    -- the [Proposed] /dev/seed path, or built at the API boundary) and
    -- linked via exactly one with_case() call, right after create_case()
    -- creates the Case row. See the immutability trigger below.
    case_id             uuid REFERENCES cases (case_id),
    version             integer NOT NULL CHECK (version >= 1),
    candidate_name      text NOT NULL,              -- PII
    candidate_email     text NOT NULL,              -- PII
    designation         text NOT NULL,
    joining_date        date NOT NULL,
    work_location_type  text NOT NULL
        CHECK (work_location_type IN ('OFFICE', 'CLIENT_SITE')),
    client_site_id      text,
        -- App-level rule (OnboardingForm._check_rules): required when
        -- work_location_type = 'CLIENT_SITE'. Expressible as:
        --   CHECK (work_location_type <> 'CLIENT_SITE' OR client_site_id IS NOT NULL)
        -- left as app code here since this extraction mirrors existing
        -- model validators 1:1; noted as a safe future upgrade.

    -- OnboardingForm.salary_and_terms (SecretStr in code, never rendered,
    -- rebuilt fresh at write time rather than read back for display) is
    -- stored encrypted, not as plain text. [Proposed, needs lead / AI
    -- governance approval]: the adapter must encrypt with
    -- pgp_sym_encrypt(plaintext, key) before INSERT/UPDATE and decrypt
    -- with pgp_sym_decrypt(salary_and_terms_encrypted, key) on read. The
    -- key comes from a secret store (Key Vault / Supabase Vault) at
    -- application runtime - NEVER from a column default, a database
    -- setting, or a non-dev .env file (same rule as DEDUP_HMAC_KEY in
    -- docs/known-gaps.md #5). See section 12 (fix #5).
    salary_and_terms_encrypted bytea NOT NULL,

    status              text NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT', 'AWAITING_APPROVAL', 'APPROVED', 'SUPERSEDED', 'REJECTED')),
    approved_by         text,
        -- App-level rule (OnboardingForm._check_rules): required when
        -- status = 'APPROVED'. Expressible as:
        --   CHECK (status <> 'APPROVED' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
        -- left as app code for the same reason as client_site_id above.
    approved_at         timestamptz,                -- field_validator rejects naive datetimes; tz-aware only
    change_reason       text

    -- One form per (case_id, version); at most one APPROVED and one
    -- AWAITING_APPROVAL per case: see the partial unique indexes below.
);

CREATE UNIQUE INDEX onboarding_forms_case_version_uq
    ON onboarding_forms (case_id, version)
    WHERE case_id IS NOT NULL;
    -- FormRepository.get(case_id, version) assumes exactly one match.

CREATE UNIQUE INDEX onboarding_forms_one_approved_per_case_uq
    ON onboarding_forms (case_id)
    WHERE status = 'APPROVED';
    -- invariants.py MULTIPLE_APPROVED_FORMS
    -- NOTE: write order. Postgres checks a non-deferrable unique index
    -- per statement, not per transaction. approve_form_change() in
    -- form_changes.py currently saves the new APPROVED form BEFORE
    -- superseding the old one, which would momentarily violate this
    -- index if applied as written. The adapter must reverse that order
    -- (SUPERSEDE the old version first, THEN INSERT the new APPROVED
    -- version), both inside one transaction. See docs/schema/
    -- uc01-schema.md section 9 and section 12 (fix #2). Deliberately not
    -- using DEFERRABLE INITIALLY DEFERRED here instead, per review.

CREATE UNIQUE INDEX onboarding_forms_one_awaiting_per_case_uq
    ON onboarding_forms (case_id)
    WHERE status = 'AWAITING_APPROVAL';
    -- invariants.py MULTIPLE_PENDING_FORMS; also defensively re-checked by propose_form_change

CREATE INDEX onboarding_forms_case_id_idx ON onboarding_forms (case_id);

-- Immutability: REJECTED/SUPERSEDED never change again; APPROVED may only
-- move to SUPERSEDED (status-only) OR stay APPROVED while case_id is
-- backfilled from NULL (OnboardingForm.with_case() called on an already-
-- APPROVED form, e.g. the direct /internal/onboarding-requests path which
-- builds an already-APPROVED form before any Case exists); case_id may
-- move NULL -> value exactly once, at ANY pre-terminal or APPROVED status.
-- DRAFT -> AWAITING_APPROVAL -> APPROVED (submit_for_approval()/approve())
-- are themselves updates of the SAME row (onboarding_form.py's
-- _dump_with_secret() always carries the existing form_id forward; only
-- next_version() pops it to start a new row) - confirmed from
-- onboarding_form.py, so those status transitions must stay allowed too.
-- See docs/schema/uc01-schema.md section 5 and section 12 (fix #1).
CREATE OR REPLACE FUNCTION uc01_enforce_onboarding_form_immutability()
RETURNS trigger AS $$
BEGIN
    -- ------------------------------------------------------------------
    -- ALWAYS-IMMUTABLE COLUMNS (every status, including DRAFT).
    -- Added on review 2026-11-03: the previous version compared NONE of
    -- these in ANY branch, so `UPDATE onboarding_forms SET client_id =
    -- 'other-tenant'` on an APPROVED row passed every check and moved
    -- the salary, candidate identity and approval evidence to another
    -- tenant. See docs/schema/review-2026-11-03.md F2.
    -- ------------------------------------------------------------------

    -- Tenant ownership. A form never changes hands.
    IF NEW.client_id IS DISTINCT FROM OLD.client_id THEN
        RAISE EXCEPTION
            'onboarding_forms: client_id is immutable (row %)', OLD.form_id;
    END IF;

    -- Form identity. form_id is the primary key AND the AES-GCM AAD the
    -- adapter binds salary_and_terms_encrypted to
    -- (adapters/supabase/repositories.py). Changing it would silently
    -- make the stored salary permanently undecryptable.
    IF NEW.form_id IS DISTINCT FROM OLD.form_id THEN
        RAISE EXCEPTION
            'onboarding_forms: form_id is immutable - it is the encryption AAD (row %)',
            OLD.form_id;
    END IF;

    -- Version belongs to the row; a new version is a NEW row
    -- (onboarding_form.next_version() pops form_id to start one).
    IF NEW.version IS DISTINCT FROM OLD.version THEN
        RAISE EXCEPTION
            'onboarding_forms: version is immutable; a new version is a new row (row %)',
            OLD.form_id;
    END IF;

    IF OLD.status IN ('REJECTED', 'SUPERSEDED') THEN
        RAISE EXCEPTION 'onboarding_forms: row % is % and cannot be updated', OLD.form_id, OLD.status;
    END IF;

    -- Status transition whitelist, mirroring the model exactly:
    --   DRAFT     -> DRAFT | AWAITING_APPROVAL | APPROVED
    --               (approve() accepts DRAFT or AWAITING_APPROVAL)
    --   AWAITING  -> AWAITING_APPROVAL | APPROVED | REJECTED
    --   APPROVED  -> APPROVED (case_id backfill only) | SUPERSEDED
    IF NOT (
        (OLD.status = 'DRAFT'
             AND NEW.status IN ('DRAFT', 'AWAITING_APPROVAL', 'APPROVED'))
        OR (OLD.status = 'AWAITING_APPROVAL'
             AND NEW.status IN ('AWAITING_APPROVAL', 'APPROVED', 'REJECTED'))
        OR (OLD.status = 'APPROVED'
             AND NEW.status IN ('APPROVED', 'SUPERSEDED'))
    ) THEN
        RAISE EXCEPTION
            'onboarding_forms: illegal status transition % -> % (row %)',
            OLD.status, NEW.status, OLD.form_id;
    END IF;

    -- ------------------------------------------------------------------
    -- BYPASS CLOSED 2026-11-04 (review F15): approving and rewriting in
    -- ONE statement.
    --
    -- The APPROVED protections below are all inside `IF OLD.status =
    -- 'APPROVED'`. A single
    --     UPDATE ... SET status='APPROVED', salary_and_terms_encrypted=...
    -- from a DRAFT row skips that block entirely, so content could be
    -- rewritten at the moment of approval - defeating the point of
    -- freezing an approved form. OnboardingForm.approve() only ever sets
    -- status/approved_by/approved_at, so requiring content to be
    -- unchanged on the way in costs nothing legitimate.
    -- ------------------------------------------------------------------
    IF OLD.status <> 'APPROVED' AND NEW.status = 'APPROVED' THEN
        IF NEW.candidate_name IS DISTINCT FROM OLD.candidate_name
            OR NEW.candidate_email IS DISTINCT FROM OLD.candidate_email
            OR NEW.designation IS DISTINCT FROM OLD.designation
            OR NEW.joining_date IS DISTINCT FROM OLD.joining_date
            OR NEW.work_location_type IS DISTINCT FROM OLD.work_location_type
            OR NEW.client_site_id IS DISTINCT FROM OLD.client_site_id
            OR NEW.salary_and_terms_encrypted IS DISTINCT FROM OLD.salary_and_terms_encrypted
        THEN
            RAISE EXCEPTION
                'onboarding_forms: approving may set status/approved_by/approved_at only, not content (row %)',
                OLD.form_id;
        END IF;
    END IF;

    -- ------------------------------------------------------------------
    -- BYPASS CLOSED 2026-11-04 (review F16): cross-tenant case linking.
    --
    -- The FK on case_id only proves the case EXISTS. Nothing stopped a
    -- form being linked to a case belonging to a DIFFERENT client, which
    -- would place one tenant's salary and candidate data under another
    -- tenant's case - and every child-table RLS policy scopes through
    -- cases, so the row would then be readable by that other tenant.
    -- ------------------------------------------------------------------
    IF NEW.case_id IS NOT NULL
       AND NEW.case_id IS DISTINCT FROM OLD.case_id
       AND NOT EXISTS (
           SELECT 1 FROM cases c
            WHERE c.case_id = NEW.case_id
              AND c.client_id = NEW.client_id
       )
    THEN
        RAISE EXCEPTION
            'onboarding_forms: case % belongs to a different client than form % (row %)',
            NEW.case_id, NEW.client_id, OLD.form_id;
    END IF;

    IF OLD.status = 'APPROVED' THEN
        IF NEW.status = 'APPROVED' THEN
            -- Only allowed change while staying APPROVED: the one-time
            -- case_id backfill (with_case()). Every other column, and
            -- case_id itself once already non-null, must be unchanged.
            IF NOT (OLD.case_id IS NULL AND NEW.case_id IS NOT NULL) THEN
                RAISE EXCEPTION 'onboarding_forms: an APPROVED row may only have case_id backfilled from NULL (row %)', OLD.form_id;
            END IF;
            -- IS DISTINCT FROM throughout, not <>: for a NULLable column
            -- `NEW.x <> OLD.x` yields NULL when either side is NULL, and
            -- a NULL is not TRUE, so the change would pass unnoticed.
            -- client_site_id and change_reason are exactly that case and
            -- were missing here entirely (review F2/F9).
            IF NEW.candidate_name IS DISTINCT FROM OLD.candidate_name
                OR NEW.candidate_email IS DISTINCT FROM OLD.candidate_email
                OR NEW.designation IS DISTINCT FROM OLD.designation
                OR NEW.joining_date IS DISTINCT FROM OLD.joining_date
                OR NEW.work_location_type IS DISTINCT FROM OLD.work_location_type
                OR NEW.client_site_id IS DISTINCT FROM OLD.client_site_id
                OR NEW.salary_and_terms_encrypted IS DISTINCT FROM OLD.salary_and_terms_encrypted
                OR NEW.approved_by IS DISTINCT FROM OLD.approved_by
                OR NEW.approved_at IS DISTINCT FROM OLD.approved_at
                OR NEW.change_reason IS DISTINCT FROM OLD.change_reason
            THEN
                RAISE EXCEPTION 'onboarding_forms: APPROVED row case_id backfill must change case_id only (row %)', OLD.form_id;
            END IF;
        ELSIF NEW.status <> 'SUPERSEDED' THEN
            RAISE EXCEPTION 'onboarding_forms: an APPROVED row may only transition to SUPERSEDED (row %)', OLD.form_id;
        ELSE
            -- APPROVED -> SUPERSEDED: status-only change.
            IF NEW.case_id IS DISTINCT FROM OLD.case_id
                OR NEW.candidate_name IS DISTINCT FROM OLD.candidate_name
                OR NEW.candidate_email IS DISTINCT FROM OLD.candidate_email
                OR NEW.designation IS DISTINCT FROM OLD.designation
                OR NEW.joining_date IS DISTINCT FROM OLD.joining_date
                OR NEW.work_location_type IS DISTINCT FROM OLD.work_location_type
                OR NEW.client_site_id IS DISTINCT FROM OLD.client_site_id
                OR NEW.salary_and_terms_encrypted IS DISTINCT FROM OLD.salary_and_terms_encrypted
                OR NEW.approved_by IS DISTINCT FROM OLD.approved_by
                OR NEW.approved_at IS DISTINCT FROM OLD.approved_at
                OR NEW.change_reason IS DISTINCT FROM OLD.change_reason
            THEN
                RAISE EXCEPTION 'onboarding_forms: APPROVED -> SUPERSEDED must change status only (row %)', OLD.form_id;
            END IF;
        END IF;
    END IF;

    -- DRAFT/AWAITING_APPROVAL stay freely updatable in place (same
    -- form_id row, per submit_for_approval()/approve()/reject()/
    -- with_case()); only the case_id-set-once rule applies across every
    -- status, including these pre-terminal ones.
    IF OLD.case_id IS NOT NULL AND NEW.case_id IS DISTINCT FROM OLD.case_id THEN
        RAISE EXCEPTION 'onboarding_forms: case_id may only be set once, from NULL (row %)', OLD.form_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER onboarding_forms_immutability
    BEFORE UPDATE ON onboarding_forms
    FOR EACH ROW EXECUTE FUNCTION uc01_enforce_onboarding_form_immutability();


-- ============================================================================
-- offer_forms  (bridge table; no dedicated Pydantic model - derived from
-- the [Proposed] OnboardingFormSource port's (client_id, offer_ref) key)
-- ============================================================================
CREATE TABLE offer_forms (
    client_id   text NOT NULL,
    offer_ref   text NOT NULL,
    form_id     uuid NOT NULL REFERENCES onboarding_forms (form_id),
    PRIMARY KEY (client_id, offer_ref)
    -- InMemoryOnboardingFormSource.save_for_offer overwrites this key on
    -- every call: no history is kept in current code. This table
    -- reproduces that (one live row per offer, not a log).
);


-- ============================================================================
-- approvals  (Approval, models/approval.py)
-- ============================================================================
CREATE TABLE approvals (
    approval_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id         uuid NOT NULL REFERENCES cases (case_id),
    checkpoint      text NOT NULL
        CHECK (checkpoint IN ('HC_01_FORM', 'HC_04_DATA', 'HC_05_HRIS_WRITE', 'HC_12_VARIANT')),
    subject_ref     text NOT NULL,
        -- Free-text tag: "form:<form_id>", "discrepancy:<discrepancy_id>",
        -- "hris:<case_id>:v<version>", or a bare variant-flag name
        -- ("is_rehire", "possible_duplicate", ...). See section 3 of the
        -- .md for the kept-as-string-plus-optional-typed-FKs proposal.
    requested_to    text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    requested_by    text,
    status          text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'APPROVED', 'EDITED_AND_APPROVED', 'REJECTED', 'EXPIRED', 'ESCALATED', 'CANCELLED')),
        -- Only PENDING/APPROVED/REJECTED/CANCELLED ever assigned by code read.
    decided_by      text,
    decided_at      timestamptz,
    reason          text,
    decision_note   text

    -- Proposed additive, nullable typed references (see .md section 3);
    -- not yet added to keep this draft a literal extraction:
    --   subject_form_id         uuid REFERENCES onboarding_forms (form_id),
    --   subject_discrepancy_id  uuid REFERENCES discrepancies (discrepancy_id),
    --   subject_case_hris_version integer  -- paired with case_id for the "hris:" batch case
);

CREATE INDEX approvals_case_id_idx ON approvals (case_id);
CREATE INDEX approvals_pending_for_idx ON approvals (requested_to, status) WHERE status = 'PENDING';
    -- ApprovalRepository.list_pending_for(requested_to)

-- Immutability: a decided approval (status left PENDING) never changes again.
CREATE OR REPLACE FUNCTION uc01_enforce_approval_immutability()
RETURNS trigger AS $$
BEGIN
    IF OLD.status <> 'PENDING' THEN
        RAISE EXCEPTION 'approvals: row % is already decided (%) and cannot be updated', OLD.approval_id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER approvals_immutability
    BEFORE UPDATE ON approvals
    FOR EACH ROW EXECUTE FUNCTION uc01_enforce_approval_immutability();


-- ============================================================================
-- discrepancies  (Discrepancy, models/discrepancy.py)
-- ============================================================================
CREATE TABLE discrepancies (
    discrepancy_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id           uuid NOT NULL REFERENCES cases (case_id),
    field             text NOT NULL
        CHECK (field IN ('candidate_name', 'designation', 'joining_date', 'work_location_type', 'salary_and_terms')),
        -- Closed set per SourceSnapshot.ALLOWED_SOURCE_SNAPSHOT_KEYS; not
        -- a Python enum, but a de facto closed, enforced list.
    value_form        text NOT NULL,
        -- "[REDACTED]" sentinel when field='salary_and_terms'; the real
        -- PII value otherwise (e.g. an actual name mismatch).
    value_other       text NOT NULL,
    other_source      text NOT NULL,            -- free text, e.g. "offer_letter", "hr_system"
    form_version      integer NOT NULL,
    status            text NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'RESOLVED', 'ACCEPTED_AS_CHANGE', 'STALE')),
    decided_by        text,
    decided_at        timestamptz,
    -- QUESTION: other_fingerprint has `exclude=True, repr=False` on the
    -- Pydantic model, meaning it is deliberately left out of
    -- model_dump()/API responses. It must still be stored here - it IS
    -- the dedup key compare_sources.py checks before inserting a new
    -- discrepancy. A real adapter must not build its INSERT by calling
    -- model_dump_json() naively, or this column's value will be lost.
    other_fingerprint text NOT NULL
);

CREATE INDEX discrepancies_case_id_idx ON discrepancies (case_id);

-- Dedup key compare_sources.py currently checks before INSERT (read-then-
-- write, with the same TOCTOU race the in-memory dict has). Promoting it
-- to a real constraint + ON CONFLICT DO NOTHING closes that race:
CREATE UNIQUE INDEX discrepancies_dedup_uq
    ON discrepancies (case_id, field, other_source, other_fingerprint, form_version);


-- ============================================================================
-- hris_records  (HrisRecord, models/hris_record.py)
-- ============================================================================
CREATE TABLE hris_records (
    record_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id               uuid NOT NULL REFERENCES cases (case_id),
    client_id             text NOT NULL,          -- denormalized copy of cases.client_id, as in the Python model
    employee_no           text,
    system                text NOT NULL
        CHECK (system IN ('ZOHO_PEOPLE', 'ZOHO_PAYROLL')),
        -- Target product still TBD: see "Zoho vs Workday/ADP" open question.
    form_version          integer NOT NULL CHECK (form_version >= 1),
    prepared_payload       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- non-sensitive field NAMES+values only
    sensitive_field_names   text[] NOT NULL DEFAULT '{}',          -- NAMES only, never values
    -- QUESTION: comparison_result is defined on the Pydantic model but no
    -- code path found in src/ ever sets or reads it. Column reserved,
    -- no current writer - do not invent a purpose for it here.
    comparison_result      jsonb,
    external_ref            text,
    -- Added 2026-11-03 (was missing from this draft): set when an
    -- approved form change lands AFTER the record was written. It is
    -- what makes the write an UPDATE of that existing employee rather
    -- than a second INSERT, so losing it risks duplicate employees.
    updates_external_ref    text,
    last_error               text,                  -- exception CLASS NAME only, never a message
    status                  text NOT NULL DEFAULT 'NOT_PREPARED'
        CHECK (status IN (
            'NOT_PREPARED', 'PREPARED', 'MISMATCH', 'AWAITING_APPROVAL', 'APPROVED',
            'WRITTEN', 'BLOCKED', 'CORRECTED', 'FAILED', 'REJECTED', 'STALE'
        )),
        -- MISMATCH/BLOCKED/CORRECTED are defined but never assigned by code read.
    approved_by             text,
    approved_at              timestamptz,
    -- REVISED 2026-11-04 (review F7). On 2026-11-03 this was changed to a
    -- plain column + CHECK on the belief that the live column was a plain
    -- nullable text column the adapter could write. That reading came from
    -- PostgREST metadata, which cannot show whether a column is generated.
    -- Tested directly, the live column is GENERATED ALWAYS (2026-11-04: an insert that
    -- supplies it fails with SQLSTATE 428C9; an omitted value is generated
    -- as case_id:system:form_version, byte-for-byte equal to
    -- HrisRecord._derive_idempotency_key; a repeat hits the UNIQUE
    -- constraint uq_hris_sync_idempotency). The draft follows the live
    -- column, and SupabaseHrisRecordRepository._to_row no longer sends it.
    -- The explicit form_version::text cast keeps the expression IMMUTABLE.
    idempotency_key          text NOT NULL
        GENERATED ALWAYS AS (case_id::text || ':' || system || ':' || form_version::text) STORED
);

CREATE INDEX hris_records_case_id_idx ON hris_records (case_id);
CREATE UNIQUE INDEX hris_records_idempotency_uq ON hris_records (idempotency_key);
    -- Equivalent to UNIQUE (case_id, system, form_version); matches
    -- invariants.py's DUPLICATE_WRITTEN_SYSTEM check's assumption that
    -- there is never more than one row per (case, system, form_version).


-- ============================================================================
-- audit_log  (AuditEntry, models/audit_entry.py) - append-only
-- ============================================================================
CREATE TABLE audit_log (
    log_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id       uuid REFERENCES cases (case_id),
    -- text, not uuid: request_id is not always a UUID. Formats found in
    -- code: `f"req-{uuid4().hex}"` (api/app.py default when the caller
    -- doesn't supply one - and OnboardingRequestBody.request_id is an
    -- unvalidated Optional[str], so a caller MAY supply any string at
    -- all), `new_id()` i.e. str(uuid4()) (intake/email_intake.py), and
    -- `f"req-eval-{uuid4().hex[:8]}"` (evaluation/uc01/runner.py). Only the
    -- intake one is a real UUID string. See section 12 (fix #4).
    request_id    text,
        -- Not a FK to any persisted request row - purely a per-call
        -- correlation id generated (or caller-supplied) at the start of
        -- a request/flow.
    actor_type    text NOT NULL
        CHECK (actor_type IN ('AGENT', 'HUMAN', 'SYSTEM')),
    actor_id      text NOT NULL,
    -- QUESTION: `action` is a free-text Python str, not a StrEnum - dozens
    -- of distinct values are used across modules (CASE_OPENED,
    -- OFFER_ACCEPTED, HRIS_WRITE_SUCCEEDED, INTAKE_REJECTED, ...) and more
    -- are added as the system grows. No CHECK list proposed; the
    -- NOT NULL + non-empty check is the only guard.
    action        text NOT NULL CHECK (action <> ''),
    before        jsonb,
    after         jsonb,
    "timestamp"   timestamptz NOT NULL DEFAULT now(),
    reference     text NOT NULL,

    -- Hash chain (audit_integrity.py). Added 2026-11-03: these three
    -- were missing from this draft, which would have produced an audit
    -- log that looks complete but whose integrity can never be
    -- verified. chain_entry() stamps sequence and prev_hash, then
    -- derives entry_hash over a canonical digest, so an edit, deletion
    -- or reordering is detectable.
    --
    -- Nullable because a row written by another use case has no UC-01
    -- chain; the partial unique index below only constrains rows that
    -- do participate.
    sequence      integer,
    prev_hash     text,
    entry_hash    text,

    CONSTRAINT audit_log_case_or_request_required
        CHECK (case_id IS NOT NULL OR request_id IS NOT NULL)
        -- AuditEntry._require_case_or_request_id, 1:1.
);

CREATE INDEX audit_log_case_id_idx ON audit_log (case_id, "timestamp");
CREATE INDEX audit_log_action_idx ON audit_log (action);

-- CORRECTION (2026-11-03, second pass): an earlier revision of this
-- file proposed a PER-CASE chain, UNIQUE (case_id, sequence). That is
-- WRONG and would break verification. audit_integrity.verify_chain
-- asserts `entry.sequence == index` over the COMPLETE log in append
-- order (see its docstring, and case_status.verify_audit_integrity
-- which is AUDITOR-only precisely because "the whole log spans every
-- client"). A per-case chain has gaps in `sequence` by construction and
-- would be reported as SEQUENCE_BREAK on an intact log.
--
-- The chain is therefore GLOBAL, contiguous, and zero-based, matching
-- InMemoryAuditLog.append (sequence = len(self._entries)).
--
-- Consequences, which are real costs and not hidden:
--   * every UC-01 audit append must serialize against one counter;
--   * a gap can never be left behind, so an append that allocates a
--     sequence and then fails must not commit - the allocation and the
--     insert have to be in the same transaction;
--   * on a table shared with other use cases, "the complete log" means
--     "all rows with sequence IS NOT NULL", so UC-01 rows must be
--     distinguishable.
-- See docs/uc01-supabase-decisions.md D4 for the options.
CREATE UNIQUE INDEX audit_log_sequence_uq
    ON audit_log (sequence)
    WHERE sequence IS NOT NULL;

-- Append-only: revoke UPDATE/DELETE from the application role AND add a
-- trigger that always raises, so a privilege misconfiguration alone can't
-- silently allow a mutation.
-- REVOKE UPDATE, DELETE ON audit_log FROM <app_role>;  -- substitute the real app role name

CREATE OR REPLACE FUNCTION uc01_audit_log_append_only()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION uc01_audit_log_append_only();

CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION uc01_audit_log_append_only();

-- TRUNCATE is NOT a DELETE and does NOT fire row-level triggers, so the
-- two above left the entire audit log erasable in one statement. A
-- truncate trigger is statement-level by definition. Added on review
-- 2026-11-03 (review F3).
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION uc01_audit_log_append_only();

-- ----------------------------------------------------------------------
-- WHO CAN STILL BYPASS THESE CONTROLS (review F3). Stated plainly
-- because an append-only claim that is not qualified is misleading:
--
--   * The TABLE OWNER can ALTER TABLE ... DISABLE TRIGGER, or DROP the
--     trigger, then mutate freely. Triggers do not protect against the
--     owner. uc01_app must therefore NOT own audit_log; ownership
--     should sit with a separate migration/DDL role that the
--     application never authenticates as.
--   * A SUPERUSER bypasses everything, including RLS and any REVOKE.
--     In Supabase, `postgres` is effectively this.
--   * `service_role` carries BYPASSRLS. It does not bypass triggers,
--     but it does bypass every row policy, so it must never be the
--     application's connection identity.
--   * Anyone with DDL on the schema can DROP the trigger function.
--   * `session_replication_role = replica` disables ordinary triggers
--     for that session; granting that setting is equivalent to
--     granting mutation rights.
--
-- The chain is the defence that survives all of the above: a mutation
-- made with sufficient privilege is still DETECTABLE, because the
-- digests stop reproducing. It is not PREVENTABLE at the database
-- level alone. Truncation of the most recent entries remains
-- undetectable without an external high-water mark
-- (audit_integrity.verify_chain says so in its own docstring).
-- ----------------------------------------------------------------------


-- ============================================================================
-- person_records  (PersonRecord, models/person_record.py)
-- ============================================================================
CREATE TABLE person_records (
    record_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   text NOT NULL,
    email       text NOT NULL,              -- PII; matched case-insensitively, trimmed, in code
    id_number   text,                       -- PII (national ID / passport-like)
    full_name   text NOT NULL,              -- PII; matched case-insensitively, trimmed
    kind        text NOT NULL
        CHECK (kind IN ('OPEN_CASE', 'PAST_EMPLOYEE', 'CURRENT_EMPLOYEE')),
    case_id     uuid REFERENCES cases (case_id),  -- only set when kind = 'OPEN_CASE'
    -- Added 2026-11-03 (was missing from this draft): how the target
    -- HRIS already identifies this person. A rehire or internal
    -- transfer reactivates that employee rather than creating a second
    -- one (EC-43). NULL when the person is not in the HRIS yet.
    hris_external_ref text
);

CREATE INDEX person_records_client_email_idx ON person_records (client_id, lower(email));
CREATE INDEX person_records_client_id_number_idx ON person_records (client_id, id_number);
CREATE INDEX person_records_client_name_idx ON person_records (client_id, lower(full_name));

-- Closes a TOCTOU race: two simultaneous requests for the same
-- candidate_email can both pass check_duplicates (both see no existing
-- OPEN_CASE match yet) and both proceed to create_case(), which inserts
-- a PersonRecord(kind=OPEN_CASE) each. This index makes the second
-- INSERT fail; the adapter must catch that unique_violation and turn it
-- into the existing DUPLICATE_OPEN_CASE / LINKED_EXISTING outcome
-- (onboarding_entry.open_onboarding_case already has this outcome for
-- the case duplicate_check finds synchronously - this is the same
-- outcome for the race it currently cannot catch). See section 4 and
-- section 12 (fix #7).
CREATE UNIQUE INDEX person_records_one_open_case_uq
    ON person_records (client_id, lower(email))
    WHERE kind = 'OPEN_CASE';


-- ============================================================================
-- offers  ([Proposed] OfferRecord, models/offer.py)
-- ============================================================================
CREATE TABLE offers (
    -- QUESTION: OfferRecord has no offer_id/UUID field of its own in
    -- Python; every port method addresses an offer by (client_id,
    -- offer_ref) or offer_ref alone. PK is the composite the code
    -- actually uses, not an invented surrogate key.
    client_id         text NOT NULL,
    offer_ref         text NOT NULL,
    candidate_email   text NOT NULL,              -- PII
    hr_officer_id     text NOT NULL,
    status            text NOT NULL DEFAULT 'SENT'
        CHECK (status IN ('SENT', 'ACCEPTED', 'WITHDRAWN')),
    accepted_at       timestamptz,
    PRIMARY KEY (client_id, offer_ref)
);

-- [Proposed] offer refs are unique across clients - enforced today only
-- in InMemoryOfferRepository.save (raises ValueError on a cross-client
-- collision). Promoted here to a real DB constraint:
CREATE UNIQUE INDEX offers_offer_ref_global_uq ON offers (offer_ref);


-- ============================================================================
-- pending_starts  ([Proposed] PendingStart, models/pending_start.py)
-- ============================================================================
CREATE TABLE pending_starts (
    pending_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id     text NOT NULL,
    offer_ref     text NOT NULL,
    reasons       text[] NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    status        text NOT NULL DEFAULT 'WAITING'
        CHECK (status IN ('WAITING', 'STARTED', 'CANCELLED')),
        -- CANCELLED is defined but never assigned by any code path read.
    FOREIGN KEY (client_id, offer_ref) REFERENCES offers (client_id, offer_ref)
    -- QUESTION: even after a PendingStart transitions to STARTED
    -- (email_intake.try_start), the code never records which case_id it
    -- started - there is no case_id column on this model in Python. The
    -- only durable trace of the offer->case link is the case-scoped
    -- OFFER_LINKED audit entry. Not adding a case_id column here would be
    -- faithful to the code; not adding one is itself the open question
    -- (see docs/schema/uc01-schema.md section 11).
);

CREATE INDEX pending_starts_client_id_idx ON pending_starts (client_id, status) WHERE status = 'WAITING';

-- At most one WAITING PendingStart per offer (email_intake
-- ._ensure_waiting_pending_start: get-then-add, never duplicates):
CREATE UNIQUE INDEX pending_starts_one_waiting_per_offer_uq
    ON pending_starts (client_id, offer_ref)
    WHERE status = 'WAITING';


-- ============================================================================
-- domain_events_outbox  (new; CaseOpenedEvent/FormChangedEvent/
-- EmployeeRecordCreatedEvent from models/events.py, OfferAcceptedEvent
-- from models/offer.py)
-- ============================================================================
CREATE TABLE domain_events_outbox (
    event_id           uuid PRIMARY KEY,   -- from the event's own event_id (already a uuid4 default in Python)
    event_type         text NOT NULL
        CHECK (event_type IN ('CaseOpenedEvent', 'FormChangedEvent', 'EmployeeRecordCreatedEvent', 'OfferAcceptedEvent')),
    case_id            uuid REFERENCES cases (case_id),   -- NULL for OfferAcceptedEvent, which has no case_id
    client_id          text NOT NULL,
    payload            jsonb NOT NULL,
        -- The event's own remaining fields, e.g. for CaseOpenedEvent:
        --   {"form_version": 1, "joining_date": "...", "work_location_type": "OFFICE", "variants": {...}}
        -- for FormChangedEvent:
        --   {"old_version": 1, "new_version": 2, "changed_fields": ["joining_date"], "joining_date_changed": true}
        -- for EmployeeRecordCreatedEvent:
        --   {"employee_no": "EMP-000001", "form_version": 2}
        -- for OfferAcceptedEvent:
        --   {"offer_ref": "OFFER-1001", "accepted_at": "...", "source": "email"}
    -- occurred_at/published_at/publish_attempts are standard outbox-
    -- pattern bookkeeping, NOT Pydantic model fields. Each event's own
    -- occurred_at/accepted_at value lives inside payload for fidelity;
    -- occurred_at here means "when this row was staged for publish".
    occurred_at        timestamptz NOT NULL DEFAULT now(),
    published_at       timestamptz,          -- NULL = not yet relayed to a real message bus
    publish_attempts   integer NOT NULL DEFAULT 0
);

CREATE INDEX domain_events_outbox_unpublished_idx ON domain_events_outbox (occurred_at) WHERE published_at IS NULL;
CREATE INDEX domain_events_outbox_case_id_idx ON domain_events_outbox (case_id);


-- ============================================================================
-- client_holidays  (new; replaces InMemoryHolidayCalendar - no Pydantic model)
-- ============================================================================
CREATE TABLE client_holidays (
    client_id      text NOT NULL,
    holiday_date   date NOT NULL,
    PRIMARY KEY (client_id, holiday_date)
);


-- ============================================================================
-- employee_number_counters  (new; replaces InMemoryEmployeeNumberGenerator
-- - no Pydantic model) - [Proposed, Q-21]
-- ============================================================================
CREATE TABLE employee_number_counters (
    client_id    text PRIMARY KEY,
    last_value   bigint NOT NULL DEFAULT 0
    -- Reproduces only the counter itself; the "EMP-%06d" formatting rule
    -- stays in application code until Q-21 (real numbering rules) is answered.
);


-- ============================================================================
-- Draft Row Level Security (commented out - not applied; see
-- docs/schema/uc01-schema.md section 6 for full discussion).
--
-- Mirrors access.case_visible(viewer, case):
--   - client_id must be in the caller's client_ids claim
--   - HR_OFFICER additionally requires hr_officer_id = caller's user id
--   - HR_MANAGER / CLIENT_USER / AUDITOR see every case of their clients
-- Role-may-call-this-action authorization (policy/uc01_policy.toml) stays
-- in application code and is NOT replicated here - see the .md for why.
--
-- IMPORTANT: Supabase's service_role key BYPASSES RLS entirely
-- (BYPASSRLS). These policies are worthless if the backend connects with
-- service_role. Intended model, see docs/schema/uc01-schema.md section 6
-- (fix #6):
--   - Browsers/clients never connect to the database directly - only the
--     UC-01 Python backend does.
--   - The backend connects as a restricted application role (uc01_app
--     below), which does NOT have BYPASSRLS, so RLS actually applies to
--     it.
--   - access.py and policy.py remain the PRIMARY access control
--     (row visibility and role/action authorization respectively); RLS
--     here is defence in depth in case application-layer checks are
--     ever bypassed or buggy, not the only line of defence.
-- ============================================================================

-- CREATE ROLE uc01_app LOGIN PASSWORD '<from secret store, never literal>' NOBYPASSRLS;
--
-- GRANT SELECT, INSERT, UPDATE ON
--     cases, onboarding_forms, offer_forms, approvals, discrepancies,
--     hris_records, person_records, offers, pending_starts,
--     client_holidays, employee_number_counters
-- TO uc01_app;
--
-- -- Append-only tables: INSERT (and SELECT, so get_audit_trail/the
-- -- outbox poll can read rows back) but never UPDATE from this role -
-- -- reinforces the audit_log_no_update trigger and keeps outbox rows
-- -- insert-only from the application's own perspective. A SEPARATE
-- -- relay-worker role (not uc01_app) would hold UPDATE on
-- -- domain_events_outbox only, to set published_at/publish_attempts
-- -- once a row is actually relayed to the real message bus.
-- GRANT SELECT, INSERT ON audit_log, domain_events_outbox TO uc01_app;
--
-- -- No DELETE grant anywhere: no code path in UC-01 ever deletes a row.

-- ----------------------------------------------------------------------
-- REVISED 2026-11-03 (review F4). The previous draft was incompatible
-- with the recommended connection method, in two separate ways:
--
--   1. It defined ONLY `FOR SELECT` policies (11 of them, zero for
--      INSERT or UPDATE). Enabling RLS as written denies every write
--      from uc01_app, because a command with no permissive policy is
--      denied. The fix is NOT a blanket `USING (true)` write policy -
--      that would disable the control it is meant to enforce. Each
--      write policy below carries the SAME tenant predicate as its
--      read policy, as a WITH CHECK.
--
--   2. It read `request.jwt.claims`, a GUC that POSTGREST sets. Under
--      a direct psycopg connection (decision D6 option A, the
--      recommended one) that setting does not exist, so every policy
--      would evaluate against an empty claim set and deny everything.
--
-- The identity source is therefore the connection's own session, set
-- by the application at the start of each transaction:
--
--     SET LOCAL app.client_ids = 'acme,globex';
--     SET LOCAL app.user_id    = 'u-123';
--     SET LOCAL app.role       = 'HR_OFFICER';
--
-- SET LOCAL, not SET: the value dies with the transaction, so a pooled
-- connection cannot leak one request's tenant scope into the next.
-- This is MANDATORY on a pooled connection.
--
-- FAIL CLOSED: current_setting(..., true) returns NULL when unset, so
-- an un-scoped session sees an empty client list and therefore no
-- rows, rather than all of them.
--
-- If decision D6 lands on option B (stay on PostgREST), replace the two
-- helper functions below with the request.jwt.claims versions and keep
-- every policy body unchanged. The policies are written so that only
-- the identity source differs between the two options.
-- ----------------------------------------------------------------------

-- CREATE OR REPLACE FUNCTION uc01_claim_client_ids() RETURNS text[] AS $$
--     SELECT CASE
--         WHEN coalesce(current_setting('app.client_ids', true), '') = '' THEN ARRAY[]::text[]
--         ELSE string_to_array(current_setting('app.client_ids', true), ',')
--     END;
-- $$ LANGUAGE sql STABLE;
--
-- CREATE OR REPLACE FUNCTION uc01_claim(name text) RETURNS text AS $$
--     SELECT current_setting('app.' || name, true);
-- $$ LANGUAGE sql STABLE;
--
-- CREATE OR REPLACE FUNCTION uc01_case_visible(p_client_id text, p_hr_officer_id text) RETURNS boolean AS $$
--     SELECT p_client_id = ANY (uc01_claim_client_ids())
--         AND (uc01_claim('role') <> 'HR_OFFICER' OR p_hr_officer_id = uc01_claim('user_id'));
-- $$ LANGUAGE sql STABLE;

-- --- cases -------------------------------------------------------------
-- ALTER TABLE cases ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE cases FORCE ROW LEVEL SECURITY;   -- applies to the owner too
-- CREATE POLICY cases_select ON cases FOR SELECT
--     USING (uc01_case_visible(client_id, hr_officer_id));
-- CREATE POLICY cases_insert ON cases FOR INSERT
--     WITH CHECK (client_id = ANY (uc01_claim_client_ids()));
-- CREATE POLICY cases_update ON cases FOR UPDATE
--     USING (uc01_case_visible(client_id, hr_officer_id))
--     WITH CHECK (client_id = ANY (uc01_claim_client_ids()));
-- -- No DELETE policy anywhere, deliberately: no UC-01 code path deletes.

-- --- onboarding_forms ---------------------------------------------------
-- ALTER TABLE onboarding_forms ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE onboarding_forms FORCE ROW LEVEL SECURITY;
-- -- Scoped on the form's OWN client_id, not via a join to cases: a form
-- -- legitimately exists with case_id IS NULL before it is linked, and a
-- -- join-based policy would make those rows invisible to their creator.
-- CREATE POLICY onboarding_forms_select ON onboarding_forms FOR SELECT
--     USING (client_id = ANY (uc01_claim_client_ids()));
-- CREATE POLICY onboarding_forms_insert ON onboarding_forms FOR INSERT
--     WITH CHECK (client_id = ANY (uc01_claim_client_ids()));
-- CREATE POLICY onboarding_forms_update ON onboarding_forms FOR UPDATE
--     USING (client_id = ANY (uc01_claim_client_ids()))
--     WITH CHECK (client_id = ANY (uc01_claim_client_ids()));
-- -- NB: the WITH CHECK above stops a form being moved to a tenant the
-- -- caller does not hold, but NOT a move between two tenants the caller
-- -- DOES hold. The immutability trigger is what makes client_id
-- -- immutable outright; RLS is the second layer, not the first.

-- --- child tables: scoped through their case ----------------------------
-- ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE approvals FORCE ROW LEVEL SECURITY;
-- CREATE POLICY approvals_select ON approvals FOR SELECT
--     USING (case_id IN (SELECT case_id FROM cases WHERE uc01_case_visible(client_id, hr_officer_id)));
-- CREATE POLICY approvals_insert ON approvals FOR INSERT
--     WITH CHECK (case_id IN (SELECT case_id FROM cases WHERE client_id = ANY (uc01_claim_client_ids())));
-- CREATE POLICY approvals_update ON approvals FOR UPDATE
--     USING (case_id IN (SELECT case_id FROM cases WHERE uc01_case_visible(client_id, hr_officer_id)))
--     WITH CHECK (case_id IN (SELECT case_id FROM cases WHERE client_id = ANY (uc01_claim_client_ids())));
--
-- -- discrepancies and hris_records take the identical three policies;
-- -- omitted here only to keep this block readable.

-- --- audit_log: INSERT and SELECT only ----------------------------------
-- ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
-- CREATE POLICY audit_log_select ON audit_log FOR SELECT
--     USING (
--         case_id IS NULL   -- pre-case entries (START_GATE_EVALUATED etc.)
--         OR case_id IN (SELECT case_id FROM cases WHERE uc01_case_visible(client_id, hr_officer_id))
--     );
-- CREATE POLICY audit_log_insert ON audit_log FOR INSERT WITH CHECK (true);
-- -- WITH CHECK (true) here is deliberate and is NOT a permissive
-- -- loophole: audit rows must be insertable even for a pre-case request
-- -- that has no tenant row to join to yet, and an audit log that can
-- -- reject its own writes would silently lose the record of the action
-- -- being audited. There is NO update and NO delete policy, so both are
-- -- denied by absence, on top of the triggers above.
-- --
-- -- CAVEAT: the SELECT policy means `verify_audit_integrity` cannot see
-- -- the complete log unless it runs as a role exempt from this policy.
-- -- verify_chain REQUIRES the complete log (it asserts sequence ==
-- -- index), so the AUDITOR path needs either a dedicated role or a
-- -- SECURITY DEFINER verification function. Open design point, not a
-- -- solved one - see review F1/F4.

-- --- discrepancies / hris_records: scoped through their case ------------
-- REWRITTEN 2026-11-04 (review F17). These were previously PROSE
-- ("omitted here only to keep this block readable"), which meant 9 of
-- the 13 tables had neither an ENABLE line nor a single policy. The
-- effect was NOT "policies missing" - it was that those tables would
-- have had NO row-level isolation at all, while 4 tables did. Every
-- table the adapters touch now carries explicit policies, and the
-- operations below were taken from the adapter call sites, not guessed.

-- ALTER TABLE discrepancies ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE discrepancies FORCE ROW LEVEL SECURITY;
-- CREATE POLICY discrepancies_select ON discrepancies FOR SELECT
--     USING (case_id IN (SELECT case_id FROM cases WHERE uc01_case_visible(client_id, hr_officer_id)));
-- CREATE POLICY discrepancies_insert ON discrepancies FOR INSERT
--     WITH CHECK (case_id IN (SELECT case_id FROM cases WHERE client_id = ANY (uc01_claim_client_ids())));
-- CREATE POLICY discrepancies_update ON discrepancies FOR UPDATE
--     USING (case_id IN (SELECT case_id FROM cases WHERE uc01_case_visible(client_id, hr_officer_id)))
--     WITH CHECK (case_id IN (SELECT case_id FROM cases WHERE client_id = ANY (uc01_claim_client_ids())));
--     -- adapter: add() / list_for_case() / get() / update()

-- ALTER TABLE hris_records ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE hris_records FORCE ROW LEVEL SECURITY;
-- CREATE POLICY hris_records_select ON hris_records FOR SELECT
--     USING (case_id IN (SELECT case_id FROM cases WHERE uc01_case_visible(client_id, hr_officer_id)));
-- CREATE POLICY hris_records_insert ON hris_records FOR INSERT
--     WITH CHECK (case_id IN (SELECT case_id FROM cases WHERE client_id = ANY (uc01_claim_client_ids())));
-- CREATE POLICY hris_records_update ON hris_records FOR UPDATE
--     USING (case_id IN (SELECT case_id FROM cases WHERE uc01_case_visible(client_id, hr_officer_id)))
--     WITH CHECK (case_id IN (SELECT case_id FROM cases WHERE client_id = ANY (uc01_claim_client_ids())));
--     -- adapter: add() / get() / list_for_case() / update()

-- --- client-scoped leaf tables -------------------------------------------
-- person_records: adapter does add() + find_by_email/id_number/name.
-- Duplicate detection reads this table, so a leak here is a PII leak
-- across tenants even without any case being visible.
-- ALTER TABLE person_records ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE person_records FORCE ROW LEVEL SECURITY;
-- CREATE POLICY person_records_select ON person_records FOR SELECT
--     USING (client_id = ANY (uc01_claim_client_ids()));
-- CREATE POLICY person_records_insert ON person_records FOR INSERT
--     WITH CHECK (client_id = ANY (uc01_claim_client_ids()));
-- -- No UPDATE policy: no adapter path updates a person record today.

-- pending_starts: adapter does add() / get_waiting() / list_waiting() / update().
-- ALTER TABLE pending_starts ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE pending_starts FORCE ROW LEVEL SECURITY;
-- CREATE POLICY pending_starts_select ON pending_starts FOR SELECT
--     USING (client_id = ANY (uc01_claim_client_ids()));
-- CREATE POLICY pending_starts_insert ON pending_starts FOR INSERT
--     WITH CHECK (client_id = ANY (uc01_claim_client_ids()));
-- CREATE POLICY pending_starts_update ON pending_starts FOR UPDATE
--     USING (client_id = ANY (uc01_claim_client_ids()))
--     WITH CHECK (client_id = ANY (uc01_claim_client_ids()));

-- offer_forms: adapter UPSERTS (save_for_offer), so it needs BOTH
-- INSERT and UPDATE - an upsert with only an INSERT policy fails the
-- moment the row already exists.
-- ALTER TABLE offer_forms ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE offer_forms FORCE ROW LEVEL SECURITY;
-- CREATE POLICY offer_forms_select ON offer_forms FOR SELECT
--     USING (client_id = ANY (uc01_claim_client_ids()));
-- CREATE POLICY offer_forms_insert ON offer_forms FOR INSERT
--     WITH CHECK (client_id = ANY (uc01_claim_client_ids()));
-- CREATE POLICY offer_forms_update ON offer_forms FOR UPDATE
--     USING (client_id = ANY (uc01_claim_client_ids()))
--     WITH CHECK (client_id = ANY (uc01_claim_client_ids()));

-- offers: OfferRepository is [Proposed]/dev-simulator-only and the
-- Supabase adapter REFUSES it, so no write policy is defined. Reads are
-- scoped in case a future path needs them.
-- ALTER TABLE offers ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE offers FORCE ROW LEVEL SECURITY;
-- CREATE POLICY offers_select ON offers FOR SELECT
--     USING (client_id = ANY (uc01_claim_client_ids()));

-- client_holidays: read-only in the adapter (is_holiday()).
-- ALTER TABLE client_holidays ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE client_holidays FORCE ROW LEVEL SECURITY;
-- CREATE POLICY client_holidays_select ON client_holidays FOR SELECT
--     USING (client_id = ANY (uc01_claim_client_ids()));

-- domain_events_outbox: the application INSERTS; a SEPARATE relay role
-- (not uc01_app) sets published_at/publish_attempts. Hence an INSERT
-- policy here and no UPDATE policy for uc01_app.
-- ALTER TABLE domain_events_outbox ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE domain_events_outbox FORCE ROW LEVEL SECURITY;
-- CREATE POLICY domain_events_outbox_select ON domain_events_outbox FOR SELECT
--     USING (client_id = ANY (uc01_claim_client_ids()));
-- CREATE POLICY domain_events_outbox_insert ON domain_events_outbox FOR INSERT
--     WITH CHECK (client_id = ANY (uc01_claim_client_ids()));

-- employee_number_counters: allocation goes through next_employee_no()
-- (SECURITY DEFINER), so uc01_app needs no direct write privilege.
--
-- !! CONFLICT TO RESOLVE BEFORE APPLYING (review F18) !!
-- FORCE ROW LEVEL SECURITY applies policies to the TABLE OWNER too. A
-- SECURITY DEFINER function runs as its owner, so if the function owner
-- also owns this table and FORCE is on, the function's UPDATE is
-- blocked by the absence of an UPDATE policy and allocation fails
-- closed. Pick one:
--   (a) ENABLE but do NOT FORCE, and let the owner bypass; or
--   (b) FORCE, and add an UPDATE policy that admits the function owner.
-- Do not leave this to chance - option (a) is the smaller change.
-- ALTER TABLE employee_number_counters ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY employee_number_counters_select ON employee_number_counters FOR SELECT
--     USING (client_id = ANY (uc01_claim_client_ids()));
