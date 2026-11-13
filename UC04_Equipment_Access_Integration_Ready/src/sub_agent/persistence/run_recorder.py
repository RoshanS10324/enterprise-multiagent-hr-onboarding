"""`DatabaseAgentRunRecorder`: one `agent_runs` row per call that returned a result.

The orchestrator reuses one correlation id as `run_id` for every worker call in a step (observed), and `agent_runs.run_id` is the primary key, so
two agents recording the same id would collide. The row id is therefore derived by default as `uc04:{run_id}:{operation}`; the scheme itself is
PENDING with the team and is a parameter. Writing is an upsert on `run_id`, so recording the same call twice is harmless.
"""

from collections.abc import Callable
from typing import Any

from sub_agent.persistence.errors import PersistenceError
from sub_agent.persistence.mappers import RUN_TABLE, is_uuid
from sub_agent.persistence.table_client import TableClient
from sub_agent.runs import AgentRunRecord


def default_run_id(run: AgentRunRecord) -> str:
    return f"uc04:{run.run_id}:{run.operation}"


class DatabaseAgentRunRecorder:
    def __init__(self, client: TableClient, *, run_id_for: Callable[[AgentRunRecord], str] = default_run_id):
        self._client = client
        self._run_id_for = run_id_for

    def row(self, run: AgentRunRecord) -> dict[str, Any]:
        if not is_uuid(run.case_id):
            raise PersistenceError("run case id is not a uuid: the shared table needs the PLATFORM identifier profile")
        return {
            "run_id": self._run_id_for(run), "case_id": run.case_id, "client_id": run.client_id, "agent_id": run.agent_id,
            "agent_version": run.agent_version, "actor_id": run.actor_id, "status": run.status, "advisor_mode": run.advisor_mode,
            "proposal_count": run.proposal_count, "action_count": run.action_count, "exception_count": run.exception_count,
            "tool_calls": list(run.tool_calls), "started_at": run.started_at.isoformat(), "finished_at": run.finished_at.isoformat(),
        }

    def record(self, run: AgentRunRecord) -> None:
        row = self.row(run)
        try:
            self._client.upsert(RUN_TABLE, row, on_conflict=("run_id",))
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(f"agent run write failed ({type(exc).__name__})") from None
