"""OpenAI-compatible model list — one "model" per agent mode.

GET /v1/models is what Open WebUI calls to populate its model picker.
"""

from __future__ import annotations

import time

from fastapi import APIRouter

from graphs.base import available_modes
from schemas import ModelListResponse, ModelObject

router = APIRouter()

_CREATED = int(time.time())


@router.get("/models", response_model=ModelListResponse, summary="List agent modes as models")
async def list_models() -> ModelListResponse:
    """Return the available agent modes in the OpenAI model-list shape."""
    return ModelListResponse(
        object="list",
        data=[
            ModelObject(id=mode, created=_CREATED, owned_by="multi-agent-backend")
            for mode in available_modes()
        ],
    )
