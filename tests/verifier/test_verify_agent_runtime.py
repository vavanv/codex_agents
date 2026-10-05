from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import verify_agent_runtime as verifier
import codex_compatibility as compatibility
import validate_agent_configs as catalog


class VerifyAgentRuntimeTests(unittest.TestCase):
    parent_session_id = "00000000-0000-4000-8000-000000000001"

    @staticmethod
    def child_session_id(index: int) -> str:
        return f"00000000-0000-4000-8000-{index + 2:012d}"

    def process(
        self,
        return_code: int | None = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        launch_error_code: str | None = None,
    ) -> verifier.DiscoveryProcessResult:
        return verifier.DiscoveryProcessResult(
            return_code,
            stdout,
            stderr,
            timed_out,
            launch_error_code,
        )

    def complete_stream(self, extra_events: list[dict[str, object]] | None = None) -> str:
        events: list[dict[str, object]] = [
            {"type": "thread.started", "thread_id": self.parent_session_id}
        ]
        for index, role in enumerate(sorted(verifier.EXPECTED_ROLES)):
            events.append(
                {
                    "type": "agent.spawned",
                    "agent_name": role,
                    "child_session_id": self.child_session_id(index),
                }
            )
        events.extend(extra_events or [])
        return "".join(json.dumps(event) + "\n" for event in events)

    def invoke(
        self,
        process_result: verifier.DiscoveryProcessResult | None = None,
        run_codex: bool = True,
        install_errors: list[str] | None = None,
        detect_side_effect: object = "0.155.1",
        evidence_path: Path | None = None,
    ) -> tuple[int, str, dict[str, object] | None]:
        arguments = ["verify_agent_runtime.py", "--target", "."]
        if run_codex:
            arguments.append("--run-codex")
        if evidence_path is not None:
            arguments.extend(["--evidence", str(evidence_path)])
        stdout = StringIO()
        stderr = StringIO()
        detect_kwargs = (
            {"side_effect": detect_side_effect}
            if isinstance(detect_side_effect, BaseException)
            else {"return_value": detect_side_effect}
        )
        with patch.object(sys, "argv", arguments):
            with patch.object(verifier, "_detect_codex_version", **detect_kwargs):
                with patch.object(verifier, "verify_installed", return_value=install_errors or []):
                    with patch.object(
                        verifier,
                        "run_discovery",
                        return_value=process_result or self.process(stdout=self.complete_stream()),
                    ):
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            exit_code = verifier.main()
        evidence = None
        if evidence_path is not None and evidence_path.exists():
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        return exit_code, stdout.getvalue() + stderr.getvalue(), evidence

    def test_static_success_is_pass_with_discovery_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "evidence.json"
            exit_code, output, evidence = self.invoke(
                run_codex=False,
                evidence_path=evidence_path,
            )

        self.assertEqual(0, exit_code)
        self.assertTrue(output.startswith("PASS:"))
        self.assertEqual("PASS", evidence["status"])
        self.assertEqual(0, evidence["exitCode"])
        self.assertEqual("UNVERIFIED", evidence["discovery"])
        self.assertEqual("installed-only", evidence["scope"])

    def test_invalid_installation_and_version_detection_fail_consistently(self) -> None:
        cases = (
            ({"install_errors": ["missing"]}, "INSTALLATION_INVALID"),
            ({"detect_side_effect": RuntimeError("secret failure")}, "CODEX_VERSION_ERROR"),
        )
        for keyword_arguments, reason in cases:
            with self.subTest(reason=reason):
                with tempfile.TemporaryDirectory() as directory:
                    exit_code, output, evidence = self.invoke(
                        evidence_path=Path(directory) / "evidence.json",
                        **keyword_arguments,
                    )
                self.assertEqual(1, exit_code)
                self.assertTrue(output.startswith("FAIL:"))
                self.assertEqual("FAIL", evidence["status"])
                self.assertEqual(1, evidence["exitCode"])
                self.assertIn(reason, evidence["reasonCodes"])

    def test_unsupported_runtime_version_fails_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exit_code, output, evidence = self.invoke(
                detect_side_effect="0.154.0",
                evidence_path=Path(directory) / "evidence.json",
            )

        self.assertEqual(1, exit_code)
        self.assertTrue(output.startswith("FAIL:"))
        self.assertEqual("FAIL", evidence["status"])
        self.assertEqual(1, evidence["exitCode"])
        self.assertIn("UNSUPPORTED_RUNTIME_VERSION", evidence["reasonCodes"])

    def test_process_outcomes_have_consistent_status_and_exit_codes(self) -> None:
        cases = (
            (self.process(return_code=7, stderr="general failure"), "FAIL", 1, "DISCOVERY_PROCESS_FAILED"),
            (self.process(return_code=1, stderr="not authenticated"), "BLOCKED", 2, "AUTH_BLOCKED"),
            (self.process(return_code=1, stderr="usage limit reached"), "BLOCKED", 2, "QUOTA_BLOCKED"),
            (self.process(return_code=None, launch_error_code="CODEX_EXECUTABLE_MISSING"), "BLOCKED", 2, "CODEX_EXECUTABLE_MISSING"),
            (self.process(return_code=None, launch_error_code="PermissionError"), "FAIL", 1, "DISCOVERY_LAUNCH_FAILED"),
            (self.process(return_code=None, timed_out=True), "BLOCKED", 2, "DISCOVERY_TIMEOUT"),
        )
        for process_result, status, expected_exit, reason in cases:
            with self.subTest(reason=reason):
                with tempfile.TemporaryDirectory() as directory:
                    exit_code, output, evidence = self.invoke(
                        process_result,
                        evidence_path=Path(directory) / "evidence.json",
                    )
                self.assertEqual(expected_exit, exit_code)
                self.assertTrue(output.startswith(f"{status}:"))
                self.assertEqual(status, evidence["status"])
                if status == "PASS":
                    expected_mapping = {
                        role: self.child_session_id(index)
                        for index, role in enumerate(sorted(verifier.EXPECTED_ROLES))
                    }
                    self.assertEqual(expected_mapping, evidence["childSessionIds"])
                self.assertEqual(expected_exit, evidence["exitCode"])
                self.assertIn(reason, evidence["reasonCodes"])

    def test_unvalidated_adapter_never_passes_even_with_complete_attribution(self) -> None:
        duplicate = {
            "type": "agent.spawned",
            "agent_name": sorted(verifier.EXPECTED_ROLES)[0],
            "child_session_id": self.child_session_id(0),
        }
        cases = (
            (self.complete_stream(), "UNVERIFIED", 3),
            (self.complete_stream([duplicate]), "UNVERIFIED", 3),
            (
                json.dumps({"type": "thread.started", "thread_id": self.parent_session_id})
                + "\n"
                + json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "\n".join(sorted(verifier.EXPECTED_ROLES)),
                        },
                    }
                )
                + "\n",
                "UNVERIFIED",
                3,
            ),
            (
                self.complete_stream().replace('"agent_name": "code_explorer"', '"agent_name": "code"'),
                "UNVERIFIED",
                3,
            ),
        )
        for stream, status, expected_exit in cases:
            with self.subTest(status=status, expected_exit=expected_exit):
                with tempfile.TemporaryDirectory() as directory:
                    exit_code, output, evidence = self.invoke(
                        self.process(stdout=stream),
                        evidence_path=Path(directory) / "evidence.json",
                    )
                self.assertEqual(expected_exit, exit_code)
                self.assertTrue(output.startswith(f"{status}:"))
                self.assertEqual(status, evidence["status"])

    def test_duplicate_and_reordered_attribution_are_explicitly_unverified(self) -> None:
        role = sorted(verifier.EXPECTED_ROLES)[0]
        duplicate = json.loads(self.complete_stream().splitlines()[1])
        complete = self.complete_stream()
        lines = complete.splitlines(keepends=True)
        reordered = lines[1] + lines[0] + "".join(lines[2:])
        cases = (
            (complete + json.dumps(duplicate) + "\n", "DUPLICATE_ATTRIBUTION"),
            (reordered, "ATTRIBUTION_BEFORE_PARENT"),
        )
        for stream, reason in cases:
            with self.subTest(reason=reason):
                exit_code, _, evidence = self.invoke(self.process(stdout=stream))
                self.assertEqual(3, exit_code)
                self.assertEqual("UNVERIFIED", evidence["status"] if evidence else "UNVERIFIED")
                parsed = verifier.parse_discovery(stream)
                self.assertIn(reason, parsed.reason_codes)
                self.assertIn(role, parsed.attributed_children)

    def test_stderr_and_partial_attribution_cannot_pass(self) -> None:
        parent_only = json.dumps({"type": "thread.started", "thread_id": self.parent_session_id}) + "\n"
        cases = (
            self.process(stdout=parent_only, stderr=self.complete_stream()),
            self.process(stdout=self.complete_stream().splitlines()[0] + "\n"),
        )
        for process_result in cases:
            with self.subTest(stderr_only=bool(process_result.stderr)):
                exit_code, _, _ = self.invoke(process_result)
                self.assertEqual(3, exit_code)

    def test_malformed_truncated_and_unsupported_streams_are_unverified(self) -> None:
        cases = (
            "not-json\n",
            json.dumps({"type": "thread.started", "thread_id": self.parent_session_id}),
            json.dumps({"unexpected": "schema"}) + "\n",
            json.dumps({"type": "future.event"}) + "\n",
        )
        for stream in cases:
            with self.subTest(stream=stream):
                exit_code, _, evidence = self.invoke(self.process(stdout=stream))
                self.assertEqual(3, exit_code)
                self.assertEqual("UNVERIFIED", evidence["status"] if evidence else "UNVERIFIED")

    def test_conflicting_child_mapping_is_unverified(self) -> None:
        role = sorted(verifier.EXPECTED_ROLES)[0]
        conflict = {
            "type": "agent.spawned",
            "agent_name": role,
            "child_session_id": "00000000-0000-4000-8000-999999999999",
        }
        exit_code, _, _ = self.invoke(self.process(stdout=self.complete_stream([conflict])))
        self.assertEqual(3, exit_code)

    def test_child_session_ids_must_be_plausible_distinct_and_non_secret(self) -> None:
        role = sorted(verifier.EXPECTED_ROLES)[0]
        cases = (
            self.parent_session_id,
            "token=synthetic-secret-value",
            "not-a-session-id",
        )
        for child_id in cases:
            with self.subTest(child_id=child_id):
                stream = self.complete_stream().replace(self.child_session_id(0), child_id)
                with tempfile.TemporaryDirectory() as directory:
                    evidence_path = Path(directory) / "evidence.json"
                    exit_code, output, evidence = self.invoke(
                        self.process(stdout=stream),
                        evidence_path=evidence_path,
                    )
                    persisted = evidence_path.read_text(encoding="utf-8")

                self.assertEqual(3, exit_code)
                self.assertIn("INVALID_CHILD_SESSION", evidence["reasonCodes"])
                self.assertNotIn(role, evidence["childSessionIds"])
                if "secret" in child_id:
                    self.assertNotIn("synthetic-secret-value", output)
                    self.assertNotIn("synthetic-secret-value", persisted)

    def test_structured_error_event_is_unverified_without_persisting_message(self) -> None:
        error_event = {
            "type": "error",
            "message": "Bearer synthetic-secret-value",
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "evidence.json"
            exit_code, output, evidence = self.invoke(
                self.process(stdout=self.complete_stream([error_event])),
                evidence_path=evidence_path,
            )
            persisted = evidence_path.read_text(encoding="utf-8")

        self.assertEqual(3, exit_code)
        self.assertTrue(output.startswith("UNVERIFIED:"))
        self.assertEqual("UNVERIFIED", evidence["status"])
        self.assertEqual(3, evidence["exitCode"])
        self.assertIn("DISCOVERY_ERROR_EVENT", evidence["reasonCodes"])
        self.assertNotIn("synthetic-secret-value", output)
        self.assertNotIn("synthetic-secret-value", persisted)

    def test_run_discovery_converts_oserror_and_timeout_without_invoking_codex(self) -> None:
        with patch.object(verifier.shutil, "which", return_value="codex"):
            with patch.object(verifier.subprocess, "run", side_effect=OSError("synthetic")):
                launch = verifier.run_discovery(Path.cwd(), 1)
            with patch.object(
                verifier.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["codex"], 1, output="partial"),
            ):
                timeout = verifier.run_discovery(Path.cwd(), 1)

        self.assertIsNotNone(launch.launch_error_code)
        self.assertTrue(timeout.timed_out)

    def test_evidence_is_allowlisted_and_secrets_never_reach_output(self) -> None:
        secrets = (
            "Authorization: Bearer bearer-value ",
            "Authorization: Basic basic-value ",
            "postgres://user:password@example.test/db?token=query-secret ",
            "api_key=api-value password=pw token=tok secret=sec connection_string=conn ",
            "sk-exampleSecret ",
            "-----BEGIN PRIVATE KEY-----private-material-----END PRIVATE KEY-----",
        )
        raw = "".join(secrets)
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "evidence.json"
            _, output, evidence = self.invoke(
                self.process(return_code=9, stdout=raw, stderr=raw),
                evidence_path=evidence_path,
            )
            persisted = evidence_path.read_text(encoding="utf-8")

        for secret in ("bearer-value", "basic-value", "password@example", "query-secret", "api-value", "private-material", "sk-exampleSecret"):
            self.assertNotIn(secret, output)
            self.assertNotIn(secret, persisted)
        self.assertEqual(
            {
                "schemaVersion", "checkedAt", "scope", "codexVersion", "status", "exitCode",
                "installation", "discovery", "expectedAgents", "attributedAgents",
                "childSessionIds", "smokeNamesObserved", "eventAdapter", "eventCount", "parseErrorCount",
                "reasonCodes",
            },
            set(evidence),
        )
        self.assertNotIn("private-material", verifier._sanitize_diagnostic(raw))

    def test_evidence_write_failure_returns_fail_without_raw_error(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        arguments = ["verify_agent_runtime.py", "--target", ".", "--evidence", "ignored.json"]
        with patch.object(sys, "argv", arguments):
            with patch.object(verifier, "_detect_codex_version", return_value="0.155.1"):
                with patch.object(verifier, "verify_installed", return_value=[]):
                    with patch.object(verifier, "_write_evidence", side_effect=OSError("secret path")):
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            exit_code = verifier.main()

        output = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(1, exit_code)
        self.assertTrue(output.startswith("FAIL:"))
        self.assertNotIn("secret path", output)


class DiscoveryPolicyTests(unittest.TestCase):
    def setUp(self):
        disposable = tempfile.TemporaryDirectory()
        self.addCleanup(disposable.cleanup)
        self.directory = Path(disposable.name)
        self.source = self.directory / "source"
        self.target = self.directory / "target"
        self.target.mkdir()
        (self.source / "compatibility").mkdir(parents=True)
        self.registry_path = self.source / "compatibility/codex-agents.json"
        self.registry_path.write_bytes((REPOSITORY_ROOT / "compatibility/codex-agents.json").read_bytes())
        self.document = json.loads(self.registry_path.read_bytes())
        for source_relative, installed_relative in verifier.CUSTOM_AGENT_FILES.items():
            source = self.source / source_relative
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY_ROOT / source_relative, source)
            target = self.target / installed_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        self.evidence_path = self.directory / "evidence.json"

    def write_policy(self):
        self.registry_path.write_text(json.dumps(self.document), encoding="utf-8")

    def invoke(self, version="0.155.1", run_codex=True, detect_side_effect=None):
        arguments = ["verify", "--target", str(self.target), "--source-root", str(self.source),
                     "--evidence", str(self.evidence_path)]
        if run_codex:
            arguments.append("--run-codex")
        process = verifier.DiscoveryProcessResult(0,
            '{"type":"thread.started","thread_id":"00000000-0000-4000-8000-000000000001"}\n',
            "", False, None)
        with patch.object(sys, "argv", arguments), \
             patch.object(verifier, "_detect_codex_version", return_value=version,
                          side_effect=detect_side_effect) as detect, \
             patch.object(verifier, "run_discovery", return_value=process) as discover, \
             redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = verifier.main()
        return result, json.loads(self.evidence_path.read_bytes()), detect, discover

    def test_independent_discovery_matrix_and_static_only_verification(self):
        oracle = {"0.155.1": 3, "0.157.1": 3, "0.159.0": 3, "0.159.3": 3, "0.160.0": 1}
        for version, expected_exit in oracle.items():
            with self.subTest(version=version):
                result, evidence, _, discover = self.invoke(version)
                self.assertEqual(expected_exit, result)
                self.assertEqual("PASS", evidence["installation"])
                if expected_exit == 3:
                    self.assertEqual("UNVERIFIED", evidence["status"])
                    self.assertIn("UNVALIDATED_EVENT_ADAPTER", evidence["reasonCodes"])
                    discover.assert_called_once()
                else:
                    self.assertEqual("FAIL", evidence["status"])
                    self.assertEqual(["UNSUPPORTED_RUNTIME_VERSION"], evidence["reasonCodes"])
                    discover.assert_not_called()
        result, evidence, _, discover = self.invoke("0.160.0", run_codex=False)
        self.assertEqual(0, result)
        self.assertEqual("PASS", evidence["status"])
        discover.assert_not_called()

    def test_changed_discovery_gate_is_enforced_without_changing_static_gate(self):
        self.document["versions"]["0.159.3"]["gates"]["discoveryDiagnostic"] = False
        self.write_policy()
        result, evidence, _, discover = self.invoke("0.159.3")
        self.assertEqual(1, result)
        self.assertEqual("PASS", evidence["installation"])
        self.assertEqual(["UNSUPPORTED_RUNTIME_VERSION"], evidence["reasonCodes"])
        discover.assert_not_called()
        result, evidence, _, discover = self.invoke("0.159.3", run_codex=False)
        self.assertEqual(0, result)
        self.assertEqual("PASS", evidence["installation"])
        discover.assert_not_called()

    def test_one_bound_snapshot_reaches_real_catalog_after_registry_bytes_change(self):
        original_loader = verifier.load_registry
        captured = []
        def change_bytes(source):
            registry = original_loader(source)
            captured.append(registry)
            self.registry_path.write_bytes(b"changed after immutable snapshot")
            return registry
        with patch.object(verifier, "load_registry", side_effect=change_bytes) as loaded, \
             patch.object(catalog, "load_registry", side_effect=AssertionError("nested policy reload")), \
             patch.object(verifier, "validate_catalog", wraps=verifier.validate_catalog) as validate, \
             patch.object(verifier, "verify_installed", wraps=verifier.verify_installed) as installed:
            result, evidence, _, discover = self.invoke()
        self.assertEqual(3, result)
        self.assertEqual("PASS", evidence["installation"])
        loaded.assert_called_once_with(self.source)
        self.assertIs(captured[0], installed.call_args.kwargs["policy"])
        self.assertIs(captured[0], validate.call_args.kwargs["policy"])
        discover.assert_called_once()

    def test_policy_corruption_missing_and_unsafe_paths_fail_before_version_probe(self):
        original = self.registry_path.read_bytes()
        for content in (b"bad JSON", b'{"schemaVersion":1,"schemaVersion":1}', None):
            for run_codex in (False, True):
                with self.subTest(content=content, run_codex=run_codex):
                    if content is None:
                        self.registry_path.unlink(missing_ok=True)
                    else:
                        self.registry_path.write_bytes(content)
                    result, evidence, detect, discover = self.invoke(run_codex=run_codex)
                    self.assertEqual(1, result)
                    self.assertIsNone(evidence["codexVersion"])
                    self.assertEqual(["CODEX_VERSION_ERROR"], evidence["reasonCodes"])
                    detect.assert_not_called()
                    discover.assert_not_called()
                    self.registry_path.write_bytes(original)
        original_lstat = Path.lstat
        original_resolve = Path.resolve
        def unsafe_metadata(path, *args, **kwargs):
            metadata = original_lstat(path, *args, **kwargs)
            if path == self.source:
                return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
            return metadata
        def source_must_not_resolve(path, *args, **kwargs):
            if path.is_relative_to(self.source):
                raise AssertionError("unsafe source resolved before check")
            return original_resolve(path, *args, **kwargs)
        with patch.object(Path, "lstat", unsafe_metadata), patch.object(Path, "resolve", source_must_not_resolve):
            result, evidence, detect, discover = self.invoke()
        self.assertEqual(1, result)
        self.assertEqual(["CODEX_VERSION_ERROR"], evidence["reasonCodes"])
        detect.assert_not_called()
        discover.assert_not_called()

    def test_foreign_policy_rejected_before_probe_or_agent_reads(self):
        foreign = self.directory / "foreign"
        (foreign / "compatibility").mkdir(parents=True)
        (foreign / "compatibility/codex-agents.json").write_bytes(self.registry_path.read_bytes())
        registry = compatibility.load_registry(foreign)
        with patch.object(verifier, "load_registry", return_value=registry), \
             patch.object(verifier, "verify_installed", side_effect=AssertionError("foreign policy reached agent reads")):
            result, evidence, detect, discover = self.invoke()
        self.assertEqual(1, result)
        self.assertEqual(["CODEX_VERSION_ERROR"], evidence["reasonCodes"])
        detect.assert_not_called()
        discover.assert_not_called()
        with patch.object(verifier, "validate_catalog", side_effect=AssertionError("foreign policy reached catalog")), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("foreign policy read agent")):
            with self.assertRaises(compatibility.RegistryError) as caught:
                verifier.verify_installed(self.target, self.source, "0.155.1", policy=registry)
        self.assertEqual("source_mismatch", caught.exception.category)

    def test_catalog_failure_returns_before_installed_agent_byte_reads(self):
        registry = compatibility.load_registry(self.source)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("invalid catalog reached agent byte reads")):
            errors = verifier.verify_installed(self.target, self.source, "999.0.0", policy=registry)
        self.assertEqual(["unsupported Codex CLI version: 999.0.0"], errors)
        (self.source / "agents/code-explorer.toml").write_bytes(b'name = "unterminated\n')
        with patch.object(Path, "read_bytes", side_effect=AssertionError("invalid TOML reached installed bytes")):
            errors = verifier.verify_installed(self.target, self.source, "0.155.1", policy=registry)
        self.assertTrue(any("invalid TOML" in error for error in errors))

    def test_library_loads_once_and_unrelated_cwd_uses_explicit_source(self):
        with patch.object(verifier, "load_registry", wraps=verifier.load_registry) as loaded, \
             patch.object(catalog, "load_registry", side_effect=AssertionError("catalog reread policy")):
            self.assertEqual([], verifier.verify_installed(self.target, self.source, "0.160.0"))
        loaded.assert_called_once_with(self.source)
        unrelated = self.directory / "unrelated"
        unrelated.mkdir()
        prior = Path.cwd()
        os.chdir(unrelated)
        try:
            with patch.dict(os.environ, {"CODEX_SOURCE_ROOT": str(unrelated), "CODEX_VERSION": "0.160.0"}):
                result, evidence, _, discover = self.invoke("0.159.3")
        finally:
            os.chdir(prior)
        self.assertEqual(3, result)
        self.assertEqual("0.159.3", evidence["codexVersion"])
        self.assertEqual("PASS", evidence["installation"])
        discover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
