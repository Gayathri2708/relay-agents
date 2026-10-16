"""Read-only lookups over operational data: deploys, metric and log snapshots, past incidents."""

import re
from datetime import datetime, timedelta
from typing import Protocol

from psycopg_pool import ConnectionPool
from pydantic import BaseModel


class Deploy(BaseModel):
    service: str
    version: str
    previous_version: str
    author: str
    commit_sha: str
    summary: str
    has_migration: bool
    finished_at: datetime


class MetricSnapshot(BaseModel):
    service: str
    captured_at: datetime
    metrics: dict[str, float]


class LogLine(BaseModel):
    service: str
    captured_at: datetime
    level: str
    message: str


class PastIncident(BaseModel):
    id: str
    service: str
    severity: str
    title: str
    summary: str
    root_cause: str
    resolution: str
    started_at: datetime


class OpsData(Protocol):
    def recent_deploys(self, at: datetime, lookback: timedelta = ...) -> list[Deploy]: ...
    def metrics(self, service: str, at: datetime) -> list[MetricSnapshot]: ...
    def logs(self, service: str, at: datetime, limit: int = ...) -> list[LogLine]: ...
    def similar_incidents(self, service: str, text: str, limit: int = ...) -> list[PastIncident]: ...


_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = {"the", "and", "for", "with", "from", "are", "was", "has", "have", "not", "this", "that", "than"}


def _or_query(text: str) -> str:
    words = sorted({w for w in _WORD.findall(text.lower()) if w not in _STOP})
    return " | ".join(words[:40])


class PostgresOps:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def _rows(self, sql: str, params: dict) -> list[dict]:
        with self._pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def recent_deploys(self, at: datetime, lookback: timedelta = timedelta(hours=6)) -> list[Deploy]:
        """Every service's deploys from `lookback` before `at` until 15 minutes after it."""
        rows = self._rows(
            "SELECT * FROM deploys WHERE finished_at BETWEEN %(lo)s AND %(hi)s ORDER BY finished_at DESC",
            {"lo": at - lookback, "hi": at + timedelta(minutes=15)},
        )
        return [Deploy.model_validate(r) for r in rows]

    def metrics(self, service: str, at: datetime) -> list[MetricSnapshot]:
        rows = self._rows(
            "SELECT service, captured_at, metrics FROM metric_snapshots"
            " WHERE service = %(s)s AND captured_at BETWEEN %(lo)s AND %(hi)s ORDER BY captured_at",
            {"s": service, "lo": at - timedelta(minutes=45), "hi": at + timedelta(minutes=20)},
        )
        return [MetricSnapshot.model_validate(r) for r in rows]

    def logs(self, service: str, at: datetime, limit: int = 15) -> list[LogLine]:
        rows = self._rows(
            "SELECT service, captured_at, level, message FROM log_snapshots"
            " WHERE service = %(s)s AND captured_at BETWEEN %(lo)s AND %(hi)s"
            " AND level IN ('ERROR', 'WARN') ORDER BY captured_at LIMIT %(n)s",
            {"s": service, "lo": at - timedelta(minutes=15), "hi": at + timedelta(minutes=20), "n": limit},
        )
        return [LogLine.model_validate(r) for r in rows]

    def similar_incidents(self, service: str, text: str, limit: int = 3) -> list[PastIncident]:
        query = _or_query(text)
        if not query:
            return []
        rows = self._rows(
            "SELECT * FROM past_incidents WHERE search @@ to_tsquery('english', %(q)s)"
            " ORDER BY ts_rank(search, to_tsquery('english', %(q)s))"
            " * CASE WHEN service = %(s)s THEN 2 ELSE 1 END DESC LIMIT %(n)s",
            {"q": query, "s": service, "n": limit},
        )
        return [PastIncident.model_validate(r) for r in rows]
