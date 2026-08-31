"""Async SQLAlchemy 2.0 engine, session factory, and DB utilities.

All PostgreSQL access in the application goes through the ORM (engine +
``AsyncSession``). Raw SQL is only allowed for DuckDB analytical queries —
see ``clients/duckdb_client.py``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from config import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session; commits on success, rolls back on error."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database_connection(
    retries: int | None = None, delay_s: float | None = None
) -> bool:
    """Ping PostgreSQL with retries. Returns True when reachable."""
    retries = retries if retries is not None else settings.db_connect_retries
    delay = delay_s if delay_s is not None else settings.db_retry_delay_s
    for attempt in range(1, retries + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning(
                "database_connect_attempt_failed attempt=%s/%s error=%s",
                attempt,
                retries,
                exc,
            )
            if attempt < retries:
                await asyncio.sleep(delay)
    return False


def _alembic_config() -> AlembicConfig:
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    return cfg


def _upgrade_to_head_sync() -> None:
    command.upgrade(_alembic_config(), "head")


async def run_migrations() -> None:
    """Apply Alembic migrations.

    Runs in a worker thread because the Alembic async template starts its own
    event loop (``asyncio.run``), which is illegal inside the running loop.
    """
    await asyncio.to_thread(_upgrade_to_head_sync)


async def dispose_engine() -> None:
    await engine.dispose()
