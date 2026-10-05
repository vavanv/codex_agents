"""Immutable repository policy. The caller supplies the trusted source root.

Policy eligibility is not a claim of runtime validation or sandbox enforcement.
Importing this module performs no I/O and does not select a source or CLI.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Mapping

MAX_REGISTRY_BYTES = 64 * 1024
GATES = ("staticInstallation", "discoveryDiagnostic", "capturedEvidence")
ROLLOUT_SCHEMAS = ("v1", "v2")
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_EFFORTS = frozenset(("low", "medium", "high", "xhigh", "max", "ultra"))


class RegistryError(ValueError):
    """A fail-closed policy error with a stable machine-readable category."""

    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class VersionInfo:
    version: str
    agent_schema_version: int
    allowed_keys: tuple[str, ...]
    models: Mapping[str, tuple[str, ...]]
    required_keys: tuple[str, ...]
    runtime_validated: bool
    sandbox_modes: tuple[str, ...]
    gates: Mapping[str, bool]
    rollout_schemas: tuple[str, ...]


@dataclass(frozen=True)
class RunProfile:
    expected_version: str

    @property
    def cli_banner(self) -> str:
        return f"codex-cli {self.expected_version}"

@dataclass(frozen=True)
class CompatibilityRegistry:
    schema_version: int
    policy_schema_version: int
    documentation_checked: str
    preferred_install_target: str
    versions: Mapping[str, VersionInfo]
    run_profiles: Mapping[str, RunProfile]
    source_path: Path
    sha256: str
    source_root: Path


def _malformed(message: str) -> None:
    raise RegistryError("malformed_policy", message)


def _object(value: object, label: str) -> dict:
    if type(value) is not dict:
        _malformed(f"{label} must be an object")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list or not value or any(type(v) is not str or not v for v in value):
        _malformed(f"{label} must be a nonempty string array")
    if len(set(value)) != len(value):
        _malformed(f"{label} contains duplicates")
    return tuple(value)


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        _malformed(f"{label} must be a nonempty string")
    return value


def _numeric_version(value: object) -> str:
    value = _text(value, "version")
    if not _VERSION.fullmatch(value):
        _malformed("version must have numeric major.minor.patch syntax")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _malformed(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _registry_path(source_root: Path | str) -> tuple[Path, Path]:
    """Validate lexical ancestry before resolving; never read policy bytes."""
    try:
        root = Path(source_root)
    except (TypeError, ValueError) as exc:
        raise RegistryError("malformed_policy", "source root must be a path") from exc
    if not root.is_absolute():
        _malformed("source root must be an explicit absolute path")
    path = root / "compatibility" / "codex-agents.json"
    try:
        # Reject links/reparse points before reading, including source ancestors.
        for component in reversed((path, *path.parents)):
            metadata = component.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                _malformed("registry path must not traverse links or reparse points")
        if not stat.S_ISREG(path.lstat().st_mode):
            _malformed("registry must be a regular file")
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        if not resolved_path.is_relative_to(resolved_root):
            _malformed("registry escapes source root")
        return root, resolved_path
    except (OSError, ValueError) as exc:
        raise RegistryError("malformed_policy", "registry is missing or unreadable") from exc


def _snapshot(source_root: Path | str) -> tuple[Path, Path, bytes]:
    root, resolved_path = _registry_path(source_root)
    path = root / "compatibility" / "codex-agents.json"
    try:
        with path.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_REGISTRY_BYTES:
                _malformed("registry must be a bounded regular file")
            raw = stream.read(MAX_REGISTRY_BYTES + 1)
        if len(raw) > MAX_REGISTRY_BYTES:
            _malformed("registry exceeds size limit")
        return root, resolved_path, raw
    except (OSError, ValueError) as exc:
        raise RegistryError("malformed_policy", "registry is missing or unreadable") from exc


def load_registry(source_root: Path | str) -> CompatibilityRegistry:
    """Read one bounded snapshot from exactly the caller's source registry."""
    root, path, raw = _snapshot(source_root)
    try:
        document = _object(json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=lambda value: _malformed(f"invalid JSON constant: {value}")), "registry")
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RegistryError("malformed_policy", "invalid registry JSON") from exc
    for key in ("schemaVersion", "policySchemaVersion"):
        if key not in document or type(document[key]) is not int:
            _malformed(f"{key} must be an integer")
        if document[key] != 1:
            raise RegistryError("unsupported_schema", f"unsupported {key}")
    checked = _text(document.get("documentationChecked"), "documentationChecked")
    entries = _object(document.get("versions"), "versions")
    if not entries:
        _malformed("versions must not be empty")
    versions = {}
    for version, raw_entry in entries.items():
        _numeric_version(version)
        entry = _object(raw_entry, f"version {version}")
        if type(entry.get("agentSchemaVersion")) is not int:
            _malformed("agentSchemaVersion must be an integer")
        if entry["agentSchemaVersion"] != 1:
            raise RegistryError("unsupported_schema", "unsupported agentSchemaVersion")
        allowed = _strings(entry.get("allowedKeys"), "allowedKeys")
        required = _strings(entry.get("requiredKeys"), "requiredKeys")
        if not set(required).issubset(allowed):
            _malformed("requiredKeys must be a subset of allowedKeys")
        models = _object(entry.get("models"), "models")
        if not models:
            _malformed("models must not be empty")
        model_efforts = {}
        for model, values in models.items():
            _text(model, "model")
            efforts = _strings(values, f"efforts for {model}")
            if not set(efforts).issubset(_EFFORTS):
                _malformed("unknown reasoning effort")
            model_efforts[model] = efforts
        sandbox = _strings(entry.get("sandboxModes"), "sandboxModes")
        if not set(sandbox).issubset(("read-only", "workspace-write")):
            _malformed("unknown sandbox mode for agent schema")
        if type(entry.get("runtimeValidated")) is not bool:
            _malformed("runtimeValidated must be a boolean")
        gates = _object(entry.get("gates"), "gates")
        if set(gates) != set(GATES) or any(type(v) is not bool for v in gates.values()):
            _malformed("gates must contain exactly the named boolean gates")
        rollouts = entry.get("rolloutSchemas")
        if type(rollouts) is not list or any(type(v) is not str or v not in ROLLOUT_SCHEMAS for v in rollouts):
            _malformed("unknown or malformed rollout schemas")
        if len(set(rollouts)) != len(rollouts) or (rollouts and not gates["capturedEvidence"]):
            _malformed("inconsistent rollout schemas")
        versions[version] = VersionInfo(version, entry["agentSchemaVersion"], allowed,
            MappingProxyType(model_efforts), required, entry["runtimeValidated"], sandbox,
            MappingProxyType(dict(gates)), tuple(rollouts))
    target = _numeric_version(document.get("preferredInstallTarget"))
    if target not in versions or not versions[target].gates["staticInstallation"]:
        raise RegistryError("invalid_reference", "preferred target must be static eligible")
    profiles = _object(document.get("runProfiles"), "runProfiles")
    if not profiles:
        _malformed("runProfiles must not be empty")
    typed_profiles = {}
    for name, value in profiles.items():
        _text(name, "profile name")
        profile = _object(value, "profile")
        if set(profile) != {"expectedVersion"}:
            _malformed("profile must contain expectedVersion only")
        version = _numeric_version(profile["expectedVersion"])
        if version not in versions or not versions[version].gates["capturedEvidence"]:
            raise RegistryError("invalid_reference", "profile must reference a capture eligible version")
        typed_profiles[name] = RunProfile(version)
    return CompatibilityRegistry(1, 1, checked, target, MappingProxyType(versions),
        MappingProxyType(typed_profiles), path, hashlib.sha256(raw).hexdigest(), root)


def require_source_root(
    registry: CompatibilityRegistry, source_root: Path | str,
) -> CompatibilityRegistry:
    """Bind a snapshot to its lexical source, without rereading or rehashing it.

    Changed regular-file bytes leave the immutable snapshot authoritative. Missing
    paths, links and reparse aliases fail before resolution or consumer use.
    """
    root, path = _registry_path(source_root)
    if root != registry.source_root or path != registry.source_path:
        raise RegistryError("source_mismatch", "policy snapshot belongs to a different source root")
    return registry


def version_info(registry: CompatibilityRegistry, version: str) -> VersionInfo:
    if type(version) is not str or version not in registry.versions:
        raise RegistryError("unknown_version", "unknown Codex version")
    return registry.versions[version]


def _gate(gate: str) -> None:
    if type(gate) is not str or gate not in GATES:
        raise RegistryError("unknown_gate", "unknown compatibility gate")


def versions_for_gate(registry: CompatibilityRegistry, gate: str) -> tuple[str, ...]:
    _gate(gate)
    return tuple(version for version, info in registry.versions.items() if info.gates[gate])


def require_gate(registry: CompatibilityRegistry, version: str, gate: str) -> VersionInfo:
    _gate(gate)
    info = version_info(registry, version)
    if not info.gates[gate]:
        raise RegistryError("denied_gate", "version is ineligible for this gate")
    return info


def require_rollout_schema(registry: CompatibilityRegistry, version: str, schema: str) -> VersionInfo:
    info = require_gate(registry, version, "capturedEvidence")
    if type(schema) is not str or schema not in info.rollout_schemas:
        raise RegistryError("unsupported_rollout", "version does not support this rollout schema")
    return info


def run_profile(registry: CompatibilityRegistry, name: str) -> RunProfile:
    if type(name) is not str or name not in registry.run_profiles:
        raise RegistryError("unknown_profile", "unknown run profile")
    return registry.run_profiles[name]
