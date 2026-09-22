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


class RecoveryTests(unittest.TestCase):
    transaction_id = "a" * 32
    created_at = "2026-09-14T12:00:00+00:00"

    def managed_path(self, target: Path, index: int) -> Path:
        relatives = sorted(manager.PACKAGE_FILES.values())
        path = target / relatives[index]
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def install(self, target: Path, dry_run: bool = False) -> None:
        with patch.object(manager, "_validate_codex_version", return_value="0.155.1"):
            with redirect_stdout(StringIO()):
                manager.install(target, REPOSITORY_ROOT, dry_run)

    def prepare(
        self,
        target: Path,
        requests: list[tuple[str, Path, bytes | None]],
        applied_indexes: set[int] | None = None,
    ) -> tuple[Path, list[manager.Action]]:
        actions = manager._prepare_actions(target, self.transaction_id, requests)
        adapter = manager.PathRepositoryAdapter(target)
        rollback_directories = manager._plan_rollback_directories(adapter, actions)
        journal_path = target / manager.JOURNAL_FILENAME
        manager._write_json(
            journal_path,
            manager._journal_value(
                target,
                self.transaction_id,
                "install",
                actions,
                "prepared",
                self.created_at,
                rollback_directories=rollback_directories,
            ),
            target,
        )
        manager._prepare_backups(target, actions)
        for action in actions:
            if applied_indexes and action.index in applied_indexes:
                action_path = target / action.path
                if action.kind == "write":
                    manager._atomic_write(action_path, action.content or b"")
                else:
                    action_path.unlink(missing_ok=True)
                action.phase = "applied"
        manager._write_json(
            journal_path,
            manager._journal_value(
                target,
                self.transaction_id,
                "install",
                actions,
                "applying",
                self.created_at,
                rollback_directories=rollback_directories,
            ),
        )
        return journal_path, actions

    def test_journal_v4_records_verified_hashes_before_target_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            existing = self.managed_path(target, 0)
            created = self.managed_path(target, 1)
            existing.write_bytes(b"before")

            actions = manager._prepare_actions(
                target,
                self.transaction_id,
                [("write", existing, b"after"), ("write", created, b"created")],
            )
            value = manager._journal_value(
                target,
                self.transaction_id,
                "install",
                actions,
                created_at=self.created_at,
                rollback_directories=manager._plan_rollback_directories(
                    manager.PathRepositoryAdapter(target), actions
                ),
            )

            self.assertEqual(manager.JOURNAL_SCHEMA, value["schema"])
            self.assertEqual(b"before", existing.read_bytes())
            self.assertFalse(created.exists())
            self.assertEqual(manager._sha256_bytes(b"before"), value["actions"][0]["preHash"])
            self.assertEqual(manager._sha256_bytes(b"after"), value["actions"][0]["postHash"])
            self.assertEqual(value["actions"][0]["preHash"], value["actions"][0]["backupHash"])
            self.assertEqual("workflow-manager", value["actions"][0]["owner"])
            self.assertIsNone(value["actions"][1]["preHash"])
            self.assertEqual(
                manager._canonical_directory_order(
                    set(value["rollbackDirectories"])
                ),
                value["rollbackDirectories"],
            )

    def test_recovery_restores_applied_actions_and_is_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            existing = self.managed_path(target, 0)
            created = self.managed_path(target, 1)
            existing.write_bytes(b"before")
            journal_path, _ = self.prepare(
                target,
                [("write", existing, b"after"), ("write", created, b"created")],
                {0, 1},
            )

            with redirect_stdout(StringIO()):
                manager.recover(target, journal_path, False)
                manager.recover(target, journal_path, False)

            self.assertEqual(b"before", existing.read_bytes())
            self.assertFalse(created.exists())
            self.assertFalse(journal_path.exists())

    def test_recovery_accepts_pre_state_as_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            path = self.managed_path(target, 0)
            path.write_bytes(b"before")
            journal_path, _ = self.prepare(target, [("write", path, b"after")])

            with redirect_stdout(StringIO()):
                manager.recover(target, journal_path, False)

            self.assertEqual(b"before", path.read_bytes())
            self.assertFalse(journal_path.exists())

    def test_intervening_edit_prevents_all_recovery_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            first = self.managed_path(target, 0)
            second = self.managed_path(target, 1)
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            journal_path, _ = self.prepare(
                target,
                [("write", first, b"new-one"), ("write", second, b"new-two")],
                {0, 1},
            )
            first.write_bytes(b"user edit")

            with self.assertRaisesRegex(manager.WorkflowError, "no files were changed"):
                manager.recover(target, journal_path, False)

            self.assertEqual(b"user edit", first.read_bytes())
            self.assertEqual(b"new-two", second.read_bytes())
            self.assertTrue(journal_path.exists())

    def test_missing_or_tampered_backup_prevents_all_recovery_mutation(self) -> None:
        for replacement in (None, b"tampered"):
            with self.subTest(replacement=replacement):
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory)
                    first = self.managed_path(target, 0)
                    second = self.managed_path(target, 1)
                    first.write_bytes(b"one")
                    second.write_bytes(b"two")
                    journal_path, actions = self.prepare(
                        target,
                        [("write", first, b"new-one"), ("write", second, b"new-two")],
                        {0, 1},
                    )
                    backup_relative = actions[0].backup
                    self.assertIsNotNone(backup_relative)
                    backup = target / backup_relative
                    if replacement is None:
                        backup.unlink()
                    else:
                        backup.write_bytes(replacement)

                    with self.assertRaisesRegex(manager.WorkflowError, "backup is missing or tampered"):
                        manager.recover(target, journal_path, False)

                    self.assertEqual(b"new-one", first.read_bytes())
                    self.assertEqual(b"new-two", second.read_bytes())
                    self.assertTrue(journal_path.exists())

    def test_forged_same_hash_backup_path_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            path = self.managed_path(target, 0)
            path.write_bytes(b"before")
            journal_path, actions = self.prepare(target, [("write", path, b"after")], {0})
            forged = target / manager.BACKUP_DIRECTORY / self.transaction_id / "forged.txt"
            forged.parent.mkdir(parents=True, exist_ok=True)
            forged.write_bytes(b"before")
            value = json.loads(journal_path.read_text(encoding="utf-8"))
            value["actions"][0]["backup"] = manager._relative(target, forged)
            journal_path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaisesRegex(manager.WorkflowError, "backup path is invalid"):
                manager.recover(target, journal_path, False)

            self.assertEqual(b"after", path.read_bytes())
            self.assertTrue(journal_path.exists())
            self.assertIsNotNone(actions[0].backup)
            self.assertTrue((target / actions[0].backup).exists())

    def test_forged_non_managed_action_path_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            managed = self.managed_path(target, 0)
            managed.write_bytes(b"before")
            journal_path, actions = self.prepare(
                target,
                [("write", managed, b"after")],
                {0},
            )
            original_backup = actions[0].backup
            self.assertIsNotNone(original_backup)
            arbitrary = target / "project-owned.txt"
            arbitrary.write_bytes(b"after")
            forged_backup = (
                target
                / manager.BACKUP_DIRECTORY
                / self.transaction_id
                / "project-owned.txt"
            )
            forged_backup.parent.mkdir(parents=True, exist_ok=True)
            forged_backup.write_bytes(b"before")
            value = json.loads(journal_path.read_text(encoding="utf-8"))
            value["actions"][0]["path"] = "project-owned.txt"
            value["actions"][0]["backup"] = manager._relative(target, forged_backup)
            journal_path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaisesRegex(manager.WorkflowError, "not package-managed"):
                manager.recover(target, journal_path, False)

            self.assertEqual(b"after", managed.read_bytes())
            self.assertEqual(b"after", arbitrary.read_bytes())
            self.assertTrue(journal_path.exists())
            self.assertTrue((target / original_backup).exists())
            self.assertTrue(forged_backup.exists())

    def test_malformed_legacy_and_invalid_hash_journals_are_retained(self) -> None:
        cases = (
            {"schema": manager.SCHEMA},
            {"schema": manager.JOURNAL_SCHEMA},
        )
        for value in cases:
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory)
                    sentinel = target / "sentinel.txt"
                    sentinel.write_bytes(b"safe")
                    journal_path = target / manager.JOURNAL_FILENAME
                    journal_path.write_text(json.dumps(value), encoding="utf-8")

                    with self.assertRaises(manager.WorkflowError):
                        manager.recover(target, journal_path, False)

                    self.assertEqual(b"safe", sentinel.read_bytes())
                    self.assertTrue(journal_path.exists())

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            path = self.managed_path(target, 0)
            path.write_bytes(b"before")
            journal_path, _ = self.prepare(target, [("write", path, b"after")])
            value = json.loads(journal_path.read_text(encoding="utf-8"))
            value["actions"][0]["preHash"] = "unsafe"
            journal_path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaisesRegex(manager.WorkflowError, "preHash is invalid"):
                manager.recover(target, journal_path, False)

            self.assertEqual(b"before", path.read_bytes())
            self.assertTrue(journal_path.exists())

    def test_dry_run_preflights_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            path = self.managed_path(target, 0)
            journal_path, _ = self.prepare(target, [("write", path, b"created")], {0})
            journal_before = journal_path.read_bytes()

            output = StringIO()
            with redirect_stdout(output):
                manager.recover(target, journal_path, True)

            self.assertIn("1 applied action", output.getvalue())
            self.assertEqual(b"created", path.read_bytes())
            self.assertEqual(journal_before, journal_path.read_bytes())

    def test_automatic_failure_rolls_back_and_removes_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            first = self.managed_path(target, 0)
            second = self.managed_path(target, 1)
            actions = manager._prepare_actions(
                target,
                self.transaction_id,
                [("write", first, b"one"), ("write", second, b"two")],
            )
            journal_path = target / manager.JOURNAL_FILENAME
            original_atomic_write = manager._atomic_write
            failed = False

            def fail_second(path: Path, content: bytes) -> None:
                nonlocal failed
                if path == second and not failed:
                    failed = True
                    raise OSError("injected failure")
                original_atomic_write(path, content)

            with patch.object(manager, "_atomic_write", side_effect=fail_second):
                with self.assertRaises(OSError):
                    manager._apply_actions(target, journal_path, self.transaction_id, "install", actions)

            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertFalse(journal_path.exists())

    def test_partial_rollback_retains_journal_and_backups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            path = self.managed_path(target, 0)
            path.write_bytes(b"before")
            journal_path, actions = self.prepare(target, [("write", path, b"after")], {0})
            backup_relative = actions[0].backup
            self.assertIsNotNone(backup_relative)
            backup = target / backup_relative
            original_atomic_write = manager._atomic_write

            def fail_restore(destination: Path, content: bytes) -> None:
                if destination == path:
                    raise OSError("injected restore failure")
                original_atomic_write(destination, content)

            with patch.object(manager, "_atomic_write", side_effect=fail_restore):
                with self.assertRaisesRegex(OSError, "injected restore failure"):
                    manager.recover(target, journal_path, False)

            self.assertEqual(b"after", path.read_bytes())
            self.assertTrue(journal_path.exists())
            self.assertTrue(backup.exists())

    def test_state_manifest_action_must_be_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with self.assertRaisesRegex(manager.WorkflowError, "must be last"):
                manager._prepare_actions(
                    target,
                    self.transaction_id,
                    [
                        ("write", target / manager.STATE_FILENAME, b"state"),
                        ("write", self.managed_path(target, 0), b"later"),
                    ],
                )

    def test_interrupted_uninstall_recovery_restores_files_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target)
            managed = target / next(iter(manager.PACKAGE_FILES.values()))
            state_path = target / manager.STATE_FILENAME
            managed_before = managed.read_bytes()
            state_before = state_path.read_bytes()
            actions = manager._prepare_actions(
                target,
                self.transaction_id,
                [("delete", managed, None), ("delete", state_path, None)],
            )
            journal_path = target / manager.JOURNAL_FILENAME
            manager._write_json(
                journal_path,
                manager._journal_value(
                    target,
                    self.transaction_id,
                    "uninstall",
                    actions,
                    "prepared",
                    self.created_at,
                ),
                target,
            )
            manager._prepare_backups(target, actions)
            for action in actions:
                (target / action.path).unlink()
                action.phase = "applied"
            manager._write_json(
                journal_path,
                manager._journal_value(
                    target,
                    self.transaction_id,
                    "uninstall",
                    actions,
                    "applying",
                    self.created_at,
                ),
                target,
            )

            with redirect_stdout(StringIO()):
                manager.recover(target, journal_path, False)

            self.assertEqual(managed_before, managed.read_bytes())
            self.assertEqual(state_before, state_path.read_bytes())
            self.assertFalse(journal_path.exists())

    def test_install_failures_at_file_and_state_replacement_roll_back(self) -> None:
        for fail_state in (False, True):
            with self.subTest(fail_state=fail_state):
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory)
                    ordinary = target / next(iter(manager.PACKAGE_FILES.values()))
                    state_path = target / manager.STATE_FILENAME
                    failed_path = state_path if fail_state else ordinary
                    original_replace = manager.os.replace
                    injected = False

                    def fail_replacement(source: Path, destination: Path) -> None:
                        nonlocal injected
                        if Path(destination) == failed_path and not injected:
                            injected = True
                            raise OSError("injected replacement failure")
                        original_replace(source, destination)

                    with patch.object(manager.os, "replace", side_effect=fail_replacement):
                        with self.assertRaisesRegex(OSError, "injected replacement failure"):
                            self.install(target)

                    self.assertTrue(injected)
                    self.assertFalse(ordinary.exists())
                    self.assertFalse(state_path.exists())
                    self.assertFalse((target / manager.JOURNAL_FILENAME).exists())

    def test_malformed_state_blocks_uninstall_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            sentinel = target / "project-owned.txt"
            sentinel.write_bytes(b"safe")
            state_path = target / manager.STATE_FILENAME
            state_path.write_text(
                json.dumps(
                    {
                        "schema": manager.SCHEMA,
                        "target": str(target),
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )
            before = state_path.read_bytes()

            with self.assertRaisesRegex(manager.WorkflowError, "malformed state manifest"):
                manager.uninstall(target, False)

            self.assertEqual(b"safe", sentinel.read_bytes())
            self.assertEqual(before, state_path.read_bytes())

    def test_uninstall_dry_run_and_repeated_lifecycle_preserve_directory_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target)
            first_state = (target / manager.STATE_FILENAME).read_bytes()
            self.install(target)
            self.assertEqual(first_state, (target / manager.STATE_FILENAME).read_bytes())
            before = {
                path.relative_to(target).as_posix(): (
                    "directory" if path.is_dir() else path.read_bytes()
                )
                for path in target.rglob("*")
            }

            with redirect_stdout(StringIO()):
                manager.uninstall(target, True)

            after_dry_run = {
                path.relative_to(target).as_posix(): (
                    "directory" if path.is_dir() else path.read_bytes()
                )
                for path in target.rglob("*")
            }
            self.assertEqual(before, after_dry_run)

            with redirect_stdout(StringIO()):
                manager.uninstall(target, False)
                manager.uninstall(target, False)

            self.assertEqual([], list(target.iterdir()))

    def test_backup_preparation_failure_retains_recoverable_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            managed = self.managed_path(target, 0)
            managed.write_bytes(b"before")
            actions = manager._prepare_actions(
                target,
                self.transaction_id,
                [("write", managed, b"after")],
            )
            backup_relative = actions[0].backup
            self.assertIsNotNone(backup_relative)
            backup = target / backup_relative
            journal_path = target / manager.JOURNAL_FILENAME
            original_atomic_write = manager._atomic_write

            def fail_backup(path: Path, content: bytes) -> None:
                if path == backup:
                    raise OSError("injected backup failure")
                original_atomic_write(path, content)

            with patch.object(manager, "_atomic_write", side_effect=fail_backup):
                with self.assertRaisesRegex(OSError, "injected backup failure"):
                    manager._apply_actions(
                        target,
                        journal_path,
                        self.transaction_id,
                        "install",
                        actions,
                    )

            self.assertEqual(b"before", managed.read_bytes())
            self.assertTrue(journal_path.exists())
            with redirect_stdout(StringIO()):
                manager.recover(target, journal_path, False)
            self.assertEqual(b"before", managed.read_bytes())
            self.assertFalse(journal_path.exists())

    def test_committed_uninstall_cleanup_failure_finishes_without_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")
            self.install(target)
            managed = target / next(iter(manager.PACKAGE_FILES.values()))
            journal_path = target / manager.JOURNAL_FILENAME
            backup_root = target / manager.BACKUP_DIRECTORY
            original_unlink = manager._safe_unlink
            cleanup_deletions = 0

            def fail_after_one_cleanup(
                cleanup_target: Path, path: Path, missing_ok: bool = False
            ) -> None:
                nonlocal cleanup_deletions
                if path.is_relative_to(backup_root) and journal_path.exists():
                    journal = json.loads(journal_path.read_text(encoding="utf-8"))
                    if journal["phase"] == "committed":
                        if cleanup_deletions == 1:
                            raise OSError("injected committed cleanup failure")
                        cleanup_deletions += 1
                original_unlink(cleanup_target, path, missing_ok)

            with patch.object(manager, "_safe_unlink", side_effect=fail_after_one_cleanup):
                with self.assertRaisesRegex(OSError, "injected committed cleanup failure"):
                    manager.uninstall(target, False)

            self.assertEqual(1, cleanup_deletions)
            self.assertFalse(managed.exists())
            self.assertFalse((target / manager.STATE_FILENAME).exists())
            self.assertTrue(journal_path.exists())
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            state_action = next(
                action
                for action in journal["actions"]
                if action["path"] == manager.STATE_FILENAME
            )
            state_backup = target / state_action["backup"]
            self.assertTrue(state_backup.exists())

            failed_journal_unlink = False

            def fail_journal_boundary(
                cleanup_target: Path, path: Path, missing_ok: bool = False
            ) -> None:
                nonlocal failed_journal_unlink
                if path == journal_path and not failed_journal_unlink:
                    failed_journal_unlink = True
                    raise OSError("injected committed cleanup failure")
                original_unlink(cleanup_target, path, missing_ok)

            with patch.object(manager, "_safe_unlink", side_effect=fail_journal_boundary):
                with self.assertRaisesRegex(OSError, "injected committed cleanup failure"):
                    manager.recover(target, journal_path, False)

            self.assertFalse(managed.exists())
            self.assertFalse((target / manager.STATE_FILENAME).exists())
            self.assertTrue(journal_path.exists())
            self.assertTrue(state_backup.exists())

            with redirect_stdout(StringIO()):
                manager.recover(target, journal_path, False)
                manager.recover(target, journal_path, False)

            self.assertEqual(["AGENTS.md"], sorted(path.name for path in target.iterdir()))
            self.assertEqual("# Existing\n", (target / "AGENTS.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
