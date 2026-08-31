"""Route tests: GET /v1/models."""

from __future__ import annotations


async def test_models_lists_all_four_modes(client) -> None:
    response = await client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    ids = {m["id"] for m in body["data"]}
    assert ids == {"analytics", "monitor", "research", "simulate"}
    for model in body["data"]:
        assert model["object"] == "model"
        assert model["owned_by"] == "multi-agent-backend"
        assert isinstance(model["created"], int)


async def test_models_requires_no_auth_when_keys_empty(client) -> None:
    response = await client.get("/v1/models")
    assert response.status_code == 200
