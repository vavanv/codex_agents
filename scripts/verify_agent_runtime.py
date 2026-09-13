#!/usr/bin/env python3
"""Verify installed custom agents and optionally run a live Codex discovery check."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from validate_agent_configs import EXPECTED_ROLES, validate_catalog
from workflow_manager import CUSTOM_AGENT_FILES, _detect_codex_version


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_output(output: str) -> str:
    output = re.sub(r"(?i)(api[_ -]?key|token|password|secret)\s*[:=]\s*\S+", r"\1=[REDACTED]", output)
    output = re.sub(r"\bsk-[A-Za-z0-9_-]+\b", "[REDACTED]", output)
    return output[-4000:]


def verify_installed(target: Path, source_root: Path, codex_version: str) -> list[str]:
    errors: list[str] = []
    report = validate_catalog(source_root, codex_version)
    errors.extend(report.errors)
    for source_relative, installed_relative in CUSTOM_AGENT_FILES.items():
        source_path = source_root / source_relative
        installed_path = target / installed_relative
        if not installed_path.is_file():
            errors.append(f"missing installed agent: {installed_relative}")
        elif installed_path.read_bytes() != source_path.read_bytes():
            errors.append(f"installed agent differs from package source: {installed_relative}")
    return errors


def run_discovery(target: Path, timeout: int) -> tuple[int, str]:
    prompt = (
        "Without reading project files or running tools, list the names of all project-level "
        "custom agents available in this session, one name per line. Return names only."
    )
    if os.name == "nt":
        executable = shutil.which("codex.cmd") or shutil.which("codex.exe")
        if executable:
            command_prefix = [executable]
        else:
            shim = shutil.which("codex.ps1")
            shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
            if not shim or not shell:
                return 1, "unable to locate Codex executable or PowerShell shim"
            command_prefix = [shell, "-NoProfile", "-File", shim]
    else:
        executable = shutil.which("codex")
        if not executable:
            return 1, "unable to locate Codex executable"
        command_prefix = [executable]
    command = command_prefix + [
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--strict-config",
        "--sandbox",
        "read-only",
        "--config",
        'approval_policy="never"',
        "--cd",
        str(target),
        prompt,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, f"unable to run Codex discovery check: {error}"
    output = f"{result.stdout}\n{result.stderr}".strip()
    return result.returncode, output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--run-codex", action="store_true")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--evidence", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    target = args.target.resolve()
    source_root = args.source_root.resolve()
    evidence: dict[str, object] = {
        "checkedAt": _now(),
        "codexVersion": None,
        "discovery": "NOT VERIFIED",
        "errors": [],
        "expectedAgents": sorted(EXPECTED_ROLES),
        "status": "PASS",
        "target": str(target),
    }
    try:
        codex_version = _detect_codex_version()
        evidence["codexVersion"] = codex_version
        errors = verify_installed(target, source_root, codex_version)
        if args.run_codex and not errors:
            return_code, output = run_discovery(target, args.timeout)
            evidence["discoveryOutput"] = _sanitize_output(output)
            missing = [name for name in EXPECTED_ROLES if name not in output]
            if return_code != 0:
                if "usage limit" in output.lower() or "not authenticated" in output.lower():
                    evidence["discovery"] = "BLOCKED"
                    evidence["status"] = "BLOCKED"
                    errors.append(
                        f"Codex discovery is blocked by the environment (exit code {return_code}): "
                        f"{_sanitize_output(output)}"
                    )
                else:
                    errors.append(
                        f"Codex discovery command failed with exit code {return_code}: "
                        f"{_sanitize_output(output)}"
                    )
            elif missing:
                errors.append(f"Codex discovery output omitted: {', '.join(sorted(missing))}")
            else:
                evidence["discovery"] = "PASS"
        evidence["errors"] = errors
    except Exception as error:  # Convert verifier failures to evidence without a traceback.
        evidence["errors"] = [str(error)]

    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    errors = evidence["errors"]
    if errors:
        for error in errors:
            prefix = "BLOCKED" if evidence["status"] == "BLOCKED" else "FAIL"
            print(f"{prefix}: {error}", file=sys.stderr)
        return 2 if evidence["status"] == "BLOCKED" else 1
    print(f"PASS: installed definitions match package sources for {evidence['codexVersion']}")
    print(f"RUNTIME DISCOVERY: {evidence['discovery']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
