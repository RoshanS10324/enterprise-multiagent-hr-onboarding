"""Mock IT service desk. Keeps an in-memory ticket map and a call log; it can be told to fail. No network, no real ticket."""

from sub_agent.ports import AdapterError, MockTicket


class MockITServiceDesk:
    real_side_effects = False

    def __init__(self, fail_with: AdapterError | None = None):
        self.fail_with = fail_with
        self.calls: list[dict] = []
        self.tickets: dict[str, MockTicket] = {}

    def submit_request(self, *, request_id: str, case_id: str, item_kind: str, item_id: str) -> MockTicket:
        self.calls.append({"request_id": request_id, "case_id": case_id, "item_kind": item_kind, "item_id": item_id})
        if self.fail_with is not None:
            raise self.fail_with
        if request_id not in self.tickets:  # idempotent per request
            self.tickets[request_id] = MockTicket(ticket_id=f"SYN-TKT-{len(self.tickets) + 1:04d}", request_id=request_id)
        return self.tickets[request_id]
