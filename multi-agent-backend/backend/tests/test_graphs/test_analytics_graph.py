"""Integration tests for the analytics graph (MemorySaver + scripted LLM)."""

from __future__ import annotations

import uuid

from langgraph.checkpoint.memory import MemorySaver

from clients.prompt_loader import get_default_prompt_loader
from graphs.base import get_graph
from tests.conftest import FakeLLM


def _config(fake: FakeLLM, thread: str) -> dict:
    return {
        "configurable": {
            "thread_id": thread,
            "litellm_client": fake,
            "prompt_loader": get_default_prompt_loader(),
        }
    }


async def test_analytics_happy_path() -> None:
    fake = FakeLLM(responses=["```sql\nSELECT SUM(amount) AS total_revenue FROM orders\n```"])
    graph = get_graph("analytics", MemorySaver())
    state = {
        "mode": "analytics",
        "query": "total revenue?",
        "input": {"query": "total revenue?"},
        "usage": [],
    }
    values = await graph.ainvoke(state, _config(fake, f"t-{uuid.uuid4()}"))

    assert values["generated_sql"] == "SELECT SUM(amount) AS total_revenue FROM orders"
    assert values["sql_valid"] is True
    assert values["context_commit_sha"]
    record = values["decision_record"]
    assert record["mode"] == "analytics"
    assert record["evaluation_score"] == 1.0
    assert record["cost_usd"] > 0
    assert record["prompt_tokens"] == 10


async def test_analytics_invalid_sql_records_errors() -> None:
    fake = FakeLLM(responses=["```sql\nDELETE FROM orders\n```"])
    graph = get_graph("analytics", MemorySaver())
    values = await graph.ainvoke(
        {
            "mode": "analytics",
            "query": "delete everything",
            "input": {"query": "delete"},
            "usage": [],
        },
        _config(fake, f"t-{uuid.uuid4()}"),
    )
    assert values["sql_valid"] is False
    assert values["sql_validation_errors"]
    assert values["decision_record"]["evaluation_score"] == 0.0
