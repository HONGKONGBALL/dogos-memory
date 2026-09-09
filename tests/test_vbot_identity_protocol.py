"""Protocol checks for the dependency-free robot-side identity bridge."""

from __future__ import annotations

import json
from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel, ConfigDict

from dogos_demo.vbot_identity_core import RobotHardwareId
from dogos_demo.vbot_identity_protocol import (
    IdentityProtocolError,
    IdentitySnapshot,
    build_identity_response,
    parse_identity_request,
)


class IdentityValues(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    success: Literal[True]
    message: Literal["hardware identity read"]
    robot_id: str
    identity_source: Literal["device-tree-serial+eth2-mac-sha256"]
    pn_code: str


class IdentityResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    op: Literal["service_response"]
    id: str
    service: Literal["/datou/identity"]
    result: Literal[True]
    values: IdentityValues


def test_identity_protocol_accepts_only_the_empty_read_request() -> None:
    # Given: the single operation exposed by the robot-side identity bridge.
    raw = json.dumps(
        {
            "op": "call_service",
            "id": "connect:dog-a",
            "service": "/datou/identity",
            "args": {},
        }
    )

    # When: untrusted WebSocket JSON crosses the parser boundary.
    request = parse_identity_request(raw)

    # Then: callers receive a typed request containing only its correlation ID.
    assert request.request_id == "connect:dog-a"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "op": "call_service",
            "id": "connect:dog-a",
            "service": "/x5/efuse",
            "args": {"operation": 0, "pn_code": ""},
        },
        {
            "op": "call_service",
            "id": "connect:dog-a",
            "service": "/datou/identity",
            "args": {"operation": 0},
        },
    ],
)
def test_identity_protocol_rejects_every_non_identity_operation(
    payload: dict[str, str | dict[str, int | str]],
) -> None:
    # Given: an attempt to expand the read-only identity surface.
    raw = json.dumps(payload)

    # When/Then: the parser rejects it before robot state is consulted.
    with pytest.raises(IdentityProtocolError):
        _ = parse_identity_request(raw)


def test_identity_response_preserves_zero_pn_as_diagnostic_not_identity() -> None:
    # Given: this Vbot has an unprogrammed all-zero eFuse PN and a stable hardware ID.
    snapshot = IdentitySnapshot(
        robot_id=RobotHardwareId("vbot-a3b739431e6709a2"),
        pn_code="0000000000000000000",
    )

    # When: the identity bridge creates its one permitted response.
    raw = build_identity_response("connect:dog-a", snapshot)

    # Then: the hardware ID and actual PN remain separate, explicit fields.
    response = IdentityResponse.model_validate_json(raw)
    assert response.values.robot_id == "vbot-a3b739431e6709a2"
    assert response.values.pn_code == "0000000000000000000"
