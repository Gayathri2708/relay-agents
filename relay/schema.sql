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

-- Pipeline runs. `state` is the IncidentState without its trace (traces live in agent_traces).
-- Alert text is stored redacted.
CREATE TABLE IF NOT EXISTS runs (
    id         uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    status     text        NOT NULL,
    alert      jsonb       NOT NULL
);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS updated_at      timestamptz NOT NULL DEFAULT now();
ALTER TABLE runs ADD COLUMN IF NOT EXISTS state           jsonb;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS resolution      jsonb;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS postmortem      jsonb;
-- v0.1 kept each stage in its own column; the state column replaces them.
ALTER TABLE runs DROP COLUMN IF EXISTS failed_stage, DROP COLUMN IF EXISTS error,
    DROP COLUMN IF EXISTS triage, DROP COLUMN IF EXISTS investigation, DROP COLUMN IF EXISTS plan;

-- One row per agent step: what went in, what came out, every LLM attempt and tool call, and the cost.
CREATE TABLE IF NOT EXISTS agent_traces (
    run_id            uuid        NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq               int         NOT NULL,
    agent             text        NOT NULL,
    loop              int         NOT NULL,
    started_at        timestamptz NOT NULL,
    latency_ms        int         NOT NULL,
    status            text        NOT NULL,
    model             text,
    prompt_tokens     int         NOT NULL,
    completion_tokens int         NOT NULL,
    cost_usd          numeric(12, 6) NOT NULL,
    input             jsonb       NOT NULL,
    output            jsonb,
    error             text,
    llm_calls         jsonb       NOT NULL,
    tool_calls        jsonb       NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS agent_traces_agent_idx ON agent_traces (agent);

CREATE TABLE IF NOT EXISTS action_decisions (
    run_id     uuid        NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    action_id  text        NOT NULL,
    decision   text        NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_by text        NOT NULL,
    note       text,
    decided_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, action_id)
);
ALTER TABLE action_decisions ADD COLUMN IF NOT EXISTS edited_action jsonb;  -- set when approved with edits
ALTER TABLE action_decisions ADD COLUMN IF NOT EXISTS simulation    jsonb;  -- mock executor dry run
