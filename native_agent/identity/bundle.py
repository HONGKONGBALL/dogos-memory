"""Build and validate immutable Vbot identity bundles.

Validation reads every manifest and payload byte into one immutable snapshot.
Callers that later mutate state use that snapshot and verify that its source did
not change before committing, avoiding validate-then-reopen races.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Mapping

SCHEMA_VERSION: Final = 1
DOG_ID_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REVISION_RE: Final = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
TOTAL_LIMIT_BYTES: Final = 64 * 1024
MANIFEST_LIMIT_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class FileSpec:
    logical_name: str
    target: str
    max_bytes: int
    required: bool


FILE_SPECS: Final[Mapping[str, FileSpec]] = {
    "AGENTS.md": FileSpec(
        logical_name="embodiment_rules",
        target="app/vbot-agent-harness/AGENTS.md",
        max_bytes=16 * 1024,
        required=True,
    ),
    "soul.md": FileSpec(
        logical_name="soul",
        target="userdata/.vbot-agent/memory/soul.md",
        max_bytes=24 * 1024,
        required=True,
    ),
    "user.md": FileSpec(
        logical_name="user",
        target="userdata/.vbot-agent/memory/user.md",
        max_bytes=16 * 1024,
        required=False,
    ),
    "memory.md": FileSpec(
        logical_name="memory",
        target="userdata/.vbot-agent/memory/memory.md",
        max_bytes=32 * 1024,
        required=False,
    ),
}

SOURCE_FILENAMES: Final = ("soul.md", "user.md", "memory.md")
TEMPLATE_PATH: Final = Path(__file__).parent / "templates" / "AGENTS.md"


class IdentityError(ValueError):
    """Stable validation error for UI and CLI callers."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class _DuplicateJsonKey(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


@dataclass(frozen=True, slots=True)
class FileRecord:
    logical_name: str
    filename: str
    bundle_path: str
    target: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "logical_name": self.logical_name,
            "filename": self.filename,
            "bundle_path": self.bundle_path,
            "target": self.target,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class IdentityManifest:
    schema_version: int
    dog_id: str
    revision: str
    created_at: str
    files: tuple[FileRecord, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "dog_id": self.dog_id,
            "revision": self.revision,
            "created_at": self.created_at,
            "files": [record.to_dict() for record in self.files],
        }


@dataclass(frozen=True, slots=True)
class ValidatedBundle:
    """One fully validated, immutable in-memory view of a bundle."""

    source_dir: Path
    manifest: IdentityManifest
    manifest_bytes: bytes
    payloads: tuple[tuple[str, bytes], ...]

    def payload(self, filename: str) -> bytes:
        for candidate, data in self.payloads:
            if candidate == filename:
                return data
        raise IdentityError("missing_bundle_file", filename)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_dog_id(dog_id: str) -> None:
    if not DOG_ID_RE.fullmatch(dog_id):
        raise IdentityError(
            "invalid_dog_id",
            "use 1-64 lowercase letters, digits, underscores, or hyphens",
        )


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _strict_json_loads(data: bytes, *, label: str) -> object:
    """Decode UTF-8 JSON while rejecting duplicate keys at every depth."""

    try:
        text = data.decode("utf-8")
        return json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except _DuplicateJsonKey as error:
        raise IdentityError("duplicate_json_key", f"{label}: {error.key}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IdentityError("invalid_json", f"{label}: {error}") from error


def _validate_markdown_bytes(data: bytes, filename: str, spec: FileSpec) -> None:
    if not data:
        raise IdentityError("empty_file", filename)
    if len(data) > spec.max_bytes:
        raise IdentityError(
            "file_too_large", f"{filename} is {len(data)} bytes; limit is {spec.max_bytes}"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise IdentityError("invalid_utf8", filename) from error
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise IdentityError("control_character", filename)


def _read_markdown(path: Path, spec: FileSpec) -> bytes:
    if path.is_symlink():
        raise IdentityError("symlink_rejected", str(path))
    if not path.is_file():
        raise IdentityError("missing_file", str(path))
    data = path.read_bytes()
    _validate_markdown_bytes(data, path.name, spec)
    return data


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _canonical_created_at(value: datetime | None) -> tuple[str, str]:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise IdentityError("invalid_created_at", "datetime must include a timezone")
    utc = moment.astimezone(timezone.utc).replace(microsecond=0)
    return utc.isoformat().replace("+00:00", "Z"), utc.strftime("%Y%m%dT%H%M%SZ")


def _strict_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise IdentityError(
            "invalid_manifest_keys",
            f"{label}: expected {sorted(expected)}, got {sorted(actual)}",
        )


def _parse_manifest(data: object) -> IdentityManifest:
    if not isinstance(data, dict):
        raise IdentityError("invalid_manifest", "root must be an object")
    _strict_keys(data, {"schema_version", "dog_id", "revision", "created_at", "files"}, "root")
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
        raise IdentityError("unsupported_schema", str(data["schema_version"]))
    dog_id = data["dog_id"]
    revision = data["revision"]
    created_at = data["created_at"]
    files = data["files"]
    if not isinstance(dog_id, str):
        raise IdentityError("invalid_manifest", "dog_id must be a string")
    _validate_dog_id(dog_id)
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise IdentityError("invalid_revision", str(revision))
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise IdentityError("invalid_created_at", str(created_at))
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise IdentityError("invalid_created_at", created_at) from error
    if not isinstance(files, list):
        raise IdentityError("invalid_manifest", "files must be an array")

    records: list[FileRecord] = []
    seen_by_field: dict[str, set[str]] = {
        "filename": set(),
        "logical_name": set(),
        "bundle_path": set(),
        "target": set(),
    }
    expected_keys = {"logical_name", "filename", "bundle_path", "target", "size_bytes", "sha256"}
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise IdentityError("invalid_manifest", f"files[{index}] must be an object")
        _strict_keys(item, expected_keys, f"files[{index}]")
        filename = item["filename"]
        if not isinstance(filename, str) or filename not in FILE_SPECS:
            raise IdentityError("unknown_bundle_file", str(filename))
        spec = FILE_SPECS[filename]
        expected = {
            "filename": filename,
            "logical_name": spec.logical_name,
            "bundle_path": f"payload/{filename}",
            "target": spec.target,
        }
        for key, expected_value in expected.items():
            if item[key] != expected_value:
                raise IdentityError("invalid_file_record", f"{filename}.{key}")
            if expected_value in seen_by_field[key]:
                raise IdentityError("duplicate_file_record", f"{key}: {expected_value}")
            seen_by_field[key].add(expected_value)
        size = item["size_bytes"]
        digest = item["sha256"]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or size > spec.max_bytes
        ):
            raise IdentityError("invalid_file_record", f"{filename}.size_bytes")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise IdentityError("invalid_file_record", f"{filename}.sha256")
        records.append(
            FileRecord(
                logical_name=spec.logical_name,
                filename=filename,
                bundle_path=f"payload/{filename}",
                target=spec.target,
                size_bytes=size,
                sha256=digest,
            )
        )
    required = {name for name, spec in FILE_SPECS.items() if spec.required}
    seen_names = seen_by_field["filename"]
    if not required.issubset(seen_names):
        raise IdentityError("missing_bundle_file", ", ".join(sorted(required - seen_names)))
    return IdentityManifest(SCHEMA_VERSION, dog_id, revision, created_at, tuple(records))


def _validate_manifest_payloads(
    manifest: IdentityManifest, payloads: Mapping[str, bytes]
) -> tuple[tuple[str, bytes], ...]:
    declared = {record.filename for record in manifest.files}
    if set(payloads) != declared:
        raise IdentityError(
            "undeclared_payload", f"declared={sorted(declared)}, found={sorted(payloads)}"
        )
    total = 0
    for record in manifest.files:
        data = payloads[record.filename]
        _validate_markdown_bytes(data, record.filename, FILE_SPECS[record.filename])
        total += len(data)
        if len(data) != record.size_bytes:
            raise IdentityError("size_mismatch", record.filename)
        if _sha256(data) != record.sha256:
            raise IdentityError("hash_mismatch", record.filename)
    if total > TOTAL_LIMIT_BYTES:
        raise IdentityError("bundle_too_large", str(total))

    digest = hashlib.sha256()
    digest.update(manifest.dog_id.encode("utf-8"))
    for filename in FILE_SPECS:
        if filename in payloads:
            digest.update(filename.encode("utf-8"))
            digest.update(payloads[filename])
    created = datetime.fromisoformat(manifest.created_at.replace("Z", "+00:00"))
    expected_revision = (
        f"{created.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{digest.hexdigest()[:12]}"
    )
    if manifest.revision != expected_revision:
        raise IdentityError("revision_mismatch", manifest.revision)
    return tuple((record.filename, payloads[record.filename]) for record in manifest.files)


def validate_bundle_snapshot(bundle_dir: Path) -> ValidatedBundle:
    """Read and validate an entire bundle exactly once into memory."""

    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise IdentityError("invalid_bundle_directory", str(bundle_dir))
    entries = list(bundle_dir.iterdir())
    found_root = {entry.name for entry in entries}
    expected_root = {"manifest.json", "payload"}
    if found_root != expected_root:
        raise IdentityError(
            "undeclared_bundle_entry",
            f"expected={sorted(expected_root)}, found={sorted(found_root)}",
        )

    manifest_path = bundle_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise IdentityError("missing_manifest", str(manifest_path))
    manifest_bytes = manifest_path.read_bytes()
    if len(manifest_bytes) > MANIFEST_LIMIT_BYTES:
        raise IdentityError("manifest_too_large", str(len(manifest_bytes)))
    try:
        raw_manifest = _strict_json_loads(manifest_bytes, label="manifest.json")
    except IdentityError as error:
        if error.code == "duplicate_json_key":
            raise
        raise IdentityError("invalid_manifest_json", error.detail) from error
    manifest = _parse_manifest(raw_manifest)

    payload_dir = bundle_dir / "payload"
    if payload_dir.is_symlink() or not payload_dir.is_dir():
        raise IdentityError("invalid_payload_directory", str(payload_dir))
    payload_entries = list(payload_dir.iterdir())
    payload_names = {entry.name for entry in payload_entries}
    declared = {record.filename for record in manifest.files}
    if payload_names != declared:
        raise IdentityError(
            "undeclared_payload", f"declared={sorted(declared)}, found={sorted(payload_names)}"
        )
    payloads = {
        record.filename: _read_markdown(
            payload_dir / record.filename, FILE_SPECS[record.filename]
        )
        for record in manifest.files
    }
    validated_payloads = _validate_manifest_payloads(manifest, payloads)
    return ValidatedBundle(
        source_dir=bundle_dir.resolve(),
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        payloads=validated_payloads,
    )


def assert_bundle_snapshot_current(snapshot: ValidatedBundle) -> None:
    """Reject a bundle whose source changed after its validated snapshot."""

    try:
        current = validate_bundle_snapshot(snapshot.source_dir)
    except IdentityError as error:
        raise IdentityError("bundle_changed_after_validation", error.code) from error
    if (
        current.manifest_bytes != snapshot.manifest_bytes
        or current.payloads != snapshot.payloads
    ):
        raise IdentityError("bundle_changed_after_validation", str(snapshot.source_dir))


def validate_bundle(bundle_dir: Path) -> IdentityManifest:
    """Validate a bundle and return its manifest for compatibility."""

    return validate_bundle_snapshot(bundle_dir).manifest


def build_bundle(
    source_dir: Path,
    output_dir: Path,
    dog_id: str,
    *,
    created_at: datetime | None = None,
    agents_template: Path | None = None,
) -> IdentityManifest:
    """Create one immutable bundle from fixed Markdown source filenames."""

    _validate_dog_id(dog_id)
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise IdentityError("missing_source_directory", str(source_dir))
    if output_dir.exists() or output_dir.is_symlink():
        raise IdentityError("output_exists", str(output_dir))

    payloads: dict[str, bytes] = {}
    template = agents_template or TEMPLATE_PATH
    payloads["AGENTS.md"] = _read_markdown(template, FILE_SPECS["AGENTS.md"])
    for filename in SOURCE_FILENAMES:
        path = source_dir / filename
        if path.exists() or path.is_symlink():
            payloads[filename] = _read_markdown(path, FILE_SPECS[filename])
        elif FILE_SPECS[filename].required:
            raise IdentityError("missing_file", filename)

    total = sum(len(data) for data in payloads.values())
    if total > TOTAL_LIMIT_BYTES:
        raise IdentityError(
            "bundle_too_large", f"payload is {total} bytes; limit is {TOTAL_LIMIT_BYTES}"
        )
    created_at_text, revision_prefix = _canonical_created_at(created_at)
    content_digest = hashlib.sha256()
    content_digest.update(dog_id.encode("utf-8"))
    for filename in FILE_SPECS:
        if filename in payloads:
            content_digest.update(filename.encode("utf-8"))
            content_digest.update(payloads[filename])
    revision = f"{revision_prefix}-{content_digest.hexdigest()[:12]}"
    records = tuple(
        FileRecord(
            logical_name=FILE_SPECS[filename].logical_name,
            filename=filename,
            bundle_path=f"payload/{filename}",
            target=FILE_SPECS[filename].target,
            size_bytes=len(data),
            sha256=_sha256(data),
        )
        for filename, data in payloads.items()
    )
    manifest = IdentityManifest(
        schema_version=SCHEMA_VERSION,
        dog_id=dog_id,
        revision=revision,
        created_at=created_at_text,
        files=records,
    )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".identity-bundle-", dir=output_dir.parent))
    try:
        for filename, data in payloads.items():
            _write_bytes(temporary / "payload" / filename, data)
        manifest_data = json.dumps(
            manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8") + b"\n"
        _write_bytes(temporary / "manifest.json", manifest_data)
        os.replace(temporary, output_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return validate_bundle(output_dir)
