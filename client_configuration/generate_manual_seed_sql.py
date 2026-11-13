"""Generates docs/manual_seed_four_synthetic_clients.sql from the four JSON profiles. Writes ONLY the two output files; never connects to a database.

    python client_configuration/generate_manual_seed_sql.py

The rows come from the same deterministic projection the Python seeder and its dry run use (``client_configuration.projection.project``), so the SQL cannot
drift from the profiles. Column types come from the live schema captured in ``UC02_Onboarding_Task_Agent/tests/fixtures/live_schema.json`` (verified equal to the
live PostgREST schema on 2026-11-04). UC-04's ``role_access_matrix`` is deliberately excluded.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "UC02_Onboarding_Task_Agent" / "src"))

from onboarding_task_management.client_configuration.document import load_profiles, validate_document  # noqa: E402
from onboarding_task_management.client_configuration.projection import project  # noqa: E402

SCHEMA = "public"
OUT = ROOT / "docs" / "manual_seed_four_synthetic_clients.sql"
LIVE = json.loads((ROOT / "UC02_Onboarding_Task_Agent" / "tests" / "fixtures" / "live_schema.json").read_text(encoding="utf-8"))
ORDER = ["client_configs", "client_config_versions", "task_templates", "client_document_requirements", "client_service_policies",
         "client_milestone_templates", "client_holidays"]
#: Duplicate-prevention keys. Tables WITH a primary key use it (the live primary key of task_templates is (template_key, client_id) - config_version is NOT in it);
#: the document, policy and milestone tables use the existing seeder's natural keys (projection.NATURAL_KEYS), which match the live UNIQUE constraints where the schema exports show them.
GUARD_KEYS = {
    "client_configs": ("client_id",), "client_config_versions": ("client_id", "version"), "task_templates": ("client_id", "template_key"),
    "client_document_requirements": ("client_id", "config_version", "requirement_key"),
    "client_service_policies": ("client_id", "config_version", "service_category", "service_key"),
    "client_milestone_templates": ("client_id", "config_version", "milestone_key"), "client_holidays": ("client_id", "holiday_date"),
}
PG_TYPE = {"text": "text", "int32": "integer", "boolean": "boolean", "jsonb": "jsonb", "uuid": "uuid", "date": "date",
           "text[]": "text[]", "integer[]": "integer[]", "timestamp with time zone": "timestamptz"}
EXPECTED_TOTAL = 207


def q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def literal(value, live_type: str) -> str:
    t = PG_TYPE[live_type]
    if value is None:
        return f"NULL::{t}"
    if t == "text":
        return f"{q(value)}::text"
    if t == "integer":
        return f"{int(value)}::integer"
    if t == "boolean":
        return "TRUE" if value else "FALSE"
    if t == "jsonb":
        return f"{q(json.dumps(value, separators=(',', ':'), ensure_ascii=True))}::jsonb"
    if t in ("uuid", "date", "timestamptz"):
        return f"{q(str(value))}::{t}"
    if t == "text[]":
        return "ARRAY[" + ",".join(q(str(x)) for x in value) + "]::text[]" if value else "ARRAY[]::text[]"
    if t == "integer[]":
        return "ARRAY[" + ",".join(str(int(x)) for x in value) + "]::integer[]" if value else "ARRAY[]::integer[]"
    raise ValueError(t)


def collect():
    docs = load_profiles()
    for d in docs:
        problems = validate_document(d)
        if problems:
            raise SystemExit(f"{d['client_master']['client_id']} is invalid: {problems[:3]}")
    per: dict[str, list[tuple[str, dict]]] = {t: [] for t in ORDER}
    for d in docs:
        rows = project(d, include_role_access_matrix=False)
        for t in ORDER:
            for r in rows[t]:
                per[t].append((d["client_master"]["client_id"], r))
    return docs, per


def columns_for(table: str, rows: list[dict]) -> list[str]:
    used = {c for r in rows for c in r}
    live = list(LIVE[table]["columns"])
    unknown = used - set(live)
    if unknown:
        raise SystemExit(f"{table}: projected columns not in the live schema: {sorted(unknown)}")
    return [c for c in live if c in used]  # live column order; columns never supplied are left to their defaults


def insert_statement(table: str, entries: list[tuple[str, dict]]) -> tuple[str, list[str]]:
    rows = [r for _, r in entries]
    cols = columns_for(table, rows)
    types = LIVE[table]["columns"]
    required_missing = [c for c in LIVE[table]["required"] if c not in cols]
    if required_missing:
        raise SystemExit(f"{table}: required columns not supplied: {required_missing}")
    lines = []
    last_client = None
    for client, r in entries:
        if client != last_client:
            lines.append(f"  -- {client}")
            last_client = client
        lines.append("  (" + ", ".join(literal(r.get(c), types[c]) for c in cols) + "),")
    lines[-1] = lines[-1].rstrip(",")
    guard = " AND ".join(f"t.{k} = v.{k}" for k in GUARD_KEYS[table])
    sql = (f"INSERT INTO {SCHEMA}.{table} ({', '.join(cols)})\nSELECT v.*\nFROM (VALUES\n" + "\n".join(lines) +
           f"\n) AS v({', '.join(cols)})\nWHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.{table} t WHERE {guard});\n")
    return sql, cols


def build() -> tuple[str, dict]:
    docs, per = collect()
    clients = [d["client_master"]["client_id"] for d in docs]
    expected = {t: {c: sum(1 for cl, _ in per[t] if cl == c) for c in clients} for t in ORDER}
    totals = {t: len(per[t]) for t in ORDER}
    if sum(totals.values()) != EXPECTED_TOTAL:
        raise SystemExit(f"generated {sum(totals.values())} rows, expected {EXPECTED_TOTAL}")
    inserts, used_cols = {}, {}
    for t in ORDER:
        inserts[t], used_cols[t] = insert_statement(t, per[t])
    client_list = ", ".join(q(c) for c in clients)
    tables_with_client = ", ".join(q(t) for t in ORDER)

    def schema_guard() -> str:
        arr = ",\n      ".join(f"({q(t)}, ARRAY[{', '.join(q(c) for c in used_cols[t])}])" for t in ORDER)
        return f"""DO $guard$
DECLARE
  spec record;
  missing text;
  unsupplied text;
BEGIN
  FOR spec IN SELECT * FROM (VALUES
      {arr}
  ) AS s(tbl, supplied) LOOP
    SELECT string_agg(c, ', ') INTO missing FROM unnest(spec.supplied) AS c
      WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns ic
                        WHERE ic.table_schema = '{SCHEMA}' AND ic.table_name = spec.tbl AND ic.column_name = c);
    IF missing IS NOT NULL THEN
      RAISE EXCEPTION 'schema drift: {SCHEMA}.% is missing column(s): %', spec.tbl, missing;
    END IF;
    SELECT string_agg(ic.column_name, ', ') INTO unsupplied FROM information_schema.columns ic
      WHERE ic.table_schema = '{SCHEMA}' AND ic.table_name = spec.tbl AND ic.is_nullable = 'NO' AND ic.column_default IS NULL
        AND ic.column_name <> ALL (spec.supplied);
    IF unsupplied IS NOT NULL THEN
      RAISE EXCEPTION 'schema drift: {SCHEMA}.% has NOT NULL column(s) without a default that this script does not supply: %', spec.tbl, unsupplied;
    END IF;
  END LOOP;
END
$guard$;"""

    def already_seeded_guard() -> str:
        parts = "\n    + ".join(f"(SELECT count(*) FROM {SCHEMA}.{t} WHERE client_id IN ({client_list}))" for t in ORDER)
        return f"""DO $guard$
DECLARE
  existing bigint;
BEGIN
  SELECT {parts} INTO existing;
  IF existing <> 0 THEN
    RAISE EXCEPTION 'refusing to run: % row(s) already exist for the four SYN-CLIENT ids. This script is a first-time seed and never overwrites, updates or deletes.', existing;
  END IF;
END
$guard$;"""

    exp_values = ",\n      ".join(f"({q(t)}, {q(c)}, {expected[t][c]})" for t in ORDER for c in clients)
    checks = f"""DO $verify$
DECLARE
  bad text;
BEGIN
  -- 1. exact row counts per table and client
  SELECT string_agg(e.tbl || '/' || e.client || ': expected ' || e.expected || ' found ' || a.n, '; ') INTO bad
  FROM (VALUES
      {exp_values}
  ) AS e(tbl, client, expected)
  JOIN LATERAL (
    SELECT CASE e.tbl
      WHEN 'client_configs' THEN (SELECT count(*) FROM {SCHEMA}.client_configs WHERE client_id = e.client)
      WHEN 'client_config_versions' THEN (SELECT count(*) FROM {SCHEMA}.client_config_versions WHERE client_id = e.client)
      WHEN 'task_templates' THEN (SELECT count(*) FROM {SCHEMA}.task_templates WHERE client_id = e.client)
      WHEN 'client_document_requirements' THEN (SELECT count(*) FROM {SCHEMA}.client_document_requirements WHERE client_id = e.client)
      WHEN 'client_service_policies' THEN (SELECT count(*) FROM {SCHEMA}.client_service_policies WHERE client_id = e.client)
      WHEN 'client_milestone_templates' THEN (SELECT count(*) FROM {SCHEMA}.client_milestone_templates WHERE client_id = e.client)
      WHEN 'client_holidays' THEN (SELECT count(*) FROM {SCHEMA}.client_holidays WHERE client_id = e.client)
    END AS n) a ON TRUE
  WHERE a.n <> e.expected;
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'row count mismatch (a duplicate-prevention check skipped a row, or a row failed): %', bad; END IF;

  -- 2. every child row's client exists in client_configs
  IF EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions c WHERE c.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_configs p WHERE p.client_id = c.client_id))
     OR EXISTS (SELECT 1 FROM {SCHEMA}.task_templates c WHERE c.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_configs p WHERE p.client_id = c.client_id))
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_document_requirements c WHERE c.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_configs p WHERE p.client_id = c.client_id))
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_service_policies c WHERE c.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_configs p WHERE p.client_id = c.client_id))
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_milestone_templates c WHERE c.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_configs p WHERE p.client_id = c.client_id))
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_holidays c WHERE c.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_configs p WHERE p.client_id = c.client_id))
  THEN RAISE EXCEPTION 'a child row references a client with no client_configs row'; END IF;

  -- 3. every versioned child row points at an existing client_config_versions row
  IF EXISTS (SELECT 1 FROM {SCHEMA}.task_templates c WHERE c.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions v WHERE v.client_id = c.client_id AND v.version = c.config_version))
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_document_requirements c WHERE c.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions v WHERE v.client_id = c.client_id AND v.version = c.config_version))
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_service_policies c WHERE c.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions v WHERE v.client_id = c.client_id AND v.version = c.config_version))
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_milestone_templates c WHERE c.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions v WHERE v.client_id = c.client_id AND v.version = c.config_version))
  THEN RAISE EXCEPTION 'a versioned row references a configuration version that does not exist'; END IF;

  -- 4. no duplicate natural keys (belt and braces: the live schema has UNIQUE (client_id, config_version, key) on documents and milestones; client_service_policies' own unique constraint was not in the schema export)
  IF EXISTS (SELECT 1 FROM {SCHEMA}.client_configs WHERE client_id IN ({client_list}) GROUP BY client_id HAVING count(*) > 1)
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions WHERE client_id IN ({client_list}) GROUP BY client_id, version HAVING count(*) > 1)
     OR EXISTS (SELECT 1 FROM {SCHEMA}.task_templates WHERE client_id IN ({client_list}) GROUP BY client_id, template_key HAVING count(*) > 1)
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_document_requirements WHERE client_id IN ({client_list}) GROUP BY client_id, config_version, requirement_key HAVING count(*) > 1)
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_service_policies WHERE client_id IN ({client_list}) GROUP BY client_id, config_version, service_category, service_key HAVING count(*) > 1)
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_milestone_templates WHERE client_id IN ({client_list}) GROUP BY client_id, config_version, milestone_key HAVING count(*) > 1)
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_holidays WHERE client_id IN ({client_list}) GROUP BY client_id, holiday_date HAVING count(*) > 1)
  THEN RAISE EXCEPTION 'duplicate natural key found'; END IF;

  -- 5. task dependencies and milestone requirements point at templates of the same client
  SELECT string_agg(t.client_id || ':' || t.template_key || '->' || d, '; ') INTO bad
  FROM {SCHEMA}.task_templates t, unnest(t.depends_on_template_keys) AS d
  WHERE t.client_id IN ({client_list})
    AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.task_templates x WHERE x.client_id = t.client_id AND x.template_key = d);
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'task depends on an unknown template: %', bad; END IF;
  SELECT string_agg(m.client_id || ':' || m.milestone_key || '->' || k, '; ') INTO bad
  FROM {SCHEMA}.client_milestone_templates m, unnest(m.required_task_keys) AS k
  WHERE m.client_id IN ({client_list})
    AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.task_templates x WHERE x.client_id = m.client_id AND x.template_key = k);
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'milestone requires an unknown template: %', bad; END IF;

  -- 6. version and client agree on the document hash; approved versions carry approver and timestamp
  IF EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions v JOIN {SCHEMA}.client_configs c ON c.client_id = v.client_id
             WHERE v.client_id IN ({client_list}) AND v.config_hash <> c.config_hash)
     OR EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions WHERE client_id IN ({client_list}) AND status = 'approved' AND (approved_by IS NULL OR approved_at IS NULL))
  THEN RAISE EXCEPTION 'configuration version metadata is inconsistent'; END IF;

  -- 7. the data is marked synthetic
  IF EXISTS (SELECT 1 FROM {SCHEMA}.client_configs WHERE client_id IN ({client_list}) AND settings_payload ->> 'data_origin' IS DISTINCT FROM 'synthetic')
  THEN RAISE EXCEPTION 'a seeded client is not marked synthetic'; END IF;

  RAISE NOTICE 'seed verified: {EXPECTED_TOTAL} rows, 4 clients, no duplicates, no missing references';
END
$verify$;"""

    exp_table = "\n".join(f"--   {t:<30} {totals[t]:>3}   " + "  ".join(f"{c[-1]}={expected[t][c]}" for c in clients) for t in ORDER)
    summary_values = ",\n    ".join(f"({q(t)}, {q(c)}, {expected[t][c]})" for t in ORDER for c in clients)
    count_cases = "\n".join(f"      WHEN {q(t)} THEN (SELECT count(*) FROM {SCHEMA}.{t} WHERE client_id = e.client)" for t in ORDER)
    pre_counts = "\nUNION ALL\n".join(f"SELECT {q(t)} AS table_name, count(*) AS rows_for_the_four_clients FROM {SCHEMA}.{t} WHERE client_id IN ({client_list})" for t in ORDER)
    total_now = "\nUNION ALL\n".join(f"SELECT {q(t)} AS table_name, count(*) AS total_rows_now FROM {SCHEMA}.{t}" for t in ORDER)

    header = f"""-- =====================================================================================================================================
-- manual_seed_four_synthetic_clients.sql
--
-- PURPOSE   Insert the four SYNTHETIC client configurations (SYN-CLIENT-A .. SYN-CLIENT-D) into the Supabase DEVELOPMENT database: {EXPECTED_TOTAL} rows
--           across seven existing tables. Generated from client_configuration/profiles/client_[a-d]_config.json by
--           client_configuration/generate_manual_seed_sql.py (the same projection the Python seeder's dry run uses).
--
-- WARNING   DEVELOPMENT ONLY. Do NOT run this against Production. Every value is a labelled synthetic test assumption, not an approved client policy.
--           It contains no secrets, no personal data and no real client data. It does not touch role_access_matrix (UC-04 has not confirmed that
--           projection) and it creates no table, column or index.
--
-- BEHAVIOUR One transaction. It INSERTs only; it never UPDATEs, DELETEs or TRUNCATEs, and it never overwrites. It refuses to run if any row already exists for
--           the four client ids. Every INSERT also carries a NOT EXISTS guard on the natural key, and a final assertion block RAISEs an EXCEPTION (so the whole
--           transaction rolls back) if any row was skipped or any reference is wrong. Nothing is ignored silently.
--
-- HOW TO RUN Paste the whole file into the Supabase SQL editor of the DEVELOPMENT project (role postgres). Read the pre-insert queries (section 1) first; run
--           sections 2-5 together. If anything fails, nothing is committed. See manual_seed_four_synthetic_clients_README.md.
--
-- EXPECTED INSERTS
{exp_table}
--   {'TOTAL':<30} {EXPECTED_TOTAL:>3}
--
-- DUPLICATE-PREVENTION KEYS
--   client_configs (client_id) | client_config_versions (client_id, version) | task_templates (client_id, template_key)  <- the live PRIMARY KEY has no config_version
--   client_document_requirements (client_id, config_version, requirement_key) | client_service_policies (client_id, config_version, service_category, service_key)
--   client_milestone_templates (client_id, config_version, milestone_key) | client_holidays (client_id, holiday_date)
--   Per the schema exports, client_document_requirements and client_milestone_templates have UNIQUE (client_id, config_version, key) and, with client_service_policies, a composite
--   foreign key (client_id, config_version) -> client_config_versions(client_id, version). client_service_policies' own PK/UNIQUE was not in the (truncated) export, so these checks plus a
--   table lock are the protection there (see the README for the concurrency limitation).
-- ====================================================================================================================================="""

    sql = f"""{header}

-- =====================================================================================================================================
-- SECTION 1. PRE-INSERT VERIFICATION (read-only SELECTs; run these first and read the results)
-- =====================================================================================================================================

-- 1a. Rows that already exist for the four client ids. EXPECTED: 0 for every table.
{pre_counts};

-- 1b. Current total rows per table (for your records; the seed adds the counts in the header).
{total_now};

-- 1c. Client ids already present (the four SYN-CLIENT ids must NOT appear here).
SELECT client_id, company_name, is_active FROM {SCHEMA}.client_configs ORDER BY client_id;

-- 1d. The primary keys and foreign keys actually present on the seven tables (so you can compare with the README).
SELECT conrelid::regclass AS table_name, conname, contype AS kind, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid IN ({", ".join(f"'{SCHEMA}.{t}'::regclass" for t in ORDER)}) AND contype IN ('p', 'f', 'u')
ORDER BY conrelid::regclass::text, contype, conname;

-- =====================================================================================================================================
-- SECTION 2. TRANSACTION, LOCKS AND GUARDS
-- =====================================================================================================================================
BEGIN;

-- Serialise concurrent writers on the three versioned tables (reads are still allowed). Needed most for client_service_policies, whose unique constraint is unconfirmed. Released at COMMIT / ROLLBACK.
LOCK TABLE {SCHEMA}.client_document_requirements, {SCHEMA}.client_service_policies, {SCHEMA}.client_milestone_templates IN SHARE ROW EXCLUSIVE MODE;

-- Guard 1: the live schema still has every column this script supplies and no NOT NULL-without-default column it does not supply.
{schema_guard()}

-- Guard 2: first-time seed only. Refuses to run if any row already exists for the four client ids.
{already_seeded_guard()}

-- =====================================================================================================================================
-- SECTION 3. INSERTS (foreign-key order: client_configs first)
-- =====================================================================================================================================
"""
    for n, t in enumerate(ORDER, 1):
        sql += f"\n-- 3.{n} {t}: {totals[t]} rows (" + ", ".join(f"{c[-1]}={expected[t][c]}" for c in clients) + ")\n" + inserts[t]
    sql += f"""
-- =====================================================================================================================================
-- SECTION 4. IN-TRANSACTION ASSERTIONS (any failure RAISEs and rolls the whole seed back)
-- =====================================================================================================================================
{checks}

COMMIT;

-- =====================================================================================================================================
-- SECTION 5. POST-INSERT VERIFICATION (read-only SELECTs, after COMMIT)
-- =====================================================================================================================================

-- 5a. Row counts per table and client against the expected counts. EXPECTED: every row ok = true; the final total below = {EXPECTED_TOTAL}.
SELECT e.tbl AS table_name, e.client AS client_id, e.expected, a.n AS found, (a.n = e.expected) AS ok
FROM (VALUES
    {summary_values}
) AS e(tbl, client, expected)
JOIN LATERAL (
  SELECT CASE e.tbl
{count_cases}
  END AS n) a ON TRUE
ORDER BY e.tbl, e.client;

-- 5b. Grand total across the seven tables for the four clients. EXPECTED: {EXPECTED_TOTAL}.
SELECT sum(n) AS total_rows_seeded FROM (
{pre_counts.replace("rows_for_the_four_clients", "n").replace(" AS table_name", " AS tbl")}
) s;

-- 5c. Each client and its configuration version.
SELECT c.client_id, c.company_name, c.timezone, c.working_days, c.settings_payload ->> 'data_origin' AS data_origin,
       v.version, v.status, v.effective_from, v.effective_to, v.approved_by, (v.config_hash = c.config_hash) AS hash_matches
FROM {SCHEMA}.client_configs c JOIN {SCHEMA}.client_config_versions v ON v.client_id = c.client_id
WHERE c.client_id IN ({client_list}) ORDER BY c.client_id;

-- 5d. Child rows whose client or version is missing. EXPECTED: no rows.
SELECT 'task_templates' AS table_name, t.client_id, t.template_key AS key FROM {SCHEMA}.task_templates t
  WHERE t.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions v WHERE v.client_id = t.client_id AND v.version = t.config_version)
UNION ALL SELECT 'client_document_requirements', d.client_id, d.requirement_key FROM {SCHEMA}.client_document_requirements d
  WHERE d.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions v WHERE v.client_id = d.client_id AND v.version = d.config_version)
UNION ALL SELECT 'client_service_policies', p.client_id, p.service_category || '/' || p.service_key FROM {SCHEMA}.client_service_policies p
  WHERE p.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions v WHERE v.client_id = p.client_id AND v.version = p.config_version)
UNION ALL SELECT 'client_milestone_templates', m.client_id, m.milestone_key FROM {SCHEMA}.client_milestone_templates m
  WHERE m.client_id IN ({client_list}) AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.client_config_versions v WHERE v.client_id = m.client_id AND v.version = m.config_version);

-- 5e. Duplicate natural keys. EXPECTED: no rows.
SELECT 'task_templates' AS table_name, client_id, template_key AS key, count(*) FROM {SCHEMA}.task_templates WHERE client_id IN ({client_list}) GROUP BY client_id, template_key HAVING count(*) > 1
UNION ALL SELECT 'client_document_requirements', client_id, requirement_key, count(*) FROM {SCHEMA}.client_document_requirements WHERE client_id IN ({client_list}) GROUP BY client_id, config_version, requirement_key HAVING count(*) > 1
UNION ALL SELECT 'client_service_policies', client_id, service_category || '/' || service_key, count(*) FROM {SCHEMA}.client_service_policies WHERE client_id IN ({client_list}) GROUP BY client_id, config_version, service_category, service_key HAVING count(*) > 1
UNION ALL SELECT 'client_milestone_templates', client_id, milestone_key, count(*) FROM {SCHEMA}.client_milestone_templates WHERE client_id IN ({client_list}) GROUP BY client_id, config_version, milestone_key HAVING count(*) > 1
UNION ALL SELECT 'client_holidays', client_id, holiday_date::text, count(*) FROM {SCHEMA}.client_holidays WHERE client_id IN ({client_list}) GROUP BY client_id, holiday_date HAVING count(*) > 1;

-- 5f. Existing clients were not touched: rows per table for every client id NOT in the four.
SELECT 'client_configs' AS table_name, count(*) AS other_clients_rows FROM {SCHEMA}.client_configs WHERE client_id NOT IN ({client_list})
UNION ALL SELECT 'task_templates', count(*) FROM {SCHEMA}.task_templates WHERE client_id NOT IN ({client_list})
UNION ALL SELECT 'client_document_requirements', count(*) FROM {SCHEMA}.client_document_requirements WHERE client_id NOT IN ({client_list})
UNION ALL SELECT 'client_service_policies', count(*) FROM {SCHEMA}.client_service_policies WHERE client_id NOT IN ({client_list});
-- (compare 5f with the "rows now" counts from 1b minus nothing: they must be unchanged: 6 / 18 / 6 / 14 on 2026-11-04)
"""
    meta = {"totals": totals, "per_client": expected, "columns": used_cols}
    return sql, meta


def main() -> None:
    sql, meta = build()
    OUT.write_text(sql, encoding="utf-8", newline="\n")
    # independent count from the generated text: one value tuple per line starting with "  ("
    counts, current = {}, None
    for line in sql.splitlines():
        m = re.match(r"INSERT INTO \w+\.(\w+) ", line)
        if m:
            current = m.group(1)
            counts[current] = 0
        elif current and re.match(r"^  \(", line) and line.rstrip().endswith((",", ")")):
            counts[current] += 1
        elif line.startswith(") AS v("):
            current = None
    print(json.dumps({"generated_rows": meta["totals"], "counted_in_file": counts, "total": sum(counts.values()),
                      "bytes": OUT.stat().st_size}, indent=2))
    if counts != meta["totals"] or sum(counts.values()) != EXPECTED_TOTAL:
        raise SystemExit("row count in the file does not match the plan")


if __name__ == "__main__":
    main()
