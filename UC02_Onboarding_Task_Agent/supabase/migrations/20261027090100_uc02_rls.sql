-- UC-02 — Row Level Security and grants.
--
-- CLAUDE.md engineering rule and UC02-BR-13: every record is scoped to a
-- client and cross-client access must be impossible, not merely discouraged.
-- The Python service enforces this in OnboardingService._load; this migration
-- enforces the same rule a second time at the storage layer, so a future
-- caller that bypasses the service still cannot read another client's data.
--
-- Fail-closed: uc02.current_client_id() returns NULL when no client claim is
-- present, and `client_id = NULL` is never true, so an unidentified caller
-- sees zero rows rather than everything.

alter table uc02.onboarding        enable row level security;
alter table uc02.task              enable row level security;
alter table uc02.task_dependency   enable row level security;
alter table uc02.plan_revision     enable row level security;
alter table uc02.approval_request  enable row level security;
alter table uc02.agent_run         enable row level security;
alter table uc02.audit_event       enable row level security;
alter table uc02.client_config_ref enable row level security;

-- Force RLS for the table owner too. Without this the owner role bypasses
-- every policy below, which would make the isolation tests meaningless.
alter table uc02.onboarding        force row level security;
alter table uc02.task              force row level security;
alter table uc02.task_dependency   force row level security;
alter table uc02.plan_revision     force row level security;
alter table uc02.approval_request  force row level security;
alter table uc02.agent_run         force row level security;
alter table uc02.audit_event       force row level security;
alter table uc02.client_config_ref force row level security;


-- ---------------------------------------------------------------------------
-- Tables carrying client_id directly: one tenant policy each.
-- ---------------------------------------------------------------------------
create policy onboarding_tenant on uc02.onboarding
  for all
  using      (client_id = uc02.current_client_id())
  with check (client_id = uc02.current_client_id());

create policy task_tenant on uc02.task
  for all
  using      (client_id = uc02.current_client_id())
  with check (client_id = uc02.current_client_id());

create policy plan_revision_tenant on uc02.plan_revision
  for all
  using      (client_id = uc02.current_client_id())
  with check (client_id = uc02.current_client_id());

create policy approval_request_tenant on uc02.approval_request
  for all
  using      (client_id = uc02.current_client_id())
  with check (client_id = uc02.current_client_id());

create policy agent_run_tenant on uc02.agent_run
  for all
  using      (client_id = uc02.current_client_id())
  with check (client_id = uc02.current_client_id());

create policy client_config_ref_tenant on uc02.client_config_ref
  for all
  using      (client_id = uc02.current_client_id())
  with check (client_id = uc02.current_client_id());


-- ---------------------------------------------------------------------------
-- task_dependency has no client_id of its own: derive it from the parent task
-- so an edge cannot be created across two clients' tasks.
-- ---------------------------------------------------------------------------
create policy task_dependency_tenant on uc02.task_dependency
  for all
  using (
    exists (
      select 1 from uc02.task t
       where t.task_id = task_dependency.task_id
         and t.client_id = uc02.current_client_id()
    )
  )
  with check (
    exists (
      select 1 from uc02.task t
       where t.task_id = task_dependency.task_id
         and t.client_id = uc02.current_client_id()
    )
    and exists (
      select 1 from uc02.task p
       where p.task_id = task_dependency.depends_on_id
         and p.client_id = uc02.current_client_id()
    )
  );


-- ---------------------------------------------------------------------------
-- Audit: readable and insertable within the tenant, never updatable or
-- deletable. The append-only trigger blocks UPDATE/DELETE even for a
-- superuser path; omitting those policies blocks them here as well.
-- ---------------------------------------------------------------------------
create policy audit_event_tenant_read on uc02.audit_event
  for select
  using (client_id = uc02.current_client_id());

create policy audit_event_tenant_insert on uc02.audit_event
  for insert
  with check (client_id = uc02.current_client_id());


-- ---------------------------------------------------------------------------
-- Role grants.
--
-- `anon` gets nothing: UC-02 has no unauthenticated surface.
-- `authenticated` gets no direct table access either — UC-02 writes are
-- business operations with prerequisite, authorisation, evidence and approval
-- rules that SQL cannot express. All writes go through the service, which runs
-- server-side under uc02_service.
--
-- The Supabase service-role key bypasses RLS entirely and must therefore never
-- reach a browser. See .env.example.
-- ---------------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'uc02_service') then
    create role uc02_service nologin;
  end if;
end;
$$;

grant usage on schema uc02 to uc02_service;

grant select, insert, update on
  uc02.onboarding, uc02.task, uc02.task_dependency,
  uc02.plan_revision, uc02.approval_request, uc02.agent_run,
  uc02.client_config_ref
  to uc02_service;

-- Audit is insert + select only. No update, no delete, by grant and by trigger.
grant select, insert on uc02.audit_event to uc02_service;

grant execute on function uc02.current_client_id() to uc02_service;

revoke all on schema uc02 from public;
