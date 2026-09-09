"""Process-level checks for the read-only two-Vbot connection gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from threading import Thread
from typing import Final

from pydantic import TypeAdapter
from websockets.sync.server import Server, ServerConnection, serve

from dogos_demo.connection_models import PairConnectionReport, PairConnectionState, RobotPn
from dogos_demo.rosbridge_identity import (
    IdentityServiceCall,
    IdentityServiceResponse,
    IdentityValues,
)
from dogos_demo.vbot_identity_core import RobotHardwareId

PROJECT: Final = Path(__file__).parents[1]
SOCKET_ADDRESS: Final = TypeAdapter(tuple[str, int])


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dogos_demo", *arguments],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def identity_server(robot_id: RobotHardwareId) -> tuple[Server, Thread]:
    def handler(connection: ServerConnection) -> None:
        request = IdentityServiceCall.model_validate_json(connection.recv(timeout=1))
        response = IdentityServiceResponse(
            op="service_response",
            id=request.id,
            service="/datou/identity",
            result=True,
            values=IdentityValues(
                success=True,
                message="hardware identity read",
                robot_id=robot_id,
                identity_source="device-tree-serial+eth2-mac-sha256",
                pn_code=RobotPn("0000000000000000000"),
            ),
        )
        connection.send(response.model_dump_json())

    server = serve(handler, "127.0.0.1", 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_connect_check_reports_both_unreachable_without_sending_motion(
    tmp_path: Path,
) -> None:
    # Given: two fixed, currently unused local tunnel endpoints.
    config_path = tmp_path / "connections.json"
    _ = config_path.write_text(
        json.dumps(
            {
                "timeout_ms": 100,
                "dogs": [
                    {
                        "dog_id": "dog_a",
                        "adapter_id": "adapter-a",
                        "url": "ws://127.0.0.1:49151",
                        "expected_robot_id": "vbot-aaaaaaaaaaaaaaaa",
                    },
                    {
                        "dog_id": "dog_b",
                        "adapter_id": "adapter-b",
                        "url": "ws://127.0.0.1:49152",
                        "expected_robot_id": "vbot-bbbbbbbbbbbbbbbb",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    # When: the operator runs the read-only pair check.
    result = run_cli("connect-check", "--config", str(config_path))

    # Then: both routes are reported, and the pair is not marked ready.
    assert result.returncode == 3, result.stderr
    report = PairConnectionReport.model_validate_json(result.stdout)
    assert report.state == PairConnectionState.DEGRADED
    assert [dog.dog_id for dog in report.dogs] == ["dog_a", "dog_b"]
    assert {dog.state.value for dog in report.dogs} == {"unreachable"}
    assert all(dog.probe_service == "/datou/identity" for dog in report.dogs)


def test_connect_check_returns_ready_for_two_distinct_verified_bodies(tmp_path: Path) -> None:
    # Given: two independent rosbridge endpoints with distinct physical PNs.
    server_a, thread_a = identity_server(RobotHardwareId("vbot-aaaaaaaaaaaaaaaa"))
    server_b, thread_b = identity_server(RobotHardwareId("vbot-bbbbbbbbbbbbbbbb"))
    host_a, port_a = SOCKET_ADDRESS.validate_python(server_a.socket.getsockname())
    host_b, port_b = SOCKET_ADDRESS.validate_python(server_b.socket.getsockname())
    config_path = tmp_path / "connections.json"
    _ = config_path.write_text(
        json.dumps(
            {
                "timeout_ms": 1_000,
                "dogs": [
                    {
                        "dog_id": "dog_a",
                        "adapter_id": "adapter-a",
                        "url": f"ws://{host_a}:{port_a}",
                        "expected_robot_id": "vbot-aaaaaaaaaaaaaaaa",
                    },
                    {
                        "dog_id": "dog_b",
                        "adapter_id": "adapter-b",
                        "url": f"ws://{host_b}:{port_b}",
                        "expected_robot_id": "vbot-bbbbbbbbbbbbbbbb",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        # When: a separate DogOS process performs the pair handshake.
        result = run_cli("connect-check", "--config", str(config_path))
    finally:
        server_a.shutdown()
        server_b.shutdown()
        thread_a.join(timeout=2)
        thread_b.join(timeout=2)

    # Then: the CLI exposes one ready pair with both verified bodies.
    assert result.returncode == 0, result.stderr
    report = PairConnectionReport.model_validate_json(result.stdout)
    assert report.state == PairConnectionState.READY
    assert [dog.observed_robot_id for dog in report.dogs] == [
        "vbot-aaaaaaaaaaaaaaaa",
        "vbot-bbbbbbbbbbbbbbbb",
    ]
    assert {dog.observed_robot_pn for dog in report.dogs} == {"0000000000000000000"}
