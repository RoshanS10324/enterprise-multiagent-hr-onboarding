-- ============================================================================
-- UC-01: onboarding-form details, onboarding budget, source-document reference
--
--   *** PROPOSED - NOT APPLIED. Needs DBA review and approval. ***
--   Kept OUTSIDE supabase/migrations/ on purpose (see supabase/README.md):
--   the CLI must not be able to push unapproved DDL to the shared project.
--   After approval, copy it into the shared project's migration history
--   under this same timestamp and apply it through the normal process.
--
-- Why: the live schema (read-only check, 2026-11-05) has no column for what
-- the 2026 onboarding form states beyond name, designation, joining date and
-- salary (decision B9), and nothing for the onboarding BUDGET. UC-01 will not
-- put these into a free-text or JSON column of a shared table. This adds five
-- UC-01-owned tables, keyed by the form version they were read from:
--
--   uc01_onboarding_form_details        one row per onboarding_forms row
--   uc01_onboarding_form_field_status   per-field status (PRESENT/MISSING/INVALID/UNCERTAIN)
--   uc01_onboarding_document_checklist  documents checklist AS STATED (never validated - UC-03 does that)
--   uc01_onboarding_budget              budget totals per category, overall total, status
--   uc01_onboarding_budget_lines        each selected expense, with provenance and source
--
-- Supersedes the B9 section of uc01_b2_b4_b7_b9_draft.sql (same table names,
-- plus the budget, the source-document reference and the supplementary
-- employee details). Mapped by src/sub_agent/documents/supabase_store.py.
--
-- Design notes:
-- * Money is numeric(14,2) with an ISO-4217 currency (char(3)) next to every
--   amount. A total is only stored when every counted amount is in ONE
--   currency (or was converted with an explicit rate, recorded in
--   `conversion`). Reference rates (the template's printed price list) are
--   stored as reference only and are never counted.
-- * The salary is NOT here: it stays in onboarding_forms.salary_and_terms_encrypted.
-- * Parents' names and the mobile number are ONE AES-256-GCM ciphertext
--   (personal_details_encrypted), encrypted by UC-01 like the salary (AAD
--   "<form_id>:personal_details").
-- * Financial rows are immutable: an UPDATE that changes a value, and any
--   DELETE, are refused by trigger. A changed form is a new form version
--   (new form_id). An idempotent retry (same values) is allowed.
-- * Partial writes (decision B6 is open: PostgREST = one transaction per
--   statement). UC-01 writes each table in ONE statement with
--   INSERT ... ON CONFLICT DO NOTHING, in this order: details, field status,
--   checklist, budget lines, budget row LAST. The budget row is the completion
--   marker: a details row without it is an interrupted save, which UC-01's
--   next run completes (only missing rows are inserted, nothing is updated).
--   This is recovery, not atomicity; a SQL function per record (B6) would make
--   it atomic.
-- * One source file -> at most one details row per client (unique index on
--   client_id + source_document_sha256): a resubmitted document can never
--   create a second employee or a second budget.
-- * RLS: enabled and forced; read-only policy for `authenticated` scoped by
--   public.current_client_id() (exists in the dev project). UC-01's backend
--   uses the service role.
--
-- Additive only: no existing table, column, constraint or row is changed.
-- Rollback (only while the new tables hold no data you need):
--   DROP TABLE public.uc01_onboarding_budget_lines, public.uc01_onboarding_budget,
--              public.uc01_onboarding_document_checklist,
--              public.uc01_onboarding_form_field_status,
--              public.uc01_onboarding_form_details;
--   DROP FUNCTION public.uc01_refuse_changes();
-- ============================================================================


-- 0. READ-ONLY pre-checks. Review the output before section 1.
SELECT to_regclass('public.onboarding_forms')  AS onboarding_forms,
       to_regclass('public.onboarding_cases')  AS onboarding_cases,
       to_regclass('public.client_configs')    AS client_configs,
       to_regprocedure('public.current_client_id()') AS current_client_id,
       to_regclass('public.uc01_onboarding_form_details') AS already_present;   -- expected NULL


BEGIN;

-- 1. Immutability guard shared by the five tables --------------------------
CREATE OR REPLACE FUNCTION public.uc01_refuse_changes() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '% rows are not deleted (UC-01 keeps onboarding records; a change is a new form version)',
            TG_TABLE_NAME USING ERRCODE = 'check_violation';
    END IF;
    IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION '% rows are immutable once written (a change is a new form version)',
            TG_TABLE_NAME USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;  -- identical re-write (idempotent retry)
END;
$$;


-- 2. Details ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.uc01_onboarding_form_details (
    form_id                     uuid PRIMARY KEY REFERENCES public.onboarding_forms (form_id),
    client_id                   text NOT NULL REFERENCES public.client_configs (client_id),
    case_id                     uuid NOT NULL REFERENCES public.onboarding_cases (case_id),
    template_id                 text NOT NULL,
    source_document_sha256      text NOT NULL CHECK (source_document_sha256 ~ '^[0-9a-f]{64}$'),
    -- Where the source file is stored (Supabase Storage). Server-made path
    -- <client_id>/uc01-intake/<document_id>.<ext>; never the uploaded file name.
    source_document_id          uuid,
    source_bucket               text,
    source_object_path          text CHECK (source_object_path IS NULL OR source_object_path !~ '(^/|\.\.)'),
    source_size_bytes           bigint CHECK (source_size_bytes IS NULL OR source_size_bytes > 0),
    source_mime_type            text CHECK (source_mime_type IS NULL OR source_mime_type IN (
                                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                                    'application/pdf')),
    data_origin                 text NOT NULL CHECK (data_origin IN ('DOCUMENT', 'SYNTHETIC')),
    issuing_entity              text,
    reporting_to                text,
    probation_months            smallint CHECK (probation_months IN (3, 6)),
    visa_duration_years         smallint CHECK (visa_duration_years IN (1, 2)),
    visa_location_category      text CHECK (visa_location_category IN ('INSIDE_COUNTRY', 'OUTSIDE_COUNTRY')),
    visit_visa_entry_permit     text,
    direct_entry_permit         text,
    insurance_plan              text,
    employee_insurance          text,
    family_insurance            text,
    annual_employee_ticket      text,
    annual_family_ticket        text,
    hotel_stay_days             smallint CHECK (hotel_stay_days >= 0),
    hotel_budget_and_location   text,
    other_promises              text,
    roles_summary               text,
    marital_status              text CHECK (marital_status IN ('SINGLE', 'MARRIED', 'DIVORCED', 'WIDOWED')),
    -- Rows the template does not print, as stated on the form (supplementary table).
    nationality                 text,
    employment_duration         text,
    work_location_on_form       text,
    personal_details_encrypted  bytea,
    responsibilities            text[] NOT NULL DEFAULT '{}',
    responsibilities_is_example boolean NOT NULL DEFAULT false,
    review_confirmed_by         text,
    review_confirmed_at         timestamptz,
    completeness                numeric(4,3) NOT NULL CHECK (completeness BETWEEN 0 AND 1),
    created_at                  timestamptz NOT NULL DEFAULT now(),
    CHECK ((review_confirmed_by IS NULL) = (review_confirmed_at IS NULL)),
    CHECK ((source_document_id IS NULL) = (source_object_path IS NULL)
           AND (source_object_path IS NULL) = (source_bucket IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uc01_form_details_one_per_document
    ON public.uc01_onboarding_form_details (client_id, source_document_sha256);
CREATE INDEX IF NOT EXISTS uc01_form_details_case_idx ON public.uc01_onboarding_form_details (case_id);

CREATE TABLE IF NOT EXISTS public.uc01_onboarding_form_field_status (
    form_id     uuid NOT NULL REFERENCES public.uc01_onboarding_form_details (form_id),
    field_key   text NOT NULL CHECK (field_key ~ '^[a-z_]+\.[a-z_]+$'),
    status      text NOT NULL CHECK (status IN ('PRESENT', 'MISSING', 'INVALID', 'UNCERTAIN')),
    source_ref  text,
    note        text,
    PRIMARY KEY (form_id, field_key)
);

CREATE TABLE IF NOT EXISTS public.uc01_onboarding_document_checklist (
    form_id     uuid NOT NULL REFERENCES public.uc01_onboarding_form_details (form_id),
    item        text NOT NULL CHECK (item IN ('passport', 'passport_cover', 'visa_copy', 'cancellation_paper',
                                              'emirates_id', 'signed_offer_letter', 'photograph',
                                              'degree_certificate')),
    -- As stated on the form. UC-01 never validates a document (UC-03 does).
    status      text NOT NULL CHECK (status IN ('RECEIVED', 'PENDING', 'MISSING', 'INVALID', 'UNCERTAIN')),
    source_ref  text,
    PRIMARY KEY (form_id, item)
);


-- 3. Budget ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.uc01_onboarding_budget (
    form_id                     uuid PRIMARY KEY REFERENCES public.uc01_onboarding_form_details (form_id),
    client_id                   text NOT NULL REFERENCES public.client_configs (client_id),
    case_id                     uuid NOT NULL REFERENCES public.onboarding_cases (case_id),
    budget_currency             char(3) CHECK (budget_currency ~ '^[A-Z]{3}$'),
    visa_budget_amount          numeric(14,2) CHECK (visa_budget_amount >= 0),
    insurance_budget_amount     numeric(14,2) CHECK (insurance_budget_amount >= 0),
    travel_budget_amount        numeric(14,2) CHECK (travel_budget_amount >= 0),
    accommodation_budget_amount numeric(14,2) CHECK (accommodation_budget_amount >= 0),
    other_expenses_amount       numeric(14,2) CHECK (other_expenses_amount >= 0),
    total_onboarding_budget     numeric(14,2) CHECK (total_onboarding_budget >= 0),
    budget_status               text NOT NULL CHECK (budget_status IN ('COMPLETE', 'PARTIAL', 'UNAVAILABLE')),
    status_reasons              text[] NOT NULL DEFAULT '{}',
    -- {"VISA": {"AED": "4800.00"}, ...}: per category and currency (amounts as strings).
    category_totals             jsonb NOT NULL DEFAULT '{}'::jsonb,
    totals_by_currency          jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Only when amounts in several currencies were converted with SUPPLIED rates.
    conversion                  jsonb,
    -- The template's printed price list for this form version. Reference only.
    reference_rates             jsonb NOT NULL DEFAULT '[]'::jsonb,
    reference_differences       text[] NOT NULL DEFAULT '{}',
    calculation_version         text NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    CHECK (total_onboarding_budget IS NULL OR budget_currency IS NOT NULL),
    CHECK (budget_status <> 'COMPLETE' OR (budget_currency IS NOT NULL AND total_onboarding_budget IS NOT NULL)),
    CHECK (budget_status <> 'UNAVAILABLE' OR total_onboarding_budget IS NULL)
);
CREATE INDEX IF NOT EXISTS uc01_budget_case_idx ON public.uc01_onboarding_budget (case_id);
CREATE INDEX IF NOT EXISTS uc01_budget_client_status_idx ON public.uc01_onboarding_budget (client_id, budget_status);

-- Lines reference the DETAILS row, not the budget row: UC-01 writes the lines
-- before the budget row, which is its completion marker (B6, see note above).
CREATE TABLE IF NOT EXISTS public.uc01_onboarding_budget_lines (
    form_id                 uuid NOT NULL REFERENCES public.uc01_onboarding_form_details (form_id),
    line_key                text NOT NULL CHECK (line_key ~ '^[a-z]+(\.[a-z0-9_]+)+$'),
    line_no                 smallint NOT NULL CHECK (line_no >= 0),
    category                text NOT NULL CHECK (category IN ('VISA', 'INSURANCE', 'TRAVEL', 'ACCOMMODATION', 'OTHER')),
    label                   text NOT NULL,
    provenance              text NOT NULL CHECK (provenance IN ('EXTRACTED', 'CALCULATED', 'NOT_APPLICABLE',
                                                                'INFERRED_NOT_APPLICABLE', 'MISSING', 'INVALID',
                                                                'UNCERTAIN')),
    counted                 boolean NOT NULL,
    amount                  numeric(14,2) CHECK (amount >= 0),
    currency                char(3) CHECK (currency ~ '^[A-Z]{3}$'),
    original_amount_text    text,
    source_ref              text,
    note                    text,
    reference_key           text,
    reference_amount        numeric(14,2) CHECK (reference_amount >= 0),
    reference_currency      char(3) CHECK (reference_currency ~ '^[A-Z]{3}$'),
    details                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (form_id, line_key),
    UNIQUE (form_id, line_no),
    CHECK ((amount IS NULL) = (currency IS NULL)),
    CHECK ((reference_amount IS NULL) = (reference_currency IS NULL)),
    -- Only an amount HR wrote (or one calculated from such amounts) is counted.
    CHECK (NOT counted OR (amount IS NOT NULL AND provenance IN ('EXTRACTED', 'CALCULATED')))
);


-- 4. Immutability triggers --------------------------------------------------
DO $t$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['uc01_onboarding_form_details', 'uc01_onboarding_form_field_status',
                             'uc01_onboarding_document_checklist', 'uc01_onboarding_budget',
                             'uc01_onboarding_budget_lines'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS uc01_refuse_changes ON public.%I', t);
        EXECUTE format('CREATE TRIGGER uc01_refuse_changes BEFORE UPDATE OR DELETE ON public.%I '
                       'FOR EACH ROW EXECUTE FUNCTION public.uc01_refuse_changes()', t);
    END LOOP;
END
$t$;


-- 5. Row-level security -----------------------------------------------------
ALTER TABLE public.uc01_onboarding_form_details       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.uc01_onboarding_form_details       FORCE ROW LEVEL SECURITY;
ALTER TABLE public.uc01_onboarding_form_field_status  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.uc01_onboarding_form_field_status  FORCE ROW LEVEL SECURITY;
ALTER TABLE public.uc01_onboarding_document_checklist ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.uc01_onboarding_document_checklist FORCE ROW LEVEL SECURITY;
ALTER TABLE public.uc01_onboarding_budget             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.uc01_onboarding_budget             FORCE ROW LEVEL SECURITY;
ALTER TABLE public.uc01_onboarding_budget_lines       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.uc01_onboarding_budget_lines       FORCE ROW LEVEL SECURITY;

CREATE POLICY uc01_form_details_client_read ON public.uc01_onboarding_form_details
    FOR SELECT TO authenticated USING (client_id = public.current_client_id());
CREATE POLICY uc01_budget_client_read ON public.uc01_onboarding_budget
    FOR SELECT TO authenticated USING (client_id = public.current_client_id());
CREATE POLICY uc01_field_status_client_read ON public.uc01_onboarding_form_field_status
    FOR SELECT TO authenticated USING (EXISTS (
        SELECT 1 FROM public.uc01_onboarding_form_details d
         WHERE d.form_id = uc01_onboarding_form_field_status.form_id AND d.client_id = public.current_client_id()));
CREATE POLICY uc01_checklist_client_read ON public.uc01_onboarding_document_checklist
    FOR SELECT TO authenticated USING (EXISTS (
        SELECT 1 FROM public.uc01_onboarding_form_details d
         WHERE d.form_id = uc01_onboarding_document_checklist.form_id AND d.client_id = public.current_client_id()));
CREATE POLICY uc01_budget_lines_client_read ON public.uc01_onboarding_budget_lines
    FOR SELECT TO authenticated USING (EXISTS (
        SELECT 1 FROM public.uc01_onboarding_budget b
         WHERE b.form_id = uc01_onboarding_budget_lines.form_id AND b.client_id = public.current_client_id()));

-- 6. Privileges. UC-01's backend uses service_role (bypasses RLS). Make its
--    rights explicit and minimal: read + insert only (no UPDATE/DELETE - writes
--    are insert-if-absent). Browsers (anon) get nothing; signed-in users read
--    through the client-scoped policies above.
DO $g$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['uc01_onboarding_form_details', 'uc01_onboarding_form_field_status',
                             'uc01_onboarding_document_checklist', 'uc01_onboarding_budget',
                             'uc01_onboarding_budget_lines'] LOOP
        EXECUTE format('REVOKE ALL ON public.%I FROM anon, authenticated', t);
        EXECUTE format('GRANT SELECT ON public.%I TO authenticated', t);
        EXECUTE format('GRANT SELECT, INSERT ON public.%I TO service_role', t);
    END LOOP;
END
$g$;

-- PostgREST must see the new tables.
NOTIFY pgrst, 'reload schema';

COMMIT;


-- 7. READ-ONLY verification after COMMIT (expected: 5 tables, RLS on and
--    forced, 5 triggers, service_role SELECT+INSERT on each).
SELECT c.relname, c.relrowsecurity AS rls, c.relforcerowsecurity AS rls_forced,
       has_table_privilege('service_role', c.oid, 'SELECT') AS sr_select,
       has_table_privilege('service_role', c.oid, 'INSERT') AS sr_insert,
       has_table_privilege('anon', c.oid, 'SELECT') AS anon_select
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relname LIKE 'uc01_onboarding_%' ORDER BY 1;
SELECT tgrelid::regclass AS table_name, tgname FROM pg_trigger
 WHERE tgname = 'uc01_refuse_changes' ORDER BY 1;
-- Then, from the UC-01 folder:  python scripts/uc01_document_live_run.py check --out output/after-migration


-- ============================================================================
-- OPTIONAL - NOT PART OF THIS PROPOSAL'S DEFAULT. Only if the DBA, UC-03 and
-- the UC-01 owner decide the onboarding form should ALSO be listed in the
-- shared candidate_documents table (today UC-01 keeps its source-file
-- reference on uc01_onboarding_form_details only).
-- Observed 2026-11-05 (zero-write probes): candidate_documents_document_type_check
-- accepts at least PASSPORT, OFFER_LETTER, VISA, NATIONAL_ID and rejects any
-- onboarding-form value; the status CHECK accepts UPLOADED, VALIDATED, REJECTED,
-- EXPIRED. Mapping the form onto OFFER_LETTER would be lossy and is NOT done.
-- Same additive pattern as b5_allow_uc01_case_statuses.sql:
--
-- DO $cd$
-- DECLARE v_def text; v_values text[];
-- BEGIN
--     SELECT pg_get_constraintdef(oid) INTO v_def FROM pg_constraint
--      WHERE conrelid = 'public.candidate_documents'::regclass
--        AND conname = 'candidate_documents_document_type_check';
--     IF v_def IS NULL THEN RAISE EXCEPTION 'constraint not found; nothing changed'; END IF;
--     SELECT array_agg(DISTINCT m[1] ORDER BY m[1]) INTO v_values
--       FROM regexp_matches(v_def, '''([^'']+)''', 'g') AS m;
--     IF NOT ('OFFER_LETTER' = ANY (v_values)) THEN
--         RAISE EXCEPTION 'unexpected definition (%); nothing changed', v_def; END IF;
--     v_values := v_values || ARRAY['ONBOARDING_FORM'];
--     EXECUTE 'ALTER TABLE public.candidate_documents DROP CONSTRAINT candidate_documents_document_type_check';
--     EXECUTE format('ALTER TABLE public.candidate_documents ADD CONSTRAINT candidate_documents_document_type_check '
--                    'CHECK (document_type = ANY (%L::text[]))', v_values);
-- END
-- $cd$;
-- ============================================================================
