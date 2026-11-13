import pytest

from sub_agent.adapters.memory import InMemoryAuditLog
from sub_agent.errors import CaseNotFoundError, PolicyViolationCode, PolicyViolationError
from sub_agent.models import Approval, Checkpoint, Role
from sub_agent.policy import authorize_action, authorize_decision, load_policy

from factories import make_case, make_policy, make_viewer

ALL_ROLES = list(Role)

ACTION_ALLOWED_ROLES = {
    "compare_sources": {Role.HR_OFFICER, Role.HR_MANAGER},
    "propose_form_change": {Role.HR_OFFICER, Role.HR_MANAGER, Role.CLIENT_USER},
    "prepare_hris": {Role.HR_OFFICER, Role.HR_MANAGER},
    "execute_hris_write": {Role.HR_MANAGER},
}

DECISION_ALLOWED_ROLES = {
    Checkpoint.HC_01_FORM: {Role.HR_MANAGER, Role.CLIENT_USER},
    Checkpoint.HC_04_DATA: {Role.HR_OFFICER, Role.HR_MANAGER},
    Checkpoint.HC_05_HRIS_WRITE: {Role.HR_MANAGER},
    Checkpoint.HC_12_VARIANT: {Role.HR_OFFICER, Role.HR_MANAGER},
}

VALID_TOML = """
autonomy_level = "L1"

[decisions.HC_01_FORM]
roles = ["HR_MANAGER", "CLIENT_USER"]
separation_of_duties = true

[decisions.HC_04_DATA]
roles = ["HR_OFFICER", "HR_MANAGER"]
separation_of_duties = false

[decisions.HC_05_HRIS_WRITE]
roles = ["HR_MANAGER"]
separation_of_duties = true

[decisions.HC_12_VARIANT]
roles = ["HR_OFFICER", "HR_MANAGER"]
separation_of_duties = false

[actions.compare_sources]
roles = ["HR_OFFICER", "HR_MANAGER"]

[actions.propose_form_change]
roles = ["HR_OFFICER", "HR_MANAGER", "CLIENT_USER"]

[actions.prepare_hris]
roles = ["HR_OFFICER", "HR_MANAGER"]

[actions.execute_hris_write]
roles = ["HR_MANAGER"]
"""


def test_shipped_policy_loads():
    policy = load_policy()
    assert policy.autonomy_level == "L1"
    assert set(policy.actions) == set(ACTION_ALLOWED_ROLES)
    assert set(policy.decisions) == set(DECISION_ALLOWED_ROLES)


def test_toml_missing_hc05_fails_at_load(tmp_path):
    text = VALID_TOML.replace(
        '[decisions.HC_05_HRIS_WRITE]\nroles = ["HR_MANAGER"]\nseparation_of_duties = true\n\n',
        "",
    )
    path = tmp_path / "policy.toml"
    path.write_text(text)

    with pytest.raises(ValueError):
        load_policy(path)


def test_unknown_role_name_fails_at_load(tmp_path):
    text = VALID_TOML.replace('roles = ["HR_MANAGER"]\nseparation_of_duties = true', 'roles = ["SUPERUSER"]\nseparation_of_duties = true', 1)
    path = tmp_path / "policy.toml"
    path.write_text(text)

    with pytest.raises(ValueError):
        load_policy(path)


def test_empty_roles_list_fails_at_load(tmp_path):
    text = VALID_TOML.replace('roles = ["HR_MANAGER"]\nseparation_of_duties = true', "roles = []\nseparation_of_duties = true", 1)
    path = tmp_path / "policy.toml"
    path.write_text(text)

    with pytest.raises(ValueError):
        load_policy(path)


@pytest.mark.parametrize("action", sorted(ACTION_ALLOWED_ROLES))
def test_action_allows_exactly_listed_roles(action):
    policy = make_policy()
    allowed = ACTION_ALLOWED_ROLES[action]

    for role in ALL_ROLES:
        audit_log = InMemoryAuditLog()
        viewer = make_viewer(role=role, user_id=f"user-{role.value.lower()}")

        if role in allowed:
            authorize_action(policy, viewer, action, audit_log)
            assert audit_log.entries() == []
        else:
            with pytest.raises(PolicyViolationError) as exc_info:
                authorize_action(policy, viewer, action, audit_log)
            assert exc_info.value.code == PolicyViolationCode.ROLE_NOT_ALLOWED

            denied = [e for e in audit_log.entries() if e.action == "ACCESS_DENIED"]
            assert len(denied) == 1
            assert denied[0].case_id is None


@pytest.mark.parametrize("checkpoint", sorted(DECISION_ALLOWED_ROLES, key=lambda c: c.value))
def test_decision_allows_exactly_listed_roles(checkpoint):
    policy = make_policy()
    allowed = DECISION_ALLOWED_ROLES[checkpoint]
    case = make_case(hr_officer_id="hr-officer-001")
    approval = Approval(
        case_id=case.case_id,
        checkpoint=checkpoint,
        subject_ref="x",
        requested_to="someone",
    )

    for role in ALL_ROLES:
        audit_log = InMemoryAuditLog()
        user_id = "hr-officer-001" if role == Role.HR_OFFICER else f"user-{role.value.lower()}"
        viewer = make_viewer(role=role, user_id=user_id, client_ids=frozenset({case.client_id}))

        if role in allowed:
            authorize_decision(policy, viewer, checkpoint, case, approval, audit_log)
            assert audit_log.entries() == []
        else:
            with pytest.raises(PolicyViolationError) as exc_info:
                authorize_decision(policy, viewer, checkpoint, case, approval, audit_log)
            assert exc_info.value.code == PolicyViolationCode.ROLE_NOT_ALLOWED

            denied = [e for e in audit_log.entries() if e.action == "ACCESS_DENIED"]
            assert len(denied) == 1
            assert denied[0].case_id is None


def test_hr_officer_not_assigned_gets_not_assigned_officer():
    policy = make_policy()
    case = make_case(hr_officer_id="hr-officer-001")
    approval = Approval(
        case_id=case.case_id,
        checkpoint=Checkpoint.HC_04_DATA,
        subject_ref="x",
        requested_to="someone",
    )
    audit_log = InMemoryAuditLog()
    viewer = make_viewer(role=Role.HR_OFFICER, user_id="hr-officer-999", client_ids=frozenset({case.client_id}))

    with pytest.raises(PolicyViolationError) as exc_info:
        authorize_decision(policy, viewer, Checkpoint.HC_04_DATA, case, approval, audit_log)
    assert exc_info.value.code == PolicyViolationCode.NOT_ASSIGNED_OFFICER

    entries = [e for e in audit_log.entries() if e.action == "POLICY_DENIED"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.case_id == case.case_id
    assert set(entry.after.keys()) == {"code", "scope", "role", "policy_version"}
    assert entry.after["scope"] == Checkpoint.HC_04_DATA.value


def test_self_approval_denied_and_different_approver_succeeds():
    policy = make_policy()
    case = make_case()
    approval = Approval(
        case_id=case.case_id,
        checkpoint=Checkpoint.HC_01_FORM,
        subject_ref="form:x",
        requested_to="hr-manager-001",
        requested_by="hr-manager-001",
    )
    audit_log = InMemoryAuditLog()
    same_viewer = make_viewer(role=Role.HR_MANAGER, user_id="hr-manager-001", client_ids=frozenset({case.client_id}))

    with pytest.raises(PolicyViolationError) as exc_info:
        authorize_decision(policy, same_viewer, Checkpoint.HC_01_FORM, case, approval, audit_log)
    assert exc_info.value.code == PolicyViolationCode.SELF_APPROVAL

    entries = [e for e in audit_log.entries() if e.action == "POLICY_DENIED"]
    assert len(entries) == 1
    assert entries[0].case_id == case.case_id

    other_viewer = make_viewer(role=Role.HR_MANAGER, user_id="hr-manager-002", client_ids=frozenset({case.client_id}))
    authorize_decision(policy, other_viewer, Checkpoint.HC_01_FORM, case, approval, InMemoryAuditLog())


def test_decision_for_invisible_case_raises_case_not_found():
    policy = make_policy()
    case = make_case(client_id="client-aaa")
    approval = Approval(
        case_id=case.case_id,
        checkpoint=Checkpoint.HC_01_FORM,
        subject_ref="form:x",
        requested_to="hr-manager-001",
    )
    audit_log = InMemoryAuditLog()
    viewer = make_viewer(role=Role.HR_MANAGER, user_id="hr-manager-001", client_ids=frozenset({"client-other"}))

    with pytest.raises(CaseNotFoundError):
        authorize_decision(policy, viewer, Checkpoint.HC_01_FORM, case, approval, audit_log)
