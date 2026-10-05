from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import copy
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import capture_live_event as capture_module
import live_validation_support as live
import validate_l5_snapshot_matrix as matrix_validator
import validate_capture_snapshot as snapshot_validator
import codex_compatibility as compatibility


class L5SnapshotMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(live.create_fixture(
            REPOSITORY_ROOT,
            allow_live=True,
            temp_base=Path(cls.temporary.name),
            timeout=30,
        )["runRoot"])
        cls.index_path = Path(cls.temporary.name) / "matrix.json"

        def fake_capture(
            fixture: Path,
            results: Path,
            bounded_timeout: int,
            codex_command: str,
            roles: tuple[str, ...],
            ephemeral: bool,
            sqlite_home: Path,
            **kwargs: object,
        ) -> tuple[int, dict[str, object]]:
            del fixture, bounded_timeout, codex_command, ephemeral, sqlite_home
            capture_module._persist_capture(
                results,
                json.dumps({
                    "type": "thread.started",
                    "thread_id": "00000000-0000-4000-8000-000000000001",
                }) + "\n",
                "", 0, False, roles, False, True, True,
                str(kwargs["output_prefix"]),
            )
            return 0, {"status": "CAPTURED"}

        cls.entries: list[dict[str, str]] = []
        with patch.object(capture_module, "capture", side_effect=fake_capture):
            for role in capture_module.ROLE_NAMES:
                code, output = capture_module.capture_owned_run(
                    cls.root, 30, "mock-codex", role
                )
                if code != 0 or output["status"] != "OWNED_CAPTURED":
                    raise AssertionError("Synthetic capture did not complete")
                sidecars = list((cls.root / "results").glob(
                    f"capture-{role}-*.snapshot-evidence.json"
                ))
                if len(sidecars) != 1:
                    raise AssertionError("Synthetic sidecar count is not one")
                sidecar = sidecars[0]
                capture_id = sidecar.name.removeprefix(f"capture-{role}-").removesuffix(
                    ".snapshot-evidence.json"
                )
                cls.entries.append({
                    "role": role,
                    "captureId": capture_id,
                    "sidecar": sidecar.relative_to(cls.root).as_posix(),
                })
        _, cls.marker = live.validate_marker(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.value: dict[str, object] = {
            "schema": matrix_validator.SCHEMA,
            "runId": self.marker["runId"],
            "codexVersion": "0.159.3",
            "entries": copy.deepcopy(self.entries),
        }
        self.write_index()

    def write_index(self) -> None:
        self.index_path.write_text(json.dumps(self.value), encoding="utf-8")

    def assert_invalid(self) -> None:
        self.write_index()
        with self.assertRaises((ValueError, live.LiveValidationError, OSError)):
            matrix_validator.validate_l5_snapshot_matrix(self.root, self.index_path)

    def test_complete_synthetic_offline_matrix_is_structural_only(self) -> None:
        self.assertEqual({
            "status": "SNAPSHOT_MATRIX_COMPLETE",
            "roleCount": 9,
            "codexVersion": "0.159.3",
            "l5Accepted": False,
            "runtimeValidated": False,
        }, matrix_validator.validate_l5_snapshot_matrix(self.root, self.index_path))

    def test_missing_duplicate_unknown_and_extra_role_rejected(self) -> None:
        original = copy.deepcopy(self.value["entries"])
        for changed in (
            original[:-1],
            original[:-1] + [copy.deepcopy(original[0])],
            original[:-1] + [{**original[-1], "role": "unknown_role"}],
            original + [copy.deepcopy(original[0])],
        ):
            self.value["entries"] = changed
            self.assert_invalid()

    def test_reused_capture_or_sidecar_and_path_escape_rejected(self) -> None:
        original = copy.deepcopy(self.value["entries"])
        variants = (
            {"captureId": original[0]["captureId"]},
            {"sidecar": original[0]["sidecar"]},
            {"sidecar": "../outside.snapshot-evidence.json"},
            {"sidecar": str((self.root / original[1]["sidecar"]).resolve())},
        )
        for changed in variants:
            entries = copy.deepcopy(original)
            entries[1].update(changed)
            self.value["entries"] = entries
            self.assert_invalid()

    def test_mixed_run_and_malformed_index_rejected(self) -> None:
        self.value["runId"] = "00000000-0000-4000-8000-000000000001"
        self.assert_invalid()
        self.value["runId"] = self.marker["runId"]
        self.value["extra"] = True
        self.assert_invalid()
        del self.value["extra"]
        self.index_path.write_text(
            '{"schema":"codex-l5-snapshot-matrix/v1",'
            '"schema":"codex-l5-snapshot-matrix/v1"}', encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            matrix_validator.validate_l5_snapshot_matrix(self.root, self.index_path)

    def test_version_mismatch_and_unknown_version_rejected(self) -> None:
        for version in ("0.155.1", "0.0.0"):
            self.value["codexVersion"] = version
            self.assert_invalid()

    def test_one_mixed_version_capture_is_rejected(self) -> None:
        sidecar = json.loads((self.root / self.entries[0]["sidecar"]).read_text(encoding="utf-8"))
        manifest_path = self.root / sidecar["files"]["manifest"]["path"]
        original_read = matrix_validator._read_json

        def mixed_manifest(path: Path) -> dict[str, object]:
            value = original_read(path)
            if path == manifest_path:
                value = {**value, "codexVersion": "0.155.1"}
            return value

        with patch.object(matrix_validator, "_read_json", side_effect=mixed_manifest):
            self.assert_invalid()

    def test_missing_oversized_and_secret_like_index_rejected(self) -> None:
        self.index_path.unlink()
        with self.assertRaises(ValueError):
            matrix_validator.validate_l5_snapshot_matrix(self.root, self.index_path)
        self.index_path.write_bytes(b" " * (matrix_validator.MAX_INDEX_BYTES + 1))
        with self.assertRaises(ValueError):
            matrix_validator.validate_l5_snapshot_matrix(self.root, self.index_path)
        self.value["api_key"] = "synthetic-secret"
        self.assert_invalid()

    def test_bad_entry_fields_types_and_bool_claim_rejected(self) -> None:
        original = copy.deepcopy(self.value["entries"])
        for changed in (
            {"captureId": True},
            {"captureId": "A" * 32},
            {"role": False},
            {"sidecar": None},
            {"extra": "field"},
        ):
            entries = copy.deepcopy(original)
            entries[0].update(changed)
            self.value["entries"] = entries
            self.assert_invalid()

    def test_missing_or_tampered_sidecar_rejected(self) -> None:
        sidecar = self.root / self.entries[0]["sidecar"]
        original = sidecar.read_bytes()
        try:
            sidecar.write_bytes(original + b"\n")
            self.assert_invalid()
        finally:
            sidecar.write_bytes(original)
        live.validate_marker(self.root)

    def test_changed_child_validation_identity_rejected(self) -> None:
        original_validator = matrix_validator.validate_snapshot_sidecar

        def wrong_identity(root: Path, sidecar: Path, **kwargs: object) -> dict[str, object]:
            result = original_validator(root, sidecar, **kwargs)
            if result["role"] == self.entries[0]["role"]:
                return {**result, "captureId": self.entries[1]["captureId"]}
            return result

        with patch.object(
            matrix_validator, "validate_snapshot_sidecar", side_effect=wrong_identity
        ):
            self.assert_invalid()

    def test_active_capture_lock_blocks_matrix_completeness(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def hold_lock() -> None:
            try:
                with capture_module._owned_run_lock(self.root):
                    entered.set()
                    if not release.wait(15):
                        raise TimeoutError("Test lock release was not signaled")
            except BaseException as error:
                errors.append(error)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            self.assertTrue(entered.wait(15))
            self.assert_invalid()
        finally:
            release.set()
            holder.join(20)
        self.assertFalse(holder.is_alive())
        self.assertEqual([], errors)

    def test_marker_change_between_precheck_and_lock_blocks_matrix(self) -> None:
        original_lock = matrix_validator._owned_run_lock
        marker_path = self.root / live.MARKER_NAME
        original_bytes = marker_path.read_bytes()

        @contextmanager
        def changed_marker(root: Path):
            marker_path.write_bytes(original_bytes + b" ")
            try:
                with original_lock(root):
                    yield
            finally:
                marker_path.write_bytes(original_bytes)

        with patch.object(matrix_validator, "_owned_run_lock", side_effect=changed_marker):
            self.assert_invalid()
        live.validate_marker(self.root)

    def test_last_sidecar_mutation_cannot_report_complete(self) -> None:
        sidecar = self.root / self.entries[-1]["sidecar"]
        original = sidecar.read_bytes()
        original_validator = matrix_validator.validate_snapshot_sidecar

        def mutate_after_validation(root: Path, path: Path, **kwargs: object) -> dict[str, object]:
            result = original_validator(root, path, **kwargs)
            if path == sidecar:
                sidecar.write_bytes(original + b"\n")
            return result

        try:
            with patch.object(
                matrix_validator,
                "validate_snapshot_sidecar",
                side_effect=mutate_after_validation,
            ):
                self.assert_invalid()
        finally:
            sidecar.write_bytes(original)
        live.validate_marker(self.root)

    def test_last_sidecar_index_or_marker_mutation_cannot_report_complete(self) -> None:
        last_sidecar = self.root / self.entries[-1]["sidecar"]
        marker_path = self.root / live.MARKER_NAME
        original_marker = marker_path.read_bytes()
        original_validator = matrix_validator.validate_snapshot_sidecar

        for target in ("index", "marker"):
            self.write_index()

            def mutate_after_validation(root: Path, path: Path, **kwargs: object) -> dict[str, object]:
                result = original_validator(root, path, **kwargs)
                if path == last_sidecar:
                    if target == "index":
                        self.index_path.write_bytes(self.index_path.read_bytes() + b" ")
                    else:
                        marker_path.write_bytes(original_marker + b" ")
                return result

            try:
                with patch.object(
                    matrix_validator,
                    "validate_snapshot_sidecar",
                    side_effect=mutate_after_validation,
                ):
                    with self.assertRaises((ValueError, live.LiveValidationError)):
                        matrix_validator.validate_l5_snapshot_matrix(
                            self.root, self.index_path
                        )
            finally:
                marker_path.write_bytes(original_marker)
        live.validate_marker(self.root)

    def test_cli_uses_allowlisted_output_without_paths(self) -> None:
        self.value["entries"] = []
        self.write_index()
        output = StringIO()
        with redirect_stdout(output):
            code = matrix_validator.main([
                "--run-root", str(self.root), "--index", str(self.index_path)
            ])
        self.assertEqual(2, code)
        self.assertEqual({
            "status": "SNAPSHOT_MATRIX_INVALID",
            "l5Accepted": False,
            "runtimeValidated": False,
        }, json.loads(output.getvalue()))
        self.assertNotIn(str(self.root), output.getvalue())


    def test_same_policy_snapshot_reaches_all_nine_sidecars_from_unrelated_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "compatibility").mkdir()
            path = source / "compatibility/codex-agents.json"
            path.write_bytes((REPOSITORY_ROOT / "compatibility/codex-agents.json").read_bytes())
            registry = compatibility.load_registry(source)
            path.write_bytes(b"changed snapshot bytes")
            prior = Path.cwd()
            os.chdir(directory)
            try:
                with patch.object(matrix_validator, "load_registry", side_effect=AssertionError("matrix snapshot reload")), \
                     patch.object(snapshot_validator, "load_registry", side_effect=AssertionError("sidecar snapshot reload")), \
                     patch.object(matrix_validator, "validate_snapshot_sidecar", wraps=matrix_validator.validate_snapshot_sidecar) as nested:
                    result = matrix_validator.validate_l5_snapshot_matrix(self.root, self.index_path, source_root=source, policy=registry)
            finally:
                os.chdir(prior)
            self.assertEqual("SNAPSHOT_MATRIX_COMPLETE", result["status"])
            self.assertEqual(9, nested.call_count)
            for call in nested.call_args_list:
                self.assertIs(registry, call.kwargs["policy"])
                self.assertEqual(source, call.kwargs["source_root"])
            with patch.object(Path, "read_bytes", side_effect=AssertionError("foreign policy read marker")):
                with self.assertRaises(compatibility.RegistryError):
                    matrix_validator.validate_l5_snapshot_matrix(self.root, self.index_path, policy=registry)
            path.unlink()
            with patch.object(Path, "read_bytes", side_effect=AssertionError("missing policy read marker")):
                with self.assertRaises(compatibility.RegistryError):
                    matrix_validator.validate_l5_snapshot_matrix(self.root, self.index_path, source_root=source)

    def test_registered_but_denied_index_version_fails_before_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "compatibility").mkdir()
            document = json.loads((REPOSITORY_ROOT / "compatibility/codex-agents.json").read_bytes())
            document["versions"]["0.155.1"]["gates"]["capturedEvidence"] = False
            document["versions"]["0.155.1"]["rolloutSchemas"] = []
            (source / "compatibility/codex-agents.json").write_text(json.dumps(document), encoding="utf-8")
            self.value["codexVersion"] = "0.155.1"
            self.write_index()
            with patch.object(matrix_validator, "validate_snapshot_sidecar", side_effect=AssertionError("denied matrix reached sidecar")):
                with self.assertRaisesRegex(ValueError, "Matrix Codex version is invalid"):
                    matrix_validator.validate_l5_snapshot_matrix(self.root, self.index_path, source_root=source)

    def test_static_candidate_and_cli_corruption_preserve_invalid_matrix_envelope(self) -> None:
        self.value["codexVersion"] = "0.160.0"
        self.write_index()
        with patch.object(matrix_validator, "validate_snapshot_sidecar", side_effect=AssertionError("static candidate reached sidecar")):
            with self.assertRaisesRegex(ValueError, "Matrix Codex version is invalid"):
                matrix_validator.validate_l5_snapshot_matrix(self.root, self.index_path)
        with patch.object(matrix_validator, "load_registry", side_effect=compatibility.RegistryError("malformed_policy", "bad")), \
             patch.object(matrix_validator, "validate_l5_snapshot_matrix", side_effect=AssertionError("CLI read fixture")), \
             redirect_stdout(StringIO()) as output:
            self.assertEqual(2, matrix_validator.main(["--run-root", str(self.root), "--index", str(self.index_path)]))
        self.assertEqual("SNAPSHOT_MATRIX_INVALID", json.loads(output.getvalue())["status"])


if __name__ == "__main__":
    unittest.main()
