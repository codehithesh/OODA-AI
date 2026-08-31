"""Integration tests for the monitor graph, including the approval interrupt.

The production runner seeds the monitor context into the initial state (the
monitor graph starts at detect_signal, not load_context); these tests do the
same before invoking the graph directly.
"""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from clients.prompt_loader import get_default_prompt_loader
from graphs.base import get_graph
from nodes.load_context import load_context_for_mode
from tests.conftest import FakeLLM

CLASSIFICATION = (
    '{"kind": "error_burst", "severity": "critical", "'
    'summary": "errors spiking", "confidence": 0.9}'
)


@pytest.fixture
async def monitor_context() -> dict:
    return (await load_context_for_mode("monitor")).model_dump()


def _monitor_state(event: dict, context: dict) -> dict:
    return {
        "mode": "monitor",
        "query": "monitor event",
        "input": {"query": "monitor event", "event": event, "signal_id": "sig-1"},
        "event": event,
        "context": context,
        "context_commit_sha": context["commit_sha"],
        "usage": [],
    }


def _config(fake: FakeLLM, thread: str) -> dict:
    return {
        "configurable": {
            "thread_id": thread,
            "litellm_client": fake,
            "prompt_loader": get_default_prompt_loader(),
        }
    }


async def test_monitor_auto_act_path(monitor_context: dict) -> None:
    fake = FakeLLM(responses=[CLASSIFICATION])
    checkpointer = MemorySaver()
    graph = get_graph("monitor", checkpointer)
    thread = f"t-{uuid.uuid4()}"
    state = _monitor_state(
        {"metric": "queue_depth", "value": 2400, "source": "ingestion"}, monitor_context
    )

    values = await graph.ainvoke(state, _config(fake, thread))
    assert values["signal_detected"] is True
    assert values["action"] == "auto_act"
    assert values["approval"] == {"required": False, "approved": True, "decided_by": "auto"}
    assert values["decision_record"]["mode"] == "monitor"
    snapshot = await graph.aget_state(_config(fake, thread))
    assert not snapshot.next  # completed, no pending nodes


async def test_monitor_noise_skips_classification(monitor_context: dict) -> None:
    fake = FakeLLM(responses=[])  # must not be called
    graph = get_graph("monitor", MemorySaver())
    values = await graph.ainvoke(
        _monitor_state(
            {"metric": "error_rate", "value": 0.001, "source": "payments"}, monitor_context
        ),
        _config(fake, f"t-{uuid.uuid4()}"),
    )
    assert values["signal_detected"] is False
    assert fake.calls == []
    assert values["decision_record"]["output"]["signal_detected"] is False


async def test_monitor_approval_interrupt_and_resume_approved(monitor_context: dict) -> None:
    fake = FakeLLM(responses=[CLASSIFICATION])
    checkpointer = MemorySaver()
    graph = get_graph("monitor", checkpointer)
    thread = f"t-{uuid.uuid4()}"
    config = _config(fake, thread)

    values = await graph.ainvoke(
        _monitor_state(
            {"metric": "error_rate", "value": 0.31, "source": "payments"}, monitor_context
        ),
        config,
    )
    assert values["action"] == "require_approval"

    snapshot = await graph.aget_state(config)
    assert snapshot.next, "graph must be paused on the approval gate"
    interrupt_values = []
    for task in snapshot.tasks:
        interrupt_values.extend(i.value for i in getattr(task, "interrupts", []) or [])
    assert interrupt_values and interrupt_values[0]["type"] == "signal_approval"
    assert interrupt_values[0]["signal_id"] == "sig-1"

    resumed = await graph.ainvoke(Command(resume={"approved": True, "approver": "n8n"}), config)
    assert resumed["approval"] == {"required": True, "approved": True, "decided_by": "n8n"}
    assert resumed["decision_record"]["output"]["action"] == "require_approval"
    final_snapshot = await graph.aget_state(config)
    assert not final_snapshot.next


async def test_monitor_approval_interrupt_and_resume_rejected(monitor_context: dict) -> None:
    fake = FakeLLM(responses=[CLASSIFICATION])
    checkpointer = MemorySaver()
    graph = get_graph("monitor", checkpointer)
    thread = f"t-{uuid.uuid4()}"
    config = _config(fake, thread)

    await graph.ainvoke(
        _monitor_state(
            {"metric": "error_rate", "value": 0.31, "source": "payments"}, monitor_context
        ),
        config,
    )
    resumed = await graph.ainvoke(Command(resume={"approved": False, "approver": "oncall"}), config)
    assert resumed["approval"]["approved"] is False
    assert resumed["approval"]["decided_by"] == "oncall"


async def test_monitor_ignored_action_skips_gate(monitor_context: dict) -> None:
    fake = FakeLLM(responses=[CLASSIFICATION])
    graph = get_graph("monitor", MemorySaver())
    values = await graph.ainvoke(
        _monitor_state(
            {"metric": "disk_free_percent", "value": 3.1, "source": "db"}, monitor_context
        ),
        _config(fake, f"t-{uuid.uuid4()}"),
    )
    # low severity -> ignore -> log_decision directly, approval never required
    assert values["action"] == "ignore"
    assert "approval" not in values or values.get("approval") is None
