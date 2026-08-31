"""Pydantic v2 schemas for every API contract.

OpenAI-compatible request/response models (used by Open WebUI) live alongside
the domain schemas for decisions, signals, and evaluation runs.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Literal

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

ChatRole = Literal["system", "user", "assistant", "tool"]


# --------------------------------------------------------------------------
# OpenAI-compatible chat completions
# --------------------------------------------------------------------------
class ChatMessage(BaseModel):
    """OpenAI chat message. ``content`` may be plain text or content parts."""

    model_config = ConfigDict(extra="ignore")

    role: ChatRole
    content: str | list[dict[str, Any]] | None = None

    @property
    def text(self) -> str:
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return " ".join(part.get("text", "") for part in self.content if isinstance(part, dict))


class ChatCompletionRequest(BaseModel):
    """POST /v1/chat/completions request body (OpenAI shape)."""

    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: str | list[str] | None = None
    user: str | None = None


class CompletionUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChoiceMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class ChatChoice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str | None = "stop"


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible non-streaming completion response."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: CompletionUsage = CompletionUsage()

    @classmethod
    def build(
        cls, *, model: str, content: str, usage: CompletionUsage | None = None
    ) -> ChatCompletionResponse:
        return cls(
            id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
            created=int(time.time()),
            model=model,
            choices=[ChatChoice(message=ChoiceMessage(content=content))],
            usage=usage or CompletionUsage(),
        )


# --------------------------------------------------------------------------
# OpenAI-compatible model list
# --------------------------------------------------------------------------
class ModelObject(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "multi-agent-backend"


class ModelListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelObject]


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------
class DecisionLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mode: str
    status: str
    context_commit_sha: str
    thread_id: str | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    evaluation_score: float | None = None
    latency_ms: int | None = None
    cost_usd: Decimal | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    created_at: datetime


class DecisionListResponse(BaseModel):
    items: list[DecisionLogRead]
    total: int
    limit: int
    offset: int


class DecisionStats(BaseModel):
    mode: str
    runs: int
    avg_latency_ms: float | None = None
    avg_cost_usd: float | None = None
    avg_evaluation_score: float | None = None


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------
class SignalCreate(BaseModel):
    """Ingest a monitor event. Triggers the monitor graph as a background task."""

    source: str = "api"
    payload: dict[str, Any] = Field(
        description="Event payload, e.g. {metric: error_rate, value: 0.31, source: payments-api}"
    )


class SignalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    kind: str | None = None
    severity: str | None = None
    status: str
    payload: dict[str, Any]
    classification: dict[str, Any] | None = None
    recommended_action: dict[str, Any] | None = None
    thread_id: str | None = None
    created_at: datetime
    updated_at: datetime


class SignalListResponse(BaseModel):
    items: list[SignalRead]
    total: int
    limit: int
    offset: int


class ApprovalDecision(BaseModel):
    """Human decision delivered by the n8n approval webhook."""

    approved: bool
    approver: str | None = "n8n"


# --------------------------------------------------------------------------
# Evaluations
# --------------------------------------------------------------------------
class EvalSuiteInfo(BaseModel):
    name: str
    mode: str
    scorer: str
    cases: int


class EvalRunRequest(BaseModel):
    suite: str


class EvalRunAccepted(BaseModel):
    run_id: str
    suite: str
    status: str = "running"
    status_url: str


class EvalCaseResult(BaseModel):
    id: str
    score: float
    passed: bool
    detail: str | None = None


class EvalRunStatus(BaseModel):
    run_id: str
    suite: str
    mode: str | None = None
    status: Literal["running", "done", "failed"]
    started_at: str | None = None
    finished_at: str | None = None
    total: int = 0
    passed: int = 0
    mean_score: float = 0.0
    pass_rate: float = 0.0
    error: str | None = None
    results: list[EvalCaseResult] = Field(default_factory=list)
