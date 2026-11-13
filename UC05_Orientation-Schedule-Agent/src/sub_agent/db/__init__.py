"""UC05 persistence for the shared Supabase database (UC05_DATABASE_MODE=supabase).

  config.py      environment configuration + validation (mock | supabase)
  postgrest.py   the one PostgREST client (httpx), error mapping, read-only retries
  mapping.py     columns UC05 depends on, status vocabularies, stable ids, audit sanitising
  repository.py  SupabaseOnboardingRepository -- OnboardingPort over the shared tables
  directory.py   employee directory + offer-event source over onboarding_cases
  approvals.py   SupabaseHITLInbox -- the shared HITLInbox stored in approval_requests
  audit.py       audit_events (SupabaseAuditLog) and agent_runs (AgentRunRecorder)
  factory.py     builds all of the above from one client and verifies the schema at startup

Business logic never imports this package: it talks to the OnboardingPort / HITLInbox / AuditLog
interfaces, so unit tests keep injecting the in-memory fakes (UC05_DATABASE_MODE=mock).
"""
