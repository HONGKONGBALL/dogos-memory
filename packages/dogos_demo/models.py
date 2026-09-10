"""Validated identity and runtime configuration boundaries."""

from __future__ import annotations

from enum import Enum, unique
from typing import Annotated, ClassVar, Final, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError
from typing_extensions import Self, assert_never, override

from dogos_memory.models import DogId, Timestamp

AdapterId = NewType("AdapterId", str)
TagFamily = NewType("TagFamily", str)
Identifier = StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
DogIdentifier = StringConstraints(
    strip_whitespace=True,
    min_length=1,
    max_length=64,
    pattern=r"^[a-z][a-z0-9_-]*$",
)
TagNumber = Annotated[int, Field(strict=True, ge=0, le=65_535)]
PAIR_SIZE: Final = 2


class BoundaryModel(BaseModel):
    """Immutable input/output model that rejects unknown fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class StringEnum(str, Enum):
    """Python 3.10-compatible string enum for the Vbot runtime."""


@unique
class DogRole(StringEnum):
    """Fixed roles used by the deterministic hackathon policy."""

    CAUTIOUS = "cautious"
    OUTGOING = "outgoing"


@unique
class IdentitySource(StringEnum):
    """Evidence source for encounter identity, not action completion."""

    OPERATOR = "operator"
    APRILTAG = "apriltag"


@unique
class IdentityStatus(StringEnum):
    """Closed encounter-gate outcomes."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPPRESSED = "suppressed"


@unique
class IdentityReason(StringEnum):
    """Machine-readable reasons shown by the demo console."""

    STABILIZING = "stabilizing"
    CONFIRMED = "confirmed"
    NO_DETECTION = "no_detection"
    LOW_CONFIDENCE = "low_confidence"
    UNKNOWN_TAG = "unknown_tag"
    SELF_TAG = "self_tag"
    CAMERA_CONFLICT = "camera_conflict"
    OUT_OF_ORDER = "out_of_order"
    OWNER_MISMATCH = "owner_mismatch"
    PEER_NOT_CONFIGURED = "peer_not_configured"
    COOLDOWN = "cooldown"


class DemoError(RuntimeError):
    """Typed configuration or orchestration error."""

    code: str
    detail: str

    def __init__(self, code: str, detail: str) -> None:
        """Store stable machine and human-readable error details."""
        self.code = code
        self.detail = detail
        super().__init__(code, detail)

    @override
    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class DogConfig(BoundaryModel):
    """One dog's immutable identity, route, and demo relationship rule."""

    dog_id: Annotated[DogId, DogIdentifier]
    name: Annotated[str, Identifier]
    role: DogRole
    adapter_id: Annotated[AdapterId, Identifier]
    tag_family: Annotated[TagFamily, Identifier]
    tag_id: TagNumber
    familiar_threshold: Annotated[int, Field(strict=True, ge=1, le=100)]
    affinity_gain: Annotated[int, Field(strict=True, ge=1, le=100)]


class DemoConfig(BoundaryModel):
    """Exactly two dogs with non-ambiguous IDs, routes, roles, and tags."""

    mode: Literal["live", "simulation"]
    dogs: tuple[DogConfig, DogConfig]
    stable_frames: Annotated[int, Field(strict=True, ge=1, le=10)] = 3
    min_decision_margin: Annotated[float, Field(strict=True, ge=0, le=1_000_000)] = 25.0
    max_frame_gap_ms: Annotated[int, Field(strict=True, ge=1, le=10_000)] = 200
    encounter_cooldown_ms: Annotated[int, Field(strict=True, ge=0, le=3_600_000)] = 1_000

    @model_validator(mode="after")
    def check_pair_contract(self) -> Self:
        """Reject every configuration that could alias identity or routing."""
        unique_ids = len({dog.dog_id for dog in self.dogs}) == PAIR_SIZE
        unique_adapters = len({dog.adapter_id for dog in self.dogs}) == PAIR_SIZE
        unique_tags = len({(dog.tag_family, dog.tag_id) for dog in self.dogs}) == PAIR_SIZE
        roles = {dog.role for dog in self.dogs}
        if not (
            unique_ids
            and unique_adapters
            and unique_tags
            and roles == {DogRole.CAUTIOUS, DogRole.OUTGOING}
        ):
            raise PydanticCustomError(
                "pair_contract",
                "Dog IDs, adapters, tags, and roles conflict",
            )
        return self

    def find_dog(self, dog_id: DogId) -> DogConfig | None:
        """Return one configured dog without inventing unknown identities."""
        return next((dog for dog in self.dogs if dog.dog_id == dog_id), None)

    def find_tag(self, family: TagFamily, tag_id: int) -> DogConfig | None:
        """Resolve a configured family/id pair; AprilTag IDs are not hashes."""
        return next(
            (dog for dog in self.dogs if dog.tag_family == family and dog.tag_id == tag_id),
            None,
        )


class TagDetection(BoundaryModel):
    """One camera's already-decoded AprilTag observation."""

    family: Annotated[TagFamily, Identifier]
    tag_id: TagNumber
    decision_margin: Annotated[float, Field(strict=True, ge=0, le=1_000_000)]
    valid: bool = True


class TagFrame(BoundaryModel):
    """Synchronized identity evidence from the Vbot left/right detections."""

    owner_id: Annotated[DogId, Identifier]
    observed_at_ms: Timestamp
    left: TagDetection | None
    right: TagDetection | None


class ManualEncounter(BoundaryModel):
    """Explicit operator identity fallback for a controlled demo."""

    owner_id: Annotated[DogId, Identifier]
    peer_id: Annotated[DogId, Identifier]
    observed_at_ms: Timestamp


class IdentityDecision(BoundaryModel):
    """Validated output of the encounter gate."""

    status: IdentityStatus
    reason: IdentityReason
    owner_id: DogId
    peer_id: DogId | None = None
    source: IdentitySource | None = None
    observed_at_ms: Timestamp
    stable_frames: Annotated[int, Field(strict=True, ge=0, le=10)] = 0

    @model_validator(mode="after")
    def check_decision_contract(self) -> Self:
        """Keep confirmed, pending, rejected, and suppressed states distinct."""
        match self.status:
            case IdentityStatus.CONFIRMED:
                valid = (
                    self.reason == IdentityReason.CONFIRMED
                    and self.peer_id is not None
                    and self.source is not None
                    and self.stable_frames > 0
                )
            case IdentityStatus.PENDING:
                valid = (
                    self.reason == IdentityReason.STABILIZING
                    and self.peer_id is not None
                    and self.source == IdentitySource.APRILTAG
                    and self.stable_frames > 0
                )
            case IdentityStatus.REJECTED:
                valid = self.peer_id is None and self.source is None and self.stable_frames == 0
            case IdentityStatus.SUPPRESSED:
                valid = (
                    self.reason == IdentityReason.COOLDOWN
                    and self.peer_id is not None
                    and self.source is not None
                    and self.stable_frames == 0
                )
            case unreachable:
                assert_never(unreachable)
        if not valid:
            raise PydanticCustomError("identity_decision", "Invalid identity decision state")
        return self
