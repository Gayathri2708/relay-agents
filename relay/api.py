"""HTTP API and dashboard. Submitting an alert starts a run in the background; the dashboard polls it.

Humans approve, edit or reject proposed actions. Approval runs the mock executor, which only simulates.
There is no code path that executes an action.
"""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from openai import OpenAIError
from pydantic import BaseModel, Field

from .auth import KeyRing, Principal, requires
from .llm import AgentOutputError, DailyLimitReached
from .schemas import Alert
from .service import Conflict, DecisionRequest, Invalid, NotFound, RunService
from .store import AlreadyDecided, Decision, Postmortem, Resolution, RunRecord, RunSummary
from .tracing import AgentUsage

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
SAMPLES = Path(__file__).parent.parent / "seed" / "alerts"


class AlertIn(BaseModel):
    # 8,000 characters is about 2,300 tokens: room for the policy and schema within small per-minute limits.
    alert: str = Field(min_length=1, max_length=8_000, description="Raw alert text; redacted before use")
    received_at: datetime | None = None


class Sample(BaseModel):
    name: str
    text: str


def create_app(service: RunService | None = None, api_keys: dict[str, dict[str, str]] | None = None) -> FastAPI:
    """Pass `service` (and optionally `api_keys`) in tests; otherwise both come from settings at startup."""

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
            router = ModelRouter(settings)  # one router: each model's pacing covers every agent using it
            postmortem = PostmortemAgent(router.for_agent("postmortem"), settings.llm_max_retries)
            app.state.service = RunService(
                Pipeline.from_settings(settings, PostgresOps(pool), router), PostgresRunStore(pool), postmortem
            )
            app.state.keys = KeyRing(settings.relay_api_keys if api_keys is None else api_keys)
        else:
            app.state.service = service
            app.state.keys = KeyRing(api_keys or {})
        if interrupted := app.state.service.store.fail_interrupted():
            log.warning("marked %d interrupted run(s) as failed", interrupted)
        yield
        if pool is not None:
            pool.close()

    app = FastAPI(title="Relay", version="0.2.0", lifespan=lifespan)

    def svc() -> RunService:
        return app.state.service

    reporter = Depends(requires(lambda: app.state.keys, "reporter"))
    approver = Depends(requires(lambda: app.state.keys, "approver"))

    def as_principal(principal: Principal, claimed: str) -> str:
        """With keys on, the key decides who acted; the free-text name is only used in open dev mode."""
        if principal.authenticated:
            return principal.name
        if not claimed.strip():
            raise HTTPException(422, "Say who you are (decided_by / resolved_by), or configure RELAY_API_KEYS.")
        return claimed.strip()

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
        except AgentOutputError as exc:
            raise HTTPException(502, f"the model did not produce valid output: {exc}") from None
        except (DailyLimitReached, OpenAIError) as exc:
            raise HTTPException(503, f"the LLM provider is unavailable: {exc}") from None

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/samples", dependencies=[reporter])
    def samples() -> list[Sample]:
        return [Sample(name=p.stem, text=p.read_text()) for p in sorted(SAMPLES.glob("*.txt"))]

    @app.get("/me")
    def me(principal: Principal = reporter) -> dict:
        return {"name": principal.name, "role": principal.role, "auth": principal.authenticated}

    @app.post("/incidents", status_code=202, dependencies=[reporter])
    def submit(body: AlertIn) -> RunRecord:
        return svc().submit(Alert.from_raw(body.alert, body.received_at))

    @app.get("/incidents", dependencies=[reporter])
    def list_runs(limit: int = 50) -> list[RunSummary]:
        return svc().store.summaries(max(1, min(limit, 200)))

    @app.get("/incidents/{run_id}", dependencies=[reporter])
    def get_incident(run_id: uuid.UUID) -> RunRecord:
        return guard(svc().get, run_id)

    @app.post("/incidents/{run_id}/actions/{action_id}/decision", status_code=201)
    def decide(run_id: uuid.UUID, action_id: str, body: DecisionRequest, principal: Principal = approver) -> Decision:
        body = body.model_copy(update={"decided_by": as_principal(principal, body.decided_by)})
        return guard(svc().decide, run_id, action_id, body)

    @app.post("/incidents/{run_id}/resolve")
    def resolve(run_id: uuid.UUID, body: Resolution, principal: Principal = approver) -> RunRecord:
        body = body.model_copy(update={"resolved_by": as_principal(principal, body.resolved_by)})
        return guard(svc().resolve, run_id, body)

    @app.post("/incidents/{run_id}/postmortem", status_code=201, dependencies=[approver])
    def postmortem(run_id: uuid.UUID) -> Postmortem:
        return guard(svc().draft_postmortem, run_id)

    @app.get("/usage", dependencies=[reporter])
    def usage() -> list[AgentUsage]:
        """Tokens, cost and latency per agent across all runs."""
        return svc().store.usage()

    return app


app = create_app()
