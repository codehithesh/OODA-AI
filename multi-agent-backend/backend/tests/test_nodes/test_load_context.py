"""Unit tests for load_context (real git-versioned context directory)."""

from __future__ import annotations

from nodes.load_context import load_context, load_context_for_mode


async def test_analytics_context_bundle() -> None:
    result = await load_context({"mode": "analytics"}, {})
    context = result["context"]
    assert result["context_commit_sha"]
    assert len(result["context_commit_sha"]) >= 40
    assert context["prompts"]["generate_sql"] == "analytics/generate_sql.md"
    assert "analytics" in context["rules"]
    assert (
        "SELECT" in context["schemas"]["analytics_warehouse"]
        or "CREATE TABLE" in context["schemas"]["analytics_warehouse"]
    )
    assert context["manifest"], "manifest must list context files"


async def test_monitor_context_bundle() -> None:
    bundle = await load_context_for_mode("monitor")
    assert bundle.prompts["classify_signal"] == "monitor/classify_signal.md"
    assert bundle.rules["monitor"]["detection"]
    assert "monitor_event" in bundle.schemas


async def test_research_context_bundle() -> None:
    bundle = await load_context_for_mode("research")
    assert len(bundle.personas["research"]) == 3
    assert bundle.rules["research"]["max_generations"] == 2


async def test_simulate_context_bundle() -> None:
    bundle = await load_context_for_mode("simulate")
    assert len(bundle.personas["simulate"]) == 4
    assert bundle.rules["simulate"]["variant_count"] == 2
