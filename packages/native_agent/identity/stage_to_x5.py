"""Stage one validated identity envelope from S100 to X5 `/app_param`.

The ROS imports are lazy so build and dry-run work on a development machine.
This command never executes shell text and cannot choose an arbitrary X5 path.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .bundle import IdentityError, _validate_dog_id
from .staging import (
    ValidatedEnvelope,
    _load_validated_envelope,
    assert_envelope_snapshot_current,
)

SERVICE_NAME = "/write_x5_file"


@dataclass(frozen=True, slots=True)
class StagePlan:
    dog_id: str
    revision: str
    source: Path
    target: str
    size_bytes: int

    def to_dict(self, status: str) -> dict[str, object]:
        return {
            "status": status,
            "dog_id": self.dog_id,
            "revision": self.revision,
            "source": str(self.source),
            "target": self.target,
            "size_bytes": self.size_bytes,
        }


def _plan_from_snapshot(snapshot: ValidatedEnvelope) -> StagePlan:
    manifest = snapshot.manifest
    target = f"/app_param/dogos-import-{manifest.dog_id}-{manifest.revision}.json"
    return StagePlan(
        manifest.dog_id,
        manifest.revision,
        snapshot.source,
        target,
        len(snapshot.raw_bytes),
    )


def plan_stage(envelope_file: Path) -> StagePlan:
    """Validate the full envelope and derive its only permitted X5 target."""

    snapshot = _load_validated_envelope(envelope_file)
    assert_envelope_snapshot_current(snapshot)
    return _plan_from_snapshot(snapshot)


def stage_to_x5(
    envelope_file: Path,
    *,
    execute: bool = False,
    expected_dog_id: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, object]:
    """Dry-run or call WriteFile after a trusted wrapper binds the dog id."""

    snapshot = _load_validated_envelope(envelope_file)
    plan = _plan_from_snapshot(snapshot)
    if not execute:
        if expected_dog_id is not None:
            _validate_dog_id(expected_dog_id)
            if expected_dog_id != plan.dog_id:
                raise IdentityError(
                    "dog_id_mismatch",
                    f"expected {expected_dog_id}, envelope is {plan.dog_id}",
                )
        assert_envelope_snapshot_current(snapshot)
        return plan.to_dict("dry_run")
    if expected_dog_id is None:
        raise IdentityError(
            "expected_dog_id_required",
            "execute mode requires an id from a trusted device-bound wrapper",
        )
    _validate_dog_id(expected_dog_id)
    if expected_dog_id != plan.dog_id:
        raise IdentityError(
            "dog_id_mismatch",
            f"expected {expected_dog_id}, envelope is {plan.dog_id}",
        )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > 60
    ):
        raise IdentityError("invalid_timeout", str(timeout_seconds))
    # The future device-bound wrapper must authorize execution.  This module
    # only guarantees that the bytes sent are the exact bytes validated above.
    assert_envelope_snapshot_current(snapshot)
    try:
        import rclpy  # type: ignore[import-not-found]
        from rclpy.node import Node  # type: ignore[import-not-found]
        from software_msgs.srv import WriteFile  # type: ignore[import-not-found]
    except (ImportError, ModuleNotFoundError) as error:
        raise IdentityError(
            "ros_dependency_unavailable",
            "run on S100 after sourcing the Vbot ROS environment",
        ) from error

    content = snapshot.raw_bytes.decode("utf-8")
    rclpy.init(args=None)
    node = Node("dogos_identity_stager")
    try:
        client = node.create_client(WriteFile, SERVICE_NAME)
        if not client.wait_for_service(timeout_sec=timeout_seconds):
            raise IdentityError("service_unavailable", SERVICE_NAME)
        request = WriteFile.Request()
        request.path = plan.target
        assert_envelope_snapshot_current(snapshot)
        request.content = content
        future = client.call_async(request)
        rclpy.spin_until_future_complete(node, future, timeout_sec=timeout_seconds)
        if not future.done():
            raise IdentityError("service_timeout", SERVICE_NAME)
        error = future.exception()
        if error is not None:
            raise IdentityError("service_error", str(error))
        response = future.result()
        if response is None or response.success is not True:
            detail = "empty response" if response is None else str(response.message)
            raise IdentityError("write_rejected", detail)
        result = plan.to_dict("staged")
        result["service"] = SERVICE_NAME
        result["message"] = str(response.message)
        return result
    finally:
        node.destroy_node()
        rclpy.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m native_agent.identity.stage_to_x5")
    parser.add_argument("--envelope-file", type=Path, required=True)
    parser.add_argument("--expected-dog-id")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    args = parser.parse_args(argv)
    try:
        result = stage_to_x5(
            args.envelope_file,
            execute=args.execute,
            expected_dog_id=args.expected_dog_id,
            timeout_seconds=args.timeout_seconds,
        )
    except IdentityError as error:
        print(json.dumps({"status": "error", "code": error.code, "detail": error.detail}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
