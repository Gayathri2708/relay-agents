"""HTTP API and dashboard. Submitting an alert starts a run in the background; the dashboard polls it.

Humans approve, edit or reject proposed actions. Approval runs the mock executor, which only simulates.
There is no code path that executes an action.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .schemas import Alert
from .service import Conflict, DecisionRequest, Invalid, NotFound, RunService
from .store import AlreadyDecided, Decision, Postmortem, Resolution, RunRecord
from .tracing import AgentUsage

STATIC = Path(__file__).with_name("static")
SAMPLES = Path(__file__).parent.parent / "seed" / "alerts"


class AlertIn(BaseModel):
    alert: str = Field(min_length=1, max_length=20_000, description="Raw alert text; redacted before use")
    received_at: datetime | None = None


class RunSummary(BaseModel):
    id: uuid.UUID
    created_at: datetime
    status: str
    service: str | None
    severity: str | None
    cost_usd: float


class Sample(BaseModel):
    name: str
    text: str


def create_app(service: RunService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = None
        if service is None:
            from .agents.postmortem import PostmortemAgent
            from .config import get_settings
            from .db import apply_schema, make_pool
            from .graph import Pipeline
            from .llm import ModelRouter
            from .store import PostgresRunStore
            from .tools.ops import PostgresOps

            settings = get_settings()
            pool = make_pool(settings.database_url)
            apply_schema(pool)
            postmortem = PostmortemAgent(ModelRouter(settings).for_agent("postmortem"), settings.llm_max_retries)
            app.state.service = RunService(
                Pipeline.from_settings(settings, PostgresOps(pool)), PostgresRunStore(pool), postmortem
            )
        else:
            app.state.service = service
        yield
        if pool is not None:
            pool.close()

    app = FastAPI(title="Relay", version="0.2.0", lifespan=lifespan)

    def svc() -> RunService:
        return app.state.service

    def guard(fn, *args):
        try:
            return fn(*args)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except AlreadyDecided as exc:
            raise HTTPException(409, f"action {exc} already has a decision; decisions are final") from None
        except Invalid as exc:
            detail = {"message": "the edited action fails grounding checks", "problems": exc.problems}
            raise HTTPException(422, detail) from None

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/samples")
    def samples() -> list[Sample]:
        return [Sample(name=p.stem, text=p.read_text()) for p in sorted(SAMPLES.glob("*.txt"))]

    @app.post("/incidents", status_code=202)
    def submit(body: AlertIn) -> RunRecord:
        return svc().submit(Alert.from_raw(body.alert, body.received_at))

    @app.get("/incidents")
    def list_runs(limit: int = 50) -> list[RunSummary]:
        return [
            RunSummary(
                id=r.id,
                created_at=r.created_at,
                status=r.state.status,
                service=r.state.triage.service if r.state.triage else None,
                severity=r.state.triage.severity if r.state.triage else None,
                cost_usd=round(sum(u.cost_usd for u in r.usage), 6),
            )
            for r in svc().store.recent(min(limit, 200))
        ]

    @app.get("/incidents/{run_id}")
    def get_incident(run_id: uuid.UUID) -> RunRecord:
        return guard(svc().get, run_id)

    @app.post("/incidents/{run_id}/actions/{action_id}/decision", status_code=201)
    def decide(run_id: uuid.UUID, action_id: str, body: DecisionRequest) -> Decision:
        return guard(svc().decide, run_id, action_id, body)

    @app.post("/incidents/{run_id}/resolve")
    def resolve(run_id: uuid.UUID, body: Resolution) -> RunRecord:
        return guard(svc().resolve, run_id, body)

    @app.post("/incidents/{run_id}/postmortem", status_code=201)
    def postmortem(run_id: uuid.UUID) -> Postmortem:
        return guard(svc().draft_postmortem, run_id)

    @app.get("/usage")
    def usage() -> list[AgentUsage]:
        """Tokens, cost and latency per agent across all runs."""
        return svc().store.usage()

    return app


app = create_app()
