"""UC-01 Employee Record Creation sub-agent.

Entry points:

- `agent` -- what the sub-agent can do, in flow order
- `api`   -- the FastAPI app (`uvicorn sub_agent.api:app`)
- `fakes` -- in-memory adapters and the mock Outlook connector

Importing this package does not pull in FastAPI; import `sub_agent.api`
explicitly when you need to serve.
"""

from .agent import CaseService, handle_acceptance_email, handle_form_approved, open_onboarding_case

__version__ = "0.1.0"

__all__ = [
    "CaseService",
    "__version__",
    "handle_acceptance_email",
    "handle_form_approved",
    "open_onboarding_case",
]
