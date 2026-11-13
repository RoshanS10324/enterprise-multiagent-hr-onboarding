"""In-memory email adapter for development and tests. Fictional data only."""

from __future__ import annotations

from datetime import datetime, timezone

from onboarding_agent.connectors.email.base import EmailServiceError, OfferAcceptanceEvent

FAILING_EVENT_ID = "EVT-PROVIDER-OUTAGE"  # requesting this event id simulates an email-service failure


class MockEmailAdapter:
    def __init__(self, events: list[OfferAcceptanceEvent]):
        self.events = {e.event_id: e for e in events}
        self._processed: set[str] = set()

    def get_event(self, event_id: str) -> OfferAcceptanceEvent:
        if event_id == FAILING_EVENT_ID:
            raise EmailServiceError(f"email provider request for {event_id} failed (simulated outage)")
        return self.events[event_id].model_copy(deep=True)

    def is_processed(self, event_id: str) -> bool:
        return event_id in self._processed

    def mark_processed(self, event_id: str) -> None:
        self._processed.add(event_id)


def build_mock_email_adapter() -> MockEmailAdapter:
    received = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    return MockEmailAdapter([
        OfferAcceptanceEvent(event_id="EVT-001", employee_id="EMP-TEST-001", received_at=received,
                            raw_subject="Offer accepted - Alex Morgan",
                            raw_body="Alex Morgan has accepted the offer for Software Engineer."),
        OfferAcceptanceEvent(event_id="EVT-002", employee_id="EMP-TEST-003", received_at=received,
                            raw_subject="Offer accepted - Priya Nair",
                            raw_body="Priya Nair has accepted the offer for QA Engineer."),
        OfferAcceptanceEvent(event_id="EVT-010", employee_id="EMP-TEST-010", received_at=received,
                            raw_subject="Offer accepted - Morgan Lee",
                            raw_body="Morgan Lee has accepted the offer for Support Engineer."),
        OfferAcceptanceEvent(event_id="EVT-MALFORMED", employee_id=None, received_at=received,
                            raw_subject="RE: Fwd: ???", raw_body="(message could not be parsed)"),
    ])
