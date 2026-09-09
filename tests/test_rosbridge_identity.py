"""Wire-level checks for the read-only Vbot hardware identity probe."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from threading import Thread
from typing import ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter
from websockets.sync.server import ServerConnection, serve

from dogos_demo.connection_models import (
    EndpointConnectionReport,
    EndpointConnectionState,
    VbotEndpoint,
)
from dogos_demo.rosbridge_identity import VbotIdentityProbe

BRIDGE_SOURCE: Final = Path(__file__).parents[1] / "robot_side" / "vbot_identity_bridge.py"


class IdentityArgs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class CapturedIdentityRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    op: Literal["call_service"]
    id: str
    service: Literal["/datou/identity"]
    args: IdentityArgs


ReplyFactory = Callable[[CapturedIdentityRequest], str]
SOCKET_ADDRESS: Final = TypeAdapter(tuple[str, int])


def probe_against(
    reply_factory: ReplyFactory,
) -> tuple[EndpointConnectionReport, tuple[CapturedIdentityRequest, ...]]:
    captured: list[CapturedIdentityRequest] = []

    def handler(connection: ServerConnection) -> None:
        request = CapturedIdentityRequest.model_validate_json(connection.recv(timeout=1))
        captured.append(request)
        connection.send(reply_factory(request))

    server = serve(handler, "127.0.0.1", 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = SOCKET_ADDRESS.validate_python(server.socket.getsockname())
    endpoint = VbotEndpoint.model_validate(
        {
            "dog_id": "dog_a",
            "adapter_id": "adapter-a",
            "url": f"ws://{host}:{port}",
            "expected_robot_id": "vbot-a3b739431e6709a2",
        }
    )

    try:
        # When: the route is probed before any encounter action.
        report = VbotIdentityProbe().probe(endpoint, timeout_ms=1_000)
    finally:
        server.shutdown()
        thread.join(timeout=2)
    return report, tuple(captured)


def service_reply(
    request: CapturedIdentityRequest,
    robot_id: str,
    *,
    result: bool = True,
    response_id: str | None = None,
) -> str:
    return json.dumps(
        {
            "op": "service_response",
            "id": request.id if response_id is None else response_id,
            "service": request.service,
            "result": result,
            "values": {
                "success": True,
                "message": "hardware identity read",
                "robot_id": robot_id,
                "identity_source": "device-tree-serial+eth2-mac-sha256",
                "pn_code": "0000000000000000000",
            },
        }
    )


def test_probe_uses_hardware_id_when_efuse_pn_is_zero() -> None:
    # Given: a real WebSocket boundary reporting this unit's all-zero PN.
    def reply(request: CapturedIdentityRequest) -> str:
        return service_reply(request, "vbot-a3b739431e6709a2")

    # When: the route is probed before any encounter action.
    report, captured = probe_against(reply)

    # Then: the physical ID is verified without treating zero PN as identity.
    assert report.state == EndpointConnectionState.CONNECTED
    assert report.observed_robot_id == report.expected_robot_id
    assert report.observed_robot_pn == "0000000000000000000"
    assert len(captured) == 1
    assert captured[0].service == "/datou/identity"
    assert captured[0].args.model_dump() == {}


def test_probe_marks_a_reachable_but_different_robot_as_identity_mismatch() -> None:
    # Given: dog A's fixed tunnel actually reaches dog B's hardware identity.
    def reply(request: CapturedIdentityRequest) -> str:
        return service_reply(request, "vbot-bbbbbbbbbbbbbbbb")

    # When: the route identity is checked.
    report, _captured = probe_against(reply)

    # Then: connectivity isn't mistaken for a safe A route.
    assert report.state == EndpointConnectionState.IDENTITY_MISMATCH
    assert report.observed_robot_id == "vbot-bbbbbbbbbbbbbbbb"


def test_probe_rejects_a_service_level_failure() -> None:
    # Given: the bridge responds, but the identity read reports failure.
    def reply(request: CapturedIdentityRequest) -> str:
        return service_reply(request, "vbot-a3b739431e6709a2", result=False)

    # When: the route identity is checked.
    report, _captured = probe_against(reply)

    # Then: a failed service call isn't treated as connection readiness.
    assert report.state == EndpointConnectionState.PROTOCOL_ERROR


def test_probe_rejects_a_stale_response_for_another_request() -> None:
    # Given: a valid-looking response carries the wrong correlation ID.
    def reply(request: CapturedIdentityRequest) -> str:
        return service_reply(
            request,
            "vbot-a3b739431e6709a2",
            response_id="stale-request",
        )

    # When: the route identity is checked.
    report, _captured = probe_against(reply)

    # Then: stale traffic cannot validate the physical route.
    assert report.state == EndpointConnectionState.PROTOCOL_ERROR


def test_probe_reports_malformed_identity_json_as_a_protocol_error() -> None:
    # Given: an endpoint accepts WebSocket but doesn't implement the schema.
    def reply(_request: CapturedIdentityRequest) -> str:
        return "{}"

    # When: the malformed endpoint is checked.
    report, _captured = probe_against(reply)

    # Then: the checker returns a bounded failure instead of crashing.
    assert report.state == EndpointConnectionState.PROTOCOL_ERROR


def test_robot_identity_bridge_exposes_no_motion_or_write_surface() -> None:
    # Given: the exact dedicated identity bridge deployed on each Vbot.
    source = BRIDGE_SOURCE.read_text(encoding="utf-8")

    # When/Then: it reads operation zero on loopback port 9092 and exposes no control path.
    assert '"/x5/efuse"' in source or "'/x5/efuse'" in source
    assert "request.operation = 0" in source
    assert 'request.pn_code = ""' in source or "request.pn_code = ''" in source
    assert 'IDENTITY_HOST: Final = "127.0.0.1"' in source
    assert "IDENTITY_PORT: Final = 9092" in source
    assert "/vel_cmd" not in source
    assert "/head_action" not in source
    assert "/sm/action/lowlevel" not in source
