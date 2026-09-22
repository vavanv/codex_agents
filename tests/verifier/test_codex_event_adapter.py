from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import codex_event_adapter as adapter


PARENT = "00000000-0000-4000-8000-000000000001"


def child(index: int) -> str:
    return f"00000000-0000-4000-8000-{index + 2:012d}"


def complete_stream(*, terminal: bool = True) -> str:
    lines = [json.dumps({"type": "thread.started", "thread_id": PARENT})]
    for index, role in enumerate(sorted(adapter.EXPECTED_ROLES)):
        lines.append(
            json.dumps(
                {
                    "type": "agent.spawned",
                    "agent_name": role,
                    "child_session_id": child(index),
                }
            )
        )
    if terminal:
        lines.append(json.dumps({"type": "turn.completed"}))
    return "\n".join(lines) + "\n"


def collab_item(
    tool: str,
    status: str,
    *,
    receivers: list[str] | None = None,
    sender: str = PARENT,
    prompt: str | None = None,
    agents_states: dict | None = None,
) -> dict:
    return {
        "type": "collab_tool_call",
        "tool": tool,
        "status": status,
        "sender_thread_id": sender,
        "receiver_thread_ids": receivers or [],
        "prompt": prompt,
        "agents_states": agents_states or {},
    }


def collab_stream(*items: tuple[str, dict]) -> str:
    lines = [json.dumps({"type": "thread.started", "thread_id": PARENT})]
    lines.extend(
        json.dumps({"type": event_type, "item": item})
        for event_type, item in items
    )
    lines.append(json.dumps({"type": "turn.completed"}))
    return "\n".join(lines) + "\n"


class SanitizeTests(unittest.TestCase):
    def test_redacts_secrets_and_preserves_jsonl_structure(self) -> None:
        raw = (
            json.dumps({"type": "error", "message": "Authorization: Bearer super-secret-token"})
            + "\n\n"
            + json.dumps({"type": "turn.completed"})
            + "\n"
        )
        sanitized = adapter.sanitize_event_stream(raw)
        self.assertNotIn("super-secret-token", sanitized)
        self.assertIn("[REDACTED]", sanitized)
        lines = sanitized.splitlines()
        self.assertEqual("turn.completed", json.loads(lines[-1])["type"])
        self.assertTrue(sanitized.endswith("\n"))

    def test_secret_detection_matches_known_patterns(self) -> None:
        for secret in ("Bearer abc", "sk-abcdef", "password=hunter2", "postgres://u:p@h"):
            self.assertTrue(adapter._contains_secret(secret))
        self.assertFalse(adapter._contains_secret("ordinary text"))

    def test_redacts_nested_json_secret_fields(self) -> None:
        raw = json.dumps(
            {
                "type": "event",
                "token": "ghp_example",
                "nested": {"clientSecret": "nested-value"},
                "usage": {"input_tokens": 12},
            }
        ) + "\n"

        sanitized = adapter.sanitize_event_stream(raw)
        event = json.loads(sanitized)

        self.assertEqual("[REDACTED]", event["token"])
        self.assertEqual("[REDACTED]", event["nested"]["clientSecret"])
        self.assertEqual(12, event["usage"]["input_tokens"])
        self.assertNotIn("ghp_example", sanitized)
        self.assertFalse(adapter._contains_secret(sanitized))

    def test_malformed_json_with_sensitive_field_is_replaced(self) -> None:
        raw = '{"type":"event","password":"must-not-survive"\n'

        sanitized = adapter.sanitize_event_stream(raw)

        self.assertNotIn("must-not-survive", sanitized)
        self.assertEqual("sanitization.error", json.loads(sanitized)["type"])

    def test_sanitize_text_redacts_json_diagnostic_fields(self) -> None:
        raw = json.dumps(
            {"clientSecret": "top secret phrase", "nested": {"token": "ghp_value"}}
        )

        sanitized = adapter.sanitize_text(raw, None)

        self.assertNotIn("top secret phrase", sanitized)
        self.assertNotIn("ghp_value", sanitized)
        self.assertEqual("[REDACTED]", json.loads(sanitized)["clientSecret"])


class ParseTests(unittest.TestCase):
    def test_complete_stream_is_attributed_without_reason_codes(self) -> None:
        parsed = adapter.parse_event_stream(complete_stream())
        self.assertEqual("complete", parsed.integrity)
        self.assertEqual(PARENT, parsed.parent_session_id)
        self.assertEqual(set(adapter.EXPECTED_ROLES), set(parsed.attributed_children))
        self.assertTrue(parsed.terminal_seen)
        self.assertEqual((), parsed.reason_codes)

    def test_item_updated_and_thread_completed_are_supported(self) -> None:
        stream = complete_stream(terminal=False)
        stream += json.dumps({"type": "item.updated", "item": {"type": "reasoning"}}) + "\n"
        stream += json.dumps({"type": "thread.completed"}) + "\n"
        parsed = adapter.parse_event_stream(stream)
        self.assertEqual("complete", parsed.integrity)
        self.assertTrue(parsed.terminal_seen)

    def test_truncated_malformed_and_unsupported_streams_fail_closed(self) -> None:
        cases = (
            ("not-json\n", "MALFORMED_EVENT_STREAM"),
            (json.dumps({"type": "thread.started", "thread_id": PARENT}), "TRUNCATED_EVENT_STREAM"),
            (json.dumps({"future": "x"}) + "\n", "UNSUPPORTED_EVENT_SCHEMA"),
        )
        for stream, code in cases:
            with self.subTest(code=code):
                self.assertIn(code, adapter.parse_event_stream(stream).reason_codes)

    def test_duplicate_and_conflicting_attribution_are_rejected(self) -> None:
        role = sorted(adapter.EXPECTED_ROLES)[0]
        duplicate = complete_stream() + json.dumps(
            {"type": "agent.spawned", "agent_name": role, "child_session_id": child(0)}
        ) + "\n"
        self.assertIn("DUPLICATE_ATTRIBUTION", adapter.parse_event_stream(duplicate).reason_codes)
        conflict = complete_stream() + json.dumps(
            {"type": "agent.spawned", "agent_name": role, "child_session_id": child(99)}
        ) + "\n"
        self.assertIn("ATTRIBUTION_CONFLICT", adapter.parse_event_stream(conflict).reason_codes)

    def test_attribution_before_parent_is_rejected(self) -> None:
        role = sorted(adapter.EXPECTED_ROLES)[0]
        reordered = json.dumps(
            {"type": "agent.spawned", "agent_name": role, "child_session_id": child(0)}
        ) + "\n" + json.dumps({"type": "thread.started", "thread_id": PARENT}) + "\n"
        self.assertIn("ATTRIBUTION_BEFORE_PARENT", adapter.parse_event_stream(reordered).reason_codes)

    def test_observed_wait_shape_is_passive_and_supported(self) -> None:
        stream = collab_stream(
            ("item.started", collab_item("wait", "in_progress")),
            ("item.completed", collab_item("wait", "completed")),
        )

        parsed = adapter.parse_event_stream(
            stream, expected_roles=("code_explorer",)
        )

        self.assertNotIn("UNSUPPORTED_EVENT_SCHEMA", parsed.reason_codes)
        self.assertIn("MISSING_ATTRIBUTION", parsed.reason_codes)
        self.assertEqual({}, parsed.attributed_children)

    def test_singleton_completed_spawn_maps_explicit_expected_role(self) -> None:
        stream = collab_stream(
            (
                "item.started",
                collab_item("spawn_agent", "in_progress", prompt="bounded task"),
            ),
            (
                "item.completed",
                collab_item(
                    "spawn_agent",
                    "completed",
                    receivers=[child(0)],
                    prompt="bounded task",
                ),
            ),
        )

        parsed = adapter.parse_event_stream(
            stream, expected_roles=("code_explorer",)
        )

        self.assertEqual("complete", parsed.integrity)
        self.assertEqual({"code_explorer": child(0)}, parsed.attributed_children)

    def test_singleton_spawn_without_explicit_role_defaults_to_catalog_and_fails(self) -> None:
        stream = collab_stream(
            (
                "item.completed",
                collab_item(
                    "spawn_agent",
                    "completed",
                    receivers=[child(0)],
                    prompt="bounded task",
                ),
            )
        )

        parsed = adapter.parse_event_stream(stream)

        self.assertIn("AMBIGUOUS_ATTRIBUTION", parsed.reason_codes)
        self.assertIn("MISSING_ATTRIBUTION", parsed.reason_codes)
        self.assertEqual({}, parsed.attributed_children)

    def test_empty_duplicate_and_unknown_expected_roles_fail_closed(self) -> None:
        stream = collab_stream(("item.completed", collab_item("wait", "completed")))
        cases = ((), ("code_explorer", "code_explorer"), ("not_a_role",))
        for roles in cases:
            with self.subTest(roles=roles):
                parsed = adapter.parse_event_stream(stream, expected_roles=roles)
                self.assertIn("INVALID_EXPECTED_ROLES", parsed.reason_codes)
                self.assertIn("MISSING_ATTRIBUTION", parsed.reason_codes)

    def test_malformed_failed_and_ambiguous_collab_calls_fail_closed(self) -> None:
        cases = (
            (
                {"type": "collab_tool_call", "tool": "wait"},
                ("code_explorer",),
                "MALFORMED_COLLAB_TOOL_CALL",
            ),
            (
                collab_item("future_tool", "completed"),
                ("code_explorer",),
                "UNSUPPORTED_COLLAB_TOOL",
            ),
            (
                collab_item("spawn_agent", "failed", prompt="bounded task"),
                ("code_explorer",),
                "COLLAB_TOOL_CALL_FAILED",
            ),
            (
                collab_item(
                    "spawn_agent",
                    "completed",
                    receivers=["not-a-uuid"],
                    prompt="bounded task",
                ),
                ("code_explorer",),
                "INVALID_CHILD_SESSION",
            ),
            (
                collab_item(
                    "spawn_agent",
                    "completed",
                    receivers=[PARENT],
                    prompt="bounded task",
                ),
                ("code_explorer",),
                "INVALID_CHILD_SESSION",
            ),
            (
                collab_item(
                    "spawn_agent",
                    "completed",
                    receivers=[child(0)],
                    sender=child(1),
                    prompt="bounded task",
                ),
                ("code_explorer",),
                "COLLAB_PARENT_MISMATCH",
            ),
            (
                collab_item(
                    "spawn_agent",
                    "completed",
                    receivers=[child(0), child(0)],
                    prompt="bounded task",
                ),
                ("code_explorer",),
                "MALFORMED_COLLAB_TOOL_CALL",
            ),
            (
                collab_item(
                    "spawn_agent",
                    "completed",
                    receivers=[child(0)],
                    prompt="bounded task",
                ),
                ("code_explorer", "implementer"),
                "AMBIGUOUS_ATTRIBUTION",
            ),
        )
        for item, roles, code in cases:
            with self.subTest(code=code):
                parsed = adapter.parse_event_stream(
                    collab_stream(("item.completed", item)), expected_roles=roles
                )
                self.assertIn(code, parsed.reason_codes)
                self.assertNotEqual("complete", parsed.integrity)

    def test_duplicate_real_spawn_attribution_fails_closed(self) -> None:
        completed_spawn = collab_item(
            "spawn_agent",
            "completed",
            receivers=[child(0)],
            prompt="bounded task",
        )
        stream = collab_stream(
            ("item.completed", completed_spawn),
            ("item.completed", completed_spawn),
        )

        parsed = adapter.parse_event_stream(
            stream, expected_roles=("code_explorer",)
        )

        self.assertIn("DUPLICATE_ATTRIBUTION", parsed.reason_codes)
        self.assertNotEqual("complete", parsed.integrity)

    def test_legacy_collaboration_tool_call_remains_supported(self) -> None:
        stream = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": PARENT}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "collaboration_tool_call",
                            "tool": "spawn_agent",
                            "agent_name": "code_explorer",
                            "child_session_id": child(0),
                        },
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            )
        ) + "\n"

        parsed = adapter.parse_event_stream(
            stream, expected_roles=("code_explorer",)
        )

        self.assertEqual("complete", parsed.integrity)
        self.assertEqual({"code_explorer": child(0)}, parsed.attributed_children)

    def test_legacy_started_spawn_does_not_attribute(self) -> None:
        stream = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": PARENT}),
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {
                            "type": "collaboration_tool_call",
                            "tool": "spawn_agent",
                            "status": "in_progress",
                            "agent_name": "code_explorer",
                            "child_session_id": child(0),
                        },
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            )
        ) + "\n"

        parsed = adapter.parse_event_stream(
            stream, expected_roles=("code_explorer",)
        )

        self.assertEqual({}, parsed.attributed_children)
        self.assertIn("MISSING_ATTRIBUTION", parsed.reason_codes)

    def test_legacy_failed_and_incompatible_statuses_fail_closed(self) -> None:
        cases = (
            ("item.completed", "failed", "COLLAB_TOOL_CALL_FAILED"),
            ("item.started", "failed", "COLLAB_TOOL_CALL_FAILED"),
            ("item.started", "completed", "MALFORMED_COLLAB_TOOL_CALL"),
            ("item.completed", "in_progress", "MALFORMED_COLLAB_TOOL_CALL"),
            ("item.completed", "future", "MALFORMED_COLLAB_TOOL_CALL"),
            ("item.completed", 1, "MALFORMED_COLLAB_TOOL_CALL"),
        )
        for event_type, status, code in cases:
            with self.subTest(event_type=event_type, status=status):
                stream = "\n".join(
                    (
                        json.dumps({"type": "thread.started", "thread_id": PARENT}),
                        json.dumps(
                            {
                                "type": event_type,
                                "item": {
                                    "type": "collaboration_tool_call",
                                    "tool": "spawn_agent",
                                    "status": status,
                                    "agent_name": "code_explorer",
                                    "child_session_id": child(0),
                                },
                            }
                        ),
                        json.dumps({"type": "turn.completed"}),
                    )
                ) + "\n"

                parsed = adapter.parse_event_stream(
                    stream, expected_roles=("code_explorer",)
                )

                self.assertEqual({}, parsed.attributed_children)
                self.assertIn(code, parsed.reason_codes)
                self.assertIn("MISSING_ATTRIBUTION", parsed.reason_codes)

    def test_legacy_completed_status_and_full_catalog_remain_supported(self) -> None:
        lines = [json.dumps({"type": "thread.started", "thread_id": PARENT})]
        for index, role in enumerate(sorted(adapter.EXPECTED_ROLES)):
            lines.append(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "collaboration_tool_call",
                            "tool": "spawn_agent",
                            "status": "completed",
                            "agent_name": role,
                            "child_session_id": child(index),
                        },
                    }
                )
            )
        lines.append(json.dumps({"type": "turn.completed"}))

        parsed = adapter.parse_event_stream("\n".join(lines) + "\n")

        self.assertEqual("complete", parsed.integrity)
        self.assertEqual(set(adapter.EXPECTED_ROLES), set(parsed.attributed_children))


class FixtureTests(unittest.TestCase):
    @staticmethod
    def manifest(
        raw: str,
        codex_version: str = "0.155.1",
        roles: tuple[str, ...] | None = None,
    ) -> dict:
        manifest = adapter.capture_fixture_manifest("fixture-1", codex_version, raw)
        manifest.update(
            {
                "reviewed": True,
                "requestedRoles": list(roles or sorted(adapter.EXPECTED_ROLES)),
                "timedOut": False,
                "exitCode": 0,
            }
        )
        return manifest

    def test_conformant_fixture_validates(self) -> None:
        raw = complete_stream()
        manifest = self.manifest(raw)
        result = adapter.validate_captured_fixture(adapter.sanitize_event_stream(raw), manifest)
        self.assertEqual("schema-conformant", result.conformance)
        self.assertEqual((), result.reason_codes)
        self.assertTrue(result.sanitized)

    def test_hash_and_version_mismatch_are_rejected(self) -> None:
        raw = complete_stream()
        manifest = self.manifest(raw)
        sanitized = adapter.sanitize_event_stream(raw)
        tampered = adapter.validate_captured_fixture(
            sanitized + json.dumps({"type": "turn.completed"}) + "\n", manifest
        )
        self.assertIn("FIXTURE_HASH_MISMATCH", tampered.reason_codes)

        wrong_version = self.manifest(raw, codex_version="0.154.0")
        versioned = adapter.validate_captured_fixture(
            adapter.sanitize_event_stream(raw), wrong_version
        )
        self.assertIn("FIXTURE_VERSION_MISMATCH", versioned.reason_codes)

    def test_unsanitized_secret_is_rejected(self) -> None:
        raw = complete_stream() + json.dumps(
            {"type": "error", "message": "api_key=secret-value"}
        ) + "\n"
        manifest = self.manifest(raw)
        result = adapter.validate_captured_fixture(raw, manifest)
        self.assertIn("FIXTURE_SECRET_PRESENT", result.reason_codes)

    def test_missing_terminal_attribution_is_rejected(self) -> None:
        raw = complete_stream(terminal=False)
        manifest = self.manifest(raw)
        result = adapter.validate_captured_fixture(adapter.sanitize_event_stream(raw), manifest)
        self.assertIn("MISSING_TERMINAL_ATTRIBUTION", result.reason_codes)
        self.assertEqual("not-schema-conformant", result.conformance)

    def test_partial_roles_are_not_conformant(self) -> None:
        raw = complete_stream()
        manifest = self.manifest(raw, roles=("code_explorer",))
        result = adapter.validate_captured_fixture(
            adapter.sanitize_event_stream(raw), manifest, expected_roles=("code_explorer",)
        )
        self.assertIn("ATTRIBUTION_UNEXPECTED", result.reason_codes)

    def test_reviewed_single_role_collab_fixture_validates(self) -> None:
        raw = collab_stream(
            (
                "item.completed",
                collab_item(
                    "spawn_agent",
                    "completed",
                    receivers=[child(0)],
                    prompt="bounded task",
                ),
            )
        )
        roles = ("code_explorer",)
        manifest = self.manifest(raw, roles=roles)

        result = adapter.validate_captured_fixture(
            adapter.sanitize_event_stream(raw), manifest, expected_roles=roles
        )

        self.assertEqual("schema-conformant", result.conformance)
        self.assertEqual((), result.reason_codes)
        self.assertEqual(
            {"code_explorer": child(0)}, result.attribution.attributed_children
        )

    def test_manifest_eligibility_fields_fail_closed_with_specific_codes(self) -> None:
        raw = complete_stream()
        cases = (
            ("schema", "future/v2", "FIXTURE_MANIFEST_SCHEMA_UNSUPPORTED"),
            ("reviewed", False, "FIXTURE_REVIEW_REQUIRED"),
            ("sanitized", False, "FIXTURE_SANITIZED_REQUIRED"),
            ("requestedRoles", [], "FIXTURE_REQUESTED_ROLES_INVALID"),
            (
                "requestedRoles",
                ["code_explorer"],
                "FIXTURE_REQUESTED_ROLES_MISMATCH",
            ),
            ("timedOut", True, "FIXTURE_CAPTURE_TIMED_OUT"),
            ("exitCode", 1, "FIXTURE_EXIT_CODE_INVALID"),
            ("exitCode", True, "FIXTURE_EXIT_CODE_INVALID"),
        )
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                manifest = self.manifest(raw)
                manifest[field] = value
                result = adapter.validate_captured_fixture(
                    adapter.sanitize_event_stream(raw), manifest
                )
                self.assertIn(code, result.reason_codes)
                self.assertEqual("not-schema-conformant", result.conformance)

    def test_missing_manifest_eligibility_fields_fail_closed(self) -> None:
        raw = complete_stream()
        fields = (
            ("schema", "FIXTURE_MANIFEST_SCHEMA_UNSUPPORTED"),
            ("reviewed", "FIXTURE_REVIEW_REQUIRED"),
            ("sanitized", "FIXTURE_SANITIZED_REQUIRED"),
            ("requestedRoles", "FIXTURE_REQUESTED_ROLES_INVALID"),
            ("timedOut", "FIXTURE_CAPTURE_TIMED_OUT"),
            ("exitCode", "FIXTURE_EXIT_CODE_INVALID"),
        )
        for field, code in fields:
            with self.subTest(field=field):
                manifest = self.manifest(raw)
                del manifest[field]
                result = adapter.validate_captured_fixture(
                    adapter.sanitize_event_stream(raw), manifest
                )
                self.assertIn(code, result.reason_codes)
                self.assertEqual("not-schema-conformant", result.conformance)


if __name__ == "__main__":
    unittest.main()

