"""Small, dependency-free redactor for explicitly entered timeline summaries."""

from __future__ import annotations

import json
import re
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk-[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)\b(?:api[_ -]?key|password|token|secret|connection[_ -]?string)\b\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\b(?:[A-Z0-9]+_)*(?:API_KEY|SECRET_ACCESS_KEY|ACCESS_TOKEN|AUTH_TOKEN|PASSWORD|PRIVATE_KEY|CLIENT_SECRET)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@"),
)
_SENSITIVE_KEYS = frozenset({
    "api_key", "access_token", "auth_token", "authorization", "client_secret",
    "connection_string", "credential", "credentials", "password", "private_key",
    "secret", "secret_access_key", "token",
})
_SENSITIVE_FIELD = re.compile(
    r"(?i)[\"']?(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|authorization|"
    r"client[_ -]?secret|connection[_ -]?string|credentials?|password|"
    r"private[_ -]?key|secret(?:[_ -]?access[_ -]?key)?|token)[\"']?\s*:"
)


def _redact_text(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def _sensitive_key(value: str) -> bool:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")
    return any(normalized == key or normalized.endswith(f"_{key}") for key in _SENSITIVE_KEYS)


def _redact_json(value: Any, depth: int = 0) -> Any:
    if depth > 32:
        return "[REDACTED_UNSAFE_SUMMARY]"
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _sensitive_key(key) else _redact_json(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def sanitize_summary(value: str, limit: int = 500) -> str:
    """Redact common credentials; never use this as permission to paste prompts."""
    # Credential blocks such as PEM keys span multiple lines. Redact them
    # before splitting into independently parsed JSON/text lines.
    value = _redact_text(value)
    lines: list[str] = []
    for line in value.splitlines() or [value]:
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            lines.append("[REDACTED_UNSAFE_SUMMARY]" if _SENSITIVE_FIELD.search(line) else _redact_text(line))
        else:
            lines.append(json.dumps(_redact_json(parsed), ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)[-limit:]
