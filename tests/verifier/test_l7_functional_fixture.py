from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import l7_functional_fixture as fixture


class L7FunctionalFixtureTests(unittest.TestCase):
    def test_pinned_bytes_match_existing_builder(self):
        # Parse literals instead of invoking the live fixture writer.
        import ast
        source = Path("scripts/live_validation_support.py").read_text(encoding="utf-8")
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "_write_fixture_sources")
        files = ast.literal_eval(function.body[0].value)
        self.assertEqual(files[fixture.SOURCE_PATH].encode(), fixture.BASELINE_SOURCE)
        self.assertEqual(files[fixture.TEST_PATH].encode(), fixture.IMMUTABLE_TEST)
        self.assertEqual(fixture.BASELINE_SOURCE.replace(b"return left - right", b"return left + right"),
                         fixture.REPAIRED_SOURCE)

    def test_baseline_repair_and_bounded_report(self):
        with fixture.prepared_fixture() as case:
            before = fixture._inventory(case.root)
            case.observe_baseline()
            case.apply_expected_repair()
            report = case.validate_repair()
            after = fixture._inventory(case.root)
            self.assertEqual([fixture.SOURCE_PATH], [key for key in before if before[key] != after[key]])
            facts = report.as_dict()
            self.assertEqual("OFFLINE_VALIDATED", facts["decision"])
            self.assertFalse(facts["l7Accepted"])
            self.assertFalse(facts["runtimeValidated"])
            self.assertEqual("NOT_EXERCISED", facts["escalation"])
            self.assertEqual(hashlib.sha256(fixture.IMMUTABLE_TEST).hexdigest(), facts["hashes"]["immutableTest"])
            with self.assertRaises(dataclasses.FrozenInstanceError):
                report.baseline_tests = 9
            facts["l7Accepted"] = True
            self.assertFalse(report.as_dict()["l7Accepted"])

    def test_missing_baseline_not_accepted(self):
        with fixture.prepared_fixture() as case:
            with self.assertRaises(fixture.FixtureRejected):
                case.apply_expected_repair()
            (case.root / fixture.SOURCE_PATH).write_bytes(fixture.REPAIRED_SOURCE)
            with self.assertRaises(fixture.FixtureRejected):
                case.validate_repair()

    def test_arbitrary_root_and_destination_rejected(self):
        with self.assertRaises(fixture.FixtureRejected):
            fixture.OfflineFixture(Path(".."))
        with self.assertRaises(TypeError):
            fixture.prepared_fixture(Path("../escape"))

    def test_cases_are_fresh_and_closed_case_cannot_be_reused(self):
        with fixture.prepared_fixture() as first:
            first_root = first.root
        self.assertFalse(first_root.exists())
        with self.assertRaises(fixture.FixtureRejected):
            first.observe_baseline()
        with fixture.prepared_fixture() as second:
            self.assertNotEqual(first_root, second.root)
            self.assertEqual(fixture.BASELINE_SOURCE, (second.root / fixture.SOURCE_PATH).read_bytes())

    def test_altered_test_rejected_before_execution(self):
        with fixture.prepared_fixture() as case:
            (case.root / fixture.TEST_PATH).write_bytes(b"raise RuntimeError('must not execute')\n")
            with mock.patch("builtins.compile", side_effect=AssertionError("executed untrusted bytes")):
                with self.assertRaises(fixture.FixtureRejected):
                    case.observe_baseline()

    def test_wrong_repairs_and_line_encodings_rejected(self):
        alternatives = [fixture.BASELINE_SOURCE, b"def add(a, b): return a + b\n",
                        b"\xef\xbb\xbf" + fixture.REPAIRED_SOURCE,
                        fixture.REPAIRED_SOURCE.replace(b"\n", b"\r\n")]
        for content in alternatives:
            with self.subTest(content=content), fixture.prepared_fixture() as case:
                case.observe_baseline()
                (case.root / fixture.SOURCE_PATH).write_bytes(content)
                with self.assertRaises(fixture.FixtureRejected):
                    case.validate_repair()

    def test_extra_missing_and_directory_entries_rejected(self):
        for alteration in ("extra", "missing", "directory"):
            with self.subTest(alteration=alteration), fixture.prepared_fixture() as case:
                if alteration == "extra":
                    (case.root / "extra.txt").write_bytes(b"extra")
                elif alteration == "missing":
                    (case.root / fixture.TEST_PATH).unlink()
                else:
                    (case.root / "extra-directory").mkdir()
                with self.assertRaises(fixture.FixtureRejected):
                    case.observe_baseline()

    def test_reparse_and_symlink_metadata_rejected(self):
        for metadata in (SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0),
                         SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)):
            with self.subTest(metadata=metadata), mock.patch.object(Path, "lstat", return_value=metadata):
                with self.assertRaises(fixture.FixtureRejected):
                    fixture._ordinary(Path("fixture"))

    def test_hardlink_metadata_rejected(self):
        with fixture.prepared_fixture() as case:
            original = Path.lstat
            target = case.root / fixture.SOURCE_PATH
            def altered(path):
                info = original(path)
                if path == target:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0, st_nlink=2)
                return info
            with mock.patch.object(Path, "lstat", altered):
                with self.assertRaises(fixture.FixtureRejected):
                    case.observe_baseline()

    def test_ancestor_reparse_checked_before_compilation(self):
        with mock.patch.object(fixture, "_ordinary", side_effect=fixture.FixtureRejected("ancestor")):
            with self.assertRaises(fixture.FixtureRejected):
                with fixture.prepared_fixture():
                    self.fail("unsafe ancestor accepted")

    def test_only_expected_assertion_is_accepted(self):
        with fixture.prepared_fixture() as case:
            for failure in ("AssertionError: another failure", ""):
                result = unittest.TestResult()
                result.testsRun = 1
                result.failures = [(None, failure)] if failure else []
                with mock.patch.object(case, "_execute", return_value=result):
                    with self.assertRaises(fixture.FixtureRejected):
                        case.observe_baseline()

    def test_interpreter_state_restored_on_success_and_exception(self):
        with fixture.prepared_fixture() as case:
            modules = sys.modules.copy()
            path = sys.path[:]
            bytecode = sys.dont_write_bytecode
            case.observe_baseline()
            self.assertEqual(path, sys.path)
            self.assertEqual(modules, sys.modules)
            self.assertEqual(bytecode, sys.dont_write_bytecode)
            with mock.patch("builtins.compile", side_effect=RuntimeError("compile failure")):
                with self.assertRaises(RuntimeError):
                    case.observe_baseline()
            self.assertEqual(path, sys.path)
            self.assertEqual(modules, sys.modules)
            self.assertEqual(bytecode, sys.dont_write_bytecode)


if __name__ == "__main__":
    unittest.main()
