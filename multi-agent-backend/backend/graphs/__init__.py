"""Graphs package — compiled LangGraph state machines, one per agent mode.

Importing the package registers each graph builder so callers can resolve modes via
``graphs.base.get_graph()`` without explicitly importing every mode module.
"""

# Import graph modules for registration side effects.
from . import analytics_graph  # noqa: F401
from . import monitor_graph  # noqa: F401
from . import research_graph  # noqa: F401
from . import simulate_graph  # noqa: F401

