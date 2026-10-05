from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import codex_compatibility as compatibility
import validate_agent_configs as catalog
import workflow_manager as manager

VERSIONS = {"0.155.1", "0.157.1", "0.159.0", "0.159.3", "0.160.0"}


class VersionPolicyTests(unittest.TestCase):
    def setUp(self):
        disposable = tempfile.TemporaryDirectory()
        self.addCleanup(disposable.cleanup)
        self.directory = Path(disposable.name)
        self.source = self.directory / "source"
        self.target = self.directory / "target"
        self.target.mkdir()
        for relative in {*manager.PACKAGE_FILES, *manager.PROJECT_TEMPLATE_FILES,
                         *manager.CUSTOM_AGENT_FILES, manager.COMPATIBILITY_FILE}:
            copied = self.source / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, copied)
        self.registry_path = self.source / manager.COMPATIBILITY_FILE
        self.document = json.loads(self.registry_path.read_bytes())

    def write_policy(self):
        self.registry_path.write_text(json.dumps(self.document), encoding="utf-8")

    def snapshot(self):
        return {path.relative_to(self.target).as_posix():
                None if path.is_dir() else path.read_bytes()
                for path in self.target.rglob("*")}

    def install(self, version="0.155.1", dry_run=False, custom=False, **kwargs):
        with patch.object(manager, "_detect_codex_version", return_value=version), redirect_stdout(StringIO()):
            return manager.install(self.target, self.source, dry_run, custom, **kwargs)

    def deny_old_static(self):
        self.document["versions"]["0.155.1"]["gates"]["staticInstallation"] = False
        self.write_policy()

    def test_baseline_versions_and_static_gate_are_shared(self):
        self.assertEqual(VERSIONS, catalog.supported_versions(self.source))
        for version in sorted(VERSIONS):
            with self.subTest(version=version):
                self.install(version, dry_run=True, custom=True)
                self.assertEqual({}, self.snapshot())
        self.deny_old_static()
        self.assertNotIn("0.155.1", catalog.supported_versions(self.source))
        for version in ("0.155.1", "999.0.0"):
            for dry_run in (False, True):
                with self.subTest(version=version, dry_run=dry_run):
                    with self.assertRaisesRegex(manager.WorkflowError, "Unsupported Codex CLI .*No files were changed"):
                        self.install(version, dry_run=dry_run)
                    self.assertEqual({}, self.snapshot())
                    report = catalog.validate_catalog(self.source, version)
                    self.assertEqual((f"unsupported Codex CLI version: {version}",), report.errors)
                    self.assertEqual((), report.agents)

    def test_package_import_and_module_cli_without_sys_path_injection(self):
        probe = subprocess.run([sys.executable, "-B", "-c",
            "from pathlib import Path; import scripts.validate_agent_configs as c; "
            "import scripts.workflow_manager as m; r=m._load_policy(); "
            "assert c.validate_catalog(Path.cwd(), '0.160.0', policy=r).passed; "
            "assert m._validate_custom_agent_sources(Path.cwd(), '0.160.0', policy=r)"],
            cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(0, probe.returncode, probe.stderr)
        probe = subprocess.run([sys.executable, "-B", "-m", "scripts.validate_agent_configs",
            "--codex-version", "0.160.0"], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(0, probe.returncode, probe.stderr)
        self.assertIn("not yet validated", probe.stdout)

    def test_fabricated_static_entry_and_managed_payload_unchanged(self):
        self.document["versions"]["999.4.2"] = json.loads(json.dumps(self.document["versions"]["0.160.0"]))
        self.document["preferredInstallTarget"] = "999.4.2"
        self.write_policy()
        self.assertTrue(catalog.validate_catalog(self.source, "999.4.2").passed)
        self.install("999.4.2", custom=True)
        state = json.loads((self.target / manager.STATE_FILENAME).read_bytes())
        self.assertEqual("999.4.2", state["codexVersion"])
        self.assertFalse(any("compatibility" in name or "codex_compatibility" in name for name in state["files"]))
        self.assertEqual(set(manager.PACKAGE_FILES.values()) | set(manager.PROJECT_TEMPLATE_FILES.values()) |
                         set(manager.CUSTOM_AGENT_FILES.values()), set(state["files"]))
        registry = compatibility.load_registry(self.source)
        with patch.object(manager, "_detect_codex_version", side_effect=AssertionError("offline CLI probe")), redirect_stdout(StringIO()):
            manager.uninstall(self.target, False, policy=registry)
        self.assertEqual({}, self.snapshot())

    def test_corrupt_missing_duplicate_and_link_policy_fail_before_writes(self):
        original = self.registry_path.read_bytes()
        for content in (None, b"invalid", b'{"schemaVersion":1,"schemaVersion":1}', b"{}"):
            with self.subTest(content=content):
                if content is None:
                    self.registry_path.unlink()
                else:
                    self.registry_path.write_bytes(content)
                for dry_run in (False, True):
                    with patch.object(manager, "_detect_codex_version", side_effect=AssertionError("policy must precede probe")):
                        with self.assertRaises(manager.WorkflowError):
                            manager.install(self.target, self.source, dry_run)
                    self.assertEqual({}, self.snapshot())
                self.assertFalse(catalog.validate_catalog(self.source, "0.160.0").passed)
                self.registry_path.write_bytes(original)
        original_lstat = Path.lstat
        unsafe = self.registry_path.parent
        def reparse_metadata(path, *args, **kwargs):
            metadata = original_lstat(path, *args, **kwargs)
            if path == unsafe:
                return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
            return metadata
        with patch.object(Path, "lstat", reparse_metadata):
            for dry_run in (False, True):
                with self.assertRaises(manager.WorkflowError):
                    self.install(dry_run=dry_run)
                self.assertEqual({}, self.snapshot())
            self.assertFalse(catalog.validate_catalog(self.source, "0.160.0").passed)

    def test_snapshot_loaded_once_reused_after_policy_file_changes(self):
        original_loader = manager.load_registry
        def change_after_load(root):
            snapshot = original_loader(root)
            self.registry_path.write_bytes(b"broken after snapshot")
            return snapshot
        with patch.object(manager, "load_registry", side_effect=change_after_load) as loaded, \
             patch.object(catalog, "load_registry", side_effect=AssertionError("nested policy reload")):
            self.install(custom=True)
        loaded.assert_called_once_with(self.source)
        self.assertEqual("0.155.1", json.loads((self.target / manager.STATE_FILENAME).read_bytes())["codexVersion"])
        self.write_policy()
        with patch.object(manager, "load_registry", side_effect=change_after_load) as loaded, \
             patch.object(manager, "_detect_codex_version", side_effect=AssertionError("offline CLI probe")), \
             redirect_stdout(StringIO()):
            manager.uninstall(self.target, False, source_root=self.source)
        loaded.assert_called_once_with(self.source)
        self.assertEqual({}, self.snapshot())

    def test_supplied_policy_from_another_source_cannot_authorize_operations(self):
        foreign = self.directory / "foreign-source"
        (foreign / "compatibility").mkdir(parents=True)
        (foreign / manager.COMPATIBILITY_FILE).write_bytes(self.registry_path.read_bytes())
        foreign_policy = compatibility.load_registry(foreign)
        self.deny_old_static()
        before = self.snapshot()
        with patch.object(manager, "_detect_codex_version", side_effect=AssertionError("mismatch reached CLI")):
            for dry_run in (False, True):
                with self.subTest(dry_run=dry_run):
                    with self.assertRaisesRegex(manager.WorkflowError, "different source root"):
                        manager.install(self.target, self.source, dry_run, policy=foreign_policy)
                    with self.assertRaisesRegex(manager.WorkflowError, "different source root"):
                        manager.uninstall(self.target, dry_run, policy=foreign_policy, source_root=self.source)
                    with self.assertRaisesRegex(manager.WorkflowError, "different source root"):
                        manager.recover(self.target, self.target / manager.JOURNAL_FILENAME,
                                        dry_run, policy=foreign_policy, source_root=self.source)
                    self.assertEqual(before, self.snapshot())
        with patch.object(Path, "read_text", side_effect=AssertionError("mismatch read agent catalog")):
            report = catalog.validate_catalog(self.source, "0.155.1", policy=foreign_policy)
            self.assertFalse(report.passed)
            self.assertIn("different source root", report.errors[0])
            self.assertEqual((), report.agents)
            with self.assertRaises(compatibility.RegistryError) as caught:
                catalog.supported_versions(self.source, policy=foreign_policy)
            self.assertEqual("source_mismatch", caught.exception.category)

    def test_same_source_supplied_snapshot_keeps_policy_hash_without_registry_reads(self):
        registry = compatibility.load_registry(self.source)
        original_hash = registry.sha256
        self.registry_path.write_bytes(b"broken but still regular registry")
        original_open = Path.open
        def block_policy_read(path, *args, **kwargs):
            if path == self.registry_path:
                raise AssertionError("snapshot reopened registry")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", block_policy_read), \
             patch.object(compatibility.hashlib, "sha256", side_effect=AssertionError("snapshot rehash")):
            self.assertEqual(VERSIONS, catalog.supported_versions(self.source, policy=registry))
            self.assertTrue(catalog.validate_catalog(self.source, "0.160.0", policy=registry).passed)
        # Installer transactions hash their payloads; block only policy reopening here.
        with patch.object(Path, "open", block_policy_read), \
             patch.object(manager, "load_registry", side_effect=AssertionError("snapshot reload")):
            self.install(custom=True, policy=registry)
        self.assertEqual(original_hash, registry.sha256)
        with patch.object(Path, "open", block_policy_read), \
             patch.object(manager, "_detect_codex_version", side_effect=AssertionError("offline CLI probe")), \
             redirect_stdout(StringIO()):
            manager.uninstall(self.target, False, policy=registry)
        self.assertEqual({}, self.snapshot())

    def test_deleted_or_reparse_policy_path_invalidates_supplied_snapshot(self):
        registry = compatibility.load_registry(self.source)
        original = self.registry_path.read_bytes()
        self.registry_path.unlink()
        for dry_run in (False, True):
            with self.assertRaises(manager.WorkflowError):
                self.install(dry_run=dry_run, policy=registry)
            self.assertEqual({}, self.snapshot())
        self.assertFalse(catalog.validate_catalog(self.source, "0.160.0", policy=registry).passed)
        with self.assertRaises(compatibility.RegistryError):
            catalog.supported_versions(self.source, policy=registry)
        with patch.object(manager, "_detect_codex_version", side_effect=AssertionError("offline CLI probe")):
            for operation in (manager.uninstall, manager.recover):
                with self.assertRaises(manager.WorkflowError):
                    if operation is manager.uninstall:
                        operation(self.target, False, policy=registry)
                    else:
                        operation(self.target, self.target / manager.JOURNAL_FILENAME, False, policy=registry)
        self.registry_path.write_bytes(original)
        original_lstat = Path.lstat
        def unsafe_source(path, *args, **kwargs):
            metadata = original_lstat(path, *args, **kwargs)
            if path == self.source:
                return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
            return metadata
        original_resolve = Path.resolve
        def safe_target_resolve(path, *args, **kwargs):
            if path.is_relative_to(self.source):
                raise AssertionError("unsafe source resolved")
            return original_resolve(path, *args, **kwargs)
        with patch.object(Path, "lstat", unsafe_source), \
             patch.object(Path, "resolve", safe_target_resolve):
            for dry_run in (False, True):
                with self.assertRaises(manager.WorkflowError):
                    self.install(dry_run=dry_run, policy=registry)
                self.assertEqual({}, self.snapshot())
            self.assertFalse(catalog.validate_catalog(self.source, "0.160.0", policy=registry).passed)
            with self.assertRaises(compatibility.RegistryError):
                catalog.supported_versions(self.source, policy=registry)

    def test_explicit_source_ignores_cwd_environment_and_target_registry(self):
        unrelated = self.directory / "unrelated"
        unrelated.mkdir()
        target_registry = self.target / manager.COMPATIBILITY_FILE
        target_registry.parent.mkdir()
        target_registry.write_bytes(b"target policy must never be used")
        before = self.snapshot()
        prior = Path.cwd()
        os.chdir(unrelated)
        try:
            with patch.dict(os.environ, {"CODEX_SOURCE_ROOT": str(unrelated), "CODEX_VERSION": "unknown",
                    "CODEX_COMPATIBILITY_REGISTRY": str(target_registry)}):
                self.install(dry_run=True, custom=True)
                self.assertTrue(catalog.validate_catalog(self.source, "0.160.0").passed)
        finally:
            os.chdir(prior)
        self.assertEqual(before, self.snapshot())

    def test_historical_state_uses_registration_and_offline_default_source(self):
        self.install()
        state_path = self.target / manager.STATE_FILENAME
        state = json.loads(state_path.read_bytes())
        self.deny_old_static()
        registry = compatibility.load_registry(self.source)
        self.assertEqual("0.160.0", registry.preferred_install_target)
        with patch.object(manager, "_detect_codex_version", side_effect=AssertionError("offline CLI probe")):
            for version in sorted(VERSIONS):
                with self.subTest(version=version):
                    state["codexVersion"] = version
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    self.assertEqual(version, manager._read_state(state_path, policy=registry)["codexVersion"])
            state["codexVersion"] = "0.155.1"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            target_registry = self.target / manager.COMPATIBILITY_FILE
            target_registry.parent.mkdir()
            target_registry.write_bytes(b"untrusted target policy")
            with patch.object(manager, "load_registry", wraps=manager.load_registry) as loaded, redirect_stdout(StringIO()):
                manager.uninstall(self.target, True)
            self.assertEqual(Path(manager.__file__).absolute().parent.parent, loaded.call_args.args[0])
            target_registry.unlink()
            target_registry.parent.rmdir()
            with redirect_stdout(StringIO()):
                manager.uninstall(self.target, False, policy=registry)
        self.assertEqual({}, self.snapshot())

    def test_unknown_and_malformed_state_identity_fail_without_mutations(self):
        self.install()
        state_path = self.target / manager.STATE_FILENAME
        state = json.loads(state_path.read_bytes())
        for version in ("999.0.0", "codex-cli 0.155.1", None, [], True):
            with self.subTest(version=version):
                state["codexVersion"] = version
                state_path.write_text(json.dumps(state), encoding="utf-8")
                before = self.snapshot()
                with patch.object(manager, "_detect_codex_version", side_effect=AssertionError("offline CLI probe")):
                    with self.assertRaisesRegex(manager.WorkflowError, "malformed state manifest"):
                        manager._read_state(state_path)
                    for dry_run in (False, True):
                        with self.assertRaisesRegex(manager.WorkflowError, "malformed state manifest"):
                            manager.uninstall(self.target, dry_run)
                self.assertEqual(before, self.snapshot())

    def test_committed_uninstall_recovery_reuses_historical_policy_offline(self):
        (self.target / "AGENTS.md").write_bytes(b"# Existing\n")
        self.install()
        self.deny_old_static()
        with patch.object(manager, "_finalize_committed_uninstall", side_effect=OSError("cleanup interrupted")), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(OSError, "cleanup interrupted"):
                manager.uninstall(self.target, False, source_root=self.source)
        journal = self.target / manager.JOURNAL_FILENAME
        self.assertEqual("committed", json.loads(journal.read_bytes())["phase"])
        original_loader = manager.load_registry
        def change_after_load(root):
            snapshot = original_loader(root)
            self.registry_path.write_bytes(b"changed after recovery snapshot")
            return snapshot
        with patch.object(manager, "load_registry", side_effect=change_after_load) as loaded, \
             patch.object(manager, "_detect_codex_version", side_effect=AssertionError("offline CLI probe")), redirect_stdout(StringIO()):
            manager.recover(self.target, journal, False, source_root=self.source)
        loaded.assert_called_once_with(self.source)
        self.assertEqual({"AGENTS.md": b"# Existing\n"}, self.snapshot())


if __name__ == "__main__":
    unittest.main()
