from __future__ import annotations

import json
import os
from io import StringIO
from contextlib import redirect_stdout
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import capture_live_event as capture_module
import codex_event_adapter as adapter
from codex_compatibility import RegistryError, load_registry


PARENT = "00000000-0000-4000-8000-000000000001"
CHILD = "00000000-0000-4000-8000-000000000002"


class CaptureLiveEventTests(unittest.TestCase):
    def directories(self, root: Path) -> tuple[Path, Path, Path]:
        fixture = root / "fixture"
        results = root / "results"
        sqlite_home = root / "sqlite"
        fixture.mkdir()
        results.mkdir()
        sqlite_home.mkdir()
        return fixture, results, sqlite_home


    def policy_source(self, root: Path, *, version: str = "0.159.3") -> Path:
        source = root / "source"
        (source / "compatibility").mkdir(parents=True)
        document = json.loads((REPOSITORY_ROOT / "compatibility/codex-agents.json").read_text(encoding="utf-8"))
        document["runProfiles"]["legacy-windows-capture"]["expectedVersion"] = version
        (source / "compatibility/codex-agents.json").write_text(json.dumps(document), encoding="utf-8")
        return source

    def test_invalid_policy_blocks_before_paths_probe_or_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.policy_source(root)
            (source / "compatibility/codex-agents.json").write_text("{}", encoding="utf-8")
            with patch.object(capture_module, "_paths") as paths, patch.object(capture_module, "_version") as probe, patch.object(capture_module, "_persist_capture") as persist:
                with self.assertRaises(RegistryError):
                    capture_module.capture(root, root, 30, "codex", ("code_explorer",), False, None, source_root=source)
            paths.assert_not_called()
            probe.assert_not_called()
            persist.assert_not_called()

    def test_injected_foreign_policy_never_selects_its_own_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.policy_source(root)
            policy = load_registry(source)
            with patch.object(capture_module, "_paths") as paths:
                with self.assertRaises(RegistryError):
                    capture_module.capture(root, root, 30, "codex", ("code_explorer",), False, None, policy=policy)
            paths.assert_not_called()

    def test_changed_profile_rejects_old_cli_before_exec_and_keeps_outputs_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.policy_source(root, version="0.157.1")
            fixture, results, _ = self.directories(root)
            with patch.object(capture_module, "_resolve_codex_command", return_value=("fake",)), patch.object(capture_module, "_version", return_value=(0, "codex-cli 0.159.3")), patch.object(capture_module.subprocess, "run") as run:
                code, output = capture_module.capture(fixture, results, 30, "fake", ("code_explorer",), False, None, source_root=source)
            self.assertEqual(2, code)
            self.assertEqual("PINNED_VERSION_UNAVAILABLE", output["reason"])
            run.assert_not_called()
            self.assertEqual([], list(results.iterdir()))

    def test_snapshot_is_reused_after_registry_bytes_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.policy_source(root)
            policy = load_registry(source)
            fixture, results, _ = self.directories(root)
            (source / "compatibility/codex-agents.json").write_text("{}", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, "", "")
            with patch.object(capture_module, "load_registry", side_effect=AssertionError("unexpected reload")), patch.object(capture_module, "_resolve_codex_command", return_value=("fake",)), patch.object(capture_module, "_version", return_value=(0, "codex-cli 0.159.3")), patch.object(capture_module.subprocess, "run", return_value=completed):
                code, _ = capture_module.capture(fixture, results, 30, "fake", ("code_explorer",), False, None, source_root=source, policy=policy)
            self.assertEqual(0, code)
            manifest = json.loads((results / "real-capture.manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("0.159.3", manifest["codexVersion"])
            self.assertEqual("windows-0.159.3-code_explorer-capture", manifest["name"])

    def test_missing_policy_blocks_owned_capture_before_marker_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(RegistryError):
                capture_module.capture_owned_run(root / "absent-run", 30, "fake", "code_explorer", source_root=root)

    def test_preferred_install_target_and_cwd_do_not_select_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.policy_source(root)
            path = source / "compatibility/codex-agents.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["preferredInstallTarget"] = "0.155.1"
            path.write_text(json.dumps(document), encoding="utf-8")
            previous = Path.cwd()
            try:
                os.chdir(root)
                identity = capture_module._legacy_identity(source)
                shipped = capture_module._legacy_identity()
            finally:
                os.chdir(previous)
            self.assertEqual("0.159.3", identity.profile.expected_version)
            self.assertEqual("codex-cli 0.159.3", shipped.profile.cli_banner)


    def test_reparse_source_is_rejected_before_paths_probe_and_cli_envelope_is_blocked(self) -> None:
        original_lstat = Path.lstat
        def reparse(path: Path, *args, **kwargs):
            metadata = original_lstat(path, *args, **kwargs)
            if path == REPOSITORY_ROOT / "compatibility/codex-agents.json":
                return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
            return metadata
        with patch.object(Path, "lstat", reparse), patch.object(capture_module, "_paths") as paths, patch.object(capture_module, "_version") as probe:
            with self.assertRaises(RegistryError):
                capture_module.capture(Path("unused"), Path("unused"), 30, "fake", ("code_explorer",), False, None)
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = capture_module.main(["--owned-run-root", "absent", "--role", "code_explorer"])
        paths.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(4, code)
        self.assertEqual({"status": "BLOCKED", "reason": "OWNED_CAPTURE_FAILED"}, json.loads(stdout.getvalue()))

    def test_identity_modules_import_without_reading_policy_or_invoking_cli(self) -> None:
        import codex_compatibility
        paths = ("capture_live_event.py", "prepare_live_preflight.py", "validate_l5_private_matrix.py", "run_live_behavior_case.py")
        sources = {name: (REPOSITORY_ROOT / "scripts" / name).read_text(encoding="utf-8") for name in paths}
        with patch.object(codex_compatibility, "load_registry", side_effect=AssertionError("import read policy")) as load, patch.object(subprocess, "run", side_effect=AssertionError("import invoked CLI")) as invoke:
            for name, source in sources.items():
                exec(compile(source, name, "exec"), {"__name__": "capture_live_event", "__file__": str(REPOSITORY_ROOT / "scripts" / name)})
        load.assert_not_called()
        invoke.assert_not_called()

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

    def test_prompt_orders_spawn_before_wait_and_requires_child_ids(self) -> None:
        prompt = capture_module._prompt(("code_explorer",))

        self.assertLess(prompt.index("spawn_agent"), prompt.index("wait for that child"))
        self.assertIn("one at a time", prompt)
        self.assertIn("one spawn_agent call using its exact agent_type", prompt)
        self.assertIn("exact agent_type once", prompt)
        self.assertIn("task_name is only a task label", prompt)
        self.assertIn("LIVE_CAPTURE_FAILED:ROLE_SELECTOR_UNAVAILABLE", prompt)
        self.assertIn("nonempty child/thread identifier", prompt)
        self.assertIn("Never call wait with an empty child set", prompt)
        self.assertIn("do not call wait for that child", prompt)
        self.assertIn("wait for that child before attempting the next role", prompt)

    def test_prompt_has_exact_role_markers_failures_and_safety_constraints(self) -> None:
        roles = ("code_explorer", "implementer")
        prompt = capture_module._prompt(roles)

        for role in roles:
            self.assertIn(
                f"- {role}: call spawn_agent with agent_type={role} and "
                f"task_name=probe_{role}",
                prompt,
            )
            self.assertIn(f"reply solely LIVE_ROLE:{role}", prompt)
        self.assertLess(prompt.index("code_explorer"), prompt.index("implementer"))
        self.assertIn(
            "Do not spawn the next role until the current child has returned "
            "its exact marker",
            prompt,
        )
        self.assertIn(
            "LIVE_CAPTURE_FAILED:SPAWN_UNAVAILABLE_OR_FAILED", prompt
        )
        self.assertIn(
            "LIVE_CAPTURE_FAILED:CHILD_RESPONSE_MISSING_OR_INVALID", prompt
        )
        self.assertIn("exact child-authored marker for every requested role", prompt)
        self.assertIn("Child agents must not delegate", prompt)
        self.assertIn("Do not invoke shell commands", prompt)
        self.assertIn("edit files", prompt)
        self.assertIn("create commits", prompt)
        self.assertIn("push", prompt)
        self.assertIn("install anything", prompt)
        self.assertIn(
            "model-emitted markers are diagnostic text only and never override "
            "structured adapter evidence",
            prompt,
        )

    def test_missing_codex_command_fails_closed(self) -> None:
        with patch.object(capture_module.shutil, "which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "Codex command was not found"):
                capture_module._resolve_codex_command("missing-codex")

    def test_trust_override_requires_isolated_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture, results, _ = self.directories(Path(temporary))
            with self.assertRaisesRegex(ValueError, "requires isolated user"):
                capture_module.capture(
                    fixture,
                    results,
                    30,
                    "codex.cmd",
                    ("code_explorer",),
                    False,
                    None,
                    trust_fixture=True,
                )

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
        event_stream = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": PARENT}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "collab_tool_call",
                            "tool": "spawn_agent",
                            "status": "completed",
                            "sender_thread_id": PARENT,
                            "receiver_thread_ids": [CHILD],
                            "prompt": "bounded task",
                            "agents_states": {},
                        },
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            )
        ) + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            fixture, results, sqlite_home = self.directories(Path(temporary))
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=event_stream, stderr=""
            )
            with patch.object(
                capture_module,
                "_version",
                return_value=(0, "codex-cli 0.159.3"),
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
            self.assertEqual(["code_explorer"], output["attributedRoles"])
            self.assertEqual([], output["reasonCodes"])
            command = run.call_args.args[0]
            self.assertEqual(
                [r"C:\Tools\node.exe", r"C:\Tools\codex.js"], command[:2]
            )
            self.assertNotIn("--ephemeral", command)
            self.assertIn("code_explorer", command[-1])
            self.assertEqual(subprocess.DEVNULL, run.call_args.kwargs["stdin"])
            self.assertEqual(
                str(sqlite_home.resolve()),
                run.call_args.kwargs["env"]["CODEX_SQLITE_HOME"],
            )
            manifest = json.loads(
                (results / "real-capture.manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual("0.159.3", manifest["codexVersion"])
            self.assertEqual("windows-0.159.3-code_explorer-capture", manifest["name"])
            self.assertEqual(["code_explorer"], manifest["requestedRoles"])
            self.assertFalse(manifest["timedOut"])
            self.assertNotIn("--ephemeral", manifest["command"])

    def test_isolated_trusted_fixture_uses_invocation_scoped_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture, results, _ = self.directories(Path(temporary))
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            with patch.object(
                capture_module,
                "_version",
                return_value=(0, "codex-cli 0.159.3"),
            ), patch.object(
                capture_module,
                "_resolve_codex_command",
                return_value=(r"C:\Tools\node.exe", r"C:\Tools\codex.js"),
            ), patch.object(
                capture_module.subprocess, "run", return_value=completed
            ) as run:
                capture_module.capture(
                    fixture,
                    results,
                    30,
                    "codex.cmd",
                    ("code_explorer",),
                    False,
                    None,
                    isolate_user_config=True,
                    trust_fixture=True,
                )

            command = run.call_args.args[0]
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--strict-config", command)
            self.assertEqual("multi_agent", command[command.index("--enable") + 1])
            self.assertEqual(subprocess.DEVNULL, run.call_args.kwargs["stdin"])
            trust_override = command[command.index("-c") + 1]
            self.assertEqual(
                f'projects={{{json.dumps(str(fixture.resolve()))}={{trust_level="trusted"}}}}',
                trust_override,
            )
            manifest = json.loads(
                (results / "real-capture.manifest.json").read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["userConfigIgnored"])
            self.assertTrue(manifest["fixtureTrustOverride"])
            self.assertNotIn(str(fixture.resolve()), manifest["command"])

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
                return_value=(0, "codex-cli 0.159.3"),
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
            validation = adapter.validate_captured_fixture(
                sanitized, manifest, expected_roles=("code_explorer",)
            )
            self.assertIn("FIXTURE_CAPTURE_TIMED_OUT", validation.reason_codes)
            self.assertIn("FIXTURE_EXIT_CODE_INVALID", validation.reason_codes)

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
                return_value=(0, "codex-cli 0.159.3"),
            ), patch.object(
                capture_module,
                "_resolve_codex_command",
                return_value=(r"C:\Tools\node.exe", r"C:\Tools\codex.js"),
            ), patch.object(
                capture_module.subprocess, "run", return_value=completed
            ):
                exit_code, output = capture_module.capture(
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
            manifest_value = json.loads(manifest)
            self.assertEqual(1, exit_code)
            self.assertEqual(1, output["exitCode"])
            validation = adapter.validate_captured_fixture(
                persisted,
                manifest_value,
                expected_roles=("code_explorer",),
            )
            self.assertIn("FIXTURE_EXIT_CODE_INVALID", validation.reason_codes)

    def test_failure_marker_without_child_remains_unattributed_capture(self) -> None:
        event_stream = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": PARENT}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": (
                                "LIVE_CAPTURE_FAILED:"
                                "SPAWN_UNAVAILABLE_OR_FAILED"
                            ),
                        },
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            )
        ) + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            fixture, results, _ = self.directories(Path(temporary))
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=event_stream, stderr=""
            )
            with patch.object(
                capture_module,
                "_version",
                return_value=(0, "codex-cli 0.159.3"),
            ), patch.object(
                capture_module,
                "_resolve_codex_command",
                return_value=(r"C:\Tools\node.exe", r"C:\Tools\codex.js"),
            ), patch.object(
                capture_module.subprocess, "run", return_value=completed
            ):
                exit_code, output = capture_module.capture(
                    fixture,
                    results,
                    30,
                    "codex.cmd",
                    ("code_explorer",),
                    True,
                    None,
                )

        self.assertEqual(0, exit_code)
        self.assertEqual("CAPTURED", output["status"])
        self.assertEqual([], output["attributedRoles"])
        self.assertIn("MISSING_ATTRIBUTION", output["reasonCodes"])


if __name__ == "__main__":
    unittest.main()
