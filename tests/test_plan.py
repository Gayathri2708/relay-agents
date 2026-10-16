import copy

import pytest

from relay.agents.plan import PlanAgent
from relay.llm import AgentOutputError
from relay.schemas import Investigation, ProposedAction

from .conftest import PLAN_REPLY, FakeLLM


def plan_with(**changes):
    reply = copy.deepcopy(PLAN_REPLY)
    for index, fields in changes.items():
        reply["actions"][int(index[1:])].update(fields)
    return reply


def test_builds_plan_with_ids_and_citations(triage, investigation):
    plan = PlanAgent(FakeLLM(PLAN_REPLY)).run(triage, investigation)
    assert [a.id for a in plan.actions] == ["A1", "A2", "A3"]
    assert plan.actions[0].citations[0].section == "PAY-2"
    assert plan.executed is False


def test_risky_actions_always_require_approval(triage, investigation):
    reply = plan_with(a0={"risk": "low", "requires_approval": False})  # model tries to wave it through
    plan = PlanAgent(FakeLLM(reply)).run(triage, investigation)
    rollback, page, status = plan.actions
    assert (rollback.risk, rollback.requires_approval) == ("high", True)
    assert (page.risk, page.requires_approval) == ("low", False)
    assert (status.risk, status.requires_approval) == ("low", True)  # publishing needs IC sign-off


@pytest.mark.parametrize(
    "kind", ["rollback", "scale_up", "feature_flag", "failover", "restart", "config_change", "terminate_query"]
)
def test_contract_enforces_approval_for_every_risky_kind(kind):
    action = ProposedAction(
        id="A1", kind=kind, title="Do the thing", details="Details of the thing",
        citations=[{"runbook": "x.md", "section": "PAY-2"}], risk="low", requires_approval=False,
    )
    assert action.requires_approval and action.risk == "high"


def test_citing_a_section_not_retrieved_is_retried(triage, investigation):
    llm = FakeLLM(plan_with(a0={"citations": [{"runbook": "payments-api.md", "section": "PAY-4"}]}), PLAN_REPLY)
    PlanAgent(llm).run(triage, investigation)
    assert "PAY-4" in llm.calls[1][-1]["content"]


def test_citing_the_wrong_runbook_is_retried(triage, investigation):
    llm = FakeLLM(plan_with(a0={"citations": [{"runbook": "checkout.md", "section": "PAY-2"}]}), PLAN_REPLY)
    PlanAgent(llm).run(triage, investigation)
    assert "is in payments-api.md" in llm.calls[1][-1]["content"]


def test_action_without_citation_rejected(triage, investigation):
    bad = plan_with(a0={"citations": []})
    with pytest.raises(AgentOutputError):
        PlanAgent(FakeLLM(bad, bad, bad)).run(triage, investigation)


def test_p1_requires_paging_and_status_update(triage, investigation):
    reply = copy.deepcopy(PLAN_REPLY)
    reply["actions"] = reply["actions"][:1]
    llm = FakeLLM(reply, PLAN_REPLY)
    PlanAgent(llm).run(triage, investigation)
    feedback = llm.calls[1][-1]["content"]
    assert "page_team" in feedback and "status_update" in feedback


def test_p3_does_not_require_paging(triage, investigation):
    reply = copy.deepcopy(PLAN_REPLY)
    reply["actions"] = reply["actions"][:1]
    plan = PlanAgent(FakeLLM(reply)).run(triage.model_copy(update={"severity": "P3"}), investigation)
    assert len(plan.actions) == 1


def test_status_update_must_not_name_internal_systems(triage, investigation):
    leaky = plan_with(a2={"status_update_draft": "payments-api is down after a deploy by priya. Next update 15:00."})
    llm = FakeLLM(leaky, PLAN_REPLY)
    PlanAgent(llm).run(triage, investigation)
    assert "internal systems" in llm.calls[1][-1]["content"]


def test_status_update_may_say_checkout(triage, investigation):
    ok = plan_with(a2={"status_update_draft": "Some customers cannot complete checkout. Next update 15:00 UTC."})
    llm = FakeLLM(ok)
    PlanAgent(llm).run(triage, investigation)
    assert len(llm.calls) == 1


def test_status_update_needs_draft_text(triage, investigation):
    llm = FakeLLM(plan_with(a2={"status_update_draft": None}), PLAN_REPLY)
    PlanAgent(llm).run(triage, investigation)
    assert "status_update_draft" in llm.calls[1][-1]["content"]


def test_no_runbooks_means_no_plan(triage, investigation):
    empty = Investigation(**investigation.model_dump(exclude={"evidence"}), evidence=[])
    llm = FakeLLM()
    with pytest.raises(AgentOutputError, match="no runbook sections"):
        PlanAgent(llm).run(triage, empty)
    assert llm.calls == []


@pytest.mark.parametrize(
    "details",
    [
        "Terminate the long-running analytics query on the replica.",
        "Roll back payments-api if errors persist.",
        "Re-publish the previous signing key to JWKS.",
    ],
)
def test_read_only_kind_cannot_hide_a_production_change(triage, investigation, details):
    sneaky = plan_with(a1={"kind": "investigate_further", "details": details})
    llm = FakeLLM(sneaky, PLAN_REPLY)
    PlanAgent(llm).run(triage, investigation)
    assert "would change production" in llm.calls[1][-1]["content"]


def test_read_only_kind_may_observe(triage, investigation):
    reply = plan_with(a1={"kind": "page_team", "details": "Page payments on-call; check deploy history and metrics."})
    llm = FakeLLM(reply)
    PlanAgent(llm).run(triage, investigation)
    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("Roll back payments-api to v2.40.3.", "Roll back"),
        ("Check the dashboards, then terminate the export query.", "terminate"),
        ("Monitor the error rate after the rollback completes.", None),
        ("Confirm the failover worked and lag is under 10s.", None),
        ("Verify whether a restart is needed.", None),
    ],
)
def test_mentions_of_a_change_are_not_changes(text, flagged):
    from relay.grounding import change_verb

    assert change_verb(text) == flagged
