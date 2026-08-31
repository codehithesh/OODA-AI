"""Unit tests for decide_action (deterministic action matrix)."""

from __future__ import annotations

from nodes.decide_action import decide_action

RULES = {
    "monitor": {
        "action_matrix": [
            {
                "severities": ["critical"],
                "action": "require_approval",
                "plan": {"type": "page_oncall", "channel": "#incidents"},
            },
            {
                "kinds": ["data_drift"],
                "action": "require_approval",
                "plan": {"type": "notify", "channel": "#data-quality"},
            },
            {
                "severities": ["medium"],
                "action": "auto_act",
                "plan": {"type": "notify", "channel": "#ops-bots"},
            },
            {"severities": ["low"], "action": "ignore", "plan": {"type": "log_only"}},
        ],
        "default_action": {
            "action": "require_approval",
            "plan": {"type": "notify", "channel": "#ops-alerts"},
        },
    }
}


async def test_critical_requires_approval() -> None:
    result = await decide_action(
        {
            "classification": {"kind": "error_burst", "severity": "critical"},
            "context": {"rules": RULES},
        },
        {},
    )
    assert result["action"] == "require_approval"
    assert result["action_plan"]["channel"] == "#incidents"


async def test_medium_auto_acts() -> None:
    result = await decide_action(
        {
            "classification": {"kind": "backlog_growth", "severity": "medium"},
            "context": {"rules": RULES},
        },
        {},
    )
    assert result["action"] == "auto_act"


async def test_low_ignored() -> None:
    result = await decide_action(
        {"classification": {"kind": "capacity", "severity": "low"}, "context": {"rules": RULES}}, {}
    )
    assert result["action"] == "ignore"


async def test_kind_override_wins_when_severity_unmatched() -> None:
    result = await decide_action(
        {"classification": {"kind": "data_drift", "severity": "low"}, "context": {"rules": RULES}},
        {},
    )
    # data_drift entry has no severity filter -> matches before the low/ignore entry
    assert result["action"] == "require_approval"


async def test_fallthrough_default() -> None:
    result = await decide_action(
        {
            "classification": {"kind": "unclassified", "severity": "high"},
            "context": {"rules": RULES},
        },
        {},
    )
    assert result["action"] == "require_approval"
    assert result["action_plan"]["channel"] == "#ops-alerts"
    assert "matched" not in result["action_plan"]["rationale"]
