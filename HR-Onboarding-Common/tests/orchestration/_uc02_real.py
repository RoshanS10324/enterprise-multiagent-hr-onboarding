"""UC02's REAL date-change business logic for the orchestration tests, shared by
test_plan_tasks_barrier_and_date_change.py and test_joining_date_reconciliation.py.

What is real: `onboarding_task_management.sub_agent.service.OnboardingService` -- UC02's actual
DATE_CHANGE logic (request/decide/apply), imported directly, bypassing `sub_agent.adapter.UC02SubAgent`,
which is unimportable (the pre-existing `agent/advisor.py:1` SyntaxError, see adapters/uc02_tasks.py).
`RealPlanAdapter` creates a real onboarding record the way `call_uc02_plan_tasks` would if the package
imported; `request_uc02_date_change`/`relay_uc02_decision` then call the same `OnboardingService`
through `deps._uc02_sub_agent.service`, as they do in production through the cached `UC02SubAgent`.
Task planning itself (the stage's adapter output) is therefore a contract-compatible double, not UC02.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from _workflow import CLIENT, Flow
from hr_onboarding_common.orchestration.adapters._shared import with_stage_status
from hr_onboarding_common.orchestration.adapters.uc02_tasks import catalog_requirements, plan_record, \
    record_initial_plan
from hr_onboarding_common.orchestration.state import Stage, StageStatus

from onboarding_task_management.contracts import Actor, DocumentSpec, OfferAcceptance, OnboardingForm, Role
from onboarding_task_management.sub_agent.service import OnboardingService, OnboardingTrigger
from onboarding_task_management.synthetic import build_registry


def real_uc02_service() -> OnboardingService:
    """A real OnboardingService, its registry carrying the test world's CLIENT (_workflow.py's
    CLIENT-TEST-A) under CLIENT-ALPHA's synthetic config (UC02's registry has no CLIENT-TEST-A
    entry of its own)."""
    registry = build_registry()
    registry.register(registry.get("CLIENT-ALPHA").model_copy(update={"client_id": CLIENT}))
    return OnboardingService(registry)


class SubAgentStub:
    """Stands in for the real (unimportable) UC02SubAgent: only its public `.service` attribute is
    ever read by request_uc02_date_change/relay_uc02_decision."""

    def __init__(self, service: OnboardingService):
        self.service = service


class RealPlanAdapter:
    """plan_tasks double that creates a REAL UC02 onboarding record via OnboardingService.
    create_onboarding -- the real business logic, not a canned result -- so the date-change tests
    exercise UC02's actual DATE_CHANGE gate against a record it actually holds. Its call is recorded
    on the flow's gate like the default PlanDouble's."""

    def __init__(self, service: OnboardingService):
        self.service = service
        self.calls = 0
        self.gate = None
        self.deps = None  # the flow's OrchestratorDeps (UC03's catalog for the plan's documents)
        self.status_override = None  # a StageStatus: UC02 could not plan (nothing is created in UC02)

    def __call__(self, state, deps):
        if self.gate is not None:
            self.gate.wait("plan_tasks")
        try:
            return self._plan(state, deps)
        finally:
            if self.gate is not None:
                self.gate.done("plan_tasks")

    def _plan(self, state, deps):
        self.calls += 1
        if self.status_override is not None:
            new_state = with_stage_status(state, Stage.PLAN_TASKS, self.status_override)
            new_state["error_code"], new_state["error_detail"] = "MISSING_DATA", "UC02 could not plan (test)"
            return new_state
        role = state["uc02_role_context"]
        actor = Actor(actor_id=state["actor"]["on_behalf_of"], role=Role.HR_OPS, client_id=state["client_id"])
        now = datetime.now(timezone.utc)
        trigger = OnboardingTrigger(
            client_id=state["client_id"], employee_id=state["employee_id"],
            joining_date=date.fromisoformat(state["joining_date"]), role_id=role["role_id"],
            department=role["department"], manager_id=role["manager_id"], location=role["location"],
            project=role["project"],
            offer=OfferAcceptance(employee_id=state["employee_id"], accepted=True, accepted_at=now,
                                  offer_reference=state.get("offer_reference")),
            form=OnboardingForm(employee_id=state["employee_id"], submitted=True, approved=True,
                                approved_by_role=Role.HR_OPS, approved_at=now))
        record = self.service.create_onboarding(actor, trigger)
        # The plan's document requirements, defined in UC02 from UC03's real catalog (as call_uc02_plan_tasks does).
        catalog = catalog_requirements(state, self.deps or deps)
        record = self.service.define_document_requirements(actor, record.onboarding_id, [
            DocumentSpec(document_type=d["document_type"], mandatory=d["required"], reason=d["reason"][:200])
            for d in catalog])
        new_state = with_stage_status(state, Stage.PLAN_TASKS, StageStatus.COMPLETE)
        new_state["task_plan"] = {"onboarding_id": record.onboarding_id, "created": True, "ready": [], "blocked": [],
                                  "tasks": [], "plan_version": record.plan_version}
        labels = {d["document_type"]: d["label"] for d in catalog}
        record_initial_plan(new_state, plan_record(
            state, plan_id=record.onboarding_id, plan_version=record.plan_version,
            required_documents=[{"document_type": d.key, "label": labels.get(d.key, d.key), "required": d.mandatory,
                                 "reason": d.reason} for d in record.documents],
            due_date=state["joining_date"], task_count=len(record.tasks), attributes=list(record.attributes),
            source="uc02"))
        return new_state


def real_uc02_flow(**flow_kwargs) -> tuple[Flow, OnboardingService]:
    """The real UC02 OnboardingService behind both plan_tasks (RealPlanAdapter) and replan_tasks (the
    real adapter, call_uc02_replan_tasks, through the sub-agent's `.service`)."""
    service = real_uc02_service()
    plan = RealPlanAdapter(service)
    flow = Flow(plan_adapter=plan, real_replan=True, **flow_kwargs)
    plan.gate, plan.deps = flow.gate, flow.deps
    flow.deps._uc02_sub_agent = SubAgentStub(service)
    return flow, service
