#!/usr/bin/env python3
"""Security boundary for disposable, non-publishing live-validation fixtures."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NoReturn


MARKER_SCHEMA = "codex-live-fixture/v1"
EVIDENCE_SCHEMA = "codex-live-evidence/v1"
EVENT_SCHEMA = "codex-live-event/v1"
SUMMARY_SCHEMA = "codex-live/v1"
CASE_MANIFEST_SCHEMA = "codex-live-case-manifest/v1"
SNAPSHOT_SCHEMA = "codex-live-git-snapshot/v1"
EXPECTED_CHILDREN = ["fixture", "worktree-a", "worktree-b", "remote.git", "results"]
MUTABLE_CHILDREN = ["fixture", "worktree-a", "worktree-b", "remote.git"]
MARKER_NAME = "ownership.json"
LIFECYCLES = {"ready", "running", "interrupted", "cleaning", "cleaned"}
RESULTS = {None, "PASS", "FAIL", "BLOCKED", "UNVERIFIED"}
EXECUTION_STATUSES = {"Not started", "In progress", "Blocked", "Complete"}
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk-[A-Za-z0-9_-]+\b"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|password|token|secret|connection[_ -]?string)\b"
        r"\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(
        r"(?i)\b(?:[A-Z0-9]+_)*(?:API_KEY|SECRET_ACCESS_KEY|ACCESS_TOKEN|"
        r"AUTH_TOKEN|PASSWORD|PRIVATE_KEY|CLIENT_SECRET)\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@"),
)
DEFAULT_CASES = [
    {"id": "fixture-known-failure", "critical": True},
    {"id": "runtime-discovery", "critical": True},
    {"id": "readonly-roles", "critical": True},
    {"id": "implementer-write", "critical": True},
    {"id": "model-routing", "critical": True},
    {"id": "isolated-concurrency", "critical": True},
    {"id": "git-authorization", "critical": True},
    {"id": "windows-restart", "critical": True},
]
CASE_KEYS = {
    "id",
    "critical",
    "executionStatus",
    "result",
    "reason",
    "startedAt",
    "completedAt",
    "durationMs",
    "command",
    "failedAssertion",
    "parentSessionId",
    "childSessionId",
    "configuredModel",
    "observedModel",
    "configuredEffort",
    "observedEffort",
    "effectivePermissions",
    "beforeStateHash",
    "afterStateHash",
    "sourceHashes",
    "installedHashes",
    "evidencePaths",
    "exitCode",
}
CASE_PASS_REQUIREMENTS = {
    "runtime-discovery": {
        "parentSessionId",
        "childSessionId",
        "configuredModel",
        "observedModel",
        "configuredEffort",
        "sourceHashes",
        "installedHashes",
    },
    "readonly-roles": {
        "parentSessionId",
        "childSessionId",
        "sourceHashes",
        "installedHashes",
    },
    "implementer-write": {
        "parentSessionId",
        "childSessionId",
        "sourceHashes",
        "installedHashes",
    },
    "model-routing": {
        "parentSessionId",
        "childSessionId",
        "configuredModel",
        "observedModel",
        "configuredEffort",
        "observedEffort",
        "sourceHashes",
        "installedHashes",
    },
    "isolated-concurrency": {
        "parentSessionId",
        "childSessionId",
        "sourceHashes",
        "installedHashes",
    },
    "git-authorization": {
        "parentSessionId",
        "childSessionId",
        "sourceHashes",
        "installedHashes",
    },
    "windows-restart": {
        "parentSessionId",
        "childSessionId",
        "sourceHashes",
        "installedHashes",
    },
}


class LiveValidationError(RuntimeError):
    """Fail-closed validation error with no implied mutation authority."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fail(message: str) -> NoReturn:
    raise LiveValidationError(message)


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        _fail(f"{field} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise LiveValidationError(f"{field} must be a canonical UUID") from error
    if value != str(parsed):
        _fail(f"{field} must be a canonical UUID")
    return value


def _validate_utc(value: Any, field: str) -> str:
    if not isinstance(value, str):
        _fail(f"{field} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise LiveValidationError(f"{field} must be a UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail(f"{field} must be a UTC timestamp")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_windows_handle_backend() -> Any:
    module_name = (
        f"{__package__}.windows_handle_fs" if __package__ else "windows_handle_fs"
    )
    try:
        return importlib.import_module(module_name)
    except (ImportError, OSError) as error:
        raise LiveValidationError(
            "Windows handle-relative filesystem backend is unavailable"
        ) from error


def _sha256_file_no_follow(path: Path, expected: os.stat_result) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        _fail("No-follow file reads are unsupported on this platform")
    descriptor = os.open(path, flags | no_follow)
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            _fail("Snapshot file identity changed during acquisition")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _contains_secret(value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in SECRET_PATTERNS)


def sanitize_text(value: str, limit: int = 500) -> str:
    sanitized = value
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized[-limit:]


def _ensure_secret_free(value: Any, context: str) -> None:
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if _contains_secret(serialized):
        _fail(f"{context} contains secret-like material")


def _is_reparse(stat_value: os.stat_result) -> bool:
    attributes = getattr(stat_value, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _assert_reparse_free(path: Path) -> None:
    absolute = path.absolute()
    anchor = Path(absolute.anchor)
    current = anchor
    for component in absolute.parts[1:]:
        current /= component
        value = os.lstat(current)
        if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
            _fail(f"Reparse points are forbidden in owned paths: {current}")


def _assert_tree_reparse_free(path: Path) -> None:
    _assert_reparse_free(path)
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        root_path = Path(root)
        for name in directories + files:
            child = root_path / name
            value = os.lstat(child)
            if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
                _fail(f"Reparse points are forbidden in owned trees: {child}")


def canonical_existing_directory(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        _fail("Run root must be absolute")
    if not candidate.is_dir():
        _fail("Run root must be an existing directory")
    canonical = candidate.resolve(strict=True)
    if candidate.absolute() != canonical:
        _fail("Run root must already be canonical")
    _assert_reparse_free(canonical)
    return canonical


def root_identity(path: Path) -> dict[str, str]:
    value = os.lstat(path)
    return {
        "platform": sys.platform,
        "deviceId": str(value.st_dev),
        "fileId": str(value.st_ino),
    }


def _strict_json(path: Path) -> Any:
    if not path.is_file():
        _fail(f"Required JSON file is missing: {path.name}")
    if path.stat().st_size > 2_000_000:
        _fail(f"JSON file is unexpectedly large: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LiveValidationError(f"JSON file is malformed: {path.name}") from error


def atomic_write_json(path: Path, value: Any) -> None:
    _ensure_secret_free(value, path.name)
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_root_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"platform", "deviceId", "fileId"}:
        _fail("Marker root identity is malformed")
    if any(not isinstance(value[key], str) or not value[key] for key in value):
        _fail("Marker root identity is malformed")
    return dict(value)


def _validate_evidence_manifest(value: Any, run_root: Path) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "finalizedAt", "files"}:
        _fail("Evidence manifest is missing or malformed")
    if value.get("schema") != EVIDENCE_SCHEMA:
        _fail("Evidence manifest schema is unsupported")
    _validate_utc(value.get("finalizedAt"), "evidence finalizedAt")
    files = value.get("files")
    if not isinstance(files, list):
        _fail("Evidence manifest files are malformed")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            _fail("Evidence manifest entry is malformed")
        relative = item.get("path")
        digest = item.get("sha256")
        if not isinstance(relative, str) or not re.fullmatch(
            r"results(?:/[A-Za-z0-9._-]+)+", relative
        ):
            _fail("Evidence manifest path is not allowlisted")
        if relative in seen or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            _fail("Evidence manifest entry is not canonical")
        seen.add(relative)
        normalized.append({"path": relative, "sha256": digest})
    if [item["path"] for item in normalized] != sorted(seen):
        _fail("Evidence manifest entries are not sorted")
    actual: dict[str, str] = {}
    results = run_root / "results"
    if not results.is_dir():
        _fail("Results directory is missing")
    _assert_tree_reparse_free(results)
    for path in sorted(candidate for candidate in results.rglob("*") if candidate.is_file()):
        relative = path.relative_to(run_root).as_posix()
        actual[relative] = _sha256_file(path)
    expected = {item["path"]: item["sha256"] for item in normalized}
    if expected != actual:
        _fail("Evidence manifest does not match preserved results")
    return {"schema": EVIDENCE_SCHEMA, "finalizedAt": value["finalizedAt"], "files": normalized}


def validate_marker(
    run_root: str | os.PathLike[str],
    *,
    require_evidence: bool = True,
    allow_active_workers: bool = False,
) -> tuple[Path, dict[str, Any]]:
    root = canonical_existing_directory(run_root)
    marker = _strict_json(root / MARKER_NAME)
    keys = {
        "schema",
        "runId",
        "createdAt",
        "runRoot",
        "rootIdentity",
        "repositoryRevision",
        "expectedChildren",
        "lifecycle",
        "activeWorkers",
        "evidenceManifest",
    }
    if not isinstance(marker, dict) or set(marker) != keys:
        _fail("Ownership marker schema is malformed")
    if marker.get("schema") != MARKER_SCHEMA:
        _fail("Ownership marker schema is unsupported")
    _canonical_uuid(marker.get("runId"), "runId")
    _validate_utc(marker.get("createdAt"), "createdAt")
    if marker.get("runRoot") != str(root):
        _fail("Ownership marker run root does not match")
    identity = _validate_root_identity(marker.get("rootIdentity"))
    if identity != root_identity(root):
        _fail("Ownership marker root identity does not match")
    revision = marker.get("repositoryRevision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        _fail("Ownership marker repository revision is malformed")
    if marker.get("expectedChildren") != EXPECTED_CHILDREN:
        _fail("Ownership marker expected children are not canonical")
    lifecycle = marker.get("lifecycle")
    if lifecycle not in LIFECYCLES:
        _fail("Ownership marker lifecycle is invalid")
    workers = marker.get("activeWorkers")
    if not isinstance(workers, list) or any(
        not isinstance(worker, str) or _canonical_uuid(worker, "active worker") != worker
        for worker in workers
    ) or len(workers) != len(set(workers)):
        _fail("Ownership marker active workers are malformed")
    if workers and not allow_active_workers:
        _fail("Owned fixture still has active workers")
    actual_children = sorted(path.name for path in root.iterdir())
    complete_top = {MARKER_NAME, *EXPECTED_CHILDREN}
    if lifecycle == "cleaned":
        valid_top = set(actual_children) == {MARKER_NAME, "results"}
    elif lifecycle in {"interrupted", "cleaning"}:
        actual_top = set(actual_children)
        valid_top = (
            {MARKER_NAME, "results"}.issubset(actual_top)
            and actual_top.issubset(complete_top)
        )
    else:
        valid_top = set(actual_children) == complete_top
    if not valid_top:
        _fail("Owned fixture has missing or unknown top-level children")
    for child in actual_children:
        _assert_tree_reparse_free(root / child)
    if require_evidence:
        marker["evidenceManifest"] = _validate_evidence_manifest(
            marker.get("evidenceManifest"), root
        )
    elif marker.get("evidenceManifest") is not None and not isinstance(
        marker.get("evidenceManifest"), dict
    ):
        _fail("Evidence manifest is malformed")
    return root, marker


def _manifest_for_results(run_root: Path) -> dict[str, Any]:
    results = run_root / "results"
    files = sorted(
        [
        {
            "path": path.relative_to(run_root).as_posix(),
            "sha256": _sha256_file(path),
        }
        for path in results.rglob("*")
        if path.is_file()
        ],
        key=lambda item: item["path"],
    )
    return {"schema": EVIDENCE_SCHEMA, "finalizedAt": utc_now(), "files": files}


def finalize_evidence(run_root: Path, marker: dict[str, Any]) -> dict[str, Any]:
    marker = dict(marker)
    marker["evidenceManifest"] = _manifest_for_results(run_root)
    atomic_write_json(run_root / MARKER_NAME, marker)
    validate_marker(run_root)
    return marker


def _is_within(root: Path, child: Path) -> bool:
    try:
        child.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except ValueError:
        return False


def _subprocess_environment(run_root: Path) -> dict[str, str]:
    environment: dict[str, str] = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C",
    }
    for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP"):
        value = os.environ.get(key)
        if value is not None:
            environment[key] = value
    environment["HOME"] = str(run_root / "fixture" / ".fixture-home")
    return environment


def run_bounded(
    run_root: Path,
    argv: list[str],
    cwd: Path,
    timeout: int,
    *,
    allowed: str,
) -> subprocess.CompletedProcess[str]:
    if not argv or not all(isinstance(item, str) and item for item in argv):
        _fail("Subprocess argv is malformed")
    if timeout < 1 or timeout > 300:
        _fail("Subprocess timeout is outside the allowed range")
    if not cwd.is_dir() or not _is_within(run_root, cwd):
        _fail("Subprocess cwd escapes the owned fixture")
    _assert_reparse_free(cwd)
    executable = Path(argv[0]).resolve(strict=True)
    if allowed == "git":
        git = shutil.which("git")
        if git is None or executable != Path(git).resolve(strict=True):
            _fail("Only the resolved Git executable is allowed")
    elif allowed == "python":
        if executable != Path(sys.executable).resolve(strict=True):
            _fail("Only the current Python executable is allowed")
    else:
        _fail("Subprocess kind is not allowlisted")
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=_subprocess_environment(run_root),
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise LiveValidationError("Bounded fixture subprocess timed out") from error
    except OSError as error:
        raise LiveValidationError(
            f"Fixture subprocess launch failed: {type(error).__name__}"
        ) from error


def _git(run_root: Path, cwd: Path, arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git")
    if executable is None:
        _fail("Git executable is unavailable")
    return run_bounded(
        run_root,
        [str(Path(executable).resolve()), *arguments],
        cwd,
        timeout,
        allowed="git",
    )


def _git_checked(run_root: Path, cwd: Path, arguments: list[str], timeout: int) -> str:
    result = _git(run_root, cwd, arguments, timeout)
    if result.returncode != 0:
        _fail(f"Fixture-local Git command failed: {sanitize_text(result.stderr)}")
    return result.stdout


def read_repository_revision(source_root: Path) -> str:
    git_path = source_root / ".git"
    if not git_path.is_dir():
        _fail("Source repository metadata is unavailable")
    head = (git_path / "HEAD").read_text(encoding="ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40}", head):
        return head
    if not head.startswith("ref: "):
        _fail("Source repository HEAD is malformed")
    reference = head[5:]
    if not re.fullmatch(r"refs/[A-Za-z0-9._/-]+", reference) or ".." in reference:
        _fail("Source repository HEAD reference is malformed")
    loose = git_path / Path(reference)
    if loose.is_file():
        revision = loose.read_text(encoding="ascii").strip()
        if re.fullmatch(r"[0-9a-f]{40}", revision):
            return revision
    packed = git_path / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="ascii").splitlines():
            if line.endswith(f" {reference}"):
                revision = line.split(" ", 1)[0]
                if re.fullmatch(r"[0-9a-f]{40}", revision):
                    return revision
    _fail("Source repository revision cannot be resolved")


def _write_fixture_sources(fixture: Path) -> None:
    files = {
        "src/calculator.py": (
            "def add(left: int, right: int) -> int:\n"
            "    # Deliberately defective live-validation baseline.\n"
            "    return left - right\n"
        ),
        "src/module_a.py": "VALUE_A = 'baseline-a'\n",
        "src/module_b.py": "VALUE_B = 'baseline-b'\n",
        "tests/test_calculator.py": (
            "from __future__ import annotations\n\n"
            "import sys\n"
            "import unittest\n"
            "from pathlib import Path\n\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n"
            "from calculator import add\n\n\n"
            "class CalculatorTests(unittest.TestCase):\n"
            "    def test_addition(self) -> None:\n"
            "        self.assertEqual(5, add(2, 3))\n\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        ),
        "README.md": (
            "# Disposable live-validation fixture\n\n"
            "This repository is intentionally defective and must never be published.\n"
        ),
        ".gitignore": ".fixture-home/\n.live-cache/\n",
        ".githooks/pre-commit": "#!/bin/sh\nexit 0\n",
        ".live-cache/controlled.txt": "controlled ignored baseline\n",
    }
    for relative, content in files.items():
        path = fixture / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def _parse_nul_records(value: str) -> list[str]:
    return [item for item in value.split("\0") if item]


def _safe_relative(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\x00" in normalized
    ):
        _fail("Git emitted an unsafe repository-relative path")
    return path.as_posix()


def _parse_refs(output: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split("\0")
        if len(parts) != 2 or not re.fullmatch(r"refs/[A-Za-z0-9._/-]+", parts[0]):
            _fail("Git reference output is malformed")
        if not re.fullmatch(r"[0-9a-f]{40}", parts[1]) or parts[0] in refs:
            _fail("Git reference output is malformed")
        refs[parts[0]] = parts[1]
    return dict(sorted(refs.items()))


def _snapshot_checkout_inventory(checkout: Path) -> dict[str, Any]:
    if not checkout.is_dir():
        _fail(f"Snapshot checkout is missing: {checkout.name}")
    _assert_reparse_free(checkout)
    native = None
    if sys.platform == "win32":
        native = _load_windows_handle_backend().RepositoryFs(str(checkout))
    directories: list[str] = []
    files: dict[str, str] = {}
    pending: list[tuple[Path, str]] = [(checkout, "")]
    try:
        while pending:
            directory, directory_relative = pending.pop()
            if native is not None and directory_relative:
                native.directory_identity(directory_relative)
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
            except OSError as error:
                raise LiveValidationError(
                    f"Snapshot checkout cannot be enumerated: {checkout.name}"
                ) from error
            for entry in entries:
                relative = Path(entry.path).relative_to(checkout).as_posix()
                if relative == ".git":
                    continue
                value = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
                    _fail(f"Snapshot checkout contains a reparse point: {relative}")
                if stat.S_ISDIR(value.st_mode):
                    directories.append(relative)
                    pending.append((Path(entry.path), relative))
                elif stat.S_ISREG(value.st_mode):
                    files[relative] = (
                        native.hash_file(relative)
                        if native is not None
                        else _sha256_file_no_follow(Path(entry.path), value)
                    )
                else:
                    _fail(f"Snapshot checkout contains an unsupported entry: {relative}")
        return {
            "directories": sorted(directories),
            "files": dict(sorted(files.items())),
        }
    finally:
        if native is not None:
            native.close()


def _snapshot_index(run_root: Path, fixture: Path, timeout: int) -> list[dict[str, str]]:
    output = _git_checked(run_root, fixture, ["ls-files", "--stage", "-z"], timeout)
    entries: list[dict[str, str]] = []
    for record in _parse_nul_records(output):
        metadata, separator, relative = record.partition("\t")
        fields = metadata.split(" ")
        if separator != "\t" or len(fields) != 3:
            _fail("Git index output is malformed")
        mode, object_id, stage = fields
        if not re.fullmatch(r"[0-7]{6}", mode) or not re.fullmatch(r"[0-9a-f]{40}", object_id):
            _fail("Git index output is malformed")
        if stage not in {"0", "1", "2", "3"}:
            _fail("Git index output is malformed")
        entries.append(
            {"path": _safe_relative(relative), "mode": mode, "objectId": object_id, "stage": stage}
        )
    return sorted(entries, key=lambda item: (item["path"], item["stage"]))


def _snapshot_config(run_root: Path, fixture: Path, timeout: int) -> dict[str, str]:
    output = _git_checked(run_root, fixture, ["config", "--local", "--list", "--null"], timeout)
    allowed_exact = {
        "user.name",
        "user.email",
        "core.hookspath",
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.symlinks",
        "core.ignorecase",
        "remote.origin.url",
        "remote.origin.fetch",
    }
    config: dict[str, str] = {}
    for record in _parse_nul_records(output):
        key, separator, value = record.partition("\n")
        allowed = key in allowed_exact or (
            key.startswith("branch.") and key.endswith((".remote", ".merge"))
        )
        if not separator or not allowed or key in config:
            _fail("Fixture-local Git config contains an unexpected key")
        if key == "remote.origin.url":
            remote = Path(value).resolve(strict=True)
            if remote != (run_root / "remote.git").resolve(strict=True):
                _fail("Fixture remote escapes the owned run root")
            value = "RUN_ROOT/remote.git"
        if _contains_secret(value):
            _fail("Fixture-local Git config contains secret-like material")
        config[key] = value
    return dict(sorted(config.items()))


def _snapshot_worktrees(run_root: Path, fixture: Path, timeout: int) -> list[dict[str, str | None]]:
    output = _git_checked(run_root, fixture, ["worktree", "list", "--porcelain"], timeout)
    records: list[dict[str, str | None]] = []
    current: dict[str, str | None] = {}
    for line in [*output.splitlines(), ""]:
        if not line:
            if current:
                if set(current) != {"path", "head", "branch"}:
                    _fail("Git worktree output is incomplete")
                records.append(current)
                current = {}
            continue
        key, separator, value = line.partition(" ")
        if not separator or key not in {"worktree", "HEAD", "branch"}:
            _fail("Git worktree output is malformed")
        if key == "worktree":
            path = Path(value).resolve(strict=True)
            if not _is_within(run_root, path):
                _fail("Git worktree escapes the owned run root")
            current["path"] = path.relative_to(run_root).as_posix()
        elif key == "HEAD":
            if not re.fullmatch(r"[0-9a-f]{40}", value):
                _fail("Git worktree HEAD is malformed")
            current["head"] = value
        else:
            if not re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", value):
                _fail("Git worktree branch is malformed")
            current["branch"] = value
    return sorted(records, key=lambda item: str(item["path"]))


def capture_git_snapshot(run_root: Path, timeout: int = 30) -> dict[str, Any]:
    fixture = run_root / "fixture"
    head = _git_checked(run_root, fixture, ["rev-parse", "HEAD"], timeout).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        _fail("Fixture HEAD is malformed")
    refs = _parse_refs(
        _git_checked(
            run_root,
            fixture,
            ["for-each-ref", "--format=%(refname)%00%(objectname)", "refs"],
            timeout,
        )
    )
    remote_refs = _parse_refs(
        _git_checked(
            run_root,
            run_root / "remote.git",
            ["for-each-ref", "--format=%(refname)%00%(objectname)", "refs"],
            timeout,
        )
    )
    status = _parse_nul_records(
        _git_checked(
            run_root,
            fixture,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching"],
            timeout,
        )
    )
    status = sorted(sanitize_text(item, 300) for item in status)
    checkout_inventories = {
        name: _snapshot_checkout_inventory(run_root / name)
        for name in ("fixture", "worktree-a", "worktree-b")
    }
    hooks = {
        relative: digest
        for relative, digest in checkout_inventories["fixture"]["files"].items()
        if relative.startswith(".githooks/")
    }
    snapshot: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "capturedAt": utc_now(),
        "head": head,
        "refs": refs,
        "index": _snapshot_index(run_root, fixture, timeout),
        "status": status,
        "fileHashes": checkout_inventories["fixture"]["files"],
        "checkoutInventories": checkout_inventories,
        "worktrees": _snapshot_worktrees(run_root, fixture, timeout),
        "remoteRefs": remote_refs,
        "localConfig": _snapshot_config(run_root, fixture, timeout),
        "hooks": hooks,
    }
    _ensure_secret_free(snapshot, "Git snapshot")
    return snapshot


def default_case_manifest() -> dict[str, Any]:
    return {"schema": CASE_MANIFEST_SCHEMA, "cases": [dict(item) for item in DEFAULT_CASES]}


def _marker_value(run_root: Path, revision: str, run_id: str, worker_id: str) -> dict[str, Any]:
    return {
        "schema": MARKER_SCHEMA,
        "runId": run_id,
        "createdAt": utc_now(),
        "runRoot": str(run_root),
        "rootIdentity": root_identity(run_root),
        "repositoryRevision": revision,
        "expectedChildren": EXPECTED_CHILDREN,
        "lifecycle": "running",
        "activeWorkers": [worker_id],
        "evidenceManifest": None,
    }


def create_fixture(
    source_root: Path,
    *,
    allow_live: bool = False,
    temp_base: Path | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    preview = {
        "mode": "preview",
        "writes": False,
        "subprocesses": False,
        "actions": [
            "create unique OS-temporary owned root",
            "create defective Python fixture and local-only Git topology",
            "capture strict baseline snapshot and finalize evidence manifest",
        ],
        "runtimeValidated": False,
    }
    if not allow_live:
        return preview
    base = canonical_existing_directory(temp_base or Path(tempfile.gettempdir()))
    resolved_source = source_root.resolve(strict=True)
    revision = read_repository_revision(resolved_source)
    run_id = str(uuid.uuid4())
    worker_id = str(uuid.uuid4())
    run_root = Path(tempfile.mkdtemp(prefix="codex-live-", dir=base)).resolve(strict=True)
    _assert_reparse_free(run_root)
    marker = _marker_value(run_root, revision, run_id, worker_id)
    marker_written = False
    try:
        atomic_write_json(run_root / MARKER_NAME, marker)
        marker_written = True
        for child in EXPECTED_CHILDREN:
            (run_root / child).mkdir()
        fixture = run_root / "fixture"
        _write_fixture_sources(fixture)
        (fixture / ".fixture-home").mkdir()
        _git_checked(run_root, fixture, ["init", "--initial-branch=main"], timeout)
        _git_checked(run_root, fixture, ["config", "user.name", "Codex Live Fixture"], timeout)
        _git_checked(run_root, fixture, ["config", "user.email", "fixture@example.invalid"], timeout)
        _git_checked(run_root, fixture, ["config", "core.hooksPath", ".githooks"], timeout)
        _git_checked(run_root, fixture, ["add", "--all"], timeout)
        _git_checked(run_root, fixture, ["commit", "-m", "fixture baseline"], timeout)
        _git_checked(
            run_root,
            run_root / "remote.git",
            ["init", "--bare"],
            timeout,
        )
        _git_checked(
            run_root,
            fixture,
            ["remote", "add", "origin", str(run_root / "remote.git")],
            timeout,
        )
        _git_checked(run_root, fixture, ["push", "origin", "main"], timeout)
        _git_checked(
            run_root,
            fixture,
            ["worktree", "add", "-b", "worker-a", str(run_root / "worktree-a"), "main"],
            timeout,
        )
        _git_checked(
            run_root,
            fixture,
            ["worktree", "add", "-b", "worker-b", str(run_root / "worktree-b"), "main"],
            timeout,
        )
        git_version = sanitize_text(
            _git_checked(run_root, fixture, ["--version"], timeout).strip(), 100
        )
        configuration_path = resolved_source / "compatibility" / "codex-agents.json"
        if not configuration_path.is_file():
            _fail("Versioned agent compatibility configuration is missing")
        environment = {
            "schema": "codex-live-environment/v1",
            "runId": run_id,
            "platform": sys.platform,
            "pythonVersion": sys.version.split()[0],
            "gitVersion": git_version,
            "codexVersion": "UNVERIFIED",
            "configurationHash": _sha256_file(configuration_path),
            "runtimeValidated": False,
        }
        atomic_write_json(run_root / "results" / "environment.json", environment)
        atomic_write_json(
            run_root / "results" / "case-manifest.json", default_case_manifest()
        )
        snapshots = run_root / "results" / "snapshots"
        snapshots.mkdir()
        atomic_write_json(
            snapshots / "baseline.json", capture_git_snapshot(run_root, timeout)
        )
        marker["lifecycle"] = "ready"
        marker["activeWorkers"] = []
        marker = finalize_evidence(run_root, marker)
    except BaseException as error:
        if not marker_written:
            raise LiveValidationError(
                "Fixture setup failed before the ownership marker could be established; "
                f"the empty run root was retained at {run_root}: {type(error).__name__}"
            ) from error
        marker["lifecycle"] = "interrupted"
        marker["activeWorkers"] = []
        try:
            (run_root / "results").mkdir(exist_ok=True)
            atomic_write_json(
                run_root / "results" / "interruption.json",
                {
                    "schema": "codex-live-interruption/v1",
                    "occurredAt": utc_now(),
                    "errorType": type(error).__name__,
                },
            )
            finalize_evidence(run_root, marker)
        except BaseException:
            atomic_write_json(run_root / MARKER_NAME, marker)
        raise LiveValidationError(
            f"Fixture setup was interrupted and retained at {run_root}: {type(error).__name__}"
        ) from error
    return {
        "mode": "created",
        "runId": run_id,
        "runRoot": str(run_root),
        "repositoryRevision": revision,
        "runtimeValidated": False,
    }


def _remove_owned_tree(
    path: Path,
    *,
    filesystem: Any | None = None,
    relative: str | None = None,
) -> None:
    if filesystem is not None:
        if not isinstance(relative, str) or not relative:
            _fail("Native cleanup relative path is missing")
        value = os.lstat(path)
        if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
            _fail(f"Refusing to remove a reparse point: {path.name}")
        if stat.S_ISDIR(value.st_mode):
            identity = filesystem.directory_identity(relative)
            with os.scandir(path) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
            for entry in entries:
                child_path = Path(entry.path)
                child_relative = f"{relative}/{entry.name}"
                child_value = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(child_value.st_mode) or _is_reparse(child_value):
                    _fail(f"Refusing to remove a reparse point: {entry.name}")
                if stat.S_ISDIR(child_value.st_mode):
                    _remove_owned_tree(
                        child_path,
                        filesystem=filesystem,
                        relative=child_relative,
                    )
                elif stat.S_ISREG(child_value.st_mode):
                    filesystem.unlink(child_relative)
                else:
                    _fail(f"Refusing to remove unsupported entry: {entry.name}")
            filesystem.rmdir(relative, expected_identity=identity)
            return
        if stat.S_ISREG(value.st_mode):
            filesystem.unlink(relative)
            return
        _fail(f"Refusing to remove unsupported entry: {path.name}")
    value = os.lstat(path)
    if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
        _fail(f"Refusing to remove a reparse point: {path.name}")
    if stat.S_ISDIR(value.st_mode):
        for child in list(os.scandir(path)):
            _remove_owned_tree(Path(child.path))
        os.chmod(path, stat.S_IRWXU)
        path.rmdir()
    else:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        path.unlink()


def _revalidate_cleanup_state(
    root: Path, expected_marker: dict[str, Any]
) -> dict[str, Any]:
    observed_root, observed = validate_marker(root)
    if observed_root != root:
        _fail("Cleanup root changed during validation")
    for field in (
        "schema",
        "runId",
        "createdAt",
        "runRoot",
        "rootIdentity",
        "repositoryRevision",
        "expectedChildren",
        "evidenceManifest",
    ):
        if observed.get(field) != expected_marker.get(field):
            _fail("Cleanup ownership marker changed during validation")
    if observed.get("lifecycle") != "cleaning" or observed.get("activeWorkers") != []:
        _fail("Cleanup lifecycle changed during validation")
    return observed


def cleanup_fixture(run_root: Path, *, apply_cleanup: bool = False) -> dict[str, Any]:
    root, marker = validate_marker(run_root)
    if marker["lifecycle"] == "cleaned":
        return {
            "mode": "already-cleaned",
            "runRoot": str(root),
            "deleted": [],
            "preserved": [MARKER_NAME, "results"],
        }
    preview = {
        "mode": "preview",
        "runRoot": str(root),
        "deleted": list(MUTABLE_CHILDREN),
        "preserved": [MARKER_NAME, "results"],
    }
    if not apply_cleanup:
        return preview
    marker["lifecycle"] = "cleaning"
    marker["activeWorkers"] = []
    atomic_write_json(root / MARKER_NAME, marker)
    marker = _revalidate_cleanup_state(root, marker)
    filesystem = None
    try:
        if sys.platform == "win32":
            filesystem = _load_windows_handle_backend().RepositoryFs(str(root))
        for child in MUTABLE_CHILDREN:
            marker = _revalidate_cleanup_state(root, marker)
            candidate = root / child
            if candidate.parent != root or child not in EXPECTED_CHILDREN:
                _fail("Cleanup target is not allowlisted")
            try:
                value = os.lstat(candidate)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
                _fail(f"Refusing to remove a reparse point: {child}")
            if filesystem is None:
                _assert_tree_reparse_free(candidate)
                _remove_owned_tree(candidate)
            else:
                _remove_owned_tree(
                    candidate,
                    filesystem=filesystem,
                    relative=child,
                )
        marker = _revalidate_cleanup_state(root, marker)
        marker["lifecycle"] = "cleaned"
        atomic_write_json(root / MARKER_NAME, marker)
        validate_marker(root)
    finally:
        if filesystem is not None:
            filesystem.close()
    return {
        "mode": "cleaned",
        "runRoot": str(root),
        "deleted": list(MUTABLE_CHILDREN),
        "preserved": [MARKER_NAME, "results"],
    }


def validate_case_manifest(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"schema", "cases"}:
        _fail("Case manifest schema is malformed")
    if value.get("schema") != CASE_MANIFEST_SCHEMA or not isinstance(value.get("cases"), list):
        _fail("Case manifest schema is unsupported")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value["cases"]:
        if not isinstance(item, dict) or set(item) != {"id", "critical"}:
            _fail("Case manifest entry is malformed")
        case_id = item.get("id")
        if (
            not isinstance(case_id, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", case_id)
            or case_id in seen
            or type(item.get("critical")) is not bool
        ):
            _fail("Case manifest entry is not canonical")
        seen.add(case_id)
        cases.append({"id": case_id, "critical": item["critical"]})
    if not cases:
        _fail("Case manifest must not be empty")
    return cases


def blank_case(case_id: str, critical: bool) -> dict[str, Any]:
    return {
        "id": case_id,
        "critical": critical,
        "executionStatus": "Not started",
        "result": None,
        "reason": "Not executed",
        "startedAt": None,
        "completedAt": None,
        "durationMs": None,
        "command": None,
        "failedAssertion": None,
        "parentSessionId": None,
        "childSessionId": None,
        "configuredModel": None,
        "observedModel": None,
        "configuredEffort": None,
        "observedEffort": None,
        "effectivePermissions": [],
        "beforeStateHash": None,
        "afterStateHash": None,
        "sourceHashes": {},
        "installedHashes": {},
        "evidencePaths": [],
        "exitCode": None,
    }


def _validate_hash_mapping(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        _fail(f"{field} must be an object")
    normalized: dict[str, str] = {}
    for key, digest in value.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", key)
            or ".." in Path(key).parts
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            _fail(f"{field} contains a malformed hash")
        normalized[key] = digest
    return dict(sorted(normalized.items()))


def _require_attempt_field(value: dict[str, Any], field: str) -> None:
    item = value[field]
    if item is None or item == [] or item == {} or item == "":
        _fail(f"Attempted case requires {field}")


def _validate_pass_requirements(value: dict[str, Any]) -> None:
    requirements = CASE_PASS_REQUIREMENTS.get(value["id"], set())
    for field in sorted(requirements):
        _require_attempt_field(value, field)
    for field in {"parentSessionId", "childSessionId"} & requirements:
        _canonical_uuid(value[field], field)


def validate_case_record(value: Any, expected: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != CASE_KEYS:
        _fail("Case result schema is malformed")
    if (
        value.get("id") != expected["id"]
        or type(value.get("critical")) is not bool
        or value.get("critical") is not expected["critical"]
    ):
        _fail("Case result identity conflicts with the manifest")
    status = value.get("executionStatus")
    result = value.get("result")
    if status not in EXECUTION_STATUSES or result not in RESULTS:
        _fail("Case result status is invalid")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason or len(reason) > 500 or _contains_secret(reason):
        _fail("Case result reason is invalid")
    timestamps: dict[str, datetime] = {}
    for field in ("startedAt", "completedAt"):
        if value[field] is not None:
            _validate_utc(value[field], field)
            timestamps[field] = datetime.fromisoformat(value[field])
    if (
        "startedAt" in timestamps
        and "completedAt" in timestamps
        and timestamps["completedAt"] < timestamps["startedAt"]
    ):
        _fail("Case completion precedes its start")
    duration = value.get("durationMs")
    if duration is not None and (type(duration) is not int or duration < 0 or duration > 3_600_000):
        _fail("Case duration is invalid")
    for field in (
        "command",
        "failedAssertion",
        "parentSessionId",
        "childSessionId",
        "configuredModel",
        "observedModel",
        "configuredEffort",
        "observedEffort",
        "beforeStateHash",
        "afterStateHash",
    ):
        item = value[field]
        if item is not None and (not isinstance(item, str) or len(item) > 500 or _contains_secret(item)):
            _fail(f"Case field {field} is invalid")
    for field in ("beforeStateHash", "afterStateHash"):
        if value[field] is not None and not re.fullmatch(r"[0-9a-f]{64}", value[field]):
            _fail(f"Case field {field} is not a SHA-256 digest")
    permissions = value.get("effectivePermissions")
    if not isinstance(permissions, list) or any(
        not isinstance(item, str) or not re.fullmatch(r"[a-z-]+", item)
        for item in permissions
    ) or len(permissions) != len(set(permissions)):
        _fail("Case effective permissions are malformed")
    evidence = value.get("evidencePaths")
    if not isinstance(evidence, list) or any(
        not isinstance(item, str)
        or not re.fullmatch(r"results(?:/[A-Za-z0-9._-]+)+", item)
        for item in evidence
    ) or evidence != sorted(set(evidence)):
        _fail("Case evidence paths are malformed")
    exit_code = value.get("exitCode")
    if exit_code is not None and (type(exit_code) is not int or exit_code < 0 or exit_code > 255):
        _fail("Case exit code is invalid")
    value["sourceHashes"] = _validate_hash_mapping(
        value.get("sourceHashes"), "sourceHashes"
    )
    value["installedHashes"] = _validate_hash_mapping(
        value.get("installedHashes"), "installedHashes"
    )
    if status == "Not started":
        blank = blank_case(value["id"], value["critical"])
        if any(value[field] != blank[field] for field in CASE_KEYS - {"id", "critical"}):
            _fail("Not-started case has contradictory evidence")
    elif status == "In progress":
        for field in ("startedAt", "command", "effectivePermissions", "beforeStateHash"):
            _require_attempt_field(value, field)
        if (
            result is not None
            or value["completedAt"] is not None
            or duration is not None
            or value["afterStateHash"] is not None
            or evidence
            or exit_code is not None
            or value["failedAssertion"] is not None
        ):
            _fail("In-progress case has contradictory terminal evidence")
    elif status == "Complete":
        if result not in {"PASS", "FAIL", "UNVERIFIED"} or exit_code is None:
            _fail("Completed case has contradictory status")
        for field in (
            "startedAt",
            "completedAt",
            "durationMs",
            "command",
            "effectivePermissions",
            "beforeStateHash",
            "afterStateHash",
            "evidencePaths",
        ):
            _require_attempt_field(value, field)
        if result == "PASS":
            if exit_code != 0 or value["failedAssertion"] is not None:
                _fail("Passing case has contradictory outcome evidence")
            _validate_pass_requirements(value)
        elif result == "FAIL":
            if exit_code == 0 or value["failedAssertion"] is None:
                _fail("Failing case lacks a failed assertion or failure exit code")
    elif status == "Blocked":
        if result != "BLOCKED":
            _fail("Blocked case has contradictory result")
        for field in (
            "startedAt",
            "completedAt",
            "durationMs",
            "command",
            "effectivePermissions",
            "beforeStateHash",
            "afterStateHash",
            "evidencePaths",
        ):
            _require_attempt_field(value, field)
    return dict(value)


def _validate_evidence_paths(record: dict[str, Any], run_root: Path | None) -> list[str]:
    errors: list[str] = []
    if record["result"] == "PASS" and not record["evidencePaths"]:
        errors.append(f"MISSING_EVIDENCE:{record['id']}")
    if run_root is None:
        return errors
    for relative in record["evidencePaths"]:
        path = run_root / Path(relative)
        if not path.is_file():
            errors.append(f"MISSING_EVIDENCE:{record['id']}")
            continue
        try:
            path.resolve(strict=True).relative_to((run_root / "results").resolve(strict=True))
            value = os.lstat(path)
            if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
                errors.append(f"INVALID_EVIDENCE:{record['id']}")
        except (OSError, ValueError):
            errors.append(f"INVALID_EVIDENCE:{record['id']}")
    return errors


def parse_event_jsonl(text: str) -> list[dict[str, Any]]:
    if text and not text.endswith("\n"):
        _fail("Sanitized event stream is truncated")
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if _contains_secret(line):
            _fail("Sanitized event stream contains secret-like material")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise LiveValidationError("Sanitized event stream is malformed") from error
        if not isinstance(event, dict) or set(event) != {"schema", "runId", "case"}:
            _fail("Sanitized event schema is malformed")
        if event.get("schema") != EVENT_SCHEMA:
            _fail("Sanitized event schema is unsupported")
        _canonical_uuid(event.get("runId"), "event runId")
        if not isinstance(event.get("case"), dict):
            _fail("Sanitized event case is malformed")
        events.append(event)
    return events


def aggregate_events(
    text: str,
    case_manifest: dict[str, Any],
    *,
    run_id: str,
    platform: str,
    codex_version: str,
    repository_commit: str,
    configuration_hash: str,
    run_root: Path | None = None,
) -> dict[str, Any]:
    _canonical_uuid(run_id, "summary runId")
    cases = validate_case_manifest(case_manifest)
    expected = {item["id"]: item for item in cases}
    events = parse_event_jsonl(text)
    records: dict[str, dict[str, Any]] = {}
    errors: set[str] = set()
    for event in events:
        if event["runId"] != run_id:
            errors.add("RUN_ID_MISMATCH")
            continue
        case_value = event["case"]
        case_id = case_value.get("id")
        if case_id not in expected:
            errors.add(f"UNKNOWN_CASE:{case_id}")
            continue
        if case_id in records:
            errors.add(f"DUPLICATE_CASE:{case_id}")
            continue
        record = validate_case_record(case_value, expected[case_id])
        records[case_id] = record
        errors.update(_validate_evidence_paths(record, run_root))
    ordered: list[dict[str, Any]] = []
    for item in cases:
        case_id = item["id"]
        if case_id not in records:
            errors.add(f"MISSING_CASE:{case_id}")
            record = blank_case(case_id, item["critical"])
        else:
            record = records[case_id]
        if record["result"] != "PASS":
            errors.add(f"NON_PASS:{case_id}")
        ordered.append(record)
    errors.add("REAL_EVENT_ADAPTER_UNVALIDATED")
    summary = {
        "schemaVersion": SUMMARY_SCHEMA,
        "runId": run_id,
        "platform": platform,
        "codexVersion": codex_version,
        "repositoryCommit": repository_commit,
        "configurationHash": configuration_hash,
        "generatedAt": utc_now(),
        "expectedCaseIds": [item["id"] for item in cases],
        "tests": ordered,
        "validationErrors": sorted(errors),
        "runtimeValidated": False,
        "releaseDecision": "NOT READY",
    }
    validate_summary(summary)
    return summary


def validate_summary(value: Any) -> dict[str, Any]:
    keys = {
        "schemaVersion",
        "runId",
        "platform",
        "codexVersion",
        "repositoryCommit",
        "configurationHash",
        "generatedAt",
        "expectedCaseIds",
        "tests",
        "validationErrors",
        "runtimeValidated",
        "releaseDecision",
    }
    if not isinstance(value, dict) or set(value) != keys:
        _fail("Live-validation summary schema is malformed")
    if value.get("schemaVersion") != SUMMARY_SCHEMA:
        _fail("Live-validation summary schema is unsupported")
    _canonical_uuid(value.get("runId"), "summary runId")
    _validate_utc(value.get("generatedAt"), "summary generatedAt")
    for field in ("platform", "codexVersion"):
        if not isinstance(value.get(field), str) or not value[field] or _contains_secret(value[field]):
            _fail(f"Summary field {field} is invalid")
    if not isinstance(value.get("repositoryCommit"), str) or not re.fullmatch(
        r"[0-9a-f]{40}", value["repositoryCommit"]
    ):
        _fail("Summary repository commit is invalid")
    if not isinstance(value.get("configurationHash"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["configurationHash"]
    ):
        _fail("Summary configuration hash is invalid")
    expected_ids = value.get("expectedCaseIds")
    if not isinstance(expected_ids, list) or any(
        not isinstance(item, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", item)
        for item in expected_ids
    ) or len(expected_ids) != len(set(expected_ids)):
        _fail("Summary expected case IDs are malformed")
    tests = value.get("tests")
    if not isinstance(tests, list) or len(tests) != len(expected_ids):
        _fail("Summary test matrix is incomplete")
    normalized: list[dict[str, Any]] = []
    for index, case_id in enumerate(expected_ids):
        record = tests[index]
        if not isinstance(record, dict):
            _fail("Summary case is malformed")
        normalized.append(
            validate_case_record(
                record,
                {"id": case_id, "critical": record.get("critical")},
            )
        )
    errors = value.get("validationErrors")
    if not isinstance(errors, list) or any(
        not isinstance(item, str) or not item or len(item) > 300 or _contains_secret(item)
        for item in errors
    ) or errors != sorted(set(errors)):
        _fail("Summary validation errors are malformed")
    allowed_error = re.compile(
        r"(?:REAL_EVENT_ADAPTER_UNVALIDATED|RUN_ID_MISMATCH|"
        r"(?:MISSING_CASE|UNKNOWN_CASE|DUPLICATE_CASE|NON_PASS|"
        r"MISSING_EVIDENCE|INVALID_EVIDENCE):[a-z0-9]+(?:-[a-z0-9]+)*)"
    )
    if any(allowed_error.fullmatch(error) is None for error in errors):
        _fail("Summary contains an unknown validation error")
    if value.get("runtimeValidated") is not False:
        _fail("V2 summaries cannot claim runtime validation")
    if value.get("releaseDecision") != "NOT READY":
        _fail("V2 summaries must remain NOT READY")
    if "REAL_EVENT_ADAPTER_UNVALIDATED" not in errors:
        _fail("Summary omits the unvalidated real-event adapter limitation")
    for record in normalized:
        non_pass = f"NON_PASS:{record['id']}"
        if record["result"] != "PASS" and non_pass not in errors:
            _fail("Summary omits a non-PASS validation error")
        if record["result"] == "PASS" and non_pass in errors:
            _fail("Summary contains a contradictory non-PASS validation error")
    _ensure_secret_free(value, "Live-validation summary")
    return dict(value)


def _snapshot_digest(snapshot: dict[str, Any]) -> str:
    stable = {key: value for key, value in snapshot.items() if key != "capturedAt"}
    return _sha256_bytes(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _write_secret_free_text(path: Path, content: str) -> None:
    if _contains_secret(content):
        _fail(f"Refusing to persist secret-like material in {path.name}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _event_text(run_id: str, cases: Iterable[dict[str, Any]]) -> str:
    lines = [
        json.dumps(
            {"schema": EVENT_SCHEMA, "runId": run_id, "case": case},
            sort_keys=True,
            separators=(",", ":"),
        )
        for case in cases
    ]
    text = "\n".join(lines) + "\n"
    if _contains_secret(text):
        _fail("Synthetic event stream contains secret-like material")
    return text


def run_behavior_tests(
    run_root: Path,
    *,
    allow_live: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    root, marker = validate_marker(run_root)
    if marker["lifecycle"] != "ready":
        _fail("Fixture must be ready before running behavior tests")
    if not allow_live:
        return {
            "mode": "preview",
            "runRoot": str(root),
            "writes": False,
            "subprocesses": False,
            "actions": [
                "run known-defective fixture test with bounded fixture-local Python",
                "capture before/after Git snapshots without process streams",
                "write sanitized synthetic events and a NOT READY summary",
            ],
            "runtimeValidated": False,
        }
    worker_id = str(uuid.uuid4())
    marker["lifecycle"] = "running"
    marker["activeWorkers"] = [worker_id]
    atomic_write_json(root / MARKER_NAME, marker)
    started = utc_now()
    started_clock = time.monotonic()
    try:
        before = capture_git_snapshot(root, timeout)
        before_digest = _snapshot_digest(before)
        process = run_bounded(
            root,
            [
                str(Path(sys.executable).resolve()),
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ],
            root / "fixture",
            timeout,
            allowed="python",
        )
        after = capture_git_snapshot(root, timeout)
        after_digest = _snapshot_digest(after)
        duration_ms = int((time.monotonic() - started_clock) * 1000)
        expected_failure = process.returncode != 0
        unchanged = before_digest == after_digest
        passed = expected_failure and unchanged
        case_evidence = {
            "schema": "codex-live-case-evidence/v1",
            "caseId": "fixture-known-failure",
            "expectedFailureDetected": expected_failure,
            "fixtureExitCode": process.returncode,
            "stateUnchanged": unchanged,
            "beforeStateHash": before_digest,
            "afterStateHash": after_digest,
            "capturedAt": utc_now(),
            "streamsPersisted": False,
        }
        cases_directory = root / "results" / "cases"
        cases_directory.mkdir(exist_ok=True)
        evidence_relative = "results/cases/fixture-known-failure.json"
        atomic_write_json(root / evidence_relative, case_evidence)
        snapshots = root / "results" / "snapshots"
        atomic_write_json(snapshots / "before-behavior.json", before)
        atomic_write_json(snapshots / "after-behavior.json", after)
        manifest_value = _strict_json(root / "results" / "case-manifest.json")
        manifest_cases = validate_case_manifest(manifest_value)
        records: list[dict[str, Any]] = []
        for expected in manifest_cases:
            record = blank_case(expected["id"], expected["critical"])
            if expected["id"] == "fixture-known-failure":
                record.update(
                    {
                        "executionStatus": "Complete",
                        "result": "PASS" if passed else "FAIL",
                        "reason": (
                            "Expected failing fixture test detected without repository drift"
                            if passed
                            else "Known-defective fixture behavior was not detected cleanly"
                        ),
                        "startedAt": started,
                        "completedAt": utc_now(),
                        "durationMs": duration_ms,
                        "command": "python -B -m unittest discover -s tests -v",
                        "failedAssertion": None if passed else "expected failure and unchanged snapshot",
                        "effectivePermissions": ["fixture-read"],
                        "beforeStateHash": before_digest,
                        "afterStateHash": after_digest,
                        "evidencePaths": [evidence_relative],
                        "exitCode": 0 if passed else 1,
                    }
                )
            records.append(record)
        event_text = _event_text(marker["runId"], records)
        _write_secret_free_text(root / "results" / "events.sanitized.jsonl", event_text)
        environment = _strict_json(root / "results" / "environment.json")
        summary = aggregate_events(
            event_text,
            manifest_value,
            run_id=marker["runId"],
            platform=str(environment["platform"]),
            codex_version=str(environment["codexVersion"]),
            repository_commit=marker["repositoryRevision"],
            configuration_hash=str(environment["configurationHash"]),
            run_root=root,
        )
        atomic_write_json(root / "results" / "live-validation-summary.json", summary)
        marker["lifecycle"] = "ready"
        marker["activeWorkers"] = []
        finalize_evidence(root, marker)
        return {
            "mode": "synthetic-complete",
            "runRoot": str(root),
            "fixtureKnownFailure": "PASS" if passed else "FAIL",
            "releaseDecision": "NOT READY",
            "runtimeValidated": False,
        }
    except BaseException as error:
        marker["lifecycle"] = "interrupted"
        marker["activeWorkers"] = []
        try:
            atomic_write_json(
                root / "results" / "runner-interruption.json",
                {
                    "schema": "codex-live-interruption/v1",
                    "occurredAt": utc_now(),
                    "errorType": type(error).__name__,
                },
            )
            finalize_evidence(root, marker)
        except BaseException:
            atomic_write_json(root / MARKER_NAME, marker)
        raise


REPORT_PLACEHOLDERS = {
    "RUN_ID",
    "GENERATED_AT",
    "PLATFORM",
    "CODEX_VERSION",
    "REPOSITORY_COMMIT",
    "CONFIGURATION_HASH",
    "RESULT_ROWS",
    "VALIDATION_ERRORS",
    "RELEASE_DECISION",
    "RUNTIME_VALIDATED",
}


def _markdown(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_report(summary: dict[str, Any], template: str) -> str:
    validated = validate_summary(summary)
    placeholders = set(re.findall(r"\{\{([A-Z_]+)\}\}", template))
    if placeholders != REPORT_PLACEHOLDERS:
        _fail("Report template placeholders are not the exact allowlist")
    rows = "\n".join(
        "| "
        + " | ".join(
            [
                _markdown(record["id"]),
                _markdown(record["executionStatus"]),
                _markdown(record["result"] if record["result"] is not None else "—"),
                _markdown(record["reason"]),
            ]
        )
        + " |"
        for record in validated["tests"]
    )
    errors = "\n".join(
        f"- `{_markdown(error)}`" for error in validated["validationErrors"]
    )
    replacements = {
        "RUN_ID": validated["runId"],
        "GENERATED_AT": validated["generatedAt"],
        "PLATFORM": validated["platform"],
        "CODEX_VERSION": validated["codexVersion"],
        "REPOSITORY_COMMIT": validated["repositoryCommit"],
        "CONFIGURATION_HASH": validated["configurationHash"],
        "RESULT_ROWS": rows,
        "VALIDATION_ERRORS": errors,
        "RELEASE_DECISION": validated["releaseDecision"],
        "RUNTIME_VALIDATED": str(validated["runtimeValidated"]).lower(),
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    if re.search(r"\{\{[A-Z_]+\}\}", rendered) or _contains_secret(rendered):
        _fail("Rendered report is incomplete or contains secret-like material")
    return rendered


def generate_report(summary_path: Path, template_path: Path, output_path: Path) -> dict[str, Any]:
    if summary_path.name != "live-validation-summary.json" or summary_path.parent.name != "results":
        _fail("Summary path is not allowlisted")
    run_root = summary_path.parent.parent
    root, marker = validate_marker(run_root)
    expected_template = Path(__file__).resolve().parents[1] / "templates" / "LIVE_VALIDATION_REPORT.md"
    if template_path.resolve(strict=True) != expected_template.resolve(strict=True):
        _fail("Report template path is not allowlisted")
    expected_output = root / "results" / "LIVE_VALIDATION.md"
    if output_path.absolute() != expected_output.absolute():
        _fail("Report output path is not allowlisted")
    summary = validate_summary(_strict_json(summary_path))
    manifest_cases = validate_case_manifest(
        _strict_json(root / "results" / "case-manifest.json")
    )
    if summary["runId"] != marker["runId"] or summary["repositoryCommit"] != marker["repositoryRevision"]:
        _fail("Summary identity does not match the owned fixture")
    if summary["expectedCaseIds"] != [case["id"] for case in manifest_cases]:
        _fail("Summary cases do not match the owned case manifest")
    for record, expected in zip(summary["tests"], manifest_cases, strict=True):
        if record["critical"] is not expected["critical"]:
            _fail("Summary case criticality does not match the owned manifest")
        for relative in record["evidencePaths"]:
            if not (root / Path(relative)).is_file():
                _fail("Summary references missing evidence")
    environment = _strict_json(root / "results" / "environment.json")
    if (
        summary["platform"] != environment.get("platform")
        or summary["codexVersion"] != environment.get("codexVersion")
        or summary["configurationHash"] != environment.get("configurationHash")
    ):
        _fail("Summary environment does not match the owned fixture")
    template = template_path.read_text(encoding="utf-8")
    report = render_report(summary, template)
    _write_secret_free_text(expected_output, report)
    finalize_evidence(root, marker)
    return {
        "report": str(expected_output),
        "releaseDecision": "NOT READY",
        "runtimeValidated": False,
    }


def _json_output(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--allow-live", action="store_true")
    create.add_argument("--temp-base", type=Path)
    create.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    create.add_argument("--timeout", type=int, default=30)
    run = subparsers.add_parser("run")
    run.add_argument("--run-root", type=Path, required=True)
    run.add_argument("--allow-live", action="store_true")
    run.add_argument("--timeout", type=int, default=30)
    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--run-root", type=Path, required=True)
    cleanup.add_argument("--apply-cleanup", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "create":
            result = create_fixture(
                arguments.source_root,
                allow_live=arguments.allow_live,
                temp_base=arguments.temp_base,
                timeout=arguments.timeout,
            )
        elif arguments.command == "run":
            result = run_behavior_tests(
                arguments.run_root,
                allow_live=arguments.allow_live,
                timeout=arguments.timeout,
            )
        else:
            result = cleanup_fixture(
                arguments.run_root,
                apply_cleanup=arguments.apply_cleanup,
            )
        _json_output(result)
        return 0
    except LiveValidationError as error:
        print(f"REFUSED: {sanitize_text(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
