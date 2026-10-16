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


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def test_pacer_waits_for_token_budget_instead_of_overrunning():
    from relay.llm import Pacer

    clock = FakeClock()
    pacer = Pacer(tpm=8000, clock=clock, sleep=clock.sleep)
    first = pacer.acquire(5000)
    clock.t = 10
    pacer.acquire(2500)  # 7500 fits
    assert clock.sleeps == []
    pacer.settle(first, 6000)  # actual usage was higher: 8500 now in the window
    pacer.acquire(1000)  # must wait until the first request leaves the 60 s window
    assert clock.sleeps and clock.t >= 60
    assert pacer.waited_s == sum(clock.sleeps)


def test_pacer_limits_requests_per_minute():
    from relay.llm import Pacer

    clock = FakeClock()
    pacer = Pacer(tpm=0, rpm=2, clock=clock, sleep=clock.sleep)
    pacer.acquire(1)
    pacer.acquire(1)
    pacer.acquire(1)
    assert clock.t >= 60


def test_oversized_request_still_goes_alone():
    from relay.llm import Pacer

    pacer = Pacer(tpm=100, clock=FakeClock(), sleep=lambda s: (_ for _ in ()).throw(AssertionError("waited")))
    pacer.acquire(500)  # bigger than the budget but the window is empty: send it rather than hang forever


def test_daily_quota_error_is_distinguished(monkeypatch):
    import httpx
    import pytest
    from openai import RateLimitError

    from relay.llm import DailyLimitReached

    llm = OpenAICompatibleLLM(settings(), "openai/gpt-oss-120b")
    response = httpx.Response(429, request=httpx.Request("POST", "https://x"))

    def raise_tpd(**kw):
        raise RateLimitError("Rate limit reached on tokens per day (TPD): Limit 200000", response=response, body=None)

    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=raise_tpd)))
    with pytest.raises(DailyLimitReached):
        llm.chat([{"role": "user", "content": "hi"}])


def test_router_gives_each_model_its_own_pacer():
    router = ModelRouter(settings(llm_tpm=8000, llm_rpm=30))
    triage, plan = router.for_agent("triage"), router.for_agent("plan")
    assert triage.pacer is not plan.pacer and plan.pacer is router.for_agent("qa").pacer
