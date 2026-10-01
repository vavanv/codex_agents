from __future__ import annotations

from contextlib import ExitStack, contextmanager, redirect_stdout
import copy
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import capture_live_event as capture_module
import live_validation_support as live
import validate_l5_rollout_matrix as matrix
from tests.verifier.test_reconcile_codex_rollouts import encoded, fixture, role_config, v2_fixture
from tests.verifier.test_validate_composite_fixture import assess, attestation


def session_id(value: int) -> str:
    return f"00000000-0000-4000-8000-{value:012x}"


class L5RolloutMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.temp = Path(cls.temporary.name)
        cls.root = Path(live.create_fixture(
            REPOSITORY_ROOT, allow_live=True, temp_base=cls.temp, timeout=30,
        )["runRoot"])
        cls.evidence_root = cls.temp / "external"
        cls.evidence_root.mkdir()
        cls.index_path = cls.temp / "rollout-matrix.json"
        cls.entries: list[dict[str, str]] = []
        parents = {role: session_id(index + 1001) for index, role in enumerate(capture_module.ROLE_NAMES)}

        def fake_capture(
            fixture_path: Path, results: Path, timeout: int, command: str,
            roles: tuple[str, ...], ephemeral: bool, sqlite_home: Path,
            **kwargs: object,
        ) -> tuple[int, dict[str, object]]:
            del fixture_path, timeout, command, ephemeral, sqlite_home
            role = roles[0]
            stream = (
                json.dumps({"type": "thread.started", "thread_id": parents[role]})
                + "\n" + json.dumps({"type": "turn.completed"}) + "\n"
            )
            capture_module._persist_capture(
                results, stream, "", 0, False, roles, False, True, True,
                str(kwargs["output_prefix"]),
            )
            return 0, {"status": "CAPTURED"}

        with patch.object(capture_module, "capture", side_effect=fake_capture):
            for index, role in enumerate(capture_module.ROLE_NAMES):
                code, output = capture_module.capture_owned_run(cls.root, 30, "mock-codex", role)
                if code != 0 or output["status"] != "OWNED_CAPTURED":
                    raise AssertionError("Synthetic capture failed")
                sidecars = list((cls.root / "results").glob(
                    f"capture-{role}-*.snapshot-evidence.json"
                ))
                if len(sidecars) != 1:
                    raise AssertionError("Synthetic sidecar identity is ambiguous")
                sidecar_path = sidecars[0]
                sidecar = json.loads(sidecar_path.read_bytes())
                capture_id = sidecar["captureId"]
                capture = (cls.root / sidecar["files"]["capture"]["path"]).read_bytes()
                manifest = (cls.root / sidecar["files"]["manifest"]["path"]).read_bytes()
                _, _, parent, child = fixture(role, version="0.159.0")
                original_parent = session_id(1)
                original_child = session_id(2)
                parent_bytes = encoded(parent).replace(original_parent.encode(), parents[role].encode())
                child_bytes = (
                    encoded(child)
                    .replace(original_parent.encode(), parents[role].encode())
                    .replace(original_child.encode(), session_id(index + 2001).encode())
                )
                values = (capture, manifest, parent_bytes, child_bytes, role_config(role))
                pending = assess(values, role=role)
                if pending["status"] != "REVIEW_REQUIRED":
                    raise AssertionError((role, pending["reasonCodes"]))
                composite_review = attestation(values, role)
                role_dir = cls.evidence_root / role
                role_dir.mkdir()
                for name, content in (
                    ("parent.jsonl", parent_bytes),
                    ("child.jsonl", child_bytes),
                    ("agent.toml", role_config(role)),
                    ("composite-review.json", composite_review),
                ):
                    (role_dir / name).write_bytes(content)
                composite = matrix.validate_composite_fixture(
                    capture, manifest, parent_bytes, child_bytes,
                    role_config(role), review_bytes=composite_review, role=role,
                )
                if composite["status"] != "ACCEPTED_ONE_ROLE":
                    raise AssertionError("Synthetic composite did not validate")
                reviewed_at = max(
                    matrix._utc(sidecar["completedAt"]),
                    datetime.fromisoformat(json.loads(composite_review)["reviewedAt"]),
                ) + timedelta(seconds=1)
                review = {
                    "schema": matrix.LINK_REVIEW_SCHEMA,
                    "decision": "APPROVE_L5_LINK",
                    "scope": role,
                    "reviewer": "/root/synthetic_l5_reviewer",
                    "reviewedAt": reviewed_at.astimezone(timezone.utc).isoformat(),
                    "runId": sidecar["runId"],
                    "captureId": capture_id,
                    "sidecarHash": matrix._sha256(sidecar_path.read_bytes()),
                    "compositeReviewHash": matrix._sha256(composite_review),
                    "captureHash": composite["captureHash"],
                    "manifestHash": composite["manifestHash"],
                    "parentRolloutHash": composite["parentRolloutHash"],
                    "childRolloutHash": composite["childRolloutHash"],
                    "agentConfigHash": composite["agentConfigHash"],
                    "parentSessionId": composite["parentSessionId"],
                    "childSessionId": composite["childSessionId"],
                    "rolloutSource": "persistent-rollouts",
                    "publicStreamAttribution": "MISSING_ATTRIBUTION",
                    "originalManifestReviewed": False,
                    "runtimeValidated": False,
                }
                (role_dir / "l5-review.json").write_text(json.dumps(review), encoding="utf-8")
                cls.entries.append({
                    "role": role,
                    "captureId": capture_id,
                    "sidecar": sidecar_path.relative_to(cls.root).as_posix(),
                    **{key: f"{role}/{name}" for key, name in matrix.EXTERNAL_NAMES.items()},
                })
        _, marker = live.validate_marker(cls.root)
        cls.run_id = marker["runId"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.index = {
            "schema": matrix.SCHEMA,
            "runId": self.run_id,
            "codexVersion": "0.159.0",
            "entries": copy.deepcopy(self.entries),
        }
        self.write_index()

    def write_index(self) -> None:
        self.index_path.write_text(json.dumps(self.index), encoding="utf-8")

    @contextmanager
    def changed_file(self, path: Path, replacement: bytes):
        original = path.read_bytes()
        path.write_bytes(replacement)
        try:
            yield
        finally:
            path.write_bytes(original)

    def assert_invalid(self) -> None:
        self.write_index()
        with self.assertRaises((OSError, ValueError, live.LiveValidationError)):
            matrix.validate_l5_rollout_matrix(
                self.root, self.index_path, self.evidence_root
            )

    @contextmanager
    def mixed_v2_matrix(self):
        """Supply eight v1 roles and one v2 reviewer without changing shared fixtures."""
        with ExitStack() as stack:
            self.index["schema"] = matrix.V2_SCHEMA
            for entry in self.index["entries"]:
                entry["rolloutVariant"] = "v2" if entry["role"] == "code_reviewer" else "v1"
                link_path = self.evidence_root / entry["l5Review"]
                link = json.loads(link_path.read_bytes())
                link.update({
                    "schema": matrix.V2_LINK_REVIEW_SCHEMA,
                    "rolloutVariant": entry["rolloutVariant"],
                    "compositeSchema": f"codex-composite-fixture/{entry['rolloutVariant']}",
                    "settingsEvidence": (
                        "child-turn-context" if entry["role"] == "code_reviewer"
                        else "thread-settings-applied"
                    ),
                })
                if entry["role"] == "code_reviewer":
                    capture, manifest, parent, child = v2_fixture()
                    del capture, manifest
                    old_parent = session_id(1)
                    old_child = session_id(2)
                    role_index = list(capture_module.ROLE_NAMES).index("code_reviewer")
                    parent_id = session_id(role_index + 1001)
                    child_id = session_id(role_index + 2001)
                    parent_bytes = encoded(parent).replace(old_parent.encode(), parent_id.encode())
                    child_bytes = (encoded(child).replace(old_parent.encode(), parent_id.encode())
                                   .replace(old_child.encode(), child_id.encode()))
                    parent_path = self.evidence_root / entry["parentRollout"]
                    child_path = self.evidence_root / entry["childRollout"]
                    stack.enter_context(self.changed_file(parent_path, parent_bytes))
                    stack.enter_context(self.changed_file(child_path, child_bytes))
                    sidecar = json.loads((self.root / entry["sidecar"]).read_bytes())
                    capture_bytes = (self.root / sidecar["files"]["capture"]["path"]).read_bytes()
                    manifest_bytes = (self.root / sidecar["files"]["manifest"]["path"]).read_bytes()
                    values = (capture_bytes, manifest_bytes, parent_bytes, child_bytes,
                              role_config("code_reviewer"))
                    pending = matrix.validate_composite_fixture(
                        *values, role="code_reviewer", rollout_variant="v2"
                    )
                    if pending["status"] != "REVIEW_REQUIRED":
                        raise AssertionError("Synthetic v2 reviewer did not correlate")
                    composite_path = self.evidence_root / entry["compositeReview"]
                    composite_review = json.loads(composite_path.read_bytes())
                    composite_review.update({
                        "schema": "codex-composite-review/v2",
                        "linkageBasis": "task-path-child-parent-and-turn-context",
                        "rolloutSchema": "codex-rollout-evidence/v2",
                        **{key: pending[key] for key in (
                            "captureHash", "manifestHash", "parentRolloutHash",
                            "childRolloutHash", "agentConfigHash", "parentSessionId",
                            "childSessionId",
                        )},
                    })
                    composite_bytes = json.dumps(composite_review).encode()
                    stack.enter_context(self.changed_file(composite_path, composite_bytes))
                    link.update({
                        "compositeReviewHash": matrix._sha256(composite_bytes),
                        **{key: pending[key] for key in (
                            "captureHash", "manifestHash", "parentRolloutHash",
                            "childRolloutHash", "agentConfigHash", "parentSessionId",
                            "childSessionId",
                        )},
                    })
                stack.enter_context(self.changed_file(link_path, json.dumps(link).encode()))
            self.write_index()
            yield

    def test_v2_mixed_matrix_requires_explicit_reviewed_variants(self) -> None:
        with self.mixed_v2_matrix():
            self.assertEqual({
                "status": "ROLLOUT_MATRIX_CORRELATED",
                "roleCount": 9,
                "codexVersion": "0.159.0",
                "rolloutSource": "persistent-rollouts",
                "publicStreamAttribution": "MISSING_ATTRIBUTION",
                "l5Accepted": False,
                "runtimeValidated": False,
                "schema": matrix.V2_SCHEMA,
                "variantCounts": {"v1": 8, "v2": 1},
            }, matrix.validate_l5_rollout_matrix(
                self.root, self.index_path, self.evidence_root
            ))
            reviewer = next(entry for entry in self.index["entries"]
                            if entry["role"] == "code_reviewer")
            reviewer["rolloutVariant"] = "v1"
            self.assert_invalid()
            reviewer["rolloutVariant"] = "v2"
            link_path = self.evidence_root / reviewer["l5Review"]
            link = json.loads(link_path.read_bytes())
            for field in ("rolloutVariant", "compositeSchema", "settingsEvidence",
                          "compositeReviewHash"):
                with self.subTest(field=field):
                    changed = {**link, field: "wrong"}
                    with self.changed_file(link_path, json.dumps(changed).encode()):
                        self.assert_invalid()

    def test_v2_matrix_rejects_old_version_and_v1_downgrade(self) -> None:
        with self.mixed_v2_matrix():
            self.index["codexVersion"] = "0.155.1"
            self.assert_invalid()
            self.index["codexVersion"] = "0.157.1"
            self.index["schema"] = matrix.SCHEMA
            self.assert_invalid()

    def test_v2_matrix_rejects_non_string_discriminators(self) -> None:
        with self.mixed_v2_matrix():
            self.index["schema"] = [matrix.V2_SCHEMA]
            self.assert_invalid()
            self.index["schema"] = matrix.V2_SCHEMA
            self.index["entries"][0]["rolloutVariant"] = ["v1"]
            self.assert_invalid()

    def test_v2_cli_rejects_hash_consistent_malformed_manifest_without_traceback(self) -> None:
        with self.mixed_v2_matrix():
            entry = self.index["entries"][0]
            sidecar_path = self.root / entry["sidecar"]
            sidecar = json.loads(sidecar_path.read_bytes())
            manifest_path = self.root / sidecar["files"]["manifest"]["path"]
            manifest = json.loads(manifest_path.read_bytes())
            manifest["codexVersion"] = []
            changed_manifest = json.dumps(manifest).encode()
            sidecar["files"]["manifest"]["sha256"] = hashlib.sha256(
                changed_manifest
            ).hexdigest()
            with self.changed_file(manifest_path, changed_manifest):
                with self.changed_file(sidecar_path, json.dumps(sidecar).encode()):
                    output = StringIO()
                    with redirect_stdout(output):
                        code = matrix.main([
                            "--run-root", str(self.root),
                            "--index", str(self.index_path),
                            "--evidence-root", str(self.evidence_root),
                        ])
                    self.assertEqual(2, code)
                    self.assertEqual({
                        "status": "ROLLOUT_MATRIX_INVALID",
                        "l5Accepted": False,
                        "runtimeValidated": False,
                    }, json.loads(output.getvalue()))
                    self.assertNotIn(str(self.root), output.getvalue())

    def test_nine_distinct_reviewed_roles_are_correlated_but_not_l5_accepted(self) -> None:
        self.assertEqual({
            "status": "ROLLOUT_MATRIX_CORRELATED",
            "roleCount": 9,
            "codexVersion": "0.159.0",
            "rolloutSource": "persistent-rollouts",
            "publicStreamAttribution": "MISSING_ATTRIBUTION",
            "l5Accepted": False,
            "runtimeValidated": False,
        }, matrix.validate_l5_rollout_matrix(
            self.root, self.index_path, self.evidence_root,
        ))

    def test_index_version_must_match_captures(self) -> None:
        for version in ("0.155.1", "0.0.0"):
            self.index["codexVersion"] = version
            self.assert_invalid()

    def test_one_mixed_version_capture_is_rejected(self) -> None:
        sidecar = json.loads((self.root / self.entries[0]["sidecar"]).read_text(encoding="utf-8"))
        manifest = (self.root / sidecar["files"]["manifest"]["path"]).read_bytes()
        original_json = matrix._safe_json

        def mixed_manifest(raw: bytes) -> dict[str, object]:
            value = original_json(raw)
            if raw == manifest:
                value = {**value, "codexVersion": "0.155.1"}
            return value

        with patch.object(matrix, "_safe_json", side_effect=mixed_manifest):
            self.assert_invalid()

    def test_missing_duplicate_or_spliced_role_rejected(self) -> None:
        entries = copy.deepcopy(self.entries)
        for variant in (
            entries[:-1],
            entries[:-1] + [copy.deepcopy(entries[0])],
            entries[:-1] + [{**entries[-1], "role": entries[0]["role"]}],
            entries[:-1] + [{**entries[-1], "sidecar": entries[0]["sidecar"]}],
            entries[:-1] + [{**entries[-1], "childRollout": entries[0]["childRollout"]}],
        ):
            self.index["entries"] = variant
            self.assert_invalid()

    def test_reused_parent_or_child_identity_rejected(self) -> None:
        for key in ("parentSessionId", "childSessionId"):
            target = self.evidence_root / self.entries[-1]["l5Review"]
            review = json.loads(target.read_bytes())
            source = json.loads((self.evidence_root / self.entries[0]["l5Review"]).read_bytes())
            review[key] = source[key]
            with self.changed_file(target, json.dumps(review).encode()):
                self.assert_invalid()

    def test_duplicate_correlated_session_ids_reach_uniqueness_guard(self) -> None:
        original_validator = matrix.validate_composite_fixture
        first_review = json.loads(
            (self.evidence_root / self.entries[0]["l5Review"]).read_bytes()
        )
        last_role = self.entries[-1]["role"]
        for field in ("parentSessionId", "childSessionId"):
            with self.subTest(field=field):
                def duplicate_last(*args: object, **kwargs: object) -> dict:
                    result = original_validator(*args, **kwargs)
                    if kwargs.get("role") == last_role:
                        return {**result, field: first_review[field]}
                    return result

                with patch.object(
                    matrix, "validate_composite_fixture", side_effect=duplicate_last
                ):
                    with self.assertRaisesRegex(ValueError, "session IDs are reused"):
                        matrix.validate_l5_rollout_matrix(
                            self.root, self.index_path, self.evidence_root
                        )

    def test_missing_stale_or_malformed_link_review_rejected(self) -> None:
        path = self.evidence_root / self.entries[0]["l5Review"]
        review = json.loads(path.read_bytes())
        variants = (
            b"{}",
            path.read_bytes()[:-1] + b',"scope":"code_explorer"}',
            json.dumps({**review, "reviewedAt": "2020-01-01T00:00:00+00:00"}).encode(),
            json.dumps({**review, "decision": "COMMENT"}).encode(),
            json.dumps({**review, "captureHash": "0" * 64}).encode(),
            json.dumps({**review, "token": "SYNTHETIC_SECRET_VALUE"}).encode(),
        )
        for variant in variants:
            with self.changed_file(path, variant):
                self.assert_invalid()

    def test_composite_review_alone_and_cross_role_splice_rejected(self) -> None:
        link_path = self.evidence_root / self.entries[0]["l5Review"]
        with self.changed_file(link_path, b"{}"):
            self.assert_invalid()
        child_path = self.evidence_root / self.entries[1]["childRollout"]
        other_child = (self.evidence_root / self.entries[0]["childRollout"]).read_bytes()
        with self.changed_file(child_path, other_child):
            self.assert_invalid()

    def test_wrong_source_or_public_attribution_claim_rejected(self) -> None:
        path = self.evidence_root / self.entries[0]["l5Review"]
        original = json.loads(path.read_bytes())
        for key, value in (
            ("rolloutSource", "caller-supplied"),
            ("publicStreamAttribution", "ATTRIBUTED"),
            ("runtimeValidated", True),
            ("originalManifestReviewed", True),
        ):
            with self.changed_file(path, json.dumps({**original, key: value}).encode()):
                self.assert_invalid()

    def test_secret_bearing_ignored_rollout_row_is_rejected(self) -> None:
        path = self.evidence_root / self.entries[0]["parentRollout"]
        secret_row = (
            json.dumps({
                "type": "event_msg",
                "payload": {"type": "unrelated", "api_key": "SYNTHETIC_SECRET_VALUE"},
            }) + "\n"
        ).encode()
        changed = path.read_bytes() + secret_row
        with self.assertRaisesRegex(ValueError, "unsafe|secret"):
            matrix._assert_safe_rollout(changed)
        with self.changed_file(path, changed):
            self.assert_invalid()

    def test_hash_binding_and_capture_tamper_rejected(self) -> None:
        review_path = self.evidence_root / self.entries[0]["l5Review"]
        original = json.loads(review_path.read_bytes())
        for key in (
            "runId", "captureId", "sidecarHash", "compositeReviewHash",
            "captureHash", "manifestHash", "parentRolloutHash",
            "childRolloutHash", "agentConfigHash", "parentSessionId", "childSessionId",
        ):
            with self.subTest(key=key):
                with self.changed_file(review_path, json.dumps({**original, key: "invalid"}).encode()):
                    self.assert_invalid()
        capture_path = self.root / json.loads((self.root / self.entries[0]["sidecar"]).read_bytes())["files"]["capture"]["path"]
        with self.changed_file(capture_path, capture_path.read_bytes() + b"\n"):
            self.assert_invalid()
        live.validate_marker(self.root)

    def test_index_duplicate_keys_bad_paths_and_secret_rejected(self) -> None:
        self.index_path.write_bytes(b'{"schema":"x","schema":"y"}')
        with self.assertRaises(ValueError):
            matrix.validate_l5_rollout_matrix(self.root, self.index_path, self.evidence_root)
        self.index["entries"][0]["parentRollout"] = "../outside.jsonl"
        self.assert_invalid()
        self.index["entries"] = copy.deepcopy(self.entries)
        self.index["api_key"] = "SYNTHETIC_SECRET_VALUE"
        self.assert_invalid()

    def test_external_drift_after_last_composite_cannot_pass(self) -> None:
        original_validator = matrix.validate_composite_fixture
        path = self.evidence_root / self.entries[0]["parentRollout"]
        original = path.read_bytes()
        count = 0

        def mutate_after_last(*args: object, **kwargs: object) -> dict:
            nonlocal count
            result = original_validator(*args, **kwargs)
            count += 1
            if count == 9:
                path.write_bytes(original + b"\n")
            return result

        try:
            with patch.object(matrix, "validate_composite_fixture", side_effect=mutate_after_last):
                self.assert_invalid()
        finally:
            path.write_bytes(original)

    def test_malformed_sidecar_swap_after_child_validation_fails_closed(self) -> None:
        sidecar_path = self.root / self.entries[0]["sidecar"]
        original = sidecar_path.read_bytes()
        original_validator = matrix.validate_snapshot_sidecar

        def swap_after_validation(root: Path, path: Path) -> dict:
            result = original_validator(root, path)
            if path == sidecar_path:
                sidecar_path.write_bytes(b'{"files":null}')
            return result

        try:
            with patch.object(
                matrix, "validate_snapshot_sidecar", side_effect=swap_after_validation
            ):
                self.assert_invalid()
        finally:
            sidecar_path.write_bytes(original)
        live.validate_marker(self.root)

    def test_cli_error_is_generic_and_contains_no_paths(self) -> None:
        self.index["entries"] = []
        self.write_index()
        output = StringIO()
        with redirect_stdout(output):
            code = matrix.main([
                "--run-root", str(self.root), "--index", str(self.index_path),
                "--evidence-root", str(self.evidence_root),
            ])
        self.assertEqual(2, code)
        self.assertEqual({
            "status": "ROLLOUT_MATRIX_INVALID",
            "l5Accepted": False,
            "runtimeValidated": False,
        }, json.loads(output.getvalue()))
        self.assertNotIn(str(self.root), output.getvalue())

    def test_deeply_nested_index_returns_generic_cli_error(self) -> None:
        self.index_path.write_bytes(
            b'{"schema":' + b"[" * 10000 + b"0" + b"]" * 10000 + b"}"
        )
        output = StringIO()
        with redirect_stdout(output):
            code = matrix.main([
                "--run-root", str(self.root), "--index", str(self.index_path),
                "--evidence-root", str(self.evidence_root),
            ])
        self.assertEqual(2, code)
        self.assertEqual({
            "status": "ROLLOUT_MATRIX_INVALID",
            "l5Accepted": False,
            "runtimeValidated": False,
        }, json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
