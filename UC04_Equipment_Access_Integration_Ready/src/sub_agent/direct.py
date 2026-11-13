"""Scenario A: an authorised caller invokes UC-04 directly for an existing onboarding case.

This entry point only builds the request envelope. Every decision, approval check and guardrail runs in Uc04Service.
"""

import hashlib
from datetime import datetime, timezone
from typing import Any

from sub_agent.contracts import CallerContext, SubmissionReport, Uc04Input, Uc04Request, Uc04Result
from sub_agent.ports import AgentRunRecorderPort
from sub_agent.runs import build_run_record
from sub_agent.service import Uc04Service
from sub_agent.states import InvocationMode


class DirectEntry:
    def __init__(self, service: Uc04Service, run_recorder: AgentRunRecorderPort | None = None):
        self.service = service
        self.run_recorder = run_recorder  # optional; off by default and never changes a call's result

    def _request(self, payload: Any, actor_id: str, roles: list[str], run_id: str | None,
                 idempotency_key: str | None) -> Uc04Request:
        inp: Uc04Input = self.service.parse_input(payload)
        digest = hashlib.sha256(inp.model_dump_json().encode()).hexdigest()[:16]
        caller = CallerContext(invocation_mode=InvocationMode.DIRECT, actor_id=actor_id, roles=roles,
                               run_id=run_id or f"direct-{digest}", idempotency_key=idempotency_key or f"key-{digest}")
        return Uc04Request(caller=caller, input=inp)

    def evaluate(self, payload: Any, *, actor_id: str, roles: list[str], run_id: str | None = None,
                 idempotency_key: str | None = None) -> Uc04Result:
        audit = self.service.audit
        with audit.context(actor_id=actor_id, invocation_mode=InvocationMode.DIRECT.value):
            request = self._request(payload, actor_id, roles, run_id, idempotency_key)
            audit.bind_context(run_id=request.caller.run_id)
            started = datetime.now(timezone.utc)
            result = self.service.evaluate(request)
            if self.run_recorder is not None:
                self.run_recorder.record(build_run_record(request=request, result=result, operation="evaluate",
                                                          started_at=started, finished_at=datetime.now(timezone.utc)))
            return result

    def submit_eligible(self, payload: Any, *, actor_id: str, roles: list[str], run_id: str | None = None,
                        idempotency_key: str | None = None) -> SubmissionReport:
        audit = self.service.audit
        with audit.context(actor_id=actor_id, invocation_mode=InvocationMode.DIRECT.value):
            request = self._request(payload, actor_id, roles, run_id, idempotency_key)
            audit.bind_context(run_id=request.caller.run_id)
            started = datetime.now(timezone.utc)
            report = self.service.submit_eligible(request)
            if self.run_recorder is not None:
                self.run_recorder.record(build_run_record(request=request, result=report.result, operation="submit_eligible",
                                                          started_at=started, finished_at=datetime.now(timezone.utc), report=report))
            return report
