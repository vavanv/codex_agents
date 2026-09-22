from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import role_orchestration as ro
from validate_agent_configs import EXPECTED_ROLES


PARENT = "00000000-0000-4000-8000-000000000001"
WRITE_ROLES = ("quick_implementer", "implementer", "luna_escalation", "commit_pusher")


def child(index: int) -> str:
    return f"00000000-0000-4000-8000-{index + 2:012d}"


def stream_for(*roles: str, terminal: bool = True) -> str:
    lines = [json.dumps({"type": "thread.started", "thread_id": PARENT})]
    for index, role in enumerate(sorted(roles)):
        lines.append(
            json.dumps(
                {
                    "type": "agent.spawned",
                    "agent_name": role,
                    "child_session_id": child(index),
                }
            )
        )
    if terminal:
        lines.append(json.dumps({"type": "turn.completed"}))
    return "\n".join(lines) + "\n"


def digests(paths: tuple[str, ...]) -> dict[str, str]:
    return {path: f"hash-of-{path}" for path in paths}


class OrchestrationTests(unittest.TestCase):
    def test_all_roles_clean_pass(self) -> None:
        allowed_paths = {
            "implementer": frozenset(),
            "quick_implementer": frozenset(),
            "luna_escalation": frozenset(),
        }
        report = ro.build_role_evidence(
            stream_for(*EXPECTED_ROLES),
            digests(("a.txt",)),
            digests(("a.txt",)),
            allowed_paths=allowed_paths,
            authorized_commit=True,
        )
        self.assertEqual("PASS", report.overall)
        self.assertEqual(PARENT, report.parent_session_id)
        self.assertEqual(11, report.event_count)
        self.assertEqual("complete", report.stream_integrity)
        self.assertEqual((), report.parse_reason_codes)
        self.assertTrue(all(role.verdict == "PASS" for role in report.roles))

    def test_single_read_only_drift_fails(self) -> None:
        report = ro.build_role_evidence(
            stream_for("code_explorer"),
            digests(("a.txt",)),
            digests(("a.txt", "sneaky.txt")),
        )
        by_role = {item.role: item for item in report.roles}
        self.assertEqual("FAIL", by_role["code_explorer"].verdict)
        self.assertIn("READONLY_DRIFT", by_role["code_explorer"].reason_codes)
        self.assertEqual("FAIL", report.overall)

    def test_single_in_scope_writer_passes(self) -> None:
        before = digests(("src/calculator.py", "src/module_a.py"))
        after = dict(before)
        after["src/calculator.py"] = "fixed"
        report = ro.build_role_evidence(
            stream_for("implementer"),
            before,
            after,
            allowed_paths={"implementer": frozenset({"src/calculator.py"})},
        )
        by_role = {item.role: item for item in report.roles}
        self.assertEqual("PASS", by_role["implementer"].verdict)
        self.assertEqual(("src/calculator.py",), by_role["implementer"].changed_paths)

    def test_single_out_of_scope_writer_fails(self) -> None:
        before = digests(("src/calculator.py", "src/module_a.py"))
        after = dict(before)
        after["src/module_a.py"] = "touched"
        report = ro.build_role_evidence(
            stream_for("implementer"),
            before,
            after,
            allowed_paths={"implementer": frozenset({"src/calculator.py"})},
        )
        by_role = {item.role: item for item in report.roles}
        self.assertEqual("FAIL", by_role["implementer"].verdict)
        self.assertIn("WRITE_OUT_OF_SCOPE", by_role["implementer"].reason_codes)

    def test_commit_pusher_requires_authorization(self) -> None:
        report = ro.build_role_evidence(stream_for("commit_pusher"), digests(()), digests(()))
        by_role = {item.role: item for item in report.roles}
        self.assertEqual("BLOCKED", by_role["commit_pusher"].verdict)
        self.assertIn("COMMIT_NOT_AUTHORIZED", by_role["commit_pusher"].reason_codes)

    def test_missing_role_surfaces_parse_reason(self) -> None:
        report = ro.build_role_evidence(
            stream_for("code_explorer"), digests(()), digests(())
        )
        self.assertIn("MISSING_ATTRIBUTION", report.parse_reason_codes)
        unattributed = [item for item in report.roles if not item.attributed]
        self.assertEqual(8, len(unattributed))

    def test_summary_is_secret_free_and_complete(self) -> None:
        allowed_paths = {
            "implementer": frozenset(),
            "quick_implementer": frozenset(),
            "luna_escalation": frozenset(),
        }
        report = ro.build_role_evidence(
            stream_for(*EXPECTED_ROLES),
            digests(()),
            digests(()),
            allowed_paths=allowed_paths,
            authorized_commit=True,
        )
        result = ro.summary(report)
        self.assertEqual("PASS", result["overall"])
        self.assertEqual(9, len(result["roles"]))
        self.assertEqual(PARENT, result["parentSessionId"])


if __name__ == "__main__":
    unittest.main()
