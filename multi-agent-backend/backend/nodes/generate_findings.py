"""LangGraph node: generate findings, visualizations, and recommendations.

This is the final analytical synthesis node.  It:
1. Asks the LLM to produce structured findings from the evidence
2. Selects appropriate visualizations for the query results
3. Generates prioritised, evidence-backed recommendations
4. Marks the analysis as complete
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
    Finding,
    Recommendation,
    StepMetrics,
    Visualization,
)
from clients.litellm_client import get_litellm_client
from clients.prompt_loader import get_prompt_loader
from tools.visualization_tool import VisualizationTool

logger = structlog.get_logger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any]:
    match = _JSON_RE.search(text)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


async def generate_findings_and_recommendations(
    state: dict[str, Any], config: RunnableConfig
) -> dict[str, Any]:
    """Produce findings, visualizations, and recommendations from all evidence.

    Input state keys: analysis_state, context
    Output state keys: analysis_state (findings/visualizations/recommendations populated), usage
    """
    t0 = time.perf_counter()
    analysis = AnalysisState.model_validate(state.get("analysis_state") or {})
    context = state.get("context") or {}

    llm = get_litellm_client(dict(config) if config else None)
    loader = get_prompt_loader(dict(config) if config else None)

    usage_records: list[dict[str, Any]] = []

    # ---- Step 1: Visualizations
    await _generate_visualizations(analysis, loader, llm, usage_records)

    # ---- Step 2: Findings and Recommendations
    await _generate_findings(analysis, loader, llm, usage_records)

    # ---- Finalize
    latency_ms = int((time.perf_counter() - t0) * 1000)
    analysis.metrics.total_latency_ms += latency_ms
    analysis.analysis_complete = True
    if not analysis.termination_reason:
        analysis.termination_reason = "Analysis complete"

    logger.info(
        "findings_generated",
        run_id=analysis.run_id,
        findings=len(analysis.findings),
        visualizations=len(analysis.visualizations),
        recommendations=len(analysis.recommendations),
    )

    return {
        "analysis_state": analysis.model_dump(),
        "usage": usage_records,
    }


async def _generate_visualizations(
    analysis: AnalysisState,
    loader: Any,
    llm: Any,
    usage_records: list,
) -> None:
    """Ask the LLM to select charts, then generate Plotly specs."""
    executed_queries = [q for q in analysis.queries if q.executed and not q.error and q.rows]
    if not executed_queries:
        return

    try:
        viz_prompt = loader.render(
            "eda/select_visualizations.md",
            question=analysis.business_question,
            queries=[q.model_dump() for q in executed_queries],
        )
    except Exception:
        # Build a simple fallback selection
        _fallback_visualizations(analysis, executed_queries)
        return

    response = await llm.chat([{"role": "user", "content": viz_prompt}], temperature=0.1)
    usage_records.append(response.usage_record())
    analysis.metrics.add_llm_usage(response.usage_record())

    viz_data = _parse_json(response.content)
    viz_tool = VisualizationTool()

    for viz_spec in (viz_data.get("visualizations") or []):
        query_id = str(viz_spec.get("query_id", ""))
        matching_query = next(
            (q for q in executed_queries if q.id == query_id or q.id.replace("q", "") == query_id),
            executed_queries[0] if executed_queries else None,
        )
        if not matching_query:
            continue

        chart_type = str(viz_spec.get("chart_type", "bar"))
        x_col = str(viz_spec.get("x_column", ""))
        y_col = viz_spec.get("y_column", "")
        color_col = viz_spec.get("color_column")
        title = str(viz_spec.get("title", f"Analysis: {matching_query.sub_question[:50]}"))

        if not x_col or not matching_query.columns:
            continue

        # Ensure columns exist
        available_cols = set(matching_query.columns)
        if x_col not in available_cols:
            x_col = matching_query.columns[0] if matching_query.columns else ""
        if isinstance(y_col, str) and y_col not in available_cols and len(matching_query.columns) > 1:
            y_col = matching_query.columns[1]
        if isinstance(y_col, list):
            y_col = [c for c in y_col if c in available_cols][:3]

        result = await viz_tool.run({
            "chart_type": chart_type,
            "rows": matching_query.rows[:50],
            "x_column": x_col,
            "y_column": y_col,
            "color_column": color_col,
            "title": title,
        })

        if result.succeeded and result.output:
            viz = Visualization(
                id=f"viz{len(analysis.visualizations) + 1}",
                chart_type=chart_type,
                title=title,
                plotly_spec=result.output.get("plotly_spec", ""),
                related_query_id=matching_query.id,
                description=str(viz_spec.get("description", ""))[:300],
            )
            analysis.visualizations.append(viz)


def _fallback_visualizations(analysis: AnalysisState, queries: list) -> None:
    """Minimal fallback: bar chart for any query with 2+ numeric columns."""
    from tools.visualization_tool import VisualizationTool
    import asyncio

    viz_tool = VisualizationTool()

    async def _create_fallback(q: Any) -> None:
        if len(q.columns) < 2:
            return
        x_col = q.columns[0]
        y_col = q.columns[1]
        result = await viz_tool.run({
            "chart_type": "bar",
            "rows": q.rows[:20],
            "x_column": x_col,
            "y_column": y_col,
            "title": q.sub_question[:60],
        })
        if result.succeeded and result.output:
            analysis.visualizations.append(Visualization(
                id=f"viz{len(analysis.visualizations) + 1}",
                chart_type="bar",
                title=q.sub_question[:60],
                plotly_spec=result.output.get("plotly_spec", ""),
                related_query_id=q.id,
            ))

    for q in queries[:3]:
        try:
            asyncio.get_event_loop().run_until_complete(_create_fallback(q))
        except Exception:
            pass


async def _generate_findings(
    analysis: AnalysisState,
    loader: Any,
    llm: Any,
    usage_records: list,
) -> None:
    """Generate findings and recommendations from all evidence."""
    try:
        prompt = loader.render(
            "eda/generate_findings.md",
            question=analysis.business_question,
            analysis_plan=analysis.analysis_plan,
            hypotheses=[h.model_dump() for h in analysis.hypotheses],
            queries=[{
                "id": q.id,
                "sub_question": q.sub_question,
                "row_count": q.row_count,
                "error": q.error,
            } for q in analysis.queries],
            fused_context=analysis.fused_context.model_dump() if analysis.fused_context else None,
        )
    except Exception:
        # Minimal fallback prompt
        h_summary = "; ".join(f"{h.statement} ({h.status})" for h in analysis.hypotheses)
        prompt = (
            f"Business question: {analysis.business_question}\n"
            f"Hypotheses: {h_summary}\n"
            f"Queries: {len(analysis.queries)} executed\n"
            "Generate findings and recommendations as JSON: "
            "{findings: [{statement, evidence_type, confidence, is_fact, is_inference}], "
            "recommendations: [{recommendation, expected_impact, confidence, priority}], "
            "executive_summary, confidence_note}"
        )

    response = await llm.chat([{"role": "user", "content": prompt}], temperature=0.3)
    usage_records.append(response.usage_record())
    analysis.metrics.add_llm_usage(response.usage_record())

    findings_data = _parse_json(response.content)

    # Parse findings
    for f_data in (findings_data.get("findings") or []):
        if not isinstance(f_data, dict) or not f_data.get("statement"):
            continue
        evidence_type_str = str(f_data.get("evidence_type", "internal")).lower()
        et_map = {"internal": EvidenceType.INTERNAL, "external": EvidenceType.EXTERNAL, "fused": EvidenceType.FUSED}
        f = analysis.add_finding(
            statement=str(f_data["statement"])[:500],
            evidence_type=et_map.get(evidence_type_str, EvidenceType.INTERNAL),
            confidence=min(1.0, max(0.0, float(f_data.get("confidence", 0.5)))),
            is_fact=bool(f_data.get("is_fact", False)),
            is_inference=bool(f_data.get("is_inference", False)),
        )
        f.supporting_queries = [str(q)[:20] for q in (f_data.get("supporting_queries") or [])]

    # Parse recommendations
    for r_data in (findings_data.get("recommendations") or []):
        if not isinstance(r_data, dict) or not r_data.get("recommendation"):
            continue
        r = analysis.add_recommendation(
            recommendation=str(r_data["recommendation"])[:500],
            supporting_evidence=[str(e)[:200] for e in (r_data.get("supporting_evidence") or [])],
            expected_impact=str(r_data.get("expected_impact", ""))[:300],
            confidence=min(1.0, max(0.0, float(r_data.get("confidence", 0.5)))),
            priority=int(r_data.get("priority", 1)),
        )
        r.assumptions = [str(a)[:200] for a in (r_data.get("assumptions") or [])]
        r.suggested_action = str(r_data.get("suggested_action", ""))[:300]

    # If no findings from LLM, add a summary finding
    if not analysis.findings:
        analysis.add_finding(
            statement=str(findings_data.get("executive_summary", "Analysis complete."))[:500],
            evidence_type=EvidenceType.INTERNAL,
            confidence=0.5,
            is_inference=True,
        )

    # Store the executive summary in termination_reason as a convenient field
    if findings_data.get("executive_summary"):
        analysis.termination_reason = str(findings_data["executive_summary"])[:500]
