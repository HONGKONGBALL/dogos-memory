"""Stateful, bounded encounter confirmation for manual or AprilTag input."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias, final

from typing_extensions import assert_never

from dogos_demo.models import (
    DemoConfig,
    DemoError,
    IdentityDecision,
    IdentityReason,
    IdentitySource,
    IdentityStatus,
    ManualEncounter,
    TagDetection,
    TagFrame,
)

if TYPE_CHECKING:
    from dogos_memory.models import DogId


@dataclass(frozen=True, slots=True)
class ResolvedPeer:
    """One unambiguous, confident peer from a stereo frame."""

    peer_id: DogId


@dataclass(frozen=True, slots=True)
class RejectedFrame:
    """One frame-level rejection before stability accumulation."""

    reason: IdentityReason


FrameResolution: TypeAlias = ResolvedPeer | RejectedFrame


@final
class EncounterGate:
    """Accumulate short-lived Tag evidence and suppress duplicate encounters.

    This object is intentionally mutable: it owns frame-stability and cooldown
    state for one running owner process. Durable relationship state lives in
    SQLite and is not duplicated here.
    """

    def __init__(self, config: DemoConfig, owner_id: DogId) -> None:
        """Create one in-memory gate for a configured owner."""
        if config.find_dog(owner_id) is None:
            raise DemoError("owner_not_configured", str(owner_id))
        self._config = config
        self._owner_id = owner_id
        self._candidate_id: DogId | None = None
        self._stable_frames = 0
        self._last_frame_ms: int | None = None
        self._last_confirmed_id: DogId | None = None
        self._last_confirmed_ms: int | None = None

    def observe(self, frame: TagFrame) -> IdentityDecision:
        """Consume one left/right Tag frame and return an explicit gate state."""
        if frame.owner_id != self._owner_id:
            return self._reject(IdentityReason.OWNER_MISMATCH, frame.observed_at_ms)
        clock_rejection = self._advance_frame_clock(frame.observed_at_ms)
        if clock_rejection is not None:
            return self._reject(clock_rejection, frame.observed_at_ms)
        match self._resolve_frame(frame):
            case RejectedFrame(reason=reason):
                return self._reject(reason, frame.observed_at_ms)
            case ResolvedPeer(peer_id=peer_id):
                return self._accumulate(peer_id, frame.observed_at_ms)
            case unreachable:
                assert_never(unreachable)
        raise DemoError("identity_resolution_unreachable", str(frame.observed_at_ms))

    def _resolve_frame(self, frame: TagFrame) -> FrameResolution:
        detections = tuple(
            detection
            for detection in (frame.left, frame.right)
            if detection is not None and detection.valid
        )
        if not detections:
            return RejectedFrame(IdentityReason.NO_DETECTION)

        confident = tuple(
            detection
            for detection in detections
            if detection.decision_margin >= self._config.min_decision_margin
        )
        if not confident:
            return RejectedFrame(IdentityReason.LOW_CONFIDENCE)

        resolved = tuple(self._resolve(detection) for detection in confident)
        if any(dog_id is None for dog_id in resolved):
            return RejectedFrame(IdentityReason.UNKNOWN_TAG)
        known = tuple(dog_id for dog_id in resolved if dog_id is not None)
        if len(set(known)) != 1:
            return RejectedFrame(IdentityReason.CAMERA_CONFLICT)

        peer_id = known[0]
        if peer_id == self._owner_id:
            return RejectedFrame(IdentityReason.SELF_TAG)
        return ResolvedPeer(peer_id)

    def _accumulate(self, peer_id: DogId, observed_at_ms: int) -> IdentityDecision:
        if peer_id != self._candidate_id:
            self._candidate_id = peer_id
            self._stable_frames = 0
        self._stable_frames += 1
        if self._stable_frames < self._config.stable_frames:
            return IdentityDecision(
                status=IdentityStatus.PENDING,
                reason=IdentityReason.STABILIZING,
                owner_id=self._owner_id,
                peer_id=peer_id,
                source=IdentitySource.APRILTAG,
                observed_at_ms=observed_at_ms,
                stable_frames=self._stable_frames,
            )
        return self._confirm(peer_id, observed_at_ms, IdentitySource.APRILTAG)

    def confirm_manual(self, encounter: ManualEncounter) -> IdentityDecision:
        """Confirm a known pair immediately while preserving cooldown safety."""
        if encounter.owner_id != self._owner_id:
            return self._reject(IdentityReason.OWNER_MISMATCH, encounter.observed_at_ms)
        if encounter.peer_id == self._owner_id:
            return self._reject(IdentityReason.SELF_TAG, encounter.observed_at_ms)
        if self._config.find_dog(encounter.peer_id) is None:
            return self._reject(IdentityReason.PEER_NOT_CONFIGURED, encounter.observed_at_ms)
        return self._confirm(
            encounter.peer_id,
            encounter.observed_at_ms,
            IdentitySource.OPERATOR,
        )

    def _resolve(self, detection: TagDetection) -> DogId | None:
        dog = self._config.find_tag(detection.family, detection.tag_id)
        return None if dog is None else dog.dog_id

    def _advance_frame_clock(self, observed_at_ms: int) -> IdentityReason | None:
        if self._last_frame_ms is not None and observed_at_ms <= self._last_frame_ms:
            return IdentityReason.OUT_OF_ORDER
        gap_exceeded = (
            self._last_frame_ms is not None
            and observed_at_ms - self._last_frame_ms > self._config.max_frame_gap_ms
        )
        self._last_frame_ms = observed_at_ms
        if gap_exceeded:
            self._clear_stability()
        return None

    def _confirm(
        self,
        peer_id: DogId,
        observed_at_ms: int,
        source: IdentitySource,
    ) -> IdentityDecision:
        within_cooldown = (
            self._last_confirmed_id == peer_id
            and self._last_confirmed_ms is not None
            and observed_at_ms - self._last_confirmed_ms < self._config.encounter_cooldown_ms
        )
        stable_frames = max(1, self._stable_frames)
        self._clear_stability()
        if within_cooldown:
            return IdentityDecision(
                status=IdentityStatus.SUPPRESSED,
                reason=IdentityReason.COOLDOWN,
                owner_id=self._owner_id,
                peer_id=peer_id,
                source=source,
                observed_at_ms=observed_at_ms,
            )
        self._last_confirmed_id = peer_id
        self._last_confirmed_ms = observed_at_ms
        return IdentityDecision(
            status=IdentityStatus.CONFIRMED,
            reason=IdentityReason.CONFIRMED,
            owner_id=self._owner_id,
            peer_id=peer_id,
            source=source,
            observed_at_ms=observed_at_ms,
            stable_frames=stable_frames,
        )

    def _reject(self, reason: IdentityReason, observed_at_ms: int) -> IdentityDecision:
        self._clear_stability()
        return IdentityDecision(
            status=IdentityStatus.REJECTED,
            reason=reason,
            owner_id=self._owner_id,
            observed_at_ms=observed_at_ms,
        )

    def _clear_stability(self) -> None:
        self._candidate_id = None
        self._stable_frames = 0
