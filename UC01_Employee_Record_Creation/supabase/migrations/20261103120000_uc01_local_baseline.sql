-- ============================================================================
-- LOCAL TEST BASELINE - replica of the UC-01-relevant subset of the
-- Onboarding_Agent public schema.
--
-- !! DO NOT `supabase db push` THIS AT THE SHARED PROJECT !!
-- These tables already exist there. This file exists so `supabase start`
-- can stand up an equivalent schema locally, giving the integration
-- tests real Postgres + real PostgREST with zero risk to shared data.
--
-- FIDELITY CAVEAT: derived from the live PostgREST schema (table names,
-- column names, types, nullability, PKs, FKs) on 2026-11-03. PostgREST
-- does not expose CHECK constraints, indexes, triggers, defaults or RLS
-- policies, so those are NOT reproduced faithfully here - the CHECKs
-- below are UC-01's own expectations, not observed facts. Replace this
-- file with a real `pg_dump --schema-only` when one is available.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- FK target for most tables; only the column UC-01 depends on.
CREATE TABLE client_configs (
    client_id text PRIMARY KEY
);

CREATE TABLE onboarding_cases (
    case_id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id                    text NOT NULL REFERENCES client_configs (client_id),
    employee_no                  text,
    candidate_name               text NOT NULL,
    candidate_email              text NOT NULL,
    hr_officer_id                text NOT NULL,
    joining_date                 date NOT NULL,
    work_location_type           text NOT NULL,
    -- The nine NOT NULL columns UC-01 currently has no value for (D1-D3).
    location_name                text NOT NULL,
    project_name                 text NOT NULL,
    role_id                      text NOT NULL,
    grade                        text NOT NULL,
    department                   text NOT NULL,
    manager_id                   text NOT NULL,
    nationality_group            text NOT NULL,
    current_stage                text NOT NULL,
    onboarding_attributes        text[] NOT NULL DEFAULT '{}',
    -- NB: live column is variant_is_resident, NOT variant_is_uae_resident.
    variant_is_rehire            boolean NOT NULL DEFAULT false,
    variant_is_internal_transfer boolean NOT NULL DEFAULT false,
    variant_is_agency_contractor boolean NOT NULL DEFAULT false,
    variant_is_minor             boolean NOT NULL DEFAULT false,
    variant_is_resident          boolean NOT NULL DEFAULT false,
    variant_is_remote            boolean NOT NULL DEFAULT false,
    status                       text NOT NULL,
    current_form_version         integer NOT NULL,
    folder_ref                   text,
    opened_at                    timestamptz NOT NULL DEFAULT now(),
    updated_at                   timestamptz NOT NULL DEFAULT now(),
    completed_at                 timestamptz,
    is_deleted                   boolean NOT NULL DEFAULT false
);

CREATE TABLE onboarding_forms (
    form_id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id                  text NOT NULL REFERENCES client_configs (client_id),
    case_id                    uuid REFERENCES onboarding_cases (case_id),
    version                    integer NOT NULL,
    candidate_name             text NOT NULL,
    candidate_email            text NOT NULL,
    designation                text NOT NULL,
    joining_date               date NOT NULL,
    work_location_type         text NOT NULL,
    client_site_id             text,
    salary_and_terms_encrypted bytea NOT NULL,
    status                     text NOT NULL,
    approved_by                text,
    approved_at                timestamptz,
    change_reason              text,
    created_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE person_records (
    record_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id  text NOT NULL REFERENCES client_configs (client_id),
    email      text NOT NULL,
    id_number  text,
    full_name  text NOT NULL,
    kind       text NOT NULL,
    case_id    uuid REFERENCES onboarding_cases (case_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE intake_discrepancies (
    discrepancy_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id           uuid NOT NULL REFERENCES onboarding_cases (case_id),
    field_name        text NOT NULL,
    value_form        text NOT NULL,
    value_other       text NOT NULL,
    other_source      text NOT NULL,
    form_version      integer NOT NULL,
    other_fingerprint text NOT NULL,
    status            text NOT NULL,
    decided_by        text,
    decided_at        timestamptz,
    decision_reason   text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE hris_sync_records (
    record_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id               uuid NOT NULL REFERENCES onboarding_cases (case_id),
    client_id             text NOT NULL,
    employee_no           text,
    target_system         text NOT NULL,
    form_version          integer NOT NULL,
    prepared_payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    sensitive_field_names text[] NOT NULL DEFAULT '{}',
    external_ref          text,
    last_error            text,
    status                text NOT NULL,
    approved_by           text,
    approved_at           timestamptz,
    written_at            timestamptz,
    -- Live column is GENERATED ALWAYS (observed 2026-11-04 by behaviour,
    -- not metadata: a supplied value fails with SQLSTATE 428C9; an omitted
    -- one is generated as case_id:target_system:form_version; a repeat
    -- hits UNIQUE constraint uq_hris_sync_idempotency). The adapter never
    -- writes it. The exact live expression is inferred from its output.
    idempotency_key       text
        GENERATED ALWAYS AS (case_id::text || ':' || target_system || ':' || form_version::text) STORED,
    CONSTRAINT uq_hris_sync_idempotency UNIQUE (idempotency_key)
);

CREATE TABLE approval_requests (
    request_id           text PRIMARY KEY,
    case_id              uuid REFERENCES onboarding_cases (case_id),
    client_id            text NOT NULL REFERENCES client_configs (client_id),
    gate                 text NOT NULL,
    approver_role        text NOT NULL,
    requested_by         text NOT NULL,
    workflow_module      text NOT NULL,
    subject_ref          text NOT NULL,
    payload              jsonb NOT NULL,
    rationale            text NOT NULL,
    confidence_score     numeric NOT NULL,
    outcome              text NOT NULL,
    decided_by           text,
    decided_at           timestamptz,
    decision_reason_code text,
    decision_notes       text,
    final_payload        jsonb,
    apply_error          text,
    sla_due_at           timestamptz NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
    event_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id        uuid REFERENCES onboarding_cases (case_id),
    client_id      text NOT NULL,
    actor_type     text NOT NULL,
    actor_id       text NOT NULL,
    actor_role     text NOT NULL,
    action         text NOT NULL,
    outcome        text NOT NULL,
    detail         jsonb NOT NULL,
    before_state   jsonb,
    after_state    jsonb,
    correlation_id text,
    reference      text,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    task_id        text
    -- NOTE: no sequence / prev_hash / entry_hash. That is the live
    -- state, and it is exactly what blocks UC-01's audit log.
    -- Decision D4 adds them - see the 'proposed' migration.
);

CREATE TABLE offers (
    client_id       text NOT NULL REFERENCES client_configs (client_id),
    offer_ref       text NOT NULL,
    candidate_email text NOT NULL,
    candidate_name  text NOT NULL,
    designation     text NOT NULL,
    hr_officer_id   text NOT NULL,
    status          text NOT NULL,
    accepted_at     timestamptz,
    offer_payload   jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, offer_ref)
);

CREATE TABLE offer_forms (
    client_id  text NOT NULL,
    offer_ref  text NOT NULL,
    form_id    uuid NOT NULL REFERENCES onboarding_forms (form_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, offer_ref)
);

CREATE TABLE pending_starts (
    pending_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id  text NOT NULL,
    offer_ref  text NOT NULL,
    reasons    text[] NOT NULL DEFAULT '{}',
    status     text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE employee_number_counters (
    client_id  text PRIMARY KEY REFERENCES client_configs (client_id),
    last_value bigint NOT NULL DEFAULT 0,
    prefix     text NOT NULL,
    padding    integer NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE domain_events_outbox (
    event_id         uuid PRIMARY KEY,
    case_id          uuid REFERENCES onboarding_cases (case_id),
    client_id        text NOT NULL,
    event_type       text NOT NULL,
    payload          jsonb NOT NULL,
    occurred_at      timestamptz NOT NULL DEFAULT now(),
    published_at     timestamptz,
    publish_attempts integer NOT NULL DEFAULT 0,
    last_error       text
);

CREATE TABLE client_holidays (
    client_id    text NOT NULL REFERENCES client_configs (client_id),
    holiday_date date NOT NULL,
    name         text NOT NULL,
    is_observed  boolean NOT NULL DEFAULT true,
    PRIMARY KEY (client_id, holiday_date)
);

-- Minimal fixture so FKs resolve in local tests.
INSERT INTO client_configs (client_id) VALUES ('acme'), ('globex');
INSERT INTO employee_number_counters (client_id, last_value, prefix, padding)
VALUES ('acme', 0, 'EMP-', 6), ('globex', 0, 'EMP-', 6);
