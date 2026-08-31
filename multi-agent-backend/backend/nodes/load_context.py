"""LangGraph node: load git-versioned agent context for the run's mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from clients.git_context import get_git_context_client
from config import get_settings

logger = structlog.get_logger(__name__)


class LoadContextInput(BaseModel):
    """Input state keys read by load_context."""

    mode: str = "analytics"


class ContextBundle(BaseModel):
    """Everything a run needs from the git-versioned context directory."""

    commit_sha: str = ""
    manifest: dict[str, str] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)  # logical name -> template path
    rules: dict[str, Any] = Field(default_factory=dict)  # mode -> rules dict
    schemas: dict[str, str] = Field(default_factory=dict)  # name -> content
    personas: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


_MODE_SCHEMAS: dict[str, list[str]] = {
    "analytics": ["analytics_warehouse.sql"],
    "monitor": ["monitor_event.json"],
}


def _scan_prompts(context_dir: Path, mode: str) -> dict[str, str]:
    prompts_dir = context_dir / "prompts" / mode
    if not prompts_dir.exists():
        return {}
    return {p.stem: f"{mode}/{p.name}" for p in sorted(prompts_dir.glob("*.md"))}


def _load_rules(context_dir: Path, mode: str) -> dict[str, Any]:
    path = context_dir / "rules" / f"{mode}_rules.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {mode: data}


def _load_schemas(context_dir: Path, mode: str) -> dict[str, str]:
    schemas: dict[str, str] = {}
    for name in _MODE_SCHEMAS.get(mode, []):
        path = context_dir / "schemas" / name
        if path.exists():
            schemas[path.stem] = path.read_text()
    return schemas


def _load_personas(context_dir: Path, mode: str) -> dict[str, list[dict[str, Any]]]:
    personas_dir = context_dir / "personas" / mode
    if not personas_dir.exists():
        return {}
    personas = []
    for path in sorted(personas_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        if isinstance(data, dict):
            data.setdefault("name", path.stem)
            data.setdefault("id", path.stem)
            personas.append(data)
    return {mode: personas}


async def load_context_for_mode(mode: str) -> ContextBundle:
    """Assemble the ContextBundle for an agent mode (shared by node + runner)."""
    context_dir = get_settings().context_path
    manifest = await get_git_context_client().amanifest()
    return ContextBundle(
        commit_sha=manifest.commit_sha,
        manifest=manifest.files,
        prompts=_scan_prompts(context_dir, mode),
        rules=_load_rules(context_dir, mode),
        schemas=_load_schemas(context_dir, mode),
        personas=_load_personas(context_dir, mode),
    )


async def load_context(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Load the git-versioned agent context for this run's mode.

    Input state keys:
        mode: the agent mode whose context should be loaded.

    Output state keys:
        context: ContextBundle (commit sha, manifest, prompts, rules, schemas,
                 personas) as a dict.
        context_commit_sha: commit SHA (or content-hash fallback) of context/.

    Side-effect guarantees:
        Read-only filesystem and git access. No database writes, no LLM calls.
    """
    inp = LoadContextInput.model_validate(state)
    bundle = await load_context_for_mode(inp.mode)
    logger.info(
        "context_loaded",
        mode=inp.mode,
        commit_sha=bundle.commit_sha,
        files=len(bundle.manifest),
    )
    return {"context": bundle.model_dump(), "context_commit_sha": bundle.commit_sha}
