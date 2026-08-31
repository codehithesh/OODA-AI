"""LangGraph node: classify detected signals (LLM-enriched, rule-anchored)."""

from __future__ import annotations

import json
import re
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from clients.litellm_client import get_litellm_client
from clients.prompt_loader import get_prompt_loader

logger = structlog.get_logger(__name__)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_VALID_SEVERITIES = {"low", "medium", "high", "critical"}
_VALID_STANCES = {"support", "neutral", "oppose"}


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from LLM output; None when unparseable."""
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


class ClassifySignalInput(BaseModel):
    """Input state keys read by classify_signal."""

    signal: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class SignalClassification(BaseModel):
    """Structured classification of a signal."""

    kind: str = "unclassified"
    severity: str = "medium"
    summary: str = ""
    confidence: float = 0.3
    evidence: list[str] = Field(default_factory=list)


_DEFAULT_MONITOR_RULES: dict[str, Any] = {
    "taxonomy": ["unclassified"],
    "severities": ["low", "medium", "high", "critical"],
}


async def classify_signal(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Classify a detected signal with taxonomy + severity.

    When detection already fixed the kind/severity (rule match) those values are
    kept so downstream decisions stay deterministic; the LLM supplies the
    summary, confidence, and evidence. Unstructured events are fully
    classified by the LLM.

    Input state keys:
        signal: SignalDraft from detect_signal.
        context: ContextBundle with rules['monitor'] and prompts
                 ['classify_signal'].

    Output state keys:
        classification: SignalClassification dict.
        usage: appended LLM usage record.

    Side-effect guarantees:
        One LLM call through the LiteLLM proxy. No database writes.
    """
    inp = ClassifySignalInput.model_validate(state)
    signal = inp.signal or {}
    raw_rules = (inp.context.get("rules") or {}).get("monitor", {})
    rules = {**_DEFAULT_MONITOR_RULES, **raw_rules}
    template = (inp.context.get("prompts") or {}).get(
        "classify_signal", "monitor/classify_signal.md"
    )

    loader = get_prompt_loader(dict(config) if config else None)
    llm = get_litellm_client(dict(config) if config else None)

    prompt = loader.render(template, signal=signal, rules=rules)
    response = await llm.chat([{"role": "user", "content": prompt}])

    parsed = parse_json_object(response.content) or {}
    classification = SignalClassification(
        kind=str(parsed.get("kind", "unclassified"))[:64],
        severity=str(parsed.get("severity", "medium")).lower(),
        summary=str(parsed.get("summary", ""))[:500],
        confidence=min(1.0, max(0.0, float(parsed.get("confidence", 0.3)))),
        evidence=[str(e)[:200] for e in (parsed.get("evidence") or [])[:5]],
    )
    if classification.severity not in _VALID_SEVERITIES:
        classification.severity = "medium"

    # Rule-anchored override: detection output wins when it is authoritative.
    if signal.get("kind") and signal["kind"] != "unclassified":
        classification.kind = str(signal["kind"])[:64]
        classification.severity = str(signal.get("severity", classification.severity)).lower()
    if not classification.summary:
        classification.summary = signal.get("description") or signal.get("matched_rule") or "signal"

    logger.info(
        "signal_classified",
        kind=classification.kind,
        severity=classification.severity,
        confidence=classification.confidence,
    )
    return {"classification": classification.model_dump(), "usage": [response.usage_record()]}


def normalize_stance(stance: Any) -> str:
    """Normalize an LLM-produced stance to support/neutral/oppose."""
    value = str(stance or "neutral").lower()
    if value not in _VALID_STANCES:
        return "neutral"
    return value
