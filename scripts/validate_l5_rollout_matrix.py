"""Correlate nine independently reviewed owned captures without accepting L5.

This is an offline, source-qualified matrix gate. The opt-in v2 schema requires
an explicit rollout variant and separate v2 link review for every role.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Any

from capture_live_event import ROLE_NAMES, _owned_run_lock
from codex_event_adapter import sanitize_event_stream
from live_validation_support import MARKER_NAME, LiveValidationError, _contains_secret, validate_marker
from reconcile_codex_rollouts import MAX_INPUT_BYTES
from validate_capture_snapshot import _utc, validate_snapshot_sidecar
from validate_composite_fixture import validate_composite_fixture
from validate_l5_snapshot_matrix import CAPTURE_ID_PATTERN
from codex_compatibility import CompatibilityRegistry, RegistryError, load_registry, require_gate, require_rollout_schema, require_source_root


SCHEMA = "codex-l5-rollout-matrix/v1"
LINK_REVIEW_SCHEMA = "codex-l5-link-review/v1"
V2_SCHEMA = "codex-l5-rollout-matrix/v2"
V2_LINK_REVIEW_SCHEMA = "codex-l5-link-review/v2"
MAX_INDEX_BYTES = 65_536
MAX_REVIEW_BYTES = 65_536
REVIEWER_ID = re.compile(r"/root/[a-z0-9_]+\Z")
ENTRY_FIELDS = frozenset({
    "role", "captureId", "sidecar", "parentRollout", "childRollout",
    "agentConfig", "compositeReview", "l5Review",
})
V2_ENTRY_FIELDS = ENTRY_FIELDS | {"rolloutVariant"}
LINK_REVIEW_FIELDS = frozenset({
    "schema", "decision", "scope", "reviewer", "reviewedAt", "runId",
    "captureId", "sidecarHash", "compositeReviewHash", "captureHash",
    "manifestHash", "parentRolloutHash", "childRolloutHash",
    "agentConfigHash", "parentSessionId", "childSessionId",
    "rolloutSource", "publicStreamAttribution", "originalManifestReviewed",
    "runtimeValidated",
})
V2_LINK_REVIEW_FIELDS = LINK_REVIEW_FIELDS | {
    "rolloutVariant", "compositeSchema", "settingsEvidence",
}
EXTERNAL_NAMES = {
    "parentRollout": "parent.jsonl",
    "childRollout": "child.jsonl",
    "agentConfig": "agent.toml",
    "compositeReview": "composite-review.json",
    "l5Review": "l5-review.json",
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _safe_file(path: Path, max_bytes: int) -> bytes:
    """Read a bounded regular file, rejecting path redirects and reparse points."""
    for component in (path, *path.parents):
        if component.is_symlink() or (
            hasattr(component, "is_junction") and component.is_junction()
        ):
            raise ValueError("Evidence path is redirected")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_size > max_bytes:
        raise ValueError("Evidence file is missing or exceeds size limit")
    if details.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT if hasattr(details, "st_file_attributes") else False:
        raise ValueError("Evidence file is a reparse point")
    value = path.read_bytes()
    if len(value) > max_bytes:
        raise ValueError("Evidence file exceeds size limit")
    return value


def _safe_json(value: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(value.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except RecursionError as error:
        raise ValueError("Evidence JSON nesting is excessive") from error
    if not isinstance(parsed, dict):
        raise ValueError("Evidence JSON must be an object")
    try:
        text = json.dumps(parsed, sort_keys=True)
        sanitized = json.loads(sanitize_event_stream(text + "\n"))
    except RecursionError as error:
        raise ValueError("Evidence JSON nesting is excessive") from error
    if _contains_secret(text) or sanitized != parsed:
        raise ValueError("Evidence JSON is unsafe")
    return parsed


def _assert_safe_rollout(value: bytes) -> None:
    """Reject secret-bearing or ambiguous raw rollout rows before correlation."""
    text = value.decode("utf-8")
    if _contains_secret(text):
        raise ValueError("Rollout contains secret-like material")
    try:
        original = [
            json.loads(line, object_pairs_hook=_no_duplicates)
            for line in text.splitlines() if line
        ]
        sanitized = [
            json.loads(line, object_pairs_hook=_no_duplicates)
            for line in sanitize_event_stream(text).splitlines() if line
        ]
    except RecursionError as error:
        raise ValueError("Rollout JSON nesting is excessive") from error
    if not original or original != sanitized:
        raise ValueError("Rollout contains unsafe or ambiguous material")


def _validate_link_review(
    review_bytes: bytes,
    composite_review_bytes: bytes,
    sidecar_bytes: bytes,
    sidecar: dict[str, Any],
    composite: dict[str, Any],
    run_id: str,
    capture_id: str,
    role: str,
    rollout_variant: str = "v1",
    matrix_variant: str = "v1",
) -> None:
    review = _safe_json(review_bytes)
    expected_fields = V2_LINK_REVIEW_FIELDS if matrix_variant == "v2" else LINK_REVIEW_FIELDS
    if set(review) != expected_fields:
        raise ValueError("Link review schema is invalid")
    reviewed_at = _utc(review.get("reviewedAt"))
    composite_review = _safe_json(composite_review_bytes)
    previous_at = composite_review.get("reviewedAt")
    if not isinstance(previous_at, str):
        raise ValueError("Composite review timestamp is invalid")
    try:
        previous_time = datetime.fromisoformat(previous_at)
    except ValueError as error:
        raise ValueError("Composite review timestamp is invalid") from error
    if previous_time.tzinfo is None or reviewed_at < previous_time:
        raise ValueError("Link review predates composite review")
    if reviewed_at < _utc(sidecar["completedAt"]):
        raise ValueError("Link review predates capture completion")
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or REVIEWER_ID.fullmatch(reviewer) is None:
        raise ValueError("Link reviewer is invalid")
    expected = {
        "schema": V2_LINK_REVIEW_SCHEMA if matrix_variant == "v2" else LINK_REVIEW_SCHEMA,
        "decision": "APPROVE_L5_LINK",
        "scope": role,
        "runId": run_id,
        "captureId": capture_id,
        "sidecarHash": _sha256(sidecar_bytes),
        "compositeReviewHash": _sha256(composite_review_bytes),
        "captureHash": composite["captureHash"],
        "manifestHash": composite["manifestHash"],
        "parentRolloutHash": composite["parentRolloutHash"],
        "childRolloutHash": composite["childRolloutHash"],
        "agentConfigHash": composite["agentConfigHash"],
        "parentSessionId": composite["parentSessionId"],
        "childSessionId": composite["childSessionId"],
        "rolloutSource": "persistent-rollouts",
        "publicStreamAttribution": "MISSING_ATTRIBUTION",
        "originalManifestReviewed": False,
        "runtimeValidated": False,
    }
    if matrix_variant == "v2":
        expected.update({
            "rolloutVariant": rollout_variant,
            "compositeSchema": f"codex-composite-fixture/{rollout_variant}",
            "settingsEvidence": (
                "child-turn-context" if rollout_variant == "v2"
                else "thread-settings-applied"
            ),
        })
    if any(type(review.get(key)) is not type(value) or review.get(key) != value for key, value in expected.items()):
        raise ValueError("Link review does not bind exact evidence")


def validate_l5_rollout_matrix(
    run_root: Path, index_path: Path, evidence_root: Path,
    *, source_root: Path | None = None, policy: CompatibilityRegistry | None = None,
) -> dict[str, object]:
    trusted_root = source_root if source_root is not None else Path(__file__).absolute().parent.parent
    registry = policy if policy is not None else load_registry(trusted_root)
    require_source_root(registry, trusted_root)
    marker_bytes = _safe_file(run_root / MARKER_NAME, MAX_INPUT_BYTES)
    root, marker = validate_marker(run_root)
    if marker["lifecycle"] != "ready" or marker["activeWorkers"]:
        raise ValueError("Owned fixture is not finalized")
    if _safe_file(root / MARKER_NAME, MAX_INPUT_BYTES) != marker_bytes:
        raise ValueError("Owned fixture marker changed")
    with _owned_run_lock(root):
        if _safe_file(root / MARKER_NAME, MAX_INPUT_BYTES) != marker_bytes:
            raise ValueError("Owned fixture marker changed before lock")
        _, locked_marker = validate_marker(root)
        if locked_marker["lifecycle"] != "ready" or locked_marker["activeWorkers"]:
            raise ValueError("Owned fixture is not ready under lock")
        return _validate_locked(root, locked_marker, marker_bytes, index_path, evidence_root,
                                source_root=trusted_root, policy=registry)


def _validate_locked(
    root: Path,
    marker: dict[str, Any],
    marker_bytes: bytes,
    index_path: Path,
    evidence_root: Path,
    *, source_root: Path, policy: CompatibilityRegistry,
) -> dict[str, object]:
    if not evidence_root.is_dir() or evidence_root.resolve() == root:
        raise ValueError("External evidence root is invalid")
    evidence_resolved = evidence_root.resolve(strict=True)
    if evidence_resolved.is_relative_to(root) or root.is_relative_to(evidence_resolved):
        raise ValueError("External evidence must be separate from owned fixture")
    index_bytes = _safe_file(index_path, MAX_INDEX_BYTES)
    index = _safe_json(index_bytes)
    if (
        set(index) != {"schema", "runId", "codexVersion", "entries"}
        or not isinstance(index["schema"], str)
        or index["schema"] not in {SCHEMA, V2_SCHEMA}
    ):
        raise ValueError("Matrix index schema is invalid")
    matrix_variant = "v2" if index["schema"] == V2_SCHEMA else "v1"
    if index["runId"] != marker["runId"]:
        raise ValueError("Matrix run identity does not match")
    expected_version = index["codexVersion"]
    try:
        require_gate(policy, expected_version, "capturedEvidence")
    except RegistryError as error:
        if error.category not in {"unknown_version", "denied_gate"}:
            raise
        raise ValueError("Matrix Codex version is invalid") from error
    try:
        require_rollout_schema(policy, expected_version, matrix_variant)
    except RegistryError as error:
        if error.category != "unsupported_rollout":
            raise
        message = ("V2 matrix requires Codex 0.157.1 or newer supported version"
                   if matrix_variant == "v2" else "Matrix Codex version is invalid")
        raise ValueError(message) from error
    entries = index["entries"]
    if not isinstance(entries, list) or len(entries) != len(ROLE_NAMES):
        raise ValueError("Matrix must have exactly nine entries")
    seen_roles: set[str] = set()
    seen_captures: set[str] = set()
    seen_ids: set[str] = set()
    variant_counts = {"v1": 0, "v2": 0}
    stable: dict[Path, tuple[str, int]] = {
        index_path: (_sha256(index_bytes), MAX_INDEX_BYTES)
    }
    for entry in entries:
        expected_entry_fields = V2_ENTRY_FIELDS if matrix_variant == "v2" else ENTRY_FIELDS
        if not isinstance(entry, dict) or set(entry) != expected_entry_fields:
            raise ValueError("Matrix entry schema is invalid")
        rollout_variant = entry["rolloutVariant"] if matrix_variant == "v2" else "v1"
        if not isinstance(rollout_variant, str) or rollout_variant not in {"v1", "v2"}:
            raise ValueError("Matrix rollout variant is invalid")
        try:
            require_rollout_schema(policy, expected_version, rollout_variant)
        except RegistryError as error:
            if error.category != "unsupported_rollout":
                raise
            raise ValueError("Matrix rollout variant is invalid") from error
        role, capture_id = entry["role"], entry["captureId"]
        if not isinstance(role, str) or role not in ROLE_NAMES or role in seen_roles:
            raise ValueError("Matrix role is unknown or repeated")
        if not isinstance(capture_id, str) or CAPTURE_ID_PATTERN.fullmatch(capture_id) is None or capture_id in seen_captures:
            raise ValueError("Matrix capture ID is invalid or repeated")
        sidecar_name = f"results/capture-{role}-{capture_id}.snapshot-evidence.json"
        if entry["sidecar"] != sidecar_name:
            raise ValueError("Matrix sidecar path is not canonical")
        for key, name in EXTERNAL_NAMES.items():
            if entry[key] != f"{role}/{name}":
                raise ValueError("External evidence path is not canonical")
        sidecar_path = root / sidecar_name
        sidecar_bytes = _safe_file(sidecar_path, MAX_INPUT_BYTES)
        stable[sidecar_path] = (_sha256(sidecar_bytes), MAX_INPUT_BYTES)
        bracket = validate_snapshot_sidecar(root, sidecar_path, source_root=source_root, policy=policy)
        if bracket != {
            "status": "SNAPSHOT_BRACKET_VALID", "runId": marker["runId"],
            "captureId": capture_id, "role": role,
            "stateUnchanged": True, "runtimeValidated": False,
        }:
            raise ValueError("Snapshot bracket does not match matrix entry")
        if _safe_file(sidecar_path, MAX_INPUT_BYTES) != sidecar_bytes:
            raise ValueError("Sidecar changed during validation")
        sidecar = _safe_json(sidecar_bytes)
        files = sidecar["files"]
        capture_path = root / files["capture"]["path"]
        manifest_path = root / files["manifest"]["path"]
        capture = _safe_file(capture_path, MAX_INPUT_BYTES)
        manifest = _safe_file(manifest_path, MAX_INPUT_BYTES)
        if _safe_json(manifest).get("codexVersion") != expected_version:
            raise ValueError("Matrix capture version does not match index")
        stable[capture_path] = (_sha256(capture), MAX_INPUT_BYTES)
        stable[manifest_path] = (_sha256(manifest), MAX_INPUT_BYTES)
        external: dict[str, bytes] = {}
        for key, name in EXTERNAL_NAMES.items():
            path = evidence_root / role / name
            limit = MAX_REVIEW_BYTES if key.endswith("Review") else MAX_INPUT_BYTES
            external[key] = _safe_file(path, limit)
            if path in stable:
                raise ValueError("Evidence path is reused")
            stable[path] = (_sha256(external[key]), limit)
        _assert_safe_rollout(external["parentRollout"])
        _assert_safe_rollout(external["childRollout"])
        composite = validate_composite_fixture(
            capture, manifest, external["parentRollout"],
            external["childRollout"], external["agentConfig"],
            review_bytes=external["compositeReview"], role=role,
            rollout_variant=rollout_variant,
            source_root=source_root, policy=policy,
        )
        if (
            composite.get("status") != "ACCEPTED_ONE_ROLE"
            or composite.get("schema") != f"codex-composite-fixture/{rollout_variant}"
            or composite.get("scope") != role
            or composite.get("rolloutSource") != "persistent-rollouts"
            or composite.get("publicStreamAttribution") != "MISSING_ATTRIBUTION"
            or composite.get("originalManifestReviewed") is not False
            or composite.get("runtimeValidated") is not False
            or (rollout_variant == "v2" and composite.get("settingsEvidence") != "child-turn-context")
        ):
            raise ValueError("One-role composite evidence is not accepted")
        parent_id = composite["parentSessionId"]
        child_id = composite["childSessionId"]
        if parent_id == child_id or parent_id in seen_ids or child_id in seen_ids:
            raise ValueError("Rollout session IDs are reused")
        seen_ids.update((parent_id, child_id))
        _validate_link_review(
            external["l5Review"], external["compositeReview"], sidecar_bytes,
            sidecar, composite, marker["runId"], capture_id, role,
            rollout_variant, matrix_variant,
        )
        seen_roles.add(role)
        seen_captures.add(capture_id)
        variant_counts[rollout_variant] += 1
    if seen_roles != set(ROLE_NAMES):
        raise ValueError("Matrix roles are incomplete")
    _, final_marker = validate_marker(root)
    if final_marker["lifecycle"] != "ready" or final_marker["activeWorkers"]:
        raise ValueError("Owned fixture changed during validation")
    if _safe_file(root / MARKER_NAME, MAX_INPUT_BYTES) != marker_bytes:
        raise ValueError("Owned fixture marker changed during validation")
    for path, (original_hash, limit) in stable.items():
        if _sha256(_safe_file(path, limit)) != original_hash:
            raise ValueError("Matrix evidence changed during validation")
    result: dict[str, object] = {
        "status": "ROLLOUT_MATRIX_CORRELATED",
        "roleCount": len(ROLE_NAMES),
        "codexVersion": expected_version,
        "rolloutSource": "persistent-rollouts",
        "publicStreamAttribution": "MISSING_ATTRIBUTION",
        "l5Accepted": False,
        "runtimeValidated": False,
    }
    if matrix_variant == "v2":
        result["schema"] = V2_SCHEMA
        result["variantCounts"] = variant_counts
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        source_root = Path(__file__).absolute().parent.parent
        registry = load_registry(source_root)
        require_source_root(registry, source_root)
        result = validate_l5_rollout_matrix(
            arguments.run_root, arguments.index, arguments.evidence_root,
            source_root=source_root, policy=registry,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, LiveValidationError, RecursionError):
        print(json.dumps({
            "status": "ROLLOUT_MATRIX_INVALID",
            "l5Accepted": False,
            "runtimeValidated": False,
        }, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
