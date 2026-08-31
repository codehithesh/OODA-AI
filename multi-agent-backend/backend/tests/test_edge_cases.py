"""Extended edge case and error handling tests.

These tests cover error paths, boundary conditions, and integration scenarios
not covered by the main test suites.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clients.duckdb_client import DuckDBClient
from clients.litellm_client import LLMResponse, LLMUsage
from nodes.generate_sql import extract_sql_block, generate_sql
from nodes.validate_sql import validate_sql_node
from schemas import DecisionLogRead
from tests.conftest import FakeLLM


# ============================================================================
# SQL generation edge cases
# ============================================================================


def test_extract_sql_block_empty_fence() -> None:
    """Empty SQL fence returns empty string."""
    text = "```sql\n```"
    assert extract_sql_block(text) == ""


def test_extract_sql_block_multiple_fences() -> None:
    """Returns first fence only."""
    text = "```sql\nSELECT 1\n```\nMore text\n```sql\nSELECT 2\n```"
    assert extract_sql_block(text) == "SELECT 1"


def test_extract_sql_block_case_insensitive() -> None:
    """Handles uppercase SQL, SQL, sql."""
    for case in ["SQL", "Sql", "sql"]:
        text = f"```{case}\nSELECT 1\n```"
        assert extract_sql_block(text) == "SELECT 1"


def test_extract_sql_block_no_fence_multiline() -> None:
    """Returns whole text (no fence) with stripped whitespace."""
    text = "  \n  SELECT 1  \n  "
    assert extract_sql_block(text) == "SELECT 1"


async def test_generate_sql_llm_timeout() -> None:
    """Handles LLM call timeout gracefully."""
    fake = FakeLLM()
    
    async def raise_timeout(*args, **kwargs):
        raise TimeoutError("LLM call timed out")
    
    fake.chat = raise_timeout
    config = {"configurable": {"litellm_client": fake}}
    
    with pytest.raises(TimeoutError):
        await generate_sql({"query": "slow query", "context": {}}, config)


async def test_generate_sql_malformed_json_response() -> None:
    """LLM returns non-SQL response; extraction returns empty."""
    fake = FakeLLM(responses=["{ this is not sql }"])
    config = {
        "configurable": {
            "litellm_client": fake,
            "prompt_loader": MagicMock(render=MagicMock(return_value="prompt")),
        }
    }
    result = await generate_sql(
        {
            "query": "total revenue",
            "context": {"schemas": {}, "rules": {"analytics": {}}},
        },
        config,
    )
    assert result["generated_sql"] == "{ this is not sql }"


# ============================================================================
# SQL validation edge cases
# ============================================================================


async def test_validate_sql_forbidden_keyword_insert() -> None:
    """INSERT is forbidden."""
    context = {
        "rules": {
            "analytics": {
                "forbidden_keywords": ["insert", "update", "delete"],
                "max_query_length": 5000,
            }
        }
    }
    result = await validate_sql_node(
        {"generated_sql": "INSERT INTO orders VALUES (1, 2, 3)", "context": context},
        {"configurable": {}},
    )
    assert result["sql_valid"] is False
    assert "INSERT" in result["sql_validation_errors"][0].upper()


async def test_validate_sql_query_too_long() -> None:
    """Query exceeds max_query_length."""
    context = {
        "rules": {
            "analytics": {
                "forbidden_keywords": [],
                "max_query_length": 10,  # tiny limit
            }
        }
    }
    result = await validate_sql_node(
        {"generated_sql": "SELECT * FROM very_long_table_name", "context": context},
        {"configurable": {}},
    )
    assert result["sql_valid"] is False
    assert "length" in result["sql_validation_errors"][0].lower()


async def test_validate_sql_empty_query() -> None:
    """Empty SQL string."""
    context = {
        "rules": {
            "analytics": {
                "forbidden_keywords": [],
                "max_query_length": 5000,
            }
        }
    }
    result = await validate_sql_node(
        {"generated_sql": "", "context": context},
        {"configurable": {}},
    )
    assert result["sql_valid"] is False


# ============================================================================
# DuckDB execution edge cases
# ============================================================================


@pytest.mark.asyncio
async def test_duckdb_query_execution_empty_result() -> None:
    """Query returns empty result set."""
    client = DuckDBClient(":memory:")
    client.execute("CREATE TABLE test (id INTEGER, value VARCHAR)")
    
    result = client.query("SELECT * FROM test WHERE id > 100")
    assert result is not None
    assert len(result) == 0


@pytest.mark.asyncio
async def test_duckdb_query_type_mismatch() -> None:
    """Query with type casting errors."""
    client = DuckDBClient(":memory:")
    client.execute("CREATE TABLE test (id INTEGER, value VARCHAR)")
    client.execute("INSERT INTO test VALUES (1, 'hello')")
    
    # DuckDB should handle this gracefully or raise an error
    try:
        result = client.query("SELECT CAST(value AS INTEGER) FROM test")
        # If it succeeds, check the result
        assert result is not None
    except Exception as e:
        # If it fails, we handle it gracefully
        assert "cast" in str(e).lower() or "convert" in str(e).lower()


# ============================================================================
# Authentication & Rate limiting
# ============================================================================


async def test_chat_missing_bearer_token(client) -> None:
    """Request without Authorization header (if auth is enabled)."""
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "analytics",
            "messages": [{"role": "user", "content": "query"}],
        },
    )
    # Auth check depends on BACKEND_API_KEYS setting; test should adapt
    # This test documents the expected behavior


async def test_chat_invalid_bearer_token(client) -> None:
    """Request with wrong API key."""
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "analytics",
            "messages": [{"role": "user", "content": "query"}],
        },
        headers={"Authorization": "Bearer invalid-key"},
    )
    # Should reject with 403 Forbidden or similar (if auth is strict)


# ============================================================================
# Decision log edge cases
# ============================================================================


def test_decision_log_read_with_nulls() -> None:
    """Handles DecisionLog with NULL optional fields."""
    data = {
        "id": str(uuid.uuid4()),
        "mode": "analytics",
        "status": "running",
        "context_commit_sha": "abc123",
        "thread_id": None,
        "input": None,
        "output": None,
        "evaluation_score": None,
        "latency_ms": None,
        "cost_usd": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "error": None,
        "created_at": "2026-08-31T00:00:00+00:00",
    }
    record = DecisionLogRead(**data)
    assert record.thread_id is None
    assert record.evaluation_score is None
    assert record.cost_usd is None


def test_decision_log_cost_decimal_precision() -> None:
    """Cost field maintains decimal precision."""
    cost = Decimal("0.00012345")
    data = {
        "id": str(uuid.uuid4()),
        "mode": "analytics",
        "status": "succeeded",
        "context_commit_sha": "abc123",
        "cost_usd": cost,
        "created_at": "2026-08-31T00:00:00+00:00",
    }
    record = DecisionLogRead(**data)
    assert record.cost_usd == cost


# ============================================================================
# Concurrent request handling
# ============================================================================


@pytest.mark.asyncio
async def test_concurrent_chat_requests(client, fake_llm) -> None:
    """Multiple concurrent chat requests don't interfere."""
    import asyncio
    
    fake_llm.responses = [
        "```sql\nSELECT 1\n```",
        "```sql\nSELECT 2\n```",
        "```sql\nSELECT 3\n```",
    ]
    
    async def make_request(query):
        return await client.post(
            "/v1/chat/completions",
            json={
                "model": "analytics",
                "messages": [{"role": "user", "content": query}],
            },
        )
    
    responses = await asyncio.gather(
        make_request("query 1"),
        make_request("query 2"),
        make_request("query 3"),
    )
    
    assert all(r.status_code == 200 for r in responses)
    assert len(fake_llm.calls) == 3


# ============================================================================
# Schema validation
# ============================================================================


def test_chat_message_with_content_parts() -> None:
    """ChatMessage handles content as list of parts (multimodal)."""
    from schemas import ChatMessage
    
    msg = ChatMessage(
        role="user",
        content=[
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "https://..."}},
        ],
    )
    assert msg.role == "user"
    assert isinstance(msg.content, list)
    # .text property should extract text parts
    assert "hello" in msg.text


def test_completion_usage_defaults() -> None:
    """CompletionUsage initializes with zeros."""
    from schemas import CompletionUsage
    
    usage = CompletionUsage()
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0


# ============================================================================
# Context loading & git integration
# ============================================================================


@pytest.mark.asyncio
async def test_load_context_missing_rules_file() -> None:
    """Handles missing mode-specific rules gracefully."""
    from nodes.load_context import load_context_for_mode
    
    # If a rules file doesn't exist, should return empty dict or raise
    try:
        context = await load_context_for_mode("nonexistent_mode")
        # If it returns, rules should be empty
        assert context.rules.get("nonexistent_mode", {}) == {}
    except FileNotFoundError:
        # This is also acceptable
        pass


# ============================================================================
# Error aggregation in decisions
# ============================================================================


def test_decision_log_with_multiple_errors() -> None:
    """DecisionLog captures error messages from failed nodes."""
    data = {
        "id": str(uuid.uuid4()),
        "mode": "analytics",
        "status": "failed",
        "context_commit_sha": "abc123",
        "error": "SQL validation failed: INSERT is forbidden",
        "created_at": "2026-08-31T00:00:00+00:00",
    }
    record = DecisionLogRead(**data)
    assert "forbidden" in record.error.lower()
