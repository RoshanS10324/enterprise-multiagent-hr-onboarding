"""Database persistence for UC-04, written against injected protocols. Contains no database client, no HTTP and no environment access.

* `DatabaseCaseRepository` over a `StateBackend`: UC-04's own case state. `AuditEventStateBackend` keeps it as versioned snapshots in the
  shared `audit_events` table (no new table); `TableStateBackend` targets the proposed (not applied) UC-04 tables; `InMemoryStateBackend`
  is the reference implementation and test double.
* `DatabaseCaseContext`: the parent case, read from `onboarding_cases` (read-only, client-scoped).
* `DatabaseAuditSink`: audit entries to `audit_events`.
* `DatabaseAgentRunRecorder`: runs to `agent_runs`.

A concrete `TableClient` is supplied by the approved integration layer. Nothing here is connected to anything.
"""

from sub_agent.persistence.audit_sink import DatabaseAuditSink
from sub_agent.persistence.audit_state import AuditEventStateBackend
from sub_agent.persistence.case_context import DatabaseCaseContext
from sub_agent.persistence.errors import (PersistenceError, SchemaMismatchError, TenantScopeError, UnsupportedParentStatus, VersionConflict)
from sub_agent.persistence.repository import DatabaseCaseRepository
from sub_agent.persistence.run_recorder import DatabaseAgentRunRecorder
from sub_agent.persistence.state_backend import InMemoryStateBackend, StateBackend, StoredCase
from sub_agent.persistence.table_client import InMemoryTableClient, TableClient, TableSchema
from sub_agent.persistence.table_state import TableStateBackend

__all__ = ["AuditEventStateBackend", "DatabaseAgentRunRecorder", "DatabaseAuditSink", "DatabaseCaseContext", "DatabaseCaseRepository", "InMemoryStateBackend",
           "InMemoryTableClient", "PersistenceError", "SchemaMismatchError", "StateBackend", "StoredCase", "TableClient", "TableSchema",
           "TableStateBackend", "TenantScopeError", "UnsupportedParentStatus", "VersionConflict"]
