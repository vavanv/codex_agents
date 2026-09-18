from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY_ROOT / "scripts"
FIXTURES = Path(__file__).with_name("fixtures")
sys.path.insert(0, str(SCRIPTS))

import live_validation_support as live


def tree_fingerprint(root: Path) -> list[tuple[str, str, str]]:
    values: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        value = os.lstat(path)
        if stat.S_ISLNK(value.st_mode):
            values.append((relative, "link", os.readlink(path)))
        elif stat.S_ISDIR(value.st_mode):
            values.append((relative, "directory", ""))
        else:
            values.append((relative, "file", live._sha256_file(path)))
    return values


class LiveHarnessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create(self) -> Path:
        result = live.create_fixture(
            REPOSITORY_ROOT,
            allow_live=True,
            temp_base=self.base,
            timeout=30,
        )
        return Path(result["runRoot"])

    def read_marker(self, root: Path) -> dict[str, object]:
        return json.loads((root / live.MARKER_NAME).read_text(encoding="utf-8"))

    def write_marker(self, root: Path, value: dict[str, object]) -> None:
        live.atomic_write_json(root / live.MARKER_NAME, value)


class OfflinePolicyTests(LiveHarnessTestCase):
    def test_create_preview_has_no_write_or_subprocess(self) -> None:
        before = list(self.base.iterdir())
        with patch.object(live.tempfile, "mkdtemp") as make_directory:
            with patch.object(live.subprocess, "run") as run:
                result = live.create_fixture(
                    REPOSITORY_ROOT,
                    allow_live=False,
                    temp_base=self.base,
                )
        self.assertEqual("preview", result["mode"])
        self.assertFalse(result["writes"])
        self.assertFalse(result["subprocesses"])
        make_directory.assert_not_called()
        run.assert_not_called()
        self.assertEqual(before, list(self.base.iterdir()))

    def test_runner_and_cleanup_preview_do_not_mutate_fixture(self) -> None:
        root = self.create()
        before = tree_fingerprint(root)
        with patch.object(live.subprocess, "run") as run:
            result = live.run_behavior_tests(root, allow_live=False)
        self.assertEqual("preview", result["mode"])
        run.assert_not_called()
        self.assertEqual(before, tree_fingerprint(root))
        cleanup = live.cleanup_fixture(root, apply_cleanup=False)
        self.assertEqual("preview", cleanup["mode"])
        self.assertEqual(before, tree_fingerprint(root))

    def test_power_shell_wrappers_preserve_offline_defaults(self) -> None:
        shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        self.assertIsNotNone(shell)
        create = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-File",
                str(SCRIPTS / "create_live_fixture.ps1"),
                "-TempBase",
                str(self.base),
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(0, create.returncode, create.stderr)
        self.assertEqual([], list(self.base.iterdir()))
        self.assertFalse(json.loads(create.stdout)["writes"])

        root = self.create()
        before = tree_fingerprint(root)
        for script, extra in (
            ("run_live_behavior_tests.ps1", []),
            ("cleanup_live_fixture.ps1", []),
        ):
            result = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-File",
                    str(SCRIPTS / script),
                    "-RunRoot",
                    str(root),
                    *extra,
                ],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("preview", json.loads(result.stdout)["mode"])
            self.assertEqual(before, tree_fingerprint(root))

        for script in (
            "create_live_fixture.ps1",
            "run_live_behavior_tests.ps1",
            "cleanup_live_fixture.ps1",
        ):
            content = (SCRIPTS / script).read_text(encoding="utf-8")
            self.assertNotIn("Invoke-Expression", content)
            self.assertNotIn("Start-Process", content)
            self.assertIn("@pythonArguments", content)


class FixtureAndSnapshotTests(LiveHarnessTestCase):
    def test_authorized_setup_is_owned_local_and_defective(self) -> None:
        root = self.create()
        self.assertEqual(
            sorted([live.MARKER_NAME, *live.EXPECTED_CHILDREN]),
            sorted(path.name for path in root.iterdir()),
        )
        marker_root, marker = live.validate_marker(root)
        self.assertEqual(root, marker_root)
        self.assertEqual("ready", marker["lifecycle"])
        self.assertEqual([], marker["activeWorkers"])
        self.assertFalse((root / "fixture" / ".git" / "config").read_text(encoding="utf-8").find("example.invalid") < 0)
        calculator = (root / "fixture" / "src" / "calculator.py").read_text(encoding="utf-8")
        self.assertIn("left - right", calculator)
        self.assertTrue((root / "remote.git" / "HEAD").is_file())

    def test_snapshot_covers_index_status_ignored_hashes_worktrees_remote_config_hooks(self) -> None:
        root = self.create()
        snapshot = live.capture_git_snapshot(root)
        self.assertEqual(live.SNAPSHOT_SCHEMA, snapshot["schema"])
        self.assertRegex(snapshot["head"], r"^[0-9a-f]{40}$")
        self.assertIn(".live-cache/controlled.txt", snapshot["fileHashes"])
        self.assertIn(".githooks/pre-commit", snapshot["hooks"])
        self.assertEqual(3, len(snapshot["worktrees"]))
        self.assertIn("refs/heads/main", snapshot["remoteRefs"])
        self.assertEqual("RUN_ROOT/remote.git", snapshot["localConfig"]["remote.origin.url"])
        self.assertTrue(snapshot["index"])
        self.assertTrue(any(".live-cache/" in item for item in snapshot["status"]))

        untracked = root / "fixture" / "untracked.txt"
        untracked.write_text("untracked\n", encoding="utf-8")
        changed = live.capture_git_snapshot(root)
        self.assertIn("untracked.txt", changed["fileHashes"])
        self.assertTrue(any("untracked.txt" in item for item in changed["status"]))

    def test_snapshot_digest_covers_every_checkout_empty_directories_and_all_refs(self) -> None:
        root = self.create()
        previous = live._snapshot_digest(live.capture_git_snapshot(root))

        mutations = (
            (root / "fixture" / ".live-cache" / "arbitrary.bin", b"ignored\x00content"),
            (root / "fixture" / "empty-primary", None),
            (root / "worktree-a" / "ignored-by-rule.tmp", b"worker a\n"),
            (root / "worktree-b" / "empty-worker-b", None),
        )
        for path, content in mutations:
            with self.subTest(path=path.relative_to(root).as_posix()):
                if content is None:
                    path.mkdir()
                else:
                    path.write_bytes(content)
                snapshot = live.capture_git_snapshot(root)
                digest = live._snapshot_digest(snapshot)
                self.assertNotEqual(previous, digest)
                previous = digest

        head = live._git_checked(root, root / "fixture", ["rev-parse", "HEAD"], 30).strip()
        for repository, reference in (
            (root / "fixture", "refs/tags/local-snapshot"),
            (root / "remote.git", "refs/tags/remote-snapshot"),
        ):
            with self.subTest(reference=reference):
                live._git_checked(root, repository, ["update-ref", reference, head], 30)
                snapshot = live.capture_git_snapshot(root)
                digest = live._snapshot_digest(snapshot)
                self.assertNotEqual(previous, digest)
                previous = digest

        self.assertIn("refs/tags/local-snapshot", snapshot["refs"])
        self.assertIn("refs/tags/remote-snapshot", snapshot["remoteRefs"])
        inventories = snapshot["checkoutInventories"]
        self.assertIn("empty-primary", inventories["fixture"]["directories"])
        self.assertIn("empty-worker-b", inventories["worktree-b"]["directories"])
        self.assertIn(".live-cache/arbitrary.bin", inventories["fixture"]["files"])
        self.assertIn("ignored-by-rule.tmp", inventories["worktree-a"]["files"])

    def test_runner_records_expected_failure_without_raw_streams_or_runtime_claim(self) -> None:
        root = self.create()
        outcome = live.run_behavior_tests(root, allow_live=True)
        self.assertEqual("PASS", outcome["fixtureKnownFailure"])
        self.assertEqual("NOT READY", outcome["releaseDecision"])
        summary = live.validate_summary(
            json.loads(
                (root / "results" / "live-validation-summary.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        self.assertFalse(summary["runtimeValidated"])
        evidence = json.loads(
            (root / "results" / "cases" / "fixture-known-failure.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(evidence["streamsPersisted"])
        self.assertNotIn("stdout", evidence)
        self.assertNotIn("stderr", evidence)
        self.assertEqual("ready", self.read_marker(root)["lifecycle"])
        live.validate_marker(root)

    def test_interrupted_setup_is_retained_with_evidence(self) -> None:
        with patch.object(live, "_git_checked", side_effect=live.LiveValidationError("injected")):
            with self.assertRaises(live.LiveValidationError):
                live.create_fixture(
                    REPOSITORY_ROOT,
                    allow_live=True,
                    temp_base=self.base,
                )
        roots = list(self.base.iterdir())
        self.assertEqual(1, len(roots))
        marker = self.read_marker(roots[0])
        self.assertEqual("interrupted", marker["lifecycle"])
        self.assertEqual([], marker["activeWorkers"])
        self.assertTrue((roots[0] / "results" / "interruption.json").is_file())
        live.validate_marker(roots[0])

    def test_failure_during_first_owned_child_creation_retains_strict_marker(self) -> None:
        original_mkdir = Path.mkdir
        failed = False

        def fail_first_fixture_mkdir(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal failed
            if path.name == "fixture" and not failed:
                failed = True
                raise OSError("injected earliest child failure")
            original_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", new=fail_first_fixture_mkdir):
            with self.assertRaises(live.LiveValidationError):
                live.create_fixture(
                    REPOSITORY_ROOT,
                    allow_live=True,
                    temp_base=self.base,
                )
        roots = list(self.base.iterdir())
        self.assertEqual(1, len(roots))
        marker = self.read_marker(roots[0])
        self.assertEqual("interrupted", marker["lifecycle"])
        self.assertEqual([], marker["activeWorkers"])
        self.assertTrue((roots[0] / "results" / "interruption.json").is_file())
        live.validate_marker(roots[0])

    def test_interrupted_runner_retains_marker_and_finalized_evidence(self) -> None:
        root = self.create()
        original = live.run_bounded

        def fail_python(
            run_root: Path,
            argv: list[str],
            cwd: Path,
            timeout: int,
            *,
            allowed: str,
        ) -> subprocess.CompletedProcess[str]:
            if allowed == "python":
                raise live.LiveValidationError("injected runner interruption")
            return original(run_root, argv, cwd, timeout, allowed=allowed)

        with patch.object(live, "run_bounded", side_effect=fail_python):
            with self.assertRaises(live.LiveValidationError):
                live.run_behavior_tests(root, allow_live=True)
        marker = self.read_marker(root)
        self.assertEqual("interrupted", marker["lifecycle"])
        self.assertEqual([], marker["activeWorkers"])
        self.assertTrue((root / "results" / "runner-interruption.json").is_file())
        live.validate_marker(root)

    def test_secret_inputs_are_hashed_but_never_persisted_as_evidence(self) -> None:
        root = self.create()
        secrets = [
            "Bearer synthetic-access-value",
            "api_key=synthetic-api-value",
            "postgres://fixture:synthetic-password@example.invalid/db",
            "-----BEGIN PRIVATE KEY-----synthetic-----END PRIVATE KEY-----",
        ]
        (root / "fixture" / "secret-input.txt").write_text(
            "\n".join(secrets) + "\n", encoding="utf-8"
        )
        outcome = live.run_behavior_tests(root, allow_live=True)
        self.assertEqual("PASS", outcome["fixtureKnownFailure"])
        persisted = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (root / "results").rglob("*")
            if path.is_file()
        )
        persisted += (root / live.MARKER_NAME).read_text(encoding="utf-8")
        for secret in secrets:
            self.assertNotIn(secret, persisted)

    def test_prefixed_secret_assignments_are_rejected_before_persistence(self) -> None:
        for secret in (
            "OPENAI_API_KEY=synthetic-openai-value",
            "AWS_SECRET_ACCESS_KEY=synthetic-aws-value",
        ):
            with self.subTest(secret_name=secret.split("=", 1)[0]):
                with self.assertRaises(live.LiveValidationError):
                    live.atomic_write_json(
                        self.base / f"{uuid.uuid4().hex}.json",
                        {"reason": secret},
                    )


class CleanupSafetyTests(LiveHarnessTestCase):
    def assert_refusal_preserves(self, root: Path) -> None:
        before = tree_fingerprint(root)
        with self.assertRaises((live.LiveValidationError, OSError)):
            live.cleanup_fixture(root, apply_cleanup=True)
        self.assertEqual(before, tree_fingerprint(root))

    def test_cleanup_refusals_delete_nothing(self) -> None:
        mutations = (
            "bad-marker",
            "missing-marker",
            "escape",
            "identity",
            "unknown-child",
            "active-worker",
            "missing-evidence",
            "tampered-evidence",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                root = self.create()
                marker = self.read_marker(root)
                if mutation == "bad-marker":
                    marker.pop("schema")
                    self.write_marker(root, marker)
                elif mutation == "missing-marker":
                    (root / live.MARKER_NAME).unlink()
                elif mutation == "escape":
                    marker["runRoot"] = str(root.parent)
                    self.write_marker(root, marker)
                elif mutation == "identity":
                    marker["rootIdentity"]["fileId"] = "different"
                    self.write_marker(root, marker)
                elif mutation == "unknown-child":
                    (root / "unknown").mkdir()
                elif mutation == "active-worker":
                    marker["activeWorkers"] = [str(uuid.uuid4())]
                    self.write_marker(root, marker)
                elif mutation == "missing-evidence":
                    (root / "results" / "environment.json").unlink()
                elif mutation == "tampered-evidence":
                    (root / "results" / "environment.json").write_text(
                        "{}\n", encoding="utf-8"
                    )
                self.assert_refusal_preserves(root)

    def test_reparse_refusal_deletes_nothing(self) -> None:
        root = self.create()
        external = self.base / "external"
        external.mkdir()
        link = root / "fixture" / "reparse"
        try:
            os.symlink(external, link, target_is_directory=True)
        except OSError:
            before = tree_fingerprint(root)
            with patch.object(
                live,
                "_assert_tree_reparse_free",
                side_effect=live.LiveValidationError("reparse"),
            ):
                with self.assertRaises(live.LiveValidationError):
                    live.cleanup_fixture(root, apply_cleanup=True)
            self.assertEqual(before, tree_fingerprint(root))
        else:
            self.assert_refusal_preserves(root)
            self.assertTrue(external.is_dir())

    def test_successful_cleanup_preserves_root_marker_and_results(self) -> None:
        root = self.create()
        results_before = tree_fingerprint(root / "results")
        outcome = live.cleanup_fixture(root, apply_cleanup=True)
        self.assertEqual("cleaned", outcome["mode"])
        self.assertTrue(root.is_dir())
        self.assertEqual(
            [live.MARKER_NAME, "results"], sorted(path.name for path in root.iterdir())
        )
        self.assertEqual(results_before, tree_fingerprint(root / "results"))
        self.assertEqual("cleaned", self.read_marker(root)["lifecycle"])
        live.validate_marker(root)

    def test_partial_cleanup_failure_preserves_evidence_and_is_resumable(self) -> None:
        root = self.create()
        results_before = tree_fingerprint(root / "results")
        original_remove = live._remove_owned_tree
        calls = 0

        def fail_second_removal(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected partial cleanup failure")
            original_remove(path, *args, **kwargs)

        with patch.object(live, "_remove_owned_tree", side_effect=fail_second_removal):
            with self.assertRaises(OSError):
                live.cleanup_fixture(root, apply_cleanup=True)
        self.assertEqual("cleaning", self.read_marker(root)["lifecycle"])
        self.assertEqual(results_before, tree_fingerprint(root / "results"))
        live.validate_marker(root)

        outcome = live.cleanup_fixture(root, apply_cleanup=True)
        self.assertEqual("cleaned", outcome["mode"])
        self.assertEqual(results_before, tree_fingerprint(root / "results"))
        live.validate_marker(root)

    def test_namespace_replacement_refuses_without_traversing_external_sentinel(self) -> None:
        root = self.create()
        external = self.base / "external-sentinel"
        external.mkdir()
        sentinel = external / "must-survive.txt"
        sentinel.write_text("external\n", encoding="utf-8")
        displaced = self.base / "displaced-fixture"
        original_validate = live.validate_marker
        calls = 0

        def replace_before_destructive_revalidation(
            run_root: str | os.PathLike[str],
            *args: object,
            **kwargs: object,
        ) -> tuple[Path, dict[str, object]]:
            nonlocal calls
            calls += 1
            if calls == 2:
                os.replace(root / "fixture", displaced)
                os.symlink(external, root / "fixture", target_is_directory=True)
            return original_validate(run_root, *args, **kwargs)

        try:
            with patch.object(
                live,
                "validate_marker",
                side_effect=replace_before_destructive_revalidation,
            ):
                with self.assertRaises((live.LiveValidationError, OSError)):
                    live.cleanup_fixture(root, apply_cleanup=True)
        except OSError as error:
            self.skipTest(f"Directory symlink creation is unavailable: {error}")
        self.assertEqual("external\n", sentinel.read_text(encoding="utf-8"))


class AggregationAndReportTests(LiveHarnessTestCase):
    run_id = "00000000-0000-4000-8000-000000000111"
    revision = "1" * 40
    configuration_hash = "2" * 64

    def aggregate(
        self,
        text: str,
        manifest: dict[str, object],
        run_root: Path | None = None,
    ) -> dict[str, object]:
        return live.aggregate_events(
            text,
            manifest,
            run_id=self.run_id,
            platform="windows-native",
            codex_version="0.154.0",
            repository_commit=self.revision,
            configuration_hash=self.configuration_hash,
            run_root=run_root,
        )

    def event(self, record: dict[str, object]) -> str:
        return json.dumps(
            {"schema": live.EVENT_SCHEMA, "runId": self.run_id, "case": record},
            sort_keys=True,
        ) + "\n"

    def complete_synthetic_pass(self, case_id: str = "case-one") -> dict[str, object]:
        record = live.blank_case(case_id, True)
        record.update(
            {
                "executionStatus": "Complete",
                "result": "PASS",
                "reason": "Synthetic pass",
                "startedAt": "2026-09-18T12:00:00+00:00",
                "completedAt": "2026-09-18T12:00:00.001000+00:00",
                "durationMs": 1,
                "command": "synthetic offline check",
                "effectivePermissions": ["fixture-read"],
                "beforeStateHash": "a" * 64,
                "afterStateHash": "a" * 64,
                "evidencePaths": ["results/cases/synthetic.json"],
                "exitCode": 0,
            }
        )
        return record

    def test_execution_statuses_are_exact_and_in_progress_is_structural(self) -> None:
        self.assertEqual(
            {"Not started", "In progress", "Blocked", "Complete"},
            live.EXECUTION_STATUSES,
        )
        manifest_entry = {"id": "case-one", "critical": True}
        for status in ("Unverified", "Interrupted", "complete", "PASS"):
            with self.subTest(status=status):
                record = live.blank_case("case-one", True)
                record["executionStatus"] = status
                with self.assertRaises(live.LiveValidationError):
                    live.validate_case_record(record, manifest_entry)

        in_progress = live.blank_case("case-one", True)
        in_progress.update(
            {
                "executionStatus": "In progress",
                "reason": "Synthetic check is running",
                "startedAt": "2026-09-18T12:00:00+00:00",
                "command": "synthetic offline check",
                "effectivePermissions": ["fixture-read"],
                "beforeStateHash": "a" * 64,
            }
        )
        live.validate_case_record(in_progress, manifest_entry)
        in_progress["completedAt"] = live.utc_now()
        with self.assertRaises(live.LiveValidationError):
            live.validate_case_record(in_progress, manifest_entry)

    def test_structurally_incomplete_or_contradictory_pass_is_rejected(self) -> None:
        manifest_entry = {"id": "case-one", "critical": True}
        required_fields = (
            "startedAt",
            "completedAt",
            "durationMs",
            "command",
            "effectivePermissions",
            "beforeStateHash",
            "afterStateHash",
            "evidencePaths",
        )
        for field in required_fields:
            with self.subTest(field=field):
                record = self.complete_synthetic_pass()
                record[field] = [] if field in {"effectivePermissions", "evidencePaths"} else None
                with self.assertRaises(live.LiveValidationError):
                    live.validate_case_record(record, manifest_entry)

        contradictory = self.complete_synthetic_pass()
        contradictory["failedAssertion"] = "should not exist on PASS"
        with self.assertRaises(live.LiveValidationError):
            live.validate_case_record(contradictory, manifest_entry)

        synthetic = self.complete_synthetic_pass("fixture-known-failure")
        live.validate_case_record(
            synthetic,
            {"id": "fixture-known-failure", "critical": True},
        )
        self.assertIsNone(synthetic["parentSessionId"])
        self.assertIsNone(synthetic["observedModel"])

    def test_checked_in_synthetic_fixture_has_expected_fail_closed_outcome(self) -> None:
        manifest = json.loads(
            (FIXTURES / "expected-cases.json").read_text(encoding="utf-8")
        )
        text = (FIXTURES / "events.unattempted.jsonl").read_text(encoding="utf-8")
        expected = json.loads(
            (FIXTURES / "expected-outcomes.json").read_text(encoding="utf-8")
        )["events.unattempted.jsonl"]
        summary = self.aggregate(text, manifest)
        self.assertEqual(expected["releaseDecision"], summary["releaseDecision"])
        self.assertEqual(expected["runtimeValidated"], summary["runtimeValidated"])
        for error in expected["requiredErrors"]:
            self.assertIn(error, summary["validationErrors"])

    def test_missing_duplicate_unknown_nonpass_and_evidence_matrices_are_not_ready(self) -> None:
        manifest = {
            "schema": live.CASE_MANIFEST_SCHEMA,
            "cases": [{"id": "case-one", "critical": True}],
        }
        missing = self.aggregate("", manifest)
        self.assertIn("MISSING_CASE:case-one", missing["validationErrors"])

        record = live.blank_case("case-one", True)
        duplicate = self.aggregate(self.event(record) * 2, manifest)
        self.assertIn("DUPLICATE_CASE:case-one", duplicate["validationErrors"])

        unknown = live.blank_case("unknown", True)
        unknown_summary = self.aggregate(self.event(unknown), manifest)
        self.assertIn("UNKNOWN_CASE:unknown", unknown_summary["validationErrors"])

        failed = live.blank_case("case-one", True)
        failed.update(
            {
                "executionStatus": "Complete",
                "result": "FAIL",
                "reason": "Synthetic failure",
                "startedAt": live.utc_now(),
                "completedAt": live.utc_now(),
                "durationMs": 1,
                "command": "synthetic offline check",
                "failedAssertion": "synthetic assertion",
                "effectivePermissions": ["fixture-read"],
                "beforeStateHash": "a" * 64,
                "afterStateHash": "a" * 64,
                "evidencePaths": ["results/cases/synthetic-failure.json"],
                "exitCode": 1,
            }
        )
        failed_summary = self.aggregate(self.event(failed), manifest)
        self.assertIn("NON_PASS:case-one", failed_summary["validationErrors"])

        passing = self.complete_synthetic_pass()
        passing["evidencePaths"] = ["results/cases/missing.json"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "results").mkdir()
            evidence_summary = self.aggregate(self.event(passing), manifest, root)
        self.assertIn("MISSING_EVIDENCE:case-one", evidence_summary["validationErrors"])

    def test_contradictions_malformed_events_and_secrets_are_rejected(self) -> None:
        manifest = {
            "schema": live.CASE_MANIFEST_SCHEMA,
            "cases": [{"id": "case-one", "critical": True}],
        }
        contradictory = live.blank_case("case-one", True)
        contradictory["result"] = "PASS"
        with self.assertRaises(live.LiveValidationError):
            self.aggregate(self.event(contradictory), manifest)
        with self.assertRaises(live.LiveValidationError):
            self.aggregate("not-json\n", manifest)
        secret = live.blank_case("case-one", True)
        secret["reason"] = "token=super-secret-value"
        with self.assertRaises(live.LiveValidationError):
            self.aggregate(self.event(secret), manifest)

    def test_report_renders_valid_summary_and_rejects_invalid_or_secret_input(self) -> None:
        manifest = {
            "schema": live.CASE_MANIFEST_SCHEMA,
            "cases": [{"id": "case-one", "critical": True}],
        }
        summary = self.aggregate("", manifest)
        template = (REPOSITORY_ROOT / "templates" / "LIVE_VALIDATION_REPORT.md").read_text(
            encoding="utf-8"
        )
        report = live.render_report(summary, template)
        self.assertIn("NOT READY", report)
        self.assertIn("runtime", report.lower())
        invalid = dict(summary)
        invalid["runtimeValidated"] = True
        with self.assertRaises(live.LiveValidationError):
            live.render_report(invalid, template)
        secret = json.loads(json.dumps(summary))
        secret["tests"][0]["reason"] = "Bearer synthetic-secret"
        with self.assertRaises(live.LiveValidationError):
            live.render_report(secret, template)

    def test_report_generator_updates_owned_evidence_manifest(self) -> None:
        root = self.create()
        live.run_behavior_tests(root, allow_live=True)
        output = root / "results" / "LIVE_VALIDATION.md"
        result = live.generate_report(
            root / "results" / "live-validation-summary.json",
            REPOSITORY_ROOT / "templates" / "LIVE_VALIDATION_REPORT.md",
            output,
        )
        self.assertEqual("NOT READY", result["releaseDecision"])
        self.assertTrue(output.is_file())
        live.validate_marker(root)


if __name__ == "__main__":
    unittest.main()
