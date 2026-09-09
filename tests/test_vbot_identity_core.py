"""Checks for stable Vbot hardware identity derivation."""

from __future__ import annotations

import pytest

from dogos_demo.vbot_identity_core import HardwareIdentityError, derive_robot_id


def test_derive_robot_id_is_stable_for_synthetic_vbot_hardware() -> None:
    # Given: synthetic board serial and locally administered MAC test values.
    serial = "0123456789ABCDEF\n"
    eth2_mac = "02:00:00:00:00:01\n"

    # When: DogOS derives a route-verification identity.
    robot_id = derive_robot_id(serial, eth2_mac)

    # Then: normalization produces the fixed, non-PN identifier for this body.
    assert robot_id == "vbot-a3b739431e6709a2"


@pytest.mark.parametrize(
    ("serial", "eth2_mac"),
    [
        ("", "02:00:00:00:00:01"),
        ("0123456789abcdef", "not-a-mac"),
    ],
)
def test_derive_robot_id_rejects_incomplete_hardware_evidence(
    serial: str,
    eth2_mac: str,
) -> None:
    # Given: hardware evidence that cannot identify one physical Vbot.
    # When/Then: the boundary fails closed instead of creating a weak identity.
    with pytest.raises(HardwareIdentityError):
        _ = derive_robot_id(serial, eth2_mac)
