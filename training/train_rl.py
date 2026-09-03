"""training/train_rl.py — Algorithm-agnostic RL training loop scaffold.

This module drives DataAnalystEnv through episodes via reset() / step().
It is intentionally algorithm-agnostic — it does NOT implement or depend on
any particular RL algorithm.

⚠ IMPORTANT — text action space compatibility note:
    Box/Discrete-oriented libraries such as Stable-Baselines3 PPO will NOT
    consume this environment's text action space out of the box.  The action
    space is gymnasium.spaces.Text (a JSON-serialised dict), which those
    libraries do not support.  This loop is scaffolding to be adapted once
    you choose an approach suited to text actions, such as:

      - TRL (trl.PPOTrainer / trl.GRPOTrainer) — designed for LLM policies
      - A custom PPO variant with a language-model policy head
      - A verifiers-style harness (e.g. the `verifiers` library by willccbb)

    The loop below is written to be readable and easy to hook into; it does
    not import or depend on any of the above libraries so it can be run for
    smoke-testing without GPU resources.

Usage (uv run from training/ directory):

    uv run python train_rl.py \\
        --warehouse ../multi-agent-backend/backend/data/analytics.duckdb \\
        --train-episodes ../rl_env/episodes/train/ \\
        --episodes 100 \\
        --max-steps 5

    Or as a library:

        from training.train_rl import run_training_loop
        run_training_loop(
            warehouse_path=":memory:",
            train_episodes_path=Path("rl_env/episodes/train/"),
            num_episodes=50,
        )
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Episode trajectory record
# ---------------------------------------------------------------------------
@dataclass
class StepRecord:
    """One (observation, action, reward, terminated, truncated) tuple."""

    step: int
    observation: str
    action: str
    reward: float
    terminated: bool
    truncated: bool
    info: dict[str, Any] = field(default_factory=dict)


@dataclass
class EpisodeTrajectory:
    """Full trajectory for one episode — passed to the policy update step."""

    episode_index: int
    episode_id: str
    steps: list[StepRecord] = field(default_factory=list)
    total_reward: float = 0.0
    num_steps: int = 0
    duration_seconds: float = 0.0

    def append(self, record: StepRecord) -> None:
        self.steps.append(record)
        self.total_reward += record.reward
        self.num_steps += 1


# ---------------------------------------------------------------------------
# Policy stub
# ---------------------------------------------------------------------------
def _default_policy(observation: str, env: Any) -> str:
    """Stub policy — replace with your trained model's inference call.

    Parameters
    ----------
    observation : str
        JSON string from DataAnalystEnv: {"schema": ..., "question": ..., ...}
    env : DataAnalystEnv
        The live environment (provides action_space for sampling if needed).

    Returns
    -------
    action : str
        JSON string: {"action_type": "sql"|"python", "content": str}

    This default implementation samples a random action from the action space,
    which is only useful as a smoke test.  Replace with:

        response = await litellm_client.chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": observation},
        ])
        return response.content  # must be valid JSON action dict

    or the equivalent call to your TRL / PPO policy.
    """
    obs_data = {}
    try:
        obs_data = json.loads(observation)
    except json.JSONDecodeError:
        pass

    question = obs_data.get("question", "")
    # Emit a trivially valid read-only SQL so the env doesn't immediately error.
    action = {
        "action_type": "sql",
        "content": f"SELECT 1 AS stub -- question: {question[:60]}",
    }
    return json.dumps(action)


# ---------------------------------------------------------------------------
# Core training loop
# ---------------------------------------------------------------------------
def run_training_loop(
    *,
    warehouse_path: str | Path = ":memory:",
    train_episodes_path: Path,
    litellm_base_url: str = "http://localhost:4000",
    litellm_api_key: str = "sk-placeholder",
    num_episodes: int = 100,
    max_steps: int = 5,
    policy_fn: Any | None = None,
    log_every: int = 10,
) -> list[EpisodeTrajectory]:
    """Run the RL training loop for *num_episodes* episodes.

    Parameters
    ----------
    warehouse_path:
        DuckDB warehouse path forwarded to DataAnalystEnv.
    train_episodes_path:
        Directory (or file) of training episode YAMLs — the TRAINING split.
        Must be strictly disjoint from the eval episodes used in evals/run_eval.py.
    litellm_base_url / litellm_api_key:
        LiteLLM proxy coordinates forwarded to DataAnalystEnv.
        Only the LiteLLM proxy is used — no raw openai SDK, which would bypass
        cost tracking and Langfuse tracing.
    num_episodes:
        Total number of episodes to run.
    max_steps:
        Per-episode step budget forwarded to DataAnalystEnv.
    policy_fn:
        Callable(observation: str, env) -> action: str.
        Defaults to _default_policy (stub that emits a trivial SQL action).
        Replace with your trained model's inference call.
    log_every:
        Log a summary every N episodes.

    Returns
    -------
    list[EpisodeTrajectory]
        All episode trajectories collected during the run.  Pass these to your
        algorithm's policy-update step (e.g. trl.PPOTrainer.step(), or a
        custom REINFORCE gradient update).
    """
    # Late import — keeps training/ importable without backend or gymnasium
    # on the path during packaging / linting.
    from rl_env.env import DataAnalystEnv  # type: ignore[import]

    policy = policy_fn or _default_policy

    env = DataAnalystEnv(
        warehouse_path=warehouse_path,
        litellm_base_url=litellm_base_url,
        litellm_api_key=litellm_api_key,
        training_episodes_path=train_episodes_path,
        max_steps=max_steps,
    )

    trajectories: list[EpisodeTrajectory] = []
    cumulative_reward = 0.0
    t_run_start = time.perf_counter()

    try:
        for episode_idx in range(num_episodes):
            t_ep_start = time.perf_counter()
            obs, info = env.reset()
            episode_id = info.get("episode_id", f"ep-{episode_idx}")
            trajectory = EpisodeTrajectory(
                episode_index=episode_idx,
                episode_id=episode_id,
            )

            terminated = False
            truncated = False
            step_num = 0

            while not (terminated or truncated):
                # ---- policy inference ----
                # Replace _default_policy with your actual model call here.
                # For async models, wrap in asyncio.run() or run the whole
                # loop as async (change run_training_loop to async def).
                action = policy(obs, env)

                # ---- environment step ----
                obs, reward, terminated, truncated, step_info = env.step(action)

                record = StepRecord(
                    step=step_num,
                    observation=obs,
                    action=action,
                    reward=reward,
                    terminated=terminated,
                    truncated=truncated,
                    info=step_info,
                )
                trajectory.append(record)
                step_num += 1

            trajectory.duration_seconds = round(time.perf_counter() - t_ep_start, 3)
            cumulative_reward += trajectory.total_reward
            trajectories.append(trajectory)

            # ---- policy update hook ----
            # Insert your algorithm's update call here, e.g.:
            #   trainer.step(trajectories[-1])          # TRL PPOTrainer
            #   optimizer.zero_grad()                   # custom REINFORCE
            #   loss = -sum(r for r in rewards)
            #   loss.backward(); optimizer.step()
            #
            # The trajectory object holds the full (obs, action, reward) sequence.

            if (episode_idx + 1) % log_every == 0:
                mean_reward = cumulative_reward / (episode_idx + 1)
                elapsed = round(time.perf_counter() - t_run_start, 1)
                logger.info(
                    "training_progress",
                    episode=episode_idx + 1,
                    total=num_episodes,
                    mean_reward=round(mean_reward, 4),
                    last_episode_reward=round(trajectory.total_reward, 4),
                    last_steps=trajectory.num_steps,
                    elapsed_s=elapsed,
                )
    finally:
        env.close()

    total_elapsed = round(time.perf_counter() - t_run_start, 1)
    final_mean = cumulative_reward / num_episodes if num_episodes else 0.0
    logger.info(
        "training_complete",
        episodes=num_episodes,
        mean_reward=round(final_mean, 4),
        elapsed_s=total_elapsed,
    )
    return trajectories


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Algorithm-agnostic RL training loop for the OODA-AI data analyst agent.\n\n"
            "NOTE: Box/Discrete-oriented libraries (SB3 PPO, etc.) will not consume\n"
            "this env's text action space.  Use TRL, a custom PPO variant, or a\n"
            "verifiers-style harness instead."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--warehouse",
        type=str,
        default=":memory:",
        help="DuckDB warehouse path (default: :memory:).",
    )
    parser.add_argument(
        "--train-episodes",
        type=str,
        required=True,
        dest="train_episodes",
        help="Path to the training episode YAML directory or file.",
    )
    parser.add_argument(
        "--litellm-url",
        type=str,
        default="http://localhost:4000",
        dest="litellm_url",
        help="LiteLLM proxy base URL (default: http://localhost:4000).",
    )
    parser.add_argument(
        "--litellm-key",
        type=str,
        default="sk-placeholder",
        dest="litellm_key",
        help="LiteLLM proxy bearer token.",
    )
    parser.add_argument(
        "--episodes",
        type=int,
        default=100,
        help="Number of training episodes (default: 100).",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=5,
        dest="max_steps",
        help="Max steps per episode (default: 5, matches eda_loop max_iterations).",
    )
    parser.add_argument(
        "--log-every",
        type=int,
        default=10,
        dest="log_every",
        help="Log a progress summary every N episodes (default: 10).",
    )
    args = parser.parse_args()

    trajectories = run_training_loop(
        warehouse_path=args.warehouse,
        train_episodes_path=Path(args.train_episodes),
        litellm_base_url=args.litellm_url,
        litellm_api_key=args.litellm_key,
        num_episodes=args.episodes,
        max_steps=args.max_steps,
        log_every=args.log_every,
    )

    total_reward = sum(t.total_reward for t in trajectories)
    mean_reward = total_reward / len(trajectories) if trajectories else 0.0
    print(
        f"\nDone. {len(trajectories)} episodes · "
        f"mean reward {mean_reward:.4f} · "
        f"total reward {total_reward:.4f}"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
