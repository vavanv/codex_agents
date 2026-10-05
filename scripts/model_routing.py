#!/usr/bin/env python3
"""Deterministic V4 model/effort routing evidence (§10, Tests 5-6).

Compares configured model/effort (from the source TOML catalog) against observed
model/effort (extracted from a versioned event stream) per role. It establishes
no live validation: absent observed metadata is UNVERIFIED and a mismatch is FAIL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from codex_event_adapter import StreamAttribution
from validate_agent_configs import EXPECTED_ROLES


_VERDICT_ORDER = {"PASS": 0, "UNVERIFIED": 1, "FAIL": 2}

REASON_CODES = frozenset(
    {
        "UNATTRIBUTED_ROLE",
        "MISSING_OBSERVED_MODEL",
        "MISSING_OBSERVED_EFFORT",
        "MODEL_MISMATCH",
        "EFFORT_MISMATCH",
    }
)


@dataclass(frozen=True)
class ModelRoutingVerdict:
    role: str
    attributed: bool
    configured_model: str
    observed_model: str | None
    configured_effort: str
    observed_effort: str | None
    verdict: str
    reason_codes: tuple[str, ...]


def evaluate_model_routing(
    configured: Mapping[str, Mapping[str, str]],
    attribution: StreamAttribution,
    *,
    expected_roles: tuple[str, ...] | None = None,
) -> tuple[ModelRoutingVerdict, ...]:
    """Compare configured vs observed model/effort for each role.

    ``configured`` is ``validate_agent_configs.configured_models(...)`` output;
    ``attribution`` is the parsed ``StreamAttribution`` carrying observed
    model/effort. Unattributed roles and missing observed metadata are
    UNVERIFIED; a configured/observed mismatch is FAIL.
    """
    return evaluate_routing_observations(
        configured, attributed_children=attribution.attributed_children,
        observed_models=attribution.observed_models,
        observed_efforts=attribution.observed_efforts, expected_roles=expected_roles,
    )


def evaluate_routing_observations(
    configured: Mapping[str, Mapping[str, str]], *,
    attributed_children: Mapping[str, str],
    observed_models: Mapping[str, str],
    observed_efforts: Mapping[str, str],
    expected_roles: tuple[str, ...] | None = None,
) -> tuple[ModelRoutingVerdict, ...]:
    """Compare already attributed observations; callers retain their source type.

    This is shared comparison logic, not an attribution or acceptance validator.
    Private-context callers must establish role/session/task linkage first.
    """
    expected_roles = tuple(sorted(expected_roles or EXPECTED_ROLES))
    verdicts: list[ModelRoutingVerdict] = []
    for role in expected_roles:
        config = configured.get(role)
        configured_model = config.get("model", "") if isinstance(config, dict) else ""
        configured_effort = config.get("effort", "") if isinstance(config, dict) else ""
        observed_model = observed_models.get(role)
        observed_effort = observed_efforts.get(role)
        reasons: list[str] = []
        if role not in attributed_children:
            reasons.append("UNATTRIBUTED_ROLE")
        else:
            if observed_model is None:
                reasons.append("MISSING_OBSERVED_MODEL")
            if observed_effort is None:
                reasons.append("MISSING_OBSERVED_EFFORT")
            if observed_model is not None and observed_model != configured_model:
                reasons.append("MODEL_MISMATCH")
            if observed_effort is not None and observed_effort != configured_effort:
                reasons.append("EFFORT_MISMATCH")
        if {"MODEL_MISMATCH", "EFFORT_MISMATCH"}.intersection(reasons):
            verdict = "FAIL"
        elif reasons:
            verdict = "UNVERIFIED"
        else:
            verdict = "PASS"
        verdicts.append(
            ModelRoutingVerdict(
                role=role,
                attributed=role in attributed_children,
                configured_model=configured_model,
                observed_model=observed_model,
                configured_effort=configured_effort,
                observed_effort=observed_effort,
                verdict=verdict,
                reason_codes=tuple(sorted(reasons)),
            )
        )
    return tuple(verdicts)


def summary(verdicts: tuple[ModelRoutingVerdict, ...]) -> dict[str, object]:
    counts = {"PASS": 0, "FAIL": 0, "UNVERIFIED": 0}
    for verdict in verdicts:
        counts[verdict.verdict] += 1
    overall = (
        max(verdicts, key=lambda item: _VERDICT_ORDER[item.verdict]).verdict
        if verdicts
        else "UNVERIFIED"
    )
    return {
        "overall": overall,
        "counts": counts,
        "roles": [
            {
                "role": item.role,
                "attributed": item.attributed,
                "configuredModel": item.configured_model,
                "observedModel": item.observed_model,
                "configuredEffort": item.configured_effort,
                "observedEffort": item.observed_effort,
                "verdict": item.verdict,
                "reasonCodes": list(item.reason_codes),
            }
            for item in verdicts
        ],
    }
