from types import SimpleNamespace

from relay.config import Settings
from relay.graph import Pipeline
from relay.llm import ModelRouter, OpenAICompatibleLLM

from .conftest import FakeDocPilot, FakeOps


def settings(**kw) -> Settings:
    return Settings(_env_file=None, llm_api_key="test-key", docpilot_api_key="k", **kw)


def test_router_picks_fast_model_for_triage_and_strong_for_plan():
    router = ModelRouter(settings())
    assert router.for_agent("triage").model == "openai/gpt-oss-20b"
    assert router.for_agent("plan").model == "openai/gpt-oss-120b"
    assert router.for_agent("plan") is router.for_agent("qa")  # one client per model


def test_routing_is_configurable():
    router = ModelRouter(settings(model_triage="tiny", model_plan="huge"))
    assert (router.for_agent("triage").model, router.for_agent("plan").model) == ("tiny", "huge")


def test_pipeline_from_settings_wires_routed_models(monkeypatch):
    monkeypatch.setattr("relay.tools.docpilot.DocPilotClient.from_settings", lambda s: FakeDocPilot())
    pipeline = Pipeline.from_settings(settings(), FakeOps())
    nodes = pipeline._graph.nodes
    assert set(nodes) >= {"triage", "investigate", "plan", "qa"}


def test_cost_comes_from_usage_and_price_table():
    llm = OpenAICompatibleLLM(settings(), "openai/gpt-oss-120b")
    reply = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        usage=SimpleNamespace(prompt_tokens=2_000, completion_tokens=500),
    )
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: reply)))
    completion = llm.chat([{"role": "user", "content": "hi"}])
    assert (completion.prompt_tokens, completion.completion_tokens) == (2_000, 500)
    assert completion.cost_usd == (2_000 * 0.15 + 500 * 0.60) / 1_000_000


def test_unpriced_model_costs_zero_rather_than_crashing():
    assert OpenAICompatibleLLM(settings(), "some/new-model")._price_in == 0.0
