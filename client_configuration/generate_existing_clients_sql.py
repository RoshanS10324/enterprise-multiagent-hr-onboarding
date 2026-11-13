"""Generates, for the four EXISTING mock clients, (1) a READ-ONLY dry-run SQL script and (2) the insert-only seed SQL for later review.
Writes only the two files; never connects to a database.

    python client_configuration/generate_existing_clients_sql.py

Rows come from the same projection the Python dry run uses. Existing rows are never rewritten: the clients' ``client_configs`` rows and draft version 1
are preserved (checked, not inserted); CLIENT_DUBAI's two stored holidays are preserved (checked, not inserted). Which rows already exist is taken from the
read-only snapshot ``tests/fixtures/existing_clients_config_snapshot.json`` and re-checked by the SQL itself at run time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "UC02_Onboarding_Task_Agent" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_manual_seed_sql as g  # noqa: E402
from onboarding_task_management.client_configuration.document import PROFILE_DIR, document_hash, load_profiles, validate_document  # noqa: E402
from onboarding_task_management.client_configuration.projection import project, sections_hash  # noqa: E402

SNAP = json.loads((ROOT / "UC02_Onboarding_Task_Agent" / "tests" / "fixtures" / "existing_clients_config_snapshot.json").read_text(encoding="utf-8"))
DRY = ROOT / "docs" / "four_mock_clients_dry_run_readonly.sql"
SEED = ROOT / "docs" / "four_mock_clients_seed.sql"
S = g.SCHEMA
TABLES = [t for t in g.ORDER if t != "client_configs"]


def main() -> None:
    docs = load_profiles(PROFILE_DIR / "existing_mock_clients")
    for d in docs:
        assert not validate_document(d), d["client_master"]["client_id"]
    clients = [d["client_master"]["client_id"] for d in docs]
    cl = ", ".join(g.q(c) for c in clients)
    existing_holidays = {(r["client_id"], r["holiday_date"]): r for r in SNAP["client_holidays"]}
    per = {t: [] for t in TABLES}
    preserved_holidays = []
    for d in docs:
        rows = project(d)
        assert rows["client_configs"] == []
        for t in TABLES:
            for r in rows[t]:
                if t == "client_holidays" and (r["client_id"], r["holiday_date"]) in existing_holidays:
                    preserved_holidays.append(r)
                else:
                    per[t].append((d["client_master"]["client_id"], r))
    totals = {t: len(per[t]) for t in TABLES}
    total = sum(totals.values())
    expected = {t: {c: sum(1 for x, _ in per[t] if x == c) for c in clients} for t in TABLES}
    masters = [(d["client_master"]["client_id"], d["client_master"]["timezone"], d["shared_configuration"]["calendar"]["working_days"]) for d in docs]
    hashes = [(d["client_master"]["client_id"], document_hash(d), sections_hash(d)) for d in docs]
    master_values = ",\n    ".join(f"({g.q(c)}, {g.q(tz)}, ARRAY[{','.join(map(str, wd))}]::integer[])" for c, tz, wd in masters)
    preserved_values = ",\n    ".join(f"({g.q(r['client_id'])}, {g.q(r['holiday_date'])}::date, {g.q(r['name'])}, {'TRUE' if r['is_observed'] else 'FALSE'})"
                                       for r in preserved_holidays) or "(NULL::text, NULL::date, NULL::text, NULL::boolean)"
    hash_values = ",\n    ".join(f"({g.q(c)}, {g.q(h)}, {g.q(sh)})" for c, h, sh in hashes)
    exp_table = "\n".join(f"--   {t:<30} {totals[t]:>3}   " + "  ".join(f"{c.split('_')[1]}={expected[t][c]}" for c in clients) for t in TABLES)

    checks_readonly = f"""-- R1. The four client master rows exist, are active, marked mock, and match the profiles' timezone and working days. EXPECTED: 4 rows, all ok.
SELECT e.client_id, c.company_name, c.timezone, c.working_days, c.is_active, c.settings_payload ->> 'environment' AS environment,
       (c.timezone = e.tz AND c.working_days = e.wd AND c.is_active AND c.settings_payload ->> 'environment' = 'mock') AS ok
FROM (VALUES
    {master_values}
) AS e(client_id, tz, wd)
LEFT JOIN {S}.client_configs c ON c.client_id = e.client_id
ORDER BY e.client_id;

-- R2. Existing versions (EXPECTED: only version 1, status draft, for each) and no version 2 yet.
SELECT client_id, version, status, effective_from, effective_to, approved_by FROM {S}.client_config_versions WHERE client_id IN ({cl}) ORDER BY 1, 2;

-- R3. Rows that would collide with the seed. EXPECTED: 0 everywhere.
SELECT 'task_templates' AS table_name, count(*) FROM {S}.task_templates WHERE client_id IN ({cl})
UNION ALL SELECT 'client_document_requirements', count(*) FROM {S}.client_document_requirements WHERE client_id IN ({cl})
UNION ALL SELECT 'client_service_policies', count(*) FROM {S}.client_service_policies WHERE client_id IN ({cl})
UNION ALL SELECT 'client_milestone_templates', count(*) FROM {S}.client_milestone_templates WHERE client_id IN ({cl})
UNION ALL SELECT 'client_config_versions v2', count(*) FROM {S}.client_config_versions WHERE client_id IN ({cl}) AND version = 2;

-- R4. Stored holidays that the profiles PRESERVE (must exist with exactly these values). EXPECTED: every row ok = true.
SELECT e.client_id, e.holiday_date, h.name, h.is_observed, (h.name = e.name AND h.is_observed = e.observed) AS ok
FROM (VALUES
    {preserved_values}
) AS e(client_id, holiday_date, name, observed)
LEFT JOIN {S}.client_holidays h ON h.client_id = e.client_id AND h.holiday_date = e.holiday_date
WHERE e.client_id IS NOT NULL;

-- R5. Holidays the seed would ADD that already exist. EXPECTED: no rows.
SELECT h.client_id, h.holiday_date, h.name FROM {S}.client_holidays h
WHERE (h.client_id, h.holiday_date) IN ({", ".join(f"({g.q(c)}, {g.q(r['holiday_date'])}::date)" for c, r in per['client_holidays'])});

-- R6. Constraints actually present on the seven tables (for comparison with docs/CLIENT_CONFIGURATION_COLUMN_MAPPING.md).
SELECT conrelid::regclass::text AS table_name, conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint WHERE conrelid IN ({", ".join(f"'{S}.{t}'::regclass" for t in g.ORDER)}) AND contype IN ('p','f','u','c') ORDER BY 1, 3, 2;"""

    dry = f"""-- =====================================================================================================================================
-- four_mock_clients_dry_run_readonly.sql  -  READ-ONLY. SELECT statements only; changes nothing.
-- Checks that the database is in the state the seed script (four_mock_clients_seed.sql) expects, for CLIENT_CANADA, CLIENT_DUBAI, CLIENT_INDIA,
-- CLIENT_USA. Run it in the SQL editor of the project you intend to seed, and confirm first which project (Development or Production) that is.
-- Expected seed: {total} inserts, 0 updates, 0 deletes; the four client_configs rows, every version 1 row and CLIENT_DUBAI's two stored holidays are kept.
-- =====================================================================================================================================
BEGIN READ ONLY;
{checks_readonly}
ROLLBACK;
"""
    DRY.write_text(dry, encoding="utf-8", newline="\n")

    inserts = "".join(f"\n-- {t}: {totals[t]} rows (" + ", ".join(f"{c.split('_')[1]}={expected[t][c]}" for c in clients) + ")\n" +
                      g.insert_statement(t, per[t])[0] for t in TABLES)
    exp_values = ",\n      ".join(f"({g.q(t)}, {g.q(c)}, {expected[t][c] + (sum(1 for r in preserved_holidays if r['client_id'] == c) if t == 'client_holidays' else 0)})"
                                   for t in TABLES for c in clients)
    seed = f"""-- =====================================================================================================================================
-- four_mock_clients_seed.sql  -  INSERT-ONLY seed of version 2 for the four EXISTING mock clients. NOT EXECUTED. For review.
--
-- WARNING  Run only after (a) the target project is confirmed (it may be labelled PRODUCTION) and (b) explicit approval. Synthetic / mock test data only:
--          no secrets, no personal data, no legal determinations. role_access_matrix is not touched. No table, column or index is created.
-- KEEPS    the four client_configs rows (not inserted, checked), every version 1 draft row, and CLIENT_DUBAI's two stored holidays (checked, not inserted).
-- INSERTS  {total} rows:
{exp_table}
-- SAFETY   one transaction; guards abort if the state differs from the read-only dry run; every INSERT has a NOT EXISTS natural-key guard; the final
--          assertions abort (rolling everything back) if any row was skipped, any reference is wrong or any hash differs. No UPDATE/DELETE/ON CONFLICT.
-- =====================================================================================================================================
BEGIN;
LOCK TABLE {S}.client_document_requirements, {S}.client_service_policies, {S}.client_milestone_templates IN SHARE ROW EXCLUSIVE MODE;

DO $guard$
BEGIN
  IF (SELECT count(*) FROM (VALUES
    {master_values}
  ) AS e(client_id, tz, wd) JOIN {S}.client_configs c ON c.client_id = e.client_id
      WHERE c.timezone = e.tz AND c.working_days = e.wd AND c.is_active AND c.settings_payload ->> 'environment' = 'mock') <> {len(clients)} THEN
    RAISE EXCEPTION 'the four client master rows are missing or differ from the profiles; nothing was written';
  END IF;
  IF EXISTS (SELECT 1 FROM {S}.client_config_versions WHERE client_id IN ({cl}) AND version = 2)
     OR EXISTS (SELECT 1 FROM {S}.task_templates WHERE client_id IN ({cl}))
     OR EXISTS (SELECT 1 FROM {S}.client_document_requirements WHERE client_id IN ({cl}))
     OR EXISTS (SELECT 1 FROM {S}.client_service_policies WHERE client_id IN ({cl}))
     OR EXISTS (SELECT 1 FROM {S}.client_milestone_templates WHERE client_id IN ({cl})) THEN
    RAISE EXCEPTION 'version 2 or configuration rows already exist for these clients; this is a first-time seed; nothing was written';
  END IF;
  IF (SELECT count(*) FROM (VALUES
    {preserved_values}
  ) AS e(client_id, holiday_date, name, observed) JOIN {S}.client_holidays h ON h.client_id = e.client_id AND h.holiday_date = e.holiday_date
      AND h.name = e.name AND h.is_observed = e.observed) <> {len(preserved_holidays)} THEN
    RAISE EXCEPTION 'the preserved stored holidays differ from the profiles; nothing was written';
  END IF;
END
$guard$;
{inserts}
DO $verify$
DECLARE bad text;
BEGIN
  SELECT string_agg(e.tbl || '/' || e.client || ': expected ' || e.expected || ' found ' || a.n, '; ') INTO bad
  FROM (VALUES
      {exp_values}
  ) AS e(tbl, client, expected)
  JOIN LATERAL (SELECT CASE e.tbl
      WHEN 'client_config_versions' THEN (SELECT count(*) FROM {S}.client_config_versions WHERE client_id = e.client AND version = 2)
      WHEN 'task_templates' THEN (SELECT count(*) FROM {S}.task_templates WHERE client_id = e.client AND config_version = 2)
      WHEN 'client_document_requirements' THEN (SELECT count(*) FROM {S}.client_document_requirements WHERE client_id = e.client AND config_version = 2)
      WHEN 'client_service_policies' THEN (SELECT count(*) FROM {S}.client_service_policies WHERE client_id = e.client AND config_version = 2)
      WHEN 'client_milestone_templates' THEN (SELECT count(*) FROM {S}.client_milestone_templates WHERE client_id = e.client AND config_version = 2)
      WHEN 'client_holidays' THEN (SELECT count(*) FROM {S}.client_holidays WHERE client_id = e.client)
    END AS n) a ON TRUE
  WHERE a.n <> e.expected;
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'row count mismatch: %', bad; END IF;

  SELECT string_agg(e.client, ', ') INTO bad FROM (VALUES
    {hash_values}
  ) AS e(client, doc_hash, sec_hash)
  WHERE (SELECT config_hash FROM {S}.client_config_versions WHERE client_id = e.client AND version = 2) IS DISTINCT FROM e.doc_hash
     OR (SELECT count(DISTINCT policy_details ->> 'sections_hash') FROM {S}.client_service_policies
         WHERE client_id = e.client AND config_version = 2 AND service_category = 'cfg_section') <> 1
     OR (SELECT min(policy_details ->> 'sections_hash') FROM {S}.client_service_policies
         WHERE client_id = e.client AND config_version = 2 AND service_category = 'cfg_section') IS DISTINCT FROM e.sec_hash;
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'document or sections hash mismatch for: %', bad; END IF;

  SELECT string_agg(t.client_id || ':' || t.template_key || '->' || d, '; ') INTO bad
  FROM {S}.task_templates t, unnest(t.depends_on_template_keys) AS d
  WHERE t.client_id IN ({cl}) AND NOT EXISTS (SELECT 1 FROM {S}.task_templates x WHERE x.client_id = t.client_id AND x.template_key = d);
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'unknown dependency: %', bad; END IF;
  SELECT string_agg(m.client_id || ':' || k, '; ') INTO bad
  FROM {S}.client_milestone_templates m, unnest(m.required_task_keys) AS k
  WHERE m.client_id IN ({cl}) AND NOT EXISTS (SELECT 1 FROM {S}.task_templates x WHERE x.client_id = m.client_id AND x.template_key = k);
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'milestone requires an unknown task: %', bad; END IF;

  IF EXISTS (SELECT 1 FROM {S}.client_service_policies WHERE client_id IN ({cl}) GROUP BY client_id, config_version, service_category, service_key HAVING count(*) > 1)
     OR EXISTS (SELECT 1 FROM {S}.client_document_requirements WHERE client_id IN ({cl}) GROUP BY client_id, config_version, requirement_key HAVING count(*) > 1)
  THEN RAISE EXCEPTION 'duplicate natural key'; END IF;
  IF (SELECT count(*) FROM {S}.client_config_versions WHERE client_id IN ({cl}) AND version = 1 AND status = 'draft') <> {len(clients)} THEN
    RAISE EXCEPTION 'a version 1 draft row changed or is missing';
  END IF;
  RAISE NOTICE 'seed verified: {total} rows, version 2 for 4 existing mock clients; existing rows preserved';
END
$verify$;

COMMIT;
"""
    SEED.write_text(seed, encoding="utf-8", newline="\n")
    print(json.dumps({"inserts": totals, "total": total, "preserved_holidays": len(preserved_holidays),
                      "dry_run_bytes": DRY.stat().st_size, "seed_bytes": SEED.stat().st_size}, indent=1))


if __name__ == "__main__":
    main()
