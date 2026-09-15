from __future__ import annotations

import ctypes
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import workflow_manager as manager


@unittest.skipUnless(sys.platform == "win32", "Windows native activation only")
class WindowsAdapterActivationTests(unittest.TestCase):
    def open_native(self, target: Path) -> manager.WindowsRepositoryAdapter:
        adapter = manager._repository_adapter_from_raw_target(str(target))
        self.assertIs(type(adapter), manager.WindowsRepositoryAdapter)
        return adapter

    def native_install(self, target: Path) -> None:
        adapter = self.open_native(target)
        try:
            with patch.object(
                manager, "_validate_codex_version", return_value="0.154.0"
            ):
                manager.install(target, REPOSITORY_ROOT, False, adapter=adapter)
        finally:
            adapter.close()

    def native_uninstall(self, target: Path) -> None:
        adapter = self.open_native(target)
        try:
            manager.uninstall(target, False, adapter=adapter)
        finally:
            adapter.close()

    def test_native_adapter_runs_complete_install_reinstall_uninstall_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.native_install(target)
            state = json.loads(
                (target / manager.STATE_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual("windows-ntfs-handle", state["rootIdentity"]["mode"])
            self.native_install(target)
            self.native_uninstall(target)
            self.assertFalse((target / manager.STATE_FILENAME).exists())
            self.assertFalse((target / manager.JOURNAL_FILENAME).exists())
            self.assertFalse((target / manager.INSTALL_DIRECTORY).exists())

    def test_matching_path_stat_state_migrates_and_mismatch_is_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with patch.object(
                manager, "_validate_codex_version", return_value="0.154.0"
            ):
                manager.install(target, REPOSITORY_ROOT, False)
            state_path = target / manager.STATE_FILENAME
            path_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("path-stat", path_state["rootIdentity"]["mode"])

            self.native_install(target)
            native_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "windows-ntfs-handle", native_state["rootIdentity"]["mode"]
            )

            mismatched = dict(native_state)
            mismatched["rootIdentity"] = dict(native_state["rootIdentity"])
            mismatched["rootIdentity"]["fileId"] = "f" * 32
            mismatched_bytes = (
                json.dumps(mismatched, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            state_path.write_bytes(mismatched_bytes)
            sentinel = target / "outside-sentinel.txt"
            sentinel.write_bytes(b"unchanged")

            adapter = self.open_native(target)
            try:
                with patch.object(
                    manager, "_validate_codex_version", return_value="0.154.0"
                ):
                    with self.assertRaisesRegex(
                        manager.WorkflowError, "root identity does not match"
                    ):
                        manager.install(
                            target, REPOSITORY_ROOT, False, adapter=adapter
                        )
            finally:
                adapter.close()
            self.assertEqual(mismatched_bytes, state_path.read_bytes())
            self.assertEqual(b"unchanged", sentinel.read_bytes())
            self.assertFalse((target / manager.JOURNAL_FILENAME).exists())

    def test_matching_path_stat_journal_recovers_through_native_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            path_adapter = manager.PathRepositoryAdapter(target)
            relative = next(iter(manager.PACKAGE_FILES.values()))
            path_adapter.atomic_write(relative, b"before")
            transaction_id = "a" * 32
            actions = manager._prepare_relative_actions(
                path_adapter,
                transaction_id,
                [("write", relative, b"after")],
            )
            manager._write_relative_json(
                path_adapter,
                manager.JOURNAL_FILENAME,
                manager._journal_value(
                    target,
                    transaction_id,
                    "install",
                    actions,
                    root_identity=path_adapter.root_identity,
                ),
                path_adapter.root_identity,
            )

            native = self.open_native(target)
            captured_modes: list[str] = []
            original_write = native.atomic_write

            def recording_write(relative_path: str, content: bytes) -> None:
                if relative_path == manager.JOURNAL_FILENAME:
                    value = json.loads(content.decode("utf-8"))
                    captured_modes.append(value["rootIdentity"]["mode"])
                original_write(relative_path, content)

            try:
                with patch.object(native, "atomic_write", side_effect=recording_write):
                    manager.recover(
                        target,
                        target / manager.JOURNAL_FILENAME,
                        False,
                        adapter=native,
                    )
            finally:
                native.close()

            self.assertTrue(captured_modes)
            self.assertEqual({"windows-ntfs-handle"}, set(captured_modes))
            self.assertEqual(b"before", (target / relative).read_bytes())
            self.assertFalse((target / manager.JOURNAL_FILENAME).exists())

    def test_unsupported_native_acquisition_has_no_pathname_fallback(self) -> None:
        class FakeRepositoryFsError(RuntimeError):
            pass

        class FakeUnsupportedTargetError(FakeRepositoryFsError):
            pass

        class FakeReparsePointError(FakeRepositoryFsError):
            pass

        class FailingRepositoryFs:
            def __init__(self, _target: str) -> None:
                raise FakeUnsupportedTargetError("unsupported test target")

        fake_module = SimpleNamespace(
            RepositoryFs=FailingRepositoryFs,
            RepositoryFsError=FakeRepositoryFsError,
            UnsupportedTargetError=FakeUnsupportedTargetError,
            ReparsePointError=FakeReparsePointError,
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                manager, "_load_windows_handle_fs", return_value=fake_module
            ):
                with patch.object(
                    manager.PathRepositoryAdapter,
                    "__init__",
                    side_effect=AssertionError("pathname fallback selected"),
                ):
                    with self.assertRaisesRegex(
                        manager.WorkflowError, "Unsupported Windows repository target"
                    ):
                        manager._repository_adapter_from_raw_target(directory)

    def test_adapter_closes_native_root_once_and_normalizes_os_errors(self) -> None:
        close_count = 0

        class FakeRepositoryFsError(RuntimeError):
            pass

        class FakeUnsupportedTargetError(FakeRepositoryFsError):
            pass

        class FakeReparsePointError(FakeRepositoryFsError):
            pass

        class FakeRepositoryFs:
            def __init__(self, target: str) -> None:
                self.display_path = target
                self.identity = SimpleNamespace(
                    volume_serial_number=1,
                    file_id="01" + "00" * 15,
                )

            def read_bytes(self, _relative: str) -> bytes:
                raise ctypes.WinError(5)

            def close(self) -> None:
                nonlocal close_count
                close_count += 1

        fake_module = SimpleNamespace(
            RepositoryFs=FakeRepositoryFs,
            RepositoryFsError=FakeRepositoryFsError,
            UnsupportedTargetError=FakeUnsupportedTargetError,
            ReparsePointError=FakeReparsePointError,
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                manager, "_load_windows_handle_fs", return_value=fake_module
            ):
                adapter = manager.WindowsRepositoryAdapter(Path(directory))
                with self.assertRaisesRegex(
                    manager.WorkflowError,
                    r"reading file for managed/file.txt: winerror=5",
                ):
                    adapter.read_bytes("managed/file.txt")
                adapter.close()
                adapter.close()
        self.assertEqual(1, close_count)

    def test_main_emits_lifecycle_pass_only_after_successful_close(self) -> None:
        class FakeAdapter:
            root = Path("C:/test-repository")

            def __init__(self, close_error: bool) -> None:
                self.close_error = close_error

            def close(self) -> None:
                if self.close_error:
                    raise manager.WorkflowError("injected native close failure")

        for operation, function_name in (
            ("install", "install"),
            ("uninstall", "uninstall"),
        ):
            for dry_run in (False, True):
                prefix = "DRY-RUN PASS" if dry_run else "PASS"
                success = f"{prefix}: deferred {operation} success"
                arguments = [
                    "workflow_manager.py",
                    operation,
                    "--target",
                    "C:/test-repository",
                ]
                if dry_run:
                    arguments.append("--dry-run")
                for close_error, expected_code, expected_out, expected_err in (
                    (False, 0, success + "\n", ""),
                    (True, 1, "", "FAIL: injected native close failure\n"),
                ):
                    with self.subTest(
                        operation=operation,
                        dry_run=dry_run,
                        close_error=close_error,
                    ):
                        output = StringIO()
                        errors = StringIO()
                        adapter = FakeAdapter(close_error)
                        with patch.object(sys, "argv", arguments):
                            with patch.object(
                                manager,
                                "_repository_adapter_from_raw_target",
                                return_value=adapter,
                            ):
                                with patch.object(
                                    manager, function_name, return_value=success
                                ):
                                    with redirect_stdout(output), redirect_stderr(errors):
                                        result = manager.main()
                        self.assertEqual(expected_code, result)
                        self.assertEqual(expected_out, output.getvalue())
                        self.assertEqual(expected_err, errors.getvalue())


class RecoveryOutputContractTests(unittest.TestCase):
    transaction_id = "c" * 32

    def write_journal(
        self,
        adapter: manager.PathRepositoryAdapter,
        operation: str,
        phase: str,
        actions: list[manager.Action],
    ) -> None:
        manager._write_relative_json(
            adapter,
            manager.JOURNAL_FILENAME,
            manager._journal_value(
                adapter.root,
                self.transaction_id,
                operation,
                actions,
                phase=phase,
                root_identity=adapter.root_identity,
            ),
            adapter.root_identity,
        )

    def capture_recover(
        self, adapter: manager.PathRepositoryAdapter, dry_run: bool
    ) -> str:
        output = StringIO()
        with redirect_stdout(output):
            manager.recover(
                adapter.root,
                adapter.root / manager.JOURNAL_FILENAME,
                dry_run,
                adapter=adapter,
            )
        return output.getvalue()

    def prepared_action(
        self, adapter: manager.PathRepositoryAdapter
    ) -> tuple[str, list[manager.Action]]:
        relative = next(iter(manager.PACKAGE_FILES.values()))
        adapter.atomic_write(relative, b"before")
        actions = manager._prepare_relative_actions(
            adapter,
            self.transaction_id,
            [("write", relative, b"after")],
        )
        return relative, actions

    def test_no_journal_and_dry_run_outputs_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = manager.PathRepositoryAdapter(Path(directory))
            self.assertEqual("No interrupted transaction found.\n", self.capture_recover(adapter, False))
            _, actions = self.prepared_action(adapter)
            self.write_journal(adapter, "install", "prepared", actions)
            self.assertEqual(
                "DRY-RUN: would roll back 0 applied action(s)\n",
                self.capture_recover(adapter, True),
            )

    def test_real_rollback_output_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = manager.PathRepositoryAdapter(Path(directory))
            relative, actions = self.prepared_action(adapter)
            manager._prepare_backups(adapter, actions)
            adapter.atomic_write(relative, b"after")
            actions[0].phase = "applied"
            self.write_journal(adapter, "install", "applying", actions)
            self.assertEqual(
                "RECOVERED: interrupted transaction rolled back\n",
                self.capture_recover(adapter, False),
            )
            self.assertEqual(b"before", adapter.read_bytes(relative))

    def test_committed_install_cleanup_output_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = manager.PathRepositoryAdapter(Path(directory))
            relative, actions = self.prepared_action(adapter)
            manager._prepare_backups(adapter, actions)
            adapter.atomic_write(relative, b"after")
            actions[0].phase = "applied"
            self.write_journal(adapter, "install", "committed", actions)
            self.assertEqual(
                "RECOVERED: committed transaction cleanup finalized\n",
                self.capture_recover(adapter, False),
            )

    def test_committed_uninstall_cleanup_output_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with patch.object(
                manager, "_validate_codex_version", return_value="0.154.0"
            ):
                manager.install(target, REPOSITORY_ROOT, False)
            adapter = manager.PathRepositoryAdapter(target)
            actions = manager._prepare_relative_actions(
                adapter,
                self.transaction_id,
                [("delete", manager.STATE_FILENAME, None)],
            )
            manager._prepare_backups(adapter, actions)
            adapter.unlink(manager.STATE_FILENAME)
            actions[0].phase = "applied"
            self.write_journal(adapter, "uninstall", "committed", actions)
            self.assertEqual(
                "RECOVERED: committed transaction cleanup finalized\n",
                self.capture_recover(adapter, False),
            )


if __name__ == "__main__":
    unittest.main()
