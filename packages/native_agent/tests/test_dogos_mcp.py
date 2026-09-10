"""Full MCP protocol test for the two fixed DogOS endpoints."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SMOKE_TEST = PROJECT_ROOT / "packages" / "native_agent" / "dogos_mcp" / "smoke_test.mjs"
DEFAULT_DOGOS = PROJECT_ROOT


def test_two_endpoint_message_and_memory_round_trip() -> None:
    """A and B exchange and persist one bounded message through real MCP clients."""
    environment = os.environ.copy()
    environment["DOGOS_MEMORY_SOURCE"] = str(
        Path(environment.get("DOGOS_MEMORY_SOURCE", DEFAULT_DOGOS))
    )
    node = shutil.which("node")
    assert node is not None  # noqa: S101
    result = subprocess.run(  # noqa: S603
        [node, str(SMOKE_TEST)],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert json.loads(result.stdout) == {  # noqa: S101
        "ok": True,
        "endpoints": 2,
        "exchanged": "smoke-1",
        "persistedViews": 2,
    }
