# UC-04 configuration gap report

Prepared 2026-10-27 from `data/client_alpha_config.json`, `data/client_bravo_config.json`, `src/sub_agent/contracts.py` (`CaseConfig`) and the code that reads each block.

## The one thing to understand first
Every value in `data/` is **synthetic test data**. A block marked `VERIFIED` in those files means "supplied, and the code treats it as authoritative".
It does **not** mean the business confirmed it. No matrix, approver, lead time, calendar or status list below has been confirmed by a business owner.
The two source documents (Business Framework v0.1 and the Enterprise HR Agent deck) give no concrete values for any of them. They only say that each client's
knowledge layer holds "equipment and access by role and grade", approvers and lead times.

## How the code treats configuration (confirmed by tests)
| State of a block | Behaviour | Test |
|---|---|---|
| `MISSING` | The affected items go to `BLOCKED` with `CONFIG_MISSING:<block>`; HR is asked. Nothing is defaulted. | TC-07 to TC-09, TC-16, TC-27 |
| `VERIFIED_EMPTY` (supplied and empty) | Access or equipment matrix: every item of that kind becomes an exception. Other blocks: `CONFIG_EMPTY:<block>` blocks the plan. This is different from `MISSING`. | `test_verified_empty_matrix_is_not_the_same_as_missing` |
| Configuration for another client or role | `CONFIG_SCOPE_MISMATCH`; never used (GR-16). | TC-19, AC-10 |
| Invalid value (lead time outside 0 to 365, negative courier days) | `CONFIG_INVALID:<block>` blocks the plan. | tests/test_calendar_boundaries.py |
| Client instruction that conflicts with a guardrail (`client_overrides`) | Ignored, logged, client HR admin notified (GR-17). | EC-51 |

## Block-by-block status
| Block | What the local mock contains | Confirmed by the business? | What is needed |
|---|---|---|---|
| `role_access_matrix` | alpha / software_engineer: email-teams-account, source-control-access, ci-pipeline-access. bravo: email-teams-account, vpn-access | **No** (synthetic) | The real list per client, role and grade |
| `role_equipment_matrix` | alpha: laptop-standard, headset-basic. bravo: laptop-standard, monitor-27 | **No** | The real list per client, role and grade; quantities are not modelled and no source asks for them |
| `approver_config` | ACCESS and EQUIPMENT: hiring_manager (alpha, delegate hiring_manager_delegate) or line_manager (bravo, no delegate). EXCEPTION: exception_approver, no delegate | **No**. The Framework names the hiring manager for IT requests only; **nobody is named for exceptions** | Approver and delegate per request class and client, and who approves exceptions |
| `lead_times` (working days before joining) | alpha: EQUIPMENT 10, ACCESS 5, CLIENT_SITE_CLEARANCE 15. bravo: 7, 3, 20 | **No**. The Framework says only "per lead time before joining" | Real lead times per client, kind and country |
| `work_calendar` | Weekend Saturday and Sunday; alpha holidays 2026-12-02 and 2026-12-03 | **No**. No calendar source or time zone is defined | Weekend days and the holiday source per country or client |
| `handover_rules` | alpha: COLLECT, keep device reserved on a missed slot. bravo: DELIVER | **No** ("collect, deliver or ready at desk" is a client choice) | The mode per client; policy for a missed collection slot |
| `evidence_roles` | PROVISIONING, DELIVERY and COLLECTION asserted by it_service_desk; CLIENT_SITE_CLEARANCE by account_manager | **No**. The Framework says completion needs evidence stored in the case but not who asserts it | Which role may assert each evidence type, and the evidence format |
| `country_rules` | `MISSING` in both clients | n/a (absent) | Courier lead days and remote-first-day rule per country. Remote or overseas equipment is blocked until supplied (EC-36) |
| `client_site_rules` | `MISSING` in both clients (tests supply a synthetic block) | n/a (absent) | Required items (badge, client device, client NDA), whether a late clearance blocks, and the owner (open question OQ-07: account manager, delivery or HR) |
| `client_overrides` | empty | n/a | Whether clients may supply any instruction that UC-04 should honour |
| IT ticket statuses | Synthetic: CREATED, IN_PROGRESS, REJECTED, COMPLETED (`states.TicketStatus`) | **No** | The real service-desk status list and how each maps to UC-04 states |
| Approval scope | client_id, role_id, client_site_required must match the request | Local design | Confirm that these three fields are the right scope of an approval |
| Role coverage | Only `software_engineer`, one role per client | **No** | Roles, grades, nationalities and locations the product must cover; the Framework says entitlements depend on role, grade and location |

## Decisions the business owner must make (nothing here was guessed)
1. Real access and equipment matrices per client, role and grade.
2. Approver and delegate per request class, and **who approves exceptions**.
3. Lead times per client, kind and country, and the working-calendar source (weekend days, holidays, time zone).
4. Evidence: which role may assert provisioning, delivery, collection and clearance, and what counts as evidence.
5. The real IT service-desk status list and its mapping.
6. Client-site profiles: required items, clearance lead time, blocking rule, and owner.
7. Country rules for remote and overseas joiners.
8. Whether any equipment or access item may complete after joining (AC-07), and how a project change is represented (EC-35). See `specs/UC04_SPEC.md` section 12.

Until these are supplied, UC-04 stays in local simulation. It fails closed on every missing value instead of defaulting to a permissive one.
