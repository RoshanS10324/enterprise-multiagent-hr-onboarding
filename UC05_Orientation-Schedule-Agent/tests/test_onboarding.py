from hr_onboarding_common.contracts import ExceptionCode, ResponseStatus
from sub_agent.agent import WelcomeSummaryInput
from sub_agent.models import CaseStatus


def test_welcome_draft_is_acted_and_submitted_for_hr_review(agent, make_request):
    response = agent.run(make_request(agent, WelcomeSummaryInput(employee_id="EMP-001"), employee_id="EMP-001"))

    assert response.status == ResponseStatus.ACTED
    assert not response.exceptions
    action = response.actions_taken[0]
    assert action.tool == "welcome.draft_communication"
    assert action.subject_id == "EMP-001"
    assert action.approval_id is not None  # the service's own real HITL item, not a second proposal
    assert "Sara Al Marzooqi" in action.detail["body"]
    assert "EMP-010" in action.detail["body"]  # buddy line
    assert action.detail["status"] == "draft"


def test_welcome_draft_without_buddy_has_no_buddy_line(agent, make_request):
    response = agent.run(make_request(agent, WelcomeSummaryInput(employee_id="EMP-002"), employee_id="EMP-002"))

    assert response.status == ResponseStatus.ACTED
    body = response.actions_taken[0].detail["body"]
    assert "onboarding buddy" not in body.lower()


def test_unknown_employee_raises_missing_data_exception(agent, make_request):
    response = agent.run(make_request(agent, WelcomeSummaryInput(employee_id="EMP-999"), employee_id="EMP-999"))

    assert response.status == ResponseStatus.EXCEPTION
    assert not response.proposals and not response.actions_taken
    assert response.exceptions[0].code == ExceptionCode.MISSING_DATA


def test_cancelled_case_is_never_welcomed(agent, ctx, make_request):
    ctx.connector.repository.update_case_status("EMP-001", CaseStatus.CANCELLED)

    response = agent.run(make_request(agent, WelcomeSummaryInput(employee_id="EMP-001"), employee_id="EMP-001"))

    assert response.status == ResponseStatus.EXCEPTION
    assert not response.actions_taken
    assert response.exceptions[0].code == ExceptionCode.POLICY_BREACH
    assert ctx.connector.repository.list_communication_drafts("EMP-001") == []


def test_withdrawn_case_is_never_welcomed(agent, ctx, make_request):
    ctx.connector.repository.update_case_status("EMP-002", CaseStatus.WITHDRAWN)

    response = agent.run(make_request(agent, WelcomeSummaryInput(employee_id="EMP-002"), employee_id="EMP-002"))

    assert response.status == ResponseStatus.EXCEPTION
    assert response.exceptions[0].code == ExceptionCode.POLICY_BREACH
