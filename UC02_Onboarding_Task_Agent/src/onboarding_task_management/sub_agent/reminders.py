"""Reminder draft preparation.

This module has **no transport**. It imports no HTTP client, no mail library and
no connector, and exposes no ``send``. Producing a :class:`ReminderDraft` is the
terminal action; dispatch belongs to UC-06 behind the ``joining_confirmation``
gate, and needs the notification connector that review C-8 lists as unavailable.

Drafts carry no PII: recipients are addressed by role, and the body references
task IDs and counts only.
"""

from __future__ import annotations

from datetime import date

from onboarding_task_management.calendar_uae import (
    UAE_PUBLIC_HOLIDAYS,
    business_days_between,
)
from onboarding_task_management.config import REMINDER_LEAD_DAYS
from onboarding_task_management.contracts import (
    OPEN_STATUSES,
    OnboardingRecord,
    ReminderDraft,
    Role,
    TaskCategory,
    TaskRecord,
)
from onboarding_task_management.sub_agent.tracker import is_overdue

OVERDUE_REASON = "overdue"
DUE_SOON_REASON = "due_soon"

#: "Due date" — used in the Arabic per-task lines.
DUE_AR = "تاريخ الاستحقاق"

#: Arabic task-category labels, so the Arabic body is genuinely Arabic.
CATEGORY_LABELS_AR: dict[TaskCategory, str] = {
    TaskCategory.HR_RECORD: "سجل الموظف",
    TaskCategory.DOCUMENT_COLLECTION: "جمع المستندات",
    TaskCategory.IT_PROVISIONING: "تجهيز تقنية المعلومات",
    TaskCategory.ACCESS_MANAGEMENT: "إدارة الصلاحيات",
    TaskCategory.WORKPLACE: "مكان العمل",
    TaskCategory.ORIENTATION: "التعريف الوظيفي",
    TaskCategory.POLICY_ACKNOWLEDGEMENT: "الإقرار بالسياسات",
    TaskCategory.PAYROLL: "الرواتب",
    TaskCategory.JOINING_CONFIRMATION: "تأكيد الالتحاق",
    TaskCategory.BACKGROUND_VERIFICATION: "التحقق من الخلفية",
    TaskCategory.VISA: "تأشيرة العمل",
    TaskCategory.MEDICAL: "الفحص الطبي",
    TaskCategory.BIOMETRICS: "البصمة الحيوية",
    TaskCategory.INSURANCE: "التأمين الصحي",
    TaskCategory.TRAVEL: "السفر",
    TaskCategory.ACCOMMODATION: "السكن",
    TaskCategory.FOOD: "الإعاشة",
    TaskCategory.BANKING_LETTER: "الخطابات البنكية",
    TaskCategory.HR_SYSTEM_ACTIVATION: "تفعيل سجل الموظف",
}

#: Arabic labels for the owning role the draft is addressed to.
ROLE_LABELS_AR: dict[Role, str] = {
    Role.HR_OPS: "عمليات الموارد البشرية",
    Role.HR_LEAD: "رئيس الموارد البشرية",
    Role.IT_MANAGER: "مدير تقنية المعلومات",
    Role.FACILITIES: "المرافق",
    Role.FINANCE: "المالية",
    Role.MANAGER: "المدير المباشر",
    Role.BUDDY: "الزميل المرافق",
    Role.EMPLOYEE: "الموظف",
    Role.SYSTEM: "النظام",
    Role.HR_OFFICER: "مسؤول الموارد البشرية",
    Role.HR_MANAGER: "مدير الموارد البشرية",
    Role.PRO: "مندوب العلاقات الحكومية",
}


def category_label_ar(category: TaskCategory) -> str:
    """Arabic label for a task category, falling back to the slug."""
    return CATEGORY_LABELS_AR.get(category, category.value)


def role_label_ar(role: Role) -> str:
    """Arabic label for a role, falling back to the slug."""
    return ROLE_LABELS_AR.get(role, role.value)


def _draft_id(onboarding_id: str, owner: Role, reason: str) -> str:
    """Deterministic: one draft per (onboarding, owner, reason) per run."""
    return f"RMD-{onboarding_id}#{owner.value}#{reason}"


def _build_draft(
    record: OnboardingRecord,
    owner: Role,
    reason: str,
    tasks: list[TaskRecord],
    as_of: date,
    holidays: frozenset[date],
) -> ReminderDraft:
    task_ids = sorted(task.task_id for task in tasks)
    days_to_joining = business_days_between(as_of, record.joining_date, holidays)

    if reason == OVERDUE_REASON:
        subject_en = f"Action needed: {len(task_ids)} overdue onboarding task(s)"
        subject_ar = f"إجراء مطلوب: {len(task_ids)} مهمة تأهيل متأخرة"
    else:
        subject_en = f"Reminder: {len(task_ids)} onboarding task(s) due soon"
        subject_ar = f"تذكير: {len(task_ids)} مهمة تأهيل مستحقة قريبًا"

    ordered = sorted(tasks, key=lambda t: (t.due_date, t.task_id))
    lines_en = [
        f"- {task.task_id} ({task.category.value}) due {task.due_date.isoformat()}"
        for task in ordered
    ]
    # Built from Arabic labels rather than reusing lines_en, which previously
    # left the Arabic body almost entirely English (finding M3).
    lines_ar = [
        f"- {task.task_id} ({category_label_ar(task.category)})"
        f" {DUE_AR} {task.due_date.isoformat()}"
        for task in ordered
    ]
    body_en = "\n".join(
        [
            f"Onboarding {record.onboarding_id} — {days_to_joining} business day(s) to joining.",
            f"Tasks owned by {owner.value}:",
            *lines_en,
        ]
    )
    body_ar = "\n".join(
        [
            f"التأهيل {record.onboarding_id} — {days_to_joining} يوم عمل حتى الالتحاق.",
            f"المهام المسندة إلى {role_label_ar(owner)}:",
            *lines_ar,
        ]
    )

    return ReminderDraft(
        draft_id=_draft_id(record.onboarding_id, owner, reason),
        onboarding_id=record.onboarding_id,
        client_id=record.client_id,
        employee_id=record.employee_id,
        task_ids=task_ids,
        recipient_role=owner,
        reason=reason,
        subject_en=subject_en,
        subject_ar=subject_ar,
        body_en=body_en,
        body_ar=body_ar,
    )


def build_reminder_drafts(
    record: OnboardingRecord,
    as_of: date,
    *,
    lead_days: int = REMINDER_LEAD_DAYS,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> list[ReminderDraft]:
    """Group outstanding tasks into one draft per owner per reason.

    A task is reported as overdue **or** due-soon, never both, so owners do not
    receive two drafts covering the same task.
    """
    overdue_by_owner: dict[Role, list[TaskRecord]] = {}
    due_soon_by_owner: dict[Role, list[TaskRecord]] = {}

    for task in record.tasks:
        if task.status not in OPEN_STATUSES:
            continue
        if is_overdue(task, as_of):
            overdue_by_owner.setdefault(task.owner, []).append(task)
            continue
        days_until_due = business_days_between(as_of, task.due_date, holidays)
        if 0 <= days_until_due <= lead_days:
            due_soon_by_owner.setdefault(task.owner, []).append(task)

    drafts = [
        _build_draft(record, owner, OVERDUE_REASON, tasks, as_of, holidays)
        for owner, tasks in sorted(overdue_by_owner.items())
    ]
    drafts += [
        _build_draft(record, owner, DUE_SOON_REASON, tasks, as_of, holidays)
        for owner, tasks in sorted(due_soon_by_owner.items())
    ]
    return drafts
