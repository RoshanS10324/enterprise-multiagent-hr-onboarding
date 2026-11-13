"""UC04 configuration per client AND role (config/tenants.yaml `uc04_client_configs`).

UC04 scopes a CaseConfig to one client and one role and refuses any other (CONFIG_SCOPE_MISMATCH). The orchestrator
therefore looks the configuration up by the case's role code -- the case's role in UC04's code form
("Power BI Developer" -> "power_bi_developer") -- and never borrows another role's or client's: a role without one is
UC04_CONFIGURATION_MISSING for that role. The CLIENT-ALPHA files are synthetic development configuration
(config/uc04/README.md); these tests pin that every per-role entry really is scoped to its client and role, so a
mis-filed configuration is caught here rather than evaluated against the wrong role."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from _helpers import UC_ROOTS, require_uc, venv_python
from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import call_uc04_equipment_access, uc04_role_code
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state
from hr_onboarding_common.orchestration.tenant_config import TenantConfig, TenantSettings

TENANTS_YAML = Path(__file__).parents[2] / "config" / "tenants.yaml"
PLATFORM_CASE = "d3c1b8e2-6a4f-4f0e-9b7a-2c5d8e1f0a93"


class _NoWorker:
    def get(self, uc_id):
        raise AssertionError("UC04 must not be called")


def _state(role_id, client_id="CLIENT-ALPHA"):
    state = new_state(case_id=PLATFORM_CASE, client_id=client_id, correlation_id="corr-uc04-role",
                      actor={"on_behalf_of": "user:hr-admin-01", "roles": ["hr_onboarding_officer"]},
                      intake_trigger={},
                      uc02_role_context={"role_id": role_id, "department": "IT", "manager_id": "TL-TEST-001",
                                         "location": "Chennai", "project": "ISRP"})
    state["employee_id"] = "EMP-000123"
    state["joining_date"] = "2026-12-14"
    return state


def _deps(worker_pool=None):
    return OrchestratorDeps(ctx=None, worker_pool=worker_pool or _NoWorker(), uc03_repo_root=UC_ROOTS["uc03"],
                            tenants=TenantConfig.load(TENANTS_YAML))


@pytest.mark.parametrize(("role", "code"), [
    ("Power BI Developer", "power_bi_developer"), ("software_engineer", "software_engineer"),
    ("  Sr. Data-Engineer ", "sr_data_engineer"), ("", None), (None, None),
])
def test_the_case_role_becomes_a_uc04_role_code(role, code):
    assert uc04_role_code(role) == code


def test_every_per_role_configuration_is_scoped_to_its_own_client_and_role():
    config = TenantConfig.load(TENANTS_YAML)
    checked = 0
    for client_id, settings in config.tenants.items():
        for role, ref in (settings.uc04_client_configs or {}).items():
            data = json.loads(Path(ref).read_text(encoding="utf-8"))
            assert (data["client_id"], data["role_id"]) == (client_id, role), ref
            checked += 1
    assert checked >= 2  # CLIENT-ALPHA's software_engineer and power_bi_developer


def test_a_role_is_never_given_another_roles_configuration():
    config = TenantConfig.load(TENANTS_YAML)
    ref = config.uc04_config_ref_for("CLIENT-ALPHA", "power_bi_developer")
    assert ref.endswith("CLIENT-ALPHA.power_bi_developer.json")
    assert config.uc04_config_ref_for("CLIENT-ALPHA", "data_analyst") is None
    assert config.uc04_config_ref_for("CLIENT-BETA", "power_bi_developer") is None


def test_a_role_without_configuration_blocks_naming_that_role_without_calling_uc04():
    result = call_uc04_equipment_access(_state("Data Analyst"), _deps())
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.BLOCKED.value
    (blocker,) = result["blockers"]
    assert blocker["code"] == "UC04_CONFIGURATION_MISSING"
    assert "client CLIENT-ALPHA, role data_analyst" in blocker["reason"]
    assert blocker["required_from"] == "Client configuration owner / HR" and blocker["retryable"] is True


def test_a_role_that_is_no_role_code_is_a_named_hr_blocker():
    result = call_uc04_equipment_access(_state("3D Artist"), _deps())
    assert result["stage_errors"][Stage.EQUIPMENT_ACCESS.value]["code"] == "UC04_ROLE_CODE_INVALID"
    (blocker,) = result["blockers"]
    assert blocker["required_from"] == "HR onboarding officer" and blocker["required_data"] == "role_id"


def test_tenant_settings_refuse_a_role_key_that_is_no_role_code():
    with pytest.raises(ValidationError):
        TenantSettings(uc04_client_configs={"Power BI Developer": "x.json"})


@pytest.fixture
def platform_uc04(monkeypatch):
    require_uc("uc04")
    from hr_onboarding_common.orchestration.workers.config import WorkerConfig
    from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker

    monkeypatch.setenv("UC04_IDENTITY_PROFILE", "PLATFORM")
    worker = UcWorker(WorkerConfig(uc_id="uc04", python_executable=venv_python(UC_ROOTS["uc04"]),
                                   src_dir=UC_ROOTS["uc04"] / "src"))
    worker.start()
    yield worker
    worker.stop()


class _Pool:
    def __init__(self, worker):
        self.worker = worker

    def get(self, uc_id):
        return self.worker


def test_real_uc04_evaluates_a_client_alpha_case_against_its_roles_configuration(platform_uc04):
    state = _state("Power BI Developer")
    state["uc02_role_context"].update(requested_equipment=["laptop-standard"], requested_access=["email-teams-account"])
    result = call_uc04_equipment_access(state, _deps(_Pool(platform_uc04)))
    codes = {b["code"] for b in result["blockers"]}
    assert not codes & {"CONFIG_SCOPE_MISMATCH", "UC04_CONFIGURATION_MISSING", "NO_ITEMS_REQUESTED"}, result["blockers"]
    evaluated = result["equipment_access_result"]
    assert evaluated["role_code"] == "power_bi_developer"
    assert evaluated["config_version"] == "alpha-v1-client-alpha-dev"  # traceable to the synthetic dev configuration
    assert {"laptop-standard", "email-teams-account"} <= {i["item_id"] for i in evaluated["items"]}


def test_real_uc04_without_requested_items_asks_hr_and_recommends_the_roles_package(platform_uc04):
    result = call_uc04_equipment_access(_state("Power BI Developer"), _deps(_Pool(platform_uc04)))
    # Still HR's decision (DL-17): the stage waits, nothing was requested on HR's behalf.
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.BLOCKED.value
    assert result["equipment_access_result"]["items"] == []
    (blocker,) = [b for b in result["blockers"] if b["code"] == "NO_ITEMS_REQUESTED"]
    rec = blocker["recommendation"]  # UC04's skill recommend_role_standard_items, from this role's configuration
    assert rec["skill"] == "recommend_role_standard_items" and rec["source"] == "uc04" and rec["advisory_only"] is True
    assert (rec["role_code"], rec["config_version"]) == ("power_bi_developer", "alpha-v1-client-alpha-dev")
    assert rec["equipment"] == ["laptop-standard", "headset-basic"]
    assert rec["access"] == ["email-teams-account", "source-control-access", "ci-pipeline-access"]
    assert blocker["resolution"] == "provide_equipment_access_requests"


def test_a_recommendation_for_another_client_or_role_or_with_bad_ids_is_not_passed_on():
    from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import _recommendation

    payload = {"client_id": "CLIENT-ALPHA", "role_id": "power_bi_developer"}
    rec = {"skill": "recommend_role_standard_items", "client_id": "CLIENT-ALPHA", "role_id": "power_bi_developer",
           "config_version": "v", "equipment": ["laptop-standard", "Bad Item!"], "access": None, "gaps": []}
    assert _recommendation(rec, payload)["equipment"] == ["laptop-standard"]
    assert _recommendation(rec, payload)["access"] is None  # a MISSING list stays "no recommendation"
    assert _recommendation({**rec, "role_id": "software_engineer"}, payload) is None
    assert _recommendation({**rec, "client_id": "CLIENT-BETA"}, payload) is None
    assert _recommendation({**rec, "equipment": []}, payload) is None  # nothing to recommend
    assert _recommendation(None, payload) is None


def test_real_uc04_at_risk_with_items_awaiting_approval_says_who_approves_instead_of_a_bare_block(platform_uc04):
    # Praveena's case: role "Developer" (CLIENT-ALPHA's `developer` configuration), joining inside UC04's lead times.
    state = _state("Developer")
    state["joining_date"] = "2026-11-15"
    state["uc02_role_context"].update(requested_equipment=["laptop-standard", "headset-basic"],
                                      requested_access=["email-teams-account"])
    result = call_uc04_equipment_access(state, _deps(_Pool(platform_uc04)))
    assert result["equipment_access_result"]["role_code"] == "developer"
    if result["equipment_access_result"]["overall_status"] == "AT_RISK":  # depends on today's date vs lead times
        assert "AT_RISK" in {b["code"] for b in result["blockers"]}
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.AWAITING_APPROVAL.value
    (pending,) = [b for b in result["blockers"] if b["code"] == "UC04_APPROVALS_PENDING"]
    assert "hiring_manager" in pending["reason"] and "laptop-standard" in pending["reason"]
    assert pending["required_data"] == "approval_records" and pending["resolution"] == "decide_in_inbox"
    assert all(p["reference"] for p in result["pending_approvals"])  # a case-level proposal has a reference too


def test_a_reevaluation_with_new_input_gets_its_own_key_and_an_identical_retry_keeps_it():
    from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import _evaluation_key

    base, payload = "C1.equipment_access.v1", {"requested_equipment": []}
    assert _evaluation_key({}, base, payload)[0] == base  # the first evaluation: the base key itself
    key, _, digest = _evaluation_key({}, base, payload)
    first = {"equipment_access_result": {"idempotency_key": key, "idempotency_key_base": base, "payload_digest": digest}}
    assert _evaluation_key(first, base, payload)[0] == base  # same input: a replay under the same key
    changed = {"requested_equipment": [{"item_id": "laptop-standard"}]}
    new_key = _evaluation_key(first, base, changed)[0]
    assert new_key.startswith(f"{base}.") and new_key != base and len(new_key) <= 80  # UC04's key pattern
    assert _evaluation_key(first, f"{base}.jd2", changed)[0] == f"{base}.jd2"  # a new joining-date revision
    legacy = {"equipment_access_result": {"overall_status": "NEEDS_HUMAN_REVIEW"}}  # recorded before keys were kept
    assert _evaluation_key(legacy, base, changed)[0] != base


def test_real_uc04_reevaluates_after_hr_records_items_instead_of_an_idempotency_conflict(platform_uc04):
    # Praveena's case: UC04 first asked for items (NO_ITEMS_REQUESTED); HR recorded them; the same key was reused
    # with a different payload and UC04 refused it (IdempotencyConflict) -- the stage FAILED.
    deps = _deps(_Pool(platform_uc04))
    first = call_uc04_equipment_access(_state("Developer"), deps)
    assert "NO_ITEMS_REQUESTED" in {b["code"] for b in first["blockers"]}
    first["uc02_role_context"].update(requested_equipment=["laptop-standard"], requested_access=["email-teams-account"])
    second = call_uc04_equipment_access(first, deps)
    assert second["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] != StageStatus.FAILED.value, second.get("error_detail")
    assert second["equipment_access_result"]["idempotency_key"] != first["equipment_access_result"]["idempotency_key"]
    again = call_uc04_equipment_access(second, deps)  # an identical retry replays under the same key
    assert again["equipment_access_result"]["idempotency_key"] == second["equipment_access_result"]["idempotency_key"]
    assert again["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == second["stage_statuses"][Stage.EQUIPMENT_ACCESS.value]


def test_a_uc04_failure_is_recorded_on_the_stage_not_left_behind_an_older_error():
    from hr_onboarding_common.orchestration.contracts import WorkerResponse

    class Failing:
        def call(self, *args, **kwargs):
            return WorkerResponse(run_id="r", ok=False, error_code="INTERNAL_ERROR", error_type="IdempotencyConflict",
                                  error_detail="IdempotencyConflict: idempotency key reused with a different payload",
                                  retryable=False)

    state = _state("Developer")
    state["stage_errors"] = {Stage.EQUIPMENT_ACCESS.value: {"code": "UC04_CONFIGURATION_MISSING", "detail": "old",
                                                            "retryable": True}}
    result = call_uc04_equipment_access(state, _deps(_Pool(Failing())))
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.FAILED.value
    error = result["stage_errors"][Stage.EQUIPMENT_ACCESS.value]
    assert error["code"] == "INTERNAL_ERROR" and "IdempotencyConflict" in error["detail"]
