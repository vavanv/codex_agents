"""Opt-in L5 acceptance from reviewed, live private Codex rollouts.

Raw rollouts stay in the user's session directory. Only bounded, allowlisted
correlation metadata is emitted; this gate does not confer runtime validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from capture_live_event import LegacyRunIdentity, ROLE_NAMES, _legacy_identity, _owned_run_lock
from codex_compatibility import CompatibilityRegistry
from live_validation_support import MARKER_NAME, LiveValidationError, validate_marker
from reconcile_codex_rollouts import MAX_INPUT_BYTES
from validate_capture_snapshot import _utc, validate_snapshot_sidecar
from validate_composite_fixture import validate_composite_fixture
from validate_l5_rollout_matrix import _no_duplicates, _safe_file, _safe_json
from validate_l5_snapshot_matrix import CAPTURE_ID_PATTERN, SCHEMA as SNAPSHOT_SCHEMA, validate_l5_snapshot_matrix
from verify_safe_rollout_projection import ProjectionError, _safe_source, canonical_bytes


SCHEMA = "codex-l5-private-matrix/v3"
REVIEW_SCHEMA = "codex-l5-private-review/v3"
PROJECTION_SCHEMA = "codex-l5-private-projection/v3"
REVIEW_FIELDS = frozenset({
    "schema", "decision", "reviewer", "reviewedAt", "role", "runId",
    "captureId", "codexVersion", "sidecarHash", "compositeReviewHash",
    "projectionHash", "parentRolloutHash", "childRolloutHash",
    "parentSessionId", "childSessionId",
})
REVIEWER_ID = re.compile(r"/root/[a-z0-9_]+\Z")
ROLLOUT_NAME = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f-]{36})\.jsonl\Z")
ALLOWED_OUTER = {"session_meta", "turn_context", "response_item", "event_msg", "world_state", "token_usage_record", "inter_agent_communication_metadata"}
ALLOWED_EVENT = {"task_started", "item_completed", "token_count", "thread_settings_applied", "task_complete"}
ALLOWED_ITEM = {"message", "agent_message", "reasoning", "function_call", "function_call_output"}


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _parse_rows(raw: bytes) -> list[dict[str, Any]]:
    if not raw or len(raw) > MAX_INPUT_BYTES or not raw.endswith(b"\n"):
        raise ValueError("Invalid private rollout")
    rows = []
    for line in raw.splitlines():
        if not line:
            raise ValueError("Invalid private rollout")
        row = json.loads(line.decode("utf-8"), object_pairs_hook=_no_duplicates)
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            raise ValueError("Invalid private rollout")
        rows.append(row)
    if not rows or len(rows) > 20_000:
        raise ValueError("Invalid private rollout")
    return rows


def _shape(raw: bytes, *, parent: bool, variant: str) -> None:
    """Accept the observed 0.159.3 envelopes, rejecting extra tools or rows."""
    rows = _parse_rows(raw)
    calls: list[str] = []
    outputs = 0
    completions = 0
    metas = 0
    for row in rows:
        outer, payload = row.get("type"), row["payload"]
        if outer not in ALLOWED_OUTER:
            raise ValueError("Unknown private rollout row")
        if outer == "session_meta":
            metas += 1
        elif outer == "event_msg":
            if payload.get("type") not in ALLOWED_EVENT:
                raise ValueError("Unknown private event")
            completions += payload.get("type") == "task_complete"
        elif outer == "response_item":
            kind = payload.get("type")
            if kind not in ALLOWED_ITEM:
                raise ValueError("Unknown private item")
            if kind == "function_call":
                calls.append(payload.get("name"))
            elif kind == "function_call_output":
                outputs += 1
    expected_metas = 1 if parent or variant == "v2" else 2
    if (metas != expected_metas or completions != 1 or
            calls != (["spawn_agent", "wait_agent"] if parent else []) or
            outputs != (2 if parent else 0)):
        raise ValueError("Ambiguous private rollout")


def _rollout_name(private_root: Path, session_id: str) -> str:
    if not isinstance(session_id, str) or re.fullmatch(r"[0-9a-f-]{36}", session_id) is None:
        raise ValueError("Invalid session ID")
    matches = [p.name for p in private_root.glob(f"rollout-*-{session_id}.jsonl")]
    match = ROLLOUT_NAME.fullmatch(matches[0]) if len(matches) == 1 else None
    if match is None or match.group(1) != session_id:
        raise ValueError("Private rollout is missing or ambiguous")
    return matches[0]


def _projection(composite: dict[str, Any], role: str, variant: str, version: str) -> bytes:
    return canonical_bytes({
        "schema": PROJECTION_SCHEMA,
        "role": role,
        "rolloutVariant": variant,
        "codexVersion": version,
        "status": "ACCEPTED_ONE_ROLE",
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
    })


def _review_matches(raw: bytes, expected: dict[str, Any], completed_at: str,
                    composite_at: str, composite_reviewer: str) -> bool:
    review = _safe_json(raw)
    if set(review) != REVIEW_FIELDS or not isinstance(review.get("reviewer"), str):
        return False
    if REVIEWER_ID.fullmatch(review["reviewer"]) is None or review["reviewer"] == composite_reviewer:
        return False
    try:
        reviewed = _utc(review["reviewedAt"])
        if reviewed < _utc(completed_at) or reviewed < _utc(composite_at):
            return False
    except (TypeError, ValueError):
        return False
    return all(type(review.get(key)) is type(value) and review.get(key) == value
               for key, value in expected.items())


def validate_l5_private_matrix(run_root: Path, index_path: Path,
                               private_root: Path, *,
                               source_root: Path | None = None,
                               policy: CompatibilityRegistry | None = None,
                               _identity: LegacyRunIdentity | None = None) -> dict[str, Any]:
    """Return pending metadata or acceptance; never expose private content."""
    identity = _identity or _legacy_identity(source_root, policy)
    version = identity.profile.expected_version
    baseline = validate_l5_snapshot_matrix(
        run_root, index_path, source_root=identity.source_root, policy=identity.policy
    )
    if baseline.get("status") != "SNAPSHOT_MATRIX_COMPLETE":
        raise ValueError("Snapshot matrix is invalid")
    root, marker = validate_marker(run_root)
    if marker["lifecycle"] != "ready" or marker["activeWorkers"]:
        raise ValueError("Owned fixture is not ready")
    marker_bytes = _safe_file(root / MARKER_NAME, MAX_INPUT_BYTES)
    with _owned_run_lock(root):
        if _safe_file(root / MARKER_NAME, MAX_INPUT_BYTES) != marker_bytes:
            raise ValueError("Owned fixture changed")
        index_bytes = _safe_file(index_path, 65_536)
        index = _safe_json(index_bytes)
        if (set(index) != {"schema", "runId", "codexVersion", "entries"}
                or index["schema"] != SNAPSHOT_SCHEMA
                or index["runId"] != marker["runId"]
                or index["codexVersion"] != version):
            raise ValueError("Snapshot index does not identify this version")
        private_resolved = private_root.resolve(strict=True)
        if (not private_root.is_dir() or private_resolved.is_relative_to(root)
                or root.is_relative_to(private_resolved)):
            raise ValueError("Private source root is invalid")

        entries = index["entries"]
        if not isinstance(entries, list) or len(entries) != len(ROLE_NAMES):
            raise ValueError("Incomplete L5 matrix")
        stable: dict[tuple[Path, str], bytes] = {(index_path, "fixture"): index_bytes}
        projections = []
        pending = []
        projection_pending = []
        sessions: set[str] = set()
        seen_roles: set[str] = set()
        seen_captures: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"role", "captureId", "sidecar"}:
                raise ValueError("Invalid matrix entry")
            role, capture_id = entry["role"], entry["captureId"]
            if (role not in ROLE_NAMES or role in seen_roles or
                    not isinstance(capture_id, str) or
                    CAPTURE_ID_PATTERN.fullmatch(capture_id) is None or
                    capture_id in seen_captures):
                raise ValueError("Repeated or invalid capture")
            stem = f"capture-{role}-{capture_id}"
            if entry["sidecar"] != f"results/{stem}.snapshot-evidence.json":
                raise ValueError("Noncanonical sidecar")
            sidecar_path = root / entry["sidecar"]
            sidecar_raw = _safe_file(sidecar_path, MAX_INPUT_BYTES)
            bracket = validate_snapshot_sidecar(
                root, sidecar_path, source_root=identity.source_root, policy=identity.policy
            )
            if bracket != {"status": "SNAPSHOT_BRACKET_VALID", "runId": marker["runId"],
                           "captureId": capture_id, "role": role,
                           "stateUnchanged": True, "runtimeValidated": False}:
                raise ValueError("Invalid snapshot bracket")
            sidecar = _safe_json(sidecar_raw)
            files = sidecar["files"]
            capture_path = root / files["capture"]["path"]
            manifest_path = root / files["manifest"]["path"]
            review_path = root / "results" / f"{stem}.composite-review.json"
            config_path = root / "fixture" / ".codex" / "agents" / f"{role.replace('_', '-')}.toml"
            fixture = {"sidecar": (sidecar_path, sidecar_raw)}
            for name, path in (("capture", capture_path), ("manifest", manifest_path),
                               ("compositeReview", review_path), ("agentConfig", config_path)):
                fixture[name] = (path, _safe_file(path, MAX_INPUT_BYTES))
            manifest = _safe_json(fixture["manifest"][1])
            composite_review = _safe_json(fixture["compositeReview"][1])
            variant = "v2" if composite_review["schema"] == "codex-composite-review/v2" else "v1"
            if manifest.get("codexVersion") != version:
                raise ValueError("Mixed Codex version")
            parent_name = _rollout_name(private_root, composite_review["parentSessionId"])
            child_name = _rollout_name(private_root, composite_review["childSessionId"])
            if parent_name == child_name:
                raise ValueError("Reused rollout")
            parent_raw = _safe_source(private_root, parent_name, limit=MAX_INPUT_BYTES)
            child_raw = _safe_source(private_root, child_name, limit=MAX_INPUT_BYTES)
            _shape(parent_raw, parent=True, variant=variant)
            _shape(child_raw, parent=False, variant=variant)
            composite = validate_composite_fixture(
                fixture["capture"][1], fixture["manifest"][1], parent_raw,
                child_raw, fixture["agentConfig"][1],
                review_bytes=fixture["compositeReview"][1], role=role,
                rollout_variant=variant, source_root=identity.source_root, policy=identity.policy)
            if (composite.get("status") != "ACCEPTED_ONE_ROLE" or
                    composite.get("runtimeValidated") is not False or
                    composite.get("publicStreamAttribution") != "MISSING_ATTRIBUTION"):
                raise ValueError("Composite is not accepted")
            for session_id in (composite["parentSessionId"], composite["childSessionId"]):
                if session_id in sessions:
                    raise ValueError("Session ID reused across roles")
                sessions.add(session_id)
            projection = _projection(composite, role, variant, version)
            projection_hash = _hash(projection)
            projection_path = root / "results" / f"{stem}.private-projection.json"
            if projection_path.exists():
                retained_projection = _safe_file(projection_path, 4096)
                if retained_projection != projection:
                    raise ValueError("Retained projection does not match private sources")
                fixture["projection"] = (projection_path, retained_projection)
            else:
                projection_pending.append(role)
            expected_review = {
                "schema": REVIEW_SCHEMA, "decision": "APPROVE_L5_PRIVATE",
                "role": role, "runId": marker["runId"], "captureId": capture_id,
                "codexVersion": version, "sidecarHash": _hash(sidecar_raw),
                "compositeReviewHash": _hash(fixture["compositeReview"][1]),
                "projectionHash": projection_hash,
                "parentRolloutHash": composite["parentRolloutHash"],
                "childRolloutHash": composite["childRolloutHash"],
                "parentSessionId": composite["parentSessionId"],
                "childSessionId": composite["childSessionId"],
            }
            l5_review_path = root / "results" / f"{stem}.l5-private-review.json"
            if l5_review_path.exists():
                l5_raw = _safe_file(l5_review_path, 65_536)
                fixture["l5Review"] = (l5_review_path, l5_raw)
                if not _review_matches(l5_raw, expected_review, sidecar["completedAt"],
                                       composite_review["reviewedAt"],
                                       composite_review["reviewer"]):
                    raise ValueError("L5 review does not bind exact evidence")
            else:
                pending.append(role)
            projections.append({"role": role, "captureId": capture_id,
                                "projectionHash": projection_hash,
                                "projectionFile": f"results/{stem}.private-projection.json",
                                "projection": json.loads(projection),
                                "reviewFile": f"results/{stem}.l5-private-review.json",
                                "reviewFields": expected_review})
            for path, raw in (value for value in fixture.values()):
                stable[(path, "fixture")] = raw
            stable[(private_root / parent_name, "private")] = parent_raw
            stable[(private_root / child_name, "private")] = child_raw
            seen_roles.add(role)
            seen_captures.add(capture_id)
        if seen_roles != set(ROLE_NAMES) or len(sessions) != 18:
            raise ValueError("Incomplete L5 matrix")
        if _safe_file(root / MARKER_NAME, MAX_INPUT_BYTES) != marker_bytes:
            raise ValueError("Owned fixture changed")
        _, final_marker = validate_marker(root)
        if final_marker["lifecycle"] != "ready" or final_marker["activeWorkers"]:
            raise ValueError("Owned fixture changed")
        for (path, source), original in stable.items():
            current = (_safe_source(private_root, path.name, limit=MAX_INPUT_BYTES)
                       if source == "private" else _safe_file(path, MAX_INPUT_BYTES))
            if current != original:
                raise ValueError("Evidence changed during validation")
        accepted = not pending and not projection_pending
        return {"schema": SCHEMA, "status": "L5_ACCEPTED" if accepted else "L5_REVIEW_REQUIRED",
                "runId": marker["runId"], "codexVersion": version,
                "roleCount": len(seen_roles), "sessionCount": len(sessions),
                "reviewPending": pending, "projectionPending": projection_pending,
                "projections": projections,
                "l5Accepted": accepted, "runtimeValidated": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = validate_l5_private_matrix(args.run_root, args.index, args.private_root)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["l5Accepted"] else 1
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError,
            ProjectionError, LiveValidationError):
        print(json.dumps({"schema": SCHEMA, "status": "L5_INVALID",
                          "l5Accepted": False, "runtimeValidated": False}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
