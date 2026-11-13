"""Tests for the reporting layer added on top of the basic tracer:
the run ledger, the end-of-run summary, approver-vs-policy checking,
the before/after transition table and per-operation external reporting.

The theme throughout is that the report must not overstate what ran. A
skipped record must not read as a completed one, a stand-in must not read
as a live system, and an approval addressed to someone who cannot action
it must be flagged rather than quietly rendered.

Synthetic values only.
"""

import io
from datetime import datetime, timezone

from sub_agent.adapters.memory import StubHrisGateway
from sub_agent.trace import (
    MASK,
    NOT_CAPTURED,
    Redactor,
    RunLedger,
    Tracer,
    check_approver_against_policy,
    derived,
    render_summary,
)

SYNTHETIC_SALARY = "Base: 118000 SYNTHETIC/yr; benefits: standard"


def render(**kwargs) -> tuple[Tracer, io.StringIO]:
    stream = io.StringIO()
    return Tracer(enabled=True, stream=stream, **kwargs), stream


def summary_text(ledger: RunLedger, unverified: list[str] | None = None) -> str:
    lines: list[str] = []
    render_summary(ledger, unverified or [], write_line=lines.append)
    return "\n".join(lines)


# --- run ledger -----------------------------------------------------------


def test_ledger_tallies_assertions():
    ledger = RunLedger()
    ledger.record_assertion(True, "R1", "a")
    ledger.record_assertion(False, "R1", "b")
    ledger.record_assertion(True, "R4", "c")
    assert len(ledger.assertions) == 3
    assert ledger.passed == 2
    assert ledger.failed == 1


def test_ledger_counts_external_ops_by_mode():
    ledger = RunLedger()
    ledger.record_external_op("SIMULATED", "StubHrisGateway", count=2)
    ledger.record_external_op("NOT_ATTEMPTED", "StubHrisGateway")
    assert ledger.external_ops == {"SIMULATED": 2, "NOT_ATTEMPTED": 1}
    assert ledger.external_gateways == {"StubHrisGateway"}


def test_ledger_counts_each_approval_once_at_its_last_status():
    """An approval reported PENDING and later APPROVED is one approval that
    ended APPROVED, not one pending plus one approved."""
    ledger = RunLedger()
    ledger.record_approval("HC_05_HRIS_WRITE", "PENDING", "ap-1")
    ledger.record_approval("HC_05_HRIS_WRITE", "APPROVED", "ap-1")
    ledger.record_approval("HC_12_VARIANT", "PENDING", "ap-2")
    outcomes = ledger.approval_outcomes()
    assert outcomes["HC_05_HRIS_WRITE"] == {"APPROVED": 1}
    assert outcomes["HC_12_VARIANT"] == {"PENDING": 1}


def test_tracer_records_totals_even_when_printing_is_off():
    """The concise run must report the same totals as the verbose one."""
    ledger = RunLedger()
    tracer = Tracer(enabled=False, ledger=ledger)
    tracer.scenario("S1", "R1", "p", "e")
    tracer.external_op("write", StubHrisGateway())
    assert ledger.scenarios == ["S1"]
    assert ledger.external_ops["SIMULATED"] == 1


# --- summary --------------------------------------------------------------


def test_render_summary_reports_every_required_section():
    ledger = RunLedger()
    ledger.record_scenario("S1")
    ledger.record_assertion(True, "R4", "ok")
    ledger.record_external_op("SIMULATED", "StubHrisGateway")
    ledger.record_approval("HC_05_HRIS_WRITE", "APPROVED", "ap-1")
    text = summary_text(ledger, ["nothing is wired to a real system"])
    assert "Scenarios run      : 1" in text
    assert "Assertions         : 1 total, 1 passed, 0 failed" in text
    assert "External operation modes used:" in text
    assert "Approval outcomes" in text
    assert "HC_05_HRIS_WRITE   APPROVED=1" in text
    assert "Incomplete or unverified integration paths:" in text
    assert "nothing is wired to a real system" in text


def test_summary_breaks_assertions_down_per_requirement():
    ledger = RunLedger()
    ledger.record_assertion(True, "R4", "a")
    ledger.record_assertion(False, "R4", "b")
    text = summary_text(ledger)
    assert "R4   1 passed, 1 FAILED" in text


def test_summary_states_plainly_when_nothing_live_ran():
    ledger = RunLedger()
    ledger.record_external_op("SIMULATED", "StubHrisGateway")
    ledger.record_external_op("NOT_ATTEMPTED", "StubHrisGateway")
    assert "NO LIVE OR MOCKED EXTERNAL CALL WAS MADE" in summary_text(ledger)


def test_summary_does_not_claim_nothing_live_when_a_live_call_ran():
    ledger = RunLedger()
    ledger.record_external_op("LIVE", "ZohoPeopleGateway")
    assert "NO LIVE OR MOCKED EXTERNAL CALL WAS MADE" not in summary_text(ledger)


def test_summary_does_not_claim_nothing_live_when_a_mocked_call_ran():
    ledger = RunLedger()
    ledger.record_external_op("MOCKED", "MockHrisGateway")
    assert "NO LIVE OR MOCKED EXTERNAL CALL WAS MADE" not in summary_text(ledger)


def test_summary_reports_absence_of_policy_flags_explicitly():
    assert "matched the policy roles" in summary_text(RunLedger())


def test_summary_lists_policy_flags_when_present():
    ledger = RunLedger()
    ledger.record_policy_flag(
        {
            "verdict": "MISMATCH",
            "checkpoint": "HC_05_HRIS_WRITE",
            "approval_id": "ap-1",
            "detail": "holds HR_OFFICER but policy permits HR_MANAGER",
        }
    )
    text = summary_text(ledger)
    assert "[MISMATCH] HC_05_HRIS_WRITE ap-1" in text
    assert "holds HR_OFFICER but policy permits HR_MANAGER" in text


# --- approver vs policy ----------------------------------------------------


class StubApproval:
    def __init__(self, requested_to="hr-manager-1", checkpoint=None, approval_id="ap-1"):
        self.requested_to = requested_to
        self.checkpoint = checkpoint
        self.approval_id = approval_id
        self.requested_by = "hr-officer-1"
        self.status = "PENDING"
        self.subject_ref = "hris:case-1:v1"
        self.created_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
        self.decided_by = None
        self.decided_at = None
        self.decision_note = None
        self.reason = None


def test_approver_matching_policy_is_reported_ok():
    verdict = check_approver_against_policy(
        StubApproval(requested_to="hr-manager-1"), ["HR_MANAGER"], {"hr-manager-1": "HR_MANAGER"}
    )
    assert verdict["verdict"] == "OK"
    assert verdict["requested_to_role"] == "HR_MANAGER"


def test_approver_outside_policy_is_reported_as_a_mismatch():
    verdict = check_approver_against_policy(
        StubApproval(requested_to="hr-officer-1"), ["HR_MANAGER"], {"hr-officer-1": "HR_OFFICER"}
    )
    assert verdict["verdict"] == "MISMATCH"
    assert "cannot action the approval" in verdict["detail"]


def test_unknown_actor_role_is_reported_unknown_not_guessed():
    verdict = check_approver_against_policy(
        StubApproval(requested_to="someone-else"), ["HR_MANAGER"], {}
    )
    assert verdict["verdict"] == "UNKNOWN"
    assert verdict["requested_to_role"] == NOT_CAPTURED


def test_policy_check_flags_a_mismatch_into_the_ledger():
    ledger = RunLedger()
    tracer, _ = render(ledger=ledger)
    tracer.policy_check(
        check_approver_against_policy(
            StubApproval(requested_to="hr-officer-1"),
            ["HR_MANAGER"],
            {"hr-officer-1": "HR_OFFICER"},
        )
    )
    assert len(ledger.policy_flags) == 1


def test_policy_check_does_not_flag_a_match():
    ledger = RunLedger()
    tracer, _ = render(ledger=ledger)
    tracer.policy_check(
        check_approver_against_policy(
            StubApproval(requested_to="hr-manager-1"),
            ["HR_MANAGER"],
            {"hr-manager-1": "HR_MANAGER"},
        )
    )
    assert ledger.policy_flags == []


# --- transition table ------------------------------------------------------


def test_transition_shows_changed_and_unchanged_fields():
    tracer, stream = render()
    tracer.transition(
        "flow state",
        {"case.stage": "READY_TO_PREPARE", "employees": 0},
        {"case.stage": "RECORD_CREATED", "employees": 0},
    )
    text = stream.getvalue()
    assert "READY_TO_PREPARE  ->  RECORD_CREATED" in text
    assert "0   (unchanged)" in text


def test_transition_masks_sensitive_values_on_both_sides():
    redactor = Redactor()
    redactor.register_secret(SYNTHETIC_SALARY)
    tracer, stream = render(redactor=redactor)
    tracer.transition("pay", {"salary_and_terms": "old"}, {"salary_and_terms": SYNTHETIC_SALARY})
    text = stream.getvalue()
    assert "118000" not in text
    assert MASK in text


# --- per-operation external reporting -------------------------------------


def test_external_operations_labels_each_call_with_the_gateway_mode():
    ledger = RunLedger()
    tracer, stream = render(ledger=ledger)
    tracer.external_operations(
        "write batch",
        StubHrisGateway(),
        [
            {"system": "ZOHO_PEOPLE", "operation": "gateway.write()", "external_ref": "ext-1"},
            {"system": "ZOHO_PAYROLL", "operation": "gateway.write()", "external_ref": "ext-2"},
        ],
        employees_before=0,
        employees_after=2,
    )
    text = stream.getvalue()
    assert "- [SIMULATED] ZOHO_PEOPLE gateway.write()" in text
    assert "- [SIMULATED] ZOHO_PAYROLL gateway.write()" in text
    assert "distinct employees in the target system: 0 -> 2" in text
    assert ledger.external_ops["SIMULATED"] == 2


def test_external_operations_spells_out_what_simulated_means():
    tracer, stream = render()
    tracer.external_operations(
        "batch", StubHrisGateway(), [{"system": "ZOHO_PEOPLE", "operation": "gateway.write()"}]
    )
    assert "No record exists in any Zoho tenant" in stream.getvalue()


def test_an_uncalled_operation_is_labelled_not_attempted_not_simulated():
    """A skipped record must never read as one that ran."""
    ledger = RunLedger()
    tracer, stream = render(ledger=ledger)
    tracer.external_operations(
        "batch",
        StubHrisGateway(),
        [
            {"_called": True, "system": "ZOHO_PEOPLE", "operation": "gateway.write()"},
            {
                "_called": False,
                "system": "ZOHO_PAYROLL",
                "operation": "gateway.write() -- NOT CALLED",
            },
        ],
    )
    text = stream.getvalue()
    assert "- [SIMULATED] ZOHO_PEOPLE" in text
    assert "- [NOT ATTEMPTED] ZOHO_PAYROLL" in text
    assert ledger.external_ops == {"SIMULATED": 1, "NOT_ATTEMPTED": 1}


def test_external_operations_hides_internal_markers_from_the_output():
    tracer, stream = render()
    tracer.external_operations(
        "batch", StubHrisGateway(), [{"_called": True, "system": "ZOHO_PEOPLE", "operation": "w"}]
    )
    assert "_called" not in stream.getvalue()


def test_external_operations_redacts_sensitive_operation_fields():
    redactor = Redactor()
    redactor.register_secret(SYNTHETIC_SALARY)
    tracer, stream = render(redactor=redactor)
    tracer.external_operations(
        "batch",
        StubHrisGateway(),
        [
            {
                "system": "ZOHO_PAYROLL",
                "operation": "gateway.write()",
                "compensation_terms": SYNTHETIC_SALARY,
                "note": f"sending {SYNTHETIC_SALARY}",
            }
        ],
    )
    text = stream.getvalue()
    assert "118000" not in text
    assert MASK in text


def test_external_op_not_attempted_counts_separately_and_says_why():
    ledger = RunLedger()
    tracer, stream = render(ledger=ledger)
    tracer.external_op_not_attempted(
        "Zoho write", StubHrisGateway(), why="the approval is still PENDING"
    )
    text = stream.getvalue()
    assert "[EXTERNAL OP] [NOT ATTEMPTED] Zoho write" in text
    assert "the approval is still PENDING" in text
    assert ledger.external_ops == {"NOT_ATTEMPTED": 1}
    assert "SIMULATED" not in ledger.external_ops


# --- approval lifecycle ----------------------------------------------------


def test_approval_required_shows_status_before_and_undecided_fields():
    tracer, stream = render()
    tracer.approval_required(
        StubApproval(), why="a reason", blocks="execute_hris_write", approver_roles=["HR_MANAGER"]
    )
    text = stream.getvalue()
    assert "status BEFORE decision" in text
    assert "<undecided>" in text
    assert "blocked while PENDING" in text
    assert "requested_by (actor)" in text
    assert "requested_to (intended approver)" in text


def test_approval_required_reports_a_missing_requester_honestly():
    approval = StubApproval()
    approval.requested_by = None
    tracer, stream = render()
    tracer.approval_required(approval, why="w", blocks="b", approver_roles=["HR_MANAGER"])
    assert NOT_CAPTURED in stream.getvalue()


def test_approval_decision_shows_the_observed_status_transition():
    approval = StubApproval()
    approval.status = "APPROVED"
    approval.decided_by = "hr-manager-1"
    approval.decided_at = datetime(2026, 4, 2, tzinfo=timezone.utc)
    tracer, stream = render()
    tracer.approval_decision(
        approval,
        effect="records -> APPROVED",
        next_step="write",
        status_before="PENDING",
        blocked_if_rejected="prepare stays BLOCKED_HRIS_REJECTED",
        resulting_state={"ZOHO_PEOPLE record status": "APPROVED"},
    )
    text = stream.getvalue()
    assert "status transition                : PENDING -> APPROVED" in text
    assert "blocked while REJECTED" in text
    assert "resulting state" in text
    assert "<none given>" in text


def test_approval_sections_feed_the_ledger():
    ledger = RunLedger()
    tracer = Tracer(enabled=False, ledger=ledger)
    tracer.approval_required(
        StubApproval(checkpoint="HC_05_HRIS_WRITE"),
        why="w",
        blocks="b",
        approver_roles=["HR_MANAGER"],
    )
    assert ledger.approval_events[0]["checkpoint"] == "HC_05_HRIS_WRITE"


# --- honest labelling ------------------------------------------------------


def test_derived_names_what_computes_the_value():
    assert "case_status.derive_stage" in derived("case_status.derive_stage")
    assert "not stored" in derived("x")
