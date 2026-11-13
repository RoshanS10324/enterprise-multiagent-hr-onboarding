"""The deterministic fake LLM, and the eight workflow scenarios it enables.

No network, no credential, no database. The fake drives the **same**
``LiteLLMAdvisor`` a real gateway would, so these tests exercise prompt
assembly, the ``<input>`` round-trip, schema validation and source tagging —
the adapter code that stub advisors bypass.

``ScenarioWorkflowTests`` is the deliverable: the eight scenarios the brief
asks for, each asserted on observable state rather than on a log line.
"""

from __future__ import annotations

import json
import unittest
from datetime import date

from onboarding_task_management.agent import UC02Agent
from onboarding_task_management.agent.advisor import (
    RECOMMENDABLE_ACTIONS,
    SYSTEM_PREAMBLE,
    LiteLLMAdvisor,
    OfflineAdvisor,
    advice_payload,
    advisor_mode,
    build_advisor,
    input_block,
)
from onboarding_task_management.agent.fake_llm import (
    DOCUMENT_KEYS,
    FAKE_MODEL,
    HALLUCINATED_TASK_ID,
    SCENARIO_CONFIGURATION_REVIEW,
    SCENARIO_DEPENDENT_BLOCKED,
    SCENARIO_DOCUMENTS_INCOMPLETE,
    SCENARIO_JOINING_RISK,
    SCENARIO_NOTHING_TO_DO,
    SCENARIO_OVERDUE_FOLLOWUP,
    SCENARIO_READY_WORK,
    FakeLLMAdvisor,
    FakeLLMClient,
    FakeLLMError,
    build_fake_advisor,
    classify,
    extract_input_block,
    respond,
)
from onboarding_task_management.agent.loop import RunStatus
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
from tests.support import EMPLOYEE, HR_OPS, HR_OPS_APPROVER

LATE = date(2026, 12, 31)


def service_with(employee_id: str = "EMP-007"):
    service = OnboardingService(build_registry())
    record = service.create_onboarding(HR_OPS, make_trigger(persona(employee_id)))
    return service, record


def payload_for(service, onboarding_id, as_of=ANCHOR_TODAY) -> dict:
    return advice_payload(
        service.checklist(HR_OPS, onboarding_id, as_of),
        service.joining_risk(HR_OPS, onboarding_id, as_of),
    )


# ---------------------------------------------------------------------------
# What the offline advisor actually is
# ---------------------------------------------------------------------------


class AdvisorTaxonomyTests(unittest.TestCase):
    """Pin down the labels, so nothing is misdescribed later."""

    def test_offline_advisor_is_rule_based_not_a_model(self) -> None:
        advisor = OfflineAdvisor()
        self.assertFalse(hasattr(advisor, "client"))
        self.assertFalse(hasattr(advisor, "model"))
        self.assertEqual(advisor.name, "offline")

    def test_the_fake_is_a_model_shaped_client_behind_the_real_adapter(self) -> None:
        advisor = build_fake_advisor()
        self.assertIsInstance(advisor, LiteLLMAdvisor)
        self.assertEqual(advisor.name, "fake_llm")
        self.assertEqual(advisor.model(), FAKE_MODEL)

    def test_the_fake_model_id_announces_itself(self) -> None:
        self.assertTrue(FAKE_MODEL.startswith("fake/"))

    def test_advisor_mode_reports_what_will_be_used(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"UC02_LLM_FAKE": "1"}):
            self.assertEqual(advisor_mode(), "fake")
            self.assertIsInstance(build_advisor(), FakeLLMAdvisor)
        with mock.patch.dict(os.environ, {"UC02_LLM_FAKE": "0"}):
            self.assertEqual(advisor_mode(), "offline")


# ---------------------------------------------------------------------------
# The adapter path the fake exercises
# ---------------------------------------------------------------------------


class PromptRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.record = service_with()
        self.client = FakeLLMClient()
        self.advisor = FakeLLMAdvisor(self.client)

    def advise(self, as_of=ANCHOR_TODAY):
        return self.advisor.advise(
            self.service.checklist(HR_OPS, self.record.onboarding_id, as_of),
            self.service.joining_risk(HR_OPS, self.record.onboarding_id, as_of),
        )

    def test_the_model_receives_the_system_preamble(self) -> None:
        self.advise()
        self.assertEqual(self.client.calls[0].system, SYSTEM_PREAMBLE)

    def test_the_model_receives_the_structured_input_block(self) -> None:
        self.advise()
        payload = self.client.last_payload
        self.assertEqual(payload["onboarding_id"], self.record.onboarding_id)
        self.assertIn("tasks", payload)
        self.assertIn("allowed_actions", payload)

    def test_the_input_block_round_trips_exactly(self) -> None:
        original = {"b": 2, "a": [1, 2], "c": {"d": True}}
        self.assertEqual(extract_input_block(input_block(original)), original)

    def test_the_payload_is_sorted_so_the_prompt_is_cache_stable(self) -> None:
        rendered = input_block({"z": 1, "a": 2})
        self.assertLess(rendered.index('"a"'), rendered.index('"z"'))

    def test_a_prompt_without_an_input_block_is_refused_not_guessed(self) -> None:
        with self.assertRaises(FakeLLMError):
            self.client.structured(
                task="uc02.advise",
                tier="workhorse",
                system="s",
                user="no input block here",
                schema=type(self.advise()),
            )

    def test_the_task_name_and_tier_are_recorded(self) -> None:
        self.advise()
        call = self.client.calls[0]
        self.assertEqual(call.task, "uc02.advise")
        self.assertEqual(call.model, FAKE_MODEL)
        self.assertTrue(call.tier)

    def test_advice_is_tagged_as_coming_from_the_fake(self) -> None:
        advice = self.advise()
        self.assertEqual(advice.source, f"fake_llm:{FAKE_MODEL}")

    def test_the_payload_carries_no_personal_data(self) -> None:
        self.advise()
        flat = json.dumps(self.client.last_payload).lower()
        for forbidden in ("email", "nationality", "passport", "phone", "@"):
            with self.subTest(term=forbidden):
                self.assertNotIn(forbidden, flat)

    def test_the_fake_is_deterministic(self) -> None:
        first = self.advise().model_dump()
        second = FakeLLMAdvisor(FakeLLMClient()).advise(
            self.service.checklist(HR_OPS, self.record.onboarding_id, ANCHOR_TODAY),
            self.service.joining_risk(HR_OPS, self.record.onboarding_id, ANCHOR_TODAY),
        ).model_dump()
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# Scenario classification and canned answers
# ---------------------------------------------------------------------------


class ClassificationTests(unittest.TestCase):
    """Scenarios are recognised from the payload alone."""

    def payload(self, **overrides) -> dict:
        base = {
            "counts": {"ready": 0, "blocked": 0, "overdue": 0, "complete": 0},
            "at_risk": False,
            "review_required": False,
            "issues": [],
            "tasks": [],
            "risk_reasons": [],
        }
        base.update(overrides)
        return base

    def task(self, key="t", **overrides) -> dict:
        base = {
            "task_id": f"ONB-X#{key}",
            "template_key": key,
            "status": "blocked",
            "owner": "hr_ops",
            "due_date": "2026-11-01",
            "is_overdue": False,
            "blocked_by": [],
            "evidence_required": False,
            "approval_role": None,
        }
        base.update(overrides)
        return base

    def test_configuration_review_outranks_everything(self) -> None:
        payload = self.payload(
            review_required=True,
            at_risk=True,
            counts={"ready": 3, "overdue": 2},
            issues=[{"code": "x", "severity": "review_required", "subject": "cfg"}],
        )
        self.assertEqual(classify(payload), SCENARIO_CONFIGURATION_REVIEW)

    def test_joining_risk_outranks_overdue(self) -> None:
        payload = self.payload(at_risk=True, counts={"overdue": 1, "ready": 1})
        self.assertEqual(classify(payload), SCENARIO_JOINING_RISK)

    def test_overdue_outranks_ready_work(self) -> None:
        payload = self.payload(counts={"overdue": 1, "ready": 2})
        self.assertEqual(classify(payload), SCENARIO_OVERDUE_FOLLOWUP)

    def test_ready_work_is_recognised(self) -> None:
        self.assertEqual(
            classify(self.payload(counts={"ready": 1})), SCENARIO_READY_WORK
        )

    def test_incomplete_documents_are_recognised(self) -> None:
        payload = self.payload(
            counts={"ready": 0, "blocked": 1},
            tasks=[self.task("visa_pack_handover", blocked_by=["collect_documents"])],
        )
        self.assertEqual(classify(payload), SCENARIO_DOCUMENTS_INCOMPLETE)

    def test_a_non_document_blocker_is_the_generic_dependent_scenario(self) -> None:
        payload = self.payload(
            counts={"ready": 0, "blocked": 1},
            tasks=[self.task("it_accounts", blocked_by=["it_laptop"])],
        )
        self.assertEqual(classify(payload), SCENARIO_DEPENDENT_BLOCKED)

    def test_nothing_to_do_is_recognised(self) -> None:
        self.assertEqual(
            classify(self.payload(counts={"complete": 5})), SCENARIO_NOTHING_TO_DO
        )

    def test_every_scenario_recommends_an_allowed_action(self) -> None:
        scenarios = {
            SCENARIO_CONFIGURATION_REVIEW: self.payload(
                review_required=True,
                issues=[{"code": "c", "severity": "review_required", "subject": "s"}],
            ),
            SCENARIO_JOINING_RISK: self.payload(
                at_risk=True, risk_reasons=["two days to joining"]
            ),
            SCENARIO_OVERDUE_FOLLOWUP: self.payload(
                counts={"overdue": 1},
                tasks=[self.task("late", is_overdue=True, status="ready")],
            ),
            SCENARIO_READY_WORK: self.payload(
                counts={"ready": 1},
                tasks=[self.task("go", status="ready")],
            ),
            SCENARIO_DOCUMENTS_INCOMPLETE: self.payload(
                tasks=[self.task("visa", blocked_by=["collect_documents"])]
            ),
            SCENARIO_DEPENDENT_BLOCKED: self.payload(
                tasks=[self.task("b", blocked_by=["a"])]
            ),
            SCENARIO_NOTHING_TO_DO: self.payload(),
        }
        for scenario, payload in scenarios.items():
            with self.subTest(scenario=scenario):
                self.assertEqual(classify(payload), scenario)
                advice = respond(payload, scenario)
                self.assertIn(advice.recommended_action, RECOMMENDABLE_ACTIONS)
                self.assertTrue(advice.summary)
                self.assertTrue(advice.rationale)

    def test_document_keys_are_configured_not_guessed(self) -> None:
        self.assertIn("collect_documents", DOCUMENT_KEYS)


class ResponseGroundingTests(unittest.TestCase):
    """Canned prose must quote the payload, never invent."""

    def test_the_document_answer_names_the_real_dependants(self) -> None:
        service, record = service_with("EMP-007")
        for task in service.ready_tasks(HR_OPS, record.onboarding_id):
            service.start_task(HR_OPS, record.onboarding_id, task.task_id)

        payload = payload_for(service, record.onboarding_id)
        self.assertEqual(classify(payload), SCENARIO_DOCUMENTS_INCOMPLETE)
        advice = respond(payload, SCENARIO_DOCUMENTS_INCOMPLETE)
        self.assertIn("visa_pack_handover", advice.rationale)
        self.assertIn("document collection", advice.rationale)

    def test_the_overdue_answer_quotes_the_stored_due_date(self) -> None:
        service, record = service_with("EMP-001")
        payload = payload_for(service, record.onboarding_id, LATE)
        overdue = next(t for t in payload["tasks"] if t["is_overdue"])
        advice = respond(payload, SCENARIO_OVERDUE_FOLLOWUP)
        self.assertIn(overdue["due_date"], advice.rationale)

    def test_blockers_are_grounded_in_real_task_ids(self) -> None:
        service, record = service_with("EMP-007")
        payload = payload_for(service, record.onboarding_id)
        known = {t["task_id"] for t in payload["tasks"]}
        advice = respond(payload, classify(payload))
        for blocker in advice.blockers:
            with self.subTest(task=blocker.task_id):
                self.assertIn(blocker.task_id, known)


# ---------------------------------------------------------------------------
# Failure injection
# ---------------------------------------------------------------------------


class FakeFailureModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.record = service_with("EMP-001")

    def run_with(self, behaviour: str):
        agent = UC02Agent(
            self.service, HR_OPS, advisor=build_fake_advisor(behaviour)
        )
        return agent.run(self.record.onboarding_id, ANCHOR_TODAY)

    def test_scripted_behaviour_is_not_degraded(self) -> None:
        result = self.run_with("scripted")
        self.assertFalse(result.degraded)
        self.assertEqual(result.model, FAKE_MODEL)

    def test_a_gateway_error_degrades_and_says_so(self) -> None:
        result = self.run_with("error")
        self.assertTrue(result.degraded)
        self.assertIn("upstream timeout", result.degraded_reason)
        # Degraded, but still safe and still useful.
        self.assertIn(result.advice.recommended_action, RECOMMENDABLE_ACTIONS)

    def test_schema_invalid_output_degrades(self) -> None:
        result = self.run_with("invalid")
        self.assertTrue(result.degraded)
        self.assertIn("schema validation failed", result.degraded_reason)

    def test_a_hallucination_is_overruled_by_the_rules_engine(self) -> None:
        result = self.run_with("hallucinate")
        self.assertTrue(result.degraded)
        self.assertIn("is not in this onboarding", result.degraded_reason)
        self.assertNotEqual(result.advice.target_task_id, HALLUCINATED_TASK_ID)

    def test_a_hallucinated_access_grant_never_executes(self) -> None:
        self.run_with("hallucinate")
        record = self.service.get(HR_OPS, self.record.onboarding_id)
        for task in record.tasks:
            with self.subTest(task=task.template_key):
                self.assertIsNot(task.status, TaskStatus.COMPLETE)

    def test_a_degraded_run_is_audited(self) -> None:
        self.run_with("error")
        self.assertIn(
            "agent.advice_degraded", self.service.audit.actions_for(HR_OPS)
        )


# ---------------------------------------------------------------------------
# The eight required scenarios, end to end
# ---------------------------------------------------------------------------


class ScenarioWorkflowTests(unittest.TestCase):
    """Retrieve → analyse → fake advice → validate → tool → verify → audit."""

    def setUp(self) -> None:
        self.table = InMemoryTableClient()
        self.store = OnboardingStore(self.table)
        self.service = OnboardingService(build_registry(), store=self.store)
        self.record = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-007"))
        )
        self.onboarding_id = self.record.onboarding_id

    def agent(self, behaviour: str = "scripted", actor=HR_OPS) -> UC02Agent:
        return UC02Agent(
            self.service, actor, advisor=build_fake_advisor(behaviour), store=self.store
        )

    def task_id(self, key: str) -> str:
        return f"{self.onboarding_id}#{key}"

    # -- 1 ------------------------------------------------------------------
    def test_1_a_normal_checklist_is_retrieved(self) -> None:
        advisor = build_fake_advisor()
        result = UC02Agent(self.service, HR_OPS, advisor=advisor).run(
            self.onboarding_id, ANCHOR_TODAY
        )
        self.assertEqual(
            [s.name for s in result.steps][:3], ["retrieve", "analyse", "advise"]
        )
        self.assertEqual(advisor.fake_client.last_payload["counts"]["total"], 23)

    # -- 2 ------------------------------------------------------------------
    def test_2_a_blocked_visa_task_is_explained_by_its_real_prerequisite(self) -> None:
        for task in self.service.ready_tasks(HR_OPS, self.onboarding_id):
            self.service.start_task(HR_OPS, self.onboarding_id, task.task_id)

        advisor = build_fake_advisor()
        result = UC02Agent(self.service, HR_OPS, advisor=advisor).run(
            self.onboarding_id, ANCHOR_TODAY
        )
        self.assertEqual(
            advisor.fake_client.last_scenario, SCENARIO_DOCUMENTS_INCOMPLETE
        )
        self.assertIn("visa_pack_handover", result.advice.rationale)

        # The explanation matches the stored record, not a guess.
        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        handover = next(
            e for e in view.entries if e.template_key == "visa_pack_handover"
        )
        self.assertEqual(handover.blocked_by_keys, ["collect_documents"])

    # -- 3 ------------------------------------------------------------------
    def test_3_the_agent_recommends_the_correct_next_action(self) -> None:
        # Capture readiness BEFORE the run: the agent starts the task it picks,
        # so afterwards that task is IN_PROGRESS and no longer ready.
        ready_before = self.service.checklist(
            HR_OPS, self.onboarding_id, ANCHOR_TODAY
        ).ready_task_ids

        result = self.agent().run(self.onboarding_id, ANCHOR_TODAY)

        self.assertEqual(result.advice.recommended_action, "progress_ready_task")
        self.assertIn(result.advice.target_task_id, ready_before)

    # -- 4 ------------------------------------------------------------------
    def test_4a_an_invented_action_is_rejected(self) -> None:
        result = self.agent("hallucinate").run(self.onboarding_id, ANCHOR_TODAY)
        self.assertTrue(result.degraded)
        self.assertEqual(
            self.table.row_count("agent_run"), 1, "the run is still recorded"
        )

    def test_4b_an_unauthorised_actor_is_rejected(self) -> None:
        result = self.agent(actor=EMPLOYEE).run(self.onboarding_id, ANCHOR_TODAY)
        self.assertIs(result.status, RunStatus.EXCEPTION)
        self.assertEqual(result.actions_taken, [])
        self.assertIn("POLICY_BREACH", result.exceptions[0])

    # -- 5 ------------------------------------------------------------------
    def test_5_a_restricted_action_waits_for_human_approval(self) -> None:
        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        view.review_required = True
        risk = self.service.joining_risk(HR_OPS, self.onboarding_id, ANCHOR_TODAY)

        advisor = build_fake_advisor()
        advice = advisor.advise(view, risk)
        self.assertEqual(
            advisor.fake_client.last_scenario, SCENARIO_CONFIGURATION_REVIEW
        )
        self.assertEqual(advice.recommended_action, "escalate_configuration_review")

        agent = UC02Agent(self.service, HR_OPS, advisor=advisor, store=self.store)
        result = agent.run(self.onboarding_id, ANCHOR_TODAY)
        # The live record is not review_required, so the fake sees ready work;
        # drive the restricted path explicitly instead of faking the record.
        agent._decide_and_act(result, advice, self.onboarding_id, ANCHOR_TODAY)
        self.assertIs(result.status, RunStatus.PROPOSED)
        self.assertEqual(result.proposals[-1].requires_approval_by, "hr_ops")

    def test_5b_a_gated_change_cannot_be_applied_without_approval(self) -> None:
        from onboarding_task_management.errors import ApprovalRequiredError

        tools = UC02Tools(self.service, HR_OPS)
        request = tools.request_approval(
            self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        self.assertTrue(request.ok)
        self.assertIs(request.data.outcome, DecisionOutcome.PENDING)
        with self.assertRaises(ApprovalRequiredError):
            self.service.apply_change(HR_OPS, request.data.request_id)

        self.service.decide_change(
            HR_OPS_APPROVER, request.data.request_id, DecisionOutcome.APPROVED
        )
        outcome = self.service.apply_change(HR_OPS, request.data.request_id)
        self.assertEqual(outcome.revision, 1)

    # -- 6 ------------------------------------------------------------------
    def test_6_a_failed_tool_call_does_not_report_success(self) -> None:
        agent = self.agent()

        def failing_update(onboarding_id, task_id, *, action, evidence_ref=None):
            from onboarding_task_management.agent.tools import (
                UPDATE_TASK_STATE,
                ToolResult,
            )

            return ToolResult(
                tool=UPDATE_TASK_STATE,
                ok=False,
                error="mock tool failure: ITSM connector unavailable",
                error_code="INTERNAL_ERROR",
            )

        agent.tools.update_task_state = failing_update
        result = agent.run(self.onboarding_id, ANCHOR_TODAY)

        self.assertIs(result.status, RunStatus.EXCEPTION)
        self.assertEqual(result.actions_taken, [])
        self.assertIn("ITSM connector unavailable", result.exceptions[0])
        # Nothing moved.
        record = self.service.get(HR_OPS, self.onboarding_id)
        self.assertIs(
            record.task_by_key("hr_record_create").status, TaskStatus.READY
        )

    def test_6b_an_unverifiable_write_is_not_reported_as_an_action(self) -> None:
        agent = self.agent()
        original = agent.tools.verify_task_state

        def disagreeing_verify(onboarding_id, task_id, expected_status):
            result = original(onboarding_id, task_id, expected_status)
            result.data["matches_expected"] = False
            return result

        agent.tools.verify_task_state = disagreeing_verify
        result = agent.run(self.onboarding_id, ANCHOR_TODAY)
        self.assertIs(result.status, RunStatus.EXCEPTION)
        self.assertFalse(result.verified)
        self.assertEqual(result.actions_taken, [])

    # -- 7 ------------------------------------------------------------------
    def test_7_a_permitted_action_changes_state_and_is_verified(self) -> None:
        result = self.agent().run(self.onboarding_id, ANCHOR_TODAY)

        self.assertIs(result.status, RunStatus.ACTED)
        self.assertTrue(result.verified)
        self.assertEqual(len(result.actions_taken), 1)

        task = self.service.get(HR_OPS, self.onboarding_id).task_by_key(
            "hr_record_create"
        )
        self.assertIs(task.status, TaskStatus.IN_PROGRESS)

        # And it is durable.
        rebuilt = OnboardingService(
            build_registry(),
            store=OnboardingStore(
                InMemoryTableClient.from_snapshot(self.table.snapshot())
            ),
        )
        rebuilt.hydrate(CLIENT_ALPHA)
        self.assertIs(
            rebuilt.get(HR_OPS, self.onboarding_id)
            .task_by_key("hr_record_create")
            .status,
            TaskStatus.IN_PROGRESS,
        )

    def test_7b_the_run_row_records_the_fake_model(self) -> None:
        self.agent().run(self.onboarding_id, ANCHOR_TODAY)
        row = self.store.agent_runs(CLIENT_ALPHA)[0]
        self.assertEqual(row["advisor"], "fake_llm")
        self.assertEqual(row["model"], FAKE_MODEL)
        self.assertTrue(row["model"].startswith("fake/"))

    # -- 8 ------------------------------------------------------------------
    def test_8_the_workflow_runs_with_no_key_and_no_database(self) -> None:
        import os

        for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AZURE_API_KEY"):
            with self.subTest(key=key):
                self.assertIsNone(os.environ.get(key))

        plain = OnboardingService(build_registry())  # no store at all
        record = plain.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.assertIsNone(plain.store)

        result = UC02Agent(plain, HR_OPS, advisor=build_fake_advisor()).run(
            record.onboarding_id, ANCHOR_TODAY
        )
        self.assertIs(result.status, RunStatus.ACTED)
        self.assertTrue(result.verified)

    def test_8b_no_real_llm_call_is_ever_claimed(self) -> None:
        from onboarding_task_management.agent.harness import integration_status

        status = integration_status()
        self.assertFalse(status["llm_credential_configured"])
        self.assertFalse(status["real_llm_call_verified"])

        result = self.agent().run(self.onboarding_id, ANCHOR_TODAY)
        self.assertTrue(result.advice.source.startswith("fake_llm:"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
