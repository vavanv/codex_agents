from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import hashlib
from io import StringIO
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import capture_live_event as capture_module
import codex_event_adapter as adapter
import live_validation_support as live
import validate_capture_snapshot as snapshot_validator


class OwnedSnapshotCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(
            live.create_fixture(
                REPOSITORY_ROOT,
                allow_live=True,
                temp_base=Path(self.temporary.name),
                timeout=30,
            )["runRoot"]
        )

    def capture(
        self, *, timeout: bool = False, mutate_fixture: bool = False
    ) -> tuple[int, dict[str, object]]:
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
                (
                    json.dumps({
                        "type": "thread.started",
                        "thread_id": "00000000-0000-4000-8000-000000000001",
                    }) + "\n"
                ),
                "password=hidden" if timeout else "",
                None if timeout else 0,
                timeout,
                roles,
                False,
                True,
                True,
                str(kwargs["output_prefix"]),
            )
            if mutate_fixture:
                (self.root / "fixture" / ".live-cache" / "controlled.txt").write_text(
                    "changed ignored state\n", encoding="utf-8"
                )
            return (3, {"status": "BLOCKED", "reason": "CAPTURE_TIMEOUT"}) if timeout else (
                0, {"status": "CAPTURED"}
            )

        with patch.object(capture_module, "capture", side_effect=fake_capture):
            return capture_module.capture_owned_run(
                self.root, 30, "mock-codex", "code_explorer"
            )

    def sidecar(self) -> Path:
        sidecars = list((self.root / "results").glob("capture-*.snapshot-evidence.json"))
        self.assertEqual(1, len(sidecars))
        return sidecars[0]

    def rewrite_manifest(self, key: str, replacement: object) -> Path:
        sidecar = self.sidecar()
        sidecar_value = json.loads(sidecar.read_text(encoding="utf-8"))
        manifest = self.root / sidecar_value["files"]["manifest"]["path"]
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_value[key] = replacement
        live.atomic_write_json(manifest, manifest_value)
        sidecar_value["files"]["manifest"]["sha256"] = hashlib.sha256(
            manifest.read_bytes()
        ).hexdigest()
        live.atomic_write_json(sidecar, sidecar_value)
        _, marker = live.validate_marker(self.root, require_evidence=False)
        live.finalize_evidence(self.root, marker)
        return sidecar

    def test_success_brackets_capture_and_finalizes(self) -> None:
        code, output = self.capture()
        self.assertEqual(0, code)
        self.assertEqual("OWNED_CAPTURED", output["status"])
        marker_root, marker = live.validate_marker(self.root)
        self.assertEqual(self.root, marker_root)
        self.assertEqual("ready", marker["lifecycle"])
        self.assertEqual([], marker["activeWorkers"])
        self.assertFalse(output["runtimeValidated"])
        sidecar = self.sidecar()
        value = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertLess(value["startedAt"], value["completedAt"])
        self.assertEqual(
            "SNAPSHOT_BRACKET_VALID",
            snapshot_validator.validate_snapshot_sidecar(self.root, sidecar)["status"],
        )
        self.assertTrue((self.root / "results" / "private" / value["captureId"]).is_dir())
        self.assertFalse((self.root / "results" / "capture.lock").exists())

    def test_timeout_still_has_after_snapshot_but_is_not_accepted(self) -> None:
        code, output = self.capture(timeout=True)
        self.assertEqual(3, code)
        self.assertEqual("OWNED_CAPTURE_INCOMPLETE", output["status"])
        sidecar = self.sidecar()
        value = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertTrue((self.root / value["files"]["after"]["path"]).is_file())
        self.assertNotIn("hidden", sidecar.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "did not complete"):
            snapshot_validator.validate_snapshot_sidecar(self.root, sidecar)

    def test_changed_ignored_file_detects_drift(self) -> None:
        code, output = self.capture(mutate_fixture=True)
        self.assertEqual(2, code)
        self.assertEqual("STATE_DRIFT", output["reason"])
        with self.assertRaisesRegex(ValueError, "state drift"):
            snapshot_validator.validate_snapshot_sidecar(self.root, self.sidecar())

    def test_missing_snapshot_and_changed_digest_are_rejected(self) -> None:
        self.capture()
        sidecar = self.sidecar()
        value = json.loads(sidecar.read_text(encoding="utf-8"))
        before = self.root / value["files"]["before"]["path"]
        before.write_text(before.read_text(encoding="utf-8").replace(
            '"head":', '"changedHead":'
        ), encoding="utf-8")
        with self.assertRaises((ValueError, live.LiveValidationError)):
            snapshot_validator.validate_snapshot_sidecar(self.root, sidecar)
        before.unlink()
        with self.assertRaises((ValueError, live.LiveValidationError, OSError)):
            snapshot_validator.validate_snapshot_sidecar(self.root, sidecar)

    def test_duplicate_sidecar_key_and_sensitive_field_are_rejected_without_raw_output(self) -> None:
        self.capture()
        sidecar = self.sidecar()
        raw = sidecar.read_text(encoding="utf-8")
        sidecar.write_text(raw.replace(
            '"schema": "codex-capture-snapshot/v1",',
            '"schema": "codex-capture-snapshot/v1", "schema": "codex-capture-snapshot/v1",',
        ), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            snapshot_validator._read_json(sidecar)
        with self.assertRaises((ValueError, live.LiveValidationError)):
            snapshot_validator.validate_snapshot_sidecar(self.root, sidecar)
        sidecar.write_text(raw.replace(
            '"runtimeValidated": false,',
            '"api_key": "synthetic-secret", "runtimeValidated": false,',
        ), encoding="utf-8")
        with self.assertRaises(ValueError):
            snapshot_validator._read_json(sidecar)

    def test_bad_marker_layout_refuses_before_capture_writes(self) -> None:
        (self.root / "unexpected").mkdir()
        before = list((self.root / "results").glob("capture-*"))
        with self.assertRaises(live.LiveValidationError):
            self.capture()
        self.assertEqual(before, list((self.root / "results").glob("capture-*")))

    def test_missing_after_snapshot_interrupts_owned_fixture(self) -> None:
        original_snapshot = capture_module.capture_git_snapshot
        calls = 0

        def snapshot(root: Path, timeout: int) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise live.LiveValidationError("synthetic after-snapshot failure")
            return original_snapshot(root, timeout)

        with patch.object(capture_module, "capture_git_snapshot", side_effect=snapshot):
            with self.assertRaisesRegex(live.LiveValidationError, "after-snapshot"):
                self.capture()
        _, marker = live.validate_marker(self.root)
        self.assertEqual("interrupted", marker["lifecycle"])
        self.assertEqual([], marker["activeWorkers"])
        self.assertEqual([], list((self.root / "results").glob("capture-*.snapshot-evidence.json")))

    def test_stale_role_output_refuses_before_marker_mutation(self) -> None:
        fixed = uuid.UUID("00000000-0000-4000-8000-000000000033")
        stale = self.root / "results" / (
            f"capture-code_explorer-{fixed.hex}.sanitized.jsonl"
        )
        stale.write_text("preexisting\n", encoding="utf-8")
        _, marker = live.validate_marker(self.root, require_evidence=False)
        live.finalize_evidence(self.root, marker)
        marker_before = (self.root / live.MARKER_NAME).read_bytes()
        with patch.object(capture_module.uuid, "uuid4", return_value=fixed):
            with self.assertRaisesRegex(ValueError, "already exists"):
                capture_module.capture_owned_run(
                    self.root, 30, "mock-codex", "code_explorer"
                )
        self.assertEqual(marker_before, (self.root / live.MARKER_NAME).read_bytes())
        self.assertEqual("preexisting\n", stale.read_text(encoding="utf-8"))

    def test_owned_cli_error_does_not_print_raw_path(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = capture_module.main([
                "--owned-run-root", str(self.root / "password=synthetic-secret"),
                "--role", "code_explorer",
            ])
        self.assertEqual(4, code)
        self.assertEqual(
            {"status": "BLOCKED", "reason": "OWNED_CAPTURE_FAILED"},
            json.loads(output.getvalue()),
        )

    def test_manifest_rejects_boolean_exit_code(self) -> None:
        self.capture()
        sidecar = self.rewrite_manifest("exitCode", False)
        with self.assertRaisesRegex(ValueError, "Manifest capture state"):
            snapshot_validator.validate_snapshot_sidecar(self.root, sidecar)

    def test_manifest_rejects_wrong_event_schema_and_codex_version(self) -> None:
        self.capture()
        for key, invalid in (
            ("eventSchema", "unknown-event-schema"),
            ("codexVersion", "0.0.0"),
            ("codexVersion", []),
            ("codexVersion", {}),
        ):
            sidecar = self.rewrite_manifest(key, invalid)
            with self.assertRaisesRegex(ValueError, "Manifest capture state"):
                snapshot_validator.validate_snapshot_sidecar(self.root, sidecar)
            self.rewrite_manifest(
                key, adapter.EVENT_SCHEMA if key == "eventSchema" else "0.155.1"
            )

    def test_manifest_rejects_unisolated_or_untrusted_mode(self) -> None:
        self.capture()
        for key in ("userConfigIgnored", "fixtureTrustOverride"):
            sidecar = self.rewrite_manifest(key, False)
            with self.assertRaisesRegex(ValueError, "Manifest capture state"):
                snapshot_validator.validate_snapshot_sidecar(self.root, sidecar)
            self.rewrite_manifest(key, True)

    def test_manifest_rejects_wrong_name_command_and_out_of_bracket_time(self) -> None:
        self.capture()
        sidecar = self.sidecar()
        original = json.loads(
            (self.root / json.loads(sidecar.read_text(encoding="utf-8"))["files"]["manifest"]["path"])
            .read_text(encoding="utf-8")
        )
        for key, invalid, message in (
            ("name", "unrelated", "Manifest capture state"),
            ("command", "codex exec --json", "Manifest capture state"),
            ("capturedAt", "2000-01-01T00:00:00+00:00", "timing"),
        ):
            sidecar = self.rewrite_manifest(key, invalid)
            with self.assertRaisesRegex(ValueError, message):
                snapshot_validator.validate_snapshot_sidecar(self.root, sidecar)
            self.rewrite_manifest(key, original[key])

    def test_unknown_role_in_sidecar_path_is_rejected(self) -> None:
        self.capture()
        original = self.sidecar()
        value = json.loads(original.read_text(encoding="utf-8"))
        changed = original.with_name(original.name.replace(
            "capture-code_explorer-", "capture-unknown_role-"
        ))
        value["role"] = "unknown_role"
        live.atomic_write_json(changed, value)
        _, marker = live.validate_marker(self.root, require_evidence=False)
        live.finalize_evidence(self.root, marker)
        with self.assertRaisesRegex(ValueError, "pinned catalog"):
            snapshot_validator.validate_snapshot_sidecar(self.root, changed)

    def test_second_owned_capture_cannot_enter_while_first_is_running(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []
        errors: list[BaseException] = []

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
            calls.append("entered")
            entered.set()
            if not release.wait(15):
                raise TimeoutError("test release was not signaled")
            capture_module._persist_capture(
                results, "", "", 0, False, roles, False, True, True,
                str(kwargs["output_prefix"]),
            )
            return 0, {"status": "CAPTURED"}

        def first_run() -> None:
            try:
                capture_module.capture_owned_run(
                    self.root, 30, "mock-codex", "code_explorer"
                )
            except BaseException as error:
                errors.append(error)

        with patch.object(capture_module, "capture", side_effect=fake_capture):
            worker = threading.Thread(target=first_run)
            worker.start()
            try:
                self.assertTrue(entered.wait(15))
                with self.assertRaises((ValueError, live.LiveValidationError)):
                    capture_module.capture_owned_run(
                        self.root, 30, "mock-codex", "code_explorer"
                    )
                self.assertEqual(["entered"], calls)
            finally:
                release.set()
                worker.join(30)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        live.validate_marker(self.root)

    def test_os_lock_excludes_another_process(self) -> None:
        child_code = (
            "import pathlib,sys\n"
            "sys.path.insert(0,sys.argv[1])\n"
            "import capture_live_event\n"
            "with capture_live_event._owned_run_lock(pathlib.Path(sys.argv[2])):\n"
            " print('READY',flush=True)\n"
            " sys.stdin.readline()\n"
        )
        process = subprocess.Popen(
            [
                sys.executable, "-B", "-c", child_code,
                str(REPOSITORY_ROOT / "scripts"), str(self.root),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual("READY\n", process.stdout.readline())
            with patch.object(capture_module, "capture") as capture:
                with self.assertRaisesRegex(ValueError, "already active"):
                    capture_module.capture_owned_run(
                        self.root, 30, "mock-codex", "code_explorer"
                    )
                capture.assert_not_called()
        finally:
            process.communicate(input="\n", timeout=10)
        self.assertEqual(0, process.returncode)
        live.validate_marker(self.root)

    def test_contender_fails_while_finalization_is_paused(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        original_finalize = capture_module.finalize_evidence

        def paused_finalize(root: Path, marker: dict[str, object]) -> dict[str, object]:
            entered.set()
            if not release.wait(15):
                raise TimeoutError("test finalization release was not signaled")
            return original_finalize(root, marker)

        def first_run() -> None:
            try:
                self.capture()
            except BaseException as error:
                errors.append(error)

        with patch.object(
            capture_module, "finalize_evidence", side_effect=paused_finalize
        ):
            worker = threading.Thread(target=first_run)
            worker.start()
            try:
                self.assertTrue(entered.wait(15))
                with self.assertRaises((ValueError, live.LiveValidationError)):
                    capture_module.capture_owned_run(
                        self.root, 30, "mock-codex", "code_explorer"
                    )
            finally:
                release.set()
                worker.join(30)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        live.validate_marker(self.root)

    def test_stale_preread_contender_fails_after_other_run_finishes(self) -> None:
        original_lock = capture_module._owned_run_lock

        @contextmanager
        def delayed_lock(root: Path):
            with patch.object(capture_module, "_owned_run_lock", original_lock):
                self.capture()
            with original_lock(root):
                yield

        with patch.object(capture_module, "_owned_run_lock", side_effect=delayed_lock):
            with self.assertRaisesRegex(ValueError, "marker changed before capture lock"):
                capture_module.capture_owned_run(
                    self.root, 30, "mock-codex", "code_explorer"
                )
        self.assertEqual(
            1,
            len(list((self.root / "results").glob("capture-*.snapshot-evidence.json"))),
        )
        live.validate_marker(self.root)

    def test_contender_fails_after_ready_write_before_final_validation(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        original_validate = live.validate_marker

        def paused_validate(*args: object, **kwargs: object):
            entered.set()
            if not release.wait(15):
                raise TimeoutError("test validation release was not signaled")
            return original_validate(*args, **kwargs)

        def first_run() -> None:
            try:
                self.capture()
            except BaseException as error:
                errors.append(error)

        with patch.object(live, "validate_marker", side_effect=paused_validate):
            worker = threading.Thread(target=first_run)
            worker.start()
            try:
                self.assertTrue(entered.wait(15))
                marker = json.loads(
                    (self.root / live.MARKER_NAME).read_text(encoding="utf-8")
                )
                self.assertEqual("ready", marker["lifecycle"])
                with self.assertRaisesRegex(ValueError, "already active"):
                    capture_module.capture_owned_run(
                        self.root, 30, "mock-codex", "code_explorer"
                    )
            finally:
                release.set()
                worker.join(30)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        live.validate_marker(self.root)

    def test_results_tamper_between_precheck_and_lock_is_rejected(self) -> None:
        original_lock = capture_module._owned_run_lock
        target = self.root / "results" / "environment.json"
        self.assertTrue(target.is_file())

        @contextmanager
        def tampering_lock(root: Path):
            target.write_text('{"tampered":true}\n', encoding="utf-8")
            with original_lock(root):
                yield

        with patch.object(capture_module, "_owned_run_lock", side_effect=tampering_lock):
            with self.assertRaises(live.LiveValidationError):
                capture_module.capture_owned_run(
                    self.root, 30, "mock-codex", "code_explorer"
                )
        self.assertEqual(
            [], list((self.root / "results").glob("capture-*.snapshot-evidence.json"))
        )


if __name__ == "__main__":
    unittest.main()
