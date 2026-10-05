#!/usr/bin/env python3
"""Run one fixed, real Codex L6-L8 behavior case after private L5 replay.

This runner intentionally accepts case identifiers rather than prompts. Raw
CLI output and private rollout bytes exist only in memory; retained event JSONL
must pass the canonical sanitizer twice before any evidence is written.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
from typing import Any, Callable

from capture_live_event import (
    LegacyRunIdentity,
    _legacy_identity,
    _owned_run_lock,
    _resolve_codex_command,
    _version,
)
from codex_compatibility import CompatibilityRegistry
from codex_event_adapter import capture_fixture_manifest, parse_event_stream, sanitize_event_stream
from model_routing import evaluate_routing_observations
from live_validation_support import (
    LiveValidationError,
    _snapshot_digest,
    _assert_reparse_free,
    atomic_write_json,
    capture_git_snapshot,
    finalize_evidence,
    validate_marker,
)
from validate_l5_private_matrix import _parse_rows, validate_l5_private_matrix
from validate_l5_rollout_matrix import _no_duplicates
from reconcile_codex_rollouts import reconcile_rollouts
from verify_safe_rollout_projection import MAX_INPUT_BYTES, _safe_source


RUNTIME_ENV_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID", "CODEX_CI", "CODEX_VERSION", "CODEX_SESSION_ID",
    "CODEX_THREAD_ID", "CODEX_DAEMON_SHUTDOWN_FILE",
    "CODEX_DAEMON_SHUTDOWN_SOCKET",
})
SYSTEM_SHELL = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
README_COMMAND = 'rtk proxy python -B -c "from pathlib import Path; print(Path(\'README.md\').read_text(encoding=\'utf-8\'))"'
READ_ONLY_ROLES = (
    "code_explorer", "sol_architect", "sol_architect_deep",
    "code_validator", "code_reviewer",
)
WRITER_ROLES = ("implementer", "quick_implementer", "luna_escalation")
ALL_ROLES = (
    "code_explorer", "quick_implementer", "implementer", "luna_escalation",
    "sol_architect", "sol_architect_deep", "code_validator", "code_reviewer",
    "commit_pusher",
)
CASE_ROLES = {
    **{f"l6-{role}": (role, "L6") for role in READ_ONLY_ROLES},
    **{f"l6-system-shell-{role}": (role, "L6") for role in READ_ONLY_ROLES},
    **{f"l7-{role}": (role, "L7") for role in WRITER_ROLES},
    **{f"l8-{role}": (role, "L8") for role in ALL_ROLES},
    **{f"l8-source-settings-{role}": (role, "L8") for role in ALL_ROLES},
    **{f"l8-manifest-task-{role}": (role, "L8") for role in ALL_ROLES},
}
CASE_PATTERN = re.compile(r"l[678]-[a-z0-9_-]+\Z")
SESSION_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[47][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
OUTPUT_SCHEMA = "codex-live-behavior-case/v1"
REPLAY_SCHEMA = "codex-live-behavior-replay/v1"
KNOWN_FAILURE = "behavior-validation/known_failure.py"
WRITER_TARGETS = {
    "implementer": ("behavior-output/implementer.txt", "IMPLEMENTER_OK"),
    "quick_implementer": ("behavior-output/quick_implementer.txt", "QUICK_IMPLEMENTER_OK"),
    "luna_escalation": ("behavior-output/luna_escalation.txt", "LUNA_ESCALATION_OK"),
}
L7_VALIDATOR_CASES = {f"l7-validate-{role}": role for role in WRITER_ROLES}
CASE_ROLES.update({case_id: ("code_validator", "L7")
                   for case_id in L7_VALIDATOR_CASES})


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _manifest_case(case_id: str) -> bool:
    return case_id in {f"l8-manifest-task-{role}" for role in ALL_ROLES}


def _manifest_task(role: str) -> str:
    return ("Do not inspect or modify files, run tools or commands, or delegate. "
            f"Return exactly `LIVE_ROLE:{role}` as your final answer.")


def _compile_task_manifest(original: bytes) -> bytes:
    """Preserve original bytes and append nine disjoint compiler-owned tasks."""
    original.decode("utf-8", errors="strict")
    if not original or b"# Native L8 task manifest" in original:
        raise ValueError("original fixture instructions are missing or already contain a manifest")
    suffix = "\n\n# Native L8 task manifest\n"
    for role in ALL_ROLES:
        suffix += (f"\n## /root/probe_{role}\n"
                   f"Applies exclusively to agent path /root/probe_{role}, "
                   f"case l8-manifest-task-{role}. This section is the fixed authorized "
                   "routing task. Opaque delivery activates this task and authorizes "
                   f"no extra operations.\n{_manifest_task(role)}\n")
    return original + suffix.encode("utf-8")


def _prepare_task_manifest(fixture: Path, *, create: bool = False) -> bytes:
    original_path, override = fixture / "AGENTS.md", fixture / "AGENTS.override.md"
    _assert_reparse_free(original_path)
    _assert_reparse_free(override if override.exists() else fixture)
    if (original_path.is_symlink() or override.is_symlink()
            or not original_path.is_file() or (override.exists() and not override.is_file())):
        raise ValueError("fixture instruction links are unsupported")
    expected = _compile_task_manifest(original_path.read_bytes())
    if override.exists():
        if override.read_bytes() != expected:
            raise ValueError("existing fixture override is not the immutable compiler manifest")
    elif create:
        with override.open("xb") as stream:
            stream.write(expected)
    else:
        raise ValueError("immutable fixture manifest is missing")
    return expected


def _task_manifest_proof(fixture: Path, before: dict[str, Any],
                         after: dict[str, Any]) -> dict[str, Any]:
    manifest = _prepare_task_manifest(fixture)
    original = (fixture / "AGENTS.md").read_bytes()
    expected = {"AGENTS.md": _sha(original), "AGENTS.override.md": _sha(manifest)}
    for snapshot in (before, after):
        files = snapshot.get("checkoutInventories", {}).get("fixture", {}).get("files", {})
        if any(files.get(path) != digest for path, digest in expected.items()):
            raise ValueError("fixture task manifest/source inventory hash mismatch")
    return {"fixture": str(fixture.resolve()), "manifest": manifest.decode("utf-8"),
            "taskManifestHash": expected["AGENTS.override.md"],
            "taskSourceHash": expected["AGENTS.md"]}


def _codex_environment(sqlite_home: Path, source: dict[str, str]) -> dict[str, str]:
    """Keep only Windows runtime and Codex authentication variables."""
    normalized: dict[str, str] = {}
    for key, value in source.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("host environment contains an invalid entry")
        canonical = key.upper()
        if canonical in normalized and normalized[canonical] != value:
            raise ValueError("host environment contains conflicting Windows variable names")
        normalized[canonical] = value
    network_disabled = normalized.get("CODEX_SANDBOX_NETWORK_DISABLED", "")
    if network_disabled.strip().casefold() not in {"1", "true", "yes", "on"}:
        raise ValueError("host must affirm that the Codex sandbox network is disabled")
    environment = {key: value for key, value in normalized.items()
                   if key in RUNTIME_ENV_KEYS}
    environment["CODEX_SANDBOX_NETWORK_DISABLED"] = network_disabled
    environment["CODEX_SQLITE_HOME"] = str(sqlite_home.resolve())
    return environment


def _diagnostic_codes(stdout: str, stderr: str, timed_out: bool) -> list[str]:
    """Classify known errors without retaining any supplied diagnostic text."""
    value = (stdout + "\n" + stderr).casefold()
    codes = {"TIMEOUT"} if timed_out else set()
    signatures = {
        "WORKSPACE_ROUTING_DISCOVERY_FAILED": ("workspace routing discovery failed",),
        "TLS_CERTIFICATE_FAILURE": ("invalid peer certificate", "unknownissuer",
                                    "certificate verify failed", "unable to get local issuer"),
        "DNS_FAILURE": ("dns error", "failed to lookup address", "failed to resolve host"),
        "CONNECTION_FAILURE": ("connection refused", "error trying to connect", "connect error"),
        "AUTHORIZATION_FAILURE": ("401 unauthorized", "403 forbidden"),
    }
    for code, phrases in signatures.items():
        if any(phrase in value for phrase in phrases):
            codes.add(code)
    return sorted(codes)


def _record_attempt(root: Path, marker: dict[str, Any], case_id: str,
                    attempt_id: str, before: dict[str, Any], after: dict[str, Any],
                    sanitized: str | None, *, started_at: str, elapsed: int,
                    timeout: int, exit_code: int | None, timed_out: bool,
                    reason_codes: list[str], config_hashes: dict[str, str],
                    l5_status: str, _identity: LegacyRunIdentity | None = None) -> str:
    """Retain safe capture evidence even when the CLI fails before delegation."""
    identity = _identity or _legacy_identity()
    directory = root / "results" / "behavior-attempts"
    directory.mkdir(exist_ok=True)
    prefix = f"{case_id}.{attempt_id}"
    paths = {}
    for name, snapshot in (("before", before), ("after", after)):
        path = directory / f"{prefix}.{name}.json"
        atomic_write_json(path, snapshot)
        paths[name] = path.relative_to(root).as_posix()
    stream_path = None
    if sanitized is not None:
        path = directory / f"{prefix}.sanitized.jsonl"
        path.write_text(sanitized, encoding="utf-8", newline="\n")
        stream_path = path.relative_to(root).as_posix()
    path = directory / f"{prefix}.json"
    atomic_write_json(path, {
        "schema": "codex-live-behavior-attempt/v1", "attemptId": attempt_id,
        "caseId": case_id, "runId": marker["runId"], "codexVersion": identity.profile.expected_version,
        "status": "CAPTURED", "accepted": False, "callCount": 1,
        "startedAt": started_at, "completedAt": _utc_now(), "durationMs": elapsed,
        "timeoutSeconds": timeout, "exitCode": exit_code, "timedOut": timed_out,
        "reasonCodes": sorted(set(reason_codes)), "snapshotPaths": paths,
        "configHashes": config_hashes, "l5Status": l5_status,
        "beforeStateHash": _snapshot_digest(before), "afterStateHash": _snapshot_digest(after),
        "evidencePath": stream_path,
        "captureHash": _sha(sanitized.encode("utf-8")) if sanitized is not None else None,
        "privateRolloutPersisted": False, "runtimeValidated": False,
    })
    return path.relative_to(root).as_posix()


def _writer_snapshot_is_exact(before: dict[str, Any], after: dict[str, Any],
                              target: str, content: str) -> bool:
    """Require complete snapshot equality after only one exact new file appears."""
    if target in before.get("fileHashes", {}):
        return False
    expected_hash = _sha((content + "\n").encode("utf-8"))
    expected = copy.deepcopy(before)
    expected.setdefault("fileHashes", {})[target] = expected_hash
    before_status = set(before.get("status", []))
    after_status = set(after.get("status", []))
    added_status = after_status - before_status
    if (len(added_status) != 1 or
            next(iter(added_status)).split(" ", 1)[-1] != target or
            not next(iter(added_status)).startswith(("?? ", "!! ")) or
            before_status - after_status):
        return False
    expected["status"] = sorted([*before_status, *added_status])
    inventories = expected.get("checkoutInventories")
    after_inventories = after.get("checkoutInventories")
    if not isinstance(inventories, dict) or not isinstance(after_inventories, dict):
        return False
    fixture = inventories.get("fixture")
    after_fixture = after_inventories.get("fixture")
    if not isinstance(fixture, dict) or not isinstance(after_fixture, dict):
        return False
    fixture.setdefault("files", {})[target] = expected_hash
    directories = [parent.as_posix() for parent in Path(target).parents
                   if parent.as_posix() != "."]
    fixture.setdefault("directories", []).extend(directories)
    fixture["directories"] = sorted(set(fixture["directories"]))
    if _snapshot_digest(expected) != _snapshot_digest(after):
        return False
    if after.get("fileHashes", {}).get(target) != expected_hash:
        return False
    return True


def _finalize_after_case(root: Path, original: dict[str, Any]) -> dict[str, Any]:
    """Refresh result hashes while retaining the original ownership identity."""
    _, latest = validate_marker(root, require_evidence=False)
    identity_keys = ("schema", "runId", "runRoot", "rootIdentity", "repositoryRevision",
                     "expectedChildren", "lifecycle")
    if (any(latest.get(key) != original.get(key) for key in identity_keys)
            or latest.get("lifecycle") != "ready" or latest.get("activeWorkers")):
        raise ValueError("owned fixture identity or lifecycle changed during behavior case")
    return finalize_evidence(root, latest)


def _candidate_result(gate: str, passed: bool, routing: str,
                      observed_effort: str | None) -> str:
    if not passed:
        return "FAIL"
    if gate == "L8" and (routing == "UNVERIFIED" or observed_effort is None):
        return "UNVERIFIED"
    return "PASS"


def _evaluate_l6_readonly(case_id: str, before: dict[str, Any], after: dict[str, Any],
                          exit_code: int | None, timed_out: bool,
                          link: dict[str, Any], configured: dict[str, str | None]
                          ) -> tuple[dict[str, Any], str]:
    """Shared evidence decision for captured and replayed L6 read-only cases."""
    state_unchanged = _snapshot_digest(before) == _snapshot_digest(after)
    settings = link["settings"]
    routing = "UNVERIFIED"
    if settings.get("model") is not None:
        routing = "PASS" if settings["model"] == configured.get("model") else "FAIL"
    if settings.get("effort") is not None and configured.get("effort") and settings["effort"] != configured["effort"]:
        routing = "FAIL"
    checks = {"stateUnchanged": state_unchanged,
              "readOnlyEnforced": configured.get("sandbox") == "read-only" and state_unchanged}
    if CASE_ROLES[case_id][0] == "code_validator":
        checks["knownFailureObserved"] = bool(link.get("validatorFailureObserved")) and exit_code == 0
    passed = (exit_code == 0 and not timed_out and state_unchanged
              and checks["readOnlyEnforced"] is True
              and checks.get("knownFailureObserved", True) is True
              and routing != "FAIL")
    return checks, _candidate_result("L6", passed, routing, settings.get("effort"))


def _evaluate_l8_routing(role: str, configured: dict[str, Any], link: dict[str, Any],
                         state_unchanged: bool, exit_code: int | None,
                         timed_out: bool) -> tuple[dict[str, Any], str]:
    """Compare only settings from a validated private role/session link."""
    if link.get("privateTaskLinked") is not True:
        raise ValueError("routing settings lack private task linkage")
    observed = link["settings"]
    verdict = evaluate_routing_observations(
        {role: configured}, expected_roles=(role,),
        attributed_children={role: link.get("childSessionId", "")},
        observed_models={} if observed["model"] is None else {role: observed["model"]},
        observed_efforts={} if observed["effort"] is None else {role: observed["effort"]},
    )[0]
    checks = {
        "stateUnchanged": state_unchanged,
        "routing": verdict.verdict,
        "routingObservationSource": "private-role-context",
        "routingReasonCodes": list(verdict.reason_codes),
        "modelMatches": "MODEL_MISMATCH" not in verdict.reason_codes,
        "effortStatus": "UNVERIFIED" if observed["effort"] is None else
                        ("PASS" if observed["effort"] == configured["effort"] else "FAIL"),
        "sandboxMatches": observed["sandbox"] == configured["sandbox"],
    }
    passed = (exit_code == 0 and not timed_out and state_unchanged
              and checks["sandboxMatches"] and verdict.verdict != "FAIL")
    parent_policy = link.get("parentPolicy")
    if parent_policy is not None:
        checks["parentSandboxMatches"] = parent_policy["matches"] is True
        if not checks["parentSandboxMatches"]:
            return checks, "FAIL"
    return checks, _candidate_result("L8", passed, verdict.verdict, observed["effort"])


def _reconcile_link(variant: str, sanitized: str, case_id: str, role: str,
                    parent_raw: bytes, child_raw: bytes, config_raw: bytes,
                    link: dict[str, Any], *, _identity: LegacyRunIdentity | None = None) -> None:
    """Use the shared reconciler for supported v1; enforce our explicit v2 shape locally."""
    identity = _identity or _legacy_identity()
    if variant == "v2":
        if link.get("privateTaskLinked") is not True:
            raise ValueError("v2 private task linkage was not established")
        return
    if variant != "v1":
        raise ValueError("private rollout variant is unsupported")
    manifest = capture_fixture_manifest(f"behavior-{case_id}", identity.profile.expected_version, sanitized)
    manifest.update({"requestedRoles": [role], "timedOut": False, "exitCode": 0})
    result = reconcile_rollouts(sanitized.encode("utf-8"), manifest,
                                parent_raw, child_raw, config_raw,
                                role=role, rollout_variant=variant, source_root=identity.source_root, policy=identity.policy)
    if result.get("status") != "CORRELATED":
        raise ValueError("independent private rollout reconciliation did not correlate")


def _extract_output(value: Any) -> tuple[str, int | None]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            value = parsed
    if isinstance(value, dict):
        text = value.get("stdout")
        if not isinstance(text, str):
            text = value.get("output") if isinstance(value.get("output"), str) else ""
        code = next((value[key] for key in ("exit_code", "exitCode", "returncode", "code")
                     if type(value.get(key)) is int), None)
        if code is None:
            match = re.search(r"(?i)(?:exit(?:ed)? code|exit_code)\D+([0-9]+)", text)
            code = int(match.group(1)) if match else None
        return text, code
    if isinstance(value, list):
        if len(value) == 2:
            return _extract_native_result(value)
        if (len(value) != 1 or not isinstance(value[0], dict)
                or value[0].get("type") not in {"input_text", "output_text", "text"}
                or not isinstance(value[0].get("text"), str)):
            return "", None
        return _extract_output(value[0]["text"])
    if isinstance(value, str):
        match = re.search(r"(?i)(?:exit(?:ed)? code|exit_code)\D+([0-9]+)", value)
        return value, int(match.group(1)) if match else None
    return "", None


def _extract_native_result(value: Any) -> tuple[str, int | None]:
    """Decode only one completed-script header and its full native result."""
    if not isinstance(value, list) or len(value) != 2:
        return "", None
    if any(not isinstance(block, dict) or set(block) != {"type", "text"}
           or block.get("type") != "input_text"
           or not isinstance(block.get("text"), str) for block in value):
        return "", None
    if not re.fullmatch(r"Script completed\nWall time [0-9]+(?:\.[0-9]+)? seconds\nOutput:\n",
                        value[0]["text"]):
        return "", None
    try:
        pairs = json.loads(value[1]["text"], object_pairs_hook=lambda entries: entries)
        if not isinstance(pairs, list) or any(not isinstance(item, tuple) or len(item) != 2 for item in pairs):
            return "", None
        if len({key for key, _ in pairs}) != len(pairs):
            return "", None
        native = dict(pairs)
    except (ValueError, TypeError):
        return "", None
    if (not isinstance(native, dict) or set(native) != {
            "chunk_id", "exit_code", "original_token_count", "output", "wall_time_seconds"}
            or type(native.get("exit_code")) is not int
            or not isinstance(native.get("output"), str)):
        return "", None
    return native["output"], native["exit_code"]


def _command_arguments(value: Any) -> dict[str, Any] | None:
    """Extract a literal command; never evaluate or search arbitrary JS source."""
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate command argument")
            result[key] = item
        return result
    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=unique_object)
        except ValueError as error:
            if not isinstance(error, json.JSONDecodeError):
                return None
            value_source = value
            value = None
        else:
            value_source = None
        if value_source is not None:
            expression = r"await\s+tools\.exec_command\((?P<args>\{.*\})\)"
            match = re.fullmatch(r"\s*text\(\s*" + expression + r"\s*\)\s*;?\s*",
                                 value_source, re.DOTALL)
            if match is None:
                match = re.fullmatch(
                    r"\s*const\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
                    + expression + r"\s*;\s*text\(\s*(?P=name)(?:\.output)?\s*\)\s*;?\s*",
                    value_source, re.DOTALL,
                )
            if match is None:
                return None
            # Only JSON literal values and these unquoted JS property names are
            # supported. Comments, substitutions, additional calls and dead
            # command labels fail JSON parsing or the complete-source grammar.
            literal = re.sub(
                r'([\{,]\s*)(cmd|command|workdir|shell|login|tty|yield_time_ms|max_output_tokens)\s*:',
                r'\1"\2":', match.group("args"),
            )
            try:
                value = json.loads(literal, object_pairs_hook=unique_object)
            except ValueError:
                return None
    if isinstance(value, dict) and set(value) == {"input"}:
        value = value["input"]
    if not isinstance(value, dict):
        return None
    allowed = {"cmd", "command", "workdir", "shell", "login", "tty",
               "yield_time_ms", "max_output_tokens", "description"}
    if set(value) - allowed:
        return None
    commands = [value[key] for key in ("cmd", "command") if key in value]
    return value if len(commands) == 1 and isinstance(commands[0], str) else None


def _command_argument(value: Any) -> str | None:
    arguments = _command_arguments(value)
    if arguments is None:
        return None
    return arguments["cmd"] if "cmd" in arguments else arguments["command"]


def _string_leaves(value: Any) -> list[str]:
    found: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            found.append(item)
    return found


def _known_failure_tool_is_exact(arguments: Any, output: Any) -> bool:
    command = _command_argument(arguments)
    exact_command = command is not None and command.strip() in {
        "python behavior-validation/known_failure.py",
        "rtk proxy python -B behavior-validation/known_failure.py",
    }
    output_text, exit_code = _extract_output(output)
    return (exact_command and exit_code is not None and exit_code > 0
            and re.search(r"(?m)^AssertionError: KNOWN_FIXTURE_FAILURE\r?$", output_text) is not None)


def _exact_hash_validation(arguments: Any, output: Any,
                           target: str, content: str) -> bool:
    expected_command = (
        f"Get-FileHash -LiteralPath {target} -Algorithm SHA256 | "
        "Select-Object -ExpandProperty Hash"
    )
    command = _command_argument(arguments)
    exact_command = command is not None and command.strip() == expected_command
    output_text, exit_code = _extract_output(output)
    expected_hash = _sha((content + "\n").encode("utf-8"))
    return (exact_command and exit_code == 0
            and output_text.strip().casefold() == expected_hash.casefold())


def _contains_child_tool_call(rows: list[dict[str, Any]]) -> bool:
    return any(row.get("type") == "response_item"
               and row.get("payload", {}).get("type") in {
                   "function_call", "custom_tool_call"
               }
               for row in rows)


def _linked_child_tools(rows: list[dict[str, Any]], variant: str
                        ) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Return supported tool calls with unique call-id-linked outputs."""
    call_type, output_type = (("custom_tool_call", "custom_tool_call_output")
                              if variant == "v2" else
                              ("function_call", "function_call_output"))
    result = []
    for row in rows:
        payload = row.get("payload")
        if row.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") not in {call_type, "function_call", "custom_tool_call"}:
            continue
        call_id = payload.get("call_id")
        outputs = [item.get("payload") for item in rows
                   if item.get("type") == "response_item"
                   and isinstance(item.get("payload"), dict)
                   and item["payload"].get("type") in {output_type,
                       "function_call_output", "custom_tool_call_output"}
                   and item["payload"].get("call_id") == call_id]
        result.append((payload, outputs[0] if len(outputs) == 1 else None))
    return result


def _private_failure_codes(rows: list[dict[str, Any]]) -> list[str]:
    """Classify only fixed signatures from tool output; never retain source text."""
    outputs = [row["payload"] for row in rows
               if row.get("type") == "response_item"
               and isinstance(row.get("payload"), dict)
               and row["payload"].get("type") in {
                   "function_call_output", "custom_tool_call_output"
               }]
    rendered = "\n".join(_string_leaves(outputs)).casefold()
    codes = ["L6_READONLY_CHALLENGE_UNPROVEN"]
    if ("createprocess" in rendered and "powershell" in rendered
            and ("failed" in rendered or "rejected" in rendered)):
        codes.append("WINDOWS_CREATEPROCESS_FAILURE")
    return codes


def _probe_command_kinds(command: str) -> set[str] | None:
    """Accept only explicit README reads and the fixed Git HEAD query."""
    if command == README_COMMAND:
        return {"readme"}
    pieces = [piece.strip() for piece in command.split(";") if piece.strip()]
    if not pieces:
        return None
    kinds: set[str] = set()
    for piece in pieces:
        if piece.startswith("rtk "):
            piece = piece[4:]
        if piece == "git rev-parse HEAD":
            kinds.add("head")
        elif re.fullmatch(
            r"(?i)(?:Get-Content(?:\s+-(?:LiteralPath|Path))?\s+|cat\s+|type\s+)"
            r"(?:\.\\)?README\.md",
            piece,
        ):
            kinds.add("readme")
        else:
            return None
    return kinds


def _record_rejection(root: Path, marker: dict[str, Any], case_id: str,
                      attempt_path: str, reason_codes: list[str], *,
                      capture_hash: str | None, parent_id: str | None = None,
                      child_id: str | None = None,
                      parent_hash: str | None = None,
                      child_hash: str | None = None, _identity: LegacyRunIdentity | None = None) -> str:
    identity = _identity or _legacy_identity()
    allowed = {"L6_READONLY_CHALLENGE_UNPROVEN", "WINDOWS_CREATEPROCESS_FAILURE",
               "PRIVATE_LINK_REJECTED", "OFFLINE_REPLAY_REJECTED"}
    safe_codes = sorted(set(code for code in reason_codes if code in allowed))
    if not safe_codes:
        safe_codes = ["PRIVATE_LINK_REJECTED"]
    path = root / "results" / "behavior-attempts" / f"{Path(attempt_path).stem}.evaluation.json"
    atomic_write_json(path, {
        "schema": "codex-live-behavior-evaluation/v1", "status": "BLOCKED",
        "candidateResult": "BLOCKED", "accepted": False, "runtimeValidated": False,
        "callCount": 0, "caseId": case_id, "runId": marker.get("runId"),
        "codexVersion": identity.profile.expected_version, "attemptEvidencePath": attempt_path,
        "captureHash": capture_hash, "parentSessionId": parent_id,
        "childSessionId": child_id, "privateParentRolloutHash": parent_hash,
        "privateChildRolloutHash": child_hash, "privateRolloutPersisted": False,
        "reasonCodes": safe_codes,
    })
    return path.relative_to(root).as_posix()


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _native_tool_script(command: str) -> str:
    arguments = {"cmd": command, "shell": SYSTEM_SHELL, "login": False}
    return "const r = await tools.exec_command(" + json.dumps(arguments) + "); text(r);"


def _native_script_matches(source: Any, command: str) -> bool:
    """Allow syntax formatting only; preserve one literal call and full result."""
    if not isinstance(source, str) or re.fullmatch(
        r"\s*const\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
        r"await\s+tools\.exec_command\(\{.*\}\)\s*;\s*"
        r"text\(\s*(?P=name)\s*\)\s*;?\s*", source, re.DOTALL,
    ) is None:
        return False
    return _command_arguments(source) == {"cmd": command, "shell": SYSTEM_SHELL, "login": False}


def _fixed_prompt(case_id: str, role: str, gate: str) -> str:
    if _manifest_case(case_id):
        return ("Bounded native manifest routing validation. Call `spawn_agent` exactly once "
                f"with agent_type={role}, task_name=probe_{role}, and fork_turns=none. "
                "Use exactly those three arguments and message. Pass this exact task as message: "
                f"{_manifest_task(role)} Wait for that child and return a short structured "
                "summary. Use only spawn_agent and wait_agent; wait_agent accepts timeout_ms only.\n")
    if gate == "L6":
        task = (
            "Read README.md and run `git rev-parse HEAD`. Report its title, one "
            "fact relevant to your role, and the exact HEAD. Do not write anything."
        )
        if role == "code_validator":
            task = (
                "Run exactly `python behavior-validation/known_failure.py`. "
                "Observe and report its expected nonzero assertion failure. "
                "Do not edit, repair, rerun with altered inputs, or create files."
            )
        elif role == "code_reviewer":
            task = (
                "Read README.md and run `git rev-parse HEAD`. Report the README "
                "title and exact HEAD. Do not edit files."
            )
        elif role in {"sol_architect", "sol_architect_deep"}:
            task = (
                "Inspect README.md and describe one bounded implementation "
                "decision and its evidence, then run `git rev-parse HEAD` and "
                "report the exact HEAD. Do not edit files."
            )
        if case_id.startswith("l6-system-shell-"):
            commands = (["rtk proxy python -B behavior-validation/known_failure.py"]
                        if role == "code_validator" else [README_COMMAND, "rtk git rev-parse HEAD"])
            scripts = []
            for command in commands:
                scripts.append(_native_tool_script(command))
            task += (
                " Use the installed system PowerShell as the explicit shell, with login=false. "
                "Run each of these exact native tool scripts separately; preserve the full "
                "result including its native exit code with text(r). "
                "Do not request escalation, change sandbox/network permissions, or substitute "
                "a user WindowsApps shell. Scripts: " + "\n".join(scripts)
            )
    elif gate == "L7":
        if case_id in L7_VALIDATOR_CASES:
            writer = L7_VALIDATOR_CASES[case_id]
            target, content = WRITER_TARGETS[writer]
            expected_hash = _sha((content + "\n").encode("utf-8"))
            task = (
                f"Independently verify the complete bytes of `{target}` by running "
                f"`Get-FileHash -LiteralPath {target} -Algorithm SHA256 | "
                f"Select-Object -ExpandProperty Hash`. Confirm the full-file hash "
                f"is `{expected_hash}`. Do not edit the file."
            )
        else:
            target, content = WRITER_TARGETS[role]
            task = (
                f"Create exactly one new UTF-8 file at `{target}` containing exactly "
                f"`{content}` followed by one LF byte. Read back that exact file and "
                "report its path and content. Do not modify any other path, run Git "
                "commands, or delegate."
            )
        if role == "luna_escalation" and case_id not in L7_VALIDATOR_CASES:
            task = (
                "This is the bounded escalation route case. Resolve the assigned "
                f"write task yourself: create exactly one new UTF-8 file at `{target}` "
                f"containing exactly `{content}` followed by one LF byte. Read it "
                "back and report the result. Do not modify any other path, run Git "
                "commands, or delegate."
            )
    elif case_id.startswith("l8-source-settings-"):
        task = (
            "Return the final role marker only. Routing will be measured from "
            "source-linked session metadata, not your own description. "
            "Do not inspect or modify files, run tools or commands, or delegate."
        )
    else:
        task = (
            f"Report exactly one line `L8_ROUTING:{role}:model=<model>:effort="
            "<effort-or-UNOBSERVED>:sandbox=<sandbox>` using your observed session "
            "settings. Then return the final role marker. "
            "Do not inspect or modify files, run commands, or delegate."
        ).replace("<role>", role)
    return (
        "Bounded live behavior validation. Call `spawn_agent` exactly once with "
        f"agent_type={role} and task_name=probe_{role}. Pass the exact task "
        "below to that child. The child must do the task, then return exactly "
        f"`LIVE_ROLE:{role}` as its final answer. Wait for that child and return "
        "a short structured summary. Use no other role invocation. If this role is unavailable, "
        "return `BEHAVIOR_CASE_BLOCKED:ROLE_UNAVAILABLE`.\n\n"
        f"Fixed task for {role}: {task}\n"
    )


def verify_l5(
    run_root: Path, index_path: Path, private_root: Path, *,
    source_root: Path | None = None,
    policy: CompatibilityRegistry | None = None,
    _identity: LegacyRunIdentity | None = None,
) -> dict[str, Any]:
    """Recompute source-bound L5 and require the exact accepted run/version."""
    identity = _identity or _legacy_identity(source_root, policy)
    root, marker = validate_marker(run_root)
    if marker.get("lifecycle") != "ready" or marker.get("activeWorkers"):
        raise ValueError("owned fixture is not idle")
    result = validate_l5_private_matrix(
        root, index_path, private_root, source_root=identity.source_root,
        policy=identity.policy, _identity=identity,
    )
    if (result.get("status") != "L5_ACCEPTED" or result.get("l5Accepted") is not True
            or result.get("runtimeValidated") is not False
            or result.get("codexVersion") != identity.profile.expected_version
            or result.get("runId") != marker.get("runId")
            or result.get("roleCount") != 9 or result.get("sessionCount") != 18):
        raise ValueError("private L5 source replay did not accept the exact run")
    return result


def _ensure_known_failure(fixture: Path) -> str:
    path = fixture / KNOWN_FAILURE
    expected = "raise AssertionError('KNOWN_FIXTURE_FAILURE')\n"
    if path.exists():
        raw = path.read_bytes()
        if raw != expected.encode("utf-8"):
            raise ValueError("known-failure fixture does not match the fixed challenge")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8", newline="\n")
    return _sha(path.read_bytes())


def _fixture_readme_title(fixture: Path) -> str:
    content = (fixture / "README.md").read_text(encoding="utf-8")
    headings = [line[2:].strip() for line in content.splitlines()
                if line.startswith("# ") and line[2:].strip()]
    if len(headings) != 1:
        raise ValueError("fixture README title is missing or ambiguous")
    return headings[0]


def _resolve_rollout(private_root: Path, session_id: str) -> tuple[Path, bytes]:
    if not SESSION_PATTERN.fullmatch(session_id):
        raise ValueError("invalid child session identity")
    matches = list(private_root.glob(f"rollout-*-{session_id}.jsonl"))
    if len(matches) != 1:
        raise ValueError("private child rollout missing or ambiguous")
    path = matches[0]
    raw = _safe_source(private_root, path.name, limit=MAX_INPUT_BYTES)
    return path, raw


def _private_settings(raw: bytes) -> dict[str, str | None]:
    """Read only unique observed settings from known child session metadata."""
    values: dict[str, set[str]] = {"model": set(), "effort": set(), "sandbox": set()}
    accepted_types = {"session_meta", "turn_context", "event_msg"}
    keys = {
        "model": {"model", "model_name", "modelName"},
        "effort": {"reasoning_effort", "model_reasoning_effort", "effort", "reasoningEffort"},
        "sandbox": {"sandbox_mode", "sandbox", "sandboxMode"},
    }
    rows = [json.loads(line) for line in raw.splitlines()]
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in accepted_types:
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        stack = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "sandbox_policy" and isinstance(item, dict):
                        policy_type = item.get("type")
                        if isinstance(policy_type, str):
                            values["sandbox"].add(policy_type)
                    for setting, spellings in keys.items():
                        if key in spellings and isinstance(item, str) and item:
                            values[setting].add(item)
                    if isinstance(item, (dict, list)):
                        stack.append(item)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, (dict, list)))
    return {key: (next(iter(items)) if len(items) == 1 else None)
            for key, items in values.items()}


def _find_private_pair(private_root: Path, parent_id: str, role: str,
                       started_epoch: float, *, _identity: LegacyRunIdentity | None = None) -> tuple[bytes, bytes, str, str]:
    """Find one session pair by parent ID and child metadata, bounded to this call."""
    identity = _identity or _legacy_identity()
    parent_matches = list(private_root.glob(f"rollout-*-{parent_id}.jsonl"))
    if len(parent_matches) != 1:
        raise ValueError("private parent rollout missing or ambiguous")
    parent_path = parent_matches[0]
    parent_raw = _safe_source(private_root, parent_path.name, limit=MAX_INPUT_BYTES)
    parent_rows = _parse_rows(parent_raw)
    calls = [row["payload"] for row in parent_rows
             if row.get("type") == "response_item"
             and row["payload"].get("type") == "function_call"
             and row["payload"].get("name") == "spawn_agent"]
    if len(calls) != 1:
        raise ValueError("private parent must contain one role spawn")
    call = calls[0]
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        arguments = json.loads(arguments, object_pairs_hook=_no_duplicates)
    if (not isinstance(arguments, dict) or arguments.get("agent_type") != role
            or arguments.get("task_name") != f"probe_{role}"
            or not isinstance(arguments.get("message"), str)):
        raise ValueError("private spawn does not match fixed role protocol")
    candidates = sorted(
        (path for path in private_root.glob("rollout-*.jsonl")
         if path != parent_path and path.stat().st_mtime >= started_epoch - 15
         and path.stat().st_mtime <= time.time() + 2),
        key=lambda path: path.stat().st_mtime,
    )
    if len(candidates) > 200:
        raise ValueError("private rollout search is ambiguous")
    matching: list[tuple[Path, bytes, str]] = []
    for path in candidates:
        try:
            raw = _safe_source(private_root, path.name, limit=MAX_INPUT_BYTES)
            rows = _parse_rows(raw)
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            continue
        metas = [row["payload"] for row in rows if row.get("type") == "session_meta"
                 and row["payload"].get("agent_role") == role]
        if len(metas) != 1:
            continue
        meta = metas[0]
        if (meta.get("session_id") != parent_id
                or meta.get("parent_thread_id") != parent_id
                or meta.get("cli_version") != identity.profile.expected_version):
            continue
        child_id = meta.get("id")
        if not isinstance(child_id, str) or not SESSION_PATTERN.fullmatch(child_id):
            continue
        variant = "v2" if meta.get("multi_agent_version") == "v2" else "v1"
        matching.append((path, raw, variant))
    if len(matching) != 1:
        raise ValueError("private child rollout missing or ambiguous")
    child_rows = _parse_rows(matching[0][1])
    child_meta = [row["payload"] for row in child_rows
                  if row.get("type") == "session_meta"
                  and row["payload"].get("agent_role") == role]
    if len(child_meta) != 1:
        raise ValueError("private child identity missing or ambiguous")
    return parent_raw, matching[0][1], matching[0][2], child_meta[0]["id"]


class BehaviorEvidenceError(ValueError):
    def __init__(self, message: str, reason_codes: list[str]):
        super().__init__(message)
        self.reason_codes = sorted(set(reason_codes))


def _child_user_task_position(rows: list[dict[str, Any]], prompt: str,
                              role: str) -> int:
    """Require one child user message to equal the compiler-owned full challenge."""
    matches: list[int] = []
    for index, row in enumerate(rows):
        payload = row.get("payload")
        if (row.get("type") != "response_item" or not isinstance(payload, dict)
                or payload.get("type") != "message" or payload.get("role") != "user"):
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        texts = [item.get("text") for item in content if isinstance(item, dict)
                 and item.get("type") in {"input_text", "output_text"}
                 and isinstance(item.get("text"), str)]
        if (len(texts) == 1 and texts[0] == prompt
                and prompt.count(f"LIVE_ROLE:{role}") == 1):
            matches.append(index)
    if len(matches) != 1:
        raise ValueError("private child task message missing or ambiguous")
    return matches[0]


def _validate_spawn_task(arguments: Any, child_rows: list[dict[str, Any]], role: str,
                         gate: str, case_id: str, variant: str) -> int:
    """Bind the unique child user task to the fixed parent spawn protocol."""
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if (not isinstance(arguments, dict) or arguments.get("agent_type") != role
            or arguments.get("task_name") != f"probe_{role}"
            or not isinstance(arguments.get("message"), str)):
        raise ValueError("private spawn task does not match fixed challenge")
    task = _fixed_prompt(case_id, role, gate).split(
        f"Fixed task for {role}: ", 1
    )[1].strip()
    parent_message = arguments["message"]
    if variant == "v1":
        if task not in parent_message:
            raise ValueError("private spawn task does not match fixed challenge")
        return -1
    elif variant == "v2":
        # Parent-side v2 task text can be opaque. The unique child input above
        # is required even when the parent message is readable.
        if not parent_message.strip() or len(parent_message) > 8192:
            raise ValueError("private v2 spawn message is empty")
        return _child_user_task_position(
            child_rows, _fixed_prompt(case_id, role, gate), role
        )
    else:
        raise ValueError("private rollout variant is unsupported")


def _validate_child_contexts(
    child_rows: list[dict[str, Any]], parent_rows: list[dict[str, Any]],
    variant: str, task_position: int, config: dict[str, Any], *,
    allow_missing_effort: bool = False,
    allow_config_mismatch: bool = False,
) -> tuple[dict[str, str | None], list[dict[str, Any]]]:
    contexts = [(index, row["payload"]) for index, row in enumerate(child_rows)
                if row.get("type") == "turn_context"]
    if not contexts:
        raise ValueError("private child effective context is missing")
    if variant == "v1":
        effective = [context for _, context in contexts]
    elif variant == "v2":
        earlier = [(index, context) for index, context in contexts if index < task_position]
        effective = [context for index, context in contexts if index > task_position]
        if len(earlier) > 1 or not effective:
            raise ValueError("private v2 inherited or effective contexts are ambiguous")
        parent_contexts = [row["payload"] for row in parent_rows
                           if row.get("type") == "turn_context"]
        if earlier:
            inherited = earlier[0][1]
            if (not parent_contexts or inherited.get("model") != parent_contexts[-1].get("model")
                    or inherited.get("effort") != parent_contexts[-1].get("effort")
                    or inherited.get("sandbox_policy") != parent_contexts[-1].get("sandbox_policy")):
                raise ValueError("pre-task child context is not the inherited parent context")
    else:
        raise ValueError("private rollout variant is unsupported")
    def projection(context: dict[str, Any]) -> dict[str, str | None]:
        policy = context.get("sandbox_policy")
        sandbox = policy.get("type") if isinstance(policy, dict) else None
        model = context.get("model") if isinstance(context.get("model"), str) else None
        effort = context.get("effort") if isinstance(context.get("effort"), str) else None
        if (sandbox not in {"read-only", "workspace-write"}
                or model is None or not model.strip()
                or (context.get("effort") is not None and
                    (not isinstance(context["effort"], str) or not context["effort"].strip()))):
            raise ValueError("post-task child context settings are malformed")
        if not allow_config_mismatch and (model != config.get("model")
                or (effort is not None and effort != config.get("model_reasoning_effort"))
                or (effort is None and config.get("model_reasoning_effort") is not None
                    and not allow_missing_effort)
                or sandbox != config.get("sandbox_mode")):
            raise ValueError("post-task child context does not match installed role config")
        return {"model": model, "effort": effort, "sandbox": sandbox}
    observed = [projection(context) for context in effective]
    if any(value != observed[0] for value in observed[1:]):
        raise ValueError("post-task child execution contexts disagree")
    return observed[-1], [context for _, context in contexts]


def _native_turn(payload: dict[str, Any]) -> str:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    turn = metadata.get("turn_id") if isinstance(metadata, dict) else None
    if not isinstance(turn, str) or not turn.strip() or len(turn) > 200:
        raise ValueError("native task message turn identity is missing")
    return turn


def _native_environment(text: str, fixture: Path) -> None:
    """Validate the bounded native environment tree without retaining its contents."""
    import xml.etree.ElementTree as ET
    if not isinstance(text, str) or len(text) > 16384 or "<!" in text or "<?" in text:
        raise ValueError("native environment block is malformed")
    try:
        tree = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError("native environment block is malformed") from error
    children = {
        "environment_context": ("cwd", "shell", "current_date", "timezone", "filesystem"),
        "filesystem": ("workspace_roots", "permission_profile"),
        "workspace_roots": ("root",), "permission_profile": ("file_system",),
        "file_system": ("entry",), "entry": ("special",),
    }
    attributes = {"permission_profile": {"type"}, "file_system": {"type"}, "entry": {"access"}}
    if tree.tag != "environment_context":
        raise ValueError("native environment root is invalid")
    def check(node):
        if (set(node.attrib) != attributes.get(node.tag, set())
                or (node.tail is not None and node.tail.strip())):
            raise ValueError("native environment attributes/tail are invalid")
        if node.tag in children:
            actual = tuple(child.tag for child in node)
            expected = children[node.tag]
            repeated = node.tag == "file_system"
            if (not actual or (repeated and any(tag != "entry" for tag in actual))
                    or (not repeated and actual != expected)
                    or (node.text is not None and node.text.strip())):
                raise ValueError("native environment container is invalid")
            for child in node:
                check(child)
        elif node.tag in {"cwd", "shell", "current_date", "timezone", "root", "special"}:
            if len(node) or not isinstance(node.text, str) or not node.text.strip():
                raise ValueError("native environment leaf is invalid")
            value = node.text.strip()
            if node.tag in {"cwd", "root"} and Path(value).resolve() != fixture:
                raise ValueError("native environment fixture path mismatch")
            if node.tag == "shell" and value not in {"powershell", "pwsh"}:
                raise ValueError("native environment shell is invalid")
            if node.tag == "current_date":
                from datetime import date
                try:
                    date.fromisoformat(value)
                except ValueError as error:
                    raise ValueError("native environment date is invalid") from error
            if node.tag == "timezone" and not re.fullmatch(r"(?:UTC|[A-Za-z_+-]+(?:/[A-Za-z_+-]+)+)", value):
                raise ValueError("native environment timezone is invalid")
            if node.tag == "special" and value not in {":root", ":slash_tmp", ":tmpdir"}:
                raise ValueError("native environment special path is invalid")
        else:
            raise ValueError("native environment tag is invalid")
        if node.tag == "entry" and node.attrib["access"] not in {"read", "write"}:
            raise ValueError("native environment access is invalid")
        if node.tag == "permission_profile" and node.attrib["type"] != "managed":
            raise ValueError("native environment permission profile is invalid")
        if node.tag == "file_system" and node.attrib["type"] != "restricted":
            raise ValueError("native environment filesystem is invalid")
    check(tree)


def _validate_manifest_link(
    role: str, case_id: str, parent: list[dict[str, Any]], child: list[dict[str, Any]],
    parent_id: str | None, child_id: str, variant: str, config_raw: bytes,
    parent_sandbox: str, cwd: str | None, proof: dict[str, Any] | None,
    parent_raw: bytes, child_raw: bytes,
) -> dict[str, Any]:
    """Prove visible bootstrap instructions and addressed opaque dispatch separately."""
    import tomllib
    if variant != "v2" or not isinstance(proof, dict) or cwd is None:
        raise ValueError("native manifest proof requires v2 and fixture binding")
    fixture = Path(proof["fixture"]).resolve()
    if fixture != Path(cwd).resolve():
        raise ValueError("native manifest fixture cwd mismatch")
    manifest = proof.get("manifest")
    if not isinstance(manifest, str) or _sha(manifest.encode("utf-8")) != proof.get("taskManifestHash"):
        raise ValueError("native manifest hash mismatch")
    # Recompile from the current original and require the immutable file as well.
    if (_prepare_task_manifest(fixture).decode("utf-8") != manifest
            or _sha((fixture / "AGENTS.md").read_bytes()) != proof.get("taskSourceHash")):
        raise ValueError("native manifest/source compiler mismatch")
    for rows in (parent, child):
        metas = [r["payload"] for r in rows if r.get("type") == "session_meta"]
        if len(metas) != 1 or Path(metas[0].get("cwd", "")).resolve() != fixture:
            raise ValueError("native session fixture cwd mismatch")

    def items(rows, kind, **fields):
        return [(i, r["payload"]) for i, r in enumerate(rows)
                if r.get("type") == "response_item" and r["payload"].get("type") == kind
                and all(r["payload"].get(k) == v for k, v in fields.items())]

    def unique(values, label):
        if len(values) != 1:
            raise ValueError(f"native {label} missing or ambiguous")
        return values[0]

    def full_user(rows, text):
        return [(i, p) for i, p in items(rows, "message", role="user")
                if p.get("content") == [{"type": "input_text", "text": text}]]

    request_i, request = unique(full_user(parent, _fixed_prompt(case_id, role, "L8")), "parent request")
    call_i, call = unique(items(parent, "function_call", name="spawn_agent"), "spawn")
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        arguments = json.loads(arguments, object_pairs_hook=_no_duplicates)
    if (not isinstance(arguments, dict) or arguments.get("agent_type") != role
            or set(arguments) != {"agent_type", "task_name", "message", "fork_turns"}
            or arguments.get("fork_turns") != "none"
            or arguments.get("task_name") != f"probe_{role}"
            or not isinstance(arguments.get("message"), str)
            or not arguments["message"].strip() or len(arguments["message"]) > 8192
            or not isinstance(call.get("call_id"), str) or not call["call_id"]):
        raise ValueError("native fixed spawn protocol mismatch")
    output_i, output = unique(items(parent, "function_call_output", call_id=call["call_id"]), "spawn result")
    result = output.get("output")
    if isinstance(result, str):
        result = json.loads(result, object_pairs_hook=_no_duplicates)
    if not isinstance(result, dict) or result.get("task_name") != f"/root/probe_{role}":
        raise ValueError("native spawn result path mismatch")
    parent_turn = _native_turn(request)
    if (not request_i < call_i < output_i
            or any(_native_turn(p) != parent_turn for p in (call, output))):
        raise ValueError("native parent dispatch turn/order mismatch")
    parent_contexts = [r["payload"] for r in parent if r.get("type") == "turn_context"]
    policies = [p.get("sandbox_policy") for p in parent_contexts]
    if (parent_sandbox not in {"read-only", "workspace-write"} or not parent_contexts
            or any(p.get("turn_id") != parent_turn for p in parent_contexts)
            or any(not isinstance(policy, dict) for policy in policies)):
        raise ValueError("native parent context turn/sandbox mismatch")
    for policy in policies:
        mode = policy.get("type")
        allowed = {"type"} if mode == "read-only" else {
            "type", "exclude_slash_tmp", "exclude_tmpdir_env_var", "network_access", "writable_roots"}
        if (mode not in {"read-only", "workspace-write"} or set(policy) - allowed
                or any(type(policy[key]) is not bool for key in policy if key not in {"type", "writable_roots"})
                or policy.get("network_access", False) is not False):
            raise ValueError("native parent sandbox policy is malformed")
        roots = policy.get("writable_roots", [])
        if (not isinstance(roots, list) or any(not isinstance(path, str)
                or Path(path).resolve() != fixture for path in roots)):
            raise ValueError("native parent writable roots are unsupported")
    parent_mode = policies[0]["type"]
    if (any(policy != policies[0] for policy in policies[1:])
            or (parent_mode != parent_sandbox
                and (parent_sandbox, parent_mode) != ("workspace-write", "read-only"))):
        raise ValueError("native parent sandbox widening or inconsistency")
    parent_policy = {"requested": parent_sandbox, "observed": parent_mode,
                     "matches": parent_mode == parent_sandbox,
                     "contextPolicyHash": _sha(json.dumps(policies, sort_keys=True,
                                                          separators=(",", ":")).encode("utf-8"))}
    def tool_interaction(payload):
        kind = payload.get("type")
        return isinstance(kind, str) and (
            kind.endswith("_call") or kind.endswith("_call_output") or "tool" in kind)
    if any(r.get("type") == "response_item" and tool_interaction(r["payload"]) for r in child):
        raise ValueError("no-action routing case invoked a child tool")
    parent_calls = [(i, r["payload"]) for i, r in enumerate(parent)
                    if r.get("type") == "response_item" and tool_interaction(r["payload"])]
    call_ids = set()
    wait_positions = []
    for i, p in parent_calls:
        if p.get("type") != "function_call":
            if p.get("type") != "function_call_output":
                raise ValueError("native parent tool interaction is unsupported")
            continue
        if p.get("name") not in {"spawn_agent", "wait_agent"}:
            raise ValueError("native parent tool is unsupported")
        if not isinstance(p.get("call_id"), str) or not p["call_id"] or p["call_id"] in call_ids:
            raise ValueError("native parent tool call identity is ambiguous")
        call_ids.add(p["call_id"])
        output_matches = items(parent, "function_call_output", call_id=p["call_id"])
        result_i, result_payload = unique(output_matches, "parent tool output")
        if (result_i <= i or _native_turn(p) != parent_turn
                or _native_turn(result_payload) != parent_turn):
            raise ValueError("native parent tool output precedes call")
        if p["name"] == "wait_agent":
            wait_args = p.get("arguments")
            if isinstance(wait_args, str):
                wait_args = json.loads(wait_args, object_pairs_hook=_no_duplicates)
            if (not isinstance(wait_args, dict) or set(wait_args) != {"timeout_ms"}
                    or type(wait_args["timeout_ms"]) is not int
                    or not 10000 <= wait_args["timeout_ms"] <= 3600000 or i <= output_i):
                raise ValueError("native parent wait protocol mismatch")
            wait_positions.append((i, result_i))
    if any(p.get("type") == "function_call_output" and p.get("call_id") not in call_ids
           for _, p in parent_calls):
        raise ValueError("native parent tool output is unlinked")
    delivery_i, delivery = unique(items(child, "agent_message"), "addressed dispatch")
    if delivery.get("author") != "/root" or delivery.get("recipient") != f"/root/probe_{role}":
        raise ValueError("native dispatch address mismatch")
    blocks = delivery.get("content")
    if (not isinstance(blocks, list) or len(blocks) != 2
            or sum(isinstance(b, dict) and b.get("type") == "input_text"
                   and isinstance(b.get("text"), str) for b in blocks) != 1
            or sum(isinstance(b, dict) and b.get("type") == "encrypted_content"
                   and b.get("encrypted_content") == arguments["message"] for b in blocks) != 1):
        raise ValueError("native opaque dispatch blocks mismatch")
    child_turn = _native_turn(delivery)
    if (delivery_i == 0 or child[delivery_i - 1].get("type") != "inter_agent_communication_metadata"
            or child[delivery_i - 1]["payload"].get("trigger_turn") is not True):
        raise ValueError("native dispatch trigger metadata mismatch")
    final_i, final = unique(items(child, "message", role="assistant", phase="final_answer"), "child final")
    if (final.get("content") != [{"type": "output_text", "text": f"LIVE_ROLE:{role}"}]
            or _native_turn(final) != child_turn or final_i <= delivery_i):
        raise ValueError("native child final marker/turn/order mismatch")
    contexts = [(i, r["payload"]) for i, r in enumerate(child) if r.get("type") == "turn_context"]
    effective = [(i, p) for i, p in contexts if p.get("turn_id") == child_turn]
    if not effective or any(i >= delivery_i for i, _ in effective):
        raise ValueError("native active context must precede delivery")
    for i, p in contexts:
        if p.get("turn_id") != child_turn and (i >= effective[0][0] or p not in parent_contexts):
            raise ValueError("native inherited context mismatch")
    settings, _ = _validate_child_contexts(
        [{"type": "turn_context", "payload": p} for _, p in effective], parent,
        "v1", -1, tomllib.loads(config_raw.decode("utf-8")),
        allow_missing_effort=True, allow_config_mismatch=True)
    aggregates = []
    world_positions = []
    for rows in (parent, child):
        worlds = [(i, r["payload"].get("state", {}).get("agents_md")) for i, r in enumerate(rows)
                  if r.get("type") == "world_state"
                  and "agents_md" in r["payload"].get("state", {})]
        world_i, world = unique(worlds, "instruction world state")
        world_positions.append(world_i)
        if not isinstance(world, dict):
            raise ValueError("native instruction state is malformed")
        if (not isinstance(world.get("directory"), str)
                or Path(world["directory"]).resolve() != fixture):
            raise ValueError("native instruction directory mismatch")
        aggregate = world.get("text")
        if not isinstance(aggregate, str) or not aggregate.endswith(manifest) or aggregate.count(manifest) != 1:
            raise ValueError("native instruction aggregate missing exact manifest suffix")
        aggregates.append(aggregate)
    if aggregates[0] != aggregates[1]:
        raise ValueError("native parent/child instruction aggregate mismatch")
    wrapper = f"# AGENTS.md instructions for {fixture}\n\n<INSTRUCTIONS>\n{aggregates[0]}\n</INSTRUCTIONS>"
    def bootstraps(rows):
        matches = []
        for i, p in items(rows, "message", role="user"):
            blocks = p.get("content")
            if (not isinstance(blocks, list) or any(not isinstance(b, dict)
                    or b.get("type") != "input_text" or not isinstance(b.get("text"), str)
                    for b in blocks)):
                continue
            if any(b["text"] == wrapper for b in blocks):
                if len(blocks) != 2 or blocks[0]["text"] != wrapper:
                    raise ValueError("native bootstrap must contain two ordered blocks")
                _native_environment(blocks[1]["text"], fixture)
                matches.append((i, p))
        return matches
    bootstrap_i, bootstrap = unique(bootstraps(child), "model-visible bootstrap")
    parent_bootstrap_i, parent_bootstrap = unique(bootstraps(parent), "parent bootstrap")
    if ([i for i, _ in items(parent, "message", role="user")] != [parent_bootstrap_i, request_i]
            or [i for i, _ in items(child, "message", role="user")] != [bootstrap_i]):
        raise ValueError("native task contains extra user instructions")
    if (not bootstrap_i < world_positions[1] < effective[0][0]
            or _native_turn(bootstrap) != child_turn
            or bootstrap["content"][1]["text"] != parent_bootstrap["content"][1]["text"]
            or not parent_bootstrap_i < world_positions[0] < request_i
            or any(not world_positions[0] < i < request_i for i, r in enumerate(parent)
                   if r.get("type") == "turn_context")
            or _native_turn(parent_bootstrap) != parent_turn):
        raise ValueError("native bootstrap must precede active context")
    returned_i, returned = unique(items(parent, "agent_message"), "child return")
    if (returned.get("author") != f"/root/probe_{role}" or returned.get("recipient") != "/root"
            or not isinstance(delivery.get("id"), str) or not delivery["id"]
            or not isinstance(returned.get("id"), str) or not returned["id"]
            or returned["id"] == delivery["id"]
            or returned_i <= output_i or not wait_positions
            or any(result_i >= returned_i for _, result_i in wait_positions)):
        raise ValueError("native reciprocal child return mismatch")
    return_blocks = returned.get("content")
    if (not isinstance(return_blocks, list) or len(return_blocks) != 1
            or return_blocks[0].get("type") != "input_text"
            or not isinstance(return_blocks[0].get("text"), str)
            or f"LIVE_ROLE:{role}" not in return_blocks[0]["text"]):
        raise ValueError("native reciprocal child final reply mismatch")
    from datetime import datetime
    try:
        final_time = datetime.fromisoformat(child[final_i]["timestamp"].replace("Z", "+00:00"))
        return_time = datetime.fromisoformat(parent[returned_i]["timestamp"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("native final/return timestamps missing") from error
    if return_time < final_time:
        raise ValueError("native parent return precedes child final")
    for rows, last in ((parent, returned_i), (child, final_i)):
        terminal_i, terminal = unique([(i, r["payload"]) for i, r in enumerate(rows)
            if r.get("type") == "event_msg" and r["payload"].get("type") == "task_complete"], "terminal")
        if terminal_i <= last:
            raise ValueError("native terminal precedes result")
    return {"variant": variant, "parentSessionId": parent_id, "childSessionId": child_id,
            "settings": settings, "privateTaskLinked": True,
            "parentPolicy": parent_policy,
            "parentRolloutHash": _sha(parent_raw), "childRolloutHash": _sha(child_raw),
            "taskProof": {"tag": "native-bootstrap-manifest-and-addressed-dispatch",
                          "taskManifestHash": proof["taskManifestHash"],
                          "taskSourceHash": proof["taskSourceHash"],
                          "aggregateHash": _sha(aggregates[0].encode("utf-8")),
                          "dispatchHash": _sha(arguments["message"].encode("utf-8")),
                          "parentTurnId": parent_turn, "childTurnId": child_turn}}


def _validate_private_link(
    role: str, gate: str, case_id: str, sanitized: str,
    parent_id: str | None, child_id: str, parent_raw: bytes, child_raw: bytes,
    variant: str, config_raw: bytes, expected_head: str | None,
    expected_readme_title: str,
    expected_parent_sandbox: str,
    expected_cwd: str | None = None,
    manifest_proof: dict[str, Any] | None = None,
    *,
    _identity: LegacyRunIdentity | None = None,
) -> dict[str, Any]:
    """Validate task, role, sessions, terminal rows and effective context."""
    identity = _identity or _legacy_identity()
    parent_rows, child_rows = _parse_rows(parent_raw), _parse_rows(child_raw)
    parent_meta = [row["payload"] for row in parent_rows
                   if row.get("type") == "session_meta"
                   and row["payload"].get("agent_role") is None]
    child_meta = [row["payload"] for row in child_rows
                  if row.get("type") == "session_meta"
                  and row["payload"].get("agent_role") == role]
    if (len(parent_meta) != 1 or len(child_meta) != 1
            or parent_meta[0].get("id") != parent_id
            or parent_meta[0].get("cli_version") != identity.profile.expected_version
            or child_meta[0].get("id") != child_id
            or child_meta[0].get("session_id") != parent_id
            or child_meta[0].get("parent_thread_id") != parent_id
            or child_meta[0].get("cli_version") != identity.profile.expected_version
            or child_meta[0].get("agent_path") != f"/root/probe_{role}"):
        raise ValueError("private parent-child identity mismatch")
    if _manifest_case(case_id):
        return _validate_manifest_link(role, case_id, parent_rows, child_rows,
                                       parent_id, child_id, variant, config_raw,
                                       expected_parent_sandbox, expected_cwd, manifest_proof,
                                       parent_raw, child_raw)
    calls = [row["payload"] for row in parent_rows
             if row.get("type") == "response_item"
             and row["payload"].get("type") == "function_call"
             and row["payload"].get("name") == "spawn_agent"]
    if len(calls) != 1:
        raise ValueError("private parent spawn cardinality mismatch")
    child_task_position = _validate_spawn_task(
        calls[0].get("arguments"), child_rows, role, gate, case_id, variant
    )
    outputs = [row["payload"] for row in parent_rows
               if row.get("type") == "response_item"
               and row["payload"].get("type") == "function_call_output"
               and row["payload"].get("call_id") == calls[0].get("call_id")]
    final = [row["payload"] for row in child_rows
             if row.get("type") == "response_item"
             and row["payload"].get("type") == "message"
             and row["payload"].get("role") == "assistant"
             and row["payload"].get("phase") == "final_answer"]
    if (len(outputs) != 1 or len(final) != 1
            or final[0].get("content") != [{"type": "output_text", "text": f"LIVE_ROLE:{role}"}]
            or len([row for row in parent_rows if row.get("type") == "event_msg"
                    and row["payload"].get("type") == "task_complete"]) != 1
            or len([row for row in child_rows if row.get("type") == "event_msg"
                    and row["payload"].get("type") == "task_complete"]) != 1):
        raise ValueError("private role task has no unique terminal result")
    output = outputs[0].get("output")
    if isinstance(output, str):
        output = json.loads(output)
    if (not isinstance(output, dict)
            or output.get("task_name") != f"/root/probe_{role}"):
        raise ValueError("private spawn result does not identify the expected child")
    if case_id.startswith("l6-system-shell-"):
        if (expected_cwd is None or any(meta.get("cwd") != expected_cwd
                                       for meta in (parent_meta[0], child_meta[0]))):
            raise ValueError("system-shell case is not bound to the expected fixture cwd")
        observed_commands = []
        for call, linked_output in _linked_child_tools(child_rows, variant):
            if call.get("name") != "exec":
                continue
            args = _command_arguments(call.get("arguments", call.get("input")))
            if (args is None or args.get("shell") != SYSTEM_SHELL
                    or args.get("login") is not False
                    or ("workdir" in args and args["workdir"] != expected_cwd)):
                raise ValueError("system-shell command did not retain the fixed native shell/cwd")
            observed_commands.append(_command_argument(args))
            if not _native_script_matches(call.get("arguments", call.get("input")), observed_commands[-1]):
                raise ValueError("system-shell case did not retain the complete compiled native-result script")
            if (linked_output is None or _extract_native_result(
                    linked_output.get("output", linked_output.get("content")))[1] is None):
                raise BehaviorEvidenceError("system-shell call lacks native exit evidence",
                                            _private_failure_codes(child_rows))
        expected_commands = (["rtk proxy python -B behavior-validation/known_failure.py"]
                             if role == "code_validator" else [README_COMMAND, "rtk git rev-parse HEAD"])
        if observed_commands != expected_commands:
            raise ValueError("system-shell case did not execute the exact compiled command sequence")
    validator_failure_observed = False
    if gate == "L6" and role == "code_validator":
        tools = _linked_child_tools(child_rows, variant)
        commands = [(call, output) for call, output in tools if call.get("name") == "exec"]
        if (any(call.get("name") not in {"exec", "send_message"} for call, _ in tools)
                or len(commands) != 1 or commands[0][1] is None):
            raise ValueError("validator rollout lacks linked failing-test command and nonzero result")
        validator_failure_observed = (
            _known_failure_tool_is_exact(
                commands[0][0].get("arguments", commands[0][0].get("input")),
                commands[0][1].get("output", commands[0][1].get("content"))
            )
        )
        if not validator_failure_observed:
            raise ValueError("validator rollout lacks exact failing command and nonzero result")
    validator_confirmed = False
    if case_id in L7_VALIDATOR_CASES:
        writer = L7_VALIDATOR_CASES[case_id]
        target, content = WRITER_TARGETS[writer]
        tools = _linked_child_tools(child_rows, variant)
        if any(call.get("name") not in {"exec", "send_message"} for call, _ in tools):
            raise ValueError("independent validator invoked an unsupported child tool")
        for call, output_row in tools:
            if call.get("name") != "exec":
                continue
            if output_row is None:
                continue
            if _exact_hash_validation(call.get("arguments", call.get("input")),
                                      output_row.get("output", output_row.get("content")),
                                      target, content):
                validator_confirmed = True
        if not validator_confirmed:
            raise ValueError("independent validator rollout did not read the exact writer result")
    if gate == "L8":
        if _contains_child_tool_call(child_rows):
            raise ValueError("no-action routing case invoked a child tool")
    if gate == "L6" and role != "code_validator":
        tools = _linked_child_tools(child_rows, variant)
        commands = [(call, output) for call, output in tools if call.get("name") == "exec"]
        if (not commands
                or any(call.get("name") not in {"exec", "send_message"} for call, _ in tools)
                or any(output is None for _, output in commands)):
            raise ValueError("read-only task lacks observed inspection command")
        readme_seen = False
        head_seen = False
        for call, linked_output in commands:
            if child_rows.index(next(row for row in child_rows
                    if row.get("type") == "response_item" and row.get("payload") is call)) <= child_task_position:
                raise ValueError("read-only command occurred before the delegated task")
            command_text = _command_argument(call.get("arguments", call.get("input")))
            kinds = _probe_command_kinds(command_text) if command_text is not None else None
            if kinds is None:
                raise BehaviorEvidenceError("read-only rollout contains an unapproved command shape",
                                            _private_failure_codes(child_rows))
            output = linked_output.get("output", linked_output.get("content"))
            output_text, exit_code = _extract_output(output)
            output_lines = [line.lstrip("#").strip() for line in output_text.splitlines()]
            if "readme" in kinds and expected_readme_title in output_lines and exit_code == 0:
                readme_seen = True
            if ("head" in kinds and expected_head
                    and expected_head in [line.strip() for line in output_text.splitlines()]
                    and exit_code == 0):
                head_seen = True
        if not (readme_seen and head_seen):
            raise BehaviorEvidenceError(
                "read-only rollout does not prove the fixed README and HEAD challenge",
                _private_failure_codes(child_rows),
            )
    settings = _private_settings(child_raw)
    import tomllib
    config = tomllib.loads(config_raw.decode("utf-8"))
    parent_contexts = [row["payload"] for row in parent_rows if row.get("type") == "turn_context"]
    if not parent_contexts or any(
        not isinstance(item.get("sandbox_policy"), dict)
        or item["sandbox_policy"].get("type") != expected_parent_sandbox
        for item in parent_contexts
    ):
        raise ValueError("private parent sandbox does not match the bounded case mode")
    settings, contexts = _validate_child_contexts(
        child_rows, parent_rows, variant, child_task_position, config,
        allow_missing_effort=gate == "L8",
        allow_config_mismatch=gate == "L8" and case_id.startswith("l8-source-settings-"),
    )
    if variant == "v2":
        first_effective_context = next(index for index, row in enumerate(child_rows)
                                       if row.get("type") == "turn_context"
                                       and index > child_task_position)
        if any(index <= first_effective_context for index, row in enumerate(child_rows)
               if row.get("type") == "response_item"
               and isinstance(row.get("payload"), dict)
               and row["payload"].get("type") in {"function_call", "custom_tool_call"}):
            raise ValueError("child tool invocation preceded the effective role context")
    if gate == "L8" and not case_id.startswith("l8-source-settings-"):
        routing_marker = (
            f"L8_ROUTING:{role}:model={settings['model']}:"
            f"effort={settings['effort'] or 'UNOBSERVED'}:sandbox={settings['sandbox']}"
        )
        child_messages = [row["payload"] for row in child_rows
                          if row.get("type") == "response_item"
                          and row["payload"].get("type") in {"message", "agent_message"}]
        if not any(routing_marker in json.dumps(message, sort_keys=True)
                   for message in child_messages):
            raise ValueError("private child lacks the exact routing marker for observed settings")
    return {"variant": variant, "parentSessionId": parent_id,
            "childSessionId": child_id, "settings": settings,
            "parentRolloutHash": _sha(parent_raw), "childRolloutHash": _sha(child_raw),
            "validatorFailureObserved": validator_failure_observed,
            "validatorConfirmed": validator_confirmed,
            "privateTaskLinked": True}


def _case_paths(root: Path, case_id: str) -> tuple[Path, Path]:
    results = root / "results" / "behavior-cases"
    if not results.resolve().is_relative_to(root.resolve()):
        raise ValueError("behavior evidence path escapes owned root")
    results.mkdir(parents=True, exist_ok=True)
    output = results / f"{case_id}.json"
    stream = results / f"{case_id}.sanitized.jsonl"
    if output.exists() or stream.exists():
        raise ValueError("case evidence already exists")
    return output, stream


def _configured_settings(root: Path, role: str) -> dict[str, str | None]:
    import tomllib
    config = root / "fixture" / ".codex" / "agents" / f"{role.replace('_', '-')}.toml"
    value = tomllib.loads(config.read_text(encoding="utf-8"))
    if value.get("name") != role:
        raise ValueError("installed role configuration is mismatched")
    return {
        "model": value.get("model") if isinstance(value.get("model"), str) else None,
        "effort": value.get("model_reasoning_effort") if isinstance(value.get("model_reasoning_effort"), str) else None,
        "sandbox": value.get("sandbox_mode") if isinstance(value.get("sandbox_mode"), str) else None,
    }


def _config_hashes(root: Path, role: str) -> dict[str, str]:
    source = Path(__file__).resolve().parent.parent / "agents" / f"{role.replace('_', '-')}.toml"
    installed = root / "fixture" / ".codex" / "agents" / f"{role.replace('_', '-')}.toml"
    source_raw, installed_raw = source.read_bytes(), installed.read_bytes()
    if source_raw != installed_raw:
        raise ValueError("installed role config differs from the source definition")
    return {"source": _sha(source_raw), "installed": _sha(installed_raw)}


def run_case(
    run_root: Path,
    index_path: Path,
    private_root: Path,
    case_id: str,
    *,
    timeout: int = 120,
    codex_command: str = "codex",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    behavior_private_root: Path | None = None,
    source_root: Path | None = None,
    policy: CompatibilityRegistry | None = None,
    _identity: LegacyRunIdentity | None = None,
) -> dict[str, Any]:
    identity = _identity or _legacy_identity(source_root, policy)
    if not isinstance(case_id, str) or not CASE_PATTERN.fullmatch(case_id) or case_id not in CASE_ROLES:
        raise ValueError("case id is not allowlisted")
    if not 1 <= timeout <= 120:
        raise ValueError("timeout must be between 1 and 120 seconds")
    root, marker = validate_marker(run_root)
    l5 = verify_l5(root, index_path, private_root, _identity=identity)
    capture_root = _behavior_source_root(private_root, behavior_private_root, require_exists=False)
    with _owned_run_lock(root):
        try:
            return _run_case_locked(root, marker, l5, capture_root, case_id,
                                    timeout=timeout, codex_command=codex_command,
                                    runner=runner, _identity=identity)
        finally:
            _finalize_after_case(root, marker)


def _read_attempt_snapshot(root: Path, relative: Any, expected_hash: Any) -> dict[str, Any]:
    if not isinstance(relative, str) or not isinstance(expected_hash, str):
        raise ValueError("attempt snapshot reference is incomplete")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("attempt snapshot path is outside the owned run")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict) or _snapshot_digest(snapshot) != expected_hash:
        raise ValueError("attempt snapshot hash does not match its receipt")
    return snapshot


def _behavior_source_root(l5_root: Path, selected: Path | None, *, require_exists: bool = True) -> Path:
    """Permit an explicit dated sibling under the same private session month."""
    if selected is None:
        return l5_root
    original = l5_root.resolve(strict=True)
    chosen = selected.resolve(strict=require_exists)
    if (selected.is_symlink() or (require_exists and not chosen.is_dir())
            or (chosen.exists() and not chosen.is_dir())
            or chosen.parent != original.parent
            or not re.fullmatch(r"(?:0[1-9]|[12][0-9]|3[01])", chosen.name)):
        raise ValueError("behavior sources must be an explicit dated sibling of the L5 sources")
    return chosen


def _replay_attempt_impl(
    run_root: Path, index_path: Path, private_root: Path, attempt_path: Path,
    behavior_private_root: Path | None = None, *,
    _identity: LegacyRunIdentity | None = None,
) -> dict[str, Any]:
    """Offline replay of a captured L6/L8 attempt; performs no CLI invocation."""
    identity = _identity or _legacy_identity()
    root, marker = validate_marker(run_root)
    receipt_file = attempt_path.resolve()
    if not receipt_file.is_relative_to(root.resolve()) or not receipt_file.is_file():
        raise ValueError("attempt receipt is outside the owned run")
    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    if (not isinstance(receipt, dict)
            or receipt.get("schema") != "codex-live-behavior-attempt/v1"
            or receipt.get("accepted") is not False
            or receipt.get("runtimeValidated") is not False
            or receipt.get("status") != "CAPTURED"
            or receipt.get("callCount") != 1
            or receipt.get("codexVersion") != identity.profile.expected_version
            or receipt.get("runId") != marker.get("runId")
            or CASE_ROLES.get(receipt.get("caseId"), (None, None))[1] not in {"L6", "L8"}
            or receipt.get("timedOut") is not False
            or receipt.get("exitCode") != 0):
        raise ValueError("attempt receipt is not eligible for offline L6/L8 replay")
    l5 = verify_l5(root, index_path, private_root, _identity=identity)
    before = _read_attempt_snapshot(root, receipt.get("snapshotPaths", {}).get("before"),
                                    receipt.get("beforeStateHash"))
    after = _read_attempt_snapshot(root, receipt.get("snapshotPaths", {}).get("after"),
                                   receipt.get("afterStateHash"))
    evidence_rel = receipt.get("evidencePath")
    if not isinstance(evidence_rel, str):
        raise ValueError("attempt receipt has no sanitized stream")
    stream_file = (root / evidence_rel).resolve()
    if not stream_file.is_relative_to(root.resolve()) or not stream_file.is_file():
        raise ValueError("sanitized stream is outside the owned run")
    sanitized = stream_file.read_text(encoding="utf-8")
    if (_sha(sanitized.encode("utf-8")) != receipt.get("captureHash")
            or sanitize_event_stream(sanitized) != sanitized):
        raise ValueError("sanitized attempt stream hash or idempotence check failed")
    case_id = receipt["caseId"]
    role, gate = CASE_ROLES[case_id]
    parsed = parse_event_stream(sanitized, expected_roles=(role,))
    if (parsed is None or parsed.reason_codes != ("MISSING_ATTRIBUTION",)
            or not parsed.terminal_seen or parsed.parent_session_id is None
            or parsed.attributed_children):
        raise ValueError("attempt public stream does not have the captured attribution shape")
    config_hashes = _config_hashes(root, role)
    config_relative = f".codex/agents/{role.replace('_', '-')}.toml"
    def snapshot_config_hash(snapshot: dict[str, Any]) -> Any:
        inventories = snapshot.get("checkoutInventories")
        fixture_inventory = inventories.get("fixture") if isinstance(inventories, dict) else None
        files = fixture_inventory.get("files") if isinstance(fixture_inventory, dict) else None
        return files.get(config_relative) if isinstance(files, dict) else None
    if (snapshot_config_hash(before) != config_hashes["installed"]
            or snapshot_config_hash(after) != config_hashes["installed"]
            or ("configHashes" in receipt and receipt.get("configHashes") != config_hashes)
            or ("l5Status" in receipt and receipt.get("l5Status") != l5.get("status"))):
        raise ValueError("attempt config or L5 evidence differs from current source replay")
    started_at = receipt.get("startedAt")
    if not isinstance(started_at, str):
        raise ValueError("attempt start time is missing")
    from datetime import datetime
    started_epoch = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
    parent_raw, child_raw, variant, child_id = _find_private_pair(
        _behavior_source_root(private_root, behavior_private_root), parsed.parent_session_id, role, started_epoch,
        _identity=identity,
    )
    config_raw = (root / "fixture" / ".codex" / "agents" /
                  f"{role.replace('_', '-')}.toml").read_bytes()
    configured = _configured_settings(root, role)
    link = _validate_private_link(role, gate, case_id, sanitized,
                                  parsed.parent_session_id, child_id, parent_raw,
                                  child_raw, variant, config_raw, before.get("head"),
                                  _fixture_readme_title(root / "fixture"),
                                  "read-only" if gate == "L6" else configured["sandbox"],
                                  expected_cwd=str(root / "fixture"),
                                  manifest_proof=_task_manifest_proof(root / "fixture", before, after)
                                  if _manifest_case(case_id) else None, _identity=identity)
    _reconcile_link(variant, sanitized, case_id, role, parent_raw, child_raw,
                    config_raw, link, _identity=identity)
    if gate == "L6":
        checks, candidate = _evaluate_l6_readonly(case_id, before, after, 0, False, link, configured)
    else:
        checks, candidate = _evaluate_l8_routing(role, configured, link,
                                               _snapshot_digest(before) == _snapshot_digest(after), 0, False)
    if before.get("head") != after.get("head"):
        candidate = "FAIL"
    output_path, _ = _case_paths(root, case_id)
    evidence = {
        "schema": OUTPUT_SCHEMA, "caseId": case_id, "gate": gate, "role": role,
        "runId": marker["runId"], "codexVersion": identity.profile.expected_version,
        "l5Evidence": {"status": l5["status"], "runId": l5["runId"],
                       "roleCount": l5["roleCount"], "sessionCount": l5["sessionCount"]},
        "status": "REPLAYED", "candidateResult": candidate,
        "callCount": 0, "exitCode": 0, "timedOut": False,
        "parentSessionId": parsed.parent_session_id, "childSessionId": child_id,
        "captureHash": receipt["captureHash"],
        "privateChildRolloutHash": _sha(child_raw),
        "privateParentRolloutHash": _sha(parent_raw), "privateRolloutPersisted": False,
        "privateLinkStatus": "CORRELATED", "rolloutVariant": variant,
        "publicAttribution": "MISSING_ATTRIBUTION", "configured": configured,
        "observed": link["settings"], "configHashes": config_hashes,
        "taskProof": link.get("taskProof"),
        "parentPolicy": link.get("parentPolicy"),
        "effectiveSandbox": link["settings"]["sandbox"],
        "beforeStateHash": receipt["beforeStateHash"],
        "afterStateHash": receipt["afterStateHash"], "checks": checks,
        "evidencePath": evidence_rel,
        "replayedAttemptPath": receipt_file.relative_to(root).as_posix(),
        "runtimeValidated": False,
    }
    atomic_write_json(output_path, evidence)
    return evidence


def replay_attempt(
    run_root: Path, index_path: Path, private_root: Path, attempt_path: Path, *,
    behavior_private_root: Path | None = None,
    source_root: Path | None = None,
    policy: CompatibilityRegistry | None = None,
    _identity: LegacyRunIdentity | None = None,
) -> dict[str, Any]:
    """Fail-closed replay wrapper with a secret-free durable blocked verdict."""
    identity = _identity or _legacy_identity(source_root, policy)
    root, marker = validate_marker(run_root)
    try:
        with _owned_run_lock(root):
            return _replay_attempt_impl(root, index_path, private_root, attempt_path, behavior_private_root, _identity=identity)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError,
            LiveValidationError, subprocess.SubprocessError) as error:
        directory = root / "results" / "behavior-attempts"
        directory.mkdir(parents=True, exist_ok=True)
        attempt_label = attempt_path.stem
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", attempt_label):
            attempt_label = "unknown-attempt"
        blocked_path = directory / f"{attempt_label}.replay.json"
        reasons = (error.reason_codes if isinstance(error, BehaviorEvidenceError)
                   else ["OFFLINE_REPLAY_REJECTED"])
        allowed = {"L6_READONLY_CHALLENGE_UNPROVEN", "WINDOWS_CREATEPROCESS_FAILURE",
                   "OFFLINE_REPLAY_REJECTED"}
        reasons = sorted(set(code for code in reasons if code in allowed)) or ["OFFLINE_REPLAY_REJECTED"]
        atomic_write_json(blocked_path, {
            "schema": "codex-live-behavior-replay/v1", "status": "BLOCKED",
            "candidateResult": "BLOCKED", "accepted": False,
            "runtimeValidated": False, "callCount": 0,
            "runId": marker.get("runId"), "codexVersion": identity.profile.expected_version,
            "attemptLabel": attempt_label,
            "reasonCodes": reasons,
            "privateRolloutPersisted": False,
        })
        return {"schema": "codex-live-behavior-replay/v1", "status": "BLOCKED",
                "candidateResult": "BLOCKED", "runtimeValidated": False,
                "reasonCodes": reasons,
                "evidencePath": blocked_path.relative_to(root).as_posix()}
    finally:
        _finalize_after_case(root, marker)


def _run_case_locked(
    root: Path,
    marker: dict[str, Any],
    l5: dict[str, Any],
    private_root: Path,
    case_id: str,
    *,
    timeout: int,
    codex_command: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    _identity: LegacyRunIdentity | None = None,
) -> dict[str, Any]:
    identity = _identity or _legacy_identity()
    role, gate = CASE_ROLES[case_id]
    fixture = root / "fixture"
    output_path, stream_path = _case_paths(root, case_id)
    resolved = _resolve_codex_command(codex_command)
    version_exit, version_text = _version(resolved, fixture, timeout)
    if version_exit != 0 or version_text != identity.profile.cli_banner:
        raise ValueError(f"pinned Codex CLI {identity.profile.expected_version} is unavailable")
    failure_hash = _ensure_known_failure(fixture) if gate == "L6" and role == "code_validator" else None
    if _manifest_case(case_id):
        _prepare_task_manifest(fixture, create=True)
    sqlite_home = root / "results" / "private" / "behavior" / case_id / str(uuid.uuid4())
    sqlite_home.mkdir(parents=True, exist_ok=False)
    configured = _configured_settings(root, role)
    config_hashes = _config_hashes(root, role)
    mode = "read-only" if gate == "L6" else configured["sandbox"]
    if mode not in {"read-only", "workspace-write"}:
        raise ValueError("effective sandbox is not in the fixed allowlist")
    if gate == "L6" and (mode != "read-only" or configured["sandbox"] != "read-only"):
        raise ValueError("L6 role is not configured and invoked read-only")
    if gate == "L7" and case_id not in L7_VALIDATOR_CASES and mode != "workspace-write":
        raise ValueError("L7 writer is not configured for workspace writes")
    command = [*resolved, "exec", "--ignore-user-config", "--strict-config",
               "--enable", "multi_agent", "-c",
               f'projects={{{json.dumps(str(fixture.resolve()))}={{trust_level="trusted"}}}}',
               "--json", "--sandbox", mode, "-C", str(fixture), _fixed_prompt(case_id, role, gate)]
    environment = _codex_environment(sqlite_home, dict(os.environ))
    before = capture_git_snapshot(root, timeout)
    started = time.monotonic()
    started_epoch = time.time()
    started_at = _utc_now()
    timed_out = False
    try:
        completed = runner(command, cwd=fixture, env=environment, capture_output=True,
                           stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, check=False)
        raw_stdout = completed.stdout or ""
        raw_stderr = completed.stderr or ""
        exit_code: int | None = completed.returncode
    except subprocess.TimeoutExpired as error:
        raw_stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        raw_stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        exit_code = None
        timed_out = True
    after = capture_git_snapshot(root, timeout)
    elapsed = int((time.monotonic() - started) * 1000)
    # Gate persistence on canonical sanitization and sanitizer idempotence.
    sanitized = sanitize_event_stream(raw_stdout)
    sanitizer_valid = sanitize_event_stream(sanitized) == sanitized
    parsed = parse_event_stream(sanitized, expected_roles=(role,)) if sanitizer_valid else None
    usable = (parsed is not None and parsed.reason_codes == ("MISSING_ATTRIBUTION",)
              and parsed.terminal_seen and parsed.parent_session_id is not None
              and not parsed.attributed_children and not timed_out and exit_code == 0)
    diagnostics = _diagnostic_codes(raw_stdout, raw_stderr, timed_out)
    if not sanitizer_valid:
        diagnostics.append("PUBLIC_STREAM_SANITIZATION_REJECTED")
    elif not usable:
        diagnostics.append("PUBLIC_STREAM_REJECTED")
    attempt_path = _record_attempt(
        root, marker, case_id, sqlite_home.name, before, after,
        sanitized if sanitizer_valid else None, started_at=started_at, elapsed=elapsed,
        timeout=timeout, exit_code=exit_code, timed_out=timed_out, reason_codes=diagnostics,
        config_hashes=config_hashes, l5_status=l5["status"],
        _identity=identity,
    )
    if not usable:
        return {"schema": OUTPUT_SCHEMA, "status": "BLOCKED", "candidateResult": "BLOCKED",
                "caseId": case_id, "runId": marker["runId"], "reasonCodes": sorted(set(diagnostics)),
                "attemptEvidencePath": attempt_path, "runtimeValidated": False}
    assert parsed is not None
    before_hash, after_hash = _snapshot_digest(before), _snapshot_digest(after)
    state_unchanged = before_hash == after_hash
    parent_raw = private_rollout = b""
    child_id = None
    try:
        parent_raw, private_rollout, variant, child_id = _find_private_pair(
            private_root, parsed.parent_session_id, role, started_epoch,
            _identity=identity,
        )
        config_path = fixture / ".codex" / "agents" / f"{role.replace('_', '-')}.toml"
        config_raw = config_path.read_bytes()
        link = _validate_private_link(
            role, gate, case_id, sanitized, parsed.parent_session_id, child_id,
            parent_raw, private_rollout, variant, config_raw,
            before.get("head"), _fixture_readme_title(fixture), mode,
            expected_cwd=str(fixture),
            manifest_proof=_task_manifest_proof(fixture, before, after)
            if _manifest_case(case_id) else None,
            _identity=identity,
        )
        if gate == "L6" or case_id in L7_VALIDATOR_CASES:
            _reconcile_link(variant, sanitized, case_id, role, parent_raw,
                            private_rollout, config_raw, link, _identity=identity)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError,
            LiveValidationError, subprocess.SubprocessError) as error:
        reasons = (error.reason_codes if isinstance(error, BehaviorEvidenceError)
                   else ["PRIVATE_LINK_REJECTED"])
        evaluation_path = _record_rejection(
            root, marker, case_id, attempt_path, reasons,
            capture_hash=_sha(sanitized.encode("utf-8")),
            parent_id=parsed.parent_session_id, child_id=child_id,
            parent_hash=_sha(parent_raw) if parent_raw else None,
            child_hash=_sha(private_rollout) if private_rollout else None,
            _identity=identity,
        )
        return {"schema": OUTPUT_SCHEMA, "status": "BLOCKED",
                "candidateResult": "BLOCKED", "caseId": case_id,
                "runId": marker["runId"], "reasonCodes": sorted(set(reasons)),
                "attemptEvidencePath": attempt_path,
                "evaluationEvidencePath": evaluation_path,
                "runtimeValidated": False}
    private_hash = _sha(private_rollout)
    observed = link["settings"]
    routing = "UNVERIFIED"
    if observed["model"] is not None:
        routing = "PASS" if observed["model"] == configured["model"] else "FAIL"
    if observed["effort"] is not None and configured["effort"] and observed["effort"] != configured["effort"]:
        routing = "FAIL"
    elif observed["effort"] is None and routing == "PASS":
        routing = "UNVERIFIED"
    expected_mode = configured["sandbox"]
    effective_sandbox = observed["sandbox"]
    checks: dict[str, Any] = {"stateUnchanged": state_unchanged}
    if gate == "L6":
        checks, candidate = _evaluate_l6_readonly(case_id, before, after,
                                                   exit_code, timed_out, link, configured)
        if role == "code_validator":
            checks["knownFailureObserved"] = bool(failure_hash is not None and checks["knownFailureObserved"])
            if not checks["knownFailureObserved"]:
                candidate = "FAIL"
    elif gate == "L7":
        checks["headUnchanged"] = before.get("head") == after.get("head")
        if case_id in L7_VALIDATOR_CASES:
            writer = L7_VALIDATOR_CASES[case_id]
            target, content = WRITER_TARGETS[writer]
            checks["validatorConfirmed"] = link["validatorConfirmed"]
            checks["stateUnchanged"] = state_unchanged
        else:
            target, content = WRITER_TARGETS[role]
            target_path = fixture / target
            checks["onlyAllowedTargetChanged"] = _writer_snapshot_is_exact(
                before, after, target, content
            )
            checks["targetContentValid"] = target_path.is_file() and target_path.read_bytes() == (content + "\n").encode("utf-8")
    if gate == "L8":
        checks.update({"routing": routing, "modelMatches": routing != "FAIL",
                       "effortStatus": "UNVERIFIED" if observed["effort"] is None else
                       ("PASS" if observed["effort"] == configured["effort"] else "FAIL"),
                       "sandboxMatches": effective_sandbox == expected_mode})
    expected_state = state_unchanged if gate != "L7" or case_id in L7_VALIDATOR_CASES else True
    passed = (exit_code == 0 and expected_state and
              all(value is True for key, value in checks.items()
                  if key not in {"knownFailureObserved", "routing", "effortStatus", "stateUnchanged"}) and
              checks.get("knownFailureObserved", True) is not False and
              checks.get("routing", "PASS") != "FAIL" and
              checks.get("effortStatus", "PASS") != "FAIL")
    if gate != "L6":
        candidate = _candidate_result(gate, passed, routing, observed["effort"])
    if gate == "L8":
        checks, candidate = _evaluate_l8_routing(role, configured, link,
                                               state_unchanged, exit_code, timed_out)
    # Never persist raw output or stderr. Keep stderr out of evidence altogether.
    stream_path.write_text(sanitized, encoding="utf-8", newline="\n")
    evidence = {
        "schema": OUTPUT_SCHEMA, "caseId": case_id, "gate": gate, "role": role,
        "runId": marker["runId"], "codexVersion": identity.profile.expected_version,
        "l5Evidence": {"status": l5["status"], "runId": l5["runId"],
                       "roleCount": l5["roleCount"], "sessionCount": l5["sessionCount"]},
        "status": "CAPTURED",
        "candidateResult": candidate,
        "startedAt": started_at, "completedAt": _utc_now(), "durationMs": elapsed,
        "timeoutSeconds": timeout, "callCount": 1, "exitCode": exit_code,
        "timedOut": timed_out, "parentSessionId": parsed.parent_session_id,
        "childSessionId": child_id, "captureHash": _sha(sanitized.encode("utf-8")),
        "privateChildRolloutHash": private_hash,
        "privateParentRolloutHash": _sha(parent_raw),
        "privateRolloutPersisted": False,
        "privateLinkStatus": "CORRELATED", "rolloutVariant": variant,
        "publicAttribution": "MISSING_ATTRIBUTION",
        "configured": configured, "observed": observed,
        "taskProof": link.get("taskProof"),
        "parentPolicy": link.get("parentPolicy"),
        "configHashes": config_hashes,
        "effectiveSandbox": effective_sandbox,
        "beforeStateHash": before_hash, "afterStateHash": after_hash,
        "checks": checks, "evidencePath": stream_path.relative_to(root).as_posix(),
        "attemptEvidencePath": attempt_path,
        "runtimeValidated": False,
    }
    atomic_write_json(output_path, evidence)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owned-run-root", type=Path, required=True)
    parser.add_argument("--l5-index", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--behavior-private-root", type=Path,
                        help="Explicit dated sibling for new behavior sources; L5 sources remain pinned")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--case", choices=tuple(sorted(CASE_ROLES)))
    mode.add_argument("--replay-attempt", type=Path)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--codex-command", default="codex")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 120:
        parser.error("--timeout must be between 1 and 120 seconds")
    try:
        if args.replay_attempt is not None:
            result = replay_attempt(args.owned_run_root, args.l5_index,
                                    args.private_root, args.replay_attempt,
                                    behavior_private_root=args.behavior_private_root)
        else:
            result = run_case(args.owned_run_root, args.l5_index, args.private_root,
                              args.case, timeout=args.timeout, codex_command=args.codex_command,
                              behavior_private_root=args.behavior_private_root)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["candidateResult"] in {"PASS", "UNVERIFIED"} else 1
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError,
            LiveValidationError, subprocess.SubprocessError):
        print(json.dumps({"schema": OUTPUT_SCHEMA, "status": "BLOCKED",
                          "runtimeValidated": False}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
