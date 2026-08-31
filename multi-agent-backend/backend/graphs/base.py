"""Shared LangGraph infrastructure: state schema, graph registry, and runner.

Node functions stay pure — they only read state and return partial updates.
Every database write happens in this runner layer (invoked from route
handlers) or inside LangGraph checkpointing itself:

* ``run_agent_graph``    — runs a graph with a checkpointer, persists the
                            DecisionLog row + ContextSnapshot, publishes a
                            Redis lifecycle event.
* ``resume_agent_graph`` — resumes an interrupted graph (human approval) and
                            updates the existing DecisionLog row.

Graph modules register their builders here via ``register_graph``; no dynamic
imports anywhere.
"""

from __future__ import annotations

import operator
import time
from collections.abc import Callable
from typing import Annotated, Any, TypedDict

import httpx
import structlog
from fastapi import Request
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clients.litellm_client import get_default_litellm_client
from clients.prompt_loader import get_default_prompt_loader
from clients.redis_client import get_redis_client
from config import get_settings
from models import ContextSnapshot, DecisionLog, Signal
from nodes.load_context import ContextBundle, load_context_for_mode

logger = structlog.get_logger(__name__)

GraphStarter = Callable[[], Any]  # returns an uncompiled StateGraph


class GraphState(TypedDict, total=False):
    """Shared state schema for all four agent graphs.

    Modes use the subset of keys they need; ``usage`` accumulates every LLM
    usage record across the run via an ``operator.add`` reducer.
    """

    # --- common ---
    mode: str
    query: str
    input: dict[str, Any]
    context: dict[str, Any]
    context_commit_sha: str
    usage: Annotated[list[dict[str, Any]], operator.add]
    decision_record: dict[str, Any]
    error: str

    # --- analytics ---
    generated_sql: str
    sql_rationale: str
    sql_valid: bool
    sql_validation_errors: list[str]

    # --- monitor ---
    event: dict[str, Any]
    signal_detected: bool
    signal: dict[str, Any]
    classification: dict[str, Any]
    action: str
    action_plan: dict[str, Any]
    approval: dict[str, Any]

    # --- research ---
    brief: str
    generation: int
    max_generations: int
    peers: list[dict[str, Any]]
    evidence_scores: list[dict[str, Any]]
    research_quality: float
    research_ready: bool
    synthesis: str
    next_brief: str

    # --- simulate ---
    personas: list[dict[str, Any]]
    drafts: list[dict[str, Any]]
    reactions: list[dict[str, Any]]
    scores: list[dict[str, Any]]
    winner: dict[str, Any]


# ===========================================================================
# Terminal decision node (shared by all graphs)
# ===========================================================================
def _evaluation_score(state: dict[str, Any]) -> float | None:
    mode = state.get("mode")
    if mode == "analytics":
        return 1.0 if state.get("sql_valid") else 0.0
    if mode == "monitor":
        classification = state.get("classification") or {}
        try:
            return float(classification.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return None
    if mode == "research":
        quality = state.get("research_quality")
        return float(quality) if quality is not None else None
    if mode == "simulate":
        winner = state.get("winner") or {}
        try:
            return float(winner.get("score") or 0.0)
        except (TypeError, ValueError):
            return None
    return None


def _summarize_output(state: dict[str, Any]) -> dict[str, Any]:
    """Compact, mode-specific output summary (never includes the context bundle)."""
    mode = state.get("mode")
    if mode == "analytics":
        return {
            "generated_sql": state.get("generated_sql", ""),
            "sql_valid": state.get("sql_valid", False),
            "sql_validation_errors": state.get("sql_validation_errors", []),
            "sql_rationale": state.get("sql_rationale", ""),
        }
    if mode == "monitor":
        return {
            "signal_detected": state.get("signal_detected", False),
            "signal": state.get("signal"),
            "classification": state.get("classification"),
            "action": state.get("action"),
            "action_plan": state.get("action_plan"),
            "approval": state.get("approval"),
        }
    if mode == "research":
        return {
            "generation": state.get("generation", 0),
            "research_quality": state.get("research_quality"),
            "research_ready": state.get("research_ready", False),
            "synthesis": state.get("synthesis", ""),
            "peer_count": len(state.get("peers") or []),
        }
    if mode == "simulate":
        return {
            "winner": state.get("winner"),
            "variant_scores": state.get("scores", []),
            "reaction_count": len(state.get("reactions") or []),
        }
    return {}


async def log_decision(state: dict[str, Any]) -> dict[str, Any]:
    """Assemble the unified decision record from the final state.

    Terminal node of every graph — pure assembly only; the runner persists
    the record into the DecisionLog table after the graph completes.

    Input state keys:
        mode, input, context_commit_sha, usage, plus the mode-specific result
        keys (generated_sql/sql_valid, classification/action/approval,
        research_quality/synthesis, winner/scores).

    Output state keys:
        decision_record: {mode, context_commit_sha, input, output,
                          evaluation_score, cost_usd, prompt_tokens,
                          completion_tokens}.

    Side-effect guarantees:
        None — no I/O, no database writes.
    """
    usage = state.get("usage") or []
    return {
        "decision_record": {
            "mode": state.get("mode", ""),
            "context_commit_sha": state.get("context_commit_sha", ""),
            "input": state.get("input", {}),
            "output": _summarize_output(state),
            "evaluation_score": _evaluation_score(state),
            "cost_usd": round(sum(u.get("cost_usd", 0.0) for u in usage), 6),
            "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usage),
            "completion_tokens": sum(u.get("completion_tokens", 0) for u in usage),
        }
    }


# ===========================================================================
# Graph registry
# ===========================================================================
_GRAPH_BUILDERS: dict[str, GraphStarter] = {}
_COMPILED: dict[tuple[str, int], Any] = {}


def register_graph(mode: str, builder: GraphStarter) -> None:
    """Register a graph builder under its agent mode (called at import time)."""
    _GRAPH_BUILDERS[mode] = builder


def available_modes() -> list[str]:
    """Agent modes that have registered a compiled graph."""
    return sorted(_GRAPH_BUILDERS)


def get_graph(mode: str, checkpointer: Any) -> Any:
    """Compile (and cache) the graph for ``mode`` bound to a checkpointer."""
    key = (mode, id(checkpointer))
    if key not in _COMPILED:
        try:
            builder = _GRAPH_BUILDERS[mode]
        except KeyError as exc:
            raise ValueError(f"unknown agent mode: {mode!r}") from exc
        _COMPILED[key] = builder().compile(checkpointer=checkpointer)
    return _COMPILED[key]


def make_checkpoint_config(thread_id: str, deps: dict[str, Any] | None = None) -> RunnableConfig:
    """LangGraph config: checkpoint thread + injectable node dependencies."""
    configurable: dict[str, Any] = {"thread_id": thread_id}
    if deps:
        configurable.update(deps)
    return {"configurable": configurable}


def default_agent_deps() -> dict[str, Any]:
    """Default node dependencies (LiteLLM client + prompt loader singletons)."""
    return {
        "litellm_client": get_default_litellm_client(),
        "prompt_loader": get_default_prompt_loader(),
    }


def get_agent_deps(request: Request) -> dict[str, Any]:
    """FastAPI dependency: default deps merged with app.state.agent_deps overrides."""
    overrides = getattr(request.app.state, "agent_deps", {}) or {}
    return {**default_agent_deps(), **overrides}


# ===========================================================================
# Runner
# ===========================================================================
class AgentRunResult(BaseModel):
    """Outcome of one agent graph run (or resume)."""

    mode: str
    thread_id: str
    status: str  # succeeded | failed | pending_approval
    input_payload: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    decision_record: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    latency_ms: int = 0
    context_commit_sha: str = ""
    error: str | None = None


def _extract_interrupt(snapshot: Any) -> dict[str, Any] | None:
    """Pull the interrupt payload out of a pending graph snapshot, if any."""
    for task in getattr(snapshot, "tasks", None) or ():
        for intr in getattr(task, "interrupts", None) or ():
            value = getattr(intr, "value", None)
            if isinstance(value, dict):
                return value
            if value is not None:
                return {"value": value}
    return None


async def _publish_event(result: AgentRunResult) -> None:
    await get_redis_client().publish(
        "agent.events",
        {
            "event": "agent.run.finished",
            "mode": result.mode,
            "thread_id": result.thread_id,
            "status": result.status,
            "latency_ms": result.latency_ms,
        },
    )


async def notify_n8n(payload: dict[str, Any]) -> bool:
    """Fire an n8n webhook (approval requests / action notifications). Best effort."""
    url = get_settings().n8n_webhook_url
    if not url:
        logger.debug("n8n_webhook_disabled")
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload)
            ok = response.status_code < 400
            logger.info("n8n_notified", url=url, status_code=response.status_code, ok=ok)
            return ok
    except httpx.HTTPError as exc:
        logger.warning("n8n_notify_failed", url=url, error=str(exc))
        return False


async def ensure_context_snapshot(db: AsyncSession, bundle: ContextBundle | dict[str, Any]) -> None:
    """Insert a ContextSnapshot the first time a context commit is seen."""
    if isinstance(bundle, dict):
        bundle = ContextBundle.model_validate(bundle)
    existing = await db.scalar(
        select(ContextSnapshot).where(ContextSnapshot.commit_sha == bundle.commit_sha).limit(1)
    )
    if existing is None:
        db.add(
            ContextSnapshot(
                commit_sha=bundle.commit_sha,
                manifest=bundle.manifest,
                file_count=len(bundle.manifest),
            )
        )


async def persist_decision_log(
    db: AsyncSession, result: AgentRunResult, record: dict[str, Any] | None
) -> DecisionLog:
    """Add a DecisionLog row for a run. The caller owns the commit."""
    record = record or {}
    row = DecisionLog(
        mode=result.mode,
        status=result.status,
        context_commit_sha=result.context_commit_sha or "unknown",
        thread_id=result.thread_id,
        input=result.input_payload,
        output=record.get("output") or result.output,
        evaluation_score=record.get("evaluation_score"),
        latency_ms=result.latency_ms,
        cost_usd=record.get("cost_usd", 0.0),
        prompt_tokens=record.get("prompt_tokens", 0),
        completion_tokens=record.get("completion_tokens", 0),
        error=result.error,
    )
    db.add(row)
    return row


def _initial_state(
    mode: str, input_payload: dict[str, Any], bundle: ContextBundle | None
) -> dict[str, Any]:
    """Seed state; analytics loads its own context in its first graph node."""
    state: dict[str, Any] = {
        "mode": mode,
        "query": str(input_payload.get("query", "")),
        "input": input_payload,
        "usage": [],
    }
    if bundle is not None:
        state["context"] = bundle.model_dump()
        state["context_commit_sha"] = bundle.commit_sha
    if mode == "monitor":
        state["event"] = input_payload.get("event") or {"description": state["query"]}
    if mode == "research":
        state["brief"] = state["query"]
        state["generation"] = 0
        rules = (bundle.rules if bundle else {}).get("research", {})  # type: ignore[union-attr]
        state["max_generations"] = int(rules.get("max_generations", 2))
    return state


async def run_agent_graph(
    mode: str,
    *,
    input_payload: dict[str, Any],
    thread_id: str,
    checkpointer: Any,
    deps: dict[str, Any] | None = None,
    db: AsyncSession | None = None,
) -> AgentRunResult:
    """Run one agent graph end-to-end with checkpointing + decision logging.

    The checkpointer persists intermediate state to PostgreSQL; after the run
    this function writes the DecisionLog row (and the ContextSnapshot on first
    sight of a context commit). Interrupts (human approval) surface as
    ``status='pending_approval'`` with the interrupt payload in ``approval``.
    """
    started = time.perf_counter()
    bundle: ContextBundle | None = None
    if mode != "analytics":
        bundle = await load_context_for_mode(mode)

    state = _initial_state(mode, input_payload, bundle)
    config = make_checkpoint_config(thread_id, deps)
    graph = get_graph(mode, checkpointer)

    values: dict[str, Any] = {}
    approval: dict[str, Any] | None = None
    status, error = "succeeded", None
    try:
        values = dict(await graph.ainvoke(state, config))
        snapshot = await graph.aget_state(config)
        if snapshot.next:
            approval = _extract_interrupt(snapshot)
            approval = approval or {}
            approval.setdefault("thread_id", thread_id)
            status = "pending_approval"
            logger.info("agent_run_awaiting_approval", mode=mode, thread_id=thread_id)
    except Exception as exc:
        logger.exception("agent_graph_failed", mode=mode, thread_id=thread_id)
        status, error, values = "failed", f"{type(exc).__name__}: {exc}", {}

    latency_ms = int((time.perf_counter() - started) * 1000)
    values_context = values.pop("context", None)
    values.pop("usage", None)
    record = values.pop("decision_record", None)
    sha = values.get("context_commit_sha") or state.get("context_commit_sha") or ""

    result = AgentRunResult(
        mode=mode,
        thread_id=thread_id,
        status=status,
        input_payload=input_payload,
        output=values,
        decision_record=record,
        approval=approval,
        latency_ms=latency_ms,
        context_commit_sha=sha,
        error=error,
    )

    if db is not None:
        try:
            snapshot_bundle = bundle or values_context
            if snapshot_bundle is not None:
                await ensure_context_snapshot(db, snapshot_bundle)
            await persist_decision_log(db, result, record)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("decision_log_persist_failed", mode=mode, thread_id=thread_id)

    logger.info(
        "agent_run_finished",
        mode=mode,
        thread_id=thread_id,
        status=status,
        latency_ms=latency_ms,
    )
    await _publish_event(result)
    return result


async def resume_agent_graph(
    mode: str,
    *,
    thread_id: str,
    resume_payload: dict[str, Any],
    checkpointer: Any,
    deps: dict[str, Any] | None = None,
    db: AsyncSession | None = None,
) -> AgentRunResult:
    """Resume an interrupted graph (human approval) with Command(resume=...).

    Updates the EXISTING DecisionLog row for the thread instead of inserting
    a new one, so one approval cycle yields exactly one audit record.
    """
    started = time.perf_counter()
    config = make_checkpoint_config(thread_id, deps)
    graph = get_graph(mode, checkpointer)
    from langgraph.types import Command

    values: dict[str, Any] = {}
    status, error = "succeeded", None
    try:
        values = dict(await graph.ainvoke(Command(resume=resume_payload), config))
    except Exception as exc:
        logger.exception("agent_resume_failed", mode=mode, thread_id=thread_id)
        status, error, values = "failed", f"{type(exc).__name__}: {exc}", {}

    latency_ms = int((time.perf_counter() - started) * 1000)
    values.pop("context", None)
    values.pop("usage", None)
    record = values.pop("decision_record", None)

    if db is not None:
        try:
            row = await db.scalar(
                select(DecisionLog)
                .where(DecisionLog.thread_id == thread_id)
                .order_by(DecisionLog.created_at.desc())
                .limit(1)
            )
            if row is not None:
                row.status = status
                row.output = (record or {}).get("output") or values
                row.evaluation_score = (record or {}).get("evaluation_score")
                row.error = error
            else:
                await persist_decision_log(
                    db,
                    AgentRunResult(
                        mode=mode,
                        thread_id=thread_id,
                        status=status,
                        input_payload={},
                        output=values,
                        decision_record=record,
                        latency_ms=latency_ms,
                        error=error,
                    ),
                    record,
                )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("decision_log_update_failed", mode=mode, thread_id=thread_id)

    result = AgentRunResult(
        mode=mode,
        thread_id=thread_id,
        status=status,
        input_payload={},
        output=values,
        decision_record=record,
        latency_ms=latency_ms,
        error=error,
    )
    await _publish_event(result)
    return result


# ===========================================================================
# Monitor pipeline helper (chat route + signal ingestion share this)
# ===========================================================================
def _sync_signal(signal: Signal, result: AgentRunResult) -> None:
    """Mirror a monitor run's outcome onto its Signal row."""
    output = result.output
    classification = output.get("classification") or {}
    signal.kind = classification.get("kind") or signal.kind
    signal.severity = classification.get("severity") or signal.severity
    signal.classification = classification or None
    signal.recommended_action = output.get("action_plan") or signal.recommended_action
    signal.thread_id = result.thread_id

    if result.status == "pending_approval":
        signal.status = "pending_approval"
    elif result.status == "failed":
        signal.status = "failed"
    elif output.get("action") == "ignore" or not output.get("signal_detected", True):
        signal.status = "ignored"
    else:
        approval = output.get("approval") or {}
        signal.status = "executed" if approval.get("approved", False) else "dismissed"


async def run_monitor_for_signal(
    db: AsyncSession | None,
    signal: Signal,
    event: dict[str, Any],
    thread_id: str,
    checkpointer: Any,
    deps: dict[str, Any] | None = None,
) -> AgentRunResult:
    """Run the monitor graph for a Signal row and keep the row in sync.

    On ``require_approval`` the interrupt payload is enriched with the signal
    id, thread id, and the callback URL, then forwarded to the n8n approval
    webhook (when configured). The n8n workflow ultimately POSTs back to
    POST /v1/signals/{id}/approve, which resumes the graph.
    """
    settings = get_settings()
    input_payload = {
        "query": str(event.get("description") or event.get("metric") or "monitor event"),
        "event": event,
        "signal_id": str(signal.id),
        "source": signal.source,
    }
    result = await run_agent_graph(
        "monitor",
        input_payload=input_payload,
        thread_id=thread_id,
        checkpointer=checkpointer,
        deps=deps,
        db=db,
    )
    _sync_signal(signal, result)

    if result.status == "pending_approval" and result.approval is not None:
        result.approval.update(
            {
                "signal_id": str(signal.id),
                "thread_id": thread_id,
                "callback_url": f"{settings.public_base_url}/v1/signals/{signal.id}/approve",
            }
        )
        await notify_n8n({"event": "approval.required", **result.approval})
    elif result.status == "succeeded":
        approval = result.output.get("approval") or {}
        if approval.get("approved"):
            await notify_n8n(
                {
                    "event": "action.execute",
                    "signal_id": str(signal.id),
                    "thread_id": thread_id,
                    "action_plan": result.output.get("action_plan"),
                }
            )
    return result
