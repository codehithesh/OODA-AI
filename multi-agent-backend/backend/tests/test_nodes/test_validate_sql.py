"""Pure unit tests for analytics + monitor nodes (no fakes needed)."""

from __future__ import annotations

from nodes.validate_sql import DEFAULT_SQL_RULES, validate_sql


def test_valid_select_passes() -> None:
    result = validate_sql("SELECT SUM(amount) FROM orders GROUP BY region", None)
    assert result.sql_valid is True
    assert result.sql_validation_errors == []


def test_with_cte_passes() -> None:
    result = validate_sql("WITH t AS (SELECT 1 AS x) SELECT x FROM t", None)
    assert result.sql_valid is True


def test_delete_rejected() -> None:
    result = validate_sql("DELETE FROM orders", None)
    assert result.sql_valid is False
    assert any("forbidden" in e for e in result.sql_validation_errors)


def test_insert_rejected() -> None:
    result = validate_sql("INSERT INTO orders VALUES (1, 2, 'na', 'paid', 10.0, NOW())", None)
    assert result.sql_valid is False


def test_drop_disguised_as_subquery_rejected() -> None:
    result = validate_sql("SELECT * FROM (DROP TABLE orders)", None)
    assert result.sql_valid is False


def test_multiple_statements_rejected() -> None:
    result = validate_sql("SELECT 1; SELECT 2", None)
    assert result.sql_valid is False


def test_trailing_semicolon_allowed() -> None:
    result = validate_sql("SELECT 1;", None)
    assert result.sql_valid is True


def test_unbalanced_parens_rejected() -> None:
    result = validate_sql("SELECT COUNT(* FROM orders", None)
    assert result.sql_valid is False


def test_empty_sql_rejected() -> None:
    result = validate_sql("   ", None)
    assert result.sql_valid is False


def test_forbidden_keyword_inside_string_literal_is_allowed() -> None:
    # 'we create value' inside quotes is data, not DDL
    result = validate_sql("SELECT 'we create value' AS note FROM orders", DEFAULT_SQL_RULES)
    assert result.sql_valid is True


def test_comments_are_stripped() -> None:
    result = validate_sql("-- delete everything\nSELECT 1", None)
    assert result.sql_valid is True


def test_update_rejected() -> None:
    result = validate_sql("UPDATE orders SET status = 'paid'", None)
    assert result.sql_valid is False


def test_custom_rules_from_context() -> None:
    rules = {
        "allowed_start_statements": ["select"],
        "forbidden_keywords": ["insert"],
        "require_single_statement": True,
    }
    assert validate_sql("WITH x AS (SELECT 1) SELECT * FROM x", rules).sql_valid is False
    assert validate_sql("SELECT 1", rules).sql_valid is True
