-- =====================================================================
-- UC-01 DRAFT DDL for DBA review - decisions B2, B4, B7, B9.
--
--   *** NOT APPLIED. NOT REVIEWED. DO NOT RUN AGAINST ANY PROJECT. ***
--
-- Written 2026-11-04 against the live dev schema as read through PostgREST
-- (read-only). It implements the RECOMMENDED option of each decision in
-- docs/uc01-supabase-decision-request.md so the DBA has something concrete
-- to approve, change or reject. Additive only: no DROP, no ALTER of an
-- existing column, no data change. Rollback for each section is the DROP
-- statement listed with it (safe only while the new objects hold no data).
--
-- Not covered here, on purpose:
--   B1 (key)        - not a schema change.
--   B3 (offers)     - needs the HR answer on where offer fields come from.
--   B5 (status)     - needs the existing CHECK constraint, if any.
--   B6 (atomicity)  - see the B2 note: the audit chain decides it.
--   B8 (client row) - shared configuration, created by its owner.
-- =====================================================================


-- ---------------------------------------------------------------------
-- B9 - onboarding-form details (extracted from template onboarding-form-2026)
--
-- Typed columns, not a JSON blob. Per-field extraction status and the
-- documents checklist are child tables, so "extracted / missing / invalid
-- / uncertain / reviewed" stays queryable and constrained.
-- Sensitive personal details (parents' names, mobile number) are ONE
-- AES-256-GCM ciphertext, encrypted by UC-01 exactly like
-- onboarding_forms.salary_and_terms_encrypted (AAD = form_id).
-- The salary itself stays in onboarding_forms; it is not duplicated here.
-- Rollback: DROP TABLE uc01_onboarding_document_checklist,
--           uc01_onboarding_form_field_status, uc01_onboarding_form_details;
-- ---------------------------------------------------------------------

create table if not exists public.uc01_onboarding_form_details (
    form_id                    uuid primary key references public.onboarding_forms (form_id),
    client_id                  text not null references public.client_configs (client_id),
    case_id                    uuid references public.onboarding_cases (case_id),
    template_id                text not null,
    source_document_sha256     text not null check (source_document_sha256 ~ '^[0-9a-f]{64}$'),
    reporting_to               text,
    probation_months           smallint check (probation_months in (3, 6)),
    visa_duration_years        smallint check (visa_duration_years in (1, 2)),
    visa_location_category     text check (visa_location_category in ('INSIDE_COUNTRY', 'OUTSIDE_COUNTRY')),
    visit_visa_entry_permit    text,
    direct_entry_permit        text,
    insurance_plan             text,
    employee_insurance         text,
    family_insurance           text,
    annual_employee_ticket     text,
    annual_family_ticket       text,
    hotel_stay_days            smallint check (hotel_stay_days >= 0),
    hotel_budget_and_location  text,
    other_promises             text,
    marital_status             text check (marital_status in ('SINGLE', 'MARRIED', 'DIVORCED', 'WIDOWED')),
    personal_details_encrypted bytea,
    responsibilities           text[] not null default '{}',
    responsibilities_is_example boolean not null default false,
    review_confirmed_by        text,
    review_confirmed_at        timestamptz,
    created_at                 timestamptz not null default now(),
    check ((review_confirmed_by is null) = (review_confirmed_at is null))
);
create index if not exists uc01_form_details_client_idx on public.uc01_onboarding_form_details (client_id);
create index if not exists uc01_form_details_case_idx on public.uc01_onboarding_form_details (case_id);

create table if not exists public.uc01_onboarding_form_field_status (
    form_id    uuid not null references public.uc01_onboarding_form_details (form_id),
    field_key  text not null check (field_key ~ '^[a-z_]+\.[a-z_]+$'),
    status     text not null check (status in ('PRESENT', 'MISSING', 'INVALID', 'UNCERTAIN')),
    source_ref text,
    note       text,
    primary key (form_id, field_key)
);

create table if not exists public.uc01_onboarding_document_checklist (
    form_id    uuid not null references public.uc01_onboarding_form_details (form_id),
    item       text not null check (item in ('passport', 'passport_cover', 'visa_copy', 'cancellation_paper',
                                             'emirates_id', 'signed_offer_letter', 'photograph',
                                             'degree_certificate')),
    -- Status as stated on the form. UC-01 never validates a document (UC-03 does).
    status     text not null check (status in ('RECEIVED', 'PENDING', 'UNCERTAIN', 'MISSING')),
    source_ref text,
    primary key (form_id, item)
);

-- Client isolation for any non-service-role reader. UC-01's backend uses the
-- service role (bypasses RLS), so these policies matter for every OTHER
-- reader; public.current_client_id() already exists in the dev project.
alter table public.uc01_onboarding_form_details enable row level security;
alter table public.uc01_onboarding_form_details force row level security;
create policy uc01_form_details_client_read on public.uc01_onboarding_form_details
    for select to authenticated using (client_id = public.current_client_id());

alter table public.uc01_onboarding_form_field_status enable row level security;
alter table public.uc01_onboarding_form_field_status force row level security;
create policy uc01_field_status_client_read on public.uc01_onboarding_form_field_status
    for select to authenticated using (exists (
        select 1 from public.uc01_onboarding_form_details d
         where d.form_id = uc01_onboarding_form_field_status.form_id
           and d.client_id = public.current_client_id()));

alter table public.uc01_onboarding_document_checklist enable row level security;
alter table public.uc01_onboarding_document_checklist force row level security;
create policy uc01_checklist_client_read on public.uc01_onboarding_document_checklist
    for select to authenticated using (exists (
        select 1 from public.uc01_onboarding_form_details d
         where d.form_id = uc01_onboarding_document_checklist.form_id
           and d.client_id = public.current_client_id()));


-- ---------------------------------------------------------------------
-- B2 - dedicated, append-only UC-01 audit log (option A)
--
-- Matches AuditEntry plus the hash-chain columns. client_id is NULLABLE:
-- request-level entries (e.g. START_GATE_EVALUATED) are written before any
-- case exists - whether UC-01 must carry a client on every entry is part of
-- the B2 decision.
--
-- COUPLING WITH B6: docs/schema/audit-chain-design.md (s.3) computes each
-- entry_hash in Python under pg_advisory_xact_lock, which needs a direct
-- connection (B6 option B, psycopg). Over PostgREST (B6 option A) the hash
-- would have to be computed inside a SQL function, re-implementing
-- audit_integrity.entry_digest's canonical JSON in SQL. B2 and B6 must be
-- decided together.
-- Rollback: DROP TABLE uc01_audit_log; DROP FUNCTION uc01_audit_log_append_only();
-- ---------------------------------------------------------------------

create table if not exists public.uc01_audit_log (
    sequence     bigint primary key check (sequence >= 0),
    entry_id     uuid not null unique,
    client_id    text references public.client_configs (client_id),
    case_id      uuid references public.onboarding_cases (case_id),
    request_id   text,
    actor_type   text not null check (actor_type in ('AGENT', 'HUMAN', 'SYSTEM')),
    actor_id     text not null,
    action       text not null,
    before_state jsonb,
    after_state  jsonb,
    reference    text,
    occurred_at  timestamptz not null,
    prev_hash    text not null check (prev_hash ~ '^[0-9a-f]{64}$'),
    entry_hash   text not null unique check (entry_hash ~ '^[0-9a-f]{64}$')
);
create index if not exists uc01_audit_log_case_idx on public.uc01_audit_log (case_id);

create or replace function public.uc01_audit_log_append_only() returns trigger
    language plpgsql as $$
begin
    raise exception 'uc01_audit_log is append-only (% refused)', tg_op;
end;
$$;
create trigger uc01_audit_log_no_update_delete
    before update or delete on public.uc01_audit_log
    for each row execute function public.uc01_audit_log_append_only();
create trigger uc01_audit_log_no_truncate
    before truncate on public.uc01_audit_log
    for each statement execute function public.uc01_audit_log_append_only();

alter table public.uc01_audit_log enable row level security;
alter table public.uc01_audit_log force row level security;
-- No policy for authenticated/anon: only the service role (UC-01 backend)
-- can read or append. Audit is verified through UC-01's AUDITOR endpoint.


-- ---------------------------------------------------------------------
-- B4 - private storage for source onboarding documents
--
-- Object path: <client_id>/<case_id>/<document_id>.docx  (all server-made;
-- never the uploaded file name, so no traversal and no PII in the path).
-- Metadata: the existing public.candidate_documents table
-- (document_ref = the object path, checksum_sha256, file_size_bytes,
-- mime_type, file_name). No public URL; downloads only through UC-01,
-- after its own authorisation, with the service role.
-- Rollback: delete from storage.buckets where id = 'uc01-onboarding-documents';
--           (only while it is empty)
-- ---------------------------------------------------------------------

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('uc01-onboarding-documents', 'uc01-onboarding-documents', false, 10485760,
        array['application/vnd.openxmlformats-officedocument.wordprocessingml.document'])
on conflict (id) do nothing;
-- Deliberately NO storage.objects policy for anon/authenticated: the bucket
-- is reachable only with the service role.


-- ---------------------------------------------------------------------
-- B7 - REFERENCE ONLY: a concurrency-safe next_employee_no, for comparison
-- with the live function (whose body PostgREST does not show). The single
-- UPDATE takes a row lock, so concurrent callers serialise and never
-- receive the same number; a client without a counter row gets NULL,
-- which UC-01's adapter refuses. Do NOT replace the live function with
-- this without the DBA's review - it is shared with other use cases.
-- ---------------------------------------------------------------------

-- create or replace function public.next_employee_no(p_client_id text) returns text
--     language sql as $$
--     update public.employee_number_counters
--        set last_value = last_value + 1, updated_at = now()
--      where client_id = p_client_id
--  returning prefix || lpad(last_value::text, padding, '0');
-- $$;
