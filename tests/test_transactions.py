"""Concurrency and all-or-nothing behavior with real SQLite transactions."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from dogos_memory import models, store
from tests.factories import event, profile
from tests.test_schema import EVENT_SQL, PROFILE_SQL


def test_only_one_writer_counts_when_duplicate_callbacks_race(tmp_path: Path) -> None:
    # Given
    memory = store.MemoryStore.initialize(tmp_path / "a.db", profile())
    callbacks = [event()] * 4
    # When
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(memory.append, callbacks, timeout=5))
    # Then
    assert sum(result.inserted for result in results) == 1
    assert {result.affinity for result in results} == {35}


def test_event_is_rolled_back_when_relationship_update_fails(database: sqlite3.Connection) -> None:
    # Given: deterministic fault injection at the actual database write.
    _ = database.execute(PROFILE_SQL)
    _ = database.execute("""CREATE TRIGGER reject_score BEFORE INSERT ON relationships
        BEGIN SELECT RAISE(ABORT, 'injected_relationship_failure'); END""")
    # When / Then
    with pytest.raises(sqlite3.IntegrityError, match="injected_relationship_failure"):
        _ = database.execute(EVENT_SQL, ("e1", "completed", 35))
    assert database.execute("SELECT count(*) FROM memories").fetchall() == [(0,)]
    assert database.execute("SELECT count(*) FROM relationships").fetchall() == [(0,)]


def test_unknown_schema_is_not_overwritten_when_initializing(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "a.db"
    _ = store.MemoryStore.initialize(path, profile())
    with closing(sqlite3.connect(path)) as connection:
        _ = connection.execute("PRAGMA user_version = 2")
    # When / Then
    with pytest.raises(models.StoreError, match="schema_version_mismatch"):
        _ = store.MemoryStore.initialize(path, profile())
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchall() == [(2,)]


def test_changed_profile_cannot_reset_relationships(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "a.db"
    memory = store.MemoryStore.initialize(path, profile())
    _ = memory.append(event())
    modified = models.Profile.model_validate(profile().model_dump() | {"personality": "outgoing"})
    # When / Then
    with pytest.raises(models.StoreError, match="profile_mismatch"):
        _ = store.MemoryStore.initialize(path, modified)
    assert memory.recall(models.RecallRequest(peer_id=event().peer_id)).affinity == 35
