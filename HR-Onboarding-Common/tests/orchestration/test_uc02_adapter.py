"""UC02 adapter unit tests.

Validates the Coordinator-to-UC02 integration boundary:
- Unauthorized actor rejection before UC02 is invoked
- Missing role context boundary checks
- Successful end-to-end task planning when preconditions are satisfied
- Controlled blocking when approval timestamps or preconditions are missing
- Safe error handling when unregistered client or genuine import/runtime defects occur
"""

from __future__ import annotations

from unittest.mock import patch

from _helpers import UC_ROOTS, make_actor
from hr_onboarding_common.orchestration.adapters.uc02_tasks import UC02_IMPORT_FAILURE_CODE, call_uc02_plan_tasks
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state


class _FakePolicy:
    settings = {"task_planning_trigger_roles": ["hr_admin"]}


class _FakeDeps:
    """What the UC02 planning adapter reads: the policy, the cached UC02 sub-agent, and UC03's root --
    the plan's document requirements are picked from UC03's real catalog of validatable documents."""

    policy = _FakePolicy()
    _uc02_sub_agent = None
    uc03_repo_root = UC_ROOTS["uc03"]
    uc03_document_store_path = None
    _uc03_graph = _uc03_case_store = _uc03_audit_store = _uc03_checklist_service = _uc03_collection_service = None


def _state(client_id="CLIENT-ALPHA", roles=("hr_admin",), role_context=None):
    state = new_state(case_id="C1", client_id=client_id, correlation_id="corr1",
                       actor=make_actor(roles=roles, client_id=client_id), intake_trigger={},
                       uc02_role_context=role_context if role_context is not None else {
                           "role_id": "backend_engineer", "department": "Engineering",
                           "manager_id": "MGR-1", "location": "Dubai", "project": "apollo",
                       })
    state["employee_id"] = "EMP-001"
    state["joining_date"] = "2026-11-16"
    # UC01 verified the offer and approved the form when it opened the case.
    state["intake_trigger"] = {"offer_accepted": True}
    state["stage_statuses"] = {**state["stage_statuses"], Stage.INTAKE.value: StageStatus.COMPLETE.value}
    return state


def test_unauthorized_actor_is_rejected_before_import_is_attempted():
    state = _state(roles=("random_role",))
    result = call_uc02_plan_tasks(state, _FakeDeps())
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.BLOCKED.value
    assert result["error_code"] == "POLICY_BREACH"


def test_missing_uc01_to_uc02_field_gap_blocks_before_calling_uc02():
    """UC01's Case/OnboardingForm do not carry role_id/department/manager_id/location/project
    (verified gap) -- missing fields must block cleanly, not silently default."""
    state = _state(role_context={"role_id": "backend_engineer"})  # missing department/manager_id/location/project
    result = call_uc02_plan_tasks(state, _FakeDeps())
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.BLOCKED.value
    assert result["error_code"] == "MISSING_DATA"


def test_successful_uc02_task_planning_completes_stage():
    """When valid role context and genuine approval timestamps are present, UC-02 executes
    successfully through the Coordinator adapter and transitions PLAN_TASKS to COMPLETE."""
    state = _state(client_id="CLIENT-ALPHA", role_context={
        "role_id": "backend_engineer", "department": "Engineering",
        "manager_id": "MGR-1", "location": "Dubai", "project": "apollo",
        "offer_accepted_at": "2026-10-20T10:00:00Z",
        "form_approved_at": "2026-10-21T10:00:00Z",
        "approved_by_role": "hr_ops",
    })
    result = call_uc02_plan_tasks(state, _FakeDeps())
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value
    assert result.get("error_code") is None
    assert "task_plan" in result
    plan = result["task_plan"]
    assert plan["onboarding_id"] == "ONB-CLIENT-ALPHA-EMP-001"
    assert plan["created"] is True
    assert len(plan["tasks"]) > 0
    assert len(plan["ready"]) > 0
    assert "outstanding_tasks" in result
    # The plan's versioned document requirements, defined in UC02 (from UC03's catalog), before any document.
    onboarding_plan = result["onboarding_plan"]
    assert (onboarding_plan["plan_id"], onboarding_plan["plan_version"]) == ("ONB-CLIENT-ALPHA-EMP-001", 1)
    assert {"passport", "degree_certificate"} <= {d["document_type"] for d in onboarding_plan["required_documents"]}
    assert all(d["due_date"] for d in onboarding_plan["required_documents"])
    assert [h["action"] for h in result["plan_history"]] == ["initial"]


def test_missing_approval_timestamp_blocks_stage_with_controlled_error():
    """If genuine approval timestamp is missing, UC-02 enforces precondition checks and
    blocks cleanly with MISSING_DATA without unhandled exceptions."""
    state = _state(client_id="CLIENT-ALPHA", role_context={
        "role_id": "backend_engineer", "department": "Engineering",
        "manager_id": "MGR-1", "location": "Dubai", "project": "apollo",
        # offer_accepted_at is omitted
    })
    result = call_uc02_plan_tasks(state, _FakeDeps())
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.BLOCKED.value
    assert result["error_code"] == "MISSING_DATA"
    assert "offer acceptance has no timestamp" in result["error_detail"]
    assert result["next_action"] == "hr_review:plan_tasks:precondition_failed"


def test_unregistered_client_blocks_stage_with_controlled_error():
    """An unregistered client configuration must safely block rather than crashing."""
    state = _state(client_id="UNREGISTERED-CLIENT", role_context={
        "role_id": "backend_engineer", "department": "Engineering",
        "manager_id": "MGR-1", "location": "Dubai", "project": "apollo",
        "offer_accepted_at": "2026-10-20T10:00:00Z",
        "form_approved_at": "2026-10-21T10:00:00Z",
        "approved_by_role": "hr_ops",
    })
    result = call_uc02_plan_tasks(state, _FakeDeps())
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.BLOCKED.value
    assert result["error_code"] == "MISSING_DATA"
    assert "no onboarding configuration registered for client" in result["error_detail"]


def test_genuine_import_defect_surfaces_as_failed():
    """A genuine syntax error or import failure must be surfaced as FAILED with
    UC02_IMPORT_FAILURE_CODE rather than crashing the graph or faking success."""
    state = _state()
    with patch("builtins.__import__", side_effect=SyntaxError("simulated syntax error in module")):
        result = call_uc02_plan_tasks(state, _FakeDeps())
        assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.FAILED.value
        assert result["error_code"] == UC02_IMPORT_FAILURE_CODE
        assert "simulated syntax error in module" in result["error_detail"]


_APPROVED_CTX = {
    "role_id": "backend_engineer", "department": "Engineering", "manager_id": "MGR-1",
    "location": "Dubai", "project": "apollo", "offer_accepted_at": "2026-10-20T10:00:00Z",
    "form_approved_at": "2026-10-21T10:00:00Z", "approved_by_role": "hr_ops",
}


def test_plan_tasks_is_blocked_when_uc01_intake_is_not_complete():
    """Offer and form approval are verified by UC01's intake; without it nothing is assumed."""
    state = _state(role_context=dict(_APPROVED_CTX))
    state["stage_statuses"] = {**state["stage_statuses"], Stage.INTAKE.value: StageStatus.PENDING.value}
    result = call_uc02_plan_tasks(state, _FakeDeps())
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.BLOCKED.value
    assert result["error_code"] == "PREREQUISITE_NOT_MET"
    assert "task_plan" not in result or not result.get("task_plan")


def test_offer_not_accepted_in_intake_input_is_not_overridden_by_the_adapter():
    """The adapter used to hardcode accepted=True; an unaccepted offer must now stop UC02."""
    state = _state(role_context=dict(_APPROVED_CTX))
    state["intake_trigger"] = {"offer_accepted": False}
    result = call_uc02_plan_tasks(state, _FakeDeps())
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.BLOCKED.value
    assert "offer has not been accepted" in result["error_detail"]


def test_missing_form_approval_evidence_is_not_defaulted_to_hr_ops():
    """No approver role / timestamp supplied means the form is NOT treated as approved."""
    ctx = {k: v for k, v in _APPROVED_CTX.items() if k not in ("approved_by_role", "form_approved_at")}
    result = call_uc02_plan_tasks(_state(role_context=ctx), _FakeDeps())
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.BLOCKED.value
    assert "form has not been submitted" in result["error_detail"] or "not been approved" in result["error_detail"]


def test_shared_case_and_correlation_ids_reach_uc02_and_come_back():
    deps = _FakeDeps()
    result = call_uc02_plan_tasks(_state(role_context=dict(_APPROVED_CTX)), deps)
    plan = result["task_plan"]
    assert plan["case_id"] == "C1" and plan["correlation_id"] == "corr1"
    # ... and every UC02 audit event for the case carries the correlation id.
    service = deps._uc02_sub_agent.service
    events = [e for e in service._records_audit_for_test()] if hasattr(service, "_records_audit_for_test") else \
        service.audit.all_events_privileged(reason="test: verify correlation id propagation")
    assert events and all(e.correlation_id == "corr1" for e in events if e.onboarding_id)


# ---------------------------------------------------------------- single, correct UC02 integration path
import re
from pathlib import Path
from types import SimpleNamespace

from hr_onboarding_common.orchestration import orchestrator_agent as _orchestrator
from hr_onboarding_common.orchestration import workflow as _workflow
from hr_onboarding_common.orchestration.adapters import uc02_document_collection as _doc_collection
from hr_onboarding_common.orchestration.adapters import uc02_tasks as _uc02_tasks

_ADAPTERS = Path(_uc02_tasks.__file__).parent


def _task_keys(plan):
    return {t["template_key"] for t in plan["tasks"]}


def test_uc02_task_management_has_exactly_one_adapter_and_the_graph_uses_it():
    callers = sorted(p.name for p in _ADAPTERS.glob("*.py") if "UC02SubAgent" in p.read_text(encoding="utf-8"))
    assert callers == ["uc02_tasks.py"]  # no second path into the UC02 agent
    assert _orchestrator.call_uc02_plan_tasks is _uc02_tasks.call_uc02_plan_tasks
    assert _workflow.STAGE_OPERATIONS[Stage.PLAN_TASKS][0] == "UC02"


def test_document_collection_is_uc03s_stage_and_never_touches_uc02():
    """UC02 plans (and replans) but never collects, stores or validates a document: the collection
    stage is UC03's, and its adapter never imports UC02. The old module name only re-exports it."""
    source = Path(_doc_collection.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)  # the docstring explains the ownership; check the code
    assert "onboarding_task_management" not in code and "UC02SubAgent" not in code
    for stage in (Stage.DOCUMENT_COLLECTION, Stage.DOCUMENT_SUBMISSION, Stage.VALIDATE_DOCUMENTS):
        assert _workflow.STAGE_OPERATIONS[stage][0] == "UC03"
    assert _workflow.STAGE_OPERATIONS[Stage.REPLAN_TASKS][0] == "UC02"
    from hr_onboarding_common.orchestration.adapters import uc03_document_collection as _uc03_collection
    assert _orchestrator.call_uc03_document_collection is _uc03_collection.call_uc03_document_collection
    assert _doc_collection.call_uc02_document_collection is _uc03_collection.call_uc03_document_collection
    assert _workflow.STAGE_OPERATIONS[Stage.DOCUMENT_COLLECTION][1] == "initiate_document_collection"


def test_grade_nationality_group_and_attributes_reach_uc02_and_change_the_checklist():
    plain = call_uc02_plan_tasks(_state(role_context=dict(_APPROVED_CTX)), _FakeDeps())
    rich_ctx = dict(_APPROVED_CTX, grade="L4", nationality_group="",
                    attributes=["bv_required", "visa_required", "insurance_required", "needs_travel"])
    rich = call_uc02_plan_tasks(_state(role_context=rich_ctx), _FakeDeps())
    assert plain["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value
    assert rich["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value, rich.get("error_detail")
    extra = _task_keys(rich["task_plan"]) - _task_keys(plain["task_plan"])
    assert {"background_verification", "visa_pack_handover", "travel_booking"} <= extra  # attribute-gated work
    assert not (_task_keys(plain["task_plan"]) & {"visa_pack_handover", "travel_booking"})  # never inferred


def test_the_orchestrator_input_and_state_carry_the_new_checklist_inputs():
    from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput

    inp = OnboardingCaseInput(client_id="CLIENT-ALPHA", hr_officer_id="u", offer_accepted=True, grade="L4",
                              nationality_group="GRP-1", attributes=["visa_required"])
    assert (inp.grade, inp.nationality_group, inp.attributes) == ("L4", "GRP-1", ["visa_required"])
    bare = OnboardingCaseInput(client_id="CLIENT-ALPHA", hr_officer_id="u", offer_accepted=True)
    assert (bare.grade, bare.nationality_group, bare.attributes) == (None, None, None)  # optional, nothing invented


class _CapturingSubAgent:
    """Records what the adapter sends and returns a canned UC02 result (no UC02 logic here)."""

    def __init__(self, *, review_required=False):
        self.requests, self.review_required = [], review_required

    def run(self, request):
        self.requests.append(request)
        checklist = {"tasks": [{"task_id": "T1", "template_key": "a"}], "ready": ["T1"], "blocked": [],
                     "review_required": self.review_required,
                     "issues": [{"code": "no_owner", "severity": "blocking", "subject": "a"}] if self.review_required else []}
        return SimpleNamespace(onboarding_id="ONB-X", created=True, case_id=request.trigger["case_id"],
                               correlation_id=request.trigger["correlation_id"], checklist=checklist,
                               agent_run={"status": "observed", "proposals": []}, advice={"degraded": False})

    @property
    def service(self):  # UC02's document-requirement calls, canned (the real ones: test_uc02_real_orchestration)
        documents = []

        def define(actor, onboarding_id, specs):
            documents[:] = [SimpleNamespace(key=s.document_type, mandatory=s.mandatory, reason=s.reason) for s in specs]
            return SimpleNamespace(plan_version=1, documents=list(documents), attributes=[])

        return SimpleNamespace(define_document_requirements=define)


def _deps_with(sub_agent):
    deps = _FakeDeps()
    deps._uc02_sub_agent = sub_agent
    return deps


def test_uc02_is_evaluated_as_of_today_not_as_of_the_joining_date():
    from datetime import date

    stub = _CapturingSubAgent()
    call_uc02_plan_tasks(_state(role_context=dict(_APPROVED_CTX)), _deps_with(stub))
    assert stub.requests[0].as_of == date.today() != date.fromisoformat("2026-11-16")
    override = _CapturingSubAgent()
    call_uc02_plan_tasks(_state(role_context=dict(_APPROVED_CTX, as_of="2026-11-02")), _deps_with(override))
    assert override.requests[0].as_of == date(2026, 11, 2)


def test_unresolved_configuration_review_blocks_the_stage_but_keeps_the_plan():
    result = call_uc02_plan_tasks(_state(role_context=dict(_APPROVED_CTX)), _deps_with(_CapturingSubAgent(review_required=True)))
    assert result["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.BLOCKED.value
    assert result["error_code"] == "CHECKLIST_REVIEW_REQUIRED" and "no_owner" in result["error_detail"]
    assert result["next_action"] == "hr_review:plan_tasks:configuration_review"
    assert result["task_plan"]["review_required"] is True and result["task_plan"]["tasks"]  # plan not discarded
    ok = call_uc02_plan_tasks(_state(role_context=dict(_APPROVED_CTX)), _deps_with(_CapturingSubAgent()))
    assert ok["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value
    assert ok["task_plan"]["review_required"] is False and ok["task_plan"]["agent_status"] == "observed"


def test_the_default_subagent_uses_the_uc02_store_selected_by_the_environment(monkeypatch, tmp_path):
    db = tmp_path / "uc02.sqlite3"
    monkeypatch.setenv("UC02_STORE_MODE", "sqlite")
    monkeypatch.setenv("UC02_SQLITE_PATH", str(db))
    first = _FakeDeps()
    one = call_uc02_plan_tasks(_state(role_context=dict(_APPROVED_CTX)), first)
    assert one["task_plan"]["created"] is True and db.exists()
    first._uc02_sub_agent.service.store.client.close()
    second = _FakeDeps()  # a "restarted" orchestrator: fresh sub-agent over the same database
    two = call_uc02_plan_tasks(_state(role_context=dict(_APPROVED_CTX)), second)
    second._uc02_sub_agent.service.store.client.close()
    assert two["task_plan"]["created"] is False  # recognised from storage: no second plan
    assert two["task_plan"]["onboarding_id"] == one["task_plan"]["onboarding_id"]


def test_by_default_the_uc02_store_is_in_memory(monkeypatch):
    monkeypatch.delenv("UC02_STORE_MODE", raising=False)
    deps = _FakeDeps()
    call_uc02_plan_tasks(_state(role_context=dict(_APPROVED_CTX)), deps)
    assert deps._uc02_sub_agent.service.store is None
