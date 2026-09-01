"""WarehouseTool — schema inspection and SQL execution against DuckDB.

Two tools are registered here:

* ``inspect_schema``  — return tables, columns, types from the warehouse
* ``execute_sql``     — execute a validated read-only SQL query and return rows

Both operate on the existing DuckDBClient singleton so they slot directly into
the existing analytics pipeline without duplicating connection logic.
"""

from __future__ import annotations

from typing import Any

from clients.duckdb_client import get_duckdb_client
from tools.base import BaseTool, ToolCategory, registry


class InspectSchemaTool(BaseTool):
    """Return the warehouse schema (tables, columns, types)."""

    name = "inspect_schema"
    description = (
        "Inspect the available warehouse tables and their columns. "
        "Use this before generating SQL to understand what data is available."
    )
    category = ToolCategory.WAREHOUSE

    async def _run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        db = get_duckdb_client()
        # SHOW TABLES is always available in DuckDB
        tables_result = await db.aquery("SHOW TABLES")
        schema: dict[str, Any] = {"tables": []}
        for row in tables_result.get("rows", []):
            table_name = row.get("name") or row.get("table_name") or list(row.values())[0]
            # DESCRIBE <table> gives column info
            try:
                cols_result = await db.aquery(f"DESCRIBE {table_name}")
                columns = [
                    {
                        "name": c.get("column_name") or c.get("name"),
                        "type": c.get("column_type") or c.get("type"),
                        "nullable": c.get("null", "YES"),
                    }
                    for c in cols_result.get("rows", [])
                ]
            except Exception:
                columns = []
            schema["tables"].append({"name": table_name, "columns": columns})
        return schema


class ExecuteSQLTool(BaseTool):
    """Execute a read-only SQL query against the warehouse and return rows."""

    name = "execute_sql"
    description = (
        "Execute a read-only SQL query against the analytics warehouse (DuckDB). "
        "Returns columns, rows, row_count, and whether the result was truncated. "
        "Never use this for write operations."
    )
    category = ToolCategory.WAREHOUSE

    async def _run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        sql = str(input_data.get("sql", "")).strip()
        if not sql:
            raise ValueError("sql is required")
        db = get_duckdb_client()
        return await db.aquery(sql)


class ProfileDataTool(BaseTool):
    """Profile a table: row count, null rates, distinct counts, min/max per column."""

    name = "profile_data"
    description = (
        "Profile a warehouse table: row counts, null rates, min/max values, "
        "distinct value counts, and sample rows. "
        "Use before EDA to understand data quality."
    )
    category = ToolCategory.PROFILING

    async def _run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        table = str(input_data.get("table", "")).strip()
        if not table:
            raise ValueError("table is required")
        db = get_duckdb_client()

        # Row count
        count_res = await db.aquery(f"SELECT COUNT(*) AS n FROM {table}")
        row_count = (count_res.get("rows") or [{}])[0].get("n", 0)

        # Column profiles using DESCRIBE
        desc_res = await db.aquery(f"DESCRIBE {table}")
        columns = [
            {
                "name": r.get("column_name") or r.get("name"),
                "type": r.get("column_type") or r.get("type"),
            }
            for r in desc_res.get("rows", [])
        ]

        profiles = []
        for col in columns:
            col_name = col["name"]
            col_type = str(col["type"]).lower()
            try:
                # Null rate
                null_res = await db.aquery(
                    f"SELECT COUNT(*) AS n_null FROM {table} "
                    f"WHERE {col_name} IS NULL"
                )
                n_null = (null_res.get("rows") or [{}])[0].get("n_null", 0)
                null_rate = round(n_null / max(row_count, 1), 4)

                # Distinct count
                dist_res = await db.aquery(
                    f"SELECT COUNT(DISTINCT {col_name}) AS n_dist FROM {table}"
                )
                n_dist = (dist_res.get("rows") or [{}])[0].get("n_dist", 0)

                profile: dict[str, Any] = {
                    "column": col_name,
                    "type": col["type"],
                    "null_rate": null_rate,
                    "distinct_count": n_dist,
                }

                # Min/max for numeric and date columns
                if any(t in col_type for t in ("int", "float", "double", "decimal", "numeric", "date", "timestamp")):
                    minmax_res = await db.aquery(
                        f"SELECT MIN({col_name}) AS min_val, MAX({col_name}) AS max_val "
                        f"FROM {table}"
                    )
                    row = (minmax_res.get("rows") or [{}])[0]
                    profile["min"] = row.get("min_val")
                    profile["max"] = row.get("max_val")

                profiles.append(profile)
            except Exception as exc:
                profiles.append({"column": col_name, "type": col["type"], "error": str(exc)})

        # Sample rows
        sample_res = await db.aquery(f"SELECT * FROM {table} LIMIT 5")

        return {
            "table": table,
            "row_count": row_count,
            "column_count": len(columns),
            "columns": profiles,
            "sample_rows": sample_res.get("rows", []),
        }


# Register all warehouse tools
registry.register(InspectSchemaTool())
registry.register(ExecuteSQLTool())
registry.register(ProfileDataTool())
