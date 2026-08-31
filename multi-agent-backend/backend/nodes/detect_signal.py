"""LangGraph node: detect monitor signals from incoming events (rule-based)."""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class DetectSignalInput(BaseModel):
    """Input state keys read by detect_signal."""

    event: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class SignalDraft(BaseModel):
    """Structured signal draft produced by detection."""

    kind: str = "unclassified"
    severity: str = "medium"
    source: str = "unknown"
    metric: str | None = None
    value: float | None = None
    matched_rule: str | None = None
    description: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def detect_signal(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Detect whether an event breaches any monitoring rule.

    Input state keys:
        event: the raw event payload ({metric, value, source} or {description}).
        context: ContextBundle with rules['monitor'] detection thresholds.

    Output state keys:
        signal_detected: True when a rule matched or the event needs triage.
        signal: SignalDraft dict (kind, severity, source, matched rule) or None.

    Side-effect guarantees:
        None — pure computation, no I/O at all.
    """
    inp = DetectSignalInput.model_validate(state)
    rules = (inp.context.get("rules") or {}).get("monitor", {})
    event = inp.event or {}

    source = str(event.get("source") or "unknown")
    metric = event.get("metric")
    value = _as_float(event.get("value"))

    for rule in rules.get("detection", []):
        if rule.get("metric") != metric or value is None:
            continue
        threshold = _as_float(rule.get("threshold"))
        if threshold is None:
            continue
        op = rule.get("op", "gt")
        breached = value > threshold if op == "gt" else value < threshold
        if breached:
            draft = SignalDraft(
                kind=str(rule.get("kind", "unclassified")),
                severity=str(rule.get("severity", "medium")),
                source=source,
                metric=str(metric),
                value=value,
                matched_rule=f"{metric} {op} {threshold}",
                payload=event,
            )
            logger.info("signal_detected", kind=draft.kind, severity=draft.severity)
            return {"signal_detected": True, "signal": draft.model_dump()}

    if metric is None and event.get("description"):
        default = rules.get("unstructured_default", {"kind": "unclassified", "severity": "medium"})
        draft = SignalDraft(
            kind=str(default.get("kind", "unclassified")),
            severity=str(default.get("severity", "medium")),
            source=source,
            description=str(event["description"]),
            payload=event,
        )
        return {"signal_detected": True, "signal": draft.model_dump()}

    # A structured metric that matched no rule is noise.
    logger.info("signal_not_detected", metric=metric, value=value)
    return {"signal_detected": False, "signal": None}
