"""Mock of the parent onboarding case registry. Stands in for the real case record (Cosmos DB per the Enterprise HR Agent proposal)."""

from sub_agent.ports import ParentCase
from sub_agent.states import ParentCaseStatus


class MockCaseContext:
    def __init__(self, cases: list[ParentCase] | None = None):
        self._cases = {c.case_id: c.model_copy() for c in cases or []}
        self.created: list[str] = []  # UC-04 must never add to this

    def get_case(self, case_id: str) -> ParentCase | None:
        case = self._cases.get(case_id)
        return case.model_copy() if case else None

    def case_for_employee(self, employee_ref: str, client_id: str | None = None) -> str | None:
        for case in self._cases.values():
            if case.employee_ref == employee_ref and (client_id is None or case.client_id == client_id):
                return case.case_id
        return None

    def set_status(self, case_id: str, status: ParentCaseStatus) -> None:
        self._cases[case_id] = self._cases[case_id].model_copy(update={"status": status})

    def set_client(self, case_id: str, client_id: str) -> None:
        """Mock only: the parent case moves to another client (EC-35). The parent workflow owns this change in production."""
        self._cases[case_id] = self._cases[case_id].model_copy(update={"client_id": client_id})

    @property
    def case_ids(self) -> list[str]:
        return sorted(self._cases)
