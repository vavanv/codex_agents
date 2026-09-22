from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import capture_live_event as capture_module


class CaptureLiveEventTests(unittest.TestCase):
    def directories(self, root: Path) -> tuple[Path, Path, Path]:
        fixture = root / "fixture"
        results = root / "results"
        sqlite_home = root / "sqlite"
        fixture.mkdir()
        results.mkdir()
        sqlite_home.mkdir()
        return fixture, results, sqlite_home

    def test_windows_default_resolves_to_cmd_shim(self) -> None:
        def resolve(candidate: str) -> str | None:
            if candidate == "codex.cmd":
                return r"C:\Tools\codex.cmd"
            return None

        with patch.object(capture_module.os, "name", "nt"), patch.object(
            capture_module.shutil, "which", side_effect=resolve
        ), patch.object(
            capture_module,
            "_windows_cmd_prefix",
            return_value=(r"C:\Tools\node.exe", r"C:\Tools\codex.js"),
        ):
            self.assertEqual(
                (r"C:\Tools\node.exe", r"C:\Tools\codex.js"),
                capture_module._resolve_codex_command("codex"),
            )

    def test_missing_codex_command_fails_closed(self) -> None:
        with patch.object(capture_module.shutil, "which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "Codex command was not found"):
                capture_module._resolve_codex_command("missing-codex")

    def test_windows_cmd_shim_is_converted_to_node_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shim_root = Path(temporary) / "bin&safe"
            entrypoint = (
                shim_root
                / "node_modules"
                / "@openai"
                / "codex"
                / "bin"
                / "codex.js"
            )
            entrypoint.parent.mkdir(parents=True)
            shim = shim_root / "codex.cmd"
            node = shim_root / "node.exe"
            shim.touch()
            node.touch()
            entrypoint.touch()

            prefix = capture_module._windows_cmd_prefix(str(shim))

            self.assertEqual((str(node.resolve()), str(entrypoint.resolve())), prefix)
            self.assertNotIn("cmd.exe", " ".join(prefix).lower())

    def test_persistent_capture_uses_underscore_role_and_isolated_sqlite(self) -> None:
        event_stream = (
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "00000000-0000-4000-8000-000000000001",
                }
            )
            + "\n"
            + json.dumps({"type": "turn.completed"})
            + "\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            fixture, results, sqlite_home = self.directories(Path(temporary))
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=event_stream, stderr=""
            )
            with patch.object(
                capture_module,
                "_version",
                return_value=(0, capture_module.PINNED_CODEX_VERSION),
            ), patch.object(
                capture_module,
                "_resolve_codex_command",
                return_value=(r"C:\Tools\node.exe", r"C:\Tools\codex.js"),
            ), patch.object(
                capture_module.subprocess, "run", return_value=completed
            ) as run:
                exit_code, output = capture_module.capture(
                    fixture,
                    results,
                    30,
                    "codex.cmd",
                    ("code_explorer",),
                    False,
                    sqlite_home,
                )

            self.assertEqual(0, exit_code)
            self.assertEqual("CAPTURED", output["status"])
            command = run.call_args.args[0]
            self.assertEqual(
                [r"C:\Tools\node.exe", r"C:\Tools\codex.js"], command[:2]
            )
            self.assertNotIn("--ephemeral", command)
            self.assertIn("code_explorer", command[-1])
            self.assertEqual(
                str(sqlite_home.resolve()),
                run.call_args.kwargs["env"]["CODEX_SQLITE_HOME"],
            )
            manifest = json.loads(
                (results / "real-capture.manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(["code_explorer"], manifest["requestedRoles"])
            self.assertFalse(manifest["timedOut"])
            self.assertNotIn("--ephemeral", manifest["command"])

    def test_timeout_persists_only_sanitized_partial_capture(self) -> None:
        raw_secret = "Authorization: Bearer should-not-survive"
        partial = json.dumps({"type": "error", "message": raw_secret}) + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            fixture, results, _ = self.directories(Path(temporary))
            timeout = subprocess.TimeoutExpired(
                cmd=["codex.cmd", "exec"],
                timeout=30,
                output=partial,
                stderr="password=should-not-survive",
            )
            with patch.object(
                capture_module,
                "_version",
                return_value=(0, capture_module.PINNED_CODEX_VERSION),
            ), patch.object(
                capture_module,
                "_resolve_codex_command",
                return_value=(r"C:\Tools\node.exe", r"C:\Tools\codex.js"),
            ), patch.object(capture_module.subprocess, "run", side_effect=timeout):
                exit_code, output = capture_module.capture(
                    fixture,
                    results,
                    30,
                    "codex.cmd",
                    ("code_explorer",),
                    True,
                    None,
                )

            self.assertEqual(3, exit_code)
            self.assertEqual("CAPTURE_TIMEOUT", output["reason"])
            sanitized = (results / "real-capture.sanitized.jsonl").read_text(
                encoding="utf-8"
            )
            manifest = json.loads(
                (results / "real-capture.manifest.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("should-not-survive", sanitized)
            self.assertNotIn("should-not-survive", manifest["stderr"])
            self.assertIn("[REDACTED]", sanitized)
            self.assertTrue(manifest["timedOut"])
            self.assertIn("--ephemeral", manifest["command"])

    def test_capture_redacts_json_secret_fields_before_persistence(self) -> None:
        event_stream = json.dumps(
            {
                "type": "error",
                "token": "ghp_should-not-survive",
                "nested": {"clientSecret": "also-secret"},
            }
        ) + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            fixture, results, _ = self.directories(Path(temporary))
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=event_stream,
                stderr=json.dumps({"clientSecret": "stderr-must-not-survive"}),
            )
            with patch.object(
                capture_module,
                "_version",
                return_value=(0, capture_module.PINNED_CODEX_VERSION),
            ), patch.object(
                capture_module,
                "_resolve_codex_command",
                return_value=(r"C:\Tools\node.exe", r"C:\Tools\codex.js"),
            ), patch.object(
                capture_module.subprocess, "run", return_value=completed
            ):
                capture_module.capture(
                    fixture,
                    results,
                    30,
                    "codex.cmd",
                    ("code_explorer",),
                    True,
                    None,
                )

            persisted = (results / "real-capture.sanitized.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("ghp_should-not-survive", persisted)
            self.assertNotIn("also-secret", persisted)
            self.assertIn("[REDACTED]", persisted)
            manifest = (results / "real-capture.manifest.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("stderr-must-not-survive", manifest)


if __name__ == "__main__":
    unittest.main()
