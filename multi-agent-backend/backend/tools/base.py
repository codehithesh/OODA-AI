"""Base tool contract — every agent tool inherits from BaseTool.

Tools are the agent's primitive action units.  Each tool has:

* a canonical ``name`` and human-readable ``description``
* a Pydantic ``InputSchema`` class (subclass of ``ToolInput``)
* a Pydantic ``OutputSchema`` class (subclass of ``ToolOutput``)
* an async ``_run(input)`` implementation
* automatic timing, retry counting, and error classification
* a structured ``ToolResult`` envelope returned to the caller

The agent selects tools by name; the ``ToolRegistry`` maps names → instances.
"""

from __future__ import annotations

import time
import traceback
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class ToolCategory(str, Enum):
    WAREHOUSE = "warehouse"
    WEB = "web"
    ANALYSIS = "analysis"
    VISUALIZATION = "visualization"
    N8N = "n8n"
    EXTRACTION = "extraction"
    PROFILING = "profiling"


class ToolStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Structured result envelope
# ---------------------------------------------------------------------------
class ToolResult(BaseModel):
    """Envelope returned from every tool invocation."""

    tool_name: str
    status: ToolStatus = ToolStatus.SUCCESS
    output: Any = None
    error: str | None = None
    error_type: str | None = None  # one of the 5 failure modes
    latency_ms: int = 0
    retries: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == ToolStatus.SUCCESS

    def to_state_record(self) -> dict[str, Any]:
        """Compact dict appended to AnalysisState.tool_calls."""
        return {
            "tool": self.tool_name,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "retries": self.retries,
            "error_type": self.error_type,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Base tool
# ---------------------------------------------------------------------------
class BaseTool(ABC):
    """Abstract base class for all agent tools."""

    name: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[ToolCategory]

    async def run(self, input_data: dict[str, Any], retries: int = 0) -> ToolResult:
        """Public entry point: wraps ``_run`` with timing and error handling."""
        t0 = time.perf_counter()
        try:
            output = await self._run(input_data)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            logger.info(
                "tool_succeeded",
                tool=self.name,
                latency_ms=latency_ms,
                retries=retries,
            )
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.SUCCESS,
                output=output,
                latency_ms=latency_ms,
                retries=retries,
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            error_type = self._classify_error(exc)
            logger.warning(
                "tool_failed",
                tool=self.name,
                error=str(exc),
                error_type=error_type,
                latency_ms=latency_ms,
            )
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.FAILURE,
                error=f"{type(exc).__name__}: {exc}",
                error_type=error_type,
                latency_ms=latency_ms,
                retries=retries,
            )

    @abstractmethod
    async def _run(self, input_data: dict[str, Any]) -> Any:
        """Subclasses implement actual logic here."""

    def _classify_error(self, exc: Exception) -> str:
        """Map an exception to one of the 5 canonical failure modes."""
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        if any(k in name or k in msg for k in ("connect", "timeout", "network", "http", "status")):
            return "runtime_error"
        if any(k in msg for k in ("sql", "syntax", "parse", "regex", "transform")):
            return "implementation_error"
        if any(k in msg for k in ("column", "table", "field", "schema", "not found")):
            return "data_selection_error"
        if any(k in msg for k in ("plan", "intent", "question")):
            return "plan_error"
        return "runtime_error"

    def input_schema(self) -> dict[str, Any]:
        """Return JSON-Schema-like description for observability."""
        return {"name": self.name, "description": self.description, "category": self.category}


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
class ToolRegistry:
    """Maps tool names to BaseTool instances."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"tool '{name}' not registered; available: {sorted(self._tools)}")
        return self._tools[name]

    def list_tools(self) -> list[dict[str, Any]]:
        return [t.input_schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return sorted(self._tools)


# Process-wide registry
registry = ToolRegistry()
