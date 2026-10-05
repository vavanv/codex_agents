#!/usr/bin/env python3
"""Read-only source and tool preflight for a bounded Codex live capture."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
from pathlib import Path

from capture_live_event import LegacyRunIdentity, _legacy_identity, _owned_run_lock, _resolve_codex_command
from codex_compatibility import CompatibilityRegistry
from live_validation_support import (
    _snapshot_digest,
    atomic_write_json,
    capture_git_snapshot,
    finalize_evidence,
    validate_marker,
)
from validate_agent_configs import AGENT_FILE_ROLES


def _version(argv: list[str], timeout: int) -> str:
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("A required version command failed")
    value = result.stdout.strip()
    if not value or len(value) > 100 or "\n" in value:
        raise ValueError("A required version command returned invalid output")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(
    source_root: Path, timeout: int, codex_command: str, *,
    policy: CompatibilityRegistry | None = None,
    _identity: LegacyRunIdentity | None = None,
) -> dict[str, object]:
    """Return allowlisted preflight facts without creating a fixture or running a model."""
    identity = _identity or _legacy_identity(source_root, policy)
    if timeout < 1 or timeout > 300:
        raise ValueError("Timeout must be between 1 and 300 seconds")
    source = source_root.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Source root must be a directory")
    config = source / "compatibility" / "codex-agents.json"
    agents = source / "agents"
    if not config.is_file() or not agents.is_dir():
        raise ValueError("The compatibility registry or agent catalog is missing")
    found = {path.stem for path in agents.glob("*.toml")}
    if found != set(AGENT_FILE_ROLES):
        raise ValueError("The agent catalog does not contain exactly nine known roles")

    codex_version = _version([*_resolve_codex_command(codex_command), "--version"], timeout)
    if codex_version != identity.profile.cli_banner:
        raise ValueError("The active Codex CLI does not match the pinned version")
    git = shutil.which("git")
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if git is None or powershell is None:
        raise ValueError("Git and PowerShell are required")
    git_version = _version([git, "--version"], timeout)
    powershell_version = _version(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"],
        timeout,
    )
    return {
        "schema": "codex-live-preflight/v1",
        "status": "SOURCE_PREFLIGHT_READY",
        "codexVersion": codex_version,
        "platform": platform.platform(),
        "pythonVersion": platform.python_version(),
        "gitVersion": git_version,
        "powershellVersion": powershell_version,
        "configurationSha256": identity.policy.sha256,
        "sourceAgentSha256": {
            role: _sha256(agents / f"{stem}.toml")
            for stem, role in sorted(AGENT_FILE_ROLES.items())
        },
        "proposedCaptureLimit": {"maxRoles": 1, "maxCalls": 1, "timeoutSeconds": timeout},
        "fixtureBaseline": None,
        "installedAgentSha256": None,
        "runtimeValidated": False,
    }


def bind_fixture(
    source_root: Path, run_root: Path, timeout: int, codex_command: str, *,
    policy: CompatibilityRegistry | None = None,
) -> dict[str, object]:
    """Bind the source preflight to an owned, installed fixture before capture."""
    identity = _legacy_identity(source_root, policy)
    root, _ = validate_marker(run_root)
    with _owned_run_lock(root):
        _, marker = validate_marker(root)
        if marker["lifecycle"] != "ready" or marker["activeWorkers"]:
            raise ValueError("The owned fixture must be ready and worker-free")
        output = root / "results" / "preflight.json"
        if output.exists():
            raise ValueError("The owned fixture already has a bound preflight")
        fixture = root / "fixture"
        installed = fixture / ".codex" / "agents"
        source_preflight = prepare(
            source_root, timeout, codex_command, policy=identity.policy, _identity=identity
        )
        installed_hashes = {}
        for stem, role in sorted(AGENT_FILE_ROLES.items()):
            path = installed / f"{stem}.toml"
            if not path.is_file():
                raise ValueError("An installed agent definition is missing")
            digest = _sha256(path)
            if digest != source_preflight["sourceAgentSha256"][role]:
                raise ValueError("An installed agent definition differs from the source")
            installed_hashes[role] = digest
        snapshot = capture_git_snapshot(root, timeout)
        result = {
            **source_preflight,
            "status": "FIXTURE_PREFLIGHT_READY",
            "fixtureIdentity": marker["runId"],
            "fixtureBaseline": {
                "head": snapshot["head"],
                "stateSha256": _snapshot_digest(snapshot),
                "fileHashes": snapshot["fileHashes"],
            },
            "installedAgentSha256": installed_hashes,
        }
        atomic_write_json(output, result)
        finalize_evidence(root, marker)
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).absolute().parent.parent)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--codex-command", default="codex")
    parser.add_argument("--owned-run-root", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.owned_run_root is None:
            result = prepare(args.source_root, args.timeout, args.codex_command)
        else:
            result = bind_fixture(
                args.source_root, args.owned_run_root, args.timeout, args.codex_command
            )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print(json.dumps({"schema": "codex-live-preflight/v1", "status": "BLOCKED", "runtimeValidated": False}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
