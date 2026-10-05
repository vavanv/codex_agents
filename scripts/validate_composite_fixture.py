"""Validate one reviewed composite Codex capture without weakening JSONL/v1.

The original public fixture remains unreviewed. A separate, hash-bound review
attestation may accept only the pinned one-role rollout correlation.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from codex_event_adapter import sanitize_event_stream, validate_captured_fixture
from codex_compatibility import CompatibilityRegistry, load_registry, require_source_root
from reconcile_codex_rollouts import (
    EVIDENCE_SCHEMA,
    V2_EVIDENCE_SCHEMA,
    _check_json_depth,
    MAX_INPUT_BYTES,
    PINNED_ROLE,
    PINNED_ROLE_CONFIG_HASHES,
    reconcile_rollouts,
)


COMPOSITE_SCHEMA = "codex-composite-fixture/v1"
REVIEW_SCHEMA = "codex-composite-review/v1"
V2_COMPOSITE_SCHEMA = "codex-composite-fixture/v2"
V2_REVIEW_SCHEMA = "codex-composite-review/v2"
PUBLIC_GAPS = frozenset({"FIXTURE_REVIEW_REQUIRED", "MISSING_ATTRIBUTION"})
MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "name",
        "codexVersion",
        "eventSchema",
        "captureHash",
        "capturedAt",
        "sanitized",
        "reviewed",
        "command",
        "exitCode",
        "timedOut",
        "requestedRoles",
        "userConfigIgnored",
        "fixtureTrustOverride",
        "stderr",
    }
)
REVIEW_FIELDS = frozenset(
    {
        "schema",
        "decision",
        "scope",
        "reviewer",
        "reviewedAt",
        "linkageBasis",
        "publicSchema",
        "rolloutSchema",
        "captureHash",
        "manifestHash",
        "parentRolloutHash",
        "childRolloutHash",
        "agentConfigHash",
        "parentSessionId",
        "childSessionId",
    }
)
REVIEWER_ID = re.compile(r"/root/[a-z0-9_]+\Z")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _json_object(value: bytes, *, strict: bool = False) -> dict[str, Any] | None:
    try:
        parsed = json.loads(
            value.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
        if strict:
            _check_json_depth(parsed)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _manifest_is_safe(manifest: dict[str, Any]) -> bool:
    if set(manifest) - MANIFEST_FIELDS:
        return False
    serialized = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    sanitized = sanitize_event_stream(serialized + "\n")
    try:
        return json.loads(sanitized) == manifest
    except ValueError:
        return False


def _review_matches(
    review: dict[str, Any],
    manifest: dict[str, Any],
    manifest_bytes: bytes,
    evidence: dict[str, Any],
    role: str,
    rollout_variant: str,
) -> bool:
    if set(review) != REVIEW_FIELDS:
        return False
    captured_at = manifest.get("capturedAt")
    reviewed_at = review.get("reviewedAt")
    if not isinstance(captured_at, str) or not isinstance(reviewed_at, str):
        return False
    try:
        capture_time = datetime.fromisoformat(captured_at)
        review_time = datetime.fromisoformat(reviewed_at)
    except ValueError:
        return False
    if (
        capture_time.tzinfo is None
        or review_time.tzinfo is None
        or review_time < capture_time
    ):
        return False
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or REVIEWER_ID.fullmatch(reviewer) is None:
        return False
    expected = {
        "schema": V2_REVIEW_SCHEMA if rollout_variant == "v2" else REVIEW_SCHEMA,
        "decision": "APPROVE_ONE_ROLE",
        "scope": role,
        "linkageBasis": (
            "task-path-child-parent-and-turn-context"
            if rollout_variant == "v2"
            else "task-path-and-child-parent-metadata"
        ),
        "publicSchema": "codex-cli-jsonl/v1",
        "rolloutSchema": V2_EVIDENCE_SCHEMA if rollout_variant == "v2" else EVIDENCE_SCHEMA,
        "captureHash": evidence["captureHash"],
        "manifestHash": _sha256(manifest_bytes),
        "parentRolloutHash": evidence["parentRolloutHash"],
        "childRolloutHash": evidence["childRolloutHash"],
        "agentConfigHash": evidence["agentConfigHash"],
        "parentSessionId": evidence["parentSessionId"],
        "childSessionId": evidence["childSessionId"],
    }
    return all(review.get(key) == value for key, value in expected.items())


def validate_composite_fixture(
    capture: bytes,
    manifest_bytes: bytes,
    parent_rollout: bytes,
    child_rollout: bytes,
    agent_config: bytes,
    *,
    review_bytes: bytes | None = None,
    role: str = PINNED_ROLE,
    rollout_variant: str = "v1",
    source_root: Path | None = None,
    policy: CompatibilityRegistry | None = None,
) -> dict[str, Any]:
    """Return fail-closed one-role acceptance from two distinct evidence sources."""
    root = source_root if source_root is not None else Path(__file__).absolute().parent.parent
    registry = policy if policy is not None else load_registry(root)
    require_source_root(registry, root)
    base: dict[str, Any] = {
        "schema": V2_COMPOSITE_SCHEMA if rollout_variant == "v2" else COMPOSITE_SCHEMA,
        "scope": role if isinstance(role, str) and role in PINNED_ROLE_CONFIG_HASHES else "unsupported",
        "status": "UNVERIFIED",
        "reasonCodes": [],
        "runtimeValidated": False,
    }
    if not isinstance(role, str) or role not in PINNED_ROLE_CONFIG_HASHES:
        base["reasonCodes"] = ["COMPOSITE_ROLE_UNSUPPORTED"]
        return base
    if rollout_variant not in {"v1", "v2"}:
        base["reasonCodes"] = ["COMPOSITE_VARIANT_UNSUPPORTED"]
        return base
    inputs = (capture, manifest_bytes, parent_rollout, child_rollout, agent_config)
    if review_bytes is not None:
        inputs += (review_bytes,)
    if any(len(item) > MAX_INPUT_BYTES for item in inputs):
        base["reasonCodes"] = ["COMPOSITE_INPUT_TOO_LARGE"]
        return base
    manifest = _json_object(manifest_bytes, strict=rollout_variant == "v2")
    if manifest is None:
        base["reasonCodes"] = ["COMPOSITE_MANIFEST_MALFORMED"]
        return base
    if not _manifest_is_safe(manifest):
        base["reasonCodes"] = ["COMPOSITE_MANIFEST_UNSAFE"]
        return base
    try:
        capture_text = capture.decode("utf-8")
    except UnicodeDecodeError:
        base["reasonCodes"] = ["COMPOSITE_CAPTURE_MALFORMED"]
        return base

    public = validate_captured_fixture(
        capture_text, manifest, expected_roles=(role,), source_root=root, policy=registry,
    )
    public_reasons = set(public.reason_codes)
    if manifest.get("reviewed") is not False or public_reasons != PUBLIC_GAPS:
        base["reasonCodes"] = ["COMPOSITE_PUBLIC_GATE_FAILED"]
        return base

    evidence = reconcile_rollouts(
        capture,
        manifest,
        parent_rollout,
        child_rollout,
        agent_config,
        role=role,
        rollout_variant=rollout_variant,
        source_root=root, policy=registry,
    )
    if (
        evidence.get("status") != "CORRELATED"
        or evidence.get("source") != "persistent-rollouts"
        or evidence.get("schema") != (
            V2_EVIDENCE_SCHEMA if rollout_variant == "v2" else EVIDENCE_SCHEMA
        )
        or (rollout_variant == "v2" and evidence.get("settingsEvidence") != "child-turn-context")
    ):
        base["reasonCodes"] = ["COMPOSITE_ROLLOUT_GATE_FAILED"]
        return base
    base.update(
        {
            "captureHash": evidence["captureHash"],
            "manifestHash": _sha256(manifest_bytes),
            "parentRolloutHash": evidence["parentRolloutHash"],
            "childRolloutHash": evidence["childRolloutHash"],
            "agentConfigHash": evidence["agentConfigHash"],
            "parentSessionId": evidence["parentSessionId"],
            "childSessionId": evidence["childSessionId"],
            "publicStreamAttribution": "MISSING_ATTRIBUTION",
            "rolloutSource": "persistent-rollouts",
            "rolloutCorrelation": "CORRELATED",
            "originalManifestReviewed": False,
        }
    )
    if rollout_variant == "v2":
        base["settingsEvidence"] = "child-turn-context"
    if review_bytes is None:
        base["status"] = "REVIEW_REQUIRED"
        base["reasonCodes"] = ["COMPOSITE_REVIEW_REQUIRED"]
        return base
    review = _json_object(review_bytes, strict=rollout_variant == "v2")
    if review is None or not _review_matches(
        review, manifest, manifest_bytes, evidence, role, rollout_variant
    ):
        base["reasonCodes"] = ["COMPOSITE_REVIEW_INVALID"]
        return base
    base["status"] = "ACCEPTED_ONE_ROLE"
    base["linkageBasis"] = (
        "task-path-child-parent-and-turn-context"
        if rollout_variant == "v2"
        else "task-path-and-child-parent-metadata"
    )
    return base


def _read_limited(path: Path) -> bytes:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("input too large")
    return path.read_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--parent-rollout", type=Path, required=True)
    parser.add_argument("--child-rollout", type=Path, required=True)
    parser.add_argument("--agent-config", type=Path, required=True)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--role", choices=tuple(PINNED_ROLE_CONFIG_HASHES), default=PINNED_ROLE)
    parser.add_argument("--rollout-variant", choices=("v1", "v2"), default="v1")
    args = parser.parse_args(argv)
    try:
        source_root = Path(__file__).absolute().parent.parent
        registry = load_registry(source_root)
        require_source_root(registry, source_root)
        result = validate_composite_fixture(
            _read_limited(args.capture),
            _read_limited(args.manifest),
            _read_limited(args.parent_rollout),
            _read_limited(args.child_rollout),
            _read_limited(args.agent_config),
            review_bytes=_read_limited(args.review) if args.review is not None else None,
            role=args.role,
            rollout_variant=args.rollout_variant,
            source_root=source_root, policy=registry,
        )
    except (OSError, ValueError):
        result = {
            "schema": V2_COMPOSITE_SCHEMA if args.rollout_variant == "v2" else COMPOSITE_SCHEMA,
            "scope": args.role,
            "status": "UNVERIFIED",
            "reasonCodes": ["COMPOSITE_INPUT_UNAVAILABLE"],
            "runtimeValidated": False,
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ACCEPTED_ONE_ROLE" else 2


if __name__ == "__main__":
    sys.exit(main())
