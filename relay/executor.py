"""Mock executor: shows what an approved action *would* do. It never does it.

This module deliberately has no way to affect anything: no subprocess, sockets, HTTP clients or
credentials (a test enforces this). It only runs for an action a human approved, and it returns
a dry-run transcript that is stored with the decision.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .schemas import ProposedAction


class NotApproved(Exception):
    pass


class Simulation(BaseModel):
    action_id: str
    simulated: Literal[True] = True
    executed: Literal[False] = False
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    transcript: list[str]


_STEPS: dict[str, list[str]] = {
    "rollback": [
        "freeze deploys to {target} (deploy-freeze label)",
        "argocd app rollback {target} <previous healthy revision>",
        "watch {target} error rate for 10 minutes",
    ],
    "scale_up": ["scale {target}: {details}", "confirm database connection headroom", "watch CPU and p99 latency"],
    "feature_flag": ["change flag for {target}: {details}", "watch error rate and latency for 10 minutes"],
    "failover": [
        "check replica lag < 10s on the candidate",
        "patronictl failover --candidate <replica>",
        "verify writes on the new primary; confirm clients reconnect",
    ],
    "restart": ["rolling restart of {target}, one pod at a time", "watch readiness and error rate"],
    "config_change": ["apply config change to {target}: {details}", "verify the change and watch error rates"],
    "terminate_query": ["identify the session in pg_stat_activity", "pg_terminate_backend(<pid>)", "watch lag"],
    "page_team": ["page {target} via PagerDuty with the incident summary"],
    "status_update": ["publish to status page: \"{draft}\""],
    "investigate_further": ["{details}"],
}


class MockExecutor:
    def simulate(self, action: ProposedAction, decision: str) -> Simulation:
        if decision != "approved":
            raise NotApproved(f"{action.id} is {decision}; only approved actions are simulated")
        values = {
            "target": action.target or "the affected service",
            "details": action.details,
            "draft": action.status_update_draft or "",
        }
        steps = [f"[dry run] {step.format(**values)}" for step in _STEPS[action.kind]]
        steps.append("[dry run] nothing was executed: Relay has no credentials or network access to production")
        return Simulation(action_id=action.id, transcript=steps)
