"""LangGraph node: spawn simulate personas from the git-versioned context."""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class SpawnPersonasInput(BaseModel):
    """Input state keys read by spawn_personas."""

    context: dict[str, Any] = Field(default_factory=dict)


async def spawn_personas(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Load the simulation personas for this run.

    Personas are declarative YAML files under context/personas/simulate/ —
    this node only selects them (bounded by rules.simulate.persona_count).

    Input state keys:
        context: ContextBundle with personas['simulate'] and rules['simulate'].

    Output state keys:
        personas: list of persona dicts (name, archetype, temperament,
                  priorities, voice, weight).

    Side-effect guarantees:
        None — reads already-loaded context only. No I/O, no LLM calls.
    """
    inp = SpawnPersonasInput.model_validate(state)
    context = inp.context or {}
    rules = (context.get("rules") or {}).get("simulate", {})
    personas = (context.get("personas") or {}).get("simulate", [])
    limit = int(rules.get("persona_count", len(personas)))
    selected = personas[:limit] if limit > 0 else personas
    logger.info("personas_spawned", count=len(selected))
    return {"personas": selected}
