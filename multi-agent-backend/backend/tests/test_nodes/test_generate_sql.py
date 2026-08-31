"""Unit tests for generate_sql (scripted LLM) and classify_signal."""

from __future__ import annotations

from clients.prompt_loader import get_default_prompt_loader
from nodes.classify_signal import classify_signal, parse_json_object
from nodes.generate_sql import extract_sql_block, generate_sql
from tests.conftest import FakeLLM

CONTEXT = {
    "rules": {
        "analytics": {
            "dialect": "duckdb",
            "allowed_start_statements": ["select", "with"],
            "forbidden_keywords": ["insert"],
            "max_rows": 200,
            "notes": [],
        }
    },
    "schemas": {
        "analytics_warehouse": "CREATE TABLE orders (order_id INTEGER, amount DECIMAL(12,2));"
    },
    "prompts": {"generate_sql": "analytics/generate_sql.md"},
}


def test_extract_sql_block_from_fence() -> None:
    text = "Here you go:\n```sql\nSELECT 1\n```\nDone."
    assert extract_sql_block(text) == "SELECT 1"


def test_extract_sql_block_plain_text() -> None:
    assert extract_sql_block("SELECT 1") == "SELECT 1"


def test_parse_json_object() -> None:
    assert parse_json_object('noise {"a": 1} noise') == {"a": 1}
    assert parse_json_object("not json") is None


async def test_generate_sql_uses_prompt_and_returns_sql() -> None:
    fake = FakeLLM(responses=["```sql\nSELECT SUM(amount) FROM orders\n```"])
    config = {
        "configurable": {"litellm_client": fake, "prompt_loader": get_default_prompt_loader()}
    }
    result = await generate_sql({"query": "total revenue?", "context": CONTEXT}, config)
    assert result["generated_sql"] == "SELECT SUM(amount) FROM orders"
    assert result["usage"][0]["total_tokens"] == 30
    # the rendered prompt must contain the DDL and the question
    rendered = " ".join(m["content"] for m in fake.calls[0])
    assert "CREATE TABLE orders" in rendered
    assert "total revenue?" in rendered


async def test_classify_signal_rule_anchored() -> None:
    fake = FakeLLM(
        responses=[
            (
                '{"kind": "wrong_kind", "severity": "low", "'
                'summary": "errors spiking", "confidence": 0.91, '
                '"evidence": ["p95"]}'
            )
        ]
    )
    config = {
        "configurable": {"litellm_client": fake, "prompt_loader": get_default_prompt_loader()}
    }
    signal = {
        "kind": "error_burst",
        "severity": "critical",
        "source": "payments",
        "metric": "error_rate",
        "value": 0.31,
    }
    context = {
        "rules": {
            "monitor": {
                "taxonomy": ["error_burst"],
                "severities": ["low", "medium", "high", "critical"],
            }
        },
        "prompts": {"classify_signal": "monitor/classify_signal.md"},
    }
    result = await classify_signal({"signal": signal, "context": context}, config)
    classification = result["classification"]
    # rule-derived kind/severity must override the (wrong) LLM guess
    assert classification["kind"] == "error_burst"
    assert classification["severity"] == "critical"
    assert classification["summary"] == "errors spiking"
    assert classification["confidence"] == 0.91


async def test_classify_signal_unstructured_llm_only() -> None:
    fake = FakeLLM(
        responses=[
            '{"kind": "latency_spike", "severity": "high", "summary": "slow", "confidence": 0.8}'
        ]
    )
    config = {
        "configurable": {"litellm_client": fake, "prompt_loader": get_default_prompt_loader()}
    }
    signal = {"kind": "unclassified", "severity": "medium", "description": "slow responses"}
    context = {
        "rules": {"monitor": {}},
        "prompts": {"classify_signal": "monitor/classify_signal.md"},
    }
    result = await classify_signal({"signal": signal, "context": context}, config)
    assert result["classification"]["kind"] == "latency_spike"
    assert result["classification"]["severity"] == "high"


async def test_classify_signal_tolerates_garbage() -> None:
    fake = FakeLLM(responses=["definitely not json"])
    config = {
        "configurable": {"litellm_client": fake, "prompt_loader": get_default_prompt_loader()}
    }
    signal = {"kind": "unclassified", "severity": "medium", "description": "something happened"}
    context = {
        "rules": {"monitor": {}},
        "prompts": {"classify_signal": "monitor/classify_signal.md"},
    }
    result = await classify_signal({"signal": signal, "context": context}, config)
    assert result["classification"]["kind"] == "unclassified"
    assert result["classification"]["summary"] == "something happened"
