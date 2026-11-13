"""Phase 4 (UC-04 side): submission that never assumes success, and the operations a parent needs to recover.

`JournalRepository` is an in-memory store that also keeps a "durable image" captured only at commit points. `restart()` rebuilds a store from
that image, which is exactly what a database-backed store would give a new process after a crash. Synthetic, in-memory, no network.
"""

import copy

import pytest

from helpers import ALL, Env, approvals, codes, make, rid
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.agent import Uc04Agent
from sub_agent.direct import DirectEntry
from sub_agent.entrypoints.orchestrated import Actor, OrchestratorRequest, WorkflowRef
from sub_agent.fixtures import CALLER_ID, CALLER_ROLES, fixed_clock, new_environment
from sub_agent.audit import AuditLog
from sub_agent.orchestrated import CaseOperationRequest
from sub_agent.policy import PolicyViolation
from sub_agent.ports import AdapterTimeout
from sub_agent.service import SUBMISSION_IN_DOUBT, CaseMismatchError, UnknownCaseError, Uc04Service, Uc04ValidationError
from sub_agent.store import CaseStore

ITEM = rid("201")
PAYLOAD = lambda: make(approval_records=approvals(["201"]))  # noqa: E731  one fully approved item


class JournalRepository:
    def __init__(self, fail_commit_on: set[int] | None = None):
        self.inner = CaseStore()
        self.image: dict = {}
        self.image_version = 0
        self.commits = 0
        self.fail_commit_on = fail_commit_on or set()

    version = property(lambda self: self.inner.version)
    case_ids = property(lambda self: self.inner.case_ids)

    def get(self, case_id): return self.inner.get(case_id)
    def ensure(self, *a): return self.inner.ensure(*a)
    def touch(self): self.inner.touch()
    def snapshot(self, case_id): return self.inner.snapshot(case_id)
    def restore(self, case_id, snapshot): self.inner.restore(case_id, snapshot)
    def version_for(self, case_id): return self.inner.version_for(case_id)
    def begin(self, case_id): pass
    def rollback(self, case_id): pass
    def request_ids(self, case_id): return self.inner.request_ids(case_id)
    def all_request_ids(self): return self.inner.all_request_ids()

    def commit(self, case_id):
        self.commits += 1
        if self.commits in self.fail_commit_on:
            raise OSError("database unavailable")
        self.image, self.image_version = copy.deepcopy(self.inner._cases), self.inner.version

    def restart(self) -> CaseStore:
        store = CaseStore()
        store._cases, store.version = copy.deepcopy(self.image), self.image_version
        return store


class Crash(BaseException):
    """The process dies: not an Exception, so nothing in UC-04 can catch it and tidy up."""


class InspectingDesk(MockITServiceDesk):
    """Records what the *durable* state said at the moment the request left the system."""

    def __init__(self, repo=None, *, die=False, **kw):
        super().__init__(**kw)
        self.repo, self.die, self.seen = repo, die, []

    def submit_request(self, **kw):
        record = self.repo.image.get("SYN-CASE-001") if self.repo else None
        self.seen.append(dict(record.adapter_errors) if record else None)
        ticket = super().submit_request(**kw)
        if self.die:
            raise Crash()
        return ticket


def build(repo, desk):
    _, cases, desk = new_environment(it_desk=desk)
    service = Uc04Service(cases=cases, it_desk=desk, audit=AuditLog(fixed_clock), store=repo)
    return service, DirectEntry(service)


def submit(direct, payload=None):
    return direct.submit_eligible(payload or PAYLOAD(), actor_id=CALLER_ID, roles=CALLER_ROLES)


# ----------------------------------------------------------------------------------------------------------- uncertain outcomes
def test_the_intent_is_durable_before_the_request_leaves_the_system():
    repo = JournalRepository()
    desk = InspectingDesk(repo)
    _, direct = build(repo, desk)
    report = submit(direct)
    assert desk.seen == [{ITEM: SUBMISSION_IN_DOUBT}]  # at the moment of the call, the durable state already said "in doubt"
    assert report.submitted == [ITEM] and report.failed == []


def test_success_records_the_ticket_and_clears_the_mark_durably():
    repo = JournalRepository()
    _, direct = build(repo, InspectingDesk(repo))
    submit(direct)
    durable = repo.image["SYN-CASE-001"]
    assert durable.adapter_errors == {} and durable.items["SYN-REQ-201"].ticket_id and durable.items["SYN-REQ-201"].submission_attempts == 1


def test_a_failed_call_is_recorded_durably_with_the_error_class_and_nothing_else():
    repo = JournalRepository()
    _, direct = build(repo, InspectingDesk(repo, fail_with=AdapterTimeout("upstream detail ZX-4471-LEAK")))
    report = submit(direct)
    assert report.failed == [{"request_id": ITEM, "error": "AdapterTimeout"}]
    assert repo.image["SYN-CASE-001"].adapter_errors == {ITEM: "AdapterTimeout"}
    assert "ZX-4471" not in str(repo.image["SYN-CASE-001"].adapter_errors)


def test_a_crash_after_the_call_never_sends_the_request_again_without_a_person():
    repo = JournalRepository()
    desk = InspectingDesk(repo, die=True)
    _, direct = build(repo, desk)
    with pytest.raises(Crash):
        submit(direct)
    assert len(desk.calls) == 1  # it left the system, and the answer was lost
    # a new process starts from what was durable
    desk.die = False
    service, direct = build(repo := _reborn(repo), desk)
    assert repo.get("SYN-CASE-001").adapter_errors == {ITEM: SUBMISSION_IN_DOUBT}
    again = submit(direct)
    assert again.submitted == [] and again.skipped == [{"request_id": ITEM, "reason": "PREVIOUS_ADAPTER_ERROR_UNRESOLVED"}]
    assert len(desk.calls) == 1  # still exactly one: the request was NOT sent again
    assert again.result.overall_status.value == "NEEDS_HUMAN_REVIEW" and "ADAPTER_ERROR" in codes(again.result.blockers)
    # only an authorised person can release it; then it is sent once more
    with pytest.raises(PolicyViolation):
        service.acknowledge_adapter_error("SYN-CASE-001", ITEM, ["not_allowed"])
    assert service.acknowledge_adapter_error("SYN-CASE-001", ITEM, CALLER_ROLES) is True
    final = submit(direct)
    assert final.submitted == [ITEM] and len(desk.calls) == 2
    assert [i.submission_attempts for i in final.result.items if i.request_id == ITEM] == [2]


def _reborn(repo: JournalRepository) -> JournalRepository:
    fresh = JournalRepository()
    fresh.inner = repo.restart()
    fresh.image, fresh.image_version = copy.deepcopy(fresh.inner._cases), fresh.inner.version
    return fresh


def test_if_the_intent_cannot_be_made_durable_nothing_is_sent():
    repo = JournalRepository(fail_commit_on={2})  # commit 1 is the evaluation; commit 2 is the intent
    desk = InspectingDesk(repo)
    _, direct = build(repo, desk)
    with pytest.raises(OSError):
        submit(direct)
    assert desk.calls == []


def test_if_the_outcome_cannot_be_made_durable_the_item_stays_unresolved_after_a_restart():
    repo = JournalRepository(fail_commit_on={3})  # commit 3 is the outcome of the first item
    desk = InspectingDesk(repo)
    _, direct = build(repo, desk)
    with pytest.raises(OSError):
        submit(direct)
    assert len(desk.calls) == 1
    service, direct = build(_reborn(repo), desk)
    assert service.store.get("SYN-CASE-001").adapter_errors == {ITEM: SUBMISSION_IN_DOUBT}
    assert submit(direct).submitted == [] and len(desk.calls) == 1


def test_the_default_in_memory_flow_is_unchanged_and_leaves_no_mark_behind():
    env = Env()
    report = env.submit(make(approval_records=approvals(ALL)))
    assert sorted(report.submitted) == sorted(rid(k) for k in ALL) and len(env.desk.calls) == 4
    assert env.service.store.get("SYN-CASE-001").adapter_errors == {}
    assert env.submit(make(approval_records=approvals(ALL))).submitted == []  # nothing is sent twice


# ----------------------------------------------------------------------------------------------------------- orchestrated operations
def op(case_id="SYN-CASE-001", *, client="client_alpha", request_id=None, roles=None, version="1.0"):
    return CaseOperationRequest(contract_version=version, run_id="op-run-1", workflow=WorkflowRef(id="onboarding", instance=case_id, step="equipment_access"),
                                actor=Actor(on_behalf_of=CALLER_ID, roles=roles or ["hr_onboarding_officer"]), client_id=client, request_id=request_id)


def failed_agent(**policy_changes):
    agent = Uc04Agent(it_desk=MockITServiceDesk(fail_with=AdapterTimeout("x")))
    report = agent.submit_eligible(PAYLOAD())
    assert report.failed
    return agent


def test_an_orchestrated_submission_returns_the_same_decision_as_the_direct_one():
    parent = Uc04Agent()
    req = OrchestratorRequest(run_id="r", idempotency_key="k", workflow=WorkflowRef(id="o", instance="SYN-CASE-001", step="s"),
                              actor=Actor(on_behalf_of=CALLER_ID, roles=["hr_onboarding_officer"]), input=make(approval_records=approvals(ALL)))
    response = parent.submit_orchestrated(req)
    direct = Uc04Agent().submit_eligible(make(approval_records=approvals(ALL)))
    assert response.status == "proposed" and response.result.business_decision() == direct.result.business_decision()


def test_the_status_operation_is_read_only_and_reports_the_unresolved_item():
    agent = failed_agent()
    before = agent.service.store.version
    view = agent.status_orchestrated(op())
    assert agent.service.store.version == before  # nothing changed
    (item,) = [i for i in view.items if i.request_id == ITEM]
    assert (item.adapter_error, item.submission_attempts, item.ticket_id, item.state.value) == ("AdapterTimeout", 1, None, "ELIGIBLE_FOR_SIMULATED_SUBMISSION")
    assert view.parent_status == "ACTIVE" and view.client_id == "client_alpha"
    assert "status.read" in [e["event_type"] for e in agent.service.audit.events]


def test_the_status_operation_never_reveals_another_clients_case():
    agent = failed_agent()
    with pytest.raises(UnknownCaseError):
        agent.status_orchestrated(op(client="client_bravo"))
    with pytest.raises(UnknownCaseError):
        agent.status_orchestrated(op(case_id="SYN-CASE-404"))
    with pytest.raises(PolicyViolation):
        agent.status_orchestrated(op(roles=["not_allowed"]))
    with pytest.raises(Uc04ValidationError):
        agent.status_orchestrated(op(version="2.0"))


def test_acknowledgement_over_the_orchestrated_path_releases_exactly_one_request():
    agent = failed_agent()
    out = agent.acknowledge_orchestrated(op(request_id=ITEM))
    assert (out.acknowledged, out.case_id, out.request_id, out.run_id) == (True, "SYN-CASE-001", ITEM, "op-run-1")
    assert agent.acknowledge_orchestrated(op(request_id=ITEM)).acknowledged is False  # nothing left to acknowledge
    agent.it_desk.fail_with = None  # the desk is healthy again
    assert agent.submit_eligible(PAYLOAD()).submitted == [ITEM]


def test_acknowledgement_is_authorised_scoped_and_validated():
    agent = failed_agent()
    with pytest.raises(PolicyViolation):
        agent.acknowledge_orchestrated(op(request_id=ITEM, roles=["not_allowed"]))
    with pytest.raises(UnknownCaseError):
        agent.acknowledge_orchestrated(op(request_id=ITEM, client="client_bravo"))  # another tenant's case looks like no case
    with pytest.raises(Uc04ValidationError):
        agent.acknowledge_orchestrated(op(request_id=None))
    assert agent.service.store.get("SYN-CASE-001").adapter_errors  # nothing was cleared by the refused calls


def test_acknowledgement_roles_from_the_policy_apply_to_the_orchestrated_path_too(tmp_path):
    import json
    from pathlib import Path
    from policy.policy import Policy
    base = json.loads((Path(__file__).resolve().parents[1] / "policy" / "uc04_policy.json").read_text(encoding="utf-8"))
    path = tmp_path / "p.json"
    path.write_text(json.dumps({**base, "acknowledge_roles": ["hr_onboarding_officer"]}), encoding="utf-8")
    agent = failed_agent()
    agent.service.policy = Policy.load(path)
    with pytest.raises(PolicyViolation):
        agent.acknowledge_orchestrated(op(request_id=ITEM, roles=["onboarding_orchestrator"]))
    assert agent.acknowledge_orchestrated(op(request_id=ITEM, roles=["hr_onboarding_officer"])).acknowledged is True


def test_a_status_for_a_case_with_no_stored_state_is_empty_not_an_error():
    view = Uc04Agent().status_orchestrated(op())
    assert view.items == [] and view.parent_status == "ACTIVE"
