"""Simulate agent — persona fan-out pipeline.

spawn_personas -> run_draft -> collect_reactions -> score_variants -> pick_winner

Every persona reacts to every draft variant (personas x variants parallel LLM
calls inside collect_reactions); deterministic scoring then picks the winner.
``run_draft`` and ``pick_winner`` are intentionally local to this module: they
are thin graph-specific steps, not reusable nodes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from clients.litellm_client import get_litellm_client
from clients.prompt_loader import get_prompt_loader
from graphs.base import GraphState, log_decision, register_graph
from nodes.collect_reactions import collect_reactions
from nodes.score_variants import score_variants
from nodes.spawn_personas import spawn_personas

logger = structlog.get_logger(__name__)

_FALLBACK_STYLE_GUIDANCE = {
    "concise": "Answer in at most three tight, information-dense sentences.",
    "detailed": "Answer thoroughly with structured markdown headings and evidence.",
}


async def run_draft(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Generate K draft variants concurrently.

    Input state keys:
        query, context (rules['simulate'].variant_count/variant_styles,
        prompts['draft']).

    Output state keys:
        drafts: list of {variant_id, style, text}.
        usage: appended LLM usage records.

    Side-effect guarantees:
        K concurrent LLM calls. No database writes.
    """
    context = state.get("context") or {}
    rules = (context.get("rules") or {}).get("simulate", {})
    styles = list(rules.get("variant_styles", ["concise", "detailed"]))
    guidance = rules.get("style_guidance", {})
    count = max(1, int(rules.get("variant_count", 2)))
    chosen = [styles[i % len(styles)] for i in range(count)]

    loader = get_prompt_loader(dict(config) if config else None)
    llm = get_litellm_client(dict(config) if config else None)
    template = (context.get("prompts") or {}).get("draft", "simulate/draft.md")

    async def draft_one(index: int, style: str) -> dict[str, Any]:
        variant_id = f"v{index + 1}"
        prompt = loader.render(
            template,
            variant_id=variant_id,
            style=style,
            style_guidance=str(guidance.get(style) or _FALLBACK_STYLE_GUIDANCE.get(style, "")),
            query=state.get("query", ""),
        )
        response = await llm.chat([{"role": "user", "content": prompt}])
        usage_records.append(response.usage_record())
        return {"variant_id": variant_id, "style": style, "text": response.content}

    usage_records: list[dict[str, Any]] = []
    drafts = list(await asyncio.gather(*(draft_one(i, s) for i, s in enumerate(chosen))))
    logger.info("drafts_generated", count=len(drafts))
    return {"drafts": drafts, "usage": usage_records}


async def pick_winner(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Select the highest-scoring draft.

    Input state keys:
        scores (sorted descending), drafts.

    Output state keys:
        winner: {variant_id, score, style, text, breakdown} or None.

    Side-effect guarantees:
        None — pure selection.
    """
    scores = state.get("scores") or []
    drafts = {str(d.get("variant_id")): d for d in state.get("drafts") or []}
    if not scores:
        return {"winner": None}
    top = scores[0]
    draft = drafts.get(str(top.get("variant_id")), {})
    return {
        "winner": {
            "variant_id": top.get("variant_id"),
            "score": top.get("score", 0.0),
            "style": draft.get("style", ""),
            "text": draft.get("text", ""),
            "breakdown": top.get("breakdown", {}),
        }
    }


def build_simulate_graph() -> StateGraph:
    """Build (uncompiled) the simulate fan-out pipeline."""
    builder = StateGraph(GraphState)
    builder.add_node("spawn_personas", spawn_personas)
    builder.add_node("run_draft", run_draft)
    builder.add_node("collect_reactions", collect_reactions)
    builder.add_node("score_variants", score_variants)
    builder.add_node("pick_winner", pick_winner)
    builder.add_node("log_decision", log_decision)

    builder.add_edge(START, "spawn_personas")
    builder.add_edge("spawn_personas", "run_draft")
    builder.add_edge("run_draft", "collect_reactions")
    builder.add_edge("collect_reactions", "score_variants")
    builder.add_edge("score_variants", "pick_winner")
    builder.add_edge("pick_winner", "log_decision")
    builder.add_edge("log_decision", END)
    return builder


register_graph("simulate", build_simulate_graph)
