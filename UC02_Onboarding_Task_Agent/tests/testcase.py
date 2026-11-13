"""Tests for the UC-02 sub-agent layer: structure, fakes, API, connector.

These cover the integration layer only. The onboarding business rules are
tested by the 638 tests under ``tests/``, and nothing here re-tests them —
but several cases assert that the sub-agent *cannot bypass* them, which is a
different and necessary claim.

No network, no credential, no database, no mail server.
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from datetime import date
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
_SRC = str(ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from onboarding_task_management.contracts import Role
from onboarding_task_management.errors import DataIsolationError
from onboarding_task_management.sub_agent.adapter import (
    UC02SubAgent,
    WorkflowError,
    WorkflowRequest,
    available_fixtures,
)
from onboarding_task_management.sub_agent.connectors.outlook import (
    DeliveryStatus,
    MockOutlookConnector,
    OutlookConnector,
    OutlookMessage,
)
from onboarding_task_management.sub_agent.fakes import (
    FAKE_BEHAVIOURS,
    FAKE_MODEL,
    fake_llm_fingerprint,
    make_fake_llm,
)

AS_OF = date(2026, 10, 26)
LATE = date(2026, 12, 31)
VALID = "01_valid_onboarding.json"


# ---------------------------------------------------------------------------
# Mandatory structure
# ---------------------------------------------------------------------------


class StructureTests(unittest.TestCase):
    """One agent package, importable, with nothing left behind."""

    REQUIRED: ClassVar[list[str]] = [
        # core agent — shipped
        "src/onboarding_task_management/agent/__init__.py",
        "src/onboarding_task_management/agent/advisor.py",
        "src/onboarding_task_management/agent/fake_llm.py",
        "src/onboarding_task_management/agent/harness.py",
        "src/onboarding_task_management/agent/loop.py",
        "src/onboarding_task_management/agent/tools.py",
        # sub-agent: the working process — checklist generation ...
        "src/onboarding_task_management/sub_agent/__init__.py",
        "src/onboarding_task_management/sub_agent/checklist.py",
        "src/onboarding_task_management/sub_agent/selection.py",
        "src/onboarding_task_management/sub_agent/planner.py",
        "src/onboarding_task_management/sub_agent/milestones.py",
        # ... progress and gates ...
        "src/onboarding_task_management/sub_agent/tracker.py",
        "src/onboarding_task_management/sub_agent/approvals.py",
        # ... message generation and sending ...
        "src/onboarding_task_management/sub_agent/reminders.py",
        "src/onboarding_task_management/sub_agent/connectors/outlook/__init__.py",
        "src/onboarding_task_management/sub_agent/connectors/outlook/base.py",
        "src/onboarding_task_management/sub_agent/connectors/outlook/mock.py",
        # ... and the entry points over them
        "src/onboarding_task_management/sub_agent/service.py",
        "src/onboarding_task_management/sub_agent/adapter.py",
        "src/onboarding_task_management/sub_agent/api.py",
        "src/onboarding_task_management/sub_agent/fakes.py",
        # verification harness — outside src/, not shipped
        "evaluation/run_evals.py",
        "context_layer/skills",
        "guardrails/policy.yaml",
        "tests/testcase.py",
        ".env.example",
        "pyproject.toml",
    ]

    def test_every_mandatory_path_exists(self) -> None:
        for rel in self.REQUIRED:
            with self.subTest(path=rel):
                self.assertTrue((ROOT / rel).exists(), f"missing {rel}")

    def test_the_skills_directory_has_content(self) -> None:
        skills = list((ROOT / "context_layer/skills").glob("*.md"))
        self.assertTrue(skills, "skills directory is empty")

    def test_there_is_exactly_one_agent_package(self) -> None:
        """The second tree at ``agent/`` was merged in; it must not return.

        Two agent packages meant two import paths to the same behaviour and a
        policy file that could drift. Re-creating the root ``agent/`` would
        silently reintroduce both.
        """
        self.assertFalse((ROOT / "agent").exists(),
                         "root agent/ is back; it was merged into the package")

    def test_the_package_resolves_past_the_legacy_shadow(self) -> None:
        """The 0-byte ``src/*.py`` scaffolds must not win an import."""
        import onboarding_task_management.agent as pkg

        self.assertIn("onboarding_task_management", pkg.__file__)
        self.assertTrue(pkg.__file__.replace("\\", "/").endswith("agent/__init__.py"),
                        f"agent package resolved to {pkg.__file__}")


class PolicyTests(unittest.TestCase):
    """There is one policy envelope, at the repository root."""

    def test_there_is_exactly_one_policy_file(self) -> None:
        found = sorted(
            str(p.relative_to(ROOT)).replace("\\", "/")
            for p in ROOT.rglob("policy.yaml")
            if ".git" not in p.parts and "__pycache__" not in p.parts
        )
        self.assertEqual(found, ["guardrails/policy.yaml"],
                         f"expected one policy envelope, found {found}")

    def test_the_policy_is_readable_and_non_empty(self) -> None:
        text = (ROOT / "guardrails/policy.yaml").read_text(encoding="utf-8")
        self.assertIn("gates", text)
        self.assertNotIn("GENERATED MIRROR", text,
                         "the authoritative policy must not be a generated copy")



class FakeLLMTests(unittest.TestCase):
    def test_no_api_key_is_required(self) -> None:
        import os

        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_API_KEY"):
            with self.subTest(key=key):
                self.assertIsNone(os.environ.get(key))
        self.assertIsNotNone(make_fake_llm())

    def test_fingerprint_states_it_is_fake(self) -> None:
        fp = fake_llm_fingerprint()
        self.assertEqual(fp["provider"], "fake")
        self.assertFalse(fp["real_api_calls"])
        self.assertFalse(fp["requires_api_key"])
        self.assertTrue(fp["model"].startswith("fake/"))

    def test_an_unknown_behaviour_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_fake_llm("definitely-not-a-behaviour")

    def test_every_declared_behaviour_constructs(self) -> None:
        for behaviour in FAKE_BEHAVIOURS:
            with self.subTest(behaviour=behaviour):
                self.assertIsNotNone(make_fake_llm(behaviour))

    def test_the_module_imports_no_llm_sdk(self) -> None:
        source = (ROOT / "src/onboarding_task_management/sub_agent/fakes.py").read_text(encoding="utf-8")
        lines = [ln for ln in source.splitlines() if ln.strip().startswith(("import ", "from "))]
        for banned in ("openai", "anthropic", "litellm", "httpx", "requests"):
            for line in lines:
                with self.subTest(banned=banned, line=line):
                    self.assertNotIn(banned, line)


# ---------------------------------------------------------------------------
# Mock Outlook
# ---------------------------------------------------------------------------


class MockOutlookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.outlook = MockOutlookConnector()
        self.message = OutlookMessage(
            idempotency_key="K1", onboarding_id="ONB-1", client_id="C1",
            recipient_role="hr_ops", subject_en="Reminder", approval_id="APR-1",
        )

    def test_it_satisfies_the_port(self) -> None:
        self.assertIsInstance(self.outlook, OutlookConnector)

    def test_delivery_without_approval_is_blocked(self) -> None:
        unapproved = dataclasses.replace(self.message, approval_id=None)
        receipt = self.outlook.send(unapproved)
        self.assertIs(receipt.status, DeliveryStatus.BLOCKED_NO_APPROVAL)
        self.assertEqual(self.outlook.sent_count(), 0)

    def test_an_approved_message_is_simulated_not_sent(self) -> None:
        receipt = self.outlook.send(self.message)
        self.assertIs(receipt.status, DeliveryStatus.SIMULATED_SENT)
        self.assertIn("simulated", receipt.detail)
        self.assertIsNotNone(receipt.message_id)
        assert receipt.message_id is not None  # narrow for the type checker
        self.assertTrue(receipt.message_id.startswith("MOCK-OUTLOOK"))

    def test_a_retry_does_not_duplicate(self) -> None:
        self.outlook.send(self.message)
        again = self.outlook.send(self.message)
        self.assertIs(again.status, DeliveryStatus.DUPLICATE_IGNORED)
        self.assertEqual(self.outlook.sent_count(), 1)

    def test_a_transient_failure_recovers_without_duplicating(self) -> None:
        outlook = MockOutlookConnector(fail_times={"K1": 2})
        statuses = [outlook.send(self.message).status for _ in range(3)]
        self.assertEqual(
            statuses,
            [DeliveryStatus.FAILED, DeliveryStatus.FAILED, DeliveryStatus.SIMULATED_SENT],
        )
        self.assertEqual(outlook.sent_count(), 1)

    def test_a_permanent_failure_never_reports_success(self) -> None:
        outlook = MockOutlookConnector(fail_keys={"K1"})
        for _ in range(3):
            self.assertIs(outlook.send(self.message).status, DeliveryStatus.FAILED)
        self.assertEqual(outlook.sent_count(), 0)

    def test_the_outbox_is_client_scoped(self) -> None:
        self.outlook.send(self.message)
        self.assertEqual(len(self.outlook.outbox("C1")), 1)
        self.assertEqual(self.outlook.outbox("OTHER-CLIENT"), [])

    def test_no_transport_library_is_imported(self) -> None:
        source = (ROOT / "src/onboarding_task_management/sub_agent/connectors/outlook/mock.py").read_text(encoding="utf-8")
        lines = [ln for ln in source.splitlines() if ln.strip().startswith(("import ", "from "))]
        for banned in ("smtplib", "httpx", "requests", "msal", "msgraph", "urllib", "socket"):
            for line in lines:
                with self.subTest(banned=banned):
                    self.assertNotIn(banned, line)
        self.assertNotIn("https://", source)


# ---------------------------------------------------------------------------
# Sub-agent adapter
# ---------------------------------------------------------------------------


class SubAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sub_agent = UC02SubAgent()

    def test_fixtures_are_discoverable(self) -> None:
        self.assertIn(VALID, available_fixtures())

    def test_a_valid_run_invokes_the_real_service_and_the_fake_llm(self) -> None:
        r = self.sub_agent.run(WorkflowRequest(fixture=VALID, as_of=AS_OF))
        self.assertTrue(r.checklist["total"] > 0)
        self.assertTrue(r.advice["source"].startswith("fake_llm:"))
        self.assertEqual(r.agent_run["model"], FAKE_MODEL)
        # Real service: the audit log recorded the creation.
        self.assertIn("onboarding.create", r.audit_actions)

    def test_the_action_is_verified_before_it_is_reported(self) -> None:
        r = self.sub_agent.run(WorkflowRequest(fixture=VALID, as_of=AS_OF))
        if r.agent_run["actions_taken"]:
            self.assertTrue(r.agent_run["verified"])

    def test_a_repeated_request_is_idempotent(self) -> None:
        first = self.sub_agent.run(WorkflowRequest(fixture=VALID, as_of=AS_OF))
        second = self.sub_agent.run(WorkflowRequest(fixture=VALID, as_of=AS_OF))
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.onboarding_id, second.onboarding_id)

    def test_either_trigger_or_fixture_is_required(self) -> None:
        with self.assertRaises(WorkflowError):
            self.sub_agent.run(WorkflowRequest())
        with self.assertRaises(WorkflowError):
            self.sub_agent.run(WorkflowRequest(trigger={}, fixture=VALID))

    def test_a_path_traversal_fixture_name_is_refused(self) -> None:
        for bad in ("../../guardrails/policy.yaml", "/etc/passwd", "..\\..\\CLAUDE.md"):
            with self.subTest(name=bad), self.assertRaises(WorkflowError):
                self.sub_agent.run(WorkflowRequest(fixture=bad))

    def test_client_isolation_still_holds_through_the_adapter(self) -> None:
        from onboarding_task_management.contracts import Actor

        r = self.sub_agent.run(WorkflowRequest(fixture=VALID, as_of=AS_OF))
        intruder = Actor(actor_id="X", role=Role.HR_OPS, client_id="CLIENT-BETA")
        with self.assertRaises(DataIsolationError):
            self.sub_agent.service.checklist(intruder, r.onboarding_id, AS_OF)

    def test_reminders_are_drafts_and_nothing_is_sent(self) -> None:
        r = self.sub_agent.run(
            WorkflowRequest(fixture=VALID, as_of=LATE, prepare_reminders=True)
        )
        self.assertFalse(r.reminders["real_email_sent"])
        self.assertEqual(r.reminders["draft_delivery_status"], ["draft_not_sent"])

    def test_reminder_delivery_needs_an_approval(self) -> None:
        without = self.sub_agent.run(
            WorkflowRequest(fixture=VALID, as_of=LATE, prepare_reminders=True)
        )
        self.assertEqual(without.reminders["simulated_accepted"], 0)

        fresh = UC02SubAgent()
        with_approval = fresh.run(
            WorkflowRequest(
                fixture=VALID, as_of=LATE, prepare_reminders=True,
                outlook_approval_id="APR-TEST",
            )
        )
        self.assertGreater(with_approval.reminders["simulated_accepted"], 0)
        self.assertFalse(with_approval.reminders["real_email_sent"])

    def test_a_hallucinated_action_never_executes(self) -> None:
        """The invented target must not be acted on.

        The run is not expected to do *nothing*: ``validate_advice`` discards
        the hallucination and substitutes the deterministic recommendation,
        which the agent may then safely carry out. What must never happen is
        the model's own invented target being executed.
        """
        from onboarding_task_management.agent.fake_llm import HALLUCINATED_TASK_ID

        r = self.sub_agent.run(
            WorkflowRequest(fixture=VALID, as_of=AS_OF, llm_behaviour="hallucinate")
        )
        self.assertTrue(r.agent_run["degraded"])
        self.assertIn("not in this onboarding", r.agent_run["degraded_reason"])
        for action in r.agent_run["actions_taken"]:
            with self.subTest(action=action):
                self.assertNotIn(HALLUCINATED_TASK_ID, action)
        # Anything that did run was a task the rules engine had marked ready.
        known = {t["task_id"] for t in r.checklist["tasks"]}
        for action in r.agent_run["actions_taken"]:
            self.assertTrue(any(k in action for k in known), action)

    def test_a_model_failure_is_reported_not_hidden(self) -> None:
        r = self.sub_agent.run(
            WorkflowRequest(fixture=VALID, as_of=AS_OF, llm_behaviour="error")
        )
        self.assertTrue(r.agent_run["degraded"])
        self.assertIsNone(r.agent_run["model"])


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class ApiTests(unittest.TestCase):
    #: Bound in setUpClass so the import failure becomes a skip, not a
    #: collection error, when fastapi is absent.
    TestClient: ClassVar[type]

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("fastapi is not installed") from None
        cls.TestClient = TestClient

    def setUp(self) -> None:
        from onboarding_task_management.sub_agent.api import app, reset_sub_agent

        reset_sub_agent()
        self.client = self.TestClient(app)

    def test_health_is_honest_about_what_is_wired(self) -> None:
        body = self.client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["llm"]["provider"], "fake")
        self.assertFalse(body["llm"]["real_api_calls"])
        self.assertFalse(body["outlook"]["real_email_sent"])
        self.assertFalse(body["database"]["configured"])

    def test_fixtures_endpoint_lists_synthetic_data(self) -> None:
        body = self.client.get("/v1/fixtures").json()
        self.assertGreater(body["count"], 0)
        self.assertIn(VALID, body["fixtures"])

    def test_a_valid_workflow_returns_200_with_real_results(self) -> None:
        r = self.client.post("/v1/onboarding/workflow", json={"fixture": VALID})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreater(body["checklist"]["total"], 0)
        self.assertTrue(body["advice"]["source"].startswith("fake_llm:"))
        self.assertIn("onboarding.create", body["audit_actions"])

    def test_repeated_posts_are_idempotent(self) -> None:
        body = {"fixture": VALID}
        first = self.client.post("/v1/onboarding/workflow", json=body).json()
        second = self.client.post("/v1/onboarding/workflow", json=body).json()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["onboarding_id"], second["onboarding_id"])

    def test_a_blocked_scenario_returns_422(self) -> None:
        r = self.client.post(
            "/v1/onboarding/workflow", json={"fixture": "02_offer_not_accepted.json"}
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["code"], "MISSING_DATA")

    def test_bad_requests_return_400(self) -> None:
        for body in ({}, {"fixture": "nope.json"},
                     {"fixture": VALID, "trigger": {}},
                     {"fixture": VALID, "llm_behaviour": "bogus"}):
            with self.subTest(body=body):
                r = self.client.post("/v1/onboarding/workflow", json=body)
                self.assertEqual(r.status_code, 400)

    def test_errors_never_leak_a_traceback(self) -> None:
        r = self.client.post("/v1/onboarding/workflow", json={"fixture": "nope.json"})
        text = r.text.lower()
        for leak in ("traceback", "file \"", ".py\", line", "f:\\"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, text)

    def test_the_api_does_not_reimplement_business_logic(self) -> None:
        """The API module must not contain planning or rule code."""
        source = (ROOT / "src/onboarding_task_management/sub_agent/api.py").read_text(encoding="utf-8")
        for forbidden in ("build_tasks", "topological_order", "add_business_days",
                          "TaskTemplate(", "applies_to("):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, source)


class EvaluationRunnerTests(unittest.TestCase):
    def test_the_runner_executes_real_scenarios(self) -> None:
        from evaluation.run_evals import SCENARIOS, run

        report = run()
        self.assertEqual(len(report.results), len(SCENARIOS))
        self.assertFalse(report.failed, [r for r in report.results if r.outcome == "FAIL"])
        # A skip must never be counted as a pass.
        self.assertEqual(
            report.count("PASS") + report.count("FAIL") + report.count("SKIP"),
            len(SCENARIOS),
        )

    def test_a_skip_is_not_a_pass(self) -> None:
        from evaluation.run_evals import Report, Result

        report = Report([Result("x", "SKIP", "unavailable")])
        self.assertEqual(report.count("PASS"), 0)
        self.assertFalse(report.failed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
