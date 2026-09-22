#!/usr/bin/env python3
"""Validate versioned Codex project-agent TOML definitions."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python older than 3.11
    tomllib = None  # type: ignore[assignment]


REGISTRY_PATH = Path("compatibility/codex-agents.json")
AGENTS_DIRECTORY = Path("agents")
AGENT_FILE_ROLES = {
    "code-explorer": "code_explorer",
    "quick-implementer": "quick_implementer",
    "implementer": "implementer",
    "luna-escalation": "luna_escalation",
    "sol-architect": "sol_architect",
    "sol-architect-deep": "sol_architect_deep",
    "code-validator": "code_validator",
    "code-reviewer": "code_reviewer",
    "commit-pusher": "commit_pusher",
}
EXPECTED_ROLES = {
    "code_explorer": "read-only",
    "quick_implementer": "workspace-write",
    "implementer": "workspace-write",
    "luna_escalation": "workspace-write",
    "sol_architect": "read-only",
    "sol_architect_deep": "read-only",
    "code_validator": "read-only",
    "code_reviewer": "read-only",
    "commit_pusher": "workspace-write",
}
RUNTIME_ROLE_PATTERN = re.compile(r"[a-z0-9_]+")
CONTRACT_PHRASES = (
    "Do not delegate",
    "Do not widen scope",
    "Do not claim an unobserved check passed",
)


@dataclass(frozen=True)
class ValidationReport:
    codex_version: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    agents: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_json(self) -> dict[str, Any]:
        return {
            "agents": list(self.agents),
            "codexVersion": self.codex_version,
            "errors": list(self.errors),
            "status": "PASS" if self.passed else "FAIL",
            "warnings": list(self.warnings),
        }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"registry is not valid JSON: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"registry root must be an object: {path}")
    return value


def supported_versions(source_root: Path) -> set[str]:
    registry = _load_json(source_root / REGISTRY_PATH)
    versions = registry.get("versions")
    if not isinstance(versions, dict) or not versions:
        raise ValueError("registry must define a non-empty versions object")
    return set(versions)


def configured_models(source_root: Path) -> dict[str, dict[str, str]]:
    """Read the source agent TOMLs and return role -> {"model", "effort"}.

    Absent or malformed values become empty strings so routing can fail closed
    rather than silently inventing a configured value.
    """
    result: dict[str, dict[str, str]] = {}
    if tomllib is None:
        return result
    directory = source_root / AGENTS_DIRECTORY
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as stream:
                value = tomllib.load(stream)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        if not isinstance(name, str):
            continue
        model = value.get("model")
        effort = value.get("model_reasoning_effort")
        result[name] = {
            "model": model if isinstance(model, str) else "",
            "effort": effort if isinstance(effort, str) else "",
        }
    return result


def validate_catalog(source_root: Path, codex_version: str) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    parsed_names: list[str] = []

    if tomllib is None:
        return ValidationReport(
            codex_version,
            ("custom agent validation requires Python 3.11 or newer with tomllib support",),
            (),
            (),
        )

    try:
        registry = _load_json(source_root / REGISTRY_PATH)
    except ValueError as error:
        return ValidationReport(codex_version, (str(error),), (), ())

    if registry.get("schemaVersion") != 1:
        errors.append("compatibility registry schemaVersion must be 1")
    versions = registry.get("versions")
    if not isinstance(versions, dict):
        errors.append("compatibility registry versions must be an object")
        return ValidationReport(codex_version, tuple(errors), (), ())
    version_config = versions.get(codex_version)
    if not isinstance(version_config, dict):
        errors.append(f"unsupported Codex CLI version: {codex_version}")
        return ValidationReport(codex_version, tuple(errors), (), ())

    required_keys = version_config.get("requiredKeys")
    allowed_keys = version_config.get("allowedKeys")
    sandbox_modes = version_config.get("sandboxModes")
    models = version_config.get("models")
    if not isinstance(required_keys, list) or not all(isinstance(key, str) for key in required_keys):
        errors.append("requiredKeys must be a string array")
        required_keys = []
    if not isinstance(allowed_keys, list) or not all(isinstance(key, str) for key in allowed_keys):
        errors.append("allowedKeys must be a string array")
        allowed_keys = []
    if not isinstance(sandbox_modes, list) or not all(isinstance(mode, str) for mode in sandbox_modes):
        errors.append("sandboxModes must be a string array")
        sandbox_modes = []
    if not isinstance(models, dict):
        errors.append("models must be an object")
        models = {}
    if version_config.get("runtimeValidated") is not True:
        warnings.append(f"runtime behavior is not yet validated for Codex CLI {codex_version}")

    agents_directory = source_root / AGENTS_DIRECTORY
    files = sorted(agents_directory.glob("*.toml")) if agents_directory.is_dir() else []
    stems = {path.stem for path in files}
    missing = sorted(set(AGENT_FILE_ROLES) - stems)
    extra = sorted(stems - set(AGENT_FILE_ROLES))
    if missing:
        errors.append(f"missing agent files: {', '.join(missing)}")
    if extra:
        errors.append(f"unexpected agent files: {', '.join(extra)}")

    seen_names: set[str] = set()
    for path in files:
        try:
            value = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            errors.append(f"{path.name}: invalid TOML: {error}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{path.name}: TOML root must be a table")
            continue
        unknown_keys = sorted(set(value) - set(allowed_keys))
        if unknown_keys:
            errors.append(f"{path.name}: unsupported keys: {', '.join(unknown_keys)}")
        for key in required_keys:
            if not isinstance(value.get(key), str) or not value[key].strip():
                errors.append(f"{path.name}: {key} must be a non-empty string")
        name = value.get("name")
        if not isinstance(name, str):
            continue
        parsed_names.append(name)
        expected_name = AGENT_FILE_ROLES.get(path.stem)
        if expected_name is not None and name != expected_name:
            errors.append(f"{path.name}: name must be {expected_name!r}")
        if RUNTIME_ROLE_PATTERN.fullmatch(name) is None:
            errors.append(
                f"{path.name}: name must use only lowercase letters, digits, and underscores"
            )
        if name in seen_names:
            errors.append(f"{path.name}: duplicate agent name: {name}")
        seen_names.add(name)

        model = value.get("model")
        effort = value.get("model_reasoning_effort")
        allowed_efforts = models.get(model)
        if not isinstance(model, str) or not isinstance(allowed_efforts, list):
            errors.append(f"{path.name}: unsupported model: {model!r}")
        elif effort not in allowed_efforts:
            errors.append(f"{path.name}: unsupported reasoning effort {effort!r} for {model}")

        sandbox_mode = value.get("sandbox_mode")
        if sandbox_mode not in sandbox_modes:
            errors.append(f"{path.name}: unsupported sandbox_mode: {sandbox_mode!r}")
        expected_sandbox = EXPECTED_ROLES.get(name)
        if expected_sandbox is not None and sandbox_mode != expected_sandbox:
            errors.append(
                f"{path.name}: sandbox_mode must be {expected_sandbox!r} for role {name}"
            )

        instructions = value.get("developer_instructions")
        if isinstance(instructions, str):
            for phrase in CONTRACT_PHRASES:
                if phrase not in instructions:
                    errors.append(f"{path.name}: developer_instructions must include {phrase!r}")

    return ValidationReport(
        codex_version,
        tuple(errors),
        tuple(warnings),
        tuple(sorted(parsed_names)),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Workflow package root",
    )
    parser.add_argument("--codex-version", required=True, help="Codex CLI version to validate")
    parser.add_argument("--json", action="store_true", help="Print a JSON report")
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = validate_catalog(args.source_root.resolve(), args.codex_version)
    if args.json:
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
    else:
        for warning in report.warnings:
            print(f"WARN: {warning}")
        for error in report.errors:
            print(f"FAIL: {error}")
        if report.passed:
            print(
                f"PASS: validated {len(report.agents)} custom agent definition(s) "
                f"for Codex CLI {report.codex_version}"
            )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
