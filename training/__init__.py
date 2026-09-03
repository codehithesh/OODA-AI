"""training — Algorithm-agnostic RL training loop scaffold.

Imports DataAnalystEnv and drives reset() / step() across episodes.
The loop itself is algorithm-agnostic; adapt it once an approach suited
to text actions is chosen (TRL, custom PPO variant, verifiers-style harness).

Public surface:
    run_training_loop  — train_rl.py
"""

from training.train_rl import run_training_loop

__all__ = ["run_training_loop"]
