# UC-01 mock employees

`employees.json` holds 10 **synthetic** onboarding requests. All of them use
client `SYN-UC01-MOCK` and `.invalid` email addresses, and every person and
offer in it is invented. Each record states the outcome UC-01 must produce.

| Scenario | Expected UC-01 outcome |
|---|---|
| `valid_new_employee` | CASE_OPENED, employee number issued, no approvals |
| `pending_form_approval` | HELD: form not approved |
| `rejected_form` | HELD: form not approved |
| `duplicate_candidate` | LINKED_EXISTING to `valid_new_employee`'s case |
| `rehire` | CASE_OPENED with variant `is_rehire` and 1 HC_12 approval |
| `internal_transfer` | CASE_OPENED with variant `is_internal_transfer` and 1 HC_12 approval |
| `missing_placement` | HELD: `location_name_required` (never invented) |
| `hiring_freeze` | PAUSED |
| `pending_hr_approval` | CASE_OPENED with `is_agency_contractor` and 1 HC_12 approval pending HR |
| `invalid_employee_details` | rejected by validation (CLIENT_SITE without `client_site_id`) |

## Seeding

```powershell
python scripts/seed_mock_employees.py            # in-memory, one line per record
python scripts/seed_mock_employees.py --twice    # also proves a second run inserts nothing
python scripts/seed_mock_employees.py --json     # machine-readable report
```

The seeder sends every record through UC-01's real `open_onboarding_case`.
It reports each record as **inserted**, **skipped** (already exists), **held**,
**paused** or **failed**, and exits non-zero if any outcome differs from the
expected one. It never deletes anything and never prints salary text.

**Target.** The default is a fresh **in-memory** dev container, so nothing is
persisted. `--target supabase` currently **refuses** and names the UC-01
Supabase repositories that do not exist yet (case, audit, approval, offer,
employee number, document store). It does not fall back to memory.
Persisting these records needs the decisions listed in
`docs/uc01-integration-status.md`.

`tests/test_mock_seed.py` keeps the dataset and the seeder in step with
UC-01's actual behaviour.
