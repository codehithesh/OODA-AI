"""LangGraph node: external web research when the agent decides it's useful.

The agent already decided ``needs_web_research=True`` during planning.
This node generates targeted search queries based on the business question
and executes them via WebSearchTool.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig

from analysis_state import AnalysisState, WebSearchRecord
from tools.web_search_tool import WebSearchTool

logger = structlog.get_logger(__name__)


async def perform_web_research(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Perform targeted web searches for external context.

    Input state keys: analysis_state
    Output state keys: analysis_state (web_searches populated)
    """
    analysis = AnalysisState.model_validate(state.get("analysis_state") or {})

    if not analysis.needs_web_research:
        return {"analysis_state": analysis.model_dump()}

    search_tool = WebSearchTool()
    question = analysis.business_question

    # Generate targeted search queries based on the business question
    search_queries = _build_search_queries(question)

    for query in search_queries[:3]:  # Limit to 3 searches
        t0 = time.perf_counter()
        result = await search_tool.run({"query": query, "max_results": 5})
        latency_ms = int((time.perf_counter() - t0) * 1000)

        analysis.metrics.total_web_searches += 1
        analysis.metrics.tool_latency_ms += latency_ms

        record = WebSearchRecord(
            id=f"ws{len(analysis.web_searches) + 1}",
            query=query,
            latency_ms=latency_ms,
        )
        if result.succeeded and result.output:
            record.results = result.output.get("results") or []
            record.relevance_note = f"Found {len(record.results)} results for: {query}"
        else:
            record.relevance_note = f"Search failed: {result.error}"

        analysis.web_searches.append(record)

        analysis.record_tool_call({
            "tool": "web_search",
            "status": "success" if result.succeeded else "failure",
            "latency_ms": latency_ms,
            "error": result.error,
            "rationale": f"External context for: {query[:80]}",
        })

        logger.info(
            "web_research_done",
            run_id=analysis.run_id,
            query=query,
            results=len(record.results),
            latency_ms=latency_ms,
        )

    return {"analysis_state": analysis.model_dump()}


def _build_search_queries(question: str) -> list[str]:
    """Heuristically generate web search queries from a business question."""
    question_lower = question.lower()
    queries: list[str] = []

    # Revenue / growth
    if any(w in question_lower for w in ("revenue", "sales", "growth", "increase")):
        queries.append(f"industry revenue growth benchmarks {question[:60]}")
        queries.append("e-commerce revenue growth strategies best practices 2025")

    # Customer / retention
    if any(w in question_lower for w in ("customer", "retention", "churn", "ltv", "lifetime")):
        queries.append("customer retention strategies benchmark industry average 2025")
        queries.append("customer lifetime value improvement tactics")

    # Conversion / funnel
    if any(w in question_lower for w in ("conversion", "funnel", "acquisition")):
        queries.append("conversion rate optimization benchmarks ecommerce 2025")

    # Pricing
    if any(w in question_lower for w in ("price", "pricing", "discount")):
        queries.append("pricing strategy optimization data-driven approaches")

    # Default: use the question directly
    if not queries:
        queries.append(f"industry benchmarks {question[:80]}")
        queries.append(f"best practices {question[:80]}")

    return queries[:3]
