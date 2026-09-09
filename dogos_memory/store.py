"""Per-dog SQLite memory API."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from typing_extensions import Self

from dogos_memory import database, queries
from dogos_memory.models import (
    AppendResult,
    DogId,
    MemoryId,
    MemoryInput,
    Profile,
    RecallRequest,
    RecallResult,
    SessionId,
    StoredMemory,
    StoreError,
)


@dataclass(frozen=True, slots=True)
class MemoryStore:
    """A bound database path/owner; each operation owns a short-lived connection.

    Writers serialize through BEGIN IMMEDIATE. LLM callers should only receive
    recall results and a restricted thought-writing facade, never this writer.
    """

    path: Path
    owner_id: DogId

    @classmethod
    def initialize(cls, path: Path, profile: Profile) -> Self:
        """Create a new experiment or resume exactly the same profile."""
        database.initialize(path, profile)
        return cls(path, profile.dog_id)

    def append(self, memory: MemoryInput) -> AppendResult:
        """Commit a memory, evidence, and score atomically; replay IDs are stable.

        Equal IDs with changed payloads are conflicts, never silent overwrites.
        A controller must assign stable IDs for the same completion callback.
        """
        try:
            with database.connect(self.path) as connection, connection:
                _ = connection.execute("BEGIN IMMEDIATE")
                _ = database.read_profile(connection, self.owner_id)
                previous = queries.find_memory(connection, memory.memory_id)
                if previous is not None:
                    old = MemoryInput.model_validate(
                        previous.model_dump(exclude={"recorded_at_ms"})
                    )
                    old_data = old.model_dump(exclude={"evidence_ids"})
                    new_data = memory.model_dump(exclude={"evidence_ids"})
                    if old_data != new_data or set(old.evidence_ids) != set(memory.evidence_ids):
                        raise StoreError("idempotency_conflict", str(memory.memory_id))
                else:
                    _ = connection.execute(
                        """INSERT INTO memories
                        (memory_id, session_id, peer_id, kind, event_type, content,
                        occurred_at_ms, recorded_at_ms, source, result, importance,
                        affinity_delta, evidence_anchor_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            memory.memory_id,
                            memory.session_id,
                            memory.peer_id,
                            memory.kind,
                            memory.event_type,
                            memory.content,
                            memory.occurred_at_ms,
                            time.time_ns() // 1_000_000,
                            memory.source,
                            memory.result,
                            memory.importance,
                            memory.affinity_delta,
                            memory.evidence_ids[0] if memory.evidence_ids else None,
                        ),
                    )
                    _ = connection.executemany(
                        "INSERT INTO memory_evidence (thought_id, evidence_id) VALUES (?, ?)",
                        [
                            (memory.memory_id, evidence_id)
                            for evidence_id in memory.evidence_ids[1:]
                        ],
                    )
                return AppendResult(
                    memory_id=memory.memory_id,
                    inserted=previous is None,
                    affinity=queries.affinity(connection, memory.peer_id),
                )
        except sqlite3.IntegrityError as error:
            raise StoreError("invalid_memory", str(error)) from error

    def get(self, memory_id: MemoryId) -> StoredMemory:
        """Inspect one stored record, including non-successful outcomes."""
        with database.connect(self.path) as connection, connection:
            _ = connection.execute("BEGIN")
            _ = database.read_profile(connection, self.owner_id)
            memory = queries.find_memory(connection, memory_id)
            if memory is None:
                raise StoreError("memory_not_found", str(memory_id))
            return memory

    def recall(self, request: RecallRequest) -> RecallResult:
        """Take one consistent snapshot; thoughts stay separate from facts."""
        with database.connect(self.path) as connection, connection:
            _ = connection.execute("BEGIN")
            profile = database.read_profile(connection, self.owner_id)
            score = queries.affinity(connection, request.peer_id)
            parameters = (request.peer_id, request.limit)
            return RecallResult(
                profile=profile,
                peer_id=request.peer_id,
                affinity=score,
                familiar=score >= profile.familiar_threshold,
                facts=tuple(
                    StoredMemory.model_validate_json(row)
                    for row in database.json_rows(connection, queries.FACTS, parameters)
                ),
                thoughts=tuple(
                    StoredMemory.model_validate_json(row)
                    for row in database.json_rows(connection, queries.THOUGHTS, parameters)
                ),
            )

    def list_session(self, session_id: SessionId) -> tuple[StoredMemory, ...]:
        """Read all records for one stable session without changing state."""
        with database.connect(self.path) as connection, connection:
            _ = connection.execute("BEGIN")
            _ = database.read_profile(connection, self.owner_id)
            return tuple(
                StoredMemory.model_validate_json(row)
                for row in database.json_rows(connection, queries.SESSION, (session_id,))
            )
