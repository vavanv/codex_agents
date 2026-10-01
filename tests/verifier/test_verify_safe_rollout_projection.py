from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_safe_rollout_projection as projection
from tests.verifier import test_reconcile_codex_rollouts as synthetic


PATHS = {
    "capture": "capture.jsonl",
    "manifest": "manifest.json",
    "parentRollout": "parent.jsonl",
    "childRollout": "child.jsonl",
    "agentConfig": "agent.toml",
}


def sources(role: str = synthetic.ROLE) -> dict[str, bytes]:
    capture, manifest, parent, child = synthetic.fixture(role)
    return {
        "capture": capture,
        "manifest": json.dumps(manifest).encode(),
        "parentRollout": synthetic.encoded(parent),
        "childRollout": synthetic.encoded(child),
        "agentConfig": synthetic.role_config(role),
    }


class SafeProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.current = sources()
        for name, relative in PATHS.items():
            (self.root / relative).write_bytes(self.current[name])
        self.retained = "projection.json"
        (self.root / self.retained).write_bytes(
            projection.project_bytes(self.current, role=synthetic.ROLE)
        )

    def verify(self, role: str = synthetic.ROLE) -> bool:
        return projection.verify_projection(
            self.root, PATHS, self.retained, role=role
        )

    def test_exact_projection_is_canonical_and_private_source_bound(self) -> None:
        self.assertTrue(self.verify())
        retained = (self.root / self.retained).read_bytes()
        document = json.loads(retained)
        self.assertEqual("codex-safe-rollout-projection/v2", document["schema"])
        self.assertEqual("CORRELATED_PRIVATE_SOURCES", document["status"])
        self.assertEqual("MISSING_ATTRIBUTION", document["publicStreamAttribution"])
        self.assertFalse(document["l5Accepted"])
        self.assertFalse(document["runtimeValidated"])
        self.assertEqual(set(PATHS), set(document["sourceHashes"]))
        self.assertNotIn(synthetic.INSTRUCTIONS, retained.decode())
        self.assertNotIn("C:\\fixture", retained.decode())

    def test_all_nine_profiles_can_be_projected_without_writer_claim(self) -> None:
        for role in synthetic.ROLE_FILES:
            with self.subTest(role=role):
                result = json.loads(projection.project_bytes(sources(role), role=role))
                self.assertEqual(role, result["role"])
                self.assertEqual("read-only", result["effectiveSandboxMode"])
                self.assertFalse(result["runtimeValidated"])

    def test_retained_tampering_or_schema_drift_fails(self) -> None:
        original = (self.root / self.retained).read_bytes()
        for mutation in (
            lambda obj: obj.update({"runtimeValidated": True}),
            lambda obj: obj.update({"secret": "SYNTHETIC_SECRET_VALUE"}),
            lambda obj: obj.update({"schema": "codex-safe-rollout-projection/v3"}),
        ):
            with self.subTest(mutation=mutation):
                document = json.loads(original)
                mutation(document)
                (self.root / self.retained).write_bytes(projection.canonical_bytes(document))
                with self.assertRaises(projection.ProjectionError) as caught:
                    self.verify()
                self.assertNotIn("SYNTHETIC_SECRET_VALUE", str(caught.exception))
        (self.root / self.retained).write_bytes(original)
        self.assertTrue(self.verify())

    def test_missing_or_changed_private_sources_fail(self) -> None:
        for name, relative in PATHS.items():
            with self.subTest(name=name):
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b"SYNTHETIC_SECRET_VALUE")
                with self.assertRaises(projection.ProjectionError) as caught:
                    self.verify()
                self.assertNotIn("SYNTHETIC_SECRET_VALUE", str(caught.exception))
                path.write_bytes(original)
                path.unlink()
                with self.assertRaises(projection.ProjectionError):
                    self.verify()
                path.write_bytes(original)
        self.assertTrue(self.verify())

    def test_swapped_pair_and_wrong_role_fail(self) -> None:
        first = self.root / PATHS["parentRollout"]
        second = self.root / PATHS["childRollout"]
        parent = first.read_bytes()
        child = second.read_bytes()
        first.write_bytes(child)
        second.write_bytes(parent)
        with self.assertRaises(projection.ProjectionError):
            self.verify()
        first.write_bytes(parent)
        second.write_bytes(child)
        with self.assertRaises(projection.ProjectionError):
            self.verify("code_validator")

    def test_duplicate_parent_child_session_id_fails(self) -> None:
        case = sources()
        rows = [json.loads(line) for line in case["childRollout"].splitlines()]
        rows[0]["payload"]["id"] = synthetic.PARENT
        case["childRollout"] = synthetic.encoded(rows)
        with self.assertRaises(projection.ProjectionError):
            projection.project_bytes(case, role=synthetic.ROLE)

    def test_duplicate_keys_malformed_deep_and_duplicate_records_fail(self) -> None:
        path = self.root / PATHS["parentRollout"]
        original = path.read_bytes()
        duplicate_key = b'{"type":"session_meta","type":"event_msg","payload":{}}\n'
        for data in (
            duplicate_key + original,
            b"SYNTHETIC_SECRET_VALUE\n" + original,
            original + original.splitlines(keepends=True)[0],
            original + b'{"type":"event_msg","payload":{"type":"task_complete"}}\n',
            original + b'{"type":"x","payload":{"deep":' + b"[" * 40 + b"0" + b"]" * 40 + b"}}\n",
        ):
            with self.subTest(data=data[:24]):
                path.write_bytes(data)
                with self.assertRaises(projection.ProjectionError) as caught:
                    self.verify()
                self.assertNotIn("SYNTHETIC_SECRET_VALUE", str(caught.exception))
        path.write_bytes(original)

    def test_capture_and_embedded_json_reject_duplicates_and_depth(self) -> None:
        for capture in (
            b'{"type":"thread.started","type":"turn.completed"}\n',
            b'{"deep":' + b"[" * 40 + b"0" + b"]" * 40 + b"}\n",
        ):
            with self.subTest(capture=capture[:20]):
                case = sources()
                case["capture"] = capture
                with self.assertRaises(projection.ProjectionError):
                    projection.project_bytes(case, role=synthetic.ROLE)
        for key, index, field in (("parentRollout", 2, "arguments"), ("parentRollout", 3, "output")):
            with self.subTest(field=field):
                case = sources()
                rows = [json.loads(line) for line in case[key].splitlines()]
                rows[index]["payload"][field] = '{"x":1,"x":2}'
                case[key] = synthetic.encoded(rows)
                with self.assertRaises(projection.ProjectionError):
                    projection.project_bytes(case, role=synthetic.ROLE)

    def test_child_tool_records_are_not_accepted(self) -> None:
        case = sources()
        rows = [json.loads(line) for line in case["childRollout"].splitlines()]
        rows.insert(5, synthetic.row("response_item", {"type": "function_call", "name": "other"}))
        case["childRollout"] = synthetic.encoded(rows)
        with self.assertRaises(projection.ProjectionError):
            projection.project_bytes(case, role=synthetic.ROLE)

    def test_ambiguous_critical_events_are_rejected(self) -> None:
        for outer, event in (("parentRollout", "thread_settings_applied"),
                             ("parentRollout", "task_cancelled"),
                             ("parentRollout", "collab_tool_call"),
                             ("parentRollout", "unknown_event"),
                             ("childRollout", "task_cancelled"),
                             ("childRollout", "agent_spawned"),
                             ("childRollout", "unknown_event")):
            with self.subTest(event=event):
                case = sources()
                rows = [json.loads(line) for line in case[outer].splitlines()]
                rows.insert(2, synthetic.row("event_msg", {"type": event}))
                case[outer] = synthetic.encoded(rows)
                with self.assertRaises(projection.ProjectionError):
                    projection.project_bytes(case, role=synthetic.ROLE)

    def test_unknown_response_item_type_is_rejected(self) -> None:
        for outer in ("parentRollout", "childRollout"):
            with self.subTest(outer=outer):
                case = sources()
                rows = [json.loads(line) for line in case[outer].splitlines()]
                rows.insert(2, synthetic.row("response_item", {"type": "unknown_item"}))
                case[outer] = synthetic.encoded(rows)
                with self.assertRaises(projection.ProjectionError):
                    projection.project_bytes(case, role=synthetic.ROLE)

    def test_extra_context_settings_final_and_spawn_fail(self) -> None:
        for name, index in (("parentRollout", 2), ("childRollout", 2), ("childRollout", 4), ("childRollout", 5)):
            with self.subTest(name=name, index=index):
                path = self.root / PATHS[name]
                original = path.read_bytes()
                rows = [json.loads(line) for line in original.splitlines()]
                rows.insert(index, copy.deepcopy(rows[index]))
                path.write_bytes(synthetic.encoded(rows))
                with self.assertRaises(projection.ProjectionError):
                    self.verify()
                path.write_bytes(original)

    def test_invalid_paths_and_duplicate_path_bindings_fail(self) -> None:
        for relative in ("../escape", "C:\\outside\\raw.jsonl", "", "sub/../../escape"):
            with self.subTest(relative=relative):
                paths = {**PATHS, "capture": relative}
                with self.assertRaises(projection.ProjectionError):
                    projection.verify_projection(self.root, paths, self.retained, role=synthetic.ROLE)
        paths = {**PATHS, "childRollout": PATHS["parentRollout"]}
        with self.assertRaises(projection.ProjectionError):
            projection.verify_projection(self.root, paths, self.retained, role=synthetic.ROLE)

    @unittest.skipUnless(os.name == "nt", "Windows handle boundary")
    def test_windows_rejects_reparse_source_and_root(self) -> None:
        target = self.root / PATHS["capture"]
        link = self.root / "capture-link.jsonl"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        paths = {**PATHS, "capture": link.name}
        with self.assertRaises(projection.ProjectionError):
            projection.verify_projection(self.root, paths, self.retained, role=synthetic.ROLE)
        linked_root = self.root.parent / (self.root.name + "-link")
        try:
            linked_root.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink creation unavailable")
        self.addCleanup(linked_root.unlink)
        with self.assertRaises(projection.ProjectionError):
            projection.verify_projection(linked_root, PATHS, self.retained, role=synthetic.ROLE)

    @unittest.skipUnless(os.name == "nt", "Windows path rules")
    def test_windows_rejects_ads_and_native_absolute_paths(self) -> None:
        for relative in ("capture.jsonl:stream", r"\??\C:\outside", r"C:relative.txt"):
            with self.subTest(relative=relative):
                with self.assertRaises(projection.ProjectionError):
                    projection.verify_projection(
                        self.root, {**PATHS, "capture": relative}, self.retained,
                        role=synthetic.ROLE,
                    )

    def test_private_source_mutation_during_read_fails(self) -> None:
        original = projection._safe_source
        count = 0

        def mutating_read(root: Path, relative: str, *, limit: int) -> bytes:
            nonlocal count
            value = original(root, relative, limit=limit)
            if relative == PATHS["childRollout"]:
                count += 1
                if count == 1:
                    target = root / relative
                    target.write_bytes(target.read_bytes() + b"SYNTHETIC_SECRET_VALUE")
            return value

        projection._safe_source = mutating_read
        try:
            with self.assertRaises((projection.ProjectionError, PermissionError)):
                self.verify()
        finally:
            projection._safe_source = original


if __name__ == "__main__":
    unittest.main()
