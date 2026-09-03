"""env.py — DataAnalystEnv: a gymnasium.Env wrapping the OODA-AI EDA loop.

Each step() call mirrors one full iteration of the existing
    generate_eda_sql → execute_eda_sql → evaluate_hypothesis → decide_next_step
cycle from nodes/eda_loop.py, not one raw code execution.

Observation space
-----------------
gymnasium.spaces.Text is used for the observation string.  This is the
standard choice for LLM-policy RL; Box/Discrete-oriented libraries (e.g.
SB3 PPO) do not consume free text observations anyway.  Callers that need
to bypass the gymnasium spaces type system for text I/O can do so — the
spaces declarations here serve as documentation / API contracts rather than
runtime type enforcement.

Action space
------------
Actions are dicts:
    {"action_type": "sql" | "python", "content": str}

Default path "sql": content is validated via nodes/validate_sql.validate_sql
(same guard as production) and executed via DuckDBClient.aquery() (read-only).

Opt-in path "python": content is routed to sandbox.DockerSandbox — isolated,
hardened, network-disabled container.  This is an *explicit opt-in*; the env
does not auto-upgrade SQL actions to Python.

gymnasium does not have a native Dict(Text, Text) space so the action space
is declared as gymnasium.spaces.Text with a docstring note rather than an
invalid composite.

Step return
-----------
Returns the modern 5-tuple: (observation, reward, terminated, truncated, info).
  terminated = True  mirrors decide_next_step's "finalize" condition
  truncated  = True  on max_steps exceeded

Episode loading
---------------
reset() draws questions from the TRAINING split of the eval dataset.
The eval split is kept disjoint — that enforcement lives in evals/run_eval.py,
which asserts at runtime that its question path != the training path used here.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import gymnasium as gym
import structlog
import yaml

logger = structlog.get_logger(__name__)

# Default step budget per episode — matches eda_loop's default max_iterations.
_DEFAULT_MAX_STEPS = 5


def _run_async(coro: Any) -> Any:
    """Run an async coroutine safely whether or not an event loop is active."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    else:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()


# ---------------------------------------------------------------------------
# Episode question schema
# ---------------------------------------------------------------------------
class EpisodeQuestion:
    """A single training question loaded from a YAML episode file.

    Expected YAML structure (one document per file, or a list of documents):
        id: str
        question: str          # natural-language analytics question
        hypothesis: str        # optional starting hypothesis
        expected:              # ground-truth for the reward scorer
          sql: str             # for exact_sql / execution_match scorers
          action: str          # for exact_match scorer
          rubric: str          # for llm_judge scorer
        scorer: str            # optional override; defaults to env's scorer
        fixture: str           # optional DDL for execution_match
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.id: str = data.get("id", str(uuid.uuid4()))
        inp = data.get("input") if isinstance(data.get("input"), dict) else {}
        self.question: str = (
            data.get("question")
            or inp.get("query")
            or inp.get("question")
            or ""
        )
        self.hypothesis: str = data.get("hypothesis", "")
        self.expected: dict[str, Any] = data.get("expected", {})
        self.scorer: str | None = data.get("scorer")
        self.fixture: str | None = data.get("fixture")


# ---------------------------------------------------------------------------
# Main environment
# ---------------------------------------------------------------------------
class DataAnalystEnv(gym.Env):
    """Gymnasium environment wrapping the OODA-AI EDA loop.

    Parameters
    ----------
    warehouse_path:
        Path to the DuckDB warehouse file (or ':memory:' for tests).
        Passed directly to DuckDBClient — reuses the existing client code.
    litellm_base_url:
        Base URL of the self-hosted LiteLLM proxy (e.g. 'http://localhost:4000').
        Passed to LiteLLMClient — the only sanctioned LLM path.
    litellm_api_key:
        Bearer token for the LiteLLM proxy.
    training_episodes_path:
        Path to a directory of YAML episode files (or a single YAML file)
        representing the TRAINING split.  reset() draws episodes from here.
        Must be disjoint from whatever path evals/run_eval.py uses — that
        disjointness is asserted at runtime in RLEvalRunner, not here.
    default_scorer:
        Scorer used when an episode file does not specify one.
        One of: 'exact_sql', 'execution_match', 'exact_match', 'llm_judge'.
    max_steps:
        Maximum steps per episode before truncation (mirrors max_iterations).
    sandbox_timeout:
        Wall-clock timeout in seconds for DockerSandbox runs.
    duckdb_max_rows:
        Row cap forwarded to DuckDBClient.
    litellm_client:
        Optional pre-built LiteLLMClient (useful for tests / injection).
    duckdb_client:
        Optional pre-built DuckDBClient (useful for tests / injection).
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        *,
        warehouse_path: str | Path = ":memory:",
        litellm_base_url: str = "http://localhost:4000",
        litellm_api_key: str = "sk-placeholder",
        training_episodes_path: str | Path,
        default_scorer: str = "execution_match",
        max_steps: int = _DEFAULT_MAX_STEPS,
        sandbox_timeout: int = 30,
        sandbox_image: str = "python:3.12-slim",
        duckdb_max_rows: int = 200,
        litellm_client: Any | None = None,
        duckdb_client: Any | None = None,
    ) -> None:
        super().__init__()

        # --- gymnasium spaces ---
        # Text space for the observation (schema + question + prior output).
        # NOTE: gymnasium.spaces.Text does not set a hard upper bound on length
        # by default.  Box/Discrete-oriented RL libraries will not consume these
        # directly; this is standard for LLM-policy RL environments.
        self.observation_space = gym.spaces.Text(max_length=1_000_000, min_length=0)

        # Action space: also Text because the action is a JSON-serialised dict
        # {"action_type": "sql"|"python", "content": str}.  A proper
        # Dict(Discrete, Text) compound space is not supported by gymnasium's
        # current Text implementation; the JSON encoding is the contract.
        self.action_space = gym.spaces.Text(max_length=100_000, min_length=1)

        self.max_steps = max_steps
        self.default_scorer = default_scorer

        # --- load training episodes ---
        self._episodes: list[EpisodeQuestion] = _load_episodes(
            Path(training_episodes_path)
        )
        if not self._episodes:
            raise ValueError(
                f"No training episodes found at {training_episodes_path!r}. "
                "Create at least one YAML file with a 'question' key."
            )
        self._episode_idx: int = 0  # round-robin pointer

        # --- backend clients (reuse existing interfaces, do not duplicate) ---
        # DuckDBClient: imported from multi-agent-backend/backend/clients/duckdb_client.py
        # LiteLLMClient: imported from multi-agent-backend/backend/clients/litellm_client.py
        # Both imports are deferred to __init__ body so rl_env can be imported
        # without the backend on sys.path (e.g. during packaging / linting).
        if duckdb_client is not None:
            self._db = duckdb_client
        else:
            from clients.duckdb_client import DuckDBClient  # type: ignore[import]
            self._db = DuckDBClient(warehouse_path, max_rows=duckdb_max_rows)

        if litellm_client is not None:
            self._llm = litellm_client
        else:
            from clients.litellm_client import LiteLLMClient  # type: ignore[import]
            self._llm = LiteLLMClient(
                base_url=litellm_base_url,
                api_key=litellm_api_key,
            )

        # --- reward calculator (reuses eval_harness scorers) ---
        from rl_env.rewards import RewardCalculator
        self._reward_calc = RewardCalculator(
            litellm_client=self._llm,
            default_scorer=default_scorer,
        )

        # --- sandbox (only used for action_type='python') ---
        from rl_env.sandbox import DockerSandbox
        self._sandbox = DockerSandbox(base_image=sandbox_image, timeout_seconds=sandbox_timeout)

        # --- mutable episode state (reset on each reset() call) ---
        self._current_episode: EpisodeQuestion | None = None
        self._step_count: int = 0
        self._analysis_state: dict[str, Any] = {}
        self._prior_output: str = ""

    # ------------------------------------------------------------------
    # gymnasium API
    # ------------------------------------------------------------------
    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Load the next training episode and return the initial observation.

        Episodes cycle round-robin through the training split.  Pass
        options={"episode_id": <str>} to pin a specific episode by id.

        Returns
        -------
        observation : str
            JSON string: {"schema": ..., "question": ..., "hypothesis": ..., "prior_output": ""}
        info : dict
            {"episode_id": str, "episode_index": int}
        """
        super().reset(seed=seed)

        # Allow pinning a specific episode for deterministic evaluation.
        if options and "episode_id" in options:
            episode = next(
                (e for e in self._episodes if e.id == options["episode_id"]), None
            )
            if episode is None:
                raise ValueError(f"Episode id {options['episode_id']!r} not found in training set.")
        else:
            episode = self._episodes[self._episode_idx % len(self._episodes)]
            self._episode_idx += 1

        self._current_episode = episode
        self._step_count = 0
        self._analysis_state = _initial_analysis_state(episode)
        self._prior_output = ""

        obs = self._build_observation()
        info: dict[str, Any] = {
            "episode_id": episode.id,
            "episode_index": self._episode_idx - 1,
        }
        logger.info("env_reset", episode_id=episode.id, question=episode.question[:80])
        return obs, info

    def step(
        self, action: str
    ) -> tuple[str, float, bool, bool, dict[str, Any]]:
        """Execute one iteration of the EDA loop and return the 5-tuple.

        Parameters
        ----------
        action : str
            JSON string: {"action_type": "sql"|"python", "content": str}

        Returns
        -------
        observation : str   — updated schema/question/prior-output text
        reward      : float — composed from scorer + execution penalty + time penalty
        terminated  : bool  — True when decide_next_step returns "finalize"
        truncated   : bool  — True when max_steps exceeded
        info        : dict  — step diagnostics
        """
        if self._current_episode is None:
            raise RuntimeError("Call reset() before step().")

        self._step_count += 1
        t0 = time.perf_counter()

        # --- parse action ---
        try:
            action_dict = json.loads(action)
            action_type: str = action_dict.get("action_type", "sql")
            content: str = action_dict.get("content", "")
        except (json.JSONDecodeError, AttributeError) as exc:
            # Malformed action → treat as execution error, no reward.
            reward_obj = self._reward_calc.execution_error_reward(
                detail=f"malformed action JSON: {exc}"
            )
            obs = self._build_observation()
            truncated = self._step_count >= self.max_steps
            info = self._build_info(
                action_type="unknown",
                content="",
                exec_error=str(exc),
                step_duration=time.perf_counter() - t0,
            )
            return obs, reward_obj.total, False, truncated, info

        # --- execute action ---
        exec_output: dict[str, Any] = {}
        exec_error: str | None = None
        terminated = False
        truncated = False

        if action_type == "sql":
            exec_output, exec_error = self._execute_sql(content)
        elif action_type == "python":
            exec_output, exec_error = self._execute_python(content)
        else:
            exec_error = f"unknown action_type: {action_type!r}; expected 'sql' or 'python'"

        # --- run EDA loop iteration (generate → execute → evaluate → decide) ---
        if exec_error is None:
            eda_result, eda_error = _run_async(
                self._run_eda_iteration(action_type, content, exec_output)
            )
            if eda_error:
                exec_error = eda_error
            else:
                self._analysis_state = eda_result.get("analysis_state", self._analysis_state)
                self._prior_output = json.dumps(exec_output, default=str)
                # Mirror decide_next_step's termination condition.
                eda_next = eda_result.get("eda_next", "loop")
                terminated = eda_next == "finalize"

        # --- compute reward ---
        if exec_error is not None:
            reward_obj = self._reward_calc.execution_error_reward(detail=exec_error)
        else:
            try:
                reward_obj = _run_async(
                    self._reward_calc.acompute(
                        output={**exec_output, "generated_sql": content if action_type == "sql" else ""},
                        expected=self._current_episode.expected,
                        scorer=self._current_episode.scorer,
                        fixture=self._current_episode.fixture,
                    )
                )
            except Exception as exc:
                reward_obj = self._reward_calc.execution_error_reward(detail=f"reward computation error: {exc}")

        # --- truncation check ---
        truncated = self._step_count >= self.max_steps and not terminated

        obs = self._build_observation()
        step_duration = time.perf_counter() - t0
        info = self._build_info(
            action_type=action_type,
            content=content,
            exec_error=exec_error,
            step_duration=step_duration,
            reward_detail=reward_obj.scorer_detail,
            eda_next=self._analysis_state.get("termination_reason", ""),
        )

        logger.info(
            "env_step",
            step=self._step_count,
            action_type=action_type,
            reward=round(reward_obj.total, 4),
            terminated=terminated,
            truncated=truncated,
        )
        return obs, reward_obj.total, terminated, truncated, info

    def render(self) -> None:
        """Not implemented — this env has no visual render mode."""

    def close(self) -> None:
        """Release the DuckDB connection."""
        try:
            self._db.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_observation(self) -> str:
        """Serialise the current observation as a JSON string."""
        schema_summary = self._get_schema_summary()
        ep = self._current_episode
        return json.dumps(
            {
                "schema": schema_summary,
                "question": ep.question if ep else "",
                "hypothesis": ep.hypothesis if ep else "",
                "prior_output": self._prior_output,
                "step": self._step_count,
            },
            ensure_ascii=False,
        )

    def _get_schema_summary(self) -> str:
        """Return a brief schema summary from DuckDB (SHOW TABLES)."""
        try:
            rows = self._db.query("SHOW TABLES")
            return json.dumps(rows, default=str)
        except Exception as exc:
            return f"<schema unavailable: {exc}>"

    def _execute_sql(self, sql: str) -> tuple[dict[str, Any], str | None]:
        """Validate and execute SQL via DuckDBClient (read-only guard identical to production).

        Validation uses nodes/validate_sql.validate_sql — the same function
        used by the production validate_sql_node.  This mirrors production exactly.
        """
        # Late import so the backend package is only required at call time.
        from nodes.validate_sql import validate_sql  # type: ignore[import]

        validation = validate_sql(sql, rules=None)
        if not validation.sql_valid:
            errors = "; ".join(validation.sql_validation_errors)
            return {}, f"SQL validation failed: {errors}"

        try:
            result = _run_async(self._db.aquery(sql))
            return result, None
        except Exception as exc:
            return {}, f"SQL execution error: {exc}"

    def _execute_python(self, code: str) -> tuple[dict[str, Any], str | None]:
        """Execute Python code in the DockerSandbox (explicit opt-in path).

        Returns a dict wrapping stdout/stderr so the reward scorer has a
        structured output to evaluate against.
        """
        result = self._sandbox.run(code)
        if not result.success:
            detail = result.error or result.stderr[:200] or f"exit code {result.exit_code}"
            return {}, f"sandbox execution error: {detail}"
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }, None

    async def _run_eda_iteration(
        self,
        action_type: str,
        content: str,
        exec_output: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        """Run one iteration of the EDA loop nodes against the current analysis state.

        Calls the four node functions from nodes/eda_loop.py directly, passing
        the current self._analysis_state as state dict.  This matches exactly
        how the production LangGraph wires them together.

        ⚠ INTERFACE NOTE: eda_loop nodes expect a full AnalysisState-shaped
        dict under the 'analysis_state' key plus a 'context' key with DDL /
        rules / prompts.  The AnalysisState datamodel lives in
        multi-agent-backend/backend/analysis_state.py.  We pass what we have;
        nodes that need richer context (prompts, schemas) will degrade
        gracefully if keys are missing, as they apply .get() with defaults.
        """
        try:
            # Late imports — keep rl_env importable without the backend on path.
            from analysis_state import AnalysisState  # type: ignore[import]
            from nodes.eda_loop import (  # type: ignore[import]
                decide_next_step,
                evaluate_hypothesis,
                execute_eda_sql,
                generate_eda_sql,
            )
        except ImportError as exc:
            return {}, f"backend eda_loop import failed: {exc}"

        # Register the executed action into analysis_state so hypothesis evaluation has evidence
        analysis = AnalysisState.model_validate(self._analysis_state)
        pending = analysis.pending_hypotheses()
        hyp_id = pending[0].id if pending else None
        sub_q = (
            pending[0].required_evidence[0]
            if pending and pending[0].required_evidence
            else (analysis.business_question or (self._current_episode.question if self._current_episode else ""))
        )

        q = analysis.add_query(
            sql=content if action_type == "sql" else f"# python action\n{content}",
            rationale=f"Policy action ({action_type})",
            hypothesis_id=hyp_id,
            sub_question=sub_q,
        )
        q.executed = True
        q.rows = exec_output.get("rows", [])
        q.row_count = exec_output.get("row_count", len(q.rows))
        q.columns = exec_output.get("columns", [])
        self._analysis_state = analysis.model_dump()

        # Seed state with the action the policy already took.
        state: dict[str, Any] = {
            "analysis_state": self._analysis_state,
            "query": self._current_episode.question if self._current_episode else "",
            "context": {},
            # Pre-populate generated_sql so execute_eda_sql has something to run
            # when action_type is 'sql'.  For 'python' the exec result is passed
            # directly via exec_output and the SQL nodes are effectively no-ops.
            "generated_sql": content if action_type == "sql" else "",
            **exec_output,
        }

        config: dict[str, Any] = {
            "configurable": {
                "litellm_client": self._llm,
            }
        }

        try:
            # execute_eda_sql & evaluate_hypothesis & decide_next_step are the
            # three post-generation nodes; generate_eda_sql was effectively
            # replaced by the policy's action, so we skip it and inject the
            # result directly.
            state = {**state, **(await execute_eda_sql(state, config))}
            state = {**state, **(await evaluate_hypothesis(state, config))}
            state = {**state, **(await decide_next_step(state, config))}
        except Exception as exc:
            logger.warning("eda_iteration_error", error=str(exc))
            return {}, f"EDA iteration error: {exc}"

        return state, None

    def _build_info(
        self,
        *,
        action_type: str,
        content: str,
        exec_error: str | None,
        step_duration: float,
        reward_detail: str | None = None,
        eda_next: str = "",
    ) -> dict[str, Any]:
        return {
            "step": self._step_count,
            "action_type": action_type,
            "content_length": len(content),
            "exec_error": exec_error,
            "step_duration_s": round(step_duration, 3),
            "reward_detail": reward_detail,
            "eda_next": eda_next,
            "episode_id": self._current_episode.id if self._current_episode else "",
        }


# ---------------------------------------------------------------------------
# Episode loading helpers
# ---------------------------------------------------------------------------
def _load_episodes(path: Path) -> list[EpisodeQuestion]:
    """Load episode YAML files from *path* (file or directory).

    Supports:
      - a single YAML file containing a mapping (one episode), a list of mappings,
        or an EvalSuite structure (with a top-level 'cases' list)
      - a directory — every *.yaml / *.yml file is loaded recursively
    """
    if path.is_file():
        episodes = _parse_yaml_file(path)
    elif path.is_dir():
        episodes = []
        for yaml_file in sorted(path.rglob("*.yaml")) + sorted(path.rglob("*.yml")):
            episodes.extend(_parse_yaml_file(yaml_file))
    else:
        raise FileNotFoundError(
            f"training_episodes_path {path!r} is neither a file nor a directory."
        )

    if not episodes:
        raise ValueError(
            f"No valid episode questions could be loaded from training_episodes_path {path!r}. "
            "Ensure the path contains valid episode YAML files with 'question' or 'cases'."
        )
    return episodes


def _parse_yaml_file(path: Path) -> list[EpisodeQuestion]:
    raw = yaml.safe_load(path.read_text()) or {}
    if isinstance(raw, list):
        return [EpisodeQuestion(item) for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        if "question" in raw:
            return [EpisodeQuestion(raw)]
        if "cases" in raw and isinstance(raw["cases"], list):
            suite_scorer = raw.get("scorer")
            suite_fixture = raw.get("fixture")
            episodes: list[EpisodeQuestion] = []
            for item in raw["cases"]:
                if isinstance(item, dict):
                    q_data = dict(item)
                    if "scorer" not in q_data and suite_scorer:
                        q_data["scorer"] = suite_scorer
                    if "fixture" not in q_data and suite_fixture:
                        q_data["fixture"] = suite_fixture
                    episodes.append(EpisodeQuestion(q_data))
            return episodes
    logger.warning("episode_yaml_skipped", path=str(path), reason="no 'question' or 'cases' key found")
    return []


def _initial_analysis_state(episode: EpisodeQuestion) -> dict[str, Any]:
    """Seed an AnalysisState-compatible dict from an episode question.

    The full AnalysisState schema is defined in
    multi-agent-backend/backend/analysis_state.py.  We populate the minimum
    required fields so eda_loop nodes don't KeyError.
    """
    return {
        "run_id": str(uuid.uuid4()),
        "business_question": episode.question,
        "hypotheses": (
            [
                {
                    "id": "h0",
                    "statement": episode.hypothesis,
                    "status": "pending",
                    "evidence": [],
                }
            ]
            if episode.hypothesis
            else []
        ),
        "queries": [],
        "findings": [],
        "recommendations": [],
        "failure_modes": [],
        "failures": [],
        "current_iteration": 0,
        "max_iterations": _DEFAULT_MAX_STEPS,
        "analysis_complete": False,
        "termination_reason": "",
        "needs_web_research": False,
        "web_searches": [],
        "metrics": {
            "total_iterations": 0,
            "total_tool_calls": 0,
            "total_sql_queries": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
        },
        "warehouse_schema": {},
        "analysis_plan": "",
    }
