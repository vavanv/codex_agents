"""Correlate one captured Codex JSONL stream with retained rollout evidence.

This is a separate, offline evidence format. It does not change the meaning of
codex-cli-jsonl/v1 or mark a captured fixture reviewed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tomllib
from typing import Any
from uuid import UUID

from codex_event_adapter import parse_event_stream, sanitize_event_stream
from codex_compatibility import CompatibilityRegistry, RegistryError, load_registry, require_rollout_schema, require_source_root


EVIDENCE_SCHEMA = "codex-rollout-evidence/v1"
V2_EVIDENCE_SCHEMA = "codex-rollout-evidence/v2"
V2_MAX_JSON_DEPTH = 32
V2_MAX_ROWS = 20_000
MAX_INPUT_BYTES = 16 * 1024 * 1024
PINNED_ROLE = "code_explorer"
PINNED_AGENT_CONFIG_HASH = (
    "c15bf506a5d1efac9227ad94a3981605df54bce84444f286d18280229998e579"
)
PINNED_ROLE_CONFIG_HASHES = {
    PINNED_ROLE: PINNED_AGENT_CONFIG_HASH,
    "quick_implementer": "8c4ec600b0732678e663afc53cae9c47fcfb65f56a37c97ebc3f22297285a10f",
    "implementer": "0cca5b58b47d4aeb3281bc6cc890af8ed36c9acc993798c79fb55795eb7dde21",
    "luna_escalation": "bf22538f1e6bcf552a237d19a029c59ee62e452d742630ead99e344048f3ae7c",
    "code_validator": "f698e938704dcd5bfd4857cacc59b3b6d58fd51d9f8581ce2b942fa9a4cdf33a",
    "code_reviewer": "05dcab217a6ac231414f38705fb11c3e0a63585fb91cb9dfae4b8d520ae0589a",
    "sol_architect": "ca2890dc8acd5d99a423ecc633601f62e72457d1f986b5f5458dd304ab556760",
    "sol_architect_deep": "703f7c1164aec0ab9cdf4729d1ce9b53a91ff641f35cc7ee08fb9ba94b2b6f51",
    "commit_pusher": "6e2692c2ab2bf641f319c78ac3aed3e27dad3ecefda039b4cc679d0e95e27a62",
}
PINNED_ROLE_SANDBOX_MODES = {
    "code_explorer": "read-only",
    "quick_implementer": "workspace-write",
    "implementer": "workspace-write",
    "luna_escalation": "workspace-write",
    "sol_architect": "read-only",
    "sol_architect_deep": "read-only",
    "code_validator": "read-only",
    "code_reviewer": "read-only",
    "commit_pusher": "workspace-write",
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _session_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if str(parsed) == value else None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = item
    return result


def _check_json_depth(value: Any, depth: int = 0) -> None:
    if depth > V2_MAX_JSON_DEPTH:
        raise ValueError("JSON depth exceeded")
    if isinstance(value, dict):
        for item in value.values():
            _check_json_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_json_depth(item, depth + 1)


def _json_object(value: Any, *, strict: bool = False) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(
            value, object_pairs_hook=_reject_duplicate_keys if strict else dict
        )
        if strict:
            _check_json_depth(parsed)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _rollout_rows(value: bytes, *, strict: bool = False) -> list[dict[str, Any]] | None:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.endswith("\n"):
        return None
    lines = text.splitlines()
    if strict and (len(lines) > V2_MAX_ROWS or any(not line for line in lines)):
        return None
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line:
            continue
        try:
            row = json.loads(
                line, object_pairs_hook=_reject_duplicate_keys if strict else dict
            )
            if strict:
                _check_json_depth(row)
        except (ValueError, RecursionError):
            return None
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            return None
        rows.append(row)
    return rows if rows else None


def _strict_jsonl(value: str) -> bool:
    if not value.endswith("\n"):
        return False
    try:
        lines = value.splitlines()
        if not lines or len(lines) > V2_MAX_ROWS or any(not line for line in lines):
            return False
        for line in lines:
            row = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            _check_json_depth(row)
            if not isinstance(row, dict):
                return False
        return True
    except (ValueError, RecursionError):
        return False


def _payloads(
    rows: list[dict[str, Any]], row_type: str, inner_type: str | None = None
) -> list[dict[str, Any]]:
    return [
        row["payload"]
        for row in rows
        if row.get("type") == row_type
        and (inner_type is None or row["payload"].get("type") == inner_type)
    ]


def _exact_message_count(
    rows: list[dict[str, Any]],
    role: str,
    phase: str | None,
    content_type: str,
    text: str,
) -> int:
    count = 0
    for payload in _payloads(rows, "response_item", "message"):
        if payload.get("role") != role:
            continue
        if phase is not None and payload.get("phase") != phase:
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if (
                isinstance(item, dict)
                and item.get("type") == content_type
                and item.get("text") == text
            ):
                count += 1
    return count


def reconcile_rollouts(
    capture: bytes,
    manifest: dict[str, Any],
    parent_rollout: bytes,
    child_rollout: bytes,
    agent_config: bytes,
    *,
    role: str,
    rollout_variant: str = "v1",
    source_root: Path | None = None,
    policy: CompatibilityRegistry | None = None,
) -> dict[str, Any]:
    """Return only allowlisted evidence fields; any mismatch is non-correlated."""
    root = source_root if source_root is not None else Path(__file__).absolute().parent.parent
    registry = policy if policy is not None else load_registry(root)
    require_source_root(registry, root)
    reasons: set[str] = set()
    evidence_schema = V2_EVIDENCE_SCHEMA if rollout_variant == "v2" else EVIDENCE_SCHEMA
    base: dict[str, Any] = {
        "schema": evidence_schema,
        "status": "UNVERIFIED",
        "reasonCodes": [],
    }
    if not isinstance(role, str) or role not in PINNED_ROLE_CONFIG_HASHES:
        reasons.add("ROLE_UNSUPPORTED")
    if rollout_variant not in {"v1", "v2"}:
        reasons.add("ROLLOUT_VARIANT_UNSUPPORTED")
    if any(
        len(value) > MAX_INPUT_BYTES
        for value in (capture, parent_rollout, child_rollout, agent_config)
    ):
        reasons.add("INPUT_TOO_LARGE")
    if reasons:
        base["reasonCodes"] = sorted(reasons)
        return base

    try:
        capture_text = capture.decode("utf-8")
        config = tomllib.loads(agent_config.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        base["reasonCodes"] = ["INPUT_MALFORMED"]
        return base
    if rollout_variant == "v2" and not _strict_jsonl(capture_text):
        base["reasonCodes"] = ["CAPTURE_MALFORMED"]
        return base
    parent_rows = _rollout_rows(parent_rollout, strict=rollout_variant == "v2")
    child_rows = _rollout_rows(child_rollout, strict=rollout_variant == "v2")
    if parent_rows is None or child_rows is None:
        base["reasonCodes"] = ["ROLLOUT_MALFORMED"]
        return base

    capture_version = manifest.get("codexVersion")
    version_eligible = True
    try:
        require_rollout_schema(registry, capture_version, rollout_variant)
    except RegistryError as error:
        if error.category not in {"unknown_version", "denied_gate", "unsupported_rollout"}:
            raise
        version_eligible = False
    if (
        manifest.get("schema") != "codex-live-fixture-manifest/v1"
        or manifest.get("eventSchema") != "codex-cli-jsonl/v1"
        or not version_eligible
        or manifest.get("requestedRoles") != [role]
        or manifest.get("sanitized") is not True
        or manifest.get("reviewed") is not False
        or manifest.get("timedOut") is not False
        or type(manifest.get("exitCode")) is not int
        or manifest.get("exitCode") != 0
    ):
        reasons.add("CAPTURE_MANIFEST_INVALID")
    if manifest.get("captureHash") != _sha256(capture):
        reasons.add("CAPTURE_HASH_MISMATCH")
    try:
        if sanitize_event_stream(capture_text) != capture_text:
            reasons.add("CAPTURE_NOT_SANITIZED")
    except ValueError:
        reasons.add("CAPTURE_NOT_SANITIZED")
    attribution = parse_event_stream(capture_text, expected_roles=(role,))
    parent_id = attribution.parent_session_id
    if (
        parent_id is None
        or not attribution.terminal_seen
        or set(attribution.reason_codes) != {"MISSING_ATTRIBUTION"}
        or attribution.attributed_children
    ):
        reasons.add("PUBLIC_STREAM_UNEXPECTED")

    if (
        config.get("name") != role
        or not isinstance(config.get("developer_instructions"), str)
        or not config["developer_instructions"]
        or not isinstance(config.get("model"), str)
        or not isinstance(config.get("model_reasoning_effort"), str)
        or config.get("sandbox_mode") != PINNED_ROLE_SANDBOX_MODES[role]
    ):
        reasons.add("AGENT_CONFIG_INVALID")
    if _sha256(agent_config) != PINNED_ROLE_CONFIG_HASHES[role]:
        reasons.add("AGENT_CONFIG_UNATTESTED")
    parent_metas = [
        item
        for item in _payloads(parent_rows, "session_meta")
        if item.get("agent_role") is None
    ]
    if (
        len(parent_metas) != 1
        or parent_id is None
        or parent_metas[0].get("id") != parent_id
        or parent_metas[0].get("session_id") != parent_id
        or parent_metas[0].get("cli_version") != capture_version
    ):
        reasons.add("PARENT_SESSION_MISMATCH")
    child_metas = [
        item
        for item in _payloads(child_rows, "session_meta")
        if item.get("agent_role") is not None
    ]
    child_meta = child_metas[0] if len(child_metas) == 1 else {}
    child_id = _session_id(child_meta.get("id"))
    if (
        len(child_metas) != 1
        or child_id is None
        or child_id == parent_id
        or child_meta.get("session_id") != parent_id
        or child_meta.get("parent_thread_id") != parent_id
        or (
            rollout_variant == "v1"
            and child_meta.get("forked_from_id") != parent_id
        )
        or (
            rollout_variant == "v2"
            and (
                child_meta.get("multi_agent_version") != "v2"
                or "forked_from_id" in child_meta
            )
        )
        or child_meta.get("agent_role") != role
        or child_meta.get("cli_version") != capture_version
        or (
            len(parent_metas) == 1
            and child_meta.get("cwd") != parent_metas[0].get("cwd")
        )
    ):
        reasons.add("CHILD_SESSION_MISMATCH")
    child_parent_metas = [
        item
        for item in _payloads(child_rows, "session_meta")
        if item.get("agent_role") is None
    ]
    if rollout_variant == "v1":
        if (
            len(child_parent_metas) != 1
            or child_parent_metas[0].get("id") != parent_id
            or child_parent_metas[0].get("session_id") != parent_id
            or child_parent_metas[0].get("cli_version") != capture_version
        ):
            reasons.add("CHILD_SESSION_MISMATCH")
    elif child_parent_metas:
        reasons.add("CHILD_SESSION_MISMATCH")

    calls = [
        item
        for item in _payloads(parent_rows, "response_item", "function_call")
        if item.get("name") == "spawn_agent"
    ]
    call = calls[0] if len(calls) == 1 else {}
    call_id = call.get("call_id")
    arguments = _json_object(call.get("arguments"), strict=rollout_variant == "v2")
    if (
        len(calls) != 1
        or not isinstance(call_id, str)
        or not call_id
        or arguments is None
        or arguments.get("agent_type") != role
        or arguments.get("task_name") != f"probe_{role}"
        or not isinstance(arguments.get("message"), str)
        or not arguments["message"]
    ):
        reasons.add("SPAWN_CALL_MISMATCH")
    outputs = [
        item
        for item in _payloads(parent_rows, "response_item", "function_call_output")
        if item.get("call_id") == call_id
    ] if isinstance(call_id, str) and call_id else []
    output = (
        _json_object(outputs[0].get("output"), strict=rollout_variant == "v2")
        if len(outputs) == 1 else None
    )
    expected_path = f"/root/probe_{role}"
    if (
        output is None
        or output.get("task_name") != expected_path
        or child_meta.get("agent_path") != expected_path
    ):
        reasons.add("SPAWN_RESULT_MISMATCH")

    parent_contexts = _payloads(parent_rows, "turn_context")
    setting_positions = [
        index for index, item in enumerate(child_rows)
        if item.get("type") == "event_msg"
        and item["payload"].get("type") == "thread_settings_applied"
    ]
    setting_index = (
        setting_positions[0] if len(setting_positions) == 1 else len(child_rows)
    ) if rollout_variant == "v1" else 0
    child_context_positions = [
        index for index, item in enumerate(child_rows)
        if index > setting_index and item.get("type") == "turn_context"
    ]
    child_contexts = [
        child_rows[index]["payload"] for index in child_context_positions
    ]
    all_child_contexts = _payloads(child_rows, "turn_context")
    if (
        not parent_contexts
        or any(
            not isinstance(item.get("sandbox_policy"), dict)
            or item["sandbox_policy"].get("type") != "read-only"
            for item in parent_contexts
        )
        or any(
            not isinstance(item.get("sandbox_policy"), dict)
            or item["sandbox_policy"].get("type") != "read-only"
            for item in all_child_contexts
        )
        or len(child_contexts) != 1
        or (rollout_variant == "v2" and len(all_child_contexts) != 1)
        or child_contexts[0].get("model") != config.get("model")
        or child_contexts[0].get("effort") != config.get("model_reasoning_effort")
        or not isinstance(child_contexts[0].get("sandbox_policy"), dict)
        or child_contexts[0]["sandbox_policy"].get("type") != "read-only"
    ):
        reasons.add("SANDBOX_OR_CONTEXT_MISMATCH")
    if rollout_variant == "v1":
        settings = [
            item
            for item in _payloads(child_rows, "event_msg", "thread_settings_applied")
            if item.get("thread_id") == child_id
        ]
        applied = settings[0].get("thread_settings") if len(settings) == 1 else None
        if (
            not isinstance(applied, dict)
            or applied.get("model") != config.get("model")
            or applied.get("reasoning_effort") != config.get("model_reasoning_effort")
        ):
            reasons.add("APPLIED_SETTINGS_MISMATCH")
    elif setting_positions:
        reasons.add("ROLLOUT_VARIANT_MIXED")
    if (
        isinstance(config.get("developer_instructions"), str)
        and (
            _exact_message_count(
                child_rows, "developer", None, "input_text",
                config["developer_instructions"],
            ) != 1
            or _exact_message_count(
                parent_rows, "developer", None, "input_text",
                config["developer_instructions"],
            ) != 0
        )
    ):
        reasons.add("DEVELOPER_INSTRUCTIONS_MISMATCH")
    final_messages = [
        item
        for item in _payloads(child_rows, "response_item", "message")
        if item.get("role") == "assistant" and item.get("phase") == "final_answer"
    ]
    exact_final = [
        {"type": "output_text", "text": f"LIVE_ROLE:{role}"}
    ]
    if len(final_messages) != 1 or final_messages[0].get("content") != exact_final:
        reasons.add("CHILD_MARKER_MISSING")
    if (
        len(_payloads(parent_rows, "event_msg", "task_complete")) != 1
        or len(_payloads(child_rows, "event_msg", "task_complete")) != 1
    ):
        reasons.add("ROLLOUT_TERMINAL_MISSING")
    parent_meta_positions = [
        index for index, item in enumerate(parent_rows)
        if item.get("type") == "session_meta"
    ]
    call_positions = [
        index for index, item in enumerate(parent_rows)
        if item.get("type") == "response_item"
        and item["payload"].get("type") == "function_call"
        and item["payload"].get("name") == "spawn_agent"
    ]
    output_positions = [
        index for index, item in enumerate(parent_rows)
        if item.get("type") == "response_item"
        and item["payload"].get("type") == "function_call_output"
        and item["payload"].get("call_id") == call_id
    ]
    parent_terminal_positions = [
        index for index, item in enumerate(parent_rows)
        if item.get("type") == "event_msg"
        and item["payload"].get("type") == "task_complete"
    ]
    child_meta_positions = [
        index for index, item in enumerate(child_rows)
        if item.get("type") == "session_meta"
        and item["payload"].get("agent_role") is not None
    ]
    final_positions = [
        index for index, item in enumerate(child_rows)
        if item.get("type") == "response_item"
        and item["payload"].get("type") == "message"
        and item["payload"].get("role") == "assistant"
        and item["payload"].get("phase") == "final_answer"
    ]
    child_terminal_positions = [
        index for index, item in enumerate(child_rows)
        if item.get("type") == "event_msg"
        and item["payload"].get("type") == "task_complete"
    ]
    instruction_positions = [
        index for index, item in enumerate(child_rows)
        if item.get("type") == "response_item"
        and item["payload"].get("type") == "message"
        and item["payload"].get("role") == "developer"
        and isinstance(item["payload"].get("content"), list)
        and any(
            isinstance(content, dict)
            and content.get("type") == "input_text"
            and content.get("text") == config.get("developer_instructions")
            for content in item["payload"]["content"]
        )
    ]
    if (
        len(parent_meta_positions) != 1
        or len(call_positions) != 1
        or len(output_positions) != 1
        or len(parent_terminal_positions) != 1
        or not (
            parent_meta_positions[0]
            < call_positions[0]
            < output_positions[0]
            < parent_terminal_positions[0]
        )
        or len(child_meta_positions) != 1
        or (rollout_variant == "v2" and child_meta_positions[0] != 0)
        or (rollout_variant == "v1" and len(setting_positions) != 1)
        or (rollout_variant == "v2" and len(setting_positions) != 0)
        or len(child_context_positions) != 1
        or len(final_positions) != 1
        or len(child_terminal_positions) != 1
        or (rollout_variant == "v2" and len(instruction_positions) != 1)
        or not (
            child_meta_positions[0]
            < child_context_positions[0]
            < final_positions[0]
            < child_terminal_positions[0]
        )
        or (
            rollout_variant == "v1"
            and not (
                child_meta_positions[0]
                < setting_positions[0]
                < child_context_positions[0]
            )
        )
        or (
            rollout_variant == "v2"
            and not (
                child_meta_positions[0]
                < instruction_positions[0]
                < child_context_positions[0]
            )
        )
    ):
        reasons.add("ROLLOUT_ORDER_OR_CARDINALITY_INVALID")

    if reasons:
        base["reasonCodes"] = sorted(reasons)
        return base
    result = {
        "schema": evidence_schema,
        "status": "CORRELATED",
        "reasonCodes": [],
        "source": "persistent-rollouts",
        "captureHash": _sha256(capture),
        "parentRolloutHash": _sha256(parent_rollout),
        "childRolloutHash": _sha256(child_rollout),
        "agentConfigHash": _sha256(agent_config),
        "parentSessionId": parent_id,
        "childSessionId": child_id,
        "role": role,
        "model": config["model"],
        "reasoningEffort": config["model_reasoning_effort"],
        "configuredSandboxMode": config["sandbox_mode"],
        # This is the observed parent/child context for the discovery probe,
        # not evidence that a writer role can exercise workspace-write.
        "sandboxMode": "read-only",
        "developerInstructionsMatched": True,
        "childMarkerObserved": True,
        "publicStreamAttribution": "MISSING_ATTRIBUTION",
        "fixtureReviewed": False,
    }
    if rollout_variant == "v2":
        result["settingsEvidence"] = "child-turn-context"
    return result


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
    parser.add_argument("--role", choices=tuple(PINNED_ROLE_CONFIG_HASHES), required=True)
    parser.add_argument("--rollout-variant", choices=("v1", "v2"), default="v1")
    args = parser.parse_args(argv)
    try:
        source_root = Path(__file__).absolute().parent.parent
        registry = load_registry(source_root)
        require_source_root(registry, source_root)
        manifest = json.loads(
            _read_limited(args.manifest),
            object_pairs_hook=_reject_duplicate_keys if args.rollout_variant == "v2" else dict,
        )
        if args.rollout_variant == "v2":
            _check_json_depth(manifest)
        if not isinstance(manifest, dict):
            raise ValueError("invalid manifest")
        result = reconcile_rollouts(
            _read_limited(args.capture),
            manifest,
            _read_limited(args.parent_rollout),
            _read_limited(args.child_rollout),
            _read_limited(args.agent_config),
            role=args.role, rollout_variant=args.rollout_variant,
            source_root=source_root, policy=registry,
        )
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        result = {
            "schema": V2_EVIDENCE_SCHEMA if args.rollout_variant == "v2" else EVIDENCE_SCHEMA,
            "status": "UNVERIFIED",
            "reasonCodes": ["INPUT_UNAVAILABLE_OR_MALFORMED"],
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "CORRELATED" else 2


if __name__ == "__main__":
    sys.exit(main())
