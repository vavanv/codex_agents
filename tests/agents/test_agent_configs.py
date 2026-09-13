from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


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
        report = validator.validate_catalog(REPOSITORY_ROOT, "0.154.0")

        self.assertTrue(report.passed, report.errors)
        self.assertEqual(set(validator.EXPECTED_ROLES), set(report.agents))

    def test_unknown_codex_version_fails(self) -> None:
        report = validator.validate_catalog(REPOSITORY_ROOT, "999.0.0")

        self.assertFalse(report.passed)
        self.assertIn("unsupported Codex CLI version", report.errors[0])

    def test_missing_agent_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.copy_catalog(target)
            (target / "agents" / "code-explorer.toml").unlink()

            report = validator.validate_catalog(target, "0.154.0")

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

            report = validator.validate_catalog(target, "0.154.0")

            self.assertFalse(report.passed)
            self.assertTrue(any("invalid TOML" in error for error in report.errors))

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

            report = validator.validate_catalog(target, "0.154.0")

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

            report = validator.validate_catalog(target, "0.154.0")

            self.assertFalse(report.passed)
            self.assertTrue(any("versions must be an object" in error for error in report.errors))


if __name__ == "__main__":
    unittest.main()
