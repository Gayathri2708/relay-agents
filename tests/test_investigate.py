import pytest

from relay.agents.investigate import InvestigateAgent
from relay.llm import AgentOutputError
from relay.tools.docpilot import DocPilotError

from .conftest import INVESTIGATION_REPLY, FakeDocPilot, FakeLLM, FakeOps


def kinds(evidence):
    return {e.kind for e in evidence}


def test_gathers_evidence_from_every_tool(triage):
    evidence = InvestigateAgent(FakeLLM(), FakeDocPilot(), FakeOps()).gather(triage)
    assert {"runbook", "runbook_answer", "deploy", "metrics", "logs", "past_incident"} <= kinds(evidence)
    assert [e.id for e in evidence] == [f"E{i}" for i in range(1, len(evidence) + 1)]


def test_checks_dependencies_too(triage):
    evidence = InvestigateAgent(FakeLLM(), FakeDocPilot(), FakeOps()).gather(triage)
    assert "orders-db metrics" in {e.source for e in evidence}  # payments-api depends on orders-db


def test_follows_runbook_cross_references(triage):
    docpilot = FakeDocPilot()
    evidence = InvestigateAgent(FakeLLM(), docpilot, FakeOps()).gather(triage)
    assert "[DEP-2]" in docpilot.queries  # PAY-2 says "following [DEP-2]"
    assert "DEP-2" in {sid for e in evidence for sid in e.defined_sections}


def test_logs_are_redacted_in_evidence(triage):
    evidence = InvestigateAgent(FakeLLM(), FakeDocPilot(), FakeOps()).gather(triage)
    logs = next(e for e in evidence if e.kind == "logs").content
    assert "jane.doe@example.com" not in logs and "dp_FAKE" not in logs
    assert "[REDACTED:email]" in logs


def test_produces_findings_citing_evidence(triage):
    llm = FakeLLM(INVESTIGATION_REPLY)
    result = InvestigateAgent(llm, FakeDocPilot(), FakeOps()).run(triage)
    assert result.findings[0].evidence_ids == ["E1"]
    assert "[E1]" in llm.calls[0][1]["content"]
    assert "jane.doe@example.com" not in llm.sent_text()


def test_unknown_evidence_ids_are_retried(triage):
    bad = {**INVESTIGATION_REPLY, "findings": [{"statement": "made up", "confidence": "high", "evidence_ids": ["E99"]}]}
    llm = FakeLLM(bad, INVESTIGATION_REPLY)
    InvestigateAgent(llm, FakeDocPilot(), FakeOps()).run(triage)
    assert "E99" in llm.calls[1][-1]["content"]


def test_findings_without_evidence_rejected(triage):
    bad = {**INVESTIGATION_REPLY, "findings": [{"statement": "trust me", "confidence": "high", "evidence_ids": []}]}
    with pytest.raises(AgentOutputError):
        InvestigateAgent(FakeLLM(bad, bad, bad), FakeDocPilot(), FakeOps()).run(triage)


def test_ops_failures_become_tool_errors_not_crashes(triage):
    evidence = InvestigateAgent(FakeLLM(), FakeDocPilot(), FakeOps(fail=True)).gather(triage)
    assert "tool_error" in kinds(evidence)
    assert "runbook" in kinds(evidence)


def test_ask_failure_is_not_fatal(triage):
    evidence = InvestigateAgent(FakeLLM(), FakeDocPilot(fail_ask=True), FakeOps()).gather(triage)
    assert "runbook_answer" not in kinds(evidence)
    assert any(e.kind == "tool_error" and "/ask" in e.source for e in evidence)


def test_docpilot_search_failure_is_fatal(triage):
    with pytest.raises(DocPilotError):
        InvestigateAgent(FakeLLM(), FakeDocPilot(fail_search=True), FakeOps()).gather(triage)
