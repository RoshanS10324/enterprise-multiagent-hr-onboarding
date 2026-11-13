"""Typed stage handoffs (handoff_contracts.py): one envelope for UC01..UC05, persisted in the stage outcome, carrying
ids, versions, counts, codes and statuses only -- never a name, an e-mail address, a document value or a message body."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hr_onboarding_common.orchestration.handoff_contracts import (CONTRACTS, UC03Output, UC05Output, build_handoff)
from hr_onboarding_common.orchestration.workflow import STAGE_OPERATIONS
from hr_onboarding_common.orchestration.workflow_models import StageOutcome

STATE = {
    "case_id": "62e57066-c75e-46b0-9e15-61b3b2178dbe", "client_id": "CLIENT_DUBAI", "employee_id": "EMP-000005",
    "joining_date": "2027-03-01", "candidate_name": "Karthi L", "candidate_email": "karthi@example.invalid",
    "blockers": [{"stage": "validate_documents", "code": "UC03_RESULT_NOT_PERSISTED", "reason": "payslips: X",
                  "owner": "ops", "next_action": "fix", "required_table": "candidate_documents"},
                 {"stage": "equipment_access", "code": "OTHER", "reason": "r", "owner": "hr_admin", "next_action": "n"}],
    "pending_approvals": [{"stage": "orientation_scheduling", "reference": "apr-1", "gate": "session_booking",
                           "role": "team_lead", "agent": "employee-onboarding", "description": "Approve the session"}],
    "document_validation_feedback": {"feedback_id": "FB-1", "plan_version": 2, "validation_status": "verified",
                                     "verified_documents": ["passport"], "replanning_required": False},
    "document_validation_result": {"passport": {"revision": 1, "classification": "verified",
                                                "persistence_status": "PERSISTED", "detail": "passport ok",
                                                "extracted_name": "Karthi L"}},
    "orientation_schedule_result": {"session_id": "S-1", "session_status": "scheduled", "start_utc": "2027-03-01T05:00:00Z",
                                    "timezone": "Asia/Dubai", "meeting_link": None, "participants": [{"name": "X"}],
                                    "body": "Dear Karthi"},
    "welcome_message_result": {"draft_id": "D-1", "delivery": "delivered", "delivery_status": "SENT_SIMULATED",
                               "simulated_delivery": True, "subject": "Welcome Karthi", "body": "Dear Karthi"},
}


def _handoff(uc, stage, result_key=None, **kw):
    return build_handoff(uc=uc, stage=stage, status="complete", result_key=result_key, state=STATE, warnings=[],
                         attempt=2, finished_at="2027-03-01T05:00:00Z", execution_id="exec-1", **kw)


def test_every_uc_stage_has_a_typed_contract_and_orchestrator_stages_none():
    for stage, (uc, _, key) in STAGE_OPERATIONS.items():
        handoff = _handoff(uc, stage.value, key)
        if uc in CONTRACTS:
            assert handoff["contract"] == f"{uc}Output" and handoff["version"] == 2
            assert handoff["case_id"] == STATE["case_id"] and handoff["client_id"] == "CLIENT_DUBAI"
            assert handoff["audit_reference"] == "exec-1" and handoff["timestamp"] == "2027-03-01T05:00:00Z"
        else:
            assert handoff is None


def test_the_uc03_handoff_carries_the_feedback_and_each_documents_persistence():
    handoff = UC03Output.model_validate(_handoff("UC03", "validate_documents", "document_validation_result"))
    assert handoff.result["feedback_id"] == "FB-1" and handoff.result["plan_version"] == 2
    assert handoff.result["documents"]["passport"] == {"revision": 1, "classification": "verified",
                                                       "persistence_status": "PERSISTED"}
    assert handoff.blockers == [{"code": "UC03_RESULT_NOT_PERSISTED", "reason": "payslips: X", "owner": "ops",
                                 "next_action": "fix", "required_table": "candidate_documents"}]


def test_the_uc05_handoffs_carry_schedule_approvals_and_delivery_status():
    orientation = UC05Output.model_validate(_handoff("UC05", "orientation_scheduling", "orientation_schedule_result"))
    assert orientation.result["session_id"] == "S-1" and orientation.result["timezone"] == "Asia/Dubai"
    assert orientation.result["meeting_link"] is None  # absent stays absent
    assert orientation.approval_requirements == [{"reference": "apr-1", "gate": "session_booking", "role": "team_lead"}]
    welcome = _handoff("UC05", "joining_day_welcome", "welcome_message_result")
    assert welcome["result"]["delivery_status"] == "SENT_SIMULATED" and welcome["result"]["simulated_delivery"] is True


def test_no_personal_data_or_message_content_reaches_a_handoff():
    text = json.dumps([_handoff(uc, stage.value, key) for stage, (uc, _, key) in STAGE_OPERATIONS.items()])
    for secret in ("Karthi", "karthi@example.invalid", "Dear", "Welcome Karthi", "passport ok"):
        assert secret not in text


def test_the_contract_is_strict_and_stored_in_the_stage_outcome():
    with pytest.raises(ValidationError):
        UC03Output.model_validate({**_handoff("UC03", "validate_documents"), "unexpected": 1})
    with pytest.raises(ValidationError):
        UC03Output.model_validate({**_handoff("UC03", "validate_documents"), "contract": "UC04Output"})
    outcome = StageOutcome(stage="validate_documents", uc="UC03", operation="validate_documents", status="complete",
                           attempt=1, started_at="t0", finished_at="t1",
                           handoff=_handoff("UC03", "validate_documents")).model_dump(mode="json")
    assert outcome["handoff"]["contract"] == "UC03Output"
