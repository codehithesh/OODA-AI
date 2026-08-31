"""Monitor agent — event-driven pipeline with a human approval gate.

detect_signal -> classify_signal -> decide_action -> [approve_or_auto_act] -> log_decision

* Undetected events skip straight to logging.
* ``ignore`` actions skip the approval gate.
* ``require_approval`` actions pause the graph via LangGraph ``interrupt()``.
  The checkpointer persists the paused state; the n8n approval workflow POSTs
  back to POST /v1/signals/{id}/approve, which resumes the graph with
  ``Command(resume={"approved": ..., "approver": ...})``.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import RunnableConfig, interrupt

from graphs.base import GraphState, log_decision, register_graph
from nodes.classify_signal import classify_signal
from nodes.decide_action import decide_action
from nodes.detect_signal import detect_signal


def _after_detection(state: dict[str, Any]) -> str:
    return "classify_signal" if state.get("signal_detected") else "log_decision"


def _after_decision(state: dict[str, Any]) -> str:
    return "approve_or_auto_act" if state.get("action") != "ignore" else "log_decision"


async def approve_or_auto_act(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Approval gate: auto-approve safe actions, pause for human judgement.

    Input state keys:
        action: 'auto_act' | 'require_approval' | 'ignore'.
        action_plan, signal, classification, input (signal_id for callbacks).

    Output state keys:
        approval: {required, approved, decided_by}.

    Side-effect guarantees:
        None in the usual sense — ``interrupt()`` only PAUSES the graph; the
        checkpointer persists the paused state to PostgreSQL and the run is
        resumed through the route layer (POST /v1/signals/{id}/approve).
    """
    action = state.get("action", "ignore")
    if action != "require_approval":
        return {
            "approval": {
                "required": False,
                "approved": action == "auto_act",
                "decided_by": "auto",
            }
        }

    payload = {
        "type": "signal_approval",
        "signal_id": (state.get("input") or {}).get("signal_id"),
        "signal": state.get("signal"),
        "classification": state.get("classification"),
        "action_plan": state.get("action_plan"),
        "summary": (state.get("classification") or {}).get("summary"),
    }
    decision = interrupt(payload)

    approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
    decided_by = (
        str(decision.get("approver") or "resumed") if isinstance(decision, dict) else "resumed"
    )
    return {"approval": {"required": True, "approved": approved, "decided_by": decided_by}}


def build_monitor_graph() -> StateGraph:
    """Build (uncompiled) the monitor event-driven pipeline."""
    builder = StateGraph(GraphState)
    builder.add_node("detect_signal", detect_signal)
    builder.add_node("classify_signal", classify_signal)
    builder.add_node("decide_action", decide_action)
    builder.add_node("approve_or_auto_act", approve_or_auto_act)
    builder.add_node("log_decision", log_decision)

    builder.add_edge(START, "detect_signal")
    builder.add_conditional_edges(
        "detect_signal",
        _after_detection,
        {"classify_signal": "classify_signal", "log_decision": "log_decision"},
    )
    builder.add_edge("classify_signal", "decide_action")
    builder.add_conditional_edges(
        "decide_action",
        _after_decision,
        {"approve_or_auto_act": "approve_or_auto_act", "log_decision": "log_decision"},
    )
    builder.add_edge("approve_or_auto_act", "log_decision")
    builder.add_edge("log_decision", END)
    return builder


register_graph("monitor", build_monitor_graph)
