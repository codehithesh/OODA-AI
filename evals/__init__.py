"""evals — RL policy evaluation suite.

Thin extension of multi-agent-backend's EvalRunner / BenchmarkRunner that:
  1. Accepts a checkpointed RL policy in place of the live LiteLLM-routed graph.
  2. Enforces train/eval split isolation at runtime.

Public surface:
    RLEvalRunner  — run_eval.py
"""

from evals.run_eval import RLEvalRunner

__all__ = ["RLEvalRunner"]
