"""Behavioral validation of the explicit recorder across fresh processes."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from timeline_collector import TimelineCollector
from timeline_exporter import TimelineExporter


def cli(project, *args, success=True):
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts/timeline_cli.py"), "--project", str(project), *args],
        cwd=project, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert (result.returncode == 0) == success, result.stderr
    return result.stdout


def start(project, role="root", parent=None):
    args = ["start", "--task-id", "example", "--stage", "IMPLEMENTATION", "--agent-role", role,
            "--summary", "Synthetic stage", "--model-configured", "synthetic-model", "--effort-configured", "high"]
    if parent:
        args += ["--parent-agent-role", parent]
    cli(project, *args)
    return len({event.turn_id for event in events(project)}) - 1


def finish(project, turn, status="SUCCESS", **kwargs):
    args = ["finish", "--task-id", "example", "--turn-id", str(turn), "--status", status,
            "--summary", "Synthetic outcome"]
    for key, value in kwargs.items():
        args += ["--" + key.replace("_", "-"), str(value)]
    return cli(project, *args)


def ledger(project):
    return project / ".codex-workflow-data/timeline/example.jsonl"


def events(project):
    return TimelineCollector("example", ledger(project)).read_events()


def record_args(*extra):
    return ["record", "--task-id", "example", "--stage", "VALIDATION", "--agent-role", "validator",
            "--status", "SUCCESS", "--summary", "Synthetic outcome", *extra]


def test_separate_process_nested_turns_and_export_parity():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        root = start(project)
        child = start(project, "worker", "root")
        grandchild = start(project, "validator", "worker")
        assert (root, child, grandchild) == (0, 1, 2)
        for turn in (grandchild, child, root):
            finish(project, turn, model_observed="synthetic-model", effort_observed="high",
                   input_tokens=10, output_tokens=5, cached_tokens=2, cost_usd=0.01)
        data = json.loads(cli(project, "show", "--task-id", "example", "--format", "json"))
        assert data["event_count"] == 6
        assert data["total_usage"] == {"input_tokens": 30, "output_tokens": 15, "cached_tokens": 6, "monetary_cost_usd": 0.03}
        assert data["total_duration_ms"] == sum(event.duration_ms for event in events(project) if event.status == "SUCCESS")
        assert data["has_coverage_gaps"] is False
        latest = {event.turn_id: event for event in events(project)}
        assert latest[2].parent_agent_role == "worker"
        assert latest[1].parent_agent_role == "root"
        md = cli(project, "show", "--task-id", "example")
        assert "**Total Turns Recorded:** 3" in md and "30 input, 15 output" in md
        rows = list(csv.DictReader(io.StringIO(cli(project, "show", "--task-id", "example", "--format", "csv"))))
        assert len(rows) == 6
        assert [(int(row["turn_id"]), row["status"]) for row in rows] == [(event.turn_id, event.status) for event in events(project)]
        output = project / "export.json"
        cli(project, "show", "--task-id", "example", "--format", "json", "--output", str(output))
        assert json.loads(output.read_text()) == data
        assert not list(project.rglob("*.pyc"))


@pytest.mark.parametrize("status", ["FAILURE", "CANCELLED", "TIMEOUT"])
def test_terminal_status_and_retry_survive_restart(status):
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        turn = start(project)
        finish(project, turn, status, retry_count=2, handoff_to="reviewer")
        event = events(project)[-1]
        assert event.status == status and event.retry_count == 2 and event.handoff_to == "reviewer"
        assert event.duration_ms >= 0
        assert event.model_observed == "unavailable"
        assert TimelineExporter(events(project)).has_coverage_gaps()


def test_restart_recovery_is_explicit_and_repeatable():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        start(project)
        assert json.loads(cli(project, "show", "--task-id", "example", "--format", "json"))["has_coverage_gaps"]
        restarted = TimelineCollector("example", ledger(project))
        assert [event.status for event in restarted.recover_interrupted_turns()] == ["ABORTED"]
        before = ledger(project).read_bytes()
        assert TimelineCollector("example", ledger(project)).recover_interrupted_turns() == []
        assert ledger(project).read_bytes() == before


def test_partial_metrics_and_absent_duration_remain_unavailable():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        cli(project, *record_args("--input-tokens", "10", "--model-configured", "configured-only"))
        data = json.loads(cli(project, "show", "--task-id", "example", "--format", "json"))
        assert data["total_usage"]["input_tokens"] == 10
        assert data["total_usage"]["output_tokens"] is None and data["total_duration_ms"] is None
        assert data["has_coverage_gaps"] and data["events"][0]["model_observed"] == "unavailable"
        cli(project, *record_args())
        data = json.loads(cli(project, "show", "--task-id", "example", "--format", "json"))
        assert data["total_usage"]["input_tokens"] is None
        assert "Unavailable" in cli(project, "show", "--task-id", "example")


@pytest.mark.parametrize("extra", [
    ["--turn-id", "0"], ["--turn-id", "-1"], ["--cost-usd", "nan"], ["--cost-usd", "inf"],
    ["--cost-usd", "-1"], ["--input-tokens", "-1"], ["--retry-count", "-1"],
    ["--task-id", "../escape"], ["--agent-role", "bad\nrole"], ["--handoff-to", "bad;role"],
])
def test_invalid_record_preserves_existing_ledger(extra):
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        cli(project, *record_args())
        before = ledger(project).read_bytes()
        cli(project, *record_args(*extra), success=False)
        assert ledger(project).read_bytes() == before


def test_invalid_and_repeated_finish_preserve_ledger():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        start(project)
        before = ledger(project).read_bytes()
        cli(project, "finish", "--task-id", "example", "--turn-id", "99", "--status", "SUCCESS", "--summary", "Synthetic", success=False)
        assert ledger(project).read_bytes() == before
        finish(project, 0)
        before = ledger(project).read_bytes()
        cli(project, "finish", "--task-id", "example", "--turn-id", "0", "--status", "SUCCESS", "--summary", "Synthetic", success=False)
        assert ledger(project).read_bytes() == before


def test_redaction_and_csv_formula_guard():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        synthetic = "synthetic-example-token-123"
        cli(project, *record_args("--summary", "=SUM(1,2) Bearer " + synthetic))
        cli(project, *record_args("--summary", json.dumps({"clientSecret": synthetic, "outcome": "ok"})))
        for format in ("markdown", "json", "csv"):
            rendered = cli(project, "show", "--task-id", "example", "--format", format)
            assert synthetic not in rendered and "REDACTED" in rendered
        rows = list(csv.DictReader(io.StringIO(cli(project, "show", "--task-id", "example", "--format", "csv"))))
        assert rows[0]["summary"].startswith("'=SUM")
        assert synthetic.encode() not in ledger(project).read_bytes()


@pytest.mark.parametrize("label", ["PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY"])
def test_multiline_private_key_redacted_before_persistence_and_export(label):
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        # This marker is synthetic text, not a functioning private key.
        marker = "SYNTHETIC-PRIVATE-KEY-CONTENT-NEVER-A-REAL-KEY"
        summary = f"Synthetic setup\n-----BEGIN {label}-----\n{marker}\n-----END {label}-----\nDone"
        cli(project, *record_args("--summary", summary))
        stored = ledger(project).read_text(encoding="utf-8")
        assert marker not in stored and "-----BEGIN" not in stored
        assert "REDACTED" in stored
        for format in ("markdown", "json", "csv"):
            output = cli(project, "show", "--task-id", "example", "--format", format)
            assert marker not in output and "-----BEGIN" not in output
            assert "REDACTED" in output


def test_unicode_summary_roundtrips_through_both_export_entrypoints():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        summary = "Synthetic résumé: готово ✓"
        cli(project, *record_args("--summary", summary))
        for format in ("markdown", "json", "csv"):
            rendered = cli(project, "show", "--task-id", "example", "--format", format)
            if format == "json":
                assert json.loads(rendered)["events"][0]["summary"] == summary
            else:
                assert summary in rendered
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/timeline_exporter.py"),
                                     "--task-id", "example", "--ledger", str(ledger(project)), "--format", format],
                                    cwd=project, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            assert result.returncode == 0, result.stderr
            assert result.stdout == rendered


def test_corrupt_ledger_fails_closed_without_append():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        cli(project, *record_args())
        with ledger(project).open("ab") as stream:
            stream.write(b'{invalid-json}\n')
        before = ledger(project).read_bytes()
        cli(project, *record_args(), success=False)
        cli(project, "show", "--task-id", "example", success=False)
        assert ledger(project).read_bytes() == before


def test_clock_rollback_cannot_append_invalid_sequence():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        collector = TimelineCollector("example", ledger(project))
        collector.record_event("VALIDATION", "validator", "SUCCESS", "Synthetic future event",
                               timestamp_start="2099-01-01T00:00:00Z")
        before = ledger(project).read_bytes()
        cli(project, *record_args(), success=False)
        assert ledger(project).read_bytes() == before


def test_linked_ledger_directory_cannot_escape_project():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        project, external = base / "project", base / "external"
        project.mkdir(); external.mkdir()
        try:
            (project / ".codex-workflow-data").symlink_to(external, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            link = project / ".codex-workflow-data"
            assert link.parent.resolve() == project.resolve()
            assert external.resolve().parent == base.resolve()
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(external)],
                                    capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, "disposable junction creation failed"
        cli(project, *record_args(), success=False)
        assert not list(external.iterdir())
