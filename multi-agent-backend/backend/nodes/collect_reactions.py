"""LangGraph node: collect persona reactions to every draft variant (fan-out)."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from clients.litellm_client import get_litellm_client
from clients.prompt_loader import get_prompt_loader
from nodes.classify_signal import normalize_stance, parse_json_object

logger = structlog.get_logger(__name__)


class CollectReactionsInput(BaseModel):
    """Input state keys read by collect_reactions."""

    query: str = ""
    drafts: list[dict[str, Any]] = Field(default_factory=list)
    personas: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class PersonaReaction(BaseModel):
    """One persona's simulated reaction to one draft variant."""

    variant_id: str
    persona_id: str
    persona: str
    stance: str = "neutral"  # support | neutral | oppose
    intensity: int = 3  # 1..5
    rationale: str = ""
    key_concern: str = ""


async def collect_reactions(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Collect every persona's reaction to every draft, concurrently.

    The fan-out is personas x drafts parallel LLM calls via asyncio.gather; a
    failing reaction degrades to a neutral one instead of failing the graph.

    Input state keys:
        query: the original question.
        drafts: list of {variant_id, style, text}.
        personas: list of persona dicts from spawn_personas.
        context: ContextBundle with prompts['persona_reaction'].

    Output state keys:
        reactions: list of PersonaReaction dicts.
        usage: appended LLM usage records (one per call).

    Side-effect guarantees:
        Concurrent LLM calls only. No database writes.
    """
    inp = CollectReactionsInput.model_validate(state)
    context = inp.context or {}
    loader = get_prompt_loader(dict(config) if config else None)
    llm = get_litellm_client(dict(config) if config else None)
    template = (context.get("prompts") or {}).get(
        "persona_reaction", "simulate/persona_reaction.md"
    )

    usage_records: list[dict[str, Any]] = []

    async def react(persona: dict[str, Any], draft: dict[str, Any]) -> PersonaReaction:
        prompt = loader.render(
            template,
            persona=persona,
            query=inp.query,
            draft=str(draft.get("text", ""))[:4000],
            variant_id=str(draft.get("variant_id", "")),
        )
        fallback = PersonaReaction(
            variant_id=str(draft.get("variant_id", "")),
            persona_id=str(persona.get("id", persona.get("name", "persona"))),
            persona=str(persona.get("name", "persona")),
        )
        try:
            response = await llm.chat([{"role": "user", "content": prompt}])
            usage_records.append(response.usage_record())
            parsed = parse_json_object(response.content) or {}
            try:
                intensity = int(parsed.get("intensity", 3))
            except (TypeError, ValueError):
                intensity = 3
            return PersonaReaction(
                variant_id=fallback.variant_id,
                persona_id=fallback.persona_id,
                persona=fallback.persona,
                stance=normalize_stance(parsed.get("stance")),
                intensity=max(1, min(5, intensity)),
                rationale=str(parsed.get("rationale", ""))[:500],
                key_concern=str(parsed.get("key_concern", ""))[:300],
            )
        except Exception as exc:
            logger.warning("persona_reaction_failed", persona=fallback.persona, error=str(exc))
            return fallback

    tasks = [react(persona, draft) for persona in inp.personas for draft in inp.drafts]
    reactions = list(await asyncio.gather(*tasks))
    logger.info("reactions_collected", count=len(reactions))
    return {"reactions": [r.model_dump() for r in reactions], "usage": usage_records}
