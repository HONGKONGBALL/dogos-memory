"""Persistence, idempotence, isolation, and query behavior against real files."""

from pathlib import Path

import pytest

from dogos_memory import models, store
from tests.factories import event, profile


def test_history_survives_when_store_is_reopened(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "a.db"
    original = store.MemoryStore.initialize(path, profile())
    _ = original.append(event())
    _ = original.append(event("e2", timestamp=2000))
    # When
    restored = store.MemoryStore(path, profile().dog_id).recall(
        models.RecallRequest.model_validate({"peer_id": "dog_b"})
    )
    # Then
    assert restored.affinity == 70
    assert restored.familiar is True
    assert [item.memory_id for item in restored.facts] == ["e2", "e1"]


def test_duplicate_is_noop_when_identical_event_arrives_twice(tmp_path: Path) -> None:
    # Given
    memory = store.MemoryStore.initialize(tmp_path / "a.db", profile())
    _ = memory.append(event())
    # When
    replay = memory.append(event())
    # Then
    assert replay.inserted is False
    assert replay.affinity == 35


def test_conflict_is_reported_when_existing_id_has_different_content(tmp_path: Path) -> None:
    # Given
    memory = store.MemoryStore.initialize(tmp_path / "a.db", profile())
    _ = memory.append(event())
    changed = models.MemoryInput.model_validate(event().model_dump() | {"content": "篡改"})
    # When / Then
    with pytest.raises(models.StoreError, match="idempotency_conflict"):
        _ = memory.append(changed)
    assert memory.get(event().memory_id).content == event().content


def test_cross_dog_open_fails_when_database_owner_differs(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "a.db"
    _ = store.MemoryStore.initialize(path, profile())
    # When / Then
    with pytest.raises(models.StoreError, match="owner_mismatch"):
        _ = store.MemoryStore(path, profile("dog_b").dog_id).recall(
            models.RecallRequest.model_validate({"peer_id": "dog_a"})
        )


def test_recall_filters_peer_and_orders_by_event_time(tmp_path: Path) -> None:
    # Given
    memory = store.MemoryStore.initialize(tmp_path / "a.db", profile())
    _ = memory.append(event("recent", timestamp=3000))
    _ = memory.append(event("other", peer_id="dog_c", timestamp=4000))
    _ = memory.append(event("late_delivery", timestamp=1000))
    # When
    recalled = memory.recall(models.RecallRequest.model_validate({"peer_id": "dog_b", "limit": 1}))
    # Then
    assert [item.memory_id for item in recalled.facts] == ["recent"]


def test_failed_action_is_auditable_but_not_recalled_as_success(tmp_path: Path) -> None:
    # Given
    memory = store.MemoryStore.initialize(tmp_path / "a.db", profile())
    failed = models.MemoryInput.model_validate(
        event().model_dump() | {"result": "failed", "affinity_delta": 0}
    )
    _ = memory.append(failed)
    # When
    recalled = memory.recall(models.RecallRequest.model_validate({"peer_id": "dog_b"}))
    # Then
    assert recalled.facts == ()
    assert recalled.affinity == 0
    assert memory.get(failed.memory_id).result == "failed"


def test_reinitialization_preserves_data_when_profile_is_unchanged(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "a.db"
    memory = store.MemoryStore.initialize(path, profile())
    _ = memory.append(event())
    # When
    reopened = store.MemoryStore.initialize(path, profile())
    # Then
    assert reopened.get(event().memory_id).content == event().content


def test_affinity_is_capped_when_many_events_are_completed(tmp_path: Path) -> None:
    # Given
    memory = store.MemoryStore.initialize(tmp_path / "a.db", profile())
    for index in range(3):
        _ = memory.append(event(str(index)))
    # When
    result = memory.append(event("fourth"))
    # Then
    assert result.affinity == 100
