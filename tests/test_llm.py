import pytest
from pydantic import BaseModel, Field

from relay.llm import AgentOutputError, extract_json, structured_call

from .conftest import FakeLLM


class Out(BaseModel):
    answer: str = Field(min_length=2)
    score: int


def call(llm, check=None, retries=2):
    return structured_call(llm, agent="test", system="sys", user="user", schema=Out, check=check, max_retries=retries)


def test_returns_valid_output_first_try():
    llm = FakeLLM({"answer": "ok", "score": 1})
    assert call(llm) == Out(answer="ok", score=1)
    assert len(llm.calls) == 1


def test_schema_is_in_the_system_prompt():
    llm = FakeLLM({"answer": "ok", "score": 1})
    call(llm)
    assert '"score"' in llm.calls[0][0]["content"]


def test_retries_invalid_json_with_error_feedback():
    llm = FakeLLM("not json at all", {"answer": "ok", "score": 2})
    assert call(llm).score == 2
    feedback = llm.calls[1][-1]["content"]
    assert "rejected" in feedback


def test_retries_schema_violation_and_names_the_field():
    llm = FakeLLM({"answer": "ok", "score": "high"}, {"answer": "ok", "score": 3})
    assert call(llm).score == 3
    assert "score" in llm.calls[1][-1]["content"]


def test_retries_when_semantic_check_fails():
    llm = FakeLLM({"answer": "no", "score": 1}, {"answer": "yes", "score": 1})
    out = call(llm, check=lambda o: [] if o.answer == "yes" else ["answer must be yes"])
    assert out.answer == "yes"
    assert "answer must be yes" in llm.calls[1][-1]["content"]


def test_raises_after_retries_and_never_returns_invalid_output():
    llm = FakeLLM(*[{"answer": "x", "score": 1}] * 3)
    with pytest.raises(AgentOutputError) as err:
        call(llm, retries=2)
    assert err.value.attempts == 3 and err.value.agent == "test"
    assert len(llm.calls) == 3


def test_redacts_every_message_sent():
    llm = FakeLLM("bad reply with key dp_FAKEdemoKEY7f3a9c2e1b4d", {"answer": "ok", "score": 1})
    structured_call(llm, agent="t", system="s", user="mail ops@brightcart.example", schema=Out)
    sent = llm.sent_text()
    assert "ops@brightcart.example" not in sent
    assert "dp_FAKEdemoKEY7f3a9c2e1b4d" not in sent


@pytest.mark.parametrize(
    "raw", ['```json\n{"a": 1}\n```', 'Sure! {"a": 1} hope that helps', '{"a": 1}']
)
def test_extract_json(raw):
    assert extract_json(raw) == '{"a": 1}'
