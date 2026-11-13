"""Builds every supabase-mode store from one validated configuration and ONE PostgREST client, and
checks the database before UC05 accepts work:

  1. the credential and URL (a real round trip; refused key -> PersistenceAuthError),
  2. every table and column UC05 reads or writes (mapping.REQUIRED_COLUMNS; missing ->
     SchemaMismatchError naming them), unless UC05_SUPABASE_VERIFY_SCHEMA=0.

Nothing here falls back to local or synthetic data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.platform.hitl import ApprovalItem
from sub_agent.db.approvals import SupabaseHITLInbox
from sub_agent.db.audit import AgentRunRecorder, SupabaseAuditLog
from sub_agent.db.config import SupabaseSettings
from sub_agent.db.directory import SupabaseCaseEventSource, SupabaseEmployeeDirectory
from sub_agent.db.mapping import REQUIRED_COLUMNS
from sub_agent.db.postgrest import PersistenceError, PostgrestClient, SchemaMismatchError
from sub_agent.db.repository import SupabaseOnboardingRepository


@dataclass
class SupabaseStores:
    client: PostgrestClient
    repository: SupabaseOnboardingRepository
    directory: SupabaseEmployeeDirectory
    events: SupabaseCaseEventSource
    audit: SupabaseAuditLog
    hitl: SupabaseHITLInbox
    runs: AgentRunRecorder


def verify_schema(client: PostgrestClient) -> None:
    """Selects every required column of every table with limit 0 (reads no rows)."""
    missing: list[str] = []
    for table, columns in REQUIRED_COLUMNS.items():
        try:
            client.select(table, columns=",".join(columns), limit=0)
        except SchemaMismatchError:
            missing.append(table)
    if missing:
        raise SchemaMismatchError("tables/columns UC05 needs are missing or renamed in "
                                  f"{', '.join(missing)} (see docs/UC05_DATABASE_MAPPING.md)")


def build_supabase_stores(settings: SupabaseSettings, *, audit_path: Path | None = None,
                          clock: Callable[[], datetime] = now_utc, transport: httpx.BaseTransport | None = None,
                          verify: bool | None = None) -> SupabaseStores:
    client = PostgrestClient(settings.url, settings.service_role_key, schema=settings.schema,
                             timeout=settings.timeout_s, read_retries=settings.read_retries, transport=transport)
    try:
        client.ping()
        if settings.verify_schema if verify is None else verify:
            verify_schema(client)
    except PersistenceError:
        client.close()
        raise
    repository = SupabaseOnboardingRepository(client, clock=clock)
    audit = SupabaseAuditLog(client, repository, path=audit_path)

    def link_review_gate(item: ApprovalItem) -> None:
        if item.proposal_type == "welcome_message" and item.payload.get("id"):
            repository.link_review_gate(item.payload["id"], item.id, client_id=item.client_id)

    return SupabaseStores(client=client, repository=repository, directory=SupabaseEmployeeDirectory(repository),
                          events=SupabaseCaseEventSource(repository), audit=audit,
                          hitl=SupabaseHITLInbox(client, repository, audit, on_submitted=link_review_gate),
                          runs=AgentRunRecorder(client, repository))
