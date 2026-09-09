"""Separate-process acceptance test for the three-minute software demo."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

from dogos_demo.session_models import ActionName, PairSnapshot, SessionSummary

PROJECT: Final = Path(__file__).parents[1]


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dogos_demo", *arguments],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def encounter(directory: Path, session_id: str, observed_at_ms: int) -> SessionSummary:
    result = run_cli(
        "encounter",
        "--directory",
        str(directory),
        "--session-id",
        session_id,
        "--observed-at-ms",
        str(observed_at_ms),
    )
    assert result.returncode == 0, result.stderr
    return SessionSummary.model_validate_json(result.stdout)


def test_two_rounds_restart_and_reunion_change_behavior_without_duplicate_scoring(
    tmp_path: Path,
) -> None:
    first = encounter(tmp_path, "encounter-1", 1_000)
    second = encounter(tmp_path, "encounter-2", 3_000)

    assert (first.after.cautious.affinity, first.after.outgoing.affinity) == (35, 25)
    assert (second.after.cautious.affinity, second.after.outgoing.affinity) == (70, 50)

    reopened = run_cli("status", "--directory", str(tmp_path))
    assert reopened.returncode == 0, reopened.stderr
    status = PairSnapshot.model_validate_json(reopened.stdout)
    assert status.mode == "simulation"
    assert (status.cautious.affinity, status.outgoing.affinity) == (70, 50)

    reunion = encounter(tmp_path, "encounter-3", 5_000)
    assert reunion.commands[0].command.action == ActionName.FAMILIAR_GREETING
    assert reunion.commands[0].command.memory_reference_id is not None
    assert (reunion.after.cautious.affinity, reunion.after.outgoing.affinity) == (100, 75)

    replay = encounter(tmp_path, "encounter-3", 6_000)
    assert all(report.replayed for report in replay.commands)
    assert replay.after == reunion.after


def test_status_does_not_create_missing_databases(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    result = run_cli("status", "--directory", str(missing))

    assert result.returncode == 2
    assert "unable to open database file" in result.stderr
    assert "Traceback" not in result.stderr
    assert not missing.exists()
