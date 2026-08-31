"""FastAPI application factory and lifespan manager.

Startup sequence (lifespan):
1. verify PostgreSQL (retry loop) and apply Alembic migrations,
2. bind the LangGraph checkpointer (AsyncPostgresSaver; MemorySaver fallback
   in development when Postgres is unavailable),
3. seed embedded DuckDB from the git-versioned context schema,
4. probe Redis + LiteLLM (non-fatal warnings),
5. record a ContextSnapshot for the current context commit.

Development mode is fault-tolerant (degraded, still serving); production
fails fast on database problems.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

# Import graph modules so they register their builders (static imports only).
import graphs.analytics_graph
import graphs.monitor_graph
import graphs.research_graph
import graphs.simulate_graph  # noqa: F401
from clients.duckdb_client import get_duckdb_client
from clients.git_context import GitContextClient
from clients.litellm_client import get_default_litellm_client
from clients.redis_client import get_redis_client
from config import get_settings
from database import AsyncSessionLocal, check_database_connection, dispose_engine, run_migrations
from models import ContextSnapshot
from observability import setup_logging, setup_sentry
from routes import chat, decisions, models, signals
from routes import eval as eval_route

logger = structlog.get_logger(__name__)

_started_at = time.time()
_bearer = HTTPBearer(auto_error=False)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
async def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Optional bearer-token auth: enabled when BACKEND_API_KEYS is non-empty."""
    keys = get_settings().api_keys
    if not keys:
        return
    if credentials is None or credentials.credentials not in keys:
        raise HTTPException(status_code=401, detail="invalid or missing API key")


# --------------------------------------------------------------------------
# middleware
# --------------------------------------------------------------------------
async def _request_context(request: Request, call_next: Any) -> Any:
    """Bind a request id + path to every structured log line in the request."""
    structlog.contextvars.bind_contextvars(request_id=str(uuid.uuid4()), path=request.url.path)
    try:
        return await call_next(request)
    finally:
        structlog.contextvars.unbind_contextvars("request_id", "path")


def _install_middleware(app: FastAPI) -> None:
    app.middleware("http")(_request_context)


# --------------------------------------------------------------------------
# lifespan
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    t0 = time.perf_counter()
    logger.info("startup_begin", environment=settings.environment)

    # 1. database + migrations
    logger.info("step_database_check_starting")
    db_ok = await check_database_connection()
    logger.info("step_database_check_complete", db_ok=db_ok)
    try:
        if db_ok:
            if settings.run_migrations_on_start:
                logger.info("migrations_starting")
                await run_migrations()
                logger.info("migrations_applied")
        elif settings.is_production:
            raise RuntimeError("PostgreSQL is unreachable — refusing to start in production")
        else:
            logger.warning("database_unavailable_degraded_mode")
        logger.info("database_step_complete")
    except Exception as e:
        logger.error("database_step_failed", error=str(e), exc_info=True)
        if settings.is_production:
            raise

    # 2. LangGraph checkpointer (PostgreSQL checkpoints; fallback MemorySaver)
    logger.info("step_checkpointer_starting")
    app.state.checkpointer_cm: Any | None = None
    app.state.checkpointer: Any | None = None
    if db_ok:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            cm = AsyncPostgresSaver.from_conn_string(settings.checkpointer_dsn)
            saver = await cm.__aenter__()
            await saver.setup()  # idempotent: creates checkpoint tables once
            app.state.checkpointer_cm = cm
            app.state.checkpointer = saver
            logger.info("checkpointer_postgres_ready")
        except Exception as exc:
            logger.warning("checkpointer_postgres_failed", error=str(exc))
    if app.state.checkpointer is None:
        if settings.is_production:
            raise RuntimeError("LangGraph Postgres checkpointer failed to initialize")
        from langgraph.checkpoint.memory import MemorySaver

        app.state.checkpointer = MemorySaver()
        logger.warning("checkpointer_memory_fallback")
    logger.info("step_checkpointer_complete")

    # 3. embedded DuckDB: schema + deterministic demo data from context/
    logger.info("step_duckdb_starting")
    try:
        await get_duckdb_client().seed_from_context(settings.context_path)
    except Exception as exc:
        logger.warning("duckdb_seed_failed", error=str(exc))
    logger.info("step_duckdb_complete")

    # 4. redis + litellm probes (non-fatal)
    logger.info("step_redis_check_starting")
    redis_ok = await get_redis_client().ping()
    logger.info("step_redis_check_complete", redis_ok=redis_ok)
    logger.info("step_litellm_check_starting")
    litellm_ok = await get_default_litellm_client().health()
    logger.info("step_litellm_check_complete", litellm_ok=litellm_ok)
    logger.info(
        "dependencies",
        database=db_ok,
        redis=redis_ok,
        litellm=litellm_ok,
    )

    # 5. context snapshot for the current commit (skipped in development to accelerate startup)
    if db_ok and settings.is_production:
        try:
            async with AsyncSessionLocal() as session:
                manifest = await GitContextClient().amanifest()
                existing = await session.scalar(
                    select(ContextSnapshot)
                    .where(ContextSnapshot.commit_sha == manifest.commit_sha)
                    .limit(1)
                )
                if existing is None:
                    session.add(
                        ContextSnapshot(
                            commit_sha=manifest.commit_sha,
                            manifest=manifest.files,
                            file_count=manifest.file_count,
                        )
                    )
                await session.commit()
                logger.info(
                    "context_snapshot_ready", commit_sha=manifest.commit_sha, source=manifest.source
                )
        except Exception as exc:
            logger.warning("context_snapshot_failed", error=str(exc))

    logger.info("startup_complete", seconds=round(time.perf_counter() - t0, 2))
    yield

    # shutdown
    logger.info("shutdown_begin")
    with contextlib.suppress(Exception):
        get_duckdb_client().close()
    await get_redis_client().close()
    await get_default_litellm_client().aclose()
    if app.state.checkpointer_cm is not None:
        await app.state.checkpointer_cm.__aexit__(None, None, None)
    await dispose_engine()
    logger.info("shutdown_complete")


# --------------------------------------------------------------------------
# error handlers (OpenAI-style error bodies)
# --------------------------------------------------------------------------
def _error_body(message: str, error_type: str, code: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "code": code}}


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> Any:
        return await _json_response(
            exc.status_code, _error_body(str(exc.detail), "api_error", str(exc.status_code))
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> Any:
        message = "; ".join(
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        return await _json_response(422, _error_body(message, "invalid_request_error"))

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> Any:
        logger.exception("unhandled_exception", path=request.url.path)
        try:
            import sentry_sdk

            sentry_sdk.capture_exception(exc)
        except Exception:
            pass
        detail = (
            f"{type(exc).__name__}: {exc}"
            if not app.state.settings.is_production
            else "internal server error"
        )
        return await _json_response(500, _error_body(detail, "internal_error"))


async def _json_response(status_code: int, body: dict[str, Any]) -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content=body)


# --------------------------------------------------------------------------
# app factory
# --------------------------------------------------------------------------
def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings)
    setup_sentry(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Multi-agent LLM backend (LangGraph state machines) with an "
            "OpenAI-compatible API for Open WebUI."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.is_production else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    _install_middleware(app)
    _install_error_handlers(app)

    auth = [Depends(verify_api_key)]
    app.include_router(models.router, prefix=settings.api_prefix, dependencies=auth)
    app.include_router(chat.router, prefix=settings.api_prefix, dependencies=auth)
    app.include_router(decisions.router, prefix=settings.api_prefix, dependencies=auth)
    app.include_router(signals.router, prefix=settings.api_prefix, dependencies=auth)
    app.include_router(eval_route.router, prefix=settings.api_prefix, dependencies=auth)

    @app.get("/", tags=["health"], summary="Service info")
    async def root() -> dict[str, Any]:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "health": "/health",
            "models": f"{settings.api_prefix}/models",
        }

    @app.get("/health", tags=["health"], summary="Liveness probe")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": settings.app_version,
            "uptime_seconds": round(time.time() - _started_at, 1),
        }

    @app.get("/ready", tags=["health"], summary="Readiness probe")
    async def ready() -> Any:
        from fastapi.responses import JSONResponse

        db_ok = await check_database_connection(retries=1, delay_s=0.1)
        redis_ok = await get_redis_client().ping()
        litellm_ok = await get_default_litellm_client().health()
        components = {"postgres": db_ok, "redis": redis_ok, "litellm": litellm_ok}
        healthy = db_ok and litellm_ok
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ready" if healthy else "degraded", "components": components},
        )

    return app


app = create_app()
