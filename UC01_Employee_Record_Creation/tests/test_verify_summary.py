"""Tests for the end-of-run summary and the refined trace sections in
scripts/verify_requirements.py.

These run the real script and assert on its output, so they fail if the
report starts overstating what happened -- claiming a live Zoho call,
hiding an approval that cannot be actioned by the actor it is addressed
to, or labelling a skipped record as a completed one.
"""

import re

import pytest

from tests.test_verify_requirements import run


@pytest.fixture(scope="module")
def verbose_run():
    return run(["--verbose"])


@pytest.fixture(scope="module")
def concise_run():
    return run([])


# --- the summary itself ----------------------------------------------------


@pytest.mark.parametrize(
    "section",
    [
        "RUN SUMMARY",
        "Scenarios run",
        "Assertions",
        "External operation modes used:",
        "Approval outcomes (final status per approval):",
        "Approval policy flags:",
        "Incomplete or unverified integration paths:",
    ],
)
def test_summary_is_present_in_both_modes(concise_run, verbose_run, section):
    assert section in concise_run[1]
    assert section in verbose_run[1]


def test_summary_totals_match_between_modes(concise_run, verbose_run):
    """The concise run tallies the same work; only the printing differs."""

    def totals(text):
        return re.findall(r"^(Scenarios run.*|Assertions.*)$", text, re.MULTILINE)

    assert totals(concise_run[1]) == totals(verbose_run[1])


def test_summary_assertion_total_matches_the_per_requirement_breakdown(verbose_run):
    _, out = verbose_run
    total = int(re.search(r"Assertions\s+: (\d+) total", out).group(1))
    per_requirement = sum(
        int(n) for n in re.findall(r"^  (?:OUT|R\d)\s+(\d+) passed", out, re.MULTILINE)
    )
    assert total == per_requirement


def test_summary_reports_no_failures_on_a_green_run(verbose_run):
    code, out = verbose_run
    assert code == 0
    assert re.search(r"Assertions\s+: \d+ total, \d+ passed, 0 failed", out)


# --- external operation modes ----------------------------------------------


def test_summary_states_that_nothing_live_or_mocked_ran(verbose_run):
    _, out = verbose_run
    assert "NO LIVE OR MOCKED EXTERNAL CALL WAS MADE" in out
    assert "gateways       : StubHrisGateway" in out


def test_no_external_operation_is_ever_labelled_live(verbose_run):
    _, out = verbose_run
    assert "[LIVE]" not in out
    assert "\n  LIVE " not in out


def test_every_target_system_operation_line_carries_a_mode(verbose_run):
    """Each per-record line under an EXTERNAL OP batch must be prefixed
    with SIMULATED, MOCKED, LIVE or NOT ATTEMPTED -- never bare."""
    _, out = verbose_run
    operation_lines = [
        line
        for line in out.splitlines()
        if line.strip().startswith("- [") and ("ZOHO_PEOPLE" in line or "ZOHO_PAYROLL" in line)
    ]
    assert operation_lines, "expected per-record external operation lines"
    for line in operation_lines:
        assert re.search(r"- \[(SIMULATED|MOCKED|LIVE|NOT ATTEMPTED)\]", line), line


def test_both_target_systems_appear_as_their_own_operations(verbose_run):
    _, out = verbose_run
    assert re.search(r"- \[SIMULATED\] ZOHO_PEOPLE gateway\.write\(\)", out)
    assert re.search(r"- \[SIMULATED\] ZOHO_PAYROLL gateway\.write\(\)", out)
    assert re.search(r"- \[SIMULATED\] ZOHO_PEOPLE gateway\.update\(external_ref=", out)
    assert re.search(r"- \[SIMULATED\] ZOHO_PAYROLL gateway\.update\(external_ref=", out)


def test_a_simulated_batch_says_no_zoho_record_exists(verbose_run):
    _, out = verbose_run
    assert "No record exists in any Zoho tenant" in out


def test_a_skipped_record_is_labelled_not_attempted(verbose_run):
    """Payroll is skipped when the people write fails; it must not be
    rendered as a SIMULATED call that happened."""
    _, out = verbose_run
    assert "- [NOT ATTEMPTED] ZOHO_PAYROLL gateway.write() -- NOT CALLED" in out


def test_blocked_and_rejected_paths_record_that_no_call_was_made(verbose_run):
    _, out = verbose_run
    assert out.count("[EXTERNAL OP] [NOT ATTEMPTED]") >= 3
    assert "execute_hris_write was never called" in out


def test_gateway_call_evidence_comes_from_audit_entries(verbose_run):
    """A call is reported because an audit entry recorded its result, not
    because a record has an external_ref."""
    _, out = verbose_run
    assert "yes -- gateway returned, audited HRIS_WRITE_SUCCEEDED" in out
    assert "yes -- gateway raised, audited HRIS_WRITE_FAILED" in out
    assert "no -- no call result was audited for this record" in out


# --- approval lifecycle ----------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "approval_id",
        "checkpoint",
        "subject_ref (subject)",
        "requested_by (actor)",
        "requested_to (intended approver)",
        "allowed_roles (policy)",
        "status BEFORE decision",
        "blocked while PENDING",
    ],
)
def test_every_approval_gate_reports_its_full_identity(verbose_run, field):
    _, out = verbose_run
    assert field in out


@pytest.mark.parametrize(
    "field",
    [
        "status transition",
        "decided_by (actor)",
        "decided_at",
        "decision_note",
        "blocked while REJECTED",
        "resulting state",
    ],
)
def test_every_approval_decision_reports_its_outcome(verbose_run, field):
    _, out = verbose_run
    assert field in out


def test_approval_outcomes_cover_every_checkpoint_the_run_exercises(verbose_run):
    _, out = verbose_run
    for checkpoint in ("HC_01_FORM", "HC_04_DATA", "HC_05_HRIS_WRITE", "HC_12_VARIANT"):
        assert re.search(rf"^  {checkpoint}\s", out, re.MULTILINE)


def test_a_rejected_approval_is_reported_as_rejected(verbose_run):
    _, out = verbose_run
    assert re.search(r"HC_05_HRIS_WRITE\s+.*REJECTED=1", out)


def test_an_undecided_approval_is_reported_as_pending(verbose_run):
    """The rehire reactivation gate is deliberately left PENDING."""
    _, out = verbose_run
    assert re.search(r"HC_12_VARIANT\s+.*PENDING=1", out)


# --- approval policy -------------------------------------------------------


def test_the_hc05_approver_mismatch_is_flagged_not_hidden(verbose_run):
    """hris_prepare addresses the HC_05 approval to the case HR officer,
    but the policy permits only HR_MANAGER to decide it. The report must
    surface that rather than quietly print whatever it found."""
    _, out = verbose_run
    assert "[POLICY CHECK] approver vs policy: MISMATCH" in out
    assert "cannot action the approval addressed to them" in out
    assert "[MISMATCH] HC_05_HRIS_WRITE" in out


def test_policy_checks_read_the_allowed_roles_from_the_policy_file(verbose_run):
    _, out = verbose_run
    assert "allowed_roles (policy) : [\"HR_MANAGER\"]" in out


def test_matching_approvers_are_reported_ok(verbose_run):
    _, out = verbose_run
    assert "[POLICY CHECK] approver vs policy: OK" in out


# --- state transitions -----------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "case.stage",
        "form.version_in_force",
        "approval.status",
        "hris.record_status",
        "hris.external_ref",
        "hris.employees_in_target_system",
    ],
)
def test_the_transition_table_covers_every_required_field(verbose_run, field):
    _, out = verbose_run
    assert field in out


def test_transitions_show_unchanged_fields_too(verbose_run):
    """"the employee count did not move" is the duplicate-prevention
    evidence, so unchanged fields must still be printed."""
    _, out = verbose_run
    assert "(unchanged)" in out
    assert "  ->  " in out


def test_a_derived_value_is_labelled_as_derived(verbose_run):
    _, out = verbose_run
    assert "derived at read time by case_status.derive_stage, not stored" in out


def test_a_value_the_implementation_does_not_store_is_labelled_honestly(verbose_run):
    _, out = verbose_run
    assert "<not captured by the implementation>" in out
    assert "HrisRecord stores approved_at, but no write timestamp" in out


# --- failure handling ------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "failed operation",
        "exception class (no message is ever stored)",
        "affected record(s)",
        "not attempted after the failure",
        "recovery action",
    ],
)
def test_the_failure_scenario_reports_every_required_detail(verbose_run, field):
    _, out = verbose_run
    assert field in out


def test_the_failure_reports_the_exception_class_only(verbose_run):
    _, out = verbose_run
    assert "HrisUnavailableError" in out
    # The stub raises "stub write failure for ZOHO_PEOPLE"; that message is
    # never stored on the record and must not reach the report.
    assert "stub write failure" not in out


def test_duplicate_prevention_evidence_is_shown_for_update_and_retry(verbose_run):
    _, out = verbose_run
    assert out.count("duplicate-prevention evidence") >= 2
    assert "distinct employees before -> after : 2 -> 2" in out
    assert "distinct employees before -> after : 0 -> 2" in out


def test_the_blocked_write_reports_an_exception_class_not_a_bare_message(verbose_run):
    _, out = verbose_run
    assert "exception class" in out
    assert "ValueError" in out
