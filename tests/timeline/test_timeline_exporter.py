"""Tests for user-visible timeline formatting, Mermaid diagrams, and CLI export."""

import json
from pathlib import Path
import sys
import tempfile
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from timeline_schema import UsageMetrics, validate_timeline_event_dict, TIMELINE_SCHEMA_VERSION
from timeline_collector import TimelineCollector
from timeline_exporter import TimelineExporter, main


def _create_sample_events():
    e1 = validate_timeline_event_dict({
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "event_id": "00000000-0000-4000-8000-000000000001",
        "task_id": "task-sample-100",
        "turn_id": 0,
        "stage": "PLANNING",
        "agent_role": "sol_architect",
        "model_configured": "gpt-6-sol",
        "effort_configured": "medium",
        "model_observed": "gpt-6-sol",
        "effort_observed": "medium",
        "timestamp_start": "2026-10-01T12:00:00Z",
        "timestamp_end": "2026-10-01T12:00:04Z",
        "duration_ms": 4000,
        "status": "SUCCESS",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cached_tokens": 500,
            "monetary_cost_usd": 0.003,
        },
        "summary": "Designed module architecture",
        "evidence_refs": ["plan/plan.md"],
        "handoff_to": "implementer",
    })

    e2 = validate_timeline_event_dict({
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "event_id": "00000000-0000-4000-8000-000000000002",
        "task_id": "task-sample-100",
        "turn_id": 1,
        "stage": "IMPLEMENTATION",
        "agent_role": "implementer",
        "parent_agent_role": "sol_architect",
        "model_configured": "gpt-6-luna",
        "effort_configured": "high",
        "model_observed": "unavailable",  # Coverage gap
        "effort_observed": "unavailable",
        "timestamp_start": "2026-10-01T12:00:05Z",
        "timestamp_end": "2026-10-01T12:00:10Z",
        "duration_ms": 5000,
        "status": "SUCCESS",
        "usage": {
            "input_tokens": 2000,
            "output_tokens": 500,
            "cached_tokens": 1000,
            "monetary_cost_usd": 0.005,
        },
        "summary": "Implemented feature functions",
        "evidence_refs": ["src/calculator.py"],
        "handoff_to": "code_validator",
    })

    return [e1, e2]


def test_exporter_markdown_generation():
    events = _create_sample_events()
    exporter = TimelineExporter(events, task_id="task-sample-100")

    md = exporter.to_markdown()

    assert "# Task Execution Timeline: `task-sample-100`" in md
    assert "- **Total Duration:** 9.0s (9000 ms)" in md
    assert "- **Token Usage:** 3,700 total (3,000 input, 700 output, 1,500 cached) ($0.0080)" in md
    assert "Partial Observability Detected" in md  # Triggered by e2 model_observed == unavailable
    assert "```mermaid" in md
    assert "sequenceDiagram" in md
    assert "sol_architect->>implementer:" in md
    assert "| 0 | `PLANNING` | `sol_architect` |" in md
    assert "| 1 | `IMPLEMENTATION` | `implementer` |" in md
    assert "- [`plan/plan.md`](plan/plan.md)" in md


def test_exporter_json_generation():
    events = _create_sample_events()
    exporter = TimelineExporter(events, task_id="task-sample-100")

    json_str = exporter.to_json()
    data = json.loads(json_str)

    assert data["task_id"] == "task-sample-100"
    assert data["total_duration_ms"] == 9000
    assert data["has_coverage_gaps"] is True
    assert data["event_count"] == 2
    assert data["total_usage"]["input_tokens"] == 3000
    assert data["total_usage"]["output_tokens"] == 700
    assert data["total_usage"]["monetary_cost_usd"] == 0.008


def test_exporter_csv_generation():
    events = _create_sample_events()
    exporter = TimelineExporter(events, task_id="task-sample-100")

    csv_str = exporter.to_csv()
    lines = csv_str.strip().splitlines()

    assert len(lines) == 3  # Header + 2 rows
    assert "task_id,turn_id,stage,agent_role" in lines[0]
    assert "task-sample-100,0,PLANNING,sol_architect" in lines[1]
    assert "task-sample-100,1,IMPLEMENTATION,implementer" in lines[2]


def test_exporter_cli_output(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "test_task.jsonl"
        collector = TimelineCollector("task-cli-test", ledger_path=ledger_path)
        collector.start_turn(stage="PLANNING", agent_role="sol_architect", summary="Drafting")
        collector.complete_turn(turn_id=0, status="SUCCESS", summary="Done")

        out_md = Path(tmpdir) / "output.md"

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "timeline_exporter.py",
                "--task-id",
                "task-cli-test",
                "--ledger",
                str(ledger_path),
                "--format",
                "markdown",
                "--output",
                str(out_md),
            ],
        )

        exit_code = main()
        assert exit_code == 0
        assert out_md.exists()
        content = out_md.read_text(encoding="utf-8")
        assert "# Task Execution Timeline: `task-cli-test`" in content
