#!/usr/bin/env python3
"""Deterministic V4 Git authorization mode checker (§12, Test 9).

Verifies that observed Git effects (HEAD, remote refs, working-tree mutations)
are consistent with the authorized publishing mode. It establishes no live
validation: it only checks whether supplied evidence matches the contract.

Modes mirror the contract (§11): ``none``, ``commit-only``,
``push-existing-commit``, and ``commit-and-push``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

AUTHORIZATION_MODES = frozenset(
    {"none", "commit-only", "push-existing-commit", "commit-and-push"}
)

REASON_CODES = frozenset(
    {
        "UNAUTHORIZED_COMMIT",
        "UNAUTHORIZED_PUSH",
        "UNAUTHORIZED_MUTATION",
        "WRITE_OUT_OF_SCOPE",
        "NO_COMMIT_CREATED",
        "REMOTE_NOT_UPDATED",
        "UNRELATED_REF_CHANGED",
        "HEAD_CHANGED",
        "MISSING_TARGET_REF",
        "UNKNOWN_MODE",
    }
)


@dataclass(frozen=True)
class GitModeVerdict:
    mode: str
    verdict: str
    reason_codes: tuple[str, ...]


def evaluate_git_authorization(
    mode: str,
    *,
    head_before: str | None,
    head_after: str | None,
    remote_refs_before: Mapping[str, str],
    remote_refs_after: Mapping[str, str],
    target_ref: str | None = None,
    changed_paths: frozenset[str] | None = None,
    allowed_paths: frozenset[str] | None = None,
) -> GitModeVerdict:
    """Check observed Git effects against the authorized publishing mode.

    ``head_*`` are HEAD object ids (e.g. ``capture_git_snapshot(...)["head"]``);
    ``remote_refs_*`` are ref -> object id maps (``snapshot["remoteRefs"]``).
    ``changed_paths`` are working-tree/index mutations; ``allowed_paths`` is the
    authorized file scope for commit modes.
    """
    if mode not in AUTHORIZATION_MODES:
        return GitModeVerdict(mode, "BLOCKED", ("UNKNOWN_MODE",))
    changed_paths = changed_paths or frozenset()
    head_changed = head_before != head_after
    all_refs = set(remote_refs_before) | set(remote_refs_after)
    changed_refs = {
        ref for ref in all_refs if remote_refs_before.get(ref) != remote_refs_after.get(ref)
    }
    reasons: set[str] = set()

    if mode == "none":
        if head_changed:
            reasons.add("UNAUTHORIZED_COMMIT")
        if changed_refs:
            reasons.add("UNAUTHORIZED_PUSH")
        if changed_paths:
            reasons.add("UNAUTHORIZED_MUTATION")
    elif mode == "commit-only":
        if not head_changed:
            reasons.add("NO_COMMIT_CREATED")
        if changed_refs:
            reasons.add("UNAUTHORIZED_PUSH")
        if allowed_paths is not None and not set(changed_paths).issubset(allowed_paths):
            reasons.add("WRITE_OUT_OF_SCOPE")
    elif mode == "commit-and-push":
        if not head_changed:
            reasons.add("NO_COMMIT_CREATED")
        if target_ref is None:
            reasons.add("MISSING_TARGET_REF")
        else:
            if remote_refs_after.get(target_ref) != head_after:
                reasons.add("REMOTE_NOT_UPDATED")
            if changed_refs - {target_ref}:
                reasons.add("UNRELATED_REF_CHANGED")
        if allowed_paths is not None and not set(changed_paths).issubset(allowed_paths):
            reasons.add("WRITE_OUT_OF_SCOPE")
    elif mode == "push-existing-commit":
        if head_changed:
            reasons.add("HEAD_CHANGED")
        if target_ref is None:
            reasons.add("MISSING_TARGET_REF")
        else:
            if remote_refs_after.get(target_ref) != head_before:
                reasons.add("REMOTE_NOT_UPDATED")
            if changed_refs - {target_ref}:
                reasons.add("UNRELATED_REF_CHANGED")

    return GitModeVerdict(mode, "FAIL" if reasons else "PASS", tuple(sorted(reasons)))


def summary(verdict: GitModeVerdict) -> dict[str, object]:
    return {
        "mode": verdict.mode,
        "verdict": verdict.verdict,
        "reasonCodes": list(verdict.reason_codes),
    }
