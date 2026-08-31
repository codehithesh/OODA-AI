"""Thin async wrapper around the LiteLLM proxy.

The proxy (external Docker container) exposes an OpenAI-compatible HTTP API;
this client is the ONLY place the backend talks to an LLM. It adds:

* retries with backoff on transport errors and 5xx responses,
* usage + cost extraction (``x-litellm-response-cost`` header),
* Langfuse tracing via ``observability.record_llm_call``,
* dependency injection for tests via ``config['configurable']['litellm_client']``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog
from pydantic import BaseModel

from config import get_settings
from observability import record_llm_call

logger = structlog.get_logger(__name__)


class LLMError(RuntimeError):
    """Raised when the LiteLLM proxy cannot fulfil a completion request."""


class LLMUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class LLMResponse(BaseModel):
    content: str = ""
    model: str = ""
    finish_reason: str | None = None
    usage: LLMUsage = LLMUsage()

    def usage_record(self) -> dict[str, Any]:
        """State-friendly usage record appended to GraphState['usage']."""
        return self.usage.model_dump()


class LiteLLMClient:
    """Minimal OpenAI-style chat client pointed at the LiteLLM proxy."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 90.0,
        max_retries: int = 2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Send a chat completion. Retries transport errors and 5xx codes."""
        settings = get_settings()
        payload: dict[str, Any] = {
            "model": model or settings.default_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._http.post("/v1/chat/completions", json=payload)
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"litellm proxy returned {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                return self._parse_response(resp, payload)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    delay = min(2.0**attempt, 8.0)
                    logger.warning(
                        "litellm_retry",
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        delay=delay,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
        raise LLMError(f"LiteLLM proxy failed after {self._max_retries + 1} attempts: {last_exc}")

    def _parse_response(self, resp: httpx.Response, payload: dict[str, Any]) -> LLMResponse:
        data = resp.json()
        choices = data.get("choices") or [{}]
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        usage = data.get("usage") or {}
        try:
            cost = float(resp.headers.get("x-litellm-response-cost", 0) or 0)
        except (TypeError, ValueError):
            cost = 0.0
        llm_response = LLMResponse(
            content=content,
            model=data.get("model", payload["model"]),
            finish_reason=choices[0].get("finish_reason"),
            usage=LLMUsage(
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
                cost_usd=cost,
            ),
        )
        record_llm_call(
            name="litellm.chat",
            model=llm_response.model,
            messages=payload["messages"],
            content=content,
            usage=llm_response.usage.model_dump(),
        )
        return llm_response

    async def health(self) -> bool:
        """Liveliness probe of the LiteLLM proxy."""
        try:
            resp = await self._http.get("/health/liveliness")
            return resp.status_code < 400
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._http.aclose()


_default_client: LiteLLMClient | None = None


def get_default_litellm_client() -> LiteLLMClient:
    """Process-wide LiteLLM client singleton built from Settings."""
    global _default_client
    if _default_client is None:
        s = get_settings()
        _default_client = LiteLLMClient(
            base_url=s.litellm_base_url,
            api_key=s.litellm_api_key,
            timeout=s.llm_timeout_seconds,
            max_retries=s.llm_max_retries,
        )
    return _default_client


def get_litellm_client(config: dict[str, Any] | None = None) -> LiteLLMClient:
    """Return the client to use for this node run.

    Order of precedence: LangGraph ``config['configurable']['litellm_client']``
    (test injection) -> the process-wide default singleton.
    """
    if config is not None:
        configurable = config.get("configurable") or {}
        override = configurable.get("litellm_client")
        if override is not None:
            return override
    return get_default_litellm_client()
