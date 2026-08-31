"""Route tests: decisions CRUD + signals ingestion/approval with a fake session."""

from __future__ import annotations

import uuid

from tests.conftest import FakeResult


async def test_list_decisions(client, override_db, sample_decision) -> None:
    override_db.results = [
        FakeResult(items=[sample_decision], scalar=1),  # rows query
        FakeResult(items=[sample_decision], scalar=1),  # count query
    ]
    response = await client.get("/v1/decisions")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["mode"] == "analytics"
    assert body["items"][0]["evaluation_score"] == 1.0


async def test_decision_stats(client, override_db) -> None:
    override_db.results = [
        FakeResult(items=[("analytics", 5, 120.0, 0.01, 0.9)]),
    ]
    response = await client.get("/v1/decisions/stats")
    assert response.status_code == 200
    stats = response.json()
    assert stats[0]["mode"] == "analytics"
    assert stats[0]["runs"] == 5
    assert stats[0]["avg_latency_ms"] == 120.0


async def test_get_decision_not_found(client, override_db) -> None:
    override_db.gets = {}
    response = await client.get(f"/v1/decisions/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_delete_decision(client, override_db, sample_decision) -> None:
    override_db.gets = {sample_decision.id: sample_decision}
    response = await client.delete(f"/v1/decisions/{sample_decision.id}")
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert override_db.deleted == [sample_decision]


async def test_ingest_signal_accepted(client, fake_llm, override_db) -> None:
    response = await client.post(
        "/v1/signals",
        json={"source": "payments-api", "payload": {"metric": "error_rate", "value": 0.31}},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["source"] == "payments-api"
    signals = [o for o in override_db.added if type(o).__name__ == "Signal"]
    assert signals, "ingestion must create a Signal row"


async def test_approve_signal_not_found(client, override_db) -> None:
    response = await client.post(
        f"/v1/signals/{uuid.uuid4()}/approve",
        json={"approved": True},
    )
    assert response.status_code == 404


async def test_approve_signal_not_pending(client, override_db, sample_signal) -> None:
    sample_signal.status = "executed"
    override_db.gets = {sample_signal.id: sample_signal}
    response = await client.post(
        f"/v1/signals/{sample_signal.id}/approve",
        json={"approved": True},
    )
    assert response.status_code == 409


async def test_ingest_signal_validation(client) -> None:
    response = await client.post("/v1/signals", json={"source": "x"})
    assert response.status_code == 422
