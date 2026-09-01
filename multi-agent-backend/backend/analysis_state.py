"""AnalysisState — structured state tracking for iterative EDA runs.

Every field in this model maps to a concrete phase of the analytical pipeline:

    business_question → analysis_plan → hypotheses → queries → results
    → evidence → decisions → external_context → fusion → findings
    → visualizations → recommendations → n8n_actions

The state is serializable to JSON and stored as the ``output`` field of the
DecisionLog row (in addition to the LangGraph checkpoint), so every analysis
run is traceable and reproducible without relying on unstructured chat history.

ObservabilityMetrics captures per-step timing, token usage, and cost so the
system satisfies requirements 17 (tool calling), 18 (cost observability),
and the benchmark driver can extract per-run metrics.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class EvidenceType(str, Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"
    FUSED = "fused"


class HypothesisStatus(str, Enum):
    PENDING = "pending"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    REFINED = "refined"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class DataReadinessStatus(str, Enum):
    UNKNOWN = "unknown"
    NOT_READY = "not_ready"
    READY = "ready"
    PARTIAL = "partial"


class FailureMode(str, Enum):
    PLANNING_FAILURE = "planning_failure"
    PLAN_ERROR = "plan_error"
    DATA_SELECTION_ERROR = "data_selection_error"
    IMPLEMENTATION_ERROR = "implementation_error"
    RUNTIME_ERROR = "runtime_error"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------
class Hypothesis(BaseModel):
    id: str
    statement: str
    required_evidence: list[str] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.0  # 0–1
    status: HypothesisStatus = HypothesisStatus.PENDING
    iteration: int = 0
    refined_from: str | None = None  # id of parent hypothesis


class QueryRecord(BaseModel):
    id: str
    sql: str
    rationale: str = ""
    hypothesis_id: str | None = None
    sub_question: str = ""
    executed: bool = False
    row_count: int = 0
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0
    iteration: int = 0


class WebSearchRecord(BaseModel):
    id: str
    query: str
    results: list[dict[str, Any]] = Field(default_factory=list)
    relevance_note: str = ""
    latency_ms: int = 0


class FusedContext(BaseModel):
    """Result of the context-fusion layer."""
    internal_summary: str = ""
    external_summary: str = ""
    fusion_notes: str = ""
    comparability_issues: list[str] = Field(default_factory=list)
    internal_evidence: list[str] = Field(default_factory=list)
    external_evidence: list[str] = Field(default_factory=list)
    combined_insights: list[str] = Field(default_factory=list)


class DataQualityCheck(BaseModel):
    table: str
    issues: list[str] = Field(default_factory=list)
    null_rates: dict[str, float] = Field(default_factory=dict)
    row_count: int = 0
    ready: bool = True
    notes: str = ""


class Visualization(BaseModel):
    id: str
    chart_type: str
    title: str
    plotly_spec: str = ""  # JSON string
    related_query_id: str | None = None
    related_hypothesis_id: str | None = None
    description: str = ""


class Finding(BaseModel):
    id: str
    statement: str
    evidence_type: EvidenceType = EvidenceType.INTERNAL
    confidence: float = 0.0
    supporting_queries: list[str] = Field(default_factory=list)
    supporting_hypotheses: list[str] = Field(default_factory=list)
    is_fact: bool = False       # observed directly from data
    is_inference: bool = False  # derived / interpolated


class Recommendation(BaseModel):
    id: str
    recommendation: str
    supporting_evidence: list[str] = Field(default_factory=list)
    expected_impact: str = ""
    confidence: float = 0.0
    assumptions: list[str] = Field(default_factory=list)
    related_visualization_id: str | None = None
    suggested_action: str = ""
    priority: int = 1  # 1 = highest


class N8nActionRecord(BaseModel):
    workflow_name: str
    payload: dict[str, Any] = Field(default_factory=dict)
    triggered_at: str = ""
    success: bool = False
    response: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-step observability
# ---------------------------------------------------------------------------
class StepMetrics(BaseModel):
    step: str
    latency_ms: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    sql_queries: int = 0
    web_searches: int = 0
    retries: int = 0


class RunMetrics(BaseModel):
    """Aggregate observability for a complete EDA run."""
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    total_latency_ms: int = 0
    llm_latency_ms: int = 0
    tool_latency_ms: int = 0
    total_llm_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_sql_queries: int = 0
    sql_latency_ms: int = 0
    total_web_searches: int = 0
    total_api_calls: int = 0
    total_retries: int = 0
    total_tool_calls: int = 0
    total_iterations: int = 0
    steps: list[StepMetrics] = Field(default_factory=list)

    def add_step(self, step: StepMetrics) -> None:
        self.steps.append(step)
        self.total_llm_calls += step.llm_calls
        self.total_input_tokens += step.input_tokens
        self.total_output_tokens += step.output_tokens
        self.total_tokens += step.total_tokens
        self.total_cost_usd = round(self.total_cost_usd + step.cost_usd, 8)
        self.total_sql_queries += step.sql_queries
        self.total_web_searches += step.web_searches
        self.total_tool_calls += step.tool_calls
        self.total_retries += step.retries
        self.llm_latency_ms += step.latency_ms if step.llm_calls > 0 else 0
        self.sql_latency_ms += step.latency_ms if step.sql_queries > 0 else 0

    def add_llm_usage(self, usage: dict[str, Any]) -> None:
        """Merge a single LLM usage record into run totals."""
        self.total_llm_calls += 1
        self.total_input_tokens += int(usage.get("prompt_tokens", 0))
        self.total_output_tokens += int(usage.get("completion_tokens", 0))
        self.total_tokens += int(usage.get("total_tokens", 0))
        self.total_cost_usd = round(
            self.total_cost_usd + float(usage.get("cost_usd", 0.0)), 8
        )

    def summary(self) -> str:
        return (
            f"Analysis completed in {self.total_latency_ms}ms · "
            f"{self.total_tool_calls} tool calls · "
            f"{self.total_tokens:,} tokens · "
            f"${self.total_cost_usd:.4f} estimated cost"
        )


# ---------------------------------------------------------------------------
# ToolCallRecord
# ---------------------------------------------------------------------------
class ToolCallRecord(BaseModel):
    tool: str
    status: str
    latency_ms: int = 0
    retries: int = 0
    error_type: str | None = None
    error: str | None = None
    iteration: int = 0
    rationale: str = ""


# ---------------------------------------------------------------------------
# Root AnalysisState
# ---------------------------------------------------------------------------
class AnalysisState(BaseModel):
    """Complete state for one iterative EDA/analysis run.

    This object is embedded in GraphState['analysis_state'] and serialized
    to the DecisionLog output column after the run completes.
    """

    run_id: str = ""

    # ---- Inputs
    business_question: str = ""
    selected_warehouse: str = "duckdb"
    selected_datasets: list[str] = Field(default_factory=list)

    # ---- Planning
    analysis_plan: str = ""
    sub_questions: list[str] = Field(default_factory=list)

    # ---- Warehouse metadata
    warehouse_schema: dict[str, Any] = Field(default_factory=dict)

    # ---- Data quality
    data_quality_checks: list[DataQualityCheck] = Field(default_factory=list)
    data_readiness: DataReadinessStatus = DataReadinessStatus.UNKNOWN
    data_readiness_notes: str = ""

    # ---- Hypothesis loop
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    current_iteration: int = 0
    max_iterations: int = 5

    # ---- Queries & results
    queries: list[QueryRecord] = Field(default_factory=list)

    # ---- External context
    web_searches: list[WebSearchRecord] = Field(default_factory=list)
    needs_web_research: bool = False
    web_research_rationale: str = ""

    # ---- Fusion
    fused_context: FusedContext | None = None

    # ---- Findings
    findings: list[Finding] = Field(default_factory=list)

    # ---- Visualizations
    visualizations: list[Visualization] = Field(default_factory=list)

    # ---- Recommendations
    recommendations: list[Recommendation] = Field(default_factory=list)

    # ---- Actions
    n8n_actions: list[N8nActionRecord] = Field(default_factory=list)

    # ---- Tool call trace
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)

    # ---- Failure classification
    failure_modes: list[FailureMode] = Field(default_factory=list)
    failures: list[dict[str, Any]] = Field(default_factory=list)

    # ---- Observability
    metrics: RunMetrics = Field(default_factory=RunMetrics)

    # ---- Terminal state flag
    analysis_complete: bool = False
    termination_reason: str = ""

    def add_hypothesis(self, statement: str, required_evidence: list[str] | None = None) -> Hypothesis:
        h = Hypothesis(
            id=f"h{len(self.hypotheses) + 1}",
            statement=statement,
            required_evidence=required_evidence or [],
            iteration=self.current_iteration,
        )
        self.hypotheses.append(h)
        return h

    def add_query(
        self,
        sql: str,
        rationale: str = "",
        hypothesis_id: str | None = None,
        sub_question: str = "",
    ) -> QueryRecord:
        q = QueryRecord(
            id=f"q{len(self.queries) + 1}",
            sql=sql,
            rationale=rationale,
            hypothesis_id=hypothesis_id,
            sub_question=sub_question,
            iteration=self.current_iteration,
        )
        self.queries.append(q)
        return q

    def add_finding(
        self,
        statement: str,
        evidence_type: EvidenceType = EvidenceType.INTERNAL,
        confidence: float = 0.5,
        is_fact: bool = False,
        is_inference: bool = False,
    ) -> Finding:
        f = Finding(
            id=f"f{len(self.findings) + 1}",
            statement=statement,
            evidence_type=evidence_type,
            confidence=confidence,
            is_fact=is_fact,
            is_inference=is_inference,
        )
        self.findings.append(f)
        return f

    def add_recommendation(
        self,
        recommendation: str,
        supporting_evidence: list[str] | None = None,
        expected_impact: str = "",
        confidence: float = 0.5,
        priority: int = 1,
    ) -> Recommendation:
        r = Recommendation(
            id=f"r{len(self.recommendations) + 1}",
            recommendation=recommendation,
            supporting_evidence=supporting_evidence or [],
            expected_impact=expected_impact,
            confidence=confidence,
            priority=priority,
        )
        self.recommendations.append(r)
        return r

    def record_tool_call(self, record: dict[str, Any]) -> None:
        self.tool_calls.append(
            ToolCallRecord(
                **{k: v for k, v in record.items() if k in ToolCallRecord.model_fields},
                iteration=self.current_iteration,
            )
        )

    def record_failure(self, mode: FailureMode, detail: str, step: str = "") -> None:
        if mode not in self.failure_modes:
            self.failure_modes.append(mode)
        self.failures.append({"mode": mode, "detail": detail, "step": step})

    def pending_hypotheses(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.status == HypothesisStatus.PENDING]

    def should_continue(self) -> bool:
        """True when there is more work to do and we haven't hit the iteration budget."""
        if self.current_iteration >= self.max_iterations:
            return False
        if self.analysis_complete:
            return False
        return bool(self.pending_hypotheses())

    def to_summary(self) -> dict[str, Any]:
        """Compact summary for DecisionLog output column."""
        return {
            "business_question": self.business_question,
            "analysis_plan": self.analysis_plan[:500],
            "hypothesis_count": len(self.hypotheses),
            "query_count": len(self.queries),
            "web_search_count": len(self.web_searches),
            "finding_count": len(self.findings),
            "visualization_count": len(self.visualizations),
            "recommendation_count": len(self.recommendations),
            "data_readiness": self.data_readiness,
            "iterations": self.current_iteration,
            "failure_modes": [f.value for f in self.failure_modes],
            "metrics_summary": self.metrics.summary(),
            "analysis_complete": self.analysis_complete,
            "termination_reason": self.termination_reason,
        }
