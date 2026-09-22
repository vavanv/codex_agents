from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import codex_event_adapter as adapter
import model_routing as routing
from validate_agent_configs import EXPECTED_ROLES, configured_models


PARENT = "00000000-0000-4000-8000-000000000001"


def child(index: int) -> str:
    return f"00000000-0000-4000-8000-{index + 2:012d}"


def spawn(role: str, index: int, model: str | None, effort: str | None) -> dict:
    event: dict[str, object] = {
        "type": "agent.spawned",
        "agent_name": role,
        "child_session_id": child(index),
    }
    if model is not None:
        event["model"] = model
    if effort is not None:
        event["model_reasoning_effort"] = effort
    return event


def build_stream(configured: dict, overrides: dict[str, dict[str, str]] | None = None) -> str:
    overrides = overrides or {}
    lines = [json.dumps({"type": "thread.started", "thread_id": PARENT})]
    for index, role in enumerate(sorted(EXPECTED_ROLES)):
        config = dict(configured.get(role, {}))
        config.update(overrides.get(role, {}))
        lines.append(
            json.dumps(
                spawn(
                    role,
                    index,
                    config.get("model") or None,
                    config.get("effort") or None,
                )
            )
        )
    lines.append(json.dumps({"type": "turn.completed"}))
    return "\n".join(lines) + "\n"


class ModelRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.configured = configured_models(REPOSITORY_ROOT)
        self.assertEqual(set(EXPECTED_ROLES), set(self.configured))

    def test_all_roles_route_pass(self) -> None:
        attribution = adapter.parse_event_stream(build_stream(self.configured))
        verdicts = routing.evaluate_model_routing(self.configured, attribution)
        self.assertTrue(all(item.verdict == "PASS" for item in verdicts))
        self.assertEqual(set(EXPECTED_ROLES), set(item.role for item in verdicts))

    def test_model_mismatch_fails(self) -> None:
        role = "code_explorer"
        overrides = {role: {"model": "wrong-model"}}
        attribution = adapter.parse_event_stream(build_stream(self.configured, overrides))
        verdicts = routing.evaluate_model_routing(self.configured, attribution)
        by_role = {item.role: item for item in verdicts}
        self.assertEqual("FAIL", by_role[role].verdict)
        self.assertIn("MODEL_MISMATCH", by_role[role].reason_codes)

    def test_effort_mismatch_fails(self) -> None:
        role = "implementer"
        overrides = {role: {"effort": "low"}}
        attribution = adapter.parse_event_stream(build_stream(self.configured, overrides))
        verdicts = routing.evaluate_model_routing(self.configured, attribution)
        by_role = {item.role: item for item in verdicts}
        self.assertEqual("FAIL", by_role[role].verdict)
        self.assertIn("EFFORT_MISMATCH", by_role[role].reason_codes)

    def test_missing_observed_metadata_is_unverified(self) -> None:
        overrides = {role: {"model": None, "effort": None} for role in EXPECTED_ROLES}
        attribution = adapter.parse_event_stream(build_stream(self.configured, overrides))
        verdicts = routing.evaluate_model_routing(self.configured, attribution)
        self.assertTrue(all(item.verdict == "UNVERIFIED" for item in verdicts))
        self.assertTrue(
            any("MISSING_OBSERVED_MODEL" in item.reason_codes for item in verdicts)
        )

    def test_unattributed_role_is_unverified(self) -> None:
        stream = (
            json.dumps({"type": "thread.started", "thread_id": PARENT}) + "\n"
            + json.dumps(
                spawn("code_explorer", 0, "gpt-5.6-luna", "low")
            )
            + "\n"
            + json.dumps({"type": "turn.completed"}) + "\n"
        )
        attribution = adapter.parse_event_stream(stream)
        verdicts = routing.evaluate_model_routing(self.configured, attribution)
        by_role = {item.role: item for item in verdicts}
        self.assertEqual("PASS", by_role["code_explorer"].verdict)
        self.assertEqual("UNVERIFIED", by_role["implementer"].verdict)
        self.assertIn("UNATTRIBUTED_ROLE", by_role["implementer"].reason_codes)

    def test_summary_reports_counts_and_overall(self) -> None:
        attribution = adapter.parse_event_stream(build_stream(self.configured))
        result = routing.summary(routing.evaluate_model_routing(self.configured, attribution))
        self.assertEqual("PASS", result["overall"])
        self.assertEqual(9, result["counts"]["PASS"])
        self.assertEqual(9, len(result["roles"]))


if __name__ == "__main__":
    unittest.main()
