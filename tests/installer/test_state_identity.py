from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import workflow_manager as manager


class StateIdentityTests(unittest.TestCase):
    def install(self, target: Path, dry_run: bool = False) -> str:
        output = StringIO()
        with patch.object(manager, "_validate_codex_version", return_value="0.154.0"):
            with redirect_stdout(output):
                manager.install(target, REPOSITORY_ROOT, dry_run)
        return output.getvalue()

    def uninstall(self, target: Path, dry_run: bool = False) -> str:
        output = StringIO()
        with redirect_stdout(output):
            manager.uninstall(target, dry_run)
        return output.getvalue()

    def state_path(self, target: Path) -> Path:
        return target / manager.STATE_FILENAME

    def read_state(self, target: Path) -> dict[str, object]:
        return json.loads(self.state_path(target).read_text(encoding="utf-8"))

    def make_legacy_state(self, target: Path) -> bytes:
        value = self.read_state(target)
        value["schema"] = manager.LEGACY_STATE_SCHEMA
        value.pop("rootIdentity")
        value.pop("directoryIdentities")
        content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.state_path(target).write_bytes(content)
        return content

    def make_v2_state(self, target: Path) -> bytes:
        value = self.read_state(target)
        value["schema"] = manager.PREVIOUS_STATE_SCHEMA
        value.pop("directoryIdentities")
        content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.state_path(target).write_bytes(content)
        return content

    def different_identity(self, identity: dict[str, str]) -> dict[str, str]:
        different = dict(identity)
        if identity["mode"] == "path-stat":
            different["fileId"] = str(int(identity["fileId"]) + 1)
        else:
            different["fileId"] = "f" * 32
        return different

    def test_root_identity_validators_accept_only_canonical_exact_schemas(self) -> None:
        path_stat = {
            "schema": manager.ROOT_IDENTITY_SCHEMA,
            "platform": sys.platform,
            "mode": "path-stat",
            "deviceId": "0",
            "fileId": "123",
        }
        windows = {
            "schema": manager.ROOT_IDENTITY_SCHEMA,
            "platform": "win32",
            "mode": "windows-ntfs-handle",
            "volumeSerialNumber": "0123456789abcdef",
            "fileId": "0123456789abcdef0123456789abcdef",
        }
        self.assertEqual(path_stat, manager._validate_root_identity(path_stat))
        self.assertEqual(windows, manager._validate_root_identity(windows))
        invalid = (
            {**path_stat, "deviceId": "01"},
            {**path_stat, "deviceId": "-1"},
            {**path_stat, "platform": "wrong"},
            {**path_stat, "extra": "value"},
            {**windows, "volumeSerialNumber": "ABCDEF0123456789"},
            {**windows, "volumeSerialNumber": "0" * 15},
            {**windows, "fileId": "0" * 31},
            {**windows, "platform": sys.platform if sys.platform != "win32" else "linux"},
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(manager.WorkflowError):
                    manager._validate_root_identity(value)
        with self.assertRaises(manager.WorkflowError):
            manager._assert_root_identity_matches(path_stat, windows, "during test")

    @unittest.skipUnless(sys.platform == "win32", "Windows identity transition only")
    def test_path_stat_to_native_transition_is_exact_and_one_way(self) -> None:
        device_id = 0x0123456789ABCDEF
        file_id = 0x123456789ABCDEF0
        path_stat = {
            "schema": manager.ROOT_IDENTITY_SCHEMA,
            "platform": "win32",
            "mode": "path-stat",
            "deviceId": str(device_id),
            "fileId": str(file_id),
        }
        native = {
            "schema": manager.ROOT_IDENTITY_SCHEMA,
            "platform": "win32",
            "mode": "windows-ntfs-handle",
            "volumeSerialNumber": f"{device_id:016x}",
            "fileId": file_id.to_bytes(16, "little").hex(),
        }

        manager._assert_persisted_root_identity_matches(
            path_stat, native, "during transition"
        )
        with self.assertRaises(manager.WorkflowError):
            manager._assert_persisted_root_identity_matches(
                native, path_stat, "during reverse transition"
            )
        with self.assertRaises(manager.WorkflowError):
            manager._assert_persisted_root_identity_matches(
                {**path_stat, "fileId": str(file_id + 1)},
                native,
                "during mismatched transition",
            )

    def test_fresh_install_writes_state_v3_and_journal_v5_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            captured: list[dict[str, object]] = []
            original = manager._write_relative_json

            def recording_write(
                adapter: manager.RepositoryAdapter,
                relative: str,
                value: dict[str, object],
                expected_root_identity: dict[str, str] | None = None,
            ) -> None:
                if relative == manager.JOURNAL_FILENAME:
                    captured.append(json.loads(json.dumps(value)))
                original(adapter, relative, value, expected_root_identity)

            with patch.object(manager, "_write_relative_json", side_effect=recording_write):
                self.install(target)

            state = self.read_state(target)
            self.assertEqual(manager.SCHEMA, state["schema"])
            self.assertEqual(
                manager.PathRepositoryAdapter(target).root_identity, state["rootIdentity"]
            )
            self.assertTrue(state["directoryIdentities"])
            self.assertTrue(captured)
            identities = {json.dumps(item["rootIdentity"], sort_keys=True) for item in captured}
            self.assertEqual(1, len(identities))
            self.assertEqual("preparingDirectories", captured[0]["phase"])
            self.assertEqual([], captured[0]["rollbackDirectories"])
            self.assertTrue(captured[0]["directories"])
            self.assertTrue(
                any(not item["prepared"] for item in captured[0]["directories"])
            )
            self.assertTrue(captured[-1]["rollbackDirectories"])
            self.assertTrue(
                all(item["prepared"] for item in captured[-1]["directories"])
            )
            self.assertTrue(all(item["schema"] == manager.JOURNAL_SCHEMA for item in captured))
            self.assertEqual(state["rootIdentity"], captured[0]["rootIdentity"])

    def test_noop_legacy_reinstall_migrates_and_failure_restores_exact_v1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target)
            legacy = self.make_legacy_state(target)

            output = self.install(target)

            self.assertIn("WARN: legacy installer state v1", output)
            self.assertEqual(manager.SCHEMA, self.read_state(target)["schema"])

            legacy = self.make_legacy_state(target)
            original = manager.PathRepositoryAdapter.atomic_write

            def fail_v2_state(
                adapter: manager.PathRepositoryAdapter, relative: str, content: bytes
            ) -> None:
                if relative == manager.STATE_FILENAME and manager.SCHEMA.encode() in content:
                    raise OSError("injected state migration failure")
                original(adapter, relative, content)

            with patch.object(manager.PathRepositoryAdapter, "atomic_write", new=fail_v2_state):
                with self.assertRaisesRegex(OSError, "injected state migration failure"):
                    self.install(target)

            self.assertEqual(legacy, self.state_path(target).read_bytes())
            self.assertFalse((target / manager.JOURNAL_FILENAME).exists())

    def test_legacy_uninstall_full_partial_and_dry_run_migration_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            full = root / "full"
            full.mkdir()
            self.install(full)
            self.make_legacy_state(full)
            self.uninstall(full)
            self.assertFalse(self.state_path(full).exists())

            partial = root / "partial"
            partial.mkdir()
            self.install(partial)
            modified = partial / "docs" / "ai" / "PROJECT_CONTEXT.md"
            modified.write_bytes(modified.read_bytes() + b"\nproject edit\n")
            self.make_legacy_state(partial)
            self.uninstall(partial)
            partial_state = self.read_state(partial)
            self.assertEqual(manager.SCHEMA, partial_state["schema"])
            self.assertEqual(
                manager.PathRepositoryAdapter(partial).root_identity,
                partial_state["rootIdentity"],
            )

            dry = root / "dry"
            dry.mkdir()
            self.install(dry)
            legacy = self.make_legacy_state(dry)
            self.uninstall(dry, True)
            self.assertEqual(legacy, self.state_path(dry).read_bytes())

    def test_valid_v2_reinstall_and_partial_uninstall_migrate_transactionally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reinstall = root / "reinstall"
            reinstall.mkdir()
            self.install(reinstall)
            self.make_v2_state(reinstall)
            output = self.install(reinstall)
            self.assertIn("legacy installer state v2", output)
            self.assertEqual(manager.SCHEMA, self.read_state(reinstall)["schema"])
            self.assertTrue(self.read_state(reinstall)["directoryIdentities"])

            partial = root / "partial"
            partial.mkdir()
            self.install(partial)
            modified = partial / "docs" / "ai" / "PROJECT_CONTEXT.md"
            modified.write_bytes(modified.read_bytes() + b"\nproject edit\n")
            self.make_v2_state(partial)
            self.uninstall(partial)
            state = self.read_state(partial)
            self.assertEqual(manager.SCHEMA, state["schema"])
            self.assertTrue(state["directoryIdentities"])

    def test_state_directory_identity_mismatch_blocks_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target)
            value = self.read_state(target)
            relative = next(iter(value["directoryIdentities"]))
            value["directoryIdentities"][relative] = self.different_identity(
                value["directoryIdentities"][relative]
            )
            before = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
            self.state_path(target).write_bytes(before)
            with self.assertRaisesRegex(
                manager.WorkflowError, "Managed directory identity does not match"
            ):
                self.uninstall(target)
            self.assertEqual(before, self.state_path(target).read_bytes())
            self.assertFalse((target / manager.JOURNAL_FILENAME).exists())

    def test_incomplete_directory_preparation_retains_evidence_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            adapter = manager.PathRepositoryAdapter(target)
            relative = next(iter(manager.PACKAGE_FILES.values()))
            actions = manager._prepare_relative_actions(
                adapter, "e" * 32, [("write", relative, b"content")]
            )
            records = manager._snapshot_directory_records(adapter, actions)
            manager._write_relative_json(
                adapter,
                manager.JOURNAL_FILENAME,
                manager._journal_value(
                    target,
                    "e" * 32,
                    "install",
                    actions,
                    phase="preparingDirectories",
                    root_identity=adapter.root_identity,
                    rollback_directories=[],
                    directories=records,
                ),
                adapter.root_identity,
            )
            journal = target / manager.JOURNAL_FILENAME
            before = journal.read_bytes()
            with self.assertRaisesRegex(
                manager.WorkflowError, "preparation is incomplete"
            ):
                manager.recover(target, journal, False, adapter=adapter)
            self.assertEqual(before, journal.read_bytes())
            self.assertFalse((target / manager.INSTALL_DIRECTORY).exists())

    def test_state_identity_mismatch_blocks_install_and_uninstall_without_mutation(self) -> None:
        for operation in ("install", "uninstall"):
            for identity_case in ("mismatch", "malformed"):
                with self.subTest(operation=operation, identity_case=identity_case):
                    self.assert_state_identity_refusal(operation, identity_case)

    def test_non_string_agents_backup_blocks_legacy_and_current_state_without_mutation(self) -> None:
        for schema in (manager.LEGACY_STATE_SCHEMA, manager.SCHEMA):
            for operation in ("install", "uninstall"):
                with self.subTest(schema=schema, operation=operation):
                    with tempfile.TemporaryDirectory() as directory:
                        target = Path(directory)
                        self.install(target)
                        value = self.read_state(target)
                        self.assertIsInstance(value["agents"], dict)
                        value["agents"]["backup"] = 1
                        if schema == manager.LEGACY_STATE_SCHEMA:
                            value["schema"] = schema
                            value.pop("rootIdentity")
                        malformed = (
                            json.dumps(value, indent=2, sort_keys=True) + "\n"
                        ).encode("utf-8")
                        self.state_path(target).write_bytes(malformed)
                        sentinel = target / "sentinel.txt"
                        sentinel.write_bytes(b"safe")

                        with self.assertRaises(manager.WorkflowError):
                            if operation == "install":
                                self.install(target)
                            else:
                                self.uninstall(target)

                        self.assertEqual(malformed, self.state_path(target).read_bytes())
                        self.assertEqual(b"safe", sentinel.read_bytes())
                        self.assertFalse((target / manager.JOURNAL_FILENAME).exists())

    def assert_state_identity_refusal(self, operation: str, identity_case: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target)
            value = self.read_state(target)
            if identity_case == "mismatch":
                value["rootIdentity"] = self.different_identity(value["rootIdentity"])
            else:
                value["rootIdentity"] = {"mode": "path-stat"}
            forged = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
            self.state_path(target).write_bytes(forged)
            sentinel = target / "sentinel.txt"
            sentinel.write_bytes(b"safe")

            with self.assertRaises(manager.WorkflowError):
                if operation == "install":
                    self.install(target)
                else:
                    self.uninstall(target)

            self.assertEqual(forged, self.state_path(target).read_bytes())
            self.assertEqual(b"safe", sentinel.read_bytes())
            self.assertFalse((target / manager.JOURNAL_FILENAME).exists())

    def test_legacy_and_invalid_journals_fail_closed_before_mutation(self) -> None:
        for schema in (
            manager.OLDER_JOURNAL_SCHEMA,
            manager.LEGACY_JOURNAL_SCHEMA,
            "unknown/journal",
        ):
            with self.subTest(schema=schema):
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory)
                    sentinel = target / "sentinel.txt"
                    sentinel.write_bytes(b"safe")
                    journal = target / manager.JOURNAL_FILENAME
                    journal.write_text(json.dumps({"schema": schema}), encoding="utf-8")
                    before = journal.read_bytes()
                    with self.assertRaisesRegex(manager.WorkflowError, "manual recovery"):
                        manager.recover(target, journal, False)
                    self.assertEqual(before, journal.read_bytes())
                    self.assertEqual(b"safe", sentinel.read_bytes())

        malformed_lists: tuple[object, ...] = (
            "not-a-list",
            [f"{manager.INSTALL_DIRECTORY}/rules", manager.INSTALL_DIRECTORY],
            [manager.INSTALL_DIRECTORY, manager.INSTALL_DIRECTORY],
            ["../escape"],
            ["unrelated"],
        )
        for rollback_directories in malformed_lists:
            with self.subTest(rollback_directories=rollback_directories):
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory)
                    adapter = manager.PathRepositoryAdapter(target)
                    relative = manager.PACKAGE_FILES["rules/ROUTING.md"]
                    actions = manager._prepare_relative_actions(
                        adapter,
                        "a" * 32,
                        [("write", relative, b"after")],
                    )
                    value = manager._journal_value(
                        target,
                        "a" * 32,
                        "install",
                        actions,
                        root_identity=adapter.root_identity,
                    )
                    value["rollbackDirectories"] = rollback_directories
                    journal = target / manager.JOURNAL_FILENAME
                    journal.write_text(json.dumps(value), encoding="utf-8")
                    before = journal.read_bytes()
                    with self.assertRaises(manager.WorkflowError):
                        manager.recover(target, journal, False)
                    self.assertEqual(before, journal.read_bytes())
                    self.assertFalse((target / relative).exists())

    def test_journal_identity_is_checked_before_actions_in_every_phase(self) -> None:
        for phase in ("prepared", "applying", "rollingBack", "committed"):
            for identity_case in ("mismatch", "malformed"):
                with self.subTest(phase=phase, identity_case=identity_case):
                    with tempfile.TemporaryDirectory() as directory:
                        target = Path(directory)
                        adapter = manager.PathRepositoryAdapter(target)
                        relative = next(iter(manager.PACKAGE_FILES.values()))
                        adapter.atomic_write(relative, b"before")
                        actions = manager._prepare_relative_actions(
                            adapter, "a" * 32, [("write", relative, b"after")]
                        )
                        value = manager._journal_value(
                            target,
                            "a" * 32,
                            "install",
                            actions,
                            phase,
                            "2026-09-14T12:00:00+00:00",
                            adapter.root_identity,
                        )
                        value["actions"] = "must-not-be-normalized"
                        if identity_case == "mismatch":
                            value["rootIdentity"] = self.different_identity(adapter.root_identity)
                        else:
                            value["rootIdentity"] = {"mode": "path-stat"}
                        journal = target / manager.JOURNAL_FILENAME
                        journal.write_text(json.dumps(value), encoding="utf-8")
                        before = journal.read_bytes()

                        with self.assertRaises(manager.WorkflowError):
                            manager.recover(target, journal, False)

                        self.assertEqual(b"before", adapter.read_bytes(relative))
                        self.assertEqual(before, journal.read_bytes())

    def test_committed_legacy_uninstall_recovery_accepts_only_verified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target)
            legacy = self.make_legacy_state(target)
            adapter = manager.PathRepositoryAdapter(target)
            actions = manager._prepare_relative_actions(
                adapter, "b" * 32, [("delete", manager.STATE_FILENAME, None)]
            )
            manager._apply_relative_actions(
                adapter, manager.JOURNAL_FILENAME, "b" * 32, "uninstall", actions
            )
            self.assertFalse(self.state_path(target).exists())
            self.assertEqual(legacy, adapter.read_bytes(actions[0].backup or ""))

            output = StringIO()
            with redirect_stdout(output):
                manager.recover(target, target / manager.JOURNAL_FILENAME, False)

            self.assertIn("RECOVERED", output.getvalue())
            self.assertFalse((target / manager.JOURNAL_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
