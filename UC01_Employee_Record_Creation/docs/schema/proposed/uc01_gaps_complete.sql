-- ============================================================================
-- UC-01 gap closure for the shared Onboarding_Agent Supabase project.
-- Paste into: Supabase Dashboard -> SQL Editor -> Run.
--
-- Consolidates docs/schema/proposed/d4_d5_additive.sql plus the extra gaps
-- found on 2026-11-03. Idempotent (safe to re-run) and ADDITIVE: nothing is
-- dropped; the only loosening is DROP NOT NULL on UC-01-irrelevant columns of
-- onboarding_cases (decisions D1-D3, already approved).
--
-- Deliberately NOT included (see section 7): the audit append-only triggers
-- and the unique sequence index. They affect UC-02/UC-05 writers and the
-- index must not be enabled until REST inserts can be made gap-free.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------- 1. onboarding_cases (D1/D2/D3)
-- UC-01 never supplies these; they were NOT NULL with no default.
ALTER TABLE onboarding_cases
    ALTER COLUMN role_id           DROP NOT NULL,
    ALTER COLUMN manager_id        DROP NOT NULL,
    ALTER COLUMN department        DROP NOT NULL,
    ALTER COLUMN grade             DROP NOT NULL,
    ALTER COLUMN nationality_group DROP NOT NULL,
    ALTER COLUMN project_name      DROP NOT NULL;
-- location_name intentionally stays NOT NULL (caller must supply it).

ALTER TABLE onboarding_cases
    ALTER COLUMN current_stage SET DEFAULT 'INTAKE';

-- ---------------------------------------------------------------- 2. Rehire columns (D3b / EC-43)
ALTER TABLE onboarding_cases
    ADD COLUMN IF NOT EXISTS reactivates_record_id    text,
    ADD COLUMN IF NOT EXISTS reactivates_external_ref text;

ALTER TABLE person_records
    ADD COLUMN IF NOT EXISTS hris_external_ref text;

ALTER TABLE hris_sync_records
    ADD COLUMN IF NOT EXISTS updates_external_ref text;

-- ---------------------------------------------------------------- 3. Audit hash chain (D4)
-- Nullable, so UC-02/UC-05 rows (no chain) are untouched.
ALTER TABLE audit_events
    ADD COLUMN IF NOT EXISTS sequence   integer,
    ADD COLUMN IF NOT EXISTS prev_hash  text,
    ADD COLUMN IF NOT EXISTS entry_hash text;

-- ---------------------------------------------------------------- 4. UC-01 approvals (human checkpoints)
-- approval_requests is the orchestrator's gate, a different entity, so UC-01
-- gets its own table. Prefixed to avoid colliding with other use cases.
CREATE TABLE IF NOT EXISTS uc01_approvals (
    approval_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id       uuid NOT NULL REFERENCES onboarding_cases (case_id),
    checkpoint    text NOT NULL
        CHECK (checkpoint IN ('HC_01_FORM', 'HC_04_DATA', 'HC_05_HRIS_WRITE', 'HC_12_VARIANT')),
    subject_ref   text NOT NULL,
    requested_to  text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    requested_by  text,
    status        text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'APPROVED', 'EDITED_AND_APPROVED', 'REJECTED',
                          'EXPIRED', 'ESCALATED', 'CANCELLED')),
    decided_by    text,
    decided_at    timestamptz,
    reason        text,
    decision_note text
);

CREATE INDEX IF NOT EXISTS uc01_approvals_case_id_idx ON uc01_approvals (case_id);
CREATE INDEX IF NOT EXISTS uc01_approvals_pending_for_idx
    ON uc01_approvals (requested_to, status) WHERE status = 'PENDING';

-- A decided approval never changes again.
CREATE OR REPLACE FUNCTION uc01_enforce_approval_immutability()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'PENDING' THEN
        RAISE EXCEPTION 'uc01_approvals: row % is already decided (%) and cannot be updated',
            OLD.approval_id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS uc01_approvals_immutability ON uc01_approvals;
CREATE TRIGGER uc01_approvals_immutability
    BEFORE UPDATE ON uc01_approvals
    FOR EACH ROW EXECUTE FUNCTION uc01_enforce_approval_immutability();

-- RLS on with no policies: anon/authenticated get nothing; the service-role
-- key (used by UC-01's backend) bypasses RLS.
ALTER TABLE uc01_approvals ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------- 5. Atomic employee numbers (D5)
-- Single UPDATE ... RETURNING = row lock, so concurrent callers never get the
-- same number. prefix/padding live in the table (one source of truth).
-- Authorised when the session GUC app.client_ids lists the client, OR the
-- caller is the Supabase service_role (how UC-01's backend connects over REST).
CREATE OR REPLACE FUNCTION next_employee_no(p_client_id text)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_allowed text[];
    v_role    text;
    v_result  text;
BEGIN
    v_allowed := CASE
        WHEN coalesce(current_setting('app.client_ids', true), '') = '' THEN ARRAY[]::text[]
        ELSE string_to_array(current_setting('app.client_ids', true), ',')
    END;
    v_role := coalesce(
        nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role', '');

    IF NOT (p_client_id = ANY (v_allowed) OR v_role = 'service_role') THEN
        RAISE EXCEPTION 'next_employee_no: session is not scoped to client %', p_client_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    UPDATE employee_number_counters
       SET last_value = last_value + 1,
           updated_at = now()
     WHERE client_id = p_client_id
    RETURNING prefix || lpad(last_value::text, padding, '0') INTO v_result;

    IF v_result IS NULL THEN
        RAISE EXCEPTION 'next_employee_no: no counter row for client % - seed employee_number_counters first',
            p_client_id USING ERRCODE = 'no_data_found';
    END IF;

    RETURN v_result;
END;
$$;

REVOKE ALL ON FUNCTION next_employee_no(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION next_employee_no(text) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION next_employee_no(text) TO service_role;

-- Seed a counter for every existing client (no-op for ones already seeded).
INSERT INTO employee_number_counters (client_id, last_value, prefix, padding)
SELECT client_id, 0, 'EMP-', 6 FROM client_configs
ON CONFLICT (client_id) DO NOTHING;

COMMIT;

-- ---------------------------------------------------------------- 6. Verify (run after the block above)
-- SELECT column_name FROM information_schema.columns
--  WHERE table_name = 'audit_events' AND column_name IN ('sequence','prev_hash','entry_hash');   -- 3 rows
-- SELECT column_name, is_nullable FROM information_schema.columns
--  WHERE table_name = 'onboarding_cases' AND column_name IN ('role_id','manager_id','department','grade','nationality_group','project_name');  -- all YES
-- SELECT to_regclass('public.uc01_approvals');   -- uc01_approvals
-- SELECT proname FROM pg_proc WHERE proname = 'next_employee_no';

-- ---------------------------------------------------------------- 7. NOT applied (apply only after DBA sign-off)
-- Audit chain is GLOBAL and must be gap-free; REST inserts cannot guarantee that.
-- CREATE UNIQUE INDEX IF NOT EXISTS audit_events_sequence_uq
--     ON audit_events (sequence) WHERE sequence IS NOT NULL;
--
-- Append-only enforcement affects every use case writing audit_events:
-- CREATE OR REPLACE FUNCTION uc01_audit_events_append_only() RETURNS trigger AS $f$
-- BEGIN RAISE EXCEPTION 'audit_events is append-only: % is not permitted', TG_OP; END;
-- $f$ LANGUAGE plpgsql;
-- CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
--     FOR EACH ROW EXECUTE FUNCTION uc01_audit_events_append_only();
-- CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
--     FOR EACH ROW EXECUTE FUNCTION uc01_audit_events_append_only();
-- CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events
--     FOR EACH STATEMENT EXECUTE FUNCTION uc01_audit_events_append_only();
