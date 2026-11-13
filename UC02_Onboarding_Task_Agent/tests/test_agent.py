"""The agentic layer: tools, advisor, the run loop, and the harness binding.

Nothing here contacts a model or a network. ``OfflineAdvisor`` is
deterministic, and the LLM paths are exercised with stub advisors that stand
in for a gateway, including one that behaves adversarially so the "code has
the last word" rule is actually tested rather than asserted in a docstring.

Harness-dependent tests skip cleanly when ``hr_recruitment_common`` is not
installed, and say so.
"""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.agent import tools as uc02_tools
from onboarding_task_management.agent.advisor import (
    RECOMMENDABLE_ACTIONS,
    AgentAdvice,
    OfflineAdvisor,
    advice_payload,
    advise_with_fallback,
    build_advisor,
    derive_action,
    llm_configured,
    validate_advice,
)
from onboarding_task_management.agent.harness import harness_available
from onboarding_task_management.agent.loop import (
    AGENT_ID,
    RESTRICTED_ACTIONS,
    RunStatus,
    UC02Agent,
    make_run_id,
)
from onboarding_task_management.agent.tools import UC02Tools
from onboarding_task_management.contracts import DecisionOutcome, TaskStatus
from onboarding_task_management.persistence import InMemoryTableClient, OnboardingStore
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import (
    ANCHOR_TODAY,
    CLIENT_ALPHA,
    build_registry,
    make_trigger,
    persona,
)
from tests.support import EMPLOYEE, HR_OPS, HR_OPS_APPROVER, HR_OPS_BETA

LATE = date(2026, 12, 31)


class AgentTestCase(unittest.TestCase):
    """A service, a created onboarding and an agent over it."""

    employee_id = "EMP-001"

    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())
        self.record = self.service.create_onboarding(
            HR_OPS, make_trigger(persona(self.employee_id))
        )
        self.onboarding_id = self.record.onboarding_id
        self.agent = UC02Agent(self.service, HR_OPS)

    def task_id(self, key: str) -> str:
        return f"{self.onboarding_id}#{key}"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class ToolSurfaceTests(unittest.TestCase):
    def test_every_tool_is_on_the_policy_allowlist(self) -> None:
        """The tool names in code must match the policy envelope exactly."""
        from tests.test_policy_parity import load_policy

        policy = load_policy()
        allowlisted = set(policy["tools"]["read"]) | set(
            policy["tools"]["write_requires_approval"]
        )
        for tool in uc02_tools.ALL_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, allowlisted)

    def test_read_tools_are_declared_read_in_the_policy(self) -> None:
        from tests.test_policy_parity import load_policy

        policy = load_policy()
        self.assertTrue(set(uc02_tools.READ_TOOLS) <= set(policy["tools"]["read"]))

    def test_write_tools_require_approval_in_the_policy(self) -> None:
        from tests.test_policy_parity import load_policy

        policy = load_policy()
        self.assertTrue(
            set(uc02_tools.WRITE_TOOLS)
            <= set(policy["tools"]["write_requires_approval"])
        )

    def test_no_tool_is_both_read_and_write(self) -> None:
        self.assertEqual(
            set(uc02_tools.READ_TOOLS) & set(uc02_tools.WRITE_TOOLS), set()
        )


class ToolBehaviourTests(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tools = UC02Tools(self.service, HR_OPS)

    def test_get_checklist_returns_the_view(self) -> None:
        result = self.tools.get_checklist(self.onboarding_id, ANCHOR_TODAY)
        self.assertTrue(result.ok)
        self.assertEqual(result.data.onboarding_id, self.onboarding_id)

    def test_a_domain_failure_is_returned_not_raised(self) -> None:
        result = self.tools.get_checklist("ONB-DOES-NOT-EXIST", ANCHOR_TODAY)
        self.assertTrue(result.failed)
        self.assertEqual(result.error_code, "POLICY_BREACH")
        self.assertIn("unknown onboarding", result.error)

    def test_cross_client_access_fails_through_the_tool(self) -> None:
        other = UC02Tools(self.service, HR_OPS_BETA)
        result = other.get_checklist(self.onboarding_id, ANCHOR_TODAY)
        self.assertTrue(result.failed)

    def test_an_unauthorised_actor_cannot_update_a_task(self) -> None:
        tools = UC02Tools(self.service, EMPLOYEE)
        result = tools.update_task_state(
            self.onboarding_id, self.task_id("hr_record_create"), action="start"
        )
        self.assertTrue(result.failed)
        self.assertEqual(result.error_code, "POLICY_BREACH")

    def test_a_blocked_task_cannot_be_started(self) -> None:
        result = self.tools.update_task_state(
            self.onboarding_id, self.task_id("it_laptop"), action="start"
        )
        self.assertTrue(result.failed)

    def test_an_unsupported_action_is_refused(self) -> None:
        result = self.tools.update_task_state(
            self.onboarding_id, self.task_id("hr_record_create"), action="delete"
        )
        self.assertTrue(result.failed)
        self.assertIn("unsupported action", result.error)

    def test_evidence_is_still_required_through_the_tool(self) -> None:
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-007")))
        tools = UC02Tools(service, HR_OPS)
        result = tools.update_task_state(
            record.onboarding_id,
            f"{record.onboarding_id}#medical_test",
            action="complete",
        )
        self.assertTrue(result.failed)
        self.assertEqual(result.error_code, "MISSING_DATA")

    def test_verify_detects_a_matching_state(self) -> None:
        task_id = self.task_id("hr_record_create")
        self.tools.update_task_state(self.onboarding_id, task_id, action="start")
        result = self.tools.verify_task_state(
            self.onboarding_id, task_id, "in_progress"
        )
        self.assertTrue(result.data["matches_expected"])

    def test_verify_detects_a_mismatching_state(self) -> None:
        result = self.tools.verify_task_state(
            self.onboarding_id, self.task_id("hr_record_create"), "complete"
        )
        self.assertFalse(result.data["matches_expected"])
        self.assertEqual(result.data["actual_status"], "ready")

    def test_verify_reports_an_unknown_task_rather_than_raising(self) -> None:
        result = self.tools.verify_task_state(
            self.onboarding_id, "ONB-X#nope", "complete"
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["found"])

    def test_prepare_followup_never_sends(self) -> None:
        result = self.tools.prepare_followup(self.onboarding_id, LATE)
        self.assertTrue(result.ok)
        for draft in result.data["drafts"]:
            self.assertEqual(draft.delivery_status, "draft_not_sent")

    def test_every_call_is_recorded(self) -> None:
        self.tools.get_checklist(self.onboarding_id, ANCHOR_TODAY)
        self.tools.assess_risk(self.onboarding_id, ANCHOR_TODAY)
        self.assertEqual(
            [c.tool for c in self.tools.calls],
            [uc02_tools.GET_CHECKLIST, uc02_tools.ASSESS_RISK],
        )


# ---------------------------------------------------------------------------
# Advisor
# ---------------------------------------------------------------------------


class OfflineAdvisorTests(AgentTestCase):
    def view_and_risk(self, as_of=ANCHOR_TODAY):
        return (
            self.service.checklist(HR_OPS, self.onboarding_id, as_of),
            self.service.joining_risk(HR_OPS, self.onboarding_id, as_of),
        )

    def test_recommends_progressing_a_ready_task(self) -> None:
        advice = OfflineAdvisor().advise(*self.view_and_risk())
        self.assertEqual(advice.recommended_action, "progress_ready_task")
        self.assertEqual(advice.target_task_id, self.task_id("hr_record_create"))

    def test_is_deterministic(self) -> None:
        view, risk = self.view_and_risk()
        self.assertEqual(
            OfflineAdvisor().advise(view, risk).model_dump(),
            OfflineAdvisor().advise(view, risk).model_dump(),
        )

    def test_blockers_name_the_missing_prerequisite(self) -> None:
        advice = OfflineAdvisor().advise(*self.view_and_risk())
        blocker = next(b for b in advice.blockers if b.template_key == "it_laptop")
        self.assertIn("hr_record_create", blocker.reason)

    def test_overdue_work_outranks_ordinary_progress(self) -> None:
        view, risk = self.view_and_risk(LATE)
        action, _, _ = derive_action(view, risk)
        self.assertEqual(action, "prepare_followup")

    def test_every_recommendation_is_an_allowed_action(self) -> None:
        for as_of in (date(2026, 10, 1), ANCHOR_TODAY, LATE):
            with self.subTest(as_of=as_of):
                view, risk = self.view_and_risk(as_of)
                self.assertIn(derive_action(view, risk)[0], RECOMMENDABLE_ACTIONS)

    def test_payload_carries_no_personal_data(self) -> None:
        view, risk = self.view_and_risk()
        payload = advice_payload(view, risk)
        flat = repr(payload).lower()
        for forbidden in ("email", "nationality", "passport", "phone", "@"):
            with self.subTest(term=forbidden):
                self.assertNotIn(forbidden, flat)

    def test_configuration_review_outranks_everything(self) -> None:
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-007")))
        # EMP-007 declares family_insurance, so the dependency-scope conflict
        # does not fire; force the review flag through a context that does.
        view = service.checklist(HR_OPS, record.onboarding_id, ANCHOR_TODAY)
        view.review_required = True
        risk = service.joining_risk(HR_OPS, record.onboarding_id, ANCHOR_TODAY)
        self.assertEqual(derive_action(view, risk)[0], "escalate_configuration_review")


class StubAdvisor:
    """Stands in for a model. Returns whatever the test wants it to return."""

    name = "stub"

    def __init__(self, advice: AgentAdvice | None = None, raises: Exception | None = None):
        self.advice = advice
        self.raises = raises

    def advise(self, view, risk):
        if self.raises is not None:
            raise self.raises
        return self.advice


class AdviceValidationTests(AgentTestCase):
    """Code overrules the model. These are the tests that prove it."""

    def view_and_risk(self):
        return (
            self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY),
            self.service.joining_risk(HR_OPS, self.onboarding_id, ANCHOR_TODAY),
        )

    def advice(self, **overrides) -> AgentAdvice:
        base = {
            "summary": "s",
            "recommended_action": "progress_ready_task",
            "target_task_id": self.task_id("hr_record_create"),
            "rationale": "r",
            "confidence": 0.9,
            "source": "litellm:test-model",
        }
        base.update(overrides)
        return AgentAdvice(**base)

    def test_valid_advice_passes_through(self) -> None:
        view, risk = self.view_and_risk()
        result = validate_advice(self.advice(), view, risk)
        self.assertFalse(result.degraded)
        self.assertEqual(result.recommended_action, "progress_ready_task")

    def test_an_invented_action_is_overruled(self) -> None:
        view, risk = self.view_and_risk()
        result = validate_advice(
            self.advice(recommended_action="delete_everything"), view, risk
        )
        self.assertTrue(result.degraded)
        self.assertIn("not an allowed action", result.degraded_reason)
        self.assertIn(result.recommended_action, RECOMMENDABLE_ACTIONS)

    def test_a_task_from_another_onboarding_is_overruled(self) -> None:
        view, risk = self.view_and_risk()
        result = validate_advice(
            self.advice(target_task_id="ONB-OTHER#hr_record_create"), view, risk
        )
        self.assertTrue(result.degraded)
        self.assertIn("not in this onboarding", result.degraded_reason)

    def test_progressing_a_blocked_task_is_overruled(self) -> None:
        view, risk = self.view_and_risk()
        result = validate_advice(
            self.advice(target_task_id=self.task_id("it_laptop")), view, risk
        )
        self.assertTrue(result.degraded)
        self.assertIn("prerequisites are unmet", result.degraded_reason)

    def test_ignoring_a_required_configuration_review_is_overruled(self) -> None:
        view, risk = self.view_and_risk()
        view.review_required = True
        result = validate_advice(self.advice(), view, risk)
        self.assertTrue(result.degraded)
        self.assertEqual(
            result.recommended_action, "escalate_configuration_review"
        )

    def test_a_model_failure_degrades_rather_than_crashing(self) -> None:
        view, risk = self.view_and_risk()
        result = advise_with_fallback(
            StubAdvisor(raises=RuntimeError("gateway timeout")), view, risk
        )
        self.assertTrue(result.degraded)
        self.assertIn("gateway timeout", result.degraded_reason)
        self.assertIn(result.recommended_action, RECOMMENDABLE_ACTIONS)

    def test_the_source_is_preserved_when_advice_is_overruled(self) -> None:
        view, risk = self.view_and_risk()
        result = validate_advice(
            self.advice(recommended_action="nonsense"), view, risk
        )
        self.assertEqual(result.source, "litellm:test-model")


class AdvisorSelectionTests(unittest.TestCase):
    """Selection follows ``LLM_MODE``; provider credentials are never read."""

    def test_offline_when_llm_mode_is_unset(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"UC02_LLM_FAKE": "0"}):
            os.environ.pop("LLM_MODE", None)
            self.assertFalse(llm_configured())
            self.assertIsInstance(build_advisor(), OfflineAdvisor)

    def test_offline_switch_forces_the_deterministic_advisor(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(
            os.environ, {"UC02_LLM_OFFLINE": "1", "LLM_MODE": "online", "UC02_LLM_FAKE": "0"}
        ):
            self.assertFalse(llm_configured())
            self.assertIsInstance(build_advisor(), OfflineAdvisor)

    def test_a_provider_key_alone_does_not_select_the_gateway(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(
            os.environ,
            {"UC02_LLM_OFFLINE": "0", "LLM_MODE": "offline", "ANTHROPIC_API_KEY": "placeholder"},
        ):
            self.assertFalse(llm_configured())

    def test_online_llm_mode_selects_the_gateway(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"UC02_LLM_OFFLINE": "0", "LLM_MODE": "online"}):
            self.assertTrue(llm_configured())


# ---------------------------------------------------------------------------
# The run loop
# ---------------------------------------------------------------------------


class AgentRunTests(AgentTestCase):
    def test_a_run_acts_and_verifies(self) -> None:
        result = self.agent.run(self.onboarding_id, ANCHOR_TODAY)
        self.assertIs(result.status, RunStatus.ACTED)
        self.assertTrue(result.verified)
        self.assertEqual(
            [s.name for s in result.steps],
            ["retrieve", "analyse", "advise", "execute", "verify"],
        )

    def test_the_action_really_changed_the_record(self) -> None:
        self.agent.run(self.onboarding_id, ANCHOR_TODAY)
        task = self.service.get(HR_OPS, self.onboarding_id).task_by_key(
            "hr_record_create"
        )
        self.assertIs(task.status, TaskStatus.IN_PROGRESS)

    def test_an_unknown_onboarding_is_an_exception_not_a_crash(self) -> None:
        result = self.agent.run("ONB-NOPE", ANCHOR_TODAY)
        self.assertIs(result.status, RunStatus.EXCEPTION)
        self.assertTrue(result.exceptions)

    def test_an_unauthorised_actor_cannot_act(self) -> None:
        agent = UC02Agent(self.service, EMPLOYEE)
        result = agent.run(self.onboarding_id, ANCHOR_TODAY)
        self.assertIs(result.status, RunStatus.EXCEPTION)
        self.assertEqual(result.actions_taken, [])

    def test_a_restricted_recommendation_becomes_a_proposal(self) -> None:
        advice = AgentAdvice(
            summary="s",
            recommended_action="escalate_configuration_review",
            rationale="configuration needs confirming",
        )
        agent = UC02Agent(self.service, HR_OPS, advisor=StubAdvisor(advice))
        result = agent.run(self.onboarding_id, ANCHOR_TODAY)
        self.assertIs(result.status, RunStatus.PROPOSED)
        self.assertEqual(result.actions_taken, [])
        self.assertEqual(result.proposals[0].requires_approval_by, "hr_ops")

    def test_a_restricted_action_never_executes_itself(self) -> None:
        for action in sorted(RESTRICTED_ACTIONS):
            with self.subTest(action=action):
                service = OnboardingService(build_registry())
                record = service.create_onboarding(
                    HR_OPS, make_trigger(persona("EMP-001"))
                )
                agent = UC02Agent(
                    service,
                    HR_OPS,
                    advisor=StubAdvisor(
                        AgentAdvice(
                            summary="s", recommended_action=action, rationale="r"
                        )
                    ),
                )
                result = agent.run(record.onboarding_id, ANCHOR_TODAY)
                self.assertEqual(result.actions_taken, [])
                self.assertIs(result.status, RunStatus.PROPOSED)

    def test_nothing_to_do_is_reported_as_observed(self) -> None:
        advice = AgentAdvice(
            summary="s", recommended_action="no_action_required", rationale="r"
        )
        agent = UC02Agent(self.service, HR_OPS, advisor=StubAdvisor(advice))
        result = agent.run(self.onboarding_id, ANCHOR_TODAY)
        self.assertIs(result.status, RunStatus.OBSERVED)

    def test_a_degraded_model_still_produces_a_safe_run(self) -> None:
        agent = UC02Agent(
            self.service, HR_OPS, advisor=StubAdvisor(raises=RuntimeError("boom"))
        )
        result = agent.run(self.onboarding_id, ANCHOR_TODAY)
        self.assertTrue(result.degraded)
        self.assertIn("boom", result.degraded_reason)
        self.assertIs(result.status, RunStatus.ACTED)

    def test_run_ids_are_deterministic(self) -> None:
        self.assertEqual(
            make_run_id("ONB-X", date(2026, 10, 26), 1),
            "RUN-ONB-X-2026-10-26-001",
        )

    def test_repeated_runs_get_distinct_ids(self) -> None:
        first = self.agent.run(self.onboarding_id, ANCHOR_TODAY)
        second = self.agent.run(self.onboarding_id, ANCHOR_TODAY)
        self.assertNotEqual(first.run_id, second.run_id)

    def test_every_run_is_audited(self) -> None:
        self.agent.run(self.onboarding_id, ANCHOR_TODAY)
        actions = self.service.audit.actions_for(HR_OPS)
        self.assertIn("agent.started", actions)
        self.assertIn("agent.acted", actions)
        self.assertIn("agent.finished", actions)

    def test_a_denied_action_is_audited(self) -> None:
        agent = UC02Agent(self.service, EMPLOYEE)
        agent.run(self.onboarding_id, ANCHOR_TODAY)
        self.assertIn("agent.action_denied", self.service.audit.actions_for(EMPLOYEE))

    def test_the_run_records_the_advisor_used(self) -> None:
        result = self.agent.run(self.onboarding_id, ANCHOR_TODAY)
        self.assertEqual(result.advisor, "offline")
        self.assertIsNone(result.model)


class VerificationFailureTests(AgentTestCase):
    """A write that cannot be verified must never be reported as an action."""

    def test_an_unverifiable_write_is_an_exception(self) -> None:
        agent = UC02Agent(self.service, HR_OPS)

        original = agent.tools.verify_task_state

        def lying_verify(onboarding_id, task_id, expected_status):
            result = original(onboarding_id, task_id, expected_status)
            # Simulate the record disagreeing with the write.
            result.data["matches_expected"] = False
            return result

        agent.tools.verify_task_state = lying_verify
        result = agent.run(self.onboarding_id, ANCHOR_TODAY)

        self.assertIs(result.status, RunStatus.EXCEPTION)
        self.assertFalse(result.verified)
        self.assertEqual(result.actions_taken, [])
        self.assertIn(
            "agent.verification_failed", self.service.audit.actions_for(HR_OPS)
        )


class AgentPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = InMemoryTableClient()
        self.store = OnboardingStore(self.client)
        self.service = OnboardingService(build_registry(), store=self.store)
        self.record = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"))
        )
        self.agent = UC02Agent(self.service, HR_OPS, store=self.store)

    def test_an_agent_run_is_recorded(self) -> None:
        result = self.agent.run(self.record.onboarding_id, ANCHOR_TODAY)
        runs = self.store.agent_runs(CLIENT_ALPHA)
        self.assertEqual([r["run_id"] for r in runs], [result.run_id])
        self.assertEqual(runs[0]["agent_id"], AGENT_ID)
        self.assertEqual(runs[0]["status"], "acted")
        self.assertEqual(runs[0]["advisor"], "offline")
        self.assertIsNone(runs[0]["model"])

    def test_the_agents_write_is_persisted(self) -> None:
        self.agent.run(self.record.onboarding_id, ANCHOR_TODAY)
        rebuilt = OnboardingService(
            build_registry(),
            store=OnboardingStore(
                InMemoryTableClient.from_snapshot(self.client.snapshot())
            ),
        )
        rebuilt.hydrate(CLIENT_ALPHA)
        task = rebuilt.get(HR_OPS, self.record.onboarding_id).task_by_key(
            "hr_record_create"
        )
        self.assertIs(task.status, TaskStatus.IN_PROGRESS)

    def test_the_run_row_carries_no_prompt_or_content(self) -> None:
        result = self.agent.run(self.record.onboarding_id, ANCHOR_TODAY)
        row = result.to_row()
        self.assertNotIn("summary", row)
        self.assertNotIn("rationale", row)
        self.assertNotIn("advice", row)


# ---------------------------------------------------------------------------
# Harness binding
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    harness_available(), "hr_recruitment_common is not installed in this environment"
)
class HarnessBindingTests(AgentTestCase):
    def test_the_uc02_policy_loads_into_the_shared_policy_store(self) -> None:
        from onboarding_task_management.agent.harness import load_policy

        policy = load_policy()
        self.assertEqual(policy.agent, AGENT_ID)
        self.assertTrue(policy.enabled)

    def test_every_tool_is_accepted_by_the_shared_tool_gateway_policy(self) -> None:
        from onboarding_task_management.agent.harness import assert_tools_allowlisted

        modes = assert_tools_allowlisted()
        self.assertEqual(len(modes), len(uc02_tools.ALL_TOOLS))
        self.assertEqual(
            set(modes[: len(uc02_tools.READ_TOOLS)]), {"read"}
        )

    def test_an_unknown_tool_is_refused_by_the_policy(self) -> None:
        from hr_recruitment_common.platform.policy import PolicyViolation

        from onboarding_task_management.agent.harness import load_policy

        with self.assertRaises(PolicyViolation):
            load_policy().tool_mode("uc02.delete_everything")

    def test_an_l0_agent_may_not_call_a_write_tool(self) -> None:
        from hr_recruitment_common.contracts import Autonomy
        from hr_recruitment_common.platform.policy import PolicyViolation

        from onboarding_task_management.agent.harness import load_policy

        l0 = load_policy().model_copy(update={"autonomy": Autonomy.L0})
        self.assertEqual(l0.tool_mode(uc02_tools.GET_CHECKLIST), "read")
        with self.assertRaises(PolicyViolation):
            l0.tool_mode(uc02_tools.UPDATE_TASK_STATE)

    def test_the_langgraph_run_matches_the_plain_loop(self) -> None:
        from onboarding_task_management.agent.harness import HarnessBoundAgent

        graph_service = OnboardingService(build_registry())
        graph_record = graph_service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"))
        )
        bound = HarnessBoundAgent(UC02Agent(graph_service, HR_OPS))
        graph_result = bound.run(graph_record.onboarding_id, ANCHOR_TODAY)
        plain_result = self.agent.run(self.onboarding_id, ANCHOR_TODAY)

        self.assertIs(graph_result.status, plain_result.status)
        self.assertEqual(
            graph_result.advice.recommended_action,
            plain_result.advice.recommended_action,
        )
        self.assertEqual([s.name for s in graph_result.steps],
                         [s.name for s in plain_result.steps])

    def test_the_kill_switch_stops_the_run(self) -> None:
        from onboarding_task_management.agent.harness import HarnessBoundAgent

        bound = HarnessBoundAgent(UC02Agent(self.service, HR_OPS))
        bound.policy = bound.policy.model_copy(update={"enabled": False})
        result = bound.run(self.onboarding_id, ANCHOR_TODAY)
        self.assertIs(result.status, RunStatus.EXCEPTION)
        self.assertIn("kill switch", result.exceptions[0])
        self.assertEqual(result.actions_taken, [])

    def test_integration_status_reports_facts(self) -> None:
        from onboarding_task_management.agent.harness import integration_status

        status = integration_status()
        self.assertTrue(status["harness_installed"])
        self.assertTrue(status["policy_loaded"])
        # No MCP server exists in this repository.
        self.assertFalse(status["mcp_server"])
        # No credential is configured, so no model call can be claimed.
        self.assertFalse(status["llm_credential_configured"])


class HarnessOptionalityTests(unittest.TestCase):
    """UC-02 must keep working if the shared harness is absent."""

    def test_the_agent_package_imports_without_the_harness(self) -> None:
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        code = (
            "import sys; "
            "sys.modules['hr_recruitment_common'] = None; "
            "sys.path.insert(0, r'" + str(root / "src") + "'); "
            "import onboarding_task_management.agent as a; "
            "from onboarding_task_management.agent import UC02Agent, OfflineAdvisor; "
            "print('ok')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ok", proc.stdout)

    def test_a_run_needs_no_harness(self) -> None:
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        result = UC02Agent(service, HR_OPS).run(record.onboarding_id, ANCHOR_TODAY)
        self.assertIs(result.status, RunStatus.ACTED)


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class EndToEndWorkflowTests(unittest.TestCase):
    """Input → plan → persist → agent → approval → act → verify → audit."""

    def test_the_full_uc02_workflow(self) -> None:
        client = InMemoryTableClient()
        store = OnboardingStore(client)
        service = OnboardingService(build_registry(), store=store)

        # 1. Validate start conditions and generate the dynamic checklist.
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-007")))
        onboarding_id = record.onboarding_id
        self.assertEqual(len(record.tasks), 23)

        # 2. The plan is persisted.
        self.assertEqual(client.row_count("onboarding"), 1)
        self.assertEqual(client.row_count("task"), 23)

        # 3. The agent retrieves context and acts, with verification.
        agent = UC02Agent(service, HR_OPS, store=store)
        first = agent.run(onboarding_id, ANCHOR_TODAY)
        self.assertIs(first.status, RunStatus.ACTED)
        self.assertTrue(first.verified)

        # 4. A restricted change is proposed, never self-applied.
        change = service.request_change(
            HR_OPS, onboarding_id, joining_date=date(2026, 11, 30)
        )
        self.assertIs(change.outcome, DecisionOutcome.PENDING)

        # 5. A human approves; only then does the plan change.
        service.decide_change(
            HR_OPS_APPROVER, change.request_id, DecisionOutcome.APPROVED
        )
        outcome = service.apply_change(HR_OPS, change.request_id)
        self.assertEqual(outcome.revision, 1)

        # 6. Completed work survived the re-plan.
        view = service.checklist(HR_OPS, onboarding_id, ANCHOR_TODAY)
        self.assertEqual(view.revision, 1)

        # 7. Everything is durable and reloadable.
        rebuilt = OnboardingService(
            build_registry(),
            store=OnboardingStore(InMemoryTableClient.from_snapshot(client.snapshot())),
        )
        self.assertEqual(rebuilt.hydrate(CLIENT_ALPHA), 1)
        reloaded = rebuilt.get(HR_OPS, onboarding_id)
        self.assertEqual(reloaded.revision, 1)
        self.assertEqual(reloaded.joining_date, date(2026, 11, 30))

        # 8. The audit trail records the agent and the approval.
        actions = service.audit.actions_for(HR_OPS)
        for expected in ("onboarding.create", "agent.started", "agent.acted",
                         "change.request", "change.apply"):
            with self.subTest(action=expected):
                self.assertIn(expected, actions)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
