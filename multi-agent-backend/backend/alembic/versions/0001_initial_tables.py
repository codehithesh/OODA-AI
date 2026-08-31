"""create initial tables: decision_logs, signals, context_snapshots

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("context_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=True),
        sa.Column("input", postgresql.JSONB(), nullable=True),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("evaluation_score", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 8), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_decision_logs_mode", "decision_logs", ["mode"])
    op.create_index("ix_decision_logs_status", "decision_logs", ["status"])
    op.create_index("ix_decision_logs_context_commit_sha", "decision_logs", ["context_commit_sha"])
    op.create_index("ix_decision_logs_thread_id", "decision_logs", ["thread_id"])
    op.create_index("ix_decision_logs_created_at", "decision_logs", ["created_at"])

    op.create_table(
        "signals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("classification", postgresql.JSONB(), nullable=True),
        sa.Column("recommended_action", postgresql.JSONB(), nullable=True),
        sa.Column("thread_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_signals_status", "signals", ["status"])
    op.create_index("ix_signals_kind", "signals", ["kind"])
    op.create_index("ix_signals_thread_id", "signals", ["thread_id"])
    op.create_index("ix_signals_created_at", "signals", ["created_at"])

    op.create_table(
        "context_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_context_snapshots_commit_sha", "context_snapshots", ["commit_sha"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_context_snapshots_commit_sha", table_name="context_snapshots")
    op.drop_table("context_snapshots")
    op.drop_index("ix_signals_created_at", table_name="signals")
    op.drop_index("ix_signals_thread_id", table_name="signals")
    op.drop_index("ix_signals_kind", table_name="signals")
    op.drop_index("ix_signals_status", table_name="signals")
    op.drop_table("signals")
    op.drop_index("ix_decision_logs_created_at", table_name="decision_logs")
    op.drop_index("ix_decision_logs_thread_id", table_name="decision_logs")
    op.drop_index("ix_decision_logs_context_commit_sha", table_name="decision_logs")
    op.drop_index("ix_decision_logs_status", table_name="decision_logs")
    op.drop_index("ix_decision_logs_mode", table_name="decision_logs")
    op.drop_table("decision_logs")
