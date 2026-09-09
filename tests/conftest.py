"""Real SQLite fixtures; no mocked persistence."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest


@pytest.fixture
def database(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(tmp_path / "test.db")) as connection:
        _ = connection.execute("PRAGMA foreign_keys = ON")
        schema = Path(__file__).parents[1] / "dogos_memory" / "schema.sql"
        _ = connection.executescript(schema.read_text(encoding="utf-8"))
        yield connection
