-- PROPOSED migration for review. NOT APPLIED. Run nothing from this file until the integration team approves it.
-- Adds two UC-04-owned tables. It changes no existing table and contains no DROP, TRUNCATE or DELETE.
-- Foreign keys point at existing tables (onboarding_cases, client_configs). Idempotent: CREATE ... IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS uc04_case_state (
    case_id       uuid    PRIMARY KEY REFERENCES onboarding_cases(case_id),
    client_id     text    NOT NULL REFERENCES client_configs(client_id),
    employee_ref  text    NOT NULL,
    version       bigint  NOT NULL CHECK (version >= 0),
    record        jsonb   NOT NULL,          -- items with exact states, ticket ids, attempts, adapter errors, archive, fingerprint
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS uc04_replay_results (
    case_id          uuid   NOT NULL REFERENCES uc04_case_state(case_id),
    idempotency_key  text   NOT NULL,
    payload_hash     text   NOT NULL,
    store_version    bigint NOT NULL,
    parent_status    text   NOT NULL,
    result           jsonb  NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (case_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_uc04_case_state_client ON uc04_case_state (client_id);

-- Row-level security: enabled with no policy, so the anon and authenticated roles see nothing; only the server-side service role reads or writes.
ALTER TABLE uc04_case_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE uc04_replay_results ENABLE ROW LEVEL SECURITY;
