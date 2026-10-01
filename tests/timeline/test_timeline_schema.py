"""Tests for timeline schema, validation, serialization, and retention policy."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import uuid
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from timeline_schema import (
    TIMELINE_SCHEMA_VERSION,
    ALLOWED_STAGES,
    ALLOWED_STATUSES,
    TimelineEvent,
    TimelineValidationError,
    UsageMetrics,
    RetentionPolicy,
    validate_timeline_event_dict,
    serialize_timeline_event,
    parse_timeline_event,
    validate_timeline_sequence,
)


def _make_valid_event_dict(**overrides):
    base = {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "task_id": "task-test-123",
        "turn_id": 1,
        "prompt_id": "prompt-001",
        "stage": "IMPLEMENTATION",
        "agent_role": "implementer",
        "parent_agent_role": "sol_architect",
        "session_id": str(uuid.uuid4()),
        "child_session_id": str(uuid.uuid4()),
        "model_configured": "gpt-6-luna",
        "effort_configured": "high",
        "model_observed": "gpt-6-luna",
        "effort_observed": "high",
        "timestamp_start": "2026-10-01T12:00:00Z",
        "timestamp_end": "2026-10-01T12:00:05Z",
        "duration_ms": 5000,
        "status": "SUCCESS",
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 350,
            "cached_tokens": 800,
            "monetary_cost_usd": 0.0045,
        },
        "summary": "Implemented calculator feature per architecture spec",
        "evidence_refs": ["plan/evidence/l5_0_159_0_snapshot_matrix.json"],
        "retry_count": 0,
        "handoff_to": "code_validator",
    }
    base.update(overrides)
    return base


def test_valid_event_roundtrip():
    data = _make_valid_event_dict()
    event = validate_timeline_event_dict(data)

    assert event.schema_version == TIMELINE_SCHEMA_VERSION
    assert event.task_id == "task-test-123"
    assert event.turn_id == 1
    assert event.stage == "IMPLEMENTATION"
    assert event.status == "SUCCESS"
    assert event.usage is not None
    assert event.usage.input_tokens == 1200
    assert event.usage.monetary_cost_usd == 0.0045

    serialized = serialize_timeline_event(event)
    assert isinstance(serialized, str)

    parsed = parse_timeline_event(serialized)
    assert parsed == event
    assert parsed.to_dict() == data


def test_minimal_valid_event():
    minimal_data = {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "task_id": "task-mini",
        "turn_id": 0,
        "stage": "CLASSIFICATION",
        "agent_role": "code_explorer",
        "status": "STARTED",
        "timestamp_start": "2026-10-01T10:00:00Z",
        "summary": "Starting initial task classification",
    }
    event = validate_timeline_event_dict(minimal_data)
    assert event.turn_id == 0
    assert event.usage is None
    assert event.prompt_id is None
    assert event.evidence_refs == ()


def test_unsupported_schema_version():
    data = _make_valid_event_dict(schema_version="codex-timeline-event/v2")
    with pytest.raises(TimelineValidationError, match="Unsupported schema_version"):
        validate_timeline_event_dict(data)


def test_missing_required_fields():
    data = _make_valid_event_dict()
    del data["task_id"]
    with pytest.raises(TimelineValidationError, match="Missing required fields.*task_id"):
        validate_timeline_event_dict(data)


def test_invalid_uuid_event_id():
    data = _make_valid_event_dict(event_id="not-a-valid-uuid")
    with pytest.raises(TimelineValidationError, match="event_id must be a valid UUID"):
        validate_timeline_event_dict(data)


def test_invalid_stage():
    data = _make_valid_event_dict(stage="INVALID_STAGE")
    with pytest.raises(TimelineValidationError, match="Invalid stage"):
        validate_timeline_event_dict(data)


def test_invalid_status():
    data = _make_valid_event_dict(status="PENDING")
    with pytest.raises(TimelineValidationError, match="Invalid status"):
        validate_timeline_event_dict(data)


def test_invalid_timestamp_format():
    data = _make_valid_event_dict(timestamp_start="2026/10/01 12:00:00")
    with pytest.raises(TimelineValidationError, match="timestamp_start must be a valid ISO-8601"):
        validate_timeline_event_dict(data)


def test_timestamp_end_before_start():
    data = _make_valid_event_dict(
        timestamp_start="2026-10-01T12:05:00Z",
        timestamp_end="2026-10-01T12:00:00Z",
    )
    with pytest.raises(TimelineValidationError, match="timestamp_end .* cannot be earlier than timestamp_start"):
        validate_timeline_event_dict(data)


def test_negative_turn_id():
    data = _make_valid_event_dict(turn_id=-1)
    with pytest.raises(TimelineValidationError, match="turn_id must be a non-negative integer"):
        validate_timeline_event_dict(data)


def test_unknown_field_rejected():
    data = _make_valid_event_dict(unknown_injected_field="malicious_payload")
    with pytest.raises(TimelineValidationError, match="Unknown key in timeline event"):
        validate_timeline_event_dict(data)


def test_usage_metrics_validation():
    # Valid None
    data = _make_valid_event_dict(usage=None)
    ev = validate_timeline_event_dict(data)
    assert ev.usage is None

    # Invalid negative tokens
    bad_data = _make_valid_event_dict(usage={"input_tokens": -5})
    with pytest.raises(TimelineValidationError, match="input_tokens must be a non-negative integer"):
        validate_timeline_event_dict(bad_data)

    # Invalid string token count
    bad_data2 = _make_valid_event_dict(usage={"input_tokens": "100"})
    with pytest.raises(TimelineValidationError, match="input_tokens must be a non-negative integer"):
        validate_timeline_event_dict(bad_data2)

    # Invalid unknown usage key
    bad_data3 = _make_valid_event_dict(usage={"unsupported_metric": 50})
    with pytest.raises(TimelineValidationError, match="Unknown usage metric key"):
        validate_timeline_event_dict(bad_data3)


def test_validate_timeline_sequence_valid():
    ev1 = validate_timeline_event_dict(
        _make_valid_event_dict(
            turn_id=1,
            timestamp_start="2026-10-01T10:00:00Z",
            stage="CLASSIFICATION",
        )
    )
    ev2 = validate_timeline_event_dict(
        _make_valid_event_dict(
            turn_id=2,
            timestamp_start="2026-10-01T10:05:00Z",
            stage="PLANNING",
        )
    )
    ev3 = validate_timeline_event_dict(
        _make_valid_event_dict(
            turn_id=3,
            timestamp_start="2026-10-01T10:10:00Z",
            stage="IMPLEMENTATION",
        )
    )

    valid, errors = validate_timeline_sequence([ev1, ev2, ev3])
    assert valid is True
    assert errors == []


def test_validate_timeline_sequence_invariants():
    ev1 = validate_timeline_event_dict(
        _make_valid_event_dict(
            task_id="task-1",
            turn_id=1,
            status="SUCCESS",
            timestamp_start="2026-10-01T10:10:00Z",
            timestamp_end="2026-10-01T10:15:00Z",
        )
    )
    ev2 = validate_timeline_event_dict(
        _make_valid_event_dict(
            task_id="task-2",  # mismatched task ID
            turn_id=1,         # reusing closed turn_id
            status="RUNNING",
            timestamp_start="2026-10-01T10:05:00Z",  # backwards emission timestamp
            timestamp_end=None,
        )
    )

    valid, errors = validate_timeline_sequence([ev1, ev2])
    assert valid is False
    assert len(errors) == 3
    assert any("task_id" in e for e in errors)
    assert any("already closed in a terminal state" in e for e in errors)
    assert any("emission timestamp" in e for e in errors)


def test_retention_policy_pruning():
    policy = RetentionPolicy(max_events_per_task=3, max_age_days=10)
    now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

    # 1 expired event (15 days old)
    old_ev = validate_timeline_event_dict(
        _make_valid_event_dict(
            turn_id=1,
            timestamp_start=(now - timedelta(days=15)).isoformat(),
        )
    )
    # 4 fresh events (1-4 days old)
    fresh_events = [
        validate_timeline_event_dict(
            _make_valid_event_dict(
                turn_id=i,
                timestamp_start=(now - timedelta(days=5 - i)).isoformat(),
            )
        )
        for i in range(2, 6)
    ]

    all_events = [old_ev] + fresh_events
    pruned = policy.prune_events(all_events, now=now)

    # old_ev should be pruned by age (leaves 4 events)
    # then max_events_per_task=3 should keep the 3 newest events (turn_id 3, 4, 5)
    assert len(pruned) == 3
    assert [e.turn_id for e in pruned] == [3, 4, 5]
