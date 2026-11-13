"""Shared fixtures for the UC-02 suite.

Importing this module also triggers ``tests/__init__.py``, which puts ``src/``
on ``sys.path``.
"""

from __future__ import annotations

import unittest

from onboarding_task_management.contracts import Actor, Role, SourceUseCase
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import (
    CLIENT_ALPHA,
    CLIENT_BETA,
    build_registry,
    make_trigger,
    persona,
)

# Two HR Ops actors: the inbox forbids deciding your own request, so gate tests
# need a requester and a separate approver.
HR_OPS = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id=CLIENT_ALPHA)
HR_OPS_APPROVER = Actor(actor_id="U-HR-2", role=Role.HR_OPS, client_id=CLIENT_ALPHA)
HR_LEAD = Actor(actor_id="U-HRL-1", role=Role.HR_LEAD, client_id=CLIENT_ALPHA)
IT_MANAGER = Actor(actor_id="U-IT-1", role=Role.IT_MANAGER, client_id=CLIENT_ALPHA)
FACILITIES = Actor(actor_id="U-FAC-1", role=Role.FACILITIES, client_id=CLIENT_ALPHA)
FINANCE = Actor(actor_id="U-FIN-1", role=Role.FINANCE, client_id=CLIENT_ALPHA)
MANAGER = Actor(actor_id="U-MGR-1", role=Role.MANAGER, client_id=CLIENT_ALPHA)
EMPLOYEE = Actor(actor_id="U-EMP-1", role=Role.EMPLOYEE, client_id=CLIENT_ALPHA)

HR_OPS_BETA = Actor(actor_id="U-HR-B1", role=Role.HR_OPS, client_id=CLIENT_BETA)

# TEST-ONLY. A real deployment must register one authenticated service identity per
# reporting workflow; none exists yet. These tests report as ``U-HR-1`` for every
# source, so that identity is registered for every source here and nowhere else.
TEST_ONLY_SOURCE_PRINCIPALS = {
    source: frozenset({"U-HR-1"}) for source in SourceUseCase
}


class ServiceTestCase(unittest.TestCase):
    """Base case with a fresh service and a created EMP-001 onboarding."""

    employee_id = "EMP-001"

    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())
        self.trigger = make_trigger(persona(self.employee_id))
        created = self.service.create_onboarding(HR_OPS, self.trigger)
        self.onboarding_id = created.onboarding_id

    # -- helpers -----------------------------------------------------------

    @property
    def record(self):
        """A fresh snapshot of the stored record.

        The service hands out deep copies (H1), so a value captured at setUp
        would go stale the moment any service call mutated stored state. This
        re-reads on every access instead.
        """
        return self.service.get(HR_OPS, self.onboarding_id)

    def task_id(self, template_key: str) -> str:
        return f"{self.onboarding_id}#{template_key}"

    def task(self, template_key: str):
        task = self.record.task_by_key(template_key)
        assert task is not None, f"no task for template {template_key!r}"
        return task

    def template_keys(self) -> set[str]:
        return {t.template_key for t in self.record.tasks}

    def complete(self, actor: Actor, template_key: str) -> None:
        self.service.complete_task(actor, self.onboarding_id, self.task_id(template_key))

    def ready_keys(self) -> set[str]:
        return {
            t.template_key
            for t in self.service.ready_tasks(HR_OPS, self.onboarding_id)
        }
