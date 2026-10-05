from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import codex_compatibility as policy


# Independent regression oracle: never derive expectations from the registry.
MATRIX = {
    "0.155.1": (True, True, True, ("v1",)),
    "0.157.1": (True, True, True, ("v1", "v2")),
    "0.159.0": (True, True, True, ("v1", "v2")),
    "0.159.3": (True, True, True, ("v1", "v2")),
    "0.160.0": (True, False, False, ()),
}


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "compatibility" / "codex-agents.json"
        self.path.parent.mkdir()
        self.document = json.loads((ROOT / "compatibility/codex-agents.json").read_bytes())
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.document), encoding="utf-8")

    def assert_error(self, category, call, *args):
        with self.assertRaises(policy.RegistryError) as caught:
            call(*args)
        self.assertEqual(category, caught.exception.category)

    def test_fixed_matrix_and_exact_rollout_membership(self):
        registry = policy.load_registry(ROOT)
        self.assertEqual(set(MATRIX), set(registry.versions))
        for version, (static, discovery, capture, schemas) in MATRIX.items():
            with self.subTest(version=version):
                info = policy.version_info(registry, version)
                self.assertEqual((static, discovery, capture), tuple(info.gates[g] for g in
                    ("staticInstallation", "discoveryDiagnostic", "capturedEvidence")))
                self.assertEqual(schemas, info.rollout_schemas)
                self.assertIs(False, info.runtime_validated)
                self.assertIs(info, policy.require_gate(registry, version, "staticInstallation"))
                for schema in ("v1", "v2"):
                    if schema in schemas:
                        self.assertIs(info, policy.require_rollout_schema(registry, version, schema))
                    else:
                        self.assert_error("unsupported_rollout" if capture else "denied_gate",
                            policy.require_rollout_schema, registry, version, schema)
        self.assertEqual(set(MATRIX), set(policy.versions_for_gate(registry, "staticInstallation")))
        for gate in ("discoveryDiagnostic", "capturedEvidence"):
            self.assertEqual({"0.155.1", "0.157.1", "0.159.0", "0.159.3"},
                set(policy.versions_for_gate(registry, gate)))
            self.assert_error("denied_gate", policy.require_gate, registry, "0.160.0", gate)

    def test_profile_target_and_snapshot_hash(self):
        registry = policy.load_registry(self.root)
        self.assertEqual("0.160.0", registry.preferred_install_target)
        profile = policy.run_profile(registry, "legacy-windows-capture")
        self.assertEqual("0.159.3", profile.expected_version)
        self.assertEqual("codex-cli 0.159.3", profile.cli_banner)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), registry.sha256)
        self.assertEqual(self.path.resolve(), registry.source_path)
        self.assertEqual(self.root, registry.source_root)
        self.document["preferredInstallTarget"] = "0.155.1"
        self.write()
        self.assertEqual(profile, policy.run_profile(policy.load_registry(self.root), "legacy-windows-capture"))

    def test_deep_immutability_and_independent_snapshot(self):
        registry = policy.load_registry(self.root)
        info = policy.version_info(registry, "0.159.3")
        for mapping, key, value in ((registry.versions, "fake", info),
            (registry.run_profiles, "fake", None), (info.gates, "capturedEvidence", False),
            (info.models, "gpt-6-sol", ("none",))):
            with self.assertRaises(TypeError):
                mapping[key] = value
        for value, field in ((registry, "preferred_install_target"), (registry, "source_root"), (info, "version"),
            (policy.run_profile(registry, "legacy-windows-capture"), "expected_version")):
            with self.assertRaises(FrozenInstanceError):
                setattr(value, field, "changed")
        self.assertIsInstance(info.allowed_keys, tuple)
        self.assertIsInstance(info.required_keys, tuple)
        self.assertIsInstance(info.sandbox_modes, tuple)
        self.assertIsInstance(info.rollout_schemas, tuple)
        self.assertIsInstance(info.models["gpt-6-sol"], tuple)
        self.path.write_text("broken", encoding="utf-8")
        self.assertTrue(info.gates["capturedEvidence"])

    def test_source_binding_is_lexical_and_does_not_reread_changed_bytes(self):
        registry = policy.load_registry(self.root)
        original_hash = registry.sha256
        self.path.write_bytes(b"broken JSON after loading")
        with patch.object(Path, "open", side_effect=AssertionError("binding reopened policy")), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("binding reread policy")), \
             patch.object(policy.hashlib, "sha256", side_effect=AssertionError("binding rehashed policy")):
            self.assertIs(registry, policy.require_source_root(registry, self.root))
        self.assertEqual(original_hash, registry.sha256)
        other = self.root / "other-source"
        (other / "compatibility").mkdir(parents=True)
        (other / "compatibility/codex-agents.json").write_bytes(b"binding does not parse JSON")
        self.assert_error("source_mismatch", policy.require_source_root, registry, other)
        # Even a lexical alias resolving to the same real root cannot substitute.
        self.assert_error("source_mismatch", policy.require_source_root, registry, other / "..")
        self.path.unlink()
        self.assert_error("malformed_policy", policy.require_source_root, registry, self.root)
        self.path.mkdir()
        self.assert_error("malformed_policy", policy.require_source_root, registry, self.root)
        for root in (None, [], "."):
            self.assert_error("malformed_policy", policy.require_source_root, registry, root)

    def test_source_binding_reparse_alias_fails_before_resolution(self):
        registry = policy.load_registry(self.root)
        alias = self.root / "alias"
        original_lstat = Path.lstat
        def alias_metadata(path, *args, **kwargs):
            actual = self.root / path.relative_to(alias) if path.is_relative_to(alias) else path
            metadata = original_lstat(actual, *args, **kwargs)
            if path == alias:
                return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
            return metadata
        with patch.object(Path, "lstat", alias_metadata), \
             patch.object(Path, "resolve", side_effect=AssertionError("unsafe alias resolved")):
            self.assert_error("malformed_policy", policy.require_source_root, registry, alias)

    def test_unknown_queries_fail_closed(self):
        registry = policy.load_registry(self.root)
        for version in ("999.0.0", "codex-cli 0.159.3", "0.159", None, []):
            self.assert_error("unknown_version", policy.version_info, registry, version)
        for gate in ("runtimeValidated", "", None, []):
            self.assert_error("unknown_gate", policy.versions_for_gate, registry, gate)
            self.assert_error("unknown_gate", policy.require_gate, registry, "0.159.3", gate)
        self.assert_error("unknown_profile", policy.run_profile, registry, "latest")
        self.assert_error("unsupported_rollout", policy.require_rollout_schema, registry, "0.159.3", "v3")

    def test_additive_static_only_extension_and_model_specific_efforts(self):
        entry = json.loads(json.dumps(self.document["versions"]["0.160.0"]))
        entry["models"] = {"fictional-small": ["low"], "fictional-large": ["high", "ultra"]}
        self.document["versions"]["999.4.2"] = entry
        self.document["preferredInstallTarget"] = "999.4.2"
        self.write()
        registry = policy.load_registry(self.root)
        info = policy.require_gate(registry, "999.4.2", "staticInstallation")
        self.assertEqual(("low",), info.models["fictional-small"])
        self.assertNotIn("ultra", info.models["fictional-small"])
        for gate in ("discoveryDiagnostic", "capturedEvidence"):
            self.assert_error("denied_gate", policy.require_gate, registry, "999.4.2", gate)
        self.assertEqual("0.159.3", policy.run_profile(registry, "legacy-windows-capture").expected_version)

    def test_malformed_static_fields_and_metadata(self):
        baseline = json.dumps(self.document)
        changes = [
            ("agentSchemaVersion", True), ("runtimeValidated", 0),
            ("allowedKeys", "name"), ("allowedKeys", ["name", "name"]),
            ("allowedKeys", [1]), ("requiredKeys", ["unknown"]),
            ("sandboxModes", []), ("sandboxModes", [False]), ("sandboxModes", ["readonly"]),
            ("models", []), ("models", {}), ("models", {"": ["low"]}),
            ("models", {"a": "low"}), ("models", {"a": ["low", "low"]}),
            ("models", {"a": ["unknown"]}), ("models", {"a": [True]}),
            ("gates", {}), ("gates", {"staticInstallation": True}),
            ("gates", {"staticInstallation": 1, "discoveryDiagnostic": True, "capturedEvidence": True}),
            ("gates", {"staticInstallation": True, "discoveryDiagnostic": True, "capturedEvidence": True, "unknown": False}),
            ("rolloutSchemas", ["v3"]), ("rolloutSchemas", ["v1", "v1"]),
            ("rolloutSchemas", [1]), ("rolloutSchemas", "v1"),
        ]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                self.document = json.loads(baseline)
                self.document["versions"]["0.159.3"][field] = value
                self.write()
                self.assert_error("malformed_policy", policy.load_registry, self.root)
        for field in ("gates", "rolloutSchemas", "models", "runtimeValidated", "agentSchemaVersion", "allowedKeys", "requiredKeys", "sandboxModes"):
            with self.subTest(missing=field):
                self.document = json.loads(baseline)
                del self.document["versions"]["0.159.3"][field]
                self.write()
                self.assert_error("malformed_policy", policy.load_registry, self.root)

    def test_schema_types_and_invalid_references(self):
        baseline = json.dumps(self.document)
        for field in ("schemaVersion", "policySchemaVersion"):
            for value, category in ((True, "malformed_policy"), ("1", "malformed_policy"), (2, "unsupported_schema"), (None, "malformed_policy")):
                self.document = json.loads(baseline)
                self.document[field] = value
                self.write()
                self.assert_error(category, policy.load_registry, self.root)
        for field in ("schemaVersion", "policySchemaVersion", "runProfiles", "preferredInstallTarget", "versions", "documentationChecked"):
            self.document = json.loads(baseline)
            del self.document[field]
            self.write()
            self.assert_error("malformed_policy", policy.load_registry, self.root)
        self.document = json.loads(baseline)
        self.document["versions"]["0.159.3"]["agentSchemaVersion"] = 2
        self.write()
        self.assert_error("unsupported_schema", policy.load_registry, self.root)
        for target in ("unknown", "01.2.3", "1.2", "1.2.3-beta"):
            self.document = json.loads(baseline)
            self.document["versions"][target] = self.document["versions"].pop("0.160.0")
            self.write()
            self.assert_error("malformed_policy", policy.load_registry, self.root)
        self.document = json.loads(baseline)
        self.document["preferredInstallTarget"] = "999.0.0"
        self.write()
        self.assert_error("invalid_reference", policy.load_registry, self.root)
        self.document = json.loads(baseline)
        self.document["versions"]["0.160.0"]["gates"]["staticInstallation"] = False
        self.write()
        self.assert_error("invalid_reference", policy.load_registry, self.root)
        for version in ("999.0.0", "0.160.0"):
            self.document = json.loads(baseline)
            self.document["runProfiles"]["legacy-windows-capture"]["expectedVersion"] = version
            self.write()
            self.assert_error("invalid_reference", policy.load_registry, self.root)
        self.document = json.loads(baseline)
        self.document["versions"]["0.160.0"]["rolloutSchemas"] = ["v1"]
        self.write()
        self.assert_error("malformed_policy", policy.load_registry, self.root)

    def test_duplicate_json_keys_at_every_policy_depth_and_bad_json(self):
        for raw in ('{"schemaVersion":1,"schemaVersion":1}',
            '{"versions":{"0.159.3":{},"0.159.3":{}}}',
            '{"gates":{"capturedEvidence":true,"capturedEvidence":false}}',
            '{"models":{"model":["low"],"model":["high"]}}',
            '{"runProfiles":{"legacy":{"expectedVersion":"1.2.3","expectedVersion":"9.8.7"}}}',
            '[]', '{broken', '{"x":NaN}'):
            self.path.write_text(raw, encoding="utf-8")
            self.assert_error("malformed_policy", policy.load_registry, self.root)
        self.path.write_bytes(b"\xff")
        self.assert_error("malformed_policy", policy.load_registry, self.root)

    def test_malformed_roots_profiles_and_nonstandard_json_numbers(self):
        baseline = json.dumps(self.document)
        changes = (("versions", []), ("versions", {}), ("documentationChecked", False),
            ("runProfiles", []), ("runProfiles", {}),
            ("runProfiles", {"legacy": []}), ("runProfiles", {"legacy": {}}),
            ("runProfiles", {"legacy": {"expectedVersion": True}}),
            ("runProfiles", {"legacy": {"expectedVersion": "0.159.3", "override": True}}),
            ("preferredInstallTarget", True))
        for field, value in changes:
            with self.subTest(field=field, value=value):
                self.document = json.loads(baseline)
                self.document[field] = value
                self.write()
                self.assert_error("malformed_policy", policy.load_registry, self.root)
        self.document = json.loads(baseline)
        self.document["extension"] = float("nan")
        self.write()
        self.assert_error("malformed_policy", policy.load_registry, self.root)

    def test_explicit_root_ignores_cwd_environment_and_other_registries(self):
        prior = Path.cwd()
        with tempfile.TemporaryDirectory() as unrelated:
            os.chdir(unrelated)
            try:
                with patch.dict(os.environ, {"CODEX_SOURCE_ROOT": unrelated, "CODEX_COMPATIBILITY_REGISTRY": unrelated,
                    "CODEX_VERSION": "999.0.0"}):
                    registry = policy.load_registry(self.root)
                    self.assertEqual(self.path.resolve(), registry.source_path)
                    self.assertEqual("0.160.0", registry.preferred_install_target)
                self.assert_error("malformed_policy", policy.load_registry, Path("."))
                self.assert_error("malformed_policy", policy.load_registry, Path(unrelated))
            finally:
                os.chdir(prior)

    def test_missing_unreadable_oversized_and_nonregular_inputs(self):
        for root in (None, [], 1, "\x00"):
            self.assert_error("malformed_policy", policy.load_registry, root)
        self.path.unlink()
        self.assert_error("malformed_policy", policy.load_registry, self.root)
        self.path.mkdir()
        self.assert_error("malformed_policy", policy.load_registry, self.root)
        self.path.rmdir()
        self.path.write_bytes(b" " * (64 * 1024 + 1))
        self.assert_error("malformed_policy", policy.load_registry, self.root)
        self.write()
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assert_error("malformed_policy", policy.load_registry, self.root)

    def test_registry_and_parent_symlinks_are_rejected(self):
        destination = self.root / "real.json"
        self.path.replace(destination)
        try:
            self.path.symlink_to(destination)
        except OSError:
            # Windows may lack link creation privileges; test the OS metadata contract.
            metadata = self.path.parent.stat()
            class ReparseMetadata:
                st_mode = metadata.st_mode
                st_file_attributes = 0x400
            with patch.object(Path, "lstat", return_value=ReparseMetadata()):
                self.assert_error("malformed_policy", policy.load_registry, self.root)
            return
        self.assert_error("malformed_policy", policy.load_registry, self.root)
        self.path.unlink()
        self.path.parent.rmdir()
        target = self.root / "other"
        target.mkdir()
        (target / "codex-agents.json").write_bytes(destination.read_bytes())
        self.path.parent.symlink_to(target, target_is_directory=True)
        self.assert_error("malformed_policy", policy.load_registry, self.root)

    def test_import_performs_no_io_or_execution(self):
        name = "compatibility_import_probe"
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/codex_compatibility.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        with patch.object(Path, "open", side_effect=AssertionError("import read")), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("import read")), \
             patch("builtins.open", side_effect=AssertionError("import read")), \
             patch("subprocess.run", side_effect=AssertionError("import execution")):
            spec.loader.exec_module(module)


if __name__ == "__main__":
    unittest.main()
