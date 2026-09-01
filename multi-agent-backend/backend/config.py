"""Centralized configuration — the single source of truth for all settings.

Every configurable value lives in the ``Settings`` class below. Values are
provided via environment variables (case-insensitive) or a ``.env`` file.
``os.getenv`` must not be used anywhere else in the codebase; import
``settings`` (or call ``get_settings()``) from here instead.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Application settings with environment-variable fallback."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- application ---
    app_name: str = "multi-agent-backend"
    app_version: str = "0.1.0"
    environment: Literal["development", "production"] = "development"
    debug: bool = True
    api_prefix: str = "/v1"
    # Comma-separated bearer tokens accepted on /v1/* endpoints.
    # Empty string disables auth entirely (local development only).
    backend_api_keys: str = ""
    run_migrations_on_start: bool = True
    public_base_url: str = "http://localhost:8000"

    # --- PostgreSQL (app state + LangGraph checkpoints) ---
    database_url: str = "postgresql+asyncpg://agent:agent@localhost:5432/agent"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_connect_retries: int = 10
    db_retry_delay_s: float = 2.0

    # --- Redis (cache + pub/sub; never Celery) ---
    redis_url: str = "redis://localhost:6379/0"

    # --- LiteLLM proxy ---
    litellm_base_url: str = "http://localhost:4000"
    litellm_api_key: str = "sk-local-litellm-master"
    default_model: str = "agent-default"
    llm_timeout_seconds: float = 90.0
    llm_max_retries: int = 2

    # --- agent context (git-versioned directory) ---
    context_dir: str = "context"

    # --- DuckDB (embedded, in-process analytics engine) ---
    duckdb_path: str = "data/analytics.duckdb"
    duckdb_max_rows: int = 200
    execute_analytics_sql: bool = True

    # --- n8n human-approval webhooks ---
    n8n_webhook_url: str = ""
    # JSON mapping of workflow_name → webhook URL
    # e.g. '{"send_email": "https://n8n.example/webhook/abc"}'
    n8n_workflows: str = ""

    # --- web search ---
    # Leave empty to use DuckDuckGo (no key required)
    # Set to a self-hosted SearXNG/Brave/Google CSE endpoint for production
    search_backend_url: str = ""

    # --- observability ---
    log_level: str = "INFO"
    log_json: bool = True
    sentry_dsn: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ---------------------------------------------------------------- helpers
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def api_keys(self) -> list[str]:
        return [k.strip() for k in self.backend_api_keys.split(",") if k.strip()]

    @property
    def context_path(self) -> Path:
        """Absolute path to the git-versioned context directory."""
        p = Path(self.context_dir)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def duckdb_file(self) -> Path:
        """Absolute path of the DuckDB database file (never a service)."""
        p = Path(self.duckdb_path)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def checkpointer_dsn(self) -> str:
        """DSN for langgraph-checkpoint-postgres (psycopg — no +asyncpg suffix)."""
        return self.database_url.replace("+asyncpg", "")


@lru_cache
def get_settings() -> Settings:
    """Return the cached Settings instance (env -> .env -> defaults)."""
    return Settings()


settings = get_settings()
