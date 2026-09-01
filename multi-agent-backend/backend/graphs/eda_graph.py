"""EDA agent graph — iterative analytical pipeline.

Pipeline:
    load_context
        ↓
    plan_analysis
        ↓
    profile_data  (data readiness gate)
        ↓
    generate_eda_sql
        ↓
    execute_eda_sql
        ↓
    evaluate_hypothesis
        ↓
    decide_next_step ──→ "loop" ──────┐
        │                              │
        ↓ "web_search"                 │
    perform_web_research               │
        ↓                              │
    fuse_context                       │
        ↓ "finalize"                   │
    generate_findings ←────────────────┘ (also via finalize)
        ↓
    log_eda_decision
        ↓
    END

The loop terminates when:
    - All hypotheses are resolved
    - max_iterations (default 5) is reached
    - The agent judges evidence sufficient

The ``eda`` mode is registered alongside the existing modes and is accessible
via model name ``eda`` in OpenAI-style requests.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from graphs.base import GraphState, log_decision, register_graph
from nodes.eda_loop import decide_next_step, evaluate_hypothesis, execute_eda_sql, generate_eda_sql
from nodes.fuse_context import fuse_context
from nodes.generate_findings import generate_findings_and_recommendations
from nodes.load_context import load_context
from nodes.plan_analysis import plan_analysis
from nodes.web_research import perform_web_research


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------
def _after_decide(state: dict[str, Any]) -> str:
    """Route based on eda_next flag set by decide_next_step."""
    next_step = state.get("eda_next", "finalize")
    if next_step == "loop":
        return "generate_eda_sql"
    if next_step == "web_search":
        return "perform_web_research"
    return "generate_findings_and_recommendations"


# ---------------------------------------------------------------------------
# Terminal log node for EDA mode
# ---------------------------------------------------------------------------
async def log_eda_decision(state: dict[str, Any]) -> dict[str, Any]:
    """Assemble the decision record for EDA mode.

    Extends the base log_decision with the full analysis_state summary.
    """
    base_record = await log_decision(state)
    analysis_state_data = state.get("analysis_state") or {}

    # Build a richer output summary
    record = base_record.get("decision_record") or {}
    record["output"] = {
        "mode": "eda",
        "analysis_summary": _build_summary(analysis_state_data),
        "analysis_state": {
            "run_id": analysis_state_data.get("run_id", ""),
            "business_question": analysis_state_data.get("business_question", ""),
            "hypothesis_count": len(analysis_state_data.get("hypotheses") or []),
            "query_count": len(analysis_state_data.get("queries") or []),
            "finding_count": len(analysis_state_data.get("findings") or []),
            "recommendation_count": len(analysis_state_data.get("recommendations") or []),
            "visualization_count": len(analysis_state_data.get("visualizations") or []),
            "iterations": analysis_state_data.get("current_iteration", 0),
            "metrics": analysis_state_data.get("metrics", {}),
            "findings": analysis_state_data.get("findings") or [],
            "recommendations": analysis_state_data.get("recommendations") or [],
            "visualizations": analysis_state_data.get("visualizations") or [],
            "fused_context": analysis_state_data.get("fused_context"),
            "failure_modes": analysis_state_data.get("failure_modes") or [],
        },
    }
    record["evaluation_score"] = _evaluation_score(analysis_state_data)

    return {"decision_record": record}


def _build_summary(data: dict[str, Any]) -> str:
    metrics = data.get("metrics") or {}
    lines = [
        f"## Analysis: {data.get('business_question', 'Unknown question')}",
        "",
        f"**Plan:** {data.get('analysis_plan', '')[:300]}",
        "",
        f"**Iterations:** {data.get('current_iteration', 0)} · "
        f"**Queries:** {len(data.get('queries') or [])} · "
        f"**Hypotheses:** {len(data.get('hypotheses') or [])} · "
        f"**Findings:** {len(data.get('findings') or [])} · "
        f"**Recommendations:** {len(data.get('recommendations') or [])}",
        "",
        f"**Performance:** {metrics.get('total_tokens', 0):,} tokens · "
        f"${metrics.get('total_cost_usd', 0.0):.4f} · "
        f"{metrics.get('total_latency_ms', 0)}ms",
    ]
    return "\n".join(lines)


def _evaluation_score(data: dict[str, Any]) -> float:
    """Score the analysis quality: 0–1 based on completion and findings."""
    score = 0.0
    if data.get("analysis_complete"):
        score += 0.3
    findings = data.get("findings") or []
    if findings:
        score += min(0.3, len(findings) * 0.1)
    recommendations = data.get("recommendations") or []
    if recommendations:
        score += min(0.2, len(recommendations) * 0.07)
    failure_modes = data.get("failure_modes") or []
    if not failure_modes:
        score += 0.2
    return round(min(1.0, score), 3)


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------
def build_eda_graph() -> StateGraph:
    """Build (uncompiled) the iterative EDA pipeline."""
    builder = StateGraph(GraphState)

    # Nodes
    builder.add_node("load_context", load_context)
    builder.add_node("plan_analysis", plan_analysis)
    builder.add_node("generate_eda_sql", generate_eda_sql)
    builder.add_node("execute_eda_sql", execute_eda_sql)
    builder.add_node("evaluate_hypothesis", evaluate_hypothesis)
    builder.add_node("decide_next_step", decide_next_step)
    builder.add_node("perform_web_research", perform_web_research)
    builder.add_node("fuse_context", fuse_context)
    builder.add_node("generate_findings_and_recommendations", generate_findings_and_recommendations)
    builder.add_node("log_eda_decision", log_eda_decision)

    # Edges
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "plan_analysis")
    builder.add_edge("plan_analysis", "generate_eda_sql")
    builder.add_edge("generate_eda_sql", "execute_eda_sql")
    builder.add_edge("execute_eda_sql", "evaluate_hypothesis")
    builder.add_edge("evaluate_hypothesis", "decide_next_step")
    builder.add_conditional_edges(
        "decide_next_step",
        _after_decide,
        {
            "generate_eda_sql": "generate_eda_sql",
            "perform_web_research": "perform_web_research",
            "generate_findings_and_recommendations": "generate_findings_and_recommendations",
        },
    )
    builder.add_edge("perform_web_research", "fuse_context")
    builder.add_edge("fuse_context", "generate_findings_and_recommendations")
    builder.add_edge("generate_findings_and_recommendations", "log_eda_decision")
    builder.add_edge("log_eda_decision", END)

    return builder


register_graph("eda", build_eda_graph)
