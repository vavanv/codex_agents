"""Timeline package lifecycle tests confined to disposable projects."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import workflow_manager as manager

MODULES = ("timeline_schema.py", "timeline_privacy.py", "timeline_collector.py", "timeline_exporter.py", "timeline_cli.py")


def install(target, source=ROOT, dry_run=False):
    with patch.object(manager, "_validate_codex_version", return_value="0.160.0"), redirect_stdout(StringIO()):
        manager.install(target, source, dry_run)


def uninstall(target):
    with redirect_stdout(StringIO()):
        manager.uninstall(target, False)


def run_installed(target, cwd, *args):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-B", str(target / ".codex-workflow/timeline/timeline_cli.py"),
                             "--project", str(target), *args], cwd=cwd, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stdout


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def copy_source(root):
    for relative in list(manager.PACKAGE_FILES) + list(manager.PROJECT_TEMPLATE_FILES) + [manager.COMPATIBILITY_FILE]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, path)


def test_timeline_dry_run_idempotence_packaged_hashes_and_clean_uninstall():
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        install(target, dry_run=True)
        assert list(target.iterdir()) == []
        install(target)
        first = snapshot(target)
        install(target)
        assert snapshot(target) == first
        state = json.loads((target / manager.STATE_FILENAME).read_text())
        for name in MODULES:
            relative = ".codex-workflow/timeline/" + name
            expected = (ROOT / "scripts" / name).read_bytes()
            assert (target / relative).read_bytes() == expected
            assert state["files"][relative]["installedHash"] == manager._sha256_bytes(expected)
            assert state["files"][relative]["source"] == "scripts/" + name
        assert ".codex-workflow/timeline" in state["createdDirectories"]
        uninstall(target)
        assert list(target.iterdir()) == []


def test_installed_cli_runs_outside_source_and_ledger_survives_uninstall():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        target, cwd = base / "target", base / "unrelated-working-directory"
        target.mkdir(); cwd.mkdir()
        install(target)
        run_installed(target, cwd, "start", "--task-id", "installed", "--stage", "PLANNING", "--agent-role", "root", "--summary", "Synthetic plan")
        run_installed(target, cwd, "finish", "--task-id", "installed", "--turn-id", "0", "--status", "SUCCESS", "--summary", "Synthetic done")
        data = json.loads(run_installed(target, cwd, "show", "--task-id", "installed", "--format", "json"))
        assert [event["status"] for event in data["events"]] == ["STARTED", "SUCCESS"]
        ledger = target / ".codex-workflow-data/timeline/installed.jsonl"
        before = ledger.read_bytes()
        install(target)
        assert ledger.read_bytes() == before
        uninstall(target)
        assert ledger.read_bytes() == before
        assert not (target / ".codex-workflow").exists()
        assert not (target / manager.STATE_FILENAME).exists()
        assert not list(base.rglob("*.pyc"))


def test_timeline_source_update_and_modified_code_preservation():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        source, target = base / "source", base / "target"
        target.mkdir(); copy_source(source)
        install(target, source)
        module = source / "scripts/timeline_privacy.py"
        module.write_bytes(module.read_bytes() + b"\n# synthetic package update\n")
        install(target, source)
        installed = target / ".codex-workflow/timeline/timeline_privacy.py"
        assert installed.read_bytes() == module.read_bytes()
        installed.write_bytes(installed.read_bytes() + b"\n# project owned modification\n")
        before = snapshot(target)
        install(target, source)
        assert snapshot(target) == before
        uninstall(target)
        assert installed.read_bytes() == before[str(installed.relative_to(target))]
        remaining = json.loads((target / manager.STATE_FILENAME).read_text())
        assert ".codex-workflow/timeline/timeline_privacy.py" in remaining["files"]


def test_legacy_pre_timeline_manifest_upgrades_without_losing_project_data():
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        install(target)
        # Reconstruct a supported pre-timeline state manifest using the installed
        # transaction's real root identities; no fabricated version/schema.
        state_path = target / manager.STATE_FILENAME
        state = json.loads(state_path.read_text())
        for name in MODULES:
            relative = ".codex-workflow/timeline/" + name
            (target / relative).unlink()
            del state["files"][relative]
        (target / ".codex-workflow/timeline").rmdir()
        state["createdDirectories"].remove(".codex-workflow/timeline")
        state["directoryIdentities"].pop(".codex-workflow/timeline", None)
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        sentinel = target / "project-owned.txt"
        sentinel.write_bytes(b"synthetic project data")
        assert not (target / ".codex-workflow/timeline").exists()
        install(target)
        for name in MODULES:
            assert (target / ".codex-workflow/timeline" / name).is_file()
        assert sentinel.read_bytes() == b"synthetic project data"
        uninstall(target)
        assert snapshot(target) == {"project-owned.txt": b"synthetic project data"}


def test_fault_during_timeline_install_rolls_back_and_retry_succeeds():
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        sentinel = target / "project-owned.txt"
        sentinel.write_bytes(b"synthetic project data")
        before = snapshot(target)
        original = manager.PathRepositoryAdapter.atomic_write
        failed = False

        def fail_once(adapter, relative, content):
            nonlocal failed
            if relative == ".codex-workflow/timeline/timeline_collector.py" and not failed:
                failed = True
                raise OSError("synthetic timeline write failure")
            return original(adapter, relative, content)

        with patch.object(manager.PathRepositoryAdapter, "atomic_write", fail_once):
            with pytest.raises((manager.WorkflowError, OSError)):
                install(target)
        assert failed
        assert snapshot(target) == before
        assert not (target / manager.JOURNAL_FILENAME).exists()
        assert not (target / ".codex-workflow/timeline").exists()
        install(target)
        uninstall(target)
        assert snapshot(target) == before
