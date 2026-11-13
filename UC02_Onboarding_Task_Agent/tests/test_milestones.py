"""Milestone tracking (UC-02 Step-6 requirement).

Milestones are declared in client configuration and evaluated on demand. These
tests pin both halves: that the configuration is validated, and that the
derived state is correct and stays correct across a re-plan.
"""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.contracts import (
    DecisionOutcome,
    MilestoneStatus,
    Role,
    TaskCategory,
)
from onboarding_task_management.errors import DataIsolationError
from onboarding_task_management.sub_agent import milestones as ms
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    MilestoneTemplate,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import (
    ALPHA_CONFIG,
    CLIENT_ALPHA,
    LOCATION_REMOTE,
    build_registry,
    make_trigger,
    persona,
)
from tests.support import (
    FINANCE,
    HR_OPS,
    HR_OPS_APPROVER,
    HR_OPS_BETA,
    IT_MANAGER,
    ServiceTestCase,
)

AS_OF_EARLY = date(2026, 10, 16)
AS_OF_MID = date(2026, 11, 5)
AS_OF_LATE = date(2026, 11, 6)


def _task(key: str, **over) -> TaskTemplate:
    kwargs = dict(
        key=key,
        category=TaskCategory.HR_RECORD,
        title="T",
        title_ar="ت",
        owner=Role.HR_OPS,
        offset_days=-1,
    )
    kwargs.update(over)
    return TaskTemplate(**kwargs)


def _milestone(key: str, **over) -> MilestoneTemplate:
    kwargs = dict(
        key=key, title="M", title_ar="م", offset_days=-1, requires=("a",)
    )
    kwargs.update(over)
    return MilestoneTemplate(**kwargs)


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


class MilestoneConfigTests(unittest.TestCase):
    def test_milestone_requiring_an_unknown_task_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id="C",
                templates=(_task("a"),),
                milestones=(_milestone("m", requires=("nope",)),),
            )
        self.assertIn("unknown task template", str(ctx.exception))

    def test_duplicate_milestone_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id="C",
                templates=(_task("a"),),
                milestones=(_milestone("m"), _milestone("m")),
            )
        self.assertIn("duplicate milestone key", str(ctx.exception))

    def test_blank_titles_are_rejected(self) -> None:
        for field in ("title", "title_ar"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    _milestone("m", **{field: "  "})

    def test_empty_requires_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _milestone("m", requires=())
        self.assertIn("at least one task", str(ctx.exception))

    def test_duplicate_requires_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _milestone("m", requires=("a", "a"))

    def test_milestones_are_optional(self) -> None:
        config = ClientOnboardingConfig(approval_status="approved", client_id="C", templates=(_task("a"),))
        self.assertEqual(config.milestones, ())
        self.assertEqual(
            config.applicable_milestones(location="x", project="y", role_id="z"), ()
        )

    def test_shipped_config_milestones_are_bilingual_and_resolvable(self) -> None:
        keys = {t.key for t in ALPHA_CONFIG.templates}
        self.assertTrue(ALPHA_CONFIG.milestones)
        for milestone in ALPHA_CONFIG.milestones:
            with self.subTest(milestone=milestone.key):
                self.assertTrue(milestone.title.strip())
                self.assertTrue(
                    any("؀" <= c <= "ۿ" for c in milestone.title_ar)
                )
                self.assertTrue(set(milestone.requires) <= keys)


# ---------------------------------------------------------------------------
# Derived state
# ---------------------------------------------------------------------------


class MilestoneEvaluationTests(ServiceTestCase):
    def states(self, as_of: date):
        return {
            m.template_key: m
            for m in self.service.milestones(HR_OPS, self.onboarding_id, as_of)
        }

    def test_all_declared_milestones_are_reported_in_target_order(self) -> None:
        result = self.service.milestones(HR_OPS, self.onboarding_id, AS_OF_EARLY)
        self.assertEqual(
            [m.template_key for m in result],
            [
                "paperwork_complete",
                "workplace_ready",
                "day_one_ready",
                "onboarding_complete",
            ],
        )
        self.assertEqual(
            [m.target_date for m in result], sorted(m.target_date for m in result)
        )

    def test_target_dates_are_business_days_from_joining(self) -> None:
        states = self.states(AS_OF_EARLY)
        self.assertEqual(states["paperwork_complete"].target_date, date(2026, 11, 5))
        self.assertEqual(states["workplace_ready"].target_date, date(2026, 11, 12))
        self.assertEqual(states["day_one_ready"].target_date, date(2026, 11, 13))
        self.assertEqual(states["onboarding_complete"].target_date, date(2026, 11, 18))

    def test_milestone_ids_are_deterministic(self) -> None:
        first = self.states(AS_OF_EARLY)["paperwork_complete"].milestone_id
        second = self.states(AS_OF_EARLY)["paperwork_complete"].milestone_id
        self.assertEqual(first, second)
        self.assertEqual(first, f"{self.onboarding_id}@paperwork_complete")

    def test_pending_when_nothing_is_due_or_overdue(self) -> None:
        for state in self.states(AS_OF_EARLY).values():
            with self.subTest(milestone=state.template_key):
                self.assertEqual(state.status, MilestoneStatus.PENDING)
                self.assertEqual(state.reasons, [])

    def test_at_risk_when_a_required_task_is_overdue(self) -> None:
        # hr_record_create is due 2026-11-02 and still open on 2026-11-05.
        state = self.states(AS_OF_MID)["paperwork_complete"]
        self.assertEqual(state.status, MilestoneStatus.AT_RISK)
        self.assertIn(self.task_id("hr_record_create"), state.overdue_task_ids)
        self.assertIn("overdue", " ".join(state.reasons))

    def test_missed_once_the_target_date_has_passed(self) -> None:
        state = self.states(AS_OF_LATE)["paperwork_complete"]
        self.assertEqual(state.status, MilestoneStatus.MISSED)
        self.assertIn("passed", " ".join(state.reasons))

    def test_achieved_when_every_required_task_is_complete(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(HR_OPS, "collect_documents")
        state = self.states(AS_OF_MID)["paperwork_complete"]
        self.assertEqual(state.status, MilestoneStatus.ACHIEVED)
        self.assertEqual(state.outstanding_task_ids, [])
        self.assertEqual(len(state.completed_task_ids), 2)

    def test_achieved_stays_achieved_after_the_target_date(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(HR_OPS, "collect_documents")
        self.assertEqual(
            self.states(AS_OF_LATE)["paperwork_complete"].status,
            MilestoneStatus.ACHIEVED,
        )

    def test_partial_completion_is_not_achieved(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        state = self.states(AS_OF_EARLY)["paperwork_complete"]
        self.assertEqual(state.status, MilestoneStatus.PENDING)
        self.assertEqual(state.completed_task_ids, [self.task_id("hr_record_create")])
        self.assertEqual(
            state.outstanding_task_ids, [self.task_id("collect_documents")]
        )

    def test_required_and_outstanding_sets_are_consistent(self) -> None:
        for state in self.states(AS_OF_MID).values():
            with self.subTest(milestone=state.template_key):
                self.assertEqual(
                    sorted(state.completed_task_ids + state.outstanding_task_ids),
                    sorted(state.required_task_ids),
                )

    def test_days_to_target_is_business_days_and_signed(self) -> None:
        states = self.states(AS_OF_MID)
        self.assertEqual(states["paperwork_complete"].days_to_target, 0)
        self.assertGreater(states["onboarding_complete"].days_to_target, 0)
        self.assertLess(
            self.states(AS_OF_LATE)["paperwork_complete"].days_to_target, 0
        )

    def test_day_one_ready_spans_several_owners(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(HR_OPS, "collect_documents")
        self.complete(IT_MANAGER, "it_laptop")
        self.complete(FINANCE, "payroll_setup")
        self.assertEqual(
            self.states(AS_OF_MID)["day_one_ready"].status, MilestoneStatus.PENDING
        )
        self.complete(IT_MANAGER, "it_accounts")
        self.assertEqual(
            self.states(AS_OF_MID)["day_one_ready"].status, MilestoneStatus.ACHIEVED
        )

    def test_applicability_filters_milestones_by_location(self) -> None:
        remote = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-003"))
        )
        keys = {
            m.template_key
            for m in self.service.milestones(
                HR_OPS, remote.onboarding_id, AS_OF_EARLY
            )
        }
        self.assertNotIn("workplace_ready", keys)
        self.assertIn("day_one_ready", keys)

    def test_cancelled_required_task_blocks_the_milestone(self) -> None:
        request = self.service.request_cancellation(HR_OPS, self.onboarding_id, "x")
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_cancellation(HR_OPS, request.request_id)

        state = self.states(AS_OF_EARLY)["paperwork_complete"]
        self.assertEqual(state.status, MilestoneStatus.AT_RISK)
        self.assertIn("cancelled", " ".join(state.reasons))
        self.assertEqual(len(state.blocking_task_ids), 2)

    def test_milestones_are_not_stored_on_the_record(self) -> None:
        self.assertFalse(hasattr(self.record, "milestones"))


# ---------------------------------------------------------------------------
# Re-planning
# ---------------------------------------------------------------------------


class MilestoneReplanTests(ServiceTestCase):
    def test_date_change_moves_milestone_targets(self) -> None:
        before = self.service.milestones(HR_OPS, self.onboarding_id, AS_OF_EARLY)[0]
        self.assertEqual(before.target_date, date(2026, 11, 5))

        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_change(HR_OPS, request.request_id)

        after = self.service.milestones(HR_OPS, self.onboarding_id, AS_OF_EARLY)[0]
        self.assertEqual(after.template_key, "paperwork_complete")
        self.assertEqual(after.target_date, date(2026, 11, 19))

    def test_location_change_drops_the_location_specific_milestone(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, location=LOCATION_REMOTE
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_change(HR_OPS, request.request_id)

        keys = {
            m.template_key
            for m in self.service.milestones(HR_OPS, self.onboarding_id, AS_OF_EARLY)
        }
        self.assertNotIn("workplace_ready", keys)

    def test_achieved_milestone_survives_a_date_change(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(HR_OPS, "collect_documents")
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_change(HR_OPS, request.request_id)

        state = next(
            m
            for m in self.service.milestones(HR_OPS, self.onboarding_id, AS_OF_EARLY)
            if m.template_key == "paperwork_complete"
        )
        self.assertEqual(state.status, MilestoneStatus.ACHIEVED)

    def test_required_task_removed_by_replan_blocks_the_milestone(self) -> None:
        # A milestone that still applies, but whose required task no longer
        # does, must block rather than silently report readiness.
        config = ClientOnboardingConfig(
            approval_status="approved",  # test fixture
            client_id="CLIENT-EDGE",
            templates=(
                _task("always"),
                _task("dubai_only", locations=("Dubai",)),
            ),
            milestones=(
                _milestone("edge", requires=("always", "dubai_only")),
            ),
        )
        registry = build_registry()
        registry.register(config)
        service = OnboardingService(registry)
        from onboarding_task_management.contracts import Actor

        actor = Actor(actor_id="U-E", role=Role.HR_OPS, client_id="CLIENT-EDGE")
        trigger = make_trigger(persona("EMP-001")).model_copy(
            update={"client_id": "CLIENT-EDGE", "location": "Dubai"}
        )
        record = service.create_onboarding(actor, trigger)
        self.assertEqual(len(record.tasks), 2)

        request = service.request_change(
            actor, record.onboarding_id, location=LOCATION_REMOTE
        )
        approver = Actor(actor_id="U-E2", role=Role.HR_OPS, client_id="CLIENT-EDGE")
        service.decide_change(approver, request.request_id, DecisionOutcome.APPROVED)
        service.apply_change(actor, request.request_id)

        state = service.milestones(actor, record.onboarding_id, AS_OF_EARLY)[0]
        self.assertEqual(state.status, MilestoneStatus.AT_RISK)
        self.assertIn("not in the plan", " ".join(state.reasons))


# ---------------------------------------------------------------------------
# Service integration: isolation and audit
# ---------------------------------------------------------------------------


class MilestoneServiceTests(ServiceTestCase):
    def test_another_client_cannot_read_milestones(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.milestones(HR_OPS_BETA, self.onboarding_id, AS_OF_EARLY)

    def test_milestone_read_is_audited(self) -> None:
        self.service.milestones(HR_OPS, self.onboarding_id, AS_OF_EARLY)
        reads = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.action == "onboarding.read"
        ]
        self.assertEqual(reads[-1].detail["operation"], "milestones")

    def test_evaluation_is_deterministic(self) -> None:
        first = self.service.milestones(HR_OPS, self.onboarding_id, AS_OF_MID)
        second = self.service.milestones(HR_OPS, self.onboarding_id, AS_OF_MID)
        self.assertEqual(first, second)

    def test_states_carry_no_personal_data(self) -> None:
        for state in self.service.milestones(HR_OPS, self.onboarding_id, AS_OF_MID):
            with self.subTest(milestone=state.template_key):
                fields = set(type(state).model_fields)
                self.assertFalse(
                    fields & {"name", "email", "nationality", "phone", "address"}
                )
                self.assertEqual(state.client_id, CLIENT_ALPHA)

    def test_evaluate_all_is_pure_and_matches_the_service(self) -> None:
        record = self.service.get(HR_OPS, self.onboarding_id)
        config = self.service.registry.get(CLIENT_ALPHA)
        direct = ms.evaluate_all(record, config, AS_OF_MID)
        self.assertEqual(
            direct, self.service.milestones(HR_OPS, self.onboarding_id, AS_OF_MID)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
