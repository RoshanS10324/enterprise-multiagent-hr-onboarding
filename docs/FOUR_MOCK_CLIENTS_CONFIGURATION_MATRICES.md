# Four existing mock clients: configuration matrices

Generated from `client_configuration/profiles/existing_mock_clients/*.json` (version 2). Every value is a **synthetic / mock assumption**, not a client policy and not a legal determination. `awaiting` = the rule must be confirmed by the named owner (see each profile's `awaiting_confirmation`). Applicability `all` = no filter.

## 1. Document applicability

| Client | Document | Category | Status | Applies when | Stage | Team | Blocking | Expiry | Confirm |
|---|---|---|---|---|---|---|---|---|---|
| Canada | signed_offer_letter | offer | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| Canada | signed_employment_contract | agreement | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - | awaiting |
| Canada | government_photo_id | identity | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | expiry 0d |  |
| Canada | work_authorization_evidence | visa | mandatory | all | pre_joining | Immigration / Global Mobility (synthetic) | blocking | - | awaiting |
| Canada | work_permit | visa | mandatory | requires_attributes=work_authorization:work_permit_holder | pre_joining | Immigration / Global Mobility (synthetic) | blocking | expiry 90d |  |
| Canada | passport | identity | mandatory | requires_attributes=work_authorization:work_permit_holder | pre_joining | HR Operations (synthetic) | blocking | expiry 180d |  |
| Canada | degree_certificate | education | optional | all | pre_joining | HR Operations (synthetic) | non-blocking | - |  |
| Canada | professional_qualification | education | mandatory | roles=finance_analyst | pre_joining | HR Operations (synthetic) | blocking | - |  |
| Canada | employment_history | employment | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| Canada | reference_contacts | employment | mandatory | grades=L4,L5 | pre_joining | HR Operations (synthetic) | blocking | - |  |
| Canada | background_check_consent | employment | mandatory | all | pre_joining | HR Onboarding Officer (synthetic) | blocking | - |  |
| Canada | tax_onboarding_forms | tax | mandatory | all | pre_joining | Payroll (synthetic) | blocking | - | awaiting |
| Canada | direct_deposit_details | bank_payroll | mandatory | all | pre_joining | Payroll (synthetic) | blocking | - |  |
| Canada | benefits_enrolment_form | insurance | mandatory | employment_types=full_time | pre_joining | Benefits Administration (synthetic) | blocking | - |  |
| Canada | dependant_details | family | optional | employment_types=full_time; requires_attributes=family_cover | pre_joining | Benefits Administration (synthetic) | non-blocking | - |  |
| Canada | emergency_contact_form | identity | mandatory | all | day_one | HR Operations (synthetic) | non-blocking | - |  |
| Dubai | signed_offer_letter | offer | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| Dubai | signed_employment_contract | agreement | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - | awaiting |
| Dubai | passport | identity | mandatory | nationality_groups=NG-GCC-SYN,NG-EXPAT-SYN | pre_joining | HR Operations (synthetic) | blocking | expiry 180d |  |
| Dubai | national_id_uae | identity | mandatory | nationality_groups=NG-UAE-NATIONAL-SYN | pre_joining | HR Operations (synthetic) | blocking | expiry 30d |  |
| Dubai | photo | identity | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| Dubai | current_residence_visa | visa | mandatory | requires_attributes=visa_route:in_country_transfer | pre_joining | HR Operations (synthetic) | blocking | expiry 30d |  |
| Dubai | previous_sponsor_noc | visa | mandatory | requires_attributes=visa_route:in_country_transfer | pre_joining | HR Operations (synthetic) | blocking | - | awaiting |
| Dubai | degree_certificate | education | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| Dubai | experience_letter | employment | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| Dubai | background_check_consent | employment | mandatory | all | pre_joining | HR Onboarding Officer (synthetic) | blocking | - |  |
| Dubai | medical_fitness_certificate | medical | mandatory | requires_attributes=visa_route:overseas_entry | post_offer | HR Operations (synthetic) | blocking | - |  |
| Dubai | medical_fitness_certificate_transfer | medical | mandatory | requires_attributes=visa_route:in_country_transfer | post_offer | HR Operations (synthetic) | blocking | - |  |
| Dubai | dependant_documents | family | optional | grades=G6,G7; requires_attributes=family_cover | pre_joining | Benefits Administration (synthetic) | non-blocking | - |  |
| Dubai | bank_account_details | bank_payroll | mandatory | all | pre_joining | Payroll (synthetic) | blocking | - |  |
| Dubai | emergency_contact_form | identity | mandatory | all | day_one | HR Operations (synthetic) | non-blocking | - |  |
| Dubai | employee_information_form | identity | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| India | signed_offer_letter | offer | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| India | signed_employment_contract | agreement | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - | awaiting |
| India | national_id | identity | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| India | tax_identifier | tax | mandatory | all | pre_joining | Payroll (synthetic) | blocking | - |  |
| India | address_proof | identity | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| India | photo | identity | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| India | degree_certificate | education | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| India | academic_transcripts | education | optional | all | pre_joining | HR Operations (synthetic) | non-blocking | - |  |
| India | professional_qualification | education | mandatory | roles=finance_analyst | pre_joining | HR Operations (synthetic) | blocking | - |  |
| India | relieving_letter | employment | mandatory | grades=G3,G4,G5,G6 | pre_joining | HR Operations (synthetic) | blocking | - |  |
| India | payslips | employment | mandatory | grades=G3,G4,G5,G6 | pre_joining | HR Operations (synthetic) | blocking | - |  |
| India | background_check_consent | employment | mandatory | all | pre_joining | HR Onboarding Officer (synthetic) | blocking | - |  |
| India | bank_account_details | bank_payroll | mandatory | all | pre_joining | Payroll (synthetic) | blocking | - |  |
| India | statutory_declaration_forms | tax | mandatory | all | pre_joining | Payroll (synthetic) | blocking | - | awaiting |
| India | insurance_nomination | insurance | optional | employment_types=full_time | pre_joining | Benefits Administration (synthetic) | non-blocking | - |  |
| India | emergency_contact_form | identity | mandatory | all | day_one | HR Operations (synthetic) | non-blocking | - |  |
| USA | signed_offer_letter | offer | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| USA | employment_eligibility_documents | identity | mandatory | all | day_one | HR Onboarding Officer (synthetic) | blocking | - | awaiting |
| USA | work_visa_documents | visa | mandatory | requires_attributes=work_authorization:authorized_visa_holder | pre_joining | Immigration / Global Mobility (synthetic) | blocking | expiry 90d |  |
| USA | passport | identity | mandatory | requires_attributes=work_authorization:authorized_visa_holder | pre_joining | HR Operations (synthetic) | blocking | expiry 180d |  |
| USA | degree_certificate | education | optional | all | pre_joining | HR Operations (synthetic) | non-blocking | - |  |
| USA | employment_history | employment | mandatory | all | pre_joining | HR Operations (synthetic) | blocking | - |  |
| USA | background_check_consent | employment | mandatory | all | pre_joining | HR Onboarding Officer (synthetic) | blocking | - | awaiting |
| USA | federal_tax_withholding_form | tax | mandatory | all | pre_joining | Payroll (synthetic) | blocking | - | awaiting |
| USA | state_tax_withholding_form | tax | mandatory | all | pre_joining | Payroll (synthetic) | blocking | - | awaiting |
| USA | direct_deposit_authorization | bank_payroll | mandatory | all | pre_joining | Payroll (synthetic) | blocking | - |  |
| USA | benefits_election_form | insurance | mandatory | employment_types=full_time | post_joining | Benefits Administration (synthetic) | non-blocking | - |  |
| USA | dependant_details | family | optional | employment_types=full_time; requires_attributes=family_cover | pre_joining | Benefits Administration (synthetic) | non-blocking | - |  |
| USA | emergency_contact_form | identity | mandatory | all | day_one | HR Operations (synthetic) | non-blocking | - |  |

## 2. Visa / work-authorisation applicability

| Client | Stated status / route | Tasks | Depends on | Missing information | Legal basis |
|---|---|---|---|---|---|
| Canada | citizen | no immigration task | - | missing status -> hr_review | awaiting_confirmation |
| Canada | permanent_resident | no immigration task | - | missing status -> hr_review | awaiting_confirmation |
| Canada | work_permit_holder | work_permit_verification | work_authorization_check | missing status -> hr_review | awaiting_confirmation |
| Canada | permit_required | work_permit_application_support | work_authorization_check | missing status -> hr_review | awaiting_confirmation |
| Dubai | not_required | no immigration task | - | missing status -> hr_review | awaiting_confirmation |
| Dubai | in_country_transfer | visa_status_change, medical_fitness_test, emirates_id_biometrics, residence_visa_stamping | collect_documents, emirates_id_biometrics, medical_fitness_test, visa_status_change | missing status -> hr_review | awaiting_confirmation |
| Dubai | overseas_entry | entry_permit, medical_fitness_test_on_arrival, emirates_id_biometrics_on_arrival, residence_visa_stamping_on_arrival | collect_documents, emirates_id_biometrics_on_arrival, entry_permit, medical_fitness_test_on_arrival | missing status -> hr_review | awaiting_confirmation |
| India | (none) | - | - | domestic employment only; no immigration task is configured | - |
| USA | citizen | no immigration task | - | missing status -> hr_review | awaiting_confirmation |
| USA | permanent_resident | no immigration task | - | missing status -> hr_review | awaiting_confirmation |
| USA | authorized_visa_holder | work_visa_status_verification | collect_documents | missing status -> hr_review | awaiting_confirmation |
| USA | sponsorship_required | visa_sponsorship_case | collect_documents | missing status -> hr_review | awaiting_confirmation |
| USA | every employee | employment_eligibility_verification | collect_documents | - | awaiting_confirmation |

## 3. Medical and insurance

| Client | Task | Category | Applies when | Approval | Team |
|---|---|---|---|---|---|
| Canada | benefits_enrolment | insurance | employment_types=full_time | - | Benefits Administration (synthetic) |
| Dubai | medical_fitness_test_on_arrival | medical | requires_attributes=visa_route:overseas_entry | - | PRO / Visa (synthetic) |
| Dubai | medical_fitness_test | medical | requires_attributes=visa_route:in_country_transfer | - | PRO / Visa (synthetic) |
| Dubai | health_insurance_enrolment | insurance | all | - | Benefits Administration (synthetic) |
| Dubai | dependant_insurance_enrolment | insurance | grades=G6,G7; requires_attributes=family_cover | hr_manager | Benefits Administration (synthetic) |
| India | group_health_insurance_enrolment | insurance | employment_types=full_time | - | Benefits Administration (synthetic) |
| USA | benefits_enrolment | insurance | employment_types=full_time | - | Benefits Administration (synthetic) |
| Canada | (policy) | summary | {"pre_employment_medical": "not configured", "health_insurance": "benefits enrolment for full-time employees", "coverage_category": "awaiting_confirmation", "dependant_cover": "when family_cover is stated", "owner": "benefits"} | - | - |
| Dubai | (policy) | summary | {"pre_employment_medical": "only on a visa route", "health_insurance": "all employees", "coverage_category": "awaiting_confirmation", "dependant_cover": "grades G6-G7 when family_cover is stated", "owner": "benefits"} | - | - |
| India | (policy) | summary | {"pre_employment_medical": "not configured", "health_insurance": "full-time employees (group cover)", "coverage_category": "awaiting_confirmation", "owner": "benefits"} | - | - |
| USA | (policy) | summary | {"pre_employment_medical": "not configured", "health_insurance": "benefits enrolment for full-time employees", "coverage_category": "awaiting_confirmation", "dependant_cover": "when family_cover is stated", "owner": "benefits"} | - | - |

## 4. Payroll and banking

| Client | Task | Applies when | Depends on | Approval | Evidence | Confirm |
|---|---|---|---|---|---|---|
| Canada | tax_onboarding_forms | all | collect_documents | - | yes | awaiting |
| Canada | direct_deposit_setup | all | collect_documents | - | yes |  |
| Canada | payroll_setup | all | tax_onboarding_forms, direct_deposit_setup | hr_manager | no |  |
| Dubai | payroll_setup | all | collect_documents | hr_manager | yes |  |
| Dubai | bank_account_setup | all | payroll_setup | - | yes |  |
| Dubai | salary_transfer_letter | requires_attributes=salary_transfer_letter_requested | bank_account_setup | - | no |  |
| India | tax_identifier_verification | all | collect_documents | - | yes |  |
| India | statutory_declarations | all | tax_identifier_verification | - | yes | awaiting |
| India | bank_account_verification | all | collect_documents | - | yes |  |
| India | payroll_setup | all | bank_account_verification, statutory_declarations | hr_manager | no |  |
| USA | federal_tax_withholding_setup | all | collect_documents | - | yes | awaiting |
| USA | state_tax_withholding_setup | all | collect_documents | - | yes | awaiting |
| USA | direct_deposit_verification | all | collect_documents | - | yes |  |
| USA | payroll_setup | all | federal_tax_withholding_setup, state_tax_withholding_setup, direct_deposit_verification | hr_manager | no |  |
| Canada | (policy) | {"currency": "CAD", "frequency": "awaiting_confirmation", "eligibility": "all employees", "required_forms": ["tax_onboarding_forms", "direct_deposit_details"], "bank_verification": "direct_deposit_setup with evidence", "owner": "finance", "approval": "hr_manager", "benefits_eligibility": "full_time", "tax_rates": "never stored in configuration"} | - | - | - |  |
| Dubai | (policy) | {"currency": "AED", "frequency": "awaiting_confirmation", "eligibility": "all employees", "required_forms": ["bank_account_details"], "bank_verification": "bank_account_setup with evidence", "owner": "finance", "approval": "hr_manager", "salary_amounts": "never stored in configuration"} | - | - | - |  |
| India | (policy) | {"currency": "INR", "frequency": "awaiting_confirmation", "eligibility": "all employees", "required_forms": ["tax_identifier", "statutory_declaration_forms", "bank_account_details"], "bank_verification": "bank_account_verification with evidence", "owner": "finance", "approval": "hr_manager", "tax_rates_and_contributions": "never stored in configuration"} | - | - | - |  |
| USA | (policy) | {"currency": "USD", "frequency": "awaiting_confirmation", "eligibility": "all employees", "required_forms": ["federal_tax_withholding_form", "state_tax_withholding_form", "direct_deposit_authorization"], "bank_verification": "direct_deposit_verification with evidence", "owner": "finance", "approval": "hr_manager", "benefits_eligibility": "full_time", "tax_rates": "never stored in configuration"} | - | - | - |  |

## 5. Task templates and dependencies

| Client | Task | Category | Owner | Offset (working days) | Depends on | Blocking | Approval |
|---|---|---|---|---|---|---|---|
| Canada | hr_record_create | hr_record | hr_ops | -10 | (start) | blocking | - |
| Canada | collect_documents | document_collection | hr_ops | -8 | hr_record_create | blocking | - |
| Canada | work_authorization_check | hr_record | hr_officer | -8 | collect_documents | blocking | - |
| Canada | work_permit_verification | visa | hr_officer | -7 | work_authorization_check | blocking | - |
| Canada | work_permit_application_support | visa | hr_officer | -15 | work_authorization_check | blocking | hr_manager |
| Canada | background_verification | background_verification | hr_officer | -7 | collect_documents | blocking | - |
| Canada | reference_check | background_verification | hr_officer | -6 | collect_documents | blocking | - |
| Canada | tax_onboarding_forms | payroll | finance | -5 | collect_documents | blocking | - |
| Canada | direct_deposit_setup | payroll | finance | -4 | collect_documents | blocking | - |
| Canada | payroll_setup | payroll | finance | -3 | tax_onboarding_forms, direct_deposit_setup | blocking | hr_manager |
| Canada | benefits_enrolment | insurance | hr_ops | -3 | collect_documents | blocking | - |
| Canada | laptop_request | it_provisioning | it_manager | -6 | hr_record_create | blocking | manager |
| Canada | system_access | access_management | it_manager | -2 | laptop_request | blocking | manager |
| Canada | remote_equipment_shipping | it_provisioning | it_manager | -8 | laptop_request | non-blocking | - |
| Canada | company_induction | orientation | hr_ops | 0 | hr_record_create | blocking | - |
| Canada | department_orientation | orientation | manager | 1 | company_induction | blocking | - |
| Canada | workplace_health_and_safety_training | orientation | hr_ops | 3 | company_induction | blocking | - |
| Canada | information_security_training | orientation | hr_ops | 3 | company_induction | blocking | - |
| Canada | policy_acknowledgement | policy_acknowledgement | employee | 2 | company_induction | blocking | - |
| Dubai | hr_record_create | hr_record | hr_ops | -15 | (start) | blocking | - |
| Dubai | collect_documents | document_collection | hr_ops | -13 | hr_record_create | blocking | - |
| Dubai | background_verification | background_verification | hr_officer | -12 | collect_documents | blocking | - |
| Dubai | entry_permit | visa | pro | -14 | collect_documents | blocking | - |
| Dubai | medical_fitness_test_on_arrival | medical | pro | -4 | entry_permit | blocking | - |
| Dubai | emirates_id_biometrics_on_arrival | biometrics | pro | -3 | medical_fitness_test_on_arrival | blocking | - |
| Dubai | residence_visa_stamping_on_arrival | visa | pro | -1 | emirates_id_biometrics_on_arrival | blocking | - |
| Dubai | visa_status_change | visa | pro | -10 | collect_documents | blocking | - |
| Dubai | medical_fitness_test | medical | pro | -7 | visa_status_change | blocking | - |
| Dubai | emirates_id_biometrics | biometrics | pro | -5 | medical_fitness_test | blocking | - |
| Dubai | residence_visa_stamping | visa | pro | -3 | emirates_id_biometrics | blocking | - |
| Dubai | flight_booking | travel | facilities | -9 | entry_permit | blocking | hr_manager |
| Dubai | temporary_accommodation | accommodation | facilities | -6 | flight_booking | blocking | hr_manager |
| Dubai | health_insurance_enrolment | insurance | hr_ops | -5 | collect_documents | blocking | - |
| Dubai | dependant_insurance_enrolment | insurance | hr_ops | -3 | health_insurance_enrolment | non-blocking | hr_manager |
| Dubai | payroll_setup | payroll | finance | -5 | collect_documents | blocking | hr_manager |
| Dubai | bank_account_setup | payroll | finance | -4 | payroll_setup | blocking | - |
| Dubai | salary_transfer_letter | banking_letter | finance | -2 | bank_account_setup | non-blocking | - |
| Dubai | laptop_request | it_provisioning | it_manager | -8 | hr_record_create | blocking | manager |
| Dubai | system_access | access_management | it_manager | -3 | laptop_request | blocking | manager |
| Dubai | privileged_access_request | access_management | it_manager | -2 | system_access | blocking | hr_lead |
| Dubai | office_access_card | workplace | facilities | -2 | hr_record_create | blocking | - |
| Dubai | company_induction | orientation | hr_ops | 0 | hr_record_create | blocking | - |
| Dubai | department_orientation | orientation | manager | 1 | company_induction | blocking | - |
| Dubai | information_security_training | orientation | hr_ops | 3 | company_induction | blocking | - |
| Dubai | policy_acknowledgement | policy_acknowledgement | employee | 2 | company_induction | blocking | - |
| India | hr_record_create | hr_record | hr_ops | -10 | (start) | blocking | - |
| India | collect_documents | document_collection | hr_ops | -8 | hr_record_create | blocking | - |
| India | background_verification | background_verification | hr_officer | -7 | collect_documents | blocking | - |
| India | previous_employment_verification | background_verification | hr_officer | -6 | collect_documents | blocking | - |
| India | tax_identifier_verification | payroll | finance | -6 | collect_documents | blocking | - |
| India | statutory_declarations | payroll | finance | -5 | tax_identifier_verification | blocking | - |
| India | bank_account_verification | payroll | finance | -5 | collect_documents | blocking | - |
| India | payroll_setup | payroll | finance | -4 | bank_account_verification, statutory_declarations | blocking | hr_manager |
| India | group_health_insurance_enrolment | insurance | hr_ops | -4 | collect_documents | blocking | - |
| India | laptop_request | it_provisioning | it_manager | -6 | hr_record_create | blocking | manager |
| India | system_access | access_management | it_manager | -2 | laptop_request, background_verification | blocking | manager |
| India | finance_application_access | access_management | it_manager | -2 | system_access | blocking | hr_manager |
| India | office_access_card | workplace | facilities | -1 | hr_record_create | blocking | - |
| India | company_induction | orientation | hr_ops | 0 | hr_record_create | blocking | - |
| India | department_orientation | orientation | manager | 1 | company_induction | blocking | - |
| India | finance_controls_training | orientation | manager | 5 | company_induction | blocking | - |
| India | data_protection_training | orientation | hr_ops | 3 | company_induction | blocking | - |
| India | policy_acknowledgement | policy_acknowledgement | employee | 2 | company_induction | blocking | - |
| USA | hr_record_create | hr_record | hr_ops | -10 | (start) | blocking | - |
| USA | collect_documents | document_collection | hr_ops | -8 | hr_record_create | blocking | - |
| USA | employment_eligibility_verification | hr_record | hr_officer | 2 | collect_documents | blocking | - |
| USA | work_visa_status_verification | visa | hr_officer | -7 | collect_documents | blocking | - |
| USA | visa_sponsorship_case | visa | hr_officer | -20 | collect_documents | blocking | hr_manager |
| USA | background_check | background_verification | hr_officer | -9 | collect_documents | blocking | - |
| USA | federal_tax_withholding_setup | payroll | finance | -4 | collect_documents | blocking | - |
| USA | state_tax_withholding_setup | payroll | finance | -4 | collect_documents | blocking | - |
| USA | direct_deposit_verification | payroll | finance | -4 | collect_documents | blocking | - |
| USA | payroll_setup | payroll | finance | -3 | federal_tax_withholding_setup, state_tax_withholding_setup, direct_deposit_verification | blocking | hr_manager |
| USA | benefits_enrolment | insurance | hr_ops | 5 | hr_record_create | blocking | - |
| USA | laptop_request | it_provisioning | it_manager | -7 | hr_record_create | blocking | manager |
| USA | system_access | access_management | it_manager | -2 | laptop_request, background_check | blocking | manager |
| USA | sales_platform_access | access_management | it_manager | -2 | system_access | blocking | manager |
| USA | office_access_card | workplace | facilities | -1 | hr_record_create | blocking | - |
| USA | company_induction | orientation | hr_ops | 0 | hr_record_create | blocking | - |
| USA | department_orientation | orientation | manager | 1 | company_induction | blocking | - |
| USA | information_security_training | orientation | hr_ops | 3 | company_induction | blocking | - |
| USA | anti_harassment_training | orientation | hr_ops | 10 | company_induction | blocking | - |
| USA | policy_acknowledgement | policy_acknowledgement | employee | 2 | company_induction | blocking | - |

## 6. Equipment and approved access (stored in the uc04 section; NOT written to role_access_matrix)

| Client | Role | Grades | Item | Category | Status | Approval class | Owner | Evidence |
|---|---|---|---|---|---|---|---|---|
| Canada | software_engineer | all | laptop-standard | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | software_engineer | all | monitor-24 | hardware | optional | STANDARD | it_manager | provisioning_confirmation |
| Canada | software_engineer | all | headset-basic | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | customer_success_specialist | all | laptop-standard | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | customer_success_specialist | all | headset-basic | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | finance_analyst | all | laptop-standard | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | finance_analyst | all | monitor-24 | hardware | optional | STANDARD | it_manager | provisioning_confirmation |
| Canada | software_engineer | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | software_engineer | all | vpn-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | software_engineer | all | source-control-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | software_engineer | all | building-access-badge | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| Canada | software_engineer | all | standard-user-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | customer_success_specialist | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | customer_success_specialist | all | crm-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | customer_success_specialist | all | building-access-badge | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| Canada | customer_success_specialist | all | standard-user-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | finance_analyst | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | finance_analyst | all | vpn-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Canada | finance_analyst | all | finance-app-access | software_access | mandatory | MANAGER_EXCEPTION | it_manager | provisioning_confirmation |
| Canada | finance_analyst | all | building-access-badge | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| Canada | finance_analyst | all | standard-user-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | software_engineer | all | laptop-standard | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | software_engineer | all | monitor-27 | hardware | optional | STANDARD | it_manager | provisioning_confirmation |
| Dubai | software_engineer | all | keyboard-mouse | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | software_engineer | all | headset-basic | hardware | optional | STANDARD | it_manager | provisioning_confirmation |
| Dubai | operations_associate | all | laptop-standard | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | operations_associate | all | keyboard-mouse | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | operations_associate | all | mobile-device | hardware | mandatory | MANAGER_EXCEPTION | it_manager | provisioning_confirmation |
| Dubai | senior_manager | G6,G7 | laptop-standard | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | senior_manager | G6,G7 | monitor-27 | hardware | optional | STANDARD | it_manager | provisioning_confirmation |
| Dubai | senior_manager | G6,G7 | mobile-device | hardware | mandatory | MANAGER_EXCEPTION | it_manager | provisioning_confirmation |
| Dubai | software_engineer | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | software_engineer | all | vpn-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | software_engineer | all | source-control-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | software_engineer | all | ide-dev-tools | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | software_engineer | all | office-entry-card | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| Dubai | software_engineer | all | standard-user-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | operations_associate | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | operations_associate | all | ops-dashboard-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | operations_associate | all | office-entry-card | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| Dubai | operations_associate | all | standard-user-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | senior_manager | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | senior_manager | all | vpn-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | senior_manager | all | office-entry-card | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| Dubai | senior_manager | all | standard-user-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |
| Dubai | senior_manager | all | privileged-admin-access | system_permission | mandatory | SECURITY_GATE | it_manager | provisioning_confirmation |
| India | finance_analyst | all | laptop-standard | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | finance_analyst | all | keyboard-mouse | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | software_engineer | all | laptop-standard | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | software_engineer | all | keyboard-mouse | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | software_engineer | all | headset-basic | hardware | optional | STANDARD | it_manager | provisioning_confirmation |
| India | operations_associate | all | laptop-standard | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | finance_analyst | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | finance_analyst | all | erp-finance-access | software_access | mandatory | MANAGER_EXCEPTION | it_manager | provisioning_confirmation |
| India | finance_analyst | all | office-entry-card | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| India | finance_analyst | all | standard-user-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | software_engineer | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | software_engineer | all | vpn-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | software_engineer | all | source-control-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | software_engineer | all | office-entry-card | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| India | software_engineer | all | standard-user-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | operations_associate | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| India | operations_associate | all | office-entry-card | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| India | operations_associate | all | standard-user-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | software_engineer | all | laptop-standard | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | software_engineer | all | monitor-27 | hardware | optional | STANDARD | it_manager | provisioning_confirmation |
| USA | sales_associate | all | laptop-standard | hardware | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | sales_associate | all | mobile-device | hardware | mandatory | MANAGER_EXCEPTION | it_manager | provisioning_confirmation |
| USA | finance_analyst | all | desktop-workstation | hardware | optional | STANDARD | it_manager | provisioning_confirmation |
| USA | finance_analyst | all | monitor-27 | hardware | optional | STANDARD | it_manager | provisioning_confirmation |
| USA | software_engineer | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | software_engineer | all | vpn-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | software_engineer | all | source-control-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | software_engineer | all | hr-self-service-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | software_engineer | all | office-access-card | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| USA | software_engineer | all | standard-user-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | sales_associate | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | sales_associate | all | sales-platform-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | sales_associate | all | hr-self-service-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | sales_associate | all | office-access-card | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| USA | sales_associate | all | standard-user-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | finance_analyst | all | email-teams-account | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | finance_analyst | all | finance-app-access | software_access | mandatory | MANAGER_EXCEPTION | it_manager | provisioning_confirmation |
| USA | finance_analyst | all | hr-self-service-access | software_access | mandatory | STANDARD | it_manager | provisioning_confirmation |
| USA | finance_analyst | all | office-access-card | physical_access | mandatory | STANDARD | facilities | handover_confirmation |
| USA | finance_analyst | all | department-access | system_permission | mandatory | STANDARD | it_manager | provisioning_confirmation |

## 7. Orientation, training and policy acknowledgement (UC-05 acknowledgement tracking is retired; these are configuration only)

| Client | Item | Kind | Applies when | Due (days from joining) | Assessment | Evidence |
|---|---|---|---|---|---|---|
| Canada | company_induction | programme | all | 0 | - | attendance_record |
| Canada | department_orientation | programme | all | 1 | - | attendance_record |
| Canada | workplace_health_and_safety | programme | all | 5 | assessment | attendance_and_assessment_record |
| Canada | information_security | programme | all | 5 | - | attendance_record |
| Canada | remote_work_onboarding | programme | requires_attributes=work_arrangement:remote | 2 | - | attendance_record |
| Dubai | company_induction | programme | all | 0 | - | attendance_record |
| Dubai | department_orientation | programme | all | 1 | - | attendance_record |
| Dubai | information_security | programme | all | 5 | assessment | attendance_record |
| Dubai | engineering_onboarding | programme | roles=software_engineer | 10 | - | attendance_record |
| Dubai | leadership_onboarding | programme | roles=senior_manager | 10 | - | attendance_record |
| India | company_induction | programme | all | 0 | - | attendance_record |
| India | department_orientation | programme | all | 1 | - | attendance_record |
| India | data_protection | programme | all | 5 | assessment | attendance_record |
| India | finance_controls | programme | roles=finance_analyst | 10 | - | attendance_record |
| USA | company_induction | programme | all | 0 | - | attendance_record |
| USA | department_orientation | programme | all | 1 | - | attendance_record |
| USA | information_security | programme | all | 5 | assessment | attendance_record |
| USA | sales_enablement | programme | roles=sales_associate | 10 | - | attendance_record |
| USA | workplace_conduct | programme | all | 10 | - | completion_record |
| Canada | code_of_conduct | policy acknowledgement | all | 2 | - | signed_acknowledgement |
| Canada | confidentiality | policy acknowledgement | all | 0 | - | signed_acknowledgement |
| Canada | acceptable_use | policy acknowledgement | all | 3 | - | signed_acknowledgement |
| Canada | remote_work | policy acknowledgement | requires_attributes=work_arrangement:remote | 3 | - | signed_acknowledgement |
| Canada | attendance_and_leave | policy acknowledgement | all | 5 | - | signed_acknowledgement |
| Dubai | code_of_conduct | policy acknowledgement | all | 2 | - | signed_acknowledgement |
| Dubai | confidentiality | policy acknowledgement | all | 0 | - | signed_acknowledgement |
| Dubai | acceptable_use | policy acknowledgement | all | 3 | - | signed_acknowledgement |
| Dubai | data_protection | policy acknowledgement | all | 3 | - | signed_acknowledgement |
| Dubai | attendance_and_leave | policy acknowledgement | all | 5 | - | signed_acknowledgement |
| Dubai | health_and_safety | policy acknowledgement | all | 5 | - | signed_acknowledgement |
| India | code_of_conduct | policy acknowledgement | all | 2 | - | signed_acknowledgement |
| India | confidentiality | policy acknowledgement | all | 0 | - | signed_acknowledgement |
| India | acceptable_use | policy acknowledgement | all | 3 | - | signed_acknowledgement |
| India | leave_and_attendance | policy acknowledgement | all | 5 | - | signed_acknowledgement |
| India | data_protection | policy acknowledgement | all | 3 | - | signed_acknowledgement |
| USA | code_of_conduct | policy acknowledgement | all | 2 | - | signed_acknowledgement |
| USA | confidentiality | policy acknowledgement | all | 0 | - | signed_acknowledgement |
| USA | acceptable_use | policy acknowledgement | all | 3 | - | signed_acknowledgement |
| USA | attendance_and_leave | policy acknowledgement | all | 5 | - | signed_acknowledgement |
| USA | data_protection | policy acknowledgement | all | 3 | - | signed_acknowledgement |

## 8. Working calendars and holidays

| Client | Timezone | Working days (1=Mon..7=Sun) | Weekend | Hours | Holidays (synthetic) |
|---|---|---|---|---|---|
| Canada | America/Toronto | [1, 2, 3, 4, 5] | [6, 7] | {"start": "09:00", "end": "17:00"} | 2026-12-25 [SAMPLE] Christmas Day (synthetic; not an authoritative calendar); 2026-12-28 [SAMPLE] Boxing Day (observed) (synthetic; not an authoritative calendar); 2027-01-01 [SAMPLE] New Year's Day (synthetic; not an authoritative calendar) |
| Dubai | Asia/Dubai | [1, 2, 3, 4, 5] | [6, 7] | {"start": "09:00", "end": "18:00"} | 2026-12-02 [SAMPLE] UAE National Day; 2026-12-03 [SAMPLE] UAE National Day (2nd) |
| India | Asia/Kolkata | [1, 2, 3, 4, 5, 6] | [7] | {"start": "09:30", "end": "18:00"} | 2026-12-25 [SAMPLE] Christmas Day (synthetic; not an authoritative calendar); 2027-01-26 [SAMPLE] Republic Day (synthetic; not an authoritative calendar) |
| USA | America/New_York | [1, 2, 3, 4, 5] | [6, 7] | {"start": "09:00", "end": "17:00"} | 2026-11-26 [SAMPLE] Thanksgiving Day (synthetic; not an authoritative calendar); 2026-12-25 [SAMPLE] Christmas Day (synthetic; not an authoritative calendar); 2027-01-01 [SAMPLE] New Year's Day (synthetic; not an authoritative calendar) |
