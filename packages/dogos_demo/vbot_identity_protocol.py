"""Dependency-free wire contract used by the robot-side identity bridge."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final, final

if TYPE_CHECKING:
    from dogos_demo.vbot_identity_core import RobotHardwareId

IDENTITY_SOURCE: Final = "device-tree-serial+eth2-mac-sha256"
_REQUEST_PATTERN: Final = re.compile(
    r"""
    ^\s*\{\s*"op"\s*:\s*"call_service"\s*,
    \s*"id"\s*:\s*"(?P<request_id>[A-Za-z0-9:_-]{1,200})"\s*,
    \s*"service"\s*:\s*"/datou/identity"\s*,
    \s*"args"\s*:\s*\{\s*\}\s*\}\s*$
    """,
    re.VERBOSE,
)


@final
class IdentityProtocolError(Exception):
    """Untrusted WebSocket input is outside the identity-only contract."""

    __slots__: ClassVar[tuple[str, ...]] = ("detail",)

    detail: str

    def __init__(self, detail: str) -> None:
        """Store one bounded protocol rejection detail."""
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class IdentityRequest:
    """Parsed identity request containing only a response correlation ID."""

    request_id: str


@dataclass(frozen=True, slots=True)
class IdentitySnapshot:
    """Immutable body identity captured before the WebSocket server starts."""

    robot_id: RobotHardwareId
    pn_code: str


def parse_identity_request(raw: str) -> IdentityRequest:
    """Parse the one permitted identity request from untrusted JSON."""
    matched = _REQUEST_PATTERN.fullmatch(raw)
    if matched is None:
        raise IdentityProtocolError("identity-request-only")
    return IdentityRequest(request_id=matched.group("request_id"))


def build_identity_response(request_id: str, snapshot: IdentitySnapshot) -> str:
    """Serialize a successful read-only hardware identity response."""
    return json.dumps(
        {
            "op": "service_response",
            "id": request_id,
            "service": "/datou/identity",
            "result": True,
            "values": {
                "success": True,
                "message": "hardware identity read",
                "robot_id": snapshot.robot_id,
                "identity_source": IDENTITY_SOURCE,
                "pn_code": snapshot.pn_code,
            },
        },
        allow_nan=False,
        separators=(",", ":"),
    )
