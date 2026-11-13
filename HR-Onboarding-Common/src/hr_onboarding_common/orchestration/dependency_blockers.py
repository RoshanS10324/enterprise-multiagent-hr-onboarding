"""What a blocked stage is waiting for, in a structure people can act on.

Every blocker the orchestrator records (adapters/_shared.add_blocker) is enriched from this catalogue by its code:
  required_from    who/which role or integration must act
  required_action  what has to happen
  required_data    the missing data, by its contract name
  dependency       the dependency that is not satisfied
  resolution       how it is resolved: an orchestrator route the UI can call ("provide_equipment_access_requests",
                   "provide_offer_event"), or "external" (configuration/data/integration outside this system)
  retryable        whether the stage can be retried/resumed once resolved
Nothing here decides or resolves anything; it describes. A code not in the catalogue keeps its plain fields.
"""

from __future__ import annotations

from typing import Any

CATALOGUE: dict[str, dict[str, Any]] = {
    # ---------------------------------------------------------------- UC04 equipment & access
    "UC04_IDENTITY_PROFILE_NOT_ENABLED": {
        "required_from": "Platform / integration owner",
        "required_action": "Enable UC04's PLATFORM identity profile (UC04_IDENTITY_PROFILE=PLATFORM on the backend) "
                           "once "
                           "the shared identity contract is approved, restart the backend, then retry UC04",
        "required_data": "UC04_IDENTITY_PROFILE", "dependency": "UC04 identity contract (platform case ids)",
        "resolution": "external", "retryable": True},
    "UC04_CONFIGURATION_MISSING": {
        "required_from": "Client configuration owner / HR",
        "required_action": "Provide and approve the client's equipment and access configuration for this role (client "
                           "configuration section uc04_equipment_access; config/tenants.yaml uc04_client_configs), "
                           "then retry UC04",
        "required_data": "uc04_equipment_access",
        "dependency": "Approved client configuration for this client and role",
        "resolution": "external", "retryable": True},
    "UC04_ROLE_CODE_INVALID": {
        "required_from": "HR onboarding officer",
        "required_action": "Correct the case's role to a role code (letters, digits and _, starting with a letter), "
                           "then retry UC04",
        "required_data": "role_id", "dependency": "Case role code", "resolution": "external", "retryable": True},
    "CONFIG_SCOPE_MISMATCH": {
        "required_from": "Client configuration owner / HR",
        "required_action": "Provide the equipment and access configuration that belongs to this client and role, then "
                           "retry UC04",
        "required_data": "uc04_equipment_access", "dependency": "Client configuration for this client and role",
        "resolution": "external", "retryable": True},
    "NO_ITEMS_REQUESTED": {
        "required_from": "HR onboarding officer",
        "required_action": "Record the equipment and access this joiner needs (items from the client's catalogue), "
                           "then retry UC04",
        "required_data": "requested_equipment, requested_access", "dependency": "Equipment / access request",
        "resolution": "provide_equipment_access_requests", "retryable": True},
    "UC02_PLAN_MISSING": {
        "required_from": "HR onboarding officer / UC-02",
        "required_action": "Run UC-02 task planning for this case (UC-02 is the source of the onboarding plan), "
                           "then retry UC04",
        "required_data": "task_plan.onboarding_id", "dependency": "This case's UC-02 onboarding plan",
        "resolution": "external", "retryable": True},
    "UC02_PLAN_CASE_MISMATCH": {
        "required_from": "HR onboarding officer / UC-02",
        "required_action": "Re-plan this case in UC-02 so it has its own plan (another case's, client's or "
                           "employee's plan is never used), then retry UC04",
        "required_data": "task_plan.onboarding_id", "dependency": "This case's own UC-02 onboarding plan",
        "resolution": "external", "retryable": True},
    "UC02_PLAN_UNAVAILABLE": {
        "required_from": "Platform / operations",
        "required_action": "Restore access to UC-02's plan store, then retry UC04",
        "required_data": "UC-02 onboarding plan", "dependency": "UC-02 plan store",
        "resolution": "external", "retryable": True},
    # ---------------------------------------------------------------- UC05 orientation
    "UC05_OFFER_EVENT_MISSING": {
        "required_from": "UC01 / offer-acceptance integration",
        "required_action": "Provide the offer-acceptance event reference UC05 resolves the employee from, "
                           "then retry UC05",
        "required_data": "offer_acceptance_event_id", "dependency": "Offer-acceptance event",
        "resolution": "provide_offer_event", "retryable": True},
    "UC05_OFFER_EVENT_NOT_FOUND": {
        "required_from": "UC01 / offer-acceptance integration",
        "required_action": "Connect UC05 to a real offer-acceptance source. In UC05's Supabase mode the event is the "
                           "case itself, so UC01 must have persisted the case to onboarding_cases. UC05's mailbox mode "
                           "is simulated (only synthetic test messages), so a real reference is never found there. "
                           "Then retry UC05",
        "required_data": "offer_acceptance_event_id", "dependency": "Offer-acceptance event source",
        # Typing another reference cannot help while the only mailbox is the simulated one.
        "resolution": "external", "retryable": True},
    "UC04_APPROVALS_PENDING": {
        "required_from": "Hiring manager (UC04 approver configuration)",
        "required_action": "The approver UC04's configuration names (hiring_manager for CLIENT-ALPHA) decides each "
                           "requested item in the approvals inbox; the decision reaches UC04 as an approval record and "
                           "UC04 re-evaluates",
        "required_data": "approval_records", "dependency": "UC04 item approvals",
        "resolution": "decide_in_inbox", "retryable": True},
    "NO_BUDDY_CANDIDATES": {
        "required_from": "HR (buddy programme owner)",
        "required_action": "Add an active buddy profile for this client in Supabase buddy_profiles (client_id, "
                           "employee_id, full_name, department, max_active_buddies >= 1, is_active); the client must "
                           "exist in client_configs. UC05 reads it with UC05_BUDDY_SOURCE=supabase. Then retry UC05",
        "required_data": "buddy_profiles", "dependency": "Buddy directory for this client",
        "resolution": "external", "retryable": True},
    "EMPLOYEE_ID_NOT_UNIQUE": {
        "required_from": "UC01 / HR system of record",
        "required_action": "Give this joiner their own employee number: UC01 must issue unique numbers (its in-memory "
                           "numbering restarts with every backend restart), or HR enters the HRMS employee number on "
                           "the form. Then start the case again; this case cannot be scheduled under a shared number",
        "required_data": "employee_id", "dependency": "Unique employee id per client",
        "resolution": "external", "retryable": True},
    "EMPLOYEE_NOT_IN_UC05_DIRECTORY": {
        "required_from": "UC01 / employee directory integration",
        "required_action": "The employee must exist in the authoritative employee directory UC05 reads "
                           "(onboarding_cases, "
                           "written by UC01), then retry UC05",
        "required_data": "employee_id", "dependency": "Authoritative employee directory (onboarding_cases)",
        "resolution": "external", "retryable": True},
    "INCOMPLETE_EMPLOYEE_DATA": {
        "required_from": "UC01 / employee record owner",
        "required_action": "Complete the employee's directory record (the fields listed), then retry UC05",
        "required_data": "employee directory fields",
        "dependency": "Authoritative employee directory (onboarding_cases)",
        "resolution": "external", "retryable": True},
    # ---------------------------------------------------------------- the case-state store (orchestrator_agent.py)
    "CASE_NOT_IN_DATABASE": {
        "required_from": "UC01 (employee record creation) / platform",
        "required_action": "UC01 opened the employee record but did not store the case in Supabase onboarding_cases, so "
                           "the workflow state cannot be kept in case_orchestration_state. Enable UC01's Supabase "
                           "mode (it then stores its cases), or run the orchestrator with a local case-state store "
                           "(ORCHESTRATOR_STATE_STORE=sqlite); then resume the case. The approval already given stands",
        "required_data": "onboarding_cases row", "dependency": "UC01 case in the shared database",
        "resolution": "external", "retryable": True},
    # ---------------------------------------------------------------- UC02 task planning (adapters/uc02_runtime.py)
    "UC02_CONFIG_MISSING": {
        "required_from": "Client configuration owner / HR",
        "required_action": "Provide and approve the client's onboarding configuration: an approved client_config_versions "
                           "row in force on the joining date (approver and timestamp, config_hash) with active "
                           "task_templates for that version. UC02 never invents tasks. Then resume the case",
        "required_data": "client_config_versions, task_templates",
        "dependency": "Approved, effective client onboarding configuration", "resolution": "external",
        "retryable": True},
    "UC02_CALENDAR_MISSING": {
        "required_from": "Client configuration owner / HR",
        "required_action": "Configure the client's working calendar (client_configs.working_days and client_holidays); "
                           "UC02 never assumes a calendar. Then resume the case",
        "required_data": "client_holidays", "dependency": "Client working calendar", "resolution": "external",
        "retryable": True},
    "UC02_EMPLOYEE_NOT_ELIGIBLE": {
        "required_from": "HR onboarding officer / client configuration owner",
        "required_action": "Correct the joiner's role, grade, work location or nationality group, or extend the client's "
                           "eligibility catalogue; then resume the case",
        "required_data": "role_id, grade, location, nationality_group", "dependency": "Client eligibility catalogue",
        "resolution": "external", "retryable": True},
    "UC02_CASE_NOT_FOUND": {
        "required_from": "UC01 (employee record creation)",
        "required_action": "UC01 must store the onboarding case in Supabase onboarding_cases for this client; UC02 plans "
                           "only for a stored case and never creates one. Then resume the case",
        "required_data": "onboarding_cases row", "dependency": "UC01 case in the shared database",
        "resolution": "external", "retryable": True},
    "UC02_CASE_IDENTITY_MISMATCH": {
        "required_from": "Operations / UC01 record owner",
        "required_action": "Reconcile the workflow's employee with onboarding_cases.employee_no, then resume the case",
        "required_data": "employee_id", "dependency": "UC01 case identity", "resolution": "external",
        "retryable": True},
    "UC04_CONFIG_PROJECTION_UNDECIDED": {
        "required_from": "UC04 owner / client configuration owner",
        "required_action": "Supabase holds the client's uc04_equipment_access section, but no approved projection to "
                           "UC04's CaseConfig exists (UC04 decisions D-1/D-2). Approve the projection; UC04 is never "
                           "evaluated on a guessed or synthetic configuration",
        "required_data": "client_service_policies uc04_equipment_access + approved projection",
        "dependency": "UC04 configuration in Supabase", "resolution": "external", "retryable": True},
    "UC04_CONFIG_UNAVAILABLE": {
        "required_from": "Operations / platform",
        "required_action": "Restore the connection to Supabase, then retry the stage",
        "required_data": "ORCHESTRATOR_SUPABASE_URL", "dependency": "Supabase client configuration",
        "resolution": "external", "retryable": True},
    "UC03_RESULT_NOT_PERSISTED": {
        "required_from": "HR (upload the file) / schema owner / UC01",
        "required_action": "A submitted document's validation result could not be stored in Supabase "
                           "document_validations: the file was not uploaded (reference only), its type has no value in "
                           "the candidate_documents.document_type CHECK, or the case is not in onboarding_cases. Fix the "
                           "named cause, then submit the document again",
        "required_data": "candidate_documents row for the document", "dependency": "UC03's Supabase tables",
        "resolution": "external", "retryable": True},
    "DOCUMENT_TYPE_NOT_IN_SCHEMA": {
        "required_from": "Schema owner",
        "required_action": "Widen the candidate_documents.document_type CHECK constraint to hold this document type",
        "required_data": "candidate_documents.document_type value", "dependency": "Supabase schema",
        "resolution": "external", "retryable": False},
    "UC02_STORE_UNAVAILABLE": {
        "required_from": "Operations / platform",
        "required_action": "Restore the connection to UC02's Supabase database, then retry the stage",
        "required_data": "UC02_WORKER_ENV__SUPABASE_URL", "dependency": "UC02's Supabase database",
        "resolution": "external", "retryable": True},
    "UC02_STORE_ERROR": {
        "required_from": "Operations / platform",
        "required_action": "UC02's database refused the request (credentials, schema or a constraint): fix it, then "
                           "resume the case",
        "required_data": "UC02 Supabase settings and schema", "dependency": "UC02's Supabase database",
        "resolution": "external", "retryable": True},
    "UC02_RUNTIME_MISCONFIGURED": {
        "required_from": "Operations / platform",
        "required_action": "Set UC02_ENV explicitly (development, test, staging or production); synthetic or mock client "
                           "configuration needs UC02_ENV=development|test and UC02_ALLOW_SYNTHETIC_CONFIG=1. Restart, "
                           "then resume",
        "required_data": "UC02_ENV", "dependency": "UC02 runtime settings", "resolution": "external",
        "retryable": True},
}


def enrich(blocker: dict[str, Any]) -> dict[str, Any]:
    """The blocker with its catalogue fields added (fields already on the blocker win)."""
    extra = CATALOGUE.get(str(blocker.get("code")))
    return {**extra, **blocker} if extra else blocker
