"""SQLAlchemy 2.0 declarative models (async ORM, PostgreSQL).

Three tables own all persistent agent state:

* ``DecisionLog``     — unified audit record for EVERY agent run (all modes).
* ``Signal``          — monitor-mode events and their lifecycle.
* ``ContextSnapshot`` — manifest of the git-versioned context/ directory,
                        keyed by commit SHA, so every decision is reproducible.

LangGraph checkpoint tables are created by the checkpointer itself
(``AsyncPostgresSaver.setup()``) and are therefore not modelled here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Float, Integer, Numeric, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class DecisionLog(Base):
    """Unified audit record for every agent run (analytics/monitor/research/simulate).

    Status lifecycle: running -> succeeded | failed | pending_approval
    (pending_approval resumes to succeeded once a human approves via n8n).
    """

    __tablename__ = "decision_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running", index=True)
    context_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    evaluation_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 8), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class Signal(Base):
    """A monitor-mode event and its lifecycle.

    Status lifecycle: new -> queued -> processing -> pending_approval -> executed | dismissed
    (or -> ignored / failed).
    """

    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="new", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    classification: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    recommended_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ContextSnapshot(Base):
    """Manifest of the git-versioned context/ directory for a given commit SHA."""

    __tablename__ = "context_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
