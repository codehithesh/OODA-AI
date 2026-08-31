"""Evaluation suite routes — list suites, launch runs, poll results.

Runs execute as FastAPI BackgroundTasks; status and reports are stored in
Redis (TTL 24h) so the API stays non-blocking.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from clients.redis_client import get_redis_client
from database import AsyncSessionLocal
from eval_harness import EvalRunner
from graphs.base import get_agent_deps
from schemas import EvalRunAccepted, EvalRunRequest, EvalRunStatus, EvalSuiteInfo

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/eval", tags=["evaluations"])


def _suites_dir() -> Path:
    from config import get_settings

    return get_settings().context_path / "evaluations"


def _suite_path(name: str) -> Path:
    path = _suites_dir() / f"{name}.yaml"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"evaluation suite '{name}' not found")
    return path


@router.get("/suites", response_model=list[EvalSuiteInfo], summary="List evaluation suites")
async def list_suites() -> list[EvalSuiteInfo]:
    """Suites discovered in context/evaluations/."""
    suites: list[EvalSuiteInfo] = []
    for path in sorted(_suites_dir().glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text()) or {}
            suites.append(
                EvalSuiteInfo(
                    name=path.stem,
                    mode=str(data.get("mode", "")),
                    scorer=str(data.get("scorer", "llm_judge")),
                    cases=len(data.get("cases") or []),
                )
            )
        except yaml.YAMLError:
            logger.warning("suite_yaml_invalid", path=str(path))
    return suites


async def _execute_suite(
    run_id: str, suite_name: str, suite_path: Path, checkpointer: object
) -> None:
    """Background worker: run the suite and store the report in Redis."""
    redis = get_redis_client()
    started_at = datetime.now(UTC).isoformat()
    try:
        runner = EvalRunner()
        try:
            async with AsyncSessionLocal() as db:
                report = await runner.run_suite_file(suite_path, db=db, checkpointer=checkpointer)
        except Exception:
            logger.warning("eval_db_unavailable_scoring_only")
            report = await runner.run_suite_file(suite_path, checkpointer=checkpointer)

        status = EvalRunStatus(
            run_id=run_id,
            suite=suite_name,
            mode=report.mode,
            status="done",
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
            total=report.total,
            passed=report.passed,
            mean_score=report.mean_score,
            pass_rate=report.pass_rate,
            results=[
                {"id": c.id, "score": c.score, "passed": c.passed, "detail": c.detail}
                for c in report.results
            ],
        )
    except Exception as exc:
        status = EvalRunStatus(
            run_id=run_id,
            suite=suite_name,
            status="failed",
            started_at=started_at,
            error=f"{type(exc).__name__}: {exc}",
        )
    await redis.set_json(f"eval:run:{run_id}", status.model_dump(), ttl_s=86400)
    await redis.publish(
        "agent.events",
        {
            "event": "eval.run.finished",
            "run_id": run_id,
            "suite": suite_name,
            "status": status.status,
        },
    )


@router.post("/runs", status_code=202, response_model=EvalRunAccepted, summary="Run a suite")
async def start_eval_run(
    body: EvalRunRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    deps: dict = Depends(get_agent_deps),
) -> EvalRunAccepted:
    """Launch an evaluation suite in the background; poll the returned URL."""
    path = _suite_path(body.suite)
    run_id = str(uuid.uuid4())
    await get_redis_client().set_json(
        f"eval:run:{run_id}",
        {
            "run_id": run_id,
            "suite": body.suite,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
        },
        ttl_s=86400,
    )
    background_tasks.add_task(
        _execute_suite, run_id, body.suite, path, request.app.state.checkpointer
    )
    return EvalRunAccepted(
        run_id=run_id,
        suite=body.suite,
        status="running",
        status_url=f"/v1/eval/runs/{run_id}",
    )


@router.get("/runs/{run_id}", response_model=EvalRunStatus, summary="Poll a run")
async def get_eval_run(run_id: str) -> EvalRunStatus:
    data = await get_redis_client().get_json(f"eval:run:{run_id}")
    if data is None:
        raise HTTPException(status_code=404, detail=f"eval run '{run_id}' not found")
    return EvalRunStatus.model_validate(data)
