from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import validate_agent_configs as validator


class AgentConfigTests(unittest.TestCase):
    def copy_catalog(self, target: Path) -> None:
        shutil.copytree(REPOSITORY_ROOT / "agents", target / "agents")
        (target / "compatibility").mkdir()
        shutil.copyfile(
            REPOSITORY_ROOT / validator.REGISTRY_PATH,
            target / validator.REGISTRY_PATH,
        )

    def test_repository_catalog_is_valid(self) -> None:
        for version in ("0.155.1", "0.157.1", "0.159.0", "0.159.3", "0.160.0"):
            with self.subTest(version=version):
                report = validator.validate_catalog(REPOSITORY_ROOT, version)
                self.assertTrue(report.passed, report.errors)
                self.assertEqual(set(validator.EXPECTED_ROLES), set(report.agents))
                self.assertTrue(any("not yet validated" in warning for warning in report.warnings))

    def test_supported_versions_lists_all_gated_versions(self) -> None:
        self.assertEqual({"0.155.1", "0.157.1", "0.159.0", "0.159.3", "0.160.0"}, validator.supported_versions(REPOSITORY_ROOT))

    def test_0_160_candidate_is_static_and_matches_existing_schema(self) -> None:
        registry = json.loads((REPOSITORY_ROOT / validator.REGISTRY_PATH).read_text(encoding="utf-8"))
        static_fields = ("agentSchemaVersion", "allowedKeys", "models", "requiredKeys",
                         "runtimeValidated", "sandboxModes")
        for field in static_fields:
            self.assertEqual(registry["versions"]["0.159.3"][field],
                             registry["versions"]["0.160.0"][field])
        self.assertEqual({"staticInstallation": True, "discoveryDiagnostic": False,
                          "capturedEvidence": False}, registry["versions"]["0.160.0"]["gates"])
        self.assertEqual({"staticInstallation": True, "discoveryDiagnostic": True,
                          "capturedEvidence": True}, registry["versions"]["0.159.3"]["gates"])
        self.assertEqual([], registry["versions"]["0.160.0"]["rolloutSchemas"])
        self.assertIs(registry["versions"]["0.160.0"]["runtimeValidated"], False)
        report = validator.validate_catalog(REPOSITORY_ROOT, "0.160.0")
        self.assertTrue(report.passed, report.errors)
        self.assertEqual(9, len(report.agents))

    def test_0_160_candidate_does_not_enable_runtime_discovery(self) -> None:
        import verify_agent_runtime as runtime
        import workflow_manager as manager
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for source, destination in manager.CUSTOM_AGENT_FILES.items():
                installed = target / destination
                installed.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPOSITORY_ROOT / source, installed)
            evidence = target / "evidence.json"
            arguments = ["verify", "--target", str(target), "--source-root", str(REPOSITORY_ROOT),
                         "--run-codex", "--evidence", str(evidence)]
            with patch.object(sys, "argv", arguments), redirect_stderr(StringIO()), \
                    patch.object(runtime, "_detect_codex_version", return_value="0.160.0"), \
                    patch.object(runtime, "run_discovery", side_effect=AssertionError("paid discovery forbidden")) as discover:
                self.assertEqual(1, runtime.main())
                discover.assert_not_called()
            report = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual("PASS", report["installation"])
            self.assertEqual("FAIL", report["status"])
            self.assertEqual(["UNSUPPORTED_RUNTIME_VERSION"], report["reasonCodes"])

    def test_0_160_candidate_does_not_change_live_capture_pin(self) -> None:
        import capture_live_event as capture
        self.assertEqual("codex-cli 0.159.3", capture._legacy_identity().profile.cli_banner)

    def test_0_160_candidate_captured_evidence_is_rejected(self) -> None:
        import codex_event_adapter as adapter
        text = ""
        manifest = adapter.capture_fixture_manifest("static-candidate-probe", "0.160.0", text)
        result = adapter.validate_captured_fixture(text, manifest)
        self.assertIn("FIXTURE_VERSION_MISMATCH", result.reason_codes)

    def test_unknown_codex_version_fails(self) -> None:
        report = validator.validate_catalog(REPOSITORY_ROOT, "999.0.0")

        self.assertFalse(report.passed)
        self.assertIn("unsupported Codex CLI version", report.errors[0])

    def test_static_model_efforts_are_specific_to_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.copy_catalog(target)
            registry_path = target / validator.REGISTRY_PATH
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["versions"]["0.160.0"]["models"]["gpt-6-luna"] = ["low"]
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            report = validator.validate_catalog(target, "0.160.0")
            self.assertFalse(report.passed)
            self.assertIn("quick-implementer.toml: unsupported reasoning effort 'medium' for gpt-6-luna",
                          report.errors)
            self.assertFalse(any("unsupported model" in error for error in report.errors))

    def test_missing_agent_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.copy_catalog(target)
            (target / "agents" / "code-explorer.toml").unlink()

            report = validator.validate_catalog(target, "0.155.1")

            self.assertFalse(report.passed)
            self.assertTrue(any("missing agent files" in error for error in report.errors))

    def test_malformed_toml_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.copy_catalog(target)
            (target / "agents" / "code-explorer.toml").write_text(
                'name = "unterminated\n',
                encoding="utf-8",
            )

            report = validator.validate_catalog(target, "0.155.1")

            self.assertFalse(report.passed)
            self.assertTrue(any("invalid TOML" in error for error in report.errors))

    def test_hyphenated_runtime_name_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.copy_catalog(target)
            agent_path = target / "agents" / "code-explorer.toml"
            agent_path.write_text(
                agent_path.read_text(encoding="utf-8").replace(
                    'name = "code_explorer"',
                    'name = "code-explorer"',
                ),
                encoding="utf-8",
            )

            report = validator.validate_catalog(target, "0.155.1")

            self.assertFalse(report.passed)
            self.assertTrue(
                any(
                    "lowercase letters, digits, and underscores" in error
                    for error in report.errors
                )
            )

    def test_unsafe_read_only_role_sandbox_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.copy_catalog(target)
            agent_path = target / "agents" / "code-validator.toml"
            agent_path.write_text(
                agent_path.read_text(encoding="utf-8").replace(
                    'sandbox_mode = "read-only"',
                    'sandbox_mode = "workspace-write"',
                ),
                encoding="utf-8",
            )

            report = validator.validate_catalog(target, "0.155.1")

            self.assertFalse(report.passed)
            self.assertTrue(any("sandbox_mode must be" in error for error in report.errors))

    def test_malformed_registry_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.copy_catalog(target)
            registry_path = target / validator.REGISTRY_PATH
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["versions"] = []
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            report = validator.validate_catalog(target, "0.155.1")

            self.assertFalse(report.passed)
            self.assertTrue(any("versions must be an object" in error for error in report.errors))


if __name__ == "__main__":
    unittest.main()
