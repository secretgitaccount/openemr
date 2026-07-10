"""Typed application configuration.

`Settings` is the single source of truth for runtime configuration. Values are
read from the process environment and an optional `.env` file (see
`.env.example`). Every field carries a sensible development default so the app
and its tests boot without a populated `.env`; production overrides everything
via real environment variables.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from the environment / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Anthropic ---
    anthropic_api_key: str = "sk-ant-xxxxxxxx"
    anthropic_model: str = "claude-sonnet-5"

    # --- OpenEMR (local development-easy stack) ---
    openemr_base_url: str = "http://localhost:8300"
    openemr_fhir_base: str = "http://localhost:8300/apis/default/fhir"
    openemr_oauth_base: str = "http://localhost:8300/oauth2/default"
    openemr_dev_user: str = "admin"
    openemr_dev_pass: str = "pass"
    openemr_client_id: str = ""
    openemr_client_secret: str = ""

    # --- Langfuse (PHI-scrubbed observability) ---
    langfuse_public_key: str = "pk-lf-xxxxxxxx"
    langfuse_secret_key: str = "sk-lf-xxxxxxxx"
    langfuse_host: str = "https://cloud.langfuse.com"

    # --- Agent ---
    agent_port: int = 8000
    log_level: str = "INFO"
    #: The agent's own public base URL — used to build the SMART launch
    #: ``redirect_uri`` (``{agent_base_url}/launch/callback``). Must match a
    #: redirect URI registered on the OpenEMR OAuth client. On Railway set this
    #: to the agent's public URL.
    agent_base_url: str = "http://localhost:8000"

    # --- Load testing ---
    # When true, ``LLMClient`` returns a canned, source-bound summary/answer
    # instead of calling Anthropic. Used by the Locust load tests (loadtest/) so
    # throughput is measured without token spend. Never enable in production.
    copilot_llm_stub: bool = False


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached `Settings` instance."""

    return Settings()
