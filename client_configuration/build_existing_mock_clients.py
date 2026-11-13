"""Generates complete SYNTHETIC configurations for the four EXISTING mock clients (CLIENT_CANADA, CLIENT_DUBAI, CLIENT_INDIA, CLIENT_USA).

    python client_configuration/build_existing_mock_clients.py      # writes profiles/existing_mock_clients/*.json

What these are: development test data for clients that already exist in ``client_configs`` (marked ``environment: mock``). They are NOT
client-approved policies and NOT legal determinations. No legal deadline, tax rate, government fee, statutory percentage or named statutory form is
stated; wherever a real rule is needed the item is marked ``awaiting_confirmation`` and its owner is listed. Where the province, state, emirate or city
is not stored, it is not assumed: the dependent requirements are flagged instead.

What is taken from the stored rows (read-only inspection, 2026-11-04) and preserved: client id, company name, timezone, working days, active flag; the
existing ``client_configs`` row and the draft version 1 are never rewritten (``existing_client_record: true``); this configuration is version 2.
CLIENT_DUBAI's two stored ``[SAMPLE]`` holidays are reproduced exactly so they are a no-op, not a conflict.
"""

from __future__ import annotations

import json
from pathlib import Path

import build_profiles as bp
from build_profiles import SYN, app, doc, expiry, item, policy, program, task

HERE = Path(__file__).resolve().parent
OUT = HERE / "profiles" / "existing_mock_clients"
NOW = "2026-11-04T00:00:00+00:00"
AWAIT = "awaiting_confirmation"

bp.TEAMS.update({"immigration": "Immigration / Global Mobility (synthetic)", "benefits": "Benefits Administration (synthetic)",
                 "security": "Information Security (synthetic)", "facilities_access": "Facilities and Physical Access (synthetic)",
                 "training": "Learning and Development (synthetic)"})


def T(key, cat, owner, offset, deps=(), *, team=None, blocking=True, awaiting=False, **kw):
    t = task(key, cat, owner, offset, deps, team=team or owner, **kw)
    t["blocking"] = blocking
    t["awaiting_confirmation"] = awaiting
    if awaiting:
        t["description"] = f"{key.replace('_', ' ')} - rule {AWAIT} with the responsible owner ({SYN})"
    return t


def D(key, name, cat, mandatory, *, country, stage="pre_joining", team="hr_ops", blocking=True, awaiting=False, extra=None, **kw):
    e = {"applicable_country": country, "submission_stage": stage, "responsible_team": bp.TEAMS[team], "blocking": blocking,
         "awaiting_confirmation": awaiting, **(extra or {})}
    return doc(key, name, cat, mandatory, extra=e, **kw)


def I(code, name, kind, category, *, owner="it_manager", approval="STANDARD", mandatory=True, dep="hr_record_create", evidence="provisioning_confirmation"):
    x = item(code, name, kind, category)
    x.update({"owner_role": owner, "approval_class": approval, "mandatory": mandatory, "provisioning_dependency": dep, "evidence_requirement": evidence})
    return x


def approvals(seq):
    """(action, approver, delegate, escalation owner, team) -> enriched approval matrix rows."""
    return [{"action": a, "approver_role": r, "delegate_role": dlg, "separation_of_duties": True, "approval_type": a, "approval_sequence": n,
             "responsible_team": bp.TEAMS[team], "escalation_owner": esc, "reminder_interval_working_days": 1,
             "notification_event": f"{a}.requested", "rejection_handling": "reason_required_then_stop_or_rework",
             "change_request_handling": "new_version_and_new_request", "approval_evidence": "signed_callback_with_approver_and_timestamp"}
            for n, (a, r, dlg, esc, team) in enumerate(seq, 1)]


def finish(spec: dict, extra: dict) -> dict:
    d = bp.build(spec)
    m = d["client_master"]
    m.update(configuration_version=2, effective_from="2026-11-04", effective_until=None, approval_status="approved",
             approved_by="synthetic-fixture (development placeholder; not a client approval)", approved_at=NOW, created_at=NOW, updated_at=NOW,
             environment="mock", synthetic=True, existing_client_record=True,
             data_source=("Existing mock client row (client_configs, read-only 2026-11-04) + synthetic test assumptions authored for UC-02 "
                          "verification; not supplied or approved by the client"),
             synthetic_notice="SYNTHETIC / MOCK configuration for an existing mock client. Not an approved client policy, not legal advice.")
    cal = d["shared_configuration"]["calendar"]
    cal.update(extra.pop("calendar"))
    d["shared_configuration"]["approval_matrix"] = extra.pop("approvals")
    d["uc01_employee_record"].update(extra.pop("uc01"))
    d["uc02_onboarding_tasks"].update(extra.pop("uc02"))
    assert not extra, extra
    return d


def assign_for(tasks):
    """One assignment rule per task category: the team of the first task in that category, and its downstream use case."""
    label_to_key = {v: k for k, v in bp.TEAMS.items()}
    out, seen = [], set()
    for t in tasks:
        if t["category"] in seen:
            continue
        seen.add(t["category"])
        out.append((t["category"], label_to_key[t["responsible_team"]], t["downstream_use_case"]))
    return out


COMMON_FIELDS = ["legal_name", "date_of_birth", "nationality", "country_of_residence", "contact_details", "emergency_contact", "employee_id",
                 "job_title", "role_id", "grade", "department", "reporting_manager_id", "employment_type", "work_country", "work_location",
                 "work_arrangement", "joining_date", "contract_type", "probation_period", "working_days_and_hours"]
EXTRA_FIELDS = [{"field": f, "label": f.replace("_", " ").capitalize(), "required": f not in ("emergency_contact", "probation_period"),
                 "type": "date" if f.endswith("date") or f == "date_of_birth" else "string",
                 "sensitive": f in ("date_of_birth", "contact_details", "emergency_contact", "legal_name")} for f in COMMON_FIELDS]
REQUIRED_FOR_CHECKLIST = ["client_id", "joining_date", "role_id", "grade", "work_location", "nationality_group", "employment_type"]


def base_spec(**kw):
    """The fields bp.build needs, with per-client values supplied by the caller."""
    spec = dict(language="en", eligibility=[], extra_fields=EXTRA_FIELDS)
    spec.update(kw)
    spec["assign"] = assign_for(spec["tasks"])
    return spec


# =================================================================================================================================
# CLIENT_DUBAI - UAE employment; visa route by current location; medical fitness; Emirates ID / residency processing
DUBAI_TASKS = [
    T("hr_record_create", "hr_record", "hr_ops", -15, team="hr_ops", prio="high"),
    T("collect_documents", "document_collection", "hr_ops", -13, ["hr_record_create"], team="hr_ops", uc="UC-03"),
    T("background_verification", "background_verification", "hr_officer", -12, ["collect_documents"], team="hr_officer", sla=7),
    # visa route chosen by the employee's stated route (Framework DL-09); UAE/GCC nationals need none
    T("entry_permit", "visa", "pro", -14, ["collect_documents"], team="pro", prio="critical", a=app(attrs=["visa_route:overseas_entry"])),
    T("medical_fitness_test_on_arrival", "medical", "pro", -4, ["entry_permit"], team="pro", a=app(attrs=["visa_route:overseas_entry"])),
    T("emirates_id_biometrics_on_arrival", "biometrics", "pro", -3, ["medical_fitness_test_on_arrival"], team="pro", a=app(attrs=["visa_route:overseas_entry"])),
    T("residence_visa_stamping_on_arrival", "visa", "pro", -1, ["emirates_id_biometrics_on_arrival"], team="pro", awaiting=True,
      a=app(attrs=["visa_route:overseas_entry"])),
    T("visa_status_change", "visa", "pro", -10, ["collect_documents"], team="pro", prio="high", a=app(attrs=["visa_route:in_country_transfer"])),
    T("medical_fitness_test", "medical", "pro", -7, ["visa_status_change"], team="pro", a=app(attrs=["visa_route:in_country_transfer"])),
    T("emirates_id_biometrics", "biometrics", "pro", -5, ["medical_fitness_test"], team="pro", a=app(attrs=["visa_route:in_country_transfer"])),
    T("residence_visa_stamping", "visa", "pro", -3, ["emirates_id_biometrics"], team="pro", awaiting=True, a=app(attrs=["visa_route:in_country_transfer"])),
    T("flight_booking", "travel", "facilities", -9, ["entry_permit"], team="facilities", approval="hr_manager",
      a=app(attrs=["visa_route:overseas_entry", "needs_travel"])),
    T("temporary_accommodation", "accommodation", "facilities", -6, ["flight_booking"], team="facilities", approval="hr_manager",
      a=app(attrs=["needs_accommodation"], grades=["G5", "G6", "G7"])),
    T("health_insurance_enrolment", "insurance", "hr_ops", -5, ["collect_documents"], team="benefits"),
    T("dependant_insurance_enrolment", "insurance", "hr_ops", -3, ["health_insurance_enrolment"], team="benefits", approval="hr_manager", blocking=False,
      a=app(attrs=["family_cover"], grades=["G6", "G7"])),
    T("payroll_setup", "payroll", "finance", -5, ["collect_documents"], team="finance", approval="hr_manager", evidence=True),
    T("bank_account_setup", "payroll", "finance", -4, ["payroll_setup"], team="finance", evidence=True),
    T("salary_transfer_letter", "banking_letter", "finance", -2, ["bank_account_setup"], team="finance", blocking=False,
      a=app(attrs=["salary_transfer_letter_requested"])),
    T("laptop_request", "it_provisioning", "it_manager", -8, ["hr_record_create"], team="it_manager", uc="UC-04", approval="manager"),
    T("system_access", "access_management", "it_manager", -3, ["laptop_request"], team="it_manager", uc="UC-04", approval="manager", evidence=True),
    T("privileged_access_request", "access_management", "it_manager", -2, ["system_access"], team="security", uc="UC-04", approval="hr_lead",
      evidence=True, a=app(roles=["senior_manager"], grades=["G7"])),
    T("office_access_card", "workplace", "facilities", -2, ["hr_record_create"], team="facilities_access"),
    T("company_induction", "orientation", "hr_ops", 0, ["hr_record_create"], team="training", uc="UC-05"),
    T("department_orientation", "orientation", "manager", 1, ["company_induction"], team="manager", uc="UC-05"),
    T("information_security_training", "orientation", "hr_ops", 3, ["company_induction"], team="security", uc="UC-05", evidence=True),
    T("policy_acknowledgement", "policy_acknowledgement", "employee", 2, ["company_induction"], team="employee", uc="UC-05", evidence=True),
]
DUBAI = base_spec(
    id="CLIENT_DUBAI", name="Dubai Mock Technologies LLC", scenario="UAE employment, Dubai; visa route by current location (synthetic)",
    country={"code": "AE", "name": "United Arab Emirates"}, tz="Asia/Dubai", working_days=[1, 2, 3, 4, 5], holidays=[], teams=list(bp.TEAMS),
    locations=[{"code": "AE-DXB", "name": "Dubai", "country_code": "AE", "work_location_type": "OFFICE"}],
    delegates={"access_request": "hr_lead", "spend": "hr_lead"}, plan_approver="hr_ops", plan_delegate="hr_lead", approval_sla_hours=24,
    roles=[{"role_id": "software_engineer", "label": "Software Engineer"}, {"role_id": "operations_associate", "label": "Operations Associate"},
           {"role_id": "senior_manager", "label": "Senior Manager"}],
    grades=[{"grade_id": g, "band_order": i} for i, g in enumerate(["G3", "G4", "G5", "G6", "G7"])],
    nat_groups=[{"group_code": "NG-UAE-NATIONAL-SYN", "label": "UAE national (synthetic grouping)", "nationality_codes": ["AE"]},
                {"group_code": "NG-GCC-SYN", "label": "Other GCC (synthetic grouping)", "nationality_codes": ["SA", "KW", "BH", "OM", "QA"]},
                {"group_code": "NG-EXPAT-SYN", "label": "Expatriate (synthetic grouping)", "nationality_codes": ["IN", "PK", "PH", "GB", "US", "CA", "EG"]}],
    employment_types=["full_time", "contract"],
    reminders={"first_after_working_days": 2, "repeat_every_working_days": 2, "handover_to_hr_call_after": 3},
    visa_rule="run_visa_and_background_check_in_parallel_flag_cost_risk_to_hr", day_one_tasks=["laptop_request", "system_access", "payroll_setup"],
    tasks=DUBAI_TASKS,
    docs=[
        D("signed_offer_letter", "Signed offer letter", "offer", True, country="AE", meta=["employee_name", "employer_signature", "date"]),
        D("signed_employment_contract", "Signed employment contract", "agreement", True, country="AE", checks=("signed", "evidence_stored"), awaiting=True,
          meta=["signature", "date"]),
        D("passport", "Passport", "identity", True, country="AE", meta=["passport_number", "nationality", "expiry_date", "full_name"], expiry=expiry(180),
          a=app(nationality_groups=["NG-GCC-SYN", "NG-EXPAT-SYN"]), blocks=["entry_permit", "visa_status_change"]),
        D("national_id_uae", "UAE national identity card", "identity", True, country="AE", a=app(nationality_groups=["NG-UAE-NATIONAL-SYN"]),
          meta=["id_number", "expiry_date"], expiry=expiry(30)),
        D("photo", "Passport-size photograph", "identity", True, country="AE", formats=("jpeg", "png"), checks=("readable",)),
        D("current_residence_visa", "Current UAE residence visa", "visa", True, country="AE", a=app(attrs=["visa_route:in_country_transfer"]),
          meta=["visa_number", "expiry_date", "sponsor"], expiry=expiry(30), deps=["visa_status_change"]),
        D("previous_sponsor_noc", "No-objection from previous sponsor", "visa", True, country="AE", awaiting=True,
          a=app(attrs=["visa_route:in_country_transfer"]), meta=["sponsor", "date"], deps=["visa_status_change"]),
        D("degree_certificate", "Degree certificate", "education", True, country="AE", meta=["institution", "degree_title", "graduate_name"],
          extra={"attestation": {"required": True, "allowed_after_joining": False, "status": AWAIT}}),
        D("experience_letter", "Experience letters", "employment", True, country="AE", meta=["employer_name", "employment_period"]),
        D("background_check_consent", "Background verification consent", "employment", True, country="AE", checks=("signed", "evidence_stored"),
          team="hr_officer", deps=["background_verification"]),
        D("medical_fitness_certificate", "Medical fitness result (HR item)", "medical", True, country="AE", approval=True, stage="post_offer",
          a=app(attrs=["visa_route:overseas_entry"]), extra={"sensitive": "hr_item_only"}, checks=("readable",), deps=["medical_fitness_test_on_arrival"]),
        D("medical_fitness_certificate_transfer", "Medical fitness result (HR item)", "medical", True, country="AE", approval=True, stage="post_offer",
          a=app(attrs=["visa_route:in_country_transfer"]), extra={"sensitive": "hr_item_only"}, checks=("readable",), deps=["medical_fitness_test"]),
        D("dependant_documents", "Dependant documents for insurance", "family", False, country="AE", a=app(attrs=["family_cover"], grades=["G6", "G7"]),
          meta=["full_name", "relationship", "date_of_birth"], team="benefits", blocking=False),
        D("bank_account_details", "Bank account details", "bank_payroll", True, country="AE", team="finance", extra={"sensitive": "hr_payroll_only"},
          checks=("readable",), deps=["bank_account_setup"]),
        D("emergency_contact_form", "Emergency contact information", "identity", True, country="AE", checks=("required_fields_present",), stage="day_one",
          blocking=False),
        D("employee_information_form", "Employee information form", "identity", True, country="AE", checks=("required_fields_present",)),
    ],
    equipment=[I("laptop-standard", "Standard laptop", "equipment", "hardware"), I("monitor-27", "27-inch monitor", "equipment", "hardware", mandatory=False),
               I("keyboard-mouse", "Keyboard and mouse", "equipment", "hardware"), I("headset-basic", "Headset", "equipment", "hardware", mandatory=False),
               I("mobile-device", "Mobile phone", "equipment", "hardware", approval="MANAGER_EXCEPTION")],
    access=[I("email-teams-account", "Email and Teams", "access", "software_access"), I("vpn-access", "VPN", "access", "software_access"),
            I("source-control-access", "Source-code repository", "access", "software_access"), I("ide-dev-tools", "IDE and development tools", "access", "software_access"),
            I("ops-dashboard-access", "Operations dashboard", "access", "software_access"), I("office-entry-card", "Office access card", "access", "physical_access",
                                                                                                    owner="facilities", evidence="handover_confirmation"),
            I("standard-user-access", "Standard user access", "access", "system_permission"),
            I("privileged-admin-access", "Privileged access", "access", "system_permission", approval="SECURITY_GATE")],
    equipment_matrix=[{"role_id": "software_engineer", "grades": [], "items": ["laptop-standard", "monitor-27", "keyboard-mouse", "headset-basic"]},
                      {"role_id": "operations_associate", "grades": [], "items": ["laptop-standard", "keyboard-mouse", "mobile-device"]},
                      {"role_id": "senior_manager", "grades": ["G6", "G7"], "items": ["laptop-standard", "monitor-27", "mobile-device"]}],
    access_matrix=[{"role_id": "software_engineer", "items": ["email-teams-account", "vpn-access", "source-control-access", "ide-dev-tools", "office-entry-card", "standard-user-access"]},
                   {"role_id": "operations_associate", "items": ["email-teams-account", "ops-dashboard-access", "office-entry-card", "standard-user-access"]},
                   {"role_id": "senior_manager", "items": ["email-teams-account", "vpn-access", "office-entry-card", "standard-user-access", "privileged-admin-access"]}],
    approver_config={"ACCESS": {"role": "manager", "delegate_role": "hr_lead"}, "EQUIPMENT": {"role": "manager", "delegate_role": "hr_lead"},
                     "EXCEPTION": {"role": "hr_manager", "delegate_role": None}},
    lead_times={"EQUIPMENT": 10, "ACCESS": 5, "CLIENT_SITE_CLEARANCE": 15}, handover={"mode": "COLLECT", "keep_reserved_on_missed_slot": True},
    programmes=[program("company_induction", "Company induction", ["orientation"], 0), program("department_orientation", "Department orientation", ["team_intro"], 1),
                program("information_security", "Information security training", ["induction"], 5, assessment={"required": True, "pass_mark_percent": 80}),
                program("engineering_onboarding", "Engineering role training", ["induction"], 10, a=app(roles=["software_engineer"])),
                program("leadership_onboarding", "Leadership onboarding", ["induction"], 10, a=app(roles=["senior_manager"]))],
    policies=[policy("code_of_conduct", "Code of conduct", True, 2), policy("confidentiality", "Confidentiality agreement", True, 0),
              policy("acceptable_use", "Acceptable use policy", True, 3), policy("data_protection", "Data protection policy", True, 3),
              policy("attendance_and_leave", "Attendance and leave policy", True, 5), policy("health_and_safety", "Health and safety policy", True, 5)],
    awaiting=bp.GEN_AWAITING + [{"item": "UAE immigration, medical fitness, Emirates ID and residency steps, documents and timing", "owning_team": "PRO / UAE country compliance pack owner"},
                                {"item": "Payroll frequency and benefit linkage", "owning_team": "client payroll"}],
)
DUBAI_EXTRA = dict(
    calendar={"weekend_days": [6, 7], "working_hours": {"start": "09:00", "end": "18:00"}, "observed_holiday_rule": AWAIT,
              "holiday_source_note": "Only the two [SAMPLE] dates already stored for this client; the full UAE calendar is awaiting confirmation"},
    approvals=approvals([("plan_approval", "hr_ops", "hr_lead", "hr_lead", "hr_ops"), ("send_to_joiner", "hr_officer", "hr_ops", "hr_ops", "hr_officer"),
                         ("visa_spend", "hr_manager", "hr_lead", "hr_lead", "pro"), ("access_request", "manager", "hr_lead", "hr_ops", "it_manager"),
                         ("payroll_record", "hr_manager", "hr_lead", "hr_lead", "finance")]),
    uc01={"work_locations": [{"code": "AE-DXB", "name": "Dubai", "country_code": "AE"}],
          "checklist_attributes": [{"family": "visa_route", "values": ["not_required", "in_country_transfer", "overseas_entry"], "required": True,
                                    "reason": "decides the UAE work-authorisation route; never inferred from nationality (synthetic)"}],
          "required_for_checklist": REQUIRED_FOR_CHECKLIST + ["visa_route"], "contract_types": ["unlimited", "fixed_term"],
          "work_arrangements": ["onsite", "hybrid"], "probation": {"months": AWAIT}, "working_hours": {"weekly_hours": AWAIT}},
    uc02={"immigration_configuration": {"routes": {"not_required": [], "in_country_transfer": ["visa_status_change", "medical_fitness_test", "emirates_id_biometrics", "residence_visa_stamping"],
                                                   "overseas_entry": ["entry_permit", "medical_fitness_test_on_arrival", "emirates_id_biometrics_on_arrival", "residence_visa_stamping_on_arrival"]},
                                        "missing_route": "hr_review", "legal_basis": AWAIT, "owner": "pro"},
          "medical_insurance_configuration": {"pre_employment_medical": "only on a visa route", "health_insurance": "all employees", "coverage_category": AWAIT,
                                              "dependant_cover": "grades G6-G7 when family_cover is stated", "owner": "benefits"},
          "payroll_configuration": {"currency": "AED", "frequency": AWAIT, "eligibility": "all employees", "required_forms": ["bank_account_details"],
                                    "bank_verification": "bank_account_setup with evidence", "owner": "finance", "approval": "hr_manager",
                                    "salary_amounts": "never stored in configuration"}},
)

# =================================================================================================================================
# CLIENT_INDIA - domestic employment only; Monday-Saturday; tax identifier and statutory declarations (scheme names awaiting confirmation)
INDIA_TASKS = [
    T("hr_record_create", "hr_record", "hr_ops", -10, team="hr_ops"),
    T("collect_documents", "document_collection", "hr_ops", -8, ["hr_record_create"], team="hr_ops", uc="UC-03"),
    T("background_verification", "background_verification", "hr_officer", -7, ["collect_documents"], team="hr_officer"),
    T("previous_employment_verification", "background_verification", "hr_officer", -6, ["collect_documents"], team="hr_officer",
      a=app(grades=["G4", "G5", "G6"])),
    T("tax_identifier_verification", "payroll", "finance", -6, ["collect_documents"], team="finance", evidence=True),
    T("statutory_declarations", "payroll", "finance", -5, ["tax_identifier_verification"], team="finance", awaiting=True, evidence=True),
    T("bank_account_verification", "payroll", "finance", -5, ["collect_documents"], team="finance", evidence=True),
    T("payroll_setup", "payroll", "finance", -4, ["bank_account_verification", "statutory_declarations"], team="finance", approval="hr_manager", prio="high"),
    T("group_health_insurance_enrolment", "insurance", "hr_ops", -4, ["collect_documents"], team="benefits", a=app(employment_types=["full_time"])),
    T("laptop_request", "it_provisioning", "it_manager", -6, ["hr_record_create"], team="it_manager", uc="UC-04", approval="manager"),
    T("system_access", "access_management", "it_manager", -2, ["laptop_request", "background_verification"], team="it_manager", uc="UC-04",
      approval="manager", evidence=True),
    T("finance_application_access", "access_management", "it_manager", -2, ["system_access"], team="security", uc="UC-04", approval="hr_manager",
      evidence=True, a=app(roles=["finance_analyst"])),
    T("office_access_card", "workplace", "facilities", -1, ["hr_record_create"], team="facilities_access"),
    T("company_induction", "orientation", "hr_ops", 0, ["hr_record_create"], team="training", uc="UC-05"),
    T("department_orientation", "orientation", "manager", 1, ["company_induction"], team="manager", uc="UC-05"),
    T("finance_controls_training", "orientation", "manager", 5, ["company_induction"], team="training", uc="UC-05", a=app(roles=["finance_analyst"])),
    T("data_protection_training", "orientation", "hr_ops", 3, ["company_induction"], team="security", uc="UC-05", evidence=True),
    T("policy_acknowledgement", "policy_acknowledgement", "employee", 2, ["company_induction"], team="employee", uc="UC-05", evidence=True),
]
INDIA = base_spec(
    id="CLIENT_INDIA", name="India Mock Technologies Pvt Ltd", scenario="Domestic employment, India; no immigration route (synthetic)",
    country={"code": "IN", "name": "India"}, tz="Asia/Kolkata", working_days=[1, 2, 3, 4, 5, 6], holidays=["2026-12-25", "2027-01-26"],
    teams=list(bp.TEAMS), locations=[{"code": "IN", "name": "India", "country_code": "IN", "work_location_type": "OFFICE"}],
    delegates={"hris_write": "hr_lead"}, plan_approver="hr_manager", plan_delegate="hr_ops", approval_sla_hours=48,
    roles=[{"role_id": "finance_analyst", "label": "Finance Analyst"}, {"role_id": "software_engineer", "label": "Software Engineer"},
           {"role_id": "operations_associate", "label": "Operations Associate"}],
    grades=[{"grade_id": g, "band_order": i} for i, g in enumerate(["G2", "G3", "G4", "G5", "G6"])],
    nat_groups=[{"group_code": "NG-DOMESTIC-SYN", "label": "Indian national (synthetic grouping)", "nationality_codes": ["IN"]}],
    employment_types=["full_time", "contract"],
    reminders={"first_after_working_days": 3, "repeat_every_working_days": 3, "handover_to_hr_call_after": 2},
    visa_rule="not_applicable_no_visa_route", day_one_tasks=["laptop_request", "payroll_setup", "system_access"], tasks=INDIA_TASKS,
    docs=[
        D("signed_offer_letter", "Signed offer letter", "offer", True, country="IN", meta=["employee_name", "employer_signature", "date"]),
        D("signed_employment_contract", "Signed employment contract", "agreement", True, country="IN", checks=("signed", "evidence_stored"), awaiting=True,
          meta=["signature", "date"]),
        D("national_id", "Government-issued identity document", "identity", True, country="IN", meta=["id_number", "full_name"]),
        D("tax_identifier", "Tax identifier document", "tax", True, country="IN", team="finance", meta=["tax_id", "full_name"],
          extra={"sensitive": "hr_payroll_only"}, deps=["tax_identifier_verification"]),
        D("address_proof", "Proof of address", "identity", True, country="IN", meta=["address"]),
        D("photo", "Photograph", "identity", True, country="IN", formats=("jpeg", "png"), checks=("readable",)),
        D("degree_certificate", "Degree certificate", "education", True, country="IN", meta=["institution", "degree_title", "graduate_name"]),
        D("academic_transcripts", "Academic transcripts", "education", False, country="IN", meta=["institution"], blocking=False),
        D("professional_qualification", "Professional qualification", "education", True, country="IN", a=app(roles=["finance_analyst"]),
          meta=["body", "membership"]),
        D("relieving_letter", "Relieving letter from previous employer", "employment", True, country="IN", meta=["employer_name", "employment_period"],
          a=app(grades=["G3", "G4", "G5", "G6"])),
        D("payslips", "Recent payslips", "employment", True, country="IN", meta=["employer_name", "period"], extra={"min_count": AWAIT},
          a=app(grades=["G3", "G4", "G5", "G6"])),
        D("background_check_consent", "Background verification consent", "employment", True, country="IN", checks=("signed", "evidence_stored"),
          team="hr_officer"),
        D("bank_account_details", "Bank account details with verification evidence", "bank_payroll", True, country="IN", team="finance",
          extra={"sensitive": "hr_payroll_only"}, checks=("readable",), deps=["bank_account_verification"]),
        D("statutory_declaration_forms", "Statutory declaration forms", "tax", True, country="IN", team="finance", awaiting=True,
          checks=("signed", "evidence_stored"), deps=["statutory_declarations"]),
        D("insurance_nomination", "Group insurance nomination", "insurance", False, country="IN", team="benefits", blocking=False,
          a=app(employment_types=["full_time"])),
        D("emergency_contact_form", "Emergency contact information", "identity", True, country="IN", checks=("required_fields_present",), stage="day_one",
          blocking=False),
    ],
    equipment=[I("laptop-standard", "Standard laptop", "equipment", "hardware"), I("keyboard-mouse", "Keyboard and mouse", "equipment", "hardware"),
               I("headset-basic", "Headset", "equipment", "hardware", mandatory=False)],
    access=[I("email-teams-account", "Email and Teams", "access", "software_access"), I("vpn-access", "VPN", "access", "software_access"),
            I("erp-finance-access", "Finance application (ERP) access", "access", "software_access", approval="MANAGER_EXCEPTION"),
            I("source-control-access", "Source-code repository", "access", "software_access"),
            I("office-entry-card", "Office access card", "access", "physical_access", owner="facilities", evidence="handover_confirmation"),
            I("standard-user-access", "Standard user access", "access", "system_permission")],
    equipment_matrix=[{"role_id": "finance_analyst", "grades": [], "items": ["laptop-standard", "keyboard-mouse"]},
                      {"role_id": "software_engineer", "grades": [], "items": ["laptop-standard", "keyboard-mouse", "headset-basic"]},
                      {"role_id": "operations_associate", "grades": [], "items": ["laptop-standard"]}],
    access_matrix=[{"role_id": "finance_analyst", "items": ["email-teams-account", "erp-finance-access", "office-entry-card", "standard-user-access"]},
                   {"role_id": "software_engineer", "items": ["email-teams-account", "vpn-access", "source-control-access", "office-entry-card", "standard-user-access"]},
                   {"role_id": "operations_associate", "items": ["email-teams-account", "office-entry-card", "standard-user-access"]}],
    approver_config={"ACCESS": {"role": "manager", "delegate_role": None}, "EQUIPMENT": {"role": "manager", "delegate_role": None},
                     "EXCEPTION": {"role": "hr_lead", "delegate_role": None}},
    lead_times={"EQUIPMENT": 7, "ACCESS": 5, "CLIENT_SITE_CLEARANCE": 10}, handover={"mode": "DELIVER", "keep_reserved_on_missed_slot": False},
    programmes=[program("company_induction", "Company induction", ["orientation"], 0), program("department_orientation", "Department orientation", ["team_intro"], 1),
                program("data_protection", "Data protection training", ["induction"], 5, assessment={"required": True, "pass_mark_percent": 75}),
                program("finance_controls", "Finance controls training", ["induction"], 10, a=app(roles=["finance_analyst"]))],
    policies=[policy("code_of_conduct", "Code of conduct", True, 2), policy("confidentiality", "Confidentiality agreement", True, 0),
              policy("acceptable_use", "Acceptable use policy", True, 3), policy("leave_and_attendance", "Leave and attendance policy", True, 5),
              policy("data_protection", "Data protection policy", True, 3)],
    awaiting=bp.GEN_AWAITING + [{"item": "Statutory declaration schemes, forms and timelines for India", "owning_team": "client payroll / India compliance pack owner"},
                                {"item": "Work city (only the country is stored)", "owning_team": "client HR"}],
)
INDIA_EXTRA = dict(
    calendar={"weekend_days": [7], "working_hours": {"start": "09:30", "end": "18:00"}, "observed_holiday_rule": AWAIT,
              "holiday_source_note": "[SAMPLE] dates only; the client's holiday list is awaiting confirmation"},
    approvals=approvals([("plan_approval", "hr_manager", "hr_ops", "hr_lead", "hr_ops"), ("send_to_joiner", "hr_officer", "hr_ops", "hr_ops", "hr_officer"),
                         ("payroll_record", "hr_manager", "hr_lead", "hr_lead", "finance"), ("access_request", "manager", None, "hr_ops", "it_manager")]),
    uc01={"work_locations": [{"code": "IN", "name": "India", "country_code": "IN"}], "checklist_attributes": [],
          "required_for_checklist": REQUIRED_FOR_CHECKLIST, "contract_types": ["permanent", "fixed_term"], "work_arrangements": ["onsite", "hybrid"],
          "probation": {"months": AWAIT}, "working_hours": {"weekly_hours": AWAIT}},
    uc02={"immigration_configuration": {"routes": {}, "policy": "domestic employment only; no immigration task is configured", "owner": None},
          "medical_insurance_configuration": {"pre_employment_medical": "not configured", "health_insurance": "full-time employees (group cover)",
                                              "coverage_category": AWAIT, "owner": "benefits"},
          "payroll_configuration": {"currency": "INR", "frequency": AWAIT, "eligibility": "all employees", "required_forms": ["tax_identifier", "statutory_declaration_forms",
                                    "bank_account_details"], "bank_verification": "bank_account_verification with evidence", "owner": "finance",
                                    "approval": "hr_manager", "tax_rates_and_contributions": "never stored in configuration"}},
)

# =================================================================================================================================
# CLIENT_CANADA - domestic employment; work authorisation by stated status; province not stored (provincial items flagged)
CANADA_TASKS = [
    T("hr_record_create", "hr_record", "hr_ops", -10, team="hr_ops"),
    T("collect_documents", "document_collection", "hr_ops", -8, ["hr_record_create"], team="hr_ops", uc="UC-03"),
    T("work_authorization_check", "hr_record", "hr_officer", -8, ["collect_documents"], team="immigration", evidence=True, awaiting=True),
    T("work_permit_verification", "visa", "hr_officer", -7, ["work_authorization_check"], team="immigration", evidence=True,
      a=app(attrs=["work_authorization:work_permit_holder"])),
    T("work_permit_application_support", "visa", "hr_officer", -15, ["work_authorization_check"], team="immigration", approval="hr_manager",
      prio="critical", awaiting=True, a=app(attrs=["work_authorization:permit_required"])),
    T("background_verification", "background_verification", "hr_officer", -7, ["collect_documents"], team="hr_officer"),
    T("reference_check", "background_verification", "hr_officer", -6, ["collect_documents"], team="hr_officer", a=app(grades=["L4", "L5"])),
    T("tax_onboarding_forms", "payroll", "finance", -5, ["collect_documents"], team="finance", awaiting=True, evidence=True),
    T("direct_deposit_setup", "payroll", "finance", -4, ["collect_documents"], team="finance", evidence=True),
    T("payroll_setup", "payroll", "finance", -3, ["tax_onboarding_forms", "direct_deposit_setup"], team="finance", approval="hr_manager", prio="high"),
    T("benefits_enrolment", "insurance", "hr_ops", -3, ["collect_documents"], team="benefits", a=app(employment_types=["full_time"])),
    T("laptop_request", "it_provisioning", "it_manager", -6, ["hr_record_create"], team="it_manager", uc="UC-04", approval="manager"),
    T("system_access", "access_management", "it_manager", -2, ["laptop_request"], team="it_manager", uc="UC-04", approval="manager", evidence=True),
    T("remote_equipment_shipping", "it_provisioning", "it_manager", -8, ["laptop_request"], team="it_manager", uc="UC-04", blocking=False,
      a=app(attrs=["work_arrangement:remote"])),
    T("company_induction", "orientation", "hr_ops", 0, ["hr_record_create"], team="training", uc="UC-05"),
    T("department_orientation", "orientation", "manager", 1, ["company_induction"], team="manager", uc="UC-05"),
    T("workplace_health_and_safety_training", "orientation", "hr_ops", 3, ["company_induction"], team="training", uc="UC-05", evidence=True,
      awaiting=True),
    T("information_security_training", "orientation", "hr_ops", 3, ["company_induction"], team="security", uc="UC-05", evidence=True),
    T("policy_acknowledgement", "policy_acknowledgement", "employee", 2, ["company_induction"], team="employee", uc="UC-05", evidence=True),
]
CANADA = base_spec(
    id="CLIENT_CANADA", name="Canada Mock Technologies Inc", scenario="Domestic employment, Canada; province not stored (synthetic)",
    country={"code": "CA", "name": "Canada"}, tz="America/Toronto", working_days=[1, 2, 3, 4, 5], holidays=["2026-12-25", "2026-12-28", "2027-01-01"],
    teams=list(bp.TEAMS), locations=[{"code": "CA", "name": "Canada", "country_code": "CA", "work_location_type": "OFFICE"}],
    delegates={"access_request": "hr_ops"}, plan_approver="hr_ops", plan_delegate="hr_manager", approval_sla_hours=24,
    roles=[{"role_id": "software_engineer", "label": "Software Engineer"}, {"role_id": "customer_success_specialist", "label": "Customer Success Specialist"},
           {"role_id": "finance_analyst", "label": "Finance Analyst"}],
    grades=[{"grade_id": g, "band_order": i} for i, g in enumerate(["L1", "L2", "L3", "L4", "L5"])],
    nat_groups=[{"group_code": "NG-CA-SYN", "label": "Canadian (synthetic grouping)", "nationality_codes": ["CA"]},
                {"group_code": "NG-INTL-SYN", "label": "Other nationalities (synthetic grouping)", "nationality_codes": ["IN", "US", "GB", "PH", "FR", "CN"]}],
    employment_types=["full_time", "part_time", "contract"],
    reminders={"first_after_working_days": 2, "repeat_every_working_days": 3, "handover_to_hr_call_after": 3},
    visa_rule="not_applicable_no_visa_route", day_one_tasks=["laptop_request", "system_access", "payroll_setup"], tasks=CANADA_TASKS,
    docs=[
        D("signed_offer_letter", "Signed offer letter", "offer", True, country="CA", meta=["employee_name", "employer_signature", "date"]),
        D("signed_employment_contract", "Signed employment agreement", "agreement", True, country="CA", checks=("signed", "evidence_stored"), awaiting=True,
          meta=["signature", "date"]),
        D("government_photo_id", "Government-issued photo identity", "identity", True, country="CA", meta=["id_number", "full_name", "expiry_date"],
          expiry=expiry(0)),
        D("work_authorization_evidence", "Work authorisation evidence", "visa", True, country="CA", team="immigration", awaiting=True,
          meta=["status", "expiry_date"], deps=["work_authorization_check"], blocks=["work_authorization_check"]),
        D("work_permit", "Work permit", "visa", True, country="CA", team="immigration", a=app(attrs=["work_authorization:work_permit_holder"]),
          meta=["permit_number", "expiry_date", "conditions"], expiry=expiry(90), deps=["work_permit_verification"]),
        D("passport", "Passport", "identity", True, country="CA", a=app(attrs=["work_authorization:work_permit_holder"]),
          meta=["passport_number", "expiry_date"], expiry=expiry(180)),
        D("degree_certificate", "Degree certificate", "education", False, country="CA", meta=["institution", "graduate_name"], blocking=False),
        D("professional_qualification", "Professional designation", "education", True, country="CA", a=app(roles=["finance_analyst"]),
          meta=["body", "membership"]),
        D("employment_history", "Employment history", "employment", True, country="CA", meta=["employer_name", "employment_period"]),
        D("reference_contacts", "Reference contacts", "employment", True, country="CA", a=app(grades=["L4", "L5"]), meta=["referee"],
          deps=["reference_check"]),
        D("background_check_consent", "Background check consent", "employment", True, country="CA", checks=("signed", "evidence_stored"), team="hr_officer"),
        D("tax_onboarding_forms", "Federal and provincial tax onboarding forms", "tax", True, country="CA", team="finance", awaiting=True,
          extra={"province": AWAIT, "sensitive": "hr_payroll_only"}, deps=["tax_onboarding_forms"]),
        D("direct_deposit_details", "Direct deposit details", "bank_payroll", True, country="CA", team="finance", extra={"sensitive": "hr_payroll_only"},
          checks=("readable",), deps=["direct_deposit_setup"]),
        D("benefits_enrolment_form", "Benefits enrolment form", "insurance", True, country="CA", team="benefits", a=app(employment_types=["full_time"]),
          meta=["plan_selection"]),
        D("dependant_details", "Dependant details for benefits", "family", False, country="CA", team="benefits", a=app(attrs=["family_cover"], employment_types=["full_time"]),
          blocking=False, meta=["full_name", "relationship"]),
        D("emergency_contact_form", "Emergency contact information", "identity", True, country="CA", checks=("required_fields_present",), stage="day_one",
          blocking=False),
    ],
    equipment=[I("laptop-standard", "Standard laptop", "equipment", "hardware"), I("monitor-24", "24-inch monitor", "equipment", "hardware", mandatory=False),
               I("headset-basic", "Headset", "equipment", "hardware")],
    access=[I("email-teams-account", "Email and Teams", "access", "software_access"), I("vpn-access", "VPN", "access", "software_access"),
            I("source-control-access", "Source-code repository", "access", "software_access"), I("crm-access", "Customer relationship system", "access", "software_access"),
            I("finance-app-access", "Finance applications", "access", "software_access", approval="MANAGER_EXCEPTION"),
            I("building-access-badge", "Building access badge", "access", "physical_access", owner="facilities", evidence="handover_confirmation"),
            I("standard-user-access", "Standard user access", "access", "system_permission")],
    equipment_matrix=[{"role_id": "software_engineer", "grades": [], "items": ["laptop-standard", "monitor-24", "headset-basic"]},
                      {"role_id": "customer_success_specialist", "grades": [], "items": ["laptop-standard", "headset-basic"]},
                      {"role_id": "finance_analyst", "grades": [], "items": ["laptop-standard", "monitor-24"]}],
    access_matrix=[{"role_id": "software_engineer", "items": ["email-teams-account", "vpn-access", "source-control-access", "building-access-badge", "standard-user-access"]},
                   {"role_id": "customer_success_specialist", "items": ["email-teams-account", "crm-access", "building-access-badge", "standard-user-access"]},
                   {"role_id": "finance_analyst", "items": ["email-teams-account", "vpn-access", "finance-app-access", "building-access-badge", "standard-user-access"]}],
    approver_config={"ACCESS": {"role": "manager", "delegate_role": "hr_ops"}, "EQUIPMENT": {"role": "manager", "delegate_role": None},
                     "EXCEPTION": {"role": "hr_manager", "delegate_role": None}},
    lead_times={"EQUIPMENT": 8, "ACCESS": 4, "CLIENT_SITE_CLEARANCE": 10}, handover={"mode": "DELIVER", "keep_reserved_on_missed_slot": True},
    country_rules={"country_code": "CA", "courier_extra_days": 2, "remote_first_day_required": True},
    programmes=[program("company_induction", "Company induction", ["orientation"], 0), program("department_orientation", "Department orientation", ["team_intro"], 1),
                program("workplace_health_and_safety", "Workplace health and safety", ["induction"], 5, evidence="attendance_and_assessment_record",
                        assessment={"required": True, "pass_mark_percent": 80}),
                program("information_security", "Information security training", ["induction"], 5),
                program("remote_work_onboarding", "Remote-work onboarding", ["induction"], 2, a=app(attrs=["work_arrangement:remote"]))],
    policies=[policy("code_of_conduct", "Code of conduct", True, 2), policy("confidentiality", "Confidentiality agreement", True, 0),
              policy("acceptable_use", "Acceptable use policy", True, 3), policy("remote_work", "Remote-work policy", True, 3, a=app(attrs=["work_arrangement:remote"])),
              policy("attendance_and_leave", "Attendance and leave policy", True, 5)],
    awaiting=bp.GEN_AWAITING + [{"item": "Work province (only the country is stored); provincial tax and health-and-safety items depend on it", "owning_team": "client HR"},
                                {"item": "Canadian work-authorisation evidence rules and permit support", "owning_team": "immigration counsel / Canada compliance pack owner"}],
)
CANADA_EXTRA = dict(
    calendar={"weekend_days": [6, 7], "working_hours": {"start": "09:00", "end": "17:00"}, "observed_holiday_rule": "synthetic: next working day when on a weekend",
              "holiday_source_note": "[SAMPLE] dates; federal/provincial holidays awaiting confirmation"},
    approvals=approvals([("plan_approval", "hr_ops", "hr_manager", "hr_manager", "hr_ops"), ("send_to_joiner", "hr_officer", "hr_ops", "hr_ops", "hr_officer"),
                         ("permit_support", "hr_manager", None, "hr_lead", "immigration"), ("payroll_record", "hr_manager", "hr_lead", "hr_lead", "finance"),
                         ("access_request", "manager", "hr_ops", "hr_ops", "it_manager")]),
    uc01={"work_locations": [{"code": "CA", "name": "Canada", "country_code": "CA"}],
          "checklist_attributes": [{"family": "work_authorization", "values": ["citizen", "permanent_resident", "work_permit_holder", "permit_required"],
                                    "required": True, "reason": "decides whether permit tasks apply; missing status must go to HR review (synthetic)"},
                                   {"family": "work_province", "values": [], "required": True, "reason": "provincial tax and safety items depend on it; not stored"},
                                   {"family": "work_arrangement", "values": ["onsite", "hybrid", "remote"], "required": False, "reason": "remote equipment and policy"}],
          "required_for_checklist": REQUIRED_FOR_CHECKLIST + ["work_authorization", "work_province"], "contract_types": ["permanent", "fixed_term"],
          "work_arrangements": ["onsite", "hybrid", "remote"], "probation": {"months": AWAIT}, "working_hours": {"weekly_hours": AWAIT}},
    uc02={"immigration_configuration": {"routes": {"citizen": [], "permanent_resident": [], "work_permit_holder": ["work_permit_verification"],
                                                   "permit_required": ["work_permit_application_support"]},
                                        "missing_status": "hr_review", "legal_basis": AWAIT, "owner": "immigration"},
          "medical_insurance_configuration": {"pre_employment_medical": "not configured", "health_insurance": "benefits enrolment for full-time employees",
                                              "coverage_category": AWAIT, "dependant_cover": "when family_cover is stated", "owner": "benefits"},
          "payroll_configuration": {"currency": "CAD", "frequency": AWAIT, "eligibility": "all employees", "required_forms": ["tax_onboarding_forms", "direct_deposit_details"],
                                    "bank_verification": "direct_deposit_setup with evidence", "owner": "finance", "approval": "hr_manager",
                                    "benefits_eligibility": "full_time", "tax_rates": "never stored in configuration"}},
)

# =================================================================================================================================
# CLIENT_USA - domestic employment; employment-eligibility verification for everyone; state not stored (state items flagged)
USA_TASKS = [
    T("hr_record_create", "hr_record", "hr_ops", -10, team="hr_ops"),
    T("collect_documents", "document_collection", "hr_ops", -8, ["hr_record_create"], team="hr_ops", uc="UC-03"),
    T("employment_eligibility_verification", "hr_record", "hr_officer", 2, ["collect_documents"], team="hr_officer", evidence=True, awaiting=True,
      prio="high"),
    T("work_visa_status_verification", "visa", "hr_officer", -7, ["collect_documents"], team="immigration", evidence=True,
      a=app(attrs=["work_authorization:authorized_visa_holder"])),
    T("visa_sponsorship_case", "visa", "hr_officer", -20, ["collect_documents"], team="immigration", approval="hr_manager", prio="critical", awaiting=True,
      a=app(attrs=["work_authorization:sponsorship_required"])),
    T("background_check", "background_verification", "hr_officer", -9, ["collect_documents"], team="hr_officer", sla=7),
    T("federal_tax_withholding_setup", "payroll", "finance", -4, ["collect_documents"], team="finance", evidence=True, awaiting=True),
    T("state_tax_withholding_setup", "payroll", "finance", -4, ["collect_documents"], team="finance", evidence=True, awaiting=True),
    T("direct_deposit_verification", "payroll", "finance", -4, ["collect_documents"], team="finance", evidence=True),
    T("payroll_setup", "payroll", "finance", -3, ["federal_tax_withholding_setup", "state_tax_withholding_setup", "direct_deposit_verification"],
      team="finance", approval="hr_manager", prio="high"),
    T("benefits_enrolment", "insurance", "hr_ops", 5, ["hr_record_create"], team="benefits", a=app(employment_types=["full_time"])),
    T("laptop_request", "it_provisioning", "it_manager", -7, ["hr_record_create"], team="it_manager", uc="UC-04", approval="manager"),
    T("system_access", "access_management", "it_manager", -2, ["laptop_request", "background_check"], team="it_manager", uc="UC-04", approval="manager",
      evidence=True),
    T("sales_platform_access", "access_management", "it_manager", -2, ["system_access"], team="it_manager", uc="UC-04", approval="manager",
      a=app(roles=["sales_associate"])),
    T("office_access_card", "workplace", "facilities", -1, ["hr_record_create"], team="facilities_access", a=app(excl=[]) if False else app()),
    T("company_induction", "orientation", "hr_ops", 0, ["hr_record_create"], team="training", uc="UC-05"),
    T("department_orientation", "orientation", "manager", 1, ["company_induction"], team="manager", uc="UC-05"),
    T("information_security_training", "orientation", "hr_ops", 3, ["company_induction"], team="security", uc="UC-05", evidence=True),
    T("anti_harassment_training", "orientation", "hr_ops", 10, ["company_induction"], team="training", uc="UC-05", evidence=True, awaiting=True),
    T("policy_acknowledgement", "policy_acknowledgement", "employee", 2, ["company_induction"], team="employee", uc="UC-05", evidence=True),
]
USA = base_spec(
    id="CLIENT_USA", name="USA Mock Technologies Inc", scenario="Domestic employment, United States; state not stored (synthetic)",
    country={"code": "US", "name": "United States"}, tz="America/New_York", working_days=[1, 2, 3, 4, 5],
    holidays=["2026-11-26", "2026-12-25", "2027-01-01"], teams=list(bp.TEAMS),
    locations=[{"code": "US", "name": "United States", "country_code": "US", "work_location_type": "OFFICE"}],
    delegates={"access_request": "hr_ops", "spend": "hr_lead"}, plan_approver="hr_manager", plan_delegate="hr_lead", approval_sla_hours=24,
    roles=[{"role_id": "software_engineer", "label": "Software Engineer"}, {"role_id": "sales_associate", "label": "Sales Associate"},
           {"role_id": "finance_analyst", "label": "Finance Analyst"}],
    grades=[{"grade_id": g, "band_order": i} for i, g in enumerate(["B1", "B2", "B3", "B4", "B5", "B6"])],
    nat_groups=[{"group_code": "NG-US-SYN", "label": "US citizen (synthetic grouping)", "nationality_codes": ["US"]},
                {"group_code": "NG-INTL-SYN", "label": "Other nationalities (synthetic grouping)", "nationality_codes": ["IN", "CA", "GB", "MX", "CN", "BR"]}],
    employment_types=["full_time", "part_time"],
    reminders={"first_after_working_days": 2, "repeat_every_working_days": 2, "handover_to_hr_call_after": 3},
    visa_rule="not_applicable_no_visa_route", day_one_tasks=["laptop_request", "system_access", "payroll_setup"], tasks=USA_TASKS,
    docs=[
        D("signed_offer_letter", "Signed offer letter", "offer", True, country="US", meta=["employee_name", "employer_signature", "date"]),
        D("employment_eligibility_documents", "Employment eligibility and identity documents", "identity", True, country="US", team="hr_officer",
          awaiting=True, stage="day_one", meta=["document_title", "expiry_date"], deps=["employment_eligibility_verification"]),
        D("work_visa_documents", "Work visa and supporting documents", "visa", True, country="US", team="immigration",
          a=app(attrs=["work_authorization:authorized_visa_holder"]), meta=["visa_type", "expiry_date"], expiry=expiry(90), deps=["work_visa_status_verification"]),
        D("passport", "Passport", "identity", True, country="US", a=app(attrs=["work_authorization:authorized_visa_holder"]), meta=["passport_number", "expiry_date"],
          expiry=expiry(180)),
        D("degree_certificate", "Degree certificate", "education", False, country="US", meta=["institution", "graduate_name"], blocking=False),
        D("employment_history", "Employment history", "employment", True, country="US", meta=["employer_name", "employment_period"]),
        D("background_check_consent", "Background check consent and disclosure acknowledgement", "employment", True, country="US", team="hr_officer",
          checks=("signed", "evidence_stored"), awaiting=True, deps=["background_check"], blocks=["background_check"]),
        D("federal_tax_withholding_form", "Federal tax withholding form", "tax", True, country="US", team="finance", awaiting=True,
          extra={"sensitive": "hr_payroll_only"}, deps=["federal_tax_withholding_setup"]),
        D("state_tax_withholding_form", "State tax withholding form", "tax", True, country="US", team="finance", awaiting=True,
          extra={"state": AWAIT, "sensitive": "hr_payroll_only"}, deps=["state_tax_withholding_setup"]),
        D("direct_deposit_authorization", "Direct deposit authorisation", "bank_payroll", True, country="US", team="finance",
          extra={"sensitive": "hr_payroll_only"}, checks=("readable", "signed"), deps=["direct_deposit_verification"]),
        D("benefits_election_form", "Benefits election form", "insurance", True, country="US", team="benefits", a=app(employment_types=["full_time"]),
          stage="post_joining", blocking=False, meta=["plan_selection"]),
        D("dependant_details", "Dependant details for benefits", "family", False, country="US", team="benefits",
          a=app(attrs=["family_cover"], employment_types=["full_time"]), blocking=False, meta=["full_name", "relationship"]),
        D("emergency_contact_form", "Emergency contact information", "identity", True, country="US", checks=("required_fields_present",), stage="day_one",
          blocking=False),
    ],
    equipment=[I("laptop-standard", "Standard laptop", "equipment", "hardware"), I("desktop-workstation", "Desktop workstation", "equipment", "hardware", mandatory=False),
               I("monitor-27", "27-inch monitor", "equipment", "hardware", mandatory=False), I("mobile-device", "Mobile phone", "equipment", "hardware",
                                                                                                    approval="MANAGER_EXCEPTION")],
    access=[I("email-teams-account", "Email and Teams", "access", "software_access"), I("vpn-access", "VPN", "access", "software_access"),
            I("source-control-access", "Source-code repository", "access", "software_access"), I("sales-platform-access", "Sales platform", "access", "software_access"),
            I("finance-app-access", "Finance applications", "access", "software_access", approval="MANAGER_EXCEPTION"),
            I("hr-self-service-access", "HR self-service system", "access", "software_access"),
            I("office-access-card", "Office access card", "access", "physical_access", owner="facilities", evidence="handover_confirmation"),
            I("standard-user-access", "Standard user access", "access", "system_permission"),
            I("department-access", "Department access", "access", "system_permission")],
    equipment_matrix=[{"role_id": "software_engineer", "grades": [], "items": ["laptop-standard", "monitor-27"]},
                      {"role_id": "sales_associate", "grades": [], "items": ["laptop-standard", "mobile-device"]},
                      {"role_id": "finance_analyst", "grades": [], "items": ["desktop-workstation", "monitor-27"]}],
    access_matrix=[{"role_id": "software_engineer", "items": ["email-teams-account", "vpn-access", "source-control-access", "hr-self-service-access", "office-access-card", "standard-user-access"]},
                   {"role_id": "sales_associate", "items": ["email-teams-account", "sales-platform-access", "hr-self-service-access", "office-access-card", "standard-user-access"]},
                   {"role_id": "finance_analyst", "items": ["email-teams-account", "finance-app-access", "hr-self-service-access", "office-access-card", "department-access"]}],
    approver_config={"ACCESS": {"role": "manager", "delegate_role": "hr_ops"}, "EQUIPMENT": {"role": "manager", "delegate_role": "hr_ops"},
                     "EXCEPTION": {"role": "hr_manager", "delegate_role": "hr_lead"}},
    lead_times={"EQUIPMENT": 9, "ACCESS": 4, "CLIENT_SITE_CLEARANCE": 12}, handover={"mode": "READY_AT_DESK", "keep_reserved_on_missed_slot": False},
    programmes=[program("company_induction", "Company induction", ["orientation"], 0), program("department_orientation", "Department orientation", ["team_intro"], 1),
                program("information_security", "Information security training", ["induction"], 5, assessment={"required": True, "pass_mark_percent": 80}),
                program("sales_enablement", "Sales enablement", ["induction"], 10, a=app(roles=["sales_associate"])),
                program("workplace_conduct", "Workplace conduct training", ["induction"], 10, evidence="completion_record")],
    policies=[policy("code_of_conduct", "Code of conduct", True, 2), policy("confidentiality", "Confidentiality agreement", True, 0),
              policy("acceptable_use", "Acceptable use policy", True, 3), policy("attendance_and_leave", "Attendance and leave policy", True, 5),
              policy("data_protection", "Data protection policy", True, 3)],
    awaiting=bp.GEN_AWAITING + [{"item": "Work state (only the country is stored); state tax and training items depend on it", "owning_team": "client HR"},
                                {"item": "Employment eligibility verification procedure, documents and timing", "owning_team": "employment counsel / US compliance pack owner"}],
)
USA_EXTRA = dict(
    calendar={"weekend_days": [6, 7], "working_hours": {"start": "09:00", "end": "17:00"}, "observed_holiday_rule": "synthetic: Friday before / Monday after a weekend holiday",
              "holiday_source_note": "[SAMPLE] dates; federal and state holidays awaiting confirmation"},
    approvals=approvals([("plan_approval", "hr_manager", "hr_lead", "hr_lead", "hr_ops"), ("send_to_joiner", "hr_officer", "hr_ops", "hr_ops", "hr_officer"),
                         ("sponsorship", "hr_manager", None, "hr_lead", "immigration"), ("payroll_record", "hr_manager", "hr_lead", "hr_lead", "finance"),
                         ("access_request", "manager", "hr_ops", "hr_ops", "it_manager")]),
    uc01={"work_locations": [{"code": "US", "name": "United States", "country_code": "US"}],
          "checklist_attributes": [{"family": "work_authorization", "values": ["citizen", "permanent_resident", "authorized_visa_holder", "sponsorship_required"],
                                    "required": True, "reason": "decides whether visa tasks apply; missing status must go to HR review (synthetic)"},
                                   {"family": "work_state", "values": [], "required": True, "reason": "state tax and training items depend on it; not stored"}],
          "required_for_checklist": REQUIRED_FOR_CHECKLIST + ["work_authorization", "work_state"], "contract_types": ["at_will", "fixed_term"],
          "work_arrangements": ["onsite", "hybrid", "remote"], "probation": {"months": AWAIT}, "working_hours": {"weekly_hours": AWAIT}},
    uc02={"immigration_configuration": {"routes": {"citizen": [], "permanent_resident": [], "authorized_visa_holder": ["work_visa_status_verification"],
                                                   "sponsorship_required": ["visa_sponsorship_case"]},
                                        "all_employees": ["employment_eligibility_verification"], "missing_status": "hr_review", "legal_basis": AWAIT, "owner": "immigration"},
          "medical_insurance_configuration": {"pre_employment_medical": "not configured", "health_insurance": "benefits enrolment for full-time employees",
                                              "coverage_category": AWAIT, "dependant_cover": "when family_cover is stated", "owner": "benefits"},
          "payroll_configuration": {"currency": "USD", "frequency": AWAIT, "eligibility": "all employees",
                                    "required_forms": ["federal_tax_withholding_form", "state_tax_withholding_form", "direct_deposit_authorization"],
                                    "bank_verification": "direct_deposit_verification with evidence", "owner": "finance", "approval": "hr_manager",
                                    "benefits_eligibility": "full_time", "tax_rates": "never stored in configuration"}},
)

PROFILES = {"client_canada_config.json": (CANADA, CANADA_EXTRA), "client_dubai_config.json": (DUBAI, DUBAI_EXTRA),
            "client_india_config.json": (INDIA, INDIA_EXTRA), "client_usa_config.json": (USA, USA_EXTRA)}
DUBAI_STORED_HOLIDAYS = [{"date": "2026-12-02", "name": "[SAMPLE] UAE National Day", "observed": True},
                         {"date": "2026-12-03", "name": "[SAMPLE] UAE National Day (2nd)", "observed": True}]
HOLIDAY_NAMES = {"2026-12-25": "[SAMPLE] Christmas Day", "2026-12-28": "[SAMPLE] Boxing Day (observed)", "2027-01-01": "[SAMPLE] New Year's Day",
                 "2027-01-26": "[SAMPLE] Republic Day", "2026-11-26": "[SAMPLE] Thanksgiving Day"}


def build_all() -> dict[str, dict]:
    out = {}
    for name, (spec, extra) in PROFILES.items():
        d = finish(dict(spec), json.loads(json.dumps(extra)))
        cal = d["shared_configuration"]["calendar"]
        if d["client_master"]["client_id"] == "CLIENT_DUBAI":
            cal["holidays"] = DUBAI_STORED_HOLIDAYS
        else:
            cal["holidays"] = [{"date": h["date"], "name": HOLIDAY_NAMES[h["date"]] + " (synthetic; not an authoritative calendar)", "observed": True}
                               for h in cal["holidays"]]
        out[name] = d
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, d in build_all().items():
        (OUT / name).write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("wrote", name, len(d["uc02_onboarding_tasks"]["task_templates"]), "tasks", len(d["uc03_document_validation"]["documents"]), "documents")


if __name__ == "__main__":
    main()
