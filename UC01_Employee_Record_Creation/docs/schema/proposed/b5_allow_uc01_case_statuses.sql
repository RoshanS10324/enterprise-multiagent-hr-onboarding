-- ============================================================================
-- B5 (case status vocabulary) - PROPOSED, NOT APPLIED. Needs DBA approval.
--
-- Observed on the dev project 2026-11-04 (zero-write probes: inserts with a
-- non-existent client_id, which CHECKs evaluate before the FK rejects them):
--   onboarding_cases_status_check ACCEPTS  INTAKE_PENDING, DOCUMENTS_PENDING,
--     ON_HOLD, WITHDRAWN, READY_FOR_DAY_ONE, COMPLETED, CANCELLED (at least)
--   and REJECTS UC-01's HELD, PLAN_PENDING, PLAN_APPROVED, IN_PROGRESS,
--     AT_RISK, ONBOARDED, CLOSED.
-- UC-01 therefore cannot create a case (its first status is PLAN_PENDING or
-- HELD). Mapping onto the shared values would be lossy (HELD and ON_HOLD mean
-- different things to UC-01), so this widens the CHECK instead.
--
-- ADDITIVE: every value the constraint allows today is read from its own
-- definition and kept; UC-01's ten statuses are added. Existing rows already
-- satisfy the old (narrower) rule, so they satisfy the new one. Nothing else
-- changes; no row is touched.
--
-- Run in the Supabase SQL editor (role postgres) after approval. Section 1 is
-- read-only - review its output first. Section 2 is one transaction.
-- ============================================================================

-- 1. READ-ONLY: the current definition.
SELECT conname, pg_get_constraintdef(oid) AS definition
  FROM pg_constraint
 WHERE conrelid = 'public.onboarding_cases'::regclass AND conname = 'onboarding_cases_status_check';

-- 2. Widen it (keeps every existing value).
BEGIN;
DO $b5$
DECLARE
    v_def     text;
    v_values  text[];
    v_uc01    text[] := ARRAY['HELD', 'PLAN_PENDING', 'PLAN_APPROVED', 'IN_PROGRESS', 'AT_RISK',
                              'READY_FOR_DAY_ONE', 'ONBOARDED', 'ON_HOLD', 'WITHDRAWN', 'CLOSED'];
BEGIN
    SELECT pg_get_constraintdef(oid) INTO v_def
      FROM pg_constraint
     WHERE conrelid = 'public.onboarding_cases'::regclass AND conname = 'onboarding_cases_status_check';
    IF v_def IS NULL THEN
        RAISE EXCEPTION 'onboarding_cases_status_check not found; nothing changed';
    END IF;
    -- Every quoted literal in the current definition is an allowed value.
    SELECT array_agg(DISTINCT m[1] ORDER BY m[1]) INTO v_values
      FROM regexp_matches(v_def, '''([^'']+)''', 'g') AS m;
    IF v_values IS NULL OR NOT ('INTAKE_PENDING' = ANY (v_values)) THEN
        RAISE EXCEPTION 'unexpected definition (%); nothing changed', v_def;
    END IF;
    SELECT array_agg(DISTINCT v ORDER BY v) INTO v_values FROM unnest(v_values || v_uc01) AS v;
    EXECUTE 'ALTER TABLE public.onboarding_cases DROP CONSTRAINT onboarding_cases_status_check';
    EXECUTE format('ALTER TABLE public.onboarding_cases ADD CONSTRAINT onboarding_cases_status_check '
                   'CHECK (status = ANY (%L::text[]))', v_values);
    RAISE NOTICE 'onboarding_cases_status_check now allows: %', v_values;
END
$b5$;
COMMIT;

-- 3. READ-ONLY verification after COMMIT. Expected: the definition lists every
--    previous value plus the ten UC-01 values. Re-running section 2 is safe
--    (it unions with what is already there).
SELECT pg_get_constraintdef(oid) AS definition
  FROM pg_constraint
 WHERE conrelid = 'public.onboarding_cases'::regclass AND conname = 'onboarding_cases_status_check';
-- Then: python scripts/uc01_document_live_run.py check --out output/after-b5
--       (probes all ten UC-01 statuses without writing; all must be accepted).

-- Re-checked 2026-11-05 (zero-write probes against every column UC-01 writes):
--   rejected : HELD, PLAN_PENDING, PLAN_APPROVED, IN_PROGRESS, AT_RISK, ONBOARDED, CLOSED
--   accepted : READY_FOR_DAY_ONE, ON_HOLD, WITHDRAWN
--   This is the ONLY live CHECK that rejects a UC-01 row: onboarding_cases.work_location_type
--   and current_stage, onboarding_forms.status/work_location_type, person_records.kind,
--   uc01_approvals.checkpoint and audit_events all accept UC-01's values.
