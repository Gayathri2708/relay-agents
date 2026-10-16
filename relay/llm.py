"""LLM access for the agents.

structured_call() is the only way agents talk to a model. It:
  1. redacts every message (defence in depth; inputs should already be redacted),
  2. asks for JSON matching a Pydantic schema,
  3. validates the reply against the schema and then against agent-specific checks
     (for example "every cited evidence id exists"),
  4. on failure, tells the model exactly what was wrong and retries,
  5. raises AgentOutputError when retries run out. Invalid output is never returned.
"""

import json
import logging
import re
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, ValidationError

from .config import Settings
from .redact import redact_text

log = logging.getLogger(__name__)

Message = dict[str, str]


class LLM(Protocol):
    def chat(self, messages: list[Message]) -> str: ...


class AgentOutputError(Exception):
    """An agent could not produce valid output within its retry budget."""

    def __init__(self, agent: str, problems: list[str], attempts: int):
        self.agent, self.problems, self.attempts = agent, problems, attempts
        super().__init__(f"{agent} output invalid after {attempts} attempt(s): " + "; ".join(problems))


class OpenAICompatibleLLM:
    """Any OpenAI-compatible chat completions endpoint (Groq, OpenAI, vLLM, Ollama, ...)."""

    def __init__(self, settings: Settings):
        from openai import OpenAI

        if not settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY is not set (see .env.example).")
        self._client = OpenAI(
            base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=settings.llm_timeout_s
        )
        self._model = settings.llm_model

    def chat(self, messages: list[Message]) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""


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
) -> T:
    system = (
        f"{system}\n\nRespond with a single JSON object and nothing else. "
        f"It must validate against this JSON schema:\n{json.dumps(schema.model_json_schema())}"
    )
    messages: list[Message] = [
        {"role": "system", "content": redact_text(system)},
        {"role": "user", "content": redact_text(user)},
    ]
    problems: list[str] = []
    attempts = 1 + max_retries

    for attempt in range(1, attempts + 1):
        raw = llm.chat(messages)
        try:
            result = schema.model_validate_json(extract_json(raw))
            problems = check(result) if check else []
        except ValidationError as err:
            problems = _describe(err)
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
