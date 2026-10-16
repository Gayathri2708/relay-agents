"""Per-agent trace spans: inputs, outputs, every LLM attempt, tool calls, latency, tokens and cost."""

import time
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class LLMCall(BaseModel):
    model: str
    attempt: int
    messages: list[dict[str, str]]
    reply: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    problems: list[str] = []  # why this attempt was rejected; empty if accepted


class ToolCall(BaseModel):
    tool: str
    input: str
    latency_ms: int
    ok: bool
    summary: str


class Span(BaseModel):
    agent: str
    loop: int = 0  # 0 on the first pass; QA sending work back increments it
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    latency_ms: int = 0
    status: Literal["ok", "error"] = "ok"
    input: dict[str, Any] = {}
    output: dict[str, Any] | None = None
    error: str | None = None
    llm_calls: list[LLMCall] = []
    tool_calls: list[ToolCall] = []

    @property
    def prompt_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.llm_calls)

    @property
    def completion_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.llm_calls)

    @property
    def cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.llm_calls), 6)

    @property
    def model(self) -> str | None:
        return self.llm_calls[-1].model if self.llm_calls else None

    def tool(self, name: str, query: str):
        """Context manager timing one tool call: `with span.tool("docpilot.search", q) as t: t.summary = ...`."""
        return _ToolTimer(self, name, query)


class _ToolTimer:
    def __init__(self, span: Span, name: str, query: str):
        self.span, self.name, self.query, self.summary = span, name, query, ""

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.span.tool_calls.append(
            ToolCall(
                tool=self.name,
                input=self.query[:500],
                latency_ms=int((time.perf_counter() - self.t0) * 1000),
                ok=exc is None,
                summary=self.summary if exc is None else f"{type(exc).__name__}: {exc}"[:500],
            )
        )
        return False  # never swallow the exception


class AgentUsage(BaseModel):
    agent: str
    runs: int
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    avg_latency_ms: int


def summarize(spans: list[Span]) -> list[AgentUsage]:
    by_agent: dict[str, list[Span]] = {}
    for s in spans:
        by_agent.setdefault(s.agent, []).append(s)
    return [
        AgentUsage(
            agent=agent,
            runs=len(group),
            llm_calls=sum(len(s.llm_calls) for s in group),
            prompt_tokens=sum(s.prompt_tokens for s in group),
            completion_tokens=sum(s.completion_tokens for s in group),
            cost_usd=round(sum(s.cost_usd for s in group), 6),
            avg_latency_ms=int(sum(s.latency_ms for s in group) / len(group)),
        )
        for agent, group in by_agent.items()
    ]
