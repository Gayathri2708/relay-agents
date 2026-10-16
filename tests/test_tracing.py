import os
import uuid

import pytest
from pydantic import BaseModel

from relay.graph import IncidentState
from relay.llm import AgentOutputError, structured_call
from relay.tracing import Span, summarize

from .conftest import FakeLLM, happy_agents


class Out(BaseModel):
    answer: str


def test_span_records_every_attempt_with_tokens_cost_and_problems():
    span = Span(agent="t")
    llm = FakeLLM("garbage", {"answer": "ok"})
    structured_call(llm, agent="t", system="s", user="u", schema=Out, span=span)
    assert [c.attempt for c in span.llm_calls] == [1, 2]
    assert span.llm_calls[0].problems and span.llm_calls[1].problems == []
    assert span.prompt_tokens > 0 and span.completion_tokens > 0 and span.cost_usd > 0
    assert span.llm_calls[0].messages[0]["role"] == "system"


def test_failed_attempts_are_traced_too():
    span = Span(agent="t")
    with pytest.raises(AgentOutputError):
        structured_call(FakeLLM("x", "y"), agent="t", system="s", user="u", schema=Out, max_retries=1, span=span)
    assert len(span.llm_calls) == 2


def test_pipeline_traces_inputs_outputs_tools_and_models(alert):
    state = happy_agents().pipeline().run(alert)
    triage, investigate, plan, qa = state.trace
    assert triage.input["alert"]["text"] == alert.text and triage.output["triage"]["severity"] == "P1"
    assert triage.model == "fast-model" and plan.model == "strong-model"
    assert {t.tool for t in investigate.tool_calls} >= {"docpilot.search", "docpilot.ask", "ops.recent_deploys"}
    assert all(s.latency_ms >= 0 and s.status == "ok" for s in state.trace)
    usage = {u.agent: u for u in summarize(state.trace)}
    assert usage["plan"].llm_calls == 1 and usage["qa"].cost_usd > 0


# --- Postgres (runs when TEST_DATABASE_URL is set, as in CI) -----------------------------------

TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run Postgres tests")


@pytest.fixture
def pg_store():
    from relay.db import apply_schema, make_pool
    from relay.store import PostgresRunStore

    pool = make_pool(TEST_DB)
    apply_schema(pool)
    yield PostgresRunStore(pool)
    pool.close()


@needs_db
def test_postgres_stores_traces_per_agent(alert, pg_store):
    from relay.service import DecisionRequest, InlineExecutor, RunService

    service = RunService(happy_agents().pipeline(), pg_store, executor=InlineExecutor())
    run_id = service.submit(alert).id
    record = pg_store.get(run_id)
    assert record.state.status == "awaiting_approval"
    assert [s.agent for s in record.state.trace] == ["triage", "investigate", "plan", "qa"]
    assert record.state.trace[2].llm_calls[0].messages  # full prompts are kept for audit
    assert {u.agent for u in pg_store.usage()} >= {"triage", "plan"}

    decision = service.decide(run_id, "A1", DecisionRequest(decision="approved", decided_by="ic"))
    assert pg_store.get(run_id).decisions[0].simulation == decision.simulation


@needs_db
def test_postgres_update_does_not_duplicate_spans(alert, pg_store):
    state = happy_agents().pipeline().run(alert)
    record = pg_store.create(IncidentState(alert=alert))
    pg_store.update(record.id, state)
    pg_store.update(record.id, state)
    assert len(pg_store.get(record.id).state.trace) == 4
    assert pg_store.get(uuid.uuid4()) is None
