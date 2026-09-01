"""N8nTool — fire n8n workflow webhooks with structured payloads.

This tool provides a clean API boundary between the agent and n8n.
The agent invokes n8n by name; the tool resolves the webhook URL, posts
the structured payload, and returns a structured result.

n8n webhook mapping is configured via the ``N8N_WORKFLOWS`` env var:

    N8N_WORKFLOWS='{"send_email": "https://n8n.example/webhook/abc",
                    "create_jira": "https://n8n.example/webhook/def"}'

If only ``N8N_WEBHOOK_URL`` is set (single webhook, existing behaviour),
all workflow invocations go to that URL.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from config import get_settings
from tools.base import BaseTool, ToolCategory, registry

logger = structlog.get_logger(__name__)


def _load_workflow_map() -> dict[str, str]:
    """Load workflow name → webhook URL mapping from settings."""
    settings = get_settings()
    raw = getattr(settings, "n8n_workflows", "") or ""
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("n8n_workflows_invalid_json")
    # Fallback: single webhook URL accepts all workflow calls
    if settings.n8n_webhook_url:
        return {"default": settings.n8n_webhook_url}
    return {}


class N8nTool(BaseTool):
    """Invoke an n8n workflow with a structured payload and receive a structured result."""

    name = "invoke_n8n"
    description = (
        "Invoke an n8n workflow to perform an external action such as sending an email, "
        "posting to Slack, creating a Jira ticket, saving to Google Drive, "
        "or triggering a scheduled report. "
        "Input: workflow_name, payload (dict). "
        "Output: success bool, response (dict), workflow_name."
    )
    category = ToolCategory.N8N

    async def _run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        workflow = str(input_data.get("workflow_name", "default"))
        payload = dict(input_data.get("payload") or {})
        payload.setdefault("workflow_name", workflow)

        workflow_map = _load_workflow_map()
        url = workflow_map.get(workflow) or workflow_map.get("default")
        if not url:
            raise RuntimeError(
                f"n8n workflow '{workflow}' not configured. "
                "Set N8N_WEBHOOK_URL or N8N_WORKFLOWS env var."
            )

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            ok = resp.status_code < 400
            try:
                response_body = resp.json()
            except Exception:
                response_body = {"raw": resp.text[:500]}

        logger.info(
            "n8n_invoked",
            workflow=workflow,
            status_code=resp.status_code,
            ok=ok,
        )
        if not ok:
            raise RuntimeError(
                f"n8n workflow '{workflow}' returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return {
            "success": ok,
            "workflow_name": workflow,
            "response": response_body,
            "status_code": resp.status_code,
        }


registry.register(N8nTool())
