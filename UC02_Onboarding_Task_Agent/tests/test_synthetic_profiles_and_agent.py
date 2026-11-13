"""Synthetic UAE/India profiles end to end: checklist, DL-09, assignment, Fake LLM analysis, critical path,
team drafts and the send gate, and the new tools. Every configuration here is SYNTHETIC TEST DATA."""

from __future__ import annotations

import json
import unittest
from datetime import date

from onboarding_task_management import synthetic_profiles as sp
from onboarding_task_management.agent import analysis as an
from onboarding_task_management.agent import tools as tools_module
from onboarding_task_management.agent.tools import UC02Tools
from onboarding_task_management.contracts import (
    Actor,
    ChangeDriver,
    DecisionOutcome,
    Role,
    TaskCategory,
    TaskStatus,
)
from onboarding_task_management.errors import (
    ApprovalRequiredError,
    InvalidStateError,
    PlanNotApprovedError,
    UnauthorizedActionError,
)
from onboarding_task_management.sub_agent import drafts as drafts_module
from onboarding_task_management.sub_agent.checklist import AssignmentRule, ClientOnboardingConfig, ConfigRegistry, TaskTemplate
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import make_trigger, persona

TODAY = date(2026, 11, 2)


def actors(client: str):
    return (Actor(actor_id="U-HR-REQ", role=Role.HR_OPS, client_id=client),
            Actor(actor_id="U-HR-APPR", role=Role.HR_OPS, client_id=client),
            Actor(actor_id="U-LEAD-1", role=Role.HR_LEAD, client_id=client))


def start(profile_key: str, *, released: bool = False, service: OnboardingService | None = None):
    p = sp.PROFILES[profile_key]
    s = service or OnboardingService(sp.registry(), require_plan_approval=True)
    hr, approver, lead = actors(p.client_id)
    oid = s.create_onboarding(hr, sp.trigger_for(p)).onboarding_id
    if released:
        s.decide_plan(approver, s.submit_plan(hr, oid).request_id, DecisionOutcome.APPROVED)
    return s, oid, hr, approver, lead


def keys(s, oid, hr) -> set[str]:
    return {t.template_key for t in s.get(hr, oid).tasks}


class ProfileChecklistTests(unittest.TestCase):
    def test_every_profile_config_is_labelled_synthetic(self) -> None:
        for config in (sp.uae_config(), sp.india_config()):
            self.assertTrue(sp.is_synthetic(config))
            self.assertIn("SYNTHETIC-TEST", config.version)

    def test_different_profiles_get_different_checklists(self) -> None:
        seen = {}
        for name in sp.PROFILES:
            s, oid, hr, *_ = start(name)
            seen[name] = frozenset(keys(s, oid, hr))
        self.assertEqual(len(set(seen.values())), len(seen))

    def test_dl09_in_country_gets_the_status_change_route_and_not_the_entry_permit(self) -> None:
        s, oid, hr, *_ = start("uae_inside")
        k = keys(s, oid, hr)
        self.assertIn("visa_status_change", k)
        self.assertTrue({"entry_permit", "flight_booking", "medical_test_arrival", "biometrics_arrival"}.isdisjoint(k))
        self.assertTrue({"medical_test", "biometrics"} <= k)

    def test_dl09_overseas_gets_the_entry_permit_route_with_travel_and_arrival_steps(self) -> None:
        s, oid, hr, *_ = start("uae_overseas")
        k = keys(s, oid, hr)
        self.assertTrue({"entry_permit", "flight_booking", "accommodation", "medical_test_arrival",
                         "biometrics_arrival"} <= k)
        self.assertNotIn("visa_status_change", k)

    def test_dl09_routes_have_the_right_dependencies_and_owner(self) -> None:
        s, oid, hr, *_ = start("uae_overseas")
        record = s.get(hr, oid)
        flight = record.task_by_key("flight_booking")
        self.assertEqual(flight.depends_on, [record.task_by_key("entry_permit").task_id])
        self.assertEqual(record.task_by_key("entry_permit").owner, Role.PRO)
        self.assertEqual(record.task_by_key("flight_booking").owner, Role.FACILITIES)
        inside = start("uae_inside")
        rec = inside[0].get(inside[2], inside[1])
        self.assertEqual(rec.task_by_key("biometrics").depends_on, [rec.task_by_key("medical_test").task_id])

    def test_an_unstated_location_selects_no_visa_route_and_the_gap_is_reported(self) -> None:
        s, oid, hr, *_ = start("uae_unstated")
        k = keys(s, oid, hr)
        self.assertTrue({"visa_status_change", "entry_permit"}.isdisjoint(k))
        analysis = an.analyse_with_fallback(s, hr, oid, TODAY)
        self.assertTrue(any("location status" in m for m in analysis.missing_information))

    def test_india_has_no_visa_biometrics_or_entry_permit_unless_its_own_configuration_says_so(self) -> None:
        for name in ("india_plain", "india_full"):
            s, oid, hr, *_ = start(name)
            cats = {t.category for t in s.get(hr, oid).tasks}
            self.assertTrue({TaskCategory.VISA, TaskCategory.BIOMETRICS, TaskCategory.TRAVEL}.isdisjoint(cats))
        full = start("india_full")
        self.assertTrue({"medical_check", "insurance_enrolment"} <= keys(full[0], full[1], full[2]))
        plain = start("india_plain")
        self.assertTrue({"medical_check", "insurance_enrolment"}.isdisjoint(keys(plain[0], plain[1], plain[2])))

    def test_grade_attributes_and_nationality_filters_change_the_plan(self) -> None:
        junior, senior = start("uae_overseas"), start("uae_inside")
        self.assertNotIn("system_access", keys(junior[0], junior[1], junior[2]))
        self.assertIn("system_access", keys(senior[0], senior[1], senior[2]))
        cfg = ClientOnboardingConfig(
            client_id="SYN-NAT-TEST", version="SYNTHETIC-TEST-NAT", approval_status="approved", templates=(
                TaskTemplate(key="base", category=TaskCategory.HR_RECORD, title="b", title_ar="b", owner=Role.HR_OPS, offset_days=-5),
                TaskTemplate(key="grp_a_only", category=TaskCategory.PAYROLL, title="g", title_ar="g", owner=Role.FINANCE,
                             offset_days=-4, nationality_groups=("grp-a",))))
        reg = ConfigRegistry()
        reg.register(cfg)
        for grp, expected in (("grp-a", {"base", "grp_a_only"}), ("grp-b", {"base"})):
            s = OnboardingService(reg)
            a = Actor(actor_id="U", role=Role.HR_OPS, client_id="SYN-NAT-TEST")
            base = persona("EMP-001")
            p = type(base)(**{**base.__dict__, "client_id": "SYN-NAT-TEST", "nationality_group": grp})
            self.assertEqual({t.template_key for t in s.create_onboarding(a, make_trigger(p)).tasks}, expected)

    def test_a_change_of_location_status_replans_through_the_hr_gate_and_keeps_completed_work(self) -> None:
        s, oid, hr, approver, _ = start("uae_inside", released=True)
        record = s.get(hr, oid)
        rec_task = record.task_by_key("hr_record_create")
        s.start_task(hr, oid, rec_task.task_id)
        s.complete_task(hr, oid, rec_task.task_id)
        request = s.request_change(hr, oid, location_status="overseas")
        self.assertEqual(s.get(hr, oid).task_by_key("visa_status_change") is not None, True)  # nothing changed yet
        s.decide_change(approver, request.request_id, DecisionOutcome.APPROVED)
        outcome = s.apply_change(hr, request.request_id)
        self.assertEqual(outcome.drivers, [ChangeDriver.LOCATION_STATUS])
        k = keys(s, oid, hr)
        self.assertTrue({"entry_permit", "medical_test_arrival", "biometrics_arrival"} <= k)
        self.assertTrue({"visa_status_change", "medical_test", "biometrics"}.isdisjoint(k))
        self.assertNotIn("flight_booking", k)  # this employee never declared needs_travel
        self.assertEqual(s.get(hr, oid).task_by_key("hr_record_create").status, TaskStatus.COMPLETE)
        self.assertEqual(sum(1 for t in s.get(hr, oid).tasks if t.template_key == "hr_record_create"), 1)
        for t in s.get(hr, oid).tasks:  # no dangling dependency
            for d in t.depends_on:
                self.assertIsNotNone(s.get(hr, oid).task_by_id(d))
        with self.assertRaises(InvalidStateError):
            s.request_change(hr, oid, location_status="overseas")  # no change


class AssignmentTests(unittest.TestCase):
    def test_assignment_rules_name_team_use_case_and_escalation_owner(self) -> None:
        cfg = sp.uae_config()
        docs = cfg.assignment_for(TaskCategory.DOCUMENT_COLLECTION)
        self.assertEqual((docs.use_case, docs.escalation_owner), ("UC-03", Role.HR_OFFICER))
        self.assertEqual(cfg.assignment_for(TaskCategory.IT_PROVISIONING).use_case, "UC-04")
        self.assertEqual(cfg.assignment_for(TaskCategory.ORIENTATION).use_case, "UC-05")
        self.assertEqual(cfg.assignment_for(TaskCategory.VISA).mode, "role")

    def test_a_category_with_no_rule_is_an_assignment_gap_not_a_guess(self) -> None:
        s, oid, hr, *_ = start("india_full")
        a = an.analyse_with_fallback(s, hr, oid, TODAY)
        gaps = {x.template_key for x in a.team_assignment_suggestions if x.assignment_gap}
        self.assertEqual(gaps, {"medical_check", "insurance_enrolment"})
        self.assertTrue(all(x.team is None for x in a.team_assignment_suggestions if x.assignment_gap))
        self.assertTrue(any("no assignment rule" in m for m in a.missing_information))

    def test_rule_validation(self) -> None:
        with self.assertRaises(ValueError):
            AssignmentRule(category=TaskCategory.VISA, team="x", mode="telepathy")
        with self.assertRaises(ValueError):
            AssignmentRule(category=TaskCategory.VISA, team=" ")

    def test_team_labels_are_synthetic_and_no_address_exists_anywhere(self) -> None:
        self.assertTrue(all("(synthetic)" in v or v == "Employee" for v in sp.TEAM_LABELS.values()))
        for cfg in (sp.uae_config(), sp.india_config()):
            self.assertNotIn("@", json.dumps(cfg.model_dump(mode="json")))


class FakeAnalystTests(unittest.TestCase):
    def facts(self, name: str):
        s, oid, hr, *_ = start(name)
        return an.gather_facts(s, hr, oid, TODAY)

    def test_same_facts_give_the_same_analysis_every_time(self) -> None:
        f = self.facts("uae_overseas")
        a, b = an.FakeAnalyst().analyse(f), an.FakeAnalyst().analyse(f)
        self.assertEqual(a.model_dump_json(), b.model_dump_json())

    def test_different_profiles_and_configurations_give_different_analyses(self) -> None:
        sections = {}
        for name in sp.PROFILES:
            sections[name] = an.FakeAnalyst().analyse(self.facts(name))
        self.assertEqual(len({a.checklist_summary for a in sections.values()}), len(sections))
        self.assertNotEqual(sections["uae_inside"].critical_path, sections["uae_overseas"].critical_path)
        self.assertNotEqual(sections["india_plain"].missing_information, sections["uae_overseas"].missing_information)

    def test_all_requested_sections_are_present_and_labelled_fake(self) -> None:
        a = an.FakeAnalyst().analyse(self.facts("uae_overseas"))
        self.assertTrue(a.source.startswith("fake_llm:fake/"))
        self.assertTrue(a.advisory_only)
        for name in ("checklist_summary", "task_priorities", "hr_review_summary", "missing_information", "risks",
                     "dependency_notes", "parallel_recommendations", "team_assignment_suggestions", "blocker_analysis",
                     "delay_impact", "replan_recommendations", "approval_summary", "critical_path"):
            self.assertTrue(hasattr(a, name), name)
        self.assertTrue(a.parallel_recommendations and a.dependency_notes and a.team_assignment_suggestions)
        self.assertIn("advisory", a.approval_summary)

    def test_facts_carry_identifiers_and_dates_only(self) -> None:
        text = json.dumps(self.facts("uae_overseas"))
        for word in ("@", "passport", "salary", "candidate", "email", "phone"):
            self.assertNotIn(word, text)

    def test_the_analyst_never_changes_the_plan(self) -> None:
        s, oid, hr, *_ = start("uae_overseas", released=True)
        before = s.get(hr, oid).model_dump_json(exclude={"updated_at"})
        an.analyse_with_fallback(s, hr, oid, TODAY, an.FakeAnalyst("hallucinate"))
        self.assertEqual(s.get(hr, oid).model_dump_json(exclude={"updated_at"}), before)

    def test_a_hallucinating_analyst_is_overruled_and_marked_degraded(self) -> None:
        s, oid, hr, *_ = start("uae_overseas")
        a = an.analyse_with_fallback(s, hr, oid, TODAY, an.FakeAnalyst("hallucinate"))
        self.assertTrue(a.degraded)
        self.assertIn("overruled", a.degraded_reason)
        self.assertNotIn("invented", json.dumps(a.model_dump(mode="json")["task_priorities"]))
        self.assertTrue(all(not x.added_to_plan for x in a.suggested_additional_tasks))
        self.assertEqual([(x.template_key, x.status) for x in a.suggested_additional_tasks],
                         [("invented_task", an.STATUS_REQUIRES_CONFIRMATION)])  # flagged, never added
        self.assertNotIn("invented_task", {t.template_key for t in s.get(hr, oid).tasks})
        self.assertEqual(a.parallel_recommendations, [list(b) for b in an.gather_facts(s, hr, oid, TODAY)["parallel_batches"]])

    def test_an_unsupported_suggestion_requires_configuration_confirmation_and_is_never_added(self) -> None:
        f = self.facts("uae_overseas")
        a = an.FakeAnalyst().analyse(f)
        a.suggested_additional_tasks = [an.SuggestedTask(template_key="loan_laptop", reason="x"),
                                        an.SuggestedTask(template_key="visa_status_change", reason="x")]
        checked = an.validate_analysis(a, f)
        status = {x.template_key: x.status for x in checked.suggested_additional_tasks}
        self.assertEqual(status, {"loan_laptop": an.STATUS_REQUIRES_CONFIRMATION,
                                  "visa_status_change": an.STATUS_CONFIGURED_NOT_APPLICABLE})
        self.assertFalse(any(x.added_to_plan for x in checked.suggested_additional_tasks))

    def test_an_analyst_failure_degrades_to_the_rules_only_analysis_visibly(self) -> None:
        s, oid, hr, *_ = start("uae_overseas")
        a = an.analyse_with_fallback(s, hr, oid, TODAY, an.FakeAnalyst("error"))
        self.assertEqual((a.source, a.degraded), ("rules", True))
        self.assertIn("rules-only", a.degraded_reason)

    def test_a_late_task_produces_blocker_and_delay_impact_sections(self) -> None:
        s, oid, hr, *_ = start("uae_overseas", released=True)
        late = date(2026, 11, 13)
        a = an.analyse_with_fallback(s, hr, oid, late)
        self.assertTrue(a.blocker_analysis and a.delay_impact)
        self.assertTrue(any(d.dependent_task_ids for d in a.delay_impact))
        self.assertTrue(any("joining" in r.lower() for r in a.replan_recommendations))
        self.assertTrue(all("UC-02 will not" in r or "HR" in r or "owner" in r or "Supply" in r
                            for r in a.replan_recommendations))

    def test_the_protocol_is_what_a_real_model_would_implement(self) -> None:
        self.assertIsInstance(an.FakeAnalyst(), an.Analyst)


class CriticalPathTests(unittest.TestCase):
    def test_critical_path_follows_dependencies_to_the_tightest_end(self) -> None:
        s, oid, hr, *_ = start("uae_overseas")
        path = s.critical_path(hr, oid)
        names = [t.split("#")[-1] for t in path.task_ids]
        self.assertEqual(names[0], "hr_record_create")
        self.assertEqual(names[-1], "biometrics_arrival")
        record = s.get(hr, oid)
        for earlier, later in zip(path.task_ids, path.task_ids[1:]):
            self.assertIn(earlier, record.task_by_id(later).depends_on)

    def test_completed_work_leaves_the_critical_path(self) -> None:
        s, oid, hr, *_ = start("uae_inside", released=True)
        first = s.critical_path(hr, oid).task_ids[0]
        s.start_task(hr, oid, first)
        s.complete_task(hr, oid, first)
        self.assertNotIn(first, s.critical_path(hr, oid).task_ids)

    def test_delay_impact_lists_everything_downstream_and_moves_nothing(self) -> None:
        s, oid, hr, *_ = start("uae_overseas")
        permit = f"{oid}#entry_permit"
        before = [t.due_date for t in s.get(hr, oid).tasks]
        impact = s.delay_impact(hr, oid, permit, TODAY)
        self.assertEqual({t.split("#")[-1] for t in impact.dependent_task_ids},
                         {"flight_booking", "accommodation", "medical_test_arrival", "biometrics_arrival"})
        self.assertTrue(impact.on_critical_path)
        self.assertEqual([t.due_date for t in s.get(hr, oid).tasks], before)

    def test_parallel_batches_put_independent_work_together(self) -> None:
        s, oid, hr, *_ = start("uae_inside")
        batches = s.parallel_batches(hr, oid)
        first = {t.split("#")[-1] for t in batches[0]}
        self.assertEqual(first, {"hr_record_create"})
        second = {t.split("#")[-1] for t in batches[1]}
        self.assertTrue({"collect_documents", "laptop_request", "orientation_slot"} <= second)


class TeamDraftTests(unittest.TestCase):
    class Mock:
        name = "mock_sender"

        def __init__(self, fail: int = 0):
            self.calls, self.fail = 0, fail

        def send(self, draft):
            self.calls += 1
            if self.calls <= self.fail:
                raise ConnectionError("synthetic transport failure")
            return drafts_module.SendResult(drafts_module.OUTCOME_MOCK, reference=f"MOCK-{self.calls}")

    def test_drafts_need_a_released_plan(self) -> None:
        s, oid, hr, *_ = start("uae_inside")
        with self.assertRaises(PlanNotApprovedError):
            s.prepare_team_drafts(hr, oid)

    def test_one_draft_per_owning_role_with_task_due_priority_and_action_and_no_address(self) -> None:
        s, oid, hr, *_ = start("uae_inside", released=True)
        drafts = s.prepare_team_drafts(hr, oid)
        self.assertEqual({d.recipient_role for d in drafts}, {t.owner for t in s.get(hr, oid).tasks})
        pro = next(d for d in drafts if d.recipient_role is Role.PRO)
        for needle in ("visa_status_change", "due 20", "priority", "action:", "PRO / Visa (synthetic)"):
            self.assertIn(needle, pro.body_en)
        self.assertNotIn("@", pro.body_en)
        self.assertTrue(all(d.delivery_status == "draft_not_sent" for d in drafts))

    def test_preparing_twice_records_each_draft_once(self) -> None:
        s, oid, hr, *_ = start("uae_inside", released=True)
        first, second = s.prepare_team_drafts(hr, oid), s.prepare_team_drafts(hr, oid)
        self.assertEqual([d.draft_id for d in first], [d.draft_id for d in second])
        events = [e for e in s.audit.events_for(hr) if e.action == "draft.team_prepared"]
        self.assertEqual(len(events), len(first))

    def test_sending_needs_authorisation_by_a_different_hr_lead(self) -> None:
        s, oid, hr, approver, lead = start("uae_inside", released=True)
        draft = s.prepare_team_drafts(hr, oid)[0]
        with self.assertRaises(ApprovalRequiredError):
            s.send_draft(hr, oid, draft.draft_id, self.Mock())
        with self.assertRaises(UnauthorizedActionError):
            s.authorise_send(approver, oid, draft.draft_id)  # an HR operator may not authorise
        lead_prepared = Actor(actor_id="U-LEAD-1", role=Role.HR_LEAD, client_id=hr.client_id)
        s.prepare_team_drafts(lead_prepared, oid)  # idempotent; the original preparer stays hr
        s.authorise_send(lead, oid, draft.draft_id)
        sender = self.Mock()
        self.assertEqual(s.send_draft(hr, oid, draft.draft_id, sender).outcome, drafts_module.OUTCOME_MOCK)

    def test_the_preparer_may_not_authorise_their_own_draft(self) -> None:
        s, oid, hr, approver, lead = start("uae_inside", released=True)
        draft = s.prepare_team_drafts(lead, oid)[0]  # an HR lead prepared it
        with self.assertRaises(UnauthorizedActionError):
            s.authorise_send(lead, oid, draft.draft_id)

    def test_without_a_transport_the_result_is_not_configured_never_sent(self) -> None:
        s, oid, hr, _, lead = start("uae_inside", released=True)
        draft = s.prepare_team_drafts(hr, oid)[0]
        s.authorise_send(lead, oid, draft.draft_id)
        result = s.send_draft(hr, oid, draft.draft_id)
        self.assertEqual(result.outcome, drafts_module.OUTCOME_NOT_CONFIGURED)
        self.assertFalse(result.real)
        self.assertNotIn(drafts_module.OUTCOME_SENT, {e.outcome for e in s.audit.events_for(hr)
                                                       if e.action == "draft.send_result"})

    def test_a_delivered_draft_is_not_sent_twice(self) -> None:
        s, oid, hr, _, lead = start("uae_inside", released=True)
        draft = s.prepare_team_drafts(hr, oid)[0]
        s.authorise_send(lead, oid, draft.draft_id)
        sender = self.Mock()
        s.send_draft(hr, oid, draft.draft_id, sender)
        again = s.send_draft(hr, oid, draft.draft_id, sender)
        self.assertEqual((sender.calls, again.outcome), (1, drafts_module.OUTCOME_MOCK))

    def test_failures_are_recorded_and_retries_stop_at_the_limit(self) -> None:
        s, oid, hr, _, lead = start("uae_inside", released=True)
        draft = s.prepare_team_drafts(hr, oid)[0]
        s.authorise_send(lead, oid, draft.draft_id)
        sender = self.Mock(fail=99)
        for _ in range(drafts_module.MAX_SEND_ATTEMPTS):
            self.assertEqual(s.send_draft(hr, oid, draft.draft_id, sender).outcome, drafts_module.OUTCOME_FAILED)
        with self.assertRaises(InvalidStateError):
            s.send_draft(hr, oid, draft.draft_id, sender)
        self.assertEqual(sender.calls, drafts_module.MAX_SEND_ATTEMPTS)

    def test_a_failure_then_success_is_recorded_truthfully(self) -> None:
        s, oid, hr, _, lead = start("uae_inside", released=True)
        draft = s.prepare_team_drafts(hr, oid)[0]
        s.authorise_send(lead, oid, draft.draft_id)
        sender = self.Mock(fail=1)
        self.assertEqual(s.send_draft(hr, oid, draft.draft_id, sender).outcome, drafts_module.OUTCOME_FAILED)
        self.assertEqual(s.send_draft(hr, oid, draft.draft_id, sender).outcome, drafts_module.OUTCOME_MOCK)
        outcomes = [e.outcome for e in s.audit.events_for(hr) if e.action == "draft.send_result"]
        self.assertEqual(outcomes, [drafts_module.OUTCOME_FAILED, drafts_module.OUTCOME_MOCK])

    def test_a_stale_draft_is_refused_after_the_plan_changes(self) -> None:
        s, oid, hr, approver, lead = start("uae_inside", released=True)
        draft = s.prepare_team_drafts(hr, oid)[0]
        s.authorise_send(lead, oid, draft.draft_id)
        r = s.request_change(hr, oid, location_status="overseas")
        s.decide_change(approver, r.request_id, DecisionOutcome.APPROVED)
        s.apply_change(hr, r.request_id)
        with self.assertRaises(InvalidStateError):
            s.send_draft(hr, oid, draft.draft_id, self.Mock())

    def test_another_client_cannot_touch_the_draft(self) -> None:
        s, oid, hr, _, lead = start("uae_inside", released=True)
        draft = s.prepare_team_drafts(hr, oid)[0]
        outsider = Actor(actor_id="U-X", role=Role.HR_LEAD, client_id=sp.INDIA_CLIENT)
        with self.assertRaises(Exception):
            s.authorise_send(outsider, oid, draft.draft_id)


class ToolTests(unittest.TestCase):
    def test_new_tools_are_read_tools_on_the_policy_allowlist(self) -> None:
        import re
        from pathlib import Path

        policy = (Path(__file__).resolve().parents[1] / "guardrails" / "policy.yaml").read_text(encoding="utf-8")
        for tool in (tools_module.GET_CONFIGURATION, tools_module.CRITICAL_PATH, tools_module.ANALYSE_PLAN,
                     tools_module.PROPOSE_REPLAN, tools_module.PREPARE_TEAM_DRAFTS):
            self.assertIn(tool, tools_module.READ_TOOLS)
            self.assertTrue(re.search(rf"^\s+- {re.escape(tool)}\s*$", policy, re.M), tool)

    def test_tools_return_facts_and_the_replan_proposal_is_never_applied(self) -> None:
        s, oid, hr, *_ = start("uae_overseas", released=True)
        t = UC02Tools(s, hr)
        cfg = t.get_configuration(oid)
        self.assertTrue(cfg.ok and cfg.data["version"].startswith("SYNTHETIC-TEST"))
        self.assertTrue(t.critical_path(oid).ok)
        proposal = t.propose_replan(oid, date(2026, 11, 13))
        self.assertTrue(proposal.ok)
        self.assertEqual((proposal.data["applied"], proposal.data["requires_hr_decision"]), (False, True))
        drafts = t.prepare_team_drafts(oid)
        self.assertTrue(drafts.ok and drafts.data)
        self.assertEqual(len(t.calls), 4)

    def test_a_tool_failure_is_a_result_not_an_exception(self) -> None:
        s, oid, hr, *_ = start("uae_overseas")
        t = UC02Tools(s, hr)
        bad = t.analyse_plan("ONB-DOES-NOT-EXIST", TODAY)
        self.assertTrue(bad.failed)
        self.assertTrue(t.prepare_team_drafts(oid).failed)  # the plan is not released yet

    def test_the_agent_still_runs_and_the_new_tools_do_not_change_its_decisions(self) -> None:
        from onboarding_task_management.agent import UC02Agent

        s, oid, hr, *_ = start("uae_overseas", released=True)
        result = UC02Agent(s, hr).run(oid, TODAY)
        self.assertIn(result.status.value, {"observed", "proposed", "acted"})
        self.assertTrue(result.steps)


class ConfigurationSwapTests(unittest.TestCase):
    def test_a_real_configuration_can_replace_a_synthetic_one_without_touching_the_engine(self) -> None:
        """The engine only needs a registry; swapping the source changes the plan, not the code."""
        first = start("uae_inside")
        other = ClientOnboardingConfig(
            client_id=sp.UAE_CLIENT, version="REAL-FUTURE-v1", approval_status="approved",
            templates=(TaskTemplate(key="only_this", category=TaskCategory.HR_RECORD, title="t", title_ar="t",
                                    owner=Role.HR_OPS, offset_days=-3),))
        reg = ConfigRegistry()
        reg.register(other)
        s = OnboardingService(reg)
        hr = actors(sp.UAE_CLIENT)[0]
        record = s.create_onboarding(hr, sp.trigger_for(sp.PROFILES["uae_inside"]))
        self.assertEqual({t.template_key for t in record.tasks}, {"only_this"})
        self.assertNotEqual({t.template_key for t in record.tasks}, keys(first[0], first[1], first[2]))
        self.assertFalse(sp.is_synthetic(other))


if __name__ == "__main__":
    unittest.main()
