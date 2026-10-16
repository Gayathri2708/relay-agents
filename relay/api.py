"""HTTP API. Submitting an alert runs the pipeline; humans then approve or reject proposed actions.

Recording a decision is all Relay does with it. There is no code path that executes an action.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .graph import Pipeline
from .schemas import Alert
from .store import AlreadyDecided, Decision, RunRecord, RunStore


class AlertIn(BaseModel):
    alert: str = Field(min_length=1, max_length=20_000, description="Raw alert text; redacted before use")
    received_at: datetime | None = None


class DecisionIn(BaseModel):
    decision: Literal["approved", "rejected"]
    decided_by: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


class DecisionOut(Decision):
    executed: Literal[False] = False
    message: str = "Decision recorded. Relay never executes actions; carry out approved actions yourself."


class RunSummary(BaseModel):
    id: uuid.UUID
    created_at: datetime
    status: str
    service: str | None
    severity: str | None


def create_app(pipeline: Pipeline | None = None, store: RunStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = None
        if pipeline is None or store is None:
            from .config import get_settings
            from .db import apply_schema, make_pool
            from .store import PostgresRunStore
            from .tools.ops import PostgresOps

            settings = get_settings()
            pool = make_pool(settings.database_url)
            apply_schema(pool)
            app.state.pipeline = pipeline or Pipeline.from_settings(settings, PostgresOps(pool))
            app.state.store = store or PostgresRunStore(pool)
        else:
            app.state.pipeline, app.state.store = pipeline, store
        yield
        if pool is not None:
            pool.close()

    app = FastAPI(title="Relay", version="0.1.0", lifespan=lifespan)

    def get_run(run_id: uuid.UUID) -> RunRecord:
        record = app.state.store.get(run_id)
        if record is None:
            raise HTTPException(404, "Run not found")
        return record

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/incidents", status_code=201)
    def submit(body: AlertIn) -> RunRecord:
        alert = Alert.from_raw(body.alert, body.received_at)
        return app.state.store.save(app.state.pipeline.run(alert))

    @app.get("/incidents")
    def list_runs(limit: int = 50) -> list[RunSummary]:
        return [
            RunSummary(
                id=r.id,
                created_at=r.created_at,
                status=r.state.status,
                service=r.state.triage.service if r.state.triage else None,
                severity=r.state.triage.severity if r.state.triage else None,
            )
            for r in app.state.store.list(min(limit, 200))
        ]

    @app.get("/incidents/{run_id}")
    def get_incident(run_id: uuid.UUID) -> RunRecord:
        return get_run(run_id)

    @app.post("/incidents/{run_id}/actions/{action_id}/decision", status_code=201)
    def decide(run_id: uuid.UUID, action_id: str, body: DecisionIn) -> DecisionOut:
        record = get_run(run_id)
        plan = record.state.plan
        if record.state.status != "awaiting_approval" or plan is None:
            raise HTTPException(409, f"Run is {record.state.status}; there is nothing to approve")
        if action_id not in {a.id for a in plan.actions}:
            raise HTTPException(404, f"Action {action_id} is not in this plan")
        try:
            decision = app.state.store.decide(run_id, Decision(action_id=action_id, **body.model_dump()))
        except AlreadyDecided:
            raise HTTPException(409, f"Action {action_id} already has a decision") from None
        return DecisionOut(**decision.model_dump())

    return app


app = create_app()
