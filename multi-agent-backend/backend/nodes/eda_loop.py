"""LangGraph nodes: iterative EDA loop — generate SQL, execute, evaluate hypotheses.

This module provides four nodes that form the core analytical loop:

1. ``generate_eda_sql``  — generate SQL for the next sub-question/hypothesis
2. ``execute_eda_sql``   — execute the SQL and record results
3. ``evaluate_hypothesis`` — assess evidence and decide: support/reject/refine
4. ``decide_next_step``  — determine whether to loop, do web research, or proceed

The loop terminates when:
    - All hypotheses are resolved (supported/rejected/refined)
    - The iteration budget is exhausted (max_iterations, default 5)
    - The agent judges evidence sufficient to proceed
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig

from analysis_state import (
    AnalysisState,
    EvidenceType,
    FailureMode,
    Hypothesis,
    HypothesisStatus,
    QueryRecord,
    StepMetrics,
    ToolCallRecord,
)
from clients.duckdb_client import get_duckdb_client
from clients.litellm_client import get_litellm_client
from clients.prompt_loader import get_prompt_loader
from nodes.validate_sql import validate_sql

logger = structlog.get_logger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_CHART_RE = re.compile(r"\*\*Suggested chart:\*\*\s*(\w+)\s+with\s+x=(\S+),\s*y=(\S+)", re.IGNORECASE)
_RATIONALE_RE = re.compile(r"\*\*Rationale:\*\*\s*(.+?)(?:\n\n|\*\*|$)", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any]:
    match = _JSON_RE.search(text)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _extract_sql(text: str) -> str:
    match = _SQL_FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _previous_queries_summary(state: AnalysisState, limit: int = 3) -> str:
    lines = []
    for q in state.queries[-limit:]:
        summary = f"- Q{q.id}: {q.sub_question} → {q.row_count} rows"
        if q.error:
            summary += f" [ERROR: {q.error[:100]}]"
        lines.append(summary)
    return "\n".join(lines) if lines else "No previous queries."


# ---------------------------------------------------------------------------
# Node 1: generate_eda_sql
# ---------------------------------------------------------------------------
async def generate_eda_sql(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Generate SQL for the next pending hypothesis/sub-question.

    Input state keys: analysis_state, context, query
    Output state keys: analysis_state (updated with new query), usage
    """
    t0 = time.perf_counter()
    analysis = AnalysisState.model_validate(state.get("analysis_state") or {})
    context = state.get("context") or {}
    ddl = (context.get("schemas") or {}).get("analytics_warehouse", "") or str(
        analysis.warehouse_schema.get("ddl", "")
    )
    rules = (context.get("rules") or {}).get("analytics", {})
    max_rows = int(rules.get("max_rows", 200))

    llm = get_litellm_client(dict(config) if config else None)
    loader = get_prompt_loader(dict(config) if config else None)

    # Pick the next pending hypothesis
    pending = analysis.pending_hypotheses()
    if not pending:
        # Fall back to sub-questions not yet queried
        queried_questions = {q.sub_question for q in analysis.queries}
        remaining = [sq for sq in analysis.sub_questions if sq not in queried_questions]
        if not remaining:
            return {"analysis_state": analysis.model_dump(), "usage": []}

        sub_question = remaining[0]
        hypothesis_text = ""
        hypothesis_id = None
    else:
        hyp = pending[0]
        sub_question = hyp.required_evidence[0] if hyp.required_evidence else analysis.business_question
        hypothesis_text = hyp.statement
        hypothesis_id = hyp.id

    # Generate SQL
    try:
        prompt = loader.render(
            "eda/generate_eda_sql.md",
            question=analysis.business_question,
            sub_question=sub_question,
            hypothesis=hypothesis_text,
            schema_ddl=ddl,
            max_rows=max_rows,
            previous_queries_summary=_previous_queries_summary(analysis),
        )
    except Exception:
        prompt = (
            f"Generate DuckDB SQL for: {sub_question}\n"
            f"Schema: {ddl[:500]}\n"
            "Return only a ```sql block."
        )

    response = await llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
    sql = _extract_sql(response.content)

    # Extract rationale and chart hint from response
    rationale = ""
    rationale_match = _RATIONALE_RE.search(response.content)
    if rationale_match:
        rationale = rationale_match.group(1).strip()[:300]

    # Store chart suggestion in query metadata for visualization step
    chart_hint: dict[str, str] = {}
    chart_match = _CHART_RE.search(response.content)
    if chart_match:
        chart_hint = {
            "chart_type": chart_match.group(1),
            "x_column": chart_match.group(2),
            "y_column": chart_match.group(3),
        }

    latency_ms = int((time.perf_counter() - t0) * 1000)
    usage_record = response.usage_record()

    # Add query to state (not yet executed)
    q = analysis.add_query(
        sql=sql,
        rationale=rationale or sub_question,
        hypothesis_id=hypothesis_id,
        sub_question=sub_question,
    )
    # Attach chart hint as metadata via a private attribute workaround
    # (stored in rationale field with prefix)
    if chart_hint:
        q.rationale = f"{q.rationale} [chart:{json.dumps(chart_hint)}]"

    analysis.metrics.add_llm_usage(usage_record)

    # Record tool call
    analysis.record_tool_call({
        "tool": "generate_eda_sql",
        "status": "success",
        "latency_ms": latency_ms,
        "rationale": f"Generating SQL for: {sub_question[:100]}",
    })

    logger.info(
        "eda_sql_generated",
        run_id=analysis.run_id,
        query_id=q.id,
        sql_length=len(sql),
        hypothesis_id=hypothesis_id,
        iteration=analysis.current_iteration,
    )

    return {
        "analysis_state": analysis.model_dump(),
        "usage": [usage_record],
    }


# ---------------------------------------------------------------------------
# Node 2: execute_eda_sql
# ---------------------------------------------------------------------------
async def execute_eda_sql(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Execute the most recently generated SQL query.

    Input state keys: analysis_state
    Output state keys: analysis_state (query updated with results)
    """
    analysis = AnalysisState.model_validate(state.get("analysis_state") or {})

    # Find the last unexecuted query
    pending_queries = [q for q in analysis.queries if not q.executed]
    if not pending_queries:
        return {"analysis_state": analysis.model_dump()}

    q = pending_queries[-1]  # most recent unexecuted

    t0 = time.perf_counter()
    db = get_duckdb_client()

    # Validate first
    validation = validate_sql(q.sql, None)
    if not validation.sql_valid:
        q.executed = True
        q.error = "Validation failed: " + "; ".join(validation.sql_validation_errors)
        q.latency_ms = int((time.perf_counter() - t0) * 1000)
        analysis.record_failure(
            FailureMode.IMPLEMENTATION_ERROR,
            detail=q.error,
            step="execute_eda_sql",
        )
        logger.warning("eda_sql_invalid", query_id=q.id, errors=validation.sql_validation_errors)
    else:
        try:
            result = await db.aquery(q.sql)
            q.executed = True
            q.row_count = result.get("row_count", 0)
            q.columns = result.get("columns") or []
            q.rows = result.get("rows") or []
            q.latency_ms = int((time.perf_counter() - t0) * 1000)
            analysis.metrics.total_sql_queries += 1
            analysis.metrics.sql_latency_ms += q.latency_ms
            logger.info(
                "eda_sql_executed",
                query_id=q.id,
                row_count=q.row_count,
                latency_ms=q.latency_ms,
            )
        except Exception as exc:
            q.executed = True
            q.error = str(exc)
            q.latency_ms = int((time.perf_counter() - t0) * 1000)
            analysis.record_failure(
                FailureMode.RUNTIME_ERROR,
                detail=str(exc),
                step="execute_eda_sql",
            )
            logger.warning("eda_sql_execution_failed", query_id=q.id, error=str(exc))

    analysis.record_tool_call({
        "tool": "execute_sql",
        "status": "success" if not q.error else "failure",
        "latency_ms": q.latency_ms,
        "error": q.error,
    })

    # Update the query in the state
    for i, existing_q in enumerate(analysis.queries):
        if existing_q.id == q.id:
            analysis.queries[i] = q
            break

    return {"analysis_state": analysis.model_dump()}


# ---------------------------------------------------------------------------
# Node 3: evaluate_hypothesis
# ---------------------------------------------------------------------------
async def evaluate_hypothesis(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Evaluate evidence for the current hypothesis and decide its status.

    Input state keys: analysis_state, context
    Output state keys: analysis_state (hypothesis updated), usage
    """
    t0 = time.perf_counter()
    analysis = AnalysisState.model_validate(state.get("analysis_state") or {})
    context = state.get("context") or {}

    llm = get_litellm_client(dict(config) if config else None)
    loader = get_prompt_loader(dict(config) if config else None)

    # Get the pending hypothesis with evidence (executed queries)
    pending = analysis.pending_hypotheses()
    if not pending:
        return {"analysis_state": analysis.model_dump(), "usage": []}

    hyp = pending[0]

    # Gather related query results
    related_queries = [
        q for q in analysis.queries
        if q.executed and (q.hypothesis_id == hyp.id or not q.hypothesis_id)
    ][-3:]  # last 3 related

    if not related_queries:
        # No evidence yet — stay pending, move on
        return {"analysis_state": analysis.model_dump(), "usage": []}

    try:
        prompt = loader.render(
            "eda/evaluate_evidence.md",
            question=analysis.business_question,
            hypothesis=hyp.model_dump(),
            query_results=[q.model_dump() for q in related_queries],
        )
    except Exception:
        # Fallback
        results_text = "\n".join(
            f"Query: {q.sub_question}, rows: {q.row_count}, error: {q.error}"
            for q in related_queries
        )
        prompt = (
            f"Hypothesis: {hyp.statement}\n"
            f"Evidence:\n{results_text}\n"
            "Evaluate: supported/rejected/insufficient_evidence/refined. "
            "Respond with JSON: {decision, confidence, supporting_evidence, "
            "contradicting_evidence, refined_hypothesis, additional_queries_needed}"
        )

    response = await llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
    eval_data = _parse_json(response.content)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    usage_record = response.usage_record()

    # Update hypothesis
    decision = str(eval_data.get("decision", "insufficient_evidence"))
    status_map = {
        "supported": HypothesisStatus.SUPPORTED,
        "rejected": HypothesisStatus.REJECTED,
        "refined": HypothesisStatus.REFINED,
        "insufficient_evidence": HypothesisStatus.INSUFFICIENT_EVIDENCE,
    }
    hyp.status = status_map.get(decision, HypothesisStatus.INSUFFICIENT_EVIDENCE)
    hyp.confidence = min(1.0, max(0.0, float(eval_data.get("confidence", 0.3))))
    hyp.supporting_evidence = [str(e)[:200] for e in (eval_data.get("supporting_evidence") or [])]
    hyp.contradicting_evidence = [str(e)[:200] for e in (eval_data.get("contradicting_evidence") or [])]

    # If refined, add a new hypothesis
    refined_stmt = eval_data.get("refined_hypothesis")
    if decision == "refined" and refined_stmt:
        new_hyp = analysis.add_hypothesis(
            statement=str(refined_stmt),
            required_evidence=list(eval_data.get("additional_queries_needed") or []),
        )
        new_hyp.refined_from = hyp.id

    # Store additional query needs as sub-questions for the next iteration
    for aq in (eval_data.get("additional_queries_needed") or []):
        if aq and aq not in analysis.sub_questions:
            analysis.sub_questions.append(str(aq))

    analysis.metrics.add_llm_usage(usage_record)

    # Update in state
    for i, existing_h in enumerate(analysis.hypotheses):
        if existing_h.id == hyp.id:
            analysis.hypotheses[i] = hyp
            break

    logger.info(
        "hypothesis_evaluated",
        run_id=analysis.run_id,
        hypothesis_id=hyp.id,
        decision=decision,
        confidence=hyp.confidence,
    )

    return {
        "analysis_state": analysis.model_dump(),
        "usage": [usage_record],
    }


# ---------------------------------------------------------------------------
# Node 4: decide_next_step
# ---------------------------------------------------------------------------
async def decide_next_step(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Decide whether to loop, do web research, or finalize the analysis.

    This is a pure-computation node (no LLM call).
    It increments the iteration counter and sets flags for the graph router.

    Output state keys:
        analysis_state (iteration incremented, analysis_complete flag set)
        eda_next: "loop" | "web_search" | "finalize"
    """
    analysis = AnalysisState.model_validate(state.get("analysis_state") or {})
    analysis.current_iteration += 1
    analysis.metrics.total_iterations += 1

    # Determine next step
    if analysis.current_iteration >= analysis.max_iterations:
        analysis.analysis_complete = True
        analysis.termination_reason = f"Reached maximum iterations ({analysis.max_iterations})"
        next_step = "finalize"
    elif not analysis.pending_hypotheses():
        # All hypotheses resolved
        analysis.analysis_complete = True
        analysis.termination_reason = "All hypotheses evaluated"
        next_step = "web_search" if analysis.needs_web_research and not analysis.web_searches else "finalize"
    else:
        # Still have pending hypotheses
        next_step = "loop"

    logger.info(
        "eda_next_step",
        run_id=analysis.run_id,
        iteration=analysis.current_iteration,
        pending_hypotheses=len(analysis.pending_hypotheses()),
        next_step=next_step,
    )

    return {
        "analysis_state": analysis.model_dump(),
        "eda_next": next_step,
    }
