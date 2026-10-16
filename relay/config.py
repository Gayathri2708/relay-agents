from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# USD per 1M tokens (input, output). Groq list prices as of October 2026; override with LLM_PRICES.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-20b": (0.075, 0.30),
    "openai/gpt-oss-120b": (0.15, 0.60),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_timeout_s: float = 60.0
    # Extra attempts after the first when the model returns output that fails validation.
    llm_max_retries: int = 2
    # Caps each reply (reasoning included). Also keeps requests inside per-minute token limits, which
    # providers like Groq check against prompt + max output.
    llm_max_output_tokens: int = 3000
    # Client-side pacing per model (0 = off). Set to your provider's per-minute limits, e.g. Groq's free
    # tier: LLM_TPM=8000, LLM_RPM=30. Requests then wait for room instead of failing with 429/413.
    llm_tpm: int = 0
    llm_rpm: int = 0

    # Model routing: a small fast model where the task is classification, a stronger one for reasoning.
    model_triage: str = "openai/gpt-oss-20b"
    model_investigate: str = "openai/gpt-oss-120b"
    model_plan: str = "openai/gpt-oss-120b"
    model_qa: str = "openai/gpt-oss-120b"
    model_postmortem: str = "openai/gpt-oss-120b"
    llm_prices: dict[str, tuple[float, float]] = Field(default_factory=lambda: dict(DEFAULT_PRICES))

    # QA gate: how many times it may send work back, and the confidence below which a human must look.
    qa_max_loops: int = 2
    qa_min_confidence: float = 0.6

    # API keys for the HTTP API, as JSON: {"<name>": {"key": "<secret>", "role": "approver" | "reporter"}}.
    # reporter: submit alerts and read runs (e.g. the MCP server). approver: also decide, resolve, postmortem.
    # Empty = no auth, for local development only (a warning is logged at startup).
    relay_api_keys: dict[str, dict[str, str]] = Field(default_factory=dict)

    database_url: str = "postgresql:///relay"

    docpilot_url: str = "http://localhost:8000"
    docpilot_api_key: str = ""  # read-only key with the engineering group
    docpilot_admin_key: str = ""  # seed/upload_runbooks.py only
    docpilot_timeout_s: float = 60.0

    def model_for(self, agent: str) -> str:
        return getattr(self, f"model_{agent}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
