"""Integration tests for the simulate graph (persona fan-out + winner)."""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver

from clients.prompt_loader import get_default_prompt_loader
from graphs.base import get_graph
from nodes.load_context import load_context_for_mode
from tests.conftest import FakeLLM


@pytest.fixture
async def simulate_context() -> dict:
    return (await load_context_for_mode("simulate")).model_dump()


def _config(fake: FakeLLM, thread: str) -> dict:
    return {
        "configurable": {
            "thread_id": thread,
            "litellm_client": fake,
            "prompt_loader": get_default_prompt_loader(),
        }
    }


async def test_simulate_picks_winner(simulate_context: dict) -> None:
    # 2 drafts, then 8 persona reactions. asyncio.gather preserves task order,
    # which is persona-major: (p1,v1),(p1,v2),(p2,v1),(p2,v2),...
    # Scripted so v1 gets 4 supports and v2 gets 2 opposes + 2 neutrals.
    reactions = [
        '{"stance": "support", "intensity": 5, "rationale": "r", "key_concern": "k"}',  # p1 v1
        '{"stance": "oppose", "intensity": 5, "rationale": "r", "key_concern": "k"}',  # p1 v2
        '{"stance": "support", "intensity": 4, "rationale": "r", "key_concern": "k"}',  # p2 v1
        '{"stance": "neutral", "intensity": 2, "rationale": "r", "key_concern": "k"}',  # p2 v2
        '{"stance": "support", "intensity": 5, "rationale": "r", "key_concern": "k"}',  # p3 v1
        '{"stance": "oppose", "intensity": 4, "rationale": "r", "key_concern": "k"}',  # p3 v2
        '{"stance": "support", "intensity": 3, "rationale": "r", "key_concern": "k"}',  # p4 v1
        '{"stance": "neutral", "intensity": 1, "rationale": "r", "key_concern": "k"}',  # p4 v2
    ]
    fake = FakeLLM(responses=["Draft one text", "Draft two text", *reactions])
    graph = get_graph("simulate", MemorySaver())
    state = {
        "mode": "simulate",
        "query": "should we ship feature X?",
        "input": {"query": "should we ship feature X?"},
        "context": simulate_context,
        "context_commit_sha": simulate_context["commit_sha"],
        "usage": [],
    }
    values = await graph.ainvoke(state, _config(fake, f"t-{uuid.uuid4()}"))

    assert len(values["drafts"]) == 2
    assert len(values["reactions"]) == 8
    winner = values["winner"]
    assert winner["variant_id"] == "v1"
    assert winner["text"] == "Draft one text"
    assert values["decision_record"]["evaluation_score"] == winner["score"]
    assert values["scores"][0]["variant_id"] == "v1"
    assert values["scores"][0]["support"] == 4
