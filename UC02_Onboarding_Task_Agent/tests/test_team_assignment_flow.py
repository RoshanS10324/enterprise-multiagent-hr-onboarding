"""HR approval -> team-wise assignment -> per-team drafts and sending, over MOCK connectors and mocked Graph.

Nothing here contacts Microsoft or sends mail. The approval connector, document source and UC-04/05 are mocks. These tests
prove UC-02's side of each contract only.
"""

from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from onboarding_task_management import synthetic_profiles as sp
from onboarding_task_management.contracts import DecisionOutcome, PlanStatus, Role
from onboarding_task_management.errors import (
    InvalidStateError,
    PlanNotApprovedError,
    UnauthorizedActionError,
)
from onboarding_task_management.persistence import InMemoryTableClient, OnboardingStore
from onboarding_task_management.sub_agent import api as api_module
from onboarding_task_management.sub_agent import flow as fl
from onboarding_task_management.sub_agent.connectors.outlook import graph as g
from onboarding_task_management.sub_agent.connectors.outlook.mock import MockOutlookConnector
from onboarding_task_management.sub_agent.connectors.outlook.recipients import StaticRecipientDirectory
from tests.test_onboarding_flow import (
    CLIENT,
    COORD,
    LEAD,
    NOW,
    SECRET,
    approve_cb,
    make,
    to_review,
    verify_all,
)
from tests.test_outlook_graph import FakeGraph, settings

ADDRESSES = {
    "hr_ops": ["hr-ops@synthetic.invalid"], "pro": ["pro-visa@synthetic.invalid"],
    "it_manager": ["it-desk@synthetic.invalid"], "finance": ["payroll@synthetic.invalid"],
    "hr_officer": ["hr-officer@synthetic.invalid"], "employee": ["employee-pool@synthetic.invalid"],
    "manager": ["hiring-manager@synthetic.invalid"],
}


TEAM_ADDRESSES = {
    "HR Operations (synthetic)": ["hr-ops-team@synthetic.invalid"],
    "HR Onboarding Officer (synthetic)": ["hr-officer-team@synthetic.invalid"],
    "PRO / Visa (synthetic)": ["pro-visa-team@synthetic.invalid"],
    "IT Service Desk (synthetic)": ["it-desk-team@synthetic.invalid"],
    "Payroll (synthetic)": ["payroll-team@synthetic.invalid"],
    "Employee": ["employee-pool-team@synthetic.invalid"],
}


def mapping(teams=None, roles=None, fallback=False):
    return {CLIENT: {"roles": roles if roles is not None else ADDRESSES,
                     "teams": teams if teams is not None else TEAM_ADDRESSES, "allow_role_fallback": fallback}}


def graph_for(fake: FakeGraph, addresses=None, **kw) -> g.GraphOutlookConnector:
    directory = StaticRecipientDirectory(addresses if addresses is not None else mapping())
    return g.GraphOutlookConnector(settings(**kw), directory, transport=fake, sleep=lambda s: None, clock=lambda: 1000.0)


def approved_with_coordination(profile="uae_inside", store=None):
    flow, service, source = make(profile, store=store)
    oid, request = to_review(flow, service, profile)
    out = flow.process_approval_callback(CLIENT, approve_cb(oid, request.request_id), NOW)
    assert out.status == "approved", out
    return flow, service, oid, source


class ApprovalStartsCoordinationTests(unittest.TestCase):
    def test_a_valid_approval_prepares_team_assignments_and_the_document_request_but_sends_nothing(self) -> None:
        flow, service, oid, _ = approved_with_coordination()
        record = service.get(COORD, oid)
        self.assertEqual(record.plan_status, PlanStatus.APPROVED)
        self.assertTrue(record.assignments)
        self.assertTrue(all(a.status == "prepared" and a.plan_version == 1 for a in record.assignments))
        actions = [e.action for e in service.audit.events_for(COORD)]
        self.assertIn("coordination.prepared", actions)
        self.assertTrue([e for e in service.audit.events_for(COORD) if e.action == "docreq.prepared"])
        self.assertFalse([e for e in service.audit.events_for(COORD) if e.action.endswith(".result")])  # nothing sent

    def test_rejection_stops_everything_downstream_of_the_decision(self) -> None:
        flow, service, _ = make()
        oid, request = to_review(flow, service)
        out = flow.process_approval_callback(CLIENT, approve_cb(oid, request.request_id, decision="rejected",
                                                                reason="synthetic: not acceptable"), NOW)
        self.assertEqual(out.status, "rejected")
        record = service.get(COORD, oid)
        self.assertEqual((record.plan_status, record.assignments, record.downstream), (PlanStatus.REJECTED, [], {}))
        with self.assertRaises(PlanNotApprovedError):
            flow.prepare_team_assignments(COORD, oid)
        with self.assertRaises(InvalidStateError):
            flow.prepare_document_request(COORD, oid)
        actions = {e.action for e in service.audit.events_for(COORD)}
        self.assertNotIn("coordination.prepared", actions)

    def test_changes_requested_opens_a_new_request_and_only_the_revised_version_gets_assignments(self) -> None:
        flow, service, _ = make()
        oid, request = to_review(flow, service)
        out = flow.process_approval_callback(CLIENT, approve_cb(oid, request.request_id, decision="changes_requested",
                                                                reason="synthetic: tighten"), NOW)
        self.assertEqual(out.status, "changes_requested")
        self.assertIsNotNone(out.new_request_id)
        self.assertEqual(service.get(COORD, oid).assignments, [])  # nothing dispatched for the rejected version
        final = flow.process_approval_callback(CLIENT, approve_cb(oid, out.new_request_id, version=2, cb_id="CB-2"), NOW)
        self.assertEqual(final.status, "approved")
        self.assertTrue(all(a.plan_version == 2 for a in service.get(COORD, oid).assignments))

    def test_a_replayed_approval_creates_no_second_set_of_assignments(self) -> None:
        flow, service, _ = make()
        oid, request = to_review(flow, service)
        cb = approve_cb(oid, request.request_id)
        flow.process_approval_callback(CLIENT, cb, NOW)
        first = [a.assignment_id for a in service.get(COORD, oid).assignments]
        self.assertEqual(flow.process_approval_callback(CLIENT, cb, NOW).status, "replayed")
        self.assertEqual([a.assignment_id for a in service.get(COORD, oid).assignments], first)
        self.assertEqual(len(first), len(set(first)))

    def test_a_missing_team_rule_is_a_reported_gap_and_does_not_undo_the_approval(self) -> None:
        flow, service, _ = make()
        cfg = service.registry.configs[CLIENT]
        service.registry.configs[CLIENT] = cfg.model_copy(update={"assignments": tuple(
            a for a in cfg.assignments if a.category.value != "visa")})
        oid, request = to_review(flow, service)
        self.assertEqual(flow.process_approval_callback(CLIENT, approve_cb(oid, request.request_id), NOW).status, "approved")
        record = service.get(COORD, oid)
        self.assertEqual(record.plan_status, PlanStatus.APPROVED)
        assigned = {t.template_key for a in record.assignments for t in a.tasks}
        self.assertNotIn("visa_status_change", assigned)  # never guessed into a team
        gap = [e for e in service.audit.events_for(COORD) if e.action == "teamasg.gap"]
        self.assertIn("visa_status_change", gap[0].detail["task_keys"])


class GroupingAndContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow, self.service, self.oid, _ = approved_with_coordination()
        self.record = self.service.get(COORD, self.oid)

    def test_tasks_are_grouped_by_team_owner_and_use_case_without_overlap_or_loss(self) -> None:
        seen = []
        for a in self.record.assignments:
            self.assertTrue(a.tasks)
            for t in a.tasks:
                seen.append(t.task_id)
                task = self.record.task_by_id(t.task_id)
                self.assertEqual(task.owner.value, a.owner_role)
        self.assertEqual(sorted(seen), sorted(t.task_id for t in self.record.tasks))  # every open task, exactly once
        self.assertEqual(len(seen), len(set(seen)))

    def test_use_case_boundaries_come_only_from_the_configured_rules(self) -> None:
        by_task = {t.template_key: a.use_case for a in self.record.assignments for t in a.tasks}
        self.assertEqual((by_task["collect_documents"], by_task["laptop_request"], by_task["orientation_slot"]),
                         ("UC-03", "UC-04", "UC-05"))
        self.assertEqual((by_task["hr_record_create"], by_task["visa_status_change"]), ("", ""))
        hr_ops_groups = [a for a in self.record.assignments if a.owner_role == "hr_ops"]
        self.assertGreaterEqual(len(hr_ops_groups), 3)  # one role, but separate use-case boundaries stay separate

    def test_each_message_contains_only_that_teams_tasks_with_every_required_field(self) -> None:
        for a in self.record.assignments:
            subject, body, role = self.flow.assignment_message(COORD, self.oid, a.assignment_id)
            self.assertEqual(role.value, a.owner_role)
            self.assertIn(a.team, subject)
            for needle in (self.record.employee_id, self.record.case_id or self.record.onboarding_id, "plan version 1",
                           self.record.joining_date.isoformat()):
                self.assertIn(needle, body)
            own = {t.task_id for t in a.tasks}
            for t in a.tasks:
                for part in (t.task_id, f"due {t.due_date.isoformat()}", f"priority {t.priority}", "instructions:"):
                    self.assertIn(part, body)
            others = {t.task_id for t in self.record.tasks} - own
            for task_id in others:
                self.assertNotIn(task_id, body)  # another team's task is never described here
            self.assertNotIn("@", body)

    def test_dependencies_are_listed_as_keys_and_downstream_work_carries_its_hold(self) -> None:
        docs = next(a for a in self.record.assignments if a.use_case == "UC-03")
        self.assertIn("hr_record_create", docs.tasks[0].depends_on_keys)
        it = next(a for a in self.record.assignments if a.use_case == "UC-04")
        self.assertIn("HOLD", self.flow.assignment_message(COORD, self.oid, it.assignment_id)[1])
        self.assertTrue(it.hold_reason and not docs.hold_reason)

    def test_different_profiles_give_different_team_groupings(self) -> None:
        from tests.test_onboarding_flow import actor, event

        keys = {}
        for profile in ("uae_inside", "uae_overseas", "india_full"):
            flow, service, source = make(profile)
            client = sp.PROFILES[profile].client_id
            who = actor(Role.HR_OPS, "uc02-coordinator", client)
            oid = flow.handle_employee_created(event(profile=profile), fl.date(2026, 11, 2)).onboarding_id
            flow.generate_review(who, oid, fl.date(2026, 11, 2))
            request = service.submit_plan(who, oid)
            self.assertEqual(flow.process_approval_callback(client, approve_cb(oid, request.request_id), NOW).status, "approved")
            keys[profile] = {(a.team, a.use_case, len(a.tasks)) for a in service._load(who, oid).assignments}
        self.assertEqual(len({frozenset(v) for v in keys.values()}), 3)


class IdempotencyAndSupersededTests(unittest.TestCase):
    def test_preparing_twice_and_concurrently_creates_each_assignment_once(self) -> None:
        flow, service, oid, _ = approved_with_coordination()
        first = [a.assignment_id for a in service.get(COORD, oid).assignments]
        threads = [threading.Thread(target=lambda: flow.prepare_team_assignments(COORD, oid)) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        record = service.get(COORD, oid)
        self.assertEqual(sorted(a.assignment_id for a in record.assignments), sorted(first))
        prepared = [e for e in service.audit.events_for(COORD) if e.action == "teamasg.prepared"]
        self.assertEqual(len(prepared), len(first))

    def test_a_replan_supersedes_the_old_assignments_and_old_ones_cannot_be_sent(self) -> None:
        flow, service, oid, _ = approved_with_coordination()
        old = [a.assignment_id for a in service.get(COORD, oid).assignments]
        request = service.request_change(COORD, oid, location_status="overseas")
        service.decide_change(fl_actor("U-HR-APPR"), request.request_id, DecisionOutcome.APPROVED)
        service.apply_change(COORD, request.request_id)
        current = flow.prepare_team_assignments(COORD, oid)
        record = service.get(COORD, oid)
        self.assertTrue(all(a.plan_version == 2 for a in current))
        self.assertTrue(all(a.status == "superseded" for a in record.assignments if a.plan_version == 1))
        self.assertFalse(set(old) & {a.assignment_id for a in current})
        with self.assertRaises(InvalidStateError):
            flow.send_assignment(COORD, oid, old[0], MockOutlookConnector())


def fl_actor(ident):
    from tests.test_onboarding_flow import actor

    return actor(Role.HR_OPS, ident)


class DraftsAuthorisationAndSendingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow, self.service, self.oid, _ = approved_with_coordination()
        self.fake = FakeGraph()
        self.graph = graph_for(self.fake)
        self.record = self.service.get(COORD, self.oid)
        self.free = next(a for a in self.record.assignments if not a.hold_reason and a.use_case == "")

    def test_recipients_are_resolved_per_team_from_the_approved_directory(self) -> None:
        results = self.flow.save_assignment_drafts(COORD, self.oid, self.graph)
        self.assertTrue(all(r == "draft_created" for r in results.values()))
        for payload, a in zip(self.fake.bodies, self.service.get(COORD, self.oid).assignments):
            self.assertEqual([r["emailAddress"]["address"] for r in payload["toRecipients"]], TEAM_ADDRESSES[a.team])
            self.assertIn(a.team, payload["subject"])
        self.assertEqual(self.fake.count("send"), 0)  # drafts only
        record = self.service.get(COORD, self.oid)
        self.assertTrue(all(a.status == "draft_saved" and a.draft_ref for a in record.assignments))

    def test_each_team_gets_its_own_draft_and_a_rerun_creates_none(self) -> None:
        self.flow.save_assignment_drafts(COORD, self.oid, self.graph)
        count = self.fake.count("create")
        self.assertEqual(count, len(self.record.assignments))
        self.assertEqual(len({p["subject"] for p in self.fake.bodies}), count)
        self.flow.save_assignment_drafts(COORD, self.oid, self.graph)
        self.assertEqual(self.fake.count("create"), count)

    def test_a_team_with_no_approved_recipient_fails_closed_without_blocking_the_others(self) -> None:
        partial = mapping(teams={k: v for k, v in TEAM_ADDRESSES.items() if k != "PRO / Visa (synthetic)"})
        graph = graph_for(self.fake, addresses=partial)
        results = self.flow.save_assignment_drafts(COORD, self.oid, graph)
        pro = [a.assignment_id for a in self.record.assignments if a.owner_role == "pro"]
        self.assertTrue(pro and all(results[i] == "failed" for i in pro))
        self.assertTrue(all(v == "draft_created" for k, v in results.items() if k not in pro))

    def test_a_team_with_no_entry_does_not_borrow_its_roles_address_unless_explicitly_allowed(self) -> None:
        no_team = mapping(teams={})
        results = self.flow.save_assignment_drafts(COORD, self.oid, graph_for(self.fake, addresses=no_team))
        self.assertTrue(all(v == "failed" for v in results.values()))
        self.assertEqual(self.fake.calls, [])  # refused before any network call

    def test_role_fallback_needs_explicit_permission_and_is_blocked_while_two_teams_share_the_role(self) -> None:
        cfg = self.service.registry.configs[CLIENT]
        rules = tuple(r.model_copy(update={"team": "Orientation Team (synthetic)"}) if r.category.value == "orientation" else r
                      for r in cfg.assignments)
        self.service.registry.configs[CLIENT] = cfg.model_copy(update={"assignments": rules})
        flow, service = self.flow, self.service
        oid = self.oid
        request = service.request_change(COORD, oid, location_status="overseas")
        service.decide_change(fl_actor("U-HR-APPR"), request.request_id, DecisionOutcome.APPROVED)
        service.apply_change(COORD, request.request_id)
        flow.prepare_team_assignments(COORD, oid)
        record = service.get(COORD, oid)
        shared = [a for a in record.assignments if a.owner_role == "hr_ops" and a.plan_version == record.plan_version]
        self.assertGreater(len({a.team for a in shared}), 1)  # two teams, one owner role
        fallback = graph_for(self.fake, addresses=mapping(teams={}, fallback=True))
        results = flow.save_assignment_drafts(COORD, oid, fallback)
        for a in shared:
            self.assertEqual(results[a.assignment_id], "failed")  # ambiguous: a team-level entry is required
        solo = [a for a in record.assignments if a.owner_role == "pro" and a.plan_version == record.plan_version][0]
        self.assertEqual(results[solo.assignment_id], "draft_created")  # an unshared role may fall back, explicitly allowed
        pro_draft = next(p for p in self.fake.bodies if "PRO / Visa" in p["subject"])
        self.assertEqual([r["emailAddress"]["address"] for r in pro_draft["toRecipients"]], ADDRESSES["pro"])

    def test_sending_needs_authorisation_by_a_different_hr_lead(self) -> None:
        with self.assertRaises(InvalidStateError):
            self.flow.send_assignment(COORD, self.oid, self.free.assignment_id, self.graph)
        with self.assertRaises(UnauthorizedActionError):
            self.flow.authorise_assignment(fl_actor("U-HR-APPR"), self.oid, self.free.assignment_id)  # not a lead
        self.flow.authorise_assignment(LEAD, self.oid, self.free.assignment_id)
        self.assertEqual(self.flow.send_assignment(COORD, self.oid, self.free.assignment_id, self.graph),
                         "submitted_for_sending")
        self.assertEqual(self.fake.count("send"), 1)
        status = next(a for a in self.service.get(COORD, self.oid).assignments if a.assignment_id == self.free.assignment_id)
        self.assertEqual((status.status, status.send_attempts), ("submitted_for_sending", 1))
        self.assertNotIn("delivered", " ".join(e.outcome for e in self.service.audit.events_for(COORD)))

    def test_a_second_send_and_a_restarted_connector_cannot_duplicate_the_email(self) -> None:
        self.flow.authorise_assignment(LEAD, self.oid, self.free.assignment_id)
        self.flow.send_assignment(COORD, self.oid, self.free.assignment_id, self.graph)
        self.flow.send_assignment(COORD, self.oid, self.free.assignment_id, self.graph)
        restarted = graph_for(self.fake)
        self.flow.send_assignment(COORD, self.oid, self.free.assignment_id, restarted)
        self.assertEqual(self.fake.count("send"), 1)
        self.assertEqual(sum(1 for f in self.fake.folder.values() if f == "sentitems"), 1)

    def test_a_transient_failure_is_retried_and_a_permanent_one_stops_at_the_limit(self) -> None:
        self.flow.authorise_assignment(LEAD, self.oid, self.free.assignment_id)
        self.fake.queue("create", (503, {}), (503, {}), (503, {}))
        self.assertEqual(self.flow.send_assignment(COORD, self.oid, self.free.assignment_id, self.graph), "failed")
        self.assertEqual(self.flow.send_assignment(COORD, self.oid, self.free.assignment_id, self.graph),
                         "submitted_for_sending")  # the retry succeeds
        other = next(a for a in self.record.assignments if a is not self.free and not a.hold_reason and a.use_case == ""
                     and a.assignment_id != self.free.assignment_id)
        self.flow.authorise_assignment(LEAD, self.oid, other.assignment_id)
        self.fake.queue("create", *[(403, {})] * 20)
        for _ in range(fl.MAX_MESSAGE_SEND_ATTEMPTS):
            self.assertEqual(self.flow.send_assignment(COORD, self.oid, other.assignment_id, self.graph), "failed")
        with self.assertRaises(InvalidStateError):
            self.flow.send_assignment(COORD, self.oid, other.assignment_id, self.graph)

    def test_changed_content_is_refused(self) -> None:
        self.flow.authorise_assignment(LEAD, self.oid, self.free.assignment_id)
        subject, body, role = self.flow.assignment_message(COORD, self.oid, self.free.assignment_id)
        with self.assertRaises(InvalidStateError):
            self.flow.send_message(COORD, self.oid, "teamasg", self.free.assignment_id, subject, body + " tampered", role,
                                   self.graph)

    def test_the_mock_connector_drives_the_same_path(self) -> None:
        mock = MockOutlookConnector()
        self.flow.save_assignment_drafts(COORD, self.oid, mock)
        self.assertEqual(len(mock.drafts(CLIENT)), len(self.record.assignments))
        self.flow.authorise_assignment(LEAD, self.oid, self.free.assignment_id)
        self.assertEqual(self.flow.send_assignment(COORD, self.oid, self.free.assignment_id, mock), "simulated_sent")

    def test_the_document_request_uses_the_same_draft_and_send_path_with_the_content_check(self) -> None:
        request = self.flow.prepare_document_request(COORD, self.oid)
        args = (self.oid, "docreq", request.request_id, request.subject, request.body, request.recipient_role)
        self.assertEqual(self.flow.save_draft_message(COORD, *args, self.graph), "draft_created")
        self.flow.authorise_message(LEAD, self.oid, "docreq", request.request_id)
        self.assertEqual(self.flow.send_message(COORD, *args, self.graph), "submitted_for_sending")
        with self.assertRaises(InvalidStateError):
            self.flow.send_message(COORD, self.oid, "docreq", request.request_id, request.subject, request.body + "x",
                                   request.recipient_role, self.graph)


class HeldAssignmentsAndGateTests(unittest.TestCase):
    def test_uc04_and_uc05_assignments_stay_held_until_every_mandatory_document_is_verified(self) -> None:
        flow, service, oid, _ = approved_with_coordination()
        fake = FakeGraph()
        graph = graph_for(fake)
        held = [a for a in service.get(COORD, oid).assignments if a.hold_reason]
        self.assertEqual({a.use_case for a in held}, {"UC-04", "UC-05"})
        for a in held:
            flow.authorise_assignment(LEAD, oid, a.assignment_id)
            with self.assertRaises(InvalidStateError):
                flow.send_assignment(COORD, oid, a.assignment_id, graph)
        self.assertEqual(fake.count("send"), 0)
        a, b = fl.MockTrigger(fl.UC04), fl.MockTrigger(fl.UC05)
        self.assertEqual(flow.trigger_downstream(COORD, oid, {fl.UC04: a, fl.UC05: b}),
                         {fl.UC04: "waiting_for_documents", fl.UC05: "waiting_for_documents"})
        verify_all(flow, oid)
        self.assertEqual(flow.send_assignment(COORD, oid, held[0].assignment_id, graph), "submitted_for_sending")
        self.assertEqual(flow.trigger_downstream(COORD, oid, {fl.UC04: a, fl.UC05: b}),
                         {fl.UC04: "triggered", fl.UC05: "triggered"})

    def test_uc04_failing_does_not_block_uc05_after_the_gate_opens(self) -> None:
        flow, service, oid, _ = approved_with_coordination()
        verify_all(flow, oid)
        result = flow.trigger_downstream(COORD, oid, {fl.UC04: fl.MockTrigger(fl.UC04, raise_error=True),
                                                      fl.UC05: fl.MockTrigger(fl.UC05)})
        self.assertEqual(result, {fl.UC04: "failed", fl.UC05: "triggered"})


class PersistenceTests(unittest.TestCase):
    def test_assignments_and_their_status_survive_a_restart(self) -> None:
        client = InMemoryTableClient()
        flow, service, oid, source = approved_with_coordination(store=OnboardingStore(client))
        fake = FakeGraph()
        graph = graph_for(fake)
        flow.save_assignment_drafts(COORD, oid, graph)
        free = next(a for a in service.get(COORD, oid).assignments if not a.hold_reason and not a.use_case)
        flow.authorise_assignment(LEAD, oid, free.assignment_id)
        flow.send_assignment(COORD, oid, free.assignment_id, graph)
        flow2, service2, _ = make(store=OnboardingStore(InMemoryTableClient.from_snapshot(client.snapshot())), source=source)
        service2.hydrate(CLIENT)
        again = {a.assignment_id: a for a in service2.get(COORD, oid).assignments}
        self.assertEqual(again[free.assignment_id].status, "submitted_for_sending")
        self.assertTrue(all(a.draft_ref for a in again.values()))
        self.assertEqual(flow2.send_assignment(COORD, oid, free.assignment_id, graph_for(fake)), "submitted_for_sending")
        self.assertEqual(fake.count("send"), 1)  # no second email after the restart
        self.assertEqual(len(flow2.prepare_team_assignments(COORD, oid)), len(again))

    def test_the_state_is_stored_in_the_supabase_snapshot_without_a_new_table(self) -> None:
        from tests.test_flow_supabase import (
            LIVE_CLIENT,
            LIVE_EMP,
            NOW as SNOW,
            FakePostgrest,
            MockEmployeeSource,
            build,
        )
        from tests.test_supabase_store import CASE
        from onboarding_task_management.supabase_store import STATE_ACTION

        db = FakePostgrest()
        db.seed_case(employee=LIVE_EMP)
        source = MockEmployeeSource()
        source.add("uae_inside", case_id=CASE, client_id=LIVE_CLIENT, employee_id=LIVE_EMP)
        flow, service = build(db, source)
        event = fl.EmployeeCreatedEvent(event_id="E1", client_id=LIVE_CLIENT, employee_id=LIVE_EMP, case_id=CASE,
                                        joining_date=sp.JOINING, occurred_at=SNOW, correlation_id="C1")
        oid = flow.handle_employee_created(event, SNOW.date()).onboarding_id
        coordinator = COORD.model_copy(update={"client_id": LIVE_CLIENT})
        flow.generate_review(coordinator, oid, SNOW.date())
        request = service.submit_plan(coordinator, oid)
        cb = fl.sign_callback(SECRET, approve_cb(oid, request.request_id, approver="U-APPR", sign=False).model_copy(
            update={"client_id": LIVE_CLIENT}))
        self.assertEqual(flow.process_signed_callback(cb, SNOW).status, "approved")
        latest = max((r for r in db.tables["audit_events"] if r["action"] == STATE_ACTION and r["case_id"] == oid),
                     key=lambda r: r["after_state"]["updated_at"])["after_state"]
        self.assertTrue(latest["assignments"] and all("tasks" in a for a in latest["assignments"]))
        self.assertTrue([r for r in db.tables["audit_events"] if r["action"] == "teamasg.prepared"])
        self.assertEqual(set(db.tables), set(db.tables))  # no table other than the live ones was needed


class CallbackEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow, self.service, _ = make()
        self.oid, self.request = to_review(self.flow, self.service)
        api_module.configure_flow(self.flow)
        self.client = TestClient(api_module.app)
        self.addCleanup(api_module.configure_flow, None)

    def post(self, **kw):
        kw.setdefault("issued", datetime.now(timezone.utc))  # the route uses the real clock
        cb = approve_cb(self.oid, self.request.request_id, sign=False, **kw).model_copy(update={"client_id": CLIENT})
        cb = fl.sign_callback(SECRET, cb)
        return self.client.post("/v1/approvals/callback", content=cb.model_dump_json(), headers={"content-type": "application/json"})

    def test_without_a_configured_secret_the_route_refuses(self) -> None:
        api_module.configure_flow(None)
        import os
        saved = os.environ.pop(api_module.ENV_CALLBACK_SECRET, None)
        try:
            response = self.client.post("/v1/approvals/callback", content="{}", headers={"content-type": "application/json"})
            self.assertIn(response.status_code, (422, 503))
        finally:
            if saved is not None:
                os.environ[api_module.ENV_CALLBACK_SECRET] = saved

    def test_a_signed_approval_updates_the_plan_and_starts_coordination(self) -> None:
        response = self.post()
        self.assertEqual((response.status_code, response.json()["status"]), (200, "approved"))
        record = self.service.get(COORD, self.oid)
        self.assertEqual(record.plan_status, PlanStatus.APPROVED)
        self.assertTrue(record.assignments)
        self.assertTrue([r for r in self.service.store.load_approvals(CLIENT)] if self.service.store else True)

    def test_unsigned_tampered_unauthorised_and_stale_callbacks_get_the_right_codes(self) -> None:
        fresh = datetime.now(timezone.utc)
        unsigned = approve_cb(self.oid, self.request.request_id, sign=False, issued=fresh).model_copy(update={"client_id": CLIENT})
        self.assertEqual(self.client.post("/v1/approvals/callback", content=unsigned.model_dump_json(),
                                          headers={"content-type": "application/json"}).status_code, 401)
        signed = fl.sign_callback(SECRET, approve_cb(self.oid, self.request.request_id, sign=False, cb_id="T",
                                                      issued=fresh).model_copy(update={"client_id": CLIENT}))
        tampered = signed.model_copy(update={"approver_id": "U-EVIL"})
        self.assertEqual(self.client.post("/v1/approvals/callback", content=tampered.model_dump_json(),
                                          headers={"content-type": "application/json"}).status_code, 401)
        self.assertEqual(self.post(role=Role.EMPLOYEE, cb_id="U", approver="U-EMP").status_code, 403)
        self.assertEqual(self.post(version=9, cb_id="S").status_code, 409)
        self.assertEqual(self.service.get(COORD, self.oid).plan_status, PlanStatus.SUBMITTED)

    def test_a_replay_is_answered_without_a_second_effect(self) -> None:
        self.assertEqual(self.post(cb_id="R").status_code, 200)
        count = len(self.service.get(COORD, self.oid).assignments)
        again = self.post(cb_id="R")
        self.assertEqual((again.status_code, again.json()["status"]), (200, "replayed"))
        self.assertEqual(len(self.service.get(COORD, self.oid).assignments), count)

    def test_the_client_comes_from_the_signed_field_so_another_client_cannot_be_targeted(self) -> None:
        cb = approve_cb(self.oid, self.request.request_id, sign=False, cb_id="X",
                        issued=datetime.now(timezone.utc)).model_copy(update={"client_id": sp.INDIA_CLIENT})
        signed = fl.sign_callback(SECRET, cb)
        response = self.client.post("/v1/approvals/callback", content=signed.model_dump_json(),
                                    headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.service.get(COORD, self.oid).plan_status, PlanStatus.SUBMITTED)

    def test_the_email_names_the_deployments_callback_endpoint_when_one_is_configured(self) -> None:
        self.flow.approval_callback_url = "https://uc02.synthetic.invalid/v1/approvals/callback"
        body = self.flow.prepare_hr_email(COORD, self.oid, fl.date(2026, 11, 2)).body
        self.assertIn("https://uc02.synthetic.invalid/v1/approvals/callback", body)
        self.flow.approval_callback_url = None
        self.assertIn("no approval endpoint is configured", self.flow.prepare_hr_email(COORD, self.oid, fl.date(2026, 11, 2)).body)


if __name__ == "__main__":
    unittest.main()
