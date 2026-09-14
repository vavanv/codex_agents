#!/usr/bin/env python3
"""Verify installed custom agents and optionally run attributed Codex discovery."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_agent_configs import EXPECTED_ROLES, validate_catalog
from workflow_manager import CUSTOM_AGENT_FILES, _detect_codex_version


EVIDENCE_SCHEMA = "codex-agent-verification/v1"
# This parser is intentionally not represented as validated against a captured
# 0.154.0 stream. Synthetic regression fixtures establish fail-closed behavior,
# not compatibility with a live Codex event schema.
DISCOVERY_ADAPTER = "codex-cli-jsonl-unvalidated/v1"
SUPPORTED_RUNTIME_VERSION = "0.154.0"
REASON_CODES = {
    "ATTRIBUTION_CONFLICT",
    "ATTRIBUTION_BEFORE_PARENT",
    "ATTRIBUTION_UNEXPECTED",
    "INVALID_CHILD_SESSION",
    "AUTH_BLOCKED",
    "CODEX_EXECUTABLE_MISSING",
    "CODEX_VERSION_ERROR",
    "DISCOVERY_NOT_REQUESTED",
    "DISCOVERY_ERROR_EVENT",
    "DISCOVERY_LAUNCH_FAILED",
    "DISCOVERY_PROCESS_FAILED",
    "DISCOVERY_TIMEOUT",
    "DUPLICATE_ATTRIBUTION",
    "DISCOVERY_VERIFIED",
    "EVIDENCE_WRITE_FAILED",
    "INSTALLATION_INVALID",
    "MALFORMED_EVENT_STREAM",
    "MISSING_ATTRIBUTION",
    "MISSING_PARENT_SESSION",
    "QUOTA_BLOCKED",
    "STATIC_VERIFIED",
    "TRUNCATED_EVENT_STREAM",
    "UNSUPPORTED_RUNTIME_VERSION",
    "UNSUPPORTED_EVENT_SCHEMA",
    "UNVALIDATED_EVENT_ADAPTER",
}


@dataclass(frozen=True)
class DiscoveryProcessResult:
    return_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    launch_error_code: str | None


@dataclass(frozen=True)
class ParsedDiscovery:
    adapter: str
    stream_integrity: str
    parent_session_id: str | None
    attributed_children: dict[str, str]
    smoke_names: list[str]
    event_count: int
    reason_codes: list[str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_diagnostic(value: str) -> str:
    sanitized = value
    sanitized = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", "[REDACTED]", sanitized)
    sanitized = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[REDACTED]", sanitized, flags=re.DOTALL)
    sanitized = re.sub(r"\bsk-[A-Za-z0-9_-]+\b", "[REDACTED]", sanitized)
    sanitized = re.sub(
        r"(?i)\b(api[_ -]?key|password|token|secret|connection[_ -]?string)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        sanitized,
    )
    sanitized = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@", r"\1[REDACTED]@", sanitized)
    sanitized = re.sub(
        r"(?i)([?&](?:api[_-]?key|password|token|secret|access_token)=)[^&#\s]+",
        r"\1[REDACTED]",
        sanitized,
    )
    return sanitized[-1000:]


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


def run_discovery(target: Path, timeout: int) -> DiscoveryProcessResult:
    prompt = (
        "Spawn each available project custom agent exactly once and return no prose. "
        "The runtime event stream is the only accepted verification evidence."
    )
    if os.name == "nt":
        executable = shutil.which("codex.cmd") or shutil.which("codex.exe")
        if executable:
            command_prefix = [executable]
        else:
            shim = shutil.which("codex.ps1")
            shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
            if not shim or not shell:
                return DiscoveryProcessResult(None, "", "", False, "CODEX_EXECUTABLE_MISSING")
            command_prefix = [shell, "-NoProfile", "-File", shim]
    else:
        executable = shutil.which("codex")
        if not executable:
            return DiscoveryProcessResult(None, "", "", False, "CODEX_EXECUTABLE_MISSING")
        command_prefix = [executable]
    command = command_prefix + [
        "exec",
        "--json",
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
        return DiscoveryProcessResult(result.returncode, result.stdout, result.stderr, False, None)
    except subprocess.TimeoutExpired as error:
        return DiscoveryProcessResult(None, error.stdout or "", error.stderr or "", True, None)
    except OSError as error:
        return DiscoveryProcessResult(None, "", "", False, type(error).__name__)


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
    reasons: set[str],
) -> None:
    if not isinstance(role, str) or not isinstance(child_id, str):
        reasons.add("UNSUPPORTED_EVENT_SCHEMA")
        return
    if role not in EXPECTED_ROLES:
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


def parse_discovery(stdout: str) -> ParsedDiscovery:
    reasons: set[str] = set()
    attributed: dict[str, str] = {}
    smoke_names: set[str] = set()
    parent_session_id: str | None = None
    event_count = 0
    if stdout and not stdout.endswith("\n"):
        reasons.add("TRUNCATED_EVENT_STREAM")
    for raw_line in stdout.splitlines():
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
                reasons,
            )
        elif event_type in {"item.started", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                reasons.add("UNSUPPORTED_EVENT_SCHEMA")
                continue
            item_type = item["type"]
            if item_type == "collaboration_tool_call" and item.get("tool") == "spawn_agent":
                _record_attribution(
                    attributed,
                    item.get("agent_name"),
                    item.get("child_session_id"),
                    parent_session_id,
                    reasons,
                )
            elif item_type == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    smoke_names.update(line for line in text.splitlines() if line in EXPECTED_ROLES)
                else:
                    reasons.add("UNSUPPORTED_EVENT_SCHEMA")
            elif item_type not in {"reasoning", "command_execution"}:
                reasons.add("UNSUPPORTED_EVENT_SCHEMA")
        elif event_type == "error":
            reasons.add("DISCOVERY_ERROR_EVENT")
        elif event_type not in {"turn.started", "turn.completed"}:
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
    if set(attributed) != set(EXPECTED_ROLES):
        reasons.add("MISSING_ATTRIBUTION")
    integrity = "complete" if not reasons else "untrusted"
    return ParsedDiscovery(
        adapter=DISCOVERY_ADAPTER,
        stream_integrity=integrity,
        parent_session_id=parent_session_id,
        attributed_children=dict(sorted(attributed.items())),
        smoke_names=sorted(smoke_names),
        event_count=event_count,
        reason_codes=sorted(reasons),
    )


def _classify_process(result: DiscoveryProcessResult) -> tuple[str, int, list[str]]:
    if result.timed_out:
        return "BLOCKED", 2, ["DISCOVERY_TIMEOUT"]
    if result.launch_error_code is not None:
        if result.launch_error_code == "CODEX_EXECUTABLE_MISSING":
            return "BLOCKED", 2, ["CODEX_EXECUTABLE_MISSING"]
        return "FAIL", 1, ["DISCOVERY_LAUNCH_FAILED"]
    combined = f"{result.stdout}\n{result.stderr}".lower()
    if result.return_code != 0:
        if any(marker in combined for marker in ("not authenticated", "authentication required", "unauthorized")):
            return "BLOCKED", 2, ["AUTH_BLOCKED"]
        if any(marker in combined for marker in ("usage limit", "quota", "rate limit")):
            return "BLOCKED", 2, ["QUOTA_BLOCKED"]
        return "FAIL", 1, ["DISCOVERY_PROCESS_FAILED"]
    return "PASS", 0, []


def _evidence(
    codex_version: str | None,
    scope: str,
    status: str,
    exit_code: int,
    installation: str,
    discovery: str,
    parsed: ParsedDiscovery | None,
    reason_codes: list[str],
) -> dict[str, Any]:
    codes = sorted(set(reason_codes))
    if any(code not in REASON_CODES for code in codes):
        raise ValueError("unknown verifier reason code")
    attributed = sorted(parsed.attributed_children) if parsed else []
    child_ids = dict(parsed.attributed_children) if parsed else {}
    return {
        "schemaVersion": EVIDENCE_SCHEMA,
        "checkedAt": _now(),
        "scope": scope,
        "codexVersion": codex_version,
        "status": status,
        "exitCode": exit_code,
        "installation": installation,
        "discovery": discovery,
        "eventAdapter": parsed.adapter if parsed else DISCOVERY_ADAPTER,
        "expectedAgents": sorted(EXPECTED_ROLES),
        "attributedAgents": attributed,
        "childSessionIds": child_ids,
        "smokeNamesObserved": parsed.smoke_names if parsed else [],
        "eventCount": parsed.event_count if parsed else 0,
        "parseErrorCount": (
            sum(
                code in {"MALFORMED_EVENT_STREAM", "TRUNCATED_EVENT_STREAM", "UNSUPPORTED_EVENT_SCHEMA"}
                for code in parsed.reason_codes
            )
            if parsed
            else 0
        ),
        "reasonCodes": codes,
    }


def _write_evidence(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as file_handle:
            json.dump(value, file_handle, indent=2, sort_keys=True)
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--run-codex", action="store_true")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--evidence", type=Path)
    return parser


def _print_outcome(status: str, reason_codes: list[str], discovery: str) -> None:
    codes = ",".join(reason_codes) if reason_codes else "NONE"
    stream = sys.stdout if status == "PASS" else sys.stderr
    print(f"{status}: reasonCodes={codes}; discovery={discovery}", file=stream)


def main() -> int:
    args = _parser().parse_args()
    target = args.target.resolve()
    source_root = args.source_root.resolve()
    codex_version: str | None = None
    parsed: ParsedDiscovery | None = None
    scope = "runtime-discovery" if args.run_codex else "installed-only"
    status = "FAIL"
    exit_code = 1
    installation = "FAIL"
    discovery = "UNVERIFIED"
    reason_codes: list[str] = []
    try:
        codex_version = _detect_codex_version()
        errors = verify_installed(target, source_root, codex_version)
        if errors:
            reason_codes = ["INSTALLATION_INVALID"]
        else:
            installation = "PASS"
            if not args.run_codex:
                status = "PASS"
                exit_code = 0
                reason_codes = ["STATIC_VERIFIED", "DISCOVERY_NOT_REQUESTED"]
            elif codex_version != SUPPORTED_RUNTIME_VERSION:
                reason_codes = ["UNSUPPORTED_RUNTIME_VERSION"]
            else:
                process_result = run_discovery(target, args.timeout)
                status, exit_code, reason_codes = _classify_process(process_result)
                if status == "PASS":
                    parsed = parse_discovery(process_result.stdout)
                    status = "UNVERIFIED"
                    exit_code = 3
                    reason_codes = sorted(
                        set(parsed.reason_codes) | {"UNVALIDATED_EVENT_ADAPTER"}
                    )
                elif status == "BLOCKED":
                    discovery = "BLOCKED"
    except Exception:
        status = "FAIL"
        exit_code = 1
        reason_codes = ["CODEX_VERSION_ERROR"]
    evidence = _evidence(
        codex_version,
        scope,
        status,
        exit_code,
        installation,
        discovery,
        parsed,
        reason_codes,
    )
    if args.evidence:
        try:
            _write_evidence(args.evidence, evidence)
        except Exception:
            _print_outcome("FAIL", ["EVIDENCE_WRITE_FAILED"], discovery)
            return 1
    _print_outcome(status, reason_codes, discovery)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
