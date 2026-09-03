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
    ontology: dict[str, Any] = Field(default_factory=dict)  # merged shared + mode-specific concepts


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


def _load_ontology(context_dir: Path, mode: str) -> dict[str, Any]:
    """Load and merge the shared ontology with any mode-specific extensions.

    Reads two files (both optional):
      - ``context_dir/ontology/shared.yaml``   — cross-mode concepts (customer,
        order, revenue) used by both ``eda`` and ``analytics``.
      - ``context_dir/ontology/{mode}_ontology.yaml`` — mode-specific additions
        or overrides.

    Merge strategy: shared is the base; mode-specific ``concepts`` and
    ``relationships`` lists are appended (not replaced) so shared concepts are
    always present.  Duplicate concept ids from the mode file are skipped.

    Returns an empty dict (not an error) when neither file exists, so callers
    that don't yet have an ontology directory work without modification.
    """
    ontology_dir = context_dir / "ontology"

    shared: dict[str, Any] = {}
    shared_path = ontology_dir / "shared.yaml"
    if shared_path.exists():
        shared = yaml.safe_load(shared_path.read_text()) or {}

    mode_specific: dict[str, Any] = {}
    mode_path = ontology_dir / f"{mode}_ontology.yaml"
    if mode_path.exists():
        mode_specific = yaml.safe_load(mode_path.read_text()) or {}

    if not shared and not mode_specific:
        return {}

    # Start from shared as the base.
    merged: dict[str, Any] = {
        "concepts": list(shared.get("concepts") or []),
        "relationships": list(shared.get("relationships") or []),
    }

    # Append any extra fields from shared (e.g. metadata) that aren't concepts/relationships.
    for key, val in shared.items():
        if key not in ("concepts", "relationships"):
            merged[key] = val

    # Merge mode-specific: append new concepts (deduplicate by id), append relationships.
    existing_ids = {c["id"] for c in merged["concepts"] if isinstance(c, dict) and "id" in c}
    for concept in mode_specific.get("concepts") or []:
        if isinstance(concept, dict) and concept.get("id") not in existing_ids:
            merged["concepts"].append(concept)
            existing_ids.add(concept["id"])

    for rel in mode_specific.get("relationships") or []:
        merged["relationships"].append(rel)

    # Carry over any other top-level keys from mode-specific (e.g. notes).
    for key, val in mode_specific.items():
        if key not in ("concepts", "relationships") and key not in merged:
            merged[key] = val

    return merged


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
        ontology=_load_ontology(context_dir, mode),
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
