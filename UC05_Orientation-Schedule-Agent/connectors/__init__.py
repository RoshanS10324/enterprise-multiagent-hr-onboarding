"""Integrations with external systems. Each subpackage owns one system's interface: its Protocol,
its request/response models, and its error types.

Each subpackage holds `base.py` (the interface) and `mock.py` (a simulated provider for tests and
the offline demo). Rules for every connector here:
  - No real provider integration exists. There are no credentials, endpoints or SDK clients
    anywhere in this repository. `mock.py` is a simulation only: deterministic in-memory state,
    synthetic data, receipts and references marked simulated ("sim-..." refs, simulated=True), a
    reset() for test isolation, and no workflow or business logic. A real adapter would be a new
    module beside it, never an edit to the mock. HR_AGENT_ENV=production refuses to start on mocks.
  - Every interface separates a provider/infrastructure failure (retryable, e.g.
    CalendarBookingError, EmailServiceError, NotificationDeliveryError) from a business outcome
    (e.g. CalendarConflictError, NotificationRejectedError). Callers must never reinterpret one as
    the other.
  - Connectors, mocks included, never import the agent, the API, or the onboarding workflow
    (tests/test_context_layer.py). Business rules and orchestration live in `sub_agent.services`
    and `sub_agent.agent`; tenant isolation, authorization, approvals and audit are enforced by
    OnboardingToolRegistry before any connector is called.

Subpackages: email (offer-acceptance events; outbound email is a notification channel),
employee_directory (HRMS reads), calendar (bookings and availability), notifications (message
delivery on every channel), teams (the Teams channel of notifications).
"""
