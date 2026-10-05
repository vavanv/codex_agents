"""Offline, fail-closed validation of one owned capture's Git-state bracket."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from capture_live_event import ROLE_NAMES
from codex_event_adapter import EVENT_SCHEMA, sanitize_event_stream
from codex_compatibility import CompatibilityRegistry, RegistryError, load_registry, require_gate, require_source_root
from live_validation_support import (
    LiveValidationError,
    SNAPSHOT_SCHEMA,
    _contains_secret,
    _snapshot_digest,
    validate_marker,
)


SCHEMA = "codex-capture-snapshot/v1"
FILE_KINDS = ("capture", "manifest", "before", "after")
MAX_JSON_BYTES = 8_000_000


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _read_json(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("Evidence JSON exceeds size limit")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    if not isinstance(value, dict):
        raise ValueError("Evidence JSON must be an object")
    if _contains_secret(json.dumps(value, sort_keys=True)):
        raise ValueError("Evidence contains secret-like material")
    sanitized = sanitize_event_stream(json.dumps(value, sort_keys=True) + "\n")
    if json.loads(sanitized) != value:
        raise ValueError("Evidence contains unsafe fields")
    return value


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc(value: object) -> datetime:
    if not isinstance(value, str) or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00", value
    ) is None:
        raise ValueError("Evidence timing is invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo != timezone.utc:
        raise ValueError("Evidence timing is invalid")
    return parsed


def validate_snapshot_sidecar(
    run_root: Path, sidecar_path: Path, *, source_root: Path | None = None,
    policy: CompatibilityRegistry | None = None,
) -> dict[str, object]:
    trusted_root = source_root if source_root is not None else Path(__file__).absolute().parent.parent
    registry = policy if policy is not None else load_registry(trusted_root)
    require_source_root(registry, trusted_root)
    root, marker = validate_marker(run_root)
    if marker["lifecycle"] != "ready" or marker["activeWorkers"]:
        raise ValueError("Owned fixture is not finalized and ready")
    sidecar = sidecar_path.resolve(strict=True)
    try:
        relative_sidecar = sidecar.relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError("Sidecar escapes owned fixture") from error
    match = re.fullmatch(
        r"results/capture-([a-z_]+)-([0-9a-f]{32})\.snapshot-evidence\.json",
        relative_sidecar,
    )
    if match is None:
        raise ValueError("Sidecar path is not canonical")
    role, capture_id = match.groups()
    if role not in ROLE_NAMES:
        raise ValueError("Sidecar role is not in the pinned catalog")
    value = _read_json(sidecar)
    expected_keys = {
        "schema", "runId", "captureId", "role", "startedAt", "completedAt",
        "captureStatus", "captureExitCode", "files", "beforeStateHash",
        "afterStateHash", "stateUnchanged", "runtimeValidated",
    }
    if set(value) != expected_keys or value["schema"] != SCHEMA:
        raise ValueError("Sidecar schema is invalid")
    if value["runId"] != marker["runId"] or value["captureId"] != capture_id:
        raise ValueError("Sidecar identity does not match")
    if value["role"] != role or value["runtimeValidated"] is not False:
        raise ValueError("Sidecar role or validation claim is invalid")
    if (
        value["captureStatus"] != "CAPTURED"
        or type(value["captureExitCode"]) is not int
        or value["captureExitCode"] != 0
    ):
        raise ValueError("Capture did not complete successfully")
    started = _utc(value["startedAt"])
    completed = _utc(value["completedAt"])
    if started > completed:
        raise ValueError("Sidecar timing is invalid")
    files = value["files"]
    if not isinstance(files, dict) or set(files) != set(FILE_KINDS):
        raise ValueError("Sidecar file set is invalid")
    suffixes = {
        "capture": ".sanitized.jsonl",
        "manifest": ".manifest.json",
        "before": ".before-snapshot.json",
        "after": ".after-snapshot.json",
    }
    resolved: dict[str, Path] = {}
    for kind in FILE_KINDS:
        entry = files[kind]
        expected = f"results/capture-{role}-{capture_id}{suffixes[kind]}"
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("Sidecar file entry is invalid")
        if entry["path"] != expected or not isinstance(entry["sha256"], str):
            raise ValueError("Sidecar file path is invalid")
        path = root / expected
        if not path.is_file() or _file_hash(path) != entry["sha256"]:
            raise ValueError("Sidecar file hash mismatch")
        resolved[kind] = path
    capture = resolved["capture"].read_text(encoding="utf-8")
    if _contains_secret(capture) or sanitize_event_stream(capture) != capture:
        raise ValueError("Capture is not sanitized")
    manifest = _read_json(resolved["manifest"])
    manifest_keys = {
        "schema", "name", "codexVersion", "eventSchema", "captureHash",
        "capturedAt", "sanitized", "reviewed", "command", "exitCode",
        "timedOut", "requestedRoles", "userConfigIgnored",
        "fixtureTrustOverride", "stderr",
    }
    if set(manifest) != manifest_keys:
        raise ValueError("Manifest schema is invalid")
    try:
        require_gate(registry, manifest.get("codexVersion"), "capturedEvidence")
    except RegistryError as error:
        if error.category not in {"unknown_version", "denied_gate"}:
            raise
        raise ValueError("Manifest capture state is invalid") from error
    if (
        manifest["schema"] != "codex-live-fixture-manifest/v1"
        or not isinstance(manifest.get("codexVersion"), str)
        or manifest["name"] != f"windows-{manifest['codexVersion']}-{role}-capture"
        or manifest["eventSchema"] != EVENT_SCHEMA
        or manifest["sanitized"] is not True
        or manifest["reviewed"] is not False
        or type(manifest["exitCode"]) is not int
        or manifest["exitCode"] != 0
        or manifest["userConfigIgnored"] is not True
        or manifest["fixtureTrustOverride"] is not True
        or manifest["command"] != (
            "codex exec --ignore-user-config --strict-config --enable multi_agent "
            "-c projects=<fixture-trust> --json --sandbox read-only "
            "-C <fixture> <role-prompt>"
        )
    ):
        raise ValueError("Manifest capture state is invalid")
    if manifest.get("captureHash") != hashlib.sha256(capture.encode("utf-8")).hexdigest():
        raise ValueError("Manifest capture hash mismatch")
    if manifest.get("requestedRoles") != [role] or manifest.get("timedOut") is not False:
        raise ValueError("Manifest role or completion is invalid")
    snapshots = {kind: _read_json(resolved[kind]) for kind in ("before", "after")}
    snapshot_keys = {
        "schema", "capturedAt", "head", "refs", "index", "status",
        "fileHashes", "checkoutInventories", "worktrees", "remoteRefs",
        "localConfig", "hooks",
    }
    for kind, key in (("before", "beforeStateHash"), ("after", "afterStateHash")):
        snapshot = snapshots[kind]
        if set(snapshot) != snapshot_keys or snapshot.get("schema") != SNAPSHOT_SCHEMA:
            raise ValueError("Snapshot schema is invalid")
        digest = _snapshot_digest(snapshot)
        if value[key] != digest:
            raise ValueError("Snapshot state digest mismatch")
    if not (
        _utc(snapshots["before"].get("capturedAt"))
        <= started
        <= _utc(manifest["capturedAt"])
        <= _utc(snapshots["after"].get("capturedAt"))
        <= completed
    ):
        raise ValueError("Snapshot timing does not bracket capture")
    if value["stateUnchanged"] is not True:
        raise ValueError("Sidecar records state drift")
    if value["beforeStateHash"] != value["afterStateHash"]:
        raise ValueError("Git state drift was detected")
    return {
        "status": "SNAPSHOT_BRACKET_VALID",
        "runId": value["runId"],
        "captureId": capture_id,
        "role": role,
        "stateUnchanged": True,
        "runtimeValidated": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--sidecar", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        source_root = Path(__file__).absolute().parent.parent
        registry = load_registry(source_root)
        require_source_root(registry, source_root)
        output = validate_snapshot_sidecar(arguments.run_root, arguments.sidecar,
                                           source_root=source_root, policy=registry)
        print(json.dumps(output, sort_keys=True))
        return 0
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, LiveValidationError):
        print(json.dumps({
            "status": "SNAPSHOT_BRACKET_INVALID",
            "runtimeValidated": False,
        }, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
