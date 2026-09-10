"""Read-only Vbot identity probing over the existing rosbridge tunnel."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import StringConstraints, ValidationError
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from dogos_demo.connection_models import (
    ConnectionBoundary,
    EndpointConnectionReport,
    EndpointConnectionState,
    RobotPn,
    RobotPnText,
    VbotEndpoint,
)
from dogos_demo.vbot_identity_core import RobotHardwareId

RequestIdText = StringConstraints(strip_whitespace=True, min_length=1, max_length=200)


class IdentityQuery(ConnectionBoundary):
    """The identity bridge accepts no caller-controlled parameters."""


class IdentityServiceCall(ConnectionBoundary):
    """Typed request for the dedicated non-mutating identity bridge."""

    op: Literal["call_service"] = "call_service"
    id: Annotated[str, RequestIdText]
    service: Literal["/datou/identity"] = "/datou/identity"
    args: IdentityQuery = IdentityQuery()


class IdentityValues(ConnectionBoundary):
    """Validated body identity with the raw eFuse PN kept as diagnostics."""

    success: bool
    message: str
    robot_id: RobotHardwareId
    identity_source: Literal["device-tree-serial+eth2-mac-sha256"]
    pn_code: Annotated[RobotPn, RobotPnText]


class IdentityServiceResponse(ConnectionBoundary):
    """Validated identity bridge response envelope."""

    op: Literal["service_response"]
    id: Annotated[str, RequestIdText]
    service: Literal["/datou/identity"]
    result: bool
    values: IdentityValues


class IdentityProbe(Protocol):
    """Capability required by the fixed-pair connection gate."""

    def probe(self, endpoint: VbotEndpoint, timeout_ms: int) -> EndpointConnectionReport:
        """Query one endpoint without sending a robot action."""
        ...


class VbotIdentityProbe:
    """Open one bounded WebSocket to the dedicated Vbot identity bridge."""

    def probe(self, endpoint: VbotEndpoint, timeout_ms: int) -> EndpointConnectionReport:
        """Query and verify one physical hardware ID without sending an action."""
        timeout_seconds = timeout_ms / 1_000
        request = IdentityServiceCall(id=f"dogos-connect:{endpoint.dog_id}")
        try:
            with connect(
                str(endpoint.url),
                proxy=None,
                open_timeout=timeout_seconds,
                close_timeout=1,
                ping_interval=20,
                ping_timeout=20,
                max_size=2**20,
            ) as connection:
                connection.send(request.model_dump_json())
                response = IdentityServiceResponse.model_validate_json(
                    connection.recv(timeout=timeout_seconds)
                )
                response_accepted = (
                    response.id == request.id and response.result and response.values.success
                )
                if not response_accepted:
                    return EndpointConnectionReport(
                        dog_id=endpoint.dog_id,
                        adapter_id=endpoint.adapter_id,
                        url=endpoint.url,
                        state=EndpointConnectionState.PROTOCOL_ERROR,
                        expected_robot_id=endpoint.expected_robot_id,
                        observed_robot_id=response.values.robot_id,
                        observed_robot_pn=response.values.pn_code,
                        identity_source=response.values.identity_source,
                        detail="identity query failed or response correlation did not match",
                    )
                identity_matches = response.values.robot_id == endpoint.expected_robot_id
                return EndpointConnectionReport(
                    dog_id=endpoint.dog_id,
                    adapter_id=endpoint.adapter_id,
                    url=endpoint.url,
                    state=(
                        EndpointConnectionState.CONNECTED
                        if identity_matches
                        else EndpointConnectionState.IDENTITY_MISMATCH
                    ),
                    expected_robot_id=endpoint.expected_robot_id,
                    observed_robot_id=response.values.robot_id,
                    observed_robot_pn=response.values.pn_code,
                    identity_source=response.values.identity_source,
                    detail=(
                        "read-only identity verified"
                        if identity_matches
                        else "observed hardware ID does not match the fixed route"
                    ),
                )
        except ValidationError as error:
            return EndpointConnectionReport(
                dog_id=endpoint.dog_id,
                adapter_id=endpoint.adapter_id,
                url=endpoint.url,
                state=EndpointConnectionState.PROTOCOL_ERROR,
                expected_robot_id=endpoint.expected_robot_id,
                detail=f"invalid identity response: {error.title}",
            )
        except (OSError, TimeoutError, WebSocketException) as error:
            return EndpointConnectionReport(
                dog_id=endpoint.dog_id,
                adapter_id=endpoint.adapter_id,
                url=endpoint.url,
                state=EndpointConnectionState.UNREACHABLE,
                expected_robot_id=endpoint.expected_robot_id,
                detail=f"tunnel unavailable: {error}",
            )
