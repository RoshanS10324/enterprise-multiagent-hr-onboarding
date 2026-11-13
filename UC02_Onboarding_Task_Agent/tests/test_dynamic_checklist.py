"""Dynamic checklist generation: selection rules, explanations and scenarios.

Unit tests cover one selection rule each; the scenario tests (A-G) cover the
end-to-end workflow through :class:`OnboardingService`. Synthetic data only —
every context is built from :mod:`onboarding_task_management.synthetic`.
"""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.contracts import (
    ChecklistContext,
    ChecklistIssueSeverity,
    DecisionOutcome,
    Role,
    SelectionDimension,
    SelectionOutcome,
    TaskCategory,
    TaskStatus,
)
from onboarding_task_management.errors import (
    DataIsolationError,
    MissingConfigurationError,
    UnauthorizedActionError,
)
from onboarding_task_management.sub_agent import selection
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    ConfigRegistry,
    MilestoneTemplate,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import (
    ALPHA_CONFIG,
    ANCHOR_TODAY,
    ATTR_BV_REQUIRED,
    ATTR_FAMILY_INSURANCE,
    ATTR_INSURANCE_REQUIRED,
    ATTR_NEEDS_TRAVEL,
    ATTR_VISA_REQUIRED,
    CLIENT_ALPHA,
    DEFAULT_JOINING_DATE,
    GRADE_L2,
    GRADE_L4,
    LOCATION_ABU_DHABI,
    LOCATION_DUBAI,
    LOCATION_REMOTE,
    PROJECT_APOLLO,
    PROJECT_ZEPHYR,
    build_registry,
    make_trigger,
    persona,
)
from tests.support import (
    EMPLOYEE,
    HR_OPS,
    HR_OPS_APPROVER,
    HR_OPS_BETA,
    IT_MANAGER,
    ServiceTestCase,
)


def context(**overrides) -> ChecklistContext:
    """A valid CLIENT-ALPHA context; override one field per test."""
    base = dict(
        client_id=CLIENT_ALPHA,
        employee_id="EMP-SYN",
        joining_date=DEFAULT_JOINING_DATE,
        location=LOCATION_DUBAI,
        project=PROJECT_APOLLO,
        role_id="backend_engineer",
        grade=GRADE_L2,
        nationality_group="",
        attributes=[],
    )
    base.update(overrides)
    return ChecklistContext(**base)


def keys(decisions) -> set[str]:
    return {d.template_key for d in decisions}


# ---------------------------------------------------------------------------
# Unit: one selection rule at a time
# ---------------------------------------------------------------------------


class SelectionRuleTests(unittest.TestCase):
    """Each applicability dimension, included and excluded."""

    def template(self, **overrides) -> TaskTemplate:
        base = dict(
            key="t",
            category=TaskCategory.HR_RECORD,
            title="Task",
            title_ar="مهمة",
            owner=Role.HR_OPS,
            offset_days=-1,
        )
        base.update(overrides)
        return TaskTemplate(**base)

    def test_unfiltered_template_applies_to_every_context(self) -> None:
        decision = selection.evaluate_template(self.template(), context())
        self.assertIs(decision.outcome, SelectionOutcome.INCLUDED)
        self.assertEqual(decision.reason_code, "applies_to_all")

    def test_location_filter_includes_a_matching_location(self) -> None:
        decision = selection.evaluate_template(
            self.template(locations=(LOCATION_DUBAI,)), context()
        )
        self.assertIs(decision.outcome, SelectionOutcome.INCLUDED)
        self.assertEqual(decision.reason_code, "matched_all_filters")
        self.assertIn("Dubai", decision.reason)

    def test_location_filter_excludes_a_non_matching_location(self) -> None:
        decision = selection.evaluate_template(
            self.template(locations=(LOCATION_REMOTE,)), context()
        )
        self.assertIs(decision.outcome, SelectionOutcome.EXCLUDED)
        self.assertEqual(decision.reason_code, "excluded_by_location")
        self.assertIn("Remote-IN", decision.reason)

    def test_project_filter_excludes_a_non_matching_project(self) -> None:
        decision = selection.evaluate_template(
            self.template(projects=(PROJECT_ZEPHYR,)), context()
        )
        self.assertEqual(decision.reason_code, "excluded_by_project")

    def test_role_filter_excludes_a_non_matching_role(self) -> None:
        decision = selection.evaluate_template(
            self.template(role_ids=("qa_engineer",)), context()
        )
        self.assertEqual(decision.reason_code, "excluded_by_role_id")

    def test_grade_filter_excludes_a_non_matching_grade(self) -> None:
        decision = selection.evaluate_template(
            self.template(grades=(GRADE_L4,)), context(grade=GRADE_L2)
        )
        self.assertEqual(decision.reason_code, "excluded_by_grade")

    def test_nationality_group_is_unfiltered_by_default(self) -> None:
        """No client declares a group, so the dimension never excludes."""
        decision = selection.evaluate_template(self.template(), context())
        verdict = next(
            v
            for v in decision.verdicts
            if v.dimension is SelectionDimension.NATIONALITY_GROUP
        )
        self.assertTrue(verdict.unrestricted)
        self.assertTrue(verdict.matched)

    def test_nationality_group_filters_when_a_group_is_declared(self) -> None:
        template = self.template(nationality_groups=("grp-a",))
        self.assertIs(
            selection.evaluate_template(template, context(nationality_group="grp-a")).outcome,
            SelectionOutcome.INCLUDED,
        )
        self.assertIs(
            selection.evaluate_template(template, context(nationality_group="grp-b")).outcome,
            SelectionOutcome.EXCLUDED,
        )

    def test_attribute_filter_names_the_missing_flag(self) -> None:
        decision = selection.evaluate_template(
            self.template(requires_attributes=(ATTR_VISA_REQUIRED, ATTR_BV_REQUIRED)),
            context(attributes=[ATTR_BV_REQUIRED]),
        )
        self.assertEqual(decision.reason_code, "excluded_by_attributes")
        self.assertIn(ATTR_VISA_REQUIRED, decision.reason)
        self.assertNotIn("excluded: requires attributes (bv_required", decision.reason)

    def test_attribute_filter_requires_every_declared_flag(self) -> None:
        decision = selection.evaluate_template(
            self.template(requires_attributes=(ATTR_NEEDS_TRAVEL,)),
            context(attributes=[ATTR_NEEDS_TRAVEL]),
        )
        self.assertIs(decision.outcome, SelectionOutcome.INCLUDED)

    def test_every_failing_dimension_is_named(self) -> None:
        decision = selection.evaluate_template(
            self.template(locations=(LOCATION_REMOTE,), projects=(PROJECT_ZEPHYR,)),
            context(),
        )
        self.assertIn("location", decision.reason)
        self.assertIn("project", decision.reason)

    def test_decision_agrees_with_applies_to_on_every_combination(self) -> None:
        """``applies_to`` and the explanation must never disagree.

        Both reduce the same ``dimension_verdicts``; this pins that down
        across the real configuration rather than trusting the refactor.
        """
        contexts = [
            context(),
            context(location=LOCATION_REMOTE),
            context(location=LOCATION_ABU_DHABI, project=PROJECT_ZEPHYR),
            context(grade=GRADE_L4, attributes=[ATTR_VISA_REQUIRED]),
            context(nationality_group="grp-a", attributes=[ATTR_NEEDS_TRAVEL]),
        ]
        for ctx in contexts:
            for template in ALPHA_CONFIG.templates:
                with self.subTest(template=template.key, location=ctx.location):
                    expected = template.applies_to(
                        location=ctx.location,
                        project=ctx.project,
                        role_id=ctx.role_id,
                        grade=ctx.grade,
                        nationality_group=ctx.nationality_group,
                        attributes=ctx.attributes,
                    )
                    decision = selection.evaluate_template(template, ctx)
                    self.assertEqual(
                        expected, decision.outcome is SelectionOutcome.INCLUDED
                    )

    def test_selection_is_deterministic(self) -> None:
        first = selection.preview(ALPHA_CONFIG, context())
        second = selection.preview(ALPHA_CONFIG, context())
        self.assertEqual(first.model_dump(), second.model_dump())


# ---------------------------------------------------------------------------
# Scenario A — local hire
# ---------------------------------------------------------------------------


class ScenarioALocalHireTests(ServiceTestCase):
    employee_id = "EMP-008"

    def test_common_tasks_are_generated(self) -> None:
        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        included = {e.template_key for e in view.entries}
        self.assertIn("hr_record_create", included)
        self.assertIn("collect_documents", included)
        self.assertIn("it_laptop", included)
        self.assertIn("payroll_setup", included)
        self.assertIn("orientation_schedule", included)
        self.assertIn("policy_ack", included)

    def test_no_relocation_or_visa_tasks_without_the_approved_flags(self) -> None:
        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        included = {e.template_key for e in view.entries}
        for key in (
            "travel_booking",
            "accommodation_booking",
            "visa_pack_handover",
            "visa_processing",
            "medical_test",
            "biometrics_capture",
        ):
            self.assertNotIn(key, included)

    def test_each_excluded_task_states_why(self) -> None:
        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        excluded = {d.template_key: d for d in view.excluded}
        self.assertEqual(
            excluded["travel_booking"].reason_code, "excluded_by_attributes"
        )
        self.assertIn("needs_travel", excluded["travel_booking"].reason)

    def test_every_included_task_states_why(self) -> None:
        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        for entry in view.entries:
            with self.subTest(task=entry.template_key):
                self.assertTrue(entry.decision.reason)
                self.assertIs(entry.decision.outcome, SelectionOutcome.INCLUDED)


# ---------------------------------------------------------------------------
# Scenario B — relocation hire
# ---------------------------------------------------------------------------


class ScenarioBRelocationHireTests(ServiceTestCase):
    employee_id = "EMP-007"

    def test_relocation_and_work_permit_tasks_are_added(self) -> None:
        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        included = {e.template_key for e in view.entries}
        for key in (
            "travel_booking",
            "accommodation_booking",
            "visa_pack_handover",
            "visa_processing",
            "medical_test",
            "biometrics_capture",
            "background_verification",
        ):
            self.assertIn(key, included)

    def test_common_tasks_are_still_present(self) -> None:
        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        included = {e.template_key for e in view.entries}
        self.assertIn("hr_record_create", included)
        self.assertIn("collect_documents", included)

    def test_document_verification_precedes_visa_pack_handover(self) -> None:
        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        handover = next(e for e in view.entries if e.template_key == "visa_pack_handover")
        self.assertIn("collect_documents", handover.blocked_by_keys)
        self.assertIs(handover.status, TaskStatus.BLOCKED)

    def test_relocation_hire_gets_strictly_more_tasks_than_a_local_hire(self) -> None:
        local = OnboardingService(build_registry())
        local_record = local.create_onboarding(HR_OPS, make_trigger(persona("EMP-008")))
        local_view = local.checklist(HR_OPS, local_record.onboarding_id, ANCHOR_TODAY)
        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)

        local_keys = {e.template_key for e in local_view.entries}
        relocation_keys = {e.template_key for e in view.entries}
        self.assertTrue(local_keys < relocation_keys)


# ---------------------------------------------------------------------------
# Scenario C — different role or grade
# ---------------------------------------------------------------------------


GRADED_CONFIG = ClientOnboardingConfig(
    approval_status="approved",  # test fixture
    client_id=CLIENT_ALPHA,
    templates=(
        TaskTemplate(
            key="hr_record_create",
            category=TaskCategory.HR_RECORD,
            title="Create HRIS employee record",
            title_ar="إنشاء سجل الموظف",
            owner=Role.HR_OPS,
            offset_days=-5,
        ),
        TaskTemplate(
            key="senior_laptop",
            category=TaskCategory.IT_PROVISIONING,
            title="Provision senior-grade workstation",
            title_ar="تجهيز محطة عمل للدرجة العليا",
            owner=Role.IT_MANAGER,
            offset_days=-3,
            depends_on=("hr_record_create",),
            grades=(GRADE_L4,),
        ),
        TaskTemplate(
            key="prod_access",
            category=TaskCategory.ACCESS_MANAGEMENT,
            title="Grant production system access",
            title_ar="منح صلاحيات الوصول إلى بيئة الإنتاج",
            owner=Role.IT_MANAGER,
            offset_days=-1,
            depends_on=("hr_record_create",),
            role_ids=("devops_engineer",),
            evidence_required=True,
            approval_role=Role.HR_MANAGER,
        ),
    ),
)


class ScenarioCRoleAndGradeTests(unittest.TestCase):
    def preview(self, **overrides):
        return selection.preview(GRADED_CONFIG, context(**overrides))

    def test_grade_specific_equipment_only_for_the_configured_grade(self) -> None:
        self.assertIn("senior_laptop", keys(self.preview(grade=GRADE_L4).included))
        self.assertNotIn("senior_laptop", keys(self.preview(grade=GRADE_L2).included))

    def test_role_specific_access_only_for_the_configured_role(self) -> None:
        devops = self.preview(role_id="devops_engineer")
        other = self.preview(role_id="backend_engineer")
        self.assertIn("prod_access", keys(devops.included))
        self.assertNotIn("prod_access", keys(other.included))

    def test_a_role_match_plans_the_task_but_does_not_grant_access(self) -> None:
        """Supplying a role creates the task; it does not perform the grant.

        The task still carries its approver and its evidence requirement, and
        only its configured owner may act on it.
        """
        decision = next(
            d
            for d in self.preview(role_id="devops_engineer").included
            if d.template_key == "prod_access"
        )
        self.assertIs(decision.outcome, SelectionOutcome.INCLUDED)
        template = next(t for t in GRADED_CONFIG.templates if t.key == "prod_access")
        self.assertIs(template.approval_role, Role.HR_MANAGER)
        self.assertTrue(template.evidence_required)


class ScenarioCAuthorizationTests(ServiceTestCase):
    """A task is still owner-gated and approval-bearing in the plan.

    EMP-007 is used because the approver mapping (Business Framework §8) only
    names approvers for the extended activities; a local hire's common tasks
    carry none, which is the configuration, not an omission here.
    """

    employee_id = "EMP-007"

    def test_access_task_is_marked_awaiting_approval_until_settled(self) -> None:
        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        with_approver = [
            e for e in view.entries if e.approval_role is not None
        ]
        self.assertTrue(with_approver)
        for entry in with_approver:
            self.assertIn(entry.task_id, view.awaiting_approval_task_ids)

    def test_non_owner_may_not_act_on_a_task_it_does_not_own(self) -> None:
        with self.assertRaises(UnauthorizedActionError):
            self.service.start_task(
                EMPLOYEE, self.onboarding_id, self.task_id("hr_record_create")
            )


# ---------------------------------------------------------------------------
# Scenario D — missing documents
# ---------------------------------------------------------------------------


class ScenarioDMissingDocumentsTests(ServiceTestCase):
    employee_id = "EMP-007"

    def view(self):
        return self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)

    def entry(self, template_key):
        return next(e for e in self.view().entries if e.template_key == template_key)

    def test_dependent_tasks_are_blocked_while_documents_are_outstanding(self) -> None:
        handover = self.entry("visa_pack_handover")
        self.assertIs(handover.status, TaskStatus.BLOCKED)
        self.assertIn(handover.task_id, self.view().blocked_task_ids)

    def test_the_missing_prerequisite_is_named(self) -> None:
        handover = self.entry("visa_pack_handover")
        self.assertEqual(handover.blocked_by_keys, ["collect_documents"])
        self.assertEqual(handover.blocked_by, [self.task_id("collect_documents")])

    def test_dependent_becomes_ready_only_after_the_prerequisite_completes(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.assertIs(self.entry("visa_pack_handover").status, TaskStatus.BLOCKED)

        self.complete(HR_OPS, "collect_documents")
        handover = self.entry("visa_pack_handover")
        self.assertIs(handover.status, TaskStatus.READY)
        self.assertEqual(handover.blocked_by, [])
        self.assertIn(handover.task_id, self.view().ready_task_ids)

    def test_an_evidence_bearing_task_reports_whether_evidence_is_recorded(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(HR_OPS, "collect_documents")
        self.assertFalse(self.entry("visa_pack_handover").evidence_recorded)

        self.service.complete_task(
            HR_OPS,
            self.onboarding_id,
            self.task_id("visa_pack_handover"),
            evidence_ref="DOC-REF-001",
        )
        handover = self.entry("visa_pack_handover")
        self.assertTrue(handover.evidence_recorded)
        self.assertTrue(handover.evidence_required)


# ---------------------------------------------------------------------------
# Scenario E — joining date changed
# ---------------------------------------------------------------------------


class ScenarioEJoiningDateChangeTests(ServiceTestCase):
    employee_id = "EMP-001"
    NEW_JOINING = date(2026, 11, 30)

    def view(self):
        return self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)

    def change_date(self):
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=self.NEW_JOINING
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        return self.service.apply_change(HR_OPS, request.request_id)

    def test_a_date_change_requires_approval_before_it_is_applied(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=self.NEW_JOINING
        )
        view = self.view()
        self.assertIn(request.request_id, view.pending_gate_request_ids)
        self.assertEqual(view.joining_date, DEFAULT_JOINING_DATE)

    def test_open_due_dates_are_recalculated_on_the_approved_calendar(self) -> None:
        before = {e.template_key: e.due_date for e in self.view().entries}
        self.change_date()
        after = {e.template_key: e.due_date for e in self.view().entries}
        self.assertNotEqual(before["it_laptop"], after["it_laptop"])
        # Ten business days before Mon 2026-11-30 is Mon 2026-11-16.
        self.assertEqual(after["hr_record_create"], date(2026, 11, 16))

    def test_completed_work_is_preserved_and_not_rescheduled(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        completed_due = next(
            e for e in self.view().entries if e.template_key == "hr_record_create"
        ).due_date

        self.change_date()

        entry = next(
            e for e in self.view().entries if e.template_key == "hr_record_create"
        )
        self.assertIs(entry.status, TaskStatus.COMPLETE)
        self.assertEqual(entry.due_date, completed_due)
        self.assertIn(entry.task_id, self.view().complete_task_ids)

    def test_the_change_is_recorded_and_the_revision_advances(self) -> None:
        self.assertEqual(self.view().revision, 0)
        self.change_date()
        self.assertEqual(self.view().revision, 1)
        actions = self.service.audit.actions_for(HR_OPS)
        self.assertIn("change.request", actions)
        self.assertIn("change.apply", actions)

    def test_audit_history_survives_the_replan(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        before = len(self.service.audit.events_for(HR_OPS))
        self.change_date()
        after = self.service.audit.events_for(HR_OPS)
        self.assertGreater(len(after), before)
        self.assertIn("task.complete", [e.action for e in after])


class ScenarioELocationChangeTests(ServiceTestCase):
    """A location change re-selects the checklist, not just the dates."""

    employee_id = "EMP-001"

    def test_moving_to_remote_swaps_location_specific_tasks(self) -> None:
        before = {
            e.template_key
            for e in self.service.checklist(
                HR_OPS, self.onboarding_id, ANCHOR_TODAY
            ).entries
        }
        self.assertIn("desk_allocation", before)
        self.assertNotIn("remote_kit_shipping", before)

        request = self.service.request_change(
            HR_OPS, self.onboarding_id, location=LOCATION_REMOTE
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_change(HR_OPS, request.request_id)

        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        after = {e.template_key for e in view.entries}
        self.assertNotIn("desk_allocation", after)
        self.assertIn("remote_kit_shipping", after)
        self.assertIn(
            "desk_allocation", {d.template_key for d in view.excluded}
        )

    def test_a_completed_task_that_no_longer_applies_is_retained_and_explained(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(HR_OPS, "desk_allocation")

        request = self.service.request_change(
            HR_OPS, self.onboarding_id, location=LOCATION_REMOTE
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_change(HR_OPS, request.request_id)

        view = self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)
        retained = next(e for e in view.entries if e.template_key == "desk_allocation")
        self.assertIs(retained.status, TaskStatus.COMPLETE)
        self.assertEqual(
            retained.decision.reason_code, "retained_from_earlier_revision"
        )
        self.assertNotIn(
            "desk_allocation", {d.template_key for d in view.excluded}
        )


# ---------------------------------------------------------------------------
# Scenario F — client-specific requirements
# ---------------------------------------------------------------------------


class ScenarioFClientSpecificTests(unittest.TestCase):
    """Client-specific tasks live in the client's own configuration.

    There is no shared "common onboarding" template set in code that a client
    can accidentally edit: each client's configuration is a separate
    :class:`ClientOnboardingConfig`, looked up by ``client_id``.
    """

    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())

    def test_beta_gets_only_the_tasks_beta_configured(self) -> None:
        preview = self.service.preview_checklist(
            HR_OPS_BETA, context(client_id="CLIENT-BETA")
        )
        self.assertEqual(keys(preview.included), {"hr_record_create", "it_accounts"})

    def test_alpha_specific_tasks_never_leak_into_beta(self) -> None:
        preview = self.service.preview_checklist(
            HR_OPS_BETA, context(client_id="CLIENT-BETA")
        )
        everything = keys(preview.included) | keys(preview.excluded)
        self.assertNotIn("travel_booking", everything)
        self.assertNotIn("desk_allocation", everything)

    def test_an_actor_may_not_preview_another_clients_checklist(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.preview_checklist(HR_OPS, context(client_id="CLIENT-BETA"))

    def test_only_hr_roles_may_preview(self) -> None:
        with self.assertRaises(UnauthorizedActionError):
            self.service.preview_checklist(IT_MANAGER, context())

    def test_an_extra_client_requirement_is_additive_only(self) -> None:
        """Adding a client template changes that client's plan and no other."""
        extra = TaskTemplate(
            key="client_security_briefing",
            category=TaskCategory.ORIENTATION,
            title="Attend client security briefing",
            title_ar="حضور الإحاطة الأمنية الخاصة بالعميل",
            owner=Role.HR_OPS,
            offset_days=-1,
        )
        extended = ALPHA_CONFIG.model_copy(
            update={"templates": ALPHA_CONFIG.templates + (extra,)}
        )
        baseline = keys(selection.preview(ALPHA_CONFIG, context()).included)
        widened = keys(selection.preview(extended, context()).included)
        self.assertEqual(widened - baseline, {"client_security_briefing"})


# ---------------------------------------------------------------------------
# Scenario G — missing or conflicting configuration
# ---------------------------------------------------------------------------


class ScenarioGMissingConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())

    def test_an_unregistered_client_raises_rather_than_guessing(self) -> None:
        with self.assertRaises(MissingConfigurationError):
            self.service.preview_checklist(
                HR_OPS.model_copy(update={"client_id": "CLIENT-UNKNOWN"}),
                context(client_id="CLIENT-UNKNOWN"),
            )

    def test_a_configured_client_with_no_templates_raises(self) -> None:
        empty_actor = HR_OPS.model_copy(update={"client_id": "CLIENT-EMPTY"})
        with self.assertRaises(MissingConfigurationError):
            self.service.preview_checklist(
                empty_actor, context(client_id="CLIENT-EMPTY")
            )

    def test_a_context_matching_no_template_is_blocking_not_empty(self) -> None:
        narrow = ClientOnboardingConfig(
            approval_status="approved",  # test fixture
            client_id=CLIENT_ALPHA,
            templates=(
                TaskTemplate(
                    key="only_remote",
                    category=TaskCategory.WORKPLACE,
                    title="Ship remote kit",
                    title_ar="شحن معدات العمل عن بعد",
                    owner=Role.FACILITIES,
                    offset_days=-2,
                    locations=(LOCATION_REMOTE,),
                ),
            ),
        )
        preview = selection.preview(narrow, context(location=LOCATION_DUBAI))
        self.assertEqual(preview.included, [])
        self.assertTrue(preview.review_required)
        codes = {i.code: i.severity for i in preview.issues}
        self.assertIs(
            codes[selection.ISSUE_NO_APPLICABLE_TASKS],
            ChecklistIssueSeverity.BLOCKING,
        )

    def test_planning_such_a_context_raises_rather_than_producing_an_empty_plan(self) -> None:
        """The preview explains it; creating the onboarding still refuses.

        CLIENT-ALPHA's common tasks are unfiltered, so a context has to be
        tested against a configuration whose every template is filtered —
        otherwise "matches nothing" is unreachable by construction.
        """
        registry = ConfigRegistry()
        registry.register(
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id=CLIENT_ALPHA,
                templates=(
                    TaskTemplate(
                        key="only_remote",
                        category=TaskCategory.WORKPLACE,
                        title="Ship remote kit",
                        title_ar="شحن معدات العمل عن بعد",
                        owner=Role.FACILITIES,
                        offset_days=-2,
                        locations=(LOCATION_REMOTE,),
                    ),
                ),
            )
        )
        service = OnboardingService(registry)
        trigger = make_trigger(persona("EMP-001")).model_copy(
            update={"location": LOCATION_DUBAI}
        )
        with self.assertRaises(MissingConfigurationError):
            service.create_onboarding(HR_OPS, trigger)


class ScenarioGConflictingConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())

    def test_a_prerequisite_narrower_than_its_dependent_is_reported(self) -> None:
        issues = self.service.configuration_issues(HR_OPS, CLIENT_ALPHA)
        subjects = {i.subject for i in issues}
        self.assertIn("family_insurance_enrolment", subjects)
        issue = next(i for i in issues if i.subject == "family_insurance_enrolment")
        self.assertIs(issue.severity, ChecklistIssueSeverity.REVIEW_REQUIRED)
        self.assertIn("insurance_enrolment", issue.detail)

    def test_a_conflict_that_materialises_sets_review_required(self) -> None:
        """Declaring family insurance without base insurance drops a prerequisite."""
        preview = selection.preview(
            ALPHA_CONFIG, context(attributes=[ATTR_FAMILY_INSURANCE])
        )
        self.assertIn("family_insurance_enrolment", keys(preview.included))
        self.assertNotIn("insurance_enrolment", keys(preview.included))
        self.assertTrue(preview.review_required)
        dropped = [
            i for i in preview.issues if i.code == selection.ISSUE_DROPPED_PREREQUISITE
        ]
        self.assertEqual([i.subject for i in dropped], ["family_insurance_enrolment"])

    def test_a_consistent_context_raises_no_dropped_prerequisite(self) -> None:
        preview = selection.preview(
            ALPHA_CONFIG,
            context(attributes=[ATTR_INSURANCE_REQUIRED, ATTR_FAMILY_INSURANCE]),
        )
        self.assertEqual(
            [i for i in preview.issues if i.code == selection.ISSUE_DROPPED_PREREQUISITE],
            [],
        )

    def test_an_unreachable_milestone_is_reported(self) -> None:
        config = ClientOnboardingConfig(
            approval_status="approved",  # test fixture
            client_id=CLIENT_ALPHA,
            templates=(
                TaskTemplate(
                    key="base",
                    category=TaskCategory.HR_RECORD,
                    title="Base",
                    title_ar="أساسي",
                    owner=Role.HR_OPS,
                    offset_days=-5,
                ),
                TaskTemplate(
                    key="remote_only",
                    category=TaskCategory.WORKPLACE,
                    title="Remote only",
                    title_ar="عن بعد فقط",
                    owner=Role.FACILITIES,
                    offset_days=-2,
                    locations=(LOCATION_REMOTE,),
                ),
            ),
            milestones=(
                MilestoneTemplate(
                    key="everything_ready",
                    title="Everything ready",
                    title_ar="الجاهزية الكاملة",
                    offset_days=-1,
                    requires=("base", "remote_only"),
                ),
            ),
        )
        preview = selection.preview(config, context(location=LOCATION_DUBAI))
        unreachable = [
            i
            for i in preview.issues
            if i.code == selection.ISSUE_MILESTONE_UNREACHABLE
        ]
        self.assertEqual([i.subject for i in unreachable], ["everything_ready"])
        self.assertTrue(preview.review_required)

    def test_an_invalid_owner_is_reported_not_substituted(self) -> None:
        config = ClientOnboardingConfig(
            approval_status="approved",  # test fixture
            client_id=CLIENT_ALPHA,
            templates=(
                TaskTemplate(
                    key="unowned",
                    category=TaskCategory.HR_RECORD,
                    title="Unowned",
                    title_ar="بدون مالك",
                    owner=Role.SYSTEM,
                    offset_days=-1,
                ),
            ),
        )
        preview = selection.preview(config, context())
        owner_issues = [
            i for i in preview.issues if i.code == selection.ISSUE_OWNER_INVALID
        ]
        self.assertEqual([i.subject for i in owner_issues], ["unowned"])

    def test_unapproved_calendar_data_is_informational_not_review_required(self) -> None:
        preview = selection.preview(ALPHA_CONFIG, context())
        calendar_issues = [
            i for i in preview.issues if i.code == selection.ISSUE_CALENDAR_UNAPPROVED
        ]
        self.assertEqual(len(calendar_issues), 1)
        self.assertIs(
            calendar_issues[0].severity, ChecklistIssueSeverity.INFORMATIONAL
        )
        self.assertFalse(preview.review_required)
        self.assertIsNotNone(preview.calendar)
        self.assertFalse(preview.calendar.approved)


# ---------------------------------------------------------------------------
# End-to-end view behaviour
# ---------------------------------------------------------------------------


class ChecklistViewTests(ServiceTestCase):
    employee_id = "EMP-001"

    def view(self, as_of=ANCHOR_TODAY):
        return self.service.checklist(HR_OPS, self.onboarding_id, as_of)

    def test_buckets_partition_every_entry(self) -> None:
        view = self.view()
        bucketed = set(
            view.ready_task_ids
            + view.blocked_task_ids
            + view.in_progress_task_ids
            + view.complete_task_ids
            + view.cancelled_task_ids
        )
        self.assertEqual(bucketed, {e.task_id for e in view.entries})

    def test_entries_and_excluded_together_cover_the_whole_configuration(self) -> None:
        view = self.view()
        covered = {e.template_key for e in view.entries} | {
            d.template_key for d in view.excluded
        }
        self.assertEqual(covered, {t.key for t in ALPHA_CONFIG.templates})

    def test_overdue_is_derived_from_as_of_and_never_stored(self) -> None:
        early = self.view(date(2026, 10, 1))
        self.assertEqual(early.overdue_task_ids, [])

        late = self.view(date(2026, 12, 31))
        self.assertTrue(late.overdue_task_ids)
        overdue_entry = next(
            e for e in late.entries if e.task_id == late.overdue_task_ids[0]
        )
        self.assertTrue(overdue_entry.is_overdue)
        self.assertGreater(overdue_entry.days_overdue, 0)
        # The stored record still holds no OVERDUE status.
        self.assertNotIn(
            "overdue", {t.status.value for t in self.record.tasks}
        )

    def test_in_progress_is_reported_separately_from_ready(self) -> None:
        self.service.start_task(
            HR_OPS, self.onboarding_id, self.task_id("hr_record_create")
        )
        view = self.view()
        self.assertEqual(
            view.in_progress_task_ids, [self.task_id("hr_record_create")]
        )
        self.assertNotIn(self.task_id("hr_record_create"), view.ready_task_ids)

    def test_parallel_batches_are_reported(self) -> None:
        view = self.view()
        self.assertEqual(
            view.parallel_batches,
            self.service.parallel_batches(HR_OPS, self.onboarding_id),
        )

    def test_milestones_are_included(self) -> None:
        view = self.view()
        self.assertEqual(
            {m.template_key for m in view.milestones},
            {
                m.template_key
                for m in self.service.milestones(
                    HR_OPS, self.onboarding_id, ANCHOR_TODAY
                )
            },
        )

    def test_the_context_round_trips_the_record(self) -> None:
        view = self.view()
        record = self.record
        self.assertEqual(view.context.location, record.location)
        self.assertEqual(view.context.project, record.project)
        self.assertEqual(view.context.role_id, record.role_id)
        self.assertEqual(view.context.grade, record.grade)

    def test_the_view_carries_no_personal_data_field(self) -> None:
        serialised = self.view().model_dump()
        for forbidden in ("name", "email", "nationality", "passport", "phone"):
            self.assertNotIn(forbidden, serialised)

    def test_reading_the_checklist_is_audited(self) -> None:
        self.view()
        self.assertIn("onboarding.read", self.service.audit.actions_for(HR_OPS))

    def test_another_clients_actor_cannot_read_the_checklist(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.checklist(HR_OPS_BETA, self.onboarding_id, ANCHOR_TODAY)

    def test_repeated_reads_return_the_same_view(self) -> None:
        self.assertEqual(self.view().model_dump(), self.view().model_dump())

    def test_the_view_is_a_snapshot_and_cannot_mutate_the_record(self) -> None:
        view = self.view()
        view.entries[0].status = TaskStatus.COMPLETE
        self.assertNotIn(
            view.entries[0].task_id,
            self.service.checklist(
                HR_OPS, self.onboarding_id, ANCHOR_TODAY
            ).complete_task_ids,
        )


class AwaitingApprovalTests(ServiceTestCase):
    employee_id = "EMP-007"

    def view(self):
        return self.service.checklist(HR_OPS, self.onboarding_id, ANCHOR_TODAY)

    def test_a_completed_task_leaves_awaiting_approval(self) -> None:
        task_key = next(
            t.key
            for t in ALPHA_CONFIG.templates
            if t.approval_role is not None and not t.depends_on
        )
        self.assertIn(self.task_id(task_key), self.view().awaiting_approval_task_ids)

        self.service.complete_task(
            HR_OPS, self.onboarding_id, self.task_id(task_key), evidence_ref="REF-1"
        )
        self.assertNotIn(
            self.task_id(task_key), self.view().awaiting_approval_task_ids
        )

    def test_a_task_with_no_configured_approver_is_not_awaiting_approval(self) -> None:
        view = self.view()
        for entry in view.entries:
            if entry.approval_role is None:
                self.assertNotIn(entry.task_id, view.awaiting_approval_task_ids)


class PreviewMatchesPlanTests(unittest.TestCase):
    """The preview must predict exactly what creating the onboarding produces."""

    def test_preview_matches_the_generated_plan_for_every_persona(self) -> None:
        for employee_id in ("EMP-001", "EMP-003", "EMP-007", "EMP-008"):
            with self.subTest(employee=employee_id):
                service = OnboardingService(build_registry())
                trigger = make_trigger(persona(employee_id))
                record = service.create_onboarding(HR_OPS, trigger)
                preview = service.preview_checklist(
                    HR_OPS,
                    ChecklistContext(
                        client_id=trigger.client_id,
                        employee_id=trigger.employee_id,
                        joining_date=trigger.joining_date,
                        location=trigger.location,
                        project=trigger.project,
                        role_id=trigger.role_id,
                        grade=trigger.grade,
                        nationality_group=trigger.nationality_group,
                        attributes=list(trigger.attributes),
                    ),
                )
                self.assertEqual(
                    keys(preview.included),
                    {t.template_key for t in record.tasks},
                )
                for task in record.tasks:
                    self.assertEqual(
                        preview.due_dates[task.template_key], task.due_date
                    )

    def test_preview_creates_no_record(self) -> None:
        service = OnboardingService(build_registry())
        service.preview_checklist(HR_OPS, context())
        self.assertEqual(service._records, {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
