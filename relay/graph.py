"""The LangGraph state machine.

    START -> triage -> investigate -> plan -> qa --pass------> END (awaiting_approval)
                          ^            ^      |  --escalate--> END (escalated: a human must look)
                          |            +------+  revise_plan     (at most QA_MAX_LOOPS times)
                          +-------------------+  reinvestigate
    any node raising ------------------------------------------> END (failed)

Each node reads typed contracts from the state and writes one, plus a trace span recording its input,
output, LLM attempts, tool calls, latency, tokens and cost. A node that fails stops the run: later
stages never see missing or invalid data.
"""

import logging
import operator
import time
from collections.abc import Callable, Iterator
from typing import Annotated, Any, Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from .agents.investigate import InvestigateAgent
from .agents.plan import PlanAgent
from .agents.qa import QAAgent
from .agents.triage import TriageAgent
from .schemas import Alert, Investigation, QAReport, ResponsePlan, TriageResult
from .tracing import Span

log = logging.getLogger(__name__)

Stage = Literal["triage", "investigate", "plan", "qa"]
Status = Literal["running", "awaiting_approval", "escalated", "failed", "resolved"]
LOOP_VERDICTS = ("revise_plan", "reinvestigate")


class IncidentState(BaseModel):
    alert: Alert
    triage: TriageResult | None = None
    investigation: Investigation | None = None
    plan: ResponsePlan | None = None
    qa: QAReport | None = None
    qa_loops: int = 0
    status: Status = "running"
    failed_stage: Stage | None = None
    error: str | None = None
    trace: Annotated[list[Span], operator.add] = []

    def qa_feedback(self) -> list[str] | None:
        return self.qa.feedback() if self.qa and self.qa.verdict in LOOP_VERDICTS else None


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def _node(stage: Stage, step: Callable[[IncidentState, Span], dict], inputs: Callable[[IncidentState], dict]):
    def node(state: IncidentState) -> dict:
        span = Span(agent=stage, loop=state.qa_loops, input={k: _dump(v) for k, v in inputs(state).items()})
        t0 = time.perf_counter()
        try:
            updates = step(state, span)
            span.output = {k: _dump(v) for k, v in updates.items() if k not in ("status", "qa_loops")}
        except Exception as exc:
            log.exception("%s failed", stage)
            span.status, span.error = "error", f"{type(exc).__name__}: {exc}"
            updates = {"status": "failed", "failed_stage": stage, "error": span.error}
        span.latency_ms = int((time.perf_counter() - t0) * 1000)
        return {**updates, "trace": [span]}

    return node


def build_graph(triage: TriageAgent, investigate: InvestigateAgent, plan: PlanAgent, qa: QAAgent):
    def triage_step(s: IncidentState, span: Span) -> dict:
        return {"triage": triage.run(s.alert, span=span)}

    def investigate_step(s: IncidentState, span: Span) -> dict:
        assert s.triage is not None
        return {"investigation": investigate.run(s.triage, s.qa_feedback(), span=span)}

    def plan_step(s: IncidentState, span: Span) -> dict:
        assert s.triage is not None and s.investigation is not None
        return {"plan": plan.run(s.triage, s.investigation, s.qa_feedback(), span=span)}

    def qa_step(s: IncidentState, span: Span) -> dict:
        assert s.triage is not None and s.investigation is not None and s.plan is not None
        report = qa.run(s.alert.injection_flags, s.triage, s.investigation, s.plan, s.qa_loops, span=span)
        if report.verdict in LOOP_VERDICTS:
            return {"qa": report, "qa_loops": s.qa_loops + 1}
        return {"qa": report, "status": "awaiting_approval" if report.verdict == "pass" else "escalated"}

    def after(nxt: str) -> Callable[[IncidentState], str]:
        return lambda s: END if s.status == "failed" else nxt

    def after_qa(s: IncidentState) -> str:
        if s.status == "failed" or s.qa is None:
            return END
        return {"revise_plan": "plan", "reinvestigate": "investigate"}.get(s.qa.verdict, END)

    graph = StateGraph(IncidentState)
    graph.add_node("triage", _node("triage", triage_step, lambda s: {"alert": s.alert}))
    graph.add_node(
        "investigate",
        _node("investigate", investigate_step, lambda s: {"triage": s.triage, "feedback": s.qa_feedback()}),
    )
    graph.add_node(
        "plan",
        _node(
            "plan",
            plan_step,
            lambda s: {
                "triage": s.triage,
                "investigation": s.investigation.model_dump(mode="json", exclude={"evidence"})
                if s.investigation
                else None,
                "feedback": s.qa_feedback(),
            },
        ),
    )
    graph.add_node("qa", _node("qa", qa_step, lambda s: {"plan": s.plan, "loop": s.qa_loops}))
    graph.add_edge(START, "triage")
    graph.add_conditional_edges("triage", after("investigate"), ["investigate", END])
    graph.add_conditional_edges("investigate", after("plan"), ["plan", END])
    graph.add_conditional_edges("plan", after("qa"), ["qa", END])
    graph.add_conditional_edges("qa", after_qa, ["plan", "investigate", END])
    return graph.compile()


class Pipeline:
    def __init__(self, triage: TriageAgent, investigate: InvestigateAgent, plan: PlanAgent, qa: QAAgent):
        self._graph = build_graph(triage, investigate, plan, qa)

    def stream(self, alert: Alert) -> Iterator[IncidentState]:
        """Yields the state after every node, so callers can show progress as it happens."""
        for values in self._graph.stream(IncidentState(alert=alert), stream_mode="values"):
            yield IncidentState.model_validate(values)

    def run(self, alert: Alert) -> IncidentState:
        state = IncidentState(alert=alert)
        for state in self.stream(alert):  # noqa: B007 - we want the last one
            pass
        return state

    @classmethod
    def from_settings(cls, settings, ops) -> "Pipeline":
        from .llm import ModelRouter
        from .tools.docpilot import DocPilotClient

        router, docpilot = ModelRouter(settings), DocPilotClient.from_settings(settings)
        retries = settings.llm_max_retries
        return cls(
            TriageAgent(router.for_agent("triage"), docpilot, retries),
            InvestigateAgent(router.for_agent("investigate"), docpilot, ops, retries),
            PlanAgent(router.for_agent("plan"), retries),
            QAAgent(router.for_agent("qa"), retries, settings.qa_max_loops, settings.qa_min_confidence),
        )
