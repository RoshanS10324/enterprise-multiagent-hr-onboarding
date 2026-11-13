"""guardrails/policy.yaml must stay identical to the Python mirror (M1/M2).

CLAUDE.md rule 3 makes policy.yaml the single source of truth for gates,
autonomy and thresholds. Until ``PolicyStore`` can load it, the values live in
two places — ``approvals.GATES`` and ``config`` — and nothing stopped them
drifting. These tests are that guard.
"""

from __future__ import annotations

import unittest

from onboarding_task_management import config
from onboarding_task_management.calendar_uae import COVERAGE_END, COVERAGE_START
from onboarding_task_management.contracts import GateName, TaskCategory
from onboarding_task_management.sub_agent.approvals import (
    CANCELLATION_GATES,
    CHANGE_GATES,
    FINAL_GATES,
    GATES,
    PLAN_GATES,
)
from tests import yaml_lite


def load_policy() -> dict:
    return yaml_lite.parse(config.POLICY_PATH.read_text(encoding="utf-8"))


class YamlLiteTests(unittest.TestCase):
    """The reader itself, so a parity pass cannot be an artefact of a bad parse."""

    def test_parses_nested_maps_scalars_and_lists(self) -> None:
        parsed = yaml_lite.parse(
            "\n".join(
                [
                    "# leading comment",
                    "name: thing          # trailing comment",
                    "count: 7",
                    "flag: true",
                    "when: 2026-01-31",
                    "empty: []",
                    'quoted: "a: b"',
                    "outer:",
                    "  inner:",
                    "    deep: 1",
                    "  items:",
                    "    - one",
                    "    - two",
                ]
            )
        )
        self.assertEqual(parsed["name"], "thing")
        self.assertEqual(parsed["count"], 7)
        self.assertIs(parsed["flag"], True)
        self.assertEqual(parsed["when"].isoformat(), "2026-01-31")
        self.assertEqual(parsed["empty"], [])
        self.assertEqual(parsed["quoted"], "a: b")
        self.assertEqual(parsed["outer"]["inner"]["deep"], 1)
        self.assertEqual(parsed["outer"]["items"], ["one", "two"])

    def test_policy_file_parses(self) -> None:
        policy = load_policy()
        self.assertEqual(policy["agent"], config.AGENT_ID)
        self.assertIsInstance(policy["gates"], dict)


class PolicyParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy()

    # -- identity and autonomy --------------------------------------------

    def test_agent_id_matches(self) -> None:
        self.assertEqual(self.policy["agent"], config.AGENT_ID)
        self.assertEqual(self.policy["policy"], config.AGENT_ID)

    def test_autonomy_matches(self) -> None:
        self.assertEqual(self.policy["autonomy"], config.AUTONOMY_LAUNCH)
        self.assertEqual(self.policy["ceiling"], config.AUTONOMY_CEILING)

    # -- gates -------------------------------------------------------------

    def test_gate_names_match_the_enum_exactly(self) -> None:
        self.assertEqual(
            set(self.policy["gates"]), {gate.value for gate in GateName}
        )

    def test_gate_names_match_the_python_gate_table(self) -> None:
        self.assertEqual(
            set(self.policy["gates"]), {gate.value for gate in GATES}
        )

    def test_approver_roles_match(self) -> None:
        for name, entry in self.policy["gates"].items():
            with self.subTest(gate=name):
                self.assertEqual(
                    entry["approver_role"], GATES[GateName(name)].approver_role.value
                )

    def test_sla_hours_match(self) -> None:
        for name, entry in self.policy["gates"].items():
            with self.subTest(gate=name):
                self.assertEqual(entry["sla_hours"], GATES[GateName(name)].sla_hours)

    def test_every_gate_has_a_description(self) -> None:
        for name, entry in self.policy["gates"].items():
            with self.subTest(gate=name):
                self.assertTrue(str(entry["description"]).strip())

    # -- gate binding (C1) -------------------------------------------------

    def test_replan_bindings_match_change_gates(self) -> None:
        declared = {
            GateName(name)
            for name, entry in self.policy["gates"].items()
            if entry["authorises"] == "replan"
        }
        self.assertEqual(declared, CHANGE_GATES)

    def test_cancel_bindings_match_cancellation_gates(self) -> None:
        declared = {
            GateName(name)
            for name, entry in self.policy["gates"].items()
            if entry["authorises"] == "cancel"
        }
        self.assertEqual(declared, CANCELLATION_GATES)

    def test_bindings_are_disjoint_and_declared(self) -> None:
        self.assertEqual(CHANGE_GATES & CANCELLATION_GATES, set())
        for name, entry in self.policy["gates"].items():
            with self.subTest(gate=name):
                self.assertIn(
                    entry["authorises"],
                    {"replan", "cancel", "unused", "plan_release", "final_release"},
                )

    def test_plan_and_final_bindings_match_their_gate_sets(self) -> None:
        for binding, expected in (("plan_release", PLAN_GATES), ("final_release", FINAL_GATES)):
            declared = {
                GateName(name)
                for name, entry in self.policy["gates"].items()
                if entry["authorises"] == binding
            }
            self.assertEqual(declared, expected, binding)
        self.assertEqual(PLAN_GATES & FINAL_GATES, set())
        self.assertEqual((PLAN_GATES | FINAL_GATES) & (CHANGE_GATES | CANCELLATION_GATES), set())

    def test_unused_gates_are_not_wired_to_any_operation(self) -> None:
        unused = {
            GateName(name)
            for name, entry in self.policy["gates"].items()
            if entry["authorises"] == "unused"
        }
        # joining_confirmation is declared but no operation consumes it yet.
        self.assertEqual(unused, {GateName.JOINING_CONFIRMATION})
        self.assertEqual(unused & (CHANGE_GATES | CANCELLATION_GATES), set())

    # -- thresholds and category coverage ---------------------------------

    def test_thresholds_match(self) -> None:
        thresholds = self.policy["thresholds"]
        self.assertEqual(
            thresholds["joining_risk_days_before"], config.JOINING_RISK_DAYS_BEFORE
        )
        self.assertEqual(thresholds["reminder_lead_days"], config.REMINDER_LEAD_DAYS)

    def test_day_one_critical_categories_match(self) -> None:
        self.assertEqual(
            set(self.policy["day_one_critical_categories"]),
            set(config.DAY_ONE_CRITICAL_CATEGORIES),
        )

    def test_day_one_critical_categories_are_real_categories(self) -> None:
        valid = {category.value for category in TaskCategory}
        for name in self.policy["day_one_critical_categories"]:
            with self.subTest(category=name):
                self.assertIn(name, valid)

    def test_calendar_coverage_matches(self) -> None:
        coverage = self.policy["calendar_coverage"]
        self.assertEqual(coverage["start"], COVERAGE_START)
        self.assertEqual(coverage["end"], COVERAGE_END)

    # -- the envelope is not yet cleared for live use ----------------------

    def test_policy_is_not_yet_signed_off(self) -> None:
        # approved_by must be filled before L1 live (review C-12).
        self.assertEqual(self.policy["approved_by"], [])

    def test_never_auto_covers_the_hard_gated_operations(self) -> None:
        never_auto = set(self.policy["never_auto"])
        self.assertIn("cancel_onboarding", never_auto)
        self.assertIn("hris.update_joining_date", never_auto)
        self.assertIn("send_joining_instructions", never_auto)


class EnforcementClassificationTests(unittest.TestCase):
    """Every policy key must declare whether it is actually enforced (§3)."""

    #: Keys that are metadata about the envelope, not enforceable settings.
    METADATA_KEYS = frozenset(
        {"policy", "agent", "version", "enabled", "enforcement", "enforcement_notes"}
    )
    VALID_STATUSES = frozenset(
        {"enforced", "declared_not_enforced", "unused_pending_decision"}
    )

    def setUp(self) -> None:
        self.policy = load_policy()
        self.enforcement = self.policy["enforcement"]
        self.notes = self.policy["enforcement_notes"]

    def test_every_status_is_one_of_the_three_allowed_values(self) -> None:
        for key, status in self.enforcement.items():
            with self.subTest(key=key):
                self.assertIn(status, self.VALID_STATUSES)

    #: Classified separately but nested under another key in the YAML, so they
    #: are not top-level settings: `gate_authorises` sits inside each gate and
    #: `write_requires_approval` sits inside `tools`.
    NESTED_KEYS = frozenset({"gate_authorises", "write_requires_approval"})

    def test_every_settings_key_has_a_classification(self) -> None:
        settings = set(self.policy) - self.METADATA_KEYS
        self.assertEqual(settings, set(self.enforcement) - self.NESTED_KEYS)

    def test_nested_classified_keys_exist_where_expected(self) -> None:
        self.assertIn("write_requires_approval", self.policy["tools"])
        for entry in self.policy["gates"].values():
            self.assertIn("authorises", entry)

    def test_every_classification_has_a_reason(self) -> None:
        self.assertEqual(set(self.enforcement), set(self.notes))
        for key, note in self.notes.items():
            with self.subTest(key=key):
                self.assertTrue(str(note).strip())

    def test_unresolved_keys_explain_the_blocker(self) -> None:
        pending = [
            key
            for key, status in self.enforcement.items()
            if status == "unused_pending_decision"
        ]
        self.assertEqual(pending, ["volume_ceiling"])
        self.assertIn("BLOCKED", self.notes["volume_ceiling"])

    # -- the "enforced" claims must be true -------------------------------

    def test_claimed_enforced_thresholds_really_drive_behaviour(self) -> None:
        self.assertEqual(self.enforcement["thresholds"], "enforced")
        self.assertEqual(
            self.policy["thresholds"]["joining_risk_days_before"],
            config.JOINING_RISK_DAYS_BEFORE,
        )
        self.assertEqual(
            self.policy["thresholds"]["reminder_lead_days"], config.REMINDER_LEAD_DAYS
        )

    def test_claimed_enforced_categories_really_drive_behaviour(self) -> None:
        self.assertEqual(self.enforcement["day_one_critical_categories"], "enforced")
        self.assertEqual(
            set(self.policy["day_one_critical_categories"]),
            set(config.DAY_ONE_CRITICAL_CATEGORIES),
        )

    def test_claimed_enforced_calendar_really_drives_behaviour(self) -> None:
        self.assertEqual(self.enforcement["calendar_coverage"], "enforced")
        self.assertEqual(self.policy["calendar_coverage"]["start"], COVERAGE_START)
        self.assertEqual(self.policy["calendar_coverage"]["end"], COVERAGE_END)

    def test_claimed_enforced_gate_binding_really_drives_behaviour(self) -> None:
        self.assertEqual(self.enforcement["gate_authorises"], "enforced")
        self.assertEqual(CHANGE_GATES | CANCELLATION_GATES | PLAN_GATES | FINAL_GATES, set(GATES) - {
            GateName(name)
            for name, entry in self.policy["gates"].items()
            if entry["authorises"] == "unused"
        })

    def test_unenforced_keys_are_not_silently_read_by_config(self) -> None:
        # autonomy/ceiling are mirrored as constants but nothing consumes them;
        # if that changes, the classification must change with it.
        self.assertEqual(self.enforcement["autonomy"], "declared_not_enforced")
        self.assertEqual(self.enforcement["ceiling"], "declared_not_enforced")
        self.assertEqual(config.AUTONOMY_LAUNCH, self.policy["autonomy"])
        self.assertEqual(config.AUTONOMY_CEILING, self.policy["ceiling"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
