"""Extended end-to-end evaluation scenarios over the SYNTHETIC UAE and India profiles.

Same rules as ``run_evals.py``: each scenario drives the real service, agent, tools and persistence and raises
``AssertionError`` to fail. Every configuration is synthetic test data; every downstream status is a MOCK, and no
scenario claims a real UC-03/04/05 integration, a real email or a real database.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from onboarding_task_management import synthetic_profiles as sp
from onboarding_task_management.agent import UC02Agent
from onboarding_task_management.agent import analysis as an
from onboarding_task_management.agent.tools import UC02Tools
from onboarding_task_management.contracts import (
    Actor,
    DecisionOutcome,
    ExternalOutcome,
    PlanStatus,
    Role,
    SourceUseCase,
    StatusUpdate,
    TaskStatus,
)
from onboarding_task_management.errors import UnauthorizedActionError
from onboarding_task_management.persistence import OnboardingStore
from onboarding_task_management.sub_agent import drafts as drafts_module
from onboarding_task_management.sub_agent.checklist import NonResponsePolicy
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.sqlite_store import SqliteTableClient

TODAY = date(2026, 11, 2)
SOURCE_ID = "svc-mock-ops"  # a mock service identity; no real one exists


def _service(**kw) -> OnboardingService:
    return OnboardingService(sp.registry(), require_plan_approval=True, **kw)


def _actors(client: str):
    return (Actor(actor_id="U-HR-REQ", role=Role.HR_OPS, client_id=client),
            Actor(actor_id="U-HR-APPR", role=Role.HR_OPS, client_id=client),
            Actor(actor_id="U-LEAD-1", role=Role.HR_LEAD, client_id=client))


def _begin(profile: str, service: OnboardingService | None = None, release: bool = False):
    p = sp.PROFILES[profile]
    s = service or _service()
    hr, approver, lead = _actors(p.client_id)
    oid = s.create_onboarding(hr, sp.trigger_for(p)).onboarding_id  # UC-01 handoff as the trigger
    if release:
        s.decide_plan(approver, s.submit_plan(hr, oid).request_id, DecisionOutcome.APPROVED)
    return s, oid, hr, approver, lead


def _keys(s, oid, hr):
    return {t.template_key for t in s.get(hr, oid).tasks}


def scenario_uae_inside_end_to_end() -> str:
    s, oid, hr, approver, lead = _begin("uae_inside")
    cfg = UC02Tools(s, hr).get_configuration(oid)
    assert cfg.ok and sp.is_synthetic(s.registry.get(sp.UAE_CLIENT)), "configuration retrieved and labelled synthetic"
    analysis = an.analyse_with_fallback(s, hr, oid, TODAY)
    assert analysis.source.startswith("fake_llm:") and analysis.advisory_only
    first = s.submit_plan(hr, oid)
    s.decide_plan(approver, first.request_id, DecisionOutcome.REJECTED, "synthetic: tighten the visa dates")
    assert s.get(hr, oid).plan_status is PlanStatus.REJECTED
    s.revise_plan(hr, oid, "synthetic: dates tightened after review")
    second = s.submit_plan(hr, oid)
    s.decide_plan(approver, second.request_id, DecisionOutcome.APPROVED)
    assert s.get(hr, oid).plan_status is PlanStatus.APPROVED
    drafts = s.prepare_team_drafts(hr, oid)
    assert drafts and all(d.delivery_status == "draft_not_sent" for d in drafts)
    result = UC02Agent(s, hr).run(oid, TODAY)
    assert result.steps
    return f"{len(s.get(hr, oid).tasks)} tasks, rejected once then approved, {len(drafts)} team drafts"


def scenario_uae_overseas_dl09() -> str:
    s, oid, hr, *_ = _begin("uae_overseas")
    k = _keys(s, oid, hr)
    assert {"entry_permit", "flight_booking", "accommodation"} <= k and "visa_status_change" not in k
    rec = s.get(hr, oid)
    assert rec.task_by_key("flight_booking").depends_on == [rec.task_by_key("entry_permit").task_id]
    assert rec.task_by_key("entry_permit").owner is Role.PRO
    return "overseas route: entry permit -> travel -> arrival medical/biometrics, owned by PRO / Facilities"


def scenario_india_onboarding() -> str:
    s, oid, hr, *_ = _begin("india_full")
    cats = {t.category.value for t in s.get(hr, oid).tasks}
    assert not ({"visa", "biometrics", "travel"} & cats), cats
    assert {"medical", "insurance", "background_verification", "payroll"} <= cats
    return "India plan has no visa/biometrics/travel; medical and insurance only because its profile declares them"


def scenario_dl09_replan_on_location_status() -> str:
    s, oid, hr, approver, _ = _begin("uae_inside", release=True)
    done = s.get(hr, oid).task_by_key("hr_record_create")
    s.start_task(hr, oid, done.task_id)
    s.complete_task(hr, oid, done.task_id)
    request = s.request_change(hr, oid, location_status="overseas")
    s.decide_change(approver, request.request_id, DecisionOutcome.APPROVED)
    s.apply_change(hr, request.request_id)
    k = _keys(s, oid, hr)
    assert "entry_permit" in k and "visa_status_change" not in k
    assert s.get(hr, oid).task_by_key("hr_record_create").status is TaskStatus.COMPLETE
    return "route switched through the HR gate; completed work kept; no duplicate tasks"


def scenario_dl14_non_response_cycle() -> str:
    reg = sp.registry()
    cfg = reg.configs[sp.UAE_CLIENT].model_copy(update={"non_response_policy": NonResponsePolicy(interval_days=2, max_attempts=2)})
    reg.register(cfg)
    s = OnboardingService(reg, require_plan_approval=True)
    s_, oid, hr, approver, _ = _begin("uae_inside", service=s, release=True)
    tid = s.get(hr, oid).tasks[0].task_id
    for day in (2, 2, 3, 4):  # same-day replay and an early attempt are not new attempts
        status = s.record_reminder_attempt(hr, oid, tid, date(2026, 11, day))
    assert status.attempts == 2, status
    status = s.escalate_unanswered(hr, oid, tid, date(2026, 11, 6))
    assert status.follow_up_required and "no message was sent" in status.delivery
    done = s.record_follow_up_outcome(hr, oid, tid, "called_no_answer")
    assert done.state == "follow_up_done"
    return "2 attempts, escalated once to HR, manual follow-up recorded; nothing claimed as sent"


def scenario_visa_delay_and_joining_risk() -> str:
    s, oid, hr, *_ = _begin("uae_overseas", release=True)
    permit = s.get(hr, oid).task_by_key("entry_permit").task_id
    s.report_delay(hr, oid, permit, "synthetic: processing delay")
    impact = s.delay_impact(hr, oid, permit, TODAY)
    assert len(impact.dependent_task_ids) >= 3 and impact.on_critical_path
    a = an.analyse_with_fallback(s, hr, oid, date(2026, 11, 13))
    assert a.delay_impact and any("UC-02 will not" in r for r in a.replan_recommendations)
    assert s.get(hr, oid).joining_date == sp.JOINING, "UC-02 never moves the joining date itself"
    return f"{len(impact.dependent_task_ids)} dependants held up; joining date untouched; HR is asked to decide"


def scenario_background_verification_delay_blocks_dependants() -> str:
    principals = {src: frozenset({SOURCE_ID}) for src in SourceUseCase}
    s = _service(source_principals=principals)
    s, oid, hr, *_ = _begin("india_full", service=s, release=True)
    source_actor = Actor(actor_id=SOURCE_ID, role=Role.SYSTEM, client_id=sp.INDIA_CLIENT)
    rec = s.get(hr, oid)
    bv = rec.task_by_key("background_verification")
    docs = rec.task_by_key("collect_documents")
    for prerequisite in (rec.task_by_key("hr_record_create"), docs):  # in dependency order
        s.start_task(hr, oid, prerequisite.task_id)
        s.complete_task(hr, oid, prerequisite.task_id)  # the prerequisites are done, so verification is startable
    update = StatusUpdate(update_id="UPD-BV", source=SourceUseCase.UC03_DOCUMENT_VALIDATION, client_id=sp.INDIA_CLIENT,
                          onboarding_id=oid, task_id=bv.task_id, outcome=ExternalOutcome.REQUIRES_REVIEW,
                          occurred_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
                          blocking_reason="synthetic: mock UC-03 report")
    assert s.apply_status_update(source_actor, oid, update).accepted
    after = s.get(hr, oid)
    assert after.task_by_id(bv.task_id).status in (TaskStatus.READY, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED), "stays open"
    a = an.analyse_with_fallback(s, hr, oid, TODAY)
    assert any(b.task_id == bv.task_id for b in a.blocker_analysis), "an unresolved report is a blocker"
    return "a mock UC-03 'requires_review' keeps verification open and surfaces it as a blocker (mock status, not a real integration)"


def scenario_parallel_execution_and_dependency_blocking() -> str:
    s, oid, hr, *_ = _begin("uae_inside", release=True)
    rec = s.get(hr, oid)
    assert rec.task_by_key("hr_record_create").status is TaskStatus.READY
    assert rec.task_by_key("laptop_request").status is TaskStatus.BLOCKED
    first = rec.task_by_key("hr_record_create").task_id
    s.start_task(hr, oid, first)
    s.complete_task(hr, oid, first)
    after = s.get(hr, oid)
    parallel = [t.template_key for t in after.tasks if t.status is TaskStatus.READY]
    assert {"collect_documents", "laptop_request", "orientation_slot"} <= set(parallel), parallel
    assert after.task_by_key("biometrics").status is TaskStatus.BLOCKED
    return f"after the root task, {len(parallel)} tasks became ready together; the visa chain still waits"


def scenario_team_drafts_and_send_gate() -> str:
    s, oid, hr, _, lead = _begin("uae_inside", release=True)
    draft = s.prepare_team_drafts(hr, oid)[0]
    try:
        s.send_draft(hr, oid, draft.draft_id)
    except Exception as exc:
        assert "authorisation" in str(exc)
    else:
        raise AssertionError("sending without authorisation must be refused")
    s.authorise_send(lead, oid, draft.draft_id)
    result = s.send_draft(hr, oid, draft.draft_id)
    assert result.outcome == drafts_module.OUTCOME_NOT_CONFIGURED and not result.real
    return "authorised by a different HR lead; with no transport the result is 'not_configured', never 'sent'"


def scenario_assignment_gap_detection() -> str:
    s, oid, hr, *_ = _begin("india_full")
    a = an.analyse_with_fallback(s, hr, oid, TODAY)
    gaps = {x.template_key for x in a.team_assignment_suggestions if x.assignment_gap}
    assert gaps == {"medical_check", "insurance_enrolment"}, gaps
    uc = {x.template_key: x.use_case for x in a.team_assignment_suggestions}
    assert uc["collect_documents"] == "UC-03" and uc["laptop_request"] == "UC-04" and uc["orientation_slot"] == "UC-05"
    return "unmapped categories reported as gaps; UC-03/04/05 come only from the configured rules"


def scenario_fake_llm_profiles_and_safety() -> str:
    texts = set()
    for name in sp.PROFILES:
        s, oid, hr, *_ = _begin(name)
        texts.add(an.analyse_with_fallback(s, hr, oid, TODAY).checklist_summary)
    assert len(texts) == len(sp.PROFILES), "different profiles must get different analyses"
    s, oid, hr, *_ = _begin("uae_overseas")
    bad = an.analyse_with_fallback(s, hr, oid, TODAY, an.FakeAnalyst("hallucinate"))
    assert bad.degraded and all(not x.added_to_plan for x in bad.suggested_additional_tasks)
    assert "invented_task" not in _keys(s, oid, hr)
    broken = an.analyse_with_fallback(s, hr, oid, TODAY, an.FakeAnalyst("error"))
    assert broken.source == "rules" and broken.degraded
    return "profile-specific analyses; hallucination overruled; failure degrades to rules visibly"


def scenario_agent_tool_selection_and_blocker_replan_proposal() -> str:
    s, oid, hr, *_ = _begin("uae_overseas", release=True)
    tools = UC02Tools(s, hr)
    proposal = tools.propose_replan(oid, date(2026, 11, 13))
    assert proposal.ok and proposal.data["applied"] is False and proposal.data["requires_hr_decision"]
    rec = s.get(hr, oid)
    assert (rec.joining_date, rec.revision, rec.plan_version) == (sp.JOINING, 0, 1), "nothing was applied"
    assert not [r for r in s.inbox.pending(hr)], "a proposal opens no request by itself"
    return "re-plan proposal produced for HR; nothing applied"


def scenario_duplicate_events_and_ownership() -> str:
    principals = {src: frozenset({SOURCE_ID}) for src in SourceUseCase}
    s = _service(source_principals=principals)
    s, oid, hr, *_ = _begin("uae_inside", service=s, release=True)
    source_actor = Actor(actor_id=SOURCE_ID, role=Role.SYSTEM, client_id=sp.UAE_CLIENT)
    laptop = s.get(hr, oid).task_by_key("laptop_request")
    update = StatusUpdate(update_id="UPD-1", source=SourceUseCase.UC03_DOCUMENT_VALIDATION, client_id=sp.UAE_CLIENT,
                          onboarding_id=oid, task_id=laptop.task_id, outcome=ExternalOutcome.COMPLETED,
                          occurred_at=datetime(2026, 11, 2, tzinfo=timezone.utc), reference="MOCK-REF")
    result = s.apply_status_update(source_actor, oid, update)
    assert not result.accepted, "UC-03 must not be able to close a UC-04 laptop task"
    again = s.apply_status_update(source_actor, oid, update)
    assert not again.accepted
    try:
        s.create_onboarding(hr, sp.trigger_for(sp.PROFILES["uae_inside"]))
    except Exception:
        pass
    else:
        raise AssertionError("a duplicate trigger must not create a second onboarding")
    return "wrong-source status refused; duplicate trigger refused (mock status updates only)"


def scenario_unauthorized_actions_rejected() -> str:
    s, oid, hr, approver, lead = _begin("uae_inside", release=True)
    draft = s.prepare_team_drafts(hr, oid)[0]
    for actor in (approver, hr):
        try:
            s.authorise_send(actor, oid, draft.draft_id)
        except UnauthorizedActionError:
            continue
        raise AssertionError("only an HR lead may authorise sending")
    outsider = Actor(actor_id="U-X", role=Role.HR_OPS, client_id=sp.INDIA_CLIENT)
    try:
        s.get(outsider, oid)
    except Exception:
        return "operators cannot authorise sends; another client cannot read the case"
    raise AssertionError("cross-client read must be refused")


def scenario_interruption_resume_and_restart_recovery() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "uc02.sqlite3"
        p = sp.PROFILES["uae_inside"]
        hr, approver, lead = _actors(p.client_id)
        client = SqliteTableClient(str(db))
        s = OnboardingService(sp.registry(), store=OnboardingStore(client), require_plan_approval=True)
        oid = s.create_onboarding(hr, sp.trigger_for(p)).onboarding_id
        request = s.submit_plan(hr, oid)  # the process stops here, with the approval pending
        client.close()
        client2 = SqliteTableClient(str(db))
        s2 = OnboardingService(sp.registry(), store=OnboardingStore(client2), require_plan_approval=True)
        s2.hydrate(p.client_id)
        assert s2.get(hr, oid).plan_status is PlanStatus.SUBMITTED, "pending approval must survive a restart"
        s2.decide_plan(approver, request.request_id, DecisionOutcome.APPROVED)  # resume after the restart
        assert s2.get(hr, oid).plan_status is PlanStatus.APPROVED
        drafts = s2.prepare_team_drafts(hr, oid)
        client2.close()
        client3 = SqliteTableClient(str(db))
        s3 = OnboardingService(sp.registry(), store=OnboardingStore(client3), require_plan_approval=True)
        s3.hydrate(p.client_id)
        assert s3.get(hr, oid).plan_status is PlanStatus.APPROVED
        assert [d.draft_id for d in s3.prepare_team_drafts(hr, oid)] == [d.draft_id for d in drafts]
        client3.close()
        return "approval resumed after restart; plan, tasks and draft records recovered (local SQLite)"


def scenario_configuration_swap_does_not_touch_the_engine() -> str:
    s, oid, hr, *_ = _begin("uae_inside")
    real_like = sp.uae_config().model_copy(update={"version": "REAL-FUTURE-v1", "templates": sp.uae_config().templates[:3]})
    reg = sp.registry()
    reg.register(real_like)
    other = OnboardingService(reg)
    p = sp.PROFILES["uae_inside"]
    record = other.create_onboarding(_actors(p.client_id)[0], sp.trigger_for(p))
    assert len(record.tasks) < len(s.get(hr, oid).tasks) and not sp.is_synthetic(real_like)
    return "a different configuration produces a different plan through the same engine"


EXTENDED_SCENARIOS = [
    ("uae_inside_end_to_end", scenario_uae_inside_end_to_end),
    ("uae_overseas_dl09_route", scenario_uae_overseas_dl09),
    ("india_onboarding", scenario_india_onboarding),
    ("dl09_replan_on_location_status", scenario_dl09_replan_on_location_status),
    ("dl14_non_response_cycle", scenario_dl14_non_response_cycle),
    ("visa_delay_and_joining_risk", scenario_visa_delay_and_joining_risk),
    ("background_verification_delay", scenario_background_verification_delay_blocks_dependants),
    ("parallel_and_dependency_blocking", scenario_parallel_execution_and_dependency_blocking),
    ("team_drafts_and_send_gate", scenario_team_drafts_and_send_gate),
    ("assignment_gap_detection", scenario_assignment_gap_detection),
    ("fake_llm_profiles_and_safety", scenario_fake_llm_profiles_and_safety),
    ("tool_use_and_replan_proposal", scenario_agent_tool_selection_and_blocker_replan_proposal),
    ("duplicate_events_and_ownership", scenario_duplicate_events_and_ownership),
    ("unauthorized_actions_rejected", scenario_unauthorized_actions_rejected),
    ("interruption_resume_restart", scenario_interruption_resume_and_restart_recovery),
    ("configuration_swap", scenario_configuration_swap_does_not_touch_the_engine),
]


# ---------------------------------------------------------------------------
# The full integration flow (synthetic configuration, MOCK connectors). Reuses the helpers of the flow tests.
# ---------------------------------------------------------------------------


def _flow_helpers():
    import sys

    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from tests import test_onboarding_flow as h

    return h


def scenario_flow_acceptance_sequence() -> str:
    h = _flow_helpers()
    from onboarding_task_management.sub_agent import flow as fl
    from onboarding_task_management.sub_agent.connectors.outlook.mock import MockOutlookConnector

    flow, service, _ = h.make("uae_overseas")
    oid = flow.handle_employee_created(h.event(profile="uae_overseas"), h.TODAY).onboarding_id
    assert service.get(h.COORD, oid).config_version == sp.uae_config().version, "config retrieved and used"
    email = flow.prepare_hr_email(h.COORD, oid, h.TODAY)
    assert "fake_llm:" in email.body and "NOT an approval" in email.body
    request = service.submit_plan(h.COORD, oid)
    flow.authorise_message(h.LEAD, oid, "hrmail", email.email_id)
    assert flow.send_message(h.COORD, oid, "hrmail", email.email_id, email.subject, email.body, email.recipient_role,
                             MockOutlookConnector()) == "simulated_sent"
    assert service.get(h.COORD, oid).plan_status is PlanStatus.SUBMITTED, "an email is not an approval"
    assert flow.process_approval_callback(h.CLIENT, h.approve_cb(oid, request.request_id), h.NOW).status == "approved"
    h.verify_all(flow, oid)
    result = flow.trigger_downstream(h.COORD, oid, {fl.UC04: fl.MockTrigger(fl.UC04), fl.UC05: fl.MockTrigger(fl.UC05)})
    assert result == {fl.UC04: "triggered", fl.UC05: "triggered"}, result
    return "event -> config -> checklist+docs -> Fake LLM -> HR email -> signed approval -> gate -> UC-04/05 (all mocks)"


def scenario_document_gate_blocks_downstream() -> str:
    h = _flow_helpers()
    from onboarding_task_management.sub_agent import flow as fl
    from onboarding_task_management.contracts import DocumentState

    flow, service, _ = h.make()
    oid = h.approved(flow, service)
    a, b = fl.MockTrigger(fl.UC04), fl.MockTrigger(fl.UC05)
    assert flow.trigger_downstream(h.COORD, oid, {fl.UC04: a, fl.UC05: b}) == {
        fl.UC04: "waiting_for_documents", fl.UC05: "waiting_for_documents"}
    key = [d.key for d in service.get(h.COORD, oid).documents if d.mandatory][0]
    flow.apply_document_update(h.DOCS, h.doc_update(oid, key, DocumentState.SUBMITTED, 1, 1))
    flow.apply_document_update(h.DOCS, h.doc_update(oid, key, DocumentState.REJECTED, 2, 2))
    flow.trigger_downstream(h.COORD, oid, {fl.UC04: a, fl.UC05: b})
    assert (a.payloads, b.payloads) == ([], []), "a rejected mandatory document keeps both blocked"
    return "nothing, partial and rejected documents all keep UC-04 and UC-05 untriggered"


def scenario_hr_callback_security() -> str:
    h = _flow_helpers()
    from onboarding_task_management.contracts import Role

    flow, service, _ = h.make()
    oid, request = h.to_review(flow, service)
    codes = {
        "unsigned": flow.process_approval_callback(h.CLIENT, h.approve_cb(oid, request.request_id, sign=False), h.NOW).status,
        "unauthorised": flow.process_approval_callback(
            h.CLIENT, h.approve_cb(oid, request.request_id, role=Role.EMPLOYEE, cb_id="B"), h.NOW).status,
        "stale": flow.process_approval_callback(
            h.CLIENT, h.approve_cb(oid, request.request_id, version=9, cb_id="C"), h.NOW).status,
    }
    assert codes == {"unsigned": "invalid_signature", "unauthorised": "unauthorized", "stale": "stale_version"}, codes
    assert service.get(h.COORD, oid).plan_status is PlanStatus.SUBMITTED
    ok = h.approve_cb(oid, request.request_id, cb_id="D")
    assert flow.process_approval_callback(h.CLIENT, ok, h.NOW).status == "approved"
    assert flow.process_approval_callback(h.CLIENT, ok, h.NOW).status == "replayed"
    return "unsigned, unauthorised and stale callbacks refused; the valid one approves once and replays are inert"


def scenario_downstream_independence_and_ordering() -> str:
    h = _flow_helpers()
    from onboarding_task_management.contracts import DownstreamState
    from onboarding_task_management.sub_agent import flow as fl

    flow, service, _ = h.make()
    oid = h.approved(flow, service)
    h.verify_all(flow, oid)
    result = flow.trigger_downstream(h.COORD, oid, {fl.UC04: fl.MockTrigger(fl.UC04, raise_error=True),
                                                    fl.UC05: fl.MockTrigger(fl.UC05)})
    assert result == {fl.UC04: "failed", fl.UC05: "triggered"}, result
    flow.apply_downstream_status(h.UC05_A, h.event_status(oid, fl.UC05, DownstreamState.IN_PROGRESS, 1, 5))
    old = flow.apply_downstream_status(h.UC05_A, h.event_status(oid, fl.UC05, DownstreamState.ACCEPTED, 2, 1))
    assert old.code == "stale_ignored"
    assert flow.downstream_summary(h.COORD, oid) == {fl.UC04: "failed", fl.UC05: "in_progress"}
    return "UC-04 failing did not stop UC-05; an older status never replaced a newer one"


EXTENDED_SCENARIOS += [
    ("flow_acceptance_sequence", scenario_flow_acceptance_sequence),
    ("document_gate_blocks_downstream", scenario_document_gate_blocks_downstream),
    ("hr_callback_security", scenario_hr_callback_security),
    ("downstream_independence_and_ordering", scenario_downstream_independence_and_ordering),
]


def scenario_approval_to_team_assignments_and_held_downstream() -> str:
    h = _flow_helpers()
    from onboarding_task_management.errors import InvalidStateError
    from onboarding_task_management.sub_agent import flow as fl

    flow, service, _ = h.make()
    oid, request = h.to_review(flow, service)
    assert service.get(h.COORD, oid).assignments == [], "nothing is assigned before approval"
    assert flow.process_approval_callback(h.CLIENT, h.approve_cb(oid, request.request_id), h.NOW).status == "approved"
    record = service.get(h.COORD, oid)
    assert record.assignments and all(a.status == "prepared" for a in record.assignments)
    seen = [t.task_id for a in record.assignments for t in a.tasks]
    assert sorted(seen) == sorted(t.task_id for t in record.tasks), "every task in exactly one team's assignment"
    held = next(a for a in record.assignments if a.use_case == "UC-04")
    flow.authorise_assignment(h.LEAD, oid, held.assignment_id)
    try:
        flow.send_assignment(h.COORD, oid, held.assignment_id, __import__(
            "onboarding_task_management.sub_agent.connectors.outlook.mock", fromlist=["x"]).MockOutlookConnector())
    except InvalidStateError:
        return f"{len(record.assignments)} team assignments prepared on approval; UC-04 work held until documents verified"
    raise AssertionError("a UC-04 assignment must not be sent before the document gate opens")


EXTENDED_SCENARIOS += [("approval_to_team_assignments", scenario_approval_to_team_assignments_and_held_downstream)]
