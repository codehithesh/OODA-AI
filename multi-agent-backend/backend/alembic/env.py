"""Alembic environment — async engine, driven by backend config.

Offline mode renders SQL without a database (``alembic upgrade head --sql``);
online mode runs migrations against the DATABASE_URL from Settings. env.py
intentionally does not call fileConfig() so the application's structlog
configuration stays intact when migrations run in-process at startup.
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# make backend/ importable regardless of the working directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings
from models import Base

config = context.config

if config.config_file_name is not None and not config.attributes.get("configure_logger", False):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    """psycopg-style URL for offline rendering."""
    return get_settings().database_url.replace("+asyncpg", "")


def run_migrations_offline() -> None:
    """Render migrations to SQL without a live connection."""
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations with an async engine (NullPool — never shared with the app)."""
    connectable = async_engine_from_config(
        {"sqlalchemy.url": _sync_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
