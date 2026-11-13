"""TEST-ONLY stand-in for the parent onboarding workflow. It is not a production orchestrator.

The reference repository has no orchestrator to call, so this harness plays the parent: it owns the (mock) case
registry, invokes UC-04 as one stage using the orchestrator contract, records the result and can query status later.
It contains no equipment or access rules.
"""

from sub_agent.states import CaseStatus


class LocalOrchestratorHarness:
    def __init__(self, env):
        self.env = env
        self.stage_log: list[str] = []
        self.results: dict[str, object] = {}

    def run_stage(self, payload: dict, *, run_id: str = "run-1", key: str = "harness-key"):
        response = self.env.run_orch(payload, run_id=run_id, key=key)
        self.stage_log.append("sub_agent")
        self.results[response.result.case_id] = response
        return response

    def run_stage_status(self, case_id: str) -> CaseStatus:
        """The parent reads the recorded UC-04 outcome later without re-running any rule."""
        return self.results[case_id].result.overall_status
