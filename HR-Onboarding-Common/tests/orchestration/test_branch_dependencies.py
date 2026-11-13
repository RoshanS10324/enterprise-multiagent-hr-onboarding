"""UC04 / UC05 dependencies after the document gate: every missing dependency is a structured, resumable block that
names what is missing, who must provide it and how it is resolved -- never a synthetic success.

Agent: scripts/run_workflow_demo.build_synthetic_agent (real graph, gates and UC03); UC04 is the real adapter in front
of a request-aware double with the real worker's reply shape; UC05 is the real adapter in front of FakeUc05Worker
(_uc05_fakes, the real worker's reply shape). Identity: the server-side development registry."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import run_workflow_demo as demo  # noqa: E402

from _uc05_fakes import FakeUc05Worker, preflight  # noqa: E402
from hr_onboarding_common.orchestration.adapters._shared import add_blocker, with_stage_status  # noqa: E402
from hr_onboarding_common.orchestration.adapters.uc05_orientation import _set, call_uc05_orientation  # noqa: E402
from hr_onboarding_common.orchestration.api import create_orchestrator_app  # noqa: E402
from hr_onboarding_common.orchestration.contracts import WorkerResponse  # noqa: E402
from hr_onboarding_common.orchestration.deps import OrchestratorDeps  # noqa: E402
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state  # noqa: E402
from hr_onboarding_common.orchestration.tenant_config import TenantConfig  # noqa: E402
from hr_onboarding_common.orchestration.workers.config import WorkerConfig  # noqa: E402
from hr_onboarding_common.platform.identity import RegistryIdentityProvider  # noqa: E402

BASE = "/v1/orchestration/cases"
CLIENT = demo.CLIENT
REGISTRY = {
    "user:hr-admin-01": {"label": "HR Admin & Onboarding Officer", "roles": ["hr_admin", "hr_onboarding_officer"],
                         "clients": [CLIENT, "CLIENT-TEST-B"]},
    "user:hr-lead-01": {"label": "HR Lead", "roles": ["hr_lead"], "clients": [CLIENT, "CLIENT-TEST-B"]},
}
HR = {"X-User-Id": "user:hr-admin-01", "X-Client-Id": CLIENT}
LEAD = {"X-User-Id": "user:hr-lead-01", "X-Client-Id": CLIENT}
OTHER = {"X-User-Id": "user:hr-admin-01", "X-Client-Id": "CLIENT-TEST-B"}
TENANTS = Path(demo.COMMON) / "config" / "tenants.yaml"


class RequestAwareUc04:
    """UC04's reply shape (worker evaluate_orchestrated). Like the real UC04: no requested items -> BLOCKED with
    NO_ITEMS_REQUESTED; requested items -> READY with one item per request."""

    is_alive = True

    def __init__(self):
        self.inputs: list[dict] = []

    def call(self, op, payload, *, actor, run_id=None, idempotency_key=None, timeout_s=None) -> WorkerResponse:
        inp = payload["input"]
        self.inputs.append(inp)
        requested = [i["item_id"] for i in (*inp.get("requested_equipment", []), *inp.get("requested_access", []))]
        if not requested:
            result = {"case_id": inp["case_id"], "overall_status": "BLOCKED", "items": [], "blockers": [
                {"code": "NO_ITEMS_REQUESTED", "reason": "No equipment or access has been requested.",
                 "owner": "HR_OFFICER", "next_action": "Ask HR which equipment and access this joiner needs."}]}
        else:
            result = {"case_id": inp["case_id"], "overall_status": "READY", "blockers": [], "items": [
                {"request_id": f"REQ-{i}", "item_id": item, "state": "FULFILLED", "ticket_id": f"TKT-{i}"}
                for i, item in enumerate(requested)]}
        return WorkerResponse(run_id=run_id or "r", ok=True, result={"result": result, "proposals": []})


def world(*, tenants: bool, uc04=None, orientation=None):
    agent = demo.build_synthetic_agent(None)
    if tenants:
        agent.deps.tenants = TenantConfig.load(TENANTS)  # CLIENT-TEST-A's configured UC04 reference
    if uc04 is not None:
        agent.deps.worker_pool.workers["uc04"] = uc04
    if orientation is not None:
        agent.deps.stage_adapters[Stage.ORIENTATION_SCHEDULING.value] = orientation
        agent.graph = agent.build_graph()  # stage adapters are bound when the graph is built
    client = TestClient(create_orchestrator_app(agent=agent, identity_provider=RegistryIdentityProvider(REGISTRY)))
    return agent, client


def through_the_gate(client, body=None):
    """Form -> HR lead approval -> UC01/UC02/UC03 -> documents verified -> UC04 || UC05 dispatched."""
    body = body or {k: v for k, v in demo.START_BODY.items() if k != "uc04_client_config_ref"}
    view = client.post(BASE, json=body, headers=HR).json()
    view = client.post(f"{BASE}/{view['case_id']}/approvals/{view['form_approval']['approval_id']}/decision",
                       json={"decision": "approved"}, headers=LEAD).json()
    required = [d["document_type"] for d in view["document_collection"]["required_documents"]]
    view = client.post(f"{BASE}/{view['case_id']}/documents", json=demo._documents(required), headers=HR).json()
    reviews = [{"document_type": t, "decision": "approved"} for t, r in view["document_validation"].items()
               if r.get("classification") == "hr_review_required"]
    return client.post(f"{BASE}/{view['case_id']}/documents/reviews", json={"reviews": reviews}, headers=HR).json()


def blocker(view, stage, code):
    (found,) = [b for b in view["blockers"] if b["stage"] == stage and b["code"] == code]
    return found


# ------------------------------------------------------------------ structured blockers


def test_a_known_dependency_code_says_who_must_act_what_to_do_and_how():
    state = new_state(case_id="C", client_id=CLIENT, correlation_id="x", actor={}, intake_trigger={})
    add_blocker(state, stage=Stage.EQUIPMENT_ACCESS, code="UC04_CONFIGURATION_MISSING", reason="none", owner="hr_admin",
                next_action="provide")
    (b,) = state["blockers"]
    assert b["required_from"] and b["required_action"] and b["required_data"] == "uc04_equipment_access"
    assert b["dependency"] == "Approved client configuration for this client and role"
    assert b["resolution"] == "external" and b["retryable"] is True
    add_blocker(state, stage=Stage.EQUIPMENT_ACCESS, code="SOMETHING_ELSE", reason="r", owner="o", next_action="n")
    assert "required_from" not in state["blockers"][1]  # unknown codes keep their plain fields


# ------------------------------------------------------------------ UC04


def test_no_uc04_configuration_blocks_uc04_while_uc05_runs_independently_and_nothing_is_merged():
    agent, client = world(tenants=False)
    view = through_the_gate(client)
    assert view["stage_statuses"]["equipment_access"] == "blocked"
    b = blocker(view, "equipment_access", "UC04_CONFIGURATION_MISSING")
    assert b["required_from"] == "Client configuration owner / HR" and CLIENT in b["reason"]
    assert view["stage_statuses"]["orientation_scheduling"] == "complete"  # UC05 never waited for UC04
    assert view["branch_statuses"]["uc04_equipment_access"]["status"] == "blocked"
    assert view["stage_statuses"]["day_one_readiness"] == "pending" and view["workflow_status"] == "blocked"
    assert not view["pending_approvals"]  # nothing was approved or proposed on UC04's behalf


def test_once_the_configuration_exists_resume_reruns_only_uc04():
    agent, client = world(tenants=False)
    view = through_the_gate(client)
    case = view["case_id"]
    plan_attempt = view["stage_outcomes"]["plan_tasks"]["attempt"]
    uc05_finished = view["stage_outcomes"]["orientation_scheduling"]["finished_at"]
    agent.deps.tenants = TenantConfig.load(TENANTS)  # the client configuration became available
    resumed = client.post(f"{BASE}/{case}/resume", headers=HR).json()
    assert resumed["stage_statuses"]["equipment_access"] == "complete"
    assert resumed["stage_outcomes"]["plan_tasks"]["attempt"] == plan_attempt  # nothing upstream re-ran
    assert resumed["stage_outcomes"]["orientation_scheduling"]["finished_at"] == uc05_finished  # UC05 kept
    assert not [b for b in resumed["blockers"] if b["stage"] == "equipment_access"]
    assert resumed["stage_statuses"]["day_one_readiness"] == "awaiting_approval"  # merged; a person signs off


def test_missing_requests_are_a_resolvable_blocker_and_recording_them_reruns_uc04():
    uc04 = RequestAwareUc04()
    agent, client = world(tenants=True, uc04=uc04)
    view = through_the_gate(client)
    case = view["case_id"]
    b = blocker(view, "equipment_access", "NO_ITEMS_REQUESTED")
    assert b["resolution"] == "provide_equipment_access_requests" and b["required_from"] == "HR onboarding officer"
    url = f"{BASE}/{case}/equipment-access/requests"
    body = {"requested_equipment": ["laptop-standard"], "requested_access": ["email-teams-account"]}
    assert client.post(url, json=body, headers=LEAD).status_code == 403  # not a case operator
    assert client.post(url, json=body, headers=OTHER).status_code == 404  # another tenant
    assert client.post(url, json={"requested_equipment": ["Laptop Pro!"]}, headers=HR).status_code == 422
    assert client.post(url, json={}, headers=HR).status_code == 422
    resolved = client.post(url, json=body, headers=HR).json()
    assert resolved["stage_statuses"]["equipment_access"] == "complete"
    # UC04 RequestedItem objects, requested by the HR user who recorded them (never a placeholder identity)
    assert uc04.inputs[-1]["requested_equipment"] == [{"request_key": "EQ-laptop-standard",
                                                       "item_id": "laptop-standard", "item_kind": "EQUIPMENT",
                                                       "requested_by": "user:hr-admin-01"}]
    assert uc04.inputs[-1]["requested_access"][0]["item_kind"] == "ACCESS"
    record = resolved["equipment_access_requests"]
    assert record["recorded_by"] == "user:hr-admin-01" and record["requested_equipment"] == ["laptop-standard"]
    stored = agent.get_case(case, client_id=CLIENT)  # persisted, not only returned
    assert stored["equipment_access_result"]["overall_status"] == "READY"
    assert client.post(url, json=body, headers=HR).status_code == 409  # UC04 completed: not changed here


# ------------------------------------------------------------------ UC05


class _Pool:
    def __init__(self, worker):
        self.worker = worker

    def get(self, uc_id):
        return self.worker


def _uc05_state(case_id="CASE-1", event_id=None):
    state = new_state(case_id=case_id, client_id=CLIENT, correlation_id="corr",
                      actor={"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": CLIENT},
                      intake_trigger={"event_id": event_id} if event_id else {})
    state["employee_id"], state["joining_date"] = "EMP-TEST-001", "2026-12-01"
    return state


def test_a_missing_offer_event_is_a_structured_resolvable_blocker():
    deps = OrchestratorDeps(ctx=None, worker_pool=None, uc03_repo_root=None)
    result = call_uc05_orientation(_uc05_state(), deps)
    assert result["stage_statuses"]["orientation_scheduling"] == "blocked"
    (b,) = result["blockers"]
    assert b["code"] == "UC05_OFFER_EVENT_MISSING" and b["required_data"] == "offer_acceptance_event_id"
    assert b["resolution"] == "provide_offer_event" and b["retryable"] is True


def test_in_uc05_supabase_mode_the_case_is_the_event_and_a_missing_case_row_is_named():
    case_id = str(uuid.uuid4())
    worker = FakeUc05Worker(preflight_offer_event=lambda payload, actor: preflight(status="event_not_found",
                                                                                   event_id=payload["event_id"]))
    deps = OrchestratorDeps(ctx=None, worker_pool=_Pool(worker), uc03_repo_root=None, uc05_offer_event_source="case")
    result = call_uc05_orientation(_uc05_state(case_id=case_id), deps)
    assert worker.calls[0][0] == "preflight_offer_event" and worker.calls[0][1]["event_id"] == case_id
    b = blocker({"blockers": result["blockers"]}, "orientation_scheduling", "UC05_OFFER_EVENT_NOT_FOUND")
    assert "onboarding_cases" in b["required_action"] and b["retryable"] is True


def test_an_employee_missing_from_the_directory_is_a_structured_blocker():
    worker = FakeUc05Worker(preflight_offer_event=preflight(status="employee_not_in_directory", employee_id=None))
    deps = OrchestratorDeps(ctx=None, worker_pool=_Pool(worker), uc03_repo_root=None)
    result = call_uc05_orientation(_uc05_state(event_id="EVT-001"), deps)
    b = blocker({"blockers": result["blockers"]}, "orientation_scheduling", "EMPLOYEE_NOT_IN_UC05_DIRECTORY")
    assert b["required_from"] == "UC01 / employee directory integration" and b["resolution"] == "external"
    assert result["stage_statuses"]["orientation_scheduling"] == "blocked"


def _event_aware_orientation(state, deps):
    """UC05's orientation step: blocked without an offer-acceptance event, scheduled once it has one."""
    if not (state.get("intake_trigger") or {}).get("event_id"):
        return _set(state, Stage.ORIENTATION_SCHEDULING, StageStatus.BLOCKED, code="UC05_OFFER_EVENT_MISSING",
                    detail="no offer-acceptance event reference (offer_acceptance_event_id) for UC05")
    new = with_stage_status(state, Stage.ORIENTATION_SCHEDULING, StageStatus.COMPLETE)
    new["orientation_schedule_result"] = {"uc05_case": True, "session_id": "SES-1", "session_status": "confirmed"}
    return new


def test_recording_the_offer_event_reruns_uc05_and_is_refused_where_not_applicable():
    agent, client = world(tenants=True, orientation=_event_aware_orientation)
    body = {k: v for k, v in demo.START_BODY.items() if k not in ("offer_acceptance_event_id",)}
    view = through_the_gate(client, body)
    case = view["case_id"]
    assert blocker(view, "orientation_scheduling", "UC05_OFFER_EVENT_MISSING")["resolution"] == "provide_offer_event"
    equipment_finished = view["stage_outcomes"]["equipment_access"]["finished_at"]
    url = f"{BASE}/{case}/orientation/offer-event"
    assert client.post(url, json={"offer_acceptance_event_id": "EVT-001"}, headers=LEAD).status_code == 403
    assert client.post(url, json={"offer_acceptance_event_id": "EVT-001"}, headers=OTHER).status_code == 404
    assert client.post(url, json={"offer_acceptance_event_id": "bad id!"}, headers=HR).status_code == 422
    resolved = client.post(url, json={"offer_acceptance_event_id": "EVT-001"}, headers=HR).json()
    assert resolved["stage_statuses"]["orientation_scheduling"] == "complete"
    assert resolved["stage_outcomes"]["equipment_access"]["finished_at"] == equipment_finished  # UC04 not re-run
    agent.deps.uc05_offer_event_source = "case"
    second = client.post(BASE, json={**body, "offer_reference": "OFR-DEMO-777"}, headers=HR).json()
    assert client.post(f"{BASE}/{second['case_id']}/orientation/offer-event",
                       json={"offer_acceptance_event_id": "EVT-001"}, headers=HR).status_code == 409


# ------------------------------------------------------------------ identity and worker settings


def test_roles_come_from_the_server_side_registry_not_the_request():
    provider = RegistryIdentityProvider(REGISTRY)
    actor = provider.authenticate({"x-user-id": "user:hr-lead-01", "x-client-id": CLIENT, "x-user-roles": "hr_admin"})
    assert actor.roles == ["hr_lead"] and actor.auth_method == "dev-registry"  # the claimed role is ignored
    _, client = world(tenants=False)
    assert client.get(BASE, headers={"X-User-Id": "user:nobody", "X-Client-Id": CLIENT}).status_code == 401
    ungranted = {"X-User-Id": "user:hr-lead-01", "X-Client-Id": "CLIENT-NOT-GRANTED"}
    assert client.get(BASE, headers=ungranted).status_code == 401
    identities = client.get("/v1/orchestration/dev/identities").json()["identities"]
    assert {i["user_id"]: i["roles"] for i in identities}["user:hr-admin-01"] == ["hr_admin", "hr_onboarding_officer"]


def test_worker_scoped_settings_reach_only_their_worker(tmp_path):
    env = {"PATH": "x", "UC05_WORKER_ENV__UC05_DATABASE_MODE": "supabase", "UC05_WORKER_ENV__SUPABASE_URL": "https://db",
           "UC01_WORKER_ENV__X": "1"}
    uc05 = WorkerConfig(uc_id="uc05", python_executable=tmp_path, src_dir=tmp_path).child_environment(env)
    uc01 = WorkerConfig(uc_id="uc01", python_executable=tmp_path, src_dir=tmp_path).child_environment(env)
    assert uc05["UC05_DATABASE_MODE"] == "supabase" and uc05["SUPABASE_URL"] == "https://db" and "X" not in uc05
    assert "SUPABASE_URL" not in uc01 and "UC05_DATABASE_MODE" not in uc01 and uc01["X"] == "1"
    assert not [k for k in (*uc05, *uc01) if "_WORKER_ENV__" in k]


# ------------------------------------------------------------------ optional documents and the mandatory gate


def _submit_and_review(client, view, *, broken: str):
    """Supply every listed document with `broken` failing UC03's required-fields check, then an HR reviewer approves
    whatever awaits review."""
    case = view["case_id"]
    docs = demo._documents([d["document_type"] for d in view["document_collection"]["required_documents"]])
    for d in docs["documents"]:
        if d["document_type"] == broken:
            d["metadata"]["required_fields_present"] = False
    view = client.post(f"{BASE}/{case}/documents", json=docs, headers=HR).json()
    reviews = [{"document_type": t, "decision": "approved"} for t, r in view["document_validation"].items()
               if r.get("classification") == "hr_review_required"]
    if reviews:
        view = client.post(f"{BASE}/{case}/documents/reviews", json={"reviews": reviews}, headers=HR).json()
    return view


def _approved_case(client):
    body = {k: v for k, v in demo.START_BODY.items() if k != "uc04_client_config_ref"}
    view = client.post(BASE, json=body, headers=HR).json()
    return client.post(f"{BASE}/{view['case_id']}/approvals/{view['form_approval']['approval_id']}/decision",
                       json={"decision": "approved"}, headers=LEAD).json()


def test_nda_and_policy_acknowledgements_are_optional_in_the_plan():
    _, client = world(tenants=True)
    view = _approved_case(client)
    required = {d["document_type"]: d["required"] for d in view["document_collection"]["required_documents"]}
    assert required["nda"] is False and required["policy_acknowledgements"] is False
    assert required["passport"] is True
    outstanding = view["document_collection"]["outstanding"]
    assert "nda" not in [o["document_type"] if isinstance(o, dict) else o for o in outstanding]


def test_a_failing_optional_document_never_blocks_the_mandatory_gate():
    _, client = world(tenants=True)
    view = _submit_and_review(client, _approved_case(client), broken="nda")
    assert view["stage_statuses"]["validate_documents"] == "complete"
    assert view["stage_statuses"]["equipment_access"] == "complete"  # the gate opened; UC04 || UC05 ran
    (nda,) = [c for c in view["correction_requirements"] if c["document_type"] == "nda"]
    assert nda["detail"].startswith("optional document:")  # still visible to HR
    assert not [b for b in view["blockers"] if b["stage"] == "validate_documents"]


def test_a_failing_mandatory_document_still_blocks_the_gate():
    _, client = world(tenants=True)
    view = _submit_and_review(client, _approved_case(client), broken="passport")
    assert view["stage_statuses"]["validate_documents"] != "complete"
    assert view["stage_statuses"]["equipment_access"] == "pending"  # no branch before the gate
    assert [b for b in view["blockers"] if b["stage"] == "validate_documents" and "passport" in b["reason"]]


# ---------------------------------------------------------------------------------------------------- UC05 identity
def _claimed(case_id, *, client=CLIENT, employee="EMP-TEST-001", sent=True):
    other = _uc05_state(case_id=case_id)
    other["client_id"] = client
    other["employee_ref"] = {"employee_id": employee}
    if sent:
        other["orientation_schedule_result"] = {"uc05_case": True, "offer_event": {"event_id": "EVT-001"}}
    return other


def _deps_with(worker, *others):
    from hr_onboarding_common.orchestration.case_store import InMemoryCaseStateStore

    store = InMemoryCaseStateStore()
    for other in others:
        store[other["case_id"]] = other
    return OrchestratorDeps(ctx=None, worker_pool=_Pool(worker), uc03_repo_root=None, case_states=store)


def test_an_employee_id_another_case_already_took_into_uc05_is_never_sent_again():
    # UC05 keeps one record per (client, employee id): a second joiner under the same number would be merged into
    # the first one's induction, buddy and welcome. Nothing is sent to UC05 for this case.
    worker = FakeUc05Worker(preflight_offer_event=preflight())
    result = call_uc05_orientation(_uc05_state(case_id="CASE-2", event_id="EVT-001"),
                                   _deps_with(worker, _claimed("CASE-1")))
    assert result["stage_statuses"]["orientation_scheduling"] == "blocked" and worker.calls == []
    b = blocker({"blockers": result["blockers"]}, "orientation_scheduling", "EMPLOYEE_ID_NOT_UNIQUE")
    assert "CASE-1" in b["reason"] and b["required_from"] == "UC01 / HR system of record"
    assert b["required_data"] == "employee_id" and b["resolution"] == "external"


def test_the_same_number_in_another_client_or_in_a_case_that_never_reached_uc05_is_not_a_conflict():
    for other in (_claimed("CASE-1", client="CLIENT-OTHER"), _claimed("CASE-1", sent=False),
                  _claimed("CASE-1", employee="EMP-TEST-999")):
        worker = FakeUc05Worker(preflight_offer_event=preflight(status="event_not_found"))
        result = call_uc05_orientation(_uc05_state(case_id="CASE-2", event_id="EVT-001"), _deps_with(worker, other))
        assert worker.ops()[0] == "preflight_offer_event"
        assert "EMPLOYEE_ID_NOT_UNIQUE" not in {x["code"] for x in result["blockers"]}


def test_in_mailbox_mode_a_missing_event_says_the_mailbox_is_simulated_and_offers_no_retyping():
    worker = FakeUc05Worker(preflight_offer_event=preflight(status="event_not_found", event_id="Off-Act-006"))
    result = call_uc05_orientation(_uc05_state(event_id="Off-Act-006"), _deps_with(worker))
    b = blocker({"blockers": result["blockers"]}, "orientation_scheduling", "UC05_OFFER_EVENT_NOT_FOUND")
    assert "simulated" in b["reason"] and "Off-Act-006" in b["reason"]
    assert b["resolution"] == "external" and b["retryable"] is True
