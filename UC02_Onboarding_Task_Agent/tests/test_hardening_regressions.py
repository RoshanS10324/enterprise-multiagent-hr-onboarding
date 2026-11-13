"""Regression tests for the second hardening pass.

Findings covered: M3 (Arabic titles and reminder content), L3 (calendar
coverage), L4 (cyclic task dependencies). Each fails against the pre-fix code.
"""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.calendar_uae import (
    COVERAGE_END,
    COVERAGE_START,
    add_business_days,
    business_days_between,
    is_covered,
    next_business_day,
    previous_business_day,
)
from onboarding_task_management.contracts import Role, TaskCategory
from onboarding_task_management.errors import CalendarRangeError
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    TaskTemplate,
    topological_order,
)
from onboarding_task_management.sub_agent.reminders import (
    CATEGORY_LABELS_AR,
    ROLE_LABELS_AR,
    category_label_ar,
    role_label_ar,
)
from onboarding_task_management.sub_agent.service import (
    OnboardingService,
    OnboardingTrigger,
)
from onboarding_task_management.synthetic import (
    ALPHA_CONFIG,
    BETA_CONFIG,
    CLIENT_ALPHA,
    accepted_offer,
    approved_form,
    build_registry,
)
from tests.support import HR_OPS, ServiceTestCase


def _template(key: str, **overrides) -> TaskTemplate:
    kwargs = dict(
        key=key,
        category=TaskCategory.HR_RECORD,
        title="Title",
        title_ar="عنوان",
        owner=Role.HR_OPS,
        offset_days=-1,
    )
    kwargs.update(overrides)
    return TaskTemplate(**kwargs)


def _is_arabic(text: str) -> bool:
    return any("؀" <= ch <= "ۿ" for ch in text)


# ---------------------------------------------------------------------------
# M3 — Arabic titles
# ---------------------------------------------------------------------------


class ArabicTitleValidationTests(unittest.TestCase):
    def test_blank_arabic_title_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _template("a", title_ar="")
        self.assertIn("title_ar", str(ctx.exception))

    def test_whitespace_only_arabic_title_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _template("a", title_ar="   ")

    def test_omitted_arabic_title_is_rejected(self) -> None:
        # title_ar used to default to "" and silently produce Arabic-less tasks.
        with self.assertRaises(ValueError):
            TaskTemplate(
                key="a",
                category=TaskCategory.HR_RECORD,
                title="English only",
                owner=Role.HR_OPS,
                offset_days=-1,
            )

    def test_blank_english_title_is_rejected_too(self) -> None:
        with self.assertRaises(ValueError):
            _template("a", title="")

    def test_shipped_configs_are_fully_bilingual(self) -> None:
        for config in (ALPHA_CONFIG, BETA_CONFIG):
            for template in config.templates:
                with self.subTest(client=config.client_id, key=template.key):
                    self.assertTrue(template.title.strip())
                    self.assertTrue(_is_arabic(template.title_ar))


# ---------------------------------------------------------------------------
# M3 — Arabic reminder content
# ---------------------------------------------------------------------------


class ArabicReminderContentTests(ServiceTestCase):
    AS_OF = date(2026, 11, 5)

    def overdue_draft(self):
        drafts = self.service.reminder_drafts(HR_OPS, self.onboarding_id, self.AS_OF)
        return next(d for d in drafts if d.reason == "overdue")

    def test_every_category_has_an_arabic_label(self) -> None:
        self.assertEqual(set(CATEGORY_LABELS_AR), set(TaskCategory))
        for category, label in CATEGORY_LABELS_AR.items():
            with self.subTest(category=category):
                self.assertTrue(_is_arabic(label))

    def test_every_role_has_an_arabic_label(self) -> None:
        self.assertEqual(set(ROLE_LABELS_AR), set(Role))
        for role, label in ROLE_LABELS_AR.items():
            with self.subTest(role=role):
                self.assertTrue(_is_arabic(label))

    def test_arabic_body_does_not_reuse_english_category_slugs(self) -> None:
        draft = self.overdue_draft()
        # Previously body_ar interpolated the English lines verbatim.
        self.assertIn("hr_record", draft.body_en)
        self.assertNotIn("hr_record)", draft.body_ar)
        self.assertNotIn("document_collection)", draft.body_ar)
        self.assertNotIn(" due ", draft.body_ar)

    def test_arabic_body_uses_arabic_category_labels(self) -> None:
        draft = self.overdue_draft()
        self.assertIn(category_label_ar(TaskCategory.HR_RECORD), draft.body_ar)
        self.assertIn(
            category_label_ar(TaskCategory.DOCUMENT_COLLECTION), draft.body_ar
        )

    def test_arabic_body_addresses_the_owner_in_arabic(self) -> None:
        draft = self.overdue_draft()
        self.assertIn(role_label_ar(Role.HR_OPS), draft.body_ar)
        self.assertNotIn("hr_ops:", draft.body_ar)

    def test_every_arabic_task_line_contains_arabic(self) -> None:
        draft = self.overdue_draft()
        task_lines = [l for l in draft.body_ar.splitlines() if l.startswith("- ")]
        self.assertTrue(task_lines)
        for line in task_lines:
            with self.subTest(line=line):
                self.assertTrue(_is_arabic(line))

    def test_identifiers_and_dates_stay_latin_in_both_bodies(self) -> None:
        draft = self.overdue_draft()
        for task_id in draft.task_ids:
            self.assertIn(task_id, draft.body_en)
            self.assertIn(task_id, draft.body_ar)
        self.assertIn("2026-11-02", draft.body_ar)

    def test_english_body_is_unchanged_in_shape(self) -> None:
        draft = self.overdue_draft()
        self.assertIn("Tasks owned by hr_ops:", draft.body_en)
        self.assertIn("(hr_record) due 2026-11-02", draft.body_en)


# ---------------------------------------------------------------------------
# L3 — calendar coverage
# ---------------------------------------------------------------------------


class CalendarCoverageTests(unittest.TestCase):
    def test_coverage_bounds_are_the_holiday_list_range(self) -> None:
        self.assertEqual(COVERAGE_START, date(2026, 1, 1))
        self.assertEqual(COVERAGE_END, date(2027, 12, 31))
        self.assertTrue(is_covered(COVERAGE_START))
        self.assertTrue(is_covered(COVERAGE_END))
        self.assertFalse(is_covered(date(2025, 12, 31)))
        self.assertFalse(is_covered(date(2028, 1, 1)))

    def test_uncovered_start_is_rejected(self) -> None:
        with self.assertRaises(CalendarRangeError) as ctx:
            add_business_days(date(2028, 11, 29), 1)
        self.assertIn("outside the verified", str(ctx.exception))

    def test_uncovered_result_is_rejected_even_from_a_covered_start(self) -> None:
        # Walking far enough forward leaves the window.
        with self.assertRaises(CalendarRangeError):
            add_business_days(COVERAGE_END, 5)
        with self.assertRaises(CalendarRangeError):
            add_business_days(COVERAGE_START, -5)

    def test_business_days_between_rejects_uncovered_ends(self) -> None:
        with self.assertRaises(CalendarRangeError):
            business_days_between(date(2026, 1, 5), date(2028, 1, 5))
        with self.assertRaises(CalendarRangeError):
            business_days_between(date(2025, 1, 5), date(2026, 1, 5))

    def test_next_and_previous_business_day_are_guarded(self) -> None:
        with self.assertRaises(CalendarRangeError):
            next_business_day(date(2028, 5, 1))
        with self.assertRaises(CalendarRangeError):
            previous_business_day(date(2025, 5, 1))

    def test_covered_dates_still_work(self) -> None:
        self.assertEqual(add_business_days(date(2026, 11, 16), -10), date(2026, 11, 2))
        self.assertEqual(business_days_between(date(2026, 11, 2), date(2026, 11, 16)), 10)

    def test_custom_holiday_sets_bypass_the_coverage_check(self) -> None:
        # A caller supplying their own calendar owns its coverage.
        custom = frozenset({date(2028, 12, 2)})
        self.assertEqual(
            add_business_days(date(2028, 11, 29), 1, custom), date(2028, 11, 30)
        )

    def test_onboarding_with_an_uncovered_joining_date_is_rejected(self) -> None:
        service = OnboardingService(build_registry())
        trigger = OnboardingTrigger(
            client_id=CLIENT_ALPHA,
            employee_id="EMP-700",
            joining_date=date(2029, 3, 1),
            role_id="backend_engineer",
            department="technology",
            manager_id="MGR-A",
            location="Dubai",
            project="apollo",
            offer=accepted_offer("EMP-700"),
            form=approved_form("EMP-700"),
        )
        with self.assertRaises(CalendarRangeError):
            service.create_onboarding(HR_OPS, trigger)


# ---------------------------------------------------------------------------
# L4 — cyclic dependencies
# ---------------------------------------------------------------------------


class DependencyCycleTests(unittest.TestCase):
    def test_two_node_cycle_is_rejected_by_the_config_validator(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id="C",
                templates=(
                    _template("a", depends_on=("b",)),
                    _template("b", depends_on=("a",)),
                ),
            )
        self.assertIn("dependency cycle", str(ctx.exception))

    def test_three_node_cycle_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id="C",
                templates=(
                    _template("a", depends_on=("b",)),
                    _template("b", depends_on=("c",)),
                    _template("c", depends_on=("a",)),
                ),
            )
        self.assertIn("dependency cycle", str(ctx.exception))

    def test_cycle_message_names_the_involved_keys(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id="C",
                templates=(
                    _template("a", depends_on=("b",)),
                    _template("b", depends_on=("a",)),
                ),
            )
        message = str(ctx.exception)
        self.assertIn("a", message)
        self.assertIn("b", message)
        self.assertIn("->", message)

    def test_self_dependency_is_still_rejected_distinctly(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id="C", templates=(_template("a", depends_on=("a",)),)
            )
        self.assertIn("depends on itself", str(ctx.exception))

    def test_a_cycle_can_no_longer_reach_plan_time(self) -> None:
        # Previously the validator accepted a 2-cycle and topological_order
        # raised later, at task-generation time.
        with self.assertRaises(ValueError):
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id="C",
                templates=(
                    _template("a", depends_on=("b",)),
                    _template("b", depends_on=("a",)),
                ),
            )

    def test_acyclic_diamond_is_accepted(self) -> None:
        config = ClientOnboardingConfig(
            approval_status="approved",  # test fixture
            client_id="C",
            templates=(
                _template("root"),
                _template("left", depends_on=("root",)),
                _template("right", depends_on=("root",)),
                _template("join", depends_on=("left", "right")),
            ),
        )
        order = [t.key for t in topological_order(config.templates)]
        self.assertLess(order.index("root"), order.index("left"))
        self.assertLess(order.index("left"), order.index("join"))
        self.assertLess(order.index("right"), order.index("join"))

    def test_shipped_configs_remain_acyclic(self) -> None:
        for config in (ALPHA_CONFIG, BETA_CONFIG):
            with self.subTest(client=config.client_id):
                self.assertEqual(
                    len(topological_order(config.templates)), len(config.templates)
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
