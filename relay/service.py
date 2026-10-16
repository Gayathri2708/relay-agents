"""Orchestration around the pipeline: background runs, human decisions, resolution, postmortems.

The rules for humans live here, so the API and any other front end share them:
  - only runs awaiting approval or escalated take decisions; decisions are final,
  - an action QA blocked cannot be approved as-is; a human may edit it (the edit must pass the same
    grounding rules) or reject it,
  - approving runs the mock executor, which only simulates; nothing is ever executed,
  - a postmortem can be drafted only after a human marks the incident resolved.
"""

import json
import logging
import time
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field

from .agents.postmortem import PostmortemAgent
from .executor import MockExecutor
from .graph import IncidentState, Pipeline
from .grounding import check_action
from .schemas import Alert, ProposedAction
from .store import Decision, Postmortem, Resolution, RunRecord, RunStore
from .tracing import Span

log = logging.getLogger(__name__)


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Invalid(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class ActionEdits(BaseModel):
    title: str | None = Field(default=None, min_length=5, max_length=200)
    details: str | None = Field(default=None, min_length=10)
    target: str | None = None
    status_update_draft: str | None = None


class DecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    decided_by: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=2000)
    edits: ActionEdits | None = Field(default=None, description="Approve a modified version of the action")


class InlineExecutor(Executor):
    """Runs submitted work immediately. For tests and the CLI."""

    def submit(self, fn, /, *args, **kwargs):
        fn(*args, **kwargs)


DECIDABLE = ("awaiting_approval", "escalated")


class RunService:
    def __init__(
        self,
        pipeline: Pipeline,
        store: RunStore,
        postmortem: PostmortemAgent | None = None,
        executor: Executor | None = None,
    ):
        self.pipeline, self.store, self.postmortem = pipeline, store, postmortem
        self._executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="relay-run")
        self._mock = MockExecutor()

    # --- runs -----------------------------------------------------------------------------------

    def submit(self, alert: Alert) -> RunRecord:
        record = self.store.create(IncidentState(alert=alert))
        self._executor.submit(self._run, record.id, alert)
        return record

    def _run(self, run_id: uuid.UUID, alert: Alert) -> None:
        state = IncidentState(alert=alert)
        try:
            for state in self.pipeline.stream(alert):
                self.store.update(run_id, state)
        except Exception as exc:  # the graph catches agent errors; this is for anything else
            log.exception("run %s crashed", run_id)
            state.status, state.error = "failed", f"{type(exc).__name__}: {exc}"
            self.store.update(run_id, state)

    def get(self, run_id: uuid.UUID) -> RunRecord:
        if (record := self.store.get(run_id)) is None:
            raise NotFound("run not found")
        return record

    # --- human decisions ------------------------------------------------------------------------

    def decide(self, run_id: uuid.UUID, action_id: str, req: DecisionRequest) -> Decision:
        record = self.get(run_id)
        state = record.state
        if state.status not in DECIDABLE or state.plan is None:
            raise Conflict(f"run is {state.status}; there is nothing to decide")
        action = next((a for a in state.plan.actions if a.id == action_id), None)
        if action is None:
            raise NotFound(f"action {action_id} is not in this plan")

        edited = None
        if req.edits and req.decision == "approved":
            changes = req.edits.model_dump(exclude_none=True)
            # Re-validate from scratch: the contract re-applies the risk flags, the rules re-check grounding.
            edited = ProposedAction.model_validate({**action.model_dump(), **changes})
            assert state.triage is not None and state.investigation is not None
            if problems := check_action(edited, action_id, state.triage, state.investigation):
                raise Invalid(problems)
        elif req.decision == "approved" and state.qa and action_id in state.qa.blocked_actions:
            raise Conflict(f"{action_id} failed QA; edit it so it passes, or reject it")

        simulation = self._mock.simulate(edited or action, req.decision) if req.decision == "approved" else None
        decision = Decision(
            action_id=action_id,
            decision=req.decision,
            decided_by=req.decided_by,
            note=req.note,
            edited_action=edited,
            simulation=simulation,
        )
        return self.store.decide(run_id, decision)

    def resolve(self, run_id: uuid.UUID, resolution: Resolution) -> RunRecord:
        record = self.get(run_id)
        if record.state.status not in DECIDABLE:
            raise Conflict(f"run is {record.state.status}; only open incidents can be resolved")
        record.state.status = "resolved"
        self.store.resolve(run_id, record.state, resolution)
        return self.get(run_id)

    # --- postmortem -----------------------------------------------------------------------------

    def draft_postmortem(self, run_id: uuid.UUID) -> Postmortem:
        record = self.get(run_id)
        if record.state.status != "resolved":
            raise Conflict("resolve the incident before drafting a postmortem")
        if self.postmortem is None:
            raise Conflict("postmortem drafting is not configured")
        incident = {
            "alert": record.state.alert.model_dump(mode="json"),
            "triage": record.state.triage.model_dump(mode="json") if record.state.triage else None,
            "investigation": (
                record.state.investigation.model_dump(mode="json") if record.state.investigation else None
            ),
            "plan": record.state.plan.model_dump(mode="json") if record.state.plan else None,
            "qa": record.state.qa.model_dump(mode="json") if record.state.qa else None,
            "decisions": [d.model_dump(mode="json") for d in record.decisions],
            "resolution": record.resolution.model_dump(mode="json") if record.resolution else None,
        }
        span = Span(agent="postmortem", input={"run_id": str(run_id)})
        t0 = time.perf_counter()
        draft = self.postmortem.run(json.dumps(incident, indent=1), span=span)
        span.latency_ms = int((time.perf_counter() - t0) * 1000)
        span.output = {"title": draft.title}
        postmortem = Postmortem(draft=draft, markdown=draft.markdown())
        self.store.save_postmortem(run_id, postmortem, span)
        return postmortem
