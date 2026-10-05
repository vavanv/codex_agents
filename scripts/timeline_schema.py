"""Timeline event schema, validation, and retention policy.

Defines the codex-timeline-event/v1 contract for tracking user-visible
execution timelines, operational metrics, and stage handoffs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
import uuid

TIMELINE_SCHEMA_VERSION = "codex-timeline-event/v1"

ALLOWED_STAGES = frozenset({
    "CLASSIFICATION",
    "PLANNING",
    "IMPLEMENTATION",
    "VALIDATION",
    "REVIEW",
    "PUBLISHING",
    "MAINTENANCE",
    "UNKNOWN",
})

ALLOWED_STATUSES = frozenset({
    "STARTED",
    "RUNNING",
    "SUCCESS",
    "FAILURE",
    "CANCELLED",
    "TIMEOUT",
    "ABORTED",
})

ALLOWED_OBSERVED_FALLBACKS = frozenset({"unavailable", "unverified"})

ISO8601_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class TimelineValidationError(ValueError):
    """Raised when timeline data fails schema or invariant validation."""


@dataclass(frozen=True)
class UsageMetrics:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    monetary_cost_usd: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "monetary_cost_usd": self.monetary_cost_usd,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> UsageMetrics:
        if not isinstance(data, dict):
            raise TimelineValidationError("Usage metrics must be a JSON object")

        for key in data:
            if key not in {"input_tokens", "output_tokens", "cached_tokens", "monetary_cost_usd"}:
                raise TimelineValidationError(f"Unknown usage metric key: {key}")

        inp = data.get("input_tokens")
        out = data.get("output_tokens")
        cac = data.get("cached_tokens")
        cost = data.get("monetary_cost_usd")

        if inp is not None:
            if not isinstance(inp, int) or isinstance(inp, bool) or inp < 0:
                raise TimelineValidationError("input_tokens must be a non-negative integer")
        if out is not None:
            if not isinstance(out, int) or isinstance(out, bool) or out < 0:
                raise TimelineValidationError("output_tokens must be a non-negative integer")
        if cac is not None:
            if not isinstance(cac, int) or isinstance(cac, bool) or cac < 0:
                raise TimelineValidationError("cached_tokens must be a non-negative integer")
        if cost is not None:
            if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not math.isfinite(cost) or cost < 0.0:
                raise TimelineValidationError("monetary_cost_usd must be a non-negative number")
            cost = float(cost)

        return cls(
            input_tokens=inp,
            output_tokens=out,
            cached_tokens=cac,
            monetary_cost_usd=cost,
        )


@dataclass(frozen=True)
class TimelineEvent:
    schema_version: str
    event_id: str
    task_id: str
    turn_id: int
    stage: str
    agent_role: str
    status: str
    timestamp_start: str
    summary: str
    prompt_id: Optional[str] = None
    parent_agent_role: Optional[str] = None
    session_id: Optional[str] = None
    child_session_id: Optional[str] = None
    model_configured: Optional[str] = None
    effort_configured: Optional[str] = None
    model_observed: Optional[str] = None
    effort_observed: Optional[str] = None
    timestamp_end: Optional[str] = None
    duration_ms: Optional[int] = None
    usage: Optional[UsageMetrics] = None
    evidence_refs: Sequence[str] = field(default_factory=tuple)
    retry_count: int = 0
    handoff_to: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "prompt_id": self.prompt_id,
            "stage": self.stage,
            "agent_role": self.agent_role,
            "parent_agent_role": self.parent_agent_role,
            "session_id": self.session_id,
            "child_session_id": self.child_session_id,
            "model_configured": self.model_configured,
            "effort_configured": self.effort_configured,
            "model_observed": self.model_observed,
            "effort_observed": self.effort_observed,
            "timestamp_start": self.timestamp_start,
            "timestamp_end": self.timestamp_end,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "usage": self.usage.to_dict() if self.usage else None,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "retry_count": self.retry_count,
            "handoff_to": self.handoff_to,
        }


@dataclass(frozen=True)
class RetentionPolicy:
    max_events_per_task: int = 500
    max_age_days: int = 30

    def prune_events(
        self,
        events: Sequence[TimelineEvent],
        now: Optional[datetime] = None,
    ) -> List[TimelineEvent]:
        if not events:
            return []

        if now is None:
            now = datetime.now(timezone.utc)

        kept: List[TimelineEvent] = []
        for event in events:
            try:
                # Parse timestamp_start to check age
                dt_str = event.timestamp_start.replace("Z", "+00:00")
                event_dt = datetime.fromisoformat(dt_str)
                age_days = (now - event_dt).total_seconds() / 86400.0
                if age_days <= self.max_age_days:
                    kept.append(event)
            except Exception:
                # If timestamp is unparseable during retention, keep it for safety
                kept.append(event)

        # Apply maximum count cap (keeping newest events)
        if len(kept) > self.max_events_per_task:
            kept = kept[-self.max_events_per_task :]

        return kept


def _validate_iso8601(ts_str: Any, field_name: str) -> None:
    if not isinstance(ts_str, str) or not ISO8601_REGEX.match(ts_str):
        raise TimelineValidationError(
            f"{field_name} must be a valid ISO-8601 timestamp string, got: {ts_str!r}"
        )
    try:
        datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception as exc:
        raise TimelineValidationError(f"Invalid {field_name} timestamp value: {exc}") from exc


def validate_timeline_event_dict(data: Dict[str, Any]) -> TimelineEvent:
    if not isinstance(data, dict):
        raise TimelineValidationError("Timeline event must be a JSON object")

    allowed_keys = {
        "schema_version",
        "event_id",
        "task_id",
        "turn_id",
        "prompt_id",
        "stage",
        "agent_role",
        "parent_agent_role",
        "session_id",
        "child_session_id",
        "model_configured",
        "effort_configured",
        "model_observed",
        "effort_observed",
        "timestamp_start",
        "timestamp_end",
        "duration_ms",
        "status",
        "usage",
        "summary",
        "evidence_refs",
        "retry_count",
        "handoff_to",
    }

    for key in data:
        if key not in allowed_keys:
            raise TimelineValidationError(f"Unknown key in timeline event: {key}")

    required_keys = {
        "schema_version",
        "event_id",
        "task_id",
        "turn_id",
        "stage",
        "agent_role",
        "status",
        "timestamp_start",
        "summary",
    }
    missing = required_keys - set(data.keys())
    if missing:
        raise TimelineValidationError(f"Missing required fields: {sorted(missing)}")

    schema_version = data["schema_version"]
    if schema_version != TIMELINE_SCHEMA_VERSION:
        raise TimelineValidationError(
            f"Unsupported schema_version: {schema_version!r} (expected {TIMELINE_SCHEMA_VERSION!r})"
        )

    event_id = data["event_id"]
    if not isinstance(event_id, str) or not event_id.strip():
        raise TimelineValidationError("event_id must be a non-empty string")
    try:
        uuid.UUID(event_id)
    except ValueError as exc:
        raise TimelineValidationError(f"event_id must be a valid UUID: {event_id!r}") from exc

    task_id = data["task_id"]
    if not isinstance(task_id, str) or not task_id.strip():
        raise TimelineValidationError("task_id must be a non-empty string")

    turn_id = data["turn_id"]
    if not isinstance(turn_id, int) or isinstance(turn_id, bool) or turn_id < 0:
        raise TimelineValidationError("turn_id must be a non-negative integer")

    stage = data["stage"]
    if stage not in ALLOWED_STAGES:
        raise TimelineValidationError(f"Invalid stage: {stage!r} (allowed: {sorted(ALLOWED_STAGES)})")

    agent_role = data["agent_role"]
    if not isinstance(agent_role, str) or not agent_role.strip():
        raise TimelineValidationError("agent_role must be a non-empty string")

    status = data["status"]
    if status not in ALLOWED_STATUSES:
        raise TimelineValidationError(
            f"Invalid status: {status!r} (allowed: {sorted(ALLOWED_STATUSES)})"
        )

    _validate_iso8601(data["timestamp_start"], "timestamp_start")
    timestamp_start = data["timestamp_start"]

    summary = data["summary"]
    if not isinstance(summary, str):
        raise TimelineValidationError("summary must be a string")

    prompt_id = data.get("prompt_id")
    if prompt_id is not None and (not isinstance(prompt_id, str) or not prompt_id.strip()):
        raise TimelineValidationError("prompt_id if provided must be a non-empty string")

    parent_agent_role = data.get("parent_agent_role")
    if parent_agent_role is not None and (
        not isinstance(parent_agent_role, str) or not parent_agent_role.strip()
    ):
        raise TimelineValidationError("parent_agent_role if provided must be a non-empty string")

    session_id = data.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
        raise TimelineValidationError("session_id if provided must be a non-empty string")

    child_session_id = data.get("child_session_id")
    if child_session_id is not None and (
        not isinstance(child_session_id, str) or not child_session_id.strip()
    ):
        raise TimelineValidationError("child_session_id if provided must be a non-empty string")

    model_configured = data.get("model_configured")
    if model_configured is not None and not isinstance(model_configured, str):
        raise TimelineValidationError("model_configured must be a string or null")

    effort_configured = data.get("effort_configured")
    if effort_configured is not None and not isinstance(effort_configured, str):
        raise TimelineValidationError("effort_configured must be a string or null")

    model_observed = data.get("model_observed")
    if model_observed is not None and not isinstance(model_observed, str):
        raise TimelineValidationError("model_observed must be a string or null")

    effort_observed = data.get("effort_observed")
    if effort_observed is not None and not isinstance(effort_observed, str):
        raise TimelineValidationError("effort_observed must be a string or null")

    timestamp_end = data.get("timestamp_end")
    if timestamp_end is not None:
        _validate_iso8601(timestamp_end, "timestamp_end")
        start_dt = datetime.fromisoformat(timestamp_start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(timestamp_end.replace("Z", "+00:00"))
        if end_dt < start_dt:
            raise TimelineValidationError(
                f"timestamp_end ({timestamp_end}) cannot be earlier than timestamp_start ({timestamp_start})"
            )

    duration_ms = data.get("duration_ms")
    if duration_ms is not None:
        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms < 0:
            raise TimelineValidationError("duration_ms must be a non-negative integer")

    usage_data = data.get("usage")
    usage = UsageMetrics.from_dict(usage_data) if usage_data is not None else None

    evidence_raw = data.get("evidence_refs", [])
    if not isinstance(evidence_raw, (list, tuple)):
        raise TimelineValidationError("evidence_refs must be an array of strings")
    for ref in evidence_raw:
        if not isinstance(ref, str) or not ref.strip():
            raise TimelineValidationError("Each item in evidence_refs must be a non-empty string")

    retry_count = data.get("retry_count", 0)
    if not isinstance(retry_count, int) or isinstance(retry_count, bool) or retry_count < 0:
        raise TimelineValidationError("retry_count must be a non-negative integer")

    handoff_to = data.get("handoff_to")
    if handoff_to is not None and (not isinstance(handoff_to, str) or not handoff_to.strip()):
        raise TimelineValidationError("handoff_to if provided must be a non-empty string")

    return TimelineEvent(
        schema_version=schema_version,
        event_id=event_id,
        task_id=task_id,
        turn_id=turn_id,
        prompt_id=prompt_id,
        stage=stage,
        agent_role=agent_role,
        parent_agent_role=parent_agent_role,
        session_id=session_id,
        child_session_id=child_session_id,
        model_configured=model_configured,
        effort_configured=effort_configured,
        model_observed=model_observed,
        effort_observed=effort_observed,
        timestamp_start=timestamp_start,
        timestamp_end=timestamp_end,
        duration_ms=duration_ms,
        status=status,
        usage=usage,
        summary=summary,
        evidence_refs=tuple(evidence_raw),
        retry_count=retry_count,
        handoff_to=handoff_to,
    )


def serialize_timeline_event(event: TimelineEvent) -> str:
    """Serialize a timeline event deterministically to a compact JSON string."""
    return json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))


def parse_timeline_event(json_str: str) -> TimelineEvent:
    """Parse and validate a single timeline event from a JSON string."""
    try:
        data = json.loads(json_str)
    except Exception as exc:
        raise TimelineValidationError(f"Invalid JSON string: {exc}") from exc
    return validate_timeline_event_dict(data)


def validate_timeline_sequence(events: Sequence[TimelineEvent]) -> Tuple[bool, List[str]]:
    """Validate sequence-level invariants: emission ordering, monotonic clocks, and valid state transitions."""
    if not events:
        return True, []

    errors: List[str] = []
    task_id = events[0].task_id
    last_emission_dt: Optional[datetime] = None
    terminal_statuses = {"SUCCESS", "FAILURE", "CANCELLED", "TIMEOUT", "ABORTED"}
    completed_turns: set[int] = set()

    for idx, ev in enumerate(events):
        if ev.task_id != task_id:
            errors.append(f"Event {idx} has task_id {ev.task_id!r} differing from initial {task_id!r}")

        if ev.turn_id in completed_turns:
            errors.append(
                f"Event {idx} references turn_id {ev.turn_id} which was already closed in a terminal state"
            )

        # Emission timestamp is timestamp_end if turn completed, else timestamp_start
        emission_str = ev.timestamp_end or ev.timestamp_start
        try:
            cur_emission_dt = datetime.fromisoformat(emission_str.replace("Z", "+00:00"))
            if last_emission_dt is not None and cur_emission_dt < last_emission_dt:
                errors.append(
                    f"Event {idx} emission timestamp ({emission_str}) is earlier than previous event emission ({last_emission_dt.isoformat()})"
                )
            last_emission_dt = cur_emission_dt
        except Exception:
            errors.append(f"Event {idx} has unparseable timestamp ({emission_str})")

        if ev.status in terminal_statuses:
            completed_turns.add(ev.turn_id)

    return len(errors) == 0, errors
