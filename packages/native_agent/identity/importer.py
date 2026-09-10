"""Transactional identity import for a local or mounted Vbot filesystem.

There is no shell or service-restart path here. Execute mode requires an
expected dog id. A future trusted wrapper must obtain that id from device-bound
provisioning; a caller-provided string alone is not proof of device identity.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Iterator, Mapping

from .bundle import (
    DOG_ID_RE,
    FILE_SPECS,
    REVISION_RE,
    SHA256_RE,
    IdentityError,
    IdentityManifest,
    ValidatedBundle,
    _strict_json_loads,
    _validate_dog_id,
    assert_bundle_snapshot_current,
    validate_bundle_snapshot,
)

STATE_PATH = Path("userdata/.vbot-agent/import-state.json")
BACKUP_ROOT = Path("userdata/.vbot-agent/import-backups")
LOCK_PATH = Path("userdata/.vbot-agent/import.lock")
PENDING_PATH = Path("userdata/.vbot-agent/import-pending.json")
BACKUP_SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION = 1

APPLY_PHASES: Final = (
    "backup_pending",
    "backup_complete",
    "installing",
    "state_written",
)
ROLLBACK_PHASES: Final = (
    "rollback_pending",
    "rollback_restoring",
    "rollback_state_written",
)


@dataclass(frozen=True, slots=True)
class ApplyResult:
    status: str
    dog_id: str
    revision: str
    restart_required: bool
    operations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "dog_id": self.dog_id,
            "revision": self.revision,
            "restart_required": self.restart_required,
            "operations": list(self.operations),
        }


@dataclass(frozen=True, slots=True)
class BaselineRecord:
    filename: str
    target: str
    existed: bool
    mode: int
    sha256: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "target": self.target,
            "existed": self.existed,
            "mode": self.mode,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class BackupSnapshot:
    path: Path
    dog_id: str
    revision: str
    created_at: str
    previous_state: dict[str, object] | None
    baseline: tuple[BaselineRecord, ...]
    metadata_bytes: bytes
    payloads: tuple[tuple[str, bytes], ...]

    @property
    def metadata_sha256(self) -> str:
        return _sha256(self.metadata_bytes)

    def payload(self, filename: str) -> bytes:
        for candidate, data in self.payloads:
            if candidate == filename:
                return data
        raise IdentityError("invalid_backup", f"missing payload {filename}")


@dataclass(frozen=True, slots=True)
class PendingJournal:
    operation: str
    phase: str
    dog_id: str
    revision: str
    previous_state: dict[str, object] | None
    baseline: tuple[BaselineRecord, ...]
    backup_metadata_sha256: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "operation": self.operation,
            "phase": self.phase,
            "dog_id": self.dog_id,
            "revision": self.revision,
            "previous_state": self.previous_state,
            "baseline": [item.to_dict() for item in self.baseline],
            "backup_metadata_sha256": self.backup_metadata_sha256,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _device_root(value: str | Path) -> Path:
    root = Path(value)
    if root.is_symlink() or not root.is_dir():
        raise IdentityError("invalid_device_root", str(root))
    return root.resolve()


def _device_path(
    device_root: Path, relative: str | Path, *, create_parent: bool = False
) -> Path:
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in ("", ".", "..") for part in relative_path.parts)
    ):
        raise IdentityError("unsafe_target", str(relative))
    root = _device_root(device_root)
    current = root
    for part in relative_path.parts[:-1]:
        candidate = current / part
        if candidate.is_symlink():
            raise IdentityError("symlink_rejected", str(candidate))
        if candidate.exists() and not candidate.is_dir():
            raise IdentityError("invalid_target_parent", str(candidate))
        if not candidate.exists():
            if not create_parent:
                return root / relative_path
            candidate.mkdir(mode=0o700)
            _fsync_directory(candidate.parent)
        current = candidate
    target = current / relative_path.parts[-1]
    if target.is_symlink():
        raise IdentityError("symlink_rejected", str(target))
    return target


def _read_json(path: Path, *, label: str) -> object | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise IdentityError("invalid_state_file", str(path))
    try:
        return _strict_json_loads(path.read_bytes(), label=label)
    except IdentityError as error:
        if error.code == "duplicate_json_key":
            raise
        raise IdentityError("invalid_state_file", f"{label}: {error.detail}") from error


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    if path.is_symlink():
        raise IdentityError("symlink_rejected", str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write(path, data, 0o600)


def _durable_unlink(path: Path, *, missing_ok: bool = False) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        if not missing_ok:
            raise
        return
    _fsync_directory(path.parent)


def _durable_rmtree(path: Path, *, missing_ok: bool = False) -> None:
    if not path.exists():
        if missing_ok:
            return
        raise FileNotFoundError(path)
    if path.is_symlink() or not path.is_dir():
        raise IdentityError("invalid_backup", str(path))
    parent = path.parent
    shutil.rmtree(path)
    _fsync_directory(parent)


@contextmanager
def _import_lock(device_root: Path) -> Iterator[None]:
    lock_path = _device_path(device_root, LOCK_PATH, create_parent=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IdentityError("import_busy", str(lock_path)) from error
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _default_mode(filename: str) -> int:
    return 0o644 if filename == "AGENTS.md" else 0o600


def _validate_state(value: object | None, *, label: str) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "dog_id",
        "current_revision",
        "applied_at",
        "restart_required",
    }:
        raise IdentityError("invalid_state", label)
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["dog_id"], str)
        or not DOG_ID_RE.fullmatch(value["dog_id"])
        or not isinstance(value["current_revision"], str)
        or not REVISION_RE.fullmatch(value["current_revision"])
        or not _valid_timestamp(value["applied_at"])
        or not isinstance(value["restart_required"], bool)
    ):
        raise IdentityError("invalid_state", label)
    return dict(value)


def _read_state(device_root: Path) -> dict[str, object] | None:
    path = _device_path(device_root, STATE_PATH)
    return _validate_state(_read_json(path, label="import state"), label="import state")


def _capture_baseline(
    manifest: IdentityManifest, device_root: Path
) -> tuple[BaselineRecord, ...]:
    result: list[BaselineRecord] = []
    for record in manifest.files:
        target = _device_path(device_root, record.target)
        existed = target.exists()
        if existed and not target.is_file():
            raise IdentityError("invalid_target", record.target)
        mode = stat.S_IMODE(target.stat().st_mode) if existed else _default_mode(record.filename)
        result.append(
            BaselineRecord(
                record.filename,
                record.target,
                existed,
                mode,
                _sha256(target.read_bytes()) if existed else None,
            )
        )
    return tuple(result)


def _parse_baseline(value: object, *, label: str) -> tuple[BaselineRecord, ...]:
    if not isinstance(value, list):
        raise IdentityError("invalid_baseline", label)
    result: list[BaselineRecord] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {
            "filename",
            "target",
            "existed",
            "mode",
            "sha256",
        }:
            raise IdentityError("invalid_baseline", f"{label}[{index}]")
        filename = item["filename"]
        existed = item["existed"]
        mode = item["mode"]
        digest = item["sha256"]
        if (
            not isinstance(filename, str)
            or filename not in FILE_SPECS
            or filename in seen
            or item["target"] != FILE_SPECS[filename].target
            or not isinstance(existed, bool)
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or not 0 <= mode <= 0o777
            or (existed and (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)))
            or (not existed and digest is not None)
            or (not existed and mode != _default_mode(filename))
        ):
            raise IdentityError("invalid_baseline", f"{label}[{index}]")
        seen.add(filename)
        result.append(
            BaselineRecord(filename, str(item["target"]), existed, mode, digest)
        )
    required = {name for name, spec in FILE_SPECS.items() if spec.required}
    if not required.issubset(seen) or len(result) > len(FILE_SPECS):
        raise IdentityError("invalid_baseline", label)
    return tuple(result)


def _baseline_matches(
    baseline: tuple[BaselineRecord, ...], device_root: Path
) -> bool:
    for record in baseline:
        target = _device_path(device_root, record.target)
        if not record.existed:
            if target.exists() or target.is_symlink():
                return False
            continue
        if target.is_symlink() or not target.is_file():
            return False
        if stat.S_IMODE(target.stat().st_mode) != record.mode:
            return False
        if _sha256(target.read_bytes()) != record.sha256:
            return False
    return True


def _state_matches(device_root: Path, expected: dict[str, object] | None) -> bool:
    try:
        return _read_state(device_root) == expected
    except IdentityError:
        return False


def _backup_value(
    manifest: IdentityManifest,
    previous_state: dict[str, object] | None,
    baseline: tuple[BaselineRecord, ...],
) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for record in baseline:
        item = record.to_dict()
        item["backup_path"] = f"files/{record.filename}" if record.existed else None
        files.append(item)
    return {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "dog_id": manifest.dog_id,
        "revision": manifest.revision,
        "created_at": _utc_now(),
        "previous_state": previous_state,
        "files": files,
    }


def _backup(
    manifest: IdentityManifest,
    device_root: Path,
    previous_state: dict[str, object] | None,
    baseline: tuple[BaselineRecord, ...] | None = None,
) -> BackupSnapshot:
    """Create a backup only after the caller has persisted a journal."""

    captured = baseline or _capture_baseline(manifest, device_root)
    backup_dir = _device_path(
        device_root, BACKUP_ROOT / manifest.revision, create_parent=True
    )
    if backup_dir.exists() or backup_dir.is_symlink():
        raise IdentityError("backup_exists", manifest.revision)
    backup_dir.mkdir(mode=0o700)
    _fsync_directory(backup_dir.parent)
    for record in captured:
        target = _device_path(device_root, record.target)
        if record.existed:
            if (
                target.is_symlink()
                or not target.is_file()
                or stat.S_IMODE(target.stat().st_mode) != record.mode
            ):
                raise IdentityError("baseline_changed_during_backup", record.target)
            data = target.read_bytes()
            if _sha256(data) != record.sha256:
                raise IdentityError("baseline_changed_during_backup", record.target)
            _atomic_write(backup_dir / "files" / record.filename, data, 0o600)
        elif target.exists() or target.is_symlink():
            raise IdentityError("baseline_changed_during_backup", record.target)
    _write_json(backup_dir / "metadata.json", _backup_value(manifest, previous_state, captured))
    return _load_backup_snapshot(
        backup_dir,
        expected_revision=manifest.revision,
        expected_dog_id=manifest.dog_id,
    )


def _validate_backup_value(
    value: object,
) -> tuple[str, str, str, dict[str, object] | None, tuple[BaselineRecord, ...]]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "dog_id",
        "revision",
        "created_at",
        "previous_state",
        "files",
    }:
        raise IdentityError("invalid_backup", "metadata shape")
    dog_id = value["dog_id"]
    revision = value["revision"]
    created_at = value["created_at"]
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != BACKUP_SCHEMA_VERSION
        or not isinstance(dog_id, str)
        or not DOG_ID_RE.fullmatch(dog_id)
        or not isinstance(revision, str)
        or not REVISION_RE.fullmatch(revision)
        or not _valid_timestamp(created_at)
        or not isinstance(value["files"], list)
    ):
        raise IdentityError("invalid_backup", "metadata values")
    previous_state = _validate_state(value["previous_state"], label="backup previous_state")
    baseline_values: list[dict[str, object]] = []
    for index, item in enumerate(value["files"]):
        if not isinstance(item, dict) or set(item) != {
            "filename",
            "target",
            "existed",
            "mode",
            "sha256",
            "backup_path",
        }:
            raise IdentityError("invalid_backup", f"files[{index}]")
        filename = item.get("filename")
        expected_path = (
            f"files/{filename}"
            if isinstance(filename, str) and item.get("existed") is True
            else None
        )
        if item.get("backup_path") != expected_path:
            raise IdentityError("invalid_backup", f"files[{index}].backup_path")
        baseline_values.append(
            {key: item[key] for key in ("filename", "target", "existed", "mode", "sha256")}
        )
    try:
        baseline = _parse_baseline(baseline_values, label="backup files")
    except IdentityError as error:
        raise IdentityError("invalid_backup", error.detail) from error
    return dog_id, revision, created_at, previous_state, baseline


def _load_backup_snapshot(
    backup_dir: Path,
    *,
    expected_revision: str | None = None,
    expected_dog_id: str | None = None,
) -> BackupSnapshot:
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        raise IdentityError("invalid_backup", str(backup_dir))
    metadata_path = backup_dir / "metadata.json"
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise IdentityError("invalid_backup", str(metadata_path))
    metadata_bytes = metadata_path.read_bytes()
    if len(metadata_bytes) > 64 * 1024:
        raise IdentityError("invalid_backup", "metadata too large")
    try:
        value = _strict_json_loads(metadata_bytes, label="backup metadata")
    except IdentityError as error:
        raise IdentityError("invalid_backup", error.detail) from error
    dog_id, revision, created_at, previous_state, baseline = _validate_backup_value(value)
    if expected_revision is not None and revision != expected_revision:
        raise IdentityError("invalid_backup", "revision mismatch")
    if expected_dog_id is not None and dog_id != expected_dog_id:
        raise IdentityError("invalid_backup", "dog id mismatch")

    expected_files = {item.filename for item in baseline if item.existed}
    expected_root = {"metadata.json"} | ({"files"} if expected_files else set())
    if {entry.name for entry in backup_dir.iterdir()} != expected_root:
        raise IdentityError("invalid_backup", "unexpected entries")
    files_dir = backup_dir / "files"
    if expected_files:
        if files_dir.is_symlink() or not files_dir.is_dir():
            raise IdentityError("invalid_backup", str(files_dir))
        if {entry.name for entry in files_dir.iterdir()} != expected_files:
            raise IdentityError("invalid_backup", "file set mismatch")
    payloads: list[tuple[str, bytes]] = []
    for item in baseline:
        if not item.existed:
            continue
        source = files_dir / item.filename
        if source.is_symlink() or not source.is_file():
            raise IdentityError("invalid_backup", str(source))
        data = source.read_bytes()
        if _sha256(data) != item.sha256:
            raise IdentityError("backup_hash_mismatch", item.target)
        payloads.append((item.filename, data))
    return BackupSnapshot(
        backup_dir.resolve(),
        dog_id,
        revision,
        created_at,
        previous_state,
        baseline,
        metadata_bytes,
        tuple(payloads),
    )


def _assert_backup_current(snapshot: BackupSnapshot) -> None:
    current = _load_backup_snapshot(
        snapshot.path,
        expected_revision=snapshot.revision,
        expected_dog_id=snapshot.dog_id,
    )
    if (
        current.metadata_bytes != snapshot.metadata_bytes
        or current.payloads != snapshot.payloads
        or current.baseline != snapshot.baseline
    ):
        raise IdentityError("backup_changed_after_validation", snapshot.revision)


def _restore_backup(snapshot: BackupSnapshot, device_root: Path) -> None:
    _assert_backup_current(snapshot)
    for item in snapshot.baseline:
        target = _device_path(device_root, item.target, create_parent=True)
        if item.existed:
            _atomic_write(target, snapshot.payload(item.filename), item.mode)
        elif target.exists():
            if target.is_symlink() or not target.is_file():
                raise IdentityError("invalid_target", item.target)
            _durable_unlink(target)


def _install_payloads(snapshot: ValidatedBundle, device_root: Path) -> None:
    for record in snapshot.manifest.files:
        target = _device_path(device_root, record.target, create_parent=True)
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else _default_mode(record.filename)
        _atomic_write(target, snapshot.payload(record.filename), mode)


def _restore_state(device_root: Path, previous_state: dict[str, object] | None) -> None:
    state_path = _device_path(device_root, STATE_PATH, create_parent=True)
    if previous_state is None:
        _durable_unlink(state_path, missing_ok=True)
    else:
        _write_json(state_path, previous_state)


def _make_pending_journal(
    *,
    operation: str,
    phase: str,
    dog_id: str,
    revision: str,
    previous_state: dict[str, object] | None,
    baseline: tuple[BaselineRecord, ...],
    backup_metadata_sha256: str | None,
) -> PendingJournal:
    return PendingJournal(
        operation,
        phase,
        dog_id,
        revision,
        previous_state,
        baseline,
        backup_metadata_sha256,
    )


def _validate_pending(value: object) -> PendingJournal:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "operation",
        "phase",
        "dog_id",
        "revision",
        "previous_state",
        "baseline",
        "backup_metadata_sha256",
    }:
        raise IdentityError("invalid_pending_import", "shape")
    operation = value["operation"]
    phase = value["phase"]
    phases = APPLY_PHASES if operation == "apply" else ROLLBACK_PHASES if operation == "rollback" else ()
    digest = value["backup_metadata_sha256"]
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != JOURNAL_SCHEMA_VERSION
        or phase not in phases
        or not isinstance(value["dog_id"], str)
        or not DOG_ID_RE.fullmatch(value["dog_id"])
        or not isinstance(value["revision"], str)
        or not REVISION_RE.fullmatch(value["revision"])
        or (digest is not None and (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)))
    ):
        raise IdentityError("invalid_pending_import", "values")
    if operation == "apply" and phase == "backup_pending":
        if digest is not None:
            raise IdentityError("invalid_pending_import", "premature backup digest")
    elif not isinstance(digest, str):
        raise IdentityError("invalid_pending_import", "missing backup digest")
    previous_state = _validate_state(value["previous_state"], label="pending previous_state")
    baseline = _parse_baseline(value["baseline"], label="pending baseline")
    return PendingJournal(
        str(operation),
        str(phase),
        value["dog_id"],
        value["revision"],
        previous_state,
        baseline,
        digest,
    )


def _write_pending(device_root: Path, journal: PendingJournal) -> None:
    _write_json(
        _device_path(device_root, PENDING_PATH, create_parent=True),
        journal.to_dict(),
    )


def _read_pending(device_root: Path) -> PendingJournal | None:
    value = _read_json(
        _device_path(device_root, PENDING_PATH), label="pending import"
    )
    return None if value is None else _validate_pending(value)


def _backup_matches_journal(backup: BackupSnapshot, journal: PendingJournal) -> bool:
    return (
        backup.dog_id == journal.dog_id
        and backup.revision == journal.revision
        and backup.previous_state == journal.previous_state
        and backup.baseline == journal.baseline
        and backup.metadata_sha256 == journal.backup_metadata_sha256
    )


def _recover_pending(device_root: Path) -> str | None:
    """Recover every journal phase idempotently; journal deletion commits."""

    journal = _read_pending(device_root)
    if journal is None:
        return None
    pending_path = _device_path(device_root, PENDING_PATH)
    backup_dir = _device_path(device_root, BACKUP_ROOT / journal.revision)
    baseline_done = _baseline_matches(journal.baseline, device_root)
    state_done = _state_matches(device_root, journal.previous_state)

    if journal.operation == "apply":
        if journal.phase == "backup_pending":
            if not baseline_done or not state_done:
                raise IdentityError("pending_baseline_mismatch", journal.revision)
            _durable_rmtree(backup_dir, missing_ok=True)
            _durable_unlink(pending_path)
            return journal.revision
        if baseline_done and state_done and not backup_dir.exists():
            _durable_unlink(pending_path)
            return journal.revision
        backup = _load_backup_snapshot(
            backup_dir,
            expected_revision=journal.revision,
            expected_dog_id=journal.dog_id,
        )
        if not _backup_matches_journal(backup, journal):
            raise IdentityError("invalid_pending_backup", journal.revision)
        if not baseline_done:
            _restore_backup(backup, device_root)
        if not state_done:
            _restore_state(device_root, journal.previous_state)
        if not _baseline_matches(journal.baseline, device_root) or not _state_matches(
            device_root, journal.previous_state
        ):
            raise IdentityError("pending_recovery_incomplete", journal.revision)
        _durable_rmtree(backup_dir)
        _durable_unlink(pending_path)
        return journal.revision

    backup = _load_backup_snapshot(
        backup_dir,
        expected_revision=journal.revision,
        expected_dog_id=journal.dog_id,
    )
    if not _backup_matches_journal(backup, journal):
        raise IdentityError("invalid_pending_backup", journal.revision)
    if not baseline_done:
        _restore_backup(backup, device_root)
    if not state_done:
        _restore_state(device_root, journal.previous_state)
    if not _baseline_matches(journal.baseline, device_root) or not _state_matches(
        device_root, journal.previous_state
    ):
        raise IdentityError("pending_recovery_incomplete", journal.revision)
    _durable_unlink(pending_path)
    return journal.revision


def _installed_matches(snapshot: ValidatedBundle, device_root: Path) -> bool:
    for record in snapshot.manifest.files:
        target = _device_path(device_root, record.target)
        if target.is_symlink() or not target.is_file():
            return False
        if _sha256(target.read_bytes()) != record.sha256:
            return False
    return True


def _check_expected_dog_id(
    *, execute: bool, expected_dog_id: str | None, actual_dog_id: str
) -> None:
    if execute and expected_dog_id is None:
        raise IdentityError(
            "expected_dog_id_required",
            "execute mode requires an id from a trusted device-bound wrapper",
        )
    if expected_dog_id is not None:
        _validate_dog_id(expected_dog_id)
        if expected_dog_id != actual_dog_id:
            raise IdentityError(
                "dog_id_mismatch",
                f"expected {expected_dog_id}, bundle or backup is {actual_dog_id}",
            )


def _live_guard(root: Path, dog_id: str, confirmation: str | None) -> None:
    if root == Path("/") and confirmation != dog_id:
        raise IdentityError(
            "live_confirmation_required",
            f"pass the exact dog id {dog_id!r} for device root /",
        )


def _recover_after_error(device_root: Path, original: BaseException) -> None:
    try:
        _recover_pending(device_root)
    except BaseException as recovery_error:
        raise IdentityError(
            "recovery_failed",
            f"original={type(original).__name__}; recovery={recovery_error}",
        ) from recovery_error


def apply_bundle(
    bundle_dir: Path,
    device_root: Path,
    *,
    execute: bool = False,
    expected_dog_id: str | None = None,
    confirm_live_device: str | None = None,
) -> ApplyResult:
    """Plan or transactionally apply one immutable bundle snapshot."""

    snapshot = validate_bundle_snapshot(bundle_dir)
    manifest = snapshot.manifest
    _check_expected_dog_id(
        execute=execute,
        expected_dog_id=expected_dog_id,
        actual_dog_id=manifest.dog_id,
    )
    root = _device_root(device_root)
    operations = tuple(f"{record.filename} -> /{record.target}" for record in manifest.files)
    if not execute:
        assert_bundle_snapshot_current(snapshot)
        return ApplyResult("dry_run", manifest.dog_id, manifest.revision, False, operations)
    _live_guard(root, manifest.dog_id, confirm_live_device)

    with _import_lock(root):
        _recover_pending(root)
        assert_bundle_snapshot_current(snapshot)
        previous_state = _read_state(root)
        if previous_state is not None and previous_state["dog_id"] != manifest.dog_id:
            raise IdentityError("device_dog_id_mismatch", str(previous_state["dog_id"]))
        if previous_state is not None and previous_state["current_revision"] == manifest.revision:
            if _installed_matches(snapshot, root):
                return ApplyResult(
                    "already_applied", manifest.dog_id, manifest.revision, False, operations
                )
            raise IdentityError("installed_state_mismatch", manifest.revision)

        baseline = _capture_baseline(manifest, root)
        backup_dir = _device_path(root, BACKUP_ROOT / manifest.revision)
        reusable_backup: BackupSnapshot | None = None
        if backup_dir.exists() or backup_dir.is_symlink():
            candidate = _load_backup_snapshot(
                backup_dir,
                expected_revision=manifest.revision,
                expected_dog_id=manifest.dog_id,
            )
            if (
                candidate.previous_state != previous_state
                or candidate.baseline != baseline
            ):
                raise IdentityError("backup_exists", manifest.revision)
            reusable_backup = candidate
        journal = _make_pending_journal(
            operation="apply",
            phase="backup_complete" if reusable_backup is not None else "backup_pending",
            dog_id=manifest.dog_id,
            revision=manifest.revision,
            previous_state=previous_state,
            baseline=baseline,
            backup_metadata_sha256=(
                None
                if reusable_backup is None
                else reusable_backup.metadata_sha256
            ),
        )
        _write_pending(root, journal)
        try:
            if reusable_backup is None:
                backup = _backup(manifest, root, previous_state, baseline)
                journal = replace(
                    journal,
                    phase="backup_complete",
                    backup_metadata_sha256=backup.metadata_sha256,
                )
                _write_pending(root, journal)
            else:
                backup = reusable_backup
                _assert_backup_current(backup)
            journal = replace(journal, phase="installing")
            _write_pending(root, journal)
            _install_payloads(snapshot, root)
            state: dict[str, object] = {
                "schema_version": 1,
                "dog_id": manifest.dog_id,
                "current_revision": manifest.revision,
                "applied_at": _utc_now(),
                "restart_required": True,
            }
            _write_json(_device_path(root, STATE_PATH, create_parent=True), state)
            journal = replace(journal, phase="state_written")
            _write_pending(root, journal)
            if not _installed_matches(snapshot, root):
                raise IdentityError("install_verification_failed", manifest.revision)
            _durable_unlink(_device_path(root, PENDING_PATH))
        except BaseException as error:
            _recover_after_error(root, error)
            raise
    return ApplyResult("applied", manifest.dog_id, manifest.revision, True, operations)


def mark_restart_complete(
    device_root: Path,
    revision: str,
    *,
    execute: bool = False,
    expected_dog_id: str | None = None,
    confirm_live_device: str | None = None,
) -> ApplyResult:
    """Record that the exact installed revision passed its harness restart check."""

    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise IdentityError("invalid_revision", str(revision))
    root = _device_root(device_root)
    state = _read_state(root)
    if state is None or state.get("current_revision") != revision:
        raise IdentityError("revision_not_current", revision)
    dog_id = state["dog_id"]
    assert isinstance(dog_id, str)
    _check_expected_dog_id(
        execute=execute,
        expected_dog_id=expected_dog_id,
        actual_dog_id=dog_id,
    )
    operations = (f"mark harness restart complete for {revision}",)
    if not execute:
        return ApplyResult(
            "restart_complete_dry_run", dog_id, revision, False, operations
        )
    _live_guard(root, dog_id, confirm_live_device)

    with _import_lock(root):
        _recover_pending(root)
        current = _read_state(root)
        if current is None or current.get("current_revision") != revision:
            raise IdentityError("revision_not_current", revision)
        if current.get("dog_id") != dog_id:
            raise IdentityError("device_dog_id_mismatch", str(current.get("dog_id")))
        if current["restart_required"] is False:
            return ApplyResult(
                "restart_already_complete", dog_id, revision, False, operations
            )
        updated = dict(current)
        updated["restart_required"] = False
        _write_json(_device_path(root, STATE_PATH), updated)
        verified = _read_state(root)
        if verified != updated:
            raise IdentityError("restart_state_verification_failed", revision)
    return ApplyResult("restart_complete", dog_id, revision, False, operations)


def rollback_bundle(
    device_root: Path,
    revision: str,
    *,
    execute: bool = False,
    expected_dog_id: str | None = None,
    confirm_live_device: str | None = None,
) -> ApplyResult:
    """Restore a revision's exact baseline and allow verified safe replay."""

    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise IdentityError("invalid_revision", str(revision))
    if execute and expected_dog_id is None:
        raise IdentityError(
            "expected_dog_id_required",
            "execute mode requires an id from a trusted device-bound wrapper",
        )
    root = _device_root(device_root)
    backup_dir = _device_path(root, BACKUP_ROOT / revision)
    initial = _load_backup_snapshot(backup_dir, expected_revision=revision)
    _check_expected_dog_id(
        execute=execute,
        expected_dog_id=expected_dog_id,
        actual_dog_id=initial.dog_id,
    )
    operations = tuple(f"restore {item.target}" for item in initial.baseline)
    if not execute:
        _assert_backup_current(initial)
        return ApplyResult("rollback_dry_run", initial.dog_id, revision, False, operations)
    _live_guard(root, initial.dog_id, confirm_live_device)

    with _import_lock(root):
        _recover_pending(root)
        backup = _load_backup_snapshot(
            backup_dir,
            expected_revision=revision,
            expected_dog_id=initial.dog_id,
        )
        if initial.metadata_bytes != backup.metadata_bytes or initial.payloads != backup.payloads:
            raise IdentityError("backup_changed_after_validation", revision)
        state = _read_state(root)
        baseline_matches = _baseline_matches(backup.baseline, root)
        if baseline_matches and state == backup.previous_state:
            return ApplyResult("already_rolled_back", backup.dog_id, revision, False, operations)
        if not isinstance(state, dict) or state.get("current_revision") != revision:
            if state == backup.previous_state:
                raise IdentityError("rollback_baseline_mismatch", revision)
            raise IdentityError("revision_not_current", revision)
        if state.get("dog_id") != backup.dog_id:
            raise IdentityError("device_dog_id_mismatch", str(state.get("dog_id")))

        journal = _make_pending_journal(
            operation="rollback",
            phase="rollback_pending",
            dog_id=backup.dog_id,
            revision=revision,
            previous_state=backup.previous_state,
            baseline=backup.baseline,
            backup_metadata_sha256=backup.metadata_sha256,
        )
        _write_pending(root, journal)
        try:
            journal = replace(journal, phase="rollback_restoring")
            _write_pending(root, journal)
            _restore_backup(backup, root)
            _restore_state(root, backup.previous_state)
            journal = replace(journal, phase="rollback_state_written")
            _write_pending(root, journal)
            if not _baseline_matches(backup.baseline, root) or not _state_matches(
                root, backup.previous_state
            ):
                raise IdentityError("rollback_verification_failed", revision)
            _durable_unlink(_device_path(root, PENDING_PATH))
        except BaseException as error:
            _recover_after_error(root, error)
            raise
    return ApplyResult("rolled_back", backup.dog_id, revision, True, operations)
