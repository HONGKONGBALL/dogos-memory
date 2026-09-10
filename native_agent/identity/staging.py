"""Pack validated identity snapshots into a fixed staging envelope."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .bundle import (
    FILE_SPECS,
    IdentityError,
    IdentityManifest,
    _parse_manifest,
    _strict_json_loads,
    _validate_manifest_payloads,
    assert_bundle_snapshot_current,
    validate_bundle_snapshot,
)

ENVELOPE_SCHEMA_VERSION = 1
MAX_ENVELOPE_BYTES = 128 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ValidatedEnvelope:
    """One validated envelope and the exact bytes that were validated."""

    source: Path
    raw_bytes: bytes
    manifest: IdentityManifest
    payloads: tuple[tuple[str, bytes], ...]
    content_sha256: str


def _canonical_manifest(manifest: IdentityManifest) -> bytes:
    return json.dumps(
        manifest.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _content_hash(manifest: IdentityManifest, payloads: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256(_canonical_manifest(manifest))
    for filename in FILE_SPECS:
        if filename in payloads:
            digest.update(filename.encode("utf-8"))
            digest.update(payloads[filename])
    return digest.hexdigest()


def _load_validated_envelope(path: Path) -> ValidatedEnvelope:
    """Read an envelope once and validate all embedded bytes in memory."""

    if path.is_symlink() or not path.is_file():
        raise IdentityError("invalid_envelope", str(path))
    raw = path.read_bytes()
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise IdentityError("envelope_too_large", str(len(raw)))
    try:
        value = _strict_json_loads(raw, label="staging envelope")
    except IdentityError as error:
        if error.code == "duplicate_json_key":
            raise
        raise IdentityError("invalid_envelope", error.detail) from error
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "manifest",
        "payload_b64",
        "content_sha256",
    }:
        raise IdentityError("invalid_envelope", "unexpected root shape")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise IdentityError("invalid_envelope", "schema_version")
    manifest_value = value["manifest"]
    payload_value = value["payload_b64"]
    digest_value = value["content_sha256"]
    if not isinstance(manifest_value, dict) or not isinstance(payload_value, dict):
        raise IdentityError("invalid_envelope", "manifest or payload_b64")
    if not isinstance(digest_value, str) or not _SHA256_RE.fullmatch(digest_value):
        raise IdentityError("invalid_envelope", "content_sha256")

    manifest = _parse_manifest(manifest_value)
    payloads: dict[str, bytes] = {}
    for filename, encoded in payload_value.items():
        if (
            not isinstance(filename, str)
            or filename not in FILE_SPECS
            or not isinstance(encoded, str)
        ):
            raise IdentityError("invalid_envelope_payload", str(filename))
        try:
            payloads[filename] = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise IdentityError("invalid_envelope_payload", filename) from error
    validated_payloads = _validate_manifest_payloads(manifest, payloads)
    computed = _content_hash(manifest, payloads)
    if computed != digest_value:
        raise IdentityError("envelope_hash_mismatch", digest_value)
    return ValidatedEnvelope(
        source=path.resolve(),
        raw_bytes=raw,
        manifest=manifest,
        payloads=validated_payloads,
        content_sha256=digest_value,
    )


def assert_envelope_snapshot_current(snapshot: ValidatedEnvelope) -> None:
    """Reject replacement or mutation after envelope validation."""

    path = snapshot.source
    if path.is_symlink() or not path.is_file():
        raise IdentityError("envelope_changed_after_validation", str(path))
    try:
        current = path.read_bytes()
    except OSError as error:
        raise IdentityError("envelope_changed_after_validation", str(path)) from error
    if current != snapshot.raw_bytes:
        raise IdentityError("envelope_changed_after_validation", str(path))


def pack_staging_envelope(bundle_dir: Path, output_file: Path) -> dict[str, object]:
    """Write one envelope from the exact validated bundle snapshot."""

    snapshot = validate_bundle_snapshot(bundle_dir)
    if output_file.exists() or output_file.is_symlink():
        raise IdentityError("output_exists", str(output_file))
    payloads = dict(snapshot.payloads)
    envelope: dict[str, object] = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "manifest": snapshot.manifest.to_dict(),
        "payload_b64": {
            filename: base64.b64encode(data).decode("ascii")
            for filename, data in snapshot.payloads
        },
        "content_sha256": _content_hash(snapshot.manifest, payloads),
    }
    data = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    if len(data) > MAX_ENVELOPE_BYTES:
        raise IdentityError("envelope_too_large", str(len(data)))
    assert_bundle_snapshot_current(snapshot)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "status": "packed",
        "dog_id": snapshot.manifest.dog_id,
        "revision": snapshot.manifest.revision,
        "path": str(output_file),
        "x5_staging_path": (
            f"/app_param/dogos-import-{snapshot.manifest.dog_id}-"
            f"{snapshot.manifest.revision}.json"
        ),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def unpack_staging_envelope(envelope_file: Path, output_dir: Path) -> IdentityManifest:
    """Reconstruct a bundle solely from one validated envelope snapshot."""

    if output_dir.exists() or output_dir.is_symlink():
        raise IdentityError("output_exists", str(output_dir))
    snapshot = _load_validated_envelope(envelope_file)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".identity-unpack-", dir=output_dir.parent))
    try:
        manifest_data = json.dumps(
            snapshot.manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8") + b"\n"
        (temporary / "payload").mkdir()
        (temporary / "manifest.json").write_bytes(manifest_data)
        for filename, data in snapshot.payloads:
            (temporary / "payload" / filename).write_bytes(data)
        rebuilt = validate_bundle_snapshot(temporary)
        if rebuilt.manifest != snapshot.manifest or rebuilt.payloads != snapshot.payloads:
            raise IdentityError("envelope_rebuild_mismatch", snapshot.manifest.revision)
        assert_envelope_snapshot_current(snapshot)
        os.replace(temporary, output_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return validate_bundle_snapshot(output_dir).manifest
