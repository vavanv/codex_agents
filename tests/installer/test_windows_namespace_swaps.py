from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Callable
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHILD_HELPER = Path(__file__).with_name("windows_namespace_child.py")
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import windows_handle_fs as handle_fs
import workflow_manager as manager


class NamespaceSwap:
    def __init__(self, victim: Path, parked: Path, external: Path) -> None:
        self.victim = victim
        self.parked = parked
        self.external = external
        self.ready = threading.Event()
        self.release = threading.Event()
        self.swapped = threading.Event()
        self.restore = threading.Event()
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.restore_error: BaseException | None = None
        self.moved_victim = False
        self.moved_external = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.ready.set()
        if not self.release.wait(5):
            self.error = TimeoutError("namespace swap release barrier timed out")
            self.done.set()
            return
        try:
            os.replace(self.victim, self.parked)
            self.moved_victim = True
            os.replace(self.external, self.victim)
            self.moved_external = True
        except BaseException as error:
            self.error = error
        finally:
            self.swapped.set()
        if self.moved_victim:
            if not self.restore.wait(5):
                self.error = TimeoutError("namespace restore barrier timed out")
            self.restore_if_needed()
        self.done.set()

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(5):
            raise AssertionError("namespace swap ready barrier timed out")

    def trigger(self) -> None:
        self.release.set()
        if not self.swapped.wait(5):
            raise AssertionError("namespace swap barrier timed out")

    def finish(self) -> None:
        self.restore.set()
        if not self.done.wait(5):
            raise AssertionError("namespace restore completion barrier timed out")
        self.thread.join(5)

    def restore_if_needed(self) -> None:
        try:
            if self.moved_external and self.victim.exists() and not self.external.exists():
                os.replace(self.victim, self.external)
                self.moved_external = False
            if self.moved_victim and self.parked.exists() and not self.victim.exists():
                os.replace(self.parked, self.victim)
                self.moved_victim = False
            if self.moved_victim and not self.parked.exists():
                self.moved_victim = False
        except BaseException as error:
            self.restore_error = error


@unittest.skipUnless(sys.platform == "win32", "Windows namespace swap tests only")
class WindowsNamespaceSwapTests(unittest.TestCase):
    def assert_external_unchanged(
        self, sentinel: Path, expected_identity: tuple[int, int], expected: bytes
    ) -> None:
        value = sentinel.stat()
        self.assertEqual(expected_identity, (value.st_dev, value.st_ino))
        self.assertEqual(expected, sentinel.read_bytes())

    def run_during_swap(
        self, swap: NamespaceSwap, operation: Callable[[], object]
    ) -> object:
        swap.trigger()
        try:
            if swap.error is not None:
                self.assertIsInstance(swap.error, OSError)
                self.assertFalse(swap.moved_victim)
            else:
                self.assertTrue(swap.moved_victim)
                self.assertTrue(swap.moved_external)
                self.assertTrue(swap.parked.is_dir())
                self.assertTrue(swap.victim.is_dir())
            return operation()
        finally:
            swap.finish()
            self.assertTrue(swap.external.is_dir())

    def native(self, target: Path) -> manager.WindowsRepositoryAdapter:
        adapter = manager._repository_adapter_from_raw_target(str(target))
        self.assertIs(type(adapter), manager.WindowsRepositoryAdapter)
        return adapter

    def install(self, adapter: manager.RepositoryAdapter) -> None:
        with patch.object(manager, "_validate_codex_version", return_value="0.155.1"):
            with redirect_stdout(StringIO()):
                manager.install(
                    adapter.root, REPOSITORY_ROOT, False, adapter=adapter
                )

    def wait_for_path(
        self, path: Path, process: subprocess.Popen[bytes], timeout: float = 10.0
    ) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists():
            if process.poll() is not None:
                self.fail(
                    "fresh-process helper exited before reaching its identity barrier"
                )
            if time.monotonic() >= deadline:
                self.fail("fresh-process identity barrier timed out")
            time.sleep(0.01)

    def run_fresh_process_recovery(
        self,
        target: Path,
        trigger: str,
        swap: NamespaceSwap,
        coordination: Path,
    ) -> dict[str, object]:
        coordination.mkdir()
        ready = coordination / "ready"
        release = coordination / "release"
        result = coordination / "result.json"
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(CHILD_HELPER),
                str(target),
                trigger,
                str(ready),
                str(release),
                str(result),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        swap_started = False
        try:
            self.wait_for_path(ready, process)
            swap.start()
            swap_started = True
            swap.trigger()
            if swap.error is not None:
                self.assertIsInstance(swap.error, OSError)
                self.assertFalse(swap.moved_victim)
            else:
                self.assertTrue(swap.moved_victim)
                self.assertTrue(swap.moved_external)
            release.write_text("release\n", encoding="utf-8")
            process.wait(timeout=15)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            raise
        finally:
            if swap_started:
                swap.finish()
        self.assertEqual(0, process.returncode)
        self.assertTrue(result.is_file())
        outcome = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual({"ok": True}, outcome)
        return outcome

    def run_fresh_process_after_missing_observation(
        self,
        target: Path,
        trigger: str,
        external: Path,
        inserted: Path,
        coordination: Path,
    ) -> dict[str, object]:
        coordination.mkdir()
        ready = coordination / "ready"
        release = coordination / "release"
        result = coordination / "result.json"
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(CHILD_HELPER),
                str(target),
                trigger,
                str(ready),
                str(release),
                str(result),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            try:
                self.wait_for_path(ready, process)
            except AssertionError as error:
                error_type = "unknown"
                error_message = ""
                if result.is_file():
                    child_result = json.loads(result.read_text(encoding="utf-8"))
                    error_type = str(child_result.get("errorType", "unknown"))
                    error_message = str(child_result.get("error", ""))
                raise AssertionError(
                    "fresh-process missing-directory barrier failed: "
                    f"{error_type}: {error_message}"
                ) from error
            os.replace(external, inserted)
            release.write_text("release\n", encoding="utf-8")
            process.wait(timeout=15)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            raise
        self.assertEqual(0, process.returncode)
        self.assertTrue(result.is_file())
        outcome = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual({"ok": True}, outcome)
        return outcome

    def test_constructor_pins_opened_target_and_absolute_ancestor(self) -> None:
        for replacement in ("target", "absolute-ancestor"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as directory:
                run_root = Path(directory)
                ancestor = run_root / "acquired-ancestor"
                target = ancestor / "repository"
                target.mkdir(parents=True)
                (target / "payload.bin").write_bytes(b"pinned-original")
                external = run_root / "external"
                external.mkdir()
                sentinel = external / "sentinel.bin"
                sentinel.write_bytes(b"external-safe")
                sentinel_stat = sentinel.stat()
                sentinel_identity = (sentinel_stat.st_dev, sentinel_stat.st_ino)
                if replacement == "target":
                    (external / "payload.bin").write_bytes(b"external-decoy")
                    victim = target
                    trigger_name = target.name
                else:
                    (external / "repository").mkdir()
                    (external / "repository" / "payload.bin").write_bytes(
                        b"external-decoy"
                    )
                    victim = ancestor
                    trigger_name = ancestor.name
                parked = run_root / f"parked-{replacement}"
                swap = NamespaceSwap(victim, parked, external)
                acquired = threading.Event()
                release_constructor = threading.Event()
                constructed: list[handle_fs.RepositoryFs] = []
                errors: list[BaseException] = []
                original_open = handle_fs.RepositoryFs._open_component

                def barrier_open(
                    filesystem: handle_fs.RepositoryFs,
                    parent: handle_fs.SafeHandle,
                    name: str,
                    **kwargs: object,
                ) -> handle_fs.SafeHandle:
                    opened = original_open(filesystem, parent, name, **kwargs)
                    if name == trigger_name and not acquired.is_set():
                        acquired.set()
                        if not release_constructor.wait(10):
                            opened.close()
                            raise TimeoutError("constructor release barrier timed out")
                    return opened

                def construct() -> None:
                    try:
                        constructed.append(handle_fs.RepositoryFs(str(target)))
                    except BaseException as error:
                        errors.append(error)

                constructor = threading.Thread(target=construct, daemon=True)
                swap_started = False
                try:
                    with patch.object(
                        handle_fs.RepositoryFs,
                        "_open_component",
                        autospec=True,
                        side_effect=barrier_open,
                    ):
                        constructor.start()
                        self.assertTrue(
                            acquired.wait(10),
                            "constructor acquisition barrier timed out",
                        )
                        swap.start()
                        swap_started = True
                        swap.trigger()
                        if swap.error is not None:
                            self.assertIsInstance(swap.error, OSError)
                            self.assertFalse(swap.moved_victim)
                        else:
                            self.assertTrue(swap.moved_victim)
                            self.assertTrue(swap.moved_external)
                        release_constructor.set()
                        constructor.join(10)
                        self.assertFalse(constructor.is_alive())
                    self.assertEqual([], errors)
                    self.assertEqual(1, len(constructed))
                    self.assertEqual(
                        b"pinned-original",
                        constructed[0].read_bytes("payload.bin"),
                    )
                finally:
                    release_constructor.set()
                    if constructor.is_alive():
                        constructor.join(10)
                    for filesystem in constructed:
                        filesystem.close()
                    if swap_started:
                        swap.finish()
                    swap.restore_if_needed()
                self.assertFalse(swap.moved_victim)
                self.assertFalse(swap.moved_external)
                self.assert_external_unchanged(
                    sentinel, sentinel_identity, b"external-safe"
                )
                if replacement == "target":
                    self.assertEqual(
                        b"external-decoy", (external / "payload.bin").read_bytes()
                    )
                else:
                    self.assertEqual(
                        b"external-decoy",
                        (external / "repository" / "payload.bin").read_bytes(),
                    )

    def test_root_and_ancestor_capabilities_block_all_native_redirection_stages(self) -> None:
        stages = ("root", "read", "hash", "mkdir", "write", "backup", "replace", "unlink", "rmdir")
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                run_root = Path(directory)
                repository = run_root / "repository"
                repository.mkdir()
                managed = repository / "managed"
                managed.mkdir()
                (managed / "read.bin").write_bytes(b"original-read")
                (managed / "replace.bin").write_bytes(b"original-replace")
                (managed / "unlink.bin").write_bytes(b"original-unlink")
                (managed / "empty").mkdir()
                (managed / "backup").mkdir()
                external = run_root / "external"
                external.mkdir()
                sentinel = external / "sentinel.bin"
                sentinel.write_bytes(b"external-safe")
                sentinel_stat = sentinel.stat()
                sentinel_identity = (sentinel_stat.st_dev, sentinel_stat.st_ino)
                filesystem = handle_fs.RepositoryFs(str(repository))
                swap: NamespaceSwap | None = None
                try:
                    pinned = filesystem.directory_identity("managed")
                    operations = {
                        "root": lambda: filesystem.read_bytes("managed/read.bin"),
                        "read": lambda: filesystem.read_bytes("managed/read.bin"),
                        "hash": lambda: filesystem.hash_file("managed/read.bin"),
                        "mkdir": lambda: filesystem.ensure_directories("managed/new/child"),
                        "write": lambda: filesystem.atomic_write("managed/new.bin", b"new"),
                        "backup": lambda: filesystem.atomic_write(
                            "managed/backup/read.bin",
                            filesystem.read_bytes("managed/read.bin"),
                        ),
                        "replace": lambda: filesystem.atomic_write(
                            "managed/replace.bin", b"replacement"
                        ),
                        "unlink": lambda: filesystem.unlink("managed/unlink.bin"),
                        "rmdir": lambda: filesystem.rmdir("managed/empty"),
                    }
                    victim = repository if stage == "root" else managed
                    swap = NamespaceSwap(
                        victim,
                        run_root / f"parked-{stage}",
                        external,
                    )
                    swap.start()
                    self.run_during_swap(swap, operations[stage])
                    self.assertEqual(pinned, filesystem.directory_identity("managed"))
                finally:
                    filesystem.close()
                    if swap is not None:
                        swap.restore_if_needed()
                self.assertFalse(swap.moved_victim)
                self.assertFalse(swap.moved_external)
                self.assert_external_unchanged(
                    sentinel, sentinel_identity, b"external-safe"
                )
                if stage == "read":
                    self.assertEqual(b"original-read", (managed / "read.bin").read_bytes())
                elif stage == "hash":
                    self.assertEqual(
                        hashlib.sha256(b"original-read").hexdigest(),
                        hashlib.sha256((managed / "read.bin").read_bytes()).hexdigest(),
                    )
                elif stage == "replace":
                    self.assertEqual(b"replacement", (managed / "replace.bin").read_bytes())
                elif stage == "unlink":
                    self.assertFalse((managed / "unlink.bin").exists())
                elif stage == "rmdir":
                    self.assertFalse((managed / "empty").exists())

    def test_expected_absent_open_fails_closed_and_is_not_owned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            filesystem = handle_fs.RepositoryFs(str(repository))
            raced = repository / "raced"
            raced.mkdir()
            sentinel = raced / "sentinel.bin"
            sentinel.write_bytes(b"not-transaction-owned")
            try:
                with self.assertRaises(handle_fs.DirectoryRaceError):
                    filesystem.ensure_directory("raced", expected_absent=True)
                self.assertEqual(b"not-transaction-owned", sentinel.read_bytes())
                self.assertTrue(raced.is_dir())
            finally:
                filesystem.close()

    def test_automatic_rollback_restores_original_during_ancestor_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            managed = target / manager.INSTALL_DIRECTORY
            managed.mkdir()
            first, second = list(manager.PACKAGE_FILES.values())[:2]
            (target / second).parent.mkdir(parents=True)
            (target / first).write_bytes(b"before-a")
            (target / second).write_bytes(b"before-b")
            external = target / "external"
            (external / "nested").mkdir(parents=True)
            sentinel = external / "sentinel.bin"
            sentinel.write_bytes(b"external-safe")
            (external / "nested" / "shape.bin").write_bytes(b"shape-safe")
            sentinel_stat = sentinel.stat()
            sentinel_identity = (sentinel_stat.st_dev, sentinel_stat.st_ino)
            external_shape = sorted(
                (
                    item.relative_to(external).as_posix(),
                    item.is_dir(),
                    None if item.is_dir() else item.read_bytes(),
                )
                for item in external.rglob("*")
            )
            adapter = self.native(target)
            actions = manager._prepare_relative_actions(
                adapter,
                "a" * 32,
                [("write", first, b"after-a"), ("write", second, b"after-b")],
            )
            swap = NamespaceSwap(managed, target / "managed-parked", external)
            swap.start()
            original_write = adapter._filesystem.atomic_write
            first_writes = 0
            failed = False
            rollback_swapped = False

            def fail_then_swap(path: str, content: bytes) -> None:
                nonlocal first_writes, failed, rollback_swapped
                if path == first:
                    first_writes += 1
                    if first_writes == 2:
                        rollback_swapped = True
                        self.run_during_swap(
                            swap, lambda: original_write(path, content)
                        )
                        return
                if path == second and not failed:
                    failed = True
                    raise OSError(5, "injected apply failure")
                original_write(path, content)

            try:
                with patch.object(
                    adapter._filesystem,
                    "atomic_write",
                    side_effect=fail_then_swap,
                ):
                    with self.assertRaises(manager.WorkflowError):
                        manager._apply_relative_actions(
                            adapter,
                            manager.JOURNAL_FILENAME,
                            "a" * 32,
                            "install",
                            actions,
                        )
            finally:
                adapter.close()
                swap.release.set()
                swap.finish()
                swap.restore_if_needed()
            self.assertTrue(failed)
            self.assertTrue(rollback_swapped)
            self.assertEqual(2, first_writes)
            self.assertEqual(b"before-a", (target / first).read_bytes())
            self.assertEqual(b"before-b", (target / second).read_bytes())
            self.assertFalse((target / manager.JOURNAL_FILENAME).exists())
            self.assertEqual(
                external_shape,
                sorted(
                    (
                        item.relative_to(external).as_posix(),
                        item.is_dir(),
                        None if item.is_dir() else item.read_bytes(),
                    )
                    for item in external.rglob("*")
                ),
            )
            self.assert_external_unchanged(
                sentinel, sentinel_identity, b"external-safe"
            )

    def test_root_and_ancestor_identity_replacements_fail_before_mutation(self) -> None:
        for replacement in ("root", "ancestor"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as directory:
                run_root = Path(directory)
                target = run_root / "repository"
                target.mkdir()
                adapter = self.native(target)
                try:
                    self.install(adapter)
                finally:
                    adapter.close()
                state_bytes = (target / manager.STATE_FILENAME).read_bytes()
                original_file = target / next(iter(manager.PACKAGE_FILES.values()))
                original_bytes = original_file.read_bytes()
                original_stat = original_file.stat()
                original_identity = (original_stat.st_dev, original_stat.st_ino)

                if replacement == "root":
                    parked = run_root / "repository-parked"
                    os.replace(target, parked)
                    target.mkdir()
                    (target / manager.STATE_FILENAME).write_bytes(state_bytes)
                    parked_file = parked / next(iter(manager.PACKAGE_FILES.values()))
                else:
                    parked = target / "workflow-parked"
                    os.replace(target / manager.INSTALL_DIRECTORY, parked)
                    (target / manager.INSTALL_DIRECTORY).mkdir()
                    parked_file = parked / Path(
                        next(iter(manager.PACKAGE_FILES.values()))
                    ).relative_to(manager.INSTALL_DIRECTORY)

                replacement_adapter = self.native(target)
                try:
                    with self.assertRaisesRegex(
                        manager.WorkflowError,
                        "(?:Repository root|Managed directory) identity does not match",
                    ):
                        with redirect_stdout(StringIO()):
                            manager.uninstall(
                                target, False, adapter=replacement_adapter
                            )
                finally:
                    replacement_adapter.close()
                self.assertEqual(state_bytes, (target / manager.STATE_FILENAME).read_bytes())
                self.assertFalse((target / manager.JOURNAL_FILENAME).exists())
                parked_stat = parked_file.stat()
                self.assertEqual(
                    original_identity, (parked_stat.st_dev, parked_stat.st_ino)
                )
                self.assertEqual(original_bytes, parked_file.read_bytes())

    def test_fresh_process_delete_recovery_pins_original_action_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            managed = target / manager.INSTALL_DIRECTORY
            managed.mkdir()
            relative = next(iter(manager.PACKAGE_FILES.values()))
            (target / relative).write_bytes(b"before")
            external = target / "external"
            external.mkdir()
            sentinel = external / "sentinel.bin"
            sentinel.write_bytes(b"external-safe")
            (external / "decoy.bin").write_bytes(b"external-decoy")
            stat_value = sentinel.stat()
            sentinel_identity = (stat_value.st_dev, stat_value.st_ino)

            adapter = self.native(target)
            transaction_id = "d" * 32
            actions = manager._prepare_relative_actions(
                adapter,
                transaction_id,
                [("delete", relative, None)],
            )
            rollback_directories, records = self._prepare_fixture_directories(
                adapter, actions
            )
            self.assertIn(
                manager.INSTALL_DIRECTORY,
                [record["path"] for record in records],
            )
            self.assertNotIn(manager.INSTALL_DIRECTORY, rollback_directories)
            manager._prepare_backups(adapter, actions)
            adapter.unlink(relative)
            actions[0].phase = "applied"
            manager._write_relative_json(
                adapter,
                manager.JOURNAL_FILENAME,
                manager._journal_value(
                    target,
                    transaction_id,
                    "install",
                    actions,
                    phase="applying",
                    root_identity=adapter.root_identity,
                    rollback_directories=rollback_directories,
                    directories=records,
                ),
                adapter.root_identity,
            )
            adapter.close()
            swap = NamespaceSwap(
                managed, target / "managed-parked", external
            )
            self.run_fresh_process_recovery(
                target,
                manager.INSTALL_DIRECTORY,
                swap,
                target / "coordination",
            )
            self.assertEqual(b"before", (target / relative).read_bytes())
            self.assertFalse((target / manager.JOURNAL_FILENAME).exists())
            self.assertEqual(b"external-decoy", (external / "decoy.bin").read_bytes())
            self.assert_external_unchanged(
                sentinel, sentinel_identity, b"external-safe"
            )

    def test_fresh_process_rolling_back_skips_late_inserted_created_subtree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            relative = next(iter(manager.PACKAGE_FILES.values()))
            adapter = self.native(target)
            transaction_id = "f" * 32
            actions = manager._prepare_relative_actions(
                adapter,
                transaction_id,
                [("write", relative, b"transaction-content")],
            )
            self.assertTrue(actions[0].created)
            self.assertIsNone(actions[0].pre_hash)
            rollback_directories, records = self._prepare_fixture_directories(
                adapter, actions
            )
            self.assertIn(manager.INSTALL_DIRECTORY, rollback_directories)
            manager._prepare_backups(adapter, actions)
            adapter.atomic_write(relative, b"transaction-content")
            actions[0].phase = "rollingBack"
            manager._write_relative_json(
                adapter,
                manager.JOURNAL_FILENAME,
                manager._journal_value(
                    target,
                    transaction_id,
                    "install",
                    actions,
                    phase="rollingBack",
                    root_identity=adapter.root_identity,
                    rollback_directories=rollback_directories,
                    directories=records,
                ),
                adapter.root_identity,
            )
            expected_identity = next(
                record["identity"]
                for record in records
                if record["path"] == manager.INSTALL_DIRECTORY
            )
            adapter.unlink(relative)
            adapter.rmdir(manager.INSTALL_DIRECTORY, expected_identity)
            adapter.close()

            inserted = target / manager.INSTALL_DIRECTORY
            external = target / "external"
            (external / "nested").mkdir(parents=True)
            sentinel = external / "sentinel.bin"
            sentinel.write_bytes(b"external-safe")
            (external / "nested" / "shape.bin").write_bytes(b"shape-safe")
            sentinel_stat = sentinel.stat()
            sentinel_identity = (sentinel_stat.st_dev, sentinel_stat.st_ino)
            external_shape = sorted(
                (
                    item.relative_to(external).as_posix(),
                    item.is_dir(),
                    None if item.is_dir() else item.read_bytes(),
                )
                for item in external.rglob("*")
            )
            try:
                self.run_fresh_process_after_missing_observation(
                    target,
                    manager.INSTALL_DIRECTORY,
                    external,
                    inserted,
                    target / "coordination",
                )
                self.assertFalse((target / manager.JOURNAL_FILENAME).exists())
                self.assert_external_unchanged(
                    inserted / "sentinel.bin",
                    sentinel_identity,
                    b"external-safe",
                )
                self.assertEqual(
                    external_shape,
                    sorted(
                        (
                            item.relative_to(inserted).as_posix(),
                            item.is_dir(),
                            None if item.is_dir() else item.read_bytes(),
                        )
                        for item in inserted.rglob("*")
                    ),
                )
            finally:
                if inserted.exists() and not external.exists():
                    os.replace(inserted, external)

    def test_fresh_process_committed_cleanup_pins_original_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            adapter = self.native(target)
            self.install(adapter)
            original_rmdir = adapter._filesystem.rmdir
            failed = False

            def fail_first_rmdir(path: str, **kwargs: object) -> None:
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError(5, "injected committed cleanup failure")
                original_rmdir(path, **kwargs)

            try:
                with patch.object(
                    adapter._filesystem, "rmdir", side_effect=fail_first_rmdir
                ):
                    with self.assertRaises(manager.WorkflowError):
                        with redirect_stdout(StringIO()):
                            manager.uninstall(target, False, adapter=adapter)
            finally:
                adapter.close()
            self.assertTrue(failed)
            journal = json.loads(
                (target / manager.JOURNAL_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual("committed", journal["phase"])

            managed = target / manager.INSTALL_DIRECTORY
            external = target / "external"
            external.mkdir()
            sentinel = external / "sentinel.bin"
            sentinel.write_bytes(b"external-safe")
            stat_value = sentinel.stat()
            sentinel_identity = (stat_value.st_dev, stat_value.st_ino)
            swap = NamespaceSwap(
                managed, target / "managed-parked", external
            )
            self.run_fresh_process_recovery(
                target,
                manager.INSTALL_DIRECTORY,
                swap,
                target / "coordination",
            )
            self.assertFalse((target / manager.JOURNAL_FILENAME).exists())
            self.assertFalse(managed.exists())
            self.assert_external_unchanged(
                sentinel, sentinel_identity, b"external-safe"
            )

    def test_fresh_process_committed_cleanup_skips_late_inserted_missing_subtree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            state_path = target / manager.STATE_FILENAME
            state_path.write_bytes(b"state-before")
            adapter = self.native(target)
            transaction_id = "e" * 32
            actions = manager._prepare_relative_actions(
                adapter,
                transaction_id,
                [("write", manager.STATE_FILENAME, b"state-after")],
            )
            rollback_directories, records = self._prepare_fixture_directories(
                adapter, actions
            )
            manager._prepare_backups(adapter, actions)
            adapter.atomic_write(manager.STATE_FILENAME, b"state-after")
            actions[0].phase = "applied"
            manager._write_relative_json(
                adapter,
                manager.JOURNAL_FILENAME,
                manager._journal_value(
                    target,
                    transaction_id,
                    "install",
                    actions,
                    phase="committed",
                    root_identity=adapter.root_identity,
                    rollback_directories=rollback_directories,
                    directories=records,
                ),
                adapter.root_identity,
            )
            self.assertIsNotNone(actions[0].backup)
            missing = Path(actions[0].backup).parent.as_posix()
            expected_identity = next(
                record["identity"]
                for record in records
                if record["path"] == missing
            )
            adapter.unlink(actions[0].backup)
            adapter.rmdir(missing, expected_identity)
            adapter.close()

            inserted = target / Path(missing)
            external = target / "external"
            (external / "nested").mkdir(parents=True)
            sentinel = external / "sentinel.bin"
            sentinel.write_bytes(b"external-safe")
            (external / "nested" / "shape.bin").write_bytes(b"shape-safe")
            sentinel_stat = sentinel.stat()
            sentinel_identity = (sentinel_stat.st_dev, sentinel_stat.st_ino)
            external_shape = sorted(
                (
                    item.relative_to(external).as_posix(),
                    item.is_dir(),
                    None if item.is_dir() else item.read_bytes(),
                )
                for item in external.rglob("*")
            )
            try:
                self.run_fresh_process_after_missing_observation(
                    target,
                    missing,
                    external,
                    inserted,
                    target / "coordination",
                )
                self.assertFalse((target / manager.JOURNAL_FILENAME).exists())
                self.assertEqual(b"state-after", state_path.read_bytes())
                inserted_sentinel = inserted / "sentinel.bin"
                self.assert_external_unchanged(
                    inserted_sentinel, sentinel_identity, b"external-safe"
                )
                self.assertEqual(
                    external_shape,
                    sorted(
                        (
                            item.relative_to(inserted).as_posix(),
                            item.is_dir(),
                            None if item.is_dir() else item.read_bytes(),
                        )
                        for item in inserted.rglob("*")
                    ),
                )
            finally:
                if inserted.exists() and not external.exists():
                    os.replace(inserted, external)

    def test_fresh_process_committed_uninstall_skips_late_inserted_missing_subtree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            adapter = self.native(target)
            self.install(adapter)
            original_rmdir = adapter._filesystem.rmdir
            failed = False

            def fail_managed_directory(path: str, **kwargs: object) -> None:
                nonlocal failed
                if path == manager.INSTALL_DIRECTORY and not failed:
                    failed = True
                    raise OSError(5, "injected committed cleanup failure")
                original_rmdir(path, **kwargs)

            try:
                with patch.object(
                    adapter._filesystem,
                    "rmdir",
                    side_effect=fail_managed_directory,
                ):
                    with self.assertRaises(manager.WorkflowError):
                        with redirect_stdout(StringIO()):
                            manager.uninstall(target, False, adapter=adapter)
            finally:
                adapter.close()
            self.assertTrue(failed)
            journal = json.loads(
                (target / manager.JOURNAL_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual("committed", journal["phase"])
            inserted = target / manager.INSTALL_DIRECTORY
            inserted.rmdir()

            external = target / "external"
            (external / "nested").mkdir(parents=True)
            sentinel = external / "sentinel.bin"
            sentinel.write_bytes(b"external-safe")
            (external / "nested" / "shape.bin").write_bytes(b"shape-safe")
            sentinel_stat = sentinel.stat()
            sentinel_identity = (sentinel_stat.st_dev, sentinel_stat.st_ino)
            external_shape = sorted(
                (
                    item.relative_to(external).as_posix(),
                    item.is_dir(),
                    None if item.is_dir() else item.read_bytes(),
                )
                for item in external.rglob("*")
            )
            try:
                self.run_fresh_process_after_missing_observation(
                    target,
                    manager.INSTALL_DIRECTORY,
                    external,
                    inserted,
                    target / "coordination",
                )
                self.assertFalse((target / manager.JOURNAL_FILENAME).exists())
                self.assertFalse((target / manager.STATE_FILENAME).exists())
                inserted_sentinel = inserted / "sentinel.bin"
                self.assert_external_unchanged(
                    inserted_sentinel, sentinel_identity, b"external-safe"
                )
                self.assertEqual(
                    external_shape,
                    sorted(
                        (
                            item.relative_to(inserted).as_posix(),
                            item.is_dir(),
                            None if item.is_dir() else item.read_bytes(),
                        )
                        for item in inserted.rglob("*")
                    ),
                )
            finally:
                if inserted.exists() and not external.exists():
                    os.replace(inserted, external)

    def _prepare_fixture_directories(
        self,
        adapter: manager.RepositoryAdapter,
        actions: list[manager.Action],
    ) -> tuple[list[str], list[dict[str, object]]]:
        records = manager._snapshot_directory_records(adapter, actions)
        rollback_directories: list[str] = []
        for record in records:
            if record["prepared"]:
                continue
            identity, created = adapter.mkdir(
                record["path"], expected_absent=True
            )
            record["identity"] = identity
            record["created"] = created
            record["prepared"] = True
            if created:
                rollback_directories.append(record["path"])
        return rollback_directories, records


if __name__ == "__main__":
    unittest.main()
