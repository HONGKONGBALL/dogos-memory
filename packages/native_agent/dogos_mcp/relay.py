"""Trusted two-Agent text relay backed by a local SQLite outbox.

The model-facing MCP process fixes the sender, recipient, relay path, and both
memory paths in its environment.  Callers can choose only a bounded message ID
and message body.  A recipient acknowledgement writes zero-affinity chat facts
to both DogOS databases before marking the relay item acknowledged.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from typing import Final

_SAFE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_MAX_MESSAGE_CHARS: Final = 1_000
_MAX_INBOX: Final = 20


class RelayError(RuntimeError):
    """Stable relay error safe to return through MCP."""

    def __init__(self, code: str, detail: str) -> None:
        """Store a machine code separately from the public detail."""
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _safe_id(name: str, value: object) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise RelayError("invalid_identifier", f"{name} must match {_SAFE_ID.pattern}")
    return value


def _message(value: object) -> str:
    if not isinstance(value, str):
        raise RelayError("invalid_message", "content must be a string")
    content = value.strip()
    if not content or len(content) > _MAX_MESSAGE_CHARS or "\x00" in content:
        raise RelayError(
            "invalid_message",
            f"content must contain 1-{_MAX_MESSAGE_CHARS} characters and no null byte",
        )
    return content


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_INBOX:
        raise RelayError("invalid_limit", f"limit must be an integer from 1 to {_MAX_INBOX}")
    return value


def _relay_path(value: str | os.PathLike[str]) -> Path:
    path = Path(os.path.abspath(Path(value)))  # noqa: PTH100 - preserve symlinks for rejection
    parent = path.parent
    if not parent.is_dir():
        raise RelayError("relay_parent_missing", "relay database parent directory is missing")
    current = Path(path.anchor)
    try:
        for part in parent.parts[1:]:
            current /= part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise RelayError("relay_symlink_rejected", "relay path contains a symbolic link")
        if path.exists():
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise RelayError("relay_path_rejected", "relay path is not a regular file")
    except OSError as error:
        raise RelayError("relay_path_unavailable", "relay path could not be inspected") from error
    return path


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS relay_messages (
            message_id TEXT PRIMARY KEY,
            sender_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            content TEXT NOT NULL,
            sent_at_ms INTEGER NOT NULL,
            acknowledged_at_ms INTEGER,
            CHECK (sender_id <> recipient_id),
            CHECK (length(content) BETWEEN 1 AND 1000)
        )"""
    )
    return connection


def health(
    relay_database: str | os.PathLike[str],
    owner_id: str,
    peer_id: str,
) -> dict[str, object]:
    """Validate a fixed endpoint and initialize only its relay schema."""
    owner = _safe_id("owner_id", owner_id)
    peer = _safe_id("peer_id", peer_id)
    if owner == peer:
        raise RelayError("peer_is_owner", "owner and peer must differ")
    path = _relay_path(relay_database)
    with closing(_connect(path)) as connection, connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM relay_messages").fetchone()
    return {"owner_id": owner, "peer_id": peer, "relay_ready": True, "message_count": row["count"]}


def send_message(
    relay_database: str | os.PathLike[str],
    owner_id: str,
    peer_id: str,
    *,
    message_id: object,
    content: object,
) -> dict[str, object]:
    """Queue one idempotent owner-to-peer message."""
    path = _relay_path(relay_database)
    owner = _safe_id("owner_id", owner_id)
    peer = _safe_id("peer_id", peer_id)
    identity = _safe_id("message_id", message_id)
    body = _message(content)
    if owner == peer:
        raise RelayError("peer_is_owner", "owner and peer must differ")
    now = time.time_ns() // 1_000_000
    with closing(_connect(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM relay_messages WHERE message_id = ?", (identity,)
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO relay_messages
                (message_id, sender_id, recipient_id, content, sent_at_ms)
                VALUES (?, ?, ?, ?, ?)""",
                (identity, owner, peer, body, now),
            )
            inserted = True
            sent_at_ms = now
        else:
            if (
                existing["sender_id"] != owner
                or existing["recipient_id"] != peer
                or existing["content"] != body
            ):
                raise RelayError("idempotency_conflict", "message ID already has another payload")
            inserted = False
            sent_at_ms = int(existing["sent_at_ms"])
    return {
        "message_id": identity,
        "from": owner,
        "to": peer,
        "state": "queued",
        "inserted": inserted,
        "sent_at_ms": sent_at_ms,
    }


def list_inbox(
    relay_database: str | os.PathLike[str],
    owner_id: str,
    peer_id: str,
    *,
    limit: object,
) -> dict[str, object]:
    """Read bounded messages addressed to this fixed endpoint without acknowledging them."""
    path = _relay_path(relay_database)
    owner = _safe_id("owner_id", owner_id)
    peer = _safe_id("peer_id", peer_id)
    bounded = _limit(limit)
    with closing(_connect(path)) as connection, connection:
        rows = connection.execute(
            """SELECT message_id, sender_id, recipient_id, content, sent_at_ms,
                      acknowledged_at_ms
               FROM relay_messages
               WHERE recipient_id = ? AND sender_id = ?
               ORDER BY sent_at_ms DESC, message_id DESC
               LIMIT ?""",
            (owner, peer, bounded),
        ).fetchall()
    return {
        "owner_id": owner,
        "peer_id": peer,
        "trust": "untrusted_peer_message",
        "messages": [
            {
                "message_id": row["message_id"],
                "content": row["content"],
                "sent_at_ms": row["sent_at_ms"],
                "acknowledged": row["acknowledged_at_ms"] is not None,
            }
            for row in rows
        ],
    }


def acknowledge_message(  # noqa: PLR0913
    relay_database: str | os.PathLike[str],
    owner_database: str | os.PathLike[str],
    peer_database: str | os.PathLike[str],
    owner_id: str,
    peer_id: str,
    *,
    message_id: object,
) -> dict[str, object]:
    """Acknowledge one incoming message and persist delivery in both DogOS views."""
    from dogos_memory.models import (  # noqa: PLC0415
        DogId,
        MemoryId,
        MemoryInput,
        RecallRequest,
        SessionId,
    )
    from dogos_memory.store import MemoryStore  # noqa: PLC0415

    path = _relay_path(relay_database)
    owner = _safe_id("owner_id", owner_id)
    peer = _safe_id("peer_id", peer_id)
    identity = _safe_id("message_id", message_id)
    with closing(_connect(path)) as connection, connection:
        row = connection.execute(
            """SELECT * FROM relay_messages
               WHERE message_id = ? AND recipient_id = ? AND sender_id = ?""",
            (identity, owner, peer),
        ).fetchone()
    if row is None:
        raise RelayError("message_not_found", "no incoming message with this ID")

    session_id = SessionId(f"relay.{identity}")
    memory_id = MemoryId(f"relay.{identity}.delivered")
    sent_at_ms = int(row["sent_at_ms"])
    body = str(row["content"])
    receiver_store = MemoryStore(Path(owner_database), DogId(owner))
    sender_store = MemoryStore(Path(peer_database), DogId(peer))
    receiver_mode = receiver_store.recall(RecallRequest(peer_id=DogId(peer), limit=1)).profile.mode
    sender_mode = sender_store.recall(RecallRequest(peer_id=DogId(owner), limit=1)).profile.mode
    if receiver_mode != sender_mode:
        raise RelayError("memory_mode_mismatch", "the two DogOS databases use different modes")
    completion_source = "simulation" if receiver_mode == "simulation" else "robot_feedback"
    receiver_record = MemoryInput(
        memory_id=memory_id,
        session_id=session_id,
        peer_id=DogId(peer),
        kind="chat",
        event_type="agent_message_received",
        content=body,
        occurred_at_ms=sent_at_ms,
        source=completion_source,
        result="completed",
        affinity_delta=0,
    )
    sender_record = MemoryInput(
        memory_id=memory_id,
        session_id=session_id,
        peer_id=DogId(owner),
        kind="chat",
        event_type="agent_message_delivered",
        content=body,
        occurred_at_ms=sent_at_ms,
        source=completion_source,
        result="completed",
        affinity_delta=0,
    )
    receiver_result = receiver_store.append(receiver_record)
    sender_result = sender_store.append(sender_record)
    acknowledged_at_ms = time.time_ns() // 1_000_000
    with closing(_connect(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE relay_messages
               SET acknowledged_at_ms = COALESCE(acknowledged_at_ms, ?)
               WHERE message_id = ? AND recipient_id = ? AND sender_id = ?""",
            (acknowledged_at_ms, identity, owner, peer),
        )
        final = connection.execute(
            "SELECT acknowledged_at_ms FROM relay_messages WHERE message_id = ?", (identity,)
        ).fetchone()
    return {
        "message_id": identity,
        "state": "acknowledged",
        "receiver_memory_inserted": bool(receiver_result.inserted),
        "sender_memory_inserted": bool(sender_result.inserted),
        "acknowledged_at_ms": int(final["acknowledged_at_ms"]),
    }
