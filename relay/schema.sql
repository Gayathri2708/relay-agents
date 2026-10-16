-- Operational data the investigate agent can query (seeded with fake data by seed/seed_db.py).
CREATE TABLE IF NOT EXISTS deploys (
    id               serial PRIMARY KEY,
    service          text        NOT NULL,
    version          text        NOT NULL,
    previous_version text        NOT NULL,
    author           text        NOT NULL,
    commit_sha       text        NOT NULL,
    summary          text        NOT NULL,
    has_migration    boolean     NOT NULL DEFAULT false,
    finished_at      timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS deploys_finished_idx ON deploys (finished_at);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id          serial PRIMARY KEY,
    service     text        NOT NULL,
    captured_at timestamptz NOT NULL,
    metrics     jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS metric_snapshots_idx ON metric_snapshots (service, captured_at);

CREATE TABLE IF NOT EXISTS log_snapshots (
    id          serial PRIMARY KEY,
    service     text        NOT NULL,
    captured_at timestamptz NOT NULL,
    level       text        NOT NULL,
    message     text        NOT NULL
);
CREATE INDEX IF NOT EXISTS log_snapshots_idx ON log_snapshots (service, captured_at);

CREATE TABLE IF NOT EXISTS past_incidents (
    id          text PRIMARY KEY,
    service     text        NOT NULL,
    severity    text        NOT NULL,
    title       text        NOT NULL,
    summary     text        NOT NULL,
    root_cause  text        NOT NULL,
    resolution  text        NOT NULL,
    started_at  timestamptz NOT NULL,
    resolved_at timestamptz NOT NULL,
    search      tsvector GENERATED ALWAYS AS (
        to_tsvector('english', title || ' ' || summary || ' ' || root_cause || ' ' || resolution)
    ) STORED
);
CREATE INDEX IF NOT EXISTS past_incidents_search_idx ON past_incidents USING gin (search);

-- Pipeline runs and the human decisions on their proposed actions. Alert text is stored redacted.
CREATE TABLE IF NOT EXISTS runs (
    id            uuid PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    status        text        NOT NULL,
    failed_stage  text,
    error         text,
    alert         jsonb       NOT NULL,
    triage        jsonb,
    investigation jsonb,
    plan          jsonb
);

CREATE TABLE IF NOT EXISTS action_decisions (
    run_id     uuid        NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    action_id  text        NOT NULL,
    decision   text        NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_by text        NOT NULL,
    note       text,
    decided_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, action_id)
);
