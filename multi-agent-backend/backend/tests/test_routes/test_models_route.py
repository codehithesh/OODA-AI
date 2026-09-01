"""Route tests: GET /v1/models."""

from __future__ import annotations

_EXPECTED_MODES = {"analytics", "monitor", "research", "simulate", "eda"}


async def test_models_lists_all_modes(client) -> None:
    response = await client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    ids = {m["id"] for m in body["data"]}
    # Must contain at minimum the original four modes plus eda
    assert _EXPECTED_MODES.issubset(ids), f"Missing modes: {_EXPECTED_MODES - ids}"
    for model in body["data"]:
        assert model["object"] == "model"
        assert model["owned_by"] == "multi-agent-backend"
        assert isinstance(model["created"], int)


# Keep old name as alias so any external CI scripts using it still pass
test_models_lists_all_four_modes = test_models_lists_all_modes


async def test_models_requires_no_auth_when_keys_empty(client) -> None:
    response = await client.get("/v1/models")
    assert response.status_code == 200
