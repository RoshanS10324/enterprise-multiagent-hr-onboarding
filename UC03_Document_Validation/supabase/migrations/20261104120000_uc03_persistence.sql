-- UC-03 Document & Information Validation: persistence additions.
--
-- STATUS: PROPOSED, NOT APPLIED. Review before running. Apply with a person's approval, to an isolated or approved project
-- first. Never run against the shared project without the team's agreement.
--
-- ADDITIVE ONLY. Nothing is dropped, renamed or narrowed; no existing column, constraint or row of another use case
-- is changed. Safe to re-run (IF NOT EXISTS everywhere).
--
-- Why each part exists (checked read-only against the live schema on 2026-11-04):
--   1. uc03_case_state: UC-03 keeps its own per-case validation state (authorised actors, documents already received,
--      UC-03 status). No shared table holds it: onboarding_cases belongs to UC-01 and has no such columns. Linked to
--      onboarding_cases when the case id is a real onboarding case.
--   2. document_validations: the table exists (0 rows) but cannot hold several UC-03 result fields. They are added as
--      NULLABLE columns, so existing writers are unaffected. UC-03's exact enum values are kept where the shared CHECK
--      lists are narrower (the shared columns get the closest mapped value).
--   3. A unique request key on document_validations makes repeated identical requests idempotent.
--   4. Model output is never mixed with the decision: it goes to the separate `advisory` column.

BEGIN;

-- 1. UC-03 case state ------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS uc03_case_state (
    case_ref            text PRIMARY KEY,                                   -- UC-03's case id (the orchestrator's case id when orchestrated)
    onboarding_case_id  uuid REFERENCES onboarding_cases(case_id) ON DELETE SET NULL,  -- set only when case_ref is a real onboarding case
    client_id           text NOT NULL,
    employee_id         text NOT NULL,
    status              text NOT NULL,                                      -- UC-03 OnboardingStatus
    state               jsonb NOT NULL,                                     -- UC-03 OnboardingCase (authorised actors, received documents, ...)
    version             integer NOT NULL DEFAULT 1 CHECK (version >= 1),     -- optimistic concurrency
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_uc03_case_state_onboarding_case ON uc03_case_state (onboarding_case_id);
CREATE INDEX IF NOT EXISTS idx_uc03_case_state_client ON uc03_case_state (client_id);

-- Least privilege: RLS on, no policies. Only the service role (which bypasses RLS) can read or write; anon and
-- authenticated clients cannot. The state holds the name from the onboarding form.
ALTER TABLE uc03_case_state ENABLE ROW LEVEL SECURITY;

-- 2. Missing UC-03 result fields on document_validations (all nullable) ---------------------------------------------
ALTER TABLE document_validations ADD COLUMN IF NOT EXISTS uc03_case_ref          text;
ALTER TABLE document_validations ADD COLUMN IF NOT EXISTS request_key            text;   -- idempotency key (sha-256 of the request)
ALTER TABLE document_validations ADD COLUMN IF NOT EXISTS document_status        text;   -- UC-03 DocumentStatus
ALTER TABLE document_validations ADD COLUMN IF NOT EXISTS required_next_action   text;   -- UC-03 RequiredAction (HR referral, reminder, ...)
ALTER TABLE document_validations ADD COLUMN IF NOT EXISTS approval_status        text;   -- UC-03 ApprovalStatus
ALTER TABLE document_validations ADD COLUMN IF NOT EXISTS evidence_status        text;   -- UC-03 EvidenceStatus
ALTER TABLE document_validations ADD COLUMN IF NOT EXISTS uc03_compliance_status text;   -- exact UC-03 value (shared column holds the mapped one)
ALTER TABLE document_validations ADD COLUMN IF NOT EXISTS uc03_visa_pack_status  text;   -- exact UC-03 value
ALTER TABLE document_validations ADD COLUMN IF NOT EXISTS decision_detail        text;   -- UC-03's own summary line (no document content)
ALTER TABLE document_validations ADD COLUMN IF NOT EXISTS audit_reference        text;   -- UC-03 audit event id
ALTER TABLE document_validations ADD COLUMN IF NOT EXISTS advisory               jsonb;  -- NON-AUTHORITATIVE model metadata, never a decision

-- 3. Idempotency -------------------------------------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_document_validations_request_key
    ON document_validations (request_key) WHERE request_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_document_validations_uc03_case ON document_validations (uc03_case_ref);

COMMIT;

-- Rollback (manual, only if nothing has been written yet):
--   DROP INDEX IF EXISTS idx_document_validations_uc03_case, uq_document_validations_request_key;
--   ALTER TABLE document_validations DROP COLUMN IF EXISTS advisory, DROP COLUMN IF EXISTS audit_reference, ...;
--   DROP TABLE IF EXISTS uc03_case_state;
