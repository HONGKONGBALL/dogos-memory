from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from native_agent.identity.importer import STATE_PATH
from native_agent.runtime.artifacts import prepare_release
from native_agent.runtime.commands import (
    build_x5_apply_command,
    build_x5_cleanup_command,
    build_x5_rollback_command,
)
from native_agent.runtime.gates import EXPECTED_SERVICE_TYPES, evaluate_gate
from native_agent.runtime.link_check import evaluate_darwin_route
from native_agent.runtime.s100_admin import (
    AdminError,
    _voice_state,
    disable_agent,
    enable_identity_only,
    restore_voice,
)
from native_agent.runtime.x5_runner import (
    DeploymentError,
    apply_identity_release,
    rollback_identity_release,
)


def valid_snapshot() -> dict[str, object]:
    return {
        "services": dict(EXPECTED_SERVICE_TYPES),
        "context": {
            "dag_status": {
                "execution_state": 0,
                "current_dag": "",
                "emergency_stop_active": False,
            },
            "function_status": {
                "follow_status": {"status": 0},
                "nav_status": {"status": 0},
            },
            "system_status": {
                "robot_is_static": True,
                "static_duration": 8.0,
                "joy_override_active": False,
                "wakeup_turn": True,
                "network_state": 1,
                "agent_enable": False,
            },
        },
        "battery": {
            "alarm": 0,
            "request_shutdown": False,
            "soc_percent": 80.0,
            "is_charger_connected": False,
            "charge_state": 1,
        },
        "faults": {"error_code": 0, "current": []},
        "speech": {
            "query_success": True,
            "chat_function_status": False,
            "cmd_recognition_mode": 0,
            "continuous_cmd_status": False,
        },
        "x5": {
            "probe_success": True,
            "harness_active": True,
            "free_bytes": 100 * 1024 * 1024,
            "memory_dir_ready": True,
            "agents_parent_ready": True,
            "identity_state": {
                "current_revision": "20260910T020000Z-aaaaaaaaaaaa",
                "restart_required": False,
            },
            "competing_agents": [],
            "policy_hook_enforced": False,
        },
    }


class RuntimeGateTests(unittest.TestCase):
    def test_install_allows_charging_but_activation_does_not(self) -> None:
        snapshot = valid_snapshot()
        battery = snapshot["battery"]
        assert isinstance(battery, dict)
        battery.update(is_charger_connected=True, charge_state=2)
        install = evaluate_gate(snapshot, purpose="install")
        self.assertTrue(install.ok)
        self.assertIn("install_while_charging_no_agent_activation", install.warnings)
        activate = evaluate_gate(
            snapshot,
            purpose="activate",
            expected_revision="20260910T020000Z-aaaaaaaaaaaa",
        )
        self.assertFalse(activate.ok)
        self.assertIn("charger_disconnected", activate.blockers)
        self.assertIn("not_actively_charging", activate.blockers)

    def test_activation_fails_closed_on_busy_fault_low_battery_or_service_drift(self) -> None:
        snapshot = valid_snapshot()
        context = snapshot["context"]
        battery = snapshot["battery"]
        faults = snapshot["faults"]
        services = snapshot["services"]
        assert isinstance(context, dict) and isinstance(battery, dict)
        assert isinstance(faults, dict) and isinstance(services, dict)
        context["dag_status"]["execution_state"] = 1  # type: ignore[index]
        battery["soc_percent"] = 10
        faults["current"] = [{"fault_id": 7}]
        services["/agent/enable"] = "wrong/Type"
        report = evaluate_gate(
            snapshot,
            purpose="activate",
            expected_revision="20260910T020000Z-bbbbbbbbbbbb",
        )
        self.assertFalse(report.ok)
        for code in (
            "dag_idle",
            "battery_above_activation_threshold",
            "no_current_faults",
            "service_type:/agent/enable",
            "identity_revision_matches",
        ):
            self.assertIn(code, report.blockers)

    def test_install_requires_the_existing_agent_and_voice_routes_to_be_off(self) -> None:
        snapshot = valid_snapshot()
        context = snapshot["context"]
        speech = snapshot["speech"]
        assert isinstance(context, dict) and isinstance(speech, dict)
        context["system_status"]["agent_enable"] = True  # type: ignore[index]
        speech["chat_function_status"] = True
        report = evaluate_gate(snapshot, purpose="install")
        self.assertFalse(report.ok)
        self.assertIn("agent_disabled_for_install", report.blockers)
        self.assertIn("chat_disabled_for_install", report.blockers)

    def test_valid_activation_is_explicit_about_missing_policy_hook(self) -> None:
        report = evaluate_gate(
            valid_snapshot(),
            purpose="activate",
            expected_revision="20260910T020000Z-aaaaaaaaaaaa",
        )
        self.assertTrue(report.ok)
        self.assertIn("native_action_policy_hook_not_enforced", report.warnings)

    def test_full_battery_is_safe_when_charger_is_disconnected(self) -> None:
        snapshot = valid_snapshot()
        battery = snapshot["battery"]
        assert isinstance(battery, dict)
        battery["charge_state"] = 3
        report = evaluate_gate(
            snapshot,
            purpose="activate",
            expected_revision="20260910T020000Z-aaaaaaaaaaaa",
        )
        self.assertTrue(report.ok)


class VoiceStateTests(unittest.TestCase):
    def test_voice_state_rejects_missing_or_coerced_switch_values(self) -> None:
        valid = {
            "context": {"system_status": {"agent_enable": False}},
            "speech": {
                "query_success": True,
                "chat_function_status": False,
                "cmd_recognition_mode": 0,
                "continuous_cmd_status": False,
            },
        }
        self.assertEqual(_voice_state(valid)["cmd_recognition_mode"], 0)
        for field, malformed in (
            ("chat_function_status", "false"),
            ("cmd_recognition_mode", None),
            ("continuous_cmd_status", 0),
        ):
            candidate = json.loads(json.dumps(valid))
            candidate["speech"][field] = malformed
            with self.subTest(field=field), self.assertRaises(AdminError):
                _voice_state(candidate)


class FakeVoiceRos:
    def __init__(self) -> None:
        self.agent = False
        self.chat = False
        self.command_mode = 0
        self.continuous = False
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_request_type_once: int | None = None

    def call(self, name: str, fields: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, dict(fields)))
        if name == "/agent/enable":
            self.agent = fields["data"] is True
            return {"success": True}
        request_type = fields["request_type"]
        if request_type == self.fail_request_type_once:
            self.fail_request_type_once = None
            return {"success": False}
        if request_type == 6:
            self.continuous = fields["enable"] is True
        elif request_type == 3:
            self.command_mode = int(fields["mode"]) if fields["enable"] is True else 0
        elif request_type == 4:
            self.chat = fields["enable"] is True
        return {"success": True}

    def snapshot(self, *args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        return {
            "context": {"system_status": {"agent_enable": self.agent}},
            "speech": {
                "query_success": True,
                "chat_function_status": self.chat,
                "cmd_recognition_mode": self.command_mode,
                "continuous_cmd_status": self.continuous,
            },
            "gate": {"ok": True, "blockers": []},
        }


class VoiceTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary.name) / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_enable_orders_switches_and_restore_returns_to_exact_prior_state(self) -> None:
        ros = FakeVoiceRos()
        revision = "20260910T020000Z-aaaaaaaaaaaa"
        with (
            patch("native_agent.runtime.s100_admin.STATE_ROOT", self.state_root),
            patch(
                "native_agent.runtime.s100_admin.collect_snapshot",
                side_effect=ros.snapshot,
            ),
        ):
            result = enable_identity_only(
                ros,  # type: ignore[arg-type]
                dog_id="dog_a",
                revision=revision,
                minimum_battery_percent=40,
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(
                [(name, values.get("request_type")) for name, values in ros.calls],
                [
                    ("/agent/enable", None),
                    ("/speech_control", 6),
                    ("/speech_control", 3),
                    ("/speech_control", 4),
                    ("/agent/enable", None),
                ],
            )
            self.assertTrue((self.state_root / "dog_a.json").is_file())
            restored = restore_voice(ros, dog_id="dog_a")  # type: ignore[arg-type]
            self.assertEqual(restored["status"], "restored")
            self.assertFalse(ros.agent)
            self.assertFalse(ros.chat)
            self.assertFalse((self.state_root / "dog_a.json").exists())

    def test_activation_failure_restores_prior_switches_and_removes_saved_state(self) -> None:
        ros = FakeVoiceRos()
        ros.fail_request_type_once = 4
        with (
            patch("native_agent.runtime.s100_admin.STATE_ROOT", self.state_root),
            patch(
                "native_agent.runtime.s100_admin.collect_snapshot",
                side_effect=ros.snapshot,
            ),
            self.assertRaises(AdminError),
        ):
            enable_identity_only(
                ros,  # type: ignore[arg-type]
                dog_id="dog_a",
                revision="20260910T020000Z-aaaaaaaaaaaa",
                minimum_battery_percent=40,
            )
        self.assertFalse(ros.agent)
        self.assertFalse(ros.chat)
        self.assertEqual(ros.command_mode, 0)
        self.assertFalse(ros.continuous)
        self.assertFalse((self.state_root / "dog_a.json").exists())

    def test_disable_keeps_restore_record_when_readback_does_not_match(self) -> None:
        ros = FakeVoiceRos()
        self.state_root.mkdir()
        state_path = self.state_root / "dog_a.json"
        state_path.write_text("{}\n", encoding="utf-8")
        bad_readback = {
            "context": {"system_status": {"agent_enable": True}},
            "speech": {
                "query_success": True,
                "chat_function_status": False,
                "cmd_recognition_mode": 0,
                "continuous_cmd_status": False,
            },
            "gate": {"ok": True, "blockers": []},
        }
        with (
            patch("native_agent.runtime.s100_admin.STATE_ROOT", self.state_root),
            patch(
                "native_agent.runtime.s100_admin.collect_snapshot",
                return_value=bad_readback,
            ),
            self.assertRaises(AdminError),
        ):
            disable_agent(ros, dog_id="dog_a")  # type: ignore[arg-type]
        self.assertTrue(state_path.is_file())


class FixedCommandTests(unittest.TestCase):
    def test_commands_accept_only_generated_identifiers(self) -> None:
        capsule = "/app_param/vbot-native-installer-0123456789abcdef.py"
        digest = "a" * 64
        revision = "20260910T020000Z-bbbbbbbbbbbb"
        envelope = f"/app_param/dogos-import-dog_a-{revision}.json"
        apply = build_x5_apply_command(
            capsule_path=capsule,
            capsule_sha256=digest,
            envelope_path=envelope,
            dog_id="dog_a",
            revision=revision,
        )
        rollback = build_x5_rollback_command(
            capsule_path=capsule,
            capsule_sha256=digest,
            dog_id="dog_a",
            revision=revision,
        )
        self.assertIn(f"sha256sum {capsule}", apply)
        self.assertIn(f"--envelope {envelope}", apply)
        self.assertIn("--execute-live-confirmation dog_a", apply)
        self.assertIn("rollback", rollback)
        self.assertEqual(build_x5_cleanup_command(capsule), f"rm -f -- {capsule}")
        for malicious in ("dog;id", "Dog", "../dog"):
            with self.subTest(malicious=malicious):
                with self.assertRaises(ValueError):
                    build_x5_apply_command(
                        capsule_path=capsule,
                        capsule_sha256=digest,
                        envelope_path=envelope,
                        dog_id=malicious,
                        revision=revision,
                    )


class LinkCheckTests(unittest.TestCase):
    def test_direct_subnet_passes_and_wifi_gateway_route_fails(self) -> None:
        direct = evaluate_darwin_route(
            "192.168.126.2",
            "gateway: link#20\ninterface: en9\n",
            "en9: flags=\n\tinet 192.168.126.10 netmask 0xffffff00\n\tstatus: active\n",
        )
        self.assertTrue(direct.ok)
        routed = evaluate_darwin_route(
            "192.168.126.2",
            "gateway: 192.168.1.1\ninterface: en0\n",
            "en0: flags=\n\tinet 192.168.1.20 netmask 0xffffff00\n\tstatus: active\n",
        )
        self.assertFalse(routed.ok)
        self.assertEqual(routed.blocker, "no_same_subnet_address")


class X5ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "identity"
        self.source.mkdir()
        (self.source / "soul.md").write_text("# Soul\n\nName: Test Dog\n", encoding="utf-8")
        (self.source / "user.md").write_text("# User\n\nSynthetic.\n", encoding="utf-8")
        self.release_dir = self.root / "release"
        self.release = prepare_release(self.source, self.release_dir, "dog_a")
        self.device = self.root / "device"
        (self.device / "app/vbot-agent-harness").mkdir(parents=True)
        (self.device / "userdata/.vbot-agent/memory").mkdir(parents=True)
        self.originals = {
            "app/vbot-agent-harness/AGENTS.md": b"factory agents\n",
            "userdata/.vbot-agent/memory/soul.md": b"factory soul\n",
            "userdata/.vbot-agent/memory/user.md": b"factory user\n",
        }
        for relative, data in self.originals.items():
            (self.device / relative).write_bytes(data)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_apply_restart_marker_idempotence_and_rollback(self) -> None:
        revision = str(self.release["revision"])
        first = apply_identity_release(
            self.release_dir / "identity-envelope.json",
            self.device,
            expected_dog_id="dog_a",
            expected_revision=revision,
            execute=True,
            restart=lambda: None,
        )
        self.assertEqual(first.status, "ready")
        state = json.loads((self.device / STATE_PATH).read_text(encoding="utf-8"))
        self.assertFalse(state["restart_required"])

        second = apply_identity_release(
            self.release_dir / "identity-envelope.json",
            self.device,
            expected_dog_id="dog_a",
            expected_revision=revision,
            execute=True,
            restart=lambda: None,
        )
        self.assertEqual(second.status, "ready")
        self.assertIn("already_applied", second.importer_status)

        rolled_back = rollback_identity_release(
            self.device,
            dog_id="dog_a",
            revision=revision,
            execute=True,
            restart=lambda: None,
        )
        self.assertEqual(rolled_back.status, "rolled_back")
        for relative, data in self.originals.items():
            self.assertEqual((self.device / relative).read_bytes(), data)

    def test_failed_new_harness_restart_restores_baseline(self) -> None:
        calls = 0

        def fail_then_recover() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DeploymentError("synthetic_restart_failure", "test")

        with self.assertRaises(DeploymentError) as raised:
            apply_identity_release(
                self.release_dir / "identity-envelope.json",
                self.device,
                expected_dog_id="dog_a",
                expected_revision=str(self.release["revision"]),
                execute=True,
                restart=fail_then_recover,
            )
        self.assertEqual(raised.exception.code, "rolled_back_after_restart_failure")
        self.assertEqual(calls, 2)
        for relative, data in self.originals.items():
            self.assertEqual((self.device / relative).read_bytes(), data)

    def test_release_is_stable_for_unchanged_identity(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            second = prepare_release(self.source, Path(other) / "release", "dog_a")
        self.assertEqual(second["revision"], self.release["revision"])
        self.assertEqual(second["identity_hashes"], self.release["identity_hashes"])

    def test_built_admin_and_x5_capsule_are_executable_python_artifacts(self) -> None:
        for artifact in ("s100-admin.pyz", "x5-installer.py"):
            with self.subTest(artifact=artifact):
                completed = subprocess.run(
                    [sys.executable, str(self.release_dir / artifact), "--help"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("usage:", completed.stdout)


if __name__ == "__main__":
    unittest.main()
