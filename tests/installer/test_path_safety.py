from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import workflow_manager as manager


class PathSafetyTests(unittest.TestCase):
    transaction_id = "b" * 32
    created_at = "2026-09-14T12:00:00+00:00"

    def create_directory_link(self, link: Path, destination: Path) -> None:
        def remove_link() -> None:
            try:
                os.rmdir(link)
            except FileNotFoundError:
                pass

        if os.name == "nt":
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(destination)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.skipTest(f"Windows junction creation unavailable: {result.stderr.strip()}")
            self.addCleanup(remove_link)
        else:
            link.symlink_to(destination, target_is_directory=True)
            self.addCleanup(remove_link)

    def assert_sentinel(self, sentinel: Path) -> None:
        self.assertEqual(b"external-safe", sentinel.read_bytes())

    def test_target_link_is_rejected_before_external_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external"
            external.mkdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_bytes(b"external-safe")
            link = root / "target-link"
            self.create_directory_link(link, external)

            with self.assertRaisesRegex(manager.WorkflowError, "link or reparse point"):
                manager._assert_safe_target(str(link))

            self.assert_sentinel(sentinel)

    def test_managed_parent_link_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            external = root / "external"
            target.mkdir()
            external.mkdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_bytes(b"external-safe")
            linked_parent = target / "managed"
            self.create_directory_link(linked_parent, external)

            with self.assertRaisesRegex(manager.WorkflowError, "link or reparse point"):
                manager._prepare_actions(
                    target,
                    self.transaction_id,
                    [("write", linked_parent / "created.txt", b"unsafe")],
                )

            self.assert_sentinel(sentinel)
            self.assertFalse((external / "created.txt").exists())

    def test_backup_root_link_is_rejected_before_backup_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            external = root / "external"
            target.mkdir()
            external.mkdir()
            existing = target / "existing.txt"
            existing.write_bytes(b"before")
            sentinel = external / "sentinel.txt"
            sentinel.write_bytes(b"external-safe")
            self.create_directory_link(target / manager.BACKUP_DIRECTORY, external)

            with self.assertRaisesRegex(manager.WorkflowError, "link or reparse point"):
                manager._prepare_actions(
                    target,
                    self.transaction_id,
                    [("write", existing, b"after")],
                )

            self.assertEqual(b"before", existing.read_bytes())
            self.assert_sentinel(sentinel)

    def test_recovery_time_parent_link_is_rejected_and_journal_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            external = root / "external"
            target.mkdir()
            external.mkdir()
            managed_parent = target / "managed"
            managed_parent.mkdir()
            path = managed_parent / "created.txt"
            actions = manager._prepare_actions(
                target,
                self.transaction_id,
                [("write", path, b"installed")],
            )
            manager._atomic_write_managed(target, path, b"installed")
            actions[0].phase = "applied"
            journal_path = target / manager.JOURNAL_FILENAME
            manager._write_json(
                journal_path,
                manager._journal_value(
                    target,
                    self.transaction_id,
                    "install",
                    actions,
                    "applying",
                    self.created_at,
                ),
                target,
            )
            path.unlink()
            managed_parent.rmdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_bytes(b"external-safe")
            self.create_directory_link(managed_parent, external)

            with self.assertRaisesRegex(manager.WorkflowError, "link or reparse point"):
                manager.recover(target, journal_path, False)

            self.assertTrue(journal_path.exists())
            self.assert_sentinel(sentinel)

    def test_cleanup_rejects_linked_backup_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            external = root / "external"
            target.mkdir()
            external.mkdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_bytes(b"external-safe")
            self.create_directory_link(target / manager.BACKUP_DIRECTORY, external)

            with self.assertRaisesRegex(manager.WorkflowError, "link or reparse point"):
                manager._cleanup_backup_files(
                    target,
                    [target / manager.BACKUP_DIRECTORY / "sentinel.txt"],
                )

            self.assert_sentinel(sentinel)


if __name__ == "__main__":
    unittest.main()
