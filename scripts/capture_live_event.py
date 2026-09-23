"""Capture one bounded, sanitized Codex JSONL live-validation event stream."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from codex_event_adapter import (
    capture_fixture_manifest,
    parse_event_stream,
    sanitize_event_stream,
    sanitize_text,
)


PINNED_CODEX_VERSION = "codex-cli 0.155.1"
ROLE_NAMES = (
    "code_explorer",
    "quick_implementer",
    "implementer",
    "luna_escalation",
    "sol_architect",
    "sol_architect_deep",
    "code_validator",
    "code_reviewer",
    "commit_pusher",
)


def _prompt(roles: tuple[str, ...]) -> str:
    role_protocol = "\n".join(
        f"- {role}: call spawn_agent with agent_type={role} and "
        f"task_name=probe_{role}; instruct that child to reply solely "
        f"LIVE_ROLE:{role} and not to delegate."
        for role in roles
    )
    return (
        "Live validation fixture. Follow this diagnostic protocol exactly.\n"
        "Process the requested roles below in order, one at a time. For each "
        "role, attempt one spawn_agent call using its exact agent_type once. "
        "task_name is only a task label, not the role "
        "selector. If agent_type is unavailable in the spawn_agent schema, "
        "reply solely LIVE_CAPTURE_FAILED:ROLE_SELECTOR_UNAVAILABLE without "
        "spawning or waiting. Do not spawn the next role until the current "
        "child has returned its exact marker.\n"
        + role_protocol
        + "\nA spawn succeeds only when it returns a nonempty child/thread identifier. "
        "If spawn_agent is unavailable, any spawn fails, or any spawn returns no "
        "nonempty child/thread identifier, do not call wait for that child and "
        "reply solely "
        "LIVE_CAPTURE_FAILED:SPAWN_UNAVAILABLE_OR_FAILED. Never call wait with an "
        "empty child set or without a successfully returned nonempty child/thread "
        "identifier for the current role.\n"
        "After each successful spawn, wait for that child before attempting the "
        "next role. Accept a role as returned only when that role's spawned child "
        "authors the exact marker LIVE_ROLE:<exact role>; marker text copied from "
        "this prompt, a parent message, or any other source is not a child response. "
        "If any exact child-authored role marker is missing or invalid after waiting, "
        "reply solely LIVE_CAPTURE_FAILED:CHILD_RESPONSE_MISSING_OR_INVALID.\n"
        "Reply solely LIVE_CAPTURE_COMPLETE only after receiving the exact "
        "child-authored marker for every requested role. Do not invoke shell "
        "commands, edit files, create commits, push, or install anything. Child "
        "agents must not delegate. These model-emitted markers are diagnostic text "
        "only and never override structured adapter evidence."
    )


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _process_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return ""


def _paths(fixture: Path, results: Path) -> tuple[Path, Path]:
    fixture_root = fixture.resolve(strict=True)
    results_root = results.resolve(strict=True)
    if not fixture_root.is_dir() or not results_root.is_dir():
        raise ValueError("Fixture and results paths must be existing directories")
    if fixture_root.parent != results_root.parent:
        raise ValueError("Fixture and results must be sibling directories in one run root")
    return fixture_root, results_root


def _windows_cmd_prefix(command: str) -> tuple[str, ...]:
    shim = Path(command).resolve(strict=True)
    node = shim.parent / "node.exe"
    if not node.is_file():
        resolved_node = shutil.which("node.exe") or shutil.which("node")
        if resolved_node is None:
            raise FileNotFoundError("Node executable for Codex shim was not found")
        node = Path(resolved_node).resolve(strict=True)
    entrypoint = shim.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if not entrypoint.is_file():
        raise FileNotFoundError("Codex JavaScript entrypoint for shim was not found")
    return str(node), str(entrypoint.resolve(strict=True))


def _resolve_codex_command(codex_command: str) -> tuple[str, ...]:
    candidates = [codex_command]
    if os.name == "nt" and Path(codex_command).suffix == "":
        candidates = [f"{codex_command}.exe", f"{codex_command}.cmd", codex_command]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved is not None:
            suffix = Path(resolved).suffix.lower()
            if suffix == ".ps1":
                raise OSError(
                    "PowerShell Codex shims are not directly executable; "
                    "pass a codex.cmd or codex.exe path"
                )
            if os.name == "nt" and suffix == ".cmd":
                return _windows_cmd_prefix(resolved)
            return (resolved,)
    raise FileNotFoundError(f"Codex command was not found: {codex_command}")


def _version(codex_command: tuple[str, ...], fixture: Path, timeout: int) -> tuple[int, str]:
    result = subprocess.run(
        [*codex_command, "--version"],
        cwd=fixture,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return result.returncode, sanitize_text(result.stdout.strip(), 100)


def _persist_capture(
    results: Path,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
    roles: tuple[str, ...],
    ephemeral: bool,
    isolate_user_config: bool,
    trust_fixture: bool,
) -> tuple[dict[str, object], object]:
    capture_name = "windows-0.155.1-" + "-".join(roles) + "-capture"
    manifest = capture_fixture_manifest(capture_name, "0.155.1", stdout)
    manifest.update(
        {
            "reviewed": False,
            "command": (
                "codex exec "
                + ("--ephemeral " if ephemeral else "")
                + ("--ignore-user-config --strict-config --enable multi_agent " if isolate_user_config else "")
                + ("-c projects=<fixture-trust> " if trust_fixture else "")
                + "--json --sandbox read-only -C <fixture> <role-prompt>"
            ),
            "exitCode": exit_code,
            "timedOut": timed_out,
            "requestedRoles": list(roles),
            "userConfigIgnored": isolate_user_config,
            "fixtureTrustOverride": trust_fixture,
            "stderr": sanitize_text(stderr, 500),
        }
    )
    sanitized_stream = sanitize_event_stream(stdout)
    stream = parse_event_stream(sanitized_stream, expected_roles=roles)
    _write_text(results / "real-capture.sanitized.jsonl", sanitized_stream)
    _write_json(results / "real-capture.manifest.json", manifest)
    return manifest, stream


def capture(
    fixture: Path,
    results: Path,
    timeout: int,
    codex_command: str,
    roles: tuple[str, ...],
    ephemeral: bool,
    sqlite_home: Path | None,
    *,
    isolate_user_config: bool = False,
    trust_fixture: bool = False,
) -> tuple[int, dict[str, object]]:
    if trust_fixture and not isolate_user_config:
        raise ValueError("Fixture trust override requires isolated user configuration")
    fixture_root, results_root = _paths(fixture, results)
    resolved_codex_command = _resolve_codex_command(codex_command)
    version_exit, version_text = _version(
        resolved_codex_command, fixture_root, timeout
    )
    if version_exit != 0 or version_text != PINNED_CODEX_VERSION:
        return 2, {
            "status": "BLOCKED",
            "reason": "PINNED_VERSION_UNAVAILABLE",
            "version": version_text,
            "exitCode": version_exit,
        }
    try:
        command = [*resolved_codex_command, "exec"]
        if ephemeral:
            command.append("--ephemeral")
        if isolate_user_config:
            command.extend(
                ["--ignore-user-config", "--strict-config", "--enable", "multi_agent"]
            )
        if trust_fixture:
            trust_value = f'projects={{{json.dumps(str(fixture_root))}={{trust_level="trusted"}}}}'
            command.extend(["-c", trust_value])
        command.extend(
            [
                "--json",
                "--sandbox",
                "read-only",
                "-C",
                str(fixture_root),
                _prompt(roles),
            ]
        )
        environment = os.environ.copy()
        if sqlite_home is not None:
            sqlite_root = sqlite_home.resolve(strict=True)
            if not sqlite_root.is_dir():
                raise ValueError("SQLite home must be an existing directory")
            environment["CODEX_SQLITE_HOME"] = str(sqlite_root)
        result = subprocess.run(
            command,
            cwd=fixture_root,
            env=environment,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        manifest, stream = _persist_capture(
            results_root,
            _process_text(error.stdout),
            _process_text(error.stderr),
            None,
            True,
            roles,
            ephemeral,
            isolate_user_config,
            trust_fixture,
        )
        return 3, {
            "status": "BLOCKED",
            "reason": "CAPTURE_TIMEOUT",
            "captureHash": manifest["captureHash"],
            "eventCount": stream.event_count,
            "parentSessionId": stream.parent_session_id,
            "attributedRoles": sorted(stream.attributed_children),
            "terminalSeen": stream.terminal_seen,
            "reasonCodes": list(stream.reason_codes),
        }

    manifest, stream = _persist_capture(
        results_root,
        result.stdout,
        result.stderr,
        result.returncode,
        False,
        roles,
        ephemeral,
        isolate_user_config,
        trust_fixture,
    )
    return result.returncode, {
        "status": "CAPTURED",
        "exitCode": result.returncode,
        "captureHash": manifest["captureHash"],
        "eventCount": stream.event_count,
        "parentSessionId": stream.parent_session_id,
        "attributedRoles": sorted(stream.attributed_children),
        "terminalSeen": stream.terminal_seen,
        "reasonCodes": list(stream.reason_codes),
        "stderr": manifest["stderr"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--codex-command", default="codex")
    parser.add_argument("--role", action="append", choices=ROLE_NAMES)
    parser.add_argument("--persistent", action="store_true")
    parser.add_argument("--sqlite-home", type=Path)
    parser.add_argument("--isolate-user-config", action="store_true")
    parser.add_argument("--trust-fixture", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.timeout < 1 or arguments.timeout > 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    try:
        exit_code, output = capture(
            arguments.fixture,
            arguments.results,
            arguments.timeout,
            arguments.codex_command,
            tuple(arguments.role or ROLE_NAMES),
            not arguments.persistent,
            arguments.sqlite_home,
            isolate_user_config=arguments.isolate_user_config,
            trust_fixture=arguments.trust_fixture,
        )
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "BLOCKED", "reason": sanitize_text(str(error), 200)}))
        return 4
    print(json.dumps(output, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
