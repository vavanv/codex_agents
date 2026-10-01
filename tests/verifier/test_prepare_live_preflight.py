"""Source preflight must fail closed before a paid live capture."""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import prepare_live_preflight as preflight  # noqa: E402
from validate_agent_configs import AGENT_FILE_ROLES  # noqa: E402


class PrepareLivePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config = self.root / "compatibility"
        config.mkdir()
        (config / "codex-agents.json").write_text("{}", encoding="utf-8")
        agents = self.root / "agents"
        agents.mkdir()
        for stem in AGENT_FILE_ROLES:
            (agents / f"{stem}.toml").write_text(stem, encoding="utf-8")

    def test_records_all_source_hashes_without_claiming_runtime_or_fixture(self) -> None:
        with (
            patch.object(preflight, "_resolve_codex_command", return_value=("codex",)),
            patch.object(preflight.shutil, "which", side_effect=lambda name: name),
            patch.object(
                preflight,
                "_version",
                side_effect=["codex-cli 0.159.0", "git version 2.0", "7.5.0"],
            ),
        ):
            result = preflight.prepare(self.root, 45, "codex")
        self.assertEqual("SOURCE_PREFLIGHT_READY", result["status"])
        self.assertEqual(9, len(result["sourceAgentSha256"]))
        self.assertEqual({"maxRoles": 1, "maxCalls": 1, "timeoutSeconds": 45}, result["proposedCaptureLimit"])
        self.assertIsNone(result["fixtureBaseline"])
        self.assertIsNone(result["installedAgentSha256"])
        self.assertFalse(result["runtimeValidated"])
        self.assertNotIn(str(self.root), str(result))

    def test_wrong_cli_version_blocks_before_other_commands(self) -> None:
        with (
            patch.object(preflight, "_resolve_codex_command", return_value=("codex",)),
            patch.object(preflight.shutil, "which") as which,
            patch.object(preflight, "_version", return_value="codex-cli 0.155.1"),
        ):
            with self.assertRaisesRegex(ValueError, "pinned version"):
                preflight.prepare(self.root, 45, "codex")
        which.assert_not_called()

    def test_catalog_drift_blocks_before_codex_invocation(self) -> None:
        (self.root / "agents" / "code-explorer.toml").unlink()
        with patch.object(preflight, "_resolve_codex_command") as resolve:
            with self.assertRaisesRegex(ValueError, "exactly nine"):
                preflight.prepare(self.root, 45, "codex")
        resolve.assert_not_called()

    def test_bind_fixture_records_post_install_baseline_and_matching_hashes(self) -> None:
        installed = self.root / "fixture" / ".codex" / "agents"
        installed.mkdir(parents=True)
        for stem in AGENT_FILE_ROLES:
            (installed / f"{stem}.toml").write_bytes(
                (self.root / "agents" / f"{stem}.toml").read_bytes()
            )
        (self.root / "results").mkdir()
        source = {
            "schema": "codex-live-preflight/v1",
            "status": "SOURCE_PREFLIGHT_READY",
            "sourceAgentSha256": {
                role: preflight._sha256(self.root / "agents" / f"{stem}.toml")
                for stem, role in AGENT_FILE_ROLES.items()
            },
        }
        marker = {"lifecycle": "ready", "activeWorkers": [], "runId": "owned-run"}
        with (
            patch.object(preflight, "validate_marker", return_value=(self.root, marker)),
            patch.object(preflight, "_owned_run_lock", return_value=nullcontext()),
            patch.object(preflight, "prepare", return_value=source),
            patch.object(
                preflight,
                "capture_git_snapshot",
                return_value={"head": "a" * 40, "fileHashes": {"fixture.txt": "b" * 64}},
            ),
            patch.object(preflight, "_snapshot_digest", return_value="c" * 64),
            patch.object(preflight, "finalize_evidence") as finalize,
        ):
            result = preflight.bind_fixture(self.root, self.root, 45, "codex")
            self.assertEqual("FIXTURE_PREFLIGHT_READY", result["status"])
            self.assertEqual("owned-run", result["fixtureIdentity"])
            self.assertEqual("c" * 64, result["fixtureBaseline"]["stateSha256"])
            self.assertEqual(source["sourceAgentSha256"], result["installedAgentSha256"])
            self.assertTrue((self.root / "results" / "preflight.json").is_file())
            finalize.assert_called_once()
            with self.assertRaisesRegex(ValueError, "already has"):
                preflight.bind_fixture(self.root, self.root, 45, "codex")

    def test_bind_fixture_rejects_installed_agent_drift(self) -> None:
        installed = self.root / "fixture" / ".codex" / "agents"
        installed.mkdir(parents=True)
        for stem in AGENT_FILE_ROLES:
            (installed / f"{stem}.toml").write_text("changed", encoding="utf-8")
        (self.root / "results").mkdir()
        source = {
            "sourceAgentSha256": {
                role: preflight._sha256(self.root / "agents" / f"{stem}.toml")
                for stem, role in AGENT_FILE_ROLES.items()
            }
        }
        marker = {"lifecycle": "ready", "activeWorkers": [], "runId": "owned-run"}
        with (
            patch.object(preflight, "validate_marker", return_value=(self.root, marker)),
            patch.object(preflight, "_owned_run_lock", return_value=nullcontext()),
            patch.object(preflight, "prepare", return_value=source),
            patch.object(preflight, "capture_git_snapshot") as snapshot,
        ):
            with self.assertRaisesRegex(ValueError, "differs from the source"):
                preflight.bind_fixture(self.root, self.root, 45, "codex")
        snapshot.assert_not_called()
        self.assertFalse((self.root / "results" / "preflight.json").exists())


if __name__ == "__main__":
    unittest.main()
