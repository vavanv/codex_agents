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


class FixtureTests(unittest.TestCase):
    @staticmethod
    def manifest(raw: str, codex_version: str = "0.155.1") -> dict:
        return adapter.capture_fixture_manifest("fixture-1", codex_version, raw)

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
        manifest = self.manifest(raw)
        result = adapter.validate_captured_fixture(
            adapter.sanitize_event_stream(raw), manifest, expected_roles=("code_explorer",)
        )
        self.assertIn("MISSING_ATTRIBUTION", result.reason_codes)


if __name__ == "__main__":
    unittest.main()

