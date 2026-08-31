"""LangGraph node: gather parallel peer analyses for the research agent."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from clients.litellm_client import get_litellm_client
from clients.prompt_loader import get_prompt_loader
from nodes.classify_signal import parse_json_object

logger = structlog.get_logger(__name__)


class ParallelPeersInput(BaseModel):
    """Input state keys read by parallel_peers."""

    query: str = ""
    brief: str = ""
    generation: int = 0
    context: dict[str, Any] = Field(default_factory=dict)


class PeerContribution(BaseModel):
    """One peer persona's independent analysis."""

    peer_id: str
    persona: str
    role: str = ""
    claim: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    open_questions: list[str] = Field(default_factory=list)


async def parallel_peers(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Run all research peer personas concurrently on the current brief.

    Input state keys:
        query: original research question.
        brief: research brief for THIS generation (falls back to query).
        generation: zero-based generation counter (incremented by this node).
        context: ContextBundle with personas['research'] and rules['research'].

    Output state keys:
        peers: list of PeerContribution dicts.
        generation: incremented generation counter.
        brief: the brief this generation analysed.
        usage: appended LLM usage records (one per peer).

    Side-effect guarantees:
        N concurrent LLM calls via asyncio.gather — no database writes, no
        shared mutable state. A failing peer degrades to an empty contribution
        instead of failing the graph.
    """
    inp = ParallelPeersInput.model_validate(state)
    context = inp.context or {}
    rules = (context.get("rules") or {}).get("research", {})
    personas = (context.get("personas") or {}).get("research", [])
    peer_count = int(rules.get("peer_count", len(personas)))
    brief = inp.brief or inp.query

    loader = get_prompt_loader(dict(config) if config else None)
    llm = get_litellm_client(dict(config) if config else None)
    template = (context.get("prompts") or {}).get("peer_response", "research/peer_response.md")

    async def run_peer(persona: dict[str, Any]) -> PeerContribution:
        prompt = loader.render(
            template,
            persona=persona,
            brief=brief,
            generation=inp.generation + 1,
        )
        try:
            response = await llm.chat([{"role": "user", "content": prompt}])
            usage_records.append(response.usage_record())
            parsed = parse_json_object(response.content) or {}
            return PeerContribution(
                peer_id=str(persona.get("id", persona.get("name", "peer"))),
                persona=str(persona.get("name", "peer")),
                role=str(persona.get("role", "")),
                claim=str(parsed.get("claim", ""))[:2000],
                evidence=[str(e)[:300] for e in (parsed.get("evidence") or [])[:6]],
                confidence=min(1.0, max(0.0, float(parsed.get("confidence", 0.5)))),
                open_questions=[str(q)[:300] for q in (parsed.get("open_questions") or [])[:4]],
            )
        except Exception as exc:
            logger.warning("peer_failed", persona=persona.get("name"), error=str(exc))
            return PeerContribution(
                peer_id=str(persona.get("id", persona.get("name", "peer"))),
                persona=str(persona.get("name", "peer")),
                role=str(persona.get("role", "")),
                open_questions=[f"peer analysis failed: {exc}"],
            )

    usage_records: list[dict[str, Any]] = []
    selected = personas[:peer_count] if peer_count > 0 else personas
    peers = list(await asyncio.gather(*(run_peer(p) for p in selected)))
    logger.info("peers_completed", generation=inp.generation + 1, peers=len(peers))
    return {
        "peers": [p.model_dump() for p in peers],
        "generation": inp.generation + 1,
        "brief": brief,
        "usage": usage_records,
    }
