import copy

from .conftest import (
    GRAPH_PLAN,
    INVESTIGATION_REPLY,
    QA_PASS,
    TRIAGE_REPLY,
    Agents,
    FakeDocPilot,
    happy_agents,
)

INVENTED = copy.deepcopy(GRAPH_PLAN)
INVENTED["actions"][0]["details"] = "Run kubectl rollout undo deployment/payments-api to restore the old release."


def test_full_run_passes_qa_and_awaits_approval(alert):
    agents = happy_agents()
    state = agents.pipeline().run(alert)
    assert state.status == "awaiting_approval" and state.error is None
    assert state.qa.verdict == "pass" and state.qa_loops == 0
    assert [s.agent for s in state.trace] == ["triage", "investigate", "plan", "qa"]
    assert "dp_FAKEdemoKEY7f3a9c2e1b4d" not in agents.plan.sent_text() + agents.triage.sent_text()


def test_qa_rule_failure_sends_plan_back_with_feedback(alert):
    agents = Agents([TRIAGE_REPLY], [INVESTIGATION_REPLY], [INVENTED, GRAPH_PLAN], [QA_PASS])
    state = agents.pipeline().run(alert)
    assert state.status == "awaiting_approval"
    assert state.qa_loops == 1
    assert [s.agent for s in state.trace] == ["triage", "investigate", "plan", "qa", "plan", "qa"]
    second_plan_prompt = agents.plan.calls[1][1]["content"]
    assert "QA gate rejected" in second_plan_prompt and "kubectl" in second_plan_prompt
    assert [s.loop for s in state.trace] == [0, 0, 0, 0, 1, 1]


def test_reviewer_can_send_back_to_investigate(alert):
    reinvestigate = {**QA_PASS, "route": "reinvestigate", "confidence": 0.3,
                     "actions": [{**a, "grounded": False, "issue": "no evidence the deploy touched wallet code"}
                                 for a in QA_PASS["actions"]]}
    agents = Agents([TRIAGE_REPLY], [INVESTIGATION_REPLY, INVESTIGATION_REPLY], [GRAPH_PLAN, GRAPH_PLAN],
                    [reinvestigate, QA_PASS])
    docpilot = FakeDocPilot()
    state = agents.pipeline(docpilot).run(alert)
    assert [s.agent for s in state.trace] == ["triage", "investigate", "plan", "qa", "investigate", "plan", "qa"]
    assert state.status == "awaiting_approval"
    assert any("no evidence the deploy touched wallet code" in q for q in docpilot.queries)  # feedback searched


def test_loops_stop_after_two_revisions_and_escalate_with_blocked_actions(alert):
    agents = Agents([TRIAGE_REPLY], [INVESTIGATION_REPLY], [INVENTED] * 3, [QA_PASS])
    state = agents.pipeline().run(alert)
    assert state.status == "escalated"
    assert state.qa_loops == 2
    assert [s.agent for s in state.trace].count("plan") == 3
    assert state.qa.blocked_actions == ["A1"]
    assert any("still failing" in r for r in state.qa.escalation_reasons)


def test_low_confidence_escalates_to_a_human(alert):
    agents = Agents([TRIAGE_REPLY], [INVESTIGATION_REPLY], [GRAPH_PLAN], [{**QA_PASS, "confidence": 0.4}])
    state = agents.pipeline().run(alert)
    assert state.status == "escalated"
    assert state.qa.blocked_actions == []  # nothing failed QA; a human just needs to look
    assert any("confidence" in r for r in state.qa.escalation_reasons)


def test_invalid_triage_stops_the_run(alert):
    bad = {**TRIAGE_REPLY, "severity": "urgent"}
    agents = Agents([bad, bad])
    state = agents.pipeline().run(alert)
    assert (state.status, state.failed_stage) == ("failed", "triage")
    assert state.triage is None and state.plan is None
    assert [(s.agent, s.status) for s in state.trace] == [("triage", "error")]
    assert len(state.trace[0].llm_calls) == 2  # both rejected attempts are traced


def test_tool_outage_stops_the_run(alert):
    agents = Agents([TRIAGE_REPLY])
    state = agents.pipeline(FakeDocPilot(fail_search=True)).run(alert)
    assert (state.status, state.failed_stage) == ("failed", "triage")
    assert "DocPilotError" in state.error
    assert agents.triage.calls == []


def test_stream_yields_progress_after_each_node(alert):
    states = list(happy_agents().pipeline().stream(alert))
    stages = [len(s.trace) for s in states]
    assert stages == sorted(stages) and stages[-1] == 4
    assert states[1].triage is not None and states[1].plan is None  # visible mid-run
