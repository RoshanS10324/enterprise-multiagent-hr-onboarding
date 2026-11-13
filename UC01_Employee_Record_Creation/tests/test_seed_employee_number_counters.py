"""The counter seed: dry run by default, inserts only what is missing, never overwrites."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
from pydantic import SecretStr

from sub_agent.adapters.supabase import PostgrestClient
from test_supabase_adapters import BASE, FakePostgrest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "seed_employee_number_counters.py"
_spec = importlib.util.spec_from_file_location("seed_employee_number_counters", SCRIPT)
seed_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_script)


def _db():
    fake = FakePostgrest()
    fake.unique["employee_number_counters"] = ("client_id",)
    fake.tables["client_configs"] = [{"client_id": "SYN-UAE-TEST"}, {"client_id": "CLIENT_DUBAI"}]
    fake.tables["employee_number_counters"] = [
        {"client_id": "CLIENT_DUBAI", "last_value": 41, "prefix": "EMP-", "padding": 6}]
    return fake, PostgrestClient(BASE, SecretStr("k"), transport=httpx.MockTransport(fake.handler))


def test_the_dry_run_lists_the_missing_client_and_writes_nothing():
    fake, client = _db()
    result = seed_script.seed(client, apply=False)
    assert result["missing"] == ["SYN-UAE-TEST"] and result["inserted"] == []
    assert len(fake.tables["employee_number_counters"]) == 1


def test_apply_inserts_only_the_missing_counter_and_keeps_issued_numbers():
    fake, client = _db()
    result = seed_script.seed(client, apply=True)
    assert result["inserted"] == ["SYN-UAE-TEST"]
    rows = {r["client_id"]: r for r in fake.tables["employee_number_counters"]}
    assert rows["SYN-UAE-TEST"] == {"client_id": "SYN-UAE-TEST", "last_value": 0, "prefix": "EMP-", "padding": 6}
    assert rows["CLIENT_DUBAI"]["last_value"] == 41  # never reset


def test_a_second_apply_is_a_no_op():
    fake, client = _db()
    seed_script.seed(client, apply=True)
    again = seed_script.seed(client, apply=True)
    assert again["missing"] == [] and again["inserted"] == []
    assert len(fake.tables["employee_number_counters"]) == 2


def test_limiting_to_a_client_that_already_has_a_counter_inserts_nothing():
    fake, client = _db()
    result = seed_script.seed(client, apply=True, only=["CLIENT_DUBAI"])
    assert result["inserted"] == [] and result["not_missing_or_unknown"] == ["CLIENT_DUBAI"]
    assert len(fake.tables["employee_number_counters"]) == 1
