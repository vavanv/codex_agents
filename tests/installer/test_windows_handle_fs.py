from __future__ import annotations

import copy
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import windows_handle_fs as handle_fs


class PathNormalizationTests(unittest.TestCase):
    def test_accepts_normal_repository_relative_components(self) -> None:
        self.assertEqual(("a", "b", "file.txt"), handle_fs._normalize_relative("a/b/file.txt"))
        self.assertEqual(("a", "b"), handle_fs._normalize_relative(r"a\b"))

    def test_rejects_escape_and_ambiguous_forms(self) -> None:
        invalid = (
            "",
            ".",
            "..",
            "a/../b",
            "a/./b",
            "/absolute",
            r"\absolute",
            r"C:\absolute",
            r"a\\b",
            "a//b",
            r"a\/b",
            "a/\\b",
            "a/",
            "a\\",
            "name:stream",
            "nul\x00name",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    handle_fs._normalize_relative(value)

    def test_absolute_target_parser_rejects_unsupported_forms(self) -> None:
        invalid = (
            r"relative\repo",
            r"\\server\share\repo",
            r"\\?\C:\repo",
            r"\\.\C:\repo",
            "C:\\",
            "C:\\repo\\\\",
            r"C:\repo\..\escape",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(handle_fs.UnsupportedTargetError):
                    handle_fs._parse_absolute_target(value)


class AbiAndHandleTests(unittest.TestCase):
    def test_declared_structure_layout_matches_runtime_layout(self) -> None:
        with mock.patch.object(handle_fs, "IS_WINDOWS", False):
            with self.assertRaisesRegex(handle_fs.UnsupportedTargetError, "Windows-only"):
                handle_fs.validate_abi()
        if not handle_fs.IS_WINDOWS:
            return
        handle_fs.validate_abi()
        self.assertEqual(16, handle_fs.ctypes.sizeof(handle_fs.UNICODE_STRING))
        self.assertEqual(48, handle_fs.ctypes.sizeof(handle_fs.OBJECT_ATTRIBUTES))
        self.assertEqual(16, handle_fs.ctypes.sizeof(handle_fs.IO_STATUS_BLOCK))
        self.assertEqual(24, handle_fs.ctypes.sizeof(handle_fs.FILE_ID_INFO))
        self.assertEqual(20, handle_fs.FILE_RENAME_INFORMATION_EX.FileName.offset)

    def test_safe_handle_is_noncopyable_and_closes_exactly_once(self) -> None:
        closed: list[int] = []
        handle = handle_fs.SafeHandle(123, lambda value: closed.append(value) or True)
        with self.assertRaises(TypeError):
            copy.copy(handle)
        with self.assertRaises(TypeError):
            copy.deepcopy(handle)
        handle.close()
        handle.close()
        self.assertTrue(handle.closed)
        self.assertEqual([123], closed)
        with self.assertRaises(ValueError):
            _ = handle.value

    def test_safe_handle_context_closes_after_error(self) -> None:
        closed: list[int] = []
        with self.assertRaisesRegex(RuntimeError, "sentinel"):
            with handle_fs.SafeHandle(456, lambda value: closed.append(value) or True):
                raise RuntimeError("sentinel")
        self.assertEqual([456], closed)

    @unittest.skipUnless(handle_fs.IS_WINDOWS, "native NTSTATUS mapping test")
    def test_ntstatus_is_mapped_to_a_windows_error(self) -> None:
        api = handle_fs._NativeApi()
        missing_status = handle_fs.ctypes.c_long(handle_fs.STATUS_OBJECT_NAME_NOT_FOUND).value
        with self.assertRaises(OSError) as raised:
            api.raise_status(missing_status, "test open")
        self.assertEqual(2, raised.exception.winerror)
        self.assertIn("NTSTATUS=0xC0000034", " ".join(raised.exception.__notes__))

    @unittest.skipUnless(handle_fs.IS_WINDOWS, "native API availability test")
    def test_initialization_fails_closed_when_required_api_is_missing(self) -> None:
        with mock.patch.object(handle_fs.ctypes, "WinDLL", side_effect=OSError("missing")):
            with self.assertRaisesRegex(handle_fs.UnsupportedTargetError, "APIs are unavailable"):
                handle_fs._NativeApi()


class ClassifierTests(unittest.TestCase):
    def test_accepts_only_local_fixed_ntfs_harddisk_volume(self) -> None:
        handle_fs._validate_volume_classification(
            handle_fs.DRIVE_FIXED,
            r"\Device\HarddiskVolume42",
            "NTFS",
        )

    def test_rejects_unsupported_volume_classifications(self) -> None:
        rejected = (
            (4, r"\Device\HarddiskVolume42", "NTFS"),
            (handle_fs.DRIVE_FIXED, r"\??\C:\substituted", "NTFS"),
            (handle_fs.DRIVE_FIXED, r"\Device\Mup\server\share", "NTFS"),
            (handle_fs.DRIVE_FIXED, r"\Device\LanmanRedirector\server\share", "NTFS"),
            (handle_fs.DRIVE_FIXED, r"\Device\UnexpectedVolume42", "NTFS"),
            (handle_fs.DRIVE_FIXED, r"\Device\HarddiskVolume42", "ReFS"),
        )
        for drive_type, mapping, filesystem in rejected:
            with self.subTest(mapping=mapping, filesystem=filesystem):
                with self.assertRaises(handle_fs.UnsupportedTargetError):
                    handle_fs._validate_volume_classification(
                        drive_type,
                        mapping,
                        filesystem,
                    )


class ControlledIoTests(unittest.TestCase):
    def create_fs(self, api: object) -> handle_fs.RepositoryFs:
        filesystem = object.__new__(handle_fs.RepositoryFs)
        filesystem._api = api
        filesystem._root = None
        return filesystem

    def test_partial_writes_advance_by_reported_count(self) -> None:
        written = bytearray()

        def write_file(_handle, buffer, length, count, _overlapped) -> bool:
            consumed = min(2, length)
            written.extend(handle_fs.ctypes.string_at(buffer, consumed))
            count._obj.value = consumed
            return True

        filesystem = self.create_fs(SimpleNamespace(WriteFile=write_file))
        closed: list[int] = []
        with handle_fs.SafeHandle(101, lambda value: closed.append(value) or True) as opened:
            filesystem._write_all(opened, b"partial-write")
        self.assertEqual(b"partial-write", bytes(written))
        self.assertEqual([101], closed)

    def test_zero_progress_write_fails_and_handle_is_closed(self) -> None:
        def write_file(_handle, _buffer, _length, count, _overlapped) -> bool:
            count._obj.value = 0
            return True

        filesystem = self.create_fs(SimpleNamespace(WriteFile=write_file))
        closed: list[int] = []
        with self.assertRaisesRegex(handle_fs.RepositoryFsError, "no progress"):
            with handle_fs.SafeHandle(102, lambda value: closed.append(value) or True) as opened:
                filesystem._write_all(opened, b"content")
        self.assertEqual([102], closed)

    def test_multiple_reads_are_joined_and_handle_is_closed(self) -> None:
        chunks = iter((b"multi-", b"read", b""))

        def read_file(_handle, buffer, _length, count, _overlapped) -> bool:
            chunk = next(chunks)
            handle_fs.ctypes.memmove(buffer, chunk, len(chunk))
            count._obj.value = len(chunk)
            return True

        filesystem = self.create_fs(SimpleNamespace(ReadFile=read_file))
        closed: list[int] = []
        with handle_fs.SafeHandle(103, lambda value: closed.append(value) or True) as opened:
            content = filesystem._read_all(opened)
        self.assertEqual(b"multi-read", content)
        self.assertEqual([103], closed)

    @unittest.skipUnless(handle_fs.IS_WINDOWS, "native read-error mapping test")
    def test_read_error_is_propagated_and_handle_is_closed(self) -> None:
        def read_file(_handle, _buffer, _length, _count, _overlapped) -> bool:
            handle_fs.ctypes.set_last_error(5)
            return False

        filesystem = self.create_fs(SimpleNamespace(ReadFile=read_file))
        closed: list[int] = []
        with self.assertRaises(OSError) as raised:
            with handle_fs.SafeHandle(104, lambda value: closed.append(value) or True) as opened:
                filesystem._read_all(opened)
        self.assertEqual(5, raised.exception.winerror)
        self.assertEqual([104], closed)


@unittest.skipUnless(handle_fs.IS_WINDOWS, "native Windows NTFS backend tests")
class NativeRepositoryFsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run_root = Path(self.temporary.name)
        self.repository = self.run_root / "repository"
        self.repository.mkdir()
        try:
            self.fs = handle_fs.RepositoryFs(str(self.repository))
        except handle_fs.UnsupportedTargetError as exc:
            self.skipTest(str(exc))
        self.addCleanup(self.fs.close)

    def create_junction(self, link: Path, destination: Path) -> None:
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(destination)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.skipTest(f"Windows junction creation unavailable: {result.stderr.strip()}")
        self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)

    def test_root_identity_and_display_path_are_stable(self) -> None:
        with handle_fs.RepositoryFs(str(self.repository)) as second:
            self.assertEqual(self.fs.identity, second.identity)
            self.assertEqual(str(self.repository), self.fs.display_path)
            self.assertRegex(self.fs.identity.file_id, r"^[0-9a-f]{32}$")
            self.assertGreater(self.fs.identity.volume_serial_number, 0)

    def test_read_hash_open_and_exists_are_handle_relative(self) -> None:
        content = b"native-read\x00content"
        (self.repository / "source.bin").write_bytes(content)
        self.assertTrue(self.fs.exists("source.bin"))
        self.assertFalse(self.fs.exists("missing.bin"))
        self.assertFalse(self.fs.exists("missing-parent/missing.bin"))
        self.assertEqual(content, self.fs.read_bytes("source.bin"))
        self.assertEqual(hashlib.sha256(content).hexdigest(), self.fs.hash_file("source.bin"))
        with self.fs.open("source.bin", directory=False) as opened:
            self.assertFalse(opened.closed)
        self.assertTrue(opened.closed)

    def test_ensure_directories_and_rmdir(self) -> None:
        self.fs.ensure_directories("one/two/three")
        self.assertTrue((self.repository / "one" / "two" / "three").is_dir())
        self.fs.ensure_directories("one/two/three")
        self.fs.rmdir("one/two/three")
        self.fs.rmdir("one/two")
        self.fs.rmdir("one")
        self.assertFalse((self.repository / "one").exists())

    def test_close_releases_cached_children_before_parents_and_acquisition_chain(self) -> None:
        self.fs.ensure_directories("one/two")
        capabilities = sorted(
            self.fs._directories.values(),
            key=lambda item: len(item.relative.split("/")) if item.relative else 0,
        )
        acquisition = list(self.fs._acquisition_handles)
        expected = [item.handle.value for item in reversed(capabilities)] + [
            item.value for item in reversed(acquisition)
        ]
        closed: list[int] = []
        real_close = self.fs._api.close

        def recording_close(value: int) -> bool:
            closed.append(value)
            return real_close(value)

        for capability in capabilities:
            capability.handle._closer = recording_close
        for handle in acquisition:
            handle._closer = recording_close

        self.fs.close()

        self.assertEqual(expected, closed)

    def test_missing_ok_remove_accepts_a_missing_ancestor(self) -> None:
        self.fs.unlink("missing/child.txt", missing_ok=True)
        self.fs.rmdir("missing/child", missing_ok=True)

    def test_atomic_create_replace_flush_and_unlink(self) -> None:
        self.fs.atomic_write("managed.bin", b"first")
        self.assertEqual(b"first", (self.repository / "managed.bin").read_bytes())
        self.fs.atomic_write("managed.bin", b"second")
        self.assertEqual(b"second", self.fs.read_bytes("managed.bin"))
        self.assertEqual([], list(self.repository.glob(".codex-*.tmp")))
        self.fs.unlink("managed.bin")
        self.assertFalse((self.repository / "managed.bin").exists())
        self.fs.unlink("managed.bin", missing_ok=True)
        with self.assertRaises(OSError):
            self.fs.unlink("managed.bin")

    def test_atomic_write_error_disposes_same_parent_temporary(self) -> None:
        with mock.patch.object(self.fs._api, "close", wraps=self.fs._api.close) as close_handle:
            with mock.patch.object(self.fs, "_rename", side_effect=RuntimeError("injected rename")):
                with self.assertRaisesRegex(RuntimeError, "injected rename"):
                    self.fs.atomic_write("failure.bin", b"content")
            self.assertEqual(2, close_handle.call_count)
        self.assertFalse((self.repository / "failure.bin").exists())
        self.assertEqual([], list(self.repository.glob(".codex-*.tmp")))

    def test_disposition_cleanup_failure_is_attached_to_primary_error(self) -> None:
        disposition_error = OSError(5, "must-not-be-copied-to-evidence")
        with mock.patch.object(self.fs._api, "close", wraps=self.fs._api.close) as close_handle:
            with mock.patch.object(self.fs, "_rename", side_effect=RuntimeError("primary rename")):
                with mock.patch.object(self.fs, "_dispose", side_effect=disposition_error):
                    with self.assertRaisesRegex(RuntimeError, "primary rename") as raised:
                        self.fs.atomic_write("failure.bin", b"sensitive-content")
            self.assertEqual(2, close_handle.call_count)
        notes = " ".join(raised.exception.__notes__)
        self.assertRegex(notes, r"disposition.*'\.codex-[0-9a-f]{32}\.tmp'.*errno=5")
        self.assertNotIn("must-not-be-copied", notes)
        self.assertNotIn(str(self.repository), notes)
        self.assertNotIn("sensitive-content", notes)
        temporary_files = list(self.repository.glob(".codex-*.tmp"))
        self.assertEqual(1, len(temporary_files))
        self.fs.unlink(temporary_files[0].name)

    def test_close_cleanup_failure_is_attached_and_parent_is_still_closed(self) -> None:
        real_close = self.fs._api.close
        close_calls: list[int] = []

        def report_first_close_failure(value: int) -> bool:
            close_calls.append(value)
            actually_closed = real_close(value)
            if len(close_calls) == 1:
                handle_fs.ctypes.set_last_error(6)
                return False
            return actually_closed

        with mock.patch.object(self.fs._api, "close", side_effect=report_first_close_failure):
            with mock.patch.object(self.fs, "_rename", side_effect=RuntimeError("primary rename")):
                with self.assertRaisesRegex(RuntimeError, "primary rename") as raised:
                    self.fs.atomic_write("failure.bin", b"content")
        self.assertEqual(2, len(close_calls))
        notes = " ".join(raised.exception.__notes__)
        self.assertRegex(notes, r"handle close.*'\.codex-[0-9a-f]{32}\.tmp'.*winerror=6")
        self.assertNotIn(str(self.repository), notes)
        self.assertEqual([], list(self.repository.glob(".codex-*.tmp")))

    def test_flush_failure_propagates_and_disposes_temporary(self) -> None:
        def fail_flush(_handle: int) -> bool:
            handle_fs.ctypes.set_last_error(5)
            return False

        with mock.patch.object(self.fs._api, "FlushFileBuffers", side_effect=fail_flush):
            with self.assertRaises(OSError) as raised:
                self.fs.atomic_write("flush-failure.bin", b"content")
        self.assertEqual(5, raised.exception.winerror)
        self.assertFalse((self.repository / "flush-failure.bin").exists())
        self.assertEqual([], list(self.repository.glob(".codex-*.tmp")))

    def test_existing_reparse_component_is_rejected_and_sentinel_unchanged(self) -> None:
        external = self.run_root / "external"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_bytes(b"external-safe")
        external_child = external / "child"
        external_child.mkdir()
        junction = self.repository / "linked"
        self.create_junction(junction, external)

        with self.assertRaises(handle_fs.ReparsePointError):
            self.fs.read_bytes("linked/sentinel.txt")
        with self.assertRaises(handle_fs.ReparsePointError):
            self.fs.atomic_write("linked/created.txt", b"unsafe")
        with self.assertRaises(handle_fs.ReparsePointError):
            self.fs.ensure_directories("linked/created")
        with self.assertRaises(handle_fs.ReparsePointError):
            self.fs.unlink("linked/sentinel.txt")
        with self.assertRaises(handle_fs.ReparsePointError):
            self.fs.rmdir("linked/child")
        with self.assertRaises(handle_fs.ReparsePointError):
            self.fs.rmdir("linked")
        with self.assertRaises(handle_fs.ReparsePointError):
            self.fs.atomic_write("linked", b"unsafe replacement")

        self.assertEqual(b"external-safe", sentinel.read_bytes())
        self.assertFalse((external / "created.txt").exists())
        self.assertTrue(external_child.is_dir())

    def test_reparse_target_root_is_rejected(self) -> None:
        external = self.run_root / "root-external"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_bytes(b"external-safe")
        junction = self.run_root / "root-link"
        self.create_junction(junction, external)
        with self.assertRaises(handle_fs.ReparsePointError):
            handle_fs.RepositoryFs(str(junction))
        self.assertEqual(b"external-safe", sentinel.read_bytes())

    def test_close_is_idempotent_and_operations_fail_after_close(self) -> None:
        self.fs.close()
        self.fs.close()
        with self.assertRaises(handle_fs.RepositoryFsError):
            _ = self.fs.identity
        with self.assertRaises(handle_fs.RepositoryFsError):
            self.fs.exists("anything")


if __name__ == "__main__":
    unittest.main()
