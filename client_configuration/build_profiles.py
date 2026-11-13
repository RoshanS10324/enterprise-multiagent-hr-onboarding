"""Generates the four SYNTHETIC client configuration profiles (client_a_config.json .. client_d_config.json).

This is a data-authoring tool, not runtime code: agents read the generated JSON (via Supabase), they never import this file. The profiles
are SYNTHETIC development samples. Nothing in them is an approved client policy, legal rule or real entitlement; every value is a labelled
test assumption. The structure follows what the Onboarding Agent Business Framework v0.1 says clients customise (documents by role, grade,
nationality and location; which activities apply; entitlements by grade; equipment and access per role; approvers and delegates; messages;
reminder schedule), plus the UC-01..UC-05 sections the central configuration needs. No secrets, credentials, addresses or real people.

    python client_configuration/build_profiles.py          # rewrites profiles/*.json deterministically
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_VERSION = "1.0.0"
NOW = "2026-11-04T00:00:00+00:00"
SYN = "SYNTHETIC ASSUMPTION - not an approved client policy"
TEAMS = {"hr_ops": "HR Operations (synthetic)", "hr_officer": "HR Onboarding Officer (synthetic)", "pro": "PRO / Visa (synthetic)",
         "facilities": "Admin / Travel Desk (synthetic)", "finance": "Payroll (synthetic)", "it_manager": "IT Service Desk (synthetic)",
         "manager": "Hiring Manager (synthetic)", "employee": "Employee"}


def app(roles=(), grades=(), nationality_groups=(), locations=(), employment_types=(), projects=(), attrs=(), excl=()):
    """Applicability. An empty list means 'all'. ``attrs`` must all be present on the case; ``excl`` must all be absent."""
    return {"roles": list(roles), "grades": list(grades), "nationality_groups": list(nationality_groups),
            "locations": list(locations), "employment_types": list(employment_types), "projects": list(projects),
            "requires_attributes": list(attrs), "excludes_attributes": list(excl)}


def task(key, cat, owner, offset, deps=(), *, title=None, team, group="g0", prio="normal", sla=3, evidence=False,
         approval=None, uc=None, a=None, desc=""):
    return {"template_key": key, "category": cat, "title": title or key.replace("_", " ").capitalize(),
            "description": desc or f"{key.replace('_', ' ')} ({SYN})", "owner_role": owner, "responsible_team": TEAMS[team],
            "priority": prio, "depends_on": list(deps), "parallel_group": group, "offset_days": offset,
            "sla_working_days": sla, "evidence_required": evidence, "approval_role": approval,
            "downstream_use_case": uc, "applicability": a or app()}


def doc(key, name, cat, mandatory, *, a=None, deadline=-7, formats=("pdf", "jpeg", "png"), meta=(), expiry=None,
        checks=("readable", "required_fields_present", "name_matches_employee"), approval=False, deps=(), blocks=(),
        evidence=True, extra=None, reasons=None, max_attempts=3):
    return {"document_key": key, "name": name, "category": cat, "mandatory": mandatory,
            "applicability": a or app(),
            "submission": {"requirement": "required" if mandatory else "optional", "deadline_offset_days": deadline,
                           "channel": "client_configured_channel"},
            "accepted_formats": list(formats), "required_metadata": list(meta),
            "expiry": expiry or {"applies": False, "min_remaining_validity_days": None, "handling": "not_applicable"},
            "verification": {"checks": list(checks), "evidence_required": evidence, "authenticity_decided_by": "hr",
                             "extraction_review": "uncertain_values_go_to_hr", "approval_required": approval},
            "initial_status": "pending",
            "rejection_reasons": reasons or ["EXPIRED_DOCUMENT", "UNREADABLE", "WRONG_TYPE", "MISSING_FIELDS", "NAME_MISMATCH"],
            "resubmission": {"allowed": True, "max_attempts": max_attempts, "replacement_request_needs_hr_approval": True},
            "depends_on_tasks": list(deps), "blocks_tasks": list(blocks), **(extra or {})}


def expiry(days):
    return {"applies": True, "min_remaining_validity_days": days, "handling": "reject_and_request_replacement"}


def program(key, name, sessions, deadline, *, mandatory=True, a=None, assessment=None, evidence="attendance_record"):
    return {"programme_key": key, "name": name, "session_types": list(sessions), "mandatory": mandatory,
            "applicability": a or app(), "completion_deadline_offset_days": deadline,
            "assessment": assessment or {"required": False}, "completion_criteria": "attended_or_completed_as_recorded",
            "evidence_required": evidence}


def policy(key, name, mandatory, deadline, *, a=None, evidence="signed_acknowledgement"):
    return {"policy_key": key, "name": name, "policy_reference": f"client-policy-library://{key} (placeholder; real policy content awaiting client)",
            "mandatory": mandatory, "applicability": a or app(), "acknowledgement_deadline_offset_days": deadline,
            "evidence_required": True, "evidence_type": evidence}


def item(code, name, kind, category=None):
    return {"item_code": code, "name": name, "kind": kind, "category": category or kind}


# ----------------------------------------------------------------------------------------------------------------------------------
# Shared base blocks (identical rules are defined once here, not copied into five sections)

EMPLOYEE_FIELDS = [
    {"field": "client_id", "label": "Client", "required": True, "type": "string", "sensitive": False},
    {"field": "candidate_name", "label": "Candidate name", "required": True, "type": "string", "sensitive": False},
    {"field": "candidate_email", "label": "Candidate email", "required": True, "type": "email", "sensitive": False},
    {"field": "designation", "label": "Designation", "required": True, "type": "string", "sensitive": False},
    {"field": "joining_date", "label": "Date of joining", "required": True, "type": "date", "sensitive": False},
    {"field": "work_location_type", "label": "Work location type", "required": True, "type": "enum",
     "values": ["OFFICE", "CLIENT_SITE"], "sensitive": False},
    {"field": "salary_and_terms", "label": "Salary and terms", "required": True, "type": "secret", "sensitive": True},
]
STATIC_CONTRACTS = [
    {"contract": "uc01_employee_event", "document": "UC02_Onboarding_Task_Agent/docs/UC02_INTEGRATION_CHECKLIST.md#2"},
    {"contract": "uc02_downstream_trigger", "document": "UC02_Onboarding_Task_Agent/docs/UC02_INTEGRATION_CHECKLIST.md#2"},
    {"contract": "client_configuration_retrieval", "document": "docs/CLIENT_CONFIGURATION_INTEGRATION_CONTRACTS.md"},
]
EVENT_TYPES = [
    {"event": "employee_created", "producer": "UC-01", "consumers": ["UC-02"], "idempotency_key_field": "event_id"},
    {"event": "plan_approval_decision", "producer": "approval_portal", "consumers": ["UC-02"], "idempotency_key_field": "callback_id"},
    {"event": "document_status_update", "producer": "UC-03", "consumers": ["UC-02"], "idempotency_key_field": "update_id"},
    {"event": "downstream_trigger", "producer": "UC-02", "consumers": ["UC-04", "UC-05"], "idempotency_key_field": "trigger_id"},
    {"event": "downstream_status", "producer": "UC-04|UC-05", "consumers": ["UC-02"], "idempotency_key_field": "event_id"},
]
BASE_HANDOFF = [
    {"uc02_field": "client_id", "source": "uc01.client_id", "required": True},
    {"uc02_field": "case_id", "source": "uc01.case_id", "required": True},
    {"uc02_field": "employee_id", "source": "uc01.employee_no", "required": True},
    {"uc02_field": "joining_date", "source": "uc01.joining_date", "required": True},
    {"uc02_field": "role_id", "source": "uc01.designation->role_id", "required": True},
    {"uc02_field": "grade", "source": "uc01.grade", "required": True},
    {"uc02_field": "nationality_group", "source": "config.nationality_groups[nationality_code]", "required": True},
    {"uc02_field": "location", "source": "uc01.location", "required": True},
    {"uc02_field": "employment_type", "source": "uc01.employment_type", "required": True},
    {"uc02_field": "attributes", "source": "uc01.variant_flags + derived", "required": False},
]
HOLD_RULES = [
    {"rule_key": "offer_not_accepted", "outcome": "hold", "reason": "Framework DL-01: start only when the offer is accepted"},
    {"rule_key": "form_not_approved", "outcome": "hold", "reason": "Framework DL-01: start only when the onboarding form is approved"},
]
APPROVAL_ACTIONS = [("send_to_joiner", "hr_officer"), ("hris_write", "hr_manager"), ("access_request", "manager"),
                    ("spend", "hr_manager"), ("visa_hr_ops", "hr_ops")]
NOTIFICATIONS = [("hr_review_email", "internal"), ("team_assignment", "internal"), ("document_request", "joiner"),
                 ("replacement_request", "joiner"), ("hr_notice", "internal")]


def shared(c):
    return {
        "calendar": {"timezone": c["tz"], "working_days": c["working_days"], "sla_calendar": "working_days",
                     "holidays": [{"date": d, "name": f"SYNTHETIC holiday {i + 1}", "observed": True}
                                  for i, d in enumerate(c["holidays"])]},
        "approval_matrix": [{"action": a, "approver_role": r, "delegate_role": c["delegates"].get(a),
                             "separation_of_duties": True} for a, r in APPROVAL_ACTIONS],
        "notification_templates": [{"key": k, "audience": aud, "channel": "email", "language": c["language"],
                                    "approval_required": True, "template_ref": f"template-library://{c['id']}/{k}"}
                                   for k, aud in NOTIFICATIONS],
        "recipient_mapping_refs": [{"role_or_team": t, "reference_key": f"recipient-map://{c['id']}/{t}"} for t in c["teams"]],
        "escalation_rules": [
            {"rule_key": "approval_stalled", "trigger": "approval_pending_past_due", "after_working_days": 1,
             "escalate_to_role": "hr_ops", "channel": "teams"},
            {"rule_key": "joining_date_at_risk", "trigger": "task_threatens_joining_date", "after_working_days": 0,
             "escalate_to_role": "hr_officer", "channel": "teams"}],
        "retry_policy": {"max_attempts": 3, "backoff_seconds": [30, 120, 600]},
        "event_types": EVENT_TYPES, "integration_contract_refs": STATIC_CONTRACTS,
    }


def uc01(c):
    return {
        "employee_fields": EMPLOYEE_FIELDS + c.get("extra_fields", []),
        "supported_roles": c["roles"], "grades": c["grades"], "nationality_groups": c["nat_groups"],
        "employment_types": c["employment_types"], "work_location_types": ["OFFICE", "CLIENT_SITE"],
        "eligibility_rules": HOLD_RULES + c.get("eligibility", []),
        "field_validation": {"candidate_email": {"format": "email"}, "joining_date": {"must_be_future": True}},
        "onboarding_record_requirements": {"offer_accepted_required": True, "approved_form_required": True,
                                           "form_approved_by_hr_role": True},
        "trigger_event": {"event_type": "employee_created", "required_fields": ["event_id", "client_id", "case_id",
                                                                                 "employee_id", "joining_date", "occurred_at"],
                          "idempotency_key_field": "event_id"},
        "duplicate_prevention": {"scope": "client", "match_on": ["candidate_email", "id_number"], "on_open_case": "link_existing",
                                 "name_only_match": "notify_hr_non_blocking"},
        "uc02_handoff_mapping": BASE_HANDOFF,
    }


def uc02(c):
    return {
        "task_templates": c["tasks"],
        "assignment_rules": [{"category": cat, "team": TEAMS[team], "downstream_use_case": uc, "mode": "use_case" if uc else "role",
                              "escalation_owner": "hr_officer"} for cat, team, uc in c["assign"]],
        "document_gates": [{"gate_key": "mandatory_documents", "requires": "all_mandatory_documents_verified",
                            "blocks_downstream": ["UC-04", "UC-05"]}],
        "hr_approval": {"plan_approval_required": True, "approver_role": c["plan_approver"], "delegate_role": c["plan_delegate"],
                        "sla_hours": c["approval_sla_hours"]},
        "due_date_calculation": {"reference": "joining_date", "direction": "backwards", "calendar": "shared_configuration.calendar",
                                 "default_sla_working_days": 3},
        "reminders": c["reminders"],
        "escalations": [{"rule_ref": "approval_stalled"}, {"rule_ref": "joining_date_at_risk"}],
        "overdue_monitoring": {"derived_from_due_date": True, "stored_status": False, "sweep": "on_demand_no_scheduler"},
        "milestones": [{"milestone_key": "day_one_ready", "title": "Ready for day one", "offset_days": -1,
                        "required_task_keys": c["day_one_tasks"]}],
        "visa_vs_background_check": c["visa_rule"],
    }


def uc03(c):
    return {
        "global": {"confidence_threshold": 0.7, "authenticity_decided_by": "hr", "name_difference_policy": "flag_to_hr_passport_name_for_legal_records",
                   "unrequested_document_policy": "do_not_use_flag_to_hr_follow_client_retention_rule",
                   "capabilities": {"text_extraction": "local_extraction_with_hr_review_of_uncertain_values",
                                    "antivirus_scanning": False, "identity_verification": False, "authority_verification": False},
                   "note": "No OCR, antivirus, identity or authority verification is claimed; HR decides authenticity (Framework DL-04)."},
        "documents": c["docs"],
    }


def uc04(c):
    return {
        "equipment_catalogue": c["equipment"], "access_catalogue": c["access"],
        "role_equipment_matrix": c["equipment_matrix"], "role_access_matrix": c["access_matrix"],
        "approver_config": c["approver_config"], "lead_times": c["lead_times"],
        "handover_rules": c["handover"], "evidence_roles": {"provisioning": "it_service_desk", "delivery": "it_service_desk",
                                                            "collection": "it_service_desk", "client_site_clearance": "account_manager"},
        "provisioning_prerequisites": ["plan_approved", "hr_record_created", "role_matrix_present"],
        "completion_evidence": ["provisioning_confirmation", "handover_confirmation"],
        "out_of_matrix_policy": "exception_approval_required",
        "client_site_rules": c.get("client_site_rules"), "country_rules": c.get("country_rules"),
    }


def uc05(c):
    return {"orientation_programmes": c["programmes"], "policy_acknowledgements": c["policies"],
            "reminder_rules": {"first_reminder_after_working_days": 2, "repeat_every_working_days": 2, "handover_to_hr_after_reminders": 3},
            "location_specific": c.get("location_specific", []),
            "scheduling": {"session_types": ["orientation", "induction", "team_intro", "buddy_intro"],
                           "invitations": "on_approval"}}


def build(c) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "client_master": {
            "client_id": c["id"], "client_name": c["name"], "client_status": "active",
            "country": c["country"], "supported_locations": c["locations"], "timezone": c["tz"],
            "configuration_version": 1, "effective_from": "2026-01-01", "effective_until": None,
            "approval_status": "approved", "approved_by": "synthetic-fixture", "approved_at": NOW,
            "data_origin": "synthetic", "created_at": NOW, "updated_at": NOW,
            "synthetic_notice": "SYNTHETIC development profile. Every value is a labelled test assumption, not an approved client policy.",
            "scenario": c["scenario"]},
        "shared_configuration": shared(c), "uc01_employee_record": uc01(c), "uc02_onboarding_tasks": uc02(c),
        "uc03_document_validation": uc03(c), "uc04_equipment_access": uc04(c), "uc05_orientation_policy": uc05(c),
        "awaiting_confirmation": c["awaiting"],
    }


# ----------------------------------------------------------------------------------------------------------------------------------
# Client-specific differences

GEN_AWAITING = [
    {"item": "All values in this profile are synthetic assumptions", "owning_team": "client HR / configuration owners"},
    {"item": "Approval authority for client configuration versions", "owning_team": "business owner (Framework OQ-04)"},
    {"item": "Real document lists, deadlines, formats and retention rules", "owning_team": "client HR admin / UC-03 team"},
    {"item": "Real equipment and access matrices, approvers and lead times", "owning_team": "client IT and hiring managers / UC-04 team"},
    {"item": "Real orientation, mandatory-training, assessment and policy-acknowledgement requirements", "owning_team": "client HR / UC-05 team"},
    {"item": "Nationality-to-group mapping", "owning_team": "client HR / process SME"},
]
DOC_DEFAULT_ATTR = {"checks": ("readable", "required_fields_present", "name_matches_employee")}

# --- SYN-CLIENT-A: UAE engineering (overseas hire: entry-permit route)
A = dict(
    id="SYN-CLIENT-A", name="SYNTHETIC CLIENT A - UAE engineering (not a real client)", scenario="Engineering onboarding, overseas hire, UAE",
    country={"code": "AE", "name": "United Arab Emirates"}, tz="Asia/Dubai", working_days=[1, 2, 3, 4, 5],
    holidays=["2026-12-02", "2026-12-03"], language="en", teams=list(TEAMS),
    locations=[{"code": "AE-DXB", "name": "Dubai", "country_code": "AE", "work_location_type": "OFFICE"}],
    delegates={"access_request": "hr_lead", "spend": "hr_lead"}, plan_approver="hr_ops", plan_delegate="hr_lead", approval_sla_hours=24,
    roles=[{"role_id": "software_engineer", "label": "Software Engineer"}, {"role_id": "qa_engineer", "label": "QA Engineer"}],
    grades=[{"grade_id": g, "band_order": i} for i, g in enumerate(["G3", "G4", "G5", "G6", "G7"])],
    nat_groups=[{"group_code": "NG-SOUTH-ASIA-SYN", "label": "South Asia (synthetic grouping)", "nationality_codes": ["IN", "PK", "BD"]}],
    employment_types=["full_time", "contract"],
    eligibility=[{"rule_key": "engineering_roles_only", "outcome": "eligible", "applies_to": app(roles=["software_engineer", "qa_engineer"]),
                  "reason": SYN}],
    reminders={"first_after_working_days": 2, "repeat_every_working_days": 2, "handover_to_hr_call_after": 3},
    visa_rule="run_visa_and_background_check_in_parallel_flag_cost_risk_to_hr",
    day_one_tasks=["laptop_request", "system_access", "payroll_setup"],
    tasks=[
        task("hr_record_create", "hr_record", "hr_ops", -12, team="hr_ops", prio="high", group="g0"),
        task("collect_documents", "document_collection", "hr_ops", -10, ["hr_record_create"], team="hr_ops", uc="UC-03", group="g1"),
        task("background_verification", "background_verification", "hr_officer", -9, ["collect_documents"], team="hr_officer", group="g2"),
        task("entry_permit", "visa", "pro", -12, ["collect_documents"], team="pro", prio="critical", group="g2",
             a=app(attrs=["location_status:overseas"])),
        task("medical_test_arrival", "medical", "pro", -3, ["entry_permit"], team="pro", group="g3", a=app(attrs=["location_status:overseas"])),
        task("biometrics_arrival", "biometrics", "pro", -2, ["medical_test_arrival"], team="pro", group="g4",
             a=app(attrs=["location_status:overseas"])),
        task("flight_booking", "travel", "facilities", -7, ["entry_permit"], team="facilities", group="g3", approval="hr_manager",
             a=app(attrs=["location_status:overseas", "needs_travel"])),
        task("accommodation", "accommodation", "facilities", -5, ["flight_booking"], team="facilities", group="g4", approval="hr_manager",
             a=app(attrs=["needs_accommodation"], grades=["G4", "G5", "G6", "G7"])),
        task("insurance_enrolment", "insurance", "hr_ops", -4, ["collect_documents"], team="hr_ops", group="g2"),
        task("payroll_setup", "payroll", "finance", -4, ["collect_documents"], team="finance", group="g2"),
        task("laptop_request", "it_provisioning", "it_manager", -5, ["hr_record_create"], team="it_manager", uc="UC-04", group="g1", approval="manager"),
        task("system_access", "access_management", "it_manager", -2, ["laptop_request"], team="it_manager", uc="UC-04", group="g2", approval="manager",
             evidence=True),
        task("orientation_slot", "orientation", "hr_ops", 0, ["hr_record_create"], team="hr_ops", uc="UC-05", group="g1"),
        task("engineering_induction", "orientation", "manager", 1, ["orientation_slot"], team="manager", uc="UC-05", group="g2",
             a=app(roles=["software_engineer", "qa_engineer"])),
        task("policy_ack", "policy_acknowledgement", "employee", 2, ["orientation_slot"], team="employee", uc="UC-05", group="g2", evidence=True),
    ],
    assign=[("hr_record", "hr_ops", None), ("document_collection", "hr_ops", "UC-03"), ("background_verification", "hr_officer", None),
            ("visa", "pro", None), ("medical", "pro", None), ("biometrics", "pro", None), ("travel", "facilities", None),
            ("accommodation", "facilities", None), ("insurance", "hr_ops", None), ("payroll", "finance", None),
            ("it_provisioning", "it_manager", "UC-04"), ("access_management", "it_manager", "UC-04"),
            ("orientation", "hr_ops", "UC-05"), ("policy_acknowledgement", "employee", "UC-05")],
    docs=[
        doc("signed_offer_letter", "Signed offer letter", "offer", True, meta=["employee_name", "employer_signature", "date"]),
        doc("passport", "Passport", "identity", True, meta=["passport_number", "nationality", "expiry_date", "full_name"],
            expiry=expiry(180), deps=["entry_permit"], blocks=["entry_permit"]),
        doc("photo", "Passport-size photo", "identity", True, formats=("jpeg", "png"), checks=("readable",), evidence=True),
        doc("emirates_id", "National ID (UAE)", "identity", False, a=app(attrs=["location_status:in_country"]),
            meta=["id_number", "expiry_date"], expiry=expiry(30)),
        doc("degree_certificate", "Degree certificate", "education", True, meta=["institution", "degree_title", "graduate_name"],
            extra={"attestation": {"required": True, "allowed_after_joining": False}}),
        doc("experience_letter", "Experience letters", "employment", True, meta=["employer_name", "employment_period"]),
        doc("payslips", "Recent payslips (last 3)", "employment", True, meta=["employer_name", "period", "amount"], extra={"min_count": 3}),
        doc("nda", "Signed NDA", "agreement", True, checks=("signed", "evidence_stored"), meta=["signature", "date"]),
        doc("policy_acknowledgements", "HR policy acknowledgement", "agreement", True, checks=("signed", "evidence_stored"),
            meta=["signature", "date", "policy_name"]),
        doc("medical_fitness_report", "Medical fitness (HR item)", "medical", True, a=app(attrs=["visa_required"]), approval=True,
            extra={"sensitive": "hr_item_only"}, checks=("readable",)),
        doc("insurance_enrolment_form", "Medical insurance enrolment form", "insurance", True, meta=["member_name"]),
        doc("dependent_documents", "Dependent documents", "family", False, a=app(attrs=["family_sponsorship"]), expiry=expiry(90),
            meta=["full_name", "relationship", "date_of_birth"]),
        doc("bank_details", "Bank account details", "bank_payroll", True, extra={"sensitive": "hr_payroll_only"}, checks=("readable",)),
    ],
    equipment=[item("laptop-standard", "Standard laptop (synthetic)", "equipment"), item("headset-basic", "Headset (synthetic)", "equipment"),
               item("monitor-27", "27-inch monitor (synthetic)", "equipment")],
    access=[item("email-teams-account", "Email and Teams account", "access", "communication"),
            item("source-control-access", "Source control access", "access", "engineering_tools"),
            item("ci-pipeline-access", "CI pipeline access", "access", "engineering_tools")],
    equipment_matrix=[{"role_id": "software_engineer", "grades": [], "items": ["laptop-standard", "headset-basic", "monitor-27"]},
                      {"role_id": "qa_engineer", "grades": [], "items": ["laptop-standard", "headset-basic"]}],
    access_matrix=[{"role_id": "software_engineer", "items": ["email-teams-account", "source-control-access", "ci-pipeline-access"]},
                   {"role_id": "qa_engineer", "items": ["email-teams-account", "source-control-access"]}],
    approver_config={"ACCESS": {"role": "manager", "delegate_role": "hr_lead"}, "EQUIPMENT": {"role": "manager", "delegate_role": "hr_lead"},
                     "EXCEPTION": {"role": "hr_manager", "delegate_role": None}},
    lead_times={"EQUIPMENT": 10, "ACCESS": 5, "CLIENT_SITE_CLEARANCE": 15},
    handover={"mode": "COLLECT", "keep_reserved_on_missed_slot": True},
    programmes=[program("general_orientation", "General orientation", ["orientation"], 0),
                program("engineering_induction", "Engineering induction", ["induction", "team_intro"], 2,
                        a=app(roles=["software_engineer", "qa_engineer"])),
                program("infosec_awareness", "Information security awareness", ["induction"], 5, assessment={"required": True, "pass_mark_percent": 80})],
    policies=[policy("nda", "Non-disclosure agreement", True, 0), policy("hr_handbook", "HR policy handbook", True, 2),
              policy("code_of_conduct", "Code of conduct", True, 2), policy("infosec_policy", "Information security policy", True, 3,
                                                                                a=app(roles=["software_engineer", "qa_engineer"]))],
    awaiting=GEN_AWAITING,
)

# --- SYN-CLIENT-B: India finance (no visa; payroll heavy; six-day week)
B = dict(
    id="SYN-CLIENT-B", name="SYNTHETIC CLIENT B - India finance (not a real client)", scenario="Finance and payroll onboarding, India",
    country={"code": "IN", "name": "India"}, tz="Asia/Kolkata", working_days=[1, 2, 3, 4, 5, 6],
    holidays=["2026-12-25", "2026-12-26"], language="en", teams=["hr_ops", "hr_officer", "finance", "it_manager", "manager", "employee"],
    locations=[{"code": "IN-BLR", "name": "Bengaluru", "country_code": "IN", "work_location_type": "OFFICE"},
               {"code": "IN-MUM", "name": "Mumbai", "country_code": "IN", "work_location_type": "OFFICE"}],
    delegates={"hris_write": "hr_lead"}, plan_approver="hr_manager", plan_delegate="hr_ops", approval_sla_hours=48,
    roles=[{"role_id": "finance_analyst", "label": "Finance Analyst"}, {"role_id": "payroll_specialist", "label": "Payroll Specialist"}],
    grades=[{"grade_id": g, "band_order": i} for i, g in enumerate(["G2", "G3", "G4", "G5"])],
    nat_groups=[{"group_code": "NG-DOMESTIC-SYN", "label": "Domestic (synthetic grouping)", "nationality_codes": ["IN"]}],
    employment_types=["full_time"],
    eligibility=[{"rule_key": "domestic_hiring_only", "outcome": "eligible", "applies_to": app(nationality_groups=["NG-DOMESTIC-SYN"]),
                  "reason": SYN}],
    extra_fields=[{"field": "tax_registration_id", "label": "Tax registration id", "required": True, "type": "string", "sensitive": True}],
    reminders={"first_after_working_days": 3, "repeat_every_working_days": 3, "handover_to_hr_call_after": 2},
    visa_rule="not_applicable_no_visa_route", day_one_tasks=["laptop_request", "payroll_setup", "finance_system_access"],
    tasks=[
        task("hr_record_create", "hr_record", "hr_ops", -10, team="hr_ops", group="g0"),
        task("collect_documents", "document_collection", "hr_ops", -8, ["hr_record_create"], team="hr_ops", uc="UC-03", group="g1"),
        task("background_verification", "background_verification", "hr_officer", -7, ["collect_documents"], team="hr_officer", group="g2"),
        task("bank_account_setup", "payroll", "finance", -5, ["collect_documents"], team="finance", group="g2", evidence=True),
        task("payroll_setup", "payroll", "finance", -4, ["bank_account_setup", "background_verification"], team="finance", group="g3", prio="high",
             approval="hr_manager", evidence=True),
        task("insurance_enrolment", "insurance", "hr_ops", -4, ["collect_documents"], team="hr_ops", group="g2"),
        task("laptop_request", "it_provisioning", "it_manager", -4, ["hr_record_create"], team="it_manager", uc="UC-04", group="g1", approval="manager"),
        task("finance_system_access", "access_management", "it_manager", -2, ["laptop_request", "background_verification"], team="it_manager",
             uc="UC-04", group="g3", approval="hr_manager", evidence=True, a=app(roles=["finance_analyst", "payroll_specialist"])),
        task("orientation_slot", "orientation", "hr_ops", 0, ["hr_record_create"], team="hr_ops", uc="UC-05", group="g1"),
        task("finance_compliance_training", "orientation", "manager", 3, ["orientation_slot"], team="manager", uc="UC-05", group="g2",
             a=app(roles=["finance_analyst", "payroll_specialist"])),
        task("policy_ack", "policy_acknowledgement", "employee", 2, ["orientation_slot"], team="employee", uc="UC-05", group="g2", evidence=True),
    ],
    assign=[("hr_record", "hr_ops", None), ("document_collection", "hr_ops", "UC-03"), ("background_verification", "hr_officer", None),
            ("payroll", "finance", None), ("insurance", "hr_ops", None), ("it_provisioning", "it_manager", "UC-04"),
            ("access_management", "it_manager", "UC-04"), ("orientation", "hr_ops", "UC-05"), ("policy_acknowledgement", "employee", "UC-05")],
    docs=[
        doc("signed_offer_letter", "Signed offer letter", "offer", True, meta=["employee_name", "employer_signature", "date"]),
        doc("national_id", "National identity document", "identity", True, meta=["id_number", "full_name"]),
        doc("tax_registration_proof", "Tax registration proof", "bank_payroll", True, meta=["registration_id", "full_name"], extra={"sensitive": "hr_payroll_only"}),
        doc("address_proof", "Address proof", "identity", True, meta=["address"], expiry=expiry(0)),
        doc("degree_certificate", "Degree certificate", "education", True, meta=["institution", "degree_title", "graduate_name"],
            extra={"attestation": {"required": False, "allowed_after_joining": True}}),
        doc("professional_qualification", "Professional qualification", "education", False, a=app(roles=["finance_analyst"]), meta=["body", "membership"]),
        doc("relieving_letter", "Previous-employer relieving letter", "employment", True, meta=["employer_name", "employment_period"]),
        doc("payslips", "Recent payslips (last 3)", "employment", True, meta=["employer_name", "period", "amount"], extra={"min_count": 3}),
        doc("bank_details", "Bank account details", "bank_payroll", True, extra={"sensitive": "hr_payroll_only"}, checks=("readable",)),
        doc("nda", "Signed NDA", "agreement", True, checks=("signed", "evidence_stored"), meta=["signature", "date"]),
        doc("policy_acknowledgements", "HR policy acknowledgement", "agreement", True, checks=("signed", "evidence_stored"),
            meta=["signature", "date", "policy_name"]),
        doc("insurance_nomination", "Group insurance nomination", "insurance", False, meta=["nominee_name"]),
    ],
    equipment=[item("laptop-standard", "Standard laptop (synthetic)", "equipment"), item("headset-basic", "Headset (synthetic)", "equipment")],
    access=[item("email-teams-account", "Email and Teams account", "access", "communication"),
            item("erp-finance-read", "Finance system (read) access", "access", "finance_systems"),
            item("payroll-system-access", "Payroll system access", "access", "finance_systems")],
    equipment_matrix=[{"role_id": "finance_analyst", "grades": [], "items": ["laptop-standard"]},
                      {"role_id": "payroll_specialist", "grades": [], "items": ["laptop-standard", "headset-basic"]}],
    access_matrix=[{"role_id": "finance_analyst", "items": ["email-teams-account", "erp-finance-read"]},
                   {"role_id": "payroll_specialist", "items": ["email-teams-account", "erp-finance-read", "payroll-system-access"]}],
    approver_config={"ACCESS": {"role": "hr_manager", "delegate_role": None}, "EQUIPMENT": {"role": "manager", "delegate_role": None},
                     "EXCEPTION": {"role": "hr_lead", "delegate_role": None}},
    lead_times={"EQUIPMENT": 7, "ACCESS": 5, "CLIENT_SITE_CLEARANCE": 10},
    handover={"mode": "DELIVER", "keep_reserved_on_missed_slot": False},
    programmes=[program("general_orientation", "General orientation", ["orientation"], 0),
                program("finance_compliance_training", "Finance compliance training", ["induction"], 7,
                        a=app(roles=["finance_analyst", "payroll_specialist"]), assessment={"required": True, "pass_mark_percent": 75})],
    policies=[policy("nda", "Non-disclosure agreement", True, 0), policy("hr_handbook", "HR policy handbook", True, 3),
              policy("financial_controls", "Financial controls policy", True, 5, a=app(roles=["finance_analyst", "payroll_specialist"])),
              policy("data_privacy", "Data privacy policy", True, 3)],
    awaiting=GEN_AWAITING,
)

# --- SYN-CLIENT-C: UAE senior management (in-country visa transfer; background check first; grade entitlements)
SENIOR = ["G6", "G7"]
C = dict(
    id="SYN-CLIENT-C", name="SYNTHETIC CLIENT C - UAE senior management (not a real client)", scenario="Senior management onboarding, in-country hire, UAE",
    country={"code": "AE", "name": "United Arab Emirates"}, tz="Asia/Dubai", working_days=[1, 2, 3, 4, 5],
    holidays=["2026-12-02", "2026-12-03", "2026-12-04"], language="en", teams=list(TEAMS),
    locations=[{"code": "AE-AUH", "name": "Abu Dhabi", "country_code": "AE", "work_location_type": "OFFICE"},
               {"code": "AE-DXB", "name": "Dubai", "country_code": "AE", "work_location_type": "OFFICE"}],
    delegates={"access_request": "hr_manager", "spend": "hr_lead", "hris_write": "hr_lead"}, plan_approver="hr_lead", plan_delegate="hr_manager",
    approval_sla_hours=12,
    roles=[{"role_id": "senior_manager", "label": "Senior Manager"}, {"role_id": "director", "label": "Director"}],
    grades=[{"grade_id": g, "band_order": i} for i, g in enumerate(["G5", "G6", "G7", "G8"])],
    nat_groups=[{"group_code": "NG-SOUTH-ASIA-SYN", "label": "South Asia (synthetic grouping)", "nationality_codes": ["IN", "PK", "BD"]}],
    employment_types=["full_time"],
    eligibility=[{"rule_key": "senior_grades_only", "outcome": "eligible", "applies_to": app(grades=["G6", "G7", "G8"]), "reason": SYN}],
    extra_fields=[{"field": "reporting_executive_id", "label": "Reporting executive", "required": True, "type": "string", "sensitive": False}],
    reminders={"first_after_working_days": 1, "repeat_every_working_days": 2, "handover_to_hr_call_after": 2},
    visa_rule="wait_for_clear_background_check_before_visa",
    day_one_tasks=["executive_laptop_request", "system_access", "leadership_briefing"],
    tasks=[
        task("hr_record_create", "hr_record", "hr_ops", -15, team="hr_ops", prio="high", group="g0"),
        task("collect_documents", "document_collection", "hr_ops", -13, ["hr_record_create"], team="hr_ops", uc="UC-03", group="g1"),
        task("background_verification", "background_verification", "hr_officer", -12, ["collect_documents"], team="hr_officer", prio="high", group="g2",
             sla=7),
        task("visa_status_change", "visa", "pro", -9, ["background_verification"], team="pro", prio="high", group="g3",
             a=app(attrs=["location_status:in_country"])),
        task("medical_test", "medical", "pro", -6, ["visa_status_change"], team="pro", group="g4", a=app(attrs=["location_status:in_country"])),
        task("biometrics", "biometrics", "pro", -4, ["medical_test"], team="pro", group="g5", a=app(attrs=["location_status:in_country"])),
        task("family_insurance_enrolment", "insurance", "hr_ops", -5, ["visa_status_change"], team="hr_ops", group="g4", approval="hr_manager",
             a=app(grades=SENIOR, attrs=["family_insurance"])),
        task("insurance_enrolment", "insurance", "hr_ops", -5, ["collect_documents"], team="hr_ops", group="g2"),
        task("payroll_setup", "payroll", "finance", -4, ["collect_documents"], team="finance", group="g2", approval="hr_manager"),
        task("executive_laptop_request", "it_provisioning", "it_manager", -8, ["hr_record_create"], team="it_manager", uc="UC-04", group="g1",
             approval="manager"),
        task("system_access", "access_management", "it_manager", -3, ["executive_laptop_request"], team="it_manager", uc="UC-04", group="g2",
             approval="hr_manager", evidence=True),
        task("privileged_access_exception", "access_management", "it_manager", -2, ["system_access"], team="it_manager", uc="UC-04", group="g3",
             approval="hr_lead", evidence=True, a=app(grades=SENIOR, attrs=["privileged_access"])),
        task("orientation_slot", "orientation", "hr_ops", 0, ["hr_record_create"], team="hr_ops", uc="UC-05", group="g1"),
        task("leadership_briefing", "orientation", "manager", 1, ["orientation_slot"], team="manager", uc="UC-05", group="g2",
             a=app(grades=SENIOR)),
        task("policy_ack", "policy_acknowledgement", "employee", 2, ["orientation_slot"], team="employee", uc="UC-05", group="g2", evidence=True),
    ],
    assign=[("hr_record", "hr_ops", None), ("document_collection", "hr_ops", "UC-03"), ("background_verification", "hr_officer", None),
            ("visa", "pro", None), ("medical", "pro", None), ("biometrics", "pro", None), ("insurance", "hr_ops", None),
            ("payroll", "finance", None), ("it_provisioning", "it_manager", "UC-04"), ("access_management", "it_manager", "UC-04"),
            ("orientation", "hr_ops", "UC-05"), ("policy_acknowledgement", "employee", "UC-05")],
    docs=[
        doc("signed_offer_letter", "Signed offer letter", "offer", True, meta=["employee_name", "employer_signature", "date"], approval=True),
        doc("passport", "Passport", "identity", True, meta=["passport_number", "nationality", "expiry_date", "full_name"], expiry=expiry(180)),
        doc("current_residence_visa", "Current residence visa", "visa", True, a=app(attrs=["location_status:in_country"]),
            meta=["visa_number", "expiry_date", "sponsor"], expiry=expiry(30), deps=["visa_status_change"], blocks=["visa_status_change"]),
        doc("previous_sponsor_noc", "No-objection certificate from previous sponsor", "visa", True, a=app(attrs=["location_status:in_country"]),
            meta=["sponsor", "date"], deps=["visa_status_change"]),
        doc("emirates_id", "National ID (UAE)", "identity", True, a=app(attrs=["location_status:in_country"]), meta=["id_number", "expiry_date"],
            expiry=expiry(30)),
        doc("photo", "Passport-size photo", "identity", True, formats=("jpeg", "png"), checks=("readable",)),
        doc("degree_certificate", "Degree certificate", "education", True, meta=["institution", "degree_title", "graduate_name"],
            extra={"attestation": {"required": True, "allowed_after_joining": True}}),
        doc("executive_references", "Executive references", "employment", True, a=app(grades=SENIOR), meta=["referee", "relationship"]),
        doc("experience_letter", "Experience letters", "employment", True, meta=["employer_name", "employment_period"]),
        doc("payslips", "Recent payslips (last 3)", "employment", True, meta=["employer_name", "period", "amount"], extra={"min_count": 3}),
        doc("senior_declaration", "Senior declaration", "agreement", True, a=app(grades=SENIOR), checks=("signed", "evidence_stored"), approval=True),
        doc("conflict_of_interest", "Conflict-of-interest declaration", "agreement", True, a=app(grades=SENIOR), checks=("signed", "evidence_stored")),
        doc("nda", "Signed NDA", "agreement", True, checks=("signed", "evidence_stored"), meta=["signature", "date"]),
        doc("policy_acknowledgements", "HR policy acknowledgement", "agreement", True, checks=("signed", "evidence_stored"),
            meta=["signature", "date", "policy_name"]),
        doc("medical_fitness_report", "Medical fitness (HR item)", "medical", True, approval=True, extra={"sensitive": "hr_item_only"}, checks=("readable",)),
        doc("family_documents", "Family documents for insurance", "family", False, a=app(grades=SENIOR, attrs=["family_insurance"]), expiry=expiry(90),
            meta=["full_name", "relationship", "date_of_birth"]),
        doc("bank_details", "Bank account details", "bank_payroll", True, extra={"sensitive": "hr_payroll_only"}, checks=("readable",)),
    ],
    equipment=[item("laptop-executive", "Executive laptop (synthetic)", "equipment"), item("mobile-phone", "Mobile phone (synthetic)", "equipment"),
               item("headset-basic", "Headset (synthetic)", "equipment")],
    access=[item("email-teams-account", "Email and Teams account", "access", "communication"),
            item("board-reporting-access", "Board reporting access", "access", "executive_tools"),
            item("privileged-admin-access", "Privileged administrative access", "access", "privileged")],
    equipment_matrix=[{"role_id": "senior_manager", "grades": SENIOR, "items": ["laptop-executive", "mobile-phone", "headset-basic"]},
                      {"role_id": "director", "grades": SENIOR, "items": ["laptop-executive", "mobile-phone"]}],
    access_matrix=[{"role_id": "senior_manager", "items": ["email-teams-account", "board-reporting-access"]},
                   {"role_id": "director", "items": ["email-teams-account", "board-reporting-access"]}],
    approver_config={"ACCESS": {"role": "hr_manager", "delegate_role": "hr_lead"}, "EQUIPMENT": {"role": "hr_manager", "delegate_role": "hr_lead"},
                     "EXCEPTION": {"role": "hr_lead", "delegate_role": "hr_manager"}},
    lead_times={"EQUIPMENT": 15, "ACCESS": 8, "CLIENT_SITE_CLEARANCE": 20},
    handover={"mode": "DELIVER", "keep_reserved_on_missed_slot": True},
    programmes=[program("general_orientation", "General orientation", ["orientation"], 0),
                program("leadership_briefing", "Leadership briefing", ["induction", "team_intro"], 3, a=app(grades=SENIOR)),
                program("executive_compliance", "Executive compliance briefing", ["induction"], 10, a=app(grades=SENIOR),
                        assessment={"required": True, "pass_mark_percent": 85})],
    policies=[policy("nda", "Non-disclosure agreement", True, 0), policy("hr_handbook", "HR policy handbook", True, 2),
              policy("code_of_conduct", "Code of conduct", True, 2), policy("conflict_of_interest", "Conflict-of-interest policy", True, 3, a=app(grades=SENIOR)),
              policy("delegation_of_authority", "Delegation of authority", True, 5, a=app(grades=SENIOR))],
    awaiting=GEN_AWAITING + [{"item": "Grade thresholds for family insurance, travel and executive equipment entitlements",
                              "owning_team": "client HR (Framework DL-10)"}],
)

# --- SYN-CLIENT-D: Saudi Arabia operations (Sun-Thu week; work-permit route; mandatory medical and insurance; client-site clearance)
D = dict(
    id="SYN-CLIENT-D", name="SYNTHETIC CLIENT D - Saudi Arabia operations (not a real client)", scenario="Operations onboarding, Saudi Arabia",
    country={"code": "SA", "name": "Saudi Arabia"}, tz="Asia/Riyadh", working_days=[7, 1, 2, 3, 4],
    holidays=["2026-09-23", "2026-12-17"], language="en", teams=["hr_ops", "hr_officer", "pro", "facilities", "finance", "it_manager", "manager", "employee"],
    locations=[{"code": "SA-RUH", "name": "Riyadh", "country_code": "SA", "work_location_type": "OFFICE"},
               {"code": "SA-JED-SITE", "name": "Jeddah client site", "country_code": "SA", "work_location_type": "CLIENT_SITE"}],
    delegates={"access_request": "hr_ops"}, plan_approver="hr_ops", plan_delegate="hr_officer", approval_sla_hours=24,
    roles=[{"role_id": "operations_associate", "label": "Operations Associate"}, {"role_id": "site_supervisor", "label": "Site Supervisor"}],
    grades=[{"grade_id": g, "band_order": i} for i, g in enumerate(["G1", "G2", "G3", "G4"])],
    nat_groups=[{"group_code": "NG-SOUTH-ASIA-SYN", "label": "South Asia (synthetic grouping)", "nationality_codes": ["IN", "PK", "BD"]}],
    employment_types=["full_time", "contract"],
    eligibility=[{"rule_key": "contract_staff_need_agency_flag", "outcome": "review", "applies_to": app(employment_types=["contract"]),
                  "reason": SYN}],
    reminders={"first_after_working_days": 2, "repeat_every_working_days": 3, "handover_to_hr_call_after": 3},
    visa_rule="run_visa_and_background_check_in_parallel_flag_cost_risk_to_hr",
    day_one_tasks=["laptop_request", "system_access", "safety_induction"],
    tasks=[
        task("hr_record_create", "hr_record", "hr_ops", -14, team="hr_ops", group="g0"),
        task("collect_documents", "document_collection", "hr_ops", -12, ["hr_record_create"], team="hr_ops", uc="UC-03", group="g1"),
        task("background_verification", "background_verification", "hr_officer", -11, ["collect_documents"], team="hr_officer", group="g2"),
        task("work_permit_processing", "visa", "pro", -13, ["collect_documents"], team="pro", prio="critical", group="g2",
             a=app(attrs=["location_status:overseas"])),
        task("medical_test", "medical", "pro", -6, ["work_permit_processing"], team="pro", group="g3", a=app(attrs=["location_status:overseas"])),
        task("residence_permit", "biometrics", "pro", -3, ["medical_test"], team="pro", group="g4", a=app(attrs=["location_status:overseas"])),
        task("flight_booking", "travel", "facilities", -8, ["work_permit_processing"], team="facilities", group="g3", approval="hr_manager",
             a=app(attrs=["location_status:overseas", "needs_travel"])),
        task("accommodation", "accommodation", "facilities", -5, ["flight_booking"], team="facilities", group="g4", approval="hr_manager",
             a=app(attrs=["needs_accommodation"])),
        task("insurance_enrolment", "insurance", "hr_ops", -5, ["collect_documents"], team="hr_ops", group="g2"),
        task("payroll_setup", "payroll", "finance", -4, ["collect_documents"], team="finance", group="g2"),
        task("client_site_setup", "workplace", "manager", -10, ["collect_documents"], team="manager", group="g2", sla=10, evidence=True,
             a=app(attrs=["client_site"])),
        task("laptop_request", "it_provisioning", "it_manager", -5, ["hr_record_create"], team="it_manager", uc="UC-04", group="g1", approval="manager"),
        task("system_access", "access_management", "it_manager", -2, ["laptop_request"], team="it_manager", uc="UC-04", group="g2", approval="manager",
             evidence=True),
        task("orientation_slot", "orientation", "hr_ops", 0, ["hr_record_create"], team="hr_ops", uc="UC-05", group="g1"),
        task("safety_induction", "orientation", "manager", 1, ["orientation_slot"], team="manager", uc="UC-05", group="g2", evidence=True),
        task("policy_ack", "policy_acknowledgement", "employee", 2, ["orientation_slot"], team="employee", uc="UC-05", group="g2", evidence=True),
    ],
    assign=[("hr_record", "hr_ops", None), ("document_collection", "hr_ops", "UC-03"), ("background_verification", "hr_officer", None),
            ("visa", "pro", None), ("medical", "pro", None), ("biometrics", "pro", None), ("travel", "facilities", None),
            ("accommodation", "facilities", None), ("insurance", "hr_ops", None), ("payroll", "finance", None), ("workplace", "manager", None),
            ("it_provisioning", "it_manager", "UC-04"), ("access_management", "it_manager", "UC-04"),
            ("orientation", "hr_ops", "UC-05"), ("policy_acknowledgement", "employee", "UC-05")],
    docs=[
        doc("signed_offer_letter", "Signed offer letter", "offer", True, meta=["employee_name", "employer_signature", "date"]),
        doc("passport", "Passport", "identity", True, meta=["passport_number", "nationality", "expiry_date", "full_name"], expiry=expiry(180),
            blocks=["work_permit_processing"]),
        doc("photo", "Passport-size photo", "identity", True, formats=("jpeg", "png"), checks=("readable",)),
        doc("work_permit_application_pack", "Work-permit application pack", "visa", True, a=app(attrs=["location_status:overseas"]),
            meta=["application_ref"], deps=["work_permit_processing"]),
        doc("education_certificate", "Education certificate", "education", False, meta=["institution", "graduate_name"]),
        doc("experience_letter", "Experience letters", "employment", True, meta=["employer_name", "employment_period"]),
        doc("medical_fitness_report", "Medical fitness (HR item)", "medical", True, approval=True, extra={"sensitive": "hr_item_only"}, checks=("readable",),
            deps=["medical_test"]),
        doc("insurance_enrolment_form", "Medical insurance enrolment form", "insurance", True, meta=["member_name"]),
        doc("client_site_clearance_form", "Client-site clearance form", "client_site", True, a=app(attrs=["client_site"]), meta=["site", "clearance_ref"],
            deps=["client_site_setup"], blocks=["client_site_setup"]),
        doc("agency_documents", "Agency contractor documents", "employment", True, a=app(employment_types=["contract"]), meta=["agency_name"]),
        doc("nda", "Signed NDA", "agreement", True, checks=("signed", "evidence_stored"), meta=["signature", "date"]),
        doc("policy_acknowledgements", "HR policy acknowledgement", "agreement", True, checks=("signed", "evidence_stored"),
            meta=["signature", "date", "policy_name"]),
        doc("bank_details", "Bank account details", "bank_payroll", True, extra={"sensitive": "hr_payroll_only"}, checks=("readable",)),
    ],
    equipment=[item("laptop-basic", "Basic laptop (synthetic)", "equipment"), item("mobile-phone", "Mobile phone (synthetic)", "equipment"),
               item("safety-kit", "Safety kit (synthetic)", "equipment")],
    access=[item("email-teams-account", "Email and Teams account", "access", "communication"),
            item("ops-dashboard-access", "Operations dashboard access", "access", "operations_tools")],
    equipment_matrix=[{"role_id": "operations_associate", "grades": [], "items": ["laptop-basic", "mobile-phone", "safety-kit"]},
                      {"role_id": "site_supervisor", "grades": [], "items": ["laptop-basic", "mobile-phone", "safety-kit"]}],
    access_matrix=[{"role_id": "operations_associate", "items": ["email-teams-account", "ops-dashboard-access"]},
                   {"role_id": "site_supervisor", "items": ["email-teams-account", "ops-dashboard-access"]}],
    approver_config={"ACCESS": {"role": "manager", "delegate_role": "hr_ops"}, "EQUIPMENT": {"role": "manager", "delegate_role": None},
                     "EXCEPTION": {"role": "hr_manager", "delegate_role": None}},
    lead_times={"EQUIPMENT": 12, "ACCESS": 6, "CLIENT_SITE_CLEARANCE": 25},
    handover={"mode": "READY_AT_DESK", "keep_reserved_on_missed_slot": False},
    client_site_rules={"required_items": ["site_badge", "client_nda", "safety_clearance"], "block_on_late_clearance": True, "owner_role": "manager"},
    country_rules={"country_code": "SA", "courier_extra_days": 3, "remote_first_day_required": False},
    programmes=[program("general_orientation", "General orientation", ["orientation"], 0),
                program("safety_induction", "Operations safety induction", ["induction"], 1,
                        assessment={"required": True, "pass_mark_percent": 90}, evidence="attendance_and_assessment_record"),
                program("client_site_orientation", "Client-site orientation", ["induction"], 3, a=app(attrs=["client_site"]),
                        mandatory=True)],
    policies=[policy("nda", "Non-disclosure agreement", True, 0), policy("hr_handbook", "HR policy handbook", True, 2),
              policy("safety_policy", "Health and safety policy", True, 1), policy("site_rules", "Client-site rules", True, 3, a=app(attrs=["client_site"]))],
    location_specific=[{"location_code": "SA-JED-SITE", "programme_keys": ["client_site_orientation"], "policy_keys": ["site_rules"]}],
    awaiting=GEN_AWAITING + [{"item": "Saudi country compliance pack (work permit, residence permit, medical, insurance rules)",
                              "owning_team": "country compliance pack owners (Framework section 7)"}],
)

PROFILES = {"client_a_config.json": A, "client_b_config.json": B, "client_c_config.json": C, "client_d_config.json": D}


def main() -> None:
    out = HERE / "profiles"
    out.mkdir(exist_ok=True)
    for name, spec in PROFILES.items():
        (out / name).write_text(json.dumps(build(spec), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("wrote", name)


if __name__ == "__main__":
    main()
