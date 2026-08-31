"""Evaluation harness — runs YAML suites from context/evaluations/ against graphs.

Scorers (per suite or per case):
    exact_sql        — normalized SQL string equality
    execution_match  — both queries executed on an in-memory DuckDB fixture,
                       result sets compared row-by-row
    exact_match      — equality on a structured output field (e.g. action)
    llm_judge        — LLM-as-judge score in [0, 1] against a rubric

Every case runs through the real graph with an isolated MemorySaver
checkpointer, so evaluations never pollute production threads, and (when a
database session is supplied) each case lands in the DecisionLog table with
its evaluation_score — the unified audit trail covers eval runs too.

CLI:
    uv run python eval_harness.py --all
    uv run python eval_harness.py --suite context/evaluations/analytics_suite.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import duckdb
import structlog
import yaml
from pydantic import BaseModel, Field

from clients.litellm_client import get_default_litellm_client
from clients.prompt_loader import get_default_prompt_loader
from config import get_settings
from graphs.base import run_agent_graph

logger = structlog.get_logger(__name__)

SCORERS = ("exact_sql", "execution_match", "exact_match", "llm_judge")


class EvalCase(BaseModel):
    id: str
    input: dict[str, Any]
    expected: dict[str, Any]
    scorer: str | None = None
    fixture: str | None = None
    weight: float = 1.0


class EvalSuite(BaseModel):
    suite: str
    mode: str
    scorer: str = "llm_judge"
    output_field: str | None = None
    threshold: float = 0.75
    fixture: str | None = None
    cases: list[EvalCase] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> EvalSuite:
        data = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate(data)


class CaseResult(BaseModel):
    id: str
    score: float
    passed: bool
    detail: str | None = None


class SuiteReport(BaseModel):
    suite: str
    mode: str
    total: int
    passed: int
    mean_score: float
    pass_rate: float
    duration_ms: int
    results: list[CaseResult]


# --------------------------------------------------------------------------
# scoring helpers
# --------------------------------------------------------------------------
def normalize_sql(sql: str) -> str:
    return " ".join(sql.lower().replace("`", "").rstrip(";").split())


def _fixture_rows(conn: duckdb.DuckDBPyConnection, sql: str) -> list[str]:
    cursor = conn.execute(sql)
    rows = cursor.fetchall()
    normalized = []
    for row in rows:
        normalized.append(
            repr(tuple(round(float(v), 6) if isinstance(v, (int, float)) else v for v in row))
        )
    return sorted(normalized)


def _run_fixture(conn: duckdb.DuckDBPyConnection, fixture: str) -> None:
    for statement in (s.strip() for s in fixture.split(";")):
        if statement:
            conn.execute(statement)


def _parse_judge_score(text: str) -> float:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return 0.0
    try:
        data = json.loads(match.group(0))
        return min(1.0, max(0.0, float(data.get("score", 0.0))))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0.0


class EvalRunner:
    """Runs evaluation suites against the compiled graphs."""

    def __init__(self, litellm: Any | None = None) -> None:
        self._litellm = litellm or get_default_litellm_client()

    async def run_suite_file(
        self,
        path: Path,
        db: Any | None = None,
        checkpointer: Any | None = None,
    ) -> SuiteReport:
        """Run one suite. ``db`` (optional) persists every case to DecisionLog."""
        from langgraph.checkpoint.memory import MemorySaver

        suite = EvalSuite.from_yaml(path)
        checkpointer = checkpointer or MemorySaver()
        started = time.perf_counter()
        results: list[CaseResult] = []

        for case in suite.cases:
            results.append(await self._run_case(suite, case, checkpointer, db))

        duration_ms = int((time.perf_counter() - started) * 1000)
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        mean = round(sum(r.score for r in results) / total, 4) if total else 0.0
        return SuiteReport(
            suite=suite.suite,
            mode=suite.mode,
            total=total,
            passed=passed,
            mean_score=mean,
            pass_rate=round(passed / total, 4) if total else 0.0,
            duration_ms=duration_ms,
            results=results,
        )

    async def _run_case(
        self, suite: EvalSuite, case: EvalCase, checkpointer: Any, db: Any | None
    ) -> CaseResult:
        input_payload = {**case.input, "eval": {"suite": suite.suite, "case": case.id}}
        thread_id = f"eval-{suite.suite}-{case.id}-{time.time_ns()}"
        result = await run_agent_graph(
            suite.mode,
            input_payload=input_payload,
            thread_id=thread_id,
            checkpointer=checkpointer,
            deps={"litellm_client": self._litellm, "prompt_loader": get_default_prompt_loader()},
            db=db,
        )
        scorer = case.scorer or suite.scorer
        if result.status == "failed":
            return CaseResult(id=case.id, score=0.0, passed=False, detail=result.error)
        score, detail = await self._score(scorer, suite, case, result.output)
        passed = score >= suite.threshold
        return CaseResult(id=case.id, score=round(score, 4), passed=passed, detail=detail)

    async def _score(
        self, scorer: str, suite: EvalSuite, case: EvalCase, output: dict[str, Any]
    ) -> tuple[float, str | None]:
        if scorer == "exact_sql":
            candidate = normalize_sql(str(output.get("generated_sql", "")))
            expected = normalize_sql(str(case.expected.get("sql", "")))
            return (
                1.0 if candidate == expected else 0.0
            ), None if candidate == expected else "candidate != expected"

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
            return (
                1.0 if candidate == expected else 0.0
            ), f"{field}: {candidate!r} vs {expected!r}"

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


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _print_report(report: SuiteReport) -> None:
    print(f"\n=== {report.suite} (mode={report.mode}) ===")
    for case in report.results:
        marker = "PASS" if case.passed else "FAIL"
        print(
            f"  [{marker}] {case.id:<32} score={case.score:.3f}"
            + (f"  ({case.detail})" if case.detail else "")
        )
    print(
        f"  -> {report.passed}/{report.total} passed · "
        f"mean score {report.mean_score:.3f} · pass rate {report.pass_rate:.1%} · "
        f"{report.duration_ms} ms"
    )


async def _run_cli(suites: list[Path], persist: bool) -> int:
    runner = EvalRunner()
    db: Any | None = None
    if persist:
        from database import AsyncSessionLocal

        try:
            db = AsyncSessionLocal()
        except Exception:
            logger.warning("eval_db_unavailable_scoring_only")
    exit_code = 0
    try:
        for path in suites:
            report = await runner.run_suite_file(path, db=db)
            _print_report(report)
            if report.total and report.pass_rate < 1.0:
                exit_code = 1
    finally:
        if db is not None:
            await db.commit()
            await db.close()
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Run agent evaluation suites")
    parser.add_argument("--suite", type=str, help="path to a suite YAML (relative to backend/)")
    parser.add_argument(
        "--all", action="store_true", help="run every suite in context/evaluations/"
    )
    parser.add_argument("--no-persist", action="store_true", help="skip DecisionLog persistence")
    args = parser.parse_args()

    context_dir = get_settings().context_path
    if args.all:
        suites = sorted((context_dir / "evaluations").glob("*.yaml"))
    elif args.suite:
        suites = [Path(args.suite)]
    else:
        parser.error("specify --suite PATH or --all")
        return

    if not suites:
        print("no evaluation suites found", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_run_cli(suites, persist=not args.no_persist)))


if __name__ == "__main__":
    main()
