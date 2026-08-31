"""Unit tests for detect_signal (rule-based, deterministic)."""

from __future__ import annotations

from nodes.detect_signal import detect_signal
from tests.conftest import FakeLLM  # noqa: F401 - fixture wiring

RULES = {
    "monitor": {
        "detection": [
            {
                "metric": "error_rate",
                "op": "gt",
                "threshold": 0.05,
                "kind": "error_burst",
                "severity": "critical",
            },
            {
                "metric": "cpu_percent",
                "op": "gt",
                "threshold": 90,
                "kind": "resource_exhaustion",
                "severity": "medium",
            },
        ],
        "unstructured_default": {"kind": "unclassified", "severity": "medium"},
    }
}


async def test_rule_breach_detected() -> None:
    state = {
        "event": {"metric": "error_rate", "value": 0.31, "source": "payments"},
        "context": {"rules": RULES},
    }
    result = await detect_signal(state, {})
    assert result["signal_detected"] is True
    assert result["signal"]["kind"] == "error_burst"
    assert result["signal"]["severity"] == "critical"
    assert result["signal"]["source"] == "payments"


async def test_below_threshold_not_detected() -> None:
    state = {
        "event": {"metric": "error_rate", "value": 0.01, "source": "payments"},
        "context": {"rules": RULES},
    }
    result = await detect_signal(state, {})
    assert result["signal_detected"] is False
    assert result["signal"] is None


async def test_unstructured_description_detected() -> None:
    state = {
        "event": {"description": "503s from provider", "source": "payments"},
        "context": {"rules": RULES},
    }
    result = await detect_signal(state, {})
    assert result["signal_detected"] is True
    assert result["signal"]["kind"] == "unclassified"


async def test_lt_operator_detected() -> None:
    rules = {
        "monitor": {
            "detection": [
                {
                    "metric": "disk_free",
                    "op": "lt",
                    "threshold": 5,
                    "kind": "capacity",
                    "severity": "low",
                }
            ]
        }
    }
    state = {"event": {"metric": "disk_free", "value": 3}, "context": {"rules": rules}}
    result = await detect_signal(state, {})
    assert result["signal_detected"] is True
    assert result["signal"]["matched_rule"] == "disk_free lt 5.0"


async def test_unknown_metric_is_noise() -> None:
    state = {"event": {"metric": "mystery", "value": 42}, "context": {"rules": RULES}}
    result = await detect_signal(state, {})
    assert result["signal_detected"] is False
