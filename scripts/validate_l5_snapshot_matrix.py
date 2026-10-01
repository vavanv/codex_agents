"""Validate a nine-role owned-fixture snapshot matrix without accepting L5."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from capture_live_event import ROLE_NAMES, _owned_run_lock
from live_validation_support import MARKER_NAME, LiveValidationError, validate_marker
from validate_capture_snapshot import _read_json, validate_snapshot_sidecar


SCHEMA = "codex-l5-snapshot-matrix/v1"
CAPTURE_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
MAX_INDEX_BYTES = 65_536
SUPPORTED_VERSIONS = {"0.155.1", "0.157.1", "0.159.0"}


def validate_l5_snapshot_matrix(run_root: Path, index_path: Path) -> dict[str, object]:
    """Check only matrix shape, identities, and nine valid state brackets."""
    marker_before_validation = (run_root / MARKER_NAME).read_bytes()
    root, marker = validate_marker(run_root)
    if marker["lifecycle"] != "ready" or marker["activeWorkers"]:
        raise ValueError("Owned fixture is not finalized and ready")
    if (root / MARKER_NAME).read_bytes() != marker_before_validation:
        raise ValueError("Owned fixture marker changed during validation")
    with _owned_run_lock(root):
        if (root / MARKER_NAME).read_bytes() != marker_before_validation:
            raise ValueError("Owned fixture marker changed before validation lock")
        _, locked_marker = validate_marker(root)
        if locked_marker["lifecycle"] != "ready" or locked_marker["activeWorkers"]:
            raise ValueError("Owned fixture is not ready under validation lock")
        return _validate_locked_matrix(
            root, locked_marker, marker_before_validation, index_path
        )


def _validate_locked_matrix(
    root: Path,
    marker: dict[str, object],
    marker_bytes: bytes,
    index_path: Path,
) -> dict[str, object]:
    if (
        index_path.is_symlink()
        or not index_path.is_file()
        or index_path.stat().st_size > MAX_INDEX_BYTES
    ):
        raise ValueError("Matrix index is missing or exceeds size limit")
    index_bytes = index_path.read_bytes()
    if len(index_bytes) > MAX_INDEX_BYTES:
        raise ValueError("Matrix index exceeds size limit")
    index = _read_json(index_path)
    if index_path.is_symlink() or index_path.read_bytes() != index_bytes:
        raise ValueError("Matrix index changed during validation")
    if set(index) != {"schema", "runId", "codexVersion", "entries"} or index["schema"] != SCHEMA:
        raise ValueError("Matrix index schema is invalid")
    if index["runId"] != marker["runId"]:
        raise ValueError("Matrix index run identity does not match")
    expected_version = index["codexVersion"]
    if not isinstance(expected_version, str) or expected_version not in SUPPORTED_VERSIONS:
        raise ValueError("Matrix Codex version is invalid")
    entries = index["entries"]
    if not isinstance(entries, list) or len(entries) != len(ROLE_NAMES):
        raise ValueError("Matrix must have exactly nine entries")
    seen_roles: set[str] = set()
    seen_capture_ids: set[str] = set()
    seen_sidecars: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"role", "captureId", "sidecar"}:
            raise ValueError("Matrix entry schema is invalid")
        role = entry["role"]
        capture_id = entry["captureId"]
        sidecar = entry["sidecar"]
        if not isinstance(role, str) or role not in ROLE_NAMES or role in seen_roles:
            raise ValueError("Matrix role is invalid or repeated")
        if (
            not isinstance(capture_id, str)
            or CAPTURE_ID_PATTERN.fullmatch(capture_id) is None
            or capture_id in seen_capture_ids
        ):
            raise ValueError("Matrix capture identity is invalid or repeated")
        expected = f"results/capture-{role}-{capture_id}.snapshot-evidence.json"
        if not isinstance(sidecar, str) or sidecar != expected or sidecar in seen_sidecars:
            raise ValueError("Matrix sidecar path is invalid or repeated")
        result = validate_snapshot_sidecar(root, root / sidecar)
        if (
            result.get("status") != "SNAPSHOT_BRACKET_VALID"
            or result.get("runId") != marker["runId"]
            or result.get("role") != role
            or result.get("captureId") != capture_id
            or result.get("stateUnchanged") is not True
            or result.get("runtimeValidated") is not False
        ):
            raise ValueError("Matrix sidecar did not validate for its identity")
        sidecar_data = _read_json(root / sidecar)
        manifest_path = root / sidecar_data["files"]["manifest"]["path"]
        manifest = _read_json(manifest_path)
        if manifest.get("codexVersion") != expected_version:
            raise ValueError("Matrix capture version does not match index")
        seen_roles.add(role)
        seen_capture_ids.add(capture_id)
        seen_sidecars.add(sidecar)
    if seen_roles != set(ROLE_NAMES):
        raise ValueError("Matrix role set is incomplete")
    _, final_marker = validate_marker(root)
    if final_marker["lifecycle"] != "ready" or final_marker["activeWorkers"]:
        raise ValueError("Owned fixture changed during matrix validation")
    if (root / MARKER_NAME).read_bytes() != marker_bytes:
        raise ValueError("Owned fixture marker changed during matrix validation")
    if index_path.is_symlink() or index_path.read_bytes() != index_bytes:
        raise ValueError("Matrix index changed during validation")
    return {
        "status": "SNAPSHOT_MATRIX_COMPLETE",
        "roleCount": len(ROLE_NAMES),
        "codexVersion": expected_version,
        "l5Accepted": False,
        "runtimeValidated": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        output = validate_l5_snapshot_matrix(arguments.run_root, arguments.index)
        print(json.dumps(output, sort_keys=True))
        return 0
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, LiveValidationError):
        print(json.dumps({
            "status": "SNAPSHOT_MATRIX_INVALID",
            "l5Accepted": False,
            "runtimeValidated": False,
        }, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
