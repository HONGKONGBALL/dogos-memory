"""SQL invariants must hold even when the Python writer is bypassed."""

import sqlite3
from typing import Final

import pytest

PROFILE_SQL: Final = """
INSERT INTO profile (singleton, dog_id, name, personality, familiar_threshold, mode)
VALUES (1, 'dog_a', 'A', 'cautious', 60, 'live')
"""
EVENT_SQL: Final = """
INSERT INTO memories
(memory_id, session_id, peer_id, kind, event_type, content, occurred_at_ms,
 recorded_at_ms, source, result, importance, affinity_delta)
VALUES (?, 'round-1', 'dog_b', 'event', 'greeting_completed', 'B greeted me',
 1000, 1000, 'operator', ?, 5, ?)
"""


def test_relationship_changes_when_completed_event_commits(database: sqlite3.Connection) -> None:
    # Given
    _ = database.execute(PROFILE_SQL)
    # When
    _ = database.execute(EVENT_SQL, ("e1", "completed", 35))
    # Then
    assert database.execute("SELECT affinity FROM relationships").fetchall() == [(35,)]


def test_score_is_rejected_when_result_is_unconfirmed(database: sqlite3.Connection) -> None:
    # Given
    _ = database.execute(PROFILE_SQL)
    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        _ = database.execute(EVENT_SQL, ("e1", "unconfirmed", 35))


def test_duplicate_id_is_rejected_when_event_is_replayed(database: sqlite3.Connection) -> None:
    # Given
    _ = database.execute(PROFILE_SQL)
    _ = database.execute(EVENT_SQL, ("e1", "completed", 35))
    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        _ = database.execute(EVENT_SQL, ("e1", "completed", 35))
    assert database.execute("SELECT affinity FROM relationships").fetchall() == [(35,)]


def test_same_text_is_retained_when_interactions_have_distinct_ids(
    database: sqlite3.Connection,
) -> None:
    # Given
    _ = database.execute(PROFILE_SQL)
    _ = database.execute(EVENT_SQL, ("e1", "completed", 35))
    # When
    _ = database.execute(EVENT_SQL, ("e2", "completed", 35))
    # Then
    assert database.execute("SELECT count(*) FROM memories").fetchall() == [(2,)]
    assert database.execute("SELECT affinity FROM relationships").fetchall() == [(70,)]


def test_event_is_immutable_when_updated_directly(database: sqlite3.Connection) -> None:
    # Given
    _ = database.execute(PROFILE_SQL)
    _ = database.execute(EVENT_SQL, ("e1", "completed", 35))
    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        _ = database.execute("UPDATE memories SET content = 'different history'")


def test_simulated_evidence_is_rejected_when_database_is_live(
    database: sqlite3.Connection,
) -> None:
    # Given
    _ = database.execute(PROFILE_SQL)
    simulated_sql = EVENT_SQL.replace("'operator'", "'simulation'")
    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        _ = database.execute(simulated_sql, ("e1", "completed", 35))


def test_database_has_one_owner_when_second_profile_is_added(
    database: sqlite3.Connection,
) -> None:
    # Given
    _ = database.execute(PROFILE_SQL)
    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        _ = database.execute(PROFILE_SQL.replace("'dog_a'", "'dog_b'"))


def test_replace_cannot_bypass_immutable_history(database: sqlite3.Connection) -> None:
    # Given
    _ = database.execute(PROFILE_SQL)
    _ = database.execute(EVENT_SQL, ("e1", "completed", 35))
    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        _ = database.execute(
            EVENT_SQL.replace("INSERT INTO", "INSERT OR REPLACE INTO"), ("e1", "completed", 35)
        )


def test_replace_cannot_change_database_owner(database: sqlite3.Connection) -> None:
    # Given
    _ = database.execute(PROFILE_SQL)
    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        _ = database.execute(
            PROFILE_SQL.replace("INSERT INTO", "INSERT OR REPLACE INTO").replace(
                "'dog_a'", "'dog_b'"
            )
        )
