"""Dependency-free physical identity primitives shared with each Vbot."""

from __future__ import annotations

import hashlib
import re
from typing import ClassVar, Final, NewType, final

from typing_extensions import override

RobotHardwareId = NewType("RobotHardwareId", str)
_SERIAL_PATTERN: Final = re.compile(r"^[0-9a-f]{8,64}$")
_MAC_PATTERN: Final = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
_DIGEST_LENGTH: Final = 16


@final
class HardwareIdentityError(Exception):
    """Hardware evidence cannot produce one stable robot identity."""

    __slots__: ClassVar[tuple[str, ...]] = ("detail",)

    detail: str

    def __init__(self, detail: str) -> None:
        """Store one bounded explanation for the rejected evidence."""
        super().__init__(detail)
        self.detail = detail

    @override
    def __str__(self) -> str:
        """Return the hardware-evidence failure detail."""
        return self.detail


def derive_robot_id(device_tree_serial: str, eth2_mac: str) -> RobotHardwareId:
    """Derive one stable opaque ID from immutable board evidence."""
    serial = device_tree_serial.replace("\x00", "").strip().lower()
    mac = eth2_mac.strip().lower()
    if _SERIAL_PATTERN.fullmatch(serial) is None:
        raise HardwareIdentityError(detail="device-tree serial is missing or malformed")
    if _MAC_PATTERN.fullmatch(mac) is None:
        raise HardwareIdentityError(detail="eth2 MAC is missing or malformed")
    material = f"device-tree:{serial}|eth2:{mac}".encode()
    digest = hashlib.sha256(material).hexdigest()[:_DIGEST_LENGTH]
    return RobotHardwareId(f"vbot-{digest}")
