"""Embedded DuckDB client — analytical (read-only) queries only.

DuckDB is imported as a library (``import duckdb``) and connects to a local
FILE, never a service. Raw SQL is permitted here and nowhere else; a
conservative statement guard rejects anything that is not a read-only query
before it reaches the engine. Calls are serialized behind an asyncio lock and
executed in a worker thread because DuckDB is synchronous.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import structlog

from config import get_settings

logger = structlog.get_logger(__name__)

_READ_ONLY_STARTERS = {"select", "with", "show", "describe", "explain", "pragma_show"}
_FORBIDDEN_KEYWORDS = {
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
    "checkpoint",
    "vacuum",
    "begin",
    "commit",
    "rollback",
    "prepare",
    "execute",
}
_WORD_RE = re.compile(r"[A-Za-z_]+")
_QUOTED_RE = re.compile(r"'(?:[^']|'')*'")


class DuckDBError(ValueError):
    """Raised when a query violates the read-only analytics contract."""


def assert_readonly_sql(sql: str) -> None:
    """Defense-in-depth guard: only single read-only statements pass."""
    stripped = _QUOTED_RE.sub("''", sql).strip().rstrip(";").strip()
    if not stripped:
        raise DuckDBError("empty SQL statement")
    words = [w.lower() for w in _WORD_RE.findall(stripped)]
    if not words or words[0] not in _READ_ONLY_STARTERS:
        raise DuckDBError(f"statement must start with one of {sorted(_READ_ONLY_STARTERS)}")
    forbidden = sorted(_FORBIDDEN_KEYWORDS.intersection(words))
    if forbidden:
        raise DuckDBError(f"forbidden keyword(s) in analytical query: {forbidden}")
    if ";" in stripped:
        raise DuckDBError("multiple statements are not allowed")


def _jsonify(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


class DuckDBClient:
    """In-process DuckDB connection for analytical queries."""

    def __init__(self, path: Path, max_rows: int = 200) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.max_rows = max_rows
        self._conn = duckdb.connect(str(path))
        self._lock = asyncio.Lock()
        logger.info("duckdb_ready", path=str(path))

    # ---------------------------------------------------------------- query
    def _query_sync(self, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        assert_readonly_sql(sql)
        cursor = self._conn.execute(sql, params or [])
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(self.max_rows)
        return {
            "columns": columns,
            "rows": [{c: _jsonify(v) for c, v in zip(columns, row, strict=False)} for row in rows],
            "row_count": len(rows),
            "truncated": len(rows) >= self.max_rows,
        }

    async def query(self, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        """Execute a read-only analytical query; returns columns + rows."""
        async with self._lock:
            return await asyncio.to_thread(self._query_sync, sql, params)

    # ---------------------------------------------------------------- schema
    def _exec_script_sync(self, script: str) -> None:
        for statement in (s.strip() for s in script.split(";")):
            if statement:
                self._conn.execute(statement)

    async def exec_script(self, script: str) -> None:
        """Run a DDL script (trusted, ships with the context directory)."""
        async with self._lock:
            await asyncio.to_thread(self._exec_script_sync, script)

    def _seed_demo_data_sync(self, ddl: str) -> None:
        """Idempotent: apply DDL, then insert deterministic demo rows if empty."""
        self._exec_script_sync(ddl)
        if self._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            rng = random.Random(42)
            regions = ["emea", "na", "apac", "latam"]
            statuses = ["paid", "shipped", "refunded"]
            today = date(2026, 8, 30)
            rows = []
            order_id = 1
            for day_offset in range(89, -1, -1):
                day = today - timedelta(days=day_offset)
                for _ in range(rng.randint(1, 4)):
                    rows.append(
                        (
                            order_id,
                            rng.randint(1, 50),
                            rng.choice(regions),
                            rng.choices(statuses, weights=[70, 20, 10])[0],
                            round(rng.uniform(20.0, 500.0), 2),
                            datetime(
                                day.year, day.month, day.day, rng.randint(8, 20), rng.randint(0, 59)
                            ),
                        )
                    )
                    order_id += 1
            self._conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", rows)
        if self._conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == 0:
            rng = random.Random(7)
            today = date(2026, 8, 30)
            rows = []
            for day_offset in range(89, -1, -1):
                day = today - timedelta(days=day_offset)
                rows.append((day, "cpu_percent", round(rng.uniform(40.0, 95.0), 2)))
                rows.append((day, "error_rate", round(rng.uniform(0.001, 0.08), 4)))
                rows.append((day, "latency_p95_ms", round(rng.uniform(200.0, 1500.0), 1)))
                rows.append((day, "queue_depth", rng.randint(50, 1500)))
            self._conn.executemany("INSERT INTO daily_metrics VALUES (?, ?, ?)", rows)
        logger.info("duckdb_seeded_demo_data")

    async def seed_from_context(self, context_dir: Path) -> None:
        """Apply context/schemas/analytics_warehouse.sql + deterministic demo data."""
        ddl_path = context_dir / "schemas" / "analytics_warehouse.sql"
        ddl = ddl_path.read_text()
        async with self._lock:
            await asyncio.to_thread(self._seed_demo_data_sync, ddl)

    def close(self) -> None:
        self._conn.close()


_default_client: DuckDBClient | None = None


def get_duckdb_client() -> DuckDBClient:
    """Process-wide DuckDB client singleton (file-backed, in-process)."""
    global _default_client
    if _default_client is None:
        s = get_settings()
        _default_client = DuckDBClient(s.duckdb_file, s.duckdb_max_rows)
    return _default_client
