"""Strict, local HTTP control plane for identity bundle previews.

The HTTP surface deliberately has no apply, rollback, shell, source-path, or
device-root operation. Incoming Markdown is written under a server-managed
root and passed to the existing ``native_agent.identity`` API.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, Iterable, Mapping
from urllib.parse import urlsplit

from native_agent.identity import IdentityError, build_bundle, validate_bundle

MAX_REQUEST_BYTES: Final = 128 * 1024
ALLOWED_INPUT_FILES: Final = frozenset({"soul.md", "user.md", "memory.md"})
STATIC_FILES: Final[Mapping[str, tuple[str, str]]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
STATIC_ROOT: Final = Path(__file__).parent / "static"


class RequestError(ValueError):
    """HTTP-facing validation error with a stable machine code."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PreviewResult:
    dog_id: str
    revision: str
    created_at: str
    files: tuple[dict[str, object], ...]
    retained: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "ok",
            "operation": "identity_preview",
            "dog_id": self.dog_id,
            "revision": self.revision,
            "created_at": self.created_at,
            "files": list(self.files),
            "dry_run": {
                "status": "validated",
                "bundle_retained": self.retained,
                "device_changes": False,
                "apply_performed": False,
            },
        }


def default_data_root() -> Path:
    return Path.home() / ".local" / "share" / "vbot-control-plane"


def _ensure_directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"managed directory cannot be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"managed path is not a directory: {path}")
    path.chmod(0o700)
    return path.resolve()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_text(parent: Path, filename: str, value: str) -> None:
    try:
        data = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise IdentityError("invalid_utf8", filename) from error

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{filename}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, parent / filename)
        _fsync_directory(parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _normalise_loopback_host(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be localhost or an IPv4 loopback address")
    host = value.lower()
    if host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError(
            f"{label} must be localhost or an IPv4 loopback address"
        ) from error
    if address.version != 4 or not address.is_loopback:
        raise ValueError(f"{label} must be localhost or an IPv4 loopback address")
    return str(address)


def _normalise_bind_host(value: str) -> str:
    host = _normalise_loopback_host(value, label="bind host")
    # Never rely on local DNS or /etc/hosts to decide which interface is bound.
    return "127.0.0.1" if host == "localhost" else host


def _default_allowed_hosts(bind_host: str) -> frozenset[str]:
    if bind_host in {"localhost", "127.0.0.1"}:
        return frozenset({"localhost", "127.0.0.1"})
    return frozenset({bind_host})


def _validated_allowed_hosts(
    bind_host: str,
    requested: Iterable[str] | None,
) -> frozenset[str]:
    defaults = _default_allowed_hosts(bind_host)
    if requested is None:
        return defaults
    accepted = frozenset(
        _normalise_loopback_host(host, label="allowed host") for host in requested
    )
    if not accepted:
        raise ValueError("at least one loopback allowed host is required")
    if not accepted.issubset(defaults):
        raise ValueError("allowed hosts must match the loopback listener")
    return accepted


class PreviewStore:
    """Create immutable preview bundles below one server-owned data root."""

    def __init__(self, data_root: Path) -> None:
        self.root = _ensure_directory(data_root)
        self.scratch = _ensure_directory(self.root / "scratch")
        self.previews = _ensure_directory(self.root / "previews")

    def create(self, dog_id: str, files: Mapping[str, str]) -> PreviewResult:
        with tempfile.TemporaryDirectory(prefix=".preview-", dir=self.scratch) as name:
            request_root = Path(name)
            source = request_root / "source"
            source.mkdir(mode=0o700)
            for filename in ("soul.md", "user.md", "memory.md"):
                if filename in files:
                    _atomic_write_text(source, filename, files[filename])

            bundle = request_root / "bundle"
            manifest = build_bundle(source, bundle, dog_id)
            manifest = validate_bundle(bundle)
            self._make_private(bundle)

            destination_parent = _ensure_directory(self.previews / manifest.dog_id)
            destination = destination_parent / manifest.revision
            retained = self._publish(bundle, destination, manifest.to_dict())
            published = validate_bundle(destination)
            if published.to_dict() != manifest.to_dict():
                raise IdentityError("published_bundle_mismatch", manifest.revision)

        summaries = tuple(
            {
                "filename": record.filename,
                "logical_name": record.logical_name,
                "size_bytes": record.size_bytes,
                "sha256": record.sha256,
            }
            for record in published.files
        )
        return PreviewResult(
            dog_id=published.dog_id,
            revision=published.revision,
            created_at=published.created_at,
            files=summaries,
            retained=retained,
        )

    @staticmethod
    def _make_private(bundle: Path) -> None:
        for directory in (bundle, bundle / "payload"):
            directory.chmod(0o700)
        for path in bundle.rglob("*"):
            if path.is_file():
                path.chmod(0o600)

    @staticmethod
    def _publish(bundle: Path, destination: Path, expected: dict[str, object]) -> bool:
        try:
            os.rename(bundle, destination)
            _fsync_directory(destination.parent)
            return True
        except OSError as error:
            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
        existing = validate_bundle(destination)
        if existing.to_dict() != expected:
            raise IdentityError("revision_collision", destination.name)
        shutil.rmtree(bundle)
        return True


class ControlPlaneHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        store: PreviewStore,
        allowed_hosts: Iterable[str],
    ) -> None:
        bind_host = _normalise_bind_host(server_address[0])
        accepted_hosts = _validated_allowed_hosts(bind_host, allowed_hosts)
        self.store = store
        self.allowed_hostnames = accepted_hosts
        super().__init__((bind_host, server_address[1]), ControlPlaneRequestHandler)
        port = int(self.server_address[1])
        self.allowed_authorities = frozenset(
            host if port == 80 else f"{host}:{port}" for host in self.allowed_hostnames
        )
        self.allowed_origins = frozenset(
            f"http://{authority}" for authority in self.allowed_authorities
        )


def create_server(
    *,
    bind_host: str = "127.0.0.1",
    port: int = 8769,
    data_root: Path | None = None,
    allowed_hosts: Iterable[str] | None = None,
) -> ControlPlaneHTTPServer:
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    safe_bind_host = _normalise_bind_host(bind_host)
    accepted = _validated_allowed_hosts(safe_bind_host, allowed_hosts)
    store = PreviewStore(data_root or default_data_root())
    return ControlPlaneHTTPServer((safe_bind_host, port), store, accepted)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _parse_json_document(data: bytes) -> object:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RequestError(400, "invalid_utf8", "request body must be UTF-8") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except DuplicateJsonKey as error:
        raise RequestError(400, "duplicate_json_key", str(error)) from error
    except (json.JSONDecodeError, ValueError) as error:
        raise RequestError(400, "invalid_json", str(error)) from error


def _validate_preview_request(value: object) -> tuple[str, dict[str, str]]:
    if type(value) is not dict:
        raise RequestError(400, "invalid_request", "JSON root must be an object")
    actual = set(value)
    expected = {"dog_id", "files"}
    if actual != expected:
        raise RequestError(
            400,
            "invalid_request_keys",
            f"expected {sorted(expected)}, got {sorted(actual)}",
        )
    dog_id = value["dog_id"]
    files = value["files"]
    if type(dog_id) is not str:
        raise RequestError(400, "invalid_dog_id", "dog_id must be a string")
    if type(files) is not dict:
        raise RequestError(400, "invalid_files", "files must be an object")
    names = set(files)
    unknown = names - ALLOWED_INPUT_FILES
    if unknown:
        raise RequestError(
            400,
            "unknown_identity_file",
            f"unsupported files: {sorted(unknown)}",
        )
    if "soul.md" not in files:
        raise RequestError(400, "missing_file", "soul.md is required")
    result: dict[str, str] = {}
    for filename, content in files.items():
        if type(filename) is not str or type(content) is not str:
            raise RequestError(
                400,
                "invalid_file_content",
                "identity filenames and contents must be strings",
            )
        result[filename] = content
    return dog_id, result


class ControlPlaneRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VbotControlPlane"
    sys_version = ""

    @property
    def control_server(self) -> ControlPlaneHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        # Keep the default operational log while never logging request bodies.
        super().log_message(format, *args)

    def _single_header(self, name: str, *, required: bool = False) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        if len(values) > 1:
            raise RequestError(400, "duplicate_header", f"{name} must appear once")
        if not values:
            if required:
                raise RequestError(400, "missing_header", f"{name} is required")
            return None
        return values[0]

    def _guard_request(self, *, require_origin: bool) -> None:
        host = self._single_header("Host", required=True)
        if host is None or host.lower() not in self.control_server.allowed_authorities:
            raise RequestError(403, "host_rejected", "Host is not allowed")
        origin = self._single_header("Origin", required=require_origin)
        if origin is not None:
            parsed = urlsplit(origin)
            canonical = (
                parsed.scheme == "http"
                and parsed.netloc.lower() in self.control_server.allowed_authorities
                and not parsed.username
                and not parsed.password
                and parsed.path == ""
                and parsed.query == ""
                and parsed.fragment == ""
            )
            if not canonical or origin.lower() not in self.control_server.allowed_origins:
                raise RequestError(403, "origin_rejected", "Origin must match this control plane")

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    def _send_json(self, status: int, value: object) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, data, "application/json; charset=utf-8")

    def _send_request_error(self, error: RequestError) -> None:
        self._send_json(
            error.status,
            {"status": "error", "error": {"code": error.code, "detail": error.detail}},
        )

    def _route_path(self) -> str:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return ""
        return parsed.path

    def _read_json_body(self) -> object:
        if self._single_header("Transfer-Encoding") is not None:
            raise RequestError(400, "transfer_encoding_rejected", "use Content-Length")
        content_type = self._single_header("Content-Type", required=True)
        if content_type is None or not self._is_json_content_type(content_type):
            raise RequestError(415, "unsupported_media_type", "use application/json with UTF-8")
        raw_length = self._single_header("Content-Length", required=True)
        if raw_length is None or not re.fullmatch(r"[0-9]+", raw_length):
            raise RequestError(400, "invalid_content_length", "Content-Length must contain decimal digits")
        length = int(raw_length, 10)
        if length > MAX_REQUEST_BYTES:
            raise RequestError(
                413,
                "request_too_large",
                f"request body limit is {MAX_REQUEST_BYTES} bytes",
            )
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestError(400, "incomplete_body", "request body ended early")
        return _parse_json_document(body)

    @staticmethod
    def _is_json_content_type(value: str) -> bool:
        parts = [part.strip() for part in value.split(";")]
        if not parts or parts[0].lower() != "application/json":
            return False
        if len(parts) == 1:
            return True
        if len(parts) != 2 or "=" not in parts[1]:
            return False
        key, raw_value = (item.strip().lower() for item in parts[1].split("=", 1))
        return key == "charset" and raw_value.strip('"') == "utf-8"

    def do_GET(self) -> None:
        try:
            self._guard_request(require_origin=False)
            path = self._route_path()
            if path == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "vbot-local-control-plane",
                        "api_version": 1,
                        "network_scope": "loopback_only",
                        "capabilities": {
                            "identity_preview": True,
                            "identity_apply": False,
                            "shell": False,
                        },
                    },
                )
                return
            static = STATIC_FILES.get(path)
            if static is None:
                raise RequestError(404, "not_found", "route not found")
            filename, content_type = static
            self._send_bytes(HTTPStatus.OK, (STATIC_ROOT / filename).read_bytes(), content_type)
        except RequestError as error:
            self._send_request_error(error)
        except OSError:
            self._send_json(
                500,
                {"status": "error", "error": {"code": "static_unavailable", "detail": "static asset unavailable"}},
            )

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        try:
            self._guard_request(require_origin=True)
            if self._route_path() != "/api/identity/preview":
                raise RequestError(404, "not_found", "route not found")
            dog_id, files = _validate_preview_request(self._read_json_body())
            result = self.control_server.store.create(dog_id, files)
            self._send_json(HTTPStatus.OK, result.to_dict())
        except RequestError as error:
            self._send_request_error(error)
        except IdentityError as error:
            self._send_json(
                400,
                {"status": "error", "error": {"code": error.code, "detail": error.detail}},
            )
        except OSError:
            self._send_json(
                500,
                {"status": "error", "error": {"code": "storage_error", "detail": "preview could not be stored"}},
            )
        except Exception as error:
            self.log_error("identity preview failed: %s", type(error).__name__)
            self._send_json(
                500,
                {"status": "error", "error": {"code": "internal_error", "detail": "preview could not be completed"}},
            )

    def _method_not_allowed(self) -> None:
        try:
            self._guard_request(require_origin=False)
            self._send_json(
                405,
                {"status": "error", "error": {"code": "method_not_allowed", "detail": "method not allowed"}},
            )
        except RequestError as error:
            self._send_request_error(error)

    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_PUT = _method_not_allowed

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        # Parse-level HTTP errors should not fall back to BaseHTTPRequestHandler's HTML page.
        detail = message or HTTPStatus(code).phrase
        self._send_json(
            code,
            {"status": "error", "error": {"code": "invalid_http_request", "detail": detail}},
        )
