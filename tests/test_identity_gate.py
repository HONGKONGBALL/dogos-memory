"""Identity boundary behavior for manual and AprilTag encounters."""

from __future__ import annotations

from dogos_demo.identity import EncounterGate
from dogos_demo.models import (
    IdentityReason,
    IdentitySource,
    IdentityStatus,
    ManualEncounter,
    TagDetection,
    TagFamily,
    TagFrame,
)
from dogos_memory.models import DogId
from tests.demo_factories import demo_config


def tag(tag_id: int, *, margin: float = 50.0) -> TagDetection:
    return TagDetection(
        family=TagFamily("tag36h11"),
        tag_id=tag_id,
        decision_margin=margin,
    )


def frame(
    observed_at_ms: int,
    *,
    left: TagDetection | None,
    right: TagDetection | None,
) -> TagFrame:
    return TagFrame(
        owner_id=DogId("dog_a"),
        observed_at_ms=observed_at_ms,
        left=left,
        right=right,
    )


def test_apriltag_requires_three_consistent_frames_before_confirmation() -> None:
    gate = EncounterGate(demo_config(), DogId("dog_a"))

    first = gate.observe(frame(1_000, left=tag(2), right=tag(2)))
    second = gate.observe(frame(1_100, left=tag(2), right=tag(2)))
    third = gate.observe(frame(1_200, left=tag(2), right=tag(2)))

    assert (first.status, first.stable_frames) == (IdentityStatus.PENDING, 1)
    assert (second.status, second.stable_frames) == (IdentityStatus.PENDING, 2)
    assert third.status == IdentityStatus.CONFIRMED
    assert third.peer_id == DogId("dog_b")
    assert third.source == IdentitySource.APRILTAG


def test_frame_gap_resets_stability_instead_of_combining_stale_evidence() -> None:
    gate = EncounterGate(demo_config(), DogId("dog_a"))

    _ = gate.observe(frame(1_000, left=tag(2), right=None))
    decision = gate.observe(frame(1_500, left=tag(2), right=None))

    assert decision.status == IdentityStatus.PENDING
    assert decision.reason == IdentityReason.STABILIZING
    assert decision.stable_frames == 1


def test_conflicting_cameras_reject_and_clear_previous_stability() -> None:
    gate = EncounterGate(demo_config(), DogId("dog_a"))

    _ = gate.observe(frame(1_000, left=tag(2), right=None))
    conflict = gate.observe(frame(1_100, left=tag(2), right=tag(1)))
    after = gate.observe(frame(1_200, left=tag(2), right=None))

    assert conflict.status == IdentityStatus.REJECTED
    assert conflict.reason == IdentityReason.CAMERA_CONFLICT
    assert after.stable_frames == 1


def test_unknown_self_low_confidence_and_out_of_order_frames_are_rejected() -> None:
    gate = EncounterGate(demo_config(), DogId("dog_a"))

    unknown = gate.observe(frame(1_000, left=tag(99), right=None))
    own_tag = gate.observe(frame(1_100, left=tag(1), right=None))
    weak = gate.observe(frame(1_200, left=tag(2, margin=24.9), right=None))
    out_of_order = gate.observe(frame(1_200, left=tag(2), right=None))

    assert unknown.reason == IdentityReason.UNKNOWN_TAG
    assert own_tag.reason == IdentityReason.SELF_TAG
    assert weak.reason == IdentityReason.LOW_CONFIDENCE
    assert out_of_order.reason == IdentityReason.OUT_OF_ORDER
    assert all(
        decision.status == IdentityStatus.REJECTED
        for decision in (unknown, own_tag, weak, out_of_order)
    )


def test_manual_identity_is_immediate_but_duplicate_encounter_is_cooled_down() -> None:
    gate = EncounterGate(demo_config(), DogId("dog_a"))

    first = gate.confirm_manual(
        ManualEncounter(owner_id=DogId("dog_a"), peer_id=DogId("dog_b"), observed_at_ms=100)
    )
    duplicate = gate.confirm_manual(
        ManualEncounter(owner_id=DogId("dog_a"), peer_id=DogId("dog_b"), observed_at_ms=500)
    )
    later = gate.confirm_manual(
        ManualEncounter(owner_id=DogId("dog_a"), peer_id=DogId("dog_b"), observed_at_ms=1_100)
    )

    assert first.status == IdentityStatus.CONFIRMED
    assert first.source == IdentitySource.OPERATOR
    assert duplicate.status == IdentityStatus.SUPPRESSED
    assert duplicate.reason == IdentityReason.COOLDOWN
    assert later.status == IdentityStatus.CONFIRMED
