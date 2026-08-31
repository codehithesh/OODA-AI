"""LangGraph node: generate DuckDB SQL from a natural-language analytics question."""

from __future__ import annotations

import re
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from clients.litellm_client import get_litellm_client
from clients.prompt_loader import get_prompt_loader

logger = structlog.get_logger(__name__)

_DEFAULT_ANALYTICS_RULES: dict[str, Any] = {
    "dialect": "duckdb",
    "allowed_start_statements": ["select", "with"],
    "forbidden_keywords": [
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "truncate",
        "grant",
        "revoke",
        "copy",
        "attach",
        "detach",
        "export",
        "import",
        "pragma",
        "call",
        "vacuum",
        "checkpoint",
        "begin",
        "commit",
        "rollback",
    ],
    "max_rows": 200,
    "notes": [
        "Use date_trunc('day', ordered_at) for daily buckets and date_trunc('week', ...) / ('month', ...) for coarser ones.",
        "amount is DECIMAL — cast with CAST(x AS DOUBLE) when averaging mixed metrics.",
        "Refunded orders have status = 'refunded'.",
    ],
}

_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class GenerateSQLInput(BaseModel):
    """Input state keys read by generate_sql."""

    query: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class GenerateSQLOutput(BaseModel):
    """Output state keys written by generate_sql."""

    generated_sql: str = ""
    sql_rationale: str = ""
    usage: list[dict[str, Any]] = Field(default_factory=list)


def extract_sql_block(text: str) -> str:
    """Return the first ```sql fenced block, or the whole text stripped."""
    match = _SQL_FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


async def generate_sql(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Generate a read-only DuckSQL query for the user's question.

    Input state keys:
        query: natural-language analytics question.
        context: ContextBundle with rules['analytics'], schemas
                 ['analytics_warehouse'] (DDL) and prompts['generate_sql'].

    Output state keys:
        generated_sql: the SQL statement extracted from the LLM response.
        sql_rationale: surrounding prose from the model (truncated).
        usage: appended LLM usage record (accumulated via state reducer).

    Side-effect guarantees:
        One LLM call through the LiteLLM proxy. No database writes, no
        filesystem mutation.
    """
    inp = GenerateSQLInput.model_validate(state)
    context = inp.context or {}
    rules = {**_DEFAULT_ANALYTICS_RULES, **((context.get("rules") or {}).get("analytics", {}))}
    ddl = (context.get("schemas") or {}).get("analytics_warehouse", "")
    template = (context.get("prompts") or {}).get("generate_sql", "analytics/generate_sql.md")

    loader = get_prompt_loader(dict(config) if config else None)
    llm = get_litellm_client(dict(config) if config else None)

    prompt = loader.render(template, query=inp.query, schema_ddl=ddl, rules=rules)
    response = await llm.chat([{"role": "user", "content": prompt}])

    sql = extract_sql_block(response.content)
    rationale = response.content.replace(sql, "").strip()[:500]
    logger.info("sql_generated", sql_length=len(sql), model=response.model)
    return GenerateSQLOutput(
        generated_sql=sql,
        sql_rationale=rationale,
        usage=[response.usage_record()],
    ).model_dump()
