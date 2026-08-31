# Multi-agent backend — Python 3.12 + uv, non-root, migrations on boot.
FROM python:3.12-slim-bookworm

# git: the context/ directory is a live git repo (commit SHA audit trail)
# curl: container-level debugging
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

# uv, pinned to the same version that generated uv.lock
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UV_CACHE_DIR=/tmp/uv-cache

WORKDIR /app

# Dependencies first: cached layer keyed on pyproject + lock only
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Application code (context/ is mounted as a volume at runtime)
COPY . .

RUN useradd --create-home appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Migrations are idempotent and run on every boot (alembic upgrade head);
# the lifespan also guards with RUN_MIGRATIONS_ON_START.
CMD ["uv", "run", "--no-sync", "python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
