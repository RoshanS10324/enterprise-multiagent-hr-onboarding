"""Dedicated regression suite for the security findings (S1, S2, S4, S5, S6) documented in
docs/ONBOARDING_COMMON_ARCHITECTURE.md Part B. (S3, forged acknowledgement evidence, no longer
applies: NDA/policy acknowledgement and signature tracking has been retired from this agent's
scope -- see README.)

Narrower assertions for some of these already live inside the per-service test files (updated
during migration); this file is the single place that proves each finding is closed end to end --
at the library level (HITLInbox/ToolGateway/identity provider directly) and, where relevant,
through the real HTTP app -- so a regression can't slip in silently while other tests are edited
for unrelated reasons. Tests use the in-memory fakes/mocks already established throughout this
project (FakeCalendar, MockOnboarding, FakeLLM, InsecureHeaderIdentityProvider) -- this is
offline/test verification, not proof of behavior against any real external provider.
"""

from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from fastapi.testclient import TestClient

from context_layer.knowledge.policy_index import PolicyKnowledgeBase
from hr_onboarding_common.contracts import Actor, Purpose
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import ApprovalItem, Decision, HITLApplyError, HITLError, HITLInbox
from hr_onboarding_common.platform.identity import IdentityError, InsecureHeaderIdentityProvider
from hr_onboarding_common.platform.policy import Policy, PolicyStore, PolicyViolation
from hr_onboarding_common.platform.tools import ToolGateway
from sub_agent import api
from sub_agent.agent import AGENT_HOME
from sub_agent.fakes import build_mock_onboarding, FakeCalendar, offline_llm
from sub_agent.models import OnboardingRecord
from sub_agent.tool_registry import OnboardingToolRegistry, TenantAuthorizationError

ACME = "CLIENT-ACME"
GLOBEX = "CLIENT-GLOBEX"
ACME_HEADERS = {"X-User-Id": "user:hr-admin-01", "X-User-Roles": "hr_admin", "X-Client-Id": ACME}
GLOBEX_HEADERS = {"X-User-Id": "user:hr-admin-02", "X-User-Roles": "hr_admin", "X-Client-Id": GLOBEX}
START = datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc)  # 09:00 Dubai on the joining date; matches test_api_routes.py START

_counter = count(1)


def _real_policy() -> Policy:
    return PolicyStore(AGENT_HOME / "guardrails").for_agent("employee-onboarding")


def _item(**overrides) -> ApprovalItem:
    base = dict(gate="comms_approval", approver_role="hr_admin", workflow="employee-onboarding",
               workflow_instance="i", agent="employee-onboarding", proposal_type="welcome_message",
               client_id=ACME, payload={}, rationale="r", confidence=0.9, run_id="run-1")
    base.update(overrides)
    return ApprovalItem(**base)


@pytest.fixture(scope="module")
def client():
    with TestClient(api.app) as c:
        yield c


@pytest.fixture
def fresh_employee_acme():
    """A brand-new CLIENT-ACME employee isolated from every other test file, written directly
    into the same MockOnboarding singleton the running API uses (same pattern as
    test_api_routes.py's `fresh_employee`, kept separate to avoid id collisions)."""
    n = next(_counter)
    employee_id = f"EMP-SEC-TEST-{n:03d}"
    api.ctx.connector.repository.create_onboarding_case(OnboardingRecord(
        employee_id=employee_id, full_name=f"Sec Tester {n}", email=f"sec.tester{n}@example.com",
        role_title="Software Engineer", department="Engineering", team="Platform", team_lead_id="TL-001",
        client_id=ACME, start_date=datetime(2026, 12, 1).date()))
    return employee_id


# =========================================================================== S1: cross-tenant approvals

def test_hitlinbox_get_across_tenants_is_indistinguishable_from_not_found():
    hitl = HITLInbox()
    item = hitl.submit(_item())
    with pytest.raises(KeyError):
        hitl.get(item.id, client_id=GLOBEX)
    assert hitl.get(item.id, client_id=ACME).id == item.id  # same-tenant access still works


def test_hitlinbox_list_is_scoped_at_the_query_not_by_post_filtering():
    hitl = HITLInbox()
    acme_item = hitl.submit(_item(workflow_instance="i1", run_id="run-1"))
    hitl.submit(_item(workflow_instance="i2", run_id="run-2", client_id=GLOBEX))

    acme_only = hitl.list(status=None, client_id=ACME)
    assert {i.id for i in acme_only} == {acme_item.id}


def test_hitlinbox_decide_across_tenants_is_rejected_even_knowing_the_id():
    hitl = HITLInbox()
    item = hitl.submit(_item())
    with pytest.raises(KeyError):
        hitl.decide(item.id, Decision.APPROVED, approver="attacker", approver_roles=["hr_admin"], client_id=GLOBEX)
    assert hitl.get(item.id, client_id=ACME).status == Decision.PENDING  # untouched


def test_http_cross_tenant_approval_get_is_404(client, fresh_employee_acme):
    booked = client.post(f"/v1/onboarding/{fresh_employee_acme}/sessions", headers=ACME_HEADERS, json={
        "session_type": "orientation", "start": START.isoformat(), "attendees": []}).json()
    approval_id = booked["approval"]["id"]

    r = client.get(f"/v1/approvals/{approval_id}", headers=GLOBEX_HEADERS)
    assert r.status_code == 404

    same_tenant = client.get(f"/v1/approvals/{approval_id}", headers=ACME_HEADERS)
    assert same_tenant.status_code == 200


def test_http_cross_tenant_approval_list_excludes_it(client, fresh_employee_acme):
    booked = client.post(f"/v1/onboarding/{fresh_employee_acme}/sessions", headers=ACME_HEADERS, json={
        "session_type": "orientation", "start": (START + timedelta(hours=1)).isoformat(), "attendees": []}).json()
    approval_id = booked["approval"]["id"]

    globex_list = client.get("/v1/approvals", headers=GLOBEX_HEADERS, params={"status": "pending"})
    assert approval_id not in {a["id"] for a in globex_list.json()}

    acme_list = client.get("/v1/approvals", headers=ACME_HEADERS, params={"status": "pending"})
    assert approval_id in {a["id"] for a in acme_list.json()}


def test_http_cross_tenant_approval_decision_is_404_and_leaves_it_pending(client, fresh_employee_acme):
    booked = client.post(f"/v1/onboarding/{fresh_employee_acme}/sessions", headers=ACME_HEADERS, json={
        "session_type": "orientation", "start": (START + timedelta(hours=2)).isoformat(), "attendees": []}).json()
    approval_id = booked["approval"]["id"]

    r = client.post(f"/v1/approvals/{approval_id}/decision", headers=GLOBEX_HEADERS, json={"decision": "approved"})
    assert r.status_code == 404

    still_pending = client.get(f"/v1/approvals/{approval_id}", headers=ACME_HEADERS)
    assert still_pending.json()["status"] == "pending"


# =========================================================================== S2: untrusted identity headers

def test_insecure_provider_disabled_by_default_rejects_everything():
    provider = InsecureHeaderIdentityProvider()
    assert provider.enabled is False
    with pytest.raises(IdentityError):
        provider.authenticate({"x-user-id": "user:x", "x-client-id": "CLIENT-X"})


def test_insecure_provider_enabled_still_requires_both_ids():
    provider = InsecureHeaderIdentityProvider(enabled=True)
    with pytest.raises(IdentityError):
        provider.authenticate({"x-user-roles": "hr_admin"})
    with pytest.raises(IdentityError):
        provider.authenticate({"x-user-id": "user:x"})  # no client_id


def test_insecure_provider_enabled_parses_roles_and_client_id_when_both_present():
    provider = InsecureHeaderIdentityProvider(enabled=True)
    actor = provider.authenticate({"x-user-id": "user:x", "x-user-roles": "hr_admin, team_lead",
                                   "x-client-id": "CLIENT-ACME"})
    assert actor.user_id == "user:x"
    assert actor.roles == ["hr_admin", "team_lead"]
    assert actor.client_id == "CLIENT-ACME"


def test_registry_construction_fails_closed_for_an_actor_with_no_verified_client_id():
    actor = Actor(on_behalf_of="user:x", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING, client_id=None)
    with pytest.raises(TenantAuthorizationError):
        OnboardingToolRegistry(port=build_mock_onboarding(), calendar=FakeCalendar(), kb=PolicyKnowledgeBase(),
                               llm=offline_llm(), policy=_real_policy(), hitl=HITLInbox(), audit=AuditLog(),
                               actor=actor)


# =========================================================================== S4: cross-tenant /v1/runs

def test_http_run_succeeds_for_the_actors_own_tenant(client):
    r = client.post("/v1/runs", headers=ACME_HEADERS,
                    json={"instance": "EMP-001", "input": {"employee_id": "EMP-001"}})
    assert r.status_code == 200
    assert r.json()["response"]["status"] in ("proposed", "acted", "exception")


def test_http_run_against_another_tenants_employee_is_404_not_executed(client):
    """EMP-101 belongs to CLIENT-GLOBEX; the caller authenticates as CLIENT-ACME. The
    _tenant_check hook must reject this before the graph ever runs (finding S4), with the same
    response as an unknown employee (Phase 4, finding F)."""
    before = len(api.ctx.connector.repository.list_communication_drafts("EMP-101"))

    r = client.post("/v1/runs", headers=ACME_HEADERS,
                    json={"instance": "EMP-101", "input": {"employee_id": "EMP-101"}})
    missing = client.post("/v1/runs", headers=ACME_HEADERS,
                          json={"instance": "EMP-NOPE", "input": {"employee_id": "EMP-NOPE"}})
    assert r.status_code == 404 and r.content == missing.content

    after = len(api.ctx.connector.repository.list_communication_drafts("EMP-101"))
    assert after == before  # no draft was created -- the run never executed


def test_http_run_against_unknown_employee_is_404(client):
    r = client.post("/v1/runs", headers=ACME_HEADERS,
                    json={"instance": "EMP-DOES-NOT-EXIST", "input": {"employee_id": "EMP-DOES-NOT-EXIST"}})
    assert r.status_code == 404


# =========================================================================== S5: approval action recovery

def test_decide_with_a_failing_listener_sets_apply_error_and_raises_distinctly():
    hitl = HITLInbox()
    item = hitl.submit(_item())
    hitl.subscribe(lambda decided: (_ for _ in ()).throw(RuntimeError("calendar provider outage")))

    with pytest.raises(HITLApplyError):
        hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"], client_id=ACME)

    # the human decision is real and durable -- not silently lost or rolled back
    stuck = hitl.get(item.id, client_id=ACME)
    assert stuck.status == Decision.APPROVED
    assert stuck.apply_error is not None and "calendar provider outage" in stuck.apply_error
    assert hitl.audit.find("hitl.apply_failed")


def test_retry_apply_recovers_once_the_underlying_fault_clears():
    hitl = HITLInbox()
    item = hitl.submit(_item())
    attempts = {"n": 0}

    def _flaky(decided):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient outage")

    hitl.subscribe(_flaky)
    with pytest.raises(HITLApplyError):
        hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"], client_id=ACME)
    assert hitl.get(item.id, client_id=ACME).apply_error is not None

    recovered = hitl.retry_apply(item.id, client_id=ACME)
    assert recovered.apply_error is None
    assert attempts["n"] == 2
    assert hitl.audit.find("hitl.apply_recovered")


def test_retry_apply_without_a_recorded_failure_is_rejected():
    hitl = HITLInbox()
    item = hitl.submit(_item())
    hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"], client_id=ACME)

    with pytest.raises(HITLError):
        hitl.retry_apply(item.id, client_id=ACME)  # already applied cleanly -- nothing to retry


def test_retry_apply_is_tenant_scoped():
    hitl = HITLInbox()
    item = hitl.submit(_item())
    hitl.subscribe(lambda decided: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(HITLApplyError):
        hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"], client_id=ACME)

    with pytest.raises(KeyError):
        hitl.retry_apply(item.id, client_id=GLOBEX)


def test_repeated_retry_apply_after_success_does_not_silently_replay_the_action():
    """Duplicate retries must be safe: once an apply succeeds, a second retry must refuse to run
    the listener again, not re-invoke it under the assumption that running it again is harmless."""
    hitl = HITLInbox()
    item = hitl.submit(_item())
    calls = {"n": 0}

    def _listener(decided):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first attempt fails")

    hitl.subscribe(_listener)
    with pytest.raises(HITLApplyError):
        hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"], client_id=ACME)

    hitl.retry_apply(item.id, client_id=ACME)
    assert calls["n"] == 2

    with pytest.raises(HITLError):
        hitl.retry_apply(item.id, client_id=ACME)
    assert calls["n"] == 2  # not invoked a third time


# =========================================================================== S6: kill-switch enforcement

def test_toolgateway_call_blocks_every_tool_when_disabled_and_never_invokes_it():
    policy = _real_policy().model_copy(update={"enabled": False})
    actor = Actor(on_behalf_of="user:x", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING, client_id=ACME)
    audit = AuditLog()
    gateway = ToolGateway(policy, audit, HITLInbox(), actor)
    calls = {"n": 0}

    def _tool():
        calls["n"] += 1
        return "did it"

    with pytest.raises(PolicyViolation, match="disabled"):
        gateway.call("onboarding.get_record", _tool)
    assert calls["n"] == 0  # the wrapped function was never invoked -- not merely "result ignored"
    assert audit.find("policy.denied")[0]["reason"] == "agent_disabled"


def test_toolgateway_call_works_normally_once_re_enabled():
    policy = _real_policy().model_copy(update={"enabled": False})
    actor = Actor(on_behalf_of="user:x", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING, client_id=ACME)
    gateway = ToolGateway(policy, AuditLog(), HITLInbox(), actor)
    with pytest.raises(PolicyViolation):
        gateway.call("onboarding.get_record", lambda: "x")

    gateway.policy = policy.model_copy(update={"enabled": True})
    assert gateway.call("onboarding.get_record", lambda: "x") == "x"


def test_http_kill_switch_blocks_onboarding_routes_not_only_v1_runs(client, fresh_employee_acme):
    """End-to-end: disabling the shared AgentContext's policy must stop /v1/onboarding/* too, not
    only /v1/runs -- this is the exact gap finding S6 documents and fixes."""
    api.ctx.policies.override("employee-onboarding", enabled=False)
    try:
        r = client.get(f"/v1/onboarding/{fresh_employee_acme}", headers=ACME_HEADERS)
        assert r.status_code == 403
    finally:
        api.ctx.policies.override("employee-onboarding", enabled=True)  # restore for every later test

    r2 = client.get(f"/v1/onboarding/{fresh_employee_acme}", headers=ACME_HEADERS)
    assert r2.status_code == 200
