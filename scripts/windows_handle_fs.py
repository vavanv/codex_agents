"""Production Windows NTFS backend using handle-relative operations.

``workflow_manager.py`` selects this backend for Windows CLI repository
lifecycles. Broader adversarial lifecycle validation remains a separate gate.
"""

from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
import re
import secrets
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable


IS_WINDOWS = os.name == "nt"

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
DRIVE_FIXED = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

DELETE = 0x00010000
FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_READ_ATTRIBUTES = 0x00000080
FILE_WRITE_ATTRIBUTES = 0x00000100
SYNCHRONIZE = 0x00100000

OBJ_CASE_INSENSITIVE = 0x00000040
FILE_DIRECTORY_FILE = 0x00000001
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_OPEN_REPARSE_POINT = 0x00200000
FILE_OPEN = 1
FILE_CREATE = 2
FILE_OPEN_IF = 3

FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
FILE_ID_INFO_CLASS = 18
FILE_DISPOSITION_INFORMATION_EX_CLASS = 64
FILE_RENAME_INFORMATION_EX_CLASS = 65
FILE_DISPOSITION_DELETE = 0x00000001
FILE_DISPOSITION_POSIX_SEMANTICS = 0x00000002
FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE = 0x00000010
FILE_RENAME_REPLACE_IF_EXISTS = 0x00000001
FILE_RENAME_POSIX_SEMANTICS = 0x00000002
DUPLICATE_SAME_ACCESS = 0x00000002

STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
STATUS_NO_SUCH_FILE = 0xC000000F
STATUS_OBJECT_NAME_COLLISION = 0xC0000035
MISSING_STATUSES = {
    STATUS_OBJECT_NAME_NOT_FOUND,
    STATUS_OBJECT_PATH_NOT_FOUND,
    STATUS_NO_SUCH_FILE,
}


class RepositoryFsError(RuntimeError):
    """Base error for the handle-relative filesystem."""


class UnsupportedTargetError(RepositoryFsError):
    """The target cannot provide the backend's security guarantees."""


class ReparsePointError(RepositoryFsError):
    """A repository component is a reparse point."""


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class FILE_ID_INFO(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", FILE_ID_128)]


class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


class FILE_DISPOSITION_INFO_EX(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD)]


class FILE_RENAME_INFORMATION_EX(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    ]


ABI_LAYOUT = {
    "UNICODE_STRING": (16, 8),
    "OBJECT_ATTRIBUTES": (48, 8),
    "IO_STATUS_BLOCK": (16, 8),
    "FILE_ID_INFO": (24, 8),
    "FILE_ATTRIBUTE_TAG_INFO": (8, 4),
    "FILE_DISPOSITION_INFO_EX": (4, 4),
    "FILE_RENAME_INFORMATION_EX.filename_offset": (20, 2),
}


def validate_abi() -> None:
    """Fail closed unless CPython's structures match the supported x64 ABI."""
    if not IS_WINDOWS:
        raise UnsupportedTargetError("The handle-relative backend is Windows-only")
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise UnsupportedTargetError("Only 64-bit CPython is supported")
    actual = {
        "UNICODE_STRING": (ctypes.sizeof(UNICODE_STRING), ctypes.alignment(UNICODE_STRING)),
        "OBJECT_ATTRIBUTES": (
            ctypes.sizeof(OBJECT_ATTRIBUTES),
            ctypes.alignment(OBJECT_ATTRIBUTES),
        ),
        "IO_STATUS_BLOCK": (
            ctypes.sizeof(IO_STATUS_BLOCK),
            ctypes.alignment(IO_STATUS_BLOCK),
        ),
        "FILE_ID_INFO": (ctypes.sizeof(FILE_ID_INFO), ctypes.alignment(FILE_ID_INFO)),
        "FILE_ATTRIBUTE_TAG_INFO": (
            ctypes.sizeof(FILE_ATTRIBUTE_TAG_INFO),
            ctypes.alignment(FILE_ATTRIBUTE_TAG_INFO),
        ),
        "FILE_DISPOSITION_INFO_EX": (
            ctypes.sizeof(FILE_DISPOSITION_INFO_EX),
            ctypes.alignment(FILE_DISPOSITION_INFO_EX),
        ),
        "FILE_RENAME_INFORMATION_EX.filename_offset": (
            FILE_RENAME_INFORMATION_EX.FileName.offset,
            ctypes.sizeof(wintypes.WCHAR),
        ),
    }
    if actual != ABI_LAYOUT:
        raise UnsupportedTargetError(f"Unsupported Windows ctypes ABI layout: {actual!r}")


def _unsigned_status(status: int) -> int:
    return ctypes.c_ulong(status).value


def _normalize_relative(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or not relative:
        raise ValueError("Repository-relative path must be a non-empty string")
    if "\x00" in relative or ntpath.isabs(relative) or ntpath.splitdrive(relative)[0]:
        raise ValueError(f"Path is not repository-relative: {relative!r}")
    if relative.startswith(("\\", "/")) or relative.endswith(("\\", "/")):
        raise ValueError(f"Path contains an empty component: {relative!r}")
    if "\\\\" in relative or "//" in relative or "\\/" in relative or "/\\" in relative:
        raise ValueError(f"Path contains ambiguous separators: {relative!r}")
    parts = tuple(re.split(r"[\\/]", relative))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Path contains an unsafe component: {relative!r}")
    if any(":" in part for part in parts):
        raise ValueError(f"Path contains a drive or stream component: {relative!r}")
    return parts


def _parse_absolute_target(target: str | os.PathLike[str]) -> tuple[str, tuple[str, ...], str]:
    value = os.fspath(target)
    if not isinstance(value, str) or "\x00" in value:
        raise UnsupportedTargetError("Target must be a Windows string path")
    if value.startswith(("\\", "//", "\\?\\", "\\.\\")):
        raise UnsupportedTargetError("UNC and device targets are unsupported")
    match = re.fullmatch(r"([A-Za-z]):\\(.*)", value)
    if match is None:
        raise UnsupportedTargetError("Target must be an absolute drive-letter path")
    drive = match.group(1).upper()
    tail = match.group(2)
    if not tail:
        raise UnsupportedTargetError("A volume root is not a repository target")
    if tail.endswith("\\\\"):
        raise UnsupportedTargetError("Target contains ambiguous trailing separators")
    try:
        parts = _normalize_relative(tail.rstrip("\\"))
    except ValueError as exc:
        raise UnsupportedTargetError(str(exc)) from exc
    display = f"{drive}:\\" + "\\".join(parts)
    return drive, parts, display


def _validate_volume_classification(
    drive_type: int,
    dos_device_mapping: str,
    filesystem_name: str | None,
) -> None:
    """Accept only a local fixed NTFS volume with a physical volume mapping."""
    if drive_type != DRIVE_FIXED:
        raise UnsupportedTargetError("Target volume must be a local fixed drive")
    if re.fullmatch(
        r"\\Device\\HarddiskVolume\d+",
        dos_device_mapping,
        re.IGNORECASE,
    ) is None:
        raise UnsupportedTargetError(
            "Mapped, substituted, and unexpected DOS device targets are unsupported"
        )
    if filesystem_name is not None and filesystem_name.upper() != "NTFS":
        raise UnsupportedTargetError("Target volume must use NTFS")


def _cleanup_failure_note(stage: str, temporary_name: str, error: BaseException) -> str:
    details = [type(error).__name__]
    winerror = getattr(error, "winerror", None)
    errno = getattr(error, "errno", None)
    if isinstance(winerror, int):
        details.append(f"winerror={winerror}")
    elif isinstance(errno, int):
        details.append(f"errno={errno}")
    return (
        f"Atomic-write cleanup {stage} failed for temporary component "
        f"{temporary_name!r}: {'; '.join(details)}"
    )


class SafeHandle:
    """Single-owner native handle; serialized use only, not thread-safe."""

    __slots__ = ("_value", "_closer")

    def __init__(self, value: int, closer: Callable[[int], object]) -> None:
        if value in (None, 0, INVALID_HANDLE_VALUE):
            raise ValueError("Cannot own an invalid Windows handle")
        self._value = int(value)
        self._closer = closer

    @property
    def value(self) -> int:
        if self._value is None:
            raise ValueError("Windows handle is closed")
        return self._value

    @property
    def closed(self) -> bool:
        return self._value is None

    def close(self) -> None:
        value = self._value
        if value is not None:
            self._value = None
            if not self._closer(value):
                raise ctypes.WinError(ctypes.get_last_error())

    def __enter__(self) -> SafeHandle:
        self.value
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __copy__(self) -> SafeHandle:
        raise TypeError("SafeHandle ownership cannot be copied")

    def __deepcopy__(self, _memo: object) -> SafeHandle:
        raise TypeError("SafeHandle ownership cannot be copied")

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


@dataclass(frozen=True)
class RootIdentity:
    volume_serial_number: int
    file_id: str


class _NativeApi:
    def __init__(self) -> None:
        validate_abi()
        if sys.version_info < (3, 11):
            raise UnsupportedTargetError("CPython 3.11 or newer is required")
        if sys_build_number() < 17763:
            raise UnsupportedTargetError("Windows 10 1809 / Server 2019 or newer is required")
        try:
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            self._bind()
        except (AttributeError, OSError) as exc:
            raise UnsupportedTargetError("Required Windows native filesystem APIs are unavailable") from exc

    def _bind(self) -> None:
        self.CloseHandle = self.kernel32.CloseHandle
        self.CloseHandle.argtypes = [wintypes.HANDLE]
        self.CloseHandle.restype = wintypes.BOOL
        self.CreateFileW = self.kernel32.CreateFileW
        self.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.CreateFileW.restype = wintypes.HANDLE
        self.GetDriveTypeW = self.kernel32.GetDriveTypeW
        self.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        self.GetDriveTypeW.restype = wintypes.UINT
        self.GetCurrentProcess = self.kernel32.GetCurrentProcess
        self.GetCurrentProcess.argtypes = []
        self.GetCurrentProcess.restype = wintypes.HANDLE
        self.DuplicateHandle = self.kernel32.DuplicateHandle
        self.DuplicateHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self.DuplicateHandle.restype = wintypes.BOOL
        self.QueryDosDeviceW = self.kernel32.QueryDosDeviceW
        self.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        self.QueryDosDeviceW.restype = wintypes.DWORD
        self.GetVolumeInformationByHandleW = self.kernel32.GetVolumeInformationByHandleW
        self.GetVolumeInformationByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        self.GetVolumeInformationByHandleW.restype = wintypes.BOOL
        self.GetFileInformationByHandleEx = self.kernel32.GetFileInformationByHandleEx
        self.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.ReadFile = self.kernel32.ReadFile
        self.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self.ReadFile.restype = wintypes.BOOL
        self.WriteFile = self.kernel32.WriteFile
        self.WriteFile.argtypes = self.ReadFile.argtypes
        self.WriteFile.restype = wintypes.BOOL
        self.FlushFileBuffers = self.kernel32.FlushFileBuffers
        self.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.FlushFileBuffers.restype = wintypes.BOOL
        self.NtCreateFile = self.ntdll.NtCreateFile
        self.NtCreateFile.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.ULONG,
            ctypes.POINTER(OBJECT_ATTRIBUTES),
            ctypes.POINTER(IO_STATUS_BLOCK),
            ctypes.POINTER(ctypes.c_longlong),
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        ]
        self.NtCreateFile.restype = ctypes.c_long
        self.NtSetInformationFile = self.ntdll.NtSetInformationFile
        self.NtSetInformationFile.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_int,
        ]
        self.NtSetInformationFile.restype = ctypes.c_long
        self.RtlNtStatusToDosError = self.ntdll.RtlNtStatusToDosError
        self.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
        self.RtlNtStatusToDosError.restype = wintypes.ULONG

    def close(self, value: int) -> bool:
        return bool(self.CloseHandle(wintypes.HANDLE(value)))

    def raise_status(self, status: int, operation: str) -> None:
        unsigned = _unsigned_status(status)
        error = int(self.RtlNtStatusToDosError(ctypes.c_long(status)))
        exc = ctypes.WinError(error)
        exc.add_note(f"{operation}; NTSTATUS=0x{unsigned:08X}")
        raise exc


def sys_build_number() -> int:
    if not IS_WINDOWS:
        return 0
    return int(sys.getwindowsversion().build)


class RepositoryFs:
    """Single-owner repository filesystem; serialized use only, not thread-safe."""

    def __init__(self, target: str | os.PathLike[str]) -> None:
        self._api = _NativeApi()
        self._root: SafeHandle | None = None
        self._drive, parts, self._display_path = _parse_absolute_target(target)
        drive_root = f"{self._drive}:\\"
        drive_type = self._api.GetDriveTypeW(drive_root)
        mapping = ctypes.create_unicode_buffer(32768)
        if not self._api.QueryDosDeviceW(f"{self._drive}:", mapping, len(mapping)):
            raise ctypes.WinError(ctypes.get_last_error())
        _validate_volume_classification(drive_type, mapping.value, None)
        raw = self._api.CreateFileW(
            drive_root,
            FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if raw in (None, 0, INVALID_HANDLE_VALUE):
            raise ctypes.WinError(ctypes.get_last_error())
        current = SafeHandle(raw, self._api.close)
        try:
            self._reject_reparse(current)
            filesystem = ctypes.create_unicode_buffer(32)
            serial = wintypes.DWORD()
            if not self._api.GetVolumeInformationByHandleW(
                current.value,
                None,
                0,
                ctypes.byref(serial),
                None,
                None,
                filesystem,
                len(filesystem),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            _validate_volume_classification(drive_type, mapping.value, filesystem.value)
            volume_identity = self._identity_for(current)
            for component in parts:
                child = self._open_component(current, component, directory=True)
                current.close()
                current = child
            self._reject_reparse(current)
            identity = self._identity_for(current)
            if identity.volume_serial_number != volume_identity.volume_serial_number:
                raise UnsupportedTargetError("Repository and bootstrapped volume identities differ")
            self._identity = identity
            self._root = current
            current = None
        finally:
            if current is not None:
                current.close()

    @property
    def identity(self) -> RootIdentity:
        self._require_open()
        return self._identity

    @property
    def display_path(self) -> str:
        return self._display_path

    def _require_open(self) -> SafeHandle:
        if self._root is None:
            raise RepositoryFsError("Repository filesystem is closed")
        return self._root

    def _open_component(
        self,
        parent: SafeHandle,
        name: str,
        *,
        directory: bool | None,
        access: int = FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        disposition: int = FILE_OPEN,
    ) -> SafeHandle:
        encoded = name.encode("utf-16-le")
        if len(encoded) > 0xFFFE:
            raise ValueError("Windows path component is too long")
        buffer = ctypes.create_unicode_buffer(name)
        unicode_name = UNICODE_STRING(len(encoded), len(encoded), ctypes.addressof(buffer))
        attributes = OBJECT_ATTRIBUTES(
            ctypes.sizeof(OBJECT_ATTRIBUTES),
            parent.value,
            ctypes.pointer(unicode_name),
            OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        options = FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_REPARSE_POINT
        if directory is True:
            options |= FILE_DIRECTORY_FILE
        elif directory is False:
            options |= FILE_NON_DIRECTORY_FILE
        result = wintypes.HANDLE()
        io_status = IO_STATUS_BLOCK()
        status = self._api.NtCreateFile(
            ctypes.byref(result),
            access,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            FILE_ATTRIBUTE_NORMAL,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            disposition,
            options,
            None,
            0,
        )
        if status < 0:
            self._api.raise_status(status, f"opening repository component {name!r}")
        handle = SafeHandle(result.value, self._api.close)
        try:
            self._reject_reparse(handle)
        except Exception:
            handle.close()
            raise
        return handle

    def _reject_reparse(self, handle: SafeHandle) -> None:
        info = FILE_ATTRIBUTE_TAG_INFO()
        if not self._api.GetFileInformationByHandleEx(
            handle.value,
            FILE_ATTRIBUTE_TAG_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise ReparsePointError("Repository paths cannot contain reparse points")

    def _identity_for(self, handle: SafeHandle) -> RootIdentity:
        info = FILE_ID_INFO()
        if not self._api.GetFileInformationByHandleEx(
            handle.value,
            FILE_ID_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return RootIdentity(info.VolumeSerialNumber, bytes(info.FileId.Identifier).hex())

    def _walk_parent(self, relative: str) -> tuple[SafeHandle, str]:
        parts = _normalize_relative(relative)
        current = self._duplicate_root()
        try:
            for component in parts[:-1]:
                child = self._open_component(current, component, directory=True)
                current.close()
                current = child
            return current, parts[-1]
        except Exception:
            current.close()
            raise

    def _duplicate_root(self) -> SafeHandle:
        root = self._require_open()
        duplicate = wintypes.HANDLE()
        process = self._api.GetCurrentProcess()
        if not self._api.DuplicateHandle(
            process,
            root.value,
            process,
            ctypes.byref(duplicate),
            0,
            False,
            DUPLICATE_SAME_ACCESS,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return SafeHandle(duplicate.value, self._api.close)

    def open(self, relative: str, *, directory: bool | None = None) -> SafeHandle:
        parent, leaf = self._walk_parent(relative)
        try:
            return self._open_component(parent, leaf, directory=directory)
        finally:
            parent.close()

    def exists(self, relative: str, *, directory: bool | None = None) -> bool:
        try:
            with self.open(relative, directory=directory):
                return True
        except OSError as exc:
            status = getattr(exc, "winerror", None)
            if status in {2, 3}:
                return False
            raise

    def read_bytes(self, relative: str) -> bytes:
        parent, leaf = self._walk_parent(relative)
        try:
            handle = self._open_component(
                parent,
                leaf,
                directory=False,
                access=FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            )
        finally:
            parent.close()
        with handle:
            return self._read_all(handle)

    def _read_all(self, handle: SafeHandle) -> bytes:
        chunks: list[bytes] = []
        while True:
            buffer = ctypes.create_string_buffer(1024 * 1024)
            count = wintypes.DWORD()
            if not self._api.ReadFile(
                handle.value,
                buffer,
                len(buffer),
                ctypes.byref(count),
                None,
            ):
                error = ctypes.get_last_error()
                if error == 38:  # ERROR_HANDLE_EOF
                    break
                raise ctypes.WinError(error)
            if count.value == 0:
                break
            chunks.append(buffer.raw[: count.value])
        return b"".join(chunks)

    def hash_file(self, relative: str) -> str:
        return hashlib.sha256(self.read_bytes(relative)).hexdigest()

    def ensure_directories(self, relative: str) -> None:
        parts = _normalize_relative(relative)
        current = self._duplicate_root()
        try:
            for component in parts:
                child = self._open_component(
                    current,
                    component,
                    directory=True,
                    access=FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES | DELETE | SYNCHRONIZE,
                    disposition=FILE_OPEN_IF,
                )
                current.close()
                current = child
        finally:
            current.close()

    def _write_all(self, handle: SafeHandle, content: bytes) -> None:
        offset = 0
        while offset < len(content):
            chunk = content[offset : offset + 1024 * 1024]
            buffer = ctypes.create_string_buffer(chunk)
            count = wintypes.DWORD()
            if not self._api.WriteFile(
                handle.value,
                buffer,
                len(chunk),
                ctypes.byref(count),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if count.value == 0:
                raise RepositoryFsError("WriteFile made no progress")
            offset += count.value

    def _rename(self, source: SafeHandle, parent: SafeHandle, destination: str) -> None:
        encoded = destination.encode("utf-16-le")
        offset = FILE_RENAME_INFORMATION_EX.FileName.offset
        storage = ctypes.create_string_buffer(offset + len(encoded))
        info = ctypes.cast(storage, ctypes.POINTER(FILE_RENAME_INFORMATION_EX)).contents
        info.Flags = FILE_RENAME_REPLACE_IF_EXISTS | FILE_RENAME_POSIX_SEMANTICS
        info.RootDirectory = parent.value
        info.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(storage) + offset, encoded, len(encoded))
        io_status = IO_STATUS_BLOCK()
        status = self._api.NtSetInformationFile(
            source.value,
            ctypes.byref(io_status),
            storage,
            len(storage),
            FILE_RENAME_INFORMATION_EX_CLASS,
        )
        if status < 0:
            self._api.raise_status(status, f"renaming repository component to {destination!r}")

    def _dispose(self, handle: SafeHandle) -> None:
        info = FILE_DISPOSITION_INFO_EX(
            FILE_DISPOSITION_DELETE
            | FILE_DISPOSITION_POSIX_SEMANTICS
            | FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE
        )
        io_status = IO_STATUS_BLOCK()
        status = self._api.NtSetInformationFile(
            handle.value,
            ctypes.byref(io_status),
            ctypes.byref(info),
            ctypes.sizeof(info),
            FILE_DISPOSITION_INFORMATION_EX_CLASS,
        )
        if status < 0:
            self._api.raise_status(status, "disposing repository component")

    def _assert_replaceable_leaf(self, parent: SafeHandle, leaf: str) -> None:
        try:
            existing = self._open_component(parent, leaf, directory=None)
        except OSError as exc:
            if getattr(exc, "winerror", None) in {2, 3}:
                return
            raise
        existing.close()

    def atomic_write(self, relative: str, content: bytes) -> None:
        if not isinstance(content, bytes):
            raise TypeError("atomic_write content must be bytes")
        parent, leaf = self._walk_parent(relative)
        temporary_name = f".codex-{secrets.token_hex(16)}.tmp"
        temporary: SafeHandle | None = None
        try:
            self._assert_replaceable_leaf(parent, leaf)
            temporary = self._open_component(
                parent,
                temporary_name,
                directory=False,
                access=FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | DELETE | SYNCHRONIZE,
                disposition=FILE_CREATE,
            )
            self._write_all(temporary, content)
            if not self._api.FlushFileBuffers(temporary.value):
                raise ctypes.WinError(ctypes.get_last_error())
            self._rename(temporary, parent, leaf)
        except BaseException as primary:
            cleanup_failures: list[tuple[str, BaseException]] = []
            if temporary is not None:
                try:
                    self._dispose(temporary)
                except BaseException as error:
                    cleanup_failures.append(("disposition", error))
                try:
                    temporary.close()
                except BaseException as error:
                    cleanup_failures.append(("handle close", error))
            try:
                parent.close()
            except BaseException as error:
                cleanup_failures.append(("parent handle close", error))
            for stage, error in cleanup_failures:
                primary.add_note(_cleanup_failure_note(stage, temporary_name, error))
            raise
        close_failures: list[tuple[str, BaseException]] = []
        if temporary is not None:
            try:
                temporary.close()
            except BaseException as error:
                close_failures.append(("handle close", error))
        try:
            parent.close()
        except BaseException as error:
            close_failures.append(("parent handle close", error))
        if close_failures:
            stage, primary = close_failures[0]
            primary.add_note(_cleanup_failure_note(stage, temporary_name, primary))
            for later_stage, error in close_failures[1:]:
                primary.add_note(_cleanup_failure_note(later_stage, temporary_name, error))
            raise primary

    def _remove(self, relative: str, *, directory: bool, missing_ok: bool) -> None:
        try:
            parent, leaf = self._walk_parent(relative)
        except OSError as exc:
            if missing_ok and getattr(exc, "winerror", None) in {2, 3}:
                return
            raise
        try:
            try:
                handle = self._open_component(
                    parent,
                    leaf,
                    directory=directory,
                    access=DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                )
            except OSError as exc:
                if missing_ok and getattr(exc, "winerror", None) in {2, 3}:
                    return
                raise
            with handle:
                self._dispose(handle)
        finally:
            parent.close()

    def unlink(self, relative: str, *, missing_ok: bool = False) -> None:
        self._remove(relative, directory=False, missing_ok=missing_ok)

    def rmdir(self, relative: str, *, missing_ok: bool = False) -> None:
        self._remove(relative, directory=True, missing_ok=missing_ok)

    def close(self) -> None:
        root = self._root
        self._root = None
        if root is not None:
            root.close()

    def __enter__(self) -> RepositoryFs:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = [
    "ABI_LAYOUT",
    "IS_WINDOWS",
    "RepositoryFs",
    "RepositoryFsError",
    "ReparsePointError",
    "RootIdentity",
    "SafeHandle",
    "UnsupportedTargetError",
    "validate_abi",
]
