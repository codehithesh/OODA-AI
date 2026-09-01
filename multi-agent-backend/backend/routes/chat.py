"""OpenAI-compatible chat completions — the entry point Open WebUI uses.

POST /v1/chat/completions routes the request to the correct LangGraph mode,
runs it with PostgreSQL checkpointing + decision logging, optionally executes
validated analytics SQL against embedded DuckDB, and returns either a JSON
completion or an SSE stream of OpenAI-style chunks.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from clients.duckdb_client import get_duckdb_client
from config import get_settings
from database import get_db
from graphs.base import (
    AgentRunResult,
    available_modes,
    get_agent_deps,
    run_agent_graph,
    run_monitor_for_signal,
)
from models import Signal
from schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    CompletionUsage,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------
# request parsing
# --------------------------------------------------------------------------
def _message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


def _extract_query_and_event(messages: list[ChatMessage]) -> tuple[str, dict[str, Any] | None]:
    """Last user message is the query; a JSON user/system message may carry an event."""
    event: dict[str, Any] | None = None
    query = ""
    for message in messages:
        text = message.text
        if message.role == "user" and text:
            query = text
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and ("event" in data or "metric" in data or "input" in data):
            event = data.get("event") or data.get("input") or data
    return query, event


def resolve_mode(model: str) -> str:
    """Map an OpenAI-style model id to an agent mode."""
    mode = model.strip().lower().split("/")[-1]
    if mode not in available_modes():
        raise HTTPException(
            status_code=404,
            detail=f"model '{model}' not found; available models: {', '.join(available_modes())}",
        )
    return mode


# --------------------------------------------------------------------------
# response formatting
# --------------------------------------------------------------------------
def _markdown_table(columns: list[str], rows: list[dict[str, Any]], limit: int = 10) -> list[str]:
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows[:limit]:
        values = [str(row.get(c, "")) for c in columns]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def _format_analytics(output: dict[str, Any], executed: dict[str, Any] | None) -> str:
    valid = output.get("sql_valid")
    errors = output.get("sql_validation_errors") or []
    status = "passed" if valid else "failed — " + "; ".join(errors)
    lines = ["### Analytics agent", "", f"SQL validation: **{status}**", "", "```sql"]
    lines.append(output.get("generated_sql") or "-- no SQL generated")
    lines += ["```"]
    rationale = output.get("sql_rationale")
    if rationale:
        lines += ["", f"> {rationale[:400]}"]
    if executed is not None:
        if executed.get("error"):
            lines += ["", f"Execution failed: `{executed['error']}`"]
        else:
            columns = executed.get("columns") or []
            rows = executed.get("rows") or []
            lines += ["", f"Executed on DuckDB ({executed.get('row_count', 0)} rows):", ""]
            if columns:
                lines += _markdown_table(columns, rows)
    return "\n".join(lines)


def _format_monitor(output: dict[str, Any], approval: dict[str, Any] | None) -> str:
    lines = ["### Monitor agent", ""]
    if not output.get("signal_detected", False):
        return "\n".join([*lines, "No signal detected — the event did not breach any rule."])
    classification = output.get("classification") or {}
    lines += [
        f"- **Kind:** {classification.get('kind', 'n/a')}",
        f"- **Severity:** {classification.get('severity', 'n/a')}",
        f"- **Summary:** {classification.get('summary', 'n/a')}",
        f"- **Confidence:** {classification.get('confidence', 'n/a')}",
        f"- **Action:** {output.get('action', 'n/a')}",
    ]
    action_plan = output.get("action_plan") or {}
    if action_plan:
        lines += [
            f"- **Plan:** {action_plan.get('type', '')} → {action_plan.get('channel', '')}".rstrip(
                " →"
            ),
        ]
    approval_state = output.get("approval") or (approval or {})
    if approval_state:
        lines += ["", f"Approval: **{approval_state}**"]
    if approval_state.get("required") and not approval_state.get("approved"):
        lines += [
            "",
            "_A human approval is pending — n8n has been notified. "
            "Approve via `POST /v1/signals/{signal_id}/approve`._",
        ]
    return "\n".join(lines)


def _format_research(output: dict[str, Any]) -> str:
    lines = [
        "### Research agent",
        "",
        output.get("synthesis") or "_no synthesis produced_",
        "",
        "---",
        f"generation: {output.get('generation', 0)} · "
        f"evidence quality: {output.get('research_quality', 0.0)} · "
        f"peers: {output.get('peer_count', 0)}",
    ]
    return "\n".join(lines)


def _format_simulate(output: dict[str, Any]) -> str:
    winner = output.get("winner") or {}
    lines = ["### Simulate agent", "", "#### Winning draft", ""]
    lines.append(winner.get("text") or "_no winner selected_")
    lines += [
        "",
        "---",
        f"winner: `{winner.get('variant_id', '-')}` (score {winner.get('score', 0.0)}) · "
        f"reactions: {output.get('reaction_count', 0)}",
        "",
        "| variant | score | support | oppose | neutral |",
        "|---|---|---|---|---|",
    ]
    for score in output.get("variant_scores") or []:
        lines.append(
            f"| {score.get('variant_id')} | {score.get('score')} | {score.get('support')} "
            f"| {score.get('oppose')} | {score.get('neutral')} |"
        )
    return "\n".join(lines)


def _format_pending_approval(result: AgentRunResult) -> str:
    """Render a monitor run that is paused on the human approval gate."""
    approval = result.approval or {}
    classification = result.output.get("classification") or {}
    action_plan = result.output.get("action_plan") or {}
    return "\n".join(
        [
            "### Monitor agent — approval required",
            "",
            f"- **Kind:** {classification.get('kind', 'n/a')}",
            f"- **Severity:** {classification.get('severity', 'n/a')}",
            f"- **Summary:** {classification.get('summary', 'n/a')}",
            f"- **Proposed action:** {result.output.get('action', 'n/a')}"
            f" ({action_plan.get('type', '')} → {action_plan.get('channel', '')})".rstrip(" →"),
            "",
            "_A human approval is **pending** — n8n has been notified and the graph is paused._",
            "",
            f"- signal_id: `{approval.get('signal_id')}`",
            f'- resume by POSTing to `{approval.get("callback_url")}` with `{{"approved": true}}`',
        ]
    )


def _format_eda(output: dict[str, Any]) -> str:
    """Format the EDA analysis result as rich markdown for chat.

    Charts are rendered as self-contained inline HTML using Plotly.js so
    Open WebUI displays them directly in the conversation.
    """
    lines: list[str] = []

    question = output.get("business_question", "")
    plan = output.get("analysis_plan", "")
    metrics = output.get("metrics") or {}
    findings = output.get("findings") or []
    recommendations = output.get("recommendations") or []
    visualizations = output.get("visualizations") or []
    fused = output.get("fused_context")
    failure_modes = output.get("failure_modes") or []

    # ── Header ───────────────────────────────────────────────────────────────
    lines += ["## 📊 EDA Results", ""]
    if question:
        lines += [f"**Question:** {question}", ""]
    if plan:
        lines += [f"**Plan:** {plan[:400]}", ""]

    # ── Run metrics ──────────────────────────────────────────────────────────
    if metrics:
        tok = metrics.get("total_tokens", 0)
        cost = metrics.get("total_cost_usd", 0.0)
        ms = metrics.get("total_latency_ms", 0)
        iters = metrics.get("total_iterations", 0)
        queries = metrics.get("total_sql_queries", 0)
        searches = metrics.get("total_web_searches", 0)
        calls = metrics.get("total_tool_calls", 0)
        search_part = f" · 🌐 `{searches} searches`" if searches else ""
        lines += [
            f"> ⚡ `{ms}ms` · 🔁 `{iters} iterations` · 🗄 `{queries} queries`"
            f" · 🛠 `{calls} tool calls`{search_part}"
            f" · 🪙 `{tok:,} tokens` · 💰 `${cost:.4f}`",
            "",
        ]

    # ── Visualizations (inline Plotly HTML) ──────────────────────────────────
    if visualizations:
        lines += ["---", "### 📈 Visualizations", ""]
        # Load Plotly once for the whole response
        lines += [
            "<script src='https://cdn.plot.ly/plotly-2.35.2.min.js'></script>",
            "",
        ]
        for viz in visualizations:
            spec_str = viz.get("plotly_spec", "")
            title = viz.get("title", "Chart")
            desc = viz.get("description", "")
            chart_id = f"chart-{viz.get('id', 'x')}"

            if desc:
                lines += [f"**{title}** — {desc}", ""]
            else:
                lines += [f"**{title}**", ""]

            if spec_str and spec_str != "{}":
                lines += [
                    f"<div id='{chart_id}' style='width:100%;height:420px;margin-bottom:1rem'></div>",
                    f"<script>Plotly.newPlot('{chart_id}', {spec_str});</script>",
                    "",
                ]
            else:
                lines += ["*No chart data available*", ""]

    # ── Findings ─────────────────────────────────────────────────────────────
    if findings:
        lines += ["---", "### 🔍 Findings", ""]
        for f in findings:
            conf = f.get("confidence", 0.5)
            et = f.get("evidence_type", "internal").upper()
            icon = "✅" if f.get("is_fact") else "🔍"
            lines.append(f"{icon} **[{et} · {conf:.0%}]** {f.get('statement', '')}")
        lines.append("")

    # ── Recommendations ───────────────────────────────────────────────────────
    if recommendations:
        lines += ["---", "### 💡 Recommendations", ""]
        for i, r in enumerate(sorted(recommendations, key=lambda x: x.get("priority", 99)), 1):
            conf = r.get("confidence", 0.5)
            lines += [
                f"**{i}. {r.get('recommendation', '')}**",
                f"- *Impact:* {r.get('expected_impact', 'N/A')}",
                f"- *Confidence:* {conf:.0%}",
                f"- *Next action:* {r.get('suggested_action', 'N/A')}",
                "",
            ]

    # ── External context (when web research ran) ─────────────────────────────
    if fused and (fused.get("combined_insights") or fused.get("external_summary")):
        lines += ["---", "### 🌐 External Context", ""]
        ext = fused.get("external_summary", "")
        if ext:
            lines += [ext, ""]
        for insight in fused.get("combined_insights") or []:
            lines.append(f"- {insight}")
        for issue in fused.get("comparability_issues") or []:
            lines += ["", f"⚠️ *{issue}*"]
        lines.append("")

    # ── Failure transparency ──────────────────────────────────────────────────
    if failure_modes:
        lines += ["", f"⚠️ *Analysis notes: {', '.join(failure_modes)}*"]

    if not findings and not recommendations and not visualizations:
        lines.append("*Analysis completed — no specific findings were generated.*")

    return "\n".join(lines)


def _format_content(mode: str, result: AgentRunResult, executed: dict[str, Any] | None) -> str:
    if result.status == "pending_approval":
        return _format_pending_approval(result)
    if mode == "analytics":
        return _format_analytics(result.output, executed)
    if mode == "monitor":
        return _format_monitor(result.output, None)
    if mode == "research":
        return _format_research(result.output)
    if mode == "simulate":
        return _format_simulate(result.output)
    if mode == "eda":
        return _format_eda(result.output)
    return json.dumps(result.output, default=str, indent=2)


# --------------------------------------------------------------------------
# SSE streaming
# --------------------------------------------------------------------------
def _chunk(
    chunk_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> str:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _sse_stream(content: str, model: str, usage: CompletionUsage) -> AsyncIterator[str]:
    """Stream the final agent answer as OpenAI-style SSE chunks."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    yield _chunk(chunk_id, created, model, {"role": "assistant"})
    step = 48
    for i in range(0, max(len(content), 1), step):
        yield _chunk(chunk_id, created, model, {"content": content[i : i + step]})
    final = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": usage.model_dump(),
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


# --------------------------------------------------------------------------
# endpoint
# --------------------------------------------------------------------------
@router.post("/chat/completions", summary="Run an agent mode (OpenAI-compatible)")
async def chat_completions(
    request_body: ChatCompletionRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    deps: dict[str, Any] = Depends(get_agent_deps),
) -> Any:
    """Route a chat completion to the correct LangGraph mode.

    The model id selects the mode (analytics | monitor | research | simulate).
    Monitor-mode requests create a Signal row; `require_approval` outcomes
    pause the graph (LangGraph interrupt), notify n8n, and resume when the
    approval webhook fires.
    """
    settings = get_settings()
    mode = resolve_mode(request_body.model)
    query, event = _extract_query_and_event(request_body.messages)
    thread_id = f"{mode}-{uuid.uuid4()}"
    checkpointer = getattr(http_request.app.state, "checkpointer", None)

    input_payload: dict[str, Any] = {
        "query": query,
        "messages": [{"role": m.role, "content": m.text} for m in request_body.messages],
        "stream": request_body.stream,
        "user": request_body.user,
    }
    if event is not None:
        input_payload["event"] = event

    if mode == "monitor":
        signal = Signal(
            source=str((event or {}).get("source") or "chat"),
            payload=event or {"description": query},
            status="processing",
        )
        db.add(signal)
        await db.flush()
        input_payload["signal_id"] = str(signal.id)
        result = await run_monitor_for_signal(
            db, signal, event or {"description": query}, thread_id, checkpointer, deps=deps
        )
    else:
        result = await run_agent_graph(
            mode,
            input_payload=input_payload,
            thread_id=thread_id,
            checkpointer=checkpointer,
            deps=deps,
            db=db,
        )

    if result.status == "failed":
        raise HTTPException(status_code=502, detail=f"agent run failed: {result.error}")

    executed: dict[str, Any] | None = None
    if (
        mode == "analytics"
        and result.output.get("sql_valid")
        and settings.execute_analytics_sql
        and result.output.get("generated_sql")
    ):
        try:
            executed = await get_duckdb_client().aquery(result.output["generated_sql"])
        except Exception as exc:
            executed = {"error": str(exc)}
            logger.warning("analytics_execution_failed", thread_id=thread_id, error=str(exc))

    content = _format_content(mode, result, executed)
    record = result.decision_record or {}
    usage = CompletionUsage(
        prompt_tokens=record.get("prompt_tokens", 0),
        completion_tokens=record.get("completion_tokens", 0),
        total_tokens=record.get("prompt_tokens", 0) + record.get("completion_tokens", 0),
    )

    if request_body.stream:
        return StreamingResponse(
            _sse_stream(content, request_body.model, usage),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return ChatCompletionResponse.build(model=request_body.model, content=content, usage=usage)
