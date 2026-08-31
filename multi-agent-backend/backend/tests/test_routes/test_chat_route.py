"""Route tests: POST /v1/chat/completions (JSON + SSE + monitor approval)."""

from __future__ import annotations

import json


def _chat_body(model: str, content: str, **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": content}], **extra}


async def test_chat_analytics_json_completion(client, fake_llm, override_db) -> None:
    fake_llm.responses = ["```sql\nSELECT SUM(amount) AS total_revenue FROM orders\n```"]
    response = await client.post(
        "/v1/chat/completions",
        json=_chat_body("analytics", "What is the total revenue?"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "analytics"
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert "SELECT SUM(amount)" in choice["message"]["content"]
    assert choice["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] == 30
    # SQL was executed against the embedded DuckDB (seeded deterministically)
    assert "Executed on DuckDB" in choice["message"]["content"]
    # DecisionLog + ContextSnapshot rows were handed to the session
    added_types = {type(obj).__name__ for obj in override_db.added}
    assert "DecisionLog" in added_types
    assert "ContextSnapshot" in added_types


async def test_chat_analytics_sse_stream(client, fake_llm, override_db) -> None:
    fake_llm.responses = ["```sql\nSELECT COUNT(*) FROM orders\n```"]
    response = await client.post(
        "/v1/chat/completions",
        json=_chat_body("analytics", "how many orders?", stream=True),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    text = response.text
    events = [line for line in text.split("\n\n") if line.startswith("data: ")]
    assert events, "at least one SSE event expected"
    assert events[-1] == "data: [DONE]"
    first = json.loads(events[0][len("data: ") :])
    assert first["object"] == "chat.completion.chunk"
    assert first["choices"][0]["delta"]["role"] == "assistant"
    joined = "".join(
        json.loads(e[len("data: ") :])["choices"][0]["delta"].get("content", "")
        for e in events[:-2]
    )
    assert "SELECT COUNT(*)" in joined
    final = json.loads(events[-2][len("data: ") :])
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"]["total_tokens"] == 30


async def test_chat_unknown_model_returns_openai_error(client) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=_chat_body("gpt-999", "hi"),
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["type"] == "api_error"
    assert "not found" in body["error"]["message"]


async def test_chat_monitor_requires_approval(client, fake_llm, override_db) -> None:
    fake_llm.responses = [
        (
            '{"kind": "error_burst", "severity": "critical", "'
            'summary": "errors spiking", "confidence": 0.9}'
        )
    ]
    event = {"metric": "error_rate", "value": 0.31, "source": "payments-api"}
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "monitor",
            "messages": [{"role": "user", "content": json.dumps(event)}],
        },
    )
    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert "pending" in content.lower()
    assert "require_approval" in content or "Approval" in content
    # a Signal row was created and marked pending_approval
    signals = [o for o in override_db.added if type(o).__name__ == "Signal"]
    assert signals and signals[0].status == "pending_approval"
    assert signals[0].thread_id


async def test_chat_research_mode(client, fake_llm, override_db) -> None:
    peer = (
        '{"claim": "c", "evidence": ["e1", "e2", "e3", "e4"], '
        '"confidence": 0.9, "open_questions": []}'
    )
    fake_llm.responses = [peer] * 3 + ["## Final answer\nThe answer is 42."]
    response = await client.post(
        "/v1/chat/completions",
        json=_chat_body("research", "What is the answer to everything?"),
    )
    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert "The answer is 42" in content
    assert "Research agent" in content


async def test_chat_validation_error_shape(client) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "analytics", "messages": []},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
