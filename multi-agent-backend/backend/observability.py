"""Observability: structlog JSON logging, Sentry error capture, Langfuse LLM tracing.

All three are optional at runtime and degrade gracefully:

* structlog always emits (JSON in production / when ``LOG_JSON=true``).
* Sentry activates only when ``SENTRY_DSN`` is set.
* Langfuse records LLM generations only when both Langfuse keys are set and
  never lets a tracing failure break an LLM call.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from config import Settings

logger = structlog.get_logger(__name__)


def setup_logging(settings: Settings) -> None:
    """Configure structlog. JSON renderer in production, pretty console in dev."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_json or settings.is_production:
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.setLevel(level)
    if settings.is_production:
        # keep access logs quiet in prod; they are noise in JSON streams
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def setup_sentry(settings: Settings) -> None:
    """Initialize Sentry when a DSN is configured."""
    if not settings.sentry_dsn:
        logger.info("sentry_disabled_no_dsn")
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.environment,
            traces_sample_rate=0.05,
            send_default_pii=False,
        )
        logger.info("sentry_enabled", environment=settings.environment)
    except Exception as exc:
        logger.warning("sentry_setup_failed", error=str(exc))


_langfuse: Any | None = None
_langfuse_checked = False


def get_langfuse() -> Any | None:
    """Return a cached Langfuse client, or None when not configured."""
    global _langfuse, _langfuse_checked
    if _langfuse_checked:
        return _langfuse
    _langfuse_checked = True
    from config import get_settings

    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse

        _langfuse = Langfuse(
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key,
            host=s.langfuse_host,
        )
        logger.info("langfuse_enabled", host=s.langfuse_host)
    except Exception as exc:
        logger.warning("langfuse_setup_failed", error=str(exc))
        _langfuse = None
    return _langfuse


def record_llm_call(
    *,
    name: str,
    model: str,
    messages: list[dict[str, Any]],
    content: str,
    usage: dict[str, Any],
) -> None:
    """Record one LLM generation in Langfuse. Best effort — never raises."""
    lf = get_langfuse()
    if lf is None:
        return
    try:
        trace = lf.trace(name=name)
        generation = trace.generation(
            name=name,
            model=model,
            input=messages,
            output=content,
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            metadata={"cost_usd": usage.get("cost_usd", 0.0)},
        )
        generation.end()
    except Exception as exc:
        logger.debug("langfuse_record_failed", error=str(exc))
