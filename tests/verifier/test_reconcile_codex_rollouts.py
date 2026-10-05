from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
import sys
import tomllib
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import codex_event_adapter as adapter
import reconcile_codex_rollouts as reconciliation
import codex_compatibility as compatibility


PARENT = "00000000-0000-4000-8000-000000000001"
CHILD = "00000000-0000-4000-8000-000000000002"
ROLE = "code_explorer"
CONFIG = (REPOSITORY_ROOT / "agents" / "code-explorer.toml").read_bytes()
INSTRUCTIONS = tomllib.loads(CONFIG.decode())["developer_instructions"]
VALIDATOR_CONFIG = (REPOSITORY_ROOT / "agents" / "code-validator.toml").read_bytes()
ARCHITECT_CONFIG = (REPOSITORY_ROOT / "agents" / "sol-architect.toml").read_bytes()
ROLE_FILES = {
    "code_explorer": "code-explorer.toml",
    "quick_implementer": "quick-implementer.toml",
    "implementer": "implementer.toml",
    "luna_escalation": "luna-escalation.toml",
    "sol_architect": "sol-architect.toml",
    "sol_architect_deep": "sol-architect-deep.toml",
    "code_validator": "code-validator.toml",
    "code_reviewer": "code-reviewer.toml",
    "commit_pusher": "commit-pusher.toml",
}


def role_config(role: str) -> bytes:
    return (REPOSITORY_ROOT / "agents" / ROLE_FILES[role]).read_bytes()


def row(row_type: str, payload: dict) -> dict:
    return {"type": row_type, "payload": payload}


def encoded(rows: list[dict]) -> bytes:
    return ("\n".join(json.dumps(item) for item in rows) + "\n").encode()


def fixture(role: str = ROLE, version: str = "0.155.1") -> tuple[bytes, dict, list[dict], list[dict]]:
    settings = tomllib.loads(role_config(role).decode())
    instructions = settings["developer_instructions"]
    task_name = f"probe_{role}"
    task_path = f"/root/{task_name}"
    raw_capture = (
        json.dumps({"type": "thread.started", "thread_id": PARENT})
        + "\n"
        + json.dumps({"type": "turn.completed"})
        + "\n"
    )
    capture = adapter.sanitize_event_stream(raw_capture).encode()
    manifest = adapter.capture_fixture_manifest("one-role", version, raw_capture)
    manifest.update(
        {
            "requestedRoles": [role],
            "sanitized": True,
            "reviewed": False,
            "timedOut": False,
            "exitCode": 0,
        }
    )
    parent = [
        row(
            "session_meta",
            {
                "id": PARENT,
                "session_id": PARENT,
                "cli_version": version,
                "cwd": "C:\\fixture",
            },
        ),
        row("turn_context", {"sandbox_policy": {"type": "read-only"}}),
        row(
            "response_item",
            {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "call-one",
                "arguments": json.dumps(
                    {
                        "agent_type": role,
                        "task_name": task_name,
                        "message": "Return the exact role marker.",
                    }
                ),
            },
        ),
        row(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call-one",
                "output": json.dumps({"task_name": task_path}),
            },
        ),
        row("event_msg", {"type": "task_complete"}),
    ]
    child = [
        row(
            "session_meta",
            {
                "id": CHILD,
                "session_id": PARENT,
                "parent_thread_id": PARENT,
                "forked_from_id": PARENT,
                "agent_role": role,
                "agent_path": task_path,
                "cli_version": version,
                "cwd": "C:\\fixture",
            },
        ),
        row(
            "session_meta",
            {
                "id": PARENT,
                "session_id": PARENT,
                "cli_version": version,
                "cwd": "C:\\fixture",
            },
        ),
        row(
            "event_msg",
            {
                "type": "thread_settings_applied",
                "thread_id": CHILD,
                "thread_settings": {
                    "model": settings["model"],
                    "reasoning_effort": settings["model_reasoning_effort"],
                },
            },
        ),
        row(
            "response_item",
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": instructions}],
            },
        ),
        row(
            "turn_context",
            {
                "model": settings["model"],
                "effort": settings["model_reasoning_effort"],
                "sandbox_policy": {"type": "read-only"},
            },
        ),
        row(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": f"LIVE_ROLE:{role}"}],
            },
        ),
        row("event_msg", {"type": "task_complete"}),
    ]
    return capture, manifest, parent, child


def run_case(
    capture: bytes,
    manifest: dict,
    parent: list[dict],
    child: list[dict],
    config: bytes | None = None,
    role: str = ROLE,
    rollout_variant: str = "v1",
) -> dict:
    return reconciliation.reconcile_rollouts(
        capture, manifest, encoded(parent), encoded(child),
        config if config is not None else role_config(role), role=role,
        rollout_variant=rollout_variant,
    )


def v2_fixture(role: str = "code_reviewer", version: str = "0.159.3") -> tuple[bytes, dict, list[dict], list[dict]]:
    capture, manifest, parent, child = fixture(role, version)
    child[0]["payload"].pop("forked_from_id")
    child[0]["payload"]["multi_agent_version"] = "v2"
    child.pop(2)  # v1-only thread_settings_applied event
    child.pop(1)  # v1-only duplicate parent session metadata
    return capture, manifest, parent, child


class ReconciliationTests(unittest.TestCase):
    def test_policy_rollout_matrix_preserves_exact_versions(self) -> None:
        for version, schemas in {"0.155.1": ("v1",), "0.157.1": ("v1", "v2"),
                                 "0.159.0": ("v1", "v2"), "0.159.3": ("v1", "v2"),
                                 "0.160.0": ()}.items():
            for variant in ("v1", "v2"):
                with self.subTest(version=version, variant=variant):
                    values = fixture(version=version) if variant == "v1" else v2_fixture("code_explorer", version)
                    result = run_case(*values, rollout_variant=variant)
                    self.assertEqual(variant in schemas, result["status"] == "CORRELATED")
                    if variant not in schemas:
                        self.assertIn("CAPTURE_MANIFEST_INVALID", result["reasonCodes"])
        capture, manifest, parent, child = fixture(version="0.159.3")
        parent[0]["payload"]["cli_version"] = "0.159.0"
        self.assertIn("PARENT_SESSION_MISMATCH", run_case(capture, manifest, parent, child)["reasonCodes"])

    def test_policy_gate_and_binding_errors_do_not_become_evidence_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compatibility").mkdir()
            path = root / "compatibility/codex-agents.json"
            document = json.loads((REPOSITORY_ROOT / "compatibility/codex-agents.json").read_bytes())
            document["versions"]["0.155.1"]["gates"]["capturedEvidence"] = False
            document["versions"]["0.155.1"]["rolloutSchemas"] = []
            path.write_text(json.dumps(document), encoding="utf-8")
            registry = compatibility.load_registry(root)
            capture, manifest, parent, child = fixture()
            result = reconciliation.reconcile_rollouts(capture, manifest, encoded(parent), encoded(child), CONFIG,
                                                       role=ROLE, source_root=root, policy=registry)
            self.assertIn("CAPTURE_MANIFEST_INVALID", result["reasonCodes"])
            with patch.object(reconciliation, "_rollout_rows", side_effect=AssertionError("foreign policy parsed rollout")):
                with self.assertRaises(compatibility.RegistryError):
                    reconciliation.reconcile_rollouts(capture, manifest, encoded(parent), encoded(child), CONFIG,
                                                       role=ROLE, policy=registry)
            path.write_bytes(b"corrupt")
            with self.assertRaises(compatibility.RegistryError):
                reconciliation.reconcile_rollouts(capture, manifest, encoded(parent), encoded(child), CONFIG,
                                                   role=ROLE, source_root=root)

    def test_cli_policy_failure_precedes_evidence_reads(self) -> None:
        args = ["--capture", "capture", "--manifest", "manifest", "--parent-rollout", "parent",
                "--child-rollout", "child", "--agent-config", "agent", "--role", ROLE]
        with patch.object(reconciliation, "load_registry", side_effect=compatibility.RegistryError("malformed_policy", "bad")), \
             patch.object(reconciliation, "_read_limited", side_effect=AssertionError("evidence read before policy")), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(2, reconciliation.main(args))

    def test_v2_context_correlates_only_as_separate_schema(self) -> None:
        capture, manifest, parent, child = v2_fixture()
        result = run_case(capture, manifest, parent, child, role="code_reviewer",
                          rollout_variant="v2")
        self.assertEqual("CORRELATED", result["status"])
        self.assertEqual(reconciliation.V2_EVIDENCE_SCHEMA, result["schema"])
        self.assertEqual("child-turn-context", result["settingsEvidence"])
        self.assertEqual("UNVERIFIED", run_case(
            capture, manifest, parent, child, role="code_reviewer"
        )["status"])

    def test_v2_rejects_ambiguous_identity_and_context(self) -> None:
        cases = (
            lambda child: child[0]["payload"].update({"parent_thread_id": CHILD}),
            lambda child: child[0]["payload"].update({"multi_agent_version": "v1"}),
            lambda child: child[0]["payload"].update({"forked_from_id": PARENT}),
            lambda child: child.insert(1, copy.deepcopy(child[0])),
            lambda child: child.insert(2, copy.deepcopy(child[2])),
            lambda child: child[2]["payload"].update({"model": "gpt-6-astra"}),
            lambda child: child[2]["payload"].update({"effort": "low"}),
            lambda child: child[2]["payload"].update({"sandbox_policy": {"type": "workspace-write"}}),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate.__code__.co_consts):
                capture, manifest, parent, child = v2_fixture()
                mutate(child)
                result = run_case(capture, manifest, parent, child,
                                  role="code_reviewer", rollout_variant="v2")
                self.assertEqual("UNVERIFIED", result["status"])

    def test_v2_rejects_wrong_profile_marker_and_older_cli(self) -> None:
        capture, manifest, parent, child = v2_fixture()
        self.assertEqual("UNVERIFIED", run_case(
            capture, manifest, parent, child, CONFIG, "code_reviewer", "v2"
        )["status"])
        child[3]["payload"]["content"][0]["text"] = "LIVE_ROLE:code_explorer"
        self.assertIn("CHILD_MARKER_MISSING", run_case(
            capture, manifest, parent, child, role="code_reviewer", rollout_variant="v2"
        )["reasonCodes"])
        capture, manifest, parent, child = v2_fixture()
        manifest["codexVersion"] = "0.155.1"
        self.assertIn("CAPTURE_MANIFEST_INVALID", run_case(
            capture, manifest, parent, child, role="code_reviewer", rollout_variant="v2"
        )["reasonCodes"])

    def test_v2_requires_instructions_before_child_turn_context(self) -> None:
        capture, manifest, parent, child = v2_fixture()
        child[1], child[2] = child[2], child[1]
        result = run_case(
            capture, manifest, parent, child,
            role="code_reviewer", rollout_variant="v2",
        )
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("ROLLOUT_ORDER_OR_CARDINALITY_INVALID", result["reasonCodes"])

    def test_v2_rejects_ambiguous_or_deep_json_inputs(self) -> None:
        capture, manifest, parent, child = v2_fixture()
        duplicate = encoded(parent).replace(
            b'"type": "session_meta"',
            b'"type": "session_meta", "type": "session_meta"', 1,
        )
        result = reconciliation.reconcile_rollouts(
            capture, manifest, duplicate, encoded(child), role_config("code_reviewer"),
            role="code_reviewer", rollout_variant="v2",
        )
        self.assertIn("ROLLOUT_MALFORMED", result["reasonCodes"])

        parent[2]["payload"]["arguments"] = parent[2]["payload"]["arguments"].replace(
            '"agent_type": ', '"agent_type": "code_reviewer", "agent_type": ', 1,
        )
        result = run_case(capture, manifest, parent, child,
                          role="code_reviewer", rollout_variant="v2")
        self.assertIn("SPAWN_CALL_MISMATCH", result["reasonCodes"])

        capture, manifest, parent, child = v2_fixture()
        blank_line = encoded(child).replace(b"\n", b"\n\n", 1)
        result = reconciliation.reconcile_rollouts(
            capture, manifest, encoded(parent), blank_line,
            role_config("code_reviewer"), role="code_reviewer", rollout_variant="v2",
        )
        self.assertIn("ROLLOUT_MALFORMED", result["reasonCodes"])

        deep: dict = {}
        for _ in range(reconciliation.V2_MAX_JSON_DEPTH + 2):
            deep = {"nested": deep}
        child[0]["payload"]["extra"] = deep
        result = run_case(capture, manifest, parent, child,
                          role="code_reviewer", rollout_variant="v2")
        self.assertIn("ROLLOUT_MALFORMED", result["reasonCodes"])

        duplicate_capture = capture + b'{"type":"turn.completed","type":"turn.completed"}\n'
        result = run_case(duplicate_capture, manifest, parent, child,
                          role="code_reviewer", rollout_variant="v2")
        self.assertEqual(["CAPTURE_MALFORMED"], result["reasonCodes"])

    def test_v2_cli_deep_manifest_fails_without_traceback(self) -> None:
        capture, _, parent, child = v2_fixture()
        sources = {
            "capture": capture,
            "manifest": (b'{"nested":' * 1100) + b"{}" + (b"}" * 1100),
            "parent": encoded(parent),
            "child": encoded(child),
            "config": role_config("code_reviewer"),
        }
        output = io.StringIO()
        with patch.object(reconciliation, "_read_limited", side_effect=lambda path: sources[path.name]):
            with redirect_stdout(output):
                exit_code = reconciliation.main([
                    "--capture", "capture",
                    "--manifest", "manifest",
                    "--parent-rollout", "parent",
                    "--child-rollout", "child",
                    "--agent-config", "config",
                    "--role", "code_reviewer",
                    "--rollout-variant", "v2",
                ])
        self.assertEqual(2, exit_code)
        result = json.loads(output.getvalue())
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertEqual(["INPUT_UNAVAILABLE_OR_MALFORMED"], result["reasonCodes"])

    def test_new_version_correlates_only_when_all_rollouts_match(self) -> None:
        capture, manifest, parent, child = fixture(version="0.157.1")
        self.assertEqual("CORRELATED", run_case(capture, manifest, parent, child)["status"])
        child[0]["payload"]["cli_version"] = "0.155.1"
        result = run_case(capture, manifest, parent, child)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("CHILD_SESSION_MISMATCH", result["reasonCodes"])

    def test_all_nine_pinned_roles_correlate_with_read_only_probe_context(self) -> None:
        self.assertEqual(set(ROLE_FILES), set(reconciliation.PINNED_ROLE_CONFIG_HASHES))
        self.assertEqual(set(ROLE_FILES), set(reconciliation.PINNED_ROLE_SANDBOX_MODES))
        for role in ROLE_FILES:
            with self.subTest(role=role):
                capture, manifest, parent, child = fixture(role)
                result = run_case(capture, manifest, parent, child, role=role)
                self.assertEqual("CORRELATED", result["status"])
                self.assertEqual(role, result["role"])
                self.assertEqual(
                    reconciliation.PINNED_ROLE_CONFIG_HASHES[role],
                    result["agentConfigHash"],
                )
                self.assertEqual("persistent-rollouts", result["source"])
                self.assertEqual(
                    reconciliation.PINNED_ROLE_SANDBOX_MODES[role],
                    result["configuredSandboxMode"],
                )
                self.assertEqual("read-only", result["sandboxMode"])
                self.assertEqual("MISSING_ATTRIBUTION", result["publicStreamAttribution"])
                self.assertFalse(result["fixtureReviewed"])

    def test_all_six_new_roles_reject_cross_role_profiles_and_markers(self) -> None:
        new_roles = set(ROLE_FILES) - {ROLE, "code_validator", "sol_architect"}
        for role in new_roles:
            with self.subTest(role=role):
                capture, manifest, parent, child = fixture(role)
                self.assertEqual(
                    "UNVERIFIED",
                    run_case(capture, manifest, parent, child, CONFIG, role)["status"],
                )
                child[5]["payload"]["content"][0]["text"] = f"LIVE_ROLE:{ROLE}"
                result = run_case(capture, manifest, parent, child, role=role)
                self.assertIn("CHILD_MARKER_MISSING", result["reasonCodes"])
                capture, manifest, parent, child = fixture(role)
                other = next(name for name in ROLE_FILES if name != role)
                self.assertEqual(
                    "UNVERIFIED",
                    run_case(capture, manifest, parent, child, role=other)["status"],
                )

    def test_declared_sandbox_must_match_pinned_role_and_effective_stays_read_only(self) -> None:
        for role in ROLE_FILES:
            with self.subTest(role=role):
                capture, manifest, parent, child = fixture(role)
                declared = reconciliation.PINNED_ROLE_SANDBOX_MODES[role]
                wrong = "workspace-write" if declared == "read-only" else "read-only"
                altered = role_config(role).replace(
                    f'sandbox_mode = "{declared}"'.encode(),
                    f'sandbox_mode = "{wrong}"'.encode(),
                )
                result = run_case(capture, manifest, parent, child, altered, role)
                self.assertIn("AGENT_CONFIG_INVALID", result["reasonCodes"])
                self.assertIn("AGENT_CONFIG_UNATTESTED", result["reasonCodes"])

                parent[1]["payload"]["sandbox_policy"]["type"] = "workspace-write"
                result = run_case(capture, manifest, parent, child, role=role)
                self.assertIn("SANDBOX_OR_CONTEXT_MISMATCH", result["reasonCodes"])
                capture, manifest, parent, child = fixture(role)
                child[4]["payload"]["sandbox_policy"]["type"] = "workspace-write"
                result = run_case(capture, manifest, parent, child, role=role)
                self.assertIn("SANDBOX_OR_CONTEXT_MISMATCH", result["reasonCodes"])

    def test_architect_role_correlates_with_its_pinned_profile(self) -> None:
        capture, manifest, parent, child = fixture("sol_architect")

        result = run_case(capture, manifest, parent, child, role="sol_architect")

        self.assertEqual("CORRELATED", result["status"])
        self.assertEqual("sol_architect", result["role"])
        self.assertEqual(
            reconciliation.PINNED_ROLE_CONFIG_HASHES["sol_architect"],
            result["agentConfigHash"],
        )

    def test_architect_cross_role_profile_and_rollout_mismatches_fail_closed(self) -> None:
        capture, manifest, parent, child = fixture("sol_architect")
        for case in (
            run_case(capture, manifest, parent, child, role=ROLE),
            run_case(capture, manifest, parent, child, CONFIG, "sol_architect"),
            run_case(capture, manifest, parent, child, VALIDATOR_CONFIG, "sol_architect"),
        ):
            self.assertEqual("UNVERIFIED", case["status"])

        capture, manifest, parent, child = fixture("sol_architect")
        parent[2]["payload"]["arguments"] = parent[2]["payload"][
            "arguments"
        ].replace("sol_architect", "code_explorer")
        self.assertIn(
            "SPAWN_CALL_MISMATCH",
            run_case(capture, manifest, parent, child, role="sol_architect")["reasonCodes"],
        )

        capture, manifest, parent, child = fixture("sol_architect")
        child[2]["payload"]["thread_settings"]["model"] = "gpt-6-astra"
        self.assertIn(
            "APPLIED_SETTINGS_MISMATCH",
            run_case(capture, manifest, parent, child, role="sol_architect")["reasonCodes"],
        )

        capture, manifest, parent, child = fixture("sol_architect")
        child[0]["payload"]["agent_path"] = "/root/probe_code_explorer"
        self.assertIn(
            "SPAWN_RESULT_MISMATCH",
            run_case(capture, manifest, parent, child, role="sol_architect")["reasonCodes"],
        )

        capture, manifest, parent, child = fixture("sol_architect")
        child[5]["payload"]["content"][0]["text"] = "LIVE_ROLE:code_explorer"
        self.assertIn(
            "CHILD_MARKER_MISSING",
            run_case(capture, manifest, parent, child, role="sol_architect")["reasonCodes"],
        )

    def test_validator_role_correlates_with_its_pinned_profile(self) -> None:
        capture, manifest, parent, child = fixture("code_validator")

        result = run_case(capture, manifest, parent, child, role="code_validator")

        self.assertEqual("CORRELATED", result["status"])
        self.assertEqual("code_validator", result["role"])
        self.assertEqual(
            reconciliation.PINNED_ROLE_CONFIG_HASHES["code_validator"],
            result["agentConfigHash"],
        )

    def test_validator_cross_role_and_profile_mismatches_fail_closed(self) -> None:
        capture, manifest, parent, child = fixture("code_validator")
        for case in (
            run_case(capture, manifest, parent, child, role=ROLE),
            run_case(capture, manifest, parent, child, CONFIG, "code_validator"),
        ):
            self.assertEqual("UNVERIFIED", case["status"])

        capture, manifest, parent, child = fixture("code_validator")
        parent[2]["payload"]["arguments"] = parent[2]["payload"][
            "arguments"
        ].replace("code_validator", "code_explorer")
        self.assertIn(
            "SPAWN_CALL_MISMATCH",
            run_case(capture, manifest, parent, child, role="code_validator")["reasonCodes"],
        )

        capture, manifest, parent, child = fixture("code_validator")
        child[2]["payload"]["thread_settings"]["model"] = "gpt-6-astra"
        self.assertIn(
            "APPLIED_SETTINGS_MISMATCH",
            run_case(capture, manifest, parent, child, role="code_validator")["reasonCodes"],
        )

    def test_correlates_exact_single_role_without_accepting_public_fixture(self) -> None:
        capture, manifest, parent, child = fixture()

        result = run_case(capture, manifest, parent, child)

        self.assertEqual("CORRELATED", result["status"])
        self.assertEqual([], result["reasonCodes"])
        self.assertEqual(PARENT, result["parentSessionId"])
        self.assertEqual(CHILD, result["childSessionId"])
        self.assertEqual("MISSING_ATTRIBUTION", result["publicStreamAttribution"])
        self.assertFalse(result["fixtureReviewed"])
        self.assertNotIn(INSTRUCTIONS, json.dumps(result))

    def test_capture_hash_and_public_event_mismatch_fail_closed(self) -> None:
        capture, manifest, parent, child = fixture()
        bad_manifest = copy.deepcopy(manifest)
        bad_manifest["captureHash"] = "0" * 64
        result = run_case(capture, bad_manifest, parent, child)
        self.assertIn("CAPTURE_HASH_MISMATCH", result["reasonCodes"])

        attributed = (
            capture
            + (
                json.dumps(
                    {
                        "type": "agent.spawned",
                        "agent_name": ROLE,
                        "child_session_id": CHILD,
                    }
                )
                + "\n"
            ).encode()
        )
        manifest["captureHash"] = reconciliation._sha256(attributed)
        result = run_case(attributed, manifest, parent, child)
        self.assertIn("PUBLIC_STREAM_UNEXPECTED", result["reasonCodes"])

    def test_parent_and_child_identity_mismatches_fail_closed(self) -> None:
        capture, manifest, parent, child = fixture()
        parent[0]["payload"]["id"] = CHILD
        self.assertIn(
            "PARENT_SESSION_MISMATCH",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        child[0]["payload"]["parent_thread_id"] = CHILD
        self.assertIn(
            "CHILD_SESSION_MISMATCH",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        child.insert(2, copy.deepcopy(child[1]))
        self.assertIn(
            "CHILD_SESSION_MISMATCH",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

    def test_spawn_selector_result_and_duplicate_fail_closed(self) -> None:
        capture, manifest, parent, child = fixture()
        args = json.loads(parent[2]["payload"]["arguments"])
        args["agent_type"] = "default"
        parent[2]["payload"]["arguments"] = json.dumps(args)
        self.assertIn(
            "SPAWN_CALL_MISMATCH",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        child[0]["payload"]["agent_path"] = "/root/other"
        self.assertIn(
            "SPAWN_RESULT_MISMATCH",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        parent.insert(3, copy.deepcopy(parent[2]))
        self.assertIn(
            "SPAWN_CALL_MISMATCH",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

    def test_profile_instruction_marker_and_terminal_mismatch_fail_closed(self) -> None:
        capture, manifest, parent, child = fixture()
        child[2]["payload"]["thread_settings"]["model"] = "gpt-6-astra"
        self.assertIn(
            "APPLIED_SETTINGS_MISMATCH",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        child[3]["payload"]["content"][0]["text"] = "generic instructions"
        self.assertIn(
            "DEVELOPER_INSTRUCTIONS_MISMATCH",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        child[5]["payload"]["content"][0]["text"] = "LIVE_CAPTURE_COMPLETE"
        self.assertIn(
            "CHILD_MARKER_MISSING",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        child.pop()
        self.assertIn(
            "ROLLOUT_TERMINAL_MISSING",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

    def test_malformed_and_unsanitized_inputs_do_not_leak_contents(self) -> None:
        capture, manifest, parent, child = fixture()
        malformed = reconciliation.reconcile_rollouts(
            capture, manifest, b"private secret without JSON\n", encoded(child),
            CONFIG, role=ROLE
        )
        self.assertEqual("UNVERIFIED", malformed["status"])
        self.assertNotIn("private secret", json.dumps(malformed))

        raw = capture + b'{"type":"error","token":"sensitive-value"}\n'
        manifest["captureHash"] = reconciliation._sha256(raw)
        result = run_case(raw, manifest, parent, child)
        self.assertIn("CAPTURE_NOT_SANITIZED", result["reasonCodes"])
        self.assertNotIn("sensitive-value", json.dumps(result))

    def test_unattested_config_cannot_echo_caller_supplied_model(self) -> None:
        capture, manifest, parent, child = fixture()
        altered = CONFIG.replace(b'gpt-6-luna', b'SYNTHETIC_SECRET_VALUE')
        child[2]["payload"]["thread_settings"]["model"] = "SYNTHETIC_SECRET_VALUE"
        child[4]["payload"]["model"] = "SYNTHETIC_SECRET_VALUE"

        result = run_case(capture, manifest, parent, child, altered)

        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("AGENT_CONFIG_UNATTESTED", result["reasonCodes"])
        self.assertNotIn("SYNTHETIC_SECRET_VALUE", json.dumps(result))

    def test_context_and_manifest_gates_remain_strict(self) -> None:
        capture, manifest, parent, child = fixture()
        manifest["requestedRoles"] = ["code_explorer", "implementer"]
        self.assertIn(
            "CAPTURE_MANIFEST_INVALID",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        child[4]["payload"]["sandbox_policy"]["type"] = "workspace-write"
        self.assertIn(
            "SANDBOX_OR_CONTEXT_MISMATCH",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        child[4]["payload"]["sandbox_policy"] = None
        self.assertIn(
            "SANDBOX_OR_CONTEXT_MISMATCH",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        manifest["reviewed"] = True
        self.assertIn(
            "CAPTURE_MANIFEST_INVALID",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        writable_config = CONFIG.replace(b'sandbox_mode = "read-only"', b'sandbox_mode = "workspace-write"')
        self.assertIn(
            "AGENT_CONFIG_INVALID",
            run_case(capture, manifest, parent, child, writable_config)["reasonCodes"],
        )

    def test_duplicate_final_and_reordered_events_fail_closed(self) -> None:
        capture, manifest, parent, child = fixture()
        child.insert(6, copy.deepcopy(child[5]))
        self.assertIn(
            "CHILD_MARKER_MISSING",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

        capture, manifest, parent, child = fixture()
        parent[2], parent[3] = parent[3], parent[2]
        self.assertIn(
            "ROLLOUT_ORDER_OR_CARDINALITY_INVALID",
            run_case(capture, manifest, parent, child)["reasonCodes"],
        )

    def test_extra_child_context_cannot_hide_writable_model_change(self) -> None:
        capture, manifest, parent, child = fixture()
        child.insert(
            5,
            row(
                "turn_context",
                {
                    "model": "gpt-6-astra",
                    "effort": "high",
                    "sandbox_policy": {"type": "workspace-write"},
                },
            ),
        )

        result = run_case(capture, manifest, parent, child)

        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("SANDBOX_OR_CONTEXT_MISMATCH", result["reasonCodes"])

    def test_inherited_writable_context_and_instruction_cannot_prove_child(self) -> None:
        capture, manifest, parent, child = fixture()
        child.insert(
            2,
            row(
                "turn_context",
                {
                    "model": "gpt-6-astra",
                    "sandbox_policy": {"type": "workspace-write"},
                },
            ),
        )
        result = run_case(capture, manifest, parent, child)
        self.assertIn("SANDBOX_OR_CONTEXT_MISMATCH", result["reasonCodes"])

        capture, manifest, parent, child = fixture()
        parent.insert(
            1,
            row(
                "response_item",
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": INSTRUCTIONS}],
                },
            ),
        )
        child.insert(
            2,
            row(
                "response_item",
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": INSTRUCTIONS}],
                },
            ),
        )
        child[4]["payload"]["content"][0]["text"] = "generic instructions"
        result = run_case(capture, manifest, parent, child)
        self.assertIn("DEVELOPER_INSTRUCTIONS_MISMATCH", result["reasonCodes"])


if __name__ == "__main__":
    unittest.main()
