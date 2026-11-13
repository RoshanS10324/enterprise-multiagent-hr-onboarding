"""Phase 4: checklist service, layered on the Phase 3 OnboardingPort.

Covers idempotent task creation, status/pending/overdue queries, and that the audit log records
every checklist action.
"""

from datetime import date

import pytest

from hr_onboarding_common.platform.audit import AuditLog
from sub_agent.fakes import build_mock_onboarding
from sub_agent.models import TaskStatus
from sub_agent.services.checklist import (REQUIRED_TASK_TYPES, ensure_required_tasks, overdue_tasks,
                                          pending_tasks, task_status)

ACME = "CLIENT-ACME"
EMPLOYEE = "EMP-001"


@pytest.fixture
def onboarding():
    return build_mock_onboarding()


@pytest.fixture
def audit():
    return AuditLog()


# --------------------------------------------------------------------------- checklist creation

def test_ensure_required_tasks_creates_the_full_catalog(onboarding, audit):
    tasks = ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME)

    assert {t.task_type for t in tasks} == set(REQUIRED_TASK_TYPES)
    assert all(t.status == TaskStatus.PENDING for t in tasks)
    assert all(t.employee_id == EMPLOYEE and t.client_id == ACME for t in tasks)


def test_repeated_requests_do_not_create_duplicate_tasks(onboarding, audit):
    first = ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME)
    second = ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME)
    third = ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME)

    assert {t.id for t in first} == {t.id for t in second} == {t.id for t in third}
    assert len(third) == len(REQUIRED_TASK_TYPES)


def test_cancelled_task_is_recreated_but_others_are_left_alone(onboarding, audit):
    first = ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME)
    cancelled = next(t for t in first if t.task_type == "it_setup")
    onboarding.update_task_status(cancelled.id, TaskStatus.CANCELLED)

    second = ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME)

    untouched_ids = {t.id for t in first if t.task_type != "it_setup"}
    assert untouched_ids <= {t.id for t in second}
    new_it_setup = next(t for t in second if t.task_type == "it_setup" and t.status == TaskStatus.PENDING)
    assert new_it_setup.id != cancelled.id


# --------------------------------------------------------------------------- status queries

def test_task_status_returns_every_task(onboarding, audit):
    ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME)
    assert len(task_status(onboarding, EMPLOYEE)) == len(REQUIRED_TASK_TYPES)


def test_pending_tasks_excludes_completed(onboarding, audit):
    tasks = ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME)
    onboarding.update_task_status(tasks[0].id, TaskStatus.COMPLETED)

    pending = pending_tasks(onboarding, EMPLOYEE)
    assert tasks[0].id not in {t.id for t in pending}
    assert len(pending) == len(REQUIRED_TASK_TYPES) - 1


def test_overdue_tasks_only_includes_past_due_pending_tasks(onboarding, audit):
    ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME, due_date=date(2026, 11, 1))
    tasks = task_status(onboarding, EMPLOYEE)
    onboarding.update_task_status(tasks[0].id, TaskStatus.COMPLETED)  # overdue but completed -> excluded

    overdue = overdue_tasks(onboarding, EMPLOYEE, as_of=date(2026, 11, 5))
    assert tasks[0].id not in {t.id for t in overdue}
    assert len(overdue) == len(REQUIRED_TASK_TYPES) - 1

    assert overdue_tasks(onboarding, EMPLOYEE, as_of=date(2026, 10, 1)) == []  # not due yet


def test_task_without_due_date_is_never_overdue(onboarding, audit):
    ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME, due_date=None)
    assert overdue_tasks(onboarding, EMPLOYEE, as_of=date(2099, 1, 1)) == []


# --------------------------------------------------------------------------- audit trail

def test_checklist_actions_are_audited(onboarding, audit):
    ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME)

    assert len(audit.find("checklist.task_created")) == len(REQUIRED_TASK_TYPES)


def test_idempotent_rerun_does_not_add_new_audit_events(onboarding, audit):
    ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME)
    ensure_required_tasks(onboarding, audit, employee_id=EMPLOYEE, client_id=ACME)

    assert len(audit.find("checklist.task_created")) == len(REQUIRED_TASK_TYPES)
