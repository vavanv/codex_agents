"""Tests for timeline collector execution tracking, token accounting, and recovery."""

import os
from pathlib import Path
import sys
import tempfile
import uuid
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from timeline_schema import (
    TimelineValidationError,
    UsageMetrics,
    RetentionPolicy,
)
from timeline_collector import TimelineCollector


@pytest.fixture
def temp_ledger():
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "test_task.jsonl"
        yield ledger_path


def test_collector_turn_lifecycle(temp_ledger):
    collector = TimelineCollector("task-life-1", ledger_path=temp_ledger)

    # 1. Start turn
    start_ev = collector.start_turn(
        stage="PLANNING",
        agent_role="sol_architect",
        summary="Drafting plan for multi-agent execution",
        model_configured="gpt-6-sol",
        effort_configured="medium",
    )
    assert start_ev.turn_id == 0
    assert start_ev.status == "STARTED"
    assert start_ev.model_configured == "gpt-6-sol"
    assert start_ev.model_observed == "unavailable"  # unobserved at start

    # 2. Complete turn
    usage = UsageMetrics(input_tokens=1500, output_tokens=200, cached_tokens=500, monetary_cost_usd=0.003)
    comp_ev = collector.complete_turn(
        turn_id=start_ev.turn_id,
        status="SUCCESS",
        summary="Architecture plan drafted successfully",
        model_observed="gpt-6-sol",
        effort_observed="medium",
        usage=usage,
        evidence_refs=["plan/plan.md"],
        handoff_to="implementer",
    )

    assert comp_ev.turn_id == 0
    assert comp_ev.status == "SUCCESS"
    assert comp_ev.model_observed == "gpt-6-sol"
    assert comp_ev.effort_observed == "medium"
    assert comp_ev.usage == usage
    assert comp_ev.duration_ms is not None
    assert comp_ev.duration_ms >= 0
    assert comp_ev.handoff_to == "implementer"

    # Read back from ledger
    events = collector.read_events()
    assert len(events) == 2
    assert events[0].status == "STARTED"
    assert events[1].status == "SUCCESS"


def test_collector_secret_redaction(temp_ledger):
    collector = TimelineCollector("task-sec-1", ledger_path=temp_ledger)

    event = collector.record_event(
        stage="IMPLEMENTATION",
        agent_role="implementer",
        status="RUNNING",
        summary="Configuring connection string mongodb://admin:SecretPass123@localhost:27017 and sk-1234567890abcdef1234",
    )

    assert "SecretPass123" not in event.summary
    assert "sk-1234567890abcdef1234" not in event.summary
    assert "[REDACTED]" in event.summary


def test_collector_crash_recovery(temp_ledger):
    collector = TimelineCollector("task-crash-1", ledger_path=temp_ledger)

    # Start turn 0 and turn 1
    t0 = collector.start_turn(stage="PLANNING", agent_role="sol_architect", summary="Start plan")
    t1 = collector.start_turn(stage="IMPLEMENTATION", agent_role="implementer", summary="Start impl")

    # Complete t0 only
    collector.complete_turn(turn_id=t0.turn_id, status="SUCCESS", summary="Plan ready")

    # Simulate crash and restart recovery on new collector instance
    new_collector = TimelineCollector("task-crash-1", ledger_path=temp_ledger)
    recovered = new_collector.recover_interrupted_turns()

    assert len(recovered) == 1
    assert recovered[0].turn_id == t1.turn_id
    assert recovered[0].status == "ABORTED"
    assert "Interrupted execution recovered" in recovered[0].summary

    # Verify ledger state
    events = new_collector.read_events()
    assert len(events) == 4  # t0 start, t1 start, t0 comp, t1 aborted


def test_collector_observed_fallbacks(temp_ledger):
    collector = TimelineCollector("task-obs-1", ledger_path=temp_ledger)

    ev = collector.record_event(
        stage="VALIDATION",
        agent_role="code_validator",
        status="SUCCESS",
        summary="Validated tests",
        model_configured="gpt-6-luna",
        effort_configured="low",
        model_observed=None,  # Missing observation
        effort_observed=None,
    )

    # Fallback to unavailable
    assert ev.model_observed == "unavailable"
    assert ev.effort_observed == "unavailable"
    assert ev.model_configured == "gpt-6-luna"


def test_collector_retention_prune(temp_ledger):
    policy = RetentionPolicy(max_events_per_task=2, max_age_days=30)
    collector = TimelineCollector("task-prune-1", ledger_path=temp_ledger, retention_policy=policy)

    for i in range(5):
        collector.record_event(
            stage="CLASSIFICATION",
            agent_role="code_explorer",
            status="SUCCESS",
            summary=f"Turn step {i}",
        )

    assert len(collector.read_events()) == 5
    pruned_count = collector.prune_ledger()
    assert pruned_count == 3
    assert len(collector.read_events()) == 2
