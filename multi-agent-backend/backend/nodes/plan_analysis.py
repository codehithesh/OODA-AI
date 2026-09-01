"""LangGraph node: plan the analytical approach for a broad business question.

For direct lookup questions (e.g. "what is total revenue today?") this node
produces a minimal single-query plan and skips the iterative EDA loop.

For broad exploratory questions (e.g. "how can we increase revenue?") this node:
1. Breaks the question into 3–8 analytical sub-questions.
2. Proposes initial hypotheses based on available schema.
3. Decides whether external web research would add value.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig

from analysis_state import AnalysisState, DataReadinessStatus, RunMetrics
from clients.duckdb_client import get_duckdb_client
from clients.litellm_client import get_litellm_client
from clients.prompt_loader import get_prompt_loader
from tools.warehouse_tool import InspectSchemaTool

logger = structlog.get_logger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any]:
    match = _JSON_RE.search(text)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def plan_analysis(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Plan the analytical approach for the user's business question.

    Input state keys:
        query: the user's business question
        context: ContextBundle (schema DDL, rules, prompts)

    Output state keys:
        analysis_state: initialised AnalysisState with plan + hypotheses
        usage: appended LLM usage record
    """
    t0 = time.perf_counter()
    query = str(state.get("query", ""))
    context = state.get("context") or {}
    ddl = (context.get("schemas") or {}).get("analytics_warehouse", "")

    llm = get_litellm_client(dict(config) if config else None)
    loader = get_prompt_loader(dict(config) if config else None)

    # Get live schema if DuckDB is available
    schema_tool = InspectSchemaTool()
    try:
        schema_result = await schema_tool.run({})
        if schema_result.succeeded:
            ddl_extra = "\n".join(
                f"-- Table: {t['name']}\n-- Columns: "
                + ", ".join(f"{c['name']} ({c['type']})" for c in t.get("columns", []))
                for t in (schema_result.output or {}).get("tables", [])
            )
            ddl = ddl or ddl_extra
    except Exception:
        pass

    # Render planning prompt
    try:
        prompt = loader.render("eda/plan_analysis.md", question=query, schema_ddl=ddl)
    except Exception:
        # Fallback: minimal prompt
        prompt = (
            f"Create a brief analysis plan for this question: {query}\n"
            f"Schema: {ddl[:1000]}\n"
            "Respond with JSON: {question_type, analysis_plan, sub_questions, "
            "initial_hypotheses, needs_web_research, web_research_rationale, relevant_tables}"
        )

    response = await llm.chat([{"role": "user", "content": prompt}], temperature=0.2)
    plan_data = _parse_json(response.content)

    latency_ms = int((time.perf_counter() - t0) * 1000)

    # Build AnalysisState
    run_id = str(uuid.uuid4())
    metrics = RunMetrics(run_id=run_id)
    metrics.add_llm_usage(response.usage_record())
    metrics.total_latency_ms = latency_ms

    analysis = AnalysisState(
        run_id=run_id,
        business_question=query,
        analysis_plan=str(plan_data.get("analysis_plan", query)),
        sub_questions=list(plan_data.get("sub_questions") or [query]),
        warehouse_schema={"ddl": ddl},
        needs_web_research=bool(plan_data.get("needs_web_research", False)),
        web_research_rationale=str(plan_data.get("web_research_rationale", "")),
        data_readiness=DataReadinessStatus.UNKNOWN,
        metrics=metrics,
    )

    # Add initial hypotheses
    for h_data in (plan_data.get("initial_hypotheses") or []):
        if isinstance(h_data, dict) and h_data.get("statement"):
            analysis.add_hypothesis(
                statement=str(h_data["statement"]),
                required_evidence=list(h_data.get("required_evidence") or []),
            )

    # If no hypotheses produced (LLM failure), add a default one
    if not analysis.hypotheses:
        analysis.add_hypothesis(
            statement=f"There are identifiable patterns in the data relevant to: {query}",
            required_evidence=["Query the available tables to find relevant data"],
        )
        analysis.record_failure(
            mode=__import__("analysis_state").FailureMode.PLANNING_FAILURE,
            detail="LLM did not produce structured plan; using fallback",
            step="plan_analysis",
        )

    logger.info(
        "analysis_planned",
        run_id=run_id,
        question_type=plan_data.get("question_type", "unknown"),
        sub_questions=len(analysis.sub_questions),
        hypotheses=len(analysis.hypotheses),
        needs_web_research=analysis.needs_web_research,
    )

    return {
        "analysis_state": analysis.model_dump(),
        "usage": [response.usage_record()],
    }
