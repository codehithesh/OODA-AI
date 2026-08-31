"""LangGraph node: synthesize peer research into a final answer or next brief."""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from clients.litellm_client import get_litellm_client
from clients.prompt_loader import get_prompt_loader
from nodes.classify_signal import parse_json_object

logger = structlog.get_logger(__name__)


class SynthesizeInput(BaseModel):
    """Input state keys read by synthesize."""

    query: str = ""
    peers: list[dict[str, Any]] = Field(default_factory=list)
    evidence_scores: list[dict[str, Any]] = Field(default_factory=list)
    research_ready: bool = False
    generation: int = 0
    max_generations: int = 2
    context: dict[str, Any] = Field(default_factory=dict)


class SynthesizeOutput(BaseModel):
    """Output state keys written by synthesize."""

    synthesis: str = ""
    next_brief: str | None = None
    research_ready: bool = False
    usage: list[dict[str, Any]] = Field(default_factory=list)


async def synthesize(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Synthesize peer contributions into an answer or a refined brief.

    When evidence is ready (or the generation budget is exhausted) the node
    produces the FINAL markdown answer. Otherwise it produces an interim
    synthesis plus ``next_brief``, which feeds the next generation of peers.

    Input state keys:
        query, peers, evidence_scores, research_ready, generation,
        max_generations, context (prompts['synthesize']).

    Output state keys:
        synthesis: final answer (when ready) or interim synthesis.
        next_brief: refined brief for the next generation (None when done).
        research_ready: forced True when the loop must stop.
        usage: appended LLM usage record.

    Side-effect guarantees:
        One LLM call through the LiteLLM proxy. No database writes.
    """
    inp = SynthesizeInput.model_validate(state)
    context = inp.context or {}
    rules = (context.get("rules") or {}).get("research", {})
    max_generations = int(
        state.get("max_generations", rules.get("max_generations", inp.max_generations))
    )
    forced_stop = inp.generation >= max_generations
    ready = inp.research_ready or forced_stop

    loader = get_prompt_loader(dict(config) if config else None)
    llm = get_litellm_client(dict(config) if config else None)
    template = (context.get("prompts") or {}).get("synthesize", "research/synthesize.md")

    prompt = loader.render(
        template,
        query=inp.query,
        peers=inp.peers,
        evaluations=inp.evidence_scores,
        generation=inp.generation,
        ready=ready,
    )
    response = await llm.chat([{"role": "user", "content": prompt}])

    if ready:
        logger.info("research_finalized", generation=inp.generation, forced=forced_stop)
        return SynthesizeOutput(
            synthesis=response.content,
            next_brief=None,
            research_ready=True,
            usage=[response.usage_record()],
        ).model_dump()

    parsed = parse_json_object(response.content) or {}
    next_brief = str(parsed.get("next_brief", "")) or inp.query
    interim = str(parsed.get("synthesis", response.content))
    logger.info("research_next_generation", generation=inp.generation)
    return SynthesizeOutput(
        synthesis=interim,
        next_brief=next_brief,
        research_ready=False,
        usage=[response.usage_record()],
    ).model_dump()
