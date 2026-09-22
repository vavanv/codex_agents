#!/usr/bin/env python3
"""Versioned, fail-closed Codex CLI JSONL event-schema adapter.

This module is the deterministic V3 slice of the live-validation plan. It turns
the previously implicit, unvalidated event parser into an explicit, versioned
schema contract and adds a sanitized-fixture capture/validation path so a real
captured ``codex exec --json`` stream can be redacted, schema-checked, and
attributed before any runtime PASS is considered.

The adapter does **not** establish runtime validation. It understands a
versioned schema and can attribute a stream deterministically, but until a
captured, sanitized Codex ``0.155.1`` fixture is reviewed and registered,
``runtimeValidated`` stays ``false`` and a parsed stream is only ever UNVERIFIED.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from validate_agent_configs import EXPECTED_ROLES


# Versioned schema identifier the parser understands. This is distinct from
# validation status: the schema is versioned, but no real captured stream has
# been reviewed against it yet.
EVENT_SCHEMA = "codex-cli-jsonl/v1"

# Top-level JSONL event types accepted by this schema.
SUPPORTED_EVENT_TYPES = frozenset(
    {
        "thread.started",
        "thread.completed",
        "turn.started",
        "turn.completed",
        "item.started",
        "item.completed",
        "item.updated",
        "error",
    }
)

# Item payload types recognized under ``item.*`` events.
SUPPORTED_ITEM_TYPES = frozenset(
    {
        "agent_message",
        "reasoning",
        "command_execution",
        "collab_tool_call",
        "collaboration_tool_call",
    }
)

# Item payload types with no attribution side effects.
PASSIVE_ITEM_TYPES = frozenset({"reasoning", "command_execution"})

# Collaboration tools that carry spawn attribution.
SPAWN_TOOLS = frozenset({"spawn_agent"})

# Codex 0.155.1 emits ``collab_tool_call`` items using this tool vocabulary.
COLLAB_TOOLS = frozenset({"spawn_agent", "send_input", "wait", "close_agent"})
COLLAB_STATUSES = frozenset({"in_progress", "completed", "failed"})
COLLAB_AGENT_STATUSES = frozenset(
    {
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
        "shutting_down",
        "shutdown",
        "not_found",
    }
)

# Terminal event types whose presence is required for a coherent attribution.
TERMINAL_EVENT_TYPES = frozenset({"turn.completed", "thread.completed"})

# Adapter reason-code vocabulary. The first block is shared with the verifier's
# parse path; the fixture block is only produced by ``validate_captured_fixture``.
REASON_CODES = frozenset(
    {
        "ATTRIBUTION_CONFLICT",
        "ATTRIBUTION_BEFORE_PARENT",
        "ATTRIBUTION_UNEXPECTED",
        "INVALID_CHILD_SESSION",
        "DUPLICATE_ATTRIBUTION",
        "MALFORMED_EVENT_STREAM",
        "TRUNCATED_EVENT_STREAM",
        "UNSUPPORTED_EVENT_SCHEMA",
        "MISSING_ATTRIBUTION",
        "MISSING_PARENT_SESSION",
        "DISCOVERY_ERROR_EVENT",
        "MISSING_TERMINAL_ATTRIBUTION",
        "INVALID_EXPECTED_ROLES",
        "MALFORMED_COLLAB_TOOL_CALL",
        "UNSUPPORTED_COLLAB_TOOL",
        "COLLAB_TOOL_CALL_FAILED",
        "COLLAB_PARENT_MISMATCH",
        "AMBIGUOUS_ATTRIBUTION",
        "FIXTURE_SCHEMA_UNSUPPORTED",
        "FIXTURE_MANIFEST_SCHEMA_UNSUPPORTED",
        "FIXTURE_VERSION_MISMATCH",
        "FIXTURE_HASH_MISMATCH",
        "FIXTURE_SECRET_PRESENT",
        "FIXTURE_REVIEW_REQUIRED",
        "FIXTURE_SANITIZED_REQUIRED",
        "FIXTURE_REQUESTED_ROLES_INVALID",
        "FIXTURE_REQUESTED_ROLES_MISMATCH",
        "FIXTURE_CAPTURE_TIMED_OUT",
        "FIXTURE_EXIT_CODE_INVALID",
    }
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        re.DOTALL,
    ),
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
_SENSITIVE_JSON_KEY_SUFFIXES = (
    "api_key",
    "access_token",
    "auth_token",
    "authorization",
    "client_secret",
    "connection_string",
    "credential",
    "credentials",
    "password",
    "private_key",
    "secret",
    "secret_access_key",
    "token",
)
_SENSITIVE_JSON_FIELD = re.compile(
    r"(?i)[\"']?(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|authorization|"
    r"client[_ -]?secret|connection[_ -]?string|credentials?|password|"
    r"private[_ -]?key|secret(?:[_ -]?access[_ -]?key)?|token)[\"']?\s*:"
)


@dataclass(frozen=True)
class StreamAttribution:
    """Structured, fail-closed result of parsing one event stream."""

    schema: str
    parent_session_id: str | None
    attributed_children: dict[str, str]
    smoke_names: tuple[str, ...]
    event_count: int
    reason_codes: tuple[str, ...]
    terminal_seen: bool
    observed_models: dict[str, str]
    observed_efforts: dict[str, str]

    @property
    def integrity(self) -> str:
        return "complete" if not self.reason_codes else "untrusted"


@dataclass(frozen=True)
class FixtureValidation:
    """Result of validating a captured, sanitized fixture against the contract."""

    fixture_name: str
    schema: str
    capture_hash: str
    codex_version: str
    sanitized: bool
    conformance: str
    reason_codes: tuple[str, ...]
    attribution: StreamAttribution | None


def _redact(value: str) -> str:
    sanitized = value
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


def _is_sensitive_json_key(key: str) -> bool:
    snake_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")
    return any(
        normalized == suffix or normalized.endswith(f"_{suffix}")
        for suffix in _SENSITIVE_JSON_KEY_SUFFIXES
    )


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_json_key(key) else _redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _redact(value)
    return value


def _json_contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_sensitive_json_key(key) and item != "[REDACTED]":
                return True
            if _json_contains_secret(item):
                return True
        return False
    if isinstance(value, list):
        return any(_json_contains_secret(item) for item in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_PATTERNS)
    return False


def sanitize_text(value: str, limit: int | None = 1000) -> str:
    sanitized_lines: list[str] = []
    for line in value.splitlines() or [value]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            if _SENSITIVE_JSON_FIELD.search(line):
                sanitized_lines.append("[REDACTED_UNSAFE_DIAGNOSTIC]")
            else:
                sanitized_lines.append(_redact(line))
        else:
            sanitized_lines.append(
                json.dumps(
                    _redact_json(parsed),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
    sanitized = "\n".join(sanitized_lines)
    if limit is None:
        return sanitized
    return sanitized[-limit:]


def _contains_secret(value: str) -> bool:
    lines = [line for line in value.splitlines() if line.strip()]
    if not lines:
        lines = [value]
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            if _SENSITIVE_JSON_FIELD.search(line):
                return True
            if any(pattern.search(line) for pattern in _SECRET_PATTERNS):
                return True
        else:
            if _json_contains_secret(parsed):
                return True
    return False


def sanitize_event_stream(raw: str) -> str:
    """Redact secrets from a raw event stream, preserving JSONL line structure.

    Returns a canonical sanitized stream: non-blank lines only, each redacted in
    place, terminated by a single trailing newline.
    """
    lines: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            if _SENSITIVE_JSON_FIELD.search(line):
                lines.append(
                    json.dumps(
                        {
                            "type": "sanitization.error",
                            "reason": "unsafe_malformed_event",
                        },
                        separators=(",", ":"),
                    )
                )
            else:
                lines.append(_redact(line))
        else:
            lines.append(
                json.dumps(
                    _redact_json(parsed),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _normalize_session_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    normalized = str(parsed)
    if (
        value.lower() != normalized
        or parsed.variant != uuid.RFC_4122
        or parsed.version not in {4, 7}
    ):
        return None
    return normalized


def _record_attribution(
    attributed: dict[str, str],
    role: Any,
    child_id: Any,
    parent_session_id: str | None,
    expected_roles: frozenset[str],
    reasons: set[str],
) -> None:
    if not isinstance(role, str) or not isinstance(child_id, str):
        reasons.add("UNSUPPORTED_EVENT_SCHEMA")
        return
    if role not in expected_roles:
        reasons.add("ATTRIBUTION_UNEXPECTED")
        return
    if parent_session_id is None:
        reasons.add("ATTRIBUTION_BEFORE_PARENT")
    normalized_child_id = _normalize_session_id(child_id)
    if normalized_child_id is None:
        reasons.add("INVALID_CHILD_SESSION")
        return
    if normalized_child_id == parent_session_id:
        reasons.add("INVALID_CHILD_SESSION")
        return
    existing = attributed.get(role)
    if existing == normalized_child_id:
        reasons.add("DUPLICATE_ATTRIBUTION")
        return
    if (existing is not None and existing != normalized_child_id) or normalized_child_id in {
        value for key, value in attributed.items() if key != role
    }:
        reasons.add("ATTRIBUTION_CONFLICT")
        return
    attributed[role] = normalized_child_id


def _record_model_effort(
    observed_models: dict[str, str],
    observed_efforts: dict[str, str],
    role: Any,
    model_value: Any,
    effort_value: Any,
    expected_roles: frozenset[str],
) -> None:
    """Record observed model/effort metadata for an attributed role, if present."""
    if not isinstance(role, str) or role not in expected_roles:
        return
    if isinstance(model_value, str) and model_value and not _contains_secret(model_value):
        observed_models[role] = model_value
    if isinstance(effort_value, str) and effort_value and not _contains_secret(effort_value):
        observed_efforts[role] = effort_value


def _normalize_expected_roles(
    expected_roles: tuple[str, ...] | None,
) -> tuple[tuple[str, ...], bool, bool]:
    """Return normalized roles, validity, and whether roles were explicit."""
    if expected_roles is None:
        return tuple(sorted(EXPECTED_ROLES)), True, False
    if not isinstance(expected_roles, tuple) or not expected_roles:
        return (), False, True
    if any(
        not isinstance(role, str) or role not in EXPECTED_ROLES
        for role in expected_roles
    ):
        return (), False, True
    if len(set(expected_roles)) != len(expected_roles):
        return (), False, True
    return tuple(sorted(expected_roles)), True, True


def _collab_agent_state_valid(value: Any) -> bool:
    if isinstance(value, str):
        return value in COLLAB_AGENT_STATUSES
    if isinstance(value, dict) and set(value) == {"status"}:
        return value.get("status") in COLLAB_AGENT_STATUSES
    return False


def _parse_collab_tool_call(
    item: dict[str, Any],
    event_type: str,
    attributed: dict[str, str],
    parent_session_id: str | None,
    expected_roles: tuple[str, ...],
    expected_roles_valid: bool,
    expected_roles_explicit: bool,
    reasons: set[str],
) -> None:
    """Validate one Codex 0.155.1 collaboration item and record safe attribution."""
    item_valid = True
    required_fields = {
        "type",
        "tool",
        "status",
        "sender_thread_id",
        "receiver_thread_ids",
        "prompt",
        "agents_states",
    }
    if not required_fields.issubset(item):
        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
        return

    tool = item.get("tool")
    if not isinstance(tool, str) or tool not in COLLAB_TOOLS:
        reasons.add("UNSUPPORTED_COLLAB_TOOL")
        return
    status = item.get("status")
    if not isinstance(status, str) or status not in COLLAB_STATUSES:
        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
        return
    permitted_statuses = {
        "item.started": {"in_progress"},
        "item.updated": {"in_progress", "completed", "failed"},
        "item.completed": {"completed", "failed"},
    }
    if status not in permitted_statuses[event_type]:
        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
        item_valid = False

    sender = _normalize_session_id(item.get("sender_thread_id"))
    if sender is None:
        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
        item_valid = False
    elif parent_session_id is None:
        reasons.add("ATTRIBUTION_BEFORE_PARENT")
        item_valid = False
    elif sender != parent_session_id:
        reasons.add("COLLAB_PARENT_MISMATCH")
        item_valid = False

    receivers_value = item.get("receiver_thread_ids")
    receivers: list[str] = []
    if not isinstance(receivers_value, list):
        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
        item_valid = False
    else:
        for receiver_value in receivers_value:
            receiver = _normalize_session_id(receiver_value)
            if receiver is None or receiver == parent_session_id:
                reasons.add("INVALID_CHILD_SESSION")
                item_valid = False
                continue
            receivers.append(receiver)
        if len(set(receivers)) != len(receivers):
            reasons.add("MALFORMED_COLLAB_TOOL_CALL")
            item_valid = False

    prompt = item.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
        item_valid = False
    if tool in {"spawn_agent", "send_input"} and not (
        isinstance(prompt, str) and prompt
    ):
        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
        item_valid = False
    if tool in {"wait", "close_agent"} and prompt is not None:
        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
        item_valid = False

    agents_states = item.get("agents_states")
    if not isinstance(agents_states, dict):
        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
        item_valid = False
    else:
        for agent_id, state in agents_states.items():
            normalized_agent_id = _normalize_session_id(agent_id)
            if (
                normalized_agent_id is None
                or normalized_agent_id == parent_session_id
                or not _collab_agent_state_valid(state)
            ):
                reasons.add("MALFORMED_COLLAB_TOOL_CALL")
                item_valid = False
            state_value = state.get("status") if isinstance(state, dict) else state
            if state_value in {"failed", "cancelled", "not_found"}:
                reasons.add("COLLAB_TOOL_CALL_FAILED")
                item_valid = False

    if tool == "wait" and receivers:
        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
        item_valid = False
    if tool in {"send_input", "close_agent"} and len(receivers) != 1:
        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
        item_valid = False
    if status == "failed":
        reasons.add("COLLAB_TOOL_CALL_FAILED")
        item_valid = False

    if tool != "spawn_agent" or status != "completed":
        return
    if not item_valid:
        return
    if len(receivers) != 1:
        reasons.add("AMBIGUOUS_ATTRIBUTION")
        return
    if not (
        expected_roles_valid
        and expected_roles_explicit
        and len(expected_roles) == 1
    ):
        reasons.add("AMBIGUOUS_ATTRIBUTION")
        return
    _record_attribution(
        attributed,
        expected_roles[0],
        receivers[0],
        parent_session_id,
        frozenset(expected_roles),
        reasons,
    )


def parse_event_stream(
    text: str,
    schema: str = EVENT_SCHEMA,
    *,
    expected_roles: tuple[str, ...] | None = None,
) -> StreamAttribution:
    """Parse one Codex CLI JSONL event stream into a fail-closed attribution.

    Mirrors the verifier's discovery parser and adds ``item.updated`` and
    ``thread.completed`` recognition plus terminal-event tracking.
    """
    if schema != EVENT_SCHEMA:
        return StreamAttribution(
            schema=schema,
            parent_session_id=None,
            attributed_children={},
            smoke_names=(),
            event_count=0,
            reason_codes=("FIXTURE_SCHEMA_UNSUPPORTED",),
            terminal_seen=False,
            observed_models={},
            observed_efforts={},
        )
    normalized_roles, roles_valid, roles_explicit = _normalize_expected_roles(
        expected_roles
    )
    expected_role_set = frozenset(normalized_roles)
    reasons: set[str] = set()
    if not roles_valid:
        reasons.add("INVALID_EXPECTED_ROLES")
    attributed: dict[str, str] = {}
    smoke_names: set[str] = set()
    parent_session_id: str | None = None
    terminal_seen = False
    event_count = 0
    observed_models: dict[str, str] = {}
    observed_efforts: dict[str, str] = {}
    if text and not text.endswith("\n"):
        reasons.add("TRUNCATED_EVENT_STREAM")
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            reasons.add("MALFORMED_EVENT_STREAM")
            continue
        event_count += 1
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            reasons.add("UNSUPPORTED_EVENT_SCHEMA")
            continue
        event_type = event["type"]
        if event_type in TERMINAL_EVENT_TYPES:
            terminal_seen = True
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            normalized_thread_id = _normalize_session_id(thread_id)
            if normalized_thread_id is None:
                reasons.add("UNSUPPORTED_EVENT_SCHEMA")
            elif parent_session_id is not None and parent_session_id != normalized_thread_id:
                reasons.add("UNSUPPORTED_EVENT_SCHEMA")
            else:
                parent_session_id = normalized_thread_id
        elif event_type == "agent.spawned":
            _record_attribution(
                attributed,
                event.get("agent_name"),
                event.get("child_session_id"),
                parent_session_id,
                expected_role_set,
                reasons,
            )
            _record_model_effort(
                observed_models,
                observed_efforts,
                event.get("agent_name"),
                event.get("model"),
                event.get("model_reasoning_effort"),
                expected_role_set,
            )
        elif event_type in {"item.started", "item.completed", "item.updated"}:
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                reasons.add("UNSUPPORTED_EVENT_SCHEMA")
                continue
            item_type = item["type"]
            if item_type == "collab_tool_call":
                _parse_collab_tool_call(
                    item,
                    event_type,
                    attributed,
                    parent_session_id,
                    normalized_roles,
                    roles_valid,
                    roles_explicit,
                    reasons,
                )
            elif item_type == "collaboration_tool_call" and item.get("tool") == "spawn_agent":
                status = item.get("status")
                status_valid = status is None
                if status is not None:
                    permitted_statuses = {
                        "item.started": {"in_progress"},
                        "item.updated": {"in_progress", "completed", "failed"},
                        "item.completed": {"completed", "failed"},
                    }
                    status_valid = (
                        isinstance(status, str)
                        and status in permitted_statuses[event_type]
                    )
                    if not status_valid:
                        reasons.add("MALFORMED_COLLAB_TOOL_CALL")
                    if status == "failed":
                        reasons.add("COLLAB_TOOL_CALL_FAILED")
                if (
                    event_type == "item.completed"
                    and status_valid
                    and status != "failed"
                ):
                    _record_attribution(
                        attributed,
                        item.get("agent_name"),
                        item.get("child_session_id"),
                        parent_session_id,
                        expected_role_set,
                        reasons,
                    )
                    _record_model_effort(
                        observed_models,
                        observed_efforts,
                        item.get("agent_name"),
                        item.get("model"),
                        item.get("model_reasoning_effort"),
                        expected_role_set,
                    )
            elif item_type == "agent_message":
                text_value = item.get("text")
                if isinstance(text_value, str):
                    smoke_names.update(
                        line for line in text_value.splitlines() if line in expected_role_set
                    )
                else:
                    reasons.add("UNSUPPORTED_EVENT_SCHEMA")
            elif item_type not in PASSIVE_ITEM_TYPES:
                reasons.add("UNSUPPORTED_EVENT_SCHEMA")
        elif event_type == "error":
            reasons.add("DISCOVERY_ERROR_EVENT")
        elif event_type not in {"turn.started", "turn.completed", "thread.completed"}:
            reasons.add("UNSUPPORTED_EVENT_SCHEMA")
    if parent_session_id is not None:
        parent_roles = [
            role for role, child_id in attributed.items() if child_id == parent_session_id
        ]
        for role in parent_roles:
            del attributed[role]
        if parent_roles:
            reasons.add("INVALID_CHILD_SESSION")
    if parent_session_id is None:
        reasons.add("MISSING_PARENT_SESSION")
    if not roles_valid or set(attributed) != expected_role_set:
        reasons.add("MISSING_ATTRIBUTION")
    observed_models = {
        role: value for role, value in observed_models.items() if role in attributed
    }
    observed_efforts = {
        role: value for role, value in observed_efforts.items() if role in attributed
    }
    return StreamAttribution(
        schema=schema,
        parent_session_id=parent_session_id,
        attributed_children=dict(sorted(attributed.items())),
        smoke_names=tuple(sorted(smoke_names)),
        event_count=event_count,
        reason_codes=tuple(sorted(reasons)),
        terminal_seen=terminal_seen,
        observed_models=dict(sorted(observed_models.items())),
        observed_efforts=dict(sorted(observed_efforts.items())),
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def capture_fixture_manifest(
    name: str,
    codex_version: str,
    raw_text: str,
    *,
    schema: str = EVENT_SCHEMA,
) -> dict[str, Any]:
    """Build a manifest for a captured raw stream after sanitizing it.

    The manifest records the sanitized capture hash, version, and review flag so
    ``validate_captured_fixture`` can re-check the fixture without retaining any
    secret material.
    """
    sanitized = sanitize_event_stream(raw_text)
    return {
        "schema": "codex-live-fixture-manifest/v1",
        "name": name,
        "codexVersion": codex_version,
        "eventSchema": schema,
        "captureHash": _sha256(sanitized),
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "sanitized": True,
        "reviewed": False,
    }


def validate_captured_fixture(
    sanitized_text: str,
    manifest: dict[str, Any],
    *,
    expected_roles: tuple[str, ...] | None = None,
) -> FixtureValidation:
    """Validate a sanitized captured fixture against the versioned contract.

    Checks manifest identity, hash, secret-freedom, schema conformance, and the
    coherent terminal/call attribution gate. ``expected_roles`` defaults to the
    full catalog so a partial fixture is a non-conformant result.
    """
    normalized_roles, roles_valid, _ = _normalize_expected_roles(expected_roles)
    reasons: set[str] = set()
    if not roles_valid:
        reasons.add("INVALID_EXPECTED_ROLES")
    name = manifest.get("name") if isinstance(manifest, dict) else None
    if not isinstance(name, str) or not name:
        reasons.add("FIXTURE_SCHEMA_UNSUPPORTED")
    manifest_schema = manifest.get("schema") if isinstance(manifest, dict) else None
    if manifest_schema != "codex-live-fixture-manifest/v1":
        reasons.add("FIXTURE_MANIFEST_SCHEMA_UNSUPPORTED")
    schema = manifest.get("eventSchema") if isinstance(manifest, dict) else None
    if schema != EVENT_SCHEMA:
        reasons.add("FIXTURE_SCHEMA_UNSUPPORTED")
    version = manifest.get("codexVersion") if isinstance(manifest, dict) else None
    if version != "0.155.1":
        reasons.add("FIXTURE_VERSION_MISMATCH")
    capture_hash = manifest.get("captureHash") if isinstance(manifest, dict) else None
    recomputed = _sha256(sanitized_text)
    if capture_hash != recomputed:
        reasons.add("FIXTURE_HASH_MISMATCH")
    if _contains_secret(sanitized_text):
        reasons.add("FIXTURE_SECRET_PRESENT")

    reviewed = manifest.get("reviewed") if isinstance(manifest, dict) else None
    if reviewed is not True:
        reasons.add("FIXTURE_REVIEW_REQUIRED")
    sanitized_flag = manifest.get("sanitized") if isinstance(manifest, dict) else None
    if sanitized_flag is not True:
        reasons.add("FIXTURE_SANITIZED_REQUIRED")
    requested_roles = (
        manifest.get("requestedRoles") if isinstance(manifest, dict) else None
    )
    requested_roles_valid = (
        isinstance(requested_roles, list)
        and bool(requested_roles)
        and all(
            isinstance(role, str) and role in EXPECTED_ROLES
            for role in requested_roles
        )
        and len(set(requested_roles)) == len(requested_roles)
    )
    if not requested_roles_valid:
        reasons.add("FIXTURE_REQUESTED_ROLES_INVALID")
    elif set(requested_roles) != set(normalized_roles):
        reasons.add("FIXTURE_REQUESTED_ROLES_MISMATCH")
    timed_out = manifest.get("timedOut") if isinstance(manifest, dict) else None
    if timed_out is not False:
        reasons.add("FIXTURE_CAPTURE_TIMED_OUT")
    exit_code = manifest.get("exitCode") if isinstance(manifest, dict) else None
    if type(exit_code) is not int or exit_code != 0:
        reasons.add("FIXTURE_EXIT_CODE_INVALID")

    attribution = parse_event_stream(
        sanitized_text,
        EVENT_SCHEMA,
        expected_roles=normalized_roles if roles_valid else (),
    )
    for code in attribution.reason_codes:
        reasons.add(code)
    if not attribution.terminal_seen:
        reasons.add("MISSING_TERMINAL_ATTRIBUTION")
    if not roles_valid or set(attribution.attributed_children) != set(normalized_roles):
        reasons.add("MISSING_ATTRIBUTION")

    conformance = "schema-conformant" if not reasons else "not-schema-conformant"
    return FixtureValidation(
        fixture_name=name if isinstance(name, str) else "<unknown>",
        schema=schema if isinstance(schema, str) else "",
        capture_hash=recomputed,
        codex_version=version if isinstance(version, str) else "",
        sanitized=not _contains_secret(sanitized_text),
        conformance=conformance,
        reason_codes=tuple(sorted(reasons)),
        attribution=attribution,
    )



