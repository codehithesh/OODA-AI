"""Integration tests for the research graph (cyclic peer review loop)."""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver

from clients.prompt_loader import get_default_prompt_loader
from graphs.base import get_graph
from nodes.load_context import load_context_for_mode
from tests.conftest import FakeLLM

STRONG_PEER = (
    '{"claim": "c", "evidence": ["e1", "e2", "e3", "e4"], "confidence": 0.9, "open_questions": []}'
)
WEAK_PEER = '{"claim": "c", "evidence": [], "confidence": 0.2, "open_questions": ["?"]}'


@pytest.fixture
async def research_context() -> dict:
    return (await load_context_for_mode("research")).model_dump()


def _config(fake: FakeLLM, thread: str) -> dict:
    return {
        "configurable": {
            "thread_id": thread,
            "litellm_client": fake,
            "prompt_loader": get_default_prompt_loader(),
        }
    }


def _state(query: str, context: dict) -> dict:
    return {
        "mode": "research",
        "query": query,
        "input": {"query": query},
        "brief": query,
        "generation": 0,
        "max_generations": int((context["rules"].get("research") or {}).get("max_generations", 2)),
        "context": context,
        "context_commit_sha": context["commit_sha"],
        "usage": [],
    }


async def test_research_stops_when_consensus_reached(research_context: dict) -> None:
    # 3 strong peers in generation 1, then the final synthesis
    fake = FakeLLM(responses=[STRONG_PEER] * 3 + ["## Final answer\n\nConsensus reached."])
    graph = get_graph("research", MemorySaver())
    values = await graph.ainvoke(
        _state("is X true?", research_context), _config(fake, f"t-{uuid.uuid4()}")
    )
    assert values["research_ready"] is True
    assert values["synthesis"].startswith("## Final answer")
    assert values["generation"] == 1
    assert len(values["peers"]) == 3


async def test_research_loops_then_exhausts_budget(research_context: dict) -> None:
    # generation 1: weak peers -> interim; generation 2: strong -> final
    fake = FakeLLM(
        responses=[WEAK_PEER] * 3
        + ['{"synthesis": "interim", "next_brief": "sharper"}']
        + [STRONG_PEER] * 3
        + ["## Final answer after two generations"]
    )
    graph = get_graph("research", MemorySaver())
    values = await graph.ainvoke(_state("q", research_context), _config(fake, f"t-{uuid.uuid4()}"))
    assert values["generation"] == 2
    assert values["synthesis"] == "## Final answer after two generations"
    assert values["decision_record"]["output"]["generation"] == 2
    # 6 peer calls + 2 synthesis calls
    assert len(fake.calls) == 8
