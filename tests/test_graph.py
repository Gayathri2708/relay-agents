import copy

from relay.agents.investigate import InvestigateAgent
from relay.agents.plan import PlanAgent
from relay.agents.triage import TriageAgent
from relay.graph import Pipeline

from .conftest import INVESTIGATION_REPLY, PLAN_REPLY, TRIAGE_REPLY, FakeDocPilot, FakeLLM, FakeOps

POLICY = "incident-severity-and-communication-policy.md"
# The fake search does not surface PAY-5; COMM-3 (owning teams and their pagers) is retrieved and fits.
GRAPH_PLAN = copy.deepcopy(PLAN_REPLY)
GRAPH_PLAN["actions"][1]["citations"] = [{"runbook": POLICY, "section": "COMM-3"}]


def pipeline(llm, docpilot=None):
    docpilot = docpilot or FakeDocPilot()
    return Pipeline(TriageAgent(llm, docpilot, 1), InvestigateAgent(llm, docpilot, FakeOps(), 1), PlanAgent(llm, 1))


def test_full_run_ends_awaiting_approval(alert):
    llm = FakeLLM(TRIAGE_REPLY, INVESTIGATION_REPLY, GRAPH_PLAN)
    state = pipeline(llm).run(alert)
    assert state.status == "awaiting_approval" and state.error is None
    assert state.triage.severity == "P1"
    assert state.investigation.findings and state.plan.actions
    assert state.plan.actions[0].requires_approval
    assert "dp_FAKEdemoKEY7f3a9c2e1b4d" not in llm.sent_text()


def test_invalid_triage_stops_the_run(alert):
    bad = {**TRIAGE_REPLY, "severity": "urgent"}
    llm = FakeLLM(bad, bad)
    state = pipeline(llm).run(alert)
    assert (state.status, state.failed_stage) == ("failed", "triage")
    assert "AgentOutputError" in state.error
    assert state.triage is None and state.investigation is None and state.plan is None
    assert len(llm.calls) == 2  # investigate and plan never ran


def test_tool_outage_stops_the_run_at_that_stage(alert):
    llm = FakeLLM(TRIAGE_REPLY)
    state = pipeline(llm, FakeDocPilot(fail_search=True)).run(alert)
    assert (state.status, state.failed_stage) == ("failed", "triage")
    assert "DocPilotError" in state.error
    assert llm.calls == []  # the policy lookup failed before any LLM call


def test_invalid_plan_leaves_no_plan(alert):
    bad_plan = {"summary": "nothing useful here", "actions": []}
    llm = FakeLLM(TRIAGE_REPLY, INVESTIGATION_REPLY, bad_plan, bad_plan)
    state = pipeline(llm).run(alert)
    assert (state.status, state.failed_stage) == ("failed", "plan")
    assert state.investigation is not None and state.plan is None
