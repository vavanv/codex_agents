#!/usr/bin/env python3
"""Transactional project installer for the Codex Multi-Agent Workflow contracts."""

from __future__ import annotations

import argparse
import hashlib
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

SCHEMA = "hybrid-codex-installer/v1"
PACKAGE_VERSION = "0.2.0-agents-preview"
SUPPORTED_CODEX_VERSIONS = {"0.154.0"}
INSTALL_DIRECTORY = ".codex-workflow"
STATE_FILENAME = ".hybrid-codex-workflow-state.json"
JOURNAL_FILENAME = ".hybrid-codex-workflow-transaction.json"
BACKUP_DIRECTORY = ".hybrid-codex-workflow-backups"
START_MARKER = "<!-- hybrid-codex-workflow:start -->"
END_MARKER = "<!-- hybrid-codex-workflow:end -->"
MANAGED_BLOCK = """<!-- hybrid-codex-workflow:start -->
## Codex multi-agent workflow

- Read `.codex-workflow/CODEX_WORKFLOW.md` and the applicable files under `.codex-workflow/rules/` before routing or implementing work.
- Use `docs/ai/PROJECT_CONTEXT.md` for project-specific architecture, commands, and boundaries.
- If `.codex/agents/*.toml` is present, use the named role definitions for delegation; treat their sandbox values as defaults subject to parent-session controls.
- For non-trivial work, the root orchestrator exclusively owns `plans/ACTIVE_PLAN.md` and `plans/HANDOFF.md`.
- Preserve unrelated changes, require observed validation evidence, and never infer commit or push authorization.
<!-- hybrid-codex-workflow:end -->"""

PACKAGE_FILES = {
    "CODEX_WORKFLOW.md": f"{INSTALL_DIRECTORY}/CODEX_WORKFLOW.md",
    "rules/ROUTING.md": f"{INSTALL_DIRECTORY}/rules/ROUTING.md",
    "rules/VALIDATION.md": f"{INSTALL_DIRECTORY}/rules/VALIDATION.md",
    "rules/ESCALATION.md": f"{INSTALL_DIRECTORY}/rules/ESCALATION.md",
    "rules/GIT_SAFETY.md": f"{INSTALL_DIRECTORY}/rules/GIT_SAFETY.md",
    "templates/PROJECT_CONTEXT.md": f"{INSTALL_DIRECTORY}/templates/PROJECT_CONTEXT.md",
    "templates/ACTIVE_PLAN.md": f"{INSTALL_DIRECTORY}/templates/ACTIVE_PLAN.md",
    "templates/HANDOFF.md": f"{INSTALL_DIRECTORY}/templates/HANDOFF.md",
    "templates/TASK_TEMPLATE.md": f"{INSTALL_DIRECTORY}/templates/TASK_TEMPLATE.md",
}

PROJECT_TEMPLATE_FILES = {
    "templates/PROJECT_CONTEXT.md": "docs/ai/PROJECT_CONTEXT.md",
    "templates/TASK_TEMPLATE.md": "plans/TASK_TEMPLATE.md",
}

CUSTOM_AGENT_FILES = {
    f"agents/{name}.toml": f".codex/agents/{name}.toml"
    for name in (
        "code-explorer",
        "quick-implementer",
        "implementer",
        "luna-escalation",
        "sol-architect",
        "sol-architect-deep",
        "code-validator",
        "code-reviewer",
        "commit-pusher",
    )
}
COMPATIBILITY_FILE = "compatibility/codex-agents.json"


class WorkflowError(RuntimeError):
    pass


@dataclass
class Action:
    kind: str
    path: Path
    content: bytes | None = None
    backup: Path | None = None
    created: bool = False
    applied: bool = False

    def to_json(self, target: Path) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": _relative(target, self.path),
            "backup": _relative(target, self.backup) if self.backup else None,
            "created": self.created,
            "applied": self.applied,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_hash(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return _sha256_bytes(normalized.encode("utf-8"))


def _relative(target: Path, path: Path | None) -> str:
    if path is None:
        raise WorkflowError("Cannot make a null path relative")
    return path.relative_to(target).as_posix()


def _target_path(target: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
        raise WorkflowError(f"Managed path is not a safe repository-relative path: {relative}")
    candidate = target.joinpath(*relative_path.parts)
    try:
        candidate.relative_to(target)
    except ValueError as error:
        raise WorkflowError(f"Managed path escapes target repository: {relative}") from error
    return candidate


def _assert_safe_target(raw_target: str) -> Path:
    supplied = Path(raw_target).expanduser().absolute()
    if supplied.is_symlink():
        raise WorkflowError(f"Target repository cannot be a symlink: {supplied}")
    target = supplied.resolve()
    if not target.is_dir():
        raise WorkflowError(f"Target repository does not exist or is not a directory: {target}")
    if target == Path(target.anchor):
        raise WorkflowError(f"Refusing to use a filesystem root as the target: {target}")
    return target


def _assert_no_symlink(target: Path, path: Path) -> None:
    relative = path.relative_to(target)
    current = target
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise WorkflowError(f"Refusing to traverse managed symlink: {current}")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as file_handle:
            file_handle.write(content)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, content)


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise WorkflowError(f"State file cannot be a symlink: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"State manifest is not valid JSON: {path}") from error
    if value.get("schema") != SCHEMA or not isinstance(value.get("files"), dict):
        raise WorkflowError(f"Unsupported or malformed state manifest: {path}")
    return value


def _detect_codex_version() -> str:
    if os.name == "nt":
        command_path = shutil.which("codex.cmd") or shutil.which("codex.exe")
        if command_path:
            command = [command_path, "--version"]
        else:
            powershell_path = shutil.which("codex.ps1")
            if not powershell_path:
                raise WorkflowError("Unable to find Codex CLI on PATH; install Codex CLI first")
            shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
            if not shell:
                raise WorkflowError("Codex uses a PowerShell shim, but PowerShell was not found")
            command = [shell, "-NoProfile", "-File", powershell_path, "--version"]
    else:
        command_path = shutil.which("codex")
        if not command_path:
            raise WorkflowError("Unable to find Codex CLI on PATH; install Codex CLI first")
        command = [command_path, "--version"]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkflowError("Unable to run 'codex --version'; install Codex CLI first") from error
    output = f"{result.stdout}\n{result.stderr}".strip()
    match = re.search(r"\bcodex-cli\s+([^\s]+)", output)
    if result.returncode != 0 or not match:
        raise WorkflowError(f"Unable to parse Codex CLI version from: {output!r}")
    return match.group(1)


def _validate_codex_version() -> str:
    version = _detect_codex_version()
    if version not in SUPPORTED_CODEX_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_CODEX_VERSIONS))
        raise WorkflowError(
            f"Unsupported Codex CLI {version}; supported version(s): {supported}. No files were changed."
        )
    return version


def _validate_sources(source_root: Path, with_custom_agents: bool = False) -> None:
    required = list(PACKAGE_FILES) + list(PROJECT_TEMPLATE_FILES)
    if with_custom_agents:
        required.extend(CUSTOM_AGENT_FILES)
        required.append(COMPATIBILITY_FILE)
    for relative in required:
        source = source_root / relative
        if not source.is_file() or source.is_symlink():
            raise WorkflowError(f"Required package source is missing or unsafe: {source}")


def _validate_custom_agent_sources(source_root: Path, codex_version: str) -> list[str]:
    try:
        from validate_agent_configs import validate_catalog
    except ModuleNotFoundError as error:
        raise WorkflowError(
            "Custom agent installation requires Python 3.11 or newer with tomllib support"
        ) from error
    report = validate_catalog(source_root, codex_version)
    if report.errors:
        raise WorkflowError("Custom agent validation failed:\n" + "\n".join(report.errors))
    return list(report.warnings)


def _extract_managed_block(content: str) -> tuple[int, int, str] | None:
    starts = [match.start() for match in re.finditer(re.escape(START_MARKER), content)]
    ends = [match.end() for match in re.finditer(re.escape(END_MARKER), content)]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise WorkflowError("AGENTS.md contains duplicate or malformed workflow markers")
    return starts[0], ends[0], content[starts[0] : ends[0]]


def _install_agents_content(
    existing: bytes | None, old_agents: dict[str, Any] | None
) -> tuple[bytes | None, dict[str, Any] | None, str | None]:
    text = existing.decode("utf-8") if existing is not None else ""
    extracted = _extract_managed_block(text)
    block_hash = _text_hash(MANAGED_BLOCK)

    if extracted is not None:
        if old_agents is None:
            raise WorkflowError(
                "AGENTS.md already contains an unmanaged workflow block; remove it or restore the state manifest"
            )
        start, end, current_block = extracted
        if _text_hash(current_block) != old_agents.get("blockHash"):
            return None, old_agents, "PRESERVED MODIFIED AGENTS.md BLOCK"
        updated = text[:start] + MANAGED_BLOCK + text[end:]
        record = dict(old_agents)
        record["blockHash"] = block_hash
        record["installedFileHash"] = _sha256_bytes(updated.encode("utf-8"))
        return updated.encode("utf-8"), record, None

    separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
    updated = f"{text}{separator}{MANAGED_BLOCK}\n"
    record = {
        "path": "AGENTS.md",
        "blockHash": block_hash,
        "createdFile": existing is None,
        "preInstallHash": _sha256_bytes(existing) if existing is not None else None,
        "installedFileHash": _sha256_bytes(updated.encode("utf-8")),
        "separator": separator,
    }
    return updated.encode("utf-8"), record, None


def _prepare_actions(
    target: Path, transaction_id: str, requests: list[tuple[str, Path, bytes | None]]
) -> list[Action]:
    actions: list[Action] = []
    transaction_backup = target / BACKUP_DIRECTORY / transaction_id
    for kind, path, content in requests:
        _assert_no_symlink(target, path)
        created = not path.exists()
        backup = None
        if not created:
            backup = transaction_backup / _relative(target, path)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, backup)
        actions.append(Action(kind=kind, path=path, content=content, backup=backup, created=created))
    return actions


def _journal_value(target: Path, transaction_id: str, operation: str, actions: list[Action]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "transactionId": transaction_id,
        "operation": operation,
        "target": str(target),
        "createdAt": _now(),
        "actions": [action.to_json(target) for action in actions],
    }


def _apply_actions(
    target: Path, journal_path: Path, transaction_id: str, operation: str, actions: list[Action]
) -> None:
    _write_json(journal_path, _journal_value(target, transaction_id, operation, actions))
    try:
        for action in actions:
            action.applied = True
            _write_json(journal_path, _journal_value(target, transaction_id, operation, actions))
            if action.kind == "write":
                if action.content is None:
                    raise WorkflowError(f"Write action has no content: {action.path}")
                _atomic_write(action.path, action.content)
            elif action.kind == "delete":
                action.path.unlink(missing_ok=True)
            else:
                raise WorkflowError(f"Unknown transaction action: {action.kind}")
    except Exception:
        _rollback_actions(actions)
        journal_path.unlink(missing_ok=True)
        raise
    journal_path.unlink(missing_ok=True)


def _rollback_actions(actions: list[Action]) -> None:
    failures: list[str] = []
    for action in reversed(actions):
        if not action.applied:
            continue
        try:
            if action.backup and action.backup.exists():
                _atomic_write(action.path, action.backup.read_bytes())
            elif action.created:
                action.path.unlink(missing_ok=True)
        except OSError as error:
            failures.append(f"{action.path}: {error}")
    if failures:
        raise WorkflowError("Rollback incomplete:\n" + "\n".join(failures))


def recover(target: Path, journal_path: Path, dry_run: bool) -> None:
    if not journal_path.exists():
        print("No interrupted transaction found.")
        return
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Cannot recover malformed transaction journal: {journal_path}") from error
    if journal.get("schema") != SCHEMA or journal.get("target") != str(target):
        raise WorkflowError("Transaction journal does not match this installer or target")
    actions: list[Action] = []
    for item in journal.get("actions", []):
        path = _target_path(target, item["path"])
        backup = _target_path(target, item["backup"]) if item.get("backup") else None
        actions.append(
            Action(
                kind=item["kind"],
                path=path,
                backup=backup,
                created=bool(item.get("created")),
                applied=bool(item.get("applied")),
            )
        )
    if dry_run:
        print(f"DRY-RUN: would roll back {sum(action.applied for action in actions)} applied action(s)")
        return
    _rollback_actions(actions)
    journal_path.unlink(missing_ok=True)
    print("RECOVERED: interrupted transaction rolled back")


def install(
    target: Path,
    source_root: Path,
    dry_run: bool,
    with_custom_agents: bool = False,
) -> None:
    _validate_sources(source_root, with_custom_agents)
    codex_version = _validate_codex_version()
    validation_warnings = (
        _validate_custom_agent_sources(source_root, codex_version)
        if with_custom_agents
        else []
    )
    state_path = target / STATE_FILENAME
    journal_path = target / JOURNAL_FILENAME
    if journal_path.exists():
        raise WorkflowError(f"Interrupted transaction found. Run the installer with --recover: {journal_path}")
    old_state = _read_state(state_path)
    if old_state and old_state.get("target") != str(target):
        raise WorkflowError("State manifest target does not match the requested repository")
    if not old_state and (target / INSTALL_DIRECTORY).exists():
        raise WorkflowError(
            f"{INSTALL_DIRECTORY} exists without a state manifest; move it aside or recover it manually"
        )

    transaction_id = uuid.uuid4().hex
    new_files: dict[str, Any] = dict(old_state.get("files", {})) if old_state else {}
    requests: list[tuple[str, Path, bytes | None]] = []
    messages: list[str] = []
    all_files = list(PACKAGE_FILES.items()) + list(PROJECT_TEMPLATE_FILES.items())
    if with_custom_agents:
        all_files.extend(CUSTOM_AGENT_FILES.items())
    directory_candidates = [
        INSTALL_DIRECTORY,
        f"{INSTALL_DIRECTORY}/rules",
        f"{INSTALL_DIRECTORY}/templates",
        "docs",
        "docs/ai",
        "plans",
    ]
    if with_custom_agents:
        directory_candidates.extend([".codex", ".codex/agents"])
    created_directories = list(old_state.get("createdDirectories", [])) if old_state else []
    for relative in directory_candidates:
        if relative not in created_directories and not _target_path(target, relative).exists():
            created_directories.append(relative)

    for source_relative, installed_relative in all_files:
        source_path = source_root / source_relative
        installed_path = _target_path(target, installed_relative)
        _assert_no_symlink(target, installed_path)
        source_content = source_path.read_bytes()
        source_hash = _sha256_bytes(source_content)
        old_record = new_files.get(installed_relative)
        is_custom_agent = installed_relative.startswith(".codex/agents/")
        current_hash = None
        record_backup = old_record.get("backup") if old_record else None
        pre_install_hash = old_record.get("preInstallHash") if old_record else None

        if installed_path.exists():
            current_hash = _sha256_file(installed_path)
            if old_record is None:
                if installed_relative.startswith(f"{INSTALL_DIRECTORY}/"):
                    raise WorkflowError(f"Unmanaged package path already exists: {installed_path}")
                if is_custom_agent:
                    if current_hash != source_hash:
                        raise WorkflowError(
                            f"Unmanaged custom agent conflicts with package agent: {installed_path}"
                        )
                    new_files[installed_relative] = {
                        "source": source_relative,
                        "installedHash": source_hash,
                        "mutable": False,
                        "owner": "preexisting-identical",
                        "preInstallHash": current_hash,
                        "backup": None,
                    }
                    messages.append(f"PRESERVED IDENTICAL CUSTOM AGENT: {installed_relative}")
                    continue
                messages.append(f"PRESERVED EXISTING PROJECT FILE: {installed_relative}")
                continue
            if old_record.get("owner") == "preexisting-identical":
                if current_hash == source_hash:
                    messages.append(f"UNCHANGED PRE-EXISTING CUSTOM AGENT: {installed_relative}")
                else:
                    messages.append(f"PRESERVED PRE-EXISTING CUSTOM AGENT: {installed_relative}")
                continue
            if current_hash != old_record.get("installedHash"):
                messages.append(f"PRESERVED MODIFIED FILE: {installed_relative}")
                continue
            if current_hash != source_hash:
                requests.append(("write", installed_path, source_content))
                record_backup = (
                    Path(BACKUP_DIRECTORY)
                    / transaction_id
                    / Path(installed_relative)
                ).as_posix()
                messages.append(f"UPDATE: {installed_relative}")
            else:
                messages.append(f"UNCHANGED: {installed_relative}")
        else:
            if old_record and old_record.get("owner") == "preexisting-identical":
                new_files.pop(installed_relative, None)
                messages.append(f"PRESERVED ABSENT PRE-EXISTING CUSTOM AGENT: {installed_relative}")
                continue
            requests.append(("write", installed_path, source_content))
            messages.append(f"INSTALL: {installed_relative}")

        new_files[installed_relative] = {
            "source": source_relative,
            "installedHash": source_hash,
            "mutable": installed_relative in PROJECT_TEMPLATE_FILES.values(),
            "owner": (
                "package"
                if installed_relative.startswith(f"{INSTALL_DIRECTORY}/")
                else (
                    "custom-agent"
                    if is_custom_agent
                    else "project-template-seed"
                )
            ),
            "preInstallHash": pre_install_hash if old_record is not None else current_hash,
            "backup": record_backup,
        }

    agents_path = target / "AGENTS.md"
    _assert_no_symlink(target, agents_path)
    existing_agents = agents_path.read_bytes() if agents_path.exists() else None
    old_agents = old_state.get("agents") if old_state else None
    agents_content, agents_record, agents_message = _install_agents_content(existing_agents, old_agents)
    if agents_message:
        messages.append(agents_message)
    if agents_content is not None and agents_content != existing_agents:
        requests.append(("write", agents_path, agents_content))
        if existing_agents is not None and agents_record is not None:
            agents_record["backup"] = (
                Path(BACKUP_DIRECTORY) / transaction_id / "AGENTS.md"
            ).as_posix()
        messages.append("INSTALL/UPDATE: AGENTS.md managed block")
    elif agents_content is not None:
        messages.append("UNCHANGED: AGENTS.md managed block")

    backups = list(old_state.get("backups", [])) if old_state else []
    for _, requested_path, _ in requests:
        if requested_path.exists():
            backup_relative = (
                Path(BACKUP_DIRECTORY) / transaction_id / Path(_relative(target, requested_path))
            ).as_posix()
            backups.append(backup_relative)
    semantic_state = {
        "schema": SCHEMA,
        "packageVersion": PACKAGE_VERSION,
        "target": str(target),
        "codexVersion": codex_version,
        "files": new_files,
        "agents": agents_record,
        "backups": sorted(set(backups)),
        "createdDirectories": created_directories,
        "features": {
            "customAgents": any(
                relative.startswith(".codex/agents/") for relative in new_files
            )
        },
    }
    old_semantic_state = None
    if old_state:
        old_semantic_state = {
            key: old_state.get(key)
            for key in (
                "schema",
                "packageVersion",
                "target",
                "codexVersion",
                "files",
                "agents",
                "backups",
                "createdDirectories",
                "features",
            )
        }
    if semantic_state != old_semantic_state:
        new_state = {
            **semantic_state,
            "installedAt": old_state.get("installedAt", _now()) if old_state else _now(),
            "updatedAt": _now(),
            "transactionId": transaction_id,
        }
        state_content = (json.dumps(new_state, indent=2, sort_keys=True) + "\n").encode("utf-8")
        requests.append(("write", state_path, state_content))

    for warning in validation_warnings:
        print(f"WARN: {warning}")
    for message in messages:
        print(f"DRY-RUN: {message}" if dry_run else message)
    if dry_run:
        print(f"DRY-RUN PASS: {len(requests)} write action(s); no files changed")
        return

    actions = _prepare_actions(target, transaction_id, requests)
    _apply_actions(target, journal_path, transaction_id, "install", actions)
    print(f"PASS: Codex Multi-Agent Workflow installed in {target}")


def _remove_agents_content(existing: bytes, agents_record: dict[str, Any]) -> tuple[bytes | None, str | None]:
    text = existing.decode("utf-8")
    extracted = _extract_managed_block(text)
    if extracted is None:
        return existing, "AGENTS.md managed block is already absent"
    start, end, block = extracted
    if _text_hash(block) != agents_record.get("blockHash"):
        return None, "PRESERVED MODIFIED AGENTS.md BLOCK"

    removal_start = start
    separator = agents_record.get("separator", "")
    if separator and text[:start].endswith(separator):
        removal_start -= len(separator)
    removal_end = end + 1 if text[end : end + 1] == "\n" else end
    updated = text[:removal_start] + text[removal_end:]
    if agents_record.get("createdFile") and not updated.strip():
        return b"", None
    return updated.encode("utf-8"), None


def _remove_empty_managed_directories(target: Path, created_directories: list[str]) -> None:
    candidates = {
        target / INSTALL_DIRECTORY / "rules",
        target / INSTALL_DIRECTORY / "templates",
        target / INSTALL_DIRECTORY,
        target / "docs" / "ai",
        target / "plans",
    }
    candidates.update(_target_path(target, relative) for relative in created_directories)
    for candidate in sorted(candidates, key=lambda value: len(value.parts), reverse=True):
        if candidate.is_dir() and not candidate.is_symlink():
            try:
                candidate.rmdir()
            except OSError:
                pass


def _cleanup_backup_files(target: Path, backup_paths: list[Path]) -> None:
    backup_root = target / BACKUP_DIRECTORY
    parents: set[Path] = set()
    for backup_path in backup_paths:
        try:
            backup_path.relative_to(backup_root)
        except ValueError as error:
            raise WorkflowError(f"Backup path escapes the managed backup directory: {backup_path}") from error
        _assert_no_symlink(target, backup_path)
        backup_path.unlink(missing_ok=True)
        current = backup_path.parent
        while current != target and current != backup_root.parent:
            parents.add(current)
            if current == backup_root:
                break
            current = current.parent
    for parent in sorted(parents, key=lambda value: len(value.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass


def uninstall(target: Path, dry_run: bool) -> None:
    state_path = target / STATE_FILENAME
    journal_path = target / JOURNAL_FILENAME
    if journal_path.exists():
        raise WorkflowError(f"Interrupted transaction found. Run the uninstaller with --recover: {journal_path}")
    state = _read_state(state_path)
    if state is None:
        print("WARN: no state manifest found; no files were removed")
        print(f"Manual review may be required for {target / INSTALL_DIRECTORY} and the AGENTS.md managed block")
        return
    if state.get("target") != str(target):
        raise WorkflowError("State manifest target does not match the requested repository")

    requests: list[tuple[str, Path, bytes | None]] = []
    remaining_files: dict[str, Any] = {}
    for installed_relative, record in state["files"].items():
        installed_path = _target_path(target, installed_relative)
        _assert_no_symlink(target, installed_path)
        if not installed_path.exists():
            print(f"ALREADY ABSENT: {installed_relative}")
            continue
        if record.get("owner") == "preexisting-identical":
            print(f"PRESERVED PRE-EXISTING FILE: {installed_relative}")
            continue
        if _sha256_file(installed_path) != record.get("installedHash"):
            print(f"PRESERVED MODIFIED FILE: {installed_relative}")
            remaining_files[installed_relative] = record
            continue
        requests.append(("delete", installed_path, None))
        print(f"DRY-RUN: REMOVE: {installed_relative}" if dry_run else f"REMOVE: {installed_relative}")

    agents_record = state.get("agents")
    remaining_agents = agents_record
    if agents_record:
        agents_path = _target_path(target, agents_record.get("path", "AGENTS.md"))
        _assert_no_symlink(target, agents_path)
        if not agents_path.exists():
            print("ALREADY ABSENT: AGENTS.md")
            remaining_agents = None
        else:
            updated_agents, warning = _remove_agents_content(agents_path.read_bytes(), agents_record)
            if warning:
                print(warning)
                if "already absent" in warning:
                    remaining_agents = None
            elif updated_agents == b"":
                requests.append(("delete", agents_path, None))
                remaining_agents = None
                print("DRY-RUN: REMOVE: generated AGENTS.md" if dry_run else "REMOVE: generated AGENTS.md")
            elif updated_agents is not None:
                requests.append(("write", agents_path, updated_agents))
                remaining_agents = None
                print("DRY-RUN: REMOVE: AGENTS.md managed block" if dry_run else "REMOVE: AGENTS.md managed block")

    transaction_id = uuid.uuid4().hex
    if remaining_files or remaining_agents:
        updated_state = dict(state)
        updated_state["files"] = remaining_files
        updated_state["agents"] = remaining_agents
        updated_state["updatedAt"] = _now()
        updated_state["transactionId"] = transaction_id
        state_content = (json.dumps(updated_state, indent=2, sort_keys=True) + "\n").encode("utf-8")
        requests.append(("write", state_path, state_content))
    else:
        requests.append(("delete", state_path, None))

    if dry_run:
        print(f"DRY-RUN PASS: {len(requests)} action(s); no files changed")
        return

    actions = _prepare_actions(target, transaction_id, requests)
    _apply_actions(target, journal_path, transaction_id, "uninstall", actions)
    cleanup_paths = [action.backup for action in actions if action.backup]
    if not remaining_files and not remaining_agents:
        cleanup_paths.extend(
            _target_path(target, backup_relative) for backup_relative in state.get("backups", [])
        )
    _cleanup_backup_files(target, cleanup_paths)
    _remove_empty_managed_directories(target, list(state.get("createdDirectories", [])))
    if remaining_files or remaining_agents:
        print("WARN: uninstall preserved modified managed content; state manifest retained")
    else:
        print(f"PASS: Codex Multi-Agent Workflow uninstalled from {target}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "uninstall"))
    parser.add_argument("--target", default=".", help="Target project directory (default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing files")
    parser.add_argument("--recover", action="store_true", help="Roll back an interrupted transaction")
    parser.add_argument(
        "--with-custom-agents",
        action="store_true",
        help="Install experimental project-level custom agent TOMLs",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        target = _assert_safe_target(args.target)
        journal_path = target / JOURNAL_FILENAME
        if args.recover:
            recover(target, journal_path, args.dry_run)
        elif args.operation == "install":
            source_root = Path(__file__).resolve().parent.parent
            install(target, source_root, args.dry_run, args.with_custom_agents)
        else:
            uninstall(target, args.dry_run)
        return 0
    except WorkflowError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"FAIL: filesystem operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
