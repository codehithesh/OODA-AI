"""VisualizationTool — generate Plotly chart specs from query result data.

The agent selects chart type based on the analytical question and data shape.
This tool returns a Plotly JSON figure spec that the UI can render directly
(Open WebUI can embed Plotly via a custom HTML block or the frontend can
deserialize the spec).

Charts supported:
    time_series, bar, horizontal_bar, scatter, histogram, pie, funnel,
    heatmap, box, waterfall

The tool does NOT hardcode a fixed chart menu.  The ``chart_type`` input is
determined by the agent based on the data and question.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from tools.base import BaseTool, ToolCategory, registry

logger = structlog.get_logger(__name__)


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class VisualizationTool(BaseTool):
    """Generate a Plotly JSON chart specification from analysis result data."""

    name = "generate_visualization"
    description = (
        "Generate a Plotly chart spec (JSON) from query result rows. "
        "Input: chart_type, x_column, y_column(s), rows, title, and optional color_column. "
        "Output: plotly_spec (JSON string) and chart_type. "
        "The agent should choose chart_type based on the data shape: "
        "use time_series for dates, bar for categories, scatter for correlations, "
        "histogram for distributions, funnel for conversion rates."
    )
    category = ToolCategory.VISUALIZATION

    _SUPPORTED = {
        "time_series", "bar", "horizontal_bar", "scatter",
        "histogram", "pie", "funnel", "heatmap", "box", "waterfall",
        "line",
    }

    async def _run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        chart_type = str(input_data.get("chart_type", "bar")).lower()
        if chart_type not in self._SUPPORTED:
            chart_type = "bar"

        rows: list[dict[str, Any]] = input_data.get("rows") or []
        x_col = str(input_data.get("x_column", ""))
        y_col = input_data.get("y_column")  # str or list[str]
        title = str(input_data.get("title", "Analysis Result"))
        color_col = input_data.get("color_column")

        if not rows:
            return {
                "chart_type": chart_type,
                "plotly_spec": json.dumps({"data": [], "layout": {"title": title}}),
                "row_count": 0,
            }

        spec = self._build_spec(chart_type, rows, x_col, y_col, color_col, title)
        return {
            "chart_type": chart_type,
            "plotly_spec": json.dumps(spec, default=str),
            "row_count": len(rows),
            "x_column": x_col,
            "y_column": y_col,
        }

    def _build_spec(
        self,
        chart_type: str,
        rows: list[dict[str, Any]],
        x_col: str,
        y_col: Any,
        color_col: str | None,
        title: str,
    ) -> dict[str, Any]:
        x_vals = [r.get(x_col) for r in rows]
        layout: dict[str, Any] = {
            "title": title,
            "template": "plotly_white",
            "xaxis": {"title": x_col},
            "yaxis": {"title": y_col if isinstance(y_col, str) else "value"},
        }

        if chart_type in ("time_series", "line"):
            y_vals = [_safe_float(r.get(y_col)) for r in rows] if isinstance(y_col, str) else []
            data = [{"type": "scatter", "mode": "lines+markers", "x": x_vals, "y": y_vals, "name": y_col}]

        elif chart_type == "bar":
            if isinstance(y_col, list):
                data = [
                    {"type": "bar", "x": x_vals, "y": [_safe_float(r.get(yc)) for r in rows], "name": yc}
                    for yc in y_col
                ]
                layout["barmode"] = "group"
            else:
                y_vals = [_safe_float(r.get(y_col)) for r in rows]
                data = [{"type": "bar", "x": x_vals, "y": y_vals, "name": y_col}]

        elif chart_type == "horizontal_bar":
            y_vals = [_safe_float(r.get(y_col)) for r in rows]
            data = [{"type": "bar", "orientation": "h", "x": y_vals, "y": x_vals, "name": y_col}]
            layout["xaxis"]["title"] = y_col if isinstance(y_col, str) else "value"
            layout["yaxis"]["title"] = x_col

        elif chart_type == "scatter":
            y_vals = [_safe_float(r.get(y_col)) for r in rows]
            trace: dict[str, Any] = {"type": "scatter", "mode": "markers", "x": x_vals, "y": y_vals}
            if color_col:
                trace["marker"] = {"color": [str(r.get(color_col)) for r in rows]}
            data = [trace]

        elif chart_type == "histogram":
            vals = [_safe_float(r.get(x_col)) for r in rows]
            data = [{"type": "histogram", "x": vals, "name": x_col}]
            layout.pop("yaxis", None)

        elif chart_type == "pie":
            y_vals = [_safe_float(r.get(y_col)) for r in rows]
            data = [{"type": "pie", "labels": x_vals, "values": y_vals}]

        elif chart_type == "funnel":
            y_vals = [_safe_float(r.get(y_col)) for r in rows]
            data = [{"type": "funnel", "x": y_vals, "y": x_vals}]

        elif chart_type == "box":
            y_vals = [_safe_float(r.get(y_col)) for r in rows]
            data = [{"type": "box", "y": y_vals, "name": y_col if isinstance(y_col, str) else "values"}]

        elif chart_type == "heatmap":
            # Expects rows with: x_col, y_col (second category), and z_col (value)
            z_col = str(y_col) if isinstance(y_col, str) else (y_col[0] if y_col else "")
            xs = sorted(set(r.get(x_col) for r in rows))
            ys = sorted(set(r.get(color_col or "") for r in rows)) if color_col else []
            z_data = [[0.0] * len(xs) for _ in ys]
            idx_x = {v: i for i, v in enumerate(xs)}
            idx_y = {v: i for i, v in enumerate(ys)}
            for r in rows:
                xi = idx_x.get(r.get(x_col), -1)
                yi = idx_y.get(r.get(color_col or ""), -1)
                if xi >= 0 and yi >= 0:
                    z_data[yi][xi] = _safe_float(r.get(z_col)) or 0.0
            data = [{"type": "heatmap", "x": xs, "y": ys, "z": z_data}]

        elif chart_type == "waterfall":
            y_vals = [_safe_float(r.get(y_col)) for r in rows]
            data = [{"type": "waterfall", "x": x_vals, "y": y_vals}]

        else:
            y_vals = [_safe_float(r.get(y_col)) for r in rows]
            data = [{"type": "bar", "x": x_vals, "y": y_vals}]

        return {"data": data, "layout": layout}


registry.register(VisualizationTool())
