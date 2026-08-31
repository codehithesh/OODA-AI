"""Analytics agent — linear pipeline.

load_context -> generate_sql -> validate_sql -> log_decision

The simplest graph: context loading is an explicit node here (the reference
implementation); the runner pre-seeds context for the other three modes.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from graphs.base import GraphState, log_decision, register_graph
from nodes.generate_sql import generate_sql
from nodes.load_context import load_context
from nodes.validate_sql import validate_sql_node


def build_analytics_graph() -> StateGraph:
    """Build (uncompiled) the analytics linear pipeline."""
    builder = StateGraph(GraphState)
    builder.add_node("load_context", load_context)
    builder.add_node("generate_sql", generate_sql)
    builder.add_node("validate_sql", validate_sql_node)
    builder.add_node("log_decision", log_decision)

    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "generate_sql")
    builder.add_edge("generate_sql", "validate_sql")
    builder.add_edge("validate_sql", "log_decision")
    builder.add_edge("log_decision", END)
    return builder


register_graph("analytics", build_analytics_graph)
