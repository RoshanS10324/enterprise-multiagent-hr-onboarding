"""Structural checks over the proposed SQL.

SCOPE AND LIMITS - read before trusting these.

These tests parse SQL TEXT. No Postgres is involved: nothing here proves
the DDL compiles, that a trigger fires, or that a policy denies. They
exist because the F2 defect was an OMISSION - the immutability trigger
simply never mentioned `client_id`, in any branch - and an omission is
exactly what a text-level check catches cheaply and reliably.

Database-level verification requires executing this SQL against an
isolated local Supabase instance (docs/local-test-env.md), which has not
happened: neither Docker nor the Supabase CLI is installed. Until it
does, treat a green run here as "the control is written down", not "the
control works".

Findings referenced: docs/schema/review-2026-11-03.md
"""

from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DRAFT = _ROOT / "docs" / "schema" / "uc01_draft.sql"
_PROPOSED = _ROOT / "docs" / "schema" / "proposed" / "d4_d5_additive.sql"


@pytest.fixture(scope="module")
def draft() -> str:
    return _DRAFT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def proposed() -> str:
    return _PROPOSED.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def form_trigger(draft: str) -> str:
    """Body of uc01_enforce_onboarding_form_immutability()."""
    start = draft.index("CREATE OR REPLACE FUNCTION uc01_enforce_onboarding_form_immutability")
    end = draft.index("CREATE TRIGGER onboarding_forms_immutability")
    return draft[start:end]


def _uncommented(sql: str) -> str:
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


# ------------------------------------------------------- F2: immutability

# Immutable in EVERY status, including DRAFT/AWAITING_APPROVAL.
#   client_id - tenant ownership; a form never changes hands.
#   form_id   - primary key AND the AES-GCM AAD the salary is bound to.
#   version   - a new version is a new row, never an edit of this one.
ALWAYS_IMMUTABLE = ["client_id", "form_id", "version"]

# Immutable once the row is APPROVED. Must be checked in BOTH approved
# branches: the case_id backfill, and APPROVED -> SUPERSEDED.
APPROVED_IMMUTABLE = [
    "candidate_name",
    "candidate_email",
    "designation",
    "joining_date",
    "work_location_type",
    "client_site_id",
    "salary_and_terms_encrypted",
    "approved_by",
    "approved_at",
    "change_reason",
]


@pytest.mark.parametrize("column", ALWAYS_IMMUTABLE)
def test_always_immutable_column_is_guarded_before_any_status_branch(form_trigger, column):
    """Regression for F2.

    `UPDATE onboarding_forms SET client_id = 'other-tenant'` on an
    APPROVED row used to pass every branch, moving the salary, candidate
    identity and approval evidence to another tenant. The guard has to
    sit BEFORE the status branching so it applies in every state.
    """
    guard_section = form_trigger[: form_trigger.index("IF OLD.status IN ('REJECTED'")]
    assert re.search(rf"NEW\.{column}\s+IS DISTINCT FROM\s+OLD\.{column}", guard_section), (
        f"{column} is not guarded before the status branches; an UPDATE in any "
        f"state could change it"
    )


@pytest.mark.parametrize("column", APPROVED_IMMUTABLE)
def test_approved_row_columns_are_guarded_in_both_approved_branches(form_trigger, column):
    approved = form_trigger[form_trigger.index("IF OLD.status = 'APPROVED'") :]
    hits = len(re.findall(rf"NEW\.{column}\s+IS DISTINCT FROM\s+OLD\.{column}", approved))
    assert hits >= 2, (
        f"{column} is compared {hits}x in the APPROVED branches; expected 2 "
        f"(case_id backfill, and APPROVED -> SUPERSEDED)"
    )


def test_trigger_never_uses_plain_inequality_between_new_and_old(form_trigger):
    """`NEW.x <> OLD.x` yields NULL when either side is NULL, and NULL is
    not TRUE - so a change to a nullable column passes unnoticed.
    client_site_id and change_reason are exactly that shape."""
    # Comments stripped: the rule is explained in prose inside this very
    # function, and the explanation must not count as a violation.
    offenders = re.findall(r"NEW\.\w+\s*<>\s*OLD\.\w+", _uncommented(form_trigger))
    assert offenders == [], f"use IS DISTINCT FROM, not <>: {offenders}"


def test_form_id_guard_explains_the_encryption_coupling(form_trigger):
    """form_id is the AES-GCM AAD (repositories.py binds aad=form.form_id).
    Changing it makes the stored salary permanently undecryptable, which
    is not obvious from the column name alone."""
    assert "AAD" in form_trigger


def test_approving_cannot_rewrite_content_in_the_same_statement(form_trigger):
    """Regression for F15.

    The APPROVED protections all sit inside `IF OLD.status = 'APPROVED'`.
    A single `UPDATE ... SET status='APPROVED', salary_...=...` from a
    DRAFT row skipped that block entirely, so content could be rewritten
    at the moment of approval. OnboardingForm.approve() only ever sets
    status/approved_by/approved_at.
    """
    guard = "IF OLD.status <> 'APPROVED' AND NEW.status = 'APPROVED' THEN"
    assert guard in form_trigger, "no guard on the transition INTO approved"
    section = form_trigger[form_trigger.index(guard) :]
    section = section[: section.index("END IF;")]
    for column in ["candidate_name", "salary_and_terms_encrypted", "joining_date"]:
        assert f"NEW.{column} IS DISTINCT FROM OLD.{column}" in section, (
            f"{column} can be rewritten while approving"
        )


def test_case_link_must_belong_to_the_same_tenant(form_trigger):
    """Regression for F16.

    The FK on case_id only proves the case EXISTS. Linking a form to
    another client's case would place one tenant's salary under another
    tenant's case - and every child-table policy scopes THROUGH cases,
    so that tenant could then read it.
    """
    assert "FROM cases c" in form_trigger
    assert "c.client_id = NEW.client_id" in form_trigger, (
        "case_id link is not checked against the form's tenant"
    )


def test_status_transitions_match_the_model(form_trigger):
    """The whitelist must allow exactly what the model allows, including
    DRAFT -> APPROVED (OnboardingForm.approve accepts DRAFT)."""
    for allowed in [
        "'DRAFT', 'AWAITING_APPROVAL', 'APPROVED'",
        "'AWAITING_APPROVAL', 'APPROVED', 'REJECTED'",
        "'APPROVED', 'SUPERSEDED'",
    ]:
        assert allowed in form_trigger, f"missing transition set: {allowed}"


# ----------------------------------------------------------- F3: TRUNCATE


def test_audit_log_blocks_truncate_as_well_as_update_and_delete(draft):
    """Regression for F3. TRUNCATE is not DELETE and does not fire
    row-level triggers, so BEFORE UPDATE + BEFORE DELETE alone left the
    entire audit log erasable in one statement."""
    sql = _uncommented(draft)
    assert re.search(
        r"CREATE TRIGGER audit_log_no_truncate\s+BEFORE TRUNCATE ON audit_log\s+"
        r"FOR EACH STATEMENT",
        sql,
    ), "audit_log has no statement-level BEFORE TRUNCATE trigger"


def test_audit_bypass_routes_are_documented(draft):
    """An unqualified 'append-only' claim is misleading. The roles that
    can still bypass triggers must be written down."""
    for term in ["DISABLE TRIGGER", "SUPERUSER", "BYPASSRLS", "session_replication_role"]:
        assert term in draft, f"privilege-model note does not mention {term}"


# ---------------------------------------------------------------- F4: RLS


# Tables UC-01 only ever reads. Least privilege: these must have NO
# write policy at all. (The original F4 check - "every table with a
# SELECT policy must also have INSERT" - was wrong in both directions:
# it passed vacuously for tables with no policies, and it would demand
# write access for read-only tables. Coverage is now asserted precisely
# by ADAPTER_OPERATIONS above; this is the inverse.)
READ_ONLY_TABLES = ["offers", "client_holidays", "employee_number_counters"]


@pytest.mark.parametrize("table", READ_ONLY_TABLES)
def test_read_only_tables_have_no_write_policy(draft, table):
    granted = {
        cmd for t, cmd in re.findall(r"CREATE POLICY \w+ ON (\w+) FOR (\w+)", draft) if t == table
    }
    assert granted <= {"SELECT"}, f"{table} is read-only for UC-01 but grants {sorted(granted)}"


def test_audit_log_is_insert_and_select_only(draft):
    """An append-only log must never carry an UPDATE or DELETE policy;
    absence is what denies them, on top of the triggers."""
    granted = {
        cmd
        for t, cmd in re.findall(r"CREATE POLICY \w+ ON (\w+) FOR (\w+)", draft)
        if t == "audit_log"
    }
    assert granted == {"SELECT", "INSERT"}, f"audit_log grants {sorted(granted)}"


# Commands each table must permit, taken from the Supabase adapter call
# sites (adapters/supabase/repositories.py), not guessed. `upsert` needs
# BOTH INSERT and UPDATE: an upsert with only an INSERT policy fails the
# moment the row already exists.
ADAPTER_OPERATIONS = {
    "onboarding_forms": {"SELECT", "INSERT", "UPDATE"},   # upsert + 3 reads
    "person_records": {"SELECT", "INSERT"},               # add + 3 finders
    "discrepancies": {"SELECT", "INSERT", "UPDATE"},
    "hris_records": {"SELECT", "INSERT", "UPDATE"},
    "pending_starts": {"SELECT", "INSERT", "UPDATE"},
    "offer_forms": {"SELECT", "INSERT", "UPDATE"},        # upsert
    "domain_events_outbox": {"INSERT"},
    "client_holidays": {"SELECT"},
}


@pytest.mark.parametrize("table", sorted(ADAPTER_OPERATIONS))
def test_rls_is_enabled_on_every_table_the_adapters_touch(draft, table):
    """Regression for F17.

    The earlier coverage test only looked at tables that ALREADY had a
    SELECT policy, so it passed vacuously for the 9 tables that had no
    policies at all. Those tables also had no ENABLE line - so the real
    defect was not "policies missing", it was that most tables had NO
    row-level isolation while a few did.
    """
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in draft, (
        f"{table} is written by the adapters but has no RLS enabled"
    )


@pytest.mark.parametrize("table,required", sorted(ADAPTER_OPERATIONS.items()))
def test_policies_cover_every_operation_the_adapters_perform(draft, table, required):
    granted = {
        cmd for t, cmd in re.findall(r"CREATE POLICY \w+ ON (\w+) FOR (\w+)", draft) if t == table
    }
    missing = required - granted
    assert not missing, f"{table}: adapter performs {sorted(missing)} with no policy"


def test_no_table_is_made_writable_with_a_blanket_true_policy(draft):
    """A `USING (true)` write policy would 'fix' F4 by disabling the
    control. Only audit_log's INSERT may be unconditional, and only
    because a pre-case audit row has no tenant row to join to."""
    blanket = re.findall(r"CREATE POLICY (\w+) ON (\w+) FOR (INSERT|UPDATE)[^;]*?\(true\)", draft)
    unexpected = [(p, t, c) for p, t, c in blanket if t != "audit_log"]
    assert unexpected == [], f"blanket permissive write policies: {unexpected}"


def test_rls_identity_does_not_depend_on_postgrest_only_settings(draft):
    """`request.jwt.claims` is set by PostgREST. Under a direct psycopg
    connection it is absent, so every policy would deny. The active
    helper must read the session GUC the application sets."""
    active = _uncommented(draft) + "\n".join(
        line for line in draft.splitlines() if line.strip().startswith("--")
    )
    assert "app.client_ids" in draft
    # The PostgREST form may be MENTIONED as the option-B alternative,
    # but must not be the one presented as current.
    helper = draft[draft.index("uc01_claim_client_ids() RETURNS text[]") :][:400]
    assert "request.jwt.claims" not in helper, (
        "the claim helper still reads a PostgREST-only setting"
    )


def test_pooled_connections_must_use_set_local(draft):
    """SET (not SET LOCAL) would leak one request's tenant scope into the
    next request on a pooled connection."""
    assert "SET LOCAL app.client_ids" in draft


# -------------------------------------------------- F7: adapter agreement


def test_idempotency_key_is_generated_by_the_database(draft):
    """F7, revised 2026-11-04. The live hris_sync_records.idempotency_key
    is GENERATED ALWAYS (verified: an insert that supplies it fails with
    SQLSTATE 428C9), so the draft follows the live column: the database
    derives the key, with an explicit cast so the expression is IMMUTABLE
    and byte-for-byte equal to HrisRecord._derive_idempotency_key."""
    block = draft[draft.index("CREATE TABLE hris_records") :]
    block = _uncommented(block[: block.index(");")])
    assert "idempotency_key" in block
    assert "GENERATED ALWAYS AS" in block, "the database no longer derives the key"
    assert "form_version::text" in block, "implicit integer->text cast is not IMMUTABLE"
    assert "CHECK (idempotency_key =" not in block, (
        "a CHECK on a generated column is redundant; the old design is back"
    )


def test_adapter_never_sends_idempotency_key():
    """The other half of the F7 contract: Postgres rejects an insert or
    update that supplies a GENERATED ALWAYS column, so the adapter's row
    mapping must leave it out (it still reads it back)."""
    repo = (
        _ROOT / "src" / "sub_agent" / "adapters" / "supabase" / "repositories.py"
    ).read_text(encoding="utf-8")
    assert '"idempotency_key": r.idempotency_key' not in repo


# ------------------------------------------- F8/F14: employee-number RPC


def test_allocation_function_runs_with_definer_rights_and_a_pinned_path(proposed):
    assert "SECURITY DEFINER" in proposed
    assert "SET search_path = pg_catalog, public" in proposed, (
        "a SECURITY DEFINER function without a pinned search_path is hijackable"
    )


def test_allocation_function_authorizes_the_requested_tenant(proposed):
    """Regression for F8: the tenant arrives as a parameter, so without a
    check any caller could mint numbers for any client."""
    fn = proposed[proposed.index("CREATE OR REPLACE FUNCTION next_employee_no") :]
    assert "app.client_ids" in fn
    assert "insufficient_privilege" in fn


def test_allocation_function_raises_for_an_unseeded_client(proposed):
    """Regression for F14: returning NULL could be mistaken for 'no
    number yet' and carried forward as a missing employee_no."""
    fn = proposed[proposed.index("CREATE OR REPLACE FUNCTION next_employee_no") :]
    assert "no_data_found" in fn


def test_allocation_stays_a_single_atomic_statement(proposed):
    """UPDATE ... RETURNING takes a row lock and returns post-update
    values. A SELECT-then-UPDATE pair would reintroduce the race."""
    fn = proposed[proposed.index("CREATE OR REPLACE FUNCTION next_employee_no") :]
    body = fn[: fn.index("$$;")]
    assert "UPDATE employee_number_counters" in body
    assert "RETURNING" in body
    assert "SELECT last_value" not in body


# --------------------------------------------------- F1: audit chain shape


def test_audit_chain_is_global_not_per_case(draft):
    """verify_chain asserts sequence == index over the COMPLETE log
    (audit_integrity.py). A per-case chain has gaps by construction and
    reports SEQUENCE_BREAK on an intact log."""
    sql = _uncommented(draft)
    assert "CREATE UNIQUE INDEX audit_log_sequence_uq" in sql
    assert "(case_id, sequence)" not in sql, (
        "per-case chain index is incompatible with verify_chain"
    )


def test_proposed_migration_warns_that_columns_alone_are_insufficient(proposed):
    """Adding three columns is not an implementation: a gap is a
    permanent verification failure, so allocation and insert must share
    one transaction - which PostgREST cannot provide."""
    assert "GAPS" in proposed or "gapless" in proposed.lower()
    assert "D6" in proposed
