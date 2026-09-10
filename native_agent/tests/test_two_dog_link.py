"""Regression tests for the physical two-Vbot startup boundary."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = PROJECT_ROOT / "scripts" / "start-two-dog-link.sh"


def _dogos_root(tmp_path: Path, ssh_config: str, first_id: str, second_id: str) -> Path:
    root = tmp_path / "dogos-memory"
    data = root / "data"
    data.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='dogos-test'\nversion='0'\n")
    (root / ".source-revision").write_text("0" * 40 + "\n")
    (data / "two_vbots.ssh_config").write_text(ssh_config)
    (data / "two_vbots.connections.json").write_text(
        json.dumps(
            {
                "timeout_ms": 2000,
                "dogs": [
                    {
                        "dog_id": "dog_a",
                        "adapter_id": "adapter-a",
                        "url": "ws://127.0.0.1:19091",
                        "expected_robot_id": first_id,
                    },
                    {
                        "dog_id": "dog_b",
                        "adapter_id": "adapter-b",
                        "url": "ws://127.0.0.1:19092",
                        "expected_robot_id": second_id,
                    },
                ],
            }
        )
    )
    return root


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DOGOS_MEMORY_SOURCE"] = str(root)
    return subprocess.run(
        [str(START_SCRIPT)],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_rejects_unfilled_physical_pair(tmp_path: Path) -> None:
    root = _dogos_root(
        tmp_path,
        "Host vbot-a\n  HostName REPLACE_WITH_DOG_A_IP\n"
        "Host vbot-b\n  HostName REPLACE_WITH_DOG_B_IP\n",
        "REPLACE_WITH_DOG_A_ID",
        "REPLACE_WITH_DOG_B_ID",
    )

    result = _run(root)

    assert result.returncode == 2
    assert "Fill two physical Vbots" in result.stderr
    assert not (root / "data" / "two_dog_link.pid").exists()


def test_rejects_two_aliases_for_same_host(tmp_path: Path) -> None:
    root = _dogos_root(
        tmp_path,
        "Host vbot-a\n  HostName 192.0.2.10\n  User vbot\n"
        "Host vbot-b\n  HostName 192.0.2.10\n  User vbot\n",
        "vbot-1111111111111111",
        "vbot-2222222222222222",
    )

    result = _run(root)

    assert result.returncode == 2
    assert "same HostName" in result.stderr
    assert not (root / "data" / "two_dog_link.pid").exists()
