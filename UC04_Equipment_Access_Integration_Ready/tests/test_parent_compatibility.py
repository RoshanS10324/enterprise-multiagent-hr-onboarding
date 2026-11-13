"""Phase 2: what UC-04 needs to be called by the real parent. Identifier profiles, client-scoped lookup, role aliases, acknowledgement roles.

Every default stays as it was: with no profile, no alias and no acknowledgement roles configured, UC-04 behaves exactly as before. The
PLATFORM profile is process-wide and fixed at import, so it is tested in a fresh interpreter.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from helpers import ALL, Env, approvals, make, rid
from guardrails import identity
from sub_agent.adapters.case_context_mock import MockCaseContext
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.contracts import Uc04Input
from sub_agent.direct import DirectEntry
from sub_agent.ports import AdapterTimeout, ParentCase
from sub_agent.service import DuplicateCaseError, Uc04Service
from policy.policy import Policy, PolicyViolation

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = json.loads((ROOT / "policy" / "uc04_policy.json").read_text(encoding="utf-8"))


def policy_from(tmp_path, **changes) -> Policy:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({**DEFAULT_POLICY, **changes}), encoding="utf-8")
    return Policy.load(path)


def run_platform(script: str) -> dict:
    """Run `script` in a new interpreter with the PLATFORM identifier profile. It must print one JSON object."""
    code = "import sys\nsys.path[:0] = [r'%s', r'%s', r'%s']\nimport guardrails.identity as i\ni.configure('PLATFORM')\n" % (
        ROOT / "src", ROOT / "tests", ROOT) + textwrap.dedent(script)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert out.returncode == 0, out.stderr[-600:]
    return json.loads(out.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------------------------------------------------ identifier profiles
def test_the_default_profile_is_exactly_the_synthetic_patterns_that_always_applied():
    assert identity.LOCAL_SIMULATION.case_id == r"^SYN-CASE-\d{3,}$" and identity.LOCAL_SIMULATION.employee_ref == r"^SYN-EMP-\d{3,}$"
    assert identity.LOCAL_SIMULATION.user_ref == r"^SYN-USER-\d{3,}$" and identity.LOCAL_SIMULATION.role == r"^[a-z][a-z0-9_]{1,40}$"
    assert identity.LOCAL_SIMULATION.approval_id == r"^SYN-APR-\d{3,}$" and identity.LOCAL_SIMULATION.ticket_id == r"^SYN-TKT-\d{4,}$"
    from sub_agent import contracts
    assert contracts.CASE_ID == identity.LOCAL_SIMULATION.case_id and contracts.USER_REF == identity.LOCAL_SIMULATION.user_ref


def test_real_looking_identifiers_are_still_refused_by_default():
    for patch in ({"case_id": "3c88238c-5d89-4f7b-a3b9-92db4c4c98b7"}, {"employee_ref": "EMP-000001"}, {"client_id": "CLIENT-TEST-A"}):
        with pytest.raises(ValueError):
            Uc04Input.model_validate(make(**patch))


def test_an_unknown_profile_is_refused_and_the_value_is_not_echoed():
    with pytest.raises(ValueError) as e:
        identity.resolve("platfrom")
    assert "platfrom" not in str(e.value)
    assert identity.resolve(None) is identity.LOCAL_SIMULATION and identity.resolve(" platform ") is identity.PLATFORM


def test_the_profile_cannot_be_changed_once_the_contracts_are_loaded():
    identity.active()  # contracts are imported in this process, so the profile is frozen
    assert identity.configure("LOCAL_SIMULATION") is identity.LOCAL_SIMULATION  # the one already in force is fine
    with pytest.raises(RuntimeError):
        identity.configure("PLATFORM")


def test_the_platform_profile_accepts_platform_identifiers_end_to_end_and_still_refuses_free_text():
    out = run_platform('''
        import json
        from helpers import make, approvals, ALL
        from sub_agent.agent import Uc04Agent
        from sub_agent.adapters.case_context_mock import MockCaseContext
        from sub_agent.contracts import Uc04Input
        from sub_agent.ports import ParentCase
        CASE = "3c88238c-5d89-4f7b-a3b9-92db4c4c98b7"
        def payload(**extra):
            p = make(case_id=CASE, employee_ref="EMP-000001", client_id="CLIENT-TEST-A", **extra)
            p["config"]["client_id"] = "CLIENT-TEST-A"
            return p
        def accepted(patch):
            try:
                Uc04Input.model_validate(patch); return True
            except ValueError:
                return False
        cases = MockCaseContext([ParentCase(case_id=CASE, employee_ref="EMP-000001", client_id="CLIENT-TEST-A")])
        agent = Uc04Agent(cases=cases)
        result = agent.evaluate(payload(), actor_id="user:hr-admin-01", idempotency_key="k1")
        print(json.dumps({
            "evaluated": result.overall_status.value, "case": result.case_id == CASE,
            "synthetic_case_id_still_valid": accepted(make()),
            "email_as_actor": accepted(make(case_id=CASE, employee_ref="EMP-000001", client_id="CLIENT-TEST-A", requested_by="a@example.com")),
            "space_in_case_id": accepted(payload() | {"case_id": CASE + " x"}),
            "short_case_id": accepted(payload() | {"case_id": "1234"}),
        }))
    ''')
    assert out["evaluated"] == "AWAITING_APPROVAL" and out["case"] is True
    assert out["synthetic_case_id_still_valid"] is False  # PLATFORM is strict: SYN ids are not accepted there
    assert out["space_in_case_id"] is False and out["short_case_id"] is False


def test_the_platform_profile_never_accepts_an_email_address_as_an_actor():
    out = run_platform('''
        import json
        from sub_agent.contracts import CallerContext
        def ok(actor):
            try:
                CallerContext(invocation_mode="DIRECT", actor_id=actor, roles=["hr_onboarding_officer"], run_id="r1", idempotency_key="k1"); return True
            except ValueError:
                return False
        print(json.dumps({a: ok(a) for a in ["user:hr-admin-01", "svc.joining-day", "someone@example.com", "has space", ""]}))
    ''')
    assert out == {"user:hr-admin-01": True, "svc.joining-day": True, "someone@example.com": False, "has space": False, "": False}


# ------------------------------------------------------------------------------------------------------------ client-scoped lookup
def test_a_scoped_lookup_never_returns_another_clients_case():
    cases = MockCaseContext([ParentCase(case_id="SYN-CASE-001", employee_ref="SYN-EMP-001", client_id="client_alpha"),
                             ParentCase(case_id="SYN-CASE-002", employee_ref="SYN-EMP-001", client_id="client_bravo")])
    assert cases.case_for_employee("SYN-EMP-001", client_id="client_bravo") == "SYN-CASE-002"
    assert cases.case_for_employee("SYN-EMP-001", client_id="client_alpha") == "SYN-CASE-001"
    assert cases.case_for_employee("SYN-EMP-001", client_id="client_other") is None
    assert cases.case_for_employee("SYN-EMP-001") == "SYN-CASE-001"  # the legacy unscoped form, unchanged


def test_the_same_employee_number_in_two_clients_is_not_a_duplicate_and_does_not_leak_the_other_case():
    cases = MockCaseContext([ParentCase(case_id="SYN-CASE-001", employee_ref="SYN-EMP-001", client_id="client_alpha"),
                             ParentCase(case_id="SYN-CASE-002", employee_ref="SYN-EMP-001", client_id="client_bravo")])
    service = Uc04Service(cases=cases, it_desk=MockITServiceDesk())
    other_client = Uc04Input.model_validate(make(case_id="SYN-CASE-002", client_id="client_bravo", employee_ref="SYN-EMP-001"))
    assert service._resolve_case(other_client).case_id == "SYN-CASE-002"  # before: DuplicateCaseError naming the other tenant's case


def test_a_real_duplicate_inside_one_client_is_still_refused():
    cases = MockCaseContext([ParentCase(case_id="SYN-CASE-001", employee_ref="SYN-EMP-001", client_id="client_alpha"),
                             ParentCase(case_id="SYN-CASE-003", employee_ref="SYN-EMP-001", client_id="client_alpha")])
    service = Uc04Service(cases=cases, it_desk=MockITServiceDesk())
    inp = Uc04Input.model_validate(make(case_id="SYN-CASE-003", client_id="client_alpha", employee_ref="SYN-EMP-001"))
    with pytest.raises(DuplicateCaseError):
        service._resolve_case(inp)


# ------------------------------------------------------------------------------------------------------------ role aliases
def test_by_default_there_are_no_aliases_and_the_orchestrators_role_is_refused():
    policy = Policy.load()
    assert policy.role_aliases == {} and policy.acknowledge_roles is None
    with pytest.raises(PolicyViolation):
        policy.check_caller(["hr_admin"])
    policy.check_caller(["hr_onboarding_officer"])


def test_an_approved_alias_lets_exactly_that_role_name_act_as_the_allowed_role(tmp_path):
    policy = policy_from(tmp_path, role_aliases={"hr_admin": "hr_onboarding_officer"})
    policy.check_caller(["hr_admin"])
    with pytest.raises(PolicyViolation):
        policy.check_caller(["hr_admin_two"])  # no prefix or pattern matching
    with pytest.raises(PolicyViolation):
        policy.check_caller([])


def test_an_alias_can_only_point_at_an_allowed_role_and_cannot_redefine_one(tmp_path):
    with pytest.raises(ValueError):
        policy_from(tmp_path, role_aliases={"hr_admin": "super_user"})
    with pytest.raises(ValueError):
        policy_from(tmp_path, role_aliases={"hiring_manager": "onboarding_orchestrator"})


def test_the_kill_switch_beats_an_alias(tmp_path):
    policy = policy_from(tmp_path, role_aliases={"hr_admin": "hr_onboarding_officer"}, enabled=False)
    with pytest.raises(PolicyViolation):
        policy.check_caller(["hr_admin"])


def test_an_aliased_orchestrator_call_works_only_with_an_approved_alias(tmp_path):
    from sub_agent.entrypoints.orchestrated import Actor, OrchestratedEntry, OrchestratorRequest, WorkflowRef
    request = OrchestratorRequest(run_id="r1", idempotency_key="k1", workflow=WorkflowRef(id="o", instance="SYN-CASE-001", step="s"),
                                  actor=Actor(on_behalf_of="SYN-USER-100", roles=["hr_admin"]), input=make())
    refused = Env()
    with pytest.raises(PolicyViolation):
        refused.orch.evaluate(request)
    service = Env().service
    service.policy = policy_from(tmp_path, role_aliases={"hr_admin": "hr_onboarding_officer"})
    assert OrchestratedEntry(service).evaluate(request).status == "proposed"


# ------------------------------------------------------------------------------------------------------------ acknowledgement roles
def failing_submission(policy=None):
    env = Env(it_desk=MockITServiceDesk(fail_with=AdapterTimeout("x")))
    if policy is not None:
        env.service.policy = policy
    report = env.submit(make(approval_records=approvals(["201"])))
    assert report.failed
    return env


def test_by_default_any_allowed_caller_role_can_acknowledge_as_before():
    env = failing_submission()
    env.service.acknowledge_adapter_error("SYN-CASE-001", rid("201"), ["hiring_manager"])
    assert env.service.store.get("SYN-CASE-001").adapter_errors == {}


def test_when_acknowledgement_roles_are_set_only_those_roles_can_acknowledge(tmp_path):
    policy = policy_from(tmp_path, acknowledge_roles=["hr_onboarding_officer"])
    env = failing_submission(policy)
    for role in ("hiring_manager", "onboarding_orchestrator"):
        with pytest.raises(PolicyViolation):
            env.service.acknowledge_adapter_error("SYN-CASE-001", rid("201"), [role])
    assert env.service.store.get("SYN-CASE-001").adapter_errors  # still unresolved: no retry was permitted
    env.service.acknowledge_adapter_error("SYN-CASE-001", rid("201"), ["hr_onboarding_officer"])
    assert env.service.store.get("SYN-CASE-001").adapter_errors == {}


def test_acknowledgement_roles_must_be_allowed_caller_roles_and_aliases_do_not_grant_them(tmp_path):
    with pytest.raises(ValueError):
        policy_from(tmp_path, acknowledge_roles=["not_a_caller_role"])
    policy = policy_from(tmp_path, acknowledge_roles=["hr_onboarding_officer"], role_aliases={"hr_admin": "hiring_manager"})
    policy.check_caller(["hr_admin"])  # may call UC-04 (as hiring_manager) ...
    with pytest.raises(PolicyViolation):
        policy.check_acknowledger(["hr_admin"])  # ... but may not acknowledge
    DirectEntry(Env().service)  # constructing an entry point with a policy-carrying service still works
