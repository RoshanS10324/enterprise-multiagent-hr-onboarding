"""UC04 reads this case's UC-02 onboarding plan (adapters/uc04_uc02_plan.py) from UC-02's REAL service.

The plan is used only when it is this case's plan (onboarding id, case id, client id and employee id all match); a
missing, foreign or unreadable plan blocks UC04 with its own code and nothing is borrowed. The service tasks reach UC04's
stage result; the configuration-version and due-date comparisons are advisory and never change the stage status."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from _helpers import BASE, UC_ROOTS
from _uc02_real import SubAgentStub
from hr_onboarding_common.orchestration.adapters import uc04_uc02_plan as link
from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import call_uc04_equipment_access
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state

from onboarding_task_management.contracts import Actor, OfferAcceptance, OnboardingForm, Role
from onboarding_task_management.sub_agent.service import OnboardingService, OnboardingTrigger
from onboarding_task_management.synthetic import build_registry

CLIENT, OTHER_CLIENT = "client_alpha", "client_beta"
CASE, OTHER_CASE = "SYN-CASE-201", "SYN-CASE-202"
EMPLOYEE = "SYN-EMP-201"
CONFIG = UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"


def _service() -> OnboardingService:
    """UC-02's real service, its registry carrying the two test tenants under CLIENT-ALPHA's synthetic configuration."""
    registry = build_registry()
    for client in (CLIENT, OTHER_CLIENT):
        registry.register(registry.get("CLIENT-ALPHA").model_copy(update={"client_id": client}))
    return OnboardingService(registry)


def _plan(service: OnboardingService, *, client=CLIENT, case_id=CASE, employee=EMPLOYEE) -> str:
    """A real UC-02 onboarding plan, created by UC-02's own create_onboarding (as call_uc02_plan_tasks does)."""
    now = datetime.now(timezone.utc)
    actor = Actor(actor_id="SYN-USER-201", role=Role.HR_OPS, client_id=client)
    record = service.create_onboarding(actor, OnboardingTrigger(
        client_id=client, employee_id=employee, joining_date=date(2026, 12, 21), role_id="software_engineer",
        department="Engineering", manager_id="SYN-MGR-001", location="Dubai", project="platform", case_id=case_id,
        offer=OfferAcceptance(employee_id=employee, accepted=True, accepted_at=now),
        form=OnboardingForm(employee_id=employee, submitted=True, approved=True, approved_by_role=Role.HR_OPS,
                            approved_at=now)))
    return record.onboarding_id


def _state(onboarding_id: str | None, *, case_id=CASE, client=CLIENT, employee=EMPLOYEE, config_ref=CONFIG):
    state = new_state(case_id=case_id, client_id=client, correlation_id="corr-201",
                      actor={"on_behalf_of": "SYN-USER-201", "roles": ["hr_onboarding_officer"]},
                      intake_trigger={}, uc04_client_config_ref=str(config_ref) if config_ref else None)
    state["employee_id"], state["joining_date"] = employee, "2026-12-21"
    if onboarding_id:
        state["task_plan"] = {"onboarding_id": onboarding_id, "plan_version": 1}
    return state


def _deps(service=None, worker=None, reader=None) -> OrchestratorDeps:
    deps = OrchestratorDeps(ctx=None, worker_pool=worker, uc03_repo_root=UC_ROOTS["uc03"], uc02_plan_reader=reader)
    if service is not None:
        deps._uc02_sub_agent = SubAgentStub(service)  # the orchestrator's cached path into UC-02, real service
    return deps


class _Pool:
    def __init__(self, worker):
        self.worker = worker

    def get(self, uc_id):
        assert uc_id == "uc04"
        return self.worker


class _NoCallPool:
    def get(self, uc_id):
        raise AssertionError("UC04 must not be called without this case's UC-02 plan")


# ------------------------------------------------------------------ retrieval from UC-02's real service


def test_the_cases_own_plan_is_read_from_uc02_with_its_service_tasks():
    service = _service()
    onboarding_id = _plan(service)
    plan = link.read_plan(_state(onboarding_id), _deps(service))
    record = service.get(Actor(actor_id="SYN-USER-201", role=Role.HR_OPS, client_id=CLIENT), onboarding_id)
    expected = {t.task_id for t in record.tasks if t.category.value in link.SERVICE_CATEGORIES}
    assert expected, "the synthetic configuration plans IT and workplace tasks"
    assert {t["task_id"] for t in plan["service_tasks"]} == expected
    assert {t["category"] for t in plan["service_tasks"]} <= set(link.SERVICE_CATEGORIES)
    assert plan["onboarding_id"] == onboarding_id and plan["source"] == "uc02"
    assert plan["plan_status"] == record.plan_status.value and plan["plan_version"] == record.plan_version
    # Only identifiers, category, owner role, status and dates -- no names or other employee data.
    assert set(plan["service_tasks"][0]) == set(link._TASK_FIELDS)


def test_reading_the_plan_creates_nothing_in_uc02():
    service = _service()
    onboarding_id = _plan(service)
    actor = Actor(actor_id="SYN-USER-201", role=Role.HR_OPS, client_id=CLIENT)
    before = service.get(actor, onboarding_id).model_dump()
    link.read_plan(_state(onboarding_id), _deps(service))
    after = service.get(actor, onboarding_id).model_dump()
    assert before == after and len(service._client_records(CLIENT)) == 1


def test_no_plan_on_the_case_is_missing():
    with pytest.raises(link.Uc02PlanError) as exc:
        link.read_plan(_state(None), _deps(_service()))
    assert exc.value.code == link.PLAN_MISSING


def test_a_plan_uc02_does_not_hold_is_missing():
    with pytest.raises(link.Uc02PlanError) as exc:
        link.read_plan(_state("ONB-client_alpha-SYN-EMP-999"), _deps(_service()))
    assert exc.value.code == link.PLAN_MISSING


def test_another_cases_plan_for_the_same_employee_is_never_attached():
    """The duplicate-employee-number case: the employee matches, the plan belongs to another case."""
    service = _service()
    onboarding_id = _plan(service, case_id=OTHER_CASE)
    with pytest.raises(link.Uc02PlanError) as exc:
        link.read_plan(_state(onboarding_id), _deps(service))
    assert exc.value.code == link.PLAN_CASE_MISMATCH
    assert "case_id" in exc.value.detail and OTHER_CASE not in exc.value.detail  # the other case's id is not copied


def test_another_clients_plan_is_refused_by_uc02_isolation():
    service = _service()
    onboarding_id = _plan(service, client=OTHER_CLIENT)
    with pytest.raises(link.Uc02PlanError) as exc:
        link.read_plan(_state(onboarding_id), _deps(service))
    assert exc.value.code == link.PLAN_CASE_MISMATCH


def test_a_plan_of_another_employee_is_refused():
    def reader(state, deps, onboarding_id):
        return {"onboarding_id": onboarding_id, "case_id": CASE, "client_id": CLIENT, "employee_id": "SYN-EMP-999",
                "tasks": []}
    with pytest.raises(link.Uc02PlanError) as exc:
        link.read_plan(_state("ONB-1"), _deps(reader=reader))
    assert exc.value.code == link.PLAN_CASE_MISMATCH and "employee_id" in exc.value.detail


def test_a_uc02_failure_is_unavailable_never_replaced():
    def reader(state, deps, onboarding_id):
        raise ConnectionError("store down")
    with pytest.raises(link.Uc02PlanError) as exc:
        link.read_plan(_state("ONB-1"), _deps(reader=reader))
    assert exc.value.code == link.PLAN_UNAVAILABLE and "ConnectionError" in exc.value.detail


def test_uc02_unbuildable_is_unavailable():
    deps = _deps()  # no cached sub-agent: built over the configured store, which fails here
    deps._uc02_sub_agent = None

    def boom(_deps):
        raise RuntimeError("no store")
    import hr_onboarding_common.orchestration.adapters.uc02_tasks as uc02_tasks
    original, uc02_tasks.uc02_sub_agent = uc02_tasks.uc02_sub_agent, boom
    try:
        with pytest.raises(link.Uc02PlanError) as exc:
            link.read_plan(_state("ONB-1"), deps)
    finally:
        uc02_tasks.uc02_sub_agent = original
    assert exc.value.code == link.PLAN_UNAVAILABLE


# ------------------------------------------------------------------ advisory checks (never blocking)


def _link(tasks, config_version="cfg-v1"):
    return {"onboarding_id": "ONB-1", "config_version": config_version, "service_tasks": tasks}


def _task(key, category, due):
    return {"task_id": f"T-{key}", "template_key": key, "category": category, "due_date": due}


def test_no_service_tasks_is_a_warning():
    checked = link.advisory_checks(_link([]), uc04_config_version="cfg-v1", uc04_items=[])
    assert [w["code"] for w in checked["warnings"]] == [link.NO_SERVICE_TASKS] and checked["advisory_only"] is True


def test_config_version_mismatch_match_and_not_comparable():
    tasks = [_task("it_laptop", "it_provisioning", "2026-12-14")]
    mismatch = link.advisory_checks(_link(tasks, "cfg-v1"), uc04_config_version="cfg-v2", uc04_items=[])
    assert mismatch["config_version_check"]["result"] == "mismatch"
    assert link.CONFIG_VERSION_MISMATCH in [w["code"] for w in mismatch["warnings"]]
    match = link.advisory_checks(_link(tasks, "cfg-v1"), uc04_config_version="cfg-v1", uc04_items=[])
    assert match["config_version_check"]["result"] == "match" and not match["warnings"]
    none = link.advisory_checks(_link(tasks, ""), uc04_config_version="cfg-v1", uc04_items=[])
    assert none["config_version_check"]["result"] == "not_comparable" and not none["warnings"]


def test_due_date_before_uc04_lead_time_is_a_warning_and_workplace_is_not_compared():
    tasks = [_task("it_laptop", "it_provisioning", "2026-12-01"), _task("it_accounts", "access_management", "2026-12-16"),
             _task("desk_allocation", "workplace", "2026-11-01")]
    items = [{"item_id": "laptop-standard", "due_date": "2026-12-07"}, {"item_id": "email", "due_date": "2026-12-10"}]
    checked = link.advisory_checks(_link(tasks), uc04_config_version="cfg-v1", uc04_items=items)
    early = [w for w in checked["warnings"] if w["code"] == link.DUE_BEFORE_LEAD_TIME]
    assert [w["template_key"] for w in early] == ["it_laptop"]  # desk_allocation is traceability only
    assert checked["due_date_check"] == {"result": "early", "uc04_earliest_lead_time_date": "2026-12-07",
                                         "compared_categories": ["it_provisioning", "access_management"]}
    later = link.advisory_checks(_link(tasks[1:]), uc04_config_version="cfg-v1", uc04_items=items)
    assert later["due_date_check"]["result"] == "ok"
    no_dates = link.advisory_checks(_link(tasks), uc04_config_version="cfg-v1", uc04_items=[{"item_id": "x"}])
    assert no_dates["due_date_check"]["result"] == "not_comparable"


# ------------------------------------------------------------------ the UC04 stage


def test_uc04_is_blocked_without_being_called_when_the_plan_is_another_cases():
    service = _service()
    onboarding_id = _plan(service, case_id=OTHER_CASE)
    result = call_uc04_equipment_access(_state(onboarding_id), _deps(service, worker=_NoCallPool()))
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.BLOCKED.value
    (blocker,) = result["blockers"]
    assert blocker["code"] == link.PLAN_CASE_MISMATCH and blocker["retryable"] is True
    assert "equipment_access_result" not in result or not result.get("equipment_access_result")


def test_uc04_is_blocked_when_the_case_has_no_plan():
    result = call_uc04_equipment_access(_state(None), _deps(_service(), worker=_NoCallPool()))
    assert result["stage_errors"][Stage.EQUIPMENT_ACCESS.value]["code"] == link.PLAN_MISSING


def test_the_service_data_reaches_uc04s_stage_and_warnings_do_not_change_its_status(uc04_worker):
    service = _service()
    onboarding_id = _plan(service)
    result = call_uc04_equipment_access(_state(onboarding_id), _deps(service, worker=_Pool(uc04_worker)))
    plan = result["equipment_access_result"]["uc02_plan"]
    assert plan["onboarding_id"] == onboarding_id and plan["service_tasks"]
    assert plan["advisory_only"] is True and {"config_version_check", "due_date_check", "warnings"} <= set(plan)
    # The same evaluation as before this link: UC04's own status decides the stage, not the advisory warnings.
    assert result["equipment_access_result"]["overall_status"] in ("AWAITING_APPROVAL", "NEEDS_HUMAN_REVIEW")
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] in (StageStatus.AWAITING_APPROVAL.value,
                                                                      StageStatus.BLOCKED.value)
    assert not any(b["code"].startswith("UC02_") for b in result["blockers"])


def test_the_demo_stand_in_reader_is_labelled_and_refuses_any_other_plan():
    import importlib.util
    spec = importlib.util.spec_from_file_location("demo", BASE / "HR-Onboarding-Common" / "scripts" / "run_workflow_demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    state = _state("ONB-SYN-001")
    state["task_plan"]["tasks"] = [{"task_id": "T-1", "template_key": "it_laptop", "due_date": "2026-11-20"}]
    plan = link.read_plan(state, _deps(reader=demo._uc02_plan_stand_in))
    assert plan["source"] == "uc02_stand_in" and plan["data_origin"] == "synthetic"
    assert [t["category"] for t in plan["service_tasks"]] == ["it_provisioning"]
    with pytest.raises(link.Uc02PlanError) as exc:
        link.read_plan(_state("ONB-OTHER"), _deps(reader=demo._uc02_plan_stand_in))
    assert exc.value.code == link.PLAN_MISSING
