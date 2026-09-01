"""LangGraph node: context-fusion layer.

Combines internal warehouse evidence with external web research into a
unified analytical context.  The fusion process explicitly documents:
  - comparability issues between internal and external metrics
  - combined insights that emerge from both sources
  - source attribution for every claim

Only runs when web research was performed.  Passes through gracefully
when no external context is available.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig

from analysis_state import AnalysisState, FusedContext
from clients.litellm_client import get_litellm_client
from clients.prompt_loader import get_prompt_loader

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


def _build_internal_summary(analysis: AnalysisState) -> str:
    lines = [f"Business question: {analysis.business_question}", ""]
    executed = [q for q in analysis.queries if q.executed and not q.error]
    for q in executed[:5]:
        lines.append(f"- {q.sub_question}: {q.row_count} rows returned")
        if q.rows:
            sample = q.rows[:2]
            lines.append(f"  Sample: {str(sample)[:200]}")
    if not executed:
        lines.append("No successful queries executed.")
    return "\n".join(lines)


async def fuse_context(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Fuse internal warehouse data with external web research context.

    Input state keys: analysis_state, context
    Output state keys: analysis_state (fused_context populated), usage
    """
    analysis = AnalysisState.model_validate(state.get("analysis_state") or {})

    # If no web searches, create a simple internal-only fusion
    if not analysis.web_searches:
        internal_summary = _build_internal_summary(analysis)
        analysis.fused_context = FusedContext(
            internal_summary=internal_summary,
            external_summary="No external research was performed.",
            internal_evidence=[
                f"Query {q.id}: {q.sub_question} ({q.row_count} rows)"
                for q in analysis.queries
                if q.executed and not q.error
            ],
        )
        return {"analysis_state": analysis.model_dump(), "usage": []}

    t0 = time.perf_counter()
    llm = get_litellm_client(dict(config) if config else None)
    loader = get_prompt_loader(dict(config) if config else None)

    internal_summary = _build_internal_summary(analysis)

    try:
        prompt = loader.render(
            "eda/fuse_context.md",
            question=analysis.business_question,
            internal_summary=internal_summary,
            web_searches=[ws.model_dump() for ws in analysis.web_searches],
        )
    except Exception:
        search_texts = "\n".join(
            f"Search: {ws.query}\n" + "\n".join(f"- {r.get('snippet', '')}" for r in ws.results[:3])
            for ws in analysis.web_searches
        )
        prompt = (
            f"Synthesize internal data with external research for: {analysis.business_question}\n"
            f"Internal: {internal_summary[:500]}\nExternal: {search_texts[:500]}\n"
            "JSON: {internal_summary, external_summary, comparability_issues, "
            "internal_evidence, external_evidence, combined_insights, fusion_notes}"
        )

    response = await llm.chat([{"role": "user", "content": prompt}], temperature=0.2)
    fusion_data = _parse_json(response.content)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    usage_record = response.usage_record()

    analysis.fused_context = FusedContext(
        internal_summary=str(fusion_data.get("internal_summary", internal_summary))[:1000],
        external_summary=str(fusion_data.get("external_summary", ""))[:1000],
        comparability_issues=[str(i)[:200] for i in (fusion_data.get("comparability_issues") or [])],
        internal_evidence=[str(e)[:200] for e in (fusion_data.get("internal_evidence") or [])],
        external_evidence=[str(e)[:200] for e in (fusion_data.get("external_evidence") or [])],
        combined_insights=[str(i)[:300] for i in (fusion_data.get("combined_insights") or [])],
        fusion_notes=str(fusion_data.get("fusion_notes", ""))[:500],
    )

    analysis.metrics.add_llm_usage(usage_record)
    analysis.record_tool_call({
        "tool": "fuse_context",
        "status": "success",
        "latency_ms": latency_ms,
        "rationale": "Fusing internal + external context",
    })

    logger.info(
        "context_fused",
        run_id=analysis.run_id,
        combined_insights=len(analysis.fused_context.combined_insights),
        comparability_issues=len(analysis.fused_context.comparability_issues),
    )

    return {
        "analysis_state": analysis.model_dump(),
        "usage": [usage_record],
    }
