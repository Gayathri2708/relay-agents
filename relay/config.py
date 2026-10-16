from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "openai/gpt-oss-120b"
    llm_timeout_s: float = 60.0
    # Extra attempts after the first when the model returns output that fails validation.
    llm_max_retries: int = 2

    database_url: str = "postgresql:///relay"

    docpilot_url: str = "http://localhost:8000"
    docpilot_api_key: str = ""  # read-only key with the engineering group
    docpilot_admin_key: str = ""  # seed/upload_runbooks.py only
    docpilot_timeout_s: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
