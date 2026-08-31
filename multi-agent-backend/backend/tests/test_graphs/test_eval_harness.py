"""End-to-end evaluation harness test with a scripted LLM.

The FakeLLM returns exactly the expected SQL (fenced) for each analytics case
and a perfect judge verdict, so the whole suite must pass with score 1.0.
"""

from __future__ import annotations

from pathlib import Path

from eval_harness import EvalRunner, EvalSuite, normalize_sql
from tests.conftest import FakeLLM

SUITE_PATH = Path("context/evaluations/analytics_suite.yaml")


def test_normalize_sql() -> None:
    assert normalize_sql("SELECT  1;") == normalize_sql("select 1")
    assert normalize_sql("`SELECT` 1") == "select 1"


def test_suite_yaml_parses() -> None:
    suite = EvalSuite.from_yaml(SUITE_PATH)
    assert suite.mode == "analytics"
    assert suite.scorer == "exact_sql"
    assert len(suite.cases) == 4
    assert suite.cases[1].scorer == "execution_match"
    assert suite.cases[3].scorer == "llm_judge"


async def test_run_suite_all_pass() -> None:
    responses = [
        # total_revenue (exact_sql)
        "```sql\nSELECT SUM(amount) AS total_revenue FROM orders\n```",
        # orders_by_region (execution_match)
        "```sql\nSELECT region, COUNT(*) AS order_count FROM orders GROUP BY region\n```",
        # refunded_orders_recent (execution_match)
        (
            "```sql\nSELECT order_id, customer_id, region, amount "
            "FROM orders WHERE status = 'refunded'\n```"
        ),
        # average_order_value (llm_judge: generation + judge)
        "```sql\nSELECT AVG(amount) AS avg_order_value FROM orders\n```",
        '{"score": 1.0, "reason": "perfect"}',
    ]
    runner = EvalRunner(litellm=FakeLLM(responses=responses))
    report = await runner.run_suite_file(SUITE_PATH, db=None)
    assert report.total == 4
    assert report.passed == 4
    assert report.pass_rate == 1.0
    assert report.mean_score == 1.0


async def test_run_suite_detects_failure() -> None:
    responses = [
        "```sql\nSELECT wrong_column FROM orders\n```",  # exact_sql mismatch
        "```sql\nSELECT region, COUNT(*) AS order_count FROM orders GROUP BY region\n```",
        (
            "```sql\nSELECT order_id, customer_id, region, amount "
            "FROM orders WHERE status = 'refunded'\n```"
        ),
        "```sql\nSELECT AVG(amount) AS avg_order_value FROM orders\n```",
        '{"score": 1.0, "reason": "perfect"}',
    ]
    runner = EvalRunner(litellm=FakeLLM(responses=responses))
    report = await runner.run_suite_file(SUITE_PATH, db=None)
    assert report.passed == 3
    failed = [r for r in report.results if not r.passed]
    assert failed[0].id == "total_revenue"
    assert failed[0].score == 0.0
