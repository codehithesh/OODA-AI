"""Research agent — cyclic peer-review pipeline.

parallel_peers -> evaluate_evidence -> synthesize -> [next_gen_or_stop]

The loop repeats (each generation gets a refined brief) until the evidence
reaches the consensus threshold or the generation budget
(rules['research'].max_generations) is exhausted.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from graphs.base import GraphState, log_decision, register_graph
from nodes.evaluate_evidence import evaluate_evidence
from nodes.parallel_peers import parallel_peers
from nodes.synthesize import synthesize


def _next_gen_or_stop(state: dict[str, Any]) -> str:
    if state.get("research_ready"):
        return "log_decision"
    if state.get("generation", 0) >= int(state.get("max_generations", 2)):
        return "log_decision"
    return "parallel_peers"


def build_research_graph() -> StateGraph:
    """Build (uncompiled) the research cyclic pipeline."""
    builder = StateGraph(GraphState)
    builder.add_node("parallel_peers", parallel_peers)
    builder.add_node("evaluate_evidence", evaluate_evidence)
    builder.add_node("synthesize", synthesize)
    builder.add_node("log_decision", log_decision)

    builder.add_edge(START, "parallel_peers")
    builder.add_edge("parallel_peers", "evaluate_evidence")
    builder.add_edge("evaluate_evidence", "synthesize")
    builder.add_conditional_edges(
        "synthesize",
        _next_gen_or_stop,
        {"parallel_peers": "parallel_peers", "log_decision": "log_decision"},
    )
    builder.add_edge("log_decision", END)
    return builder


register_graph("research", build_research_graph)
