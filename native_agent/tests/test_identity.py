from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from native_agent.identity.bundle import (
    IdentityError,
    build_bundle,
    validate_bundle,
    validate_bundle_snapshot,
)
from native_agent.identity.importer import (
    APPLY_PHASES,
    BACKUP_ROOT,
    PENDING_PATH,
    ROLLBACK_PHASES,
    STATE_PATH,
    _atomic_write,
    _backup,
    _capture_baseline,
    _install_payloads,
    _load_backup_snapshot,
    _make_pending_journal,
    _recover_pending,
    _restore_backup,
    _restore_state,
    _write_json,
    _write_pending,
    apply_bundle,
    mark_restart_complete,
    rollback_bundle,
)
from native_agent.identity.stage_to_x5 import plan_stage, stage_to_x5
from native_agent.identity.staging import (
    _load_validated_envelope,
    pack_staging_envelope,
    unpack_staging_envelope,
)


class IdentityBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "soul.md").write_text("# Soul\n\nName: Harbor\n", encoding="utf-8")
        (self.source / "user.md").write_text("# User\n\nCalls me Chen.\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, name: str = "bundle"):
        return build_bundle(
            self.source,
            self.root / name,
            "dog_a",
            created_at=datetime(2026, 9, 10, 2, 0, tzinfo=timezone.utc),
        )

    def test_build_preserves_source_and_generates_neutral_agents(self) -> None:
        manifest = self.build()
        bundle = self.root / "bundle"
        self.assertEqual(manifest.dog_id, "dog_a")
        self.assertEqual(
            (bundle / "payload/soul.md").read_bytes(),
            (self.source / "soul.md").read_bytes(),
        )
        agents = (bundle / "payload/AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Identity", agents)
        self.assertNotIn("Da Tou Bo Bo", agents)
        self.assertEqual(
            {record.filename for record in manifest.files},
            {"AGENTS.md", "soul.md", "user.md"},
        )
        self.assertEqual(validate_bundle(bundle), manifest)

    def test_missing_or_unsafe_source_is_rejected(self) -> None:
        (self.source / "soul.md").unlink()
        with self.assertRaisesRegex(IdentityError, "missing_file"):
            self.build()
        target = self.root / "actual-soul.md"
        target.write_text("# soul\n", encoding="utf-8")
        (self.source / "soul.md").symlink_to(target)
        with self.assertRaisesRegex(IdentityError, "symlink_rejected"):
            self.build("symlink-bundle")

    def test_invalid_dog_id_and_control_character_are_rejected(self) -> None:
        with self.assertRaisesRegex(IdentityError, "invalid_dog_id"):
            build_bundle(self.source, self.root / "bad", "Dog A")
        (self.source / "soul.md").write_bytes(b"# soul\x00\n")
        with self.assertRaisesRegex(IdentityError, "control_character"):
            self.build("control-bundle")

    def test_payload_tampering_is_detected(self) -> None:
        self.build()
        (self.root / "bundle/payload/soul.md").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "size_mismatch|hash_mismatch"):
            validate_bundle(self.root / "bundle")

    def test_bundle_rejects_extra_root_and_payload_entries(self) -> None:
        self.build()
        bundle = self.root / "bundle"
        (bundle / "extra.txt").write_text("extra\n", encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "undeclared_bundle_entry"):
            validate_bundle(bundle)
        (bundle / "extra.txt").unlink()
        (bundle / "payload/extra.md").write_text("extra\n", encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "undeclared_payload"):
            validate_bundle(bundle)

    def test_manifest_rejects_duplicate_json_keys(self) -> None:
        self.build()
        path = self.root / "bundle/manifest.json"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"dog_id": "dog_a",', '"dog_id": "dog_a",\n  "dog_id": "dog_a",'), encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "duplicate_json_key"):
            validate_bundle(self.root / "bundle")

    def test_manifest_binds_all_file_identity_fields_and_uniqueness(self) -> None:
        self.build()
        path = self.root / "bundle/manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["files"][1]["logical_name"] = "user"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "invalid_file_record"):
            validate_bundle(self.root / "bundle")

        self.build("duplicate")
        path = self.root / "duplicate/manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["files"].append(dict(value["files"][0]))
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "duplicate_file_record"):
            validate_bundle(self.root / "duplicate")

    def test_staging_round_trip_and_duplicate_key_rejection(self) -> None:
        manifest = self.build()
        envelope = self.root / "stage.json"
        packed = pack_staging_envelope(self.root / "bundle", envelope)
        self.assertEqual(packed["status"], "packed")
        self.assertEqual(
            packed["x5_staging_path"],
            f"/app_param/dogos-import-dog_a-{manifest.revision}.json",
        )
        self.assertEqual(plan_stage(envelope).target, packed["x5_staging_path"])
        self.assertEqual(stage_to_x5(envelope)["status"], "dry_run")
        self.assertEqual(
            unpack_staging_envelope(envelope, self.root / "unpacked"), manifest
        )

        raw = envelope.read_text(encoding="utf-8")
        duplicate = self.root / "duplicate-envelope.json"
        duplicate.write_text(raw.replace('{"content_sha256":', '{"schema_version":1,"content_sha256":', 1), encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "duplicate_json_key"):
            unpack_staging_envelope(duplicate, self.root / "duplicate-output")

    def test_pack_rejects_bundle_replaced_after_snapshot_validation(self) -> None:
        self.build()
        bundle = self.root / "bundle"
        original = validate_bundle_snapshot

        def validate_then_replace(path: Path):
            snapshot = original(path)
            (path / "payload/soul.md").write_text("changed after validation\n", encoding="utf-8")
            return snapshot

        with patch(
            "native_agent.identity.staging.validate_bundle_snapshot",
            side_effect=validate_then_replace,
        ):
            with self.assertRaisesRegex(IdentityError, "bundle_changed_after_validation"):
                pack_staging_envelope(bundle, self.root / "should-not-exist.json")
        self.assertFalse((self.root / "should-not-exist.json").exists())

    def test_stage_rejects_envelope_replaced_after_validation(self) -> None:
        self.build()
        envelope = self.root / "stage.json"
        pack_staging_envelope(self.root / "bundle", envelope)
        original = _load_validated_envelope

        def validate_then_replace(path: Path):
            snapshot = original(path)
            path.write_text("{}\n", encoding="utf-8")
            return snapshot

        with patch(
            "native_agent.identity.stage_to_x5._load_validated_envelope",
            side_effect=validate_then_replace,
        ):
            with self.assertRaisesRegex(IdentityError, "envelope_changed_after_validation"):
                stage_to_x5(envelope, execute=True, expected_dog_id="dog_a")

    def test_stage_execute_requires_matching_expected_dog_id(self) -> None:
        self.build()
        envelope = self.root / "stage.json"
        pack_staging_envelope(self.root / "bundle", envelope)
        with self.assertRaisesRegex(IdentityError, "expected_dog_id_required"):
            stage_to_x5(envelope, execute=True)
        with self.assertRaisesRegex(IdentityError, "dog_id_mismatch"):
            stage_to_x5(envelope, execute=True, expected_dog_id="dog_b")

    def test_stage_execute_rejects_non_finite_timeout_before_ros_import(self) -> None:
        self.build()
        envelope = self.root / "stage.json"
        pack_staging_envelope(self.root / "bundle", envelope)
        for timeout in (float("nan"), float("inf"), 0.0, 60.1, True):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(IdentityError, "invalid_timeout"):
                    stage_to_x5(
                        envelope,
                        execute=True,
                        expected_dog_id="dog_a",
                        timeout_seconds=timeout,
                    )

    def test_staging_payload_tamper_and_revision_tamper_are_rejected(self) -> None:
        self.build()
        envelope = self.root / "stage.json"
        pack_staging_envelope(self.root / "bundle", envelope)
        value = json.loads(envelope.read_text(encoding="utf-8"))
        value["payload_b64"]["soul.md"] = "dGFtcGVyZWQK"
        damaged = self.root / "damaged.json"
        damaged.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "size_mismatch|hash_mismatch"):
            unpack_staging_envelope(damaged, self.root / "damaged-output")

        self.build("revision")
        path = self.root / "revision/manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["revision"] = "20260910T020000Z-000000000000"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "revision_mismatch"):
            validate_bundle(self.root / "revision")


class IdentityImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "soul.md").write_text("# New Soul\n", encoding="utf-8")
        (self.source / "user.md").write_text("# New User\n", encoding="utf-8")
        self.bundle = self.root / "bundle"
        self.manifest = build_bundle(
            self.source,
            self.bundle,
            "dog_a",
            created_at=datetime(2026, 9, 10, 2, 1, tzinfo=timezone.utc),
        )
        self.snapshot = validate_bundle_snapshot(self.bundle)
        self.device, self.targets = self.make_device("device")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_restart_completion_requires_current_revision_and_is_idempotent(self) -> None:
        self.apply()
        dry_run = mark_restart_complete(
            self.device,
            self.manifest.revision,
            expected_dog_id="dog_a",
        )
        self.assertEqual(dry_run.status, "restart_complete_dry_run")

        completed = mark_restart_complete(
            self.device,
            self.manifest.revision,
            execute=True,
            expected_dog_id="dog_a",
        )
        self.assertEqual(completed.status, "restart_complete")
        state = json.loads((self.device / STATE_PATH).read_text(encoding="utf-8"))
        self.assertFalse(state["restart_required"])

        repeated = mark_restart_complete(
            self.device,
            self.manifest.revision,
            execute=True,
            expected_dog_id="dog_a",
        )
        self.assertEqual(repeated.status, "restart_already_complete")

        with self.assertRaisesRegex(IdentityError, "revision_not_current"):
            mark_restart_complete(
                self.device,
                "20260910T020000Z-000000000000",
                execute=True,
                expected_dog_id="dog_a",
            )

    def make_device(self, name: str) -> tuple[Path, dict[str, Path]]:
        device = self.root / name
        device.mkdir()
        targets = {
            "AGENTS.md": device / "app/vbot-agent-harness/AGENTS.md",
            "soul.md": device / "userdata/.vbot-agent/memory/soul.md",
            "user.md": device / "userdata/.vbot-agent/memory/user.md",
            "memory.md": device / "userdata/.vbot-agent/memory/memory.md",
        }
        for filename, path in targets.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"old {filename}\n", encoding="utf-8")
        return device, targets

    @staticmethod
    def bytes_of(targets: dict[str, Path]) -> dict[str, bytes]:
        return {name: path.read_bytes() for name, path in targets.items()}

    def apply(self, device: Path | None = None):
        return apply_bundle(
            self.bundle,
            device or self.device,
            execute=True,
            expected_dog_id="dog_a",
        )

    def rollback(self, device: Path | None = None):
        return rollback_bundle(
            device or self.device,
            self.manifest.revision,
            execute=True,
            expected_dog_id="dog_a",
        )

    def test_dry_run_does_not_change_device(self) -> None:
        before = self.bytes_of(self.targets)
        result = apply_bundle(self.bundle, self.device)
        self.assertEqual(result.status, "dry_run")
        self.assertFalse(result.restart_required)
        self.assertEqual(before, self.bytes_of(self.targets))
        self.assertFalse((self.device / STATE_PATH).exists())

    def test_execute_requires_expected_dog_id_for_apply_and_rollback(self) -> None:
        before = self.bytes_of(self.targets)
        with self.assertRaisesRegex(IdentityError, "expected_dog_id_required"):
            apply_bundle(self.bundle, self.device, execute=True)
        self.assertEqual(before, self.bytes_of(self.targets))
        self.apply()
        installed = self.bytes_of(self.targets)
        with self.assertRaisesRegex(IdentityError, "expected_dog_id_required"):
            rollback_bundle(self.device, self.manifest.revision, execute=True)
        self.assertEqual(installed, self.bytes_of(self.targets))

    def test_wrong_dog_is_rejected_before_changes(self) -> None:
        before = self.bytes_of(self.targets)
        with self.assertRaisesRegex(IdentityError, "dog_id_mismatch"):
            apply_bundle(
                self.bundle,
                self.device,
                execute=True,
                expected_dog_id="dog_b",
            )
        self.assertEqual(before, self.bytes_of(self.targets))
        self.apply()
        installed = self.bytes_of(self.targets)
        with self.assertRaisesRegex(IdentityError, "dog_id_mismatch"):
            rollback_bundle(
                self.device,
                self.manifest.revision,
                execute=True,
                expected_dog_id="dog_b",
            )
        self.assertEqual(installed, self.bytes_of(self.targets))

    def test_apply_and_rollback_are_idempotent_with_safe_replay(self) -> None:
        before = self.bytes_of(self.targets)
        result = self.apply()
        self.assertEqual(result.status, "applied")
        self.assertTrue(result.restart_required)
        self.assertEqual(self.targets["soul.md"].read_text(), "# New Soul\n")
        self.assertEqual(self.targets["memory.md"].read_bytes(), before["memory.md"])
        self.assertEqual(self.apply().status, "already_applied")

        rolled_back = self.rollback()
        self.assertEqual(rolled_back.status, "rolled_back")
        self.assertEqual(before, self.bytes_of(self.targets))
        self.assertFalse((self.device / STATE_PATH).exists())
        replay = self.rollback()
        self.assertEqual(replay.status, "already_rolled_back")
        self.assertFalse(replay.restart_required)

        self.targets["soul.md"].write_text("external change\n", encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "rollback_baseline_mismatch"):
            self.rollback()

    def test_same_revision_can_be_reapplied_after_verified_rollback(self) -> None:
        before = self.bytes_of(self.targets)
        self.assertEqual(self.apply().status, "applied")
        self.assertEqual(self.rollback().status, "rolled_back")
        self.assertEqual(before, self.bytes_of(self.targets))

        reapplied = self.apply()
        self.assertEqual(reapplied.status, "applied")
        self.assertEqual(self.targets["soul.md"].read_text(), "# New Soul\n")
        self.assertEqual(self.rollback().status, "rolled_back")
        self.assertEqual(before, self.bytes_of(self.targets))

    def test_apply_rejects_payload_replaced_after_validation(self) -> None:
        before = self.bytes_of(self.targets)
        original = validate_bundle_snapshot

        def validate_then_replace(path: Path):
            snapshot = original(path)
            (path / "payload/soul.md").write_text("changed after validation\n", encoding="utf-8")
            return snapshot

        with patch(
            "native_agent.identity.importer.validate_bundle_snapshot",
            side_effect=validate_then_replace,
        ):
            with self.assertRaisesRegex(IdentityError, "bundle_changed_after_validation"):
                self.apply()
        self.assertEqual(before, self.bytes_of(self.targets))
        self.assertFalse((self.device / PENDING_PATH).exists())

    def test_apply_journal_exists_before_backup_and_failure_recovers(self) -> None:
        before = self.bytes_of(self.targets)

        def fail_backup(*_args, **_kwargs):
            pending = json.loads((self.device / PENDING_PATH).read_text(encoding="utf-8"))
            self.assertEqual(pending["operation"], "apply")
            self.assertEqual(pending["phase"], "backup_pending")
            raise OSError("synthetic backup failure")

        with patch("native_agent.identity.importer._backup", side_effect=fail_backup):
            with self.assertRaisesRegex(OSError, "synthetic backup"):
                self.apply()
        self.assertEqual(before, self.bytes_of(self.targets))
        self.assertFalse((self.device / PENDING_PATH).exists())

    def test_partial_install_failure_recovers_exact_baseline(self) -> None:
        before = self.bytes_of(self.targets)

        def fail_after_first(snapshot, device_root):
            first = snapshot.manifest.files[0]
            _atomic_write(
                device_root / first.target,
                snapshot.payload(first.filename),
                0o644,
            )
            raise OSError("synthetic install failure")

        with patch(
            "native_agent.identity.importer._install_payloads",
            side_effect=fail_after_first,
        ):
            with self.assertRaisesRegex(OSError, "synthetic install"):
                self.apply()
        self.assertEqual(before, self.bytes_of(self.targets))
        self.assertFalse((self.device / STATE_PATH).exists())
        self.assertFalse((self.device / PENDING_PATH).exists())
        self.assertFalse((self.device / BACKUP_ROOT / self.manifest.revision).exists())

    def test_recovery_handles_every_apply_journal_phase(self) -> None:
        for phase in APPLY_PHASES:
            with self.subTest(phase=phase):
                device, targets = self.make_device(f"apply-{phase}")
                before = self.bytes_of(targets)
                baseline = _capture_baseline(self.manifest, device)
                journal = _make_pending_journal(
                    operation="apply",
                    phase="backup_pending",
                    dog_id="dog_a",
                    revision=self.manifest.revision,
                    previous_state=None,
                    baseline=baseline,
                    backup_metadata_sha256=None,
                )
                _write_pending(device, journal)
                if phase == "backup_pending":
                    partial = device / BACKUP_ROOT / self.manifest.revision
                    partial.mkdir(parents=True)
                    (partial / "partial").write_text("partial", encoding="utf-8")
                else:
                    backup = _backup(self.manifest, device, None, baseline)
                    journal = replace(
                        journal,
                        phase=phase,
                        backup_metadata_sha256=backup.metadata_sha256,
                    )
                    _write_pending(device, journal)
                    if phase == "installing":
                        first = self.snapshot.manifest.files[0]
                        _atomic_write(
                            device / first.target,
                            self.snapshot.payload(first.filename),
                            0o644,
                        )
                    elif phase == "state_written":
                        _install_payloads(self.snapshot, device)
                        _write_json(
                            device / STATE_PATH,
                            {
                                "schema_version": 1,
                                "dog_id": "dog_a",
                                "current_revision": self.manifest.revision,
                                "applied_at": "2026-09-10T02:02:00Z",
                                "restart_required": True,
                            },
                        )
                self.assertEqual(_recover_pending(device), self.manifest.revision)
                self.assertEqual(before, self.bytes_of(targets))
                self.assertFalse((device / STATE_PATH).exists())
                self.assertFalse((device / PENDING_PATH).exists())
                self.assertFalse((device / BACKUP_ROOT / self.manifest.revision).exists())
                self.assertIsNone(_recover_pending(device))

    def test_recovery_handles_every_rollback_journal_phase(self) -> None:
        for phase in ROLLBACK_PHASES:
            with self.subTest(phase=phase):
                device, targets = self.make_device(f"rollback-{phase}")
                before = self.bytes_of(targets)
                self.apply(device)
                backup = _load_backup_snapshot(
                    device / BACKUP_ROOT / self.manifest.revision,
                    expected_revision=self.manifest.revision,
                    expected_dog_id="dog_a",
                )
                journal = _make_pending_journal(
                    operation="rollback",
                    phase=phase,
                    dog_id="dog_a",
                    revision=self.manifest.revision,
                    previous_state=backup.previous_state,
                    baseline=backup.baseline,
                    backup_metadata_sha256=backup.metadata_sha256,
                )
                _write_pending(device, journal)
                if phase == "rollback_restoring":
                    first = backup.baseline[0]
                    _atomic_write(device / first.target, backup.payload(first.filename), first.mode)
                elif phase == "rollback_state_written":
                    _restore_backup(backup, device)
                    _restore_state(device, backup.previous_state)
                self.assertEqual(_recover_pending(device), self.manifest.revision)
                self.assertEqual(before, self.bytes_of(targets))
                self.assertFalse((device / STATE_PATH).exists())
                self.assertFalse((device / PENDING_PATH).exists())
                self.assertTrue((device / BACKUP_ROOT / self.manifest.revision).is_dir())
                self.assertIsNone(_recover_pending(device))
                self.assertEqual(self.rollback(device).status, "already_rolled_back")

    def test_rollback_journal_precedes_restore_and_failure_finishes_recovery(self) -> None:
        before = self.bytes_of(self.targets)
        self.apply()
        original = _restore_backup
        calls = 0

        def fail_once(snapshot, device_root):
            nonlocal calls
            calls += 1
            if calls == 1:
                pending = json.loads((self.device / PENDING_PATH).read_text(encoding="utf-8"))
                self.assertEqual(pending["operation"], "rollback")
                self.assertEqual(pending["phase"], "rollback_restoring")
                raise OSError("synthetic rollback failure")
            return original(snapshot, device_root)

        with patch(
            "native_agent.identity.importer._restore_backup", side_effect=fail_once
        ):
            with self.assertRaisesRegex(OSError, "synthetic rollback"):
                self.rollback()
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(before, self.bytes_of(self.targets))
        self.assertFalse((self.device / PENDING_PATH).exists())
        self.assertEqual(self.rollback().status, "already_rolled_back")

    def test_corrupt_backup_metadata_is_rejected_before_rollback_mutation(self) -> None:
        self.apply()
        installed = self.bytes_of(self.targets)
        metadata_path = self.device / BACKUP_ROOT / self.manifest.revision / "metadata.json"
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
        value["files"][0]["target"] = "tmp/escape"
        metadata_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "invalid_backup"):
            self.rollback()
        self.assertEqual(installed, self.bytes_of(self.targets))
        self.assertTrue((self.device / STATE_PATH).is_file())

    def test_manifest_targets_and_symlinked_device_parent_cannot_escape(self) -> None:
        path = self.bundle / "manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["files"][0]["target"] = "tmp/escape"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "invalid_file_record"):
            apply_bundle(self.bundle, self.device)

        # Rebuild before testing the device-side path guard.
        self.bundle = self.root / "bundle-clean"
        self.manifest = build_bundle(
            self.source,
            self.bundle,
            "dog_a",
            created_at=datetime(2026, 9, 10, 2, 3, tzinfo=timezone.utc),
        )
        outside = self.root / "outside"
        outside.mkdir()
        app = self.device / "app"
        for child in sorted(app.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        app.rmdir()
        app.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(IdentityError, "symlink_rejected"):
            apply_bundle(
                self.bundle,
                self.device,
                execute=True,
                expected_dog_id="dog_a",
            )
        self.assertEqual(list(outside.iterdir()), [])

    def test_rollback_rejects_invalid_revision_path(self) -> None:
        with self.assertRaisesRegex(IdentityError, "invalid_revision"):
            rollback_bundle(self.device, "../escape")


if __name__ == "__main__":
    unittest.main()
