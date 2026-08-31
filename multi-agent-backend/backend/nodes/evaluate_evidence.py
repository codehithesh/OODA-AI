"""LangGraph node: deterministic evidence evaluation of peer contributions."""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class EvaluateEvidenceInput(BaseModel):
    """Input state keys read by evaluate_evidence."""

    peers: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class PeerScore(BaseModel):
    """Evidence score for a single peer."""

    peer_id: str
    score: float
    evidence_count: int
    reasons: list[str] = Field(default_factory=list)


def score_peer(peer: dict[str, Any], scoring: dict[str, Any]) -> PeerScore:
    """Score one peer: evidence strength calibrated against claimed confidence."""
    evidence_weight = float(scoring.get("evidence_weight", 0.6))
    calibration_weight = float(scoring.get("confidence_calibration_weight", 0.4))

    evidence = peer.get("evidence") or []
    evidence_count = len(evidence)
    evidence_strength = min(1.0, 0.25 * evidence_count)  # 4+ pieces -> 1.0
    confidence = float(peer.get("confidence", 0.5))
    calibration = max(0.0, 1.0 - abs(confidence - evidence_strength))

    reasons: list[str] = []
    if evidence_count == 0:
        reasons.append("no evidence provided")
    else:
        reasons.append(f"{evidence_count} evidence item(s)")
    if confidence > evidence_strength + 0.3:
        reasons.append("confidence exceeds evidence strength")
    if peer.get("open_questions"):
        reasons.append(f"{len(peer['open_questions'])} open question(s)")

    score = evidence_weight * evidence_strength + calibration_weight * calibration
    return PeerScore(
        peer_id=str(peer.get("peer_id", "peer")),
        score=round(score, 4),
        evidence_count=evidence_count,
        reasons=reasons,
    )


async def evaluate_evidence(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Evaluate peer evidence deterministically and decide research readiness.

    Input state keys:
        peers: list of PeerContribution dicts.
        context: ContextBundle with rules['research'] (thresholds, weights).

    Output state keys:
        evidence_scores: list of PeerScore dicts.
        research_quality: mean peer score (0..1).
        research_ready: True when quality >= consensus threshold AND every peer
                        provided the minimum evidence.

    Side-effect guarantees:
        None — pure computation, no I/O at all.
    """
    inp = EvaluateEvidenceInput.model_validate(state)
    rules = (inp.context.get("rules") or {}).get("research", {})
    scoring = rules.get("scoring", {})
    consensus_threshold = float(rules.get("consensus_threshold", 0.6))
    min_evidence = int(rules.get("min_evidence_per_peer", 1))

    scores = [score_peer(p, scoring) for p in inp.peers]
    quality = round(sum(s.score for s in scores) / len(scores), 4) if scores else 0.0
    ready = (
        bool(scores)
        and quality >= consensus_threshold
        and all(s.evidence_count >= min_evidence for s in scores)
    )
    logger.info("evidence_evaluated", peers=len(scores), quality=quality, ready=ready)
    return {
        "evidence_scores": [s.model_dump() for s in scores],
        "research_quality": quality,
        "research_ready": ready,
    }
