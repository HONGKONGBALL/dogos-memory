"""Explicitly simulated two-dog fixtures; no robot or model calls."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from dogos_memory.models import (
    BoundaryModel,
    DogId,
    MemoryId,
    MemoryInput,
    Profile,
    RecallRequest,
    RecallResult,
    SessionId,
)
from dogos_memory.store import MemoryStore


class DemoResult(BoundaryModel):
    """Two private perspectives with an explicit simulation label."""

    mode: Literal["simulation"] = "simulation"
    dog_a: RecallResult
    dog_b: RecallResult


def read_demo(directory: Path) -> DemoResult:
    """Read existing files only, suitable for a separate process after restart."""
    dog_a = MemoryStore(directory / "dog_a.db", DogId("dog_a"))
    dog_b = MemoryStore(directory / "dog_b.db", DogId("dog_b"))
    a_context = dog_a.recall(RecallRequest(peer_id=DogId("dog_b")))
    b_context = dog_b.recall(RecallRequest(peer_id=DogId("dog_a")))
    return DemoResult(dog_a=a_context, dog_b=b_context)


def seed_demo(directory: Path) -> DemoResult:
    """Insert two fixed simulated rounds; rerunning does not add extra score."""
    participants = (
        ("dog_a", "dog_b", "谨慎", 60, 35),
        ("dog_b", "dog_a", "外向", 40, 25),
    )
    for owner, peer, personality, threshold, delta in participants:
        profile = Profile(
            dog_id=DogId(owner),
            name=owner,
            personality=personality,
            familiar_threshold=threshold,
            mode="simulation",
        )
        memory = MemoryStore.initialize(directory / f"{owner}.db", profile)
        evidence: list[MemoryId] = []
        for round_number in (1, 2):
            memory_id = MemoryId(f"demo:{round_number}:greeting_completed")
            evidence.append(memory_id)
            _ = memory.append(
                MemoryInput(
                    memory_id=memory_id,
                    session_id=SessionId(f"demo:{round_number}"),
                    peer_id=DogId(peer),
                    kind="event",
                    event_type="greeting_completed",
                    content=f"[模拟] {peer} 向我打招呼，我回应了它。",
                    occurred_at_ms=round_number * 1000,
                    source="simulation",
                    result="completed",
                    affinity_delta=delta,
                )
            )
        _ = memory.append(
            MemoryInput(
                memory_id=MemoryId("demo:impression"),
                session_id=SessionId("demo:2"),
                peer_id=DogId(peer),
                kind="thought",
                event_type="impression",
                content=f"[人工编写的模拟印象] 我觉得 {peer} 很友好。",
                occurred_at_ms=3000,
                source="operator",
                result="derived",
                evidence_ids=tuple(evidence),
            )
        )
    return read_demo(directory)
