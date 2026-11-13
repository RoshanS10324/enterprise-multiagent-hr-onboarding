"""Shared test helpers. All data is synthetic; dates are fixed; no network. Scenario builders live in sub_agent.fixtures."""

import json

from sub_agent.entrypoints.direct import DirectEntry
from sub_agent.entrypoints.orchestrated import Actor, OrchestratedEntry, OrchestratorRequest, WorkflowRef
from sub_agent.fixtures import (ACCESS_KEYS, ALL, CALLER_ID, CALLER_ROLES, EQUIPMENT_KEYS, ITEMS, approval, approvals,  # noqa: F401
                           collection, delivery, make, new_environment, provisioning, ready_payload, rid, ticket)


class Env:
    def __init__(self, **kwargs):
        self.service, self.cases, self.desk = new_environment(**kwargs)
        self.direct = DirectEntry(self.service)
        self.orch = OrchestratedEntry(self.service)

    def run(self, payload, **kw):
        return self.direct.evaluate(payload, actor_id=CALLER_ID, roles=CALLER_ROLES, **kw)

    def submit(self, payload, **kw):
        return self.direct.submit_eligible(payload, actor_id=CALLER_ID, roles=CALLER_ROLES, **kw)

    def run_orch(self, payload, *, run_id="run-1", key="orch-key-1", step="sub_agent", instance=None, roles=None):
        return self.orch.evaluate(self.orch_request(payload, run_id=run_id, key=key, step=step, instance=instance, roles=roles))

    @staticmethod
    def orch_request(payload, *, run_id="run-1", key="orch-key-1", step="sub_agent", instance=None, roles=None):
        case_id = payload["case_id"] if isinstance(payload, dict) else payload.case_id
        return OrchestratorRequest(run_id=run_id, idempotency_key=key,
                                   workflow=WorkflowRef(id="onboarding", instance=instance or case_id, step=step),
                                   actor=Actor(on_behalf_of=CALLER_ID, roles=roles or ["onboarding_orchestrator"]),
                                   input=payload)


def item(result, key: str, n: int = 1):
    return next(i for i in result.items if i.request_id == rid(key, n))


def codes(blockers) -> set[str]:
    return {b.code for b in blockers}


def dump(result) -> str:
    return json.dumps(result.model_dump(mode="json"), default=str)
