"""Trusted S100 administrator for one Vbot native Agent.

This program is copied to S100 as a zipapp and invoked only over a host-key
verified SSH session.  It is intentionally separate from every Agent/MCP tool
surface.  State-changing subcommands use fixed ROS services and fixed X5
commands assembled from validated identifiers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from native_agent.identity.bundle import DOG_ID_RE, REVISION_RE, SHA256_RE, IdentityError
from native_agent.identity.staging import (
    _load_validated_envelope,
    assert_envelope_snapshot_current,
)
from native_agent.runtime.commands import (
    X5_READ_ONLY_PROBE_COMMAND,
    build_x5_apply_command,
    build_x5_cleanup_command,
    build_x5_rollback_command,
)
from native_agent.runtime.gates import EXPECTED_SERVICE_TYPES, evaluate_gate


STATE_ROOT = Path("/userdata/vbot/native-agent-admin/state")
INBOX_ROOT = Path("/userdata/vbot/native-agent-admin/inbox")
MAX_CAPSULE_BYTES = 256 * 1024
CAPSULE_TARGET_RE = re.compile(r"^/app_param/vbot-native-installer-[0-9a-f]{16}\.py$")


class AdminError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _plain(value: object) -> object:
    if hasattr(value, "get_fields_and_field_types"):
        from rosidl_runtime_py.convert import message_to_ordereddict

        return _plain(message_to_ordereddict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "tolist"):
        return _plain(value.tolist())
    if isinstance(value, bytes):
        return list(value)
    return value


class RosAccess:
    def __init__(self) -> None:
        import rclpy

        self.rclpy = rclpy
        rclpy.init(args=None)
        self.node = rclpy.create_node("vbot_native_agent_admin")

    def close(self) -> None:
        self.node.destroy_node()
        self.rclpy.shutdown()

    def service_types(self) -> dict[str, str]:
        # The global ROS graph includes names held only by clients. A listed
        # /agent/enable without any server must not pass activation preflight.
        return {
            name: types[0]
            for node_name, namespace in self.node.get_node_names_and_namespaces()
            for name, types in self.node.get_service_names_and_types_by_node(node_name, namespace)
            if types
        }

    def call(self, name: str, fields: Mapping[str, object], timeout: float = 8.0) -> dict[str, object]:
        from rosidl_runtime_py.set_message import set_message_fields
        from rosidl_runtime_py.utilities import get_service

        services = self.service_types()
        type_name = services.get(name)
        if type_name is None:
            raise AdminError("service_missing", name)
        service_type = get_service(type_name)
        request = service_type.Request()
        try:
            set_message_fields(request, dict(fields))
        except (TypeError, ValueError, AttributeError, AssertionError) as error:
            raise AdminError("invalid_service_request", name) from error
        client = self.node.create_client(service_type, name)
        try:
            if not client.wait_for_service(timeout_sec=min(timeout, 2.0)):
                raise AdminError("service_unavailable", name)
            future = client.call_async(request)
            deadline = time.monotonic() + timeout
            while not future.done() and time.monotonic() < deadline:
                self.rclpy.spin_once(self.node, timeout_sec=0.05)
            if not future.done():
                future.cancel()
                raise AdminError("service_outcome_unknown", name)
            error = future.exception()
            if error is not None:
                raise AdminError("service_failed", name) from error
            value = _plain(future.result())
            if not isinstance(value, dict):
                raise AdminError("invalid_service_response", name)
            return value
        finally:
            self.node.destroy_client(client)

    def topic_once(self, name: str, timeout: float = 6.0) -> dict[str, object]:
        from rclpy.qos import qos_profile_sensor_data
        from rosidl_runtime_py.utilities import get_message

        topics = {
            topic: types[0]
            for topic, types in self.node.get_topic_names_and_types()
            if types
        }
        type_name = topics.get(name)
        if type_name is None:
            raise AdminError("topic_missing", name)
        received: list[object] = []
        subscription = self.node.create_subscription(
            get_message(type_name), name, received.append, qos_profile_sensor_data
        )
        try:
            deadline = time.monotonic() + timeout
            while not received and time.monotonic() < deadline:
                self.rclpy.spin_once(self.node, timeout_sec=0.1)
            if not received:
                raise AdminError("topic_timeout", name)
            value = _plain(received[0])
            if not isinstance(value, dict):
                raise AdminError("invalid_topic_message", name)
            return value
        finally:
            self.node.destroy_subscription(subscription)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _strict_inbox_file(path: Path, *, maximum: int) -> bytes:
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise AdminError("invalid_inbox_path", path.name) from error
    if resolved_parent != INBOX_ROOT or path.is_symlink() or not path.is_file():
        raise AdminError("invalid_inbox_path", path.name)
    size = path.stat().st_size
    if size < 1 or size > maximum:
        raise AdminError("invalid_inbox_size", path.name)
    return path.read_bytes()


def _safe_context(raw: Mapping[str, object]) -> dict[str, object]:
    snapshot = raw.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise AdminError("invalid_context", "snapshot")
    result: dict[str, object] = {}
    for name in ("dag_status", "function_status", "system_status", "rcp_status"):
        value = snapshot.get(name)
        if isinstance(value, Mapping):
            result[name] = dict(value)
    return result


def _safe_battery(raw: Mapping[str, object]) -> dict[str, object]:
    allowed = {
        "request_shutdown",
        "last_alarm",
        "voltage_mv",
        "current_ma",
        "soc_percent",
        "soh_percent",
        "alarm",
        "vendor_alarm",
        "is_charger_connected",
        "charge_state",
    }
    result = {name: raw.get(name) for name in sorted(allowed)}
    result["charge_state_supported"] = "charge_state" in raw
    return result


def _query_speech(ros: RosAccess) -> dict[str, object]:
    responses = [
        ros.call("/speech_control", {"request_type": request_type})
        for request_type in (1, 2, 5)
    ]
    success = all(item.get("success") is True for item in responses)
    modes = {item.get("cmd_recognition_mode") for item in responses}
    chats = {item.get("chat_function_status") for item in responses}
    continuous = {item.get("continuous_cmd_status") for item in responses}
    consistent = len(modes) == len(chats) == len(continuous) == 1
    return {
        "query_success": success and consistent,
        "cmd_recognition_mode": responses[-1].get("cmd_recognition_mode"),
        "chat_function_status": responses[-1].get("chat_function_status"),
        "continuous_cmd_status": responses[-1].get("continuous_cmd_status"),
    }


def _current_faults(ros: RosAccess) -> dict[str, object]:
    # ROS ``byte`` is represented as a one-byte bytes value, unlike uint8.
    response = ros.call("/get_faults_info", {"type": b"\x03", "fault_list": []})
    items = response.get("fault_info_array")
    safe_items: list[dict[str, object]] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, Mapping):
                safe_items.append(
                    {
                        "fault_id": item.get("fault_id"),
                        "level": item.get("level"),
                        "soc_id": item.get("soc_id"),
                    }
                )
    return {"error_code": response.get("error_code"), "current": safe_items}


def _parse_x5_response(response: Mapping[str, object], *, operation: str) -> dict[str, object]:
    if response.get("success") is not True or response.get("ret_code") != 0:
        raise AdminError(f"x5_{operation}_failed", f"return code {response.get('ret_code')}")
    output = response.get("output")
    if not isinstance(output, str):
        raise AdminError(f"x5_{operation}_invalid", "missing output")
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise AdminError(f"x5_{operation}_invalid", "empty output")
    try:
        value = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise AdminError(f"x5_{operation}_invalid", "non-JSON output") from error
    if not isinstance(value, dict):
        raise AdminError(f"x5_{operation}_invalid", "output shape")
    return value


def _x5_probe(ros: RosAccess) -> dict[str, object]:
    try:
        response = ros.call(
            "/execute_x5_command", {"command": X5_READ_ONLY_PROBE_COMMAND}, timeout=12.0
        )
        return _parse_x5_response(response, operation="probe")
    except AdminError as error:
        return {
            "probe_success": False,
            "harness_active": False,
            "free_bytes": 0,
            "memory_dir_ready": False,
            "agents_parent_ready": False,
            "identity_state": None,
            "competing_agents": [],
            "policy_hook_enforced": False,
            "probe_error": error.code,
        }


def collect_snapshot(
    ros: RosAccess,
    *,
    purpose: str,
    expected_revision: str | None = None,
    minimum_battery_percent: float = 40.0,
) -> dict[str, object]:
    services = ros.service_types()
    selected_services = {
        name: services.get(name)
        for name in EXPECTED_SERVICE_TYPES
        if services.get(name) is not None
    }
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "captured_at": _utc_now(),
        "services": selected_services,
        "context": _safe_context(ros.call("/function/context/get_context", {})),
        "battery": _safe_battery(ros.topic_once("/bms_state")),
        "faults": _current_faults(ros),
        "speech": _query_speech(ros),
        "x5": _x5_probe(ros),
    }
    snapshot["gate"] = evaluate_gate(
        snapshot,
        purpose=purpose,
        expected_revision=expected_revision,
        minimum_battery_percent=minimum_battery_percent,
    ).to_dict()
    return snapshot


def _require_gate(snapshot: Mapping[str, object]) -> None:
    gate = snapshot.get("gate")
    if not isinstance(gate, Mapping) or gate.get("ok") is not True:
        blockers = gate.get("blockers") if isinstance(gate, Mapping) else None
        detail = ",".join(str(item) for item in blockers) if isinstance(blockers, list) else "invalid gate"
        raise AdminError("preflight_blocked", detail)


def _write_x5(ros: RosAccess, path: str, content: str) -> None:
    response = ros.call("/write_x5_file", {"path": path, "content": content}, timeout=15.0)
    if response.get("success") is not True:
        raise AdminError("x5_stage_rejected", path)


def _cleanup_x5_staging(ros: RosAccess, *paths: str) -> str | None:
    try:
        response = ros.call(
            "/execute_x5_command",
            {"command": build_x5_cleanup_command(*paths)},
            timeout=8.0,
        )
    except AdminError:
        return "x5_staging_cleanup_outcome_unknown"
    if response.get("success") is not True or response.get("ret_code") != 0:
        return "x5_staging_cleanup_failed"
    return None


def _capsule(
    capsule_file: Path, expected_sha256: str
) -> tuple[str, str, str]:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise AdminError("invalid_capsule_digest", expected_sha256)
    raw = _strict_inbox_file(capsule_file, maximum=MAX_CAPSULE_BYTES)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise AdminError("capsule_hash_mismatch", capsule_file.name)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AdminError("capsule_not_utf8", capsule_file.name) from error
    target = f"/app_param/vbot-native-installer-{actual[:16]}.py"
    if not CAPSULE_TARGET_RE.fullmatch(target):
        raise AdminError("invalid_capsule_target", target)
    return target, actual, content


def install_release(
    ros: RosAccess,
    *,
    envelope_file: Path,
    envelope_sha256: str,
    capsule_file: Path,
    capsule_sha256: str,
    dog_id: str,
    revision: str,
) -> dict[str, object]:
    if not DOG_ID_RE.fullmatch(dog_id) or not REVISION_RE.fullmatch(revision):
        raise AdminError("invalid_release_identity", "dog id or revision")
    if not SHA256_RE.fullmatch(envelope_sha256):
        raise AdminError("invalid_envelope_digest", envelope_sha256)
    preflight = collect_snapshot(ros, purpose="install")
    _require_gate(preflight)

    envelope_bytes = _strict_inbox_file(envelope_file, maximum=128 * 1024)
    if hashlib.sha256(envelope_bytes).hexdigest() != envelope_sha256:
        raise AdminError("envelope_hash_mismatch", envelope_file.name)
    try:
        envelope = _load_validated_envelope(envelope_file)
    except IdentityError as error:
        raise AdminError(error.code, error.detail) from error
    if envelope.raw_bytes != envelope_bytes:
        raise AdminError("envelope_changed_after_hash", envelope_file.name)
    if envelope.manifest.dog_id != dog_id or envelope.manifest.revision != revision:
        raise AdminError("release_identity_mismatch", revision)
    assert_envelope_snapshot_current(envelope)
    envelope_text = envelope.raw_bytes.decode("utf-8")
    envelope_target = (
        f"/app_param/dogos-import-{dog_id}-{revision}.json"
    )
    capsule_target, capsule_digest, capsule_text = _capsule(capsule_file, capsule_sha256)

    staged = False
    try:
        _write_x5(ros, capsule_target, capsule_text)
        staged = True
        _write_x5(ros, envelope_target, envelope_text)
        command = build_x5_apply_command(
            capsule_path=capsule_target,
            capsule_sha256=capsule_digest,
            envelope_path=envelope_target,
            dog_id=dog_id,
            revision=revision,
        )
        response = ros.call("/execute_x5_command", {"command": command}, timeout=50.0)
        installed = _parse_x5_response(response, operation="install")
        if installed.get("status") != "ready" or installed.get("revision") != revision:
            raise AdminError("x5_install_not_ready", str(installed.get("status")))
    except AdminError as error:
        if staged and error.code != "service_outcome_unknown":
            _cleanup_x5_staging(ros, envelope_target, capsule_target)
        raise

    cleanup_warning = _cleanup_x5_staging(ros, envelope_target, capsule_target)

    postflight = collect_snapshot(ros, purpose="install")
    state = postflight.get("x5", {}).get("identity_state") if isinstance(postflight.get("x5"), Mapping) else None
    if not isinstance(state, Mapping) or state.get("current_revision") != revision or state.get("restart_required") is not False:
        raise AdminError("post_install_verification_failed", revision)
    return {
        "status": "installed",
        "dog_id": dog_id,
        "revision": revision,
        "x5": installed,
        "cleanup_warning": cleanup_warning,
        "postflight": postflight["gate"],
    }


def _state_path(dog_id: str) -> Path:
    if not DOG_ID_RE.fullmatch(dog_id):
        raise AdminError("invalid_dog_id", dog_id)
    return STATE_ROOT / f"{dog_id}.json"


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.parent.resolve() != STATE_ROOT.resolve():
        raise AdminError("unsafe_state_directory", str(path.parent))
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _set_agent(ros: RosAccess, enabled: bool) -> None:
    response = ros.call("/agent/enable", {"data": enabled})
    if response.get("success") is not True:
        raise AdminError("agent_switch_rejected", str(enabled))


def _set_speech(ros: RosAccess, request_type: int, enabled: bool, mode: int = 0) -> None:
    response = ros.call(
        "/speech_control",
        {"request_type": request_type, "enable": enabled, "mode": mode},
    )
    if response.get("success") is not True:
        raise AdminError("speech_switch_rejected", str(request_type))


def _voice_state(snapshot: Mapping[str, object]) -> dict[str, object]:
    context = snapshot.get("context")
    system = context.get("system_status") if isinstance(context, Mapping) else None
    speech = snapshot.get("speech")
    if not isinstance(system, Mapping) or not isinstance(speech, Mapping):
        raise AdminError("invalid_voice_state", "context")
    agent_enabled = system.get("agent_enable")
    chat_enabled = speech.get("chat_function_status")
    command_mode = speech.get("cmd_recognition_mode")
    continuous_enabled = speech.get("continuous_cmd_status")
    if speech.get("query_success") is not True:
        raise AdminError("invalid_voice_state", "speech query")
    if not isinstance(agent_enabled, bool):
        raise AdminError("invalid_voice_state", "agent switch")
    if not isinstance(chat_enabled, bool) or not isinstance(continuous_enabled, bool):
        raise AdminError("invalid_voice_state", "speech switches")
    if (
        isinstance(command_mode, bool)
        or not isinstance(command_mode, int)
        or command_mode not in {0, 1, 2}
    ):
        raise AdminError("invalid_voice_state", "command mode")
    return {
        "agent_enable": agent_enabled,
        "chat_function_status": chat_enabled,
        "cmd_recognition_mode": command_mode,
        "continuous_cmd_status": continuous_enabled,
    }


def _apply_voice_state(ros: RosAccess, state: Mapping[str, object]) -> None:
    agent_enabled = state.get("agent_enable")
    chat_enabled = state.get("chat_function_status")
    continuous_enabled = state.get("continuous_cmd_status")
    if not all(
        isinstance(value, bool)
        for value in (agent_enabled, chat_enabled, continuous_enabled)
    ):
        raise AdminError("invalid_saved_voice_state", "switches")
    _set_agent(ros, False)
    command_mode = state.get("cmd_recognition_mode")
    if isinstance(command_mode, bool) or not isinstance(command_mode, int) or command_mode not in {0, 1, 2}:
        raise AdminError("invalid_saved_voice_state", "cmd mode")
    _set_speech(ros, 6, continuous_enabled, 0)
    _set_speech(ros, 3, command_mode != 0, command_mode)
    _set_speech(ros, 4, chat_enabled, 0)
    if agent_enabled:
        _set_agent(ros, True)


def enable_identity_only(
    ros: RosAccess,
    *,
    dog_id: str,
    revision: str,
    minimum_battery_percent: float,
) -> dict[str, object]:
    if not DOG_ID_RE.fullmatch(dog_id) or not REVISION_RE.fullmatch(revision):
        raise AdminError("invalid_release_identity", "dog id or revision")
    return _enable_voice(
        ros, dog_id=dog_id, revision=revision,
        minimum_battery_percent=minimum_battery_percent, profile="identity-only",
    )


def enable_existing_voice(
    ros: RosAccess,
    *,
    dog_id: str,
    minimum_battery_percent: float = 40.0,
) -> dict[str, object]:
    """Enable factory voice using its current identity, without importing files."""
    if not DOG_ID_RE.fullmatch(dog_id):
        raise AdminError("invalid_dog_id", dog_id)
    return _enable_voice(
        ros, dog_id=dog_id, revision=None,
        minimum_battery_percent=minimum_battery_percent, profile="existing-identity-voice",
    )


def _enable_voice(
    ros: RosAccess,
    *,
    dog_id: str,
    revision: str | None,
    minimum_battery_percent: float,
    profile: str,
) -> dict[str, object]:
    before = collect_snapshot(
        ros,
        purpose="activate",
        expected_revision=revision,
        minimum_battery_percent=minimum_battery_percent,
    )
    _require_gate(before)
    previous = _voice_state(before)
    desired = {
        "agent_enable": True,
        "chat_function_status": True,
        "cmd_recognition_mode": 0,
        "continuous_cmd_status": False,
    }
    state_path = _state_path(dog_id)
    if previous == desired:
        return {
            "status": "already_ready",
            "profile": profile,
            "dog_id": dog_id,
            "revision": revision,
            "wake_words": ["大头大头", "Hey_Babo"],
            "voice": previous,
            "gate": before["gate"],
            "body_actions": "not_enabled_or_validated_by_this_launcher",
            "voice_round_trip": "not_tested",
        }
    if previous != desired:
        if state_path.exists() or state_path.is_symlink():
            raise AdminError("saved_state_exists", state_path.name)
        _atomic_json(
            state_path,
            {
                "schema_version": 1,
                "dog_id": dog_id,
                "revision": revision,
                "captured_at": _utc_now(),
                "voice": previous,
            },
        )
    try:
        _set_agent(ros, False)
        _set_speech(ros, 6, False, 0)
        _set_speech(ros, 3, False, 0)
        _set_speech(ros, 4, True, 0)
        _set_agent(ros, True)
        after = collect_snapshot(
            ros,
            purpose="activate",
            expected_revision=revision,
            minimum_battery_percent=minimum_battery_percent,
        )
        _require_gate(after)
        actual = _voice_state(after)
        if actual != desired:
            raise AdminError("voice_activation_verification_failed", json.dumps(actual, sort_keys=True))
    except BaseException as failure:
        try:
            _apply_voice_state(ros, previous)
        except BaseException as restore_failure:
            raise AdminError(
                "activation_and_restore_failed",
                f"activation={failure}; restore={restore_failure}",
            ) from restore_failure
        if state_path.exists():
            state_path.unlink()
        raise
    return {
        "status": "ready",
        "profile": profile,
        "dog_id": dog_id,
        "revision": revision,
        "wake_words": ["大头大头", "Hey_Babo"],
        "voice": actual,
        "gate": after["gate"],
        "body_actions": "not_enabled_or_validated_by_this_launcher",
        "voice_round_trip": "not_tested",
    }


def disable_agent(ros: RosAccess, *, dog_id: str | None = None) -> dict[str, object]:
    saved_path: Path | None = None
    if dog_id is not None:
        saved_path = _state_path(dog_id)
        if saved_path.parent.exists() and (
            saved_path.parent.is_symlink()
            or saved_path.parent.resolve() != STATE_ROOT.resolve()
        ):
            raise AdminError("unsafe_state_directory", str(saved_path.parent))
        if saved_path.exists() and (saved_path.is_symlink() or not saved_path.is_file()):
            raise AdminError("invalid_saved_state", saved_path.name)
    _set_agent(ros, False)
    _set_speech(ros, 6, False, 0)
    _set_speech(ros, 3, False, 0)
    _set_speech(ros, 4, False, 0)
    after = collect_snapshot(ros, purpose="inspect")
    actual = _voice_state(after)
    desired = {
        "agent_enable": False,
        "chat_function_status": False,
        "cmd_recognition_mode": 0,
        "continuous_cmd_status": False,
    }
    if actual != desired:
        raise AdminError("voice_disable_verification_failed", json.dumps(actual, sort_keys=True))
    if saved_path is not None and saved_path.exists():
        saved_path.unlink()
    return {"status": "disabled", "voice": actual}


def restore_voice(ros: RosAccess, *, dog_id: str) -> dict[str, object]:
    path = _state_path(dog_id)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.parent.resolve() != STATE_ROOT.resolve()
    ):
        raise AdminError("saved_state_missing", path.name)
    if path.stat().st_size > 4096:
        raise AdminError("invalid_saved_state", path.name)
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AdminError("invalid_saved_state", path.name) from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("dog_id") != dog_id
        or not isinstance(value.get("voice"), Mapping)
    ):
        raise AdminError("invalid_saved_state", path.name)
    voice = value["voice"]
    # A rejected first request can leave the exact original state intact.
    # Verify that case without demanding an unavailable switch service.
    before = collect_snapshot(ros, purpose="inspect")
    if _voice_state(before) != dict(voice):
        _apply_voice_state(ros, voice)
    after = collect_snapshot(ros, purpose="inspect")
    actual = _voice_state(after)
    if actual != dict(voice):
        raise AdminError("voice_restore_verification_failed", json.dumps(actual, sort_keys=True))
    path.unlink()
    return {"status": "restored", "voice": actual}


def rollback_release(
    ros: RosAccess,
    *,
    capsule_file: Path,
    capsule_sha256: str,
    dog_id: str,
    revision: str,
) -> dict[str, object]:
    disable_agent(ros, dog_id=dog_id)
    capsule_target, capsule_digest, capsule_text = _capsule(capsule_file, capsule_sha256)
    _write_x5(ros, capsule_target, capsule_text)
    command = build_x5_rollback_command(
        capsule_path=capsule_target,
        capsule_sha256=capsule_digest,
        dog_id=dog_id,
        revision=revision,
    )
    try:
        response = ros.call("/execute_x5_command", {"command": command}, timeout=50.0)
        rolled_back = _parse_x5_response(response, operation="rollback")
        if rolled_back.get("status") != "rolled_back":
            raise AdminError("x5_rollback_not_ready", str(rolled_back.get("status")))
    except AdminError as error:
        if error.code != "service_outcome_unknown":
            _cleanup_x5_staging(ros, capsule_target)
        raise
    cleanup_warning = _cleanup_x5_staging(ros, capsule_target)
    return {
        "status": "rolled_back",
        "dog_id": dog_id,
        "revision": revision,
        "x5": rolled_back,
        "cleanup_warning": cleanup_warning,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vbot-native-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--purpose", choices=("inspect", "install", "activate"), default="inspect")
    inspect.add_argument("--expected-revision")
    inspect.add_argument("--minimum-battery", type=float, default=40.0)

    install = commands.add_parser("install")
    install.add_argument("--envelope", type=Path, required=True)
    install.add_argument("--envelope-sha256", required=True)
    install.add_argument("--capsule", type=Path, required=True)
    install.add_argument("--capsule-sha256", required=True)
    install.add_argument("--dog-id", required=True)
    install.add_argument("--revision", required=True)

    enable = commands.add_parser("enable")
    enable.add_argument("--dog-id", required=True)
    enable.add_argument("--revision", required=True)
    enable.add_argument("--minimum-battery", type=float, default=40.0)

    voice_start = commands.add_parser("voice-start")
    voice_start.add_argument("--dog-id", required=True)
    voice_start.add_argument("--minimum-battery", type=float, default=40.0)

    disable = commands.add_parser("disable")
    disable.add_argument("--dog-id")

    restore = commands.add_parser("restore")
    restore.add_argument("--dog-id", required=True)

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--capsule", type=Path, required=True)
    rollback.add_argument("--capsule-sha256", required=True)
    rollback.add_argument("--dog-id", required=True)
    rollback.add_argument("--revision", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ros: RosAccess | None = None
    try:
        ros = RosAccess()
        if args.command == "inspect":
            result = collect_snapshot(
                ros,
                purpose=args.purpose,
                expected_revision=args.expected_revision,
                minimum_battery_percent=args.minimum_battery,
            )
        elif args.command == "install":
            result = install_release(
                ros,
                envelope_file=args.envelope,
                envelope_sha256=args.envelope_sha256,
                capsule_file=args.capsule,
                capsule_sha256=args.capsule_sha256,
                dog_id=args.dog_id,
                revision=args.revision,
            )
        elif args.command == "enable":
            result = enable_identity_only(
                ros,
                dog_id=args.dog_id,
                revision=args.revision,
                minimum_battery_percent=args.minimum_battery,
            )
        elif args.command == "voice-start":
            result = enable_existing_voice(
                ros,
                dog_id=args.dog_id,
                minimum_battery_percent=args.minimum_battery,
            )
        elif args.command == "disable":
            result = disable_agent(ros, dog_id=args.dog_id)
        elif args.command == "restore":
            result = restore_voice(ros, dog_id=args.dog_id)
        else:
            result = rollback_release(
                ros,
                capsule_file=args.capsule,
                capsule_sha256=args.capsule_sha256,
                dog_id=args.dog_id,
                revision=args.revision,
            )
    except (AdminError, IdentityError, ValueError) as error:
        code = error.code if isinstance(error, (AdminError, IdentityError)) else "invalid_argument"
        detail = error.detail if isinstance(error, (AdminError, IdentityError)) else str(error)
        print(json.dumps({"status": "error", "code": code, "detail": detail}, sort_keys=True))
        return 2
    finally:
        if ros is not None:
            ros.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
