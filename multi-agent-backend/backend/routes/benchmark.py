"""Benchmark routes — extended evaluation with failure taxonomy and dashboard.

GET  /v1/benchmark/suites             — list benchmark suites
POST /v1/benchmark/runs               — launch a benchmark run (background)
GET  /v1/benchmark/runs/{run_id}      — poll run status
GET  /v1/benchmark/dashboard          — latest aggregated dashboard
POST /v1/benchmark/compare            — compare two run results
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from benchmark import BenchmarkDashboard, BenchmarkReport, BenchmarkRunner, compare_reports
from clients.redis_client import get_redis_client
from graphs.base import get_agent_deps
from schemas import EvalSuiteInfo

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/benchmark", tags=["benchmark"])


class BenchmarkRunRequest(BaseModel):
    suite: str
    category: str | None = None


class BenchmarkCompareRequest(BaseModel):
    baseline_run_id: str
    current_run_id: str


def _suites_dir() -> Path:
    from config import get_settings
    return get_settings().context_path / "evaluations"


def _suite_path(name: str) -> Path:
    path = _suites_dir() / f"{name}.yaml"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"suite '{name}' not found")
    return path


@router.get("/suites", summary="List benchmark suites")
async def list_suites() -> list[EvalSuiteInfo]:
    suites = []
    for path in sorted(_suites_dir().glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text()) or {}
            suites.append(EvalSuiteInfo(
                name=path.stem,
                mode=str(data.get("mode", "")),
                scorer=str(data.get("scorer", "llm_judge")),
                cases=len(data.get("cases") or []),
            ))
        except yaml.YAMLError:
            pass
    return suites


async def _execute_benchmark(run_id: str, suite_name: str, category: str | None) -> None:
    redis = get_redis_client()
    started_at = datetime.now(UTC).isoformat()
    try:
        runner = BenchmarkRunner()
        report = await runner.run_suite(_suite_path(suite_name), category=category)
        report_dict = report.model_dump()
        report_dict["run_id"] = run_id
        report_dict["started_at"] = started_at
        report_dict["finished_at"] = datetime.now(UTC).isoformat()
        report_dict["status"] = "done"
        await redis.set_json(f"benchmark:run:{run_id}", report_dict, ttl_s=86400 * 7)
        await redis.publish("agent.events", {
            "event": "benchmark.run.finished",
            "run_id": run_id,
            "suite": suite_name,
            "pass_rate": report.pass_rate,
        })
    except Exception as exc:
        error_payload = {
            "run_id": run_id,
            "suite": suite_name,
            "status": "failed",
            "started_at": started_at,
            "error": f"{type(exc).__name__}: {exc}",
        }
        await redis.set_json(f"benchmark:run:{run_id}", error_payload, ttl_s=86400)


@router.post("/runs", status_code=202, summary="Launch a benchmark run")
async def start_benchmark_run(
    body: BenchmarkRunRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    _suite_path(body.suite)  # validate exists
    run_id = str(uuid.uuid4())
    await get_redis_client().set_json(
        f"benchmark:run:{run_id}",
        {"run_id": run_id, "suite": body.suite, "status": "running",
         "started_at": datetime.now(UTC).isoformat()},
        ttl_s=86400 * 7,
    )
    background_tasks.add_task(_execute_benchmark, run_id, body.suite, body.category)
    return {
        "run_id": run_id,
        "suite": body.suite,
        "status": "running",
        "status_url": f"/v1/benchmark/runs/{run_id}",
    }


@router.get("/runs/{run_id}", summary="Poll a benchmark run")
async def get_benchmark_run(run_id: str) -> dict[str, Any]:
    data = await get_redis_client().get_json(f"benchmark:run:{run_id}")
    if data is None:
        raise HTTPException(status_code=404, detail=f"benchmark run '{run_id}' not found")
    return data


@router.post("/compare", summary="Compare two benchmark run results")
async def compare_benchmark_runs(body: BenchmarkCompareRequest) -> dict[str, Any]:
    redis = get_redis_client()
    baseline = await redis.get_json(f"benchmark:run:{body.baseline_run_id}")
    current = await redis.get_json(f"benchmark:run:{body.current_run_id}")
    if baseline is None:
        raise HTTPException(status_code=404, detail=f"baseline run '{body.baseline_run_id}' not found")
    if current is None:
        raise HTTPException(status_code=404, detail=f"current run '{body.current_run_id}' not found")
    return compare_reports(baseline, current)
