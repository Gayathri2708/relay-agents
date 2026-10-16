import copy

import pytest

from relay.agents.qa import QAAgent
from relay.grounding import check_action
from relay.schemas import Evidence, Investigation, ResponsePlan

from .conftest import PLAN_REPLY, QA_PASS, FakeLLM, runbook_evidence


@pytest.fixture
def investigation(investigation) -> Investigation:
    deploy = Evidence(id="E90", kind="deploy", source="payments-api v2.41.0",
                      content="payments-api deployed v2.41.0 (previous v2.40.3) 15 min before the incident.")
    return investigation.model_copy(update={"evidence": [*runbook_evidence(), deploy]})


def make_plan(**changes) -> ResponsePlan:
    reply = copy.deepcopy(PLAN_REPLY)
    for index, fields in changes.items():
        reply["actions"][int(index[1:])].update(fields)
    actions = [{"id": f"A{i}", **a} for i, a in enumerate(reply["actions"], 1)]
    return ResponsePlan(summary=reply["summary"], actions=actions)


def run_qa(triage, investigation, plan, *replies, loop=0, flags=()):
    llm = FakeLLM(*replies)
    report = QAAgent(llm, max_retries=1).run(list(flags), triage, investigation, plan, loop)
    return report, llm


def test_grounded_plan_passes(triage, investigation):
    report, llm = run_qa(triage, investigation, make_plan(), QA_PASS)
    assert report.verdict == "pass" and report.problems == []
    assert report.confidence == 0.85


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ("Run kubectl rollout undo deployment/payments-api.", "no retrieved runbook mentions kubectl"),
        ("Run argocd app delete payments-api and recreate it.", "does not appear in the runbooks"),
        ("Enable payments.wallet.safe_mode to stop the errors.", "payments.wallet.safe_mode"),
        ("Roll back payments-api to v2.39.9.", "v2.39.9"),
        ("Roll back payments-api, then run it at 40 replicas.", "replica count of 40"),
    ],
)
def test_invented_specifics_are_caught(triage, investigation, details, expected):
    report, llm = run_qa(triage, investigation, make_plan(a0={"details": details}))
    assert report.verdict == "revise_plan"
    assert any(expected in p.problem and p.action_id == "A1" for p in report.problems)
    assert llm.calls == []  # rule failures go straight back without paying for a review


def test_specifics_from_runbooks_are_allowed(triage, investigation):
    details = "Per DEP-2 run argocd app rollback payments-api v2.40.3 and watch the error rate."
    assert check_action(make_plan(a0={"details": details}).actions[0], "A1", triage, investigation) == []


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"target": "all services"}, "exactly one in-scope service"),
        ({"target": "auth-service"}, "exactly one in-scope service"),  # not payments-api or its deps
        ({"details": "Roll back all services to their previous release."}, "many services"),
    ],
)
def test_change_actions_must_stay_in_scope(triage, investigation, changes, expected):
    report, _ = run_qa(triage, investigation, make_plan(a0=changes))
    assert any(expected in p.problem for p in report.problems)


def test_rollback_needs_a_deploy_in_evidence(triage, investigation):
    no_deploys = investigation.model_copy(update={"evidence": runbook_evidence()})
    report, _ = run_qa(triage, no_deploys, make_plan())
    assert any("no deploy of it is in the evidence" in p.problem for p in report.problems)


def test_reviewer_rejection_sends_plan_back(triage, investigation):
    review = {**QA_PASS, "route": "revise_plan",
              "actions": [{"action_id": "A1", "grounded": False, "issue": "processor outage, not a bad deploy"},
                          *QA_PASS["actions"][1:]]}
    report, _ = run_qa(triage, investigation, make_plan(), review)
    assert report.verdict == "revise_plan"
    assert report.problems[0].source == "reviewer"
    assert "A1: processor outage" in report.feedback()[0]


def test_reviewer_must_be_consistent(triage, investigation):
    inconsistent = {**QA_PASS, "actions": [{**QA_PASS["actions"][0], "grounded": False}, *QA_PASS["actions"][1:]]}
    report, llm = run_qa(triage, investigation, make_plan(), inconsistent, QA_PASS)
    assert 'cannot be "pass"' in llm.calls[1][-1]["content"]
    assert report.verdict == "pass"


def test_exhausted_loops_escalate_and_block_failing_actions(triage, investigation):
    report, _ = run_qa(triage, investigation, make_plan(a0={"target": "all services"}), QA_PASS, loop=2)
    assert report.verdict == "escalate"
    assert report.blocked_actions == ["A1"]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"replies": [{**QA_PASS, "confidence": 0.3}]}, "confidence"),
        ({"replies": [QA_PASS], "flags": ["override"]}, "instructions to the AI"),
    ],
)
def test_escalation_reasons(triage, investigation, kwargs, reason):
    report, _ = run_qa(triage, investigation, make_plan(), *kwargs["replies"], flags=kwargs.get("flags", ()))
    assert report.verdict == "escalate"
    assert any(reason in r for r in report.escalation_reasons)


def test_unknown_service_cannot_have_change_actions(triage, investigation):
    report, _ = run_qa(triage.model_copy(update={"service": "unknown"}), investigation, make_plan(), QA_PASS, loop=2)
    assert report.verdict == "escalate"
    assert "A1" in report.blocked_actions
