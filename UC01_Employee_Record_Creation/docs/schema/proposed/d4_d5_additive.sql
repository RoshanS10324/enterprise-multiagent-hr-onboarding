-- ============================================================================
-- *** NOT APPLIED - REQUIRES DBA REVIEW ***
--
-- PROPOSED - requires sign-off on D1/D2/D3 (placement columns) and
-- D4/D5 (audit chain, allocation) before it is applied anywhere.
--
-- Deliberately NOT placed in supabase/migrations/, so that a stray
-- `supabase db push` cannot apply unapproved DDL to the shared
-- Onboarding_Agent project.
--
-- Apply locally (after `supabase start`) to run the integration test:
--     psql "$(supabase status -o env | grep DB_URL | cut -d= -f2-)" \
--          -f docs/schema/proposed/d4_d5_additive.sql
--
-- Every statement is ADDITIVE. Nothing is dropped, no column is made
-- stricter, no existing row is rewritten, and no other use case's
-- behaviour changes: the new columns are nullable and the new index is
-- partial, so UC-02/UC-05 rows (which have NULL sequence) are untouched.
-- ============================================================================


-- ---------------------------------------------------------------- D4
-- Audit hash chain. Without these three columns UC-01's audit log can
-- be written but never verified: chain_entry() stamps sequence and
-- prev_hash and derives entry_hash, and /audit/integrity replays that
-- chain. See src/sub_agent/audit_integrity.py.

ALTER TABLE audit_events
    ADD COLUMN IF NOT EXISTS sequence   integer,
    ADD COLUMN IF NOT EXISTS prev_hash  text,
    ADD COLUMN IF NOT EXISTS entry_hash text;

-- CORRECTION (second review pass): this previously proposed
-- UNIQUE (case_id, sequence), a PER-CASE chain. That is wrong -
-- verify_chain asserts sequence == index over the COMPLETE log, so a
-- per-case chain reports SEQUENCE_BREAK on an intact log.
--
-- The chain is GLOBAL, contiguous and zero-based. The index below
-- enforces no-duplicate-position; it does NOT by itself prevent GAPS,
-- and verify_chain treats a gap as a violation. Allocation and insert
-- must therefore share one transaction - which PostgREST cannot
-- provide. DO NOT ENABLE THIS UNTIL D4 AND D6 ARE BOTH SETTLED.
CREATE UNIQUE INDEX IF NOT EXISTS audit_events_sequence_uq
    ON audit_events (sequence)
    WHERE sequence IS NOT NULL;

-- Append-only, enforced twice: a trigger (so a privilege
-- misconfiguration alone cannot permit a mutation) and, separately,
-- REVOKE on the application role.
CREATE OR REPLACE FUNCTION uc01_audit_events_append_only()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

-- NOTE FOR REVIEW: these triggers apply to EVERY row in audit_events,
-- including other use cases'. Confirm no current writer updates or
-- deletes audit rows before enabling them.
--
-- TRUNCATE GAP: a row-level BEFORE DELETE trigger does NOT fire on
-- TRUNCATE, so the pair below would still allow the entire audit table
-- to be emptied in one statement by anyone holding TRUNCATE rights. A
-- statement-level truncate trigger is required as well:
--
-- CREATE TRIGGER audit_events_no_truncate
--     BEFORE TRUNCATE ON audit_events
--     FOR EACH STATEMENT EXECUTE FUNCTION uc01_audit_events_append_only();
--
-- Note also that triggers do not constrain a superuser or the table
-- owner, and that verify_chain cannot detect truncation of the most
-- recent entries (its own docstring says so) - a shortened chain is
-- internally valid. Detecting that needs a high-water mark held
-- OUTSIDE the table.
-- DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events;
-- CREATE TRIGGER audit_events_no_update
--     BEFORE UPDATE ON audit_events
--     FOR EACH ROW EXECUTE FUNCTION uc01_audit_events_append_only();
-- DROP TRIGGER IF EXISTS audit_events_no_delete ON audit_events;
-- CREATE TRIGGER audit_events_no_delete
--     BEFORE DELETE ON audit_events
--     FOR EACH ROW EXECUTE FUNCTION uc01_audit_events_append_only();
-- REVOKE UPDATE, DELETE ON audit_events FROM uc01_app;


-- ---------------------------------------------------------------- D5
-- Atomic employee-number allocation.
--
-- The read-modify-write the in-memory generator does is unsafe over
-- PostgREST: no transaction, no row lock, so two concurrent requests
-- can mint the SAME number. UPDATE ... RETURNING is a single statement
-- that takes a row lock, so it is atomic even over PostgREST.
--
-- It also removes a duplicated rule: prefix/padding live in the table,
-- so the Python 'EMP-%06d' literal is deleted rather than kept as a
-- second source of truth.

-- REVISED on review (findings F8/F14). Three changes from the first
-- draft of this function:
--
--  1. SECURITY DEFINER + a pinned search_path. As a plain VOLATILE sql
--     function it ran as the CALLER, so uc01_app still needed direct
--     UPDATE on employee_number_counters - which defeats the point of
--     routing allocation through a function. As SECURITY DEFINER the
--     privilege lives with the function owner and uc01_app can be left
--     with NO write privilege on the counter table at all.
--     `SET search_path = pg_catalog, public` is mandatory for any
--     SECURITY DEFINER function: without it a caller can prepend a
--     schema and hijack the unqualified names inside the body.
--
--  2. CALLER AUTHORIZATION. The tenant arrives as a parameter, so
--     without a check any principal able to execute this could mint
--     numbers for ANY client. It now asserts the requested client is
--     one the session is scoped to, using the same GUC the RLS policies
--     use (see the RLS block in uc01_draft.sql). This is also why the
--     function must NOT be exposed as a PostgREST RPC to anon.
--
--  3. UNSEEDED CLIENT IS AN ERROR, not NULL. Previously an unknown
--     client matched no row, RETURNING produced nothing, and the
--     function returned NULL - which an adapter could mistake for "no
--     number yet" and carry forward as a missing employee_no.
--
-- Concurrency is unchanged and correct: UPDATE ... RETURNING takes a
-- row lock and returns POST-update values, so a second caller blocks
-- and then re-reads under READ COMMITTED. Different clients touch
-- different rows and never contend.
CREATE OR REPLACE FUNCTION next_employee_no(p_client_id text)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_allowed text[];
    v_result  text;
BEGIN
    v_allowed := CASE
        WHEN coalesce(current_setting('app.client_ids', true), '') = '' THEN ARRAY[]::text[]
        ELSE string_to_array(current_setting('app.client_ids', true), ',')
    END;

    -- Fail closed: an un-scoped session allocates nothing.
    IF NOT (p_client_id = ANY (v_allowed)) THEN
        RAISE EXCEPTION
            'next_employee_no: session is not scoped to client %', p_client_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    UPDATE employee_number_counters
       SET last_value = last_value + 1,
           updated_at = now()
     WHERE client_id = p_client_id
    RETURNING prefix || lpad(last_value::text, padding, '0')
      INTO v_result;

    IF v_result IS NULL THEN
        RAISE EXCEPTION
            'next_employee_no: no counter row for client % - seed employee_number_counters first',
            p_client_id
            USING ERRCODE = 'no_data_found';
    END IF;

    RETURN v_result;
END;
$$;

-- The function mints identifiers; it must not be callable by browsers.
REVOKE ALL ON FUNCTION next_employee_no(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION next_employee_no(text) FROM anon;
-- GRANT EXECUTE ON FUNCTION next_employee_no(text) TO uc01_app;
--
-- With SECURITY DEFINER in place, uc01_app needs NO direct write on the
-- counter table. Revoke it so the function is the only allocation path:
-- REVOKE INSERT, UPDATE, DELETE ON employee_number_counters FROM uc01_app;
--
-- The function owner must NOT be a superuser (a SECURITY DEFINER
-- function runs with the owner's rights). Give it to the same
-- non-superuser DDL/migration role that owns the tables.

-- Seeding is a prerequisite, not optional - an unseeded client now
-- raises rather than returning NULL:
-- INSERT INTO employee_number_counters (client_id, last_value, prefix, padding)
-- SELECT client_id, 0, 'EMP-', 6 FROM client_configs
-- ON CONFLICT (client_id) DO NOTHING;


-- ---------------------------------------------------------------- D1/D2
-- APPROVED: role_id and manager_id are no longer mandatory for UC-01
-- onboarding-case creation.
--
-- Nothing changed in the application - UC-01 never produced either value
-- (zero references in src/, specs/, skills/ and the
-- POST /internal/onboarding-requests contract). The requirement exists
-- ONLY as a database constraint, so this is the whole change:
--
-- ALTER TABLE onboarding_cases
--     ALTER COLUMN role_id    DROP NOT NULL,
--     ALTER COLUMN manager_id DROP NOT NULL;
--
-- SCOPE CHECK BEFORE APPLYING. onboarding_cases is SHARED. A code search
-- found no other use case reading these two columns - UC-04 references
-- onboarding_cases only as a foreign-key target in its schema spec, not
-- for role_id/manager_id - but that is evidence of absence from the
-- repository, not proof. The DBA must confirm no non-UC-01 writer or
-- reporting query depends on them being non-null before this is applied.
--
-- ---------------------------------------------------------------- D3
-- NOT APPLIED - REQUIRES DBA REVIEW.
--
-- APPROVED 2026-11-04. Per-column outcome, implemented in the
-- application already (models/case.py CasePlacement):
--
--   department          optional  -> DROP NOT NULL
--   grade               optional  -> DROP NOT NULL
--   nationality_group   optional  -> DROP NOT NULL
--   project_name        optional  -> DROP NOT NULL
--   location_name       REQUIRED  -> unchanged, stays NOT NULL
--   current_stage       default 'INTAKE'
--   onboarding_attributes default empty
--
-- ALTER TABLE onboarding_cases
--     ALTER COLUMN department        DROP NOT NULL,
--     ALTER COLUMN grade             DROP NOT NULL,
--     ALTER COLUMN nationality_group DROP NOT NULL,
--     ALTER COLUMN project_name      DROP NOT NULL;
--
-- location_name is deliberately absent above: it stays NOT NULL, with
-- NO server-side default. A fallback value would silently place
-- employees at a location nobody chose, so the caller must supply it
-- and the application rejects missing/empty/whitespace input before any
-- insert.
--
-- current_stage already carries NOT NULL. The application always sends
-- a value ('INTAKE' when omitted), so a DB default is belt-and-braces
-- for other writers rather than something UC-01 relies on:
--
-- ALTER TABLE onboarding_cases
--     ALTER COLUMN current_stage SET DEFAULT 'INTAKE';
--
-- onboarding_attributes ALREADY has `NOT NULL DEFAULT '{}'` live, so
-- NO DDL is needed for it.
--
--   !! TYPE DISCREPANCY - DBA CONFIRMATION NEEDED !!
--   The approved wording says "empty JSON object {}", but the live
--   column is `text[]`, not jsonb. In Postgres `'{}'` is ALSO the
--   empty-ARRAY literal, so the existing default already produces an
--   empty value for a text[] column, and the application sends an empty
--   ARRAY to match. If a jsonb OBJECT was genuinely intended, the
--   column type must change - a shared-schema change affecting every
--   writer, which is NOT proposed here. Confirm which was meant.
--
-- SCOPE CHECK: relaxing NOT NULL affects every writer of this shared
-- table, not just UC-01. A reader that assumes a non-null department or
-- grade would start seeing NULLs. No such reader was found in the
-- repository, but that is not proof - the DBA must confirm.
--
-- NOT SUFFICIENT ON ITS OWN. Dropping these NOT NULLs does not make
-- case creation work: D4 (audit chain) and D6 (transactional
-- connection) still block ALL persistence, and the rehire columns below
-- are still missing. See docs/uc01-dba-security-decision-package.md.


-- ------------------------------------------------------- D3b (rehire, EC-43)
-- APPROVED: rehire support is in scope.
--
-- Rehire detection and handling already exist in the application and are
-- tested (duplicate_check.check_duplicates -> open_case.create_case ->
-- hris_prepare, which turns the proposal into an UPDATE of the existing
-- employee rather than a second INSERT). What is missing is PERSISTENCE:
-- the live schema has nowhere to put the link, so the Supabase adapters
-- REFUSE those records rather than silently dropping the reference -
-- dropping it would risk creating a duplicate employee in the HRIS.
--
-- The four columns below are exactly what the existing model fields need.
-- All additive and nullable; no existing row or other use case changes.
--
-- ALTER TABLE onboarding_cases
--     ADD COLUMN IF NOT EXISTS reactivates_record_id    text,
--     ADD COLUMN IF NOT EXISTS reactivates_external_ref text;
--     -- Case.reactivates_record_id / .reactivates_external_ref:
--     -- which prior person record HR is being asked to reactivate.
--
-- ALTER TABLE person_records
--     ADD COLUMN IF NOT EXISTS hris_external_ref text;
--     -- PersonRecord.hris_external_ref: how the target HRIS already
--     -- identifies this person. Without it a rehire cannot be matched
--     -- back to the existing employee at all.
--
-- ALTER TABLE hris_sync_records
--     ADD COLUMN IF NOT EXISTS updates_external_ref text;
--     -- HrisRecord.updates_external_ref: what makes the HRIS write an
--     -- UPDATE of that employee instead of creating a second one. This
--     -- is the column that prevents duplicate employees.
--
-- NOTE: reactivates_record_id is text, matching the Python model, and is
-- deliberately NOT a foreign key to person_records.record_id (uuid) -
-- the types differ and a rehire proposal must survive even if the prior
-- record is later archived. Converting it to a typed FK is a separate
-- decision.
--
-- Until these are applied, the adapters keep refusing and rehire cases
-- cannot be persisted. The refusal is deliberate and tested; it is not a
-- silent skip.
