"""Tests for scripts/verify_requirements.py in both output modes.

The script is a release gate, so two things need to hold: it still reports
PASS/FAIL honestly, and its verbose trace never puts a salary figure on a
terminal. Both modes are run once per session and the captured output is
shared, because each run drives the whole UC-01 flow end to end.
"""

import io
from contextlib import redirect_stdout

import pytest

from scripts.verify_requirements import OTHER_SALARY, SALARY, main

# The bare figures out of the two synthetic salary strings. If either of
# these turns up in the output, something printed a salary.
SALARY_FIGURE = "118000"
OTHER_SALARY_FIGURE = "999999"


def run(argv: list[str]) -> tuple[int, str]:
    """Drive the script the way a release gate does and capture its output.

    redirect_stdout rather than capsys so the fixtures below can be
    module-scoped: each call walks the whole UC-01 flow end to end, so
    doing it once per mode is worth the extra plumbing.
    """
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(argv)
    return code, buffer.getvalue()


@pytest.fixture(scope="module")
def concise_run():
    return run([])


@pytest.fixture(scope="module")
def verbose_run():
    return run(["--verbose"])


# --- both modes agree on the verdict --------------------------------------


def test_concise_run_passes(concise_run):
    code, out = concise_run
    assert code == 0, out
    assert "ALL REQUIREMENTS VERIFIED" in out


def test_verbose_run_passes(verbose_run):
    code, out = verbose_run
    assert code == 0, out
    assert "ALL REQUIREMENTS VERIFIED" in out


def test_both_modes_report_the_same_per_requirement_totals(concise_run, verbose_run):
    def totals(text):
        return sorted(
            line.strip() for line in text.splitlines() if "checks passed" in line
        )

    assert totals(concise_run[1]) == totals(verbose_run[1])


def test_every_mapped_requirement_is_covered(concise_run):
    _, out = concise_run
    for requirement in ("R1", "R2", "R3", "R4", "R5", "R6", "OUT"):
        assert f"{requirement}:" in out


# --- redaction ------------------------------------------------------------


@pytest.mark.parametrize("figure", [SALARY_FIGURE, OTHER_SALARY_FIGURE])
def test_verbose_output_never_prints_a_salary_figure(verbose_run, figure):
    _, out = verbose_run
    assert figure not in out


@pytest.mark.parametrize("salary", [SALARY, OTHER_SALARY])
def test_verbose_output_never_prints_a_salary_string(verbose_run, salary):
    _, out = verbose_run
    assert salary not in out


def test_verbose_output_shows_the_mask_where_salary_would_have_been(verbose_run):
    """Masked, not silently dropped: a reader must see that something was
    withheld rather than assume the field was empty."""
    _, out = verbose_run
    assert "***REDACTED***" in out


def test_concise_output_never_prints_a_salary_figure(concise_run):
    _, out = concise_run
    assert SALARY_FIGURE not in out
    assert OTHER_SALARY_FIGURE not in out


# --- the trace is actually a trace ----------------------------------------


def test_verbose_output_is_substantially_longer_than_concise(concise_run, verbose_run):
    assert len(verbose_run[1].splitlines()) > 10 * len(concise_run[1].splitlines())


@pytest.mark.parametrize(
    "label",
    [
        "[INPUT]",
        "[PROCESSING]",
        "[BUSINESS RULE]",
        "[APPROVAL REQUIRED]",
        "[APPROVAL DECISION]",
        "[STATE CHANGE]",
        "[EXTERNAL OP]",
        "[OUTPUT]",
        "[AUDIT EVENT]",
        "[ASSERTION RESULT]",
    ],
)
def test_verbose_output_carries_every_section_label(verbose_run, label):
    _, out = verbose_run
    assert label in out


def test_verbose_output_shows_expected_against_actual_for_assertions(verbose_run):
    _, out = verbose_run
    assert "[ASSERTION RESULT] PASS" in out
    assert "expected :" in out
    assert "actual   :" in out


def test_verbose_output_labels_external_writes_as_simulated(verbose_run):
    """No live Zoho tenant is touched by a verification run, and the
    report must say so rather than let a green write imply otherwise.

    The mode sits on each per-record operation line, not just on the
    batch header, so a single line can never be read without it. See
    tests/test_verify_summary.py for the per-operation coverage.
    """
    _, out = verbose_run
    assert "- [SIMULATED] ZOHO_PEOPLE" in out
    assert "- [SIMULATED] ZOHO_PAYROLL" in out
    assert "StubHrisGateway" in out
    assert "[LIVE]" not in out


def test_verbose_output_names_the_approver_roles_from_policy(verbose_run):
    _, out = verbose_run
    assert "allowed_roles (policy)" in out
    assert "HR_MANAGER" in out


@pytest.mark.parametrize(
    "scenario_id",
    [
        "SCENARIO R1-A",  # acceptance without an approved form
        "SCENARIO R1-B",  # approved form without acceptance
        "SCENARIO R1-C",  # both conditions satisfied
        "SCENARIO VAL-1a",  # a missing field
        "SCENARIO VAL-1b",  # an invalid field combination
        "SCENARIO R6",  # a discrepancy needing HR resolution
        "SCENARIO R5-A",  # a duplicate acceptance
        "SCENARIO R5-B",  # a rehire
        "SCENARIO R5-C",  # an internal transfer
        "SCENARIO R5-D",  # a detail change awaiting approval
        "SCENARIO R4-UPDATE",  # an approved post-write update
        "SCENARIO PEND-1",  # a pending approval blocking an operation
        "SCENARIO REJ-1",  # a rejected approval
        "SCENARIO WRITE-1",  # a failed then successful external write
    ],
)
def test_verbose_output_covers_each_required_scenario(verbose_run, scenario_id):
    _, out = verbose_run
    assert scenario_id in out


def test_verbose_output_reports_payloads_for_both_target_systems(verbose_run):
    _, out = verbose_run
    assert "ZOHO_PEOPLE" in out
    assert "ZOHO_PAYROLL" in out
    # The payroll payload names its sensitive field without carrying a value.
    assert "compensation_terms" in out


def test_verbose_output_reports_a_real_failure_path(verbose_run):
    """The failed-write scenario must show the actual error, not a
    sanitised success."""
    _, out = verbose_run
    assert "[ERROR]" in out
    assert "HrisUnavailableError" in out
