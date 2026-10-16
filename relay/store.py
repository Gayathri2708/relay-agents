"""Persistence for runs, their per-agent traces, human decisions, resolution and postmortem."""

import uuid
from datetime import UTC, datetime
from typing import Literal, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, computed_field

from .executor import Simulation
from .graph import IncidentState
from .schemas import PostmortemDraft, ProposedAction
from .tracing import AgentUsage, Span, summarize


def _now() -> datetime:
    return datetime.now(UTC)


class Decision(BaseModel):
    action_id: str
    decision: Literal["approved", "rejected"]
    decided_by: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=2000)
    decided_at: datetime = Field(default_factory=_now)
    edited_action: ProposedAction | None = None  # the action as approved, when a human edited it
    simulation: Simulation | None = None  # mock executor dry run, approved actions only


class Resolution(BaseModel):
    resolved_by: str = Field(default="", max_length=100, description="Ignored when API keys are on")
    note: str = Field(default="", max_length=4000)
    resolved_at: datetime = Field(default_factory=_now)


class Postmortem(BaseModel):
    draft: PostmortemDraft
    markdown: str
    created_at: datetime = Field(default_factory=_now)


class RunRecord(BaseModel):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    state: IncidentState
    decisions: list[Decision] = []
    resolution: Resolution | None = None
    postmortem: Postmortem | None = None

    @computed_field
    @property
    def usage(self) -> list[AgentUsage]:
        return summarize(self.state.trace)


class RunSummary(BaseModel):
    id: uuid.UUID
    created_at: datetime
    status: str
    service: str | None
    severity: str | None
    cost_usd: float


INTERRUPTED = "interrupted: Relay restarted while this run was in progress; submit the alert again"


class AlreadyDecided(Exception):
    pass


class RunStore(Protocol):
    def create(self, state: IncidentState) -> RunRecord: ...
    def update(self, run_id: uuid.UUID, state: IncidentState) -> None: ...
    def get(self, run_id: uuid.UUID) -> RunRecord | None: ...
    def recent(self, limit: int = 50) -> list[RunRecord]: ...
    def summaries(self, limit: int = 50) -> list[RunSummary]: ...
    def fail_interrupted(self) -> int: ...
    def decide(self, run_id: uuid.UUID, decision: Decision) -> Decision: ...
    def resolve(self, run_id: uuid.UUID, state: IncidentState, resolution: Resolution) -> None: ...
    def save_postmortem(self, run_id: uuid.UUID, postmortem: Postmortem, span: Span) -> None: ...
    def usage(self) -> list[AgentUsage]: ...


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, RunRecord] = {}

    def create(self, state: IncidentState) -> RunRecord:
        now = _now()
        record = RunRecord(id=uuid.uuid4(), created_at=now, updated_at=now, state=state)
        self._runs[record.id] = record
        return record.model_copy(deep=True)

    def update(self, run_id: uuid.UUID, state: IncidentState) -> None:
        self._runs[run_id].state, self._runs[run_id].updated_at = state, _now()

    def get(self, run_id: uuid.UUID) -> RunRecord | None:
        record = self._runs.get(run_id)
        return record.model_copy(deep=True) if record else None

    def recent(self, limit: int = 50) -> list[RunRecord]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)[:limit]

    def summaries(self, limit: int = 50) -> list[RunSummary]:
        return [
            RunSummary(
                id=r.id, created_at=r.created_at, status=r.state.status,
                service=r.state.triage.service if r.state.triage else None,
                severity=r.state.triage.severity if r.state.triage else None,
                cost_usd=round(sum(u.cost_usd for u in r.usage), 6),
            )
            for r in self.recent(limit)
        ]

    def fail_interrupted(self) -> int:
        stuck = [r for r in self._runs.values() if r.state.status == "running"]
        for r in stuck:
            r.state.status, r.state.error = "failed", INTERRUPTED
        return len(stuck)

    def decide(self, run_id: uuid.UUID, decision: Decision) -> Decision:
        record = self._runs[run_id]
        if any(d.action_id == decision.action_id for d in record.decisions):
            raise AlreadyDecided(decision.action_id)
        record.decisions.append(decision)
        return decision

    def resolve(self, run_id: uuid.UUID, state: IncidentState, resolution: Resolution) -> None:
        self.update(run_id, state)
        self._runs[run_id].resolution = resolution

    def save_postmortem(self, run_id: uuid.UUID, postmortem: Postmortem, span: Span) -> None:
        record = self._runs[run_id]
        record.postmortem = postmortem
        record.state.trace.append(span)

    def usage(self) -> list[AgentUsage]:
        return summarize([s for r in self._runs.values() for s in r.state.trace])


class PostgresRunStore:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    @staticmethod
    def _state_json(state: IncidentState) -> Jsonb:
        return Jsonb(state.model_dump(mode="json", exclude={"trace"}))

    def _insert_spans(self, conn, run_id: uuid.UUID, spans: list[Span], first_seq: int) -> None:
        for seq, s in enumerate(spans, first_seq):
            conn.execute(
                "INSERT INTO agent_traces (run_id, seq, agent, loop, started_at, latency_ms, status, model,"
                " prompt_tokens, completion_tokens, cost_usd, input, output, error, llm_calls, tool_calls)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (run_id, seq) DO NOTHING",
                (
                    run_id, seq, s.agent, s.loop, s.started_at, s.latency_ms, s.status, s.model,
                    s.prompt_tokens, s.completion_tokens, s.cost_usd, Jsonb(s.input),
                    Jsonb(s.output) if s.output is not None else None, s.error,
                    Jsonb([c.model_dump(mode="json") for c in s.llm_calls]),
                    Jsonb([c.model_dump(mode="json") for c in s.tool_calls]),
                ),
            )

    def create(self, state: IncidentState) -> RunRecord:
        run_id = uuid.uuid4()
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO runs (id, status, alert, state) VALUES (%s, %s, %s, %s) RETURNING created_at, updated_at",
                (run_id, state.status, Jsonb(state.alert.model_dump(mode="json")), self._state_json(state)),
            ).fetchone()
        return RunRecord(id=run_id, created_at=row["created_at"], updated_at=row["updated_at"], state=state)

    def update(self, run_id: uuid.UUID, state: IncidentState) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE runs SET status = %s, state = %s, updated_at = now() WHERE id = %s",
                (state.status, self._state_json(state), run_id),
            )
            stored = conn.execute("SELECT count(*) AS n FROM agent_traces WHERE run_id = %s", (run_id,)).fetchone()
            self._insert_spans(conn, run_id, state.trace[stored["n"] :], stored["n"])

    def _spans(self, conn, run_id: uuid.UUID) -> list[Span]:
        rows = conn.execute(
            "SELECT agent, loop, started_at, latency_ms, status, input, output, error, llm_calls, tool_calls"
            " FROM agent_traces WHERE run_id = %s ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [Span.model_validate(r) for r in rows]

    def _record(self, conn, row: dict) -> RunRecord:
        decisions = conn.execute(
            "SELECT action_id, decision, decided_by, note, decided_at, edited_action, simulation"
            " FROM action_decisions WHERE run_id = %s ORDER BY decided_at",
            (row["id"],),
        ).fetchall()
        state = IncidentState.model_validate({**row["state"], "trace": self._spans(conn, row["id"])})
        return RunRecord(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            state=state,
            decisions=[Decision.model_validate(d) for d in decisions],
            resolution=row["resolution"],
            postmortem=row["postmortem"],
        )

    def get(self, run_id: uuid.UUID) -> RunRecord | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = %s", (run_id,)).fetchone()
            return self._record(conn, row) if row else None

    def recent(self, limit: int = 50) -> list[RunRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
            return [self._record(conn, r) for r in rows]

    def summaries(self, limit: int = 50) -> list[RunSummary]:
        """One cheap query for the runs list, instead of loading every run's full trace."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT r.id, r.created_at, r.status, r.state->'triage'->>'service' AS service,"
                " r.state->'triage'->>'severity' AS severity,"
                " coalesce((SELECT sum(t.cost_usd) FROM agent_traces t WHERE t.run_id = r.id), 0)::float AS cost_usd"
                " FROM runs r ORDER BY r.created_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [RunSummary.model_validate(r) for r in rows]

    def fail_interrupted(self) -> int:
        """Runs live in an in-process thread pool; after a restart, 'running' rows would never finish."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE runs SET status = 'failed', updated_at = now(),"
                " state = jsonb_set(jsonb_set(state, '{status}', '\"failed\"'), '{error}', to_jsonb(%s::text))"
                " WHERE status = 'running'",
                (INTERRUPTED,),
            )
            return cur.rowcount

    def decide(self, run_id: uuid.UUID, decision: Decision) -> Decision:
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO action_decisions (run_id, action_id, decision, decided_by, note, decided_at,"
                " edited_action, simulation) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT DO NOTHING RETURNING action_id",
                (
                    run_id, decision.action_id, decision.decision, decision.decided_by, decision.note,
                    decision.decided_at,
                    Jsonb(decision.edited_action.model_dump(mode="json")) if decision.edited_action else None,
                    Jsonb(decision.simulation.model_dump(mode="json")) if decision.simulation else None,
                ),
            ).fetchone()
        if row is None:
            raise AlreadyDecided(decision.action_id)
        return decision

    def resolve(self, run_id: uuid.UUID, state: IncidentState, resolution: Resolution) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE runs SET status = %s, state = %s, resolution = %s, updated_at = now() WHERE id = %s",
                (state.status, self._state_json(state), Jsonb(resolution.model_dump(mode="json")), run_id),
            )

    def save_postmortem(self, run_id: uuid.UUID, postmortem: Postmortem, span: Span) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE runs SET postmortem = %s, updated_at = now() WHERE id = %s",
                (Jsonb(postmortem.model_dump(mode="json")), run_id),
            )
            n = conn.execute("SELECT count(*) AS n FROM agent_traces WHERE run_id = %s", (run_id,)).fetchone()["n"]
            self._insert_spans(conn, run_id, [span], n)

    def usage(self) -> list[AgentUsage]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT agent, count(*) AS runs,"
                " coalesce(sum(jsonb_array_length(llm_calls)), 0) AS llm_calls,"
                " coalesce(sum(prompt_tokens), 0) AS prompt_tokens,"
                " coalesce(sum(completion_tokens), 0) AS completion_tokens,"
                " coalesce(sum(cost_usd), 0)::float AS cost_usd, avg(latency_ms)::int AS avg_latency_ms"
                " FROM agent_traces GROUP BY agent ORDER BY agent"
            ).fetchall()
        return [AgentUsage.model_validate(r) for r in rows]
