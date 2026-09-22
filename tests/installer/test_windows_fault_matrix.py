from __future__ import annotations

import hashlib
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


def native_failure() -> OSError:
    error = OSError(13, "sensitive localized filesystem detail")
    error.winerror = 5  # type: ignore[attr-defined]
    error.add_note("injected native failure; NTSTATUS=0xC0000022")
    return error


@unittest.skipUnless(sys.platform == "win32", "Windows native fault matrix only")
class WindowsFaultMatrixTests(unittest.TestCase):
    def native(self, target: Path) -> manager.WindowsRepositoryAdapter:
        adapter = manager._repository_adapter_from_raw_target(str(target))
        self.assertIs(type(adapter), manager.WindowsRepositoryAdapter)
        return adapter

    def install(
        self, adapter: manager.RepositoryAdapter
    ) -> tuple[str | None, str]:
        output = StringIO()
        with patch.object(manager, "_validate_codex_version", return_value="0.155.1"):
            with redirect_stdout(output):
                result = manager.install(
                    adapter.root,
                    REPOSITORY_ROOT,
                    False,
                    adapter=adapter,
                )
        return result, output.getvalue()

    def uninstall(
        self, adapter: manager.RepositoryAdapter
    ) -> tuple[str | None, str]:
        output = StringIO()
        with redirect_stdout(output):
            result = manager.uninstall(adapter.root, False, adapter=adapter)
        return result, output.getvalue()

    def recover(self, adapter: manager.RepositoryAdapter) -> str:
        output = StringIO()
        with redirect_stdout(output):
            manager.recover(
                adapter.root,
                adapter.root / manager.JOURNAL_FILENAME,
                False,
                adapter=adapter,
            )
        return output.getvalue()

    def write_legacy_state(self, adapter: manager.RepositoryAdapter) -> None:
        state = json.loads(adapter.read_bytes(manager.STATE_FILENAME).decode("utf-8"))
        state["schema"] = manager.LEGACY_STATE_SCHEMA
        state.pop("rootIdentity")
        state.pop("directoryIdentities")
        adapter.atomic_write(
            manager.STATE_FILENAME,
            (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    def tree(self, target: Path) -> dict[str, tuple[str, bytes | None]]:
        return {
            path.relative_to(target).as_posix(): (
                ("file", path.read_bytes()) if path.is_file() else ("directory", None)
            )
            for path in target.rglob("*")
        }

    def assert_sanitized_failure(self, error: BaseException, output: str) -> None:
        message = str(error)
        self.assertIn("winerror=5", message)
        self.assertIn("NTSTATUS=0xC0000022", message)
        self.assertNotIn("sensitive localized", message)
        self.assertNotIn("PASS:", output)

    def test_native_hash_and_mkdir_failures_are_sanitized_and_nonmutating(self) -> None:
        for stage in ("hash", "mkdir", "mkdir-partial"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                sentinel = target / "sentinel.txt"
                sentinel.write_bytes(b"outside")
                if stage == "hash":
                    managed = target / next(iter(manager.PROJECT_TEMPLATE_FILES.values()))
                    managed.parent.mkdir(parents=True)
                    managed.write_bytes(b"preexisting")
                before = self.tree(target)
                adapter = self.native(target)
                method = "hash_file" if stage == "hash" else "ensure_directories"
                output = StringIO()
                original_method = getattr(adapter._filesystem, method)

                def failing_method(*args: object, **kwargs: object) -> object:
                    if stage == "mkdir-partial":
                        original_method(*args, **kwargs)
                    raise native_failure()

                try:
                    with patch.object(
                        adapter._filesystem, method, side_effect=failing_method
                    ):
                        with self.assertRaises(manager.WorkflowError) as raised:
                            with redirect_stdout(output):
                                self.install(adapter)
                finally:
                    adapter.close()
                self.assert_sanitized_failure(raised.exception, output.getvalue())
                self.assertEqual(b"outside", sentinel.read_bytes())
                journal = target / manager.JOURNAL_FILENAME
                if stage == "hash":
                    self.assertEqual(before, self.tree(target))
                    self.assertFalse(journal.exists())
                else:
                    self.assertTrue(journal.is_file())
                    value = json.loads(journal.read_text(encoding="utf-8"))
                    self.assertEqual("preparingDirectories", value["phase"])
                    self.assertTrue(
                        any(not item["prepared"] for item in value["directories"])
                    )

    def test_journal_phase_and_backup_write_faults_have_deterministic_restart(self) -> None:
        for stage in ("initial-journal", "phase-journal", "backup"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                (target / "sentinel.txt").write_bytes(b"outside")
                if stage == "backup":
                    (target / "AGENTS.md").write_bytes(b"preexisting agents\n")
                before = self.tree(target)
                adapter = self.native(target)
                original = adapter._filesystem.atomic_write
                journal_writes = 0

                def failing_write(relative: str, content: bytes) -> None:
                    nonlocal journal_writes
                    if relative == manager.JOURNAL_FILENAME:
                        journal_writes += 1
                    should_fail = (
                        stage == "initial-journal"
                        and relative == manager.JOURNAL_FILENAME
                        and journal_writes == 1
                    ) or (
                        stage == "phase-journal"
                        and relative == manager.JOURNAL_FILENAME
                        and journal_writes == 2
                    ) or (
                        stage == "backup"
                        and relative.startswith(manager.BACKUP_DIRECTORY + "/")
                    )
                    if should_fail:
                        raise native_failure()
                    original(relative, content)

                output = StringIO()
                try:
                    with patch.object(
                        adapter._filesystem,
                        "atomic_write",
                        side_effect=failing_write,
                    ):
                        with self.assertRaises(manager.WorkflowError) as raised:
                            with redirect_stdout(output):
                                self.install(adapter)
                finally:
                    adapter.close()
                self.assert_sanitized_failure(raised.exception, output.getvalue())
                journal = target / manager.JOURNAL_FILENAME
                self.assertEqual(stage != "initial-journal", journal.exists())
                if journal.exists():
                    value = json.loads(journal.read_text(encoding="utf-8"))
                    if stage == "phase-journal":
                        self.assertEqual("preparingDirectories", value["phase"])
                        self.assertTrue(
                            any(not item["prepared"] for item in value["directories"])
                        )
                        restart = self.native(target)
                        try:
                            with self.assertRaisesRegex(
                                manager.WorkflowError, "preparation is incomplete"
                            ):
                                self.recover(restart)
                        finally:
                            restart.close()
                        self.assertTrue(journal.exists())
                    else:
                        self.assertEqual("prepared", value["phase"])
                        restart = self.native(target)
                        try:
                            self.assertEqual(
                                "RECOVERED: interrupted transaction rolled back\n",
                                self.recover(restart),
                            )
                        finally:
                            restart.close()
                if stage != "phase-journal":
                    self.assertEqual(before, self.tree(target))
                else:
                    self.assertEqual(b"outside", (target / "sentinel.txt").read_bytes())

    def test_apply_replace_fault_rolls_back_exact_file_prestate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "sentinel.txt").write_bytes(b"outside")
            before = self.tree(target)
            adapter = self.native(target)
            original = adapter._filesystem.atomic_write
            failed = False
            fail_relative = list(manager.PACKAGE_FILES.values())[1]

            def failing_write(relative: str, content: bytes) -> None:
                nonlocal failed
                if relative == fail_relative and not failed:
                    failed = True
                    raise native_failure()
                original(relative, content)

            output = StringIO()
            try:
                with patch.object(
                    adapter._filesystem, "atomic_write", side_effect=failing_write
                ):
                    with self.assertRaises(manager.WorkflowError) as raised:
                        with redirect_stdout(output):
                            self.install(adapter)
            finally:
                adapter.close()
            self.assertTrue(failed)
            self.assert_sanitized_failure(raised.exception, output.getvalue())
            self.assertEqual(before, self.tree(target))
            self.assertFalse((target / manager.JOURNAL_FILENAME).exists())
            self.assertFalse((target / manager.BACKUP_DIRECTORY).exists())

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            preexisting = target / manager.INSTALL_DIRECTORY / "rules"
            preexisting.mkdir(parents=True)
            before = self.tree(target)
            adapter = self.native(target)
            first_relative = manager.PACKAGE_FILES["rules/ROUTING.md"]
            fail_relative = manager.PACKAGE_FILES["templates/PROJECT_CONTEXT.md"]
            actions = manager._prepare_relative_actions(
                adapter,
                "e" * 32,
                [
                    ("write", first_relative, b"first"),
                    ("write", fail_relative, b"second"),
                ],
            )
            original = adapter._filesystem.atomic_write

            def failing_write(relative: str, content: bytes) -> None:
                if relative == fail_relative:
                    raise native_failure()
                original(relative, content)

            try:
                with patch.object(
                    adapter._filesystem, "atomic_write", side_effect=failing_write
                ):
                    with self.assertRaises(manager.WorkflowError):
                        manager._apply_relative_actions(
                            adapter,
                            manager.JOURNAL_FILENAME,
                            "e" * 32,
                            "install",
                            actions,
                        )
            finally:
                adapter.close()
            self.assertEqual(before, self.tree(target))

    def test_rollback_fault_retains_verified_backup_and_restart_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "sentinel.txt").write_bytes(b"outside")
            (target / "AGENTS.md").write_bytes(b"preexisting agents\n")
            before = self.tree(target)
            adapter = self.native(target)
            original_write = adapter._filesystem.atomic_write
            original_unlink = adapter._filesystem.unlink
            first_relative, fail_relative = list(manager.PACKAGE_FILES.values())[:2]
            apply_failed = False
            rollback_failed = False

            def failing_write(relative: str, content: bytes) -> None:
                nonlocal apply_failed
                if relative == fail_relative and not apply_failed:
                    apply_failed = True
                    raise native_failure()
                original_write(relative, content)

            def failing_unlink(relative: str, *, missing_ok: bool = False) -> None:
                nonlocal rollback_failed
                if relative == first_relative and not rollback_failed:
                    rollback_failed = True
                    raise native_failure()
                original_unlink(relative, missing_ok=missing_ok)

            output = StringIO()
            try:
                with patch.object(
                    adapter._filesystem, "atomic_write", side_effect=failing_write
                ):
                    with patch.object(
                        adapter._filesystem, "unlink", side_effect=failing_unlink
                    ):
                        with self.assertRaises(manager.WorkflowError) as raised:
                            with redirect_stdout(output):
                                self.install(adapter)
            finally:
                adapter.close()
            self.assertTrue(apply_failed)
            self.assertTrue(rollback_failed)
            self.assertNotIn("PASS:", output.getvalue())
            self.assertIn("automatic rollback is incomplete", str(raised.exception))
            journal_path = target / manager.JOURNAL_FILENAME
            self.assertTrue(journal_path.is_file())
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            agent_action = next(
                action for action in journal["actions"] if action["path"] == "AGENTS.md"
            )
            backup = target / Path(agent_action["backup"])
            self.assertTrue(backup.is_file())
            self.assertEqual(agent_action["backupHash"], hashlib.sha256(backup.read_bytes()).hexdigest())

            restart = self.native(target)
            try:
                self.assertEqual(
                    "RECOVERED: interrupted transaction rolled back\n",
                    self.recover(restart),
                )
            finally:
                restart.close()
            self.assertEqual(before, self.tree(target))
            self.assertFalse(journal_path.exists())
            backup_root = target / manager.BACKUP_DIRECTORY
            self.assertFalse(
                backup_root.exists()
                and any(path.is_file() for path in backup_root.rglob("*"))
            )

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "sentinel.txt").write_bytes(b"outside")
            adapter = self.native(target)
            original_write = adapter._filesystem.atomic_write
            original_rmdir = adapter._filesystem.rmdir
            fail_relative = list(manager.PACKAGE_FILES.values())[1]
            apply_failed = False
            cleanup_failed = False

            def failing_write(relative: str, content: bytes) -> None:
                nonlocal apply_failed
                if relative == fail_relative and not apply_failed:
                    apply_failed = True
                    raise native_failure()
                original_write(relative, content)

            def failing_rmdir(
                relative: str,
                *,
                missing_ok: bool = False,
                expected_identity: object = None,
            ) -> None:
                nonlocal cleanup_failed
                if not cleanup_failed:
                    cleanup_failed = True
                    raise native_failure()
                original_rmdir(
                    relative,
                    missing_ok=missing_ok,
                    expected_identity=expected_identity,
                )

            try:
                with patch.object(
                    adapter._filesystem, "atomic_write", side_effect=failing_write
                ):
                    with patch.object(
                        adapter._filesystem, "rmdir", side_effect=failing_rmdir
                    ):
                        with self.assertRaises(manager.WorkflowError):
                            self.install(adapter)
            finally:
                adapter.close()
            self.assertTrue(apply_failed)
            self.assertTrue(cleanup_failed)
            journal_path = target / manager.JOURNAL_FILENAME
            self.assertTrue(journal_path.is_file())

            retained_directory = target / manager.INSTALL_DIRECTORY
            retained_directory.mkdir(exist_ok=True)
            foreign = retained_directory / "foreign.txt"
            foreign.write_bytes(b"preserve")
            expected = {
                "sentinel.txt": ("file", b"outside"),
                manager.INSTALL_DIRECTORY: ("directory", None),
                f"{manager.INSTALL_DIRECTORY}/foreign.txt": (
                    "file",
                    b"preserve",
                ),
            }

            restart = self.native(target)
            try:
                self.assertEqual(
                    "RECOVERED: interrupted transaction rolled back\n",
                    self.recover(restart),
                )
            finally:
                restart.close()
            self.assertEqual(expected, self.tree(target))
            self.assertEqual(b"preserve", foreign.read_bytes())

    def test_committed_cleanup_faults_resume_without_rollback(self) -> None:
        for stage in ("install-unlink", "uninstall-rmdir"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                adapter = self.native(target)
                if stage == "uninstall-rmdir":
                    self.install(adapter)
                method = "unlink" if stage == "install-unlink" else "rmdir"
                original = getattr(adapter._filesystem, method)
                failed = False

                def failing_cleanup(relative: str, **kwargs: object) -> None:
                    nonlocal failed
                    should_fail = (
                        stage == "install-unlink"
                        and relative == manager.JOURNAL_FILENAME
                    ) or stage == "uninstall-rmdir"
                    if should_fail and not failed:
                        failed = True
                        raise native_failure()
                    original(relative, **kwargs)

                output = StringIO()
                try:
                    with patch.object(
                        adapter._filesystem, method, side_effect=failing_cleanup
                    ):
                        with self.assertRaises(manager.WorkflowError) as raised:
                            with redirect_stdout(output):
                                if stage == "install-unlink":
                                    self.install(adapter)
                                else:
                                    self.uninstall(adapter)
                finally:
                    adapter.close()
                self.assertTrue(failed)
                self.assert_sanitized_failure(raised.exception, output.getvalue())
                journal_path = target / manager.JOURNAL_FILENAME
                self.assertTrue(journal_path.is_file())
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                self.assertEqual("committed", journal["phase"])
                state_existed = (target / manager.STATE_FILENAME).exists()

                restart = self.native(target)
                try:
                    self.assertEqual(
                        "RECOVERED: committed transaction cleanup finalized\n",
                        self.recover(restart),
                    )
                finally:
                    restart.close()
                self.assertFalse(journal_path.exists())
                self.assertEqual(state_existed, (target / manager.STATE_FILENAME).exists())
                if stage == "install-unlink":
                    self.assertTrue((target / manager.INSTALL_DIRECTORY).is_dir())
                else:
                    self.assertFalse((target / manager.INSTALL_DIRECTORY).exists())

    def test_native_v1_reinstall_full_and_partial_uninstall_migrate_transactionally(self) -> None:
        for operation in ("reinstall", "full-uninstall", "partial-uninstall"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                adapter = self.native(target)
                try:
                    self.install(adapter)
                    if operation == "partial-uninstall":
                        modified = next(iter(manager.PACKAGE_FILES.values()))
                        adapter.atomic_write(modified, b"user modified\n")
                    self.write_legacy_state(adapter)
                    legacy_bytes = adapter.read_bytes(manager.STATE_FILENAME)
                    self.assertIn(manager.LEGACY_STATE_SCHEMA.encode(), legacy_bytes)
                    if operation == "reinstall":
                        self.install(adapter)
                    else:
                        self.uninstall(adapter)
                finally:
                    adapter.close()
                state_path = target / manager.STATE_FILENAME
                if operation == "full-uninstall":
                    self.assertFalse(state_path.exists())
                else:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    self.assertEqual(manager.SCHEMA, state["schema"])
                    self.assertEqual("windows-ntfs-handle", state["rootIdentity"]["mode"])
                self.assertFalse((target / manager.JOURNAL_FILENAME).exists())

    def test_path_stat_journal_v3_restart_matrix_is_deterministic(self) -> None:
        cases = [
            (operation, phase)
            for operation in ("install", "uninstall")
            for phase in ("prepared", "applying", "rollingBack", "committed")
        ]
        for operation, phase in cases:
            with self.subTest(operation=operation, phase=phase), tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                path_adapter = manager.PathRepositoryAdapter(target)
                committed_uninstall = operation == "uninstall" and phase == "committed"
                if committed_uninstall:
                    with patch.object(
                        manager, "_validate_codex_version", return_value="0.155.1"
                    ):
                        manager.install(target, REPOSITORY_ROOT, False)
                    relative = manager.STATE_FILENAME
                    actions = manager._prepare_relative_actions(
                        path_adapter,
                        "d" * 32,
                        [("delete", relative, None)],
                    )
                else:
                    relative = next(iter(manager.PACKAGE_FILES.values()))
                    path_adapter.atomic_write(relative, b"before")
                    actions = manager._prepare_relative_actions(
                        path_adapter,
                        "d" * 32,
                        [
                            (
                                "write" if operation == "install" else "delete",
                                relative,
                                b"after" if operation == "install" else None,
                            )
                        ],
                    )
                rollback_directories = manager._plan_rollback_directories(
                    path_adapter, actions
                )
                manager._prepare_backups(path_adapter, actions)
                should_be_post = phase != "prepared"
                if should_be_post:
                    if operation == "install":
                        path_adapter.atomic_write(relative, b"after")
                    else:
                        path_adapter.unlink(relative)
                    actions[0].phase = "applied" if phase != "rollingBack" else "rollingBack"
                manager._write_relative_json(
                    path_adapter,
                    manager.JOURNAL_FILENAME,
                    manager._journal_value(
                        target,
                        "d" * 32,
                        operation,
                        actions,
                        phase=phase,
                        root_identity=path_adapter.root_identity,
                        rollback_directories=rollback_directories,
                    ),
                    path_adapter.root_identity,
                )

                native = self.native(target)
                try:
                    output = self.recover(native)
                finally:
                    native.close()
                self.assertFalse((target / manager.JOURNAL_FILENAME).exists())
                if phase == "committed":
                    self.assertEqual(
                        "RECOVERED: committed transaction cleanup finalized\n", output
                    )
                    if committed_uninstall:
                        self.assertFalse((target / manager.STATE_FILENAME).exists())
                    else:
                        self.assertEqual(operation == "install", (target / relative).exists())
                else:
                    self.assertEqual(
                        "RECOVERED: interrupted transaction rolled back\n", output
                    )
                    self.assertEqual(b"before", (target / relative).read_bytes())


if __name__ == "__main__":
    unittest.main()
