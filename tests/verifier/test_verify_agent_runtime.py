from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import verify_agent_runtime as verifier


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
        detect_side_effect: object = "0.154.0",
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
                detect_side_effect="0.155.0",
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
                self.complete_stream().replace('"agent_name": "code-explorer"', '"agent_name": "code"'),
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
            with patch.object(verifier, "_detect_codex_version", return_value="0.154.0"):
                with patch.object(verifier, "verify_installed", return_value=[]):
                    with patch.object(verifier, "_write_evidence", side_effect=OSError("secret path")):
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            exit_code = verifier.main()

        output = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(1, exit_code)
        self.assertTrue(output.startswith("FAIL:"))
        self.assertNotIn("secret path", output)


if __name__ == "__main__":
    unittest.main()
