"""UC-02 — Onboarding Task Management (Phase 1 local synthetic-data prototype).

Scope of this package is deliberately narrow:

* Deterministic onboarding task planning from explicit synthetic client config.
* No live APIs, no LLM calls, no real employee data.
* No message sending — reminders are produced as drafts only.

Harness integration status
--------------------------
``docs/pre-development-review.md`` §B recommends reusing ``hr-recruitment-common``
for ``BaseAgent``, ``HITLInbox``, ``AuditLog``, ``PolicyStore`` and ``ToolGateway``.
That package is verified present in the reference repo but is **not installable on
this machine** because ``uv`` is missing (review C-14 / R-10 / D-4).

Rather than duplicating the shared harness, this prototype keeps the domain layer
(:mod:`planner`, :mod:`tracker`, :mod:`checklist`, :mod:`calendar_uae`,
:mod:`reminders`) free of harness imports, and confines the platform concerns to
three thin seams that are documented for replacement:

===========================  =========================================
Local prototype module       Replace with (once ``uv sync`` works)
===========================  =========================================
:mod:`approvals`             ``hr_recruitment_common.platform.hitl``
:mod:`audit`                 ``hr_recruitment_common.platform.audit``
:mod:`service`               ``hr_recruitment_common.agent.BaseAgent``
===========================  =========================================
"""

from onboarding_task_management.config import AGENT_ID, VERSION

__all__ = ["AGENT_ID", "VERSION"]
