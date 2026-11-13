-- ============================================================================
-- Seed the missing employee_number_counters rows (INSERT-ONLY, idempotent).
--
-- next_employee_no(p_client_id) refuses a client without a counter row.
-- uc01_gaps_complete.sql seeded one for every client that existed when it
-- ran (2026-11-03); clients created later (SYN-UAE-TEST, 2026-11-04) have
-- none. This applies the same rule to them. Existing counters - and the
-- numbers already issued - are never updated, and nothing is deleted.
--
-- Same effect as: python scripts/seed_employee_number_counters.py --apply
-- Safe to run repeatedly; a second run inserts nothing.
-- ============================================================================

INSERT INTO employee_number_counters (client_id, last_value, prefix, padding)
SELECT c.client_id, 0, 'EMP-', 6
  FROM client_configs c
 WHERE NOT EXISTS (SELECT 1 FROM employee_number_counters n WHERE n.client_id = c.client_id)
ON CONFLICT (client_id) DO NOTHING;
