"""Decision log CRUD + aggregate stats.

Every agent run (all four modes, chat / ingestion / evaluation) writes exactly
one DecisionLog row; this module exposes them.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import DecisionLog
from schemas import DecisionListResponse, DecisionLogRead, DecisionStats

router = APIRouter(prefix="/decisions", tags=["decisions"])


def _apply_filters(stmt: Any, mode: str | None, status: str | None) -> Any:
    if mode:
        stmt = stmt.where(DecisionLog.mode == mode)
    if status:
        stmt = stmt.where(DecisionLog.status == status)
    return stmt


@router.get("", response_model=DecisionListResponse, summary="List decision logs")
async def list_decisions(
    mode: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> DecisionListResponse:
    """Paginated decision logs, newest first; filter by mode and/or status."""
    base = _apply_filters(select(DecisionLog), mode, status)
    rows = (
        (await db.execute(base.order_by(DecisionLog.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    count_stmt = _apply_filters(select(func.count()).select_from(DecisionLog), mode, status)
    total = (await db.execute(count_stmt)).scalar_one()
    return DecisionListResponse(
        items=[DecisionLogRead.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/stats", response_model=list[DecisionStats], summary="Aggregate stats per mode")
async def decision_stats(db: AsyncSession = Depends(get_db)) -> list[DecisionStats]:
    """Run count + average latency / cost / evaluation score, grouped by mode."""
    stmt = select(
        DecisionLog.mode,
        func.count(DecisionLog.id),
        func.avg(DecisionLog.latency_ms),
        func.avg(DecisionLog.cost_usd),
        func.avg(DecisionLog.evaluation_score),
    ).group_by(DecisionLog.mode)
    rows = (await db.execute(stmt)).all()
    return [
        DecisionStats(
            mode=row[0],
            runs=int(row[1]),
            avg_latency_ms=float(row[2]) if row[2] is not None else None,
            avg_cost_usd=float(row[3]) if row[3] is not None else None,
            avg_evaluation_score=float(row[4]) if row[4] is not None else None,
        )
        for row in rows
    ]


@router.get("/{decision_id}", response_model=DecisionLogRead, summary="Get one decision log")
async def get_decision(
    decision_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> DecisionLogRead:
    row = await db.get(DecisionLog, decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"decision {decision_id} not found")
    return DecisionLogRead.model_validate(row)


@router.delete("/{decision_id}", status_code=200, summary="Delete one decision log")
async def delete_decision(
    decision_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    row = await db.get(DecisionLog, decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"decision {decision_id} not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": True, "id": str(decision_id)}
