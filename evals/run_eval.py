"""evals/run_eval.py — RL policy evaluation runner.

Thin extension of the live inference harness (backend/eval_harness.EvalRunner
and backend/benchmark.BenchmarkRunner).  This module adds exactly two things
on top of the existing harness:

  1. Swap in a checkpointed RL policy in place of the live LiteLLM-routed
     graph, so the same scorer infrastructure measures both.

  2. Assert at runtime that the eval question set is disjoint from the
     training set used by DataAnalystEnv.reset() — leakage fails loudly
     instead of silently inflating the accuracy number.

Everything else — suites, scorers, CaseResult, SuiteReport, BenchmarkReport,
failure taxonomy — is inherited from the backend package without modification.

Usage (uv run from the evals/ directory):

    uv run python run_eval.py \\
        --checkpoint ../checkpoints/policy_v1.pt \\
        --suite ../multi-agent-backend/backend/context/evaluations/analytics_suite.yaml \\
        --train-episodes ../rl_env/episodes/train/ \\
        --eval-episodes ../evals/episodes/eval/

    uv run python run_eval.py --benchmark --all \\
        --checkpoint ../checkpoints/policy_v1.pt \\
        --eval-episodes ../evals/episodes/eval/

INTERFACE NOTE:
    The ``checkpoint`` flag is accepted by the CLI but the mechanism for
    loading and running the checkpointed policy (e.g. HuggingFace model,
    TRL trainer state, custom PPO checkpoint) is intentionally left as a
    stub (``_load_policy``).  Fill that in once the training algorithm is
    chosen — the harness plumbing is fully wired.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Train / eval split guard
# ---------------------------------------------------------------------------
def assert_disjoint_splits(train_path: Path, eval_path: Path) -> None:
    """Assert that the training and evaluation episode sets are disjoint.

    Compares the resolved absolute paths of every YAML file found under each
    root.  Raises RuntimeError (not just a warning) so leakage fails loudly
    during automated runs rather than silently inflating eval numbers.

    Parameters
    ----------
    train_path : Path  — root passed to DataAnalystEnv(training_episodes_path=...)
    eval_path  : Path  — root that RLEvalRunner loads its suites from
    """
    def _collect(root: Path) -> set[Path]:
        if root.is_file():
            return {root.resolve()}
        return {p.resolve() for p in root.rglob("*.yaml")} | {
            p.resolve() for p in root.rglob("*.yml")
        }

    train_files = _collect(train_path)
    eval_files = _collect(eval_path)
    overlap = train_files & eval_files

    if overlap:
        overlap_display = "\n  ".join(str(p) for p in sorted(overlap))
        raise RuntimeError(
            f"TRAIN/EVAL SPLIT LEAKAGE DETECTED — {len(overlap)} file(s) appear in "
            f"both the training episodes path ({train_path}) and the eval episodes "
            f"path ({eval_path}):\n  {overlap_display}\n"
            "Fix by using strictly separate directories for training and eval episodes."
        )

    logger.info(
        "split_disjoint_ok",
        train_files=len(train_files),
        eval_files=len(eval_files),
    )


# ---------------------------------------------------------------------------
# Stub: policy loader
# ---------------------------------------------------------------------------
def _load_policy(checkpoint_path: Path | None) -> Any | None:
    """Load a checkpointed RL policy and return a LiteLLMClient-compatible object.

    STUB — fill in once the training algorithm is chosen.

    The returned object must implement the LiteLLMClient.chat() async interface:
        async def chat(self, messages: list[dict], **kwargs) -> LLMResponse

    so it can be injected into EvalRunner / BenchmarkRunner via the
    ``litellm_client`` constructor argument without any other changes.

    Examples of what goes here depending on approach:
      - TRL / HuggingFace:  load the PEFT adapter, wrap in a thin async shim
      - Custom PPO:          load the policy network, wrap generate() as chat()
      - vLLM-served ckpt:   point LiteLLMClient at the vLLM OpenAI-compat endpoint

    If checkpoint_path is None the live LiteLLM proxy client is used (i.e. the
    same behaviour as the existing harness — useful as a baseline).
    """
    if checkpoint_path is None:
        logger.info("policy_load_skipped", reason="no checkpoint path — using live LiteLLM proxy")
        return None  # EvalRunner falls back to get_default_litellm_client()

    # TODO: implement checkpoint loading here.
    raise NotImplementedError(
        f"_load_policy({checkpoint_path!r}) is a stub.  "
        "Implement checkpoint loading once the training algorithm is finalised."
    )


# ---------------------------------------------------------------------------
# RLEvalRunner
# ---------------------------------------------------------------------------
class RLEvalRunner:
    """Evaluate an RL-trained (or live) policy using the existing harness.

    Parameters
    ----------
    checkpoint_path:
        Path to a saved policy checkpoint.  None → use the live LiteLLM proxy
        (identical to running the existing eval_harness directly).
    train_episodes_path:
        Root directory (or file) used by DataAnalystEnv for training episodes.
        Passed to assert_disjoint_splits() before any eval run.
    eval_episodes_path:
        Root directory (or file) of eval YAML suites.  Must be disjoint from
        train_episodes_path.
    use_benchmark_runner:
        True → use BenchmarkRunner (failure taxonomy + observability metrics).
        False → use EvalRunner (simpler pass/fail + mean score).
    """

    def __init__(
        self,
        *,
        checkpoint_path: Path | None = None,
        train_episodes_path: Path | None = None,
        eval_episodes_path: Path | None = None,
        use_benchmark_runner: bool = False,
    ) -> None:
        self.checkpoint_path = checkpoint_path
        self.train_episodes_path = train_episodes_path
        self.eval_episodes_path = eval_episodes_path
        self.use_benchmark_runner = use_benchmark_runner

        # Enforce split disjointness at construction time (not deferred to run).
        if train_episodes_path is not None and eval_episodes_path is not None:
            assert_disjoint_splits(train_episodes_path, eval_episodes_path)

        # Load policy (or None → live proxy).
        self._policy = _load_policy(checkpoint_path)

    # ------------------------------------------------------------------
    # Public run methods
    # ------------------------------------------------------------------
    async def run_suite(self, suite_path: Path) -> Any:
        """Run a single eval suite and return a SuiteReport or BenchmarkReport.

        Delegates entirely to the backend harness after injecting the policy
        client — no parallel accuracy loop here.
        """
        if self.use_benchmark_runner:
            # Late import — backend must be on sys.path (editable install via uv).
            from benchmark import BenchmarkRunner  # type: ignore[import]
            runner = BenchmarkRunner(litellm=self._policy)
            return await runner.run_suite(suite_path)
        else:
            from eval_harness import EvalRunner  # type: ignore[import]
            runner = EvalRunner(litellm=self._policy)
            return await runner.run_suite_file(suite_path)

    async def run_all(self, category: str | None = None) -> list[Any]:
        """Run every suite found under eval_episodes_path."""
        if self.eval_episodes_path is None:
            raise ValueError("eval_episodes_path must be set to use run_all().")

        root = self.eval_episodes_path
        suite_files = sorted(root.rglob("*.yaml")) if root.is_dir() else [root]
        if not suite_files:
            raise FileNotFoundError(f"No YAML suite files found under {root}")

        reports = []
        for path in suite_files:
            logger.info("running_suite", path=str(path))
            report = await self.run_suite(path)
            reports.append(report)
        return reports


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_report(report: Any) -> None:
    """Best-effort pretty-print for SuiteReport or BenchmarkReport."""
    suite_name = getattr(report, "suite", "?")
    mode = getattr(report, "mode", "?")
    total = getattr(report, "total", 0)
    passed = getattr(report, "passed", 0)
    mean_score = getattr(report, "mean_score", 0.0)
    pass_rate = getattr(report, "pass_rate", 0.0)
    duration_ms = getattr(report, "duration_ms", 0)

    print(f"\n=== {suite_name} (mode={mode}) ===")
    results = getattr(report, "results", [])
    for case in results:
        marker = "PASS" if getattr(case, "passed", False) else "FAIL"
        score = getattr(case, "score", 0.0)
        detail = getattr(case, "detail", None)
        cid = getattr(case, "id", "?")
        print(
            f"  [{marker}] {cid:<32} score={score:.3f}"
            + (f"  ({detail})" if detail else "")
        )
    print(
        f"  -> {passed}/{total} passed · mean score {mean_score:.3f} · "
        f"pass rate {pass_rate:.1%} · {duration_ms} ms"
    )


async def _run_cli(args: argparse.Namespace) -> int:
    checkpoint = Path(args.checkpoint) if args.checkpoint else None
    train_path = Path(args.train_episodes) if args.train_episodes else None
    eval_path = Path(args.eval_episodes) if args.eval_episodes else None

    runner = RLEvalRunner(
        checkpoint_path=checkpoint,
        train_episodes_path=train_path,
        eval_episodes_path=eval_path,
        use_benchmark_runner=args.benchmark,
    )

    if args.all:
        reports = await runner.run_all()
    elif args.suite:
        reports = [await runner.run_suite(Path(args.suite))]
    else:
        print("specify --suite PATH or --all", file=sys.stderr)
        return 2

    exit_code = 0
    for report in reports:
        _print_report(report)
        pass_rate = getattr(report, "pass_rate", 1.0)
        total = getattr(report, "total", 0)
        if total and pass_rate < 1.0:
            exit_code = 1

    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate an RL-trained OODA-AI policy using the existing benchmark harness."
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        default=None,
        help="Path to a saved policy checkpoint.  Omit to use the live LiteLLM proxy (baseline).",
    )
    parser.add_argument(
        "--suite",
        type=str,
        help="Path to a single eval suite YAML.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run every suite under --eval-episodes.",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Use BenchmarkRunner (failure taxonomy) instead of EvalRunner.",
    )
    parser.add_argument(
        "--train-episodes",
        type=str,
        default=None,
        dest="train_episodes",
        help=(
            "Path to the training episode directory used by DataAnalystEnv. "
            "Must be disjoint from --eval-episodes.  "
            "Omit to skip the split-leakage assertion."
        ),
    )
    parser.add_argument(
        "--eval-episodes",
        type=str,
        default=None,
        dest="eval_episodes",
        help="Root directory of eval YAML suites (used with --all).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run_cli(args)))


if __name__ == "__main__":
    main()
