"""Persistence for pipeline runs and the human decisions on their proposed actions."""

import uuid
from datetime import UTC, datetime
from typing import Literal, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from .graph import IncidentState


class Decision(BaseModel):
    action_id: str
    decision: Literal["approved", "rejected"]
    decided_by: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=2000)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunRecord(BaseModel):
    id: uuid.UUID
    created_at: datetime
    state: IncidentState
    decisions: list[Decision] = []


class AlreadyDecided(Exception):
    pass


class RunStore(Protocol):
    def save(self, state: IncidentState) -> RunRecord: ...
    def get(self, run_id: uuid.UUID) -> RunRecord | None: ...
    def list(self, limit: int = 50) -> list[RunRecord]: ...
    def decide(self, run_id: uuid.UUID, decision: Decision) -> Decision: ...


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, RunRecord] = {}

    def save(self, state: IncidentState) -> RunRecord:
        record = RunRecord(id=uuid.uuid4(), created_at=datetime.now(UTC), state=state)
        self._runs[record.id] = record
        return record

    def get(self, run_id: uuid.UUID) -> RunRecord | None:
        return self._runs.get(run_id)

    def list(self, limit: int = 50) -> list[RunRecord]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)[:limit]

    def decide(self, run_id: uuid.UUID, decision: Decision) -> Decision:
        record = self._runs[run_id]
        if any(d.action_id == decision.action_id for d in record.decisions):
            raise AlreadyDecided(decision.action_id)
        record.decisions.append(decision)
        return decision


class PostgresRunStore:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def save(self, state: IncidentState) -> RunRecord:
        run_id = uuid.uuid4()
        dump = state.model_dump(mode="json")
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO runs (id, status, failed_stage, error, alert, triage, investigation, plan)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING created_at",
                (
                    run_id, state.status, state.failed_stage, state.error, Jsonb(dump["alert"]),
                    *(Jsonb(dump[k]) if dump[k] is not None else None for k in ("triage", "investigation", "plan")),
                ),
            ).fetchone()
        return RunRecord(id=run_id, created_at=row["created_at"], state=state)

    def _record(self, conn, row: dict) -> RunRecord:
        decisions = conn.execute(
            "SELECT action_id, decision, decided_by, note, decided_at FROM action_decisions"
            " WHERE run_id = %s ORDER BY decided_at",
            (row["id"],),
        ).fetchall()
        state = IncidentState.model_validate(
            {k: row[k] for k in ("alert", "triage", "investigation", "plan", "status", "failed_stage", "error")}
        )
        return RunRecord(
            id=row["id"], created_at=row["created_at"], state=state, decisions=[Decision(**d) for d in decisions]
        )

    def get(self, run_id: uuid.UUID) -> RunRecord | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = %s", (run_id,)).fetchone()
            return self._record(conn, row) if row else None

    def list(self, limit: int = 50) -> list[RunRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
            return [self._record(conn, r) for r in rows]

    def decide(self, run_id: uuid.UUID, decision: Decision) -> Decision:
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO action_decisions (run_id, action_id, decision, decided_by, note, decided_at)"
                " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING action_id",
                (
                    run_id, decision.action_id, decision.decision,
                    decision.decided_by, decision.note, decision.decided_at,
                ),
            ).fetchone()
        if row is None:
            raise AlreadyDecided(decision.action_id)
        return decision
