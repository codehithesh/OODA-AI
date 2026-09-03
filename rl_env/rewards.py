"""rewards.py — Reward composition for DataAnalystEnv.

Reward signal is built from three components:

  +1.0   scorer match   — one of the four eval_harness scorers returns 1.0
  -0.1   execution error — SQL validation failure OR sandbox non-zero exit
  -0.01  per-step time penalty — always applied to encourage brevity

The four scorers (exact_sql, execution_match, exact_match, llm_judge) are
imported directly from backend/eval_harness.py so there is no parallel
re-implementation.  Only the async llm_judge scorer needs a LiteLLMClient;
the other three are pure-computation.

NOTE on interfaces sourced from multi-agent-backend/backend/eval_harness.py:
  - normalize_sql(sql: str) -> str
  - _fixture_rows(conn, sql) -> list[str]
  - _run_fixture(conn, fixture) -> None
  - _parse_judge_score(text: str) -> float
  - EvalCase, EvalSuite  (for rubric / fixture metadata)
All are imported at call time to avoid import-time failures when the backend
package is not yet on the path (e.g. during sandbox-only tests).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REWARD_MATCH: float = 1.0
PENALTY_EXEC_ERROR: float = -0.1
PENALTY_TIME_STEP: float = -0.01


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class StepReward:
    """Decomposed reward returned for a single environment step.

    Attributes:
        total:          Final scalar reward fed to the RL algorithm.
        scorer_reward:  REWARD_MATCH (1.0) if the scorer approved, else 0.0.
        exec_penalty:   PENALTY_EXEC_ERROR (-0.1) on execution failure, else 0.0.
        time_penalty:   PENALTY_TIME_STEP (-0.01) always.
        scorer_used:    Name of the scorer that was applied.
        scorer_detail:  Human-readable detail string from the scorer (may be None).
    """

    total: float
    scorer_reward: float
    exec_penalty: float
    time_penalty: float = PENALTY_TIME_STEP
    scorer_used: str = ""
    scorer_detail: str | None = None

    @classmethod
    def execution_error(cls, detail: str | None = None) -> "StepReward":
        """Convenience constructor for the execution-failure case."""
        total = PENALTY_EXEC_ERROR + PENALTY_TIME_STEP
        return cls(
            total=total,
            scorer_reward=0.0,
            exec_penalty=PENALTY_EXEC_ERROR,
            time_penalty=PENALTY_TIME_STEP,
            scorer_used="none",
            scorer_detail=detail,
        )


# ---------------------------------------------------------------------------
# Reward calculator
# ---------------------------------------------------------------------------
class RewardCalculator:
    """Computes StepReward by delegating to eval_harness scorers.

    Parameters
    ----------
    litellm_client:
        A LiteLLMClient instance (from clients.litellm_client).  Only needed
        when scorer == 'llm_judge'; safe to pass None for SQL-only workflows.
    default_scorer:
        Which scorer to use when the episode metadata does not specify one.
        Must be one of: 'exact_sql', 'execution_match', 'exact_match', 'llm_judge'.
    """

    _VALID_SCORERS = frozenset({"exact_sql", "execution_match", "exact_match", "llm_judge"})

    def __init__(
        self,
        litellm_client: Any | None = None,
        default_scorer: str = "execution_match",
    ) -> None:
        if default_scorer not in self._VALID_SCORERS:
            raise ValueError(
                f"default_scorer must be one of {sorted(self._VALID_SCORERS)}, "
                f"got {default_scorer!r}"
            )
        self._litellm = litellm_client
        self.default_scorer = default_scorer

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def execution_error_reward(self, detail: str | None = None) -> StepReward:
        """Return the fixed penalty reward for an execution failure."""
        return StepReward.execution_error(detail)

    def compute(
        self,
        *,
        output: dict[str, Any],
        expected: dict[str, Any],
        scorer: str | None = None,
        fixture: str | None = None,
        output_field: str | None = None,
        rubric: str = "",
    ) -> StepReward:
        """Synchronous entry point — runs the async scorer in a new event loop.

        For async callers (e.g. env.step() driven from an async training loop)
        prefer ``await acompute(...)`` directly.
        """
        return asyncio.run(
            self.acompute(
                output=output,
                expected=expected,
                scorer=scorer,
                fixture=fixture,
                output_field=output_field,
                rubric=rubric,
            )
        )

    async def acompute(
        self,
        *,
        output: dict[str, Any],
        expected: dict[str, Any],
        scorer: str | None = None,
        fixture: str | None = None,
        output_field: str | None = None,
        rubric: str = "",
    ) -> StepReward:
        """Async reward computation — reuses eval_harness scorer logic directly.

        Parameters
        ----------
        output:       State dict produced by the current env step.
        expected:     Ground-truth dict for the current episode (from eval YAML).
        scorer:       Override scorer; falls back to self.default_scorer.
        fixture:      SQL DDL string used for execution_match setup.
        output_field: Field name for exact_match scorer (default 'action').
        rubric:       Rubric string passed to llm_judge.
        """
        # Late import so the backend package is only required at call time,
        # not at module import time — important for isolated unit tests.
        from eval_harness import (  # type: ignore[import]
            _fixture_rows,
            _parse_judge_score,
            _run_fixture,
            normalize_sql,
        )

        scorer = scorer or self.default_scorer
        score, detail = await self._run_scorer(
            scorer=scorer,
            output=output,
            expected=expected,
            fixture=fixture or "",
            output_field=output_field or "action",
            rubric=rubric,
            normalize_sql=normalize_sql,
            fixture_rows=_fixture_rows,
            run_fixture=_run_fixture,
            parse_judge_score=_parse_judge_score,
        )

        scorer_reward = REWARD_MATCH if score >= 1.0 else 0.0
        total = scorer_reward + PENALTY_TIME_STEP  # no exec penalty here
        result = StepReward(
            total=total,
            scorer_reward=scorer_reward,
            exec_penalty=0.0,
            time_penalty=PENALTY_TIME_STEP,
            scorer_used=scorer,
            scorer_detail=detail,
        )
        logger.info(
            "reward_computed",
            scorer=scorer,
            score=score,
            total=total,
        )
        return result

    # ------------------------------------------------------------------
    # Internal scorer dispatch — mirrors eval_harness._score() exactly
    # ------------------------------------------------------------------
    async def _run_scorer(
        self,
        *,
        scorer: str,
        output: dict[str, Any],
        expected: dict[str, Any],
        fixture: str,
        output_field: str,
        rubric: str,
        normalize_sql: Any,
        fixture_rows: Any,
        run_fixture: Any,
        parse_judge_score: Any,
    ) -> tuple[float, str | None]:
        import duckdb  # local import — duckdb is a direct dep of rl_env

        try:
            if scorer == "exact_sql":
                candidate_raw = str(output.get("generated_sql", "")).strip()
                expected_raw = str(expected.get("sql", "")).strip()
                if not candidate_raw:
                    return 0.0, "no SQL generated"
                candidate = normalize_sql(candidate_raw)
                expected_sql = normalize_sql(expected_raw)
                matched = candidate == expected_sql
                return (1.0 if matched else 0.0), (
                    None if matched else f"candidate != expected\n  got:      {candidate[:120]}\n  expected: {expected_sql[:120]}"
                )

            if scorer == "execution_match":
                candidate_sql = str(output.get("generated_sql", "")).strip()
                expected_sql = str(expected.get("sql", "")).strip()
                if not candidate_sql:
                    return 0.0, "no SQL generated"
                if not expected_sql:
                    return 0.0, "no expected SQL in ground truth"
                with duckdb.connect(":memory:") as conn:
                    if fixture:
                        run_fixture(conn, fixture)
                    candidate_rows = fixture_rows(conn, candidate_sql)
                    expected_rows = fixture_rows(conn, expected_sql)
                matched = candidate_rows == expected_rows
                return (1.0 if matched else 0.0), (None if matched else "result sets differ")

            if scorer == "exact_match":
                candidate = output.get(output_field)
                exp_val = expected.get(output_field)
                matched = candidate == exp_val
                return (1.0 if matched else 0.0), f"{output_field}: {candidate!r} vs {exp_val!r}"

            if scorer == "llm_judge":
                if self._litellm is None:
                    raise RuntimeError(
                        "llm_judge scorer requires a LiteLLMClient — pass one to "
                        "RewardCalculator(litellm_client=...)"
                    )
                # Late import — prompt_loader lives in the backend package.
                from clients.prompt_loader import get_default_prompt_loader  # type: ignore[import]

                loader = get_default_prompt_loader()
                prompt = loader.render(
                    "eval/judge.md",
                    input={},
                    expected=expected,
                    output=output,
                    rubric=rubric,
                )
                response = await self._litellm.chat([{"role": "user", "content": prompt}])
                score = parse_judge_score(response.content)
                return score, response.content[:200]

            raise ValueError(f"unknown scorer: {scorer!r}")
        except Exception as exc:
            return 0.0, f"scorer error: {exc}"
