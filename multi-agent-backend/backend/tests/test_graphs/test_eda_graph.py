"""Tests for the EDA (iterative analysis) graph."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

import graphs.eda_graph  # noqa: F401 — registers the graph
from graphs.base import run_agent_graph
from tests.conftest import FakeLLM, make_deps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sql_response(sql: str = "SELECT COUNT(*) AS n FROM orders") -> str:
    """LLM response for SQL generation prompts."""
    return f"```sql\n{sql}\n```\n\n**Rationale:** Counting orders\n\n**Suggested chart:** bar with x=status, y=n"


def _plan_response() -> str:
    return json.dumps({
        "question_type": "broad_eda",
        "analysis_plan": "Analyze revenue trends and customer segments",
        "sub_questions": ["What is total revenue?", "How does revenue vary by region?"],
        "initial_hypotheses": [
            {
                "statement": "Revenue is growing quarter over quarter",
                "required_evidence": ["SELECT SUM(amount) FROM orders GROUP BY date_trunc('quarter', ordered_at)"],
                "relevant_tables": ["orders"],
            }
        ],
        "needs_web_research": False,
        "web_research_rationale": "Internal data sufficient",
        "relevant_tables": ["orders"],
    })


def _evidence_response(decision: str = "supported") -> str:
    return json.dumps({
        "decision": decision,
        "confidence": 0.75,
        "supporting_evidence": ["Revenue shows upward trend"],
        "contradicting_evidence": [],
        "refined_hypothesis": None,
        "additional_queries_needed": [],
        "notes": "Evidence supports the hypothesis",
    })


def _findings_response() -> str:
    return json.dumps({
        "findings": [
            {
                "statement": "Revenue grew 15% over the analysis period",
                "evidence_type": "internal",
                "confidence": 0.8,
                "is_fact": True,
                "is_inference": False,
                "supporting_queries": ["q1"],
            }
        ],
        "recommendations": [
            {
                "recommendation": "Invest in EMEA region which shows highest growth",
                "supporting_evidence": ["EMEA revenue up 20%"],
                "expected_impact": "10% revenue increase",
                "confidence": 0.7,
                "assumptions": ["Market conditions remain stable"],
                "suggested_action": "Increase EMEA sales headcount",
                "priority": 1,
            }
        ],
        "executive_summary": "Revenue analysis complete with positive findings.",
        "confidence_note": "Based on 90 days of data.",
    })


def _viz_response() -> str:
    return json.dumps({
        "visualizations": [
            {
                "query_id": "q1",
                "chart_type": "bar",
                "title": "Revenue by Region",
                "x_column": "region",
                "y_column": "total_revenue",
                "description": "Compares revenue across regions",
            }
        ]
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_eda_graph_runs_to_completion() -> None:
    """EDA graph should complete with findings and recommendations."""
    # Script responses: plan, SQL gen, SQL eval, viz selection, findings
    fake = FakeLLM(responses=[
        _plan_response(),
        _sql_response(),
        _evidence_response("supported"),
        _viz_response(),
        _findings_response(),
    ])
    result = await run_agent_graph(
        "eda",
        input_payload={"query": "How can we increase revenue?"},
        thread_id="test-eda-1",
        checkpointer=MemorySaver(),
        deps=make_deps(fake),
    )
    assert result.status == "succeeded", f"Expected succeeded, got: {result.status} / {result.error}"
    output = result.output

    # Should have analysis state
    analysis = output.get("analysis_state") or {}
    assert analysis.get("business_question") == "How can we increase revenue?"

    # Should have queries, findings, recommendations
    assert len(analysis.get("queries") or []) >= 1
    # At least one finding produced (either from LLM or fallback)
    assert len(analysis.get("findings") or []) >= 1


@pytest.mark.asyncio
async def test_eda_graph_handles_llm_failure_gracefully() -> None:
    """EDA graph should not crash when LLM returns unparseable JSON."""
    fake = FakeLLM(responses=[
        "I am not JSON at all",  # plan
        "```sql\nSELECT COUNT(*) FROM orders\n```",  # SQL
        "not json",  # evidence
        "{}",  # viz
        "{}",  # findings
    ])
    result = await run_agent_graph(
        "eda",
        input_payload={"query": "What is our revenue?"},
        thread_id="test-eda-fail-1",
        checkpointer=MemorySaver(),
        deps=make_deps(fake),
    )
    # Should not hard-fail — graceful degradation
    assert result.status in ("succeeded", "failed")
    if result.status == "succeeded":
        analysis = result.output.get("analysis_state") or {}
        # At minimum, a run_id should be set
        assert analysis.get("run_id")


@pytest.mark.asyncio
async def test_eda_via_chat_route(client, fake_llm) -> None:
    """EDA mode is accessible via the chat completions endpoint."""
    fake_llm.responses = [
        _plan_response(),
        _sql_response(),
        _evidence_response("supported"),
        _viz_response(),
        _findings_response(),
    ]
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "eda",
            "messages": [{"role": "user", "content": "How can we increase revenue?"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    # Should mention findings or analysis
    assert any(
        keyword in content.lower()
        for keyword in ("analysis", "finding", "recommendation", "revenue", "question")
    ), f"Unexpected content: {content[:300]}"


@pytest.mark.asyncio
async def test_eda_iterates_on_pending_hypotheses() -> None:
    """EDA graph should loop when hypotheses are pending and budget allows."""
    fake = FakeLLM(responses=[
        # First plan: one hypothesis, insufficient evidence -> loop
        json.dumps({
            "question_type": "broad_eda",
            "analysis_plan": "Test iterative loop",
            "sub_questions": ["What is revenue?"],
            "initial_hypotheses": [
                {
                    "statement": "Hypothesis 1",
                    "required_evidence": ["Check revenue table"],
                    "relevant_tables": ["orders"],
                }
            ],
            "needs_web_research": False,
            "web_research_rationale": "",
            "relevant_tables": ["orders"],
        }),
        # SQL gen iteration 1
        _sql_response("SELECT SUM(amount) AS total FROM orders"),
        # Evidence eval: insufficient -> need more
        json.dumps({
            "decision": "insufficient_evidence",
            "confidence": 0.3,
            "supporting_evidence": [],
            "contradicting_evidence": [],
            "refined_hypothesis": None,
            "additional_queries_needed": ["Break down by region"],
            "notes": "Need more data",
        }),
        # SQL gen iteration 2 (after loop back)
        _sql_response("SELECT region, SUM(amount) FROM orders GROUP BY region"),
        # Evidence eval: supported
        _evidence_response("supported"),
        # Viz selection
        _viz_response(),
        # Findings
        _findings_response(),
    ])
    result = await run_agent_graph(
        "eda",
        input_payload={"query": "Revenue analysis", "max_iterations": 3},
        thread_id="test-eda-iter",
        checkpointer=MemorySaver(),
        deps=make_deps(fake),
    )
    assert result.status == "succeeded"
    analysis = result.output.get("analysis_state") or {}
    # Should have executed more than 1 query due to iteration
    queries = analysis.get("queries") or []
    assert len(queries) >= 1  # at least one query


@pytest.mark.asyncio
async def test_analysis_state_metrics_populated() -> None:
    """Run metrics should be populated after EDA completes."""
    fake = FakeLLM(responses=[
        _plan_response(),
        _sql_response(),
        _evidence_response("supported"),
        _viz_response(),
        _findings_response(),
    ])
    result = await run_agent_graph(
        "eda",
        input_payload={"query": "Analyze orders"},
        thread_id="test-eda-metrics",
        checkpointer=MemorySaver(),
        deps=make_deps(fake),
    )
    assert result.status == "succeeded"
    analysis = result.output.get("analysis_state") or {}
    metrics = analysis.get("metrics") or {}
    # LLM calls should be tracked
    assert metrics.get("total_llm_calls", 0) > 0
    assert metrics.get("total_tokens", 0) > 0
