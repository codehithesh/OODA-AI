"""Test fixtures: fake LLM, fake DB session, app/client with injected deps.

No external services are required: LiteLLM is faked per test, the DB session
is faked, the checkpointer is an in-memory saver, and Redis calls degrade
gracefully by design.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

# Tests never connect to Postgres, but database.py builds a module-level
# engine whose URL must be parseable. Guard against ambient env vars that
# would break it (e.g. a non-SQLAlchemy DATABASE_URL from the host machine).
os.environ["DATABASE_URL"] = "postgresql+asyncpg://agent:agent@localhost:5432/agent"

import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import MemorySaver

import graphs.analytics_graph
import graphs.monitor_graph
import graphs.research_graph
import graphs.simulate_graph  # noqa: F401
from clients.litellm_client import LLMResponse, LLMUsage
from clients.prompt_loader import get_default_prompt_loader
from database import get_db
from main import create_app
from models import DecisionLog, Signal


# --------------------------------------------------------------------------
# fake LLM
# --------------------------------------------------------------------------
class FakeLLM:
    """Scripted LiteLLM stand-in returning queued responses."""

    def __init__(self, responses: list[str] | None = None, fn: Any = None) -> None:
        self.responses = list(responses or [])
        self.fn = fn
        self.calls: list[list[dict[str, str]]] = []

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(messages)
        if self.fn is not None:
            content = self.fn(messages)
        elif self.responses:
            content = self.responses.pop(0)
        else:
            content = "{}"
        return LLMResponse(
            content=content,
            model=model or "fake-model",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.002),
        )


# --------------------------------------------------------------------------
# fake DB session
# --------------------------------------------------------------------------
class FakeResult:
    """Deterministic result object for FakeSession.execute()."""

    def __init__(self, items: list[Any] | None = None, scalar: Any = None) -> None:
        self._items = items or []
        self._scalar = scalar

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._items

    def scalar_one(self) -> Any:
        return self._scalar

    def scalar_one_or_none(self) -> Any | None:
        return self._scalar

    def first(self) -> Any | None:
        return self._items[0] if self._items else None


class FakeSession:
    """Minimal AsyncSession stand-in: queued results + recorded writes."""

    def __init__(
        self, results: list[FakeResult] | None = None, gets: dict[Any, Any] | None = None
    ) -> None:
        self.results = list(results or [])
        self.gets = dict(gets or {})
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.commits = 0
        self.rolled_back = 0

    async def execute(self, stmt: Any) -> FakeResult:
        if self.results:
            return self.results.pop(0)
        return FakeResult()

    async def scalar(self, stmt: Any) -> Any | None:
        result = await self.execute(stmt)
        return result.scalar_one_or_none()

    async def get(self, model: Any, pk: Any) -> Any | None:
        return self.gets.get(pk)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)

    async def flush(self) -> None:
        # emulate database-side defaults so response models can serialize
        from datetime import datetime

        now = datetime.now(UTC)
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            if getattr(obj, "created_at", None) is None:
                obj.created_at = now
            if getattr(obj, "updated_at", None) is None:
                obj.updated_at = now

    async def commit(self) -> None:
        self.commits += 1
        await self.flush()

    async def rollback(self) -> None:
        self.rolled_back += 1

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()


def make_deps(fake_llm: FakeLLM) -> dict[str, Any]:
    """Dependency bundle injected via LangGraph config / app.state."""
    return {"litellm_client": fake_llm, "prompt_loader": get_default_prompt_loader()}


@pytest.fixture
def app(fake_llm: FakeLLM):
    application = create_app()
    application.state.checkpointer = MemorySaver()
    application.state.agent_deps = make_deps(fake_llm)
    return application


@pytest.fixture
async def client(app) -> AsyncClient:
    # ASGITransport does not run the lifespan, so seed DuckDB here exactly
    # the way the lifespan would (idempotent, deterministic demo data).
    import contextlib

    from clients.duckdb_client import get_duckdb_client
    from config import get_settings

    with contextlib.suppress(Exception):
        await get_duckdb_client().seed_from_context(get_settings().context_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


@pytest.fixture
def override_db(app, fake_session: FakeSession) -> FakeSession:
    app.dependency_overrides[get_db] = lambda: fake_session
    return fake_session


@pytest.fixture
def sample_decision() -> DecisionLog:
    return DecisionLog(
        id=uuid.uuid4(),
        mode="analytics",
        status="succeeded",
        context_commit_sha="a" * 40,
        thread_id="analytics-1",
        input={"query": "revenue"},
        output={"generated_sql": "SELECT 1"},
        evaluation_score=1.0,
        latency_ms=123,
        cost_usd=0.002,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def sample_signal() -> Signal:
    return Signal(
        id=uuid.uuid4(),
        source="payments-api",
        payload={"metric": "error_rate", "value": 0.31},
        status="pending_approval",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
