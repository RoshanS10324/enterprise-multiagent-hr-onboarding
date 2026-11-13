"""Pre-integration boundaries: business logic is independent of external services, the mocks implement the intended interfaces,
mock mode cannot be mistaken for production, and UC-04 never commits spend or acts for a person (GR-02, GR-19, GR-20).

HC-08: the Framework mentions it once (GR-20: "approval at HC-08") and defines it nowhere, so it is NOT implemented. What can be
verified without guessing its meaning is that UC-04 raises no spend-committing request and every workaround is a proposal that
names who must approve it. The open question is recorded in specs/UC04_SPEC.md section 12."""

import inspect
from pathlib import Path

from helpers import ALL, Env, approvals, delivery, make, ticket
from sub_agent import IMPLEMENTATION_MODE
from sub_agent.adapters.approval_mock import MockApprovalDesk
from sub_agent.adapters.case_context_mock import MockCaseContext
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.ports import CaseContextPort, ITServiceDeskPort
from sub_agent.states import CaseStatus

SRC = Path(__file__).resolve().parents[1] / "src" / "sub_agent"
ROOT = Path(__file__).resolve().parents[1]
KNOWN_PROPOSALS = {"LOAN_DEVICE", "EXCHANGE_REQUEST_DRAFT", "OFFICE_START_OR_LATER_DEPLOYMENT", "NEXT_WORKING_DAY_SUGGESTION",
                   "NOTIFY_IT_WITHDRAWAL", "NOTIFY_IT_SUPERSEDED_REQUEST",
                   "MESSAGE_DRAFT_EMPLOYEE_DELAY", "MESSAGE_DRAFT_MANAGER_DELAY", "MESSAGE_DRAFT_MANAGER_REJECTION",
                   "MESSAGE_DRAFT_EMPLOYEE_COLLECTION_REMINDER", "MESSAGE_DRAFT_MANAGER_COLLECTION_REMINDER", "REQUEST_COLLECTION_SLOT",
                   "RETAIN_RESERVATION_REQUEST", "COURIER_REQUEST_DRAFT", "REQUEST_ACCESS_REMOVAL", "CONFIRM_EQUIPMENT_SUITABLE",
                   "REQUEST_EQUIPMENT_CHANGE"}


def _shape(fn):
    return [(p.name, p.kind) for p in inspect.signature(fn).parameters.values() if p.name != "self"]


def test_mock_adapters_have_exactly_the_signatures_of_their_ports():
    assert _shape(MockITServiceDesk.submit_request) == _shape(ITServiceDeskPort.submit_request)
    assert _shape(MockCaseContext.get_case) == _shape(CaseContextPort.get_case)
    assert _shape(MockCaseContext.case_for_employee) == _shape(CaseContextPort.case_for_employee)


def test_the_service_accepts_any_object_with_the_port_methods():
    class OtherDesk:                                    # stands in for a future MCP-backed adapter
        def submit_request(self, *, request_id, case_id, item_kind, item_id):
            return MockITServiceDesk().submit_request(request_id=request_id, case_id=case_id, item_kind=item_kind, item_id=item_id)

    env = Env(it_desk=OtherDesk())
    report = env.submit(make(approval_records=approvals(ALL)))
    assert len(report.submitted) == 4


def test_business_modules_never_import_adapters_or_the_api():
    for path in SRC.glob("*.py"):
        if path.name in ("fixtures.py", "agent.py", "fakes.py"):
            continue                                     # facade, mocks and shim modules
        text = path.read_text("utf-8")
        assert "sub_agent.adapters" not in text and "sub_agent.api" not in text, path.name


def test_the_api_package_is_the_only_code_that_imports_fastapi():
    offenders = [p.name for p in SRC.rglob("*.py") if "api" not in p.parts and "fastapi" in p.read_text("utf-8")]
    assert offenders == []


def test_mock_mode_is_always_visible_and_the_mocks_declare_no_real_side_effects():
    env = Env()
    assert env.run(make()).implementation_mode == IMPLEMENTATION_MODE == "LOCAL_SIMULATION"
    assert MockITServiceDesk.real_side_effects is False
    assert {n for n in dir(MockApprovalDesk) if not n.startswith("_")} == {"issue"}      # it issues records for tests; it approves nothing


def test_no_credentials_or_endpoints_are_read_from_the_environment_or_hardcoded():
    for path in SRC.rglob("*.py"):
        if "api" in path.parts:
            continue
        text = path.read_text("utf-8")
        assert "os.environ" not in text and "getenv" not in text and "http://" not in text and "https://" not in text, path.name


def test_the_only_external_write_is_one_gated_tool_and_nothing_else_is_ever_called():
    env = Env()
    env.submit(make(approval_records=approvals(ALL)))
    env.run(make(approval_records=approvals(ALL)), idempotency_key="again")
    assert set(env.service.gateway.calls) == {"it_service_desk.submit_request"} and len(env.service.gateway.calls) == 4
    tools = env.service.policy.tools
    assert tools.write == [] and tools.write_requires_approval == ["it_service_desk.submit_request"]


def test_no_spend_is_ever_committed_and_every_workaround_is_a_proposal_naming_its_approver():
    """GR-20 / HC-08 (undefined): the verifiable part. See the module docstring."""
    scenarios = [
        make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")], delivery_status=[delivery("201", "PENDING", expected="2026-11-20")]),
        make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")], delivery_status=[delivery("201", "FAULTY")]),
        make(joining_date="2026-11-14"),
        make(as_of_date="2026-11-12"),
    ]
    for n, payload in enumerate(scenarios):
        env = Env()
        r = env.run(payload)
        for p in r.proposals + r.cancellation_drafts:
            assert p.proposal_type in KNOWN_PROPOSALS and p.executed is False and p.requires_approval_by, (n, p)
        assert env.desk.calls == [] and r.overall_status != CaseStatus.READY


def test_hc08_is_recorded_as_an_open_question_and_not_implemented_as_logic():
    import re
    spec = (ROOT / "specs" / "UC04_SPEC.md").read_text("utf-8")
    assert "HC-08" in spec and "not implemented" in spec.lower()
    assert not [p.name for p in SRC.rglob("*.py") if re.search(r"[\"']HC-08[\"']", p.read_text("utf-8"))]   # no code keyed on it
