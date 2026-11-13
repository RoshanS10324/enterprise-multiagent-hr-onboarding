"""What UC-02 needs from the live Supabase schema, and a check that it is there.

The live tables were created by the shared team, so UC-02 adapts to them rather than the
other way round. ``REQUIRED`` lists, per live table, the columns UC-02 must be able to
store; :func:`verify_schema` reports anything missing. It only reads, never alters.

Verified against the live schema on 2026-11-02 (see
``docs/UC02_SUPABASE_MAPPING_AND_PROPOSED_MIGRATION.md``). The columns UC-02 has no home
for today are the ones :func:`verify_schema` reports; a migration is *proposed* in that
document and is not applied.
"""

from __future__ import annotations

from collections.abc import Mapping

#: UC-02 table -> live table.
LIVE_TABLE = {
    "onboarding": "onboarding_cases",
    "task": "onboarding_tasks",
    "task_dependency": "task_dependencies",
    "plan_revision": "plan_revisions",
    "approval_request": "approval_requests",
    "agent_run": "agent_runs",
    "audit_event": "audit_events",
    "client_config_ref": "client_config_versions",
}

#: UC-02 field -> live column, where the names differ.
COLUMN_MAP = {
    "onboarding_cases": {"onboarding_id": "case_id", "employee_id": "employee_no",
                         "location": "location_name", "project": "project_name",
                         "attributes": "onboarding_attributes"},
    "onboarding_tasks": {"onboarding_id": "case_id", "owner": "owner_role",
                         "assignee_id": "assigned_actor_id", "assignee_team": "assigned_team"},
    "task_dependencies": {"depends_on_id": "depends_on_task_id"},
}

REQUIRED: dict[str, tuple[str, ...]] = {
    "onboarding_cases": ("case_id", "client_id", "employee_no", "joining_date", "location_name",
                         "project_name", "role_id", "grade", "department", "manager_id",
                         "nationality_group", "onboarding_attributes", "status"),
    "onboarding_tasks": ("task_id", "case_id", "client_id", "employee_id", "template_key",
                         "plan_order", "category", "title", "title_ar", "owner_role",
                         "assigned_actor_id", "assigned_team", "assigned_by", "assigned_at",
                         "status", "due_date", "planned_start_date", "started_at", "completed_at",
                         "cancelled_at", "evidence_required", "evidence_ref",
                         "evidence_recorded_at", "approval_role", "notes", "priority",
                         "sensitive_to", "requires_attributes", "config_version",
                         "status_summary", "external_outcome", "external_source",
                         "external_reference", "external_blocking_reason", "external_updated_at"),
    "task_dependencies": ("task_id", "depends_on_task_id", "client_id"),
    "plan_revisions": ("revision_id", "case_id", "revision", "client_id", "drivers",
                       "added_task_ids", "removed_task_ids", "rescheduled_task_ids",
                       "retained_task_ids", "applied_by", "approval_request_id"),
    "approval_requests": ("request_id", "case_id", "client_id", "gate", "approver_role",
                          "requested_by", "workflow_module", "subject_ref", "payload", "outcome",
                          "decided_by", "decided_at", "decision_notes", "created_at"),
    "audit_events": ("event_id", "case_id", "client_id", "actor_type", "actor_id", "actor_role",
                     "action", "outcome", "detail", "before_state", "after_state", "correlation_id",
                     "reference", "task_id", "occurred_at"),
    "agent_runs": ("run_id", "case_id", "client_id", "agent_id", "agent_version", "actor_id",
                   "status", "advisor_mode", "model_name", "proposal_count", "action_count",
                   "exception_count", "tool_calls", "started_at", "finished_at"),
    "client_config_versions": ("client_id", "version", "status", "approved_by", "config_hash"),
    "task_templates": ("template_key", "client_id", "config_version", "category", "title", "title_ar", "owner_role",
                       "offset_days", "depends_on_template_keys", "sensitive_to", "role_ids", "grades", "locations",
                       "projects", "nationality_groups", "requires_attributes", "evidence_required", "approval_role",
                       "default_priority", "is_active"),
    "client_holidays": ("client_id", "holiday_date", "is_observed"),
    "client_document_requirements": ("client_id", "config_version", "requirement_key", "document_type", "description",
                                     "is_mandatory", "applicable_roles", "applicable_grades", "applicable_locations",
                                     "applicable_projects", "eligibility_rules", "is_active"),
    "client_milestone_templates": ("client_id", "config_version", "milestone_key", "title", "offset_days",
                                   "required_task_keys", "eligibility_rules", "is_active"),
}


def verify_schema(live: Mapping[str, set[str]]) -> list[str]:
    """Human-readable problems; an empty list means every required column exists."""
    problems = []
    for table, columns in REQUIRED.items():
        if table not in live:
            problems.append(f"missing table {table}")
            continue
        absent = [c for c in columns if c not in live[table]]
        if absent:
            problems.append(f"{table}: missing columns {', '.join(absent)}")
    return problems


__all__ = ["COLUMN_MAP", "LIVE_TABLE", "REQUIRED", "verify_schema"]
