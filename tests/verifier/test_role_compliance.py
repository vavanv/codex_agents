from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import role_compliance as rc
from validate_agent_configs import EXPECTED_ROLES


def digests(paths: tuple[str, ...]) -> dict[str, str]:
    return {path: f"hash-of-{path}" for path in paths}


class ChangedPathsTests(unittest.TestCase):
    def test_detects_added_removed_and_modified(self) -> None:
        before = digests(("a.txt", "b.txt"))
        after = digests(("b.txt", "c.txt"))
        after["b.txt"] = "changed"
        self.assertEqual({"a.txt", "b.txt", "c.txt"}, set(rc.changed_paths(before, after)))

    def test_no_change_returns_empty(self) -> None:
        before = digests(("a.txt", "b.txt"))
        self.assertEqual(set(), rc.changed_paths(before, dict(before)))


class ReadOnlyRoleTests(unittest.TestCase):
    def test_clean_read_only_role_passes(self) -> None:
        verdict = rc.evaluate_role("code-explorer", digests(("a.txt",)), digests(("a.txt",)))
        self.assertEqual("PASS", verdict.verdict)
        self.assertEqual((), verdict.reason_codes)

    def test_read_only_drift_fails(self) -> None:
        verdict = rc.evaluate_role(
            "code-explorer",
            digests(("a.txt",)),
            digests(("a.txt", "sneaky.txt")),
        )
        self.assertEqual("FAIL", verdict.verdict)
        self.assertIn("READONLY_DRIFT", verdict.reason_codes)
        self.assertEqual(("sneaky.txt",), verdict.changed_paths)

    def test_unattributed_role_is_unverified(self) -> None:
        verdict = rc.evaluate_role(
            "code-reviewer", digests(()), digests(()), attributed=False
        )
        self.assertEqual("UNVERIFIED", verdict.verdict)
        self.assertIn("UNATTRIBUTED_ROLE", verdict.reason_codes)


class WriteRoleTests(unittest.TestCase):
    def test_in_scope_write_passes(self) -> None:
        before = digests(("src/calculator.py", "src/module_a.py"))
        after = dict(before)
        after["src/calculator.py"] = "fixed"
        verdict = rc.evaluate_role(
            "implementer",
            before,
            after,
            allowed_paths=frozenset({"src/calculator.py"}),
        )
        self.assertEqual("PASS", verdict.verdict)
        self.assertEqual(("src/calculator.py",), verdict.changed_paths)

    def test_out_of_scope_write_fails(self) -> None:
        before = digests(("src/calculator.py", "src/module_a.py"))
        after = dict(before)
        after["src/module_a.py"] = "touched"
        verdict = rc.evaluate_role(
            "implementer",
            before,
            after,
            allowed_paths=frozenset({"src/calculator.py"}),
        )
        self.assertEqual("FAIL", verdict.verdict)
        self.assertIn("WRITE_OUT_OF_SCOPE", verdict.reason_codes)

    def test_unauthorized_head_change_fails(self) -> None:
        verdict = rc.evaluate_role(
            "implementer",
            digests(()),
            digests(()),
            head_before="a" * 40,
            head_after="b" * 40,
            allowed_paths=frozenset(),
        )
        self.assertEqual("FAIL", verdict.verdict)
        self.assertIn("UNAUTHORIZED_COMMIT", verdict.reason_codes)

    def test_missing_scope_blocks(self) -> None:
        verdict = rc.evaluate_role(
            "quick-implementer", digests(()), digests(("new.txt",))
        )
        self.assertEqual("BLOCKED", verdict.verdict)
        self.assertIn("MISSING_ALLOWED_SCOPE", verdict.reason_codes)


class PublishRoleTests(unittest.TestCase):
    def test_commit_pusher_requires_authorization(self) -> None:
        verdict = rc.evaluate_role("commit-pusher", digests(()), digests(()))
        self.assertEqual("BLOCKED", verdict.verdict)
        self.assertIn("COMMIT_NOT_AUTHORIZED", verdict.reason_codes)

    def test_authorized_commit_pusher_with_clean_tree_passes(self) -> None:
        verdict = rc.evaluate_role(
            "commit-pusher", digests(()), digests(()), authorized_commit=True
        )
        self.assertEqual("PASS", verdict.verdict)

    def test_commit_pusher_file_drift_fails(self) -> None:
        verdict = rc.evaluate_role(
            "commit-pusher",
            digests(()),
            digests(("unexpected.txt",)),
            authorized_commit=True,
        )
        self.assertEqual("FAIL", verdict.verdict)
        self.assertIn("WRITE_OUT_OF_SCOPE", verdict.reason_codes)


class EvaluateRolesTests(unittest.TestCase):
    @staticmethod
    def attribution() -> dict[str, str]:
        return {
            role: f"00000000-0000-4000-8000-{index + 2:012d}"
            for index, role in enumerate(sorted(EXPECTED_ROLES))
        }

    def test_single_clean_read_only_role_with_unattributed_rest(self) -> None:
        attribution = {"code-explorer": "00000000-0000-4000-8000-000000000002"}
        verdicts = rc.evaluate_roles(
            attribution, digests(("a.txt",)), digests(("a.txt",))
        )
        by_role = {item.role: item for item in verdicts}
        self.assertEqual("PASS", by_role["code-explorer"].verdict)
        unattributed = [item for item in verdicts if not item.attributed]
        self.assertEqual(8, len(unattributed))
        self.assertTrue(all(item.verdict == "UNVERIFIED" for item in unattributed))

    def test_single_in_scope_writer_with_unattributed_rest(self) -> None:
        attribution = {"implementer": "00000000-0000-4000-8000-000000000002"}
        before = digests(("src/calculator.py",))
        after = dict(before)
        after["src/calculator.py"] = "fixed"
        verdicts = rc.evaluate_roles(
            attribution,
            before,
            after,
            allowed_paths={"implementer": frozenset({"src/calculator.py"})},
        )
        by_role = {item.role: item for item in verdicts}
        self.assertEqual("PASS", by_role["implementer"].verdict)
        self.assertEqual("UNVERIFIED", by_role["code-explorer"].verdict)
        self.assertEqual("UNVERIFIED", by_role["quick-implementer"].verdict)

    def test_attributed_write_role_without_scope_is_blocked(self) -> None:
        attribution = {"quick-implementer": "00000000-0000-4000-8000-000000000002"}
        verdicts = rc.evaluate_roles(
            attribution, digests(("a.txt",)), digests(("a.txt", "b.txt"))
        )
        by_role = {item.role: item for item in verdicts}
        self.assertEqual("BLOCKED", by_role["quick-implementer"].verdict)

    def test_summary_reports_counts_and_overall(self) -> None:
        allowed_paths = {
            "implementer": frozenset(),
            "quick-implementer": frozenset(),
            "luna-escalation": frozenset(),
        }
        verdicts = rc.evaluate_roles(
            self.attribution(),
            digests(()),
            digests(()),
            allowed_paths=allowed_paths,
            authorized_commit=True,
        )
        result = rc.summary(verdicts)
        self.assertEqual("PASS", result["overall"])
        self.assertEqual(9, result["counts"]["PASS"])
        self.assertEqual(9, len(result["roles"]))


if __name__ == "__main__":
    unittest.main()

