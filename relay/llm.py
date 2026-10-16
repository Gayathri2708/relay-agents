"""LLM access for the agents.

structured_call() is the only way agents talk to a model. It:
  1. redacts every message (defence in depth; inputs should already be redacted),
  2. asks for JSON matching a Pydantic schema,
  3. validates the reply against the schema and then against agent-specific checks
     (for example "every cited evidence id exists"),
  4. on failure, tells the model exactly what was wrong and retries,
  5. raises AgentOutputError when retries run out. Invalid output is never returned.
Every attempt (messages, reply, tokens, cost, latency, rejection reasons) is recorded on the span.
"""

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ValidationError

from .config import Settings
from .redact import redact_text
from .tracing import LLMCall, Span
from .untrusted import UNTRUSTED_NOTICE

log = logging.getLogger(__name__)

Message = dict[str, str]


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class LLM(Protocol):
    model: str

    def chat(self, messages: list[Message]) -> Completion: ...


class AgentOutputError(Exception):
    """An agent could not produce valid output within its retry budget."""

    def __init__(self, agent: str, problems: list[str], attempts: int):
        self.agent, self.problems, self.attempts = agent, problems, attempts
        super().__init__(f"{agent} output invalid after {attempts} attempt(s): " + "; ".join(problems))


class OpenAICompatibleLLM:
    """One model on any OpenAI-compatible chat completions endpoint (Groq, OpenAI, vLLM, Ollama, ...)."""

    def __init__(self, settings: Settings, model: str):
        from openai import OpenAI

        if not settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY is not set (see .env.example).")
        self._client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_s,
            max_retries=5,  # 429s back off and retry, honouring Retry-After
        )
        self.model = model
        self._max_tokens = settings.llm_max_output_tokens
        self._price_in, self._price_out = settings.llm_prices.get(model, (0.0, 0.0))
        if model not in settings.llm_prices:
            log.warning("no price configured for %s; its cost will show as 0", model)

    def chat(self, messages: list[Message]) -> Completion:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0,
            max_tokens=self._max_tokens,
            response_format={"type": "json_object"},
        )
        usage = resp.usage
        p_in, p_out = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        return Completion(
            text=resp.choices[0].message.content or "",
            model=self.model,
            prompt_tokens=p_in,
            completion_tokens=p_out,
            cost_usd=(p_in * self._price_in + p_out * self._price_out) / 1_000_000,
        )


class ModelRouter:
    """Builds one client per agent from the MODEL_<AGENT> settings."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._clients: dict[str, OpenAICompatibleLLM] = {}

    def for_agent(self, agent: str) -> OpenAICompatibleLLM:
        model = self._settings.model_for(agent)
        if model not in self._clients:
            self._clients[model] = OpenAICompatibleLLM(self._settings, model)
        return self._clients[model]


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(raw: str) -> str:
    """Pull the JSON object out of a reply that may be wrapped in a code fence or prose."""
    if m := _FENCE.search(raw):
        raw = m.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start : end + 1] if start != -1 and end > start else raw


def _describe(err: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in err.errors()]


def structured_call[T: BaseModel](
    llm: LLM,
    *,
    agent: str,
    system: str,
    user: str,
    schema: type[T],
    check: Callable[[T], list[str]] | None = None,
    max_retries: int = 2,
    span: Span | None = None,
) -> T:
    system = (
        f"{system}\n\n{UNTRUSTED_NOTICE}\n\nRespond with a single JSON object and nothing else. "
        f"It must validate against this JSON schema:\n{json.dumps(schema.model_json_schema())}"
    )
    messages: list[Message] = [
        {"role": "system", "content": redact_text(system)},
        {"role": "user", "content": redact_text(user)},
    ]
    problems: list[str] = []
    attempts = 1 + max_retries

    for attempt in range(1, attempts + 1):
        t0 = time.perf_counter()
        completion = llm.chat(messages)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        raw = completion.text
        try:
            result = schema.model_validate_json(extract_json(raw))
            problems = check(result) if check else []
        except ValidationError as err:
            problems = _describe(err)

        if span is not None:
            span.llm_calls.append(
                LLMCall(
                    model=completion.model,
                    attempt=attempt,
                    messages=[dict(m) for m in messages],
                    reply=redact_text(raw),
                    prompt_tokens=completion.prompt_tokens,
                    completion_tokens=completion.completion_tokens,
                    cost_usd=completion.cost_usd,
                    latency_ms=latency_ms,
                    problems=problems,
                )
            )
        if not problems:
            return result

        log.warning("%s attempt %d/%d invalid: %s", agent, attempt, attempts, problems)
        messages += [
            {"role": "assistant", "content": redact_text(raw)},
            {
                "role": "user",
                "content": "Your JSON was rejected:\n- "
                + "\n- ".join(problems)
                + "\nReturn a corrected JSON object only.",
            },
        ]

    raise AgentOutputError(agent, problems, attempts)
