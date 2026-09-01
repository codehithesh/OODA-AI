"""Tools introspection and direct invocation routes.

GET  /v1/tools          — list all registered tools with their schemas
POST /v1/tools/{name}   — directly invoke a tool (for debugging/testing)
POST /v1/n8n/invoke     — directly invoke an n8n workflow
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# Import tool modules to trigger their registration
import tools.n8n_tool  # noqa: F401
import tools.visualization_tool  # noqa: F401
import tools.warehouse_tool  # noqa: F401
import tools.web_search_tool  # noqa: F401
from tools.base import registry

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tools", tags=["tools"])


class ToolInvocationRequest(BaseModel):
    input: dict[str, Any] = {}


class N8nInvocationRequest(BaseModel):
    workflow_name: str
    payload: dict[str, Any] = {}


@router.get("", summary="List all registered tools")
async def list_tools() -> dict[str, Any]:
    """Return all registered tools with their names, descriptions, and categories."""
    return {"tools": registry.list_tools(), "count": len(registry.names())}


@router.post("/{tool_name}", summary="Directly invoke a tool")
async def invoke_tool(tool_name: str, body: ToolInvocationRequest) -> dict[str, Any]:
    """Directly invoke a registered tool (useful for testing and debugging)."""
    try:
        tool = registry.get(tool_name)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"tool '{tool_name}' not found; available: {registry.names()}",
        )
    result = await tool.run(body.input)
    return {
        "tool": tool_name,
        "status": result.status,
        "output": result.output,
        "error": result.error,
        "error_type": result.error_type,
        "latency_ms": result.latency_ms,
        "retries": result.retries,
    }


n8n_router = APIRouter(prefix="/n8n", tags=["n8n"])


@n8n_router.post("/invoke", summary="Invoke an n8n workflow directly")
async def invoke_n8n(body: N8nInvocationRequest) -> dict[str, Any]:
    """Invoke an n8n workflow with a structured payload."""
    from tools.n8n_tool import N8nTool
    tool = N8nTool()
    result = await tool.run({"workflow_name": body.workflow_name, "payload": body.payload})
    if not result.succeeded:
        raise HTTPException(status_code=502, detail=result.error)
    return result.output or {}
