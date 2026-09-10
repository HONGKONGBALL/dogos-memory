"""X5-side identity installer used by the signed-by-hash deployment capsule.

The module never accepts a shell command.  Its live CLI is fixed to device root
``/`` and invokes only ``systemctl restart/is-active`` with a constant unit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from native_agent.identity.bundle import DOG_ID_RE, REVISION_RE, IdentityError
from native_agent.identity.importer import (
    ApplyResult,
    apply_bundle,
    mark_restart_complete,
    rollback_bundle,
)
from native_agent.identity.staging import (
    _load_validated_envelope,
    unpack_staging_envelope,
)


HARNESS_UNIT = "vbot-agent-harness.service"


class DeploymentError(RuntimeError):
    def __init__(self, code: str, detail: str, *, rollback: dict[str, object] | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.rollback = rollback


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    status: str
    dog_id: str
    revision: str
    importer_status: str
    harness_active: bool
    rollback_performed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "dog_id": self.dog_id,
            "revision": self.revision,
            "importer_status": self.importer_status,
            "harness_active": self.harness_active,
            "rollback_performed": self.rollback_performed,
        }


def _validate_identity(dog_id: str, revision: str) -> None:
    if not DOG_ID_RE.fullmatch(dog_id):
        raise DeploymentError("invalid_dog_id", dog_id)
    if not REVISION_RE.fullmatch(revision):
        raise DeploymentError("invalid_revision", revision)


def restart_harness(timeout_seconds: float = 20.0) -> None:
    """Restart the one constant harness unit and wait for an active state."""

    if shutil.which("systemctl") is None:
        raise DeploymentError("systemctl_unavailable", HARNESS_UNIT)
    try:
        restarted = subprocess.run(
            ["systemctl", "restart", HARNESS_UNIT],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError("harness_restart_failed", type(error).__name__) from error
    if restarted.returncode != 0:
        raise DeploymentError(
            "harness_restart_failed", f"systemctl return code {restarted.returncode}"
        )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", HARNESS_UNIT],
            check=False,
            capture_output=True,
        )
        if active.returncode == 0:
            return
        time.sleep(0.25)
    raise DeploymentError("harness_health_timeout", HARNESS_UNIT)


def _bundle_from_envelope(envelope: Path, device_root: Path) -> tuple[Path, object]:
    snapshot = _load_validated_envelope(envelope)
    work_parent = device_root / "userdata/.vbot-agent/import-work"
    work_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix="release-", dir=work_parent))
    bundle = temporary / "bundle"
    try:
        manifest = unpack_staging_envelope(envelope, bundle)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    if manifest != snapshot.manifest:
        shutil.rmtree(temporary, ignore_errors=True)
        raise DeploymentError("envelope_rebuild_mismatch", snapshot.manifest.revision)
    return temporary, manifest


def apply_identity_release(
    envelope: Path,
    device_root: Path,
    *,
    expected_dog_id: str,
    expected_revision: str,
    execute: bool,
    live_confirmation: str | None = None,
    restart: Callable[[], None] = restart_harness,
) -> ReleaseResult:
    """Apply one release, verify a harness restart, or restore its exact baseline."""

    _validate_identity(expected_dog_id, expected_revision)
    root = device_root.resolve()
    work, manifest = _bundle_from_envelope(envelope, root)
    if manifest.dog_id != expected_dog_id:
        shutil.rmtree(work, ignore_errors=True)
        raise DeploymentError("dog_id_mismatch", manifest.dog_id)
    if manifest.revision != expected_revision:
        shutil.rmtree(work, ignore_errors=True)
        raise DeploymentError("revision_mismatch", manifest.revision)
    try:
        applied = apply_bundle(
            work / "bundle",
            root,
            execute=execute,
            expected_dog_id=expected_dog_id,
            confirm_live_device=live_confirmation,
        )
        if not execute:
            return ReleaseResult(
                "dry_run",
                expected_dog_id,
                expected_revision,
                applied.status,
                False,
                False,
            )
        try:
            restart()
            marked = mark_restart_complete(
                root,
                expected_revision,
                execute=True,
                expected_dog_id=expected_dog_id,
                confirm_live_device=live_confirmation,
            )
        except BaseException as failure:
            rollback_result: ApplyResult | None = None
            rollback_restart_ok = False
            try:
                rollback_result = rollback_bundle(
                    root,
                    expected_revision,
                    execute=True,
                    expected_dog_id=expected_dog_id,
                    confirm_live_device=live_confirmation,
                )
                restart()
                rollback_restart_ok = True
            except BaseException as rollback_failure:
                raise DeploymentError(
                    "rollback_after_restart_failure_failed",
                    f"install={type(failure).__name__}; rollback={type(rollback_failure).__name__}",
                    rollback={
                        "performed": rollback_result is not None,
                        "harness_active": rollback_restart_ok,
                    },
                ) from rollback_failure
            raise DeploymentError(
                "rolled_back_after_restart_failure",
                type(failure).__name__,
                rollback={
                    "performed": True,
                    "status": rollback_result.status,
                    "harness_active": True,
                },
            ) from failure
        return ReleaseResult(
            "ready",
            expected_dog_id,
            expected_revision,
            f"{applied.status};{marked.status}",
            True,
            False,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def rollback_identity_release(
    device_root: Path,
    *,
    dog_id: str,
    revision: str,
    execute: bool,
    live_confirmation: str | None = None,
    restart: Callable[[], None] = restart_harness,
) -> ReleaseResult:
    _validate_identity(dog_id, revision)
    result = rollback_bundle(
        device_root,
        revision,
        execute=execute,
        expected_dog_id=dog_id,
        confirm_live_device=live_confirmation,
    )
    if not execute:
        return ReleaseResult("rollback_dry_run", dog_id, revision, result.status, False, False)
    restart()
    return ReleaseResult("rolled_back", dog_id, revision, result.status, True, True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vbot-x5-installer")
    commands = parser.add_subparsers(dest="command", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--envelope", type=Path, required=True)
    apply.add_argument("--expected-dog-id", required=True)
    apply.add_argument("--expected-revision", required=True)
    apply.add_argument("--execute-live-confirmation", required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--revision", required=True)
    rollback.add_argument("--expected-dog-id", required=True)
    rollback.add_argument("--execute-live-confirmation", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.execute_live_confirmation != args.expected_dog_id:
            raise DeploymentError("live_confirmation_mismatch", "dog id")
        if args.command == "apply":
            result = apply_identity_release(
                args.envelope,
                Path("/"),
                expected_dog_id=args.expected_dog_id,
                expected_revision=args.expected_revision,
                execute=True,
                live_confirmation=args.execute_live_confirmation,
            )
        else:
            result = rollback_identity_release(
                Path("/"),
                dog_id=args.expected_dog_id,
                revision=args.revision,
                execute=True,
                live_confirmation=args.execute_live_confirmation,
            )
    except IdentityError as error:
        print(json.dumps({"status": "error", "code": error.code, "detail": error.detail}))
        return 2
    except DeploymentError as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": error.code,
                    "detail": error.detail,
                    "rollback": error.rollback,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
