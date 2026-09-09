"""Derived thoughts never become observations or update relationships."""

from pathlib import Path

import pytest

from dogos_memory import models, store
from tests.factories import event, profile, thought


def test_thought_is_separate_from_facts_when_recalled(tmp_path: Path) -> None:
    # Given
    memory = store.MemoryStore.initialize(tmp_path / "a.db", profile())
    _ = memory.append(event())
    _ = memory.append(thought())
    # When
    context = memory.recall(models.RecallRequest.model_validate({"peer_id": "dog_b"}))
    # Then
    assert [item.kind for item in context.facts] == ["event"]
    assert context.thoughts[0].evidence_ids == ("e1",)
    assert context.affinity == 35


def test_entire_thought_rolls_back_when_any_evidence_is_invalid(tmp_path: Path) -> None:
    # Given
    memory = store.MemoryStore.initialize(tmp_path / "a.db", profile())
    _ = memory.append(event())
    invalid = models.MemoryInput.model_validate(
        thought().model_dump() | {"evidence_ids": ("e1", "missing")}
    )
    # When / Then
    with pytest.raises(models.StoreError, match="invalid_memory"):
        _ = memory.append(invalid)
    with pytest.raises(models.StoreError, match="memory_not_found"):
        _ = memory.get(invalid.memory_id)


def test_cross_peer_evidence_is_rejected(tmp_path: Path) -> None:
    # Given
    memory = store.MemoryStore.initialize(tmp_path / "a.db", profile())
    _ = memory.append(event(peer_id="dog_c"))
    # When / Then
    with pytest.raises(models.StoreError, match="invalid_memory"):
        _ = memory.append(thought())
