"""Benchmark framework — extended evaluation with failure taxonomy.

Extends the base eval_harness with:

* Failure classification (5 canonical failure modes)
* Multi-category benchmark support (Text2SQL, TableQA, EDA, cross-database, extraction)
* Per-run observability (tokens, cost, latency, tool calls)
* Benchmark dashboard / report generation
* Regression comparison (current vs baseline)

Usage::

    uv run python benchmark.py --suite eda_suite
    uv run python benchmark.py --all --category eda
    uv run python benchmark.py --compare baseline.json current.json
    uv run python benchmark.py --all --report benchmark_report.json

"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, Field

from clients.litellm_client import get_default_litellm_client
from clients.prompt_loader import get_default_prompt_loader
from config import get_settings
from graphs.base import run_agent_graph
from analysis_state import FailureMode

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------
FAILURE_MODES = {
    FailureMode.PLANNING_FAILURE: "planning_failure_rate",
    FailureMode.PLAN_ERROR: "plan_error_rate",
    FailureMode.DATA_SELECTION_ERROR: "data_selection_error_rate",
    FailureMode.IMPLEMENTATION_ERROR: "implementation_error_rate",
    FailureMode.RUNTIME_ERROR: "runtime_error_rate",
    FailureMode.SEMANTIC_MISUNDERSTANDING: "semantic_misunderstanding_rate",
}


class FailureRecord(BaseModel):
    run_id: str
    mode: str
    failure_mode: str
    detail: str
    step: str = ""


# ---------------------------------------------------------------------------
# Extended case result
# ---------------------------------------------------------------------------
class BenchmarkCaseResult(BaseModel):
    id: str
    category: str = "general"
    score: float
    passed: bool
    detail: str | None = None
    # Observability
    latency_ms: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    sql_queries: int = 0
    iterations: int = 0
    # Failure classification
    failure_modes: list[str] = Field(default_factory=list)
    failures: list[dict[str, Any]] = Field(default_factory=list)
    # Planning / data selection
    plan_produced: bool = False
    hypothesis_count: int = 0
    finding_count: int = 0
    recommendation_count: int = 0


# ---------------------------------------------------------------------------
# Benchmark suite report
# ---------------------------------------------------------------------------
class BenchmarkReport(BaseModel):
    suite: str
    mode: str
    category: str = "general"
    total: int = 0
    passed: int = 0
    mean_score: float = 0.0
    pass_rate: float = 0.0
    duration_ms: int = 0
    # Aggregated observability
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    avg_tokens: float = 0.0
    avg_cost_usd: float = 0.0
    avg_tool_calls: float = 0.0
    avg_iterations: float = 0.0
    # Failure rates (requirement 28–29)
    planning_failure_rate: float = 0.0
    plan_error_rate: float = 0.0
    data_selection_error_rate: float = 0.0
    implementation_error_rate: float = 0.0
    runtime_error_rate: float = 0.0
    semantic_misunderstanding_rate: float = 0.0
    results: list[BenchmarkCaseResult] = Field(default_factory=list)

    def compute_aggregates(self) -> None:
        n = len(self.results)
        if n == 0:
            return
        latencies = sorted(r.latency_ms for r in self.results)
        self.avg_latency_ms = round(sum(latencies) / n, 1)
        self.p50_latency_ms = float(latencies[int(n * 0.5)])
        self.p95_latency_ms = float(latencies[min(int(n * 0.95), n - 1)])
        self.avg_tokens = round(sum(r.total_tokens for r in self.results) / n, 1)
        self.avg_cost_usd = round(sum(r.cost_usd for r in self.results) / n, 6)
        self.avg_tool_calls = round(sum(r.tool_calls for r in self.results) / n, 1)
        self.avg_iterations = round(sum(r.iterations for r in self.results) / n, 1)
        # Failure rates
        fm_counts: dict[str, int] = defaultdict(int)
        for r in self.results:
            for fm in r.failure_modes:
                fm_counts[fm] += 1
        self.planning_failure_rate = round(fm_counts.get("planning_failure", 0) / n, 4)
        self.plan_error_rate = round(fm_counts.get("plan_error", 0) / n, 4)
        self.data_selection_error_rate = round(fm_counts.get("data_selection_error", 0) / n, 4)
        self.implementation_error_rate = round(fm_counts.get("implementation_error", 0) / n, 4)
        self.runtime_error_rate = round(fm_counts.get("runtime_error", 0) / n, 4)
        self.semantic_misunderstanding_rate = round(fm_counts.get("semantic_misunderstanding", 0) / n, 4)
        self.passed = sum(1 for r in self.results if r.passed)
        self.pass_rate = round(self.passed / n, 4) if n > 0 else 0.0
        self.mean_score = round(sum(r.score for r in self.results) / n, 4) if n > 0 else 0.0


# ---------------------------------------------------------------------------
# Overall benchmark dashboard
# ---------------------------------------------------------------------------
class BenchmarkDashboard(BaseModel):
    """Aggregate report across all suites (requirement 30)."""
    run_timestamp: str = ""
    total_cases: int = 0
    total_passed: int = 0
    overall_pass_rate: float = 0.0
    overall_mean_score: float = 0.0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    avg_tokens: float = 0.0
    avg_cost_usd: float = 0.0
    # Failure taxonomy aggregates
    planning_failure_rate: float = 0.0
    plan_error_rate: float = 0.0
    data_selection_error_rate: float = 0.0
    implementation_error_rate: float = 0.0
    runtime_error_rate: float = 0.0
    semantic_misunderstanding_rate: float = 0.0
    # Per-category breakdown
    by_category: dict[str, Any] = Field(default_factory=dict)
    # Per-suite results
    suite_reports: list[BenchmarkReport] = Field(default_factory=list)

    def compute(self) -> None:
        from datetime import UTC, datetime
        self.run_timestamp = datetime.now(UTC).isoformat()
        all_results = [r for s in self.suite_reports for r in s.results]
        n = len(all_results)
        if n == 0:
            return
        self.total_cases = n
        self.total_passed = sum(1 for r in all_results if r.passed)
        self.overall_pass_rate = round(self.total_passed / n, 4)
        self.overall_mean_score = round(sum(r.score for r in all_results) / n, 4)
        latencies = sorted(r.latency_ms for r in all_results)
        self.avg_latency_ms = round(sum(latencies) / n, 1)
        self.p95_latency_ms = float(latencies[min(int(n * 0.95), n - 1)])
        self.avg_tokens = round(sum(r.total_tokens for r in all_results) / n, 1)
        self.avg_cost_usd = round(sum(r.cost_usd for r in all_results) / n, 6)
        # Failure aggregates
        fm_counts: dict[str, int] = defaultdict(int)
        for r in all_results:
            for fm in r.failure_modes:
                fm_counts[fm] += 1
        self.planning_failure_rate = round(fm_counts.get("planning_failure", 0) / n, 4)
        self.plan_error_rate = round(fm_counts.get("plan_error", 0) / n, 4)
        self.data_selection_error_rate = round(fm_counts.get("data_selection_error", 0) / n, 4)
        self.implementation_error_rate = round(fm_counts.get("implementation_error", 0) / n, 4)
        self.runtime_error_rate = round(fm_counts.get("runtime_error", 0) / n, 4)
        self.semantic_misunderstanding_rate = round(fm_counts.get("semantic_misunderstanding", 0) / n, 4)
        # By category
        by_cat: dict[str, list[BenchmarkCaseResult]] = defaultdict(list)
        for r in all_results:
            by_cat[r.category].append(r)
        for cat, results in by_cat.items():
            cn = len(results)
            self.by_category[cat] = {
                "total": cn,
                "passed": sum(1 for r in results if r.passed),
                "pass_rate": round(sum(1 for r in results if r.passed) / cn, 4),
                "mean_score": round(sum(r.score for r in results) / cn, 4),
                "avg_latency_ms": round(sum(r.latency_ms for r in results) / cn, 1),
            }


# ---------------------------------------------------------------------------
# Extended benchmark runner
# ---------------------------------------------------------------------------
class BenchmarkRunner:
    """Extended runner that classifies failures and captures observability."""

    def __init__(self, litellm: Any | None = None) -> None:
        self._litellm = litellm or get_default_litellm_client()

    async def run_suite(self, path: Path, category: str | None = None) -> BenchmarkReport:
        from langgraph.checkpoint.memory import MemorySaver
        from eval_harness import EvalSuite, normalize_sql, _run_fixture, _fixture_rows, _parse_judge_score
        import duckdb

        suite = EvalSuite.from_yaml(path)
        checkpointer = MemorySaver()
        started = time.perf_counter()
        results: list[BenchmarkCaseResult] = []

        for case in suite.cases:
            result = await self._run_case(suite, case, checkpointer, duckdb, _fixture_rows, _run_fixture, normalize_sql, _parse_judge_score)
            result.category = category or suite.mode
            results.append(result)

        duration_ms = int((time.perf_counter() - started) * 1000)
        report = BenchmarkReport(
            suite=suite.suite,
            mode=suite.mode,
            category=category or suite.mode,
            total=len(results),
            duration_ms=duration_ms,
            results=results,
        )
        report.compute_aggregates()
        return report

    async def _run_case(
        self,
        suite: Any,
        case: Any,
        checkpointer: Any,
        duckdb: Any,
        _fixture_rows: Any,
        _run_fixture: Any,
        normalize_sql: Any,
        _parse_judge_score: Any,
    ) -> BenchmarkCaseResult:
        input_payload = {**case.input, "eval": {"suite": suite.suite, "case": case.id}}
        thread_id = f"bench-{suite.suite}-{case.id}-{time.time_ns()}"
        result = await run_agent_graph(
            suite.mode,
            input_payload=input_payload,
            thread_id=thread_id,
            checkpointer=checkpointer,
            deps={"litellm_client": self._litellm, "prompt_loader": get_default_prompt_loader()},
        )

        # Extract observability metrics
        latency_ms = result.latency_ms
        decision_record = result.decision_record or {}
        total_tokens = int(decision_record.get("prompt_tokens", 0)) + int(decision_record.get("completion_tokens", 0))
        cost_usd = float(decision_record.get("cost_usd", 0.0))

        # Extract EDA-specific metrics
        analysis_data = result.output.get("analysis_state") or {}
        metrics_data = analysis_data.get("metrics") or {}
        tool_calls = int(metrics_data.get("total_tool_calls", 0))
        sql_queries = int(metrics_data.get("total_sql_queries", 0))
        iterations = int(metrics_data.get("total_iterations", 0))
        failure_modes = analysis_data.get("failure_modes") or []
        failures = analysis_data.get("failures") or []

        # Use tokens from analysis_state if available (more accurate)
        if metrics_data.get("total_tokens"):
            total_tokens = int(metrics_data["total_tokens"])
            cost_usd = float(metrics_data.get("total_cost_usd", cost_usd))

        if result.status == "failed":
            return BenchmarkCaseResult(
                id=case.id,
                score=0.0,
                passed=False,
                detail=result.error,
                latency_ms=latency_ms,
                failure_modes=["runtime_error"],
                failures=[{"mode": "runtime_error", "detail": result.error or "", "step": "run_agent_graph"}],
            )

        # Score the result
        scorer = case.scorer or suite.scorer
        try:
            score, detail = await self._score(
                scorer, suite, case, result.output, duckdb, _fixture_rows, _run_fixture, normalize_sql, _parse_judge_score
            )
        except Exception as exc:
            score, detail = 0.0, str(exc)

        passed = score >= suite.threshold

        return BenchmarkCaseResult(
            id=case.id,
            score=round(score, 4),
            passed=passed,
            detail=detail,
            latency_ms=latency_ms,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            tool_calls=tool_calls,
            sql_queries=sql_queries,
            iterations=iterations,
            failure_modes=[str(fm) for fm in failure_modes],
            failures=failures,
            plan_produced=bool(analysis_data.get("analysis_plan")),
            hypothesis_count=len(analysis_data.get("hypotheses") or []),
            finding_count=len(analysis_data.get("findings") or []),
            recommendation_count=len(analysis_data.get("recommendations") or []),
        )

    async def _score(
        self, scorer: str, suite: Any, case: Any, output: dict[str, Any],
        duckdb: Any, _fixture_rows: Any, _run_fixture: Any, normalize_sql: Any, _parse_judge_score: Any,
    ) -> tuple[float, str | None]:
        if scorer == "exact_sql":
            candidate = normalize_sql(str(output.get("generated_sql", "")))
            expected = normalize_sql(str(case.expected.get("sql", "")))
            return (1.0 if candidate == expected else 0.0), None

        if scorer == "execution_match":
            fixture = case.fixture or suite.fixture or ""
            with duckdb.connect(":memory:") as conn:
                _run_fixture(conn, fixture)
                candidate_rows = _fixture_rows(conn, str(output.get("generated_sql", "")))
                expected_rows = _fixture_rows(conn, str(case.expected.get("sql", "")))
            matched = candidate_rows == expected_rows
            return (1.0 if matched else 0.0), None if matched else "result sets differ"

        if scorer == "exact_match":
            field = suite.output_field or "action"
            candidate = output.get(field)
            expected = case.expected.get(field)
            return (1.0 if candidate == expected else 0.0), f"{field}: {candidate!r} vs {expected!r}"

        if scorer == "llm_judge":
            loader = get_default_prompt_loader()
            prompt = loader.render(
                "eval/judge.md",
                input=case.input,
                expected=case.expected,
                output=output,
                rubric=case.expected.get("rubric", ""),
            )
            response = await self._litellm.chat([{"role": "user", "content": prompt}])
            return _parse_judge_score(response.content), response.content[:200]

        raise ValueError(f"unknown scorer: {scorer}")


# ---------------------------------------------------------------------------
# Regression comparison
# ---------------------------------------------------------------------------
def compare_reports(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compare two benchmark reports and surface regressions."""
    def delta(old: Any, new: Any, fmt: str = ".3f") -> str:
        try:
            d = float(new) - float(old)
            sign = "+" if d >= 0 else ""
            return f"{sign}{d:{fmt}}"
        except (TypeError, ValueError):
            return "N/A"

    return {
        "pass_rate": {
            "baseline": baseline.get("overall_pass_rate"),
            "current": current.get("overall_pass_rate"),
            "delta": delta(baseline.get("overall_pass_rate", 0), current.get("overall_pass_rate", 0)),
        },
        "mean_score": {
            "baseline": baseline.get("overall_mean_score"),
            "current": current.get("overall_mean_score"),
            "delta": delta(baseline.get("overall_mean_score", 0), current.get("overall_mean_score", 0)),
        },
        "avg_latency_ms": {
            "baseline": baseline.get("avg_latency_ms"),
            "current": current.get("avg_latency_ms"),
            "delta": delta(baseline.get("avg_latency_ms", 0), current.get("avg_latency_ms", 0), ".0f"),
        },
        "avg_cost_usd": {
            "baseline": baseline.get("avg_cost_usd"),
            "current": current.get("avg_cost_usd"),
            "delta": delta(baseline.get("avg_cost_usd", 0), current.get("avg_cost_usd", 0), ".6f"),
        },
        "planning_failure_rate": {
            "baseline": baseline.get("planning_failure_rate"),
            "current": current.get("planning_failure_rate"),
            "delta": delta(
                baseline.get("planning_failure_rate", 0),
                current.get("planning_failure_rate", 0),
            ),
        },
        "regressions_detected": (
            current.get("overall_pass_rate", 1.0) < baseline.get("overall_pass_rate", 0.0) - 0.05
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_report(report: BenchmarkReport) -> None:
    print(f"\n=== {report.suite} (mode={report.mode}, category={report.category}) ===")
    for r in report.results:
        marker = "PASS" if r.passed else "FAIL"
        fm = f" [{', '.join(r.failure_modes)}]" if r.failure_modes else ""
        print(
            f"  [{marker}] {r.id:<40} score={r.score:.3f} "
            f"{r.latency_ms}ms {r.total_tokens}tok ${r.cost_usd:.4f}{fm}"
        )
    print(
        f"  → {report.passed}/{report.total} passed · "
        f"mean={report.mean_score:.3f} · pass_rate={report.pass_rate:.1%} · "
        f"{report.duration_ms}ms"
    )
    print(
        f"  Failures: planning={report.planning_failure_rate:.1%} "
        f"plan={report.plan_error_rate:.1%} "
        f"data_selection={report.data_selection_error_rate:.1%} "
        f"impl={report.implementation_error_rate:.1%} "
        f"runtime={report.runtime_error_rate:.1%}"
    )


def _print_dashboard(dashboard: BenchmarkDashboard) -> None:
    print(f"\n{'='*60}")
    print("BENCHMARK DASHBOARD")
    print(f"{'='*60}")
    print(f"Overall: {dashboard.total_passed}/{dashboard.total_cases} passed "
          f"({dashboard.overall_pass_rate:.1%}) · mean={dashboard.overall_mean_score:.3f}")
    print(f"Performance: avg={dashboard.avg_latency_ms:.0f}ms p95={dashboard.p95_latency_ms:.0f}ms")
    print(f"Cost: avg={dashboard.avg_tokens:.0f} tokens · ${dashboard.avg_cost_usd:.4f}/run")
    print("\nFailure rates:")
    print(f"  Planning failures:    {dashboard.planning_failure_rate:.1%}")
    print(f"  Plan errors:          {dashboard.plan_error_rate:.1%}")
    print(f"  Data selection errors:{dashboard.data_selection_error_rate:.1%}")
    print(f"  Implementation errors:{dashboard.implementation_error_rate:.1%}")
    print(f"  Runtime errors:       {dashboard.runtime_error_rate:.1%}")
    if dashboard.by_category:
        print("\nBy category:")
        for cat, stats in sorted(dashboard.by_category.items()):
            print(f"  {cat}: {stats['passed']}/{stats['total']} "
                  f"({stats['pass_rate']:.1%}) · mean={stats['mean_score']:.3f}")


async def _run_cli(suites: list[Path], category: str | None, report_path: str | None) -> int:
    runner = BenchmarkRunner()
    dashboard = BenchmarkDashboard()
    exit_code = 0

    for path in suites:
        report = await runner.run_suite(path, category=category)
        _print_report(report)
        dashboard.suite_reports.append(report)
        if report.total and report.pass_rate < 1.0:
            exit_code = 1

    dashboard.compute()
    _print_dashboard(dashboard)

    if report_path:
        Path(report_path).write_text(dashboard.model_dump_json(indent=2))
        print(f"\nReport written to: {report_path}")

    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Run benchmark evaluation suites")
    parser.add_argument("--suite", type=str, help="path to a suite YAML")
    parser.add_argument("--all", action="store_true", help="run all suites")
    parser.add_argument("--category", type=str, help="filter/tag results by category")
    parser.add_argument("--report", type=str, help="write JSON report to this path")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "CURRENT"),
                        help="compare two JSON benchmark reports")
    args = parser.parse_args()

    if args.compare:
        baseline = json.loads(Path(args.compare[0]).read_text())
        current = json.loads(Path(args.compare[1]).read_text())
        diff = compare_reports(baseline, current)
        print(json.dumps(diff, indent=2))
        sys.exit(1 if diff.get("regressions_detected") else 0)

    context_dir = get_settings().context_path
    if args.all:
        suites = sorted((context_dir / "evaluations").glob("*.yaml"))
    elif args.suite:
        suites = [Path(args.suite)]
    else:
        parser.error("specify --suite PATH, --all, or --compare")
        return

    if not suites:
        print("no evaluation suites found", file=sys.stderr)
        sys.exit(2)

    sys.exit(asyncio.run(_run_cli(suites, args.category, args.report)))


if __name__ == "__main__":
    main()
