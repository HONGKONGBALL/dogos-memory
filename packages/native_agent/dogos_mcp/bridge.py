#!/usr/bin/env python3
"""Process-isolated JSON bridge used by the Node MCP server."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPOSITORY_ROOT / "packages"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

dogos_source = Path(os.environ.get("DOGOS_MEMORY_SOURCE", REPOSITORY_ROOT))
dogos_packages = dogos_source / "packages"
if dogos_packages.is_dir():
    dogos_source = dogos_packages
if str(dogos_source) not in sys.path:
    sys.path.insert(0, str(dogos_source))

from native_agent.dogos_adapter import DogOSAdapterError, bind_social_memory  # noqa: E402
from native_agent.dogos_mcp.relay import (  # noqa: E402
    RelayError,
    acknowledge_message,
    health,
    list_inbox,
    send_message,
)

_ARGUMENT_COUNT = 3


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RelayError("configuration_missing", f"{name} is required")
    return value


def _arguments() -> tuple[str, dict[str, Any]]:
    if len(sys.argv) != _ARGUMENT_COUNT:
        raise RelayError("invalid_request", "expected operation and one JSON object")
    operation = sys.argv[1]
    value = json.loads(sys.argv[2])
    if not isinstance(value, dict):
        raise RelayError("invalid_request", "arguments must be an object")
    return operation, value


def run() -> dict[str, object]:
    """Dispatch one fixed-owner bridge operation."""
    operation, arguments = _arguments()
    owner = _required("DOGOS_OWNER_ID")
    peer = _required("DOGOS_PEER_ID")
    database = _required("DOGOS_DATABASE")
    peer_database = _required("DOGOS_PEER_DATABASE")
    relay_database = _required("DOGOS_RELAY_DATABASE")
    if operation == "health":
        result = health(relay_database, owner, peer)
        _ = bind_social_memory(database, owner)
        _ = bind_social_memory(peer_database, peer)
    elif operation == "recall":
        result = bind_social_memory(database, owner).recall(peer, limit=arguments.get("limit", 3))
    elif operation == "send":
        result = send_message(
            relay_database,
            owner,
            peer,
            message_id=arguments.get("message_id"),
            content=arguments.get("content"),
        )
    elif operation == "inbox":
        result = list_inbox(relay_database, owner, peer, limit=arguments.get("limit", 10))
    elif operation == "ack":
        _ = bind_social_memory(database, owner)
        _ = bind_social_memory(peer_database, peer)
        result = acknowledge_message(
            relay_database,
            database,
            peer_database,
            owner,
            peer,
            message_id=arguments.get("message_id"),
        )
    else:
        raise RelayError("unknown_operation", "operation is not supported")
    return {"ok": True, "result": result}


def main() -> None:
    """Return one bounded JSON response and hide internal exception details."""
    try:
        payload = run()
    except (RelayError, DogOSAdapterError) as error:
        payload = {"ok": False, "error": {"code": error.code, "message": error.detail}}
    except (json.JSONDecodeError, TypeError, ValueError):
        payload = {
            "ok": False,
            "error": {"code": "invalid_request", "message": "request validation failed"},
        }
    except Exception:  # noqa: BLE001 - process boundary must not leak internals
        payload = {
            "ok": False,
            "error": {"code": "bridge_error", "message": "DogOS bridge failed"},
        }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
