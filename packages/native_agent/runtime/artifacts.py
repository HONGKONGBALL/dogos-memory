"""Build deterministic S100 and X5 deployment artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from native_agent.identity.bundle import FILE_SPECS, IdentityError, build_bundle
from native_agent.identity.staging import pack_staging_envelope


ARCHIVE_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
MAX_X5_WRAPPER_BYTES = 256 * 1024


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def _python_root() -> Path:
    return _root() / "packages"


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _python_files(*directories: Path) -> list[Path]:
    result: list[Path] = []
    for directory in directories:
        result.extend(path for path in directory.glob("*.py") if path.is_file())
    return sorted(set(result))


def _write_archive(
    output: Path,
    *,
    main_module: str,
    source_files: Iterable[Path],
) -> None:
    if output.exists() or output.is_symlink():
        raise IdentityError("output_exists", str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    entries: dict[str, bytes] = {
        "__main__.py": (f"from {main_module} import main\nraise SystemExit(main())\n").encode(
            "utf-8"
        )
    }
    for source in source_files:
        relative = source.relative_to(_python_root()).as_posix()
        entries[relative] = source.read_bytes()
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, ARCHIVE_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, entries[name])
    os.chmod(output, 0o700)


def build_s100_admin(output: Path) -> dict[str, object]:
    project = _root()
    files = [project / "packages/native_agent/__init__.py"]
    files += _python_files(project / "packages/native_agent/identity")
    files += _python_files(project / "packages/native_agent/runtime")
    _write_archive(
        output,
        main_module="native_agent.runtime.s100_admin",
        source_files=files,
    )
    return {"path": str(output), "size_bytes": output.stat().st_size, "sha256": _hash(output)}


def _x5_archive(path: Path) -> None:
    project = _root()
    files = [project / "packages/native_agent/__init__.py"]
    files += _python_files(project / "packages/native_agent/identity")
    files += [
        project / "packages/native_agent/runtime/__init__.py",
        project / "packages/native_agent/runtime/gates.py",
        project / "packages/native_agent/runtime/x5_runner.py",
    ]
    _write_archive(
        path,
        main_module="native_agent.runtime.x5_runner",
        source_files=files,
    )


def build_x5_capsule(output: Path) -> dict[str, object]:
    if output.exists() or output.is_symlink():
        raise IdentityError("output_exists", str(output))
    temporary = output.with_name(f".{output.name}.payload.pyz")
    try:
        _x5_archive(temporary)
        payload = temporary.read_bytes()
    finally:
        temporary.unlink(missing_ok=True)
    payload_sha = hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode("ascii")
    lines = "\n".join(
        f'    "{encoded[index : index + 96]}"' for index in range(0, len(encoded), 96)
    )
    wrapper = f'''#!/usr/bin/env python3
import base64
import hashlib
import os
import subprocess
import sys
import tempfile

PAYLOAD = (
{lines}
)
EXPECTED = "{payload_sha}"
data = base64.b64decode(PAYLOAD, validate=True)
if hashlib.sha256(data).hexdigest() != EXPECTED:
    raise SystemExit("embedded payload checksum mismatch")
descriptor, name = tempfile.mkstemp(prefix="vbot-native-agent-", suffix=".pyz")
try:
    os.fchmod(descriptor, 0o700)
    with os.fdopen(descriptor, "wb") as handle:
        descriptor = -1
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    completed = subprocess.run([sys.executable, name, *sys.argv[1:]], check=False)
    raise SystemExit(completed.returncode)
finally:
    if descriptor >= 0:
        os.close(descriptor)
    try:
        os.unlink(name)
    except FileNotFoundError:
        pass
'''.encode("utf-8")
    if len(wrapper) > MAX_X5_WRAPPER_BYTES:
        raise IdentityError("capsule_too_large", str(len(wrapper)))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as handle:
        handle.write(wrapper)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(output, 0o700)
    return {
        "path": str(output),
        "size_bytes": len(wrapper),
        "sha256": hashlib.sha256(wrapper).hexdigest(),
        "payload_sha256": payload_sha,
    }


def _deterministic_created_at(source_dir: Path) -> datetime:
    candidates = [
        source_dir / filename
        for filename in FILE_SPECS
        if filename != "AGENTS.md" and (source_dir / filename).is_file()
    ]
    candidates.append(_root() / "packages/native_agent/identity/templates/AGENTS.md")
    latest = max(path.stat().st_mtime for path in candidates)
    now = datetime.now(timezone.utc).timestamp()
    if latest > now + 300:
        raise IdentityError("source_mtime_in_future", str(int(latest)))
    return datetime.fromtimestamp(latest, timezone.utc).replace(microsecond=0)


def prepare_release(source_dir: Path, output_dir: Path, dog_id: str) -> dict[str, object]:
    """Build one stable release; unchanged source bytes keep the same revision."""

    if output_dir.exists() or output_dir.is_symlink():
        raise IdentityError("output_exists", str(output_dir))
    output_dir.mkdir(parents=True, mode=0o700)
    try:
        manifest = build_bundle(
            source_dir,
            output_dir / "bundle",
            dog_id,
            created_at=_deterministic_created_at(source_dir),
        )
        envelope = output_dir / "identity-envelope.json"
        packed = pack_staging_envelope(output_dir / "bundle", envelope)
        os.chmod(envelope, 0o600)
        s100 = build_s100_admin(output_dir / "s100-admin.pyz")
        x5 = build_x5_capsule(output_dir / "x5-installer.py")
        source_hashes = {
            record.filename: record.sha256
            for record in manifest.files
            if record.filename != "AGENTS.md"
        }
        release = {
            "schema_version": 1,
            "dog_id": manifest.dog_id,
            "revision": manifest.revision,
            "identity_hashes": source_hashes,
            "envelope": {
                "name": envelope.name,
                "size_bytes": packed["size_bytes"],
                "sha256": packed["sha256"],
            },
            "s100_admin": {
                "name": Path(str(s100["path"])).name,
                "size_bytes": s100["size_bytes"],
                "sha256": s100["sha256"],
            },
            "x5_installer": {
                "name": Path(str(x5["path"])).name,
                "size_bytes": x5["size_bytes"],
                "sha256": x5["sha256"],
                "payload_sha256": x5["payload_sha256"],
            },
        }
        release_path = output_dir / "release.json"
        release_path.write_text(
            json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(release_path, 0o600)
        return release
    except BaseException:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
