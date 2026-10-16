"""The LangGraph state machine: triage -> investigate -> plan.

Each node reads typed contracts from the state and writes one. If a node fails (an agent could not
produce valid output, or a required tool is down), the run stops there with status "failed": later
stages never see missing or invalid data.

    START -> triage -> investigate -> plan -> END (awaiting_approval)
               |            |           |
               +------------+-----------+--> END (failed)
"""

import logging
from collections.abc import Callable
from typing import Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from .agents.investigate import InvestigateAgent
from .agents.plan import PlanAgent
from .agents.triage import TriageAgent
from .schemas import Alert, Investigation, ResponsePlan, TriageResult

log = logging.getLogger(__name__)

Stage = Literal["triage", "investigate", "plan"]


class IncidentState(BaseModel):
    alert: Alert
    triage: TriageResult | None = None
    investigation: Investigation | None = None
    plan: ResponsePlan | None = None
    status: Literal["running", "awaiting_approval", "failed"] = "running"
    failed_stage: Stage | None = None
    error: str | None = None


def _guard(stage: Stage, step: Callable[[IncidentState], dict]) -> Callable[[IncidentState], dict]:
    def node(state: IncidentState) -> dict:
        try:
            return step(state)
        except Exception as exc:
            log.exception("%s failed", stage)
            return {"status": "failed", "failed_stage": stage, "error": f"{type(exc).__name__}: {exc}"}

    return node


def _next_unless_failed(nxt: str) -> Callable[[IncidentState], str]:
    return lambda state: END if state.status == "failed" else nxt


def build_graph(triage: TriageAgent, investigate: InvestigateAgent, plan: PlanAgent):
    def triage_step(s: IncidentState) -> dict:
        return {"triage": triage.run(s.alert)}

    def investigate_step(s: IncidentState) -> dict:
        assert s.triage is not None
        return {"investigation": investigate.run(s.triage)}

    def plan_step(s: IncidentState) -> dict:
        assert s.triage is not None and s.investigation is not None
        return {"plan": plan.run(s.triage, s.investigation), "status": "awaiting_approval"}

    graph = StateGraph(IncidentState)
    graph.add_node("triage", _guard("triage", triage_step))
    graph.add_node("investigate", _guard("investigate", investigate_step))
    graph.add_node("plan", _guard("plan", plan_step))
    graph.add_edge(START, "triage")
    graph.add_conditional_edges("triage", _next_unless_failed("investigate"), ["investigate", END])
    graph.add_conditional_edges("investigate", _next_unless_failed("plan"), ["plan", END])
    graph.add_edge("plan", END)
    return graph.compile()


class Pipeline:
    def __init__(self, triage: TriageAgent, investigate: InvestigateAgent, plan: PlanAgent):
        self._graph = build_graph(triage, investigate, plan)

    def run(self, alert: Alert) -> IncidentState:
        return IncidentState.model_validate(self._graph.invoke(IncidentState(alert=alert)))

    @classmethod
    def from_settings(cls, settings, ops) -> "Pipeline":
        from .llm import OpenAICompatibleLLM
        from .tools.docpilot import DocPilotClient

        llm, docpilot = OpenAICompatibleLLM(settings), DocPilotClient.from_settings(settings)
        retries = settings.llm_max_retries
        return cls(
            TriageAgent(llm, docpilot, retries),
            InvestigateAgent(llm, docpilot, ops, retries),
            PlanAgent(llm, retries),
        )
