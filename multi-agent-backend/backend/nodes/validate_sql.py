"""LangGraph node: validate generated SQL against the analytics guardrail rules."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

# Fallback guardrails used when the context rules are unavailable.
DEFAULT_SQL_RULES: dict[str, Any] = {
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
    ],
    "require_single_statement": True,
    "max_query_length": 5000,
}

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_QUOTED_RE = re.compile(r"'(?:[^']|'')*'")
_WORD_RE = re.compile(r"[A-Za-z_]+")


class ValidateSQLInput(BaseModel):
    """Input state keys read by validate_sql."""

    generated_sql: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    """Output state keys written by validate_sql."""

    sql_valid: bool = False
    sql_validation_errors: list[str] = Field(default_factory=list)


def validate_sql(sql: str, rules: dict[str, Any] | None) -> ValidationResult:
    """Pure rule-based SQL validation (unit-testable, deterministic).

    Checks: non-empty, single statement, allowed opening keyword, forbidden
    keywords (outside quoted strings), balanced parentheses, length limit.
    """
    rules = {**DEFAULT_SQL_RULES, **(rules or {})}
    errors: list[str] = []

    cleaned = _QUOTED_RE.sub("''", _BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub(" ", sql)))
    stripped = cleaned.strip()

    if not sql.strip():
        return ValidationResult(sql_valid=False, sql_validation_errors=["generated SQL is empty"])

    first_word = _WORD_RE.search(stripped)
    starters = [s.lower() for s in rules.get("allowed_start_statements", ["select", "with"])]
    if not first_word or first_word.group(0).lower() not in starters:
        errors.append(f"statement must start with {' or '.join(starters).upper()}")

    words = {w.lower() for w in _WORD_RE.findall(stripped)}
    forbidden = sorted(words.intersection({k.lower() for k in rules.get("forbidden_keywords", [])}))
    if forbidden:
        errors.append(f"forbidden keyword(s): {', '.join(forbidden)}")

    body = sql.strip()
    if body.endswith(";"):
        body = body[:-1]
    if rules.get("require_single_statement", True) and ";" in body:
        errors.append("multiple statements are not allowed")

    if body.count("(") != body.count(")"):
        errors.append("unbalanced parentheses")

    max_len = int(rules.get("max_query_length", 5000))
    if len(sql) > max_len:
        errors.append(f"query exceeds max length of {max_len} characters")

    return ValidationResult(sql_valid=not errors, sql_validation_errors=errors)


async def validate_sql_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Validate the generated SQL (registered in the graph as 'validate_sql').

    Input state keys:
        generated_sql: SQL produced by generate_sql.
        context: ContextBundle with rules['analytics'] guardrails.

    Output state keys:
        sql_valid: True when every check passed.
        sql_validation_errors: human-readable list of violations.

    Side-effect guarantees:
        None — pure computation, no I/O at all.
    """
    inp = ValidateSQLInput.model_validate(state)
    rules = (inp.context.get("rules") or {}).get("analytics") or DEFAULT_SQL_RULES
    result = validate_sql(inp.generated_sql, rules)
    return result.model_dump()
