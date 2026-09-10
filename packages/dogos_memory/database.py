"""SQLite connections and non-destructive schema initialization."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter

from dogos_memory.models import DogId, Profile, StoreError

if TYPE_CHECKING:
    from collections.abc import Generator

SCHEMA_VERSION: Final = 1
JSON_ROWS: Final = TypeAdapter(list[tuple[str]])
INTEGER_ROWS: Final = TypeAdapter(list[tuple[int]])
PROFILE_QUERY: Final = """
SELECT json_object('dog_id', dog_id, 'name', name, 'personality', personality,
    'familiar_threshold', familiar_threshold, 'mode', mode) FROM profile
"""


@contextmanager
def connect(path: Path, *, create: bool = False) -> Generator[sqlite3.Connection, None, None]:
    """Close every connection; normal reads/writes never create missing files."""
    target = str(path) if create else path.resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(target, timeout=5.0, uri=not create)) as connection:
        _ = connection.execute("PRAGMA foreign_keys = ON")
        _ = connection.execute("PRAGMA synchronous = FULL")
        yield connection


def json_rows(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[str | int, ...] = (),
) -> tuple[str, ...]:
    """Parse driver output exactly once; callers receive typed JSON strings."""
    rows = JSON_ROWS.validate_python(connection.execute(sql, parameters).fetchall())
    return tuple(row[0] for row in rows)


def check_schema(connection: sqlite3.Connection) -> None:
    """Refuse unknown schema versions instead of silently migrating them."""
    rows = INTEGER_ROWS.validate_python(connection.execute("PRAGMA user_version").fetchall())
    if rows[0][0] != SCHEMA_VERSION:
        raise StoreError("schema_version_mismatch", str(rows[0][0]))


def read_profile(connection: sqlite3.Connection, owner_id: DogId) -> Profile:
    """Check the bound identity on every operation, not just initialization."""
    check_schema(connection)
    rows = json_rows(connection, PROFILE_QUERY)
    if not rows:
        raise StoreError("profile_missing", str(owner_id))
    profile = Profile.model_validate_json(rows[0])
    if profile.dog_id != owner_id:
        raise StoreError("owner_mismatch", f"expected {owner_id}, found {profile.dog_id}")
    return profile


def initialize(path: Path, profile: Profile) -> None:
    """Create or reopen a matching experiment, without erasing existing state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path, create=True) as connection:
        version = INTEGER_ROWS.validate_python(
            connection.execute("PRAGMA user_version").fetchall()
        )[0][0]
        if version == 0:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if tables:
                raise StoreError("unrecognized_database", str(path))
            _ = connection.executescript(
                Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            )
        check_schema(connection)
        _ = connection.execute("PRAGMA journal_mode = WAL")
        with connection:
            _ = connection.execute("BEGIN IMMEDIATE")
            existing = json_rows(connection, PROFILE_QUERY)
            if existing:
                if Profile.model_validate_json(existing[0]) != profile:
                    raise StoreError("profile_mismatch", str(profile.dog_id))
            else:
                _ = connection.execute(
                    """INSERT INTO profile
                    (singleton, dog_id, name, personality, familiar_threshold, mode)
                    VALUES (1, ?, ?, ?, ?, ?)""",
                    (
                        profile.dog_id,
                        profile.name,
                        profile.personality,
                        profile.familiar_threshold,
                        profile.mode,
                    ),
                )
