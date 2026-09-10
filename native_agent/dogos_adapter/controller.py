"""Controller-only DogOS write boundary.

This module must not be registered as an Agent tool provider. Its caller must
authenticate terminal robot callbacks and explicit operator confirmations
before invoking :func:`record_confirmed_interaction`.
"""

from __future__ import annotations

import os
from typing import Final, Literal

from .adapter import (
    DogOSAdapterError,
    _backend_error,
    _database_file,
    _identifier,
    _load_dogos,
    _strict_int,
    _text,
)

CompletionStatus = Literal["confirmed", "completed"]

_COMPLETION_SOURCES: Final[dict[str, str]] = {
    "confirmed": "operator",
    "completed": "robot_feedback",
}
_MAX_TIMESTAMP: Final = 9_223_372_036_854_775_807


def record_confirmed_interaction(
    database_path: str | os.PathLike[str],
    owner_id: str,
    *,
    memory_id: str,
    session_id: str,
    peer_id: str,
    event_type: str,
    content: str,
    occurred_at_ms: int,
    completion_status: CompletionStatus,
    affinity_delta: int = 0,
    importance: int = 5,
) -> dict[str, object]:
    """Record one authenticated terminal interaction from a controller."""

    if completion_status not in _COMPLETION_SOURCES:
        raise DogOSAdapterError(
            "interaction_not_completed",
            "completion_status must be confirmed or completed",
        )

    path = _database_file(database_path)
    owner = _identifier("owner_id", owner_id)
    peer = _identifier("peer_id", peer_id)
    if owner == peer:
        raise DogOSAdapterError("peer_is_owner", peer)

    memory = _identifier("memory_id", memory_id)
    session = _identifier("session_id", session_id)
    event = _text("event_type", event_type)
    description = _text("content", content)
    timestamp = _strict_int(
        "occurred_at_ms", occurred_at_ms, minimum=0, maximum=_MAX_TIMESTAMP
    )
    score = _strict_int("affinity_delta", affinity_delta, minimum=0, maximum=100)
    ranked_importance = _strict_int("importance", importance, minimum=1, maximum=10)

    models, store = _load_dogos()
    try:
        observation = models.MemoryInput.model_validate(
            {
                "memory_id": models.MemoryId(memory),
                "session_id": models.SessionId(session),
                "peer_id": models.DogId(peer),
                "kind": "event",
                "event_type": event,
                "content": description,
                "occurred_at_ms": timestamp,
                "source": _COMPLETION_SOURCES[completion_status],
                "result": "completed",
                "importance": ranked_importance,
                "affinity_delta": score,
                "evidence_ids": (),
            }
        )
        result = store.MemoryStore(path, models.DogId(owner)).append(observation)
        response = {
            "memory_id": str(result.memory_id),
            "inserted": bool(result.inserted),
            "affinity": int(result.affinity),
            "completion_status": completion_status,
        }
    except Exception as error:
        raise _backend_error(error) from None

    return response


__all__ = ["CompletionStatus", "record_confirmed_interaction"]
