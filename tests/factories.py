"""Small, deterministic domain fixtures."""

from __future__ import annotations

from dogos_memory import models


def profile(dog_id: str = "dog_a") -> models.Profile:
    return models.Profile.model_validate(
        {
            "dog_id": dog_id,
            "name": dog_id,
            "personality": "cautious",
            "familiar_threshold": 60,
            "mode": "live",
        }
    )


def event(
    memory_id: str = "e1", peer_id: str = "dog_b", timestamp: int = 1000
) -> models.MemoryInput:
    return models.MemoryInput.model_validate(
        {
            "memory_id": memory_id,
            "session_id": "round-1",
            "peer_id": peer_id,
            "kind": "event",
            "event_type": "greeting_completed",
            "content": "B 向我打招呼",
            "occurred_at_ms": timestamp,
            "source": "operator",
            "result": "completed",
            "affinity_delta": 35,
        }
    )


def thought(evidence_id: str = "e1") -> models.MemoryInput:
    return models.MemoryInput.model_validate(
        {
            "memory_id": "t1",
            "session_id": "round-1",
            "peer_id": "dog_b",
            "kind": "thought",
            "event_type": "impression",
            "content": "我觉得 B 很友好",
            "occurred_at_ms": 2000,
            "source": "llm",
            "result": "derived",
            "evidence_ids": (evidence_id,),
        }
    )
