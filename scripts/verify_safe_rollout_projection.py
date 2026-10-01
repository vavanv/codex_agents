"""Verify a local, non-portable projection of private Codex rollout evidence.

Private raw sources must remain available for every verification. A hash alone
does not authenticate a projection after those sources are removed. This v2
format deliberately does not satisfy the legacy L5 raw-rollout gate.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from contextlib import ExitStack
from contextvars import ContextVar
import hashlib
import json
import ntpath
import os
from pathlib import Path
import stat
import sys
from typing import Any

from reconcile_codex_rollouts import MAX_INPUT_BYTES, PINNED_ROLE_CONFIG_HASHES, reconcile_rollouts


SCHEMA = "codex-safe-rollout-projection/v2"
MAX_JSON_DEPTH = 32
MAX_ROWS = 20_000
MAX_PROJECTION_BYTES = 4096
SOURCE_NAMES = ("capture", "manifest", "parentRollout", "childRollout", "agentConfig")
_ACTIVE_HANDLES: ContextVar[ExitStack | None] = ContextVar("projection_handles", default=None)

_READ = 0x80000000
_READ_ATTRS = 0x80
_SHARE_READ = 1
_OPEN_EXISTING = 3
_BACKUP = 0x02000000
_NO_FOLLOW = 0x00200000
_REPARSE = 0x00000400
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class ProjectionError(ValueError):
    """A deliberately content-free failure for sensitive private inputs."""


class _TagInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


class _FileId(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_ulonglong), ("id", ctypes.c_ubyte * 16)]


def _win_api() -> Any:
    if os.name != "nt":
        raise ProjectionError("unsupported source platform")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, wintypes.DWORD]
    api.GetFileInformationByHandleEx.restype = wintypes.BOOL
    api.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                              wintypes.DWORD, wintypes.DWORD]
    api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    api.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    api.GetFileSizeEx.restype = wintypes.BOOL
    api.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    api.ReadFile.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _win_open(api: Any, path: str, stack: ExitStack, *, directory: bool) -> int:
    access = _READ_ATTRS if directory else _READ | _READ_ATTRS
    flags = _NO_FOLLOW | (_BACKUP if directory else 0)
    handle = api.CreateFileW(path, access, _SHARE_READ, None, _OPEN_EXISTING, flags, None)
    if handle in (None, 0, _INVALID_HANDLE):
        raise ProjectionError("source unavailable")
    stack.callback(api.CloseHandle, handle)
    tag = _TagInfo()
    if not api.GetFileInformationByHandleEx(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
        raise ProjectionError("unsupported source")
    if tag.attributes & _REPARSE:
        raise ProjectionError("invalid source path")
    is_dir = bool(tag.attributes & 0x10)
    if is_dir != directory:
        raise ProjectionError("invalid source path")
    return handle


def _win_identity(api: Any, handle: int) -> tuple[int, bytes]:
    info = _FileId()
    if not api.GetFileInformationByHandleEx(handle, 18, ctypes.byref(info), ctypes.sizeof(info)):
        raise ProjectionError("unsupported source")
    return info.volume, bytes(info.id)


def _win_final(api: Any, handle: int) -> str:
    buffer = ctypes.create_unicode_buffer(32768)
    count = api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if count == 0 or count >= len(buffer):
        raise ProjectionError("unsupported source")
    value = buffer.value
    if not value.startswith("\\\\?\\"):
        raise ProjectionError("unsupported source")
    return ntpath.normcase(ntpath.normpath(value[4:]))


def _win_read(root: Path, relative: str, limit: int) -> bytes:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ProjectionError("invalid source path")
    if ntpath.isabs(relative) or ntpath.splitdrive(relative)[0]:
        raise ProjectionError("invalid source path")
    parts = relative.replace("/", "\\").split("\\")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ProjectionError("invalid source path")
    root_path = str(root.absolute())
    drive, tail = ntpath.splitdrive(root_path)
    if len(drive) != 2 or not drive[0].isalpha() or not tail.startswith("\\"):
        raise ProjectionError("unsupported source root")
    active = _ACTIVE_HANDLES.get()
    if active is None:
        with ExitStack() as local:
            return _win_read_open(root_path, parts, limit, local)
    return _win_read_open(root_path, parts, limit, active)


def _win_read_open(root_path: str, parts: list[str], limit: int, stack: ExitStack) -> bytes:
    api = _win_api()
    drive, tail = ntpath.splitdrive(root_path)
    root_parts = tail.strip("\\").split("\\") if tail.strip("\\") else []
    if not root_parts:
        raise ProjectionError("invalid source root")
    if any(part in ("", ".", "..") or ":" in part for part in root_parts):
        raise ProjectionError("invalid source root")
    current = drive + "\\"
    _win_open(api, current, stack, directory=True)
    for part in root_parts:
        current = ntpath.join(current, part)
        handle = _win_open(api, current, stack, directory=True)
        if _win_final(api, handle) != ntpath.normcase(ntpath.normpath(current)):
            raise ProjectionError("invalid source path")
    root_final = ntpath.normcase(ntpath.normpath(current))
    for part in parts[:-1]:
        current = ntpath.join(current, part)
        handle = _win_open(api, current, stack, directory=True)
        if _win_final(api, handle) != ntpath.normcase(ntpath.normpath(current)):
            raise ProjectionError("invalid source path")
    target = ntpath.join(current, parts[-1])
    handle = _win_open(api, target, stack, directory=False)
    final = _win_final(api, handle)
    if not final.startswith(root_final + "\\") or final != ntpath.normcase(ntpath.normpath(target)):
        raise ProjectionError("invalid source path")
    return _win_read_handle(api, handle, limit)


def _win_read_handle(api: Any, handle: int, limit: int) -> bytes:
    identity = _win_identity(api, handle)
    size = ctypes.c_longlong()
    if not api.GetFileSizeEx(handle, ctypes.byref(size)) or size.value < 0 or size.value > limit:
        raise ProjectionError("invalid source")
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        buffer = ctypes.create_string_buffer(min(65536, limit + 1 - total))
        count = wintypes.DWORD()
        if not api.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
            raise ProjectionError("source unavailable")
        if count.value == 0:
            break
        chunks.append(buffer.raw[:count.value])
        total += count.value
    after = ctypes.c_longlong()
    if not api.GetFileSizeEx(handle, ctypes.byref(after)):
        raise ProjectionError("source unavailable")
    if total > limit or total != size.value or after.value != size.value or _win_identity(api, handle) != identity:
        raise ProjectionError("source changed")
    return b"".join(chunks)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectionError("invalid JSON")
        result[key] = value
    return result


def _check_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ProjectionError("invalid JSON")
    if isinstance(value, dict):
        for item in value.values():
            _check_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_depth(item, depth + 1)


def _parse_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
        _check_depth(value)
    except (UnicodeError, ValueError, RecursionError):
        raise ProjectionError("invalid JSON") from None
    if not isinstance(value, dict):
        raise ProjectionError("invalid JSON")
    return value


def _parse_rows(raw: bytes) -> list[dict[str, Any]]:
    if not raw or len(raw) > MAX_INPUT_BYTES or not raw.endswith(b"\n"):
        raise ProjectionError("invalid rollout")
    lines = raw.splitlines()
    if len(lines) > MAX_ROWS or any(not line for line in lines):
        raise ProjectionError("invalid rollout")
    rows = [_parse_object(line) for line in lines]
    if any(not isinstance(row.get("payload"), dict) for row in rows):
        raise ProjectionError("invalid rollout")
    return rows


def _parse_capture(raw: bytes) -> None:
    if not raw or len(raw) > MAX_INPUT_BYTES or not raw.endswith(b"\n"):
        raise ProjectionError("invalid capture")
    lines = raw.splitlines()
    if len(lines) > MAX_ROWS or any(not line for line in lines):
        raise ProjectionError("invalid capture")
    for line in lines:
        _parse_object(line)


def _payloads(rows: list[dict[str, Any]], outer: str, inner: str | None = None) -> list[dict[str, Any]]:
    return [
        row["payload"] for row in rows
        if row.get("type") == outer
        and (inner is None or row["payload"].get("type") == inner)
    ]


def _strict_shape(parent: list[dict[str, Any]], child: list[dict[str, Any]]) -> None:
    """Reject ambiguous duplicates the v1 correlation deliberately tolerates."""
    allowed_events = (
        (parent, {"task_complete"}),
        (child, {"thread_settings_applied", "task_complete"}),
    )
    for rows, allowed in allowed_events:
        if any(item.get("type") not in allowed for item in _payloads(rows, "event_msg")):
            raise ProjectionError("ambiguous rollout")
    allowed_items = (
        (parent, {"message", "function_call", "function_call_output"}),
        (child, {"message"}),
    )
    for rows, allowed in allowed_items:
        if any(item.get("type") not in allowed for item in _payloads(rows, "response_item")):
            raise ProjectionError("ambiguous rollout")
    if len(_payloads(parent, "session_meta")) != 1 or len(_payloads(child, "session_meta")) != 2:
        raise ProjectionError("ambiguous rollout")
    if len(_payloads(parent, "response_item", "function_call")) != 1:
        raise ProjectionError("ambiguous rollout")
    if len(_payloads(parent, "response_item", "function_call_output")) != 1:
        raise ProjectionError("ambiguous rollout")
    call = _payloads(parent, "response_item", "function_call")[0]
    output = _payloads(parent, "response_item", "function_call_output")[0]
    for embedded in (call.get("arguments"), output.get("output")):
        if not isinstance(embedded, str) or len(embedded.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ProjectionError("ambiguous rollout")
        _parse_object(embedded.encode("utf-8"))
    if _payloads(child, "response_item", "function_call") or _payloads(child, "response_item", "function_call_output"):
        raise ProjectionError("ambiguous rollout")
    if len(_payloads(child, "event_msg", "thread_settings_applied")) != 1:
        raise ProjectionError("ambiguous rollout")
    if len(_payloads(child, "turn_context")) != 1:
        raise ProjectionError("ambiguous rollout")
    if not _payloads(parent, "turn_context"):
        raise ProjectionError("ambiguous rollout")
    if len(_payloads(parent, "event_msg", "task_complete")) != 1:
        raise ProjectionError("ambiguous rollout")
    if len(_payloads(child, "event_msg", "task_complete")) != 1:
        raise ProjectionError("ambiguous rollout")
    finals = [
        item for item in _payloads(child, "response_item", "message")
        if item.get("role") == "assistant" and item.get("phase") == "final_answer"
    ]
    if len(finals) != 1:
        raise ProjectionError("ambiguous rollout")
    for rows in (parent, child):
        if any(row.get("type") not in {"session_meta", "turn_context", "response_item", "event_msg"} for row in rows):
            raise ProjectionError("ambiguous rollout")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def project_bytes(sources: dict[str, bytes], *, role: str) -> bytes:
    """Derive only allowlisted fields; never include raw text or source paths."""
    if role not in PINNED_ROLE_CONFIG_HASHES or set(sources) != set(SOURCE_NAMES):
        raise ProjectionError("invalid sources")
    if any(not isinstance(raw, bytes) or len(raw) > MAX_INPUT_BYTES for raw in sources.values()):
        raise ProjectionError("invalid sources")
    manifest = _parse_object(sources["manifest"])
    _parse_capture(sources["capture"])
    parent = _parse_rows(sources["parentRollout"])
    child = _parse_rows(sources["childRollout"])
    _strict_shape(parent, child)
    result = reconcile_rollouts(
        sources["capture"], manifest, sources["parentRollout"],
        sources["childRollout"], sources["agentConfig"], role=role,
    )
    if result.get("status") != "CORRELATED" or result.get("reasonCodes") != []:
        raise ProjectionError("correlation failed")
    expected = {
        "schema", "status", "reasonCodes", "source", "captureHash",
        "parentRolloutHash", "childRolloutHash", "agentConfigHash",
        "parentSessionId", "childSessionId", "role", "model",
        "reasoningEffort", "configuredSandboxMode", "sandboxMode",
        "developerInstructionsMatched", "childMarkerObserved",
        "publicStreamAttribution", "fixtureReviewed",
    }
    if set(result) != expected or result["source"] != "persistent-rollouts":
        raise ProjectionError("correlation schema changed")
    if (result["developerInstructionsMatched"] is not True
            or result["childMarkerObserved"] is not True
            or result["publicStreamAttribution"] != "MISSING_ATTRIBUTION"
            or result["sandboxMode"] != "read-only"
            or result["fixtureReviewed"] is not False):
        raise ProjectionError("correlation schema changed")
    projection = {
        "schema": SCHEMA,
        "status": "CORRELATED_PRIVATE_SOURCES",
        "sourceHashes": {name: _sha256(sources[name]) for name in SOURCE_NAMES},
        "parentSessionId": result["parentSessionId"],
        "childSessionId": result["childSessionId"],
        "role": result["role"],
        "model": result["model"],
        "reasoningEffort": result["reasoningEffort"],
        "configuredSandboxMode": result["configuredSandboxMode"],
        "effectiveSandboxMode": result["sandboxMode"],
        "developerInstructionsMatched": True,
        "childMarkerObserved": True,
        "publicStreamAttribution": "MISSING_ATTRIBUTION",
        "l5Accepted": False,
        "runtimeValidated": False,
    }
    return canonical_bytes(projection)


def _safe_source(root: Path, relative: str, *, limit: int) -> bytes:
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ProjectionError("invalid source path")
    if os.name == "nt":
        return _win_read(root, relative, limit)
    target = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            raise ProjectionError("source unavailable") from None
        if stat.S_ISLNK(mode):
            raise ProjectionError("invalid source path")
    try:
        if not target.resolve().is_relative_to(root.resolve()):
            raise ProjectionError("invalid source path")
        before = target.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ProjectionError("invalid source")
        raw = target.read_bytes()
        after = target.stat()
    except OSError:
        raise ProjectionError("source unavailable") from None
    if len(raw) > limit or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise ProjectionError("source changed")
    return raw


def _read_sources(root: Path, paths: dict[str, str]) -> dict[str, bytes]:
    if set(paths) != set(SOURCE_NAMES) or len(set(paths.values())) != len(SOURCE_NAMES):
        raise ProjectionError("invalid source paths")
    sources = {name: _safe_source(root, paths[name], limit=MAX_INPUT_BYTES) for name in SOURCE_NAMES}
    for name in SOURCE_NAMES:
        if _safe_source(root, paths[name], limit=MAX_INPUT_BYTES) != sources[name]:
            raise ProjectionError("source changed")
    return sources


def _verify_core(root: Path, paths: dict[str, str], retained: str, *, role: str) -> bool:
    root = root.absolute() if os.name == "nt" else root.resolve(strict=True)
    if not root.is_dir():
        raise ProjectionError("invalid source root")
    if retained in paths.values():
        raise ProjectionError("invalid projection path")
    retained_bytes = _safe_source(root, retained, limit=MAX_PROJECTION_BYTES)
    if retained_bytes != canonical_bytes(_parse_object(retained_bytes)):
        raise ProjectionError("invalid projection")
    first = _read_sources(root, paths)
    expected = project_bytes(first, role=role)
    if retained_bytes != expected:
        raise ProjectionError("projection mismatch")
    if _read_sources(root, paths) != first:
        raise ProjectionError("source changed")
    if _safe_source(root, retained, limit=MAX_PROJECTION_BYTES) != retained_bytes:
        raise ProjectionError("projection changed")
    return True


def verify_projection(root: Path, paths: dict[str, str], retained: str, *, role: str) -> bool:
    """Require live private sources; keep Windows read handles pinned through the check."""
    with ExitStack() as handles:
        token = _ACTIVE_HANDLES.set(handles)
        try:
            return _verify_core(root, paths, retained, role=role)
        finally:
            _ACTIVE_HANDLES.reset(token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--parent-rollout", required=True)
    parser.add_argument("--child-rollout", required=True)
    parser.add_argument("--agent-config", required=True)
    parser.add_argument("--projection", required=True)
    parser.add_argument("--role", choices=tuple(PINNED_ROLE_CONFIG_HASHES), required=True)
    args = parser.parse_args(argv)
    paths = {
        "capture": args.capture, "manifest": args.manifest,
        "parentRollout": args.parent_rollout, "childRollout": args.child_rollout,
        "agentConfig": args.agent_config,
    }
    try:
        verify_projection(args.private_root, paths, args.projection, role=args.role)
    except (ProjectionError, OSError, ValueError):
        print("PROJECTION_UNVERIFIED")
        return 2
    print("PROJECTION_VERIFIED_PRIVATE_SOURCES")
    return 0


if __name__ == "__main__":
    sys.exit(main())
