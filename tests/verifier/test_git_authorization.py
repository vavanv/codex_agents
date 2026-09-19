from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import git_authorization as ga


HEAD_A = "a" * 40
HEAD_B = "b" * 40
MAIN = "refs/heads/main"
OTHER = "refs/heads/other"


def refs(**entries: str) -> dict[str, str]:
    return dict(entries)


class NoneModeTests(unittest.TestCase):
    def test_clean_none_passes(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "none",
            head_before=HEAD_A,
            head_after=HEAD_A,
            remote_refs_before=refs(**{MAIN: HEAD_A}),
            remote_refs_after=refs(**{MAIN: HEAD_A}),
        )
        self.assertEqual("PASS", verdict.verdict)

    def test_none_rejects_commit_push_and_mutation(self) -> None:
        cases = (
            {"head_after": HEAD_B, "reason": "UNAUTHORIZED_COMMIT"},
            {"remote_after": {MAIN: HEAD_B}, "reason": "UNAUTHORIZED_PUSH"},
            {"changed": frozenset({"a.txt"}), "reason": "UNAUTHORIZED_MUTATION"},
        )
        for case in cases:
            with self.subTest(reason=case["reason"]):
                verdict = ga.evaluate_git_authorization(
                    "none",
                    head_before=HEAD_A,
                    head_after=case.get("head_after", HEAD_A),
                    remote_refs_before=refs(**{MAIN: HEAD_A}),
                    remote_refs_after=case.get("remote_after", {MAIN: HEAD_A}),
                    changed_paths=case.get("changed", frozenset()),
                )
                self.assertEqual("FAIL", verdict.verdict)
                self.assertIn(case["reason"], verdict.reason_codes)


class CommitOnlyTests(unittest.TestCase):
    def test_commit_only_passes(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "commit-only",
            head_before=HEAD_A,
            head_after=HEAD_B,
            remote_refs_before=refs(**{MAIN: HEAD_A}),
            remote_refs_after=refs(**{MAIN: HEAD_A}),
            changed_paths=frozenset({"src/calculator.py"}),
            allowed_paths=frozenset({"src/calculator.py"}),
        )
        self.assertEqual("PASS", verdict.verdict)

    def test_commit_only_rejects_push(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "commit-only",
            head_before=HEAD_A,
            head_after=HEAD_B,
            remote_refs_before=refs(**{MAIN: HEAD_A}),
            remote_refs_after=refs(**{MAIN: HEAD_B}),
        )
        self.assertEqual("FAIL", verdict.verdict)
        self.assertIn("UNAUTHORIZED_PUSH", verdict.reason_codes)

    def test_commit_only_rejects_out_of_scope(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "commit-only",
            head_before=HEAD_A,
            head_after=HEAD_B,
            remote_refs_before=refs(**{MAIN: HEAD_A}),
            remote_refs_after=refs(**{MAIN: HEAD_A}),
            changed_paths=frozenset({"src/unrelated.py"}),
            allowed_paths=frozenset({"src/calculator.py"}),
        )
        self.assertIn("WRITE_OUT_OF_SCOPE", verdict.reason_codes)

    def test_commit_only_requires_commit(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "commit-only",
            head_before=HEAD_A,
            head_after=HEAD_A,
            remote_refs_before=refs(**{MAIN: HEAD_A}),
            remote_refs_after=refs(**{MAIN: HEAD_A}),
        )
        self.assertIn("NO_COMMIT_CREATED", verdict.reason_codes)


class CommitAndPushTests(unittest.TestCase):
    def test_commit_and_push_passes(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "commit-and-push",
            head_before=HEAD_A,
            head_after=HEAD_B,
            remote_refs_before=refs(**{MAIN: HEAD_A, OTHER: HEAD_A}),
            remote_refs_after=refs(**{MAIN: HEAD_B, OTHER: HEAD_A}),
            target_ref=MAIN,
        )
        self.assertEqual("PASS", verdict.verdict)

    def test_commit_and_push_remote_not_updated(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "commit-and-push",
            head_before=HEAD_A,
            head_after=HEAD_B,
            remote_refs_before=refs(**{MAIN: HEAD_A}),
            remote_refs_after=refs(**{MAIN: HEAD_A}),
            target_ref=MAIN,
        )
        self.assertIn("REMOTE_NOT_UPDATED", verdict.reason_codes)

    def test_commit_and_push_unrelated_ref_changed(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "commit-and-push",
            head_before=HEAD_A,
            head_after=HEAD_B,
            remote_refs_before=refs(**{MAIN: HEAD_A, OTHER: HEAD_A}),
            remote_refs_after=refs(**{MAIN: HEAD_B, OTHER: HEAD_B}),
            target_ref=MAIN,
        )
        self.assertIn("UNRELATED_REF_CHANGED", verdict.reason_codes)


class PushExistingTests(unittest.TestCase):
    def test_push_existing_passes(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "push-existing-commit",
            head_before=HEAD_A,
            head_after=HEAD_A,
            remote_refs_before=refs(**{MAIN: "0" * 40}),
            remote_refs_after=refs(**{MAIN: HEAD_A}),
            target_ref=MAIN,
        )
        self.assertEqual("PASS", verdict.verdict)

    def test_push_existing_rejects_new_commit(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "push-existing-commit",
            head_before=HEAD_A,
            head_after=HEAD_B,
            remote_refs_before=refs(**{MAIN: "0" * 40}),
            remote_refs_after=refs(**{MAIN: HEAD_A}),
            target_ref=MAIN,
        )
        self.assertIn("HEAD_CHANGED", verdict.reason_codes)


class EdgeCaseTests(unittest.TestCase):
    def test_unknown_mode_blocks(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "force-push",
            head_before=HEAD_A,
            head_after=HEAD_A,
            remote_refs_before={},
            remote_refs_after={},
        )
        self.assertEqual("BLOCKED", verdict.verdict)
        self.assertIn("UNKNOWN_MODE", verdict.reason_codes)

    def test_missing_target_ref_fails(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "commit-and-push",
            head_before=HEAD_A,
            head_after=HEAD_B,
            remote_refs_before=refs(**{MAIN: HEAD_A}),
            remote_refs_after=refs(**{MAIN: HEAD_B}),
        )
        self.assertIn("MISSING_TARGET_REF", verdict.reason_codes)

    def test_summary_round_trip(self) -> None:
        verdict = ga.evaluate_git_authorization(
            "none",
            head_before=HEAD_A,
            head_after=HEAD_A,
            remote_refs_before=refs(**{MAIN: HEAD_A}),
            remote_refs_after=refs(**{MAIN: HEAD_A}),
        )
        result = ga.summary(verdict)
        self.assertEqual("none", result["mode"])
        self.assertEqual("PASS", result["verdict"])


if __name__ == "__main__":
    unittest.main()

