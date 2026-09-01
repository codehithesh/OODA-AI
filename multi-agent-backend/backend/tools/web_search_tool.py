"""WebSearchTool — external information retrieval via HTTP.

Wraps a configurable search backend.  The default backend calls the
DuckDuckGo Instant Answer API (no API key required) with a JSON result.
A ``SEARCH_BACKEND_URL`` env var lets operators substitute a self-hosted
SearXNG/Brave/Google CSE endpoint.

The tool returns structured snippets rather than raw HTML, so the agent
context-fusion layer can work with clean text.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from config import get_settings
from tools.base import BaseTool, ToolCategory, registry

logger = structlog.get_logger(__name__)

_DDG_URL = "https://api.duckduckgo.com/"
_DEFAULT_MAX_RESULTS = 5


class WebSearchTool(BaseTool):
    """Search the web for external context to complement warehouse data."""

    name = "web_search"
    description = (
        "Search the web for external information such as industry trends, "
        "market benchmarks, competitor data, or public reports. "
        "Use when the business question requires context that isn't in the warehouse. "
        "Returns a list of {title, url, snippet} results."
    )
    category = ToolCategory.WEB

    async def _run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        query = str(input_data.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")

        max_results = int(input_data.get("max_results", _DEFAULT_MAX_RESULTS))
        settings = get_settings()
        backend_url = getattr(settings, "search_backend_url", "") or _DDG_URL

        # DuckDuckGo Instant Answer JSON API
        if "duckduckgo" in backend_url or backend_url == _DDG_URL:
            return await self._ddg_search(query, max_results)

        # Generic fallback: POST to a custom search backend
        return await self._custom_search(backend_url, query, max_results)

    async def _ddg_search(self, query: str, max_results: int) -> dict[str, Any]:
        """Use DuckDuckGo Instant Answer API (no key required)."""
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(_DDG_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"DuckDuckGo search failed: {exc}") from exc

        results: list[dict[str, Any]] = []

        # Abstract (top result)
        if data.get("Abstract"):
            results.append({
                "title": data.get("Heading", ""),
                "url": data.get("AbstractURL", ""),
                "snippet": data.get("Abstract", ""),
                "source": data.get("AbstractSource", ""),
            })

        # RelatedTopics
        for topic in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append({
                    "title": topic.get("Text", "")[:100],
                    "url": topic.get("FirstURL", ""),
                    "snippet": topic.get("Text", ""),
                    "source": "DuckDuckGo",
                })

        return {
            "query": query,
            "results": results[:max_results],
            "result_count": len(results[:max_results]),
            "source": "duckduckgo",
        }

    async def _custom_search(
        self, backend_url: str, query: str, max_results: int
    ) -> dict[str, Any]:
        """POST to a custom JSON search backend."""
        payload = {"q": query, "max_results": max_results}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(backend_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Custom search backend failed: {exc}") from exc

        # Normalize: expect {results: [{title, url, snippet}]}
        results = data.get("results") or []
        return {
            "query": query,
            "results": results[:max_results],
            "result_count": len(results[:max_results]),
            "source": backend_url,
        }


registry.register(WebSearchTool())
