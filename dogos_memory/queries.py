"""Parameterized memory reads and deterministic exact-peer retrieval."""

from __future__ import annotations

import sqlite3
from typing import Final

from dogos_memory.database import INTEGER_ROWS, json_rows
from dogos_memory.models import DogId, MemoryId, StoredMemory

MEMORY_SELECT: Final = """
SELECT json_object(
    'memory_id', m.memory_id, 'session_id', m.session_id, 'peer_id', m.peer_id,
    'kind', m.kind, 'event_type', m.event_type, 'content', m.content,
    'occurred_at_ms', m.occurred_at_ms, 'recorded_at_ms', m.recorded_at_ms,
    'source', m.source, 'result', m.result, 'importance', m.importance,
    'affinity_delta', m.affinity_delta,
    'evidence_ids', json((SELECT json_group_array(evidence_id) FROM (
        SELECT evidence_id FROM memory_evidence
        WHERE thought_id = m.memory_id ORDER BY evidence_id
    )))
) FROM memories AS m
"""
BY_ID: Final = MEMORY_SELECT + " WHERE m.memory_id = ?"
FACTS: Final = (
    MEMORY_SELECT
    + """
 WHERE m.peer_id = ? AND m.kind IN ('event', 'chat') AND m.result = 'completed'
 ORDER BY m.occurred_at_ms DESC, m.memory_id DESC LIMIT ?
"""
)
THOUGHTS: Final = (
    MEMORY_SELECT
    + """
 WHERE m.peer_id = ? AND m.kind = 'thought'
 ORDER BY m.occurred_at_ms DESC, m.memory_id DESC LIMIT ?
"""
)
SESSION: Final = (
    MEMORY_SELECT
    + """
 WHERE m.session_id = ?
 ORDER BY m.occurred_at_ms ASC, m.memory_id ASC
"""
)


def find_memory(connection: sqlite3.Connection, memory_id: MemoryId) -> StoredMemory | None:
    """Read one record including failed/unconfirmed audit events."""
    rows = json_rows(connection, BY_ID, (memory_id,))
    return StoredMemory.model_validate_json(rows[0]) if rows else None


def affinity(connection: sqlite3.Connection, peer_id: DogId) -> int:
    """Unknown peers start at zero without creating a relationship row."""
    rows = INTEGER_ROWS.validate_python(
        connection.execute(
            "SELECT affinity FROM relationships WHERE peer_id = ?", (peer_id,)
        ).fetchall()
    )
    return rows[0][0] if rows else 0
