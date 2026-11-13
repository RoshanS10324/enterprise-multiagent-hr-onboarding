-- ============================================================================
-- HR ONBOARDING AGENT SYSTEM — CONSOLIDATED ENTERPRISE DATABASE SCHEMA
-- ============================================================================
-- Target Database: PostgreSQL 15+ / Supabase
-- Architecture: Multi-Tenant, Role-Based Access, Append-Only Auditing,
--               Transactional Outbox, HITL Gateways, and LangGraph State Storage.
-- Modules Covered:
--   - Central Orchestrator (HR-Onboarding-Common)
--   - UC01: Employee Record Creation & HRIS Synchronization
--   - UC02: Onboarding Task Planning & Milestone Tracking
--   - UC03: Document Collection, OCR & Compliance Validation
--   - UC04: Equipment Provisioning & IT Access Management
--   - UC05: Orientation Scheduling, Buddy Allocation & Welcome Automation
--   - Cross-Cutting: Unified HITL Approvals, Audit Logging, and Agent Telemetry
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Target standard Supabase public schema (so tables appear directly in Supabase Table Editor & REST API)
SET search_path TO public;

-- Clean reset of all existing tables if re-running (avoids "relation already exists" errors)
DROP TABLE IF EXISTS
    domain_events_outbox,
    agent_runs,
    audit_events,
    approval_requests,
    joining_day_jobs,
    communications,
    session_participants,
    orientation_sessions,
    buddy_assignments,
    buddy_profiles,
    equipment_handovers,
    provisioning_tickets,
    provisioning_requests,
    role_access_matrix,
    document_validations,
    candidate_documents,
    plan_revisions,
    task_dependencies,
    onboarding_tasks,
    task_templates,
    hris_sync_records,
    intake_discrepancies,
    offer_forms,
    onboarding_forms,
    case_orchestration_state,
    onboarding_cases,
    pending_starts,
    offers,
    person_records,
    employee_number_counters,
    client_holidays,
    client_configs
CASCADE;

DROP SCHEMA IF EXISTS hr_onboarding CASCADE;

-- ============================================================================
-- DOMAIN 1: MULTI-TENANT CONFIGURATION & MASTER CASE MANAGEMENT
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. client_configs
-- Consolidated tenant configuration store covering checklist configurations,
-- working days, SLA parameters, and tenant policy rules.
-- Origin: UC01, UC02 (client_config_ref), UC04 (CaseConfig), UC05 (client_configs)
-- ----------------------------------------------------------------------------
CREATE TABLE client_configs (
    client_id               text PRIMARY KEY,
    company_name            text NOT NULL,
    config_hash             text NOT NULL,
    working_days            integer[] NOT NULL DEFAULT '{1,2,3,4,5}', -- 1=Mon, 7=Sun
    timezone                text NOT NULL DEFAULT 'Asia/Dubai',
    template_count          integer NOT NULL DEFAULT 0 CHECK (template_count >= 0),
    milestone_count         integer NOT NULL DEFAULT 0 CHECK (milestone_count >= 0),
    settings_payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_active               boolean NOT NULL DEFAULT true,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_client_configs_active ON client_configs (client_id) WHERE is_active = true;

-- ----------------------------------------------------------------------------
-- 2. client_holidays
-- Organization-wide holiday calendars used for SLA calculations and lead-times.
-- Origin: UC01 (client_holidays), UC04 (WorkCalendar), UC05 (calendar integration)
-- ----------------------------------------------------------------------------
CREATE TABLE client_holidays (
    client_id               text NOT NULL REFERENCES client_configs(client_id) ON DELETE CASCADE,
    holiday_date            date NOT NULL,
    name                    text NOT NULL DEFAULT 'Public Holiday',
    is_observed             boolean NOT NULL DEFAULT true,
    PRIMARY KEY (client_id, holiday_date)
);

CREATE INDEX idx_client_holidays_date ON client_holidays (client_id, holiday_date);

-- ----------------------------------------------------------------------------
-- 3. employee_number_counters
-- Atomic counters per tenant for deterministic employee number generation.
-- Origin: UC01 (employee_number_counters)
-- ----------------------------------------------------------------------------
CREATE TABLE employee_number_counters (
    client_id               text PRIMARY KEY REFERENCES client_configs(client_id) ON DELETE CASCADE,
    last_value              bigint NOT NULL DEFAULT 0 CHECK (last_value >= 0),
    prefix                  text NOT NULL DEFAULT 'EMP-',
    padding                 integer NOT NULL DEFAULT 6,
    updated_at              timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- 4. person_records
-- Identity deduplication registry tracking active candidates, past employees,
-- and current staff to avoid duplicate hires and support re-hires.
-- Origin: UC01 (person_records)
-- ----------------------------------------------------------------------------
CREATE TABLE person_records (
    record_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               text NOT NULL REFERENCES client_configs(client_id) ON DELETE CASCADE,
    email                   text NOT NULL, -- PII (Normalized lower-case)
    id_number               text,          -- National ID / Passport identifier
    full_name               text NOT NULL, -- PII (Normalized trimmed)
    kind                    text NOT NULL CHECK (kind IN ('OPEN_CASE', 'PAST_EMPLOYEE', 'CURRENT_EMPLOYEE')),
    case_id                 uuid,          -- Linked when kind = 'OPEN_CASE'
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_person_records_lookup ON person_records (client_id, lower(email));
CREATE INDEX idx_person_records_id_num ON person_records (client_id, id_number) WHERE id_number IS NOT NULL;
CREATE INDEX idx_person_records_name ON person_records (client_id, lower(full_name));

-- Prevent concurrent race conditions when opening multiple cases for the same email
CREATE UNIQUE INDEX uq_person_records_open_case
    ON person_records (client_id, lower(email))
    WHERE kind = 'OPEN_CASE';

-- ----------------------------------------------------------------------------
-- 5. offers
-- Formal pre-onboarding offer records captured from ATS or recruitment sources.
-- Origin: UC01 (offers)
-- ----------------------------------------------------------------------------
CREATE TABLE offers (
    client_id               text NOT NULL REFERENCES client_configs(client_id) ON DELETE CASCADE,
    offer_ref               text NOT NULL,
    candidate_email         text NOT NULL, -- PII
    candidate_name          text NOT NULL, -- PII
    designation             text NOT NULL,
    hr_officer_id           text NOT NULL,
    status                  text NOT NULL DEFAULT 'SENT' CHECK (status IN ('SENT', 'ACCEPTED', 'WITHDRAWN', 'EXPIRED')),
    accepted_at             timestamptz,
    offer_payload           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, offer_ref)
);

CREATE UNIQUE INDEX uq_offers_global_ref ON offers (offer_ref);
CREATE INDEX idx_offers_status ON offers (client_id, status);

-- ----------------------------------------------------------------------------
-- 6. pending_starts
-- Holding queue for accepted offers awaiting case creation or trigger date.
-- Origin: UC01 (pending_starts)
-- ----------------------------------------------------------------------------
CREATE TABLE pending_starts (
    pending_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               text NOT NULL,
    offer_ref               text NOT NULL,
    reasons                 text[] NOT NULL DEFAULT '{}',
    status                  text NOT NULL DEFAULT 'WAITING' CHECK (status IN ('WAITING', 'STARTED', 'CANCELLED')),
    created_at              timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (client_id, offer_ref) REFERENCES offers (client_id, offer_ref) ON DELETE CASCADE
);

CREATE INDEX idx_pending_starts_client ON pending_starts (client_id, status) WHERE status = 'WAITING';
CREATE UNIQUE INDEX uq_pending_starts_active_offer ON pending_starts (client_id, offer_ref) WHERE status = 'WAITING';

-- ----------------------------------------------------------------------------
-- 7. onboarding_cases
-- Consolidated Enterprise Master Case Table.
-- Unified across Orchestrator, UC01 (cases), UC02 (onboarding), UC03 (OnboardingCase),
-- UC04 (CaseRecord), and UC05 (records).
-- ----------------------------------------------------------------------------
CREATE TABLE onboarding_cases (
    case_id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               text NOT NULL REFERENCES client_configs(client_id) ON DELETE RESTRICT,
    employee_no             text, -- Generated unique employee number
    candidate_name          text NOT NULL, -- PII
    candidate_email         text NOT NULL, -- PII
    hr_officer_id           text NOT NULL,
    joining_date            date NOT NULL,
    work_location_type      text NOT NULL CHECK (work_location_type IN ('OFFICE', 'CLIENT_SITE', 'REMOTE', 'HYBRID')),
    location_name           text NOT NULL DEFAULT 'Headquarters',
    project_name            text NOT NULL DEFAULT 'Internal Operations',
    role_id                 text NOT NULL,
    grade                   text NOT NULL DEFAULT '',
    department              text NOT NULL DEFAULT 'General',
    manager_id              text NOT NULL,
    nationality_group       text NOT NULL DEFAULT '', -- Anonymized grouping (e.g. GCC, EXP_EXP, EXP_VIS)
    
    -- Candidate Employment Variants (Flattened for indexed filtering and fast business rules)
    variant_is_rehire             boolean NOT NULL DEFAULT false,
    variant_is_internal_transfer  boolean NOT NULL DEFAULT false,
    variant_is_agency_contractor  boolean NOT NULL DEFAULT false,
    variant_is_minor              boolean NOT NULL DEFAULT false,
    variant_is_resident           boolean NOT NULL DEFAULT false,
    variant_is_remote             boolean NOT NULL DEFAULT false,
    
    -- Global Lifecycle Status
    status                  text NOT NULL DEFAULT 'INTAKE_PENDING'
        CHECK (status IN (
            'INTAKE_PENDING', 'DOCUMENTS_PENDING', 'VALIDATION_IN_PROGRESS', 
            'PROVISIONING_ACTIVE', 'ORIENTATION_SCHEDULED', 'READY_FOR_DAY_ONE', 
            'COMPLETED', 'ON_HOLD', 'WITHDRAWN', 'CANCELLED'
        )),
    current_stage           text NOT NULL DEFAULT 'intake',
    current_form_version    integer NOT NULL DEFAULT 1 CHECK (current_form_version >= 1),
    folder_ref              text, -- Document repository bucket/directory pointer
    opened_at               timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    completed_at            timestamptz
);

CREATE INDEX idx_cases_client_id ON onboarding_cases (client_id);
CREATE INDEX idx_cases_hr_officer ON onboarding_cases (client_id, hr_officer_id);
CREATE INDEX idx_cases_status ON onboarding_cases (client_id, status);
CREATE INDEX idx_cases_joining_date ON onboarding_cases (client_id, joining_date);
CREATE UNIQUE INDEX uq_cases_client_employee_no ON onboarding_cases (client_id, employee_no) WHERE employee_no IS NOT NULL;

-- Wire circular/forward reference from person_records to onboarding_cases
ALTER TABLE person_records
    ADD CONSTRAINT fk_person_records_case
    FOREIGN KEY (case_id) REFERENCES onboarding_cases(case_id) ON DELETE SET NULL;

-- ----------------------------------------------------------------------------
-- 8. case_orchestration_state
-- Durable state snapshot for the Central LangGraph Orchestrator.
-- Origin: HR-Onboarding-Common (OnboardingCaseState)
-- ----------------------------------------------------------------------------
CREATE TABLE case_orchestration_state (
    case_id                 uuid PRIMARY KEY REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    client_id               text NOT NULL,
    correlation_id          text NOT NULL,
    workflow_status         text NOT NULL DEFAULT 'pending'
        CHECK (workflow_status IN ('pending', 'running', 'waiting_for_input', 'waiting_for_approval', 'completed', 'blocked', 'failed', 'retry_pending')),
    overall_status          text NOT NULL DEFAULT 'in_progress'
        CHECK (overall_status IN ('in_progress', 'at_risk', 'blocked', 'complete', 'failed')),
    
    -- Fine-grained per-stage and parallel branch state projections
    stage_statuses          jsonb NOT NULL DEFAULT '{}'::jsonb,
    branch_statuses         jsonb NOT NULL DEFAULT '{}'::jsonb,
    completed_stages        text[] NOT NULL DEFAULT '{}',
    pending_actions         jsonb NOT NULL DEFAULT '[]'::jsonb,
    blockers                jsonb NOT NULL DEFAULT '[]'::jsonb,
    stage_outcomes          jsonb NOT NULL DEFAULT '{}'::jsonb,
    retry_metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
    
    -- Checkpoint data & context projections
    verified_context        jsonb,
    orchestrator_context    jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error_code         text,
    last_error_detail       text,
    version                 integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_orch_state_workflow ON case_orchestration_state (workflow_status);
CREATE INDEX idx_orch_state_client ON case_orchestration_state (client_id);

-- ============================================================================
-- DOMAIN 2: EMPLOYEE INTAKE & HRIS SYNCHRONIZATION (UC01)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 9. onboarding_forms
-- Versioned onboarding intake form submissions with PII and encrypted compensation.
-- Origin: UC01 (onboarding_forms)
-- ----------------------------------------------------------------------------
CREATE TABLE onboarding_forms (
    form_id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               text NOT NULL REFERENCES client_configs(client_id) ON DELETE RESTRICT,
    case_id                 uuid REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    version                 integer NOT NULL CHECK (version >= 1),
    candidate_name          text NOT NULL, -- PII
    candidate_email         text NOT NULL, -- PII
    designation             text NOT NULL,
    joining_date            date NOT NULL,
    work_location_type      text NOT NULL CHECK (work_location_type IN ('OFFICE', 'CLIENT_SITE', 'REMOTE', 'HYBRID')),
    client_site_id          text,
    
    -- Encrypted sensitive salary & legal terms (PGP encrypted at application layer)
    salary_and_terms_encrypted bytea NOT NULL,
    
    status                  text NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT', 'AWAITING_APPROVAL', 'APPROVED', 'SUPERSEDED', 'REJECTED')),
    approved_by             text,
    approved_at             timestamptz,
    change_reason           text,
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uq_onboarding_forms_version ON onboarding_forms (case_id, version) WHERE case_id IS NOT NULL;
CREATE UNIQUE INDEX uq_onboarding_forms_one_approved ON onboarding_forms (case_id) WHERE status = 'APPROVED';
CREATE UNIQUE INDEX uq_onboarding_forms_one_awaiting ON onboarding_forms (case_id) WHERE status = 'AWAITING_APPROVAL';
CREATE INDEX idx_onboarding_forms_case ON onboarding_forms (case_id);

-- ----------------------------------------------------------------------------
-- 10. offer_forms
-- Bridge table linking ATS offers to candidate-submitted onboarding forms.
-- Origin: UC01 (offer_forms)
-- ----------------------------------------------------------------------------
CREATE TABLE offer_forms (
    client_id               text NOT NULL,
    offer_ref               text NOT NULL,
    form_id                 uuid NOT NULL REFERENCES onboarding_forms(form_id) ON DELETE CASCADE,
    created_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, offer_ref),
    FOREIGN KEY (client_id, offer_ref) REFERENCES offers (client_id, offer_ref) ON DELETE CASCADE
);

-- ----------------------------------------------------------------------------
-- 11. intake_discrepancies
-- Discrepancies detected between offer terms, candidate entries, and HR records.
-- Origin: UC01 (discrepancies) & Orchestrator (intake_mismatch)
-- ----------------------------------------------------------------------------
CREATE TABLE intake_discrepancies (
    discrepancy_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id                 uuid NOT NULL REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    field_name              text NOT NULL
        CHECK (field_name IN ('candidate_name', 'designation', 'joining_date', 'work_location_type', 'salary_and_terms')),
    value_form              text NOT NULL, -- [REDACTED] for salary
    value_other             text NOT NULL,
    other_source            text NOT NULL, -- e.g. 'offer_letter', 'hr_system'
    form_version            integer NOT NULL,
    other_fingerprint       text NOT NULL,
    status                  text NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'RESOLVED', 'ACCEPTED_AS_CHANGE', 'STALE')),
    decided_by              text,
    decided_at              timestamptz,
    decision_reason         text,
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_discrepancies_case ON intake_discrepancies (case_id);
CREATE UNIQUE INDEX uq_discrepancies_dedup 
    ON intake_discrepancies (case_id, field_name, other_source, other_fingerprint, form_version);

-- ----------------------------------------------------------------------------
-- 12. hris_sync_records
-- Staged and synchronized employee profile records for downstream HRIS.
-- Origin: UC01 (hris_records)
-- ----------------------------------------------------------------------------
CREATE TABLE hris_sync_records (
    record_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id                 uuid NOT NULL REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    client_id               text NOT NULL,
    employee_no             text,
    target_system           text NOT NULL CHECK (target_system IN ('ZOHO_PEOPLE', 'ZOHO_PAYROLL', 'WORKDAY', 'SAP_SF')),
    form_version            integer NOT NULL CHECK (form_version >= 1),
    prepared_payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
    sensitive_field_names   text[] NOT NULL DEFAULT '{}',
    external_ref            text,
    last_error              text,
    status                  text NOT NULL DEFAULT 'NOT_PREPARED'
        CHECK (status IN ('NOT_PREPARED', 'PREPARED', 'MISMATCH', 'AWAITING_APPROVAL', 'APPROVED', 'WRITTEN', 'BLOCKED', 'FAILED', 'STALE')),
    approved_by             text,
    approved_at             timestamptz,
    written_at              timestamptz,
    
    -- Immutable idempotency key preventing duplicate downstream HRIS writes
    idempotency_key         text GENERATED ALWAYS AS (case_id::text || ':' || target_system || ':' || form_version::text) STORED
);

CREATE INDEX idx_hris_sync_case ON hris_sync_records (case_id);
CREATE UNIQUE INDEX uq_hris_sync_idempotency ON hris_sync_records (idempotency_key);

-- ============================================================================
-- DOMAIN 3: ONBOARDING TASK PLANNING & MILESTONE TRACKING (UC02 & UC05 TASKS)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 13. task_templates
-- Master checklist templates catalog defining onboarding activities.
-- Origin: UC02 (ClientOnboardingConfig templates)
-- ----------------------------------------------------------------------------
CREATE TABLE task_templates (
    template_key            text NOT NULL,
    client_id               text NOT NULL REFERENCES client_configs(client_id) ON DELETE CASCADE,
    category                text NOT NULL CHECK (category IN (
        'hr_record', 'document_collection', 'it_provisioning', 'access_management',
        'workplace', 'orientation', 'policy_acknowledgement', 'payroll',
        'joining_confirmation', 'background_verification', 'visa', 'medical',
        'biometrics', 'insurance', 'travel', 'accommodation', 'food',
        'banking_letter', 'hr_system_activation'
    )),
    title                   text NOT NULL,
    title_ar                text NOT NULL DEFAULT '',
    owner_role              text NOT NULL CHECK (owner_role IN (
        'hr_ops', 'hr_lead', 'it_manager', 'facilities', 'finance',
        'manager', 'buddy', 'employee', 'system', 'hr_officer', 'hr_manager', 'pro'
    )),
    default_lead_days       integer NOT NULL DEFAULT 0, -- Relative to joining date
    evidence_required       boolean NOT NULL DEFAULT false,
    requires_attributes     text[] NOT NULL DEFAULT '{}',
    sensitive_to            text[] NOT NULL DEFAULT '{}',
    approval_role           text,
    PRIMARY KEY (client_id, template_key)
);

-- ----------------------------------------------------------------------------
-- 14. onboarding_tasks
-- Consolidated Task Execution Instance Table.
-- Combines UC02 checklist tasks with UC05 induction & orientation tasks.
-- ----------------------------------------------------------------------------
CREATE TABLE onboarding_tasks (
    task_id                 text PRIMARY KEY, -- e.g. <onboarding_id>#<template_key>
    case_id                 uuid NOT NULL REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    client_id               text NOT NULL,
    employee_id             text NOT NULL,
    template_key            text NOT NULL,
    plan_order              integer NOT NULL DEFAULT 0 CHECK (plan_order >= 0),
    category                text NOT NULL,
    title                   text NOT NULL,
    title_ar                text NOT NULL DEFAULT '',
    owner_role              text NOT NULL,
    assigned_actor_id       text,
    status                  text NOT NULL DEFAULT 'blocked'
        CHECK (status IN ('blocked', 'ready', 'in_progress', 'complete', 'cancelled')),
    due_date                date NOT NULL,
    started_at              timestamptz,
    completed_at            timestamptz,
    cancelled_at            timestamptz,
    evidence_required       boolean NOT NULL DEFAULT false,
    evidence_ref            text,
    evidence_recorded_at    timestamptz,
    approval_role           text,
    notes                   text NOT NULL DEFAULT '',
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    
    CONSTRAINT uq_task_template_per_case UNIQUE (case_id, template_key),
    CONSTRAINT chk_task_evidence_on_complete CHECK (
        status <> 'complete' OR evidence_required = false OR (evidence_ref IS NOT NULL AND length(btrim(evidence_ref)) > 0)
    ),
    CONSTRAINT chk_task_complete_timestamp CHECK (status <> 'complete' OR completed_at IS NOT NULL),
    CONSTRAINT chk_task_cancelled_timestamp CHECK (status <> 'cancelled' OR cancelled_at IS NOT NULL)
);

CREATE INDEX idx_tasks_case ON onboarding_tasks (case_id);
CREATE INDEX idx_tasks_client_due ON onboarding_tasks (client_id, due_date) WHERE status IN ('blocked', 'ready', 'in_progress');
CREATE INDEX idx_tasks_owner_status ON onboarding_tasks (client_id, owner_role, status);

-- ----------------------------------------------------------------------------
-- 15. task_dependencies
-- Normalized Directed Acyclic Graph (DAG) edges representing prerequisite tasks.
-- Origin: UC02 (task_dependency)
-- ----------------------------------------------------------------------------
CREATE TABLE task_dependencies (
    task_id                 text NOT NULL REFERENCES onboarding_tasks(task_id) ON DELETE CASCADE,
    depends_on_task_id      text NOT NULL REFERENCES onboarding_tasks(task_id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_task_id),
    CONSTRAINT chk_no_self_dependency CHECK (task_id <> depends_on_task_id)
);

CREATE INDEX idx_task_deps_reverse ON task_dependencies (depends_on_task_id);

-- ----------------------------------------------------------------------------
-- 16. plan_revisions
-- Audit trail for dynamic task plan recalculations when joining dates or scopes shift.
-- Origin: UC02 (plan_revision)
-- ----------------------------------------------------------------------------
CREATE TABLE plan_revisions (
    revision_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id                 uuid NOT NULL REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    revision                integer NOT NULL CHECK (revision >= 0),
    client_id               text NOT NULL,
    drivers                 text[] NOT NULL DEFAULT '{}', -- e.g. ['joining_date', 'location']
    added_task_ids          text[] NOT NULL DEFAULT '{}',
    removed_task_ids        text[] NOT NULL DEFAULT '{}',
    rescheduled_task_ids    text[] NOT NULL DEFAULT '{}',
    retained_task_ids       text[] NOT NULL DEFAULT '{}',
    approval_request_id     text,
    applied_by              text NOT NULL,
    applied_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_plan_revision_case UNIQUE (case_id, revision)
);

-- ============================================================================
-- DOMAIN 4: DOCUMENT VALIDATION & COMPLIANCE VERIFICATION (UC03)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 17. candidate_documents
-- Metadata repository for candidate-uploaded identity and legal documents.
-- Origin: UC03 & Orchestrator (submitted_documents)
-- ----------------------------------------------------------------------------
CREATE TABLE candidate_documents (
    document_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id                 uuid NOT NULL REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    client_id               text NOT NULL,
    document_type           text NOT NULL CHECK (document_type IN (
        'PASSPORT', 'NATIONAL_ID', 'VISA', 'EDUCATIONAL_CERTIFICATE',
        'OFFER_LETTER', 'MEDICAL_FITNESS', 'BACKGROUND_CHECK', 'SIGNED_POLICY'
    )),
    document_ref            text NOT NULL, -- Storage bucket object key
    file_name               text NOT NULL,
    file_size_bytes         bigint NOT NULL CHECK (file_size_bytes > 0),
    mime_type               text NOT NULL,
    checksum_sha256         text NOT NULL,
    status                  text NOT NULL DEFAULT 'UPLOADED'
        CHECK (status IN ('UPLOADED', 'PARSING', 'VALIDATED', 'FAILED', 'REJECTED', 'EXPIRED')),
    uploaded_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_case_type_checksum UNIQUE (case_id, document_type, checksum_sha256)
);

CREATE INDEX idx_candidate_documents_case ON candidate_documents (case_id);

-- ----------------------------------------------------------------------------
-- 18. document_validations
-- Detailed OCR extractions, model parsing confidence, and business rule evaluations.
-- Origin: UC03 (DocumentValidationResult / validation_findings)
-- ----------------------------------------------------------------------------
CREATE TABLE document_validations (
    validation_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id             uuid NOT NULL REFERENCES candidate_documents(document_id) ON DELETE CASCADE,
    case_id                 uuid NOT NULL REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    extracted_name          text,
    extracted_expiry_date   date,
    is_readable             boolean NOT NULL DEFAULT true,
    required_fields_present boolean NOT NULL DEFAULT true,
    extraction_confidence   numeric(5,4) NOT NULL DEFAULT 1.0000 CHECK (extraction_confidence BETWEEN 0.0 AND 1.0),
    extracted_fields        jsonb NOT NULL DEFAULT '{}'::jsonb,
    validation_findings     text[] NOT NULL DEFAULT '{}', -- e.g. ['NAME_MISMATCH', 'EXPIRED']
    outstanding_requirements text[] NOT NULL DEFAULT '{}',
    compliance_item_status  text NOT NULL DEFAULT 'INCOMPLETE'
        CHECK (compliance_item_status IN ('COMPLIANT', 'INCOMPLETE', 'NON_COMPLIANT', 'WAIVED')),
    visa_pack_status        text NOT NULL DEFAULT 'NOT_APPLICABLE'
        CHECK (visa_pack_status IN ('NOT_APPLICABLE', 'READY', 'INCOMPLETE', 'BLOCKED')),
    hr_review_status        text NOT NULL DEFAULT 'PENDING'
        CHECK (hr_review_status IN ('NOT_REQUIRED', 'PENDING', 'ACCEPTED', 'REJECTED')),
    injection_detected      boolean NOT NULL DEFAULT false, -- Guardrail anti-tampering
    draft_communication     text,
    validated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_doc_validations_case ON document_validations (case_id);
CREATE INDEX idx_doc_validations_doc ON document_validations (document_id);

-- ============================================================================
-- DOMAIN 5: EQUIPMENT & SYSTEM ACCESS PROVISIONING (UC04)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 19. role_access_matrix
-- Entitlement matrix determining mandatory hardware & software per role and client.
-- Origin: UC04 (role_access_matrix / role_equipment_matrix)
-- ----------------------------------------------------------------------------
CREATE TABLE role_access_matrix (
    matrix_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               text NOT NULL REFERENCES client_configs(client_id) ON DELETE CASCADE,
    role_id                 text NOT NULL,
    item_id                 text NOT NULL, -- e.g. 'laptop-macbook-pro', 'github-access', 'sap-finance'
    item_kind               text NOT NULL CHECK (item_kind IN ('HARDWARE', 'SOFTWARE_ACCESS', 'PHYSICAL_BADGE', 'SYSTEM_PERM')),
    is_mandatory            boolean NOT NULL DEFAULT true,
    lead_time_days          integer NOT NULL DEFAULT 5 CHECK (lead_time_days >= 0),
    approval_class          text NOT NULL DEFAULT 'STANDARD' CHECK (approval_class IN ('STANDARD', 'MANAGER_EXCEPTION', 'SECURITY_GATE')),
    created_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_role_access_matrix UNIQUE (client_id, role_id, item_id)
);

-- ----------------------------------------------------------------------------
-- 20. provisioning_requests
-- Tracked hardware and application access requests allocated to an employee.
-- Origin: UC04 (ItemRecord / RequestedItem)
-- ----------------------------------------------------------------------------
CREATE TABLE provisioning_requests (
    request_id              text PRIMARY KEY, -- e.g. 'SYN-REQ-001'
    case_id                 uuid NOT NULL REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    client_id               text NOT NULL,
    item_id                 text NOT NULL,
    item_kind               text NOT NULL CHECK (item_kind IN ('HARDWARE', 'SOFTWARE_ACCESS', 'PHYSICAL_BADGE', 'SYSTEM_PERM')),
    requested_by            text NOT NULL,
    client_site             boolean NOT NULL DEFAULT false,
    state                   text NOT NULL DEFAULT 'DRAFT_PREPARED'
        CHECK (state IN (
            'DRAFT_PREPARED', 'PENDING_APPROVAL', 'APPROVED', 'TICKET_CREATED',
            'PROVISIONING', 'DISPATCHED', 'DELIVERED', 'ACTIVE', 'CANCELLED', 'FAILED'
        )),
    ticket_id               text,
    clearance_status        text NOT NULL DEFAULT 'NOT_REQUIRED'
        CHECK (clearance_status IN ('NOT_REQUIRED', 'PENDING', 'CLEARED', 'REJECTED')),
    is_archived             boolean NOT NULL DEFAULT false, -- If client site is shifted
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_prov_requests_case ON provisioning_requests (case_id);
CREATE INDEX idx_prov_requests_state ON provisioning_requests (client_id, state);

-- ----------------------------------------------------------------------------
-- 21. provisioning_tickets
-- Downstream ITSM tickets (Jira Service Desk, ServiceNow) synchronized with agent.
-- Origin: UC04 (adapters/it_service_desk)
-- ----------------------------------------------------------------------------
CREATE TABLE provisioning_tickets (
    ticket_id               text PRIMARY KEY,
    request_id              text NOT NULL REFERENCES provisioning_requests(request_id) ON DELETE CASCADE,
    external_system         text NOT NULL DEFAULT 'SERVICE_DESK',
    ticket_status           text NOT NULL DEFAULT 'OPEN' CHECK (ticket_status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'CLOSED', 'CANCELLED')),
    assigned_technician     text,
    payload                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    resolved_at             timestamptz
);

-- ----------------------------------------------------------------------------
-- 22. equipment_handovers
-- Physical hardware delivery, tracking, and signoff logistics.
-- Origin: UC04 (HandoverRules, DeliveryStatus)
-- ----------------------------------------------------------------------------
CREATE TABLE equipment_handovers (
    handover_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id                 uuid NOT NULL REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    request_id              text NOT NULL REFERENCES provisioning_requests(request_id) ON DELETE CASCADE,
    mode                    text NOT NULL CHECK (mode IN ('DESK_COLLECTION', 'COURIER_DELIVERY', 'CLIENT_OFFICE')),
    tracking_number         text,
    courier_carrier         text,
    delivery_status         text NOT NULL DEFAULT 'SCHEDULED'
        CHECK (delivery_status IN ('SCHEDULED', 'IN_TRANSIT', 'OUT_FOR_DELIVERY', 'DELIVERED', 'COLLECTED', 'MISSED_SLOT')),
    scheduled_slot          timestamptz,
    recipient_signature_ref text,
    confirmed_at            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_handovers_case ON equipment_handovers (case_id);

-- ============================================================================
-- DOMAIN 6: ORIENTATION, SOCIALIZATION & SCHEDULING (UC05)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 23. buddy_profiles
-- Registered buddy pool members available for assignment.
-- Origin: UC05 (buddy_candidates)
-- ----------------------------------------------------------------------------
CREATE TABLE buddy_profiles (
    buddy_id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               text NOT NULL REFERENCES client_configs(client_id) ON DELETE CASCADE,
    employee_id             text NOT NULL,
    full_name               text NOT NULL,
    department              text NOT NULL,
    location                text NOT NULL,
    max_active_buddies      integer NOT NULL DEFAULT 2 CHECK (max_active_buddies >= 1),
    is_active               boolean NOT NULL DEFAULT true,
    profile_payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_buddy_client_employee UNIQUE (client_id, employee_id)
);

CREATE INDEX idx_buddy_profiles_dept ON buddy_profiles (client_id, department, is_active);

-- ----------------------------------------------------------------------------
-- 24. buddy_assignments
-- Formal pairing between an onboarding new hire and a workplace buddy.
-- Origin: UC05 & Orchestrator (buddy_assignment_result)
-- ----------------------------------------------------------------------------
CREATE TABLE buddy_assignments (
    assignment_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id                 uuid NOT NULL REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    client_id               text NOT NULL,
    buddy_employee_id       text NOT NULL,
    status                  text NOT NULL DEFAULT 'PROPOSED'
        CHECK (status IN ('PROPOSED', 'CONFIRMED', 'DECLINED', 'REPLACED', 'COMPLETED')),
    match_score             numeric(4,2) NOT NULL DEFAULT 1.00,
    matching_criteria       jsonb NOT NULL DEFAULT '{}'::jsonb,
    assigned_at             timestamptz NOT NULL DEFAULT now(),
    confirmed_at            timestamptz,
    CONSTRAINT uq_case_active_buddy UNIQUE (case_id)
);

CREATE INDEX idx_buddy_assignments_buddy ON buddy_assignments (client_id, buddy_employee_id, status);

-- ----------------------------------------------------------------------------
-- 25. orientation_sessions
-- Structured orientation, IT induction, and team welcome calendar meetings.
-- Origin: UC05 (sessions)
-- ----------------------------------------------------------------------------
CREATE TABLE orientation_sessions (
    session_id              text PRIMARY KEY, -- e.g. 'sess-001'
    case_id                 uuid NOT NULL REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    client_id               text NOT NULL,
    session_type            text NOT NULL CHECK (session_type IN (
        'HR_INDUCTION', 'IT_BOOTCAMP', 'BUDDY_SYNC', 'DEPARTMENT_WELCOME', 'LEADERSHIP_MEET'
    )),
    start_utc               timestamptz NOT NULL,
    end_utc                 timestamptz NOT NULL,
    meeting_link            text,
    room_location           text,
    status                  text NOT NULL DEFAULT 'SCHEDULED'
        CHECK (status IN ('SCHEDULED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED', 'RESCHEDULED')),
    calendar_event_id       text,
    session_metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_session_times CHECK (end_utc > start_utc)
);

CREATE INDEX idx_orientation_sessions_case ON orientation_sessions (case_id);
CREATE UNIQUE INDEX uq_orientation_sessions_live 
    ON orientation_sessions (case_id, session_type, start_utc) 
    WHERE status <> 'CANCELLED';

-- ----------------------------------------------------------------------------
-- 26. session_participants
-- Participants (Presenter, New Hire, HR, Manager) and their RSVP attendance status.
-- Origin: UC05 (participant_responses / invitations)
-- ----------------------------------------------------------------------------
CREATE TABLE session_participants (
    participant_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              text NOT NULL REFERENCES orientation_sessions(session_id) ON DELETE CASCADE,
    actor_id                text NOT NULL,
    email                   text NOT NULL,
    role                    text NOT NULL CHECK (role IN ('NEW_HIRE', 'HOST', 'PRESENTER', 'BUDDY', 'MANAGER', 'OBSERVER')),
    rsvp_status             text NOT NULL DEFAULT 'PENDING'
        CHECK (rsvp_status IN ('PENDING', 'ACCEPTED', 'TENTATIVE', 'DECLINED', 'DELEGATED')),
    proposed_new_time       timestamptz,
    response_note           text,
    responded_at            timestamptz,
    CONSTRAINT uq_session_participant UNIQUE (session_id, actor_id)
);

CREATE INDEX idx_session_participants_session ON session_participants (session_id);

-- ----------------------------------------------------------------------------
-- 27. communications
-- Consolidated communications queue covering invitation emails, welcome drafts,
-- Slack/Teams announcements, and policy notifications.
-- Origin: UC05 (notifications, drafts), UC03 (draft_communication), Orchestrator
-- ----------------------------------------------------------------------------
CREATE TABLE communications (
    comm_id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id                 uuid REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    client_id               text NOT NULL,
    comm_type               text NOT NULL CHECK (comm_type IN (
        'WELCOME_EMAIL', 'SESSION_INVITE', 'SLACK_INTRO', 'TEAMS_BROADCAST', 'SURVEY_PROMPT', 'URGENT_NOTICE'
    )),
    recipient_email         text NOT NULL,
    subject                 text NOT NULL,
    body_text               text NOT NULL,
    body_html               text,
    status                  text NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT', 'AWAITING_REVIEW', 'APPROVED', 'DISPATCHED', 'FAILED', 'CANCELLED')),
    review_gate_id          text,
    dispatched_at           timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_communications_case ON communications (case_id);
CREATE INDEX idx_communications_status ON communications (client_id, status);

-- ----------------------------------------------------------------------------
-- 28. joining_day_jobs
-- Scheduled automation batches executing Day-1 welcome and IT validation tasks.
-- Origin: UC05 (joining_day_jobs)
-- ----------------------------------------------------------------------------
CREATE TABLE joining_day_jobs (
    job_id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               text NOT NULL REFERENCES client_configs(client_id) ON DELETE CASCADE,
    run_date                date NOT NULL,
    status                  text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'SKIPPED')),
    executed_cases_count    integer NOT NULL DEFAULT 0 CHECK (executed_cases_count >= 0),
    job_payload             jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at              timestamptz,
    finished_at             timestamptz,
    CONSTRAINT uq_joining_day_jobs_client_date UNIQUE (client_id, run_date)
);

-- ============================================================================
-- DOMAIN 7: ENTERPRISE GOVERNANCE, SECURITY & AGENT TELEMETRY (PLATFORM)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 29. approval_requests
-- Unified Enterprise Human-in-the-Loop (HITL) Gateways.
-- Consolidates:
--   - UC01: approvals (HC_01_FORM, HC_04_DATA, HC_05_HRIS_WRITE, HC_12_VARIANT)
--   - UC02: approval_request (date_change, location_change, cancellation)
--   - UC03: ApprovalRecord (document_exception, name_mismatch)
--   - UC04: ApprovalRecord (matrix_exception, client_site_clearance)
--   - UC05: hitl.db (induction_schedule, buddy_confirmation, announcement_draft)
--   - Orchestrator: intake_mismatch_review, day_one_signoff
-- ----------------------------------------------------------------------------
CREATE TABLE approval_requests (
    request_id              text PRIMARY KEY, -- e.g. 'apr-1001' or UUID
    case_id                 uuid REFERENCES onboarding_cases(case_id) ON DELETE CASCADE,
    client_id               text NOT NULL REFERENCES client_configs(client_id) ON DELETE RESTRICT,
    gate                    text NOT NULL,
    approver_role           text NOT NULL,
    requested_by            text NOT NULL,
    workflow_module         text NOT NULL, -- e.g. 'orchestrator', 'uc01', 'uc02', 'uc03', 'uc04', 'uc05'
    subject_ref             text NOT NULL, -- e.g. 'form:<id>', 'task:<id>', 'session:<id>'
    payload                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    rationale               text NOT NULL DEFAULT '',
    confidence_score        numeric(4,2) NOT NULL DEFAULT 1.00,
    outcome                 text NOT NULL DEFAULT 'PENDING'
        CHECK (outcome IN ('PENDING', 'APPROVED', 'EDITED_AND_APPROVED', 'REJECTED', 'CANCELLED', 'EXPIRED')),
    decided_by              text,
    decided_at              timestamptz,
    decision_reason_code    text,
    decision_notes          text,
    final_payload           jsonb,
    apply_error             text, -- Captured if post-decision action fails (Finding S5)
    sla_due_at              timestamptz NOT NULL DEFAULT (now() + interval '24 hours'),
    created_at              timestamptz NOT NULL DEFAULT now(),
    
    -- Enforce segregation of duties: requester cannot approve their own item
    CONSTRAINT chk_no_self_approval CHECK (decided_by IS NULL OR decided_by <> requested_by),
    CONSTRAINT chk_decided_has_timestamp CHECK (outcome = 'PENDING' OR decided_at IS NOT NULL)
);

CREATE INDEX idx_approvals_pending_role ON approval_requests (client_id, approver_role, outcome) WHERE outcome = 'PENDING';
CREATE INDEX idx_approvals_case ON approval_requests (case_id);
CREATE INDEX idx_approvals_gate ON approval_requests (client_id, gate);

-- ----------------------------------------------------------------------------
-- 30. audit_events
-- Unified Append-Only Immutable Compliance Audit Log.
-- Consolidates:
--   - UC01: audit_log
--   - UC02: audit_event
--   - UC03: AuditEvent
--   - UC04/UC05: audit loggers
--   - HR-Onboarding-Common: platform/audit.py
-- ----------------------------------------------------------------------------
CREATE TABLE audit_events (
    event_id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id                 uuid REFERENCES onboarding_cases(case_id) ON DELETE SET NULL,
    client_id               text NOT NULL,
    actor_type              text NOT NULL CHECK (actor_type IN ('HUMAN', 'AGENT', 'SYSTEM')),
    actor_id                text NOT NULL,
    actor_role              text NOT NULL DEFAULT 'system',
    action                  text NOT NULL CHECK (length(btrim(action)) > 0),
    outcome                 text NOT NULL DEFAULT 'SUCCESS',
    detail                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    before_state            jsonb,
    after_state             jsonb,
    correlation_id          text,
    reference               text,
    occurred_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_case_time ON audit_events (case_id, occurred_at DESC);
CREATE INDEX idx_audit_client_time ON audit_events (client_id, occurred_at DESC);
CREATE INDEX idx_audit_action ON audit_events (client_id, action);

-- Trigger 1: Forbid mutation (Enforce strict append-only immutability)
CREATE OR REPLACE FUNCTION fn_audit_events_immutable()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is strictly append-only; % operation is forbidden', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_events_immutable
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION fn_audit_events_immutable();

-- Trigger 2: PII Leak Prevention Filter
-- Automatically blocks storage of forbidden personal identity strings in JSON details
CREATE OR REPLACE FUNCTION fn_audit_events_reject_pii()
RETURNS trigger AS $$
DECLARE
    leaked text;
BEGIN
    SELECT string_agg(k, ', ')
      INTO leaked
      FROM jsonb_object_keys(NEW.detail) AS k
     WHERE k IN ('passport', 'national_id', 'ssn', 'salary', 'bank_account', 'password', 'token');
    IF leaked IS NOT NULL THEN
        RAISE EXCEPTION 'Audit detail contains prohibited PII or credential keys: %', leaked
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_events_reject_pii
    BEFORE INSERT ON audit_events
    FOR EACH ROW EXECUTE FUNCTION fn_audit_events_reject_pii();

-- ----------------------------------------------------------------------------
-- 31. agent_runs
-- Telemetry and LLM observability for all autonomous agent and sub-agent invocations.
-- Origin: UC02 (agent_run) & HR-Onboarding-Common (api.py / BaseAgent)
-- ----------------------------------------------------------------------------
CREATE TABLE agent_runs (
    run_id                  text PRIMARY KEY,
    case_id                 uuid REFERENCES onboarding_cases(case_id) ON DELETE SET NULL,
    client_id               text NOT NULL,
    agent_id                text NOT NULL, -- e.g. 'onboarding-orchestrator', 'uc02-agent', 'uc05-agent'
    agent_version           text NOT NULL DEFAULT '1.0.0',
    actor_id                text NOT NULL,
    status                  text NOT NULL CHECK (status IN ('proposed', 'acted', 'exception')),
    advisor_mode            text NOT NULL DEFAULT 'offline',
    model_name              text, -- e.g. 'gemini-2.5-flash', 'claude-3-5-sonnet'
    prompt_tokens           integer NOT NULL DEFAULT 0,
    completion_tokens       integer NOT NULL DEFAULT 0,
    proposal_count          integer NOT NULL DEFAULT 0 CHECK (proposal_count >= 0),
    action_count            integer NOT NULL DEFAULT 0 CHECK (action_count >= 0),
    exception_count         integer NOT NULL DEFAULT 0 CHECK (exception_count >= 0),
    tool_calls              text[] NOT NULL DEFAULT '{}',
    error_summary           text,
    started_at              timestamptz NOT NULL DEFAULT now(),
    finished_at             timestamptz
);

CREATE INDEX idx_agent_runs_case ON agent_runs (case_id);
CREATE INDEX idx_agent_runs_client ON agent_runs (client_id, started_at DESC);

-- ----------------------------------------------------------------------------
-- 32. domain_events_outbox
-- Transactional Outbox Pattern for reliable asynchronous multi-agent messaging.
-- Origin: UC01 (domain_events_outbox) & Common Orchestrator
-- ----------------------------------------------------------------------------
CREATE TABLE domain_events_outbox (
    event_id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id                 uuid REFERENCES onboarding_cases(case_id) ON DELETE SET NULL,
    client_id               text NOT NULL,
    event_type              text NOT NULL CHECK (event_type IN (
        'CaseOpenedEvent', 'FormChangedEvent', 'DocumentsVerifiedEvent',
        'TasksPlannedEvent', 'EquipmentRequestedEvent', 'OrientationScheduledEvent',
        'DayOneReadinessAchievedEvent', 'OfferAcceptedEvent', 'OnboardingCompletedEvent'
    )),
    payload                 jsonb NOT NULL,
    occurred_at             timestamptz NOT NULL DEFAULT now(),
    published_at            timestamptz, -- NULL indicates pending relay to message broker
    publish_attempts        integer NOT NULL DEFAULT 0 CHECK (publish_attempts >= 0),
    last_error              text
);

CREATE INDEX idx_events_outbox_unprocessed ON domain_events_outbox (occurred_at) WHERE published_at IS NULL;
CREATE INDEX idx_events_outbox_case ON domain_events_outbox (case_id);

-- ============================================================================
-- COMMON TRIGGERS FOR TIMESTAMPS & LIFECYCLE
-- ============================================================================

CREATE OR REPLACE FUNCTION fn_touch_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_touch_cases_updated_at
    BEFORE UPDATE ON onboarding_cases
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

CREATE TRIGGER trg_touch_tasks_updated_at
    BEFORE UPDATE ON onboarding_tasks
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

CREATE TRIGGER trg_touch_orch_state_updated_at
    BEFORE UPDATE ON case_orchestration_state
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

CREATE TRIGGER trg_touch_prov_requests_updated_at
    BEFORE UPDATE ON provisioning_requests
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

CREATE TRIGGER trg_touch_orientation_updated_at
    BEFORE UPDATE ON orientation_sessions
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

-- ============================================================================
-- ROW LEVEL SECURITY (RLS) & TENANT ISOLATION POLICIES
-- ============================================================================

-- Function to extract client_id claim from JWT token or session setting
CREATE OR REPLACE FUNCTION current_client_id()
RETURNS text AS $$
    SELECT coalesce(
        nullif(current_setting('request.jwt.claims', true)::jsonb -> 'app_metadata' ->> 'client_id', ''),
        nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'client_id', ''),
        nullif(current_setting('app.current_client_id', true), '')
    );
$$ LANGUAGE sql STABLE;

-- Enable RLS across all multi-tenant tables
ALTER TABLE client_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_holidays ENABLE ROW LEVEL SECURITY;
ALTER TABLE employee_number_counters ENABLE ROW LEVEL SECURITY;
ALTER TABLE person_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE offers ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_starts ENABLE ROW LEVEL SECURITY;
ALTER TABLE onboarding_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_orchestration_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE onboarding_forms ENABLE ROW LEVEL SECURITY;
ALTER TABLE offer_forms ENABLE ROW LEVEL SECURITY;
ALTER TABLE intake_discrepancies ENABLE ROW LEVEL SECURITY;
ALTER TABLE hris_sync_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE onboarding_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE plan_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE candidate_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_validations ENABLE ROW LEVEL SECURITY;
ALTER TABLE role_access_matrix ENABLE ROW LEVEL SECURITY;
ALTER TABLE provisioning_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE equipment_handovers ENABLE ROW LEVEL SECURITY;
ALTER TABLE buddy_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE buddy_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE orientation_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE communications ENABLE ROW LEVEL SECURITY;
ALTER TABLE joining_day_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE domain_events_outbox ENABLE ROW LEVEL SECURITY;

-- Tenant isolation RLS policies (Fail closed if current_client_id() is null)
CREATE POLICY p_client_configs_isolation ON client_configs
    USING (client_id = current_client_id());

CREATE POLICY p_onboarding_cases_isolation ON onboarding_cases
    USING (client_id = current_client_id());

CREATE POLICY p_onboarding_tasks_isolation ON onboarding_tasks
    USING (client_id = current_client_id());

CREATE POLICY p_candidate_docs_isolation ON candidate_documents
    USING (client_id = current_client_id());

CREATE POLICY p_approval_requests_isolation ON approval_requests
    USING (client_id = current_client_id());

CREATE POLICY p_audit_events_select_isolation ON audit_events
    FOR SELECT USING (client_id = current_client_id());

-- ============================================================================
-- END OF CONSOLIDATED DATABASE SCHEMA
-- ============================================================================
