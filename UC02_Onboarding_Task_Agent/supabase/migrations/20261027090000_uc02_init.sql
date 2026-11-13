-- UC-02 — Onboarding Task Management: initial schema.
--
-- Scope: the UC-02 *runtime* records only. Everything here maps 1:1 onto a
-- Pydantic contract in src/onboarding_task_management/contracts.py.
--
-- Deliberately NOT stored
-- -----------------------
-- * Milestones. MilestoneState is derived on demand from the tasks plus an
--   explicit as_of date (milestones.py). Storing it would create a second
--   source of truth that goes stale the moment a task completes or the plan is
--   re-planned.
-- * Overdue state. Derived from due_date against as_of (tracker.is_overdue).
-- * Client checklist configuration. ClientOnboardingConfig is code-owned and
--   validated at load time. Only a *reference* (client id + content hash) is
--   recorded, so a plan can be traced to the configuration that produced it
--   without duplicating the configuration itself.
--
-- Privacy (CLAUDE.md rules 1, 2 and 8)
-- ------------------------------------
-- No column holds a name, email, nationality, passport or contact detail.
-- employee_id, client_id, manager_id and actor_id are opaque identifiers.
-- nationality_group is an opaque GROUP CODE, never a nationality value.
-- Audit detail is a jsonb bag of ids/counts/outcomes; a trigger rejects
-- personal-data keys, mirroring audit.FORBIDDEN_DETAIL_KEYS.

create schema if not exists uc02;

comment on schema uc02 is
  'UC-02 Onboarding Task Management runtime records. Isolated from public so a '
  'shared Enterprise HR Agent database cannot collide with another use case.';

-- ---------------------------------------------------------------------------
-- Tenant resolution for Row Level Security.
--
-- Resolution order:
--   1. the `client_id` claim on the request JWT (Supabase/PostgREST path);
--   2. the `uc02.client_id` session GUC (server-side jobs, psql, tests).
-- Returns NULL when neither is set, which denies every row: fail closed.
-- ---------------------------------------------------------------------------
create or replace function uc02.current_client_id()
returns text
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'client_id', ''),
    nullif(current_setting('uc02.client_id', true), '')
  );
$$;

comment on function uc02.current_client_id() is
  'Acting client for RLS. NULL when unset, which denies all rows (fail closed).';


-- ---------------------------------------------------------------------------
-- Client configuration reference.
--
-- Not the configuration itself: the content hash of the ClientOnboardingConfig
-- that was in force. Lets an audit answer "which checklist definition produced
-- this plan?" without copying client content into the database.
-- ---------------------------------------------------------------------------
create table uc02.client_config_ref (
  client_id        text        not null,
  config_hash      text        not null,
  template_count   integer     not null check (template_count >= 0),
  milestone_count  integer     not null check (milestone_count >= 0),
  first_seen_at    timestamptz not null default now(),
  primary key (client_id, config_hash)
);

comment on table uc02.client_config_ref is
  'Content hash of the client checklist configuration in force. Reference only.';


-- ---------------------------------------------------------------------------
-- Onboarding cases.
-- ---------------------------------------------------------------------------
create table uc02.onboarding (
  onboarding_id     text        primary key,
  client_id         text        not null,
  employee_id       text        not null,
  joining_date      date        not null,
  location          text        not null,
  project           text        not null,
  role_id           text        not null,
  grade             text        not null default '',
  -- Opaque group code supplied by the caller. UC-02 stores no nationality.
  nationality_group text        not null default '',
  department        text        not null,
  manager_id        text        not null,
  attributes        text[]      not null default '{}',
  status            text        not null default 'active'
                    check (status in ('active', 'complete', 'cancelled')),
  revision          integer     not null default 0 check (revision >= 0),
  config_hash       text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  constraint onboarding_client_config_fk
    foreign key (client_id, config_hash)
    references uc02.client_config_ref (client_id, config_hash)
    on delete set null
);

-- Only one live onboarding per employee per client; cancelled ones may repeat
-- so a withdrawn candidate can be re-onboarded (planner.make_onboarding_id -Rn).
-- Mirrors the duplicate check in service.create_onboarding.
create unique index onboarding_one_live_per_employee
  on uc02.onboarding (client_id, employee_id)
  where status <> 'cancelled';

create index onboarding_client_idx       on uc02.onboarding (client_id);
create index onboarding_joining_date_idx on uc02.onboarding (client_id, joining_date);


-- ---------------------------------------------------------------------------
-- Tasks.
-- ---------------------------------------------------------------------------
create table uc02.task (
  task_id              text        primary key,
  onboarding_id        text        not null
                       references uc02.onboarding (onboarding_id) on delete cascade,
  client_id            text        not null,
  employee_id          text        not null,
  template_key         text        not null,
  -- Position in the topologically ordered plan produced by planner.build_tasks.
  -- Stored because the order is meaningful (prerequisites first) and sorting by
  -- task_id on read would silently reorder the checklist.
  plan_order           integer     not null default 0 check (plan_order >= 0),
  category             text        not null check (category in (
                         'hr_record', 'document_collection', 'it_provisioning',
                         'access_management', 'workplace', 'orientation',
                         'policy_acknowledgement', 'payroll', 'joining_confirmation',
                         'background_verification', 'visa', 'medical', 'biometrics',
                         'insurance', 'travel', 'accommodation', 'food',
                         'banking_letter', 'hr_system_activation')),
  title                text        not null check (length(btrim(title)) > 0),
  title_ar             text        not null default '',
  owner                text        not null check (owner in (
                         'hr_ops', 'hr_lead', 'it_manager', 'facilities', 'finance',
                         'manager', 'buddy', 'employee', 'system', 'hr_officer',
                         'hr_manager', 'pro')),
  status               text        not null default 'blocked'
                       check (status in ('blocked', 'ready', 'in_progress',
                                         'complete', 'cancelled')),
  due_date             date        not null,
  sensitive_to         text[]      not null default '{}',
  requires_attributes  text[]      not null default '{}',
  started_at           timestamptz,
  completed_at         timestamptz,
  cancelled_at         timestamptz,
  notes                text        not null default '',
  evidence_required    boolean     not null default false,
  evidence_ref         text,
  evidence_recorded_at timestamptz,
  approval_role        text        check (approval_role is null or approval_role in (
                         'hr_ops', 'hr_lead', 'it_manager', 'facilities', 'finance',
                         'manager', 'buddy', 'employee', 'system', 'hr_officer',
                         'hr_manager', 'pro')),
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),

  -- One task per template per onboarding: make the deterministic task_id
  -- (<onboarding_id>#<template_key>) enforceable by the database too.
  constraint task_unique_template unique (onboarding_id, template_key),
  constraint task_unique_plan_order unique (onboarding_id, plan_order) deferrable initially deferred,

  -- UC02-BR-12: a task requiring evidence cannot be complete without a
  -- reference. The database refuses what tracker.complete_task refuses.
  constraint task_evidence_required_on_complete check (
    status <> 'complete'
    or evidence_required = false
    or (evidence_ref is not null and length(btrim(evidence_ref)) > 0)
  ),
  constraint task_complete_has_timestamp check (
    status <> 'complete' or completed_at is not null
  ),
  constraint task_cancelled_has_timestamp check (
    status <> 'cancelled' or cancelled_at is not null
  )
);

create index task_onboarding_idx on uc02.task (onboarding_id);
create index task_client_idx     on uc02.task (client_id);
create index task_open_due_idx   on uc02.task (client_id, due_date)
  where status in ('blocked', 'ready', 'in_progress');
create index task_owner_idx      on uc02.task (client_id, owner, status);


-- ---------------------------------------------------------------------------
-- Task dependencies. Normalised rather than an array column so the edge can be
-- constrained and indexed in both directions.
-- ---------------------------------------------------------------------------
create table uc02.task_dependency (
  task_id         text not null references uc02.task (task_id) on delete cascade,
  depends_on_id   text not null references uc02.task (task_id) on delete cascade,
  primary key (task_id, depends_on_id),
  constraint task_dependency_not_self check (task_id <> depends_on_id)
);

create index task_dependency_reverse_idx on uc02.task_dependency (depends_on_id);

comment on table uc02.task_dependency is
  'Prerequisite edges. Acyclicity is enforced at configuration load time by '
  'checklist._find_cycle; a per-row SQL check cannot see the whole graph.';


-- ---------------------------------------------------------------------------
-- Plan revisions. One row per applied re-plan (planner.replan / ReplanOutcome).
-- ---------------------------------------------------------------------------
create table uc02.plan_revision (
  onboarding_id         text        not null
                        references uc02.onboarding (onboarding_id) on delete cascade,
  revision              integer     not null check (revision >= 0),
  client_id             text        not null,
  drivers               text[]      not null default '{}',
  added_task_ids        text[]      not null default '{}',
  removed_task_ids      text[]      not null default '{}',
  rescheduled_task_ids  text[]      not null default '{}',
  retained_task_ids     text[]      not null default '{}',
  approval_request_id   text,
  applied_by            text        not null,
  applied_at            timestamptz not null default now(),
  primary key (onboarding_id, revision),
  constraint plan_revision_drivers_known check (
    drivers <@ array['joining_date', 'location', 'project']::text[]
  )
);


-- ---------------------------------------------------------------------------
-- Approval requests (hard gates). Mirrors contracts.ApprovalRequest.
-- ---------------------------------------------------------------------------
create table uc02.approval_request (
  request_id     text        primary key,
  onboarding_id  text        not null
                 references uc02.onboarding (onboarding_id) on delete cascade,
  client_id      text        not null,
  gate           text        not null check (gate in (
                   'date_change_approval', 'location_change_approval',
                   'project_change_approval', 'cancellation_approval',
                   'joining_confirmation')),
  requested_by   text        not null,
  approver_role  text        not null check (approver_role in (
                   'hr_ops', 'hr_lead', 'it_manager', 'facilities', 'finance',
                   'manager', 'buddy', 'employee', 'system', 'hr_officer',
                   'hr_manager', 'pro')),
  payload        jsonb       not null default '{}'::jsonb,
  outcome        text        not null default 'pending'
                 check (outcome in ('pending', 'approved', 'rejected')),
  decided_by     text,
  decided_at     timestamptz,
  reason         text        not null default '',
  created_at     timestamptz not null default now(),

  -- A requester may not decide their own request (approvals.ApprovalInbox).
  constraint approval_no_self_decision check (
    decided_by is null or decided_by <> requested_by
  ),
  constraint approval_decided_has_timestamp check (
    outcome = 'pending' or decided_at is not null
  )
);

create index approval_pending_idx on uc02.approval_request (client_id, approver_role)
  where outcome = 'pending';
create index approval_onboarding_idx on uc02.approval_request (onboarding_id);


-- ---------------------------------------------------------------------------
-- Agent runs. One row per BaseAgent.run (ADR-005 / hr_recruitment_common).
-- Records what the agent did; never what a model was told or replied.
-- ---------------------------------------------------------------------------
create table uc02.agent_run (
  run_id          text        primary key,
  agent_id        text        not null,
  agent_version   text        not null,
  client_id       text        not null,
  onboarding_id   text        references uc02.onboarding (onboarding_id) on delete set null,
  actor_id        text        not null,
  status          text        not null check (status in ('proposed', 'acted', 'exception')),
  advisor         text        not null default 'offline',
  model           text,
  proposal_count  integer     not null default 0 check (proposal_count  >= 0),
  action_count    integer     not null default 0 check (action_count    >= 0),
  exception_count integer     not null default 0 check (exception_count >= 0),
  tool_calls      text[]      not null default '{}',
  started_at      timestamptz not null default now(),
  finished_at     timestamptz
);

create index agent_run_onboarding_idx on uc02.agent_run (onboarding_id);
create index agent_run_client_idx     on uc02.agent_run (client_id, started_at desc);

comment on column uc02.agent_run.model is
  'LiteLLM route actually used, or NULL when the deterministic offline advisor ran.';


-- ---------------------------------------------------------------------------
-- Audit events. Append-only: no UPDATE or DELETE grant, enforced by trigger.
-- ---------------------------------------------------------------------------
create table uc02.audit_event (
  event_id      text        primary key,
  client_id     text        not null,
  onboarding_id text,
  actor_id      text        not null,
  actor_role    text        not null,
  action        text        not null,
  outcome       text        not null,
  detail        jsonb       not null default '{}'::jsonb,
  at            timestamptz not null default now()
);

create index audit_client_at_idx   on uc02.audit_event (client_id, at desc);
create index audit_onboarding_idx  on uc02.audit_event (onboarding_id) where onboarding_id is not null;
create index audit_action_idx      on uc02.audit_event (client_id, action);

-- Mirrors audit.FORBIDDEN_DETAIL_KEYS. The Python log already refuses these;
-- the database refuses them again so a future writer cannot bypass the rule.
create or replace function uc02.reject_personal_data_keys()
returns trigger
language plpgsql
as $$
declare
  leaked text;
begin
  select string_agg(k, ', ')
    into leaked
    from jsonb_object_keys(new.detail) as k
   where k in ('name', 'full_name', 'email', 'phone',
               'nationality', 'passport', 'address');
  if leaked is not null then
    raise exception 'audit detail must not contain personal data keys: %', leaked
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

create trigger audit_event_reject_personal_data
  before insert or update on uc02.audit_event
  for each row execute function uc02.reject_personal_data_keys();

create or replace function uc02.forbid_mutation()
returns trigger
language plpgsql
as $$
begin
  raise exception 'uc02.audit_event is append-only; % is not permitted', tg_op
    using errcode = 'insufficient_privilege';
end;
$$;

create trigger audit_event_append_only
  before update or delete on uc02.audit_event
  for each row execute function uc02.forbid_mutation();


-- ---------------------------------------------------------------------------
-- updated_at maintenance.
-- ---------------------------------------------------------------------------
create or replace function uc02.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger onboarding_touch_updated_at
  before update on uc02.onboarding
  for each row execute function uc02.touch_updated_at();

create trigger task_touch_updated_at
  before update on uc02.task
  for each row execute function uc02.touch_updated_at();
