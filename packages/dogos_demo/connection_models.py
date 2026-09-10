"""Validated boundaries for two fixed Vbot control connections."""

from __future__ import annotations

from enum import unique
from typing import Annotated, ClassVar, Final, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, WebsocketUrl, model_validator
from pydantic_core import PydanticCustomError
from typing_extensions import Self, assert_never

from dogos_demo.models import AdapterId, StringEnum
from dogos_demo.vbot_identity_core import RobotHardwareId
from dogos_memory.models import DogId

RobotPn = NewType("RobotPn", str)
RobotPnText = StringConstraints(
    strip_whitespace=True,
    min_length=19,
    max_length=19,
    pattern=r"^[A-Za-z0-9]{19}$",
)
RobotHardwareIdText = StringConstraints(
    strip_whitespace=True,
    min_length=21,
    max_length=21,
    pattern=r"^vbot-[0-9a-f]{16}$",
)
Identifier = StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
DogIdentifier = StringConstraints(
    strip_whitespace=True,
    min_length=1,
    max_length=64,
    pattern=r"^[a-z][a-z0-9_-]*$",
)
PAIR_SIZE: Final = 2


class ConnectionBoundary(BaseModel):
    """Immutable connection data that rejects unknown fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


@unique
class EndpointConnectionState(StringEnum):
    """Closed outcomes for one read-only identity probe."""

    CONNECTED = "connected"
    UNREACHABLE = "unreachable"
    PROTOCOL_ERROR = "protocol_error"
    IDENTITY_MISMATCH = "identity_mismatch"


@unique
class PairConnectionState(StringEnum):
    """Closed readiness outcomes for the fixed pair."""

    READY = "ready"
    DEGRADED = "degraded"
    ROUTING_CONFLICT = "routing_conflict"
    DUPLICATE_DEVICE = "duplicate_device"


class VbotEndpoint(ConnectionBoundary):
    """One logical dog bound to one tunnel and one physical hardware ID."""

    dog_id: Annotated[DogId, DogIdentifier]
    adapter_id: Annotated[AdapterId, Identifier]
    url: WebsocketUrl
    expected_robot_id: Annotated[RobotHardwareId, RobotHardwareIdText]


class PairConnectionConfig(ConnectionBoundary):
    """Exactly two fixed Vbot endpoints used by the central coordinator."""

    timeout_ms: Annotated[int, Field(strict=True, ge=50, le=30_000)] = 2_000
    dogs: tuple[VbotEndpoint, VbotEndpoint]

    @model_validator(mode="after")
    def check_pair_contract(self) -> Self:
        """Reject aliases that could connect two logical dogs to one route or body."""
        unique_dogs = len({dog.dog_id for dog in self.dogs}) == PAIR_SIZE
        unique_adapters = len({dog.adapter_id for dog in self.dogs}) == PAIR_SIZE
        unique_urls = len({str(dog.url) for dog in self.dogs}) == PAIR_SIZE
        unique_robot_ids = len({dog.expected_robot_id for dog in self.dogs}) == PAIR_SIZE
        if not (unique_dogs and unique_adapters and unique_urls and unique_robot_ids):
            raise PydanticCustomError(
                "pair_connection_contract",
                "Dog IDs, adapters, WebSocket routes, and hardware IDs must all be unique",
            )
        return self


class EndpointConnectionReport(ConnectionBoundary):
    """Observable result of one non-mutating Vbot identity probe."""

    dog_id: DogId
    adapter_id: AdapterId
    url: WebsocketUrl
    state: EndpointConnectionState
    expected_robot_id: RobotHardwareId
    observed_robot_id: RobotHardwareId | None = None
    observed_robot_pn: RobotPn | None = None
    identity_source: Literal["device-tree-serial+eth2-mac-sha256"] | None = None
    probe_service: Literal["/datou/identity"] = "/datou/identity"
    detail: Annotated[str, Identifier]

    @model_validator(mode="after")
    def check_connected_identity(self) -> Self:
        """Prevent an unverified or different body from being labelled connected."""
        match self.state:
            case EndpointConnectionState.CONNECTED:
                valid = self.observed_robot_id == self.expected_robot_id
            case (
                EndpointConnectionState.UNREACHABLE
                | EndpointConnectionState.PROTOCOL_ERROR
                | EndpointConnectionState.IDENTITY_MISMATCH
            ):
                valid = True
            case unreachable:
                assert_never(unreachable)
        if not valid:
            raise PydanticCustomError(
                "endpoint_connection_contract",
                "Connected routes require the configured physical hardware ID",
            )
        return self


class PairConnectionReport(ConnectionBoundary):
    """Readiness report consumed before any two-dog encounter can run."""

    state: PairConnectionState
    dogs: tuple[EndpointConnectionReport, EndpointConnectionReport]
