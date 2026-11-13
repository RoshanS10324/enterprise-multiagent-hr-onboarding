"""Opt-in, READ-ONLY checks against the real Supabase project.

Skipped unless UC01_LIVE_DB_TESTS=1, so the default suite never touches
a real database and CI stays hermetic. Run with:

    UC01_LIVE_DB_TESTS=1 uv run pytest tests/test_supabase_live.py

These tests only read. They never INSERT, UPDATE or DELETE, and they
never select candidate PII - only the table metadata and column names
the adapters depend on.
"""

from __future__ import annotations

import os

import pytest

from sub_agent.adapters.supabase import PostgrestClient
from sub_agent.api.settings import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("UC01_LIVE_DB_TESTS") != "1",
    reason="live database tests are opt-in (set UC01_LIVE_DB_TESTS=1)",
)


@pytest.fixture(scope="module")
def live() -> PostgrestClient:
    settings = Settings.from_env()
    if not settings.persistence_enabled:
        pytest.skip("SUPABASE_URL is not configured")
    client = PostgrestClient(
        settings.supabase_url,
        settings.supabase_service_role_key,
        schema=settings.supabase_schema,
        timeout=settings.supabase_timeout_s,
    )
    yield client
    client.close()


def test_connection_test_succeeds(live: PostgrestClient):
    """The same ping the ASGI startup hook performs."""
    live.ping()


@pytest.mark.parametrize(
    "table",
    [
        "onboarding_forms",
        "person_records",
        "intake_discrepancies",
        "hris_sync_records",
        "pending_starts",
        "offer_forms",
        "domain_events_outbox",
        "client_holidays",
    ],
)
def test_each_mapped_table_is_reachable(live: PostgrestClient, table: str):
    """A zero-row read proves the table exists and is readable with the
    configured credentials, without reading anyone's data."""
    live.select(table, columns="*", limit=0)


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        (
            "onboarding_forms",
            "form_id,client_id,case_id,version,salary_and_terms_encrypted,status",
        ),
        ("person_records", "record_id,client_id,email,full_name,kind,case_id"),
        ("intake_discrepancies", "discrepancy_id,field_name,other_fingerprint"),
        ("hris_sync_records", "record_id,target_system,idempotency_key"),
        ("domain_events_outbox", "event_id,event_type,payload,published_at"),
        ("client_holidays", "client_id,holiday_date,is_observed"),
    ],
)
def test_columns_the_adapters_depend_on_exist(
    live: PostgrestClient, table: str, columns: str
):
    """Selecting a named column list fails loudly if a column was
    renamed or dropped - a cheap schema-contract check."""
    live.select(table, columns=columns, limit=0)
