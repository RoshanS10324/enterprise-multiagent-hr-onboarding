-- =====================================================================================================================================
-- four_mock_clients_dry_run_readonly.sql  -  READ-ONLY. SELECT statements only; changes nothing.
-- Checks that the database is in the state the seed script (four_mock_clients_seed.sql) expects, for CLIENT_CANADA, CLIENT_DUBAI, CLIENT_INDIA,
-- CLIENT_USA. Run it in the SQL editor of the project you intend to seed, and confirm first which project (Development or Production) that is.
-- Expected seed: 231 inserts, 0 updates, 0 deletes; the four client_configs rows, every version 1 row and CLIENT_DUBAI's two stored holidays are kept.
-- =====================================================================================================================================
BEGIN READ ONLY;
-- R1. The four client master rows exist, are active, marked mock, and match the profiles' timezone and working days. EXPECTED: 4 rows, all ok.
SELECT e.client_id, c.company_name, c.timezone, c.working_days, c.is_active, c.settings_payload ->> 'environment' AS environment,
       (c.timezone = e.tz AND c.working_days = e.wd AND c.is_active AND c.settings_payload ->> 'environment' = 'mock') AS ok
FROM (VALUES
    ('CLIENT_CANADA', 'America/Toronto', ARRAY[1,2,3,4,5]::integer[]),
    ('CLIENT_DUBAI', 'Asia/Dubai', ARRAY[1,2,3,4,5]::integer[]),
    ('CLIENT_INDIA', 'Asia/Kolkata', ARRAY[1,2,3,4,5,6]::integer[]),
    ('CLIENT_USA', 'America/New_York', ARRAY[1,2,3,4,5]::integer[])
) AS e(client_id, tz, wd)
LEFT JOIN public.client_configs c ON c.client_id = e.client_id
ORDER BY e.client_id;

-- R2. Existing versions (EXPECTED: only version 1, status draft, for each) and no version 2 yet.
SELECT client_id, version, status, effective_from, effective_to, approved_by FROM public.client_config_versions WHERE client_id IN ('CLIENT_CANADA', 'CLIENT_DUBAI', 'CLIENT_INDIA', 'CLIENT_USA') ORDER BY 1, 2;

-- R3. Rows that would collide with the seed. EXPECTED: 0 everywhere.
SELECT 'task_templates' AS table_name, count(*) FROM public.task_templates WHERE client_id IN ('CLIENT_CANADA', 'CLIENT_DUBAI', 'CLIENT_INDIA', 'CLIENT_USA')
UNION ALL SELECT 'client_document_requirements', count(*) FROM public.client_document_requirements WHERE client_id IN ('CLIENT_CANADA', 'CLIENT_DUBAI', 'CLIENT_INDIA', 'CLIENT_USA')
UNION ALL SELECT 'client_service_policies', count(*) FROM public.client_service_policies WHERE client_id IN ('CLIENT_CANADA', 'CLIENT_DUBAI', 'CLIENT_INDIA', 'CLIENT_USA')
UNION ALL SELECT 'client_milestone_templates', count(*) FROM public.client_milestone_templates WHERE client_id IN ('CLIENT_CANADA', 'CLIENT_DUBAI', 'CLIENT_INDIA', 'CLIENT_USA')
UNION ALL SELECT 'client_config_versions v2', count(*) FROM public.client_config_versions WHERE client_id IN ('CLIENT_CANADA', 'CLIENT_DUBAI', 'CLIENT_INDIA', 'CLIENT_USA') AND version = 2;

-- R4. Stored holidays that the profiles PRESERVE (must exist with exactly these values). EXPECTED: every row ok = true.
SELECT e.client_id, e.holiday_date, h.name, h.is_observed, (h.name = e.name AND h.is_observed = e.observed) AS ok
FROM (VALUES
    ('CLIENT_DUBAI', '2026-12-02'::date, '[SAMPLE] UAE National Day', TRUE),
    ('CLIENT_DUBAI', '2026-12-03'::date, '[SAMPLE] UAE National Day (2nd)', TRUE)
) AS e(client_id, holiday_date, name, observed)
LEFT JOIN public.client_holidays h ON h.client_id = e.client_id AND h.holiday_date = e.holiday_date
WHERE e.client_id IS NOT NULL;

-- R5. Holidays the seed would ADD that already exist. EXPECTED: no rows.
SELECT h.client_id, h.holiday_date, h.name FROM public.client_holidays h
WHERE (h.client_id, h.holiday_date) IN (('CLIENT_CANADA', '2026-12-25'::date), ('CLIENT_CANADA', '2026-12-28'::date), ('CLIENT_CANADA', '2027-01-01'::date), ('CLIENT_INDIA', '2026-12-25'::date), ('CLIENT_INDIA', '2027-01-26'::date), ('CLIENT_USA', '2026-11-26'::date), ('CLIENT_USA', '2026-12-25'::date), ('CLIENT_USA', '2027-01-01'::date));

-- R6. Constraints actually present on the seven tables (for comparison with docs/CLIENT_CONFIGURATION_COLUMN_MAPPING.md).
SELECT conrelid::regclass::text AS table_name, conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint WHERE conrelid IN ('public.client_configs'::regclass, 'public.client_config_versions'::regclass, 'public.task_templates'::regclass, 'public.client_document_requirements'::regclass, 'public.client_service_policies'::regclass, 'public.client_milestone_templates'::regclass, 'public.client_holidays'::regclass) AND contype IN ('p','f','u','c') ORDER BY 1, 3, 2;
ROLLBACK;
