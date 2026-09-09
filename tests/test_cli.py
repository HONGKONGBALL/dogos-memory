"""Separate-process demo verification, including a safe failure path."""

import subprocess
import sys
from pathlib import Path
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict

PROJECT: Final = Path(__file__).parents[1]


class DogSummary(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    affinity: int
    familiar: bool


class DemoSummary(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    mode: str
    dog_a: DogSummary
    dog_b: DogSummary


def run_cli(command: str, directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dogos_memory", command, "--directory", str(directory)],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_two_dog_memories_survive_when_entire_process_restarts(tmp_path: Path) -> None:
    # Given: seed process exits, no in-memory Python objects can carry over.
    seeded = run_cli("demo-seed", tmp_path)
    assert seeded.returncode == 0, seeded.stderr
    # When: a different process reads the database files.
    reopened = run_cli("demo-recall", tmp_path)
    # Then
    assert reopened.returncode == 0, reopened.stderr
    result = DemoSummary.model_validate_json(reopened.stdout)
    assert result.mode == "simulation"
    assert (result.dog_a.affinity, result.dog_b.affinity) == (70, 50)
    assert result.dog_a.familiar
    assert result.dog_b.familiar


def test_demo_replay_does_not_inflate_scores(tmp_path: Path) -> None:
    # Given
    seeded = run_cli("demo-seed", tmp_path)
    assert seeded.returncode == 0, seeded.stderr
    # When
    replayed = run_cli("demo-seed", tmp_path)
    # Then
    assert replayed.returncode == 0, replayed.stderr
    result = DemoSummary.model_validate_json(replayed.stdout)
    assert (result.dog_a.affinity, result.dog_b.affinity) == (70, 50)


def test_read_does_not_create_database_when_directory_is_missing(tmp_path: Path) -> None:
    # Given
    missing = tmp_path / "not_initialized"
    # When
    result = run_cli("demo-recall", missing)
    # Then
    assert result.returncode == 2
    assert "unable to open database file" in result.stderr
    assert "Traceback" not in result.stderr
    assert not missing.exists()
