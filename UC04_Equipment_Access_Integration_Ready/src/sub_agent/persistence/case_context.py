"""`DatabaseCaseContext`: the parent case registry (`CaseContextPort`) read from `onboarding_cases`. Read-only: UC-04 never creates a case (BR-24).

Fail closed throughout: a lookup that cannot be scoped to one client is refused, a case with no employee number is treated as not found, a
soft-deleted case (`is_deleted`) is WITHDRAWN whatever its status says (nothing is prepared for a deleted case), a parent status UC-04 has no
behaviour for raises, and any client failure becomes a `PersistenceError`.
"""

from typing import Any

from sub_agent.persistence.errors import PersistenceError, TenantScopeError
from sub_agent.persistence.mappers import CASE_TABLE, is_uuid, parent_status
from sub_agent.persistence.table_client import TableClient
from sub_agent.ports import ParentCase
from sub_agent.states import ParentCaseStatus

_COLUMNS = ("case_id", "employee_no", "client_id", "status", "is_deleted")


class DatabaseCaseContext:
    def __init__(self, client: TableClient):
        self._client = client

    def _select(self, filters: dict[str, Any], columns: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
        try:
            return self._client.select(CASE_TABLE, filters=filters, columns=columns, limit=limit)
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(f"parent case read failed ({type(exc).__name__})") from None

    def get_case(self, case_id: str) -> ParentCase | None:
        if not is_uuid(case_id):
            return None  # the table's key is a uuid: nothing else can exist, and nothing else is sent to the database
        rows = self._select({"case_id": case_id}, _COLUMNS, 1)
        if not rows:
            return None
        row = rows[0]
        if not row.get("employee_no"):
            return None  # not ready for UC-04 (no employee number yet): fail closed as "not found"
        status = ParentCaseStatus.WITHDRAWN if row.get("is_deleted") is True else parent_status(row.get("status"))
        return ParentCase(case_id=str(row["case_id"]), employee_ref=row["employee_no"], client_id=row["client_id"], status=status)

    def case_for_employee(self, employee_ref: str, client_id: str | None = None) -> str | None:
        if not client_id:
            raise TenantScopeError("employee lookup needs a client: employee numbers are unique per client only")
        rows = [r for r in self._select({"client_id": client_id, "employee_no": employee_ref}, ("case_id", "is_deleted"), 3)
                if r.get("is_deleted") is not True]  # a soft-deleted case is history, not the employee's case
        if len(rows) > 1:
            raise PersistenceError("more than one case for one employee number in one client")  # the unique index should make this impossible
        return str(rows[0]["case_id"]) if rows else None
