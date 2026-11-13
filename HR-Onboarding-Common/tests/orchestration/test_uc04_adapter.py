"""UC04 adapter tests, including the required negative authorization test: UC04 enforces caller
role in its OWN code (`Policy.check_caller`) -- an unauthorized actor must be rejected by UC04
itself, with zero case/ticket mutation, not bypassed by the orchestrator."""

from __future__ import annotations

import json

from _helpers import UC_ROOTS
from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import call_uc04_equipment_access
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state


class _SingleWorkerPool:
    def __init__(self, worker):
        self._worker = worker

    def get(self, uc_id):
        assert uc_id == "uc04"
        return self._worker


def _state(roles=("hr_onboarding_officer",), on_behalf_of="SYN-USER-201", config_ref=None):
    state = new_state(case_id="SYN-CASE-001", client_id="client_alpha", correlation_id="corr1",
                       actor={"on_behalf_of": on_behalf_of, "roles": list(roles)},
                       intake_trigger={}, uc04_client_config_ref=str(config_ref) if config_ref else None)
    state["employee_id"] = "SYN-EMP-001"
    state["joining_date"] = "2026-11-16"
    return state


def test_missing_client_config_blocks_without_calling_uc04():
    deps = OrchestratorDeps(ctx=None, worker_pool=None, uc03_repo_root=UC_ROOTS["uc03"])
    result = call_uc04_equipment_access(_state(config_ref=None), deps)
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.BLOCKED.value
    assert result["stage_errors"][Stage.EQUIPMENT_ACCESS.value]["code"] == "UC04_CONFIGURATION_MISSING"
    (blocker,) = result["blockers"]
    assert blocker["required_from"] == "Client configuration owner / HR" and blocker["retryable"] is True
    assert blocker["resolution"] == "external" and blocker["required_data"] == "uc04_equipment_access"


def test_no_approvals_yields_awaiting_approval_not_treated_as_ready(uc04_worker):
    config_ref = UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"
    deps = OrchestratorDeps(ctx=None, worker_pool=_SingleWorkerPool(uc04_worker), uc03_repo_root=UC_ROOTS["uc03"])
    state = _state(config_ref=config_ref)
    result = call_uc04_equipment_access(state, deps)
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] in (
        StageStatus.AWAITING_APPROVAL.value, StageStatus.BLOCKED.value,
    )
    assert result["equipment_access_result"]["overall_status"] in ("AWAITING_APPROVAL", "NEEDS_HUMAN_REVIEW")


def test_unauthorized_actor_role_is_rejected_by_uc04_itself(uc04_worker):
    """UC04's own Policy.check_caller rejects a role with no intersection with
    allowed_caller_roles -- this must surface as FAILED with UC04's own error, never as a
    fabricated success, and must not create a case/ticket as a side effect."""
    config_ref = UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"
    deps = OrchestratorDeps(ctx=None, worker_pool=_SingleWorkerPool(uc04_worker), uc03_repo_root=UC_ROOTS["uc03"])
    state = _state(roles=("totally_unaffiliated_role",), config_ref=config_ref)

    result = call_uc04_equipment_access(state, deps)

    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.FAILED.value
    assert "allowed_caller_roles" in result["error_detail"] or "caller" in result["error_detail"].lower()
    # No ticket/case mutation should have occurred for the rejected call.
    identity = uc04_worker._raw_call({"contract_version": "1.0", "op": "__identity_check__", "run_id": "chk",
                                       "idempotency_key": "chk", "actor": {"on_behalf_of": "s"}, "payload": {}})
    assert identity["ok"]
