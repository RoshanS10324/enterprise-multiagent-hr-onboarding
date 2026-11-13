# UC04 client configurations used by the orchestrator

UC04 evaluates equipment and access against a `CaseConfig` (UC04 `sub_agent/contracts.py`) scoped to **one client and
one role**: it refuses another client's or role's configuration (`CONFIG_SCOPE_MISMATCH`). The orchestrator chooses
where that configuration comes from with `UC04_CONFIG_SOURCE`:

- `file` (default): the file `config/tenants.yaml` names for the case's client and role code
  (`uc04_client_configs: {role_code: path}`, else the tenant's single `uc04_client_config_ref`). The files below are
  used only in this mode.
- `central`: the client's in-force approved configuration in Supabase, mapped by
  `src/hr_onboarding_common/orchestration/uc04_central_config.py`. These files are **not** used as a fallback: a client
  without a valid central configuration is blocked. See `docs/UC04_CENTRAL_CLIENT_CONFIGURATION.md`.

The role code is the case's role in UC04's code form: lower case, other characters as `_`
(`Power BI Developer` -> `power_bi_developer`). A role with no file is reported as `UC04_CONFIGURATION_MISSING` for that
client and role: nothing is borrowed from another role or client.

| File | Client | Role code |
|---|---|---|
| `CLIENT-ALPHA.software_engineer.json` | CLIENT-ALPHA | software_engineer |
| `CLIENT-ALPHA.power_bi_developer.json` | CLIENT-ALPHA | power_bi_developer |
| `CLIENT-ALPHA.developer.json` | CLIENT-ALPHA | developer |

## Provenance: synthetic development configuration, not a client approval

Both files were created on 2026-11-05, on the product owner's instruction, so UC04 can run for the synthetic
CLIENT-ALPHA test client in the Control Center. Each is UC04's own synthetic Alpha test configuration
(`UC04_Equipment_Access_Integration_Ready/data/client_alpha_config.json`, version `alpha-v1`) with only `client_id`,
`role_id` and `version` (`alpha-v1-client-alpha-dev`) changed. Every matrix, approver, lead time, calendar and evidence
value is UC04's test value, unchanged; `power_bi_developer` and `developer` (added the same day, for the role "Developer") were given the
same lists as `software_engineer` because no role-specific list exists. An item a case requests outside its role's lists still needs UC04's EXCEPTION
approval (`exception_approver`); nothing here pre-approves anything.

**Not HR-approved.** Replace each file with the client's approved equipment/access configuration for that role
before any real use, or run with `UC04_CONFIG_SOURCE=central` once the client's configuration is approved centrally.
