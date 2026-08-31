"""LangGraph node: decide the action for a classified signal (deterministic)."""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class DecideActionInput(BaseModel):
    """Input state keys read by decide_action."""

    classification: dict[str, Any] = Field(default_factory=dict)
    signal: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class ActionPlan(BaseModel):
    """Action chosen for a classified signal."""

    action: str  # auto_act | require_approval | ignore
    type: str = "notify"
    channel: str = ""
    runbook: str = ""
    rationale: str = ""


async def decide_action(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Choose the action for a signal from the monitor action matrix.

    Input state keys:
        classification: SignalClassification (kind, severity).
        signal: SignalDraft (used only for the rationale line).
        context: ContextBundle with rules['monitor'] action_matrix.

    Output state keys:
        action: one of 'auto_act' | 'require_approval' | 'ignore'.
        action_plan: ActionPlan dict (type, channel, runbook, rationale).

    Side-effect guarantees:
        None — pure rule-matrix computation, no I/O at all (no LLM either,
        so decisions are cheap, explainable, and reproducible).
    """
    inp = DecideActionInput.model_validate(state)
    rules = (inp.context.get("rules") or {}).get("monitor", {})
    classification = inp.classification or {}
    kind = str(classification.get("kind", "unclassified"))
    severity = str(classification.get("severity", "medium")).lower()

    for entry in rules.get("action_matrix", []):
        kinds = entry.get("kinds")
        severities = entry.get("severities")
        if kinds and kind not in kinds:
            continue
        if severities and severity not in [str(s).lower() for s in severities]:
            continue
        if not kinds and not severities:
            continue
        plan = entry.get("plan") or {}
        action_plan = ActionPlan(
            action=str(entry.get("action", "require_approval")),
            type=str(plan.get("type", "notify")),
            channel=str(plan.get("channel", "")),
            runbook=str(plan.get("runbook", "")),
            rationale=f"kind='{kind}' severity='{severity}' matched action matrix entry",
        )
        logger.info("action_decided", action=action_plan.action, kind=kind, severity=severity)
        return {"action": action_plan.action, "action_plan": action_plan.model_dump()}

    default = rules.get(
        "default_action",
        {"action": "require_approval", "plan": {"type": "notify", "channel": "#ops-alerts"}},
    )
    plan = default.get("plan") or {}
    action_plan = ActionPlan(
        action=str(default.get("action", "require_approval")),
        type=str(plan.get("type", "notify")),
        channel=str(plan.get("channel", "")),
        runbook=str(plan.get("runbook", "")),
        rationale=f"kind='{kind}' severity='{severity}' fell through to default action",
    )
    return {"action": action_plan.action, "action_plan": action_plan.model_dump()}
