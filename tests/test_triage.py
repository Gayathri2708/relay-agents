from datetime import timedelta

import pytest

from relay.agents.triage import TriageAgent
from relay.llm import AgentOutputError
from relay.tools.docpilot import DocPilotError

from .conftest import START, TRIAGE_REPLY, FakeDocPilot, FakeLLM


def test_classifies_alert(alert):
    result = TriageAgent(FakeLLM(TRIAGE_REPLY), FakeDocPilot()).run(alert)
    assert (result.service, result.severity, result.policy_section) == ("payments-api", "P1", "SEV-1")
    assert result.start_time == START


def test_prompt_contains_policy_and_no_secrets(alert):
    llm = FakeLLM(TRIAGE_REPLY)
    TriageAgent(llm, FakeDocPilot()).run(alert)
    sent = llm.sent_text()
    assert "[SEV-1] P1 Critical" in sent  # policy pulled from DocPilot
    assert "jane.doe@example.com" not in sent and "dp_FAKEdemoKEY7f3a9c2e1b4d" not in sent
    assert "[REDACTED:email]" in sent


def test_missing_start_time_falls_back_to_received_at(alert):
    result = TriageAgent(FakeLLM({**TRIAGE_REPLY, "start_time": None}), FakeDocPilot()).run(alert)
    assert result.start_time == alert.received_at


def test_severity_contradicting_policy_section_is_retried(alert):
    llm = FakeLLM({**TRIAGE_REPLY, "severity": "P3"}, TRIAGE_REPLY)
    assert TriageAgent(llm, FakeDocPilot()).run(alert).severity == "P1"
    assert "contradicts" in llm.calls[1][-1]["content"]


def test_unknown_policy_section_is_retried(alert):
    llm = FakeLLM({**TRIAGE_REPLY, "policy_section": "SEV-9"}, TRIAGE_REPLY)
    TriageAgent(llm, FakeDocPilot()).run(alert)
    assert "SEV-9" in llm.calls[1][-1]["content"]


def test_invalid_service_and_severity_rejected(alert):
    bad = {**TRIAGE_REPLY, "service": "billing", "severity": "SEV1"}
    llm = FakeLLM(bad, bad, bad)
    with pytest.raises(AgentOutputError):
        TriageAgent(llm, FakeDocPilot(), max_retries=2).run(alert)


def test_future_start_time_rejected(alert):
    future = (alert.received_at + timedelta(hours=2)).isoformat()
    llm = FakeLLM({**TRIAGE_REPLY, "start_time": future}, TRIAGE_REPLY)
    TriageAgent(llm, FakeDocPilot()).run(alert)
    assert "after the alert was received" in llm.calls[1][-1]["content"]


def test_fails_clearly_without_severity_policy(alert):
    with pytest.raises(DocPilotError, match="Severity policy"):
        TriageAgent(FakeLLM(TRIAGE_REPLY), FakeDocPilot(only_files={"checkout.md"})).run(alert)
