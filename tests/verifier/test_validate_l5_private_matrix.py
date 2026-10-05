"""Focused fail-closed checks for the opt-in private L5 gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from validate_l5_private_matrix import (  # noqa: E402
    REVIEW_SCHEMA, _parse_rows, _projection, _review_matches, _rollout_name,
    _shape, validate_l5_private_matrix,
)
from codex_compatibility import RegistryError, load_registry
import capture_live_event as capture_module  # noqa: E402
import live_validation_support as live  # noqa: E402
import validate_l5_private_matrix as private_matrix  # noqa: E402
from tests.verifier.test_reconcile_codex_rollouts import (  # noqa: E402
    encoded, fixture, role_config,
)
from tests.verifier.test_validate_composite_fixture import attestation  # noqa: E402


def _rows(*pairs: tuple[str, str | None]) -> bytes:
    return b"".join(json.dumps({"type": outer, "payload": {"type": inner}
                               if inner else {}}, sort_keys=True).encode() + b"\n"
                    for outer, inner in pairs)


PARENT = (
    ("session_meta", None), ("response_item", "function_call"),
    ("response_item", "function_call_output"),
    ("response_item", "function_call"),
    ("response_item", "function_call_output"),
    ("event_msg", "task_complete"),
)


def _parent() -> bytes:
    items = [json.loads(line) for line in _rows(*PARENT).splitlines()]
    items[1]["payload"]["name"] = "spawn_agent"
    items[3]["payload"]["name"] = "wait_agent"
    return b"".join(json.dumps(item).encode() + b"\n" for item in items)


class PrivateL5Tests(unittest.TestCase):

    def test_bad_or_foreign_policy_blocks_before_fixture_or_private_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "compatibility").mkdir(parents=True)
            registry_path = source / "compatibility/codex-agents.json"
            registry_path.write_bytes((Path(__file__).resolve().parents[2] / "compatibility/codex-agents.json").read_bytes())
            policy = load_registry(source)
            with patch.object(private_matrix, "validate_l5_snapshot_matrix") as snapshot, patch.object(private_matrix, "_safe_source") as private:
                with self.assertRaises(RegistryError):
                    validate_l5_private_matrix(root, root, root, policy=policy)
                registry_path.write_text("{}", encoding="utf-8")
                with self.assertRaises(RegistryError):
                    validate_l5_private_matrix(root, root, root, source_root=source)
            snapshot.assert_not_called()
            private.assert_not_called()

    def test_profile_mismatch_rejects_index_before_private_source_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "compatibility").mkdir(parents=True)
            document = json.loads((Path(__file__).resolve().parents[2] / "compatibility/codex-agents.json").read_text(encoding="utf-8"))
            document["runProfiles"]["legacy-windows-capture"]["expectedVersion"] = "0.157.1"
            (source / "compatibility/codex-agents.json").write_text(json.dumps(document), encoding="utf-8")
            policy = load_registry(source)
            marker_path = root / live.MARKER_NAME
            marker_path.write_bytes(b"marker")
            index = root / "index.json"
            index.write_text(json.dumps({"schema": "codex-l5-snapshot-matrix/v1", "runId": "run", "codexVersion": "0.159.3", "entries": []}), encoding="utf-8")
            original = index.read_bytes()
            from contextlib import nullcontext
            with patch.object(private_matrix, "validate_l5_snapshot_matrix", return_value={"status": "SNAPSHOT_MATRIX_COMPLETE"}) as baseline, patch.object(private_matrix, "validate_marker", return_value=(root, {"lifecycle": "ready", "activeWorkers": [], "runId": "run"})), patch.object(private_matrix, "_owned_run_lock", return_value=nullcontext()), patch.object(private_matrix, "_safe_source") as private:
                with self.assertRaisesRegex(ValueError, "this version"):
                    validate_l5_private_matrix(root, index, root / "absent-private", source_root=source, policy=policy)
            self.assertIs(policy, baseline.call_args.kwargs["policy"])
            self.assertEqual(source, baseline.call_args.kwargs["source_root"])
            private.assert_not_called()
            self.assertEqual(original, index.read_bytes())

    def test_shape_accepts_observed_v1_v2_and_secret_text_is_not_emitted(self) -> None:
        _shape(_parent(), parent=True, variant="v1")
        v1_child = _rows(("session_meta", None), ("session_meta", None),
                         ("response_item", "message"), ("event_msg", "task_complete"))
        _shape(v1_child, parent=False, variant="v1")
        v2_child = _rows(("session_meta", None), ("turn_context", None),
                         ("response_item", "reasoning"), ("event_msg", "task_complete"))
        _shape(v2_child, parent=False, variant="v2")
        secret_child = v2_child.replace(b'"type": "reasoning"',
                                        b'"type": "reasoning", "text": "sk-secret-value"')
        _shape(secret_child, parent=False, variant="v2")

    def test_shape_rejects_extra_child_tool_unknown_row_and_duplicate_key(self) -> None:
        child = _rows(("session_meta", None), ("event_msg", "task_complete"))
        with self.assertRaises(ValueError):
            _shape(child + _rows(("response_item", "function_call")),
                   parent=False, variant="v2")
        with self.assertRaises(ValueError):
            _shape(child + _rows(("mystery", None)), parent=False, variant="v2")
        with self.assertRaises(ValueError):
            _parse_rows(b'{"type":"session_meta","type":"session_meta","payload":{}}\n')
        with self.assertRaises(ValueError):
            _shape(_parent().replace(b'"name": "wait_agent"',
                                     b'"name": "send_message"'), parent=True,
                   variant="v1")

    def test_review_requires_later_exact_hash_bound_attestation(self) -> None:
        now = datetime.now(timezone.utc)
        previous = (now - timedelta(minutes=2)).isoformat()
        expected = {
            "schema": REVIEW_SCHEMA, "decision": "APPROVE_L5_PRIVATE",
            "role": "code_explorer", "runId": "run-id", "captureId": "a" * 32,
            "codexVersion": "0.159.3", "sidecarHash": "b" * 64,
            "compositeReviewHash": "c" * 64, "projectionHash": "d" * 64,
            "parentRolloutHash": "e" * 64, "childRolloutHash": "f" * 64,
            "parentSessionId": "parent", "childSessionId": "child",
        }
        review = dict(expected, reviewer="/root/l5_reviewer", reviewedAt=now.isoformat())
        raw = json.dumps(review).encode()
        self.assertTrue(_review_matches(raw, expected, previous, previous,
                                        "/root/capture_reviewer"))
        review["projectionHash"] = "0" * 64
        self.assertFalse(_review_matches(json.dumps(review).encode(), expected,
                                         previous, previous, "/root/capture_reviewer"))
        review["projectionHash"] = "d" * 64
        review["reviewedAt"] = (now - timedelta(minutes=3)).isoformat()
        self.assertFalse(_review_matches(json.dumps(review).encode(), expected,
                                         previous, previous, "/root/capture_reviewer"))
        review["reviewedAt"] = now.isoformat()
        review["reviewer"] = "external"
        self.assertFalse(_review_matches(json.dumps(review).encode(), expected,
                                         previous, previous, "/root/capture_reviewer"))
        review["reviewer"] = "/root/capture_reviewer"
        self.assertFalse(_review_matches(json.dumps(review).encode(), expected,
                                         previous, previous, "/root/capture_reviewer"))

    def test_projection_is_small_and_allowlisted(self) -> None:
        composite = {key: key for key in ("captureHash", "manifestHash",
                     "parentRolloutHash", "childRolloutHash", "agentConfigHash",
                     "parentSessionId", "childSessionId")}
        raw = _projection(composite, "code_explorer", "v1", "0.159.3")
        self.assertLess(len(raw), 4096)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         hashlib.sha256(_projection(composite, "code_explorer", "v1", "0.159.3")).hexdigest())
        self.assertNotIn(b"sk-secret", raw)

    def test_rollout_lookup_rejects_missing_duplicate_and_bad_path(self) -> None:
        session = "1" * 36
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                _rollout_name(root, session)
            (root / f"rollout-junk-{session}.jsonl").write_bytes(b"{}\n")
            with self.assertRaises(ValueError):
                _rollout_name(root, session)
            (root / f"rollout-junk-{session}.jsonl").unlink()
            name = f"rollout-2026-10-01T00-00-00-{session}.jsonl"
            (root / name).write_bytes(b"{}\n")
            self.assertEqual(_rollout_name(root, session), name)
            (root / f"rollout-2026-10-01T00-00-01-{session}.jsonl").write_bytes(b"{}\n")
            with self.assertRaises(ValueError):
                _rollout_name(root, session)
            with self.assertRaises(ValueError):
                _rollout_name(root, "../escape")


class NineRolePrivateL5Tests(unittest.TestCase):
    def test_full_acceptance_and_fail_closed_variants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = Path(live.create_fixture(
                Path(__file__).resolve().parents[2], allow_live=True,
                temp_base=base, timeout=30)["runRoot"])
            private = base / "private-sessions"
            private.mkdir()
            agents = root / "fixture" / ".codex" / "agents"
            agents.mkdir(parents=True, exist_ok=True)
            for role in capture_module.ROLE_NAMES:
                (agents / f"{role.replace('_', '-')}.toml").write_bytes(role_config(role))
            parent_ids = {
                role: f"00000000-0000-4000-8000-{index + 1001:012x}"
                for index, role in enumerate(capture_module.ROLE_NAMES)
            }
            child_ids = {
                role: f"00000000-0000-4000-8000-{index + 2001:012x}"
                for index, role in enumerate(capture_module.ROLE_NAMES)
            }

            def fake_capture(fixture_path: Path, results: Path, timeout: int,
                             command: str, roles: tuple[str, ...], ephemeral: bool,
                             sqlite_home: Path, **kwargs: object) -> tuple[int, dict]:
                del fixture_path, timeout, command, ephemeral, sqlite_home
                role = roles[0]
                stream = (json.dumps({"type": "thread.started",
                                      "thread_id": parent_ids[role]}) + "\n"
                          + json.dumps({"type": "turn.completed"}) + "\n")
                capture_module._persist_capture(
                    results, stream, "", 0, False, roles, False, True, True,
                    str(kwargs["output_prefix"]), _identity=kwargs["_identity"])
                return 0, {"status": "CAPTURED"}

            entries = []
            with patch.object(capture_module, "capture", side_effect=fake_capture):
                for role in capture_module.ROLE_NAMES:
                    code, outcome = capture_module.capture_owned_run(
                        root, 30, "mock-codex", role)
                    self.assertEqual((0, "OWNED_CAPTURED"),
                                     (code, outcome["status"]))
                    sidecar_path = next((root / "results").glob(
                        f"capture-{role}-*.snapshot-evidence.json"))
                    sidecar = json.loads(sidecar_path.read_bytes())
                    capture_id = sidecar["captureId"]
                    capture = (root / sidecar["files"]["capture"]["path"]).read_bytes()
                    manifest = (root / sidecar["files"]["manifest"]["path"]).read_bytes()
                    _, _, parent, child = fixture(role, version="0.159.3")
                    parent_rows = copy.deepcopy(parent)
                    spawn = next(x for x in parent_rows
                                 if x.get("payload", {}).get("type") == "function_call")
                    spawned = next(x for x in parent_rows
                                   if x.get("payload", {}).get("type") == "function_call_output")
                    wait_call = copy.deepcopy(spawn)
                    wait_call["payload"]["name"] = "wait_agent"
                    wait_call["payload"]["call_id"] = "synthetic-wait"
                    wait_output = copy.deepcopy(spawned)
                    wait_output["payload"]["call_id"] = "synthetic-wait"
                    parent_rows[-1:-1] = [wait_call, wait_output]
                    original_parent = b"00000000-0000-4000-8000-000000000001"
                    original_child = b"00000000-0000-4000-8000-000000000002"
                    parent_raw = encoded(parent_rows).replace(
                        original_parent, parent_ids[role].encode())
                    child_raw = (encoded(child).replace(
                        original_parent, parent_ids[role].encode()).replace(
                        original_child, child_ids[role].encode()))
                    self.assertEqual(2, len([x for x in _parse_rows(parent_raw)
                                             if x["type"] == "response_item" and
                                             x["payload"].get("type") == "function_call"]))
                    values = (capture, manifest, parent_raw, child_raw, role_config(role))
                    composite_review = attestation(values, role)
                    stem = f"capture-{role}-{capture_id}"
                    (root / "results" / f"{stem}.composite-review.json").write_bytes(composite_review)
                    for session_id, raw in ((parent_ids[role], parent_raw),
                                            (child_ids[role], child_raw)):
                        (private / f"rollout-2026-10-01T00-00-00-{session_id}.jsonl").write_bytes(raw)
                    entries.append({"role": role, "captureId": capture_id,
                                    "sidecar": f"results/{stem}.snapshot-evidence.json"})
                    live.finalize_evidence(
                        root, json.loads((root / live.MARKER_NAME).read_bytes()))
            index_path = root / "results" / "private-index.json"
            _, marker = live.validate_marker(root)
            index_path.write_text(json.dumps({"schema": "codex-l5-snapshot-matrix/v1",
                                              "runId": marker["runId"],
                                              "codexVersion": "0.159.3",
                                              "entries": entries}), encoding="utf-8")
            live.finalize_evidence(root, marker)
            policy = load_registry(Path(__file__).resolve().parents[2])
            source = Path(__file__).resolve().parents[2]
            with patch.object(private_matrix, "validate_l5_snapshot_matrix", wraps=private_matrix.validate_l5_snapshot_matrix) as snapshot, patch.object(private_matrix, "validate_snapshot_sidecar", wraps=private_matrix.validate_snapshot_sidecar) as sidecar_check, patch.object(private_matrix, "validate_composite_fixture", wraps=private_matrix.validate_composite_fixture) as composite:
                pending = validate_l5_private_matrix(root, index_path, private, source_root=source, policy=policy)
            for mock in (snapshot, sidecar_check, composite):
                for call in mock.call_args_list:
                    self.assertIs(policy, call.kwargs["policy"])
                    self.assertEqual(source, call.kwargs["source_root"])
            self.assertEqual("L5_REVIEW_REQUIRED", pending["status"])
            self.assertEqual(9, len(pending["projectionPending"]))
            self.assertEqual(9, len(pending["reviewPending"]))
            self.assertEqual(18, pending["sessionCount"])

            first = pending["projections"][0]
            first_projection = root / first["projectionFile"]
            first_review = root / first["reviewFile"]

            def finalize() -> None:
                live.finalize_evidence(root, json.loads((root / live.MARKER_NAME).read_bytes()))

            @contextmanager
            def changed(path: Path, replacement: bytes):
                original = path.read_bytes()
                path.write_bytes(replacement)
                finalize()
                try:
                    yield
                finally:
                    path.write_bytes(original)
                    finalize()

            for item in pending["projections"]:
                (root / item["projectionFile"]).write_bytes(
                    (json.dumps(item["projection"], sort_keys=True,
                                separators=(",", ":"), ensure_ascii=True) + "\n").encode())
                review = dict(item["reviewFields"])
                review["reviewer"] = "/root/synthetic_l5_reviewer"
                review["reviewedAt"] = (datetime.now(timezone.utc)
                                        + timedelta(minutes=1)).isoformat()
                (root / item["reviewFile"]).write_text(json.dumps(review), encoding="utf-8")
            finalize()
            accepted = validate_l5_private_matrix(root, index_path, private)
            self.assertEqual("L5_ACCEPTED", accepted["status"])
            self.assertTrue(accepted["l5Accepted"])
            self.assertFalse(accepted["runtimeValidated"])

            with changed(first_projection, b"{}\n"):
                with self.assertRaises(ValueError):
                    validate_l5_private_matrix(root, index_path, private)
            with changed(first_review, b"{}\n"):
                with self.assertRaises(ValueError):
                    validate_l5_private_matrix(root, index_path, private)
            original_projection = first_projection.read_bytes()
            first_projection.unlink()
            finalize()
            self.assertEqual("L5_REVIEW_REQUIRED",
                             validate_l5_private_matrix(root, index_path, private)["status"])
            first_projection.write_bytes(original_projection)
            finalize()
            first_parent = next(private.glob(f"*{parent_ids[first['role']]}.jsonl"))
            with changed(first_parent, first_parent.read_bytes() + b'\n'):
                with self.assertRaises(ValueError):
                    validate_l5_private_matrix(root, index_path, private)
            with self.assertRaises(ValueError):
                validate_l5_private_matrix(root, index_path, root)
            with self.assertRaises(ValueError):
                validate_l5_private_matrix(root, index_path, base)

            second_review = root / pending["projections"][1]["reviewFile"]
            altered = json.loads(second_review.read_bytes())
            altered["parentSessionId"] = json.loads(first_review.read_bytes())["parentSessionId"]
            with changed(second_review, json.dumps(altered).encode()):
                with self.assertRaises(ValueError):
                    validate_l5_private_matrix(root, index_path, private)
            original_composite = private_matrix.validate_composite_fixture

            def duplicate_session(*args: object, **kwargs: object) -> dict:
                result = original_composite(*args, **kwargs)
                if kwargs.get("role") == pending["projections"][1]["role"]:
                    return {**result, "parentSessionId": json.loads(
                        first_review.read_bytes())["parentSessionId"]}
                return result

            with patch.object(private_matrix, "validate_composite_fixture",
                              side_effect=duplicate_session):
                with self.assertRaisesRegex(ValueError, "Session ID reused"):
                    validate_l5_private_matrix(root, index_path, private)


if __name__ == "__main__":
    unittest.main()
