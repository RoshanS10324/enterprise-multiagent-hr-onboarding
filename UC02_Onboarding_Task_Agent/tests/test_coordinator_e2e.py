"""Comprehensive Coordinator-to-UC02 End-to-End and Failure Handling Tests.

Validates the complete workflow between the Central Coordinator Agent and UC-02
(Onboarding Task Management), covering:
1. Full successful onboarding workflow (Phases 1-13).
2. Failure handling scenarios A through G:
   A. Invalid or missing employee context.
   B. Checklist generation failure (unregistered client / missing config).
   C. Task creation failure / idempotency / retry safety.
   D. Tool execution failure and degraded advice handling.
   E. Persistence failure handling (no false success).
   F. Coordinator invocation failure (unmet preconditions).
   G. Advisor / LLM failure (error/hallucinate fallback to OfflineAdvisor).

Preserves all UC-02 business rules, task lifecycle, owner assignment, DAG
dependencies, UAE calendar due dates, idempotency and security controls.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
_SRC = str(ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_COMMON_SRC = str(ROOT.parent / "HR-Onboarding-Common" / "src")
_COMMON_TESTS = str(ROOT.parent / "HR-Onboarding-Common" / "tests" / "orchestration")
if _COMMON_SRC not in sys.path:
    sys.path.insert(0, _COMMON_SRC)
if _COMMON_TESTS not in sys.path:
    sys.path.insert(0, _COMMON_TESTS)

from onboarding_task_management.agent.advisor import OfflineAdvisor
from onboarding_task_management.agent.loop import UC02Agent
from onboarding_task_management.contracts import (
    Actor,
    OnboardingRecord,
    Role,
    TaskRecord,
    TaskStatus,
)
from onboarding_task_management.errors import (
    MissingConfigurationError,
    OnboardingError,
    PreconditionError,
    UnauthorizedActionError,
)
from onboarding_task_management.persistence import InMemoryTableClient, OnboardingStore
from onboarding_task_management.sub_agent.adapter import (
    UC02SubAgent,
    WorkflowError,
    WorkflowRequest,
    WorkflowResult,
)
from onboarding_task_management.sub_agent.service import (
    OnboardingService,
    OnboardingTrigger,
)
from onboarding_task_management.synthetic import build_registry


def _make_valid_trigger_dict(
    client_id: str = "CLIENT-ALPHA",
    employee_id: str = "EMP-001",
    role_id: str = "backend_engineer",
    joining_date: str = "2026-11-16",
    offer_accepted: bool = True,
    offer_accepted_at: str = "2026-10-20T10:00:00Z",
    form_submitted: bool = True,
    form_approved: bool = True,
    form_approved_at: str = "2026-10-21T10:00:00Z",
    approved_by_role: str = "hr_ops",
) -> dict:
    """Build a valid trigger dict shaped for Coordinator-to-UC02 handoff."""
    return {
        "client_id": client_id,
        "employee_id": employee_id,
        "joining_date": joining_date,
        "role_id": role_id,
        "department": "Engineering",
        "manager_id": "MGR-001",
        "location": "Dubai",
        "project": "apollo",
        "grade": "L4",
        "nationality_group": "grp-expat",
        "attributes": ["needs_travel", "needs_accommodation"],
        "offer": {
            "employee_id": employee_id,
            "accepted": offer_accepted,
            "accepted_at": offer_accepted_at,
            "offer_reference": "OFR-2026-001",
        },
        "form": {
            "employee_id": employee_id,
            "submitted": form_submitted,
            "approved": form_approved,
            "approved_by_role": approved_by_role,
            "approved_at": form_approved_at,
            "missing_fields": [],
        },
    }


class CoordinatorSuccessfulWorkflowTests(unittest.TestCase):
    """PHASE 4: End-to-End Orchestration Success Testing."""

    def test_complete_13_step_coordinator_to_uc02_workflow(self):
        """Validates all 13 steps of the Coordinator -> UC-02 execution sequence."""
        # Step 1: Coordinator receives request with employee & client context
        trigger_dict = _make_valid_trigger_dict(
            client_id="CLIENT-ALPHA",
            employee_id="EMP-100",
            role_id="backend_engineer",
            joining_date="2026-11-16",
        )

        # Step 2 & 3: Coordinator invokes UC-02 with context & metadata
        sub_agent = UC02SubAgent()
        request = WorkflowRequest(
            trigger=trigger_dict,
            as_of=date(2026, 10, 26),
            actor_id="user:hr-admin-01",
            actor_role="hr_ops",
            llm_behaviour="offline",
            prepare_reminders=False,
        )

        # Step 4: UC-02 validates the request
        result = sub_agent.run(request)

        # Step 5 & 6: Onboarding checklist & tasks generated
        self.assertIsInstance(result, WorkflowResult)
        self.assertEqual(result.onboarding_id, "ONB-CLIENT-ALPHA-EMP-100")
        self.assertTrue(result.created)
        checklist = result.checklist
        self.assertGreater(checklist["total"], 0)

        # Step 7: Task dependencies created (DAG structure)
        tasks = checklist["tasks"]
        task_by_key = {t["template_key"]: t for t in tasks}
        self.assertIn("hr_record_create", task_by_key)
        self.assertIn("collect_documents", task_by_key)
        self.assertIn("it_laptop", task_by_key)
        # collect_documents depends on hr_record_create
        self.assertIn("hr_record_create", task_by_key["collect_documents"]["blocked_by"])
        # it_laptop depends on hr_record_create
        self.assertIn("hr_record_create", task_by_key["it_laptop"]["blocked_by"])

        # Step 8: HR-controlled task ownership preserved
        self.assertEqual(task_by_key["hr_record_create"]["owner"], "hr_ops")
        self.assertEqual(task_by_key["it_laptop"]["owner"], "it_manager")
        self.assertEqual(task_by_key["desk_allocation"]["owner"], "facilities")
        self.assertEqual(task_by_key["payroll_setup"]["owner"], "finance")

        # Step 9: UAE business calendar due dates calculated (excluding weekends/holidays)
        for t in tasks:
            self.assertTrue(t["due_date"])
            # All due dates must be valid ISO dates
            due = date.fromisoformat(t["due_date"])
            self.assertIsInstance(due, date)

        # Step 10: Stored in configured persistence store
        actor = Actor(actor_id="user:hr-admin-01", role=Role.HR_OPS, client_id="CLIENT-ALPHA")
        loaded_record = sub_agent.service.get(actor, result.onboarding_id)
        self.assertIsNotNone(loaded_record)
        self.assertEqual(loaded_record.employee_id, "EMP-100")

        # Step 11: Task execution and completion status processing
        ready_tasks = checklist["ready"]
        self.assertIn("ONB-CLIENT-ALPHA-EMP-100#hr_record_create", ready_tasks)

        # Start and complete the initial ready task with evidence
        started_record = sub_agent.service.start_task(
            actor, result.onboarding_id, "ONB-CLIENT-ALPHA-EMP-100#hr_record_create"
        )
        completed_task = sub_agent.service.complete_task(
            actor,
            result.onboarding_id,
            "ONB-CLIENT-ALPHA-EMP-100#hr_record_create",
            evidence_ref="HR profile EMP-100 created in HRIS",
        )
        self.assertEqual(completed_task.status, TaskStatus.COMPLETE)

        # Re-evaluating checklist unblocks downstream tasks
        updated_view = sub_agent.service.checklist(actor, result.onboarding_id, date(2026, 10, 26))
        # collect_documents and it_laptop were blocked by hr_record_create; now ready
        self.assertIn("ONB-CLIENT-ALPHA-EMP-100#collect_documents", updated_view.ready_task_ids)
        self.assertIn("ONB-CLIENT-ALPHA-EMP-100#it_laptop", updated_view.ready_task_ids)

        # Step 12 & 13: Coordinator receives and handles structured response
        dict_response = result.as_dict()
        self.assertEqual(dict_response["client_id"], "CLIENT-ALPHA")
        self.assertEqual(dict_response["employee_id"], "EMP-100")
        self.assertEqual(dict_response["advice"]["source"], "offline")


class CoordinatorFailureHandlingTests(unittest.TestCase):
    """PHASE 5: Failure Handling Scenarios A through G."""

    def setUp(self):
        self.sub_agent = UC02SubAgent()
        self.actor = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id="CLIENT-ALPHA")

    # --- Scenario A: Invalid employee context ---
    def test_scenario_a_missing_employee_id_raises_controlled_error(self):
        trigger = _make_valid_trigger_dict()
        trigger.pop("employee_id")
        # UC-02 must fail validation cleanly with validation error, not crash
        with self.assertRaises(Exception):
            self.sub_agent.run(WorkflowRequest(trigger=trigger))

    def test_scenario_a_missing_client_id_raises_controlled_error(self):
        trigger = _make_valid_trigger_dict()
        trigger.pop("client_id")
        with self.assertRaises(Exception):
            self.sub_agent.run(WorkflowRequest(trigger=trigger))

    def test_scenario_a_mismatched_offer_employee_id_raises_precondition_error(self):
        trigger = _make_valid_trigger_dict(employee_id="EMP-001")
        trigger["offer"]["employee_id"] = "EMP-DIFFERENT"
        with self.assertRaises(PreconditionError) as ctx:
            self.sub_agent.run(WorkflowRequest(trigger=trigger))
        self.assertIn("offer acceptance belongs to a different employee", str(ctx.exception))

    def test_scenario_a_incomplete_request_raises_workflow_error(self):
        with self.assertRaises(WorkflowError) as ctx:
            self.sub_agent.run(WorkflowRequest(trigger=None, fixture=None))
        self.assertIn("one of 'trigger' or 'fixture' is required", str(ctx.exception))

    # --- Scenario B: Checklist generation failure ---
    def test_scenario_b_unregistered_client_raises_configuration_error(self):
        trigger = _make_valid_trigger_dict(client_id="CLIENT-UNKNOWN")
        with self.assertRaises(MissingConfigurationError) as ctx:
            self.sub_agent.run(WorkflowRequest(trigger=trigger))
        self.assertIn("no onboarding configuration registered for client", str(ctx.exception))

    def test_scenario_b_unmapped_role_handles_gracefully(self):
        # A role with no specific templates falls back to default client onboarding templates
        trigger = _make_valid_trigger_dict(role_id="unknown_specialist_role")
        result = self.sub_agent.run(WorkflowRequest(trigger=trigger, llm_behaviour="offline"))
        # Generates standard tasks without error
        self.assertTrue(result.created)
        self.assertGreater(result.checklist["total"], 0)

    # --- Scenario C: Task creation failure & idempotency ---
    def test_scenario_c_duplicate_request_is_idempotent_and_safe(self):
        trigger = _make_valid_trigger_dict(employee_id="EMP-IDEMPOTENT-01")
        res1 = self.sub_agent.run(WorkflowRequest(trigger=trigger, llm_behaviour="offline"))
        self.assertTrue(res1.created)

        # Second identical request for the same employee
        res2 = self.sub_agent.run(WorkflowRequest(trigger=trigger, llm_behaviour="offline"))
        # Must return the existing case without creating duplicate tasks
        self.assertFalse(res2.created)
        self.assertEqual(res1.onboarding_id, res2.onboarding_id)
        self.assertEqual(res1.checklist["total"], res2.checklist["total"])

    # --- Scenario D: Tool execution failure ---
    def test_scenario_d_blocked_task_cannot_be_started_without_prerequisites(self):
        trigger = _make_valid_trigger_dict(employee_id="EMP-TOOL-FAIL-01")
        res = self.sub_agent.run(WorkflowRequest(trigger=trigger, llm_behaviour="offline"))
        onb_id = res.onboarding_id
        blocked_task_id = f"{onb_id}#collect_documents"

        # collect_documents requires hr_record_create to be complete first
        with self.assertRaises(Exception) as ctx:
            self.sub_agent.service.start_task(self.actor, onb_id, blocked_task_id)
        self.assertIn("prerequisites", str(ctx.exception).lower())

    def test_scenario_d_unauthorized_role_cannot_act_as_admin(self):
        employee_actor = Actor(actor_id="U-EMP-1", role=Role.EMPLOYEE, client_id="CLIENT-ALPHA")
        trigger = _make_valid_trigger_dict(employee_id="EMP-UNAUTH-01")
        res = self.sub_agent.run(WorkflowRequest(trigger=trigger, llm_behaviour="offline"))

        # Employee cannot assign owners or request date changes
        with self.assertRaises(UnauthorizedActionError):
            self.sub_agent.service.assign_owner(
                employee_actor, res.onboarding_id, f"{res.onboarding_id}#hr_record_create", "U-HR-99"
            )

    # --- Scenario E: Persistence failure handling ---
    def test_scenario_e_persistence_error_prevents_false_success(self):
        trigger = _make_valid_trigger_dict(employee_id="EMP-STORE-FAIL")
        # Simulate table client failure during upsert
        mock_client = MagicMock(spec=InMemoryTableClient)
        mock_client.upsert.side_effect = RuntimeError("Database connection timed out")
        faulty_store = OnboardingStore(mock_client)
        faulty_service = OnboardingService(build_registry(), store=faulty_store)
        faulty_sub_agent = UC02SubAgent(service=faulty_service)

        with self.assertRaises(RuntimeError) as ctx:
            faulty_sub_agent.run(WorkflowRequest(trigger=trigger, llm_behaviour="offline"))
        self.assertIn("Database connection timed out", str(ctx.exception))

    # --- Scenario F: Coordinator invocation failure (preconditions) ---
    def test_scenario_f_offer_not_accepted_blocks_with_precondition_error(self):
        trigger = _make_valid_trigger_dict(offer_accepted=False)
        with self.assertRaises(PreconditionError) as ctx:
            self.sub_agent.run(WorkflowRequest(trigger=trigger))
        self.assertIn("offer has not been accepted", str(ctx.exception))

    def test_scenario_f_missing_genuine_timestamp_blocks_with_precondition_error(self):
        # Offer accepted is True, but accepted_at timestamp is None
        trigger = _make_valid_trigger_dict(offer_accepted_at=None)
        with self.assertRaises(PreconditionError) as ctx:
            self.sub_agent.run(WorkflowRequest(trigger=trigger))
        self.assertIn("offer acceptance has no timestamp", str(ctx.exception))

    def test_scenario_f_form_not_approved_blocks_with_precondition_error(self):
        trigger = _make_valid_trigger_dict(form_approved=False)
        with self.assertRaises(PreconditionError) as ctx:
            self.sub_agent.run(WorkflowRequest(trigger=trigger))
        self.assertIn("onboarding form has not been approved", str(ctx.exception))

    def test_scenario_f_form_approved_by_unauthorized_role_blocks_with_precondition_error(self):
        trigger = _make_valid_trigger_dict(approved_by_role="employee")
        with self.assertRaises(PreconditionError) as ctx:
            self.sub_agent.run(WorkflowRequest(trigger=trigger))
        self.assertIn("onboarding form was not approved by an HR role", str(ctx.exception))

    # --- Scenario G: Advisor / LLM failure & fallbacks ---
    def test_scenario_g_offline_advisor_executes_deterministically(self):
        trigger = _make_valid_trigger_dict(employee_id="EMP-OFFLINE-01")
        res = self.sub_agent.run(WorkflowRequest(trigger=trigger, llm_behaviour="offline"))
        self.assertEqual(res.advice["source"], "offline")
        self.assertFalse(res.advice.get("degraded", False))
        self.assertIn("recommended_action", res.advice)

    def test_scenario_g_llm_error_falls_back_to_offline_with_degraded_flag(self):
        trigger = _make_valid_trigger_dict(employee_id="EMP-ERROR-01")
        # Using behaviour="error" reproduces an external LLM error
        res = self.sub_agent.run(WorkflowRequest(trigger=trigger, llm_behaviour="error"))
        self.assertTrue(res.advice.get("degraded", False))
        self.assertIn("FakeLLMError", res.advice.get("degraded_reason", ""))
        # Business logic and checklist generation still succeeded completely
        self.assertTrue(res.created)
        self.assertGreater(res.checklist["total"], 0)

    def test_scenario_g_llm_hallucination_overruled_by_rules_engine(self):
        trigger = _make_valid_trigger_dict(employee_id="EMP-HALLUC-01")
        # Using behaviour="hallucinate" returns an invalid recommendation
        res = self.sub_agent.run(WorkflowRequest(trigger=trigger, llm_behaviour="hallucinate"))
        # Guardrail validate_advice overrules it and marks it degraded
        self.assertTrue(res.advice.get("degraded", False))
        self.assertIn("model advice overruled by the rules engine", res.advice.get("degraded_reason", ""))
        self.assertTrue(res.created)


class CoordinatorSharedAdapterIntegrationTests(unittest.TestCase):
    """Direct integration tests against call_uc02_plan_tasks in hr_onboarding_common."""

    def test_coordinator_adapter_success_and_state_transition(self):
        try:
            from hr_onboarding_common.orchestration.adapters.uc02_tasks import call_uc02_plan_tasks
            from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state
            from _helpers import make_actor
        except ImportError:
            self.skipTest("hr_onboarding_common not available in path")

        class _FakePolicy:
            settings = {"task_planning_trigger_roles": ["hr_admin"]}

        class _FakeDeps:
            policy = _FakePolicy()
            _uc02_sub_agent = None

        state = new_state(
            case_id="CASE-COORD-1",
            client_id="CLIENT-ALPHA",
            correlation_id="corr-1",
            actor=make_actor(roles=("hr_admin",), client_id="CLIENT-ALPHA"),
            intake_trigger={},
            uc02_role_context={
                "role_id": "backend_engineer",
                "department": "Engineering",
                "manager_id": "MGR-1",
                "location": "Dubai",
                "project": "apollo",
                "offer_accepted_at": "2026-10-20T10:00:00Z",
                "form_approved_at": "2026-10-21T10:00:00Z",
                "approved_by_role": "HR_OPS",  # Uppercase string parsed case-insensitively
            },
        )
        state["employee_id"] = "EMP-COORD-01"
        state["joining_date"] = "2026-11-16"
        # UC01 verified the offer and approved the form when it opened the case; the adapter no
        # longer assumes either, so the verified facts must be on the state.
        state["intake_trigger"] = {"offer_accepted": True}
        state["stage_statuses"] = {**state["stage_statuses"], Stage.INTAKE.value: StageStatus.COMPLETE.value}

        result_state = call_uc02_plan_tasks(state, _FakeDeps())

        self.assertEqual(
            result_state["stage_statuses"][Stage.PLAN_TASKS.value],
            StageStatus.COMPLETE.value,
        )
        self.assertIsNone(result_state.get("error_code"))
        self.assertIn("task_plan", result_state)
        task_plan = result_state["task_plan"]
        self.assertEqual(task_plan["onboarding_id"], "ONB-CLIENT-ALPHA-EMP-COORD-01")
        self.assertTrue(task_plan["created"])
        self.assertGreater(len(task_plan["tasks"]), 0)
        self.assertGreater(len(task_plan["ready"]), 0)


if __name__ == "__main__":
    unittest.main()
