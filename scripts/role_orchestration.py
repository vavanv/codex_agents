#!/usr/bin/env python3
"""Deterministic V3 orchestration: feed event streams and snapshots through the
versioned event adapter and the role-compliance engine.

This module is the entry point that will consume real captured Codex 0.154.0
fixtures and real before/after snapshots to produce per-role compliance
evidence. It is deterministic and fully testable with synthetic inputs; it does
not invoke Codex and establishes no live runtime validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from codex_event_adapter import parse_event_stream
from role_compliance import evaluate_roles
from validate_agent_configs import EXPECTED_ROLES


_VERDICT_ORDER = {"PASS": 0, "UNVERIFIED": 1, "BLOCKED": 2, "FAIL": 3}


@dataclass(frozen=True)
class RoleEvidence:
    role: str
    attributed: bool
    child_session_id: str | None
    sandbox_mode: str
    verdict: str
    reason_codes: tuple[str, ...]
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class RoleEvidenceReport:
    parent_session_id: str | None
    event_count: int
    stream_integrity: str
    parse_reason_codes: tuple[str, ...]
    roles: tuple[RoleEvidence, ...]

    @property
    def overall(self) -> str:
        if not self.roles:
            return "UNVERIFIED"
        return max(self.roles, key=lambda item: _VERDICT_ORDER[item.verdict]).verdict


def build_role_evidence(
    event_stream: str,
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    head_before: str | None = None,
    head_after: str | None = None,
    allowed_paths: Mapping[str, frozenset[str]] | None = None,
    authorized_commit: bool = False,
) -> RoleEvidenceReport:
    """Feed one event stream + snapshots through the adapter and compliance engine.

    ``event_stream`` is a Codex CLI JSONL stream; ``before``/``after`` are file
    digest maps (path -> sha256), e.g. ``capture_git_snapshot(...)["fileHashes"]``.
    Returns per-role attribution + compliance evidence plus the adapter's
    stream-level diagnostics.
    """
    attribution = parse_event_stream(event_stream)
    verdicts = evaluate_roles(
        attribution.attributed_children,
        before,
        after,
        head_before=head_before,
        head_after=head_after,
        allowed_paths=allowed_paths,
        authorized_commit=authorized_commit,
    )
    by_verdict = {item.role: item for item in verdicts}
    roles = tuple(
        RoleEvidence(
            role=role,
            attributed=role in attribution.attributed_children,
            child_session_id=attribution.attributed_children.get(role),
            sandbox_mode=by_verdict[role].sandbox_mode,
            verdict=by_verdict[role].verdict,
            reason_codes=by_verdict[role].reason_codes,
            changed_paths=by_verdict[role].changed_paths,
        )
        for role in sorted(EXPECTED_ROLES)
    )
    return RoleEvidenceReport(
        parent_session_id=attribution.parent_session_id,
        event_count=attribution.event_count,
        stream_integrity=attribution.integrity,
        parse_reason_codes=attribution.reason_codes,
        roles=roles,
    )


def summary(report: RoleEvidenceReport) -> dict[str, object]:
    """Collapse a role-evidence report into a compact, secret-free summary."""
    counts: dict[str, int] = {}
    for role in report.roles:
        counts[role.verdict] = counts.get(role.verdict, 0) + 1
    return {
        "parentSessionId": report.parent_session_id,
        "eventCount": report.event_count,
        "streamIntegrity": report.stream_integrity,
        "parseReasonCodes": list(report.parse_reason_codes),
        "overall": report.overall,
        "counts": counts,
        "roles": [
            {
                "role": role.role,
                "attributed": role.attributed,
                "childSessionId": role.child_session_id,
                "sandboxMode": role.sandbox_mode,
                "verdict": role.verdict,
                "reasonCodes": list(role.reason_codes),
                "changedPaths": list(role.changed_paths),
            }
            for role in report.roles
        ],
    }
