from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sys
import unittest
import tempfile
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import validate_composite_fixture as composite
import codex_compatibility as compatibility
import codex_event_adapter as adapter
import reconcile_codex_rollouts as reconciliation
from tests.verifier.test_reconcile_codex_rollouts import (
    ROLE_FILES,
    encoded,
    fixture,
    role_config,
    v2_fixture,
)


def inputs(role: str = "code_explorer") -> tuple[bytes, bytes, bytes, bytes, bytes]:
    capture, manifest, parent, child = fixture(role)
    return (
        capture,
        json.dumps(manifest, sort_keys=True).encode(),
        encoded(parent),
        encoded(child),
        role_config(role),
    )


def assess(
    values: tuple[bytes, bytes, bytes, bytes, bytes],
    review: bytes | None = None,
    role: str = "code_explorer",
) -> dict:
    return composite.validate_composite_fixture(*values, review_bytes=review, role=role)


def attestation(
    values: tuple[bytes, bytes, bytes, bytes, bytes], role: str = "code_explorer"
) -> bytes:
    pending = assess(values, role=role)
    manifest = json.loads(values[1])
    captured_at = datetime.fromisoformat(manifest["capturedAt"])
    reviewed_at = captured_at + timedelta(seconds=1)
    review = {
        "schema": composite.REVIEW_SCHEMA,
        "decision": "APPROVE_ONE_ROLE",
        "scope": role,
        "reviewer": "/root/synthetic_reviewer",
        "reviewedAt": reviewed_at.astimezone(timezone.utc).isoformat(),
        "linkageBasis": "task-path-and-child-parent-metadata",
        "publicSchema": "codex-cli-jsonl/v1",
        "rolloutSchema": "codex-rollout-evidence/v1",
        "captureHash": pending["captureHash"],
        "manifestHash": pending["manifestHash"],
        "parentRolloutHash": pending["parentRolloutHash"],
        "childRolloutHash": pending["childRolloutHash"],
        "agentConfigHash": pending["agentConfigHash"],
        "parentSessionId": pending["parentSessionId"],
        "childSessionId": pending["childSessionId"],
    }
    return json.dumps(review, sort_keys=True).encode()


class CompositeFixtureTests(unittest.TestCase):
    def test_same_snapshot_and_source_are_forwarded_to_both_evidence_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compatibility").mkdir()
            path = root / "compatibility/codex-agents.json"
            path.write_bytes((REPOSITORY_ROOT / "compatibility/codex-agents.json").read_bytes())
            registry = compatibility.load_registry(root)
            values = inputs()
            path.write_bytes(b"changed snapshot bytes")
            with patch.object(composite, "load_registry", side_effect=AssertionError("snapshot reload")), \
                 patch.object(adapter, "load_registry", side_effect=AssertionError("adapter reload")), \
                 patch.object(reconciliation, "load_registry", side_effect=AssertionError("rollout reload")), \
                 patch.object(composite, "validate_captured_fixture", wraps=composite.validate_captured_fixture) as public, \
                 patch.object(composite, "reconcile_rollouts", wraps=composite.reconcile_rollouts) as rollout:
                result = composite.validate_composite_fixture(*values, source_root=root, policy=registry)
            self.assertEqual("REVIEW_REQUIRED", result["status"])
            for nested in (public, rollout):
                self.assertIs(registry, nested.call_args.kwargs["policy"])
                self.assertEqual(root, nested.call_args.kwargs["source_root"])
            with patch.object(composite, "_json_object", side_effect=AssertionError("foreign policy parsed evidence")):
                with self.assertRaises(compatibility.RegistryError):
                    composite.validate_composite_fixture(*values, policy=registry)

    def test_cli_invalid_policy_precedes_input_reads(self) -> None:
        args = ["--capture", "capture", "--manifest", "manifest", "--parent-rollout", "parent",
                "--child-rollout", "child", "--agent-config", "agent"]
        with patch.object(composite, "load_registry", side_effect=compatibility.RegistryError("malformed_policy", "bad")), \
             patch.object(composite, "_read_limited", side_effect=AssertionError("policy failure read evidence")), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(2, composite.main(args))

    def test_v2_requires_separate_hash_bound_review(self) -> None:
        capture, manifest, parent, child = v2_fixture()
        values = (
            capture,
            json.dumps(manifest, sort_keys=True).encode(),
            encoded(parent),
            encoded(child),
            role_config("code_reviewer"),
        )
        pending = composite.validate_composite_fixture(
            *values, role="code_reviewer", rollout_variant="v2"
        )
        self.assertEqual("REVIEW_REQUIRED", pending["status"])
        self.assertEqual(composite.V2_COMPOSITE_SCHEMA, pending["schema"])
        self.assertEqual("child-turn-context", pending["settingsEvidence"])
        self.assertEqual("MISSING_ATTRIBUTION", pending["publicStreamAttribution"])
        self.assertFalse(pending["runtimeValidated"])
        self.assertEqual(
            ["COMPOSITE_ROLLOUT_GATE_FAILED"],
            composite.validate_composite_fixture(*values, role="code_reviewer")["reasonCodes"],
        )

        review = json.loads(attestation(inputs("code_reviewer"), "code_reviewer"))
        review.update({
            "schema": composite.V2_REVIEW_SCHEMA,
            "linkageBasis": "task-path-child-parent-and-turn-context",
            "rolloutSchema": composite.V2_EVIDENCE_SCHEMA,
            **{key: pending[key] for key in (
                "captureHash", "manifestHash", "parentRolloutHash", "childRolloutHash",
                "agentConfigHash", "parentSessionId", "childSessionId",
            )},
        })
        accepted = composite.validate_composite_fixture(
            *values, review_bytes=json.dumps(review).encode(),
            role="code_reviewer", rollout_variant="v2",
        )
        self.assertEqual("ACCEPTED_ONE_ROLE", accepted["status"])
        self.assertFalse(accepted["runtimeValidated"])
        for field in ("schema", "linkageBasis", "rolloutSchema", "manifestHash", "childSessionId"):
            with self.subTest(field=field):
                altered = dict(review)
                altered[field] = "wrong"
                result = composite.validate_composite_fixture(
                    *values, review_bytes=json.dumps(altered).encode(),
                    role="code_reviewer", rollout_variant="v2",
                )
                self.assertEqual(["COMPOSITE_REVIEW_INVALID"], result["reasonCodes"])

    def test_v2_rejects_deep_and_ambiguous_manifest_and_review(self) -> None:
        capture, manifest, parent, child = v2_fixture()
        values = (capture, json.dumps(manifest).encode(), encoded(parent),
                  encoded(child), role_config("code_reviewer"))
        duplicate = values[1][:-1] + b',"reviewed":false}'
        self.assertEqual(
            ["COMPOSITE_MANIFEST_MALFORMED"],
            composite.validate_composite_fixture(
                values[0], duplicate, *values[2:], role="code_reviewer",
                rollout_variant="v2",
            )["reasonCodes"],
        )
        deep = dict(manifest)
        nested: object = "x"
        for _ in range(35):
            nested = [nested]
        deep["stderr"] = nested
        result = composite.validate_composite_fixture(
            values[0], json.dumps(deep).encode(), *values[2:],
            role="code_reviewer", rollout_variant="v2",
        )
        self.assertEqual(["COMPOSITE_MANIFEST_MALFORMED"], result["reasonCodes"])

    def test_all_nine_roles_require_separate_exact_review(self) -> None:
        for role in ROLE_FILES:
            with self.subTest(role=role):
                values = inputs(role)
                pending = assess(values, role=role)
                self.assertEqual("REVIEW_REQUIRED", pending["status"])
                self.assertEqual(role, pending["scope"])
                self.assertEqual("persistent-rollouts", pending["rolloutSource"])
                self.assertEqual("MISSING_ATTRIBUTION", pending["publicStreamAttribution"])
                self.assertFalse(pending["runtimeValidated"])
                accepted = assess(values, attestation(values, role), role=role)
                self.assertEqual("ACCEPTED_ONE_ROLE", accepted["status"])
                self.assertEqual(role, accepted["scope"])
                self.assertEqual("persistent-rollouts", accepted["rolloutSource"])
                self.assertFalse(accepted["runtimeValidated"])

    def test_six_new_role_reviews_are_hash_bound_and_secret_safe(self) -> None:
        for role in set(ROLE_FILES) - {"code_explorer", "code_validator", "sol_architect"}:
            with self.subTest(role=role):
                values = inputs(role)
                for field in (
                    "scope",
                    "captureHash",
                    "manifestHash",
                    "parentRolloutHash",
                    "childRolloutHash",
                    "agentConfigHash",
                    "parentSessionId",
                    "childSessionId",
                ):
                    with self.subTest(field=field):
                        review = json.loads(attestation(values, role))
                        review[field] = "invalid"
                        self.assertEqual(
                            ["COMPOSITE_REVIEW_INVALID"],
                            assess(values, json.dumps(review).encode(), role)["reasonCodes"],
                        )
                review = json.loads(attestation(values, role))
                review["token"] = "SYNTHETIC_SECRET_VALUE"
                result = assess(values, json.dumps(review).encode(), role)
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertNotIn("SYNTHETIC_SECRET_VALUE", json.dumps(result))

    def test_composite_rejects_unqualified_rollout_source(self) -> None:
        values = inputs()
        evidence = composite.reconcile_rollouts(*(
            values[0], json.loads(values[1]), values[2], values[3], values[4]
        ), role="code_explorer")
        evidence["source"] = "caller-supplied"
        with patch.object(composite, "reconcile_rollouts", return_value=evidence):
            result = assess(values)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertEqual(["COMPOSITE_ROLLOUT_GATE_FAILED"], result["reasonCodes"])

    def test_architect_requires_its_own_review_and_scope(self) -> None:
        values = inputs("sol_architect")
        pending = assess(values, role="sol_architect")
        self.assertEqual("REVIEW_REQUIRED", pending["status"])
        self.assertEqual("sol_architect", pending["scope"])

        accepted = assess(
            values, attestation(values, "sol_architect"), role="sol_architect"
        )
        self.assertEqual("ACCEPTED_ONE_ROLE", accepted["status"])
        self.assertEqual("sol_architect", accepted["scope"])
        self.assertFalse(accepted["runtimeValidated"])

        wrong_scope = json.loads(attestation(values, "sol_architect"))
        wrong_scope["scope"] = "code_validator"
        rejected = assess(
            values, json.dumps(wrong_scope).encode(), role="sol_architect"
        )
        self.assertEqual(["COMPOSITE_REVIEW_INVALID"], rejected["reasonCodes"])

    def test_architect_inputs_cannot_pass_as_other_roles(self) -> None:
        values = inputs("sol_architect")
        for role in ("code_explorer", "code_validator"):
            with self.subTest(role=role):
                self.assertEqual(
                    ["COMPOSITE_PUBLIC_GATE_FAILED"],
                    assess(values, role=role)["reasonCodes"],
                )

    def test_architect_config_model_path_and_marker_tampering_fail_closed(self) -> None:
        values = inputs("sol_architect")
        wrong_config = (values[0], values[1], values[2], values[3], role_config("code_validator"))
        self.assertEqual(
            ["COMPOSITE_ROLLOUT_GATE_FAILED"],
            assess(wrong_config, role="sol_architect")["reasonCodes"],
        )

        for index, field, value in (
            (2, "model", "gpt-6-astra"),
            (0, "agent_path", "/root/probe_code_explorer"),
            (5, "marker", "LIVE_ROLE:code_explorer"),
        ):
            with self.subTest(field=field):
                child = [json.loads(line) for line in values[3].splitlines()]
                if field == "model":
                    child[index]["payload"]["thread_settings"][field] = value
                elif field == "marker":
                    child[index]["payload"]["content"][0]["text"] = value
                else:
                    child[index]["payload"][field] = value
                altered_child = encoded(child)
                altered = (values[0], values[1], values[2], altered_child, values[4])
                self.assertEqual(
                    ["COMPOSITE_ROLLOUT_GATE_FAILED"],
                    assess(altered, role="sol_architect")["reasonCodes"],
                )

    def test_validator_requires_its_own_review_and_scope(self) -> None:
        values = inputs("code_validator")
        pending = assess(values, role="code_validator")
        self.assertEqual("REVIEW_REQUIRED", pending["status"])
        self.assertEqual("code_validator", pending["scope"])

        accepted = assess(
            values, attestation(values, "code_validator"), role="code_validator"
        )
        self.assertEqual("ACCEPTED_ONE_ROLE", accepted["status"])
        self.assertEqual("code_validator", accepted["scope"])
        self.assertFalse(accepted["runtimeValidated"])

        wrong_scope = json.loads(attestation(values, "code_validator"))
        wrong_scope["scope"] = "code_explorer"
        rejected = assess(
            values, json.dumps(wrong_scope).encode(), role="code_validator"
        )
        self.assertEqual(["COMPOSITE_REVIEW_INVALID"], rejected["reasonCodes"])

    def test_validator_inputs_cannot_pass_as_explorer_or_unknown_role(self) -> None:
        values = inputs("code_validator")
        self.assertEqual(
            ["COMPOSITE_PUBLIC_GATE_FAILED"], assess(values)["reasonCodes"]
        )
        self.assertEqual(
            ["COMPOSITE_ROLE_UNSUPPORTED"],
            assess(values, role="not_a_role")["reasonCodes"],
        )

    def test_machine_evidence_alone_requires_separate_review(self) -> None:
        result = assess(inputs())

        self.assertEqual("REVIEW_REQUIRED", result["status"])
        self.assertEqual(["COMPOSITE_REVIEW_REQUIRED"], result["reasonCodes"])
        self.assertEqual("MISSING_ATTRIBUTION", result["publicStreamAttribution"])
        self.assertEqual("CORRELATED", result["rolloutCorrelation"])
        self.assertFalse(result["originalManifestReviewed"])
        self.assertFalse(result["runtimeValidated"])

    def test_exact_hash_bound_review_accepts_only_one_role(self) -> None:
        values = inputs()
        result = assess(values, attestation(values))

        self.assertEqual("ACCEPTED_ONE_ROLE", result["status"])
        self.assertEqual([], result["reasonCodes"])
        self.assertEqual("code_explorer", result["scope"])
        self.assertFalse(result["runtimeValidated"])
        self.assertEqual(
            "task-path-and-child-parent-metadata", result["linkageBasis"]
        )

    def test_review_hashes_and_scope_are_exact(self) -> None:
        values = inputs()
        original = json.loads(attestation(values))
        for field in (
            "captureHash",
            "manifestHash",
            "parentRolloutHash",
            "childRolloutHash",
            "agentConfigHash",
            "parentSessionId",
            "childSessionId",
            "scope",
            "linkageBasis",
        ):
            with self.subTest(field=field):
                changed = dict(original)
                changed[field] = "other"
                result = assess(values, json.dumps(changed).encode())
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertIn("COMPOSITE_REVIEW_INVALID", result["reasonCodes"])

    def test_missing_or_untrusted_review_is_not_acceptance(self) -> None:
        values = inputs()
        original = json.loads(attestation(values))
        original["decision"] = "COMMENT"
        self.assertEqual(
            "UNVERIFIED", assess(values, json.dumps(original).encode())["status"]
        )
        original = json.loads(attestation(values))
        original["reviewer"] = "anonymous"
        self.assertEqual(
            "UNVERIFIED", assess(values, json.dumps(original).encode())["status"]
        )
        original = json.loads(attestation(values))
        original["reviewedAt"] = "2020-01-01T00:00:00+00:00"
        self.assertEqual(
            "UNVERIFIED", assess(values, json.dumps(original).encode())["status"]
        )

    def test_duplicate_json_keys_and_extra_sensitive_field_fail_closed(self) -> None:
        values = inputs()
        review = attestation(values)
        duplicate = review[:-1] + b',"decision":"APPROVE_ONE_ROLE"}'
        result = assess(values, duplicate)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("COMPOSITE_REVIEW_INVALID", result["reasonCodes"])

        changed = json.loads(review)
        changed["token"] = "SYNTHETIC_SECRET_VALUE"
        result = assess(values, json.dumps(changed).encode())
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertNotIn("SYNTHETIC_SECRET_VALUE", json.dumps(result))

    def test_unknown_or_secret_bearing_manifest_fields_fail_closed(self) -> None:
        values = inputs()
        for key, value in (
            ("password", "SYNTHETIC_SECRET_VALUE"),
            ("extraMetadata", "harmless"),
            ("stderr", "api_key=SYNTHETIC_SECRET_VALUE"),
        ):
            with self.subTest(key=key):
                manifest = json.loads(values[1])
                manifest[key] = value
                altered = (values[0], json.dumps(manifest).encode(), *values[2:])
                result = assess(altered)
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertEqual(
                    ["COMPOSITE_MANIFEST_UNSAFE"], result["reasonCodes"]
                )
                self.assertNotIn("SYNTHETIC_SECRET_VALUE", json.dumps(result))

    def test_public_stream_and_rollout_failures_cannot_be_reviewed_away(self) -> None:
        values = inputs()
        review = attestation(values)
        changed_manifest = json.loads(values[1])
        changed_manifest["reviewed"] = True
        altered = (values[0], json.dumps(changed_manifest).encode(), *values[2:])
        result = assess(altered, review)
        self.assertIn("COMPOSITE_PUBLIC_GATE_FAILED", result["reasonCodes"])

        child = json.loads(values[3].splitlines()[0])
        child["payload"]["agent_role"] = "default"
        lines = values[3].splitlines()
        lines[0] = json.dumps(child).encode()
        altered_child = b"\n".join(lines) + b"\n"
        altered = (values[0], values[1], values[2], altered_child, values[4])
        result = assess(altered, review)
        self.assertIn("COMPOSITE_ROLLOUT_GATE_FAILED", result["reasonCodes"])

    def test_public_error_event_blocks_even_if_hashes_are_updated(self) -> None:
        values = inputs()
        review = attestation(values)
        capture = values[0] + b'{"type":"error"}\n'
        manifest = json.loads(values[1])
        manifest["captureHash"] = composite._sha256(capture)
        altered = (capture, json.dumps(manifest).encode(), *values[2:])
        result = assess(altered, review)
        self.assertIn("COMPOSITE_PUBLIC_GATE_FAILED", result["reasonCodes"])


if __name__ == "__main__":
    unittest.main()
