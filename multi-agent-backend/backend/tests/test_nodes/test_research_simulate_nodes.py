"""Unit tests for research + simulate nodes (scripted LLM, deterministic scoring)."""

from __future__ import annotations

from clients.prompt_loader import get_default_prompt_loader
from nodes.collect_reactions import collect_reactions
from nodes.evaluate_evidence import evaluate_evidence
from nodes.parallel_peers import parallel_peers
from nodes.score_variants import score_variants
from nodes.spawn_personas import spawn_personas
from nodes.synthesize import synthesize
from tests.conftest import FakeLLM

PEER_JSON = '{"claim": "c", "evidence": ["e1", "e2"], "confidence": 0.5, "open_questions": []}'
RESEARCH_CONTEXT = {
    "rules": {
        "research": {
            "peer_count": 3,
            "max_generations": 2,
            "consensus_threshold": 0.6,
            "min_evidence_per_peer": 1,
            "scoring": {"evidence_weight": 0.6, "confidence_calibration_weight": 0.4},
        }
    },
    "personas": {
        "research": [
            {
                "id": "p1",
                "name": "peer1",
                "role": "r",
                "disposition": "skeptical",
                "focus": ["validity"],
            },
            {
                "id": "p2",
                "name": "peer2",
                "role": "r",
                "disposition": "pragmatic",
                "focus": ["ops"],
            },
            {
                "id": "p3",
                "name": "peer3",
                "role": "r",
                "disposition": "empirical",
                "focus": ["data"],
            },
        ]
    },
    "prompts": {
        "peer_response": "research/peer_response.md",
        "synthesize": "research/synthesize.md",
    },
}

SIMULATE_CONTEXT = {
    "rules": {
        "simulate": {
            "variant_count": 2,
            "variant_styles": ["concise", "detailed"],
            "persona_count": 4,
            "scoring": {
                "support_weight": 0.6,
                "opposition_weight": 0.25,
                "engagement_weight": 0.15,
            },
        }
    },
    "personas": {
        "simulate": [
            {
                "id": "a",
                "name": "A",
                "archetype": "arch",
                "temperament": "calm",
                "priorities": ["clarity"],
                "voice": "plain",
            },
            {
                "id": "b",
                "name": "B",
                "archetype": "arch",
                "temperament": "bold",
                "priorities": ["speed"],
                "voice": "direct",
            },
        ]
    },
    "prompts": {"draft": "simulate/draft.md", "persona_reaction": "simulate/persona_reaction.md"},
}


def _config(fake: FakeLLM) -> dict:
    return {"configurable": {"litellm_client": fake, "prompt_loader": get_default_prompt_loader()}}


async def test_parallel_peers_gathers_all_personas() -> None:
    fake = FakeLLM(responses=[PEER_JSON] * 3)
    result = await parallel_peers(
        {"query": "q", "brief": "b", "generation": 0, "context": RESEARCH_CONTEXT}, _config(fake)
    )
    assert len(result["peers"]) == 3
    assert result["generation"] == 1
    assert len(result["usage"]) == 3
    assert result["peers"][0]["evidence"] == ["e1", "e2"]


async def test_parallel_peers_degrades_on_failure() -> None:
    def fail(_messages: object) -> str:
        raise RuntimeError("llm down")

    fake = FakeLLM(fn=fail)
    result = await parallel_peers(
        {"query": "q", "generation": 0, "context": RESEARCH_CONTEXT}, _config(fake)
    )
    assert len(result["peers"]) == 3
    assert all(p["claim"] == "" for p in result["peers"])


async def test_evaluate_evidence_ready() -> None:
    peers = [
        {"peer_id": "p1", "evidence": ["e1", "e2"], "confidence": 0.5},
        {"peer_id": "p2", "evidence": ["e1", "e2"], "confidence": 0.5},
    ]
    result = await evaluate_evidence({"peers": peers, "context": RESEARCH_CONTEXT}, {})
    assert result["research_ready"] is True
    assert result["research_quality"] >= 0.6
    assert len(result["evidence_scores"]) == 2


async def test_evaluate_evidence_not_ready_without_evidence() -> None:
    peers = [{"peer_id": "p1", "evidence": [], "confidence": 0.9}]
    result = await evaluate_evidence({"peers": peers, "context": RESEARCH_CONTEXT}, {})
    assert result["research_ready"] is False
    assert "no evidence provided" in result["evidence_scores"][0]["reasons"]


async def test_synthesize_final_when_ready() -> None:
    fake = FakeLLM(responses=["FINAL ANSWER markdown"])
    result = await synthesize(
        {
            "query": "q",
            "peers": [],
            "evidence_scores": [],
            "research_ready": True,
            "generation": 1,
            "context": RESEARCH_CONTEXT,
        },
        _config(fake),
    )
    assert result["synthesis"] == "FINAL ANSWER markdown"
    assert result["next_brief"] is None
    assert result["research_ready"] is True


async def test_synthesize_next_brief_when_not_ready() -> None:
    fake = FakeLLM(responses=['{"synthesis": "interim", "next_brief": "sharper brief"}'])
    result = await synthesize(
        {
            "query": "q",
            "peers": [],
            "evidence_scores": [],
            "research_ready": False,
            "generation": 1,
            "context": RESEARCH_CONTEXT,
        },
        _config(fake),
    )
    assert result["synthesis"] == "interim"
    assert result["next_brief"] == "sharper brief"
    assert result["research_ready"] is False


async def test_spawn_personas_limits_count() -> None:
    result = await spawn_personas({"context": SIMULATE_CONTEXT}, {})
    assert len(result["personas"]) == 2


async def test_collect_reactions_fan_out() -> None:
    reactions = ['{"stance": "support", "intensity": 5, "rationale": "r", "key_concern": "k"}'] * 4
    fake = FakeLLM(responses=reactions)
    state = {
        "query": "q",
        "drafts": [
            {"variant_id": "v1", "style": "concise", "text": "d1"},
            {"variant_id": "v2", "style": "detailed", "text": "d2"},
        ],
        "personas": SIMULATE_CONTEXT["personas"]["simulate"],
        "context": SIMULATE_CONTEXT,
    }
    result = await collect_reactions(state, _config(fake))
    assert len(result["reactions"]) == 4  # 2 personas x 2 drafts
    assert result["reactions"][0]["stance"] == "support"
    assert len(result["usage"]) == 4


async def test_score_variants_orders_descending() -> None:
    state = {
        "drafts": [{"variant_id": "v1"}, {"variant_id": "v2"}],
        "reactions": [
            {"variant_id": "v1", "stance": "support", "intensity": 5},
            {"variant_id": "v1", "stance": "support", "intensity": 4},
            {"variant_id": "v2", "stance": "oppose", "intensity": 5},
            {"variant_id": "v2", "stance": "neutral", "intensity": 2},
        ],
        "context": SIMULATE_CONTEXT,
    }
    result = await score_variants(state, {})
    scores = result["scores"]
    assert scores[0]["variant_id"] == "v1"
    assert scores[0]["support"] == 2
    assert scores[0]["score"] > scores[1]["score"]
    assert scores[1]["oppose"] == 1
