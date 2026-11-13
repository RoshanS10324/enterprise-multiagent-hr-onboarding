"""UC-04's own case state as versioned snapshots in the shared `audit_events` table (`AuditEventStateBackend`), proved over an in-memory
table client that enforces the table's columns, required columns and primary key. No database, no network."""

import pytest

from helpers import ALL, approvals, make
from sub_agent.persistence import (AuditEventStateBackend, DatabaseCaseRepository, InMemoryTableClient, PersistenceError, TableSchema,
                                   VersionConflict)
from sub_agent.persistence.audit_state import ACTION, AUDIT_TABLE, snapshot_event_id, state_reference
from sub_agent.store import CaseRecord, CaseStore
from test_database_persistence import build, ev, run_flow_a, run_flow_b, sub

# audit_events as the live schema has it (columns; NOT NULL without default; primary key)
SCHEMAS = {
    AUDIT_TABLE: TableSchema(
        frozenset({"event_id", "case_id", "client_id", "actor_type", "actor_id", "actor_role", "action", "outcome", "detail", "before_state",
                   "after_state", "correlation_id", "reference", "occurred_at", "task_id", "sequence", "prev_hash", "entry_hash"}),
        frozenset({"client_id", "actor_type", "actor_id", "action", "detail"}), ("event_id",)),
}


@pytest.fixture(autouse=True)
def _neutral_fixture_counter():
    """These tests draw synthetic approval ids from the global counter in `sub_agent.fixtures`. A direct call's run_id is a digest of
    its input (approval ids included), and for some counter values that digest reads as a phone number to the privacy scanner
    (`direct-00…`), failing an unrelated later test. Restoring the counter keeps every other test's ids exactly as they were."""
    import itertools

    from sub_agent import fixtures

    start = next(fixtures._apr)
    fixtures._apr = itertools.count(start)
    yield
    fixtures._apr = itertools.count(start)


def backend(client=None):
    return AuditEventStateBackend(client or InMemoryTableClient(SCHEMAS))


@pytest.mark.parametrize("flow", [run_flow_a, run_flow_b], ids=["approvals-replay-submit-withdraw", "adapter-failure-acknowledge-retry"])
def test_the_audit_backend_decides_and_audits_exactly_like_the_in_memory_store(flow):
    assert flow(DatabaseCaseRepository(backend())) == flow(CaseStore())


def test_state_survives_a_restart_and_nothing_is_sent_twice():
    client = InMemoryTableClient(SCHEMAS)
    _, direct, desk, _ = build(DatabaseCaseRepository(AuditEventStateBackend(client)))
    payload = make(approval_records=approvals(ALL))
    ev(direct, payload, "k1")
    service2, direct2, _, _ = build(DatabaseCaseRepository(AuditEventStateBackend(client)), desk=desk)  # a new process, same table
    ev(direct2, payload, "k1")
    assert any(e["event_type"] == "replay.served" for e in service2.audit.events)  # the stored result came back from the snapshot
    assert len(sub(direct2, payload).submitted) == 4
    _, direct3, _, _ = build(DatabaseCaseRepository(AuditEventStateBackend(client)), desk=desk)
    assert sub(direct3, payload).submitted == [] and len(desk.calls) == 4


def test_snapshots_are_append_only_rows_holding_the_exact_item_states():
    client = InMemoryTableClient(SCHEMAS)
    _, direct, _, _ = build(DatabaseCaseRepository(AuditEventStateBackend(client)))
    payload = make(approval_records=approvals(ALL))
    ev(direct, payload, "k1")
    sub(direct, payload)
    rows = [r for r in client.tables[AUDIT_TABLE] if r["action"] == ACTION]
    keys = sorted((r["detail"]["version"], r["detail"]["seq"]) for r in rows)
    assert len(set(keys)) == len(rows) >= 2  # one row per save, never updated
    assert sorted({v for v, _ in keys}) == list(range(1, max(v for v, _ in keys) + 1))  # versions move by one
    latest = max(rows, key=lambda r: (r["detail"]["version"], r["detail"]["seq"]))
    items = latest["after_state"]["record"]["items"]
    assert len(items) == 4 and {i["state"] for i in items.values()} == {"MOCK_TICKET_CREATED"}
    assert latest["client_id"] == "client_alpha" and latest["actor_type"] == "AGENT"
    assert latest["reference"] == state_reference(latest["case_id"])
    assert latest["event_id"] == snapshot_event_id(latest["case_id"], latest["detail"]["version"], latest["detail"]["seq"])


def record(client_id="client_alpha"):
    return CaseRecord("SYN-CASE-001", "SYN-EMP-001", client_id)


def test_a_second_first_write_and_a_stale_save_are_both_refused():
    b = backend()
    b.save(record(), expected_version=0, new_version=1)
    with pytest.raises(VersionConflict):
        b.save(record(), expected_version=0, new_version=1)
    b.save(record(), expected_version=1, new_version=2)
    with pytest.raises(VersionConflict):
        b.save(record(), expected_version=1, new_version=2)
    assert b.load("SYN-CASE-001").version == 2


def test_a_lost_race_collides_on_the_primary_key_and_writes_nothing():
    client = InMemoryTableClient(SCHEMAS)
    b = AuditEventStateBackend(client)
    b.save(record(), expected_version=0, new_version=1)
    # another writer read version 1 too and saved version 2 first: the derived event id is now taken
    client.insert(AUDIT_TABLE, {"event_id": snapshot_event_id("SYN-CASE-001", 2, 0), "case_id": "SYN-CASE-001", "client_id": "client_alpha",
                                "actor_type": "AGENT", "actor_id": "uc04-agent", "action": "SOMETHING_ELSE", "detail": {}})
    before = len(client.tables[AUDIT_TABLE])
    with pytest.raises(VersionConflict):
        b.save(record(), expected_version=1, new_version=2)
    assert len(client.tables[AUDIT_TABLE]) == before


def test_a_results_only_save_keeps_the_version_and_appends_the_next_sequence():
    client = InMemoryTableClient(SCHEMAS)
    b = AuditEventStateBackend(client)
    b.save(record(), expected_version=0, new_version=1)
    b.save(record(), expected_version=1, new_version=1)
    b.save(record(), expected_version=1, new_version=1)
    assert [(r["detail"]["version"], r["detail"]["seq"]) for r in client.tables[AUDIT_TABLE]] == [(1, 0), (1, 1), (1, 2)]
    assert b.load("SYN-CASE-001").version == 1
    with pytest.raises(PersistenceError):
        b.save(record(), expected_version=1, new_version=3)


def test_a_stored_case_cannot_change_client():
    b = backend()
    b.save(record(), expected_version=0, new_version=1)
    with pytest.raises(PersistenceError):
        b.save(record("client_beta"), expected_version=1, new_version=2)


def test_a_failing_client_is_a_persistence_error_without_its_text():
    client = InMemoryTableClient(SCHEMAS)
    client.fail_with = RuntimeError("postgrest said: secret-key-123")
    with pytest.raises(PersistenceError) as exc:
        AuditEventStateBackend(client).load("SYN-CASE-001")
    assert "secret-key-123" not in str(exc.value)


def test_listing_every_case_is_refused():
    with pytest.raises(PersistenceError):
        backend().case_ids()
