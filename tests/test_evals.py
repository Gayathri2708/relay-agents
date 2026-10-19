"""The eval harness: dataset integrity, fixture ops, and the scoring rules (no LLM involved)."""

from collections import Counter
from typing import get_args

import pytest

from evals.fixture_ops import FixtureOps
from evals.run import load_cases, record_for
from evals.scoring import first_action, score, summarize, unsafe_reasons
from relay.schemas import KNOWN_SERVICES, ActionKind, Alert

from .conftest import GRAPH_PLAN, INVESTIGATION_REPLY, QA_PASS, TRIAGE_REPLY, Agents, happy_agents

CASES = load_cases()
BY_ID = {c["id"]: c for c in CASES}
KINDS = set(get_args(ActionKind)) | {"none"}


def test_dataset_size_and_mix():
    assert 40 <= len(CASES) <= 60
    counts = Counter(c["category"] for c in CASES)
    assert set(counts) == {"clear", "ambiguous", "multi_service", "malicious"}
    assert min(counts.values()) >= 8
    assert len(BY_ID) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_labels_are_well_formed(case):
    e = case["expect"]
    assert set(e["service"]) <= {*KNOWN_SERVICES, "unknown"}
    assert set(e["severity"]) <= {"P1", "P2", "P3", "P4"} and e["severity"]
    assert set(e["first_action"]) <= KINDS and e["first_action"]
    assert set(e["scope"]) <= set(KNOWN_SERVICES)
    assert set(e["forbidden"]["kinds"]) <= KINDS
    if set(e["first_action"]) - {"none"}:
        assert e["scope"], "a change is expected, so something must be in scope"
    if case["category"] == "malicious":
        assert e["escalate"] is True and e["injected"]


@pytest.mark.parametrize("case", CASES[:5] + [c for c in CASES if c["category"] == "malicious"][:2],
                         ids=lambda c: c["id"])
def test_fixture_ops_feed_the_real_pipeline(case):
    ops = FixtureOps(case)
    alert = Alert.from_raw(case["alert"])
    assert alert.text
    state = happy_agents().pipeline(ops=ops).run(alert)  # scripted LLM; the evidence comes from the fixture
    kinds = {e.kind for e in state.investigation.evidence}
    assert "metrics" in kinds or not case["ops"]["metrics"]


def test_deploy_window_uses_case_times():
    case = BY_ID["clear-pay-deploy-1"]
    ops = FixtureOps(case)
    recent = ops.recent_deploys(ops.start)
    assert [d.service for d in recent] == ["payments-api", "checkout"]  # 15 min and 4 h before


# --- scoring -------------------------------------------------------------------------------------

def action(kind, target="payments-api", text="Roll back payments-api per DEP-2.", blocked=False, grounded=True):
    return {"id": "A1", "kind": kind, "target": target, "title": text, "details": text,
            "status_update_draft": None, "blocked": blocked, "grounded": grounded}


def test_first_action_skips_paging_and_comms():
    assert first_action([action("page_team"), action("status_update"), action("rollback")]) == "rollback"
    assert first_action([action("page_team"), action("investigate_further")]) == "none"


@pytest.mark.parametrize(
    ("act", "reason"),
    [
        (action("rollback", target="all services"), "outside"),
        (action("failover", target="orders-db"), "forbidden for this incident"),
        (action("restart", text="Restart all services now."), "mass action"),
    ],
)
def test_unsafe_reasons(act, reason):
    reasons = unsafe_reasons(act, BY_ID["clear-pay-processor-1"]["expect"] | {"forbidden": {
        "kinds": ["failover"], "targets": [], "text": []}})
    assert any(reason in r for r in reasons)


def test_forbidden_target_and_text():
    expect = BY_ID["multi-checkout-from-payments-1"]["expect"]
    assert unsafe_reasons(action("rollback", target="checkout"), expect)
    assert not unsafe_reasons(action("rollback", target="payments-api"), expect)
    mal = BY_ID["mal-log-disable-validation"]["expect"]
    assert unsafe_reasons(action("config_change", target="auth-service", text="Disable token validation."), mal)


def test_blocked_actions_are_not_counted_as_unsafe():
    case = BY_ID["clear-pay-processor-1"]
    record = {"id": case["id"], "category": "clear", "status": "escalated", "triage": None,
              "actions": [action("rollback", blocked=True)]}
    assert score(record, case)["unsafe"] == {}
    record["actions"][0]["blocked"] = False
    assert score(record, case)["unsafe"]  # rollback is forbidden in a processor outage


def test_record_and_summary_from_a_real_state():
    case = BY_ID["clear-pay-deploy-1"]
    plan = {**GRAPH_PLAN, "actions": [dict(a) for a in GRAPH_PLAN["actions"]]}
    plan["actions"][0].update(title="Roll back payments-api", details="Roll back the latest payments-api deploy.")
    triage = {**TRIAGE_REPLY, "start_time": case["start"]}  # evidence windows follow the triaged start time
    agents = Agents([triage], [INVESTIGATION_REPLY], [plan], [QA_PASS])
    state = agents.pipeline(ops=FixtureOps(case)).run(Alert.from_raw(case["alert"]))
    record = record_for(case, "routed", state, latency_s=12.5, waited_s=0.0)
    assert record["triage"] == {"service": "payments-api", "severity": "P1"}
    assert record["cost_usd"] > 0 and set(record["by_agent"]) == {"triage", "investigate", "plan", "qa"}
    s = summarize([record], BY_ID)
    assert s["severity_accuracy_pct"] == 100.0 and s["service_accuracy_pct"] == 100.0
    assert s["first_action_accuracy_pct"] == 100.0 and s["unsafe_action_rate_pct"] == 0.0
    assert s["latency_p50_s"] == 12.5


def test_failed_runs_count_against_accuracy():
    case = BY_ID["clear-pay-deploy-1"]
    failed = {"id": case["id"], "category": "clear", "status": "failed", "triage": None, "actions": [],
              "latency_s": 1, "pacing_wait_s": 0, "cost_usd": 0, "tokens": 0}
    s = summarize([failed], BY_ID)
    assert s["failed"] == 1 and s["severity_accuracy_pct"] == 0.0 and s["unsafe_action_rate_pct"] == 0.0


def test_run_order_rotates_through_categories():
    from evals.run import stratified

    ordered = stratified(CASES)
    assert sorted(c["id"] for c in ordered) == sorted(BY_ID)
    first_eight = [c["category"] for c in ordered[:8]]
    assert set(first_eight) == {"clear", "ambiguous", "multi_service", "malicious"}  # any prefix is mixed


def test_readme_block_states_progress_and_only_measured_numbers(tmp_path, monkeypatch):
    import json

    from evals import report, update_readme

    case = BY_ID["clear-pay-deploy-1"]
    plan = {**GRAPH_PLAN, "actions": [dict(a) for a in GRAPH_PLAN["actions"]]}
    plan["actions"][0].update(title="Roll back payments-api", details="Roll back the latest payments-api deploy.")
    agents = Agents([{**TRIAGE_REPLY, "start_time": case["start"]}], [INVESTIGATION_REPLY], [plan], [QA_PASS])
    state = agents.pipeline(ops=FixtureOps(case)).run(Alert.from_raw(case["alert"]))
    (tmp_path / "results.jsonl").write_text(json.dumps(record_for(case, "routed", state, 40.0, 30.0, "abc1234")) + "\n")
    (tmp_path / "meta.json").write_text(json.dumps({"git_shas": ["abc1234"], "configs": {}}))
    report.main(tmp_path)

    readme = tmp_path / "README.md"
    readme.write_text(f"intro\n{update_readme.START}\nSTALE-BLOCK\n{update_readme.END}\noutro\n")
    monkeypatch.setattr(update_readme, "README", readme)
    update_readme.main(tmp_path)
    text = readme.read_text()
    assert text.startswith("intro\n") and text.endswith("\noutro\n") and "STALE-BLOCK" not in text
    assert "**1 of 49** incidents scored" in text and "**0 of 49**" in text and "Partial results" in text
    assert "| Metric | routed (n=1) |" in text  # single has no results yet, so no column for it
    assert "`abc1234`" in text
