"""Tests for the verification reporter's formatting and redaction.

The reporter only ever prints, so the thing worth testing hard is that a
sensitive value cannot reach the terminal: salary, bank and loan data stay
inside HR and payroll scopes (CLAUDE.md), and a verbose verification run
is read in a terminal, pasted into CI logs and screenshotted.

Synthetic values only.
"""

import io
from datetime import date, datetime, timezone

import pytest
from pydantic import SecretStr

from sub_agent.adapters.memory import InMemoryAuditLog, StubHrisGateway
from sub_agent.models import ActorType, AuditEntry
from sub_agent.trace import (
    MASK,
    AuditTap,
    Redactor,
    Tracer,
    audit_summary,
    diff_states,
    external_mode,
    external_mode_detail,
    is_sensitive_field,
)

SYNTHETIC_SALARY = "Base: 118000 SYNTHETIC/yr; benefits: standard"


def render(**tracer_kwargs) -> tuple[Tracer, io.StringIO]:
    stream = io.StringIO()
    tracer = Tracer(enabled=True, stream=stream, **tracer_kwargs)
    return tracer, stream


# --- field-name based redaction -------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "salary_and_terms",
        "compensation_terms",
        "SALARY_AND_TERMS",
        "employee_salary",
        "bank_account",
        "iban",
        "loan_amount",
        "access_token",
        "api_key",
        "client_secret",
        "other_fingerprint",
    ],
)
def test_sensitive_field_names_are_recognised(name):
    assert is_sensitive_field(name)


@pytest.mark.parametrize(
    "name", ["candidate_name", "designation", "joining_date", "employee_no", "case_id"]
)
def test_ordinary_field_names_are_not_sensitive(name):
    assert not is_sensitive_field(name)


def test_sensitive_key_masks_its_value_without_registration():
    # Nothing registered: the field NAME alone must be enough.
    redactor = Redactor()
    out = redactor.redact({"salary_and_terms": "Base: 777777 SYNTHETIC/yr"})
    assert out == {"salary_and_terms": MASK}


def test_secretstr_is_masked_anywhere_it_appears():
    redactor = Redactor()
    out = redactor.redact({"anything": SecretStr(SYNTHETIC_SALARY)})
    assert out == {"anything": MASK}
    assert "118000" not in str(out)


def test_nested_sensitive_values_are_masked():
    redactor = Redactor()
    out = redactor.redact(
        {"records": [{"system": "ZOHO_PAYROLL", "payload": {"compensation_terms": "x"}}]}
    )
    assert out["records"][0]["payload"]["compensation_terms"] == MASK
    assert out["records"][0]["system"] == "ZOHO_PAYROLL"


# --- literal based redaction ----------------------------------------------


def test_registered_secret_is_scrubbed_from_free_text():
    redactor = Redactor()
    redactor.register_secret(SYNTHETIC_SALARY)
    # The same text under an entirely innocuous key, e.g. an approval reason.
    out = redactor.redact({"reason": f"offer says {SYNTHETIC_SALARY}"})
    assert SYNTHETIC_SALARY not in out["reason"]
    assert MASK in out["reason"]


def test_registering_a_secret_also_scrubs_the_bare_figure():
    """A leak is usually partial: just the number, not the whole string."""
    redactor = Redactor()
    redactor.register_secret(SYNTHETIC_SALARY)
    assert "118000" not in redactor.scrub_text("the amount is 118000 per year")


def test_register_secret_accepts_secretstr():
    redactor = Redactor()
    redactor.register_secret(SecretStr(SYNTHETIC_SALARY))
    assert "118000" not in redactor.scrub_text("leaked 118000")


def test_register_secret_ignores_empty_values():
    redactor = Redactor()
    redactor.register_secret(None)
    redactor.register_secret("   ")
    assert redactor.registered == frozenset()
    assert redactor.scrub_text("untouched") == "untouched"


def test_short_and_digitless_tokens_are_not_registered():
    """Masking "Base" or "standard" would make the report unreadable and
    protects nothing."""
    redactor = Redactor()
    redactor.register_secret(SYNTHETIC_SALARY)
    assert redactor.scrub_text("Base rate is standard") == "Base rate is standard"


def test_longest_literal_wins_so_partial_masking_does_not_shadow_the_whole():
    redactor = Redactor()
    redactor.register_secret(SYNTHETIC_SALARY)
    out = redactor.scrub_text(SYNTHETIC_SALARY)
    assert out == MASK


# --- general redaction behaviour ------------------------------------------


def test_redact_renders_models_dates_and_enums_json_safely():
    redactor = Redactor()
    out = redactor.redact(
        {
            "when": datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
            "day": date(2026, 4, 1),
            "actor": ActorType.AGENT,
            "flag": True,
            "count": 2,
            "nothing": None,
        }
    )
    assert out["when"].startswith("2026-04-01T12:00:00")
    assert out["day"] == "2026-04-01"
    assert out["actor"] == "AGENT"
    assert out["flag"] is True and out["count"] == 2 and out["nothing"] is None


def test_dumps_produces_indented_json():
    redactor = Redactor()
    text = redactor.dumps({"a": {"b": 1}})
    assert '\n  "a": {' in text


# --- external operation labelling -----------------------------------------


def test_stub_gateway_is_labelled_simulated():
    assert external_mode(StubHrisGateway()) == "SIMULATED"


def test_external_mode_reports_the_class_actually_wired_in():
    detail = external_mode_detail(StubHrisGateway())
    assert detail["gateway_class"] == "StubHrisGateway"
    assert detail["gateway_module"] == "sub_agent.adapters.memory"
    assert detail["mode"] == "SIMULATED"


def test_unknown_gateway_is_labelled_live():
    """Fail loud, not quiet: anything not recognisably a stand-in is
    reported as LIVE so a real connector is never mislabelled."""

    class ZohoPeopleGateway:
        pass

    assert external_mode(ZohoPeopleGateway()) == "LIVE"


def test_mock_connector_is_labelled_mocked():
    class MockHrisGateway:
        pass

    assert external_mode(MockHrisGateway()) == "MOCKED"


# --- diffing and audit taps -----------------------------------------------


def test_diff_states_reports_only_what_changed():
    diff = diff_states({"a": 1, "b": 2}, {"a": 1, "b": 3})
    assert diff == {"b": {"before": 2, "after": 3}}


def test_diff_states_marks_added_and_removed_keys():
    diff = diff_states({"gone": 1}, {"added": 2})
    assert diff["gone"] == {"before": 1, "after": "<absent>"}
    assert diff["added"] == {"before": "<absent>", "after": 2}


def make_entry(action: str, after: dict | None = None) -> AuditEntry:
    return AuditEntry(
        request_id="rq-synthetic",
        actor_type=ActorType.AGENT,
        actor_id="uc01-test",
        action=action,
        after=after,
        reference="rq-synthetic",
    )


def test_audit_tap_returns_only_entries_appended_after_the_mark():
    log = InMemoryAuditLog()
    log.append(make_entry("BEFORE"))
    tap = AuditTap(log)
    log.append(make_entry("AFTER_ONE"))
    log.append(make_entry("AFTER_TWO"))
    assert [e.action for e in tap.new_entries()] == ["AFTER_ONE", "AFTER_TWO"]
    tap.reset()
    assert tap.new_entries() == []


def test_audit_summary_redacts_entry_payloads():
    redactor = Redactor()
    redactor.register_secret(SYNTHETIC_SALARY)
    summary = audit_summary([make_entry("LEAKY", {"note": SYNTHETIC_SALARY})], redactor)
    assert summary[0]["action"] == "LEAKY"
    assert "118000" not in str(summary)


# --- the tracer -----------------------------------------------------------


def test_disabled_tracer_writes_nothing():
    stream = io.StringIO()
    tracer = Tracer(enabled=False, stream=stream)
    tracer.scenario("S1", "R1", "purpose", "expected")
    tracer.input("anything", data={"a": 1})
    tracer.assertion(True, "R1", "label")
    assert stream.getvalue() == ""


def test_emit_rejects_an_unknown_section_kind():
    tracer, _ = render()
    with pytest.raises(ValueError, match="unknown trace section kind"):
        tracer.emit("MADE_UP", "title")


def test_scenario_header_carries_requirement_purpose_and_expectation():
    tracer, stream = render()
    tracer.scenario("R1-A", "R1: start only after...", "a purpose", "an expectation")
    text = stream.getvalue()
    assert "SCENARIO R1-A" in text
    assert "REQUIREMENT : R1: start only after..." in text
    assert "PURPOSE     : a purpose" in text
    assert "EXPECTED    : an expectation" in text


def test_tracer_masks_sensitive_fields_in_output():
    redactor = Redactor()
    redactor.register_secret(SYNTHETIC_SALARY)
    tracer, stream = render(redactor=redactor)
    tracer.input(
        "form",
        data={"designation": "Engineer", "salary_and_terms": SYNTHETIC_SALARY},
        fields={"salary_and_terms": SecretStr(SYNTHETIC_SALARY)},
    )
    text = stream.getvalue()
    assert "118000" not in text
    assert "Engineer" in text
    assert MASK in text


def test_tracer_masks_a_secret_appearing_in_a_section_title():
    redactor = Redactor()
    redactor.register_secret(SYNTHETIC_SALARY)
    tracer, stream = render(redactor=redactor)
    tracer.processing(f"writing {SYNTHETIC_SALARY} to payroll")
    assert "118000" not in stream.getvalue()


def test_assertion_section_shows_expected_and_actual():
    tracer, stream = render()
    tracer.assertion(False, "R4", "write completed", expected="2 written", actual="1 written")
    text = stream.getvalue()
    assert "[ASSERTION RESULT] FAIL  R4  write completed" in text
    assert "expected : 2 written" in text
    assert "actual   : 1 written" in text


def test_state_change_shows_no_field_changed_when_nothing_moved():
    tracer, stream = render()
    tracer.state_change("nothing happened", {"a": 1}, {"a": 1})
    assert "<no field changed>" in stream.getvalue()


def test_external_op_is_labelled_from_the_gateway_in_use():
    tracer, stream = render()
    tracer.external_op("gateway.write()", StubHrisGateway(), fields={"extra": "kept"})
    text = stream.getvalue()
    assert "[EXTERNAL OP] [SIMULATED] gateway.write()" in text
    assert "StubHrisGateway" in text
    assert "kept" in text


def test_approval_required_reports_actor_roles_and_what_is_blocked():
    class FakeApproval:
        approval_id = "ap-1"
        checkpoint = "HC_05_HRIS_WRITE"
        status = "PENDING"
        subject_ref = "hris:case-1:v1"
        requested_to = "hr-officer-1"
        requested_by = "hr-officer-1"
        created_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
        reason = "prepare HRIS write"

    tracer, stream = render()
    tracer.approval_required(
        FakeApproval(),
        why="writes to a system of record need a person",
        blocks="execute_hris_write",
        approver_roles=["HR_MANAGER"],
        separation_of_duties=True,
        awaiting={"records": 2},
    )
    text = stream.getvalue()
    assert "[APPROVAL REQUIRED] human decision gate" in text
    assert "ap-1" in text
    assert "HR_MANAGER" in text
    assert "execute_hris_write" in text
    assert "separation_of_duties" in text
    assert "data awaiting this decision" in text


def test_approval_decision_reports_who_decided_and_what_follows():
    class FakeApproval:
        approval_id = "ap-1"
        checkpoint = "HC_05_HRIS_WRITE"
        status = "APPROVED"
        decided_by = "hr-manager-1"
        decided_at = datetime(2026, 4, 2, tzinfo=timezone.utc)
        decision_note = "synthetic note"

    tracer, stream = render()
    tracer.approval_decision(FakeApproval(), effect="records -> APPROVED", next_step="write")
    text = stream.getvalue()
    assert "[APPROVAL DECISION] decided" in text
    assert "hr-manager-1" in text
    assert "records -> APPROVED" in text
