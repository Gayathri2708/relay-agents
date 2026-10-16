"""Test doubles: a scripted LLM, an in-memory DocPilot over the real seed runbooks, and fake ops data."""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from relay.schemas import Alert, Evidence, Finding, Investigation, TriageResult
from relay.tools.docpilot import DocPilotError, RunbookAnswer, RunbookHit
from relay.tools.ops import Deploy, LogLine, MetricSnapshot, PastIncident

RUNBOOKS = Path(__file__).parent.parent / "seed" / "runbooks"
START = datetime(2026, 10, 14, 14, 2, tzinfo=UTC)

Reply = str | dict | Callable[[list[dict]], str | dict]


class FakeLLM:
    """Returns scripted replies in order. A reply may be a callable that sees the messages."""

    def __init__(self, *replies: Reply):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict]) -> str:
        self.calls.append([dict(m) for m in messages])
        if not self.replies:
            raise AssertionError("FakeLLM ran out of scripted replies")
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(messages)
        return reply if isinstance(reply, str) else json.dumps(reply)

    def sent_text(self) -> str:
        return "\n".join(m["content"] for call in self.calls for m in call)


def _sections() -> list[RunbookHit]:
    """One chunk per runbook section, a simplification of DocPilot's sentence-window chunking."""
    hits, cid = [], 0
    for path in sorted(RUNBOOKS.glob("*.md")):
        for part in re.split(r"\n(?=## )", path.read_text()):
            cid += 1
            hits.append(RunbookHit(cid, path.name, part.strip(), 0.0))
    return hits


class FakeDocPilot:
    def __init__(self, fail_search: bool = False, fail_ask: bool = False, only_files: set[str] | None = None):
        self.chunks = [h for h in _sections() if only_files is None or h.filename in only_files]
        self.fail_search, self.fail_ask = fail_search, fail_ask
        self.queries: list[str] = []

    def search(self, query: str, top_k: int = 5, mode: str | None = None) -> list[RunbookHit]:
        self.queries.append(query)
        if self.fail_search:
            raise DocPilotError("DocPilot unreachable")
        if re.fullmatch(r"\[[A-Z]+-\d+\]", query):  # section lookups made by reference expansion
            return [h for h in self.chunks if query in h.content][:top_k]
        words = set(re.findall(r"[a-z0-9-]{3,}", query.lower()))
        scored = sorted(self.chunks, key=lambda h: -len(words & set(re.findall(r"[a-z0-9-]{3,}", h.content.lower()))))
        return scored[:top_k]

    def ask(self, question: str, top_k: int = 5) -> RunbookAnswer:
        if self.fail_ask:
            raise DocPilotError("DocPilot /ask returned 503")
        return RunbookAnswer("Check for a recent deploy and roll back per DEP-2 [1].", True, ["payments-api.md"])


class FakeOps:
    def __init__(self, fail: bool = False):
        self.fail = fail

    def recent_deploys(self, at, lookback=timedelta(hours=6)):
        if self.fail:
            raise ConnectionError("database down")
        return [
            Deploy(service="payments-api", version="v2.41.0", previous_version="v2.40.3", author="priya.n",
                   commit_sha="e7b02d9", summary="Refactor idempotency key handling", has_migration=False,
                   finished_at=at - timedelta(minutes=15)),
        ]

    def metrics(self, service, at):
        if self.fail:
            raise ConnectionError("database down")
        rate = 14.8 if service == "payments-api" else 0.3
        return [MetricSnapshot(service=service, captured_at=at, metrics={"error_rate_pct": rate})]

    def logs(self, service, at, limit=15):
        if self.fail:
            raise ConnectionError("database down")
        if service != "payments-api":
            return []
        return [
            LogLine(service=service, captured_at=at, level="ERROR",
                    message="KeyError: 'idempotency_key' customer=jane.doe@example.com "
                            "api_key=dp_FAKEdemoKEY7f3a9c2e1b4d"),
        ]

    def similar_incidents(self, service, text, limit=3):
        if self.fail:
            raise ConnectionError("database down")
        return [
            PastIncident(id="INC-1987", service="payments-api", severity="P1", title="Payments 5xx after release",
                         summary="5xx after deploy.", root_cause="Bad release.", resolution="Rolled back.",
                         started_at=START - timedelta(days=100)),
        ]


# --- canned agent outputs ------------------------------------------------------------------------

TRIAGE_REPLY = {
    "service": "payments-api",
    "severity": "P1",
    "symptoms": ["5xx error rate 14.8% on POST /v1/payments/authorize", "KeyError: 'idempotency_key'"],
    "start_time": "2026-10-14T14:02:00Z",
    "severity_rationale": "Authorizations failing above 10% is P1 per SEV-1.",
    "policy_section": "SEV-1",
}

INVESTIGATION_REPLY = {
    "summary": "payments-api errors began 15 minutes after deploy v2.41.0.",
    "likely_cause": "Deploy v2.41.0 broke idempotency key handling.",
    "findings": [
        {"statement": "Errors started after the v2.41.0 deploy", "confidence": "high", "evidence_ids": ["E1"]}
    ],
}

PLAN_REPLY = {
    "summary": "Roll back payments-api, page payments, update status page.",
    "actions": [
        {
            "kind": "rollback",
            "title": "Roll back payments-api to v2.40.3",
            "details": "Errors started right after v2.41.0; roll back per the runbook.",
            "target": "payments-api",
            "citations": [{"runbook": "payments-api.md", "section": "PAY-2"}],
        },
        {
            "kind": "page_team",
            "title": "Page team payments",
            "details": "P1 on payments-api; page PagerDuty service PAY.",
            "target": "payments",
            "citations": [{"runbook": "payments-api.md", "section": "PAY-5"}],
        },
        {
            "kind": "status_update",
            "title": "Post a status page update",
            "details": "Customer-facing update per the template.",
            "citations": [{"runbook": "incident-severity-and-communication-policy.md", "section": "COMM-1"}],
            "status_update_draft": "We are investigating reports of failed card payments. Browsing still works. "
            "Next update by 14:45 UTC.",
        },
    ],
}


@pytest.fixture
def alert() -> Alert:
    return Alert.from_raw(
        "PaymentsAPIHighErrorRate service: payments-api startsAt: 2026-10-14T14:02:00Z "
        "5xx 14.8% sample: customer=jane.doe@example.com api_key=dp_FAKEdemoKEY7f3a9c2e1b4d",
        received_at=START + timedelta(minutes=3),
    )


@pytest.fixture
def triage() -> TriageResult:
    return TriageResult.model_validate({**TRIAGE_REPLY})


def runbook_evidence() -> list[Evidence]:
    chunks = [h for h in _sections() if re.search(r"\[(PAY-2|PAY-5|DEP-2|COMM-1)\]", h.content.split("\n")[0])]
    return [Evidence(id=f"E{i}", kind="runbook", source=h.filename, content=h.content) for i, h in enumerate(chunks, 1)]


@pytest.fixture
def investigation() -> Investigation:
    return Investigation(
        summary="payments-api errors began after deploy v2.41.0.",
        likely_cause="Bad deploy",
        findings=[Finding(statement="Errors follow the deploy", confidence="high", evidence_ids=["E1"])],
        evidence=runbook_evidence(),
    )
