"""End-to-end protocol test for the local-only MCP registration probe."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SMOKE_TEST = PROJECT_ROOT / "packages" / "native_agent" / "mcp_probe" / "smoke_test.mjs"


def test_mcp_probe_protocol_smoke() -> None:
    environment = os.environ.copy()
    environment.pop("MCP_PROBE_PORT", None)

    completed = subprocess.run(
        ["node", str(SMOKE_TEST)],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(output_lines) == 1, completed.stdout
    result = json.loads(output_lines[0])
    assert result["ok"] is True
    assert result["endpoint"].startswith("http://127.0.0.1:")
    assert result["endpoint"].endswith("/mcp")
    assert result["tools"] == ["vbot_extension_ping"]
    assert result["result"] == "pong"
    assert result["extraFieldsRejected"] is True
    assert result["oversizedBodyRejected"] is True
    assert result["chunkedOversizedBodyRejected"] is True
    assert result["concurrencyLimited"] is True
    assert result["shutdownWithActiveRequest"] is True
