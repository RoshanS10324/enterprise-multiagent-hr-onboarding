"""Mock approval desk used by tests and the demo to issue approval records. It stands in for approval routing
(Logic Apps with Teams approvals in the Enterprise HR Agent proposal) and approves nothing by itself."""

from datetime import date

from sub_agent.contracts import ApprovalRecord, Uc04Input
from sub_agent.states import ApprovalDecision, ApprovalType


class MockApprovalDesk:
    def __init__(self):
        self._n = 0
        self.issued: list[ApprovalRecord] = []

    def issue(self, inp: Uc04Input, request_key: str, *, item_id: str, approver_role: str, decided_on: date,
              approval_type: ApprovalType = ApprovalType.STANDARD, decision: ApprovalDecision = ApprovalDecision.APPROVED,
              approver_id: str = "SYN-USER-900", request_id: str | None = None) -> ApprovalRecord:
        self._n += 1
        record = ApprovalRecord(
            approval_id=f"SYN-APR-{self._n:03d}", request_id=request_id or f"{inp.case_id}:{request_key}",
            approval_type=approval_type, approver_id=approver_id, approver_role=approver_role, decision=decision,
            decided_on=decided_on, item_id=item_id, client_id=inp.client_id, role_id=inp.role_id,
            client_site_required=inp.client_site_required)
        self.issued.append(record)
        return record
