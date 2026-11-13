"""UC-04's own case state in UC-04-owned tables (`TableStateBackend`), proved over an in-memory table client. No database, no network.
This shows the code is correct against the proposed table design; it does NOT show the tables exist in Supabase (the migration is unapplied)."""

import re
from pathlib import Path

import pytest

from helpers import ALL, approvals, make
from sub_agent.persistence import (DatabaseCaseRepository, InMemoryTableClient, PersistenceError, TableSchema, TableStateBackend, VersionConflict)
from sub_agent.persistence.table_state import RESULTS_TABLE, STATE_TABLE
from sub_agent.store import CaseRecord, CaseStore
from test_database_persistence import build, ev, run_flow_a, run_flow_b, sub

ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "docs" / "proposed_migrations" / "uc04_state_tables.sql").read_text(encoding="utf-8")

SCHEMAS = {
    STATE_TABLE: TableSchema(frozenset({"case_id", "client_id", "employee_ref", "version", "record", "updated_at"}),
                             frozenset({"case_id", "client_id", "employee_ref", "version", "record"}), ("case_id",)),
    RESULTS_TABLE: TableSchema(frozenset({"case_id", "idempotency_key", "payload_hash", "store_version", "parent_status", "result", "created_at"}),
                               frozenset({"case_id", "idempotency_key", "payload_hash", "store_version", "parent_status", "result"}),
                               ("case_id", "idempotency_key")),
}


def backend(client=None):
    return TableStateBackend(client or InMemoryTableClient(SCHEMAS))


# ------------------------------------------------------------------------------------------------------ same decisions as the in-memory store
@pytest.mark.parametrize("flow", [run_flow_a, run_flow_b], ids=["approvals-replay-submit-withdraw", "adapter-failure-acknowledge-retry"])
def test_the_table_backend_decides_and_audits_exactly_like_the_in_memory_store(flow):
    assert flow(DatabaseCaseRepository(backend())) == flow(CaseStore())


def test_state_survives_a_restart_and_nothing_is_sent_twice():
    client = InMemoryTableClient(SCHEMAS)
    _, direct, desk, _ = build(DatabaseCaseRepository(TableStateBackend(client)))
    payload = make(approval_records=approvals(ALL))
    ev(direct, payload, "k1")
    service2, direct2, _, _ = build(DatabaseCaseRepository(TableStateBackend(client)), desk=desk)  # a new process over the same tables
    ev(direct2, payload, "k1")
    assert any(e["event_type"] == "replay.served" for e in service2.audit.events)  # the stored result came back from the table
    assert len(sub(direct2, payload).submitted) == 4
    _, direct3, _, _ = build(DatabaseCaseRepository(TableStateBackend(client)), desk=desk)
    assert sub(direct3, payload).submitted == [] and len(desk.calls) == 4


def test_the_stored_rows_hold_the_exact_item_states_and_one_row_per_idempotency_key():
    client = InMemoryTableClient(SCHEMAS)
    _, direct, _, _ = build(DatabaseCaseRepository(TableStateBackend(client)))
    payload = make(approval_records=approvals(ALL))
    ev(direct, payload, "k1")
    sub(direct, payload)
    (state_row,) = client.tables[STATE_TABLE]
    items = state_row["record"]["items"]
    assert len(items) == 4 and all(i["ticket_id"] and i["submission_attempts"] == 1 for i in items.values())
    assert {i["state"] for i in items.values()} == {"MOCK_TICKET_CREATED"}
    assert state_row["version"] >= 1 and state_row["client_id"] == "client_alpha"
    assert len(client.tables[RESULTS_TABLE]) == len({r["idempotency_key"] for r in client.tables[RESULTS_TABLE]}) >= 1


# ------------------------------------------------------------------------------------------------------ concurrency and tenancy
def record(client_id="client_alpha"):
    return CaseRecord("SYN-CASE-001", "SYN-EMP-001", client_id)


def test_a_second_first_write_and_a_stale_update_are_both_refused():
    b = backend()
    b.save(record(), expected_version=0, new_version=1)
    with pytest.raises(VersionConflict):
        b.save(record(), expected_version=0, new_version=1)  # another writer created the case first
    b.save(record(), expected_version=1, new_version=2)
    with pytest.raises(VersionConflict):
        b.save(record(), expected_version=1, new_version=2)  # stale: the case is already at version 2
    assert b.load("SYN-CASE-001").version == 2


def test_a_lost_race_on_the_conditional_update_writes_nothing():
    client = InMemoryTableClient(SCHEMAS)
    b = TableStateBackend(client)
    b.save(record(), expected_version=0, new_version=1)
    original = client.update
    client.update = lambda *a, **k: 0  # the row changed between the read and the update
    with pytest.raises(VersionConflict):
        b.save(record(), expected_version=1, new_version=2)
    client.update = original
    assert b.load("SYN-CASE-001").version == 1


def test_a_stored_case_can_never_change_client():
    b = backend()
    b.save(record("client_alpha"), expected_version=0, new_version=1)
    with pytest.raises(PersistenceError):
        b.save(record("client_bravo"), expected_version=1, new_version=2)
    assert b.load("SYN-CASE-001").record.client_id == "client_alpha"


def test_listing_every_case_is_not_available_and_unfiltered_access_is_refused_by_the_client():
    with pytest.raises(PersistenceError):
        backend().case_ids()
    with pytest.raises(PersistenceError):
        InMemoryTableClient(SCHEMAS).select(STATE_TABLE, filters={})
    with pytest.raises(PersistenceError):
        InMemoryTableClient(SCHEMAS).update(STATE_TABLE, filters={}, values={"version": 1})


# ------------------------------------------------------------------------------------------------------ failures
def test_a_storage_failure_surfaces_as_a_persistence_error_and_stores_nothing():
    client = InMemoryTableClient(SCHEMAS)
    _, direct, _, _ = build(DatabaseCaseRepository(TableStateBackend(client)))
    client.fail_with = OSError("host unavailable")
    with pytest.raises(PersistenceError) as e:
        ev(direct, make(), "k1")
    assert "unavailable" not in str(e.value)
    client.fail_with = None
    assert client.tables.get(STATE_TABLE, []) == []


def test_a_failure_between_the_two_tables_loses_only_the_replay_entry_never_the_state():
    class Flaky(InMemoryTableClient):
        def upsert(self, table, row, *, on_conflict):
            if table == RESULTS_TABLE:
                raise OSError("results write failed")
            super().upsert(table, row, on_conflict=on_conflict)

    client = Flaky(SCHEMAS)
    b = TableStateBackend(client)
    rec = record()
    rec.adapter_errors["x"] = "AdapterTimeout"
    from sub_agent.contracts import Uc04Result
    from sub_agent.states import CaseStatus
    rec.results["k"] = ("hash", 1, "ACTIVE", Uc04Result(case_id="SYN-CASE-001", overall_status=CaseStatus.BLOCKED, readiness_reason="r"))
    with pytest.raises(OSError):
        b.save(rec, expected_version=0, new_version=1)
    stored = b.load("SYN-CASE-001")
    assert stored.version == 1 and stored.record.adapter_errors == {"x": "AdapterTimeout"} and stored.record.results == {}  # state kept, no result served


def test_a_result_round_trips_through_the_table():
    from sub_agent.contracts import Uc04Result
    from sub_agent.states import CaseStatus
    b = backend()
    rec = record()
    result = Uc04Result(case_id="SYN-CASE-001", overall_status=CaseStatus.AT_RISK, readiness_reason="late")
    rec.results["k"] = ("hash", 3, "ACTIVE", result)
    b.save(rec, expected_version=0, new_version=3)
    assert b.load("SYN-CASE-001").record.results["k"] == ("hash", 3, "ACTIVE", result)


# ------------------------------------------------------------------------------------------------------ the proposed migration matches the code
def test_the_proposed_migration_is_additive_and_matches_the_columns_the_code_writes():
    statements = "\n".join(line for line in SQL.splitlines() if not line.strip().startswith("--"))  # comments are not statements
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|ALTER\s+TABLE\s+(?!uc04_))", statements, re.I)
    assert "NOT APPLIED" in SQL and "ENABLE ROW LEVEL SECURITY" in SQL
    for table, schema in SCHEMAS.items():
        block = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", SQL, re.S).group(1)
        columns = {m.group(1) for m in re.finditer(r"^\s+([a-z_]+)\s+(?:uuid|text|bigint|jsonb|timestamptz)", block, re.M)}
        assert columns == set(schema.columns), (table, columns ^ set(schema.columns))
    assert "REFERENCES onboarding_cases(case_id)" in SQL and "REFERENCES client_configs(client_id)" in SQL
