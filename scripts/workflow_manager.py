#!/usr/bin/env python3
"""Transactional project installer for the Codex Multi-Agent Workflow contracts."""

from __future__ import annotations

import argparse
import errno
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, Protocol

SCHEMA = "hybrid-codex-installer/v2"
LEGACY_STATE_SCHEMA = "hybrid-codex-installer/v1"
JOURNAL_SCHEMA = "hybrid-codex-installer-transaction/v4"
LEGACY_JOURNAL_SCHEMA = "hybrid-codex-installer-transaction/v3"
OLDER_JOURNAL_SCHEMA = "hybrid-codex-installer-transaction/v2"
ROOT_IDENTITY_SCHEMA = "hybrid-codex-repository-root/v1"
PACKAGE_VERSION = "0.2.0-agents-preview"
SUPPORTED_CODEX_VERSIONS = {"0.154.0"}
INSTALL_DIRECTORY = ".codex-workflow"
STATE_FILENAME = ".hybrid-codex-workflow-state.json"
JOURNAL_FILENAME = ".hybrid-codex-workflow-transaction.json"
BACKUP_DIRECTORY = ".hybrid-codex-workflow-backups"
START_MARKER = "<!-- hybrid-codex-workflow:start -->"
END_MARKER = "<!-- hybrid-codex-workflow:end -->"
MANAGED_BLOCK = """<!-- hybrid-codex-workflow:start -->
## Codex multi-agent workflow

- Read `.codex-workflow/CODEX_WORKFLOW.md` and the applicable files under `.codex-workflow/rules/` before routing or implementing work.
- Use `docs/ai/PROJECT_CONTEXT.md` for project-specific architecture, commands, and boundaries.
- If `.codex/agents/*.toml` is present, use the named role definitions for delegation; treat their sandbox values as defaults subject to parent-session controls.
- For non-trivial work, the root orchestrator exclusively owns `plans/ACTIVE_PLAN.md` and `plans/HANDOFF.md`.
- Preserve unrelated changes, require observed validation evidence, and never infer commit or push authorization.
<!-- hybrid-codex-workflow:end -->"""

PACKAGE_FILES = {
    "CODEX_WORKFLOW.md": f"{INSTALL_DIRECTORY}/CODEX_WORKFLOW.md",
    "rules/ROUTING.md": f"{INSTALL_DIRECTORY}/rules/ROUTING.md",
    "rules/VALIDATION.md": f"{INSTALL_DIRECTORY}/rules/VALIDATION.md",
    "rules/ESCALATION.md": f"{INSTALL_DIRECTORY}/rules/ESCALATION.md",
    "rules/GIT_SAFETY.md": f"{INSTALL_DIRECTORY}/rules/GIT_SAFETY.md",
    "templates/PROJECT_CONTEXT.md": f"{INSTALL_DIRECTORY}/templates/PROJECT_CONTEXT.md",
    "templates/ACTIVE_PLAN.md": f"{INSTALL_DIRECTORY}/templates/ACTIVE_PLAN.md",
    "templates/HANDOFF.md": f"{INSTALL_DIRECTORY}/templates/HANDOFF.md",
    "templates/TASK_TEMPLATE.md": f"{INSTALL_DIRECTORY}/templates/TASK_TEMPLATE.md",
}

PROJECT_TEMPLATE_FILES = {
    "templates/PROJECT_CONTEXT.md": "docs/ai/PROJECT_CONTEXT.md",
    "templates/TASK_TEMPLATE.md": "plans/TASK_TEMPLATE.md",
}

CUSTOM_AGENT_FILES = {
    f"agents/{name}.toml": f".codex/agents/{name}.toml"
    for name in (
        "code-explorer",
        "quick-implementer",
        "implementer",
        "luna-escalation",
        "sol-architect",
        "sol-architect-deep",
        "code-validator",
        "code-reviewer",
        "commit-pusher",
    )
}
COMPATIBILITY_FILE = "compatibility/codex-agents.json"


def _managed_action_paths() -> set[str]:
    return {
        *PACKAGE_FILES.values(),
        *PROJECT_TEMPLATE_FILES.values(),
        *CUSTOM_AGENT_FILES.values(),
        "AGENTS.md",
        STATE_FILENAME,
    }


def _is_managed_backup_path(relative: str) -> bool:
    parts = Path(relative).parts
    if len(parts) < 3 or parts[0] != BACKUP_DIRECTORY or not _is_transaction_id(parts[1]):
        return False
    managed_relative = Path(*parts[2:]).as_posix()
    return managed_relative in _managed_action_paths()


class WorkflowError(RuntimeError):
    pass


@dataclass
class Action:
    index: int
    kind: str
    path: str
    content: bytes | None = None
    backup: str | None = None
    created: bool = False
    pre_hash: str | None = None
    post_hash: str | None = None
    backup_hash: str | None = None
    phase: str = "prepared"

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "path": self.path,
            "owner": "workflow-manager",
            "backup": self.backup,
            "created": self.created,
            "preHash": self.pre_hash,
            "postHash": self.post_hash,
            "backupHash": self.backup_hash,
            "phase": self.phase,
        }


class RepositoryAdapter(Protocol):
    """Repository I/O boundary using normalized repository-relative paths."""

    root: Path
    root_identity: dict[str, str]

    def normalize(self, relative: str) -> str: ...

    def display(self, relative: str) -> Path: ...

    def exists(self, relative: str, require_file: bool = False) -> bool: ...

    def read_bytes(self, relative: str) -> bytes: ...

    def hash_file(self, relative: str) -> str | None: ...

    def mkdir(self, relative: str) -> None: ...

    def atomic_write(self, relative: str, content: bytes) -> None: ...

    def unlink(self, relative: str, missing_ok: bool = False) -> None: ...

    def rmdir(self, relative: str) -> None: ...

    def observe_root_identity(self) -> dict[str, str]: ...

    def close(self) -> None: ...


def _normalize_repository_relative(relative: str) -> str:
    """Validate canonical repository-relative syntax without filesystem access."""
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise WorkflowError(f"Managed path is not a safe repository-relative path: {relative!r}")
    if "\\" in relative or relative.startswith("/"):
        raise WorkflowError(f"Managed path is not a safe repository-relative path: {relative}")
    if re.match(r"^[A-Za-z]:", relative) is not None:
        raise WorkflowError(f"Managed path is not a safe repository-relative path: {relative}")

    components = relative.split("/")
    reserved_device = re.compile(
        r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9]|conin\$|conout\$)(?:\..*)?",
        re.IGNORECASE,
    )
    for component in components:
        if component in {"", ".", ".."}:
            raise WorkflowError(
                f"Managed path is not a normalized repository-relative path: {relative}"
            )
        if component.endswith((".", " ")):
            raise WorkflowError(
                f"Managed path has a trailing-dot or trailing-space alias: {relative}"
            )
        if ":" in component:
            raise WorkflowError(f"Managed path contains a Windows stream or drive alias: {relative}")
        if reserved_device.fullmatch(component) is not None:
            raise WorkflowError(f"Managed path uses a reserved Windows device name: {relative}")
    return "/".join(components)


class PathRepositoryAdapter:
    """Current pathname implementation of the repository I/O boundary."""

    def __init__(self, root: Path):
        self.root = root
        self.root_identity = self.observe_root_identity()

    def observe_root_identity(self) -> dict[str, str]:
        value = os.lstat(self.root)
        return {
            "schema": ROOT_IDENTITY_SCHEMA,
            "platform": sys.platform,
            "mode": "path-stat",
            "deviceId": str(value.st_dev),
            "fileId": str(value.st_ino),
        }

    def normalize(self, relative: str) -> str:
        lexical = _normalize_repository_relative(relative)
        path = _target_path(self.root, lexical)
        normalized = _relative(self.root, path)
        if normalized != lexical:
            raise WorkflowError(
                f"Managed path is not a normalized repository-relative path: {relative}"
            )
        _assert_safe_path(self.root, path)
        return normalized

    def display(self, relative: str) -> Path:
        return _target_path(self.root, self.normalize(relative))

    def exists(self, relative: str, require_file: bool = False) -> bool:
        return _safe_exists(self.root, self.display(relative), require_file=require_file)

    def read_bytes(self, relative: str) -> bytes:
        return _safe_read_bytes(self.root, self.display(relative))

    def hash_file(self, relative: str) -> str | None:
        return _current_hash(self.display(relative), self.root)

    def mkdir(self, relative: str) -> None:
        path = self.display(relative)
        path.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(self.root, path)

    def atomic_write(self, relative: str, content: bytes) -> None:
        _atomic_write_managed(self.root, self.display(relative), content)

    def unlink(self, relative: str, missing_ok: bool = False) -> None:
        _safe_unlink(self.root, self.display(relative), missing_ok=missing_ok)

    def rmdir(self, relative: str) -> None:
        _safe_rmdir(self.root, self.display(relative))

    def close(self) -> None:
        """The pathname backend does not own a persistent resource."""


def _load_windows_handle_fs() -> Any:
    module_name = (
        f"{__package__}.windows_handle_fs" if __package__ else "windows_handle_fs"
    )
    return importlib.import_module(module_name)


def _native_error_code(error: BaseException) -> str:
    winerror = getattr(error, "winerror", None)
    errno = getattr(error, "errno", None)
    details = []
    if isinstance(winerror, int):
        details.append(f"winerror={winerror}")
    elif isinstance(errno, int):
        details.append(f"errno={errno}")
    for note in getattr(error, "__notes__", ()):
        match = re.search(r"NTSTATUS=0x[0-9A-Fa-f]{8}", note)
        if match is not None:
            details.append(
                "NTSTATUS=0x" + match.group(0).rsplit("0x", 1)[1].upper()
            )
            break
    return ", ".join(details) if details else type(error).__name__


def _raise_native_workflow_error(
    module: Any,
    error: BaseException,
    operation: str,
    relative: str | None = None,
) -> NoReturn:
    location = f" for {relative}" if relative is not None else ""
    if isinstance(error, module.UnsupportedTargetError):
        raise WorkflowError(f"Unsupported Windows repository target: {error}") from error
    if isinstance(error, module.ReparsePointError):
        raise WorkflowError(
            f"Refusing Windows repository reparse point while {operation}{location}"
        ) from error
    if isinstance(error, module.RepositoryFsError):
        raise WorkflowError(
            f"Windows repository backend failed while {operation}{location}: "
            f"{type(error).__name__}"
        ) from error
    if isinstance(error, OSError):
        raise WorkflowError(
            f"Windows repository filesystem failed while {operation}{location}: "
            f"{_native_error_code(error)}"
        ) from error
    if isinstance(error, (TypeError, ValueError)):
        raise WorkflowError(
            f"Windows repository backend contract failed while {operation}{location}: "
            f"{type(error).__name__}"
        ) from error
    raise error


class WindowsRepositoryAdapter:
    """Repository adapter backed by one pinned native Windows root handle."""

    def __init__(self, root: Path):
        try:
            self._module = _load_windows_handle_fs()
        except (ImportError, OSError) as error:
            raise WorkflowError(
                "Windows handle-relative repository backend is unavailable"
            ) from error
        self._filesystem = None
        try:
            self._filesystem = self._module.RepositoryFs(str(root))
        except Exception as error:
            _raise_native_workflow_error(self._module, error, "acquiring repository root")
        try:
            self.root = Path(self._filesystem.display_path)
            self.root_identity = self.observe_root_identity()
        except BaseException as primary:
            filesystem = self._filesystem
            self._filesystem = None
            if filesystem is not None:
                try:
                    filesystem.close()
                except Exception as cleanup_error:
                    primary.add_note(
                        "Windows repository root cleanup failed after acquisition: "
                        f"{_native_error_code(cleanup_error)}"
                    )
            raise

    def normalize(self, relative: str) -> str:
        return _normalize_repository_relative(relative)

    def display(self, relative: str) -> Path:
        return _target_path(self.root, self.normalize(relative))

    def _call(self, operation: str, relative: str, callback: Any) -> Any:
        try:
            return callback()
        except Exception as error:
            _raise_native_workflow_error(
                self._module, error, operation, self.normalize(relative)
            )

    def exists(self, relative: str, require_file: bool = False) -> bool:
        relative = self.normalize(relative)
        return bool(
            self._call(
                "checking existence",
                relative,
                lambda: self._filesystem.exists(
                    relative, directory=False if require_file else None
                ),
            )
        )

    def read_bytes(self, relative: str) -> bytes:
        relative = self.normalize(relative)
        return self._call(
            "reading file", relative, lambda: self._filesystem.read_bytes(relative)
        )

    def hash_file(self, relative: str) -> str | None:
        relative = self.normalize(relative)
        if not self.exists(relative, require_file=True):
            return None
        return self._call(
            "hashing file", relative, lambda: self._filesystem.hash_file(relative)
        )

    def mkdir(self, relative: str) -> None:
        relative = self.normalize(relative)
        self._call(
            "creating directories",
            relative,
            lambda: self._filesystem.ensure_directories(relative),
        )

    def atomic_write(self, relative: str, content: bytes) -> None:
        relative = self.normalize(relative)
        parent = relative.rpartition("/")[0]
        if parent:
            self.mkdir(parent)
        self._call(
            "atomically writing file",
            relative,
            lambda: self._filesystem.atomic_write(relative, content),
        )

    def unlink(self, relative: str, missing_ok: bool = False) -> None:
        relative = self.normalize(relative)
        self._call(
            "removing file",
            relative,
            lambda: self._filesystem.unlink(relative, missing_ok=missing_ok),
        )

    def rmdir(self, relative: str) -> None:
        relative = self.normalize(relative)
        try:
            self._filesystem.rmdir(relative, missing_ok=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 145:
                return
            _raise_native_workflow_error(
                self._module, error, "removing directory", relative
            )
        except Exception as error:
            _raise_native_workflow_error(
                self._module, error, "removing directory", relative
            )

    def observe_root_identity(self) -> dict[str, str]:
        try:
            identity = self._filesystem.identity
        except Exception as error:
            _raise_native_workflow_error(
                self._module, error, "observing repository root identity"
            )
        return {
            "schema": ROOT_IDENTITY_SCHEMA,
            "platform": "win32",
            "mode": "windows-ntfs-handle",
            "volumeSerialNumber": f"{identity.volume_serial_number:016x}",
            "fileId": identity.file_id,
        }

    def close(self) -> None:
        filesystem = self._filesystem
        self._filesystem = None
        if filesystem is None:
            return
        try:
            filesystem.close()
        except Exception as error:
            _raise_native_workflow_error(
                self._module, error, "closing repository root"
            )


def _repository_adapter(target: Path) -> RepositoryAdapter:
    """Explicit pathname adapter retained for non-CLI compatibility and tests."""
    return PathRepositoryAdapter(target)


def _repository_adapter_from_raw_target(raw_target: str) -> RepositoryAdapter:
    if os.name == "nt":
        supplied = Path(raw_target).expanduser().absolute()
        return WindowsRepositoryAdapter(supplied)
    return PathRepositoryAdapter(_assert_safe_target(raw_target))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_transaction_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _is_canonical_unsigned_decimal(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is not None


def _validate_root_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise WorkflowError("Repository root identity is malformed")
    mode = value.get("mode")
    if mode == "path-stat":
        expected_keys = {"schema", "platform", "mode", "deviceId", "fileId"}
        if (
            set(value) != expected_keys
            or value.get("schema") != ROOT_IDENTITY_SCHEMA
            or value.get("platform") != sys.platform
            or not _is_canonical_unsigned_decimal(value.get("deviceId"))
            or not _is_canonical_unsigned_decimal(value.get("fileId"))
        ):
            raise WorkflowError("Repository root identity is malformed")
    elif mode == "windows-ntfs-handle":
        expected_keys = {
            "schema",
            "platform",
            "mode",
            "volumeSerialNumber",
            "fileId",
        }
        if (
            set(value) != expected_keys
            or value.get("schema") != ROOT_IDENTITY_SCHEMA
            or value.get("platform") != "win32"
            or not isinstance(value.get("volumeSerialNumber"), str)
            or re.fullmatch(r"[0-9a-f]{16}", value["volumeSerialNumber"]) is None
            or not isinstance(value.get("fileId"), str)
            or re.fullmatch(r"[0-9a-f]{32}", value["fileId"]) is None
        ):
            raise WorkflowError("Repository root identity is malformed")
    else:
        raise WorkflowError("Repository root identity is malformed")
    return dict(value)


def _assert_root_identity_matches(
    expected: dict[str, str], actual: dict[str, str], context: str
) -> None:
    validated_expected = _validate_root_identity(expected)
    validated_actual = _validate_root_identity(actual)
    if validated_expected != validated_actual:
        raise WorkflowError(f"Repository root identity does not match {context}")


def _path_stat_identity_matches_windows_handle(
    expected: dict[str, str], actual: dict[str, str]
) -> bool:
    validated_expected = _validate_root_identity(expected)
    validated_actual = _validate_root_identity(actual)
    if (
        validated_expected.get("mode") != "path-stat"
        or validated_expected.get("platform") != "win32"
        or validated_actual.get("mode") != "windows-ntfs-handle"
    ):
        return False
    native_file_id = bytes.fromhex(validated_actual["fileId"])
    return (
        int(validated_expected["deviceId"])
        == int(validated_actual["volumeSerialNumber"], 16)
        and int(validated_expected["fileId"])
        == int.from_bytes(native_file_id, "little")
    )


def _assert_persisted_root_identity_matches(
    expected: dict[str, str], actual: dict[str, str], context: str
) -> None:
    validated_expected = _validate_root_identity(expected)
    validated_actual = _validate_root_identity(actual)
    if validated_expected == validated_actual:
        return
    if _path_stat_identity_matches_windows_handle(
        validated_expected, validated_actual
    ):
        return
    raise WorkflowError(f"Repository root identity does not match {context}")


def _assert_current_root_identity(
    adapter: RepositoryAdapter, expected: dict[str, str], context: str
) -> None:
    _assert_root_identity_matches(expected, adapter.observe_root_identity(), context)


def _current_hash(path: Path, target: Path | None = None) -> str | None:
    if target is not None:
        _assert_safe_path(target, path, require_file=True)
    value = _lstat_optional(path)
    if value is None:
        return None
    if not stat.S_ISREG(value.st_mode):
        raise WorkflowError(f"Managed path is not a regular file: {path}")
    return _sha256_file(path)


def _text_hash(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return _sha256_bytes(normalized.encode("utf-8"))


def _relative(target: Path, path: Path | None) -> str:
    if path is None:
        raise WorkflowError("Cannot make a null path relative")
    return path.relative_to(target).as_posix()


def _target_path(target: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
        raise WorkflowError(f"Managed path is not a safe repository-relative path: {relative}")
    candidate = target.joinpath(*relative_path.parts)
    try:
        candidate.relative_to(target)
    except ValueError as error:
        raise WorkflowError(f"Managed path escapes target repository: {relative}") from error
    return candidate


def _assert_safe_target(raw_target: str) -> Path:
    supplied = Path(raw_target).expanduser().absolute()
    _assert_no_link_components(supplied)
    supplied_stat = _lstat_optional(supplied)
    if supplied_stat is None or not stat.S_ISDIR(supplied_stat.st_mode):
        raise WorkflowError(f"Target repository does not exist or is not a directory: {supplied}")
    target = supplied.resolve()
    if target == Path(target.anchor):
        raise WorkflowError(f"Refusing to use a filesystem root as the target: {target}")
    return target


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_link_or_reparse(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    return stat.S_ISLNK(value.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_no_link_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        value = _lstat_optional(current)
        if value is not None and _is_link_or_reparse(value):
            raise WorkflowError(f"Refusing to traverse link or reparse point: {current}")


def _assert_safe_path(target: Path, path: Path, require_file: bool = False) -> None:
    try:
        path.absolute().relative_to(target.absolute())
    except ValueError as error:
        raise WorkflowError(f"Managed path escapes target repository: {path}") from error
    _assert_no_link_components(path)
    value = _lstat_optional(path)
    if require_file and value is not None and not stat.S_ISREG(value.st_mode):
        raise WorkflowError(f"Managed path is not a regular file: {path}")


def _assert_no_symlink(target: Path, path: Path) -> None:
    _assert_safe_path(target, path)


def _safe_exists(target: Path, path: Path, require_file: bool = False) -> bool:
    _assert_safe_path(target, path, require_file=require_file)
    return _lstat_optional(path) is not None


def _safe_read_bytes(target: Path, path: Path) -> bytes:
    _assert_safe_path(target, path, require_file=True)
    if _lstat_optional(path) is None:
        raise WorkflowError(f"Managed file does not exist: {path}")
    return path.read_bytes()


def _safe_unlink(target: Path, path: Path, missing_ok: bool = False) -> None:
    _assert_safe_path(target, path, require_file=True)
    if _lstat_optional(path) is None:
        if missing_ok:
            return
        raise FileNotFoundError(path)
    path.unlink()


def _safe_rmdir(target: Path, path: Path) -> None:
    _assert_safe_path(target, path)
    value = _lstat_optional(path)
    if value is None or not stat.S_ISDIR(value.st_mode):
        return
    path.rmdir()


def _atomic_write(path: Path, content: bytes) -> None:
    managed_target = path.parent
    _assert_safe_path(managed_target, path, require_file=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_path(managed_target, path.parent)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        _assert_safe_path(managed_target, temporary, require_file=True)
        with temporary.open("xb") as file_handle:
            file_handle.write(content)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        _assert_safe_path(managed_target, temporary, require_file=True)
        _assert_safe_path(managed_target, path, require_file=True)
        os.replace(temporary, path)
    finally:
        _assert_safe_path(managed_target, temporary, require_file=True)
        if _lstat_optional(temporary) is not None:
            _safe_unlink(managed_target, temporary)


def _atomic_write_managed(target: Path, path: Path, content: bytes) -> None:
    _assert_safe_path(target, path, require_file=True)
    _atomic_write(path, content)


def _write_json(path: Path, value: dict[str, Any], target: Path | None = None) -> None:
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if target is None:
        _atomic_write(path, content)
    else:
        _atomic_write_managed(target, path, content)


def _read_state(
    path: Path,
    expected_target: Path | None = None,
    adapter: RepositoryAdapter | None = None,
    expected_root_identity: dict[str, str] | None = None,
    allow_legacy: bool = False,
) -> dict[str, Any] | None:
    target = expected_target or path.parent
    repository = adapter or _repository_adapter(target)
    relative = repository.normalize(_relative(target, path))
    if not repository.exists(relative, require_file=True):
        return None
    try:
        content = repository.read_bytes(relative).decode("utf-8")
        value = json.loads(content)
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"State manifest is not valid JSON: {path}") from error
    legacy_root_keys = {
        "schema", "packageVersion", "target", "codexVersion", "files", "agents",
        "backups", "createdDirectories", "features", "installedAt", "updatedAt",
        "transactionId",
    }
    if not isinstance(value, dict):
        raise WorkflowError(f"Unsupported or malformed state manifest: {path}")
    schema = value.get("schema")
    if schema == SCHEMA:
        if set(value) != legacy_root_keys | {"rootIdentity"}:
            raise WorkflowError(f"Unsupported or malformed state manifest: {path}")
        state_identity = _validate_root_identity(value.get("rootIdentity"))
        current_identity = expected_root_identity or repository.root_identity
        _assert_persisted_root_identity_matches(
            state_identity, current_identity, "since the state manifest was written"
        )
    elif schema == LEGACY_STATE_SCHEMA and allow_legacy:
        if set(value) != legacy_root_keys:
            raise WorkflowError(f"Unsupported or malformed state manifest: {path}")
    else:
        raise WorkflowError(f"Unsupported or malformed state manifest: {path}")
    if value.get("packageVersion") != PACKAGE_VERSION:
        raise WorkflowError(f"Unsupported or malformed state manifest: {path}")
    if value.get("codexVersion") not in SUPPORTED_CODEX_VERSIONS:
        raise WorkflowError(f"Unsupported or malformed state manifest: {path}")
    if not _is_timestamp(value.get("installedAt")) or not _is_timestamp(value.get("updatedAt")):
        raise WorkflowError(f"Unsupported or malformed state manifest: {path}")
    if not _is_transaction_id(value.get("transactionId")):
        raise WorkflowError(f"Unsupported or malformed state manifest: {path}")
    if not isinstance(value.get("files"), dict):
        raise WorkflowError(f"Unsupported or malformed state manifest: {path}")
    if value.get("target") != str(target):
        raise WorkflowError(f"State manifest target does not match its repository: {path}")
    source_by_destination = {
        destination: source
        for source, destination in (
            list(PACKAGE_FILES.items())
            + list(PROJECT_TEMPLATE_FILES.items())
            + list(CUSTOM_AGENT_FILES.items())
        )
    }
    referenced_backups: set[str] = set()
    for relative, record in value["files"].items():
        if not isinstance(relative, str) or not isinstance(record, dict):
            raise WorkflowError(f"Malformed managed file record in state manifest: {path}")
        if relative not in source_by_destination or "\\" in relative:
            raise WorkflowError(f"Malformed managed file path in state manifest: {relative}")
        if set(record) != {
            "source", "installedHash", "mutable", "owner", "preInstallHash", "backup"
        }:
            raise WorkflowError(f"Malformed managed file record in state manifest: {relative}")
        if record.get("source") != source_by_destination[relative]:
            raise WorkflowError(f"Malformed source for managed file: {relative}")
        expected_mutable = relative in PROJECT_TEMPLATE_FILES.values()
        if type(record.get("mutable")) is not bool or record["mutable"] != expected_mutable:
            raise WorkflowError(f"Malformed mutable flag for managed file: {relative}")
        if relative.startswith(f"{INSTALL_DIRECTORY}/"):
            allowed_owners = {"package"}
        elif relative.startswith(".codex/agents/"):
            allowed_owners = {"custom-agent", "preexisting-identical"}
        else:
            allowed_owners = {"project-template-seed"}
        if record.get("owner") not in allowed_owners:
            raise WorkflowError(f"Malformed owner for managed file: {relative}")
        if not _is_sha256(record.get("installedHash")):
            raise WorkflowError(f"Malformed installedHash for managed file: {relative}")
        pre_hash = record.get("preInstallHash")
        if pre_hash is not None and not _is_sha256(pre_hash):
            raise WorkflowError(f"Malformed preInstallHash for managed file: {relative}")
        backup = record.get("backup")
        if backup is not None:
            if not isinstance(backup, str) or "\\" in backup:
                raise WorkflowError(f"Malformed backup path for managed file: {relative}")
            parts = Path(backup).parts
            expected_suffix = Path(relative).parts
            if (
                len(parts) != len(expected_suffix) + 2
                or parts[0] != BACKUP_DIRECTORY
                or not _is_transaction_id(parts[1])
                or tuple(parts[2:]) != expected_suffix
            ):
                raise WorkflowError(f"Malformed backup path for managed file: {relative}")
            referenced_backups.add(backup)
        if record.get("owner") == "preexisting-identical" and (
            pre_hash != record.get("installedHash") or backup is not None
        ):
            raise WorkflowError(f"Malformed pre-existing ownership for managed file: {relative}")
    agents = value.get("agents")
    if agents is not None:
        base_agent_keys = {
            "path", "blockHash", "createdFile", "preInstallHash", "installedFileHash",
            "separator",
        }
        if not isinstance(agents, dict) or frozenset(agents) not in {
            frozenset(base_agent_keys), frozenset(base_agent_keys | {"backup"})
        }:
            raise WorkflowError(f"Malformed AGENTS record in state manifest: {path}")
        if agents.get("path") != "AGENTS.md" or type(agents.get("createdFile")) is not bool:
            raise WorkflowError(f"Malformed AGENTS record in state manifest: {path}")
        if not _is_sha256(agents.get("blockHash")) or not _is_sha256(agents.get("installedFileHash")):
            raise WorkflowError(f"Malformed AGENTS hashes in state manifest: {path}")
        agent_pre_hash = agents.get("preInstallHash")
        if agent_pre_hash is not None and not _is_sha256(agent_pre_hash):
            raise WorkflowError(f"Malformed AGENTS preInstallHash in state manifest: {path}")
        if not isinstance(agents.get("separator"), str):
            raise WorkflowError(f"Malformed AGENTS separator in state manifest: {path}")
        if agents["createdFile"] and agent_pre_hash is not None:
            raise WorkflowError(f"Malformed generated AGENTS record in state manifest: {path}")
        agent_backup = agents.get("backup")
        if agent_backup is not None:
            if not isinstance(agent_backup, str):
                raise WorkflowError(f"Malformed AGENTS backup in state manifest: {path}")
            parts = Path(agent_backup).parts
            if (
                "\\" in agent_backup
                or len(parts) != 3
                or parts[0] != BACKUP_DIRECTORY
                or not _is_transaction_id(parts[1])
                or parts[2] != "AGENTS.md"
            ):
                raise WorkflowError(f"Malformed AGENTS backup in state manifest: {path}")
            referenced_backups.add(agent_backup)
    backups = value.get("backups", [])
    if (
        not isinstance(backups, list)
        or any(not isinstance(item, str) for item in backups)
        or len(backups) != len(set(backups))
        or backups != sorted(backups)
        or set(backups) != referenced_backups
    ):
        raise WorkflowError(f"Malformed backups list in state manifest: {path}")
    created_directories = value.get("createdDirectories", [])
    if not isinstance(created_directories, list) or any(
        not isinstance(item, str) for item in created_directories
    ):
        raise WorkflowError(f"Malformed createdDirectories in state manifest: {path}")
    allowed_directories = {
        INSTALL_DIRECTORY,
        f"{INSTALL_DIRECTORY}/rules",
        f"{INSTALL_DIRECTORY}/templates",
        "docs",
        "docs/ai",
        "plans",
        ".codex",
        ".codex/agents",
    }
    if (
        len(created_directories) != len(set(created_directories))
        or any(relative not in allowed_directories or "\\" in relative for relative in created_directories)
    ):
        raise WorkflowError(f"Malformed createdDirectories in state manifest: {path}")
    features = value.get("features")
    expected_custom_agents = any(
        relative.startswith(".codex/agents/") for relative in value["files"]
    )
    if (
        not isinstance(features, dict)
        or set(features) != {"customAgents"}
        or type(features.get("customAgents")) is not bool
        or features["customAgents"] != expected_custom_agents
    ):
        raise WorkflowError(f"Malformed features in state manifest: {path}")
    if schema == LEGACY_STATE_SCHEMA:
        print(
            "WARN: legacy installer state v1 was validated and will be migrated "
            "to state v2 by the next successful write"
        )
    return value


def _detect_codex_version() -> str:
    if os.name == "nt":
        command_path = shutil.which("codex.cmd") or shutil.which("codex.exe")
        if command_path:
            command = [command_path, "--version"]
        else:
            powershell_path = shutil.which("codex.ps1")
            if not powershell_path:
                raise WorkflowError("Unable to find Codex CLI on PATH; install Codex CLI first")
            shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
            if not shell:
                raise WorkflowError("Codex uses a PowerShell shim, but PowerShell was not found")
            command = [shell, "-NoProfile", "-File", powershell_path, "--version"]
    else:
        command_path = shutil.which("codex")
        if not command_path:
            raise WorkflowError("Unable to find Codex CLI on PATH; install Codex CLI first")
        command = [command_path, "--version"]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkflowError("Unable to run 'codex --version'; install Codex CLI first") from error
    output = f"{result.stdout}\n{result.stderr}".strip()
    match = re.search(r"\bcodex-cli\s+([^\s]+)", output)
    if result.returncode != 0 or not match:
        raise WorkflowError(f"Unable to parse Codex CLI version from: {output!r}")
    return match.group(1)


def _validate_codex_version() -> str:
    version = _detect_codex_version()
    if version not in SUPPORTED_CODEX_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_CODEX_VERSIONS))
        raise WorkflowError(
            f"Unsupported Codex CLI {version}; supported version(s): {supported}. No files were changed."
        )
    return version


def _validate_sources(source_root: Path, with_custom_agents: bool = False) -> None:
    required = list(PACKAGE_FILES) + list(PROJECT_TEMPLATE_FILES)
    if with_custom_agents:
        required.extend(CUSTOM_AGENT_FILES)
        required.append(COMPATIBILITY_FILE)
    for relative in required:
        source = source_root / relative
        if not source.is_file() or source.is_symlink():
            raise WorkflowError(f"Required package source is missing or unsafe: {source}")


def _validate_custom_agent_sources(source_root: Path, codex_version: str) -> list[str]:
    try:
        from validate_agent_configs import validate_catalog
    except ModuleNotFoundError as error:
        raise WorkflowError(
            "Custom agent installation requires Python 3.11 or newer with tomllib support"
        ) from error
    report = validate_catalog(source_root, codex_version)
    if report.errors:
        raise WorkflowError("Custom agent validation failed:\n" + "\n".join(report.errors))
    return list(report.warnings)


def _extract_managed_block(content: str) -> tuple[int, int, str] | None:
    starts = [match.start() for match in re.finditer(re.escape(START_MARKER), content)]
    ends = [match.end() for match in re.finditer(re.escape(END_MARKER), content)]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise WorkflowError("AGENTS.md contains duplicate or malformed workflow markers")
    return starts[0], ends[0], content[starts[0] : ends[0]]


def _install_agents_content(
    existing: bytes | None, old_agents: dict[str, Any] | None
) -> tuple[bytes | None, dict[str, Any] | None, str | None]:
    text = existing.decode("utf-8") if existing is not None else ""
    extracted = _extract_managed_block(text)
    block_hash = _text_hash(MANAGED_BLOCK)

    if extracted is not None:
        if old_agents is None:
            raise WorkflowError(
                "AGENTS.md already contains an unmanaged workflow block; remove it or restore the state manifest"
            )
        start, end, current_block = extracted
        if _text_hash(current_block) != old_agents.get("blockHash"):
            return None, old_agents, "PRESERVED MODIFIED AGENTS.md BLOCK"
        updated = text[:start] + MANAGED_BLOCK + text[end:]
        record = dict(old_agents)
        record["blockHash"] = block_hash
        record["installedFileHash"] = _sha256_bytes(updated.encode("utf-8"))
        return updated.encode("utf-8"), record, None

    separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
    updated = f"{text}{separator}{MANAGED_BLOCK}\n"
    record = {
        "path": "AGENTS.md",
        "blockHash": block_hash,
        "createdFile": existing is None,
        "preInstallHash": _sha256_bytes(existing) if existing is not None else None,
        "installedFileHash": _sha256_bytes(updated.encode("utf-8")),
        "separator": separator,
    }
    return updated.encode("utf-8"), record, None


def _prepare_actions(
    target: Path, transaction_id: str, requests: list[tuple[str, Path, bytes | None]]
) -> list[Action]:
    adapter = _repository_adapter(target)
    relative_requests = [
        (kind, _relative(target, path), content) for kind, path, content in requests
    ]
    return _prepare_relative_actions(adapter, transaction_id, relative_requests)


def _prepare_relative_actions(
    adapter: RepositoryAdapter,
    transaction_id: str,
    requests: list[tuple[str, str, bytes | None]],
) -> list[Action]:
    actions: list[Action] = []
    state_indexes = [index for index, (_, path, _) in enumerate(requests) if path == STATE_FILENAME]
    if state_indexes and state_indexes != [len(requests) - 1]:
        raise WorkflowError("State manifest transaction action must be last")
    seen_paths: set[str] = set()
    for index, (kind, path, content) in enumerate(requests):
        if kind not in {"write", "delete"}:
            raise WorkflowError(f"Unknown transaction action: {kind}")
        if kind == "write" and content is None:
            raise WorkflowError(f"Write action has no content: {path}")
        if kind == "delete" and content is not None:
            raise WorkflowError(f"Delete action unexpectedly has content: {path}")
        path = adapter.normalize(path)
        if path in seen_paths:
            raise WorkflowError(f"Duplicate transaction action path: {path}")
        seen_paths.add(path)
        pre_hash = adapter.hash_file(path)
        created = pre_hash is None
        backup = None
        backup_hash = None
        if not created:
            backup = adapter.normalize(f"{BACKUP_DIRECTORY}/{transaction_id}/{path}")
            backup_hash = pre_hash
        post_hash = _sha256_bytes(content) if content is not None else None
        actions.append(
            Action(
                index=index,
                kind=kind,
                path=path,
                content=content,
                backup=backup,
                created=created,
                pre_hash=pre_hash,
                post_hash=post_hash,
                backup_hash=backup_hash,
            )
        )
    for action in actions:
        if adapter.hash_file(action.path) != action.pre_hash:
            raise WorkflowError(
                f"Managed file changed while planning transaction: {adapter.display(action.path)}"
            )
    return actions


def _prepare_backups(
    adapter: RepositoryAdapter | Path, actions: list[Action]
) -> None:
    repository = _repository_adapter(adapter) if isinstance(adapter, Path) else adapter
    for action in actions:
        if action.backup is None:
            continue
        if repository.hash_file(action.path) != action.pre_hash:
            raise WorkflowError(
                f"Managed file changed before backup: {repository.display(action.path)}"
            )
        repository.atomic_write(action.backup, repository.read_bytes(action.path))
        if repository.hash_file(action.backup) != action.backup_hash:
            raise WorkflowError(
                f"Backup verification failed for: {repository.display(action.path)}"
            )


def _parent_directories(relative: str) -> list[str]:
    parts = Path(relative).parts[:-1]
    return [Path(*parts[:index]).as_posix() for index in range(1, len(parts) + 1)]


def _eligible_rollback_directories(actions: list[Action]) -> set[str]:
    eligible: set[str] = set()
    for action in actions:
        if action.kind == "write":
            eligible.update(_parent_directories(action.path))
        if action.backup is not None:
            eligible.update(_parent_directories(action.backup))
    return eligible


def _canonical_directory_order(directories: set[str]) -> list[str]:
    return sorted(directories, key=lambda relative: (len(Path(relative).parts), relative))


def _plan_rollback_directories(
    adapter: RepositoryAdapter, actions: list[Action]
) -> list[str]:
    absent: set[str] = set()
    for relative in _eligible_rollback_directories(actions):
        normalized = adapter.normalize(relative)
        if not adapter.exists(normalized):
            absent.add(normalized)
    return _canonical_directory_order(absent)


def _validate_rollback_directories(
    adapter: RepositoryAdapter, value: Any, actions: list[Action]
) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WorkflowError("Transaction journal rollbackDirectories is malformed")
    normalized = [adapter.normalize(item) for item in value]
    if normalized != value or len(normalized) != len(set(normalized)):
        raise WorkflowError("Transaction journal rollbackDirectories is not canonical")
    if normalized != _canonical_directory_order(set(normalized)):
        raise WorkflowError("Transaction journal rollbackDirectories is not canonical")
    eligible = _eligible_rollback_directories(actions)
    if any(relative not in eligible for relative in normalized):
        raise WorkflowError("Transaction journal rollbackDirectories is unrelated to its actions")
    for relative in normalized:
        adapter.exists(relative)
    return normalized


def _journal_value(
    target: Path,
    transaction_id: str,
    operation: str,
    actions: list[Action],
    phase: str = "prepared",
    created_at: str | None = None,
    root_identity: dict[str, str] | None = None,
    rollback_directories: list[str] | None = None,
) -> dict[str, Any]:
    identity = root_identity or _repository_adapter(target).root_identity
    return {
        "schema": JOURNAL_SCHEMA,
        "transactionId": transaction_id,
        "operation": operation,
        "target": str(target),
        "rootIdentity": _validate_root_identity(identity),
        "createdAt": created_at or _now(),
        "phase": phase,
        "rollbackDirectories": list(rollback_directories or []),
        "actions": [action.to_json() for action in actions],
    }


def _parse_journal(
    target: Path,
    value: Any,
    adapter: RepositoryAdapter | None = None,
    expected_root_identity: dict[str, str] | None = None,
) -> tuple[str, str, str, str, list[Action], list[str]]:
    if not isinstance(value, dict):
        raise WorkflowError("Transaction journal has an incomplete or unknown root schema")
    if value.get("schema") in {LEGACY_JOURNAL_SCHEMA, OLDER_JOURNAL_SCHEMA}:
        raise WorkflowError("Legacy transaction journal requires manual recovery")
    if value.get("schema") != JOURNAL_SCHEMA:
        raise WorkflowError("Legacy or unsupported transaction journal requires manual recovery")
    root_keys = {
        "schema", "transactionId", "operation", "target", "rootIdentity", "createdAt",
        "phase", "rollbackDirectories", "actions",
    }
    action_keys = {
        "index", "kind", "path", "owner", "created", "backup", "preHash", "postHash",
        "backupHash", "phase",
    }
    if set(value) != root_keys:
        raise WorkflowError("Transaction journal has an incomplete or unknown root schema")
    journal_identity = _validate_root_identity(value.get("rootIdentity"))
    repository = adapter or _repository_adapter(target)
    current_identity = expected_root_identity or repository.root_identity
    _assert_persisted_root_identity_matches(
        journal_identity, current_identity, "since the transaction journal was written"
    )
    transaction_id = value.get("transactionId")
    if not isinstance(transaction_id, str) or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
        raise WorkflowError("Transaction journal has an invalid transactionId")
    operation = value.get("operation")
    if operation not in {"install", "uninstall"}:
        raise WorkflowError("Transaction journal has an invalid operation")
    if value.get("target") != str(target):
        raise WorkflowError("Transaction journal does not match this target")
    created_at = value.get("createdAt")
    try:
        parsed_created_at = datetime.fromisoformat(created_at) if isinstance(created_at, str) else None
    except ValueError as error:
        raise WorkflowError("Transaction journal has an invalid createdAt") from error
    if parsed_created_at is None or parsed_created_at.tzinfo is None:
        raise WorkflowError("Transaction journal has an invalid createdAt")
    journal_phase = value.get("phase")
    if journal_phase not in {"prepared", "applying", "rollingBack", "committed"}:
        raise WorkflowError("Transaction journal has an invalid phase")
    items = value.get("actions")
    if not isinstance(items, list) or not items:
        raise WorkflowError("Transaction journal must contain actions")
    actions: list[Action] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != action_keys:
            raise WorkflowError("Transaction journal has an incomplete or unknown action schema")
        if item.get("index") != index or item.get("owner") != "workflow-manager":
            raise WorkflowError("Transaction journal action identity is invalid")
        kind = item.get("kind")
        created = item.get("created")
        phase = item.get("phase")
        relative = item.get("path")
        if kind not in {"write", "delete"} or type(created) is not bool:
            raise WorkflowError("Transaction journal action type is invalid")
        if phase not in {"prepared", "applying", "applied", "rollingBack", "rolledBack"}:
            raise WorkflowError("Transaction journal action phase is invalid")
        if not isinstance(relative, str):
            raise WorkflowError("Transaction journal action path is invalid")
        path = repository.normalize(relative)
        if relative not in _managed_action_paths() and not _is_managed_backup_path(relative):
            raise WorkflowError(f"Transaction journal action path is not package-managed: {relative}")
        if path in seen_paths:
            raise WorkflowError(f"Transaction journal action path is unsafe or duplicated: {relative}")
        seen_paths.add(path)
        pre_hash = item.get("preHash")
        post_hash = item.get("postHash")
        backup_hash = item.get("backupHash")
        backup_relative = item.get("backup")
        if pre_hash is not None and not _is_sha256(pre_hash):
            raise WorkflowError(f"Transaction journal preHash is invalid: {relative}")
        if kind == "write" and not _is_sha256(post_hash):
            raise WorkflowError(f"Transaction journal postHash is invalid: {relative}")
        if kind == "delete" and post_hash is not None:
            raise WorkflowError(f"Delete action postHash must be null: {relative}")
        backup = None
        if created:
            if pre_hash is not None or backup_relative is not None or backup_hash is not None:
                raise WorkflowError(f"Created action has invalid pre-state metadata: {relative}")
        else:
            expected_backup = f"{BACKUP_DIRECTORY}/{transaction_id}/{relative}"
            if not _is_sha256(pre_hash) or backup_hash != pre_hash:
                raise WorkflowError(f"Pre-existing action hash metadata is invalid: {relative}")
            if backup_relative != expected_backup:
                raise WorkflowError(f"Transaction journal backup path is invalid: {relative}")
            backup = repository.normalize(backup_relative)
        actions.append(
            Action(
                index=index,
                kind=kind,
                path=path,
                backup=backup,
                created=created,
                pre_hash=pre_hash,
                post_hash=post_hash,
                backup_hash=backup_hash,
                phase=phase,
            )
        )
    state_indexes = [action.index for action in actions if action.path == STATE_FILENAME]
    if state_indexes and state_indexes != [len(actions) - 1]:
        raise WorkflowError("State manifest transaction action must be last")
    rollback_directories = _validate_rollback_directories(
        repository, value.get("rollbackDirectories"), actions
    )
    return (
        transaction_id,
        operation,
        created_at,
        journal_phase,
        actions,
        rollback_directories,
    )


def _read_journal(
    target: Path, journal_path: Path
) -> tuple[str, str, str, str, list[Action], list[str]]:
    adapter = _repository_adapter(target)
    return _read_relative_journal(adapter, _relative(target, journal_path))


def _read_relative_journal(
    adapter: RepositoryAdapter,
    journal_relative: str,
    expected_root_identity: dict[str, str] | None = None,
) -> tuple[str, str, str, str, list[Action], list[str]]:
    journal_relative = adapter.normalize(journal_relative)
    try:
        value = json.loads(adapter.read_bytes(journal_relative).decode("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(
            "Cannot recover malformed transaction journal; retain it for manual recovery: "
            f"{adapter.display(journal_relative)}"
        ) from error
    return _parse_journal(adapter.root, value, adapter, expected_root_identity)


def _classify_actions(adapter: RepositoryAdapter, actions: list[Action]) -> dict[int, str]:
    classifications: dict[int, str] = {}
    errors: list[str] = []
    for action in actions:
        try:
            current_hash = adapter.hash_file(action.path)
            if (
                current_hash == action.post_hash
                and action.backup is not None
                and adapter.hash_file(action.backup) != action.backup_hash
            ):
                errors.append(f"backup is missing or tampered: {adapter.display(action.backup)}")
            if current_hash == action.pre_hash:
                classifications[action.index] = "pre"
            elif current_hash == action.post_hash:
                classifications[action.index] = "post"
            else:
                errors.append(f"intervening content at {adapter.display(action.path)}")
        except (OSError, WorkflowError) as error:
            errors.append(str(error))
    if errors:
        raise WorkflowError(
            "Recovery preflight failed; no files were changed and the journal was retained:\n"
            + "\n".join(errors)
        )
    return classifications


def _apply_actions(
    target: Path, journal_path: Path, transaction_id: str, operation: str, actions: list[Action]
) -> None:
    adapter = _repository_adapter(target)
    _apply_relative_actions(
        adapter, _relative(target, journal_path), transaction_id, operation, actions
    )


def _apply_relative_actions(
    adapter: RepositoryAdapter,
    journal_relative: str,
    transaction_id: str,
    operation: str,
    actions: list[Action],
) -> None:
    if not actions:
        return
    created_at = _now()
    root_identity = _validate_root_identity(adapter.root_identity)
    rollback_directories = _plan_rollback_directories(adapter, actions)
    _write_relative_json(
        adapter,
        journal_relative,
        _journal_value(
            adapter.root, transaction_id, operation, actions, "prepared", created_at,
            root_identity, rollback_directories,
        ),
        root_identity,
    )
    (
        parsed_id,
        parsed_operation,
        _,
        _,
        parsed_actions,
        parsed_rollback_directories,
    ) = _read_relative_journal(
        adapter, journal_relative, root_identity
    )
    if (
        parsed_id != transaction_id
        or parsed_operation != operation
        or parsed_rollback_directories != rollback_directories
    ):
        raise WorkflowError("Transaction journal verification failed before apply")
    _assert_current_root_identity(adapter, root_identity, "before backup creation")
    _prepare_backups(adapter, actions)
    _classify_actions(adapter, parsed_actions)
    try:
        for action in actions:
            _assert_current_root_identity(adapter, root_identity, "before applying an action")
            if adapter.hash_file(action.path) != action.pre_hash:
                raise WorkflowError(f"Managed file changed before apply: {adapter.display(action.path)}")
            action.phase = "applying"
            _write_relative_json(adapter, journal_relative, _journal_value(
                adapter.root, transaction_id, operation, actions, "applying", created_at,
                root_identity, rollback_directories,
            ), root_identity)
            if action.kind == "write":
                if action.content is None:
                    raise WorkflowError(f"Write action has no content: {action.path}")
                adapter.atomic_write(action.path, action.content)
            elif action.kind == "delete":
                adapter.unlink(action.path, missing_ok=True)
            else:
                raise WorkflowError(f"Unknown transaction action: {action.kind}")
            if adapter.hash_file(action.path) != action.post_hash:
                raise WorkflowError(
                    f"Managed file failed post-write verification: {adapter.display(action.path)}"
                )
            action.phase = "applied"
            _write_relative_json(adapter, journal_relative, _journal_value(
                adapter.root, transaction_id, operation, actions, "applying", created_at,
                root_identity, rollback_directories,
            ), root_identity)
    except Exception:
        try:
            _rollback_relative_actions(
                adapter,
                journal_relative,
                transaction_id,
                operation,
                created_at,
                actions,
                root_identity,
                rollback_directories,
            )
        except Exception as rollback_error:
            raise WorkflowError(
                "Transaction failed and automatic rollback is incomplete; journal retained at "
                f"{adapter.display(journal_relative)}: "
                f"{rollback_error}"
            ) from rollback_error
        _complete_rollback_cleanup(
            adapter,
            journal_relative,
            actions,
            rollback_directories,
            root_identity,
        )
        raise
    _write_relative_json(adapter, journal_relative, _journal_value(
        adapter.root, transaction_id, operation, actions, "committed", created_at,
        root_identity, rollback_directories,
    ), root_identity)


def _write_relative_json(
    adapter: RepositoryAdapter,
    relative: str,
    value: dict[str, Any],
    expected_root_identity: dict[str, str] | None = None,
) -> None:
    if expected_root_identity is not None:
        _assert_current_root_identity(adapter, expected_root_identity, "before metadata mutation")
    adapter.atomic_write(
        relative, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def _discard_transaction_backups(
    adapter: RepositoryAdapter,
    actions: list[Action],
    expected_root_identity: dict[str, str] | None = None,
) -> None:
    if expected_root_identity is not None:
        _assert_current_root_identity(adapter, expected_root_identity, "before backup cleanup")
    for action in actions:
        if action.backup is not None:
            adapter.unlink(action.backup, missing_ok=True)


def _remove_rollback_directories(
    adapter: RepositoryAdapter,
    rollback_directories: list[str],
    expected_root_identity: dict[str, str],
) -> None:
    _assert_current_root_identity(
        adapter, expected_root_identity, "before rollback directory cleanup"
    )
    for relative in reversed(rollback_directories):
        try:
            adapter.rmdir(relative)
        except OSError as error:
            if getattr(error, "winerror", None) == 145 or error.errno in {
                errno.ENOTEMPTY,
                errno.EEXIST,
            }:
                continue
            raise


def _complete_rollback_cleanup(
    adapter: RepositoryAdapter,
    journal_relative: str,
    actions: list[Action],
    rollback_directories: list[str],
    expected_root_identity: dict[str, str],
) -> None:
    _discard_transaction_backups(adapter, actions, expected_root_identity)
    _remove_rollback_directories(
        adapter, rollback_directories, expected_root_identity
    )
    _assert_current_root_identity(
        adapter, expected_root_identity, "before rollback journal cleanup"
    )
    adapter.unlink(journal_relative, missing_ok=True)


def _rollback_actions(
    target: Path,
    journal_path: Path,
    transaction_id: str,
    operation: str,
    created_at: str,
    actions: list[Action],
    rollback_directories: list[str] | None = None,
) -> None:
    adapter = _repository_adapter(target)
    _rollback_relative_actions(
        adapter,
        _relative(target, journal_path),
        transaction_id,
        operation,
        created_at,
        actions,
        rollback_directories=rollback_directories,
    )


def _rollback_relative_actions(
    adapter: RepositoryAdapter,
    journal_relative: str,
    transaction_id: str,
    operation: str,
    created_at: str,
    actions: list[Action],
    root_identity: dict[str, str] | None = None,
    rollback_directories: list[str] | None = None,
) -> None:
    transaction_identity = _validate_root_identity(root_identity or adapter.root_identity)
    directories = (
        list(rollback_directories)
        if rollback_directories is not None
        else _plan_rollback_directories(adapter, actions)
    )
    _assert_current_root_identity(adapter, transaction_identity, "before rollback")
    classifications = _classify_actions(adapter, actions)
    _write_relative_json(adapter, journal_relative, _journal_value(
        adapter.root, transaction_id, operation, actions, "rollingBack", created_at,
        transaction_identity, directories,
    ), transaction_identity)
    for action in reversed(actions):
        _assert_current_root_identity(adapter, transaction_identity, "before rolling back an action")
        if classifications[action.index] == "pre":
            action.phase = "rolledBack"
            _write_relative_json(adapter, journal_relative, _journal_value(
                adapter.root, transaction_id, operation, actions, "rollingBack", created_at,
                transaction_identity, directories,
            ), transaction_identity)
            continue
        action.phase = "rollingBack"
        _write_relative_json(adapter, journal_relative, _journal_value(
            adapter.root, transaction_id, operation, actions, "rollingBack", created_at,
            transaction_identity, directories,
        ), transaction_identity)
        if action.created:
            if adapter.hash_file(action.path) != action.post_hash:
                raise WorkflowError(
                    f"Created file no longer matches transaction content: {adapter.display(action.path)}"
                )
            adapter.unlink(action.path)
        else:
            if action.backup is None or adapter.hash_file(action.backup) != action.backup_hash:
                raise WorkflowError(
                    f"Verified backup is unavailable for: {adapter.display(action.path)}"
                )
            if adapter.hash_file(action.path) != action.post_hash:
                raise WorkflowError(
                    f"Managed file no longer matches transaction content: {adapter.display(action.path)}"
                )
            adapter.atomic_write(action.path, adapter.read_bytes(action.backup))
        if adapter.hash_file(action.path) != action.pre_hash:
            raise WorkflowError(f"Rollback verification failed for: {adapter.display(action.path)}")
        action.phase = "rolledBack"
        _write_relative_json(adapter, journal_relative, _journal_value(
            adapter.root, transaction_id, operation, actions, "rollingBack", created_at,
            transaction_identity, directories,
        ), transaction_identity)


def _recover_with_adapter(
    adapter: RepositoryAdapter, target: Path, journal_path: Path, dry_run: bool
) -> None:
    root_identity = _validate_root_identity(adapter.root_identity)
    journal_relative = adapter.normalize(_relative(target, journal_path))
    if not adapter.exists(journal_relative, require_file=True):
        print("No interrupted transaction found.")
        return
    (
        transaction_id,
        operation,
        created_at,
        journal_phase,
        actions,
        rollback_directories,
    ) = _read_relative_journal(adapter, journal_relative, root_identity)
    if journal_phase == "committed":
        if dry_run:
            print("DRY-RUN: would finalize committed transaction cleanup")
            return
        if operation == "uninstall":
            _assert_current_root_identity(adapter, root_identity, "before committed cleanup")
            state_backup = _state_action_backup(target, actions, adapter)
            previous_state = _read_state(
                adapter.display(state_backup), target, adapter, root_identity, allow_legacy=True
            )
            if previous_state is None:
                raise WorkflowError("Committed uninstall state backup is unavailable")
            _finalize_committed_uninstall(
                target, journal_path, actions, previous_state, adapter, root_identity
            )
        else:
            _cleanup_backup_files(
                target, _install_commit_cleanup_paths(target, actions), adapter, root_identity
            )
            _assert_current_root_identity(adapter, root_identity, "before journal cleanup")
            adapter.unlink(journal_relative, missing_ok=True)
        print("RECOVERED: committed transaction cleanup finalized")
        return
    classifications = _classify_actions(adapter, actions)
    if dry_run:
        print(
            "DRY-RUN: would roll back "
            f"{sum(state == 'post' for state in classifications.values())} applied action(s)"
        )
        return
    _rollback_relative_actions(
        adapter, journal_relative, transaction_id, operation, created_at, actions,
        root_identity, rollback_directories,
    )
    _complete_rollback_cleanup(
        adapter,
        journal_relative,
        actions,
        rollback_directories,
        root_identity,
    )
    print("RECOVERED: interrupted transaction rolled back")


def recover(
    target: Path,
    journal_path: Path,
    dry_run: bool,
    adapter: RepositoryAdapter | None = None,
) -> None:
    repository = adapter or _repository_adapter(target)
    try:
        _recover_with_adapter(repository, target, journal_path, dry_run)
    finally:
        if adapter is None:
            repository.close()


def _install_with_adapter(
    adapter: RepositoryAdapter,
    target: Path,
    source_root: Path,
    dry_run: bool,
    with_custom_agents: bool = False,
) -> str | None:
    _validate_sources(source_root, with_custom_agents)
    codex_version = _validate_codex_version()
    validation_warnings = (
        _validate_custom_agent_sources(source_root, codex_version)
        if with_custom_agents
        else []
    )
    root_identity = _validate_root_identity(adapter.root_identity)
    if adapter.exists(JOURNAL_FILENAME, require_file=True):
        raise WorkflowError(
            "Interrupted transaction found. Run the installer with --recover: "
            f"{adapter.display(JOURNAL_FILENAME)}"
        )
    old_state = _read_state(
        adapter.display(STATE_FILENAME), target, adapter, root_identity, allow_legacy=True
    )
    if old_state and old_state.get("target") != str(target):
        raise WorkflowError("State manifest target does not match the requested repository")
    if not old_state and adapter.exists(INSTALL_DIRECTORY):
        raise WorkflowError(
            f"{INSTALL_DIRECTORY} exists without a state manifest; move it aside or recover it manually"
        )

    transaction_id = uuid.uuid4().hex
    new_files: dict[str, Any] = dict(old_state.get("files", {})) if old_state else {}
    requests: list[tuple[str, str, bytes | None]] = []
    messages: list[str] = []
    all_files = list(PACKAGE_FILES.items()) + list(PROJECT_TEMPLATE_FILES.items())
    if with_custom_agents:
        all_files.extend(CUSTOM_AGENT_FILES.items())
    directory_candidates = [
        INSTALL_DIRECTORY,
        f"{INSTALL_DIRECTORY}/rules",
        f"{INSTALL_DIRECTORY}/templates",
        "docs",
        "docs/ai",
        "plans",
    ]
    if with_custom_agents:
        directory_candidates.extend([".codex", ".codex/agents"])
    created_directories = list(old_state.get("createdDirectories", [])) if old_state else []
    for relative in directory_candidates:
        if relative not in created_directories and not adapter.exists(relative):
            created_directories.append(relative)

    for source_relative, installed_relative in all_files:
        source_path = source_root / source_relative
        installed_path = adapter.display(installed_relative)
        source_content = source_path.read_bytes()
        source_hash = _sha256_bytes(source_content)
        old_record = new_files.get(installed_relative)
        is_custom_agent = installed_relative.startswith(".codex/agents/")
        current_hash = None
        record_backup = old_record.get("backup") if old_record else None
        pre_install_hash = old_record.get("preInstallHash") if old_record else None

        if adapter.exists(installed_relative, require_file=True):
            current_hash = adapter.hash_file(installed_relative)
            if old_record is None:
                if installed_relative.startswith(f"{INSTALL_DIRECTORY}/"):
                    raise WorkflowError(f"Unmanaged package path already exists: {installed_path}")
                if is_custom_agent:
                    if current_hash != source_hash:
                        raise WorkflowError(
                            f"Unmanaged custom agent conflicts with package agent: {installed_path}"
                        )
                    new_files[installed_relative] = {
                        "source": source_relative,
                        "installedHash": source_hash,
                        "mutable": False,
                        "owner": "preexisting-identical",
                        "preInstallHash": current_hash,
                        "backup": None,
                    }
                    messages.append(f"PRESERVED IDENTICAL CUSTOM AGENT: {installed_relative}")
                    continue
                messages.append(f"PRESERVED EXISTING PROJECT FILE: {installed_relative}")
                continue
            if old_record.get("owner") == "preexisting-identical":
                if current_hash == source_hash:
                    messages.append(f"UNCHANGED PRE-EXISTING CUSTOM AGENT: {installed_relative}")
                else:
                    messages.append(f"PRESERVED PRE-EXISTING CUSTOM AGENT: {installed_relative}")
                continue
            if current_hash != old_record.get("installedHash"):
                messages.append(f"PRESERVED MODIFIED FILE: {installed_relative}")
                continue
            if current_hash != source_hash:
                requests.append(("write", installed_relative, source_content))
                record_backup = (
                    Path(BACKUP_DIRECTORY)
                    / transaction_id
                    / Path(installed_relative)
                ).as_posix()
                messages.append(f"UPDATE: {installed_relative}")
            else:
                messages.append(f"UNCHANGED: {installed_relative}")
        else:
            if old_record and old_record.get("owner") == "preexisting-identical":
                new_files.pop(installed_relative, None)
                messages.append(f"PRESERVED ABSENT PRE-EXISTING CUSTOM AGENT: {installed_relative}")
                continue
            requests.append(("write", installed_relative, source_content))
            messages.append(f"INSTALL: {installed_relative}")

        new_files[installed_relative] = {
            "source": source_relative,
            "installedHash": source_hash,
            "mutable": installed_relative in PROJECT_TEMPLATE_FILES.values(),
            "owner": (
                "package"
                if installed_relative.startswith(f"{INSTALL_DIRECTORY}/")
                else (
                    "custom-agent"
                    if is_custom_agent
                    else "project-template-seed"
                )
            ),
            "preInstallHash": pre_install_hash if old_record is not None else current_hash,
            "backup": record_backup,
        }

    agents_path = adapter.display("AGENTS.md")
    existing_agents = (
        adapter.read_bytes("AGENTS.md")
        if adapter.exists("AGENTS.md", require_file=True)
        else None
    )
    old_agents = old_state.get("agents") if old_state else None
    agents_content, agents_record, agents_message = _install_agents_content(existing_agents, old_agents)
    if agents_message:
        messages.append(agents_message)
    if agents_content is not None and agents_content != existing_agents:
        requests.append(("write", "AGENTS.md", agents_content))
        if existing_agents is not None and agents_record is not None:
            agents_record["backup"] = (
                Path(BACKUP_DIRECTORY) / transaction_id / "AGENTS.md"
            ).as_posix()
        messages.append("INSTALL/UPDATE: AGENTS.md managed block")
    elif agents_content is not None:
        messages.append("UNCHANGED: AGENTS.md managed block")

    referenced_backups = {
        record["backup"]
        for record in new_files.values()
        if record.get("backup") is not None
    }
    if agents_record is not None and agents_record.get("backup") is not None:
        referenced_backups.add(agents_record["backup"])
    old_backups = set(old_state.get("backups", [])) if old_state else set()
    for obsolete_backup in sorted(old_backups - referenced_backups):
        if adapter.exists(obsolete_backup, require_file=True):
            requests.append(("delete", obsolete_backup, None))
            messages.append(f"CLEAN OBSOLETE BACKUP: {obsolete_backup}")
    semantic_state = {
        "schema": SCHEMA,
        "rootIdentity": root_identity,
        "packageVersion": PACKAGE_VERSION,
        "target": str(target),
        "codexVersion": codex_version,
        "files": new_files,
        "agents": agents_record,
        "backups": sorted(referenced_backups),
        "createdDirectories": created_directories,
        "features": {
            "customAgents": any(
                relative.startswith(".codex/agents/") for relative in new_files
            )
        },
    }
    old_semantic_state = None
    if old_state:
        old_semantic_state = {
            key: old_state.get(key)
            for key in (
                "schema",
                "rootIdentity",
                "packageVersion",
                "target",
                "codexVersion",
                "files",
                "agents",
                "backups",
                "createdDirectories",
                "features",
            )
        }
    if semantic_state != old_semantic_state:
        new_state = {
            **semantic_state,
            "installedAt": old_state.get("installedAt", _now()) if old_state else _now(),
            "updatedAt": _now(),
            "transactionId": transaction_id,
        }
        state_content = (json.dumps(new_state, indent=2, sort_keys=True) + "\n").encode("utf-8")
        requests.append(("write", STATE_FILENAME, state_content))

    for warning in validation_warnings:
        print(f"WARN: {warning}")
    for message in messages:
        print(f"DRY-RUN: {message}" if dry_run else message)
    if dry_run:
        return f"DRY-RUN PASS: {len(requests)} write action(s); no files changed"

    actions = _prepare_relative_actions(adapter, transaction_id, requests)
    _assert_current_root_identity(adapter, root_identity, "before transaction mutation")
    _apply_relative_actions(adapter, JOURNAL_FILENAME, transaction_id, "install", actions)
    _cleanup_backup_files(
        target, _install_commit_cleanup_paths(target, actions), adapter, root_identity
    )
    _assert_current_root_identity(adapter, root_identity, "before journal cleanup")
    adapter.unlink(JOURNAL_FILENAME, missing_ok=True)
    return f"PASS: Codex Multi-Agent Workflow installed in {target}"


def install(
    target: Path,
    source_root: Path,
    dry_run: bool,
    with_custom_agents: bool = False,
    adapter: RepositoryAdapter | None = None,
) -> str | None:
    repository = adapter or _repository_adapter(target)
    try:
        success_message = _install_with_adapter(
            repository, target, source_root, dry_run, with_custom_agents
        )
    finally:
        if adapter is None:
            repository.close()
    if adapter is None and success_message is not None:
        print(success_message)
    return success_message


def _remove_agents_content(existing: bytes, agents_record: dict[str, Any]) -> tuple[bytes | None, str | None]:
    text = existing.decode("utf-8")
    extracted = _extract_managed_block(text)
    if extracted is None:
        return existing, "AGENTS.md managed block is already absent"
    start, end, block = extracted
    if _text_hash(block) != agents_record.get("blockHash"):
        return None, "PRESERVED MODIFIED AGENTS.md BLOCK"

    removal_start = start
    separator = agents_record.get("separator", "")
    if separator and text[:start].endswith(separator):
        removal_start -= len(separator)
    removal_end = end + 1 if text[end : end + 1] == "\n" else end
    updated = text[:removal_start] + text[removal_end:]
    if agents_record.get("createdFile") and not updated.strip():
        return b"", None
    return updated.encode("utf-8"), None


def _remove_empty_managed_directories(
    target: Path,
    created_directories: list[str],
    adapter: RepositoryAdapter | None = None,
    expected_root_identity: dict[str, str] | None = None,
) -> None:
    repository = adapter or _repository_adapter(target)
    if expected_root_identity is not None:
        _assert_current_root_identity(
            repository, expected_root_identity, "before directory cleanup"
        )
    candidates = {
        f"{INSTALL_DIRECTORY}/rules",
        f"{INSTALL_DIRECTORY}/templates",
        INSTALL_DIRECTORY,
        "docs/ai",
        "plans",
    }
    candidates.update(repository.normalize(relative) for relative in created_directories)
    for candidate in sorted(candidates, key=lambda value: len(Path(value).parts), reverse=True):
        try:
            repository.rmdir(candidate)
        except OSError:
            pass


def _cleanup_backup_files(
    target: Path,
    backup_paths: list[str | Path],
    adapter: RepositoryAdapter | None = None,
    expected_root_identity: dict[str, str] | None = None,
) -> None:
    repository = adapter or _repository_adapter(target)
    if expected_root_identity is not None:
        _assert_current_root_identity(
            repository, expected_root_identity, "before backup cleanup"
        )
    parents: set[str] = set()
    for backup_path in backup_paths:
        relative = (
            _relative(target, backup_path) if isinstance(backup_path, Path) else backup_path
        )
        relative = repository.normalize(relative)
        parts = Path(relative).parts
        if not parts or parts[0] != BACKUP_DIRECTORY:
            raise WorkflowError(
                f"Backup path escapes the managed backup directory: {repository.display(relative)}"
            )
        repository.unlink(relative, missing_ok=True)
        current = Path(relative).parent
        while current != Path("."):
            current_relative = current.as_posix()
            parents.add(current_relative)
            if current_relative == BACKUP_DIRECTORY:
                break
            current = current.parent
    for parent in sorted(parents, key=lambda value: len(Path(value).parts), reverse=True):
        try:
            repository.rmdir(parent)
        except OSError:
            pass


def _state_action_backup(
    target: Path, actions: list[Action], adapter: RepositoryAdapter | None = None
) -> str:
    repository = adapter or _repository_adapter(target)
    state_actions = [action for action in actions if action.path == STATE_FILENAME]
    if len(state_actions) != 1 or state_actions[0].backup is None:
        raise WorkflowError("Committed transaction lacks its verified state backup")
    state_action = state_actions[0]
    if repository.hash_file(state_action.backup) != state_action.backup_hash:
        raise WorkflowError("Committed transaction state backup is missing or tampered")
    return state_action.backup


def _install_commit_cleanup_paths(target: Path, actions: list[Action]) -> list[str]:
    return [
        action.backup
        for action in actions
        if action.backup is not None
        and (
            action.path == STATE_FILENAME
            or Path(action.path).parts[0] == BACKUP_DIRECTORY
        )
    ]


def _finalize_committed_uninstall(
    target: Path,
    journal_path: Path,
    actions: list[Action],
    previous_state: dict[str, Any],
    adapter: RepositoryAdapter | None = None,
    expected_root_identity: dict[str, str] | None = None,
) -> None:
    repository = adapter or _repository_adapter(target)
    root_identity = _validate_root_identity(
        expected_root_identity or repository.root_identity
    )
    _assert_current_root_identity(repository, root_identity, "before uninstall cleanup")
    state_backup = _state_action_backup(target, actions, repository)
    committed_state = _read_state(
        repository.display(STATE_FILENAME), target, repository, root_identity,
        allow_legacy=False,
    )
    retained_backups = set(committed_state["backups"]) if committed_state is not None else set()
    prerequisites = [
        action.backup
        for action in actions
        if action.backup is not None and action.backup != state_backup
    ] + [
        backup
        for backup in previous_state["backups"]
        if backup not in retained_backups
    ]
    _cleanup_backup_files(target, prerequisites, repository, root_identity)
    _remove_empty_managed_directories(
        target,
        list(previous_state["createdDirectories"]),
        repository,
        root_identity,
    )
    _assert_current_root_identity(repository, root_identity, "before journal cleanup")
    repository.unlink(_relative(target, journal_path), missing_ok=True)
    try:
        _cleanup_backup_files(target, [state_backup], repository, root_identity)
    except (OSError, WorkflowError):
        pass


def _uninstall_with_adapter(
    adapter: RepositoryAdapter, target: Path, dry_run: bool
) -> str | None:
    root_identity = _validate_root_identity(adapter.root_identity)
    if adapter.exists(JOURNAL_FILENAME, require_file=True):
        raise WorkflowError(
            "Interrupted transaction found. Run the uninstaller with --recover: "
            f"{adapter.display(JOURNAL_FILENAME)}"
        )
    state = _read_state(
        adapter.display(STATE_FILENAME), target, adapter, root_identity, allow_legacy=True
    )
    if state is None:
        print("WARN: no state manifest found; no files were removed")
        print(f"Manual review may be required for {target / INSTALL_DIRECTORY} and the AGENTS.md managed block")
        return None
    if state.get("target") != str(target):
        raise WorkflowError("State manifest target does not match the requested repository")

    requests: list[tuple[str, str, bytes | None]] = []
    remaining_files: dict[str, Any] = {}
    for installed_relative, record in state["files"].items():
        installed_path = adapter.display(installed_relative)
        if not adapter.exists(installed_relative, require_file=True):
            print(f"ALREADY ABSENT: {installed_relative}")
            continue
        if record.get("owner") == "preexisting-identical":
            print(f"PRESERVED PRE-EXISTING FILE: {installed_relative}")
            continue
        if adapter.hash_file(installed_relative) != record.get("installedHash"):
            print(f"PRESERVED MODIFIED FILE: {installed_relative}")
            remaining_files[installed_relative] = record
            continue
        requests.append(("delete", installed_relative, None))
        print(f"DRY-RUN: REMOVE: {installed_relative}" if dry_run else f"REMOVE: {installed_relative}")

    agents_record = state.get("agents")
    remaining_agents = agents_record
    if agents_record:
        agents_relative = adapter.normalize(agents_record.get("path", "AGENTS.md"))
        if not adapter.exists(agents_relative, require_file=True):
            print("ALREADY ABSENT: AGENTS.md")
            remaining_agents = None
        else:
            updated_agents, warning = _remove_agents_content(
                adapter.read_bytes(agents_relative), agents_record
            )
            if warning:
                print(warning)
                if "already absent" in warning:
                    remaining_agents = None
            elif updated_agents == b"":
                requests.append(("delete", agents_relative, None))
                remaining_agents = None
                print("DRY-RUN: REMOVE: generated AGENTS.md" if dry_run else "REMOVE: generated AGENTS.md")
            elif updated_agents is not None:
                requests.append(("write", agents_relative, updated_agents))
                remaining_agents = None
                print("DRY-RUN: REMOVE: AGENTS.md managed block" if dry_run else "REMOVE: AGENTS.md managed block")

    transaction_id = uuid.uuid4().hex
    if remaining_files or remaining_agents:
        remaining_backups = {
            record["backup"]
            for record in remaining_files.values()
            if record.get("backup") is not None
        }
        if remaining_agents is not None and remaining_agents.get("backup") is not None:
            remaining_backups.add(remaining_agents["backup"])
        for obsolete_backup in sorted(set(state["backups"]) - remaining_backups):
            if adapter.exists(obsolete_backup, require_file=True):
                requests.append(("delete", obsolete_backup, None))
        updated_state = dict(state)
        updated_state["schema"] = SCHEMA
        updated_state["rootIdentity"] = root_identity
        updated_state["files"] = remaining_files
        updated_state["agents"] = remaining_agents
        updated_state["backups"] = sorted(remaining_backups)
        updated_state["updatedAt"] = _now()
        updated_state["transactionId"] = transaction_id
        state_content = (json.dumps(updated_state, indent=2, sort_keys=True) + "\n").encode("utf-8")
        requests.append(("write", STATE_FILENAME, state_content))
    else:
        requests.append(("delete", STATE_FILENAME, None))

    if dry_run:
        return f"DRY-RUN PASS: {len(requests)} action(s); no files changed"

    actions = _prepare_relative_actions(adapter, transaction_id, requests)
    _assert_current_root_identity(adapter, root_identity, "before transaction mutation")
    _apply_relative_actions(adapter, JOURNAL_FILENAME, transaction_id, "uninstall", actions)
    _finalize_committed_uninstall(
        target, adapter.display(JOURNAL_FILENAME), actions, state, adapter, root_identity
    )
    if remaining_files or remaining_agents:
        print("WARN: uninstall preserved modified managed content; state manifest retained")
        return None
    return f"PASS: Codex Multi-Agent Workflow uninstalled from {target}"


def uninstall(
    target: Path,
    dry_run: bool,
    adapter: RepositoryAdapter | None = None,
) -> str | None:
    repository = adapter or _repository_adapter(target)
    try:
        success_message = _uninstall_with_adapter(repository, target, dry_run)
    finally:
        if adapter is None:
            repository.close()
    if adapter is None and success_message is not None:
        print(success_message)
    return success_message


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "uninstall"))
    parser.add_argument("--target", default=".", help="Target project directory (default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing files")
    parser.add_argument("--recover", action="store_true", help="Roll back an interrupted transaction")
    parser.add_argument(
        "--with-custom-agents",
        action="store_true",
        help="Install experimental project-level custom agent TOMLs",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    adapter: RepositoryAdapter | None = None
    success_message: str | None = None
    result = 0
    try:
        adapter = _repository_adapter_from_raw_target(args.target)
        target = adapter.root
        journal_path = target / JOURNAL_FILENAME
        if args.recover:
            recover(target, journal_path, args.dry_run, adapter)
        elif args.operation == "install":
            source_root = Path(__file__).resolve().parent.parent
            success_message = install(
                target,
                source_root,
                args.dry_run,
                args.with_custom_agents,
                adapter,
            )
        else:
            success_message = uninstall(target, args.dry_run, adapter)
    except WorkflowError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        result = 1
    except (OSError, UnicodeError) as error:
        print(f"FAIL: filesystem operation failed: {error}", file=sys.stderr)
        result = 1
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except (OSError, WorkflowError) as error:
                print(f"FAIL: {error}", file=sys.stderr)
                result = 1
    if result == 0 and success_message is not None:
        print(success_message)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
