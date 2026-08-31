"""LangGraph node: score draft variants from aggregated persona reactions."""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class ScoreVariantsInput(BaseModel):
    """Input state keys read by score_variants."""

    drafts: list[dict[str, Any]] = Field(default_factory=list)
    reactions: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class VariantScore(BaseModel):
    """Aggregate score for one draft variant."""

    variant_id: str
    score: float
    support: int
    oppose: int
    neutral: int
    avg_intensity: float
    breakdown: dict[str, Any] = Field(default_factory=dict)


def score_variant(
    variant_id: str, reactions: list[dict[str, Any]], weights: dict[str, Any]
) -> VariantScore:
    """Score one variant: weighted support, engagement, and opposition penalty."""
    support_weight = float(weights.get("support_weight", 0.6))
    opposition_weight = float(weights.get("opposition_weight", 0.25))
    engagement_weight = float(weights.get("engagement_weight", 0.15))

    support = sum(1 for r in reactions if r.get("stance") == "support")
    oppose = sum(1 for r in reactions if r.get("stance") == "oppose")
    neutral = sum(1 for r in reactions if r.get("stance") == "neutral")
    total = len(reactions)
    support_ratio = support / total if total else 0.0
    oppose_ratio = oppose / total if total else 0.0
    avg_intensity = (sum(int(r.get("intensity", 3)) for r in reactions) / total) if total else 0.0

    score = max(
        0.0,
        min(
            1.0,
            support_weight * support_ratio
            + engagement_weight * (avg_intensity / 5.0)
            - opposition_weight * oppose_ratio,
        ),
    )
    return VariantScore(
        variant_id=variant_id,
        score=round(score, 4),
        support=support,
        oppose=oppose,
        neutral=neutral,
        avg_intensity=round(avg_intensity, 2),
        breakdown={
            "support_ratio": round(support_ratio, 3),
            "oppose_ratio": round(oppose_ratio, 3),
            "avg_intensity": round(avg_intensity, 2),
        },
    )


async def score_variants(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Score every draft variant from its persona reactions (deterministic).

    Input state keys:
        drafts: list of {variant_id, ...}.
        reactions: list of PersonaReaction dicts.
        context: ContextBundle with rules['simulate'] scoring weights.

    Output state keys:
        scores: list of VariantScore dicts sorted by score descending.

    Side-effect guarantees:
        None — pure computation, no I/O at all.
    """
    inp = ScoreVariantsInput.model_validate(state)
    rules = (inp.context.get("rules") or {}).get("simulate", {})
    weights = rules.get("scoring", {})

    by_variant: dict[str, list[dict[str, Any]]] = {}
    for reaction in inp.reactions:
        by_variant.setdefault(str(reaction.get("variant_id", "")), []).append(reaction)

    scores = [
        score_variant(
            str(d.get("variant_id", "")), by_variant.get(str(d.get("variant_id", "")), []), weights
        )
        for d in inp.drafts
    ]
    scores.sort(key=lambda s: s.score, reverse=True)
    logger.info("variants_scored", count=len(scores), top=scores[0].variant_id if scores else None)
    return {"scores": [s.model_dump() for s in scores]}
