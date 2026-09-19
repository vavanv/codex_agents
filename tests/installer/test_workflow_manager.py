from __future__ import annotations

import sys
import tempfile
import unittest
import json
import shutil
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import workflow_manager as manager


class WorkflowManagerTests(unittest.TestCase):
    def install(
        self,
        target: Path,
        dry_run: bool = False,
        with_custom_agents: bool = False,
    ) -> None:
        with patch.object(manager, "_validate_codex_version", return_value="0.154.0"):
            with redirect_stdout(StringIO()):
                manager.install(target, REPOSITORY_ROOT, dry_run, with_custom_agents)

    def uninstall(self, target: Path, dry_run: bool = False) -> None:
        with redirect_stdout(StringIO()):
            manager.uninstall(target, dry_run)

    def convert_to_legacy_branded_block(self, target: Path) -> None:
        agents_path = target / "AGENTS.md"
        legacy_content = agents_path.read_text(encoding="utf-8").replace(
            "## Codex multi-agent workflow",
            "## Hybrid Codex workflow",
        )
        agents_path.write_text(legacy_content, encoding="utf-8")
        extracted = manager._extract_managed_block(legacy_content)
        self.assertIsNotNone(extracted)
        state_path = target / manager.STATE_FILENAME
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["agents"]["blockHash"] = manager._text_hash(extracted[2])
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def copy_install_sources(self, destination: Path) -> None:
        for relative in list(manager.PACKAGE_FILES) + list(manager.PROJECT_TEMPLATE_FILES):
            source = REPOSITORY_ROOT / relative
            copied = destination / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, copied)

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)

            self.install(target, dry_run=True)

            self.assertEqual([], list(target.iterdir()))

    def test_clean_install_is_idempotent_and_fully_uninstalls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)

            self.install(target)
            first_state = (target / manager.STATE_FILENAME).read_bytes()
            self.install(target)
            second_state = (target / manager.STATE_FILENAME).read_bytes()

            self.assertEqual(first_state, second_state)
            self.assertTrue((target / manager.INSTALL_DIRECTORY / "CODEX_WORKFLOW.md").is_file())
            self.assertTrue((target / "docs" / "ai" / "PROJECT_CONTEXT.md").is_file())
            self.assertIn(manager.START_MARKER, (target / "AGENTS.md").read_text(encoding="utf-8"))

            self.uninstall(target)

            self.assertEqual([], list(target.iterdir()))

    def test_uninstall_restores_existing_agents_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            original = "# Existing instructions\n\n- Preserve this exactly.\n"
            (target / "AGENTS.md").write_text(original, encoding="utf-8")

            self.install(target)
            self.uninstall(target)

            self.assertEqual(original, (target / "AGENTS.md").read_text(encoding="utf-8"))

    def test_reinstall_migrates_legacy_branded_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target)
            self.convert_to_legacy_branded_block(target)

            self.install(target)

            agents_content = (target / "AGENTS.md").read_text(encoding="utf-8")
            self.assertNotIn("Hybrid", agents_content)
            self.assertIn("## Codex multi-agent workflow", agents_content)

    def test_uninstall_recognizes_legacy_branded_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target)
            self.convert_to_legacy_branded_block(target)

            self.uninstall(target)

            self.assertEqual([], list(target.iterdir()))

    def test_uninstall_preserves_modified_project_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target)
            context_path = target / "docs" / "ai" / "PROJECT_CONTEXT.md"
            context_path.write_text(
                context_path.read_text(encoding="utf-8") + "\nProject-owned content.\n",
                encoding="utf-8",
            )
            agents_path = target / "AGENTS.md"
            agents_path.write_text(
                agents_path.read_text(encoding="utf-8") + "\n## Project-owned instruction\n",
                encoding="utf-8",
            )

            self.uninstall(target)

            self.assertTrue(context_path.is_file())
            self.assertIn("Project-owned content", context_path.read_text(encoding="utf-8"))
            agents_content = agents_path.read_text(encoding="utf-8")
            self.assertIn("Project-owned instruction", agents_content)
            self.assertNotIn(manager.START_MARKER, agents_content)
            self.assertTrue((target / manager.STATE_FILENAME).is_file())

    def test_uninstall_preserves_modified_managed_agents_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target)
            agents_path = target / "AGENTS.md"
            agents_path.write_text(
                agents_path.read_text(encoding="utf-8").replace(
                    "require observed validation evidence",
                    "require project-specific validation evidence",
                ),
                encoding="utf-8",
            )

            self.uninstall(target)

            agents_content = agents_path.read_text(encoding="utf-8")
            self.assertIn(manager.START_MARKER, agents_content)
            self.assertIn("project-specific validation evidence", agents_content)
            self.assertTrue((target / manager.STATE_FILENAME).is_file())

    def test_unsupported_codex_version_fails_closed(self) -> None:
        with patch.object(manager, "_detect_codex_version", return_value="999.0.0"):
            with self.assertRaisesRegex(manager.WorkflowError, "Unsupported Codex CLI"):
                manager._validate_codex_version()

    def test_supported_version_0_155_0_is_accepted(self) -> None:
        with patch.object(manager, "_detect_codex_version", return_value="0.155.0"):
            self.assertEqual("0.155.0", manager._validate_codex_version())

    def test_state_path_cannot_escape_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve()

            with self.assertRaisesRegex(manager.WorkflowError, "safe repository-relative path"):
                manager._target_path(target, "../outside.txt")

    def test_custom_agents_install_and_fully_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)

            self.install(target, with_custom_agents=True)

            for installed_relative in manager.CUSTOM_AGENT_FILES.values():
                self.assertTrue((target / installed_relative).is_file())
            state = json.loads((target / manager.STATE_FILENAME).read_text(encoding="utf-8"))
            self.assertTrue(state["features"]["customAgents"])

            self.uninstall(target)

            self.assertEqual([], list(target.iterdir()))

    def test_contracts_only_install_upgrades_to_custom_agents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target)

            self.install(target, with_custom_agents=True)

            self.assertTrue((target / ".codex" / "agents" / "code-explorer.toml").is_file())
            state = json.loads((target / manager.STATE_FILENAME).read_text(encoding="utf-8"))
            self.assertTrue(state["features"]["customAgents"])

    def test_custom_agent_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)

            self.install(target, dry_run=True, with_custom_agents=True)

            self.assertEqual([], list(target.iterdir()))

    def test_conflicting_unmanaged_custom_agent_fails_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            agent_path = target / ".codex" / "agents" / "code-explorer.toml"
            agent_path.parent.mkdir(parents=True)
            agent_path.write_text('name = "project-owned"\n', encoding="utf-8")

            with self.assertRaisesRegex(manager.WorkflowError, "Unmanaged custom agent conflicts"):
                self.install(target, with_custom_agents=True)

            self.assertEqual('name = "project-owned"\n', agent_path.read_text(encoding="utf-8"))
            self.assertFalse((target / manager.STATE_FILENAME).exists())

    def test_identical_preexisting_custom_agent_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            source_relative = "agents/code-explorer.toml"
            installed_relative = manager.CUSTOM_AGENT_FILES[source_relative]
            agent_path = target / installed_relative
            agent_path.parent.mkdir(parents=True)
            source_content = (REPOSITORY_ROOT / source_relative).read_bytes()
            agent_path.write_bytes(source_content)

            self.install(target, with_custom_agents=True)
            self.uninstall(target)

            self.assertEqual(source_content, agent_path.read_bytes())
            self.assertFalse((target / manager.STATE_FILENAME).exists())

    def test_uninstall_preserves_modified_custom_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.install(target, with_custom_agents=True)
            agent_path = target / ".codex" / "agents" / "code-reviewer.toml"
            agent_path.write_text(
                agent_path.read_text(encoding="utf-8") + "\n# Project customization\n",
                encoding="utf-8",
            )

            self.uninstall(target)

            self.assertTrue(agent_path.is_file())
            self.assertIn("Project customization", agent_path.read_text(encoding="utf-8"))
            self.assertTrue((target / manager.STATE_FILENAME).is_file())

    def test_forged_state_is_rejected_before_install_or_uninstall_mutation(self) -> None:
        mutations = (
            lambda value: value.update({"unexpected": True}),
            lambda value: value["files"].update(
                {"project-owned.txt": next(iter(value["files"].values()))}
            ),
            lambda value: value["createdDirectories"].append("project-owned-directory"),
            lambda value: value["backups"].append(
                f"{manager.BACKUP_DIRECTORY}/{'f' * 32}/AGENTS.md"
            ),
            lambda value: value["features"].update(
                {"customAgents": not value["features"]["customAgents"]}
            ),
            lambda value: value["agents"].update({"path": "project-owned.txt"}),
        )
        for index, mutate in enumerate(mutations):
            for operation in ("install", "uninstall"):
                with self.subTest(index=index, operation=operation):
                    with tempfile.TemporaryDirectory() as directory:
                        target = Path(directory)
                        self.install(target)
                        sentinel = target / "project-owned.txt"
                        sentinel.write_bytes(b"safe")
                        protected_directory = target / "project-owned-directory"
                        protected_directory.mkdir()
                        state_path = target / manager.STATE_FILENAME
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                        mutate(state)
                        state_path.write_text(json.dumps(state), encoding="utf-8")
                        forged_state = state_path.read_bytes()

                        with self.assertRaises(manager.WorkflowError):
                            if operation == "install":
                                self.install(target)
                            else:
                                self.uninstall(target)

                        self.assertEqual(b"safe", sentinel.read_bytes())
                        self.assertTrue(protected_directory.is_dir())
                        self.assertEqual(forged_state, state_path.read_bytes())

    def test_two_content_changing_reinstalls_keep_backup_references_coherent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            source_root = root / "source"
            target.mkdir()
            self.copy_install_sources(source_root)
            with patch.object(manager, "_validate_codex_version", return_value="0.154.0"):
                with redirect_stdout(StringIO()):
                    manager.install(target, source_root, False)
                package_source = source_root / "CODEX_WORKFLOW.md"
                for revision in ("first", "second"):
                    package_source.write_text(
                        package_source.read_text(encoding="utf-8")
                        + f"\n{revision} revision\n",
                        encoding="utf-8",
                    )
                    with redirect_stdout(StringIO()):
                        manager.install(target, source_root, False)
                    state = manager._read_state(target / manager.STATE_FILENAME)
                    self.assertIsNotNone(state)
                    referenced = {
                        record["backup"]
                        for record in state["files"].values()
                        if record.get("backup") is not None
                    }
                    if state["agents"] and state["agents"].get("backup") is not None:
                        referenced.add(state["agents"]["backup"])
                    self.assertEqual(referenced, set(state["backups"]))
                with redirect_stdout(StringIO()):
                    manager.install(target, source_root, True)

    def test_partial_uninstall_restores_existing_agents_and_emits_valid_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            original_agents = "# Existing project instructions\n"
            (target / "AGENTS.md").write_text(original_agents, encoding="utf-8")
            self.install(target)
            modified = target / "docs" / "ai" / "PROJECT_CONTEXT.md"
            modified.write_text(
                modified.read_text(encoding="utf-8") + "\nProject-owned edit.\n",
                encoding="utf-8",
            )

            self.uninstall(target)

            self.assertEqual(original_agents, (target / "AGENTS.md").read_text(encoding="utf-8"))
            state = manager._read_state(target / manager.STATE_FILENAME)
            self.assertIsNotNone(state)
            self.assertIsNone(state["agents"])
            self.assertEqual([], state["backups"])
            with redirect_stdout(StringIO()):
                manager.uninstall(target, True)


if __name__ == "__main__":
    unittest.main()
