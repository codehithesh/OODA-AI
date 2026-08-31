"""Signal CRUD + ingestion + the human approval callback.

Ingesting a signal (POST /v1/signals) triggers the monitor graph as a FastAPI
BackgroundTask. When the graph pauses on ``require_approval``, n8n receives a
webhook payload whose ``callback_url`` points at POST /v1/signals/{id}/approve;
that endpoint resumes the interrupted LangGraph run with Command(resume=...).
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal, get_db
from graphs.base import get_agent_deps, notify_n8n, resume_agent_graph, run_monitor_for_signal
from models import Signal
from schemas import ApprovalDecision, SignalCreate, SignalListResponse, SignalRead

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])


async def _process_signal_background(
    signal_id: str,
    event: dict[str, Any],
    thread_id: str,
    checkpointer: Any,
    deps: dict[str, Any],
) -> None:
    """Background worker: run the monitor graph for an ingested signal.

    Opens its own session; any failure is logged and never surfaces to the
    ingestion caller (the response has already been sent).
    """
    try:
        async with AsyncSessionLocal() as session:
            signal = await session.get(Signal, uuid.UUID(signal_id))
            if signal is None:
                logger.warning("background_signal_missing", signal_id=signal_id)
                return
            await run_monitor_for_signal(session, signal, event, thread_id, checkpointer, deps=deps)
            await session.commit()
    except Exception:
        logger.exception("signal_processing_failed", signal_id=signal_id)


@router.post("", status_code=202, response_model=SignalRead, summary="Ingest a monitor event")
async def ingest_signal(
    payload: SignalCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    deps: dict[str, Any] = Depends(get_agent_deps),
) -> SignalRead:
    """Create a Signal and run the monitor graph for it in the background."""
    signal = Signal(source=payload.source, payload=payload.payload, status="queued")
    db.add(signal)
    await db.flush()
    await db.commit()
    thread_id = f"monitor-{signal.id}"
    background_tasks.add_task(
        _process_signal_background,
        str(signal.id),
        payload.payload,
        thread_id,
        request.app.state.checkpointer,
        deps,
    )
    return SignalRead.model_validate(signal)


@router.get("", response_model=SignalListResponse, summary="List signals")
async def list_signals(
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> SignalListResponse:
    stmt = select(Signal)
    if status:
        stmt = stmt.where(Signal.status == status)
    if kind:
        stmt = stmt.where(Signal.kind == kind)
    rows = (
        (await db.execute(stmt.order_by(Signal.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    count = select(func.count()).select_from(Signal)
    if status:
        count = count.where(Signal.status == status)
    if kind:
        count = count.where(Signal.kind == kind)
    total = (await db.execute(count)).scalar_one()
    return SignalListResponse(
        items=[SignalRead.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/{signal_id}", response_model=SignalRead, summary="Get one signal")
async def get_signal(signal_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> SignalRead:
    signal = await db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")
    return SignalRead.model_validate(signal)


@router.post(
    "/{signal_id}/approve",
    response_model=SignalRead,
    summary="Human approval callback (n8n webhook target)",
)
async def approve_signal(
    signal_id: uuid.UUID,
    decision: ApprovalDecision,
    request: Request,
    db: AsyncSession = Depends(get_db),
    deps: dict[str, Any] = Depends(get_agent_deps),
) -> SignalRead:
    """Resume a paused monitor graph with the human decision.

    This is the endpoint n8n's approval workflow POSTs back to. It resumes the
    interrupted LangGraph run, updates the Signal lifecycle, and forwards the
    action plan to the n8n execution webhook when approved.
    """
    signal = await db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")
    if signal.status != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail=f"signal {signal_id} is '{signal.status}', not pending_approval",
        )
    if not signal.thread_id:
        raise HTTPException(status_code=409, detail="signal has no graph thread to resume")

    result = await resume_agent_graph(
        "monitor",
        thread_id=signal.thread_id,
        resume_payload=decision.model_dump(),
        checkpointer=request.app.state.checkpointer,
        deps=deps,
        db=db,
    )

    approval = result.output.get("approval") or {}
    signal.status = "executed" if approval.get("approved", False) else "dismissed"
    await db.commit()

    if approval.get("approved", False):
        await notify_n8n(
            {
                "event": "action.execute",
                "signal_id": str(signal.id),
                "thread_id": signal.thread_id,
                "action_plan": result.output.get("action_plan"),
                "approver": decision.approver,
            }
        )
    logger.info(
        "signal_approval_resolved",
        signal_id=str(signal.id),
        approved=approval.get("approved", False),
    )
    return SignalRead.model_validate(signal)
