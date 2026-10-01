"""End-to-end multi-agent timeline integration, privacy audit, and export parity tests."""

import csv
import json
from pathlib import Path
import sys
import tempfile
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from timeline_schema import UsageMetrics, validate_timeline_sequence
from timeline_collector import TimelineCollector
from timeline_exporter import TimelineExporter


def test_complete_multi_agent_e2e_workflow():
    """Test full multi-agent task lifecycle from classification to publishing with nested workers and retries."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = Path(tmpdir) / "multi_agent_task.jsonl"
        collector = TimelineCollector("task-e2e-complex-001", ledger_path=ledger)

        # 1. Classification (code_explorer)
        t0 = collector.start_turn(
            stage="CLASSIFICATION",
            agent_role="code_explorer",
            summary="Inspected codebase structure and identified requirements",
            model_configured="gpt-6-luna",
            effort_configured="low",
        )
        collector.complete_turn(
            turn_id=t0.turn_id,
            status="SUCCESS",
            summary="Classified task as non-trivial feature addition",
            model_observed="gpt-6-luna",
            effort_observed="low",
            usage=UsageMetrics(input_tokens=800, output_tokens=150, cached_tokens=300, monetary_cost_usd=0.0012),
            handoff_to="sol_architect",
        )

        # 2. Planning (sol_architect)
        t1 = collector.start_turn(
            stage="PLANNING",
            agent_role="sol_architect",
            summary="Drafting bounded execution plan and sub-worker ownership",
            model_configured="gpt-6-sol",
            effort_configured="medium",
        )

        # 3. Nested Deep Review (sol_architect_deep)
        t2 = collector.start_turn(
            stage="PLANNING",
            agent_role="sol_architect_deep",
            parent_agent_role="sol_architect",
            summary="Deep architecture and security isolation analysis",
            model_configured="gpt-6-sol",
            effort_configured="high",
        )
        collector.complete_turn(
            turn_id=t2.turn_id,
            status="SUCCESS",
            summary="Deep architecture approved with disjoint worktree boundary",
            model_observed="gpt-6-sol",
            effort_observed="high",
            usage=UsageMetrics(input_tokens=2500, output_tokens=400, cached_tokens=1200, monetary_cost_usd=0.0065),
            evidence_refs=["plan/plan.md"],
            handoff_to="sol_architect",
        )

        # Complete t1
        collector.complete_turn(
            turn_id=t1.turn_id,
            status="SUCCESS",
            summary="Finalized execution plan with verified worker bounds",
            model_observed="gpt-6-sol",
            effort_observed="medium",
            usage=UsageMetrics(input_tokens=1200, output_tokens=250, cached_tokens=600, monetary_cost_usd=0.0028),
            evidence_refs=["plan/plan.md"],
            handoff_to="implementer",
        )

        # 4. Implementation with failure & retry
        t3 = collector.start_turn(
            stage="IMPLEMENTATION",
            agent_role="implementer",
            summary="Initial implementation attempt",
            model_configured="gpt-6-luna",
            effort_configured="high",
        )
        collector.complete_turn(
            turn_id=t3.turn_id,
            status="FAILURE",
            summary="Unit test failed due to edge-case division by zero",
            model_observed="gpt-6-luna",
            effort_observed="high",
            usage=UsageMetrics(input_tokens=1800, output_tokens=300, cached_tokens=900, monetary_cost_usd=0.0035),
            retry_count=0,
        )

        # Retry turn (t4)
        t4 = collector.start_turn(
            stage="IMPLEMENTATION",
            agent_role="implementer",
            summary="Retry implementation with zero-guard handling",
            model_configured="gpt-6-luna",
            effort_configured="high",
        )
        collector.complete_turn(
            turn_id=t4.turn_id,
            status="SUCCESS",
            summary="Implementation corrected and local tests pass",
            model_observed="gpt-6-luna",
            effort_observed="high",
            usage=UsageMetrics(input_tokens=1900, output_tokens=320, cached_tokens=950, monetary_cost_usd=0.0038),
            evidence_refs=["src/calculator.py"],
            retry_count=1,
            handoff_to="code_validator",
        )

        # 5. Independent Validation (code_validator)
        t5 = collector.start_turn(
            stage="VALIDATION",
            agent_role="code_validator",
            summary="Executing independent verification test suite",
            model_configured="gpt-6-luna",
            effort_configured="low",
        )
        collector.complete_turn(
            turn_id=t5.turn_id,
            status="SUCCESS",
            summary="All 24 test cases passed with zero regression",
            model_observed="gpt-6-luna",
            effort_observed="low",
            usage=UsageMetrics(input_tokens=1100, output_tokens=180, cached_tokens=500, monetary_cost_usd=0.0018),
            evidence_refs=["tests/test_calculator.py"],
            handoff_to="code_reviewer",
        )

        # 6. Independent Review (code_reviewer)
        t6 = collector.start_turn(
            stage="REVIEW",
            agent_role="code_reviewer",
            summary="Performing security and boundary compliance review",
            model_configured="gpt-6-sol",
            effort_configured="high",
        )
        collector.complete_turn(
            turn_id=t6.turn_id,
            status="SUCCESS",
            summary="Approved changes with zero findings",
            model_observed="gpt-6-sol",
            effort_observed="high",
            usage=UsageMetrics(input_tokens=2200, output_tokens=280, cached_tokens=1100, monetary_cost_usd=0.0052),
            handoff_to="commit_pusher",
        )

        # 7. Publishing (commit_pusher)
        t7 = collector.start_turn(
            stage="PUBLISHING",
            agent_role="commit_pusher",
            summary="Finalizing authorized branch update",
            model_configured="gpt-6-luna",
            effort_configured="low",
        )
        collector.complete_turn(
            turn_id=t7.turn_id,
            status="SUCCESS",
            summary="Branch fast-forwarded cleanly under mode 9C",
            model_observed="gpt-6-luna",
            effort_observed="low",
            usage=UsageMetrics(input_tokens=600, output_tokens=90, cached_tokens=250, monetary_cost_usd=0.0009),
        )

        # Verify Sequence Invariants
        events = collector.read_events()
        valid, errors = validate_timeline_sequence(events)
        assert valid is True
        assert errors == []
        assert len(events) == 16  # 8 started + 8 completed/failed events

        # Exporter Parity Checks
        exporter = TimelineExporter(events, task_id="task-e2e-complex-001")

        # 1. Total Metrics Parity
        total_usage = exporter.calculate_total_usage()
        expected_inp = 800 + 2500 + 1200 + 1800 + 1900 + 1100 + 2200 + 600
        expected_out = 150 + 400 + 250 + 300 + 320 + 180 + 280 + 90
        assert total_usage.input_tokens == expected_inp
        assert total_usage.output_tokens == expected_out

        # 2. Markdown Export Verification
        md = exporter.to_markdown()
        assert "# Task Execution Timeline: `task-e2e-complex-001`" in md
        assert "sol_architect->>sol_architect_deep:" in md
        assert "| 7 | `PUBLISHING` | `commit_pusher` |" in md

        # 3. JSON Export Verification
        json_data = json.loads(exporter.to_json())
        assert json_data["task_id"] == "task-e2e-complex-001"
        assert json_data["event_count"] == 16
        assert json_data["total_usage"]["input_tokens"] == expected_inp

        # 4. CSV Export Verification
        csv_rows = list(csv.reader(exporter.to_csv().strip().splitlines()))
        assert len(csv_rows) == 17  # Header + 16 event rows


def test_security_and_privacy_audit():
    """Verify that credentials, tokens, and secret connection strings are redacted across all formats."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = Path(tmpdir) / "secret_leak_test.jsonl"
        collector = TimelineCollector("task-leak-audit", ledger_path=ledger)

        # Inject various high-risk secret patterns in summaries
        collector.record_event(
            stage="IMPLEMENTATION",
            agent_role="implementer",
            status="SUCCESS",
            summary="Connecting with Bearer ya29.a0AfH6SMD-123456789 and sk-proj-abcdef1234567890",
        )
        collector.record_event(
            stage="MAINTENANCE",
            agent_role="implementer",
            status="SUCCESS",
            summary="Database URI: postgresql://dbuser:UltraSecretPass999@db.internal:5432/prod",
        )

        events = collector.read_events()
        exporter = TimelineExporter(events, task_id="task-leak-audit")

        md_output = exporter.to_markdown()
        json_output = exporter.to_json()
        csv_output = exporter.to_csv()

        forbidden_strings = [
            "ya29.a0AfH6SMD-123456789",
            "sk-proj-abcdef1234567890",
            "UltraSecretPass999",
        ]

        for secret in forbidden_strings:
            assert secret not in md_output, f"Secret leaked in Markdown: {secret}"
            assert secret not in json_output, f"Secret leaked in JSON: {secret}"
            assert secret not in csv_output, f"Secret leaked in CSV: {secret}"


def test_export_parity_and_coverage_alerts():
    """Verify that missing observed metadata correctly triggers coverage alerts across views."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = Path(tmpdir) / "coverage_gap_test.jsonl"
        collector = TimelineCollector("task-coverage-gap", ledger_path=ledger)

        collector.record_event(
            stage="CLASSIFICATION",
            agent_role="code_explorer",
            status="SUCCESS",
            summary="Exploration step",
            model_configured="gpt-6-luna",
            effort_configured="low",
            model_observed="unavailable",
            effort_observed="unavailable",
        )

        events = collector.read_events()
        exporter = TimelineExporter(events, task_id="task-coverage-gap")

        assert exporter.has_coverage_gaps() is True
        md = exporter.to_markdown()
        assert "Partial Observability Detected" in md

        json_data = json.loads(exporter.to_json())
        assert json_data["has_coverage_gaps"] is True

