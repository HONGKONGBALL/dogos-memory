#!/usr/bin/env python3
"""Expose one read-only Vbot hardware identity operation on loopback."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import ClassVar, Final, final

import rclpy
from rosidl_runtime_py.utilities import get_service
from typing_extensions import assert_never
from websockets.sync.server import ServerConnection, serve

from dogos_demo.vbot_identity_core import derive_robot_id
from dogos_demo.vbot_identity_protocol import (
    IdentityProtocolError,
    IdentitySnapshot,
    build_identity_response,
    parse_identity_request,
)

IDENTITY_HOST: Final = "127.0.0.1"
IDENTITY_PORT: Final = 9092
_SERIAL_PATH: Final = Path("/proc/device-tree/serial-number")
_ETH2_MAC_PATH: Final = Path("/sys/class/net/eth2/address")
_PN_PATTERN: Final = re.compile(r"^[A-Za-z0-9]{19}$")
_LOGGER: Final = logging.getLogger(__name__)
_EFUSE_UNAVAILABLE: Final = "/x5/efuse-unavailable"
_EFUSE_TIMEOUT: Final = "/x5/efuse-timeout"
_EFUSE_FAILED: Final = "/x5/efuse-failed"
_EFUSE_MALFORMED: Final = "/x5/efuse-malformed-pn"


@final
class IdentityBridgeStartupError(Exception):
    """Required immutable identity evidence could not be read at startup."""

    __slots__: ClassVar[tuple[str, ...]] = ("detail",)

    detail: str

    def __init__(self, detail: str) -> None:
        """Store one bounded startup failure detail."""
        super().__init__(detail)
        self.detail = detail


def _read_efuse_pn() -> str:
    """Read the PN through the vendor's query-only service operation."""
    rclpy.init()
    node = rclpy.create_node("dogos_read_only_identity_bridge")
    service_class = get_service("peripheral_msgs/srv/EfuseControl")
    client = node.create_client(service_class, "/x5/efuse")
    try:
        if not client.wait_for_service(timeout_sec=5.0):
            raise IdentityBridgeStartupError(_EFUSE_UNAVAILABLE)
        request = service_class.Request()
        request.operation = 0
        request.pn_code = ""
        future = client.call_async(request)
        rclpy.spin_until_future_complete(node, future, timeout_sec=8.0)
        if not future.done():
            raise IdentityBridgeStartupError(_EFUSE_TIMEOUT)
        response = future.result()
        if response is None or not response.success:
            raise IdentityBridgeStartupError(_EFUSE_FAILED)
        pn_code = response.pn_code.strip()
        if _PN_PATTERN.fullmatch(pn_code) is None:
            raise IdentityBridgeStartupError(_EFUSE_MALFORMED)
        return pn_code
    finally:
        node.destroy_client(client)
        node.destroy_node()
        rclpy.shutdown()


def _read_snapshot() -> IdentitySnapshot:
    serial = _SERIAL_PATH.read_text(encoding="ascii")
    eth2_mac = _ETH2_MAC_PATH.read_text(encoding="ascii")
    return IdentitySnapshot(
        robot_id=derive_robot_id(serial, eth2_mac),
        pn_code=_read_efuse_pn(),
    )


def main() -> None:
    """Capture immutable identity once, then serve only that snapshot."""
    snapshot = _read_snapshot()

    def handler(connection: ServerConnection) -> None:
        raw = connection.recv(timeout=5.0)
        match raw:
            case str() as text:
                pass
            case bytes() as data:
                text = data.decode("utf-8")
            case unreachable:
                assert_never(unreachable)
        try:
            request = parse_identity_request(text)
        except IdentityProtocolError as error:
            connection.close(code=1008, reason=str(error))
            return
        connection.send(build_identity_response(request.request_id, snapshot))

    with serve(
        handler,
        IDENTITY_HOST,
        IDENTITY_PORT,
        max_size=4096,
        ping_interval=20,
        ping_timeout=20,
    ) as server:
        _LOGGER.info(
            "DOGOS_IDENTITY_READY %s:%d robot_id=%s pn=%s",
            IDENTITY_HOST,
            IDENTITY_PORT,
            snapshot.robot_id,
            snapshot.pn_code,
        )
        server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
