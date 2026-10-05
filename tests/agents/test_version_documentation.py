"""Offline drift checks for explicitly marked current policy documentation.

Historical observations outside the markers are deliberately not version oracles.
The expected policy comes directly from JSON, without importing production loaders.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
GUIDES = (
    "README.md", "CODEX_WORKFLOW.md", "CODEX_WORKFLOW.ru.md",
    "WORK_FLOW.md", "WORK_FLOW.ru.md", "LIVE_VALIDATION.md",
)
# The installer ships the English contract; its source translation keeps the
# same portable reference contract without becoming a new installer payload.
CONTRACT_GUIDES = frozenset(("CODEX_WORKFLOW.md", "CODEX_WORKFLOW.ru.md"))
SUMMARY = "codex_version.md"
SUMMARY_TARGET = "codex_version.md#current-version-policy"
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
EXAMPLE_COUNTS = {"README.md": 1, "WORK_FLOW.md": 2, "WORK_FLOW.ru.md": 2}
SOURCE_CONTEXT = {
    "CODEX_WORKFLOW.md": "In the package source repository,",
    "CODEX_WORKFLOW.ru.md": "В исходном репозитории пакета",
}


def _blocks(text: str, marker: str, count: int, label: str) -> list[str]:
    begin = f"<!-- {marker}:begin -->"
    end = f"<!-- {marker}:end -->"
    tokens = list(re.finditer(re.escape(begin) + "|" + re.escape(end), text))
    if len(tokens) != count * 2:
        raise ValueError(f"{label}: missing, duplicate or unclosed {marker}")
    result = []
    for offset in range(0, len(tokens), 2):
        first, last = tokens[offset:offset + 2]
        if first.group() != begin or last.group() != end:
            raise ValueError(f"{label}: out-of-order {marker}")
        result.append(text[first.end():last.start()])
    return result


def documentation_errors(registry: dict, documents: dict[str, str]) -> list[str]:
    """Check maintained facts and references, leaving historical prose untouched."""
    errors = []
    try:
        summary = _blocks(documents[SUMMARY], "codex-current-policy", 1, SUMMARY)[0]
        target = registry["preferredInstallTarget"]
        entry = registry["versions"][target]
        legacy = registry["runProfiles"]["legacy-windows-capture"]["expectedVersion"]
        expected = {
            "preferredInstallTarget": target,
            **{f"preferred target: {gate}": json.dumps(entry["gates"][gate])
               for gate in ("staticInstallation", "discoveryDiagnostic", "capturedEvidence")},
            "preferred target: rolloutSchemas": json.dumps(entry["rolloutSchemas"]),
            "runtimeValidated (all registered versions)": "false",
            "legacy-windows-capture.expectedVersion": legacy,
            "legacy-windows-capture CLI banner": f"codex-cli {legacy}",
            "release readiness": "NOT_READY",
            "serving-backend/effective-policy provenance": "BLOCKED",
        }
        rows = {}
        for line in summary.splitlines():
            match = re.fullmatch(r"\| ([^|]+) \| `([^`]+)` \|", line)
            if match:
                key, value = match.groups()
                if key in rows:
                    errors.append(f"summary: duplicate field {key}")
                rows[key] = value
        if rows != expected:
            errors.append("summary: registry facts or readiness/provenance differ")
        if any(info["runtimeValidated"] is not False for info in registry["versions"].values()):
            errors.append("summary: not every registered runtimeValidated flag is false")
        if summary.count("| Registry field | Current policy |") != 1:
            errors.append("summary: expected one policy table")
        if "## Current version policy" not in summary:
            errors.append("summary: current-version-policy anchor missing")
        summary_links = LINK.findall(summary)
        for required in ("compatibility/codex-agents.json", "docs/validation/codex-0.160.0-windows.md"):
            if summary_links.count(required) != 1:
                errors.append(f"summary: missing or duplicated source/migration link {required}")

        for name in GUIDES:
            text = documents[name]
            reference = _blocks(text, "codex-policy-reference", 1, name)[0]
            if not all(word in reference for word in ("NOT_READY", "BLOCKED")):
                errors.append(f"{name}: readiness/provenance reference missing")
            if re.search(r"\b\d+\.\d+\.\d+\b", reference):
                errors.append(f"{name}: reference duplicates a mutable version")
            if name in CONTRACT_GUIDES:
                if SOURCE_CONTEXT[name] not in reference or SUMMARY_TARGET not in reference:
                    errors.append(f"{name}: package-source-only reference missing")
                if any(link.split("#")[0] in (SUMMARY, "compatibility/codex-agents.json")
                       for link in LINK.findall(text)):
                    errors.append(f"{name}: link would point to an uninstalled policy file")
            elif LINK.findall(reference) != [SUMMARY_TARGET]:
                errors.append(f"{name}: current summary link missing, duplicated or wrong")

            examples = _blocks(text, "codex-current-target-example", EXAMPLE_COUNTS.get(name, 0), name)
            for index, example in enumerate(examples):
                fences = re.findall(r"```(?:text|powershell)\s*\n(.*?)```", example, re.S)
                if len(fences) != 1:
                    errors.append(f"{name}: example must contain one complete command/banner fence")
                    continue
                body = fences[0]
                banner_expected = name != "README.md" and index == 0
                if banner_expected:
                    if body.strip() != f"codex-cli {target}":
                        errors.append(f"{name}: preferred target banner differs")
                else:
                    versions = re.findall(r"--codex-version\s+(\S+)", body)
                    if versions != [target] or "validate_agent_configs.py" not in body:
                        errors.append(f"{name}: static validator example differs")
    except (KeyError, TypeError, ValueError) as error:
        errors.append(str(error))
    return errors


class VersionDocumentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = json.loads((ROOT / "compatibility/codex-agents.json").read_text(encoding="utf-8"))
        self.documents = {name: (ROOT / name).read_text(encoding="utf-8") for name in (*GUIDES, SUMMARY)}

    def test_repository_marked_documentation_matches_raw_registry(self) -> None:
        self.assertEqual([], documentation_errors(self.registry, self.documents))
        self.assertEqual(5, sum(EXAMPLE_COUNTS.values()))

    def test_marked_local_links_and_summary_anchor_exist(self) -> None:
        for name in (*GUIDES, SUMMARY):
            marker = "codex-current-policy" if name == SUMMARY else "codex-policy-reference"
            block = _blocks(self.documents[name], marker, 1, name)[0]
            for link in LINK.findall(block):
                with self.subTest(document=name, link=link):
                    path, _, anchor = link.partition("#")
                    self.assertTrue((ROOT / path).is_file())
                    if anchor:
                        self.assertEqual("current-version-policy", anchor)
                        self.assertIn("## Current version policy", self.documents[path])

    def test_raw_policy_mutations_are_detected(self) -> None:
        mutations = [
            ("preferred target", lambda r: r.update(preferredInstallTarget="0.159.3")),
            ("legacy profile", lambda r: r["runProfiles"]["legacy-windows-capture"].update(expectedVersion="0.157.1")),
            ("rollout schemas", lambda r: r["versions"][r["preferredInstallTarget"]].update(rolloutSchemas=["v1"])),
            ("runtime", lambda r: r["versions"]["0.159.3"].update(runtimeValidated=True)),
        ]
        for gate in ("staticInstallation", "discoveryDiagnostic", "capturedEvidence"):
            mutations.append((gate, lambda r, gate=gate: r["versions"][r["preferredInstallTarget"]]["gates"].update({gate: not r["versions"][r["preferredInstallTarget"]]["gates"][gate]})))
        for label, mutate in mutations:
            with self.subTest(mutation=label):
                registry = copy.deepcopy(self.registry)
                mutate(registry)
                self.assertTrue(documentation_errors(registry, self.documents))

    def test_marked_example_drift_is_detected_in_both_languages(self) -> None:
        for name, count in EXAMPLE_COUNTS.items():
            blocks = _blocks(self.documents[name], "codex-current-target-example", count, name)
            for index, block in enumerate(blocks):
                with self.subTest(document=name, example=index):
                    documents = dict(self.documents)
                    documents[name] = documents[name].replace(block, block.replace("0.160.0", "0.159.3"), 1)
                    self.assertTrue(documentation_errors(self.registry, documents))

    def test_summary_fields_and_links_cannot_drift(self) -> None:
        for old, new in (
            ("`codex-cli 0.159.3`", "`codex-cli 0.160.0`"),
            ("| release readiness | `NOT_READY` |", "| release readiness | `READY` |"),
            ("| serving-backend/effective-policy provenance | `BLOCKED` |", "| serving-backend/effective-policy provenance | `PASS` |"),
            ("(compatibility/codex-agents.json)", "(missing-registry.json)"),
            ("(docs/validation/codex-0.160.0-windows.md)", "(missing-report.md)"),
            ("## Current version policy", "## Other policy"),
            ("| preferredInstallTarget | `0.160.0` |", "| preferredInstallTarget | `0.160.0` |\n| preferredInstallTarget | `0.160.0` |"),
        ):
            with self.subTest(replacement=old):
                documents = dict(self.documents)
                block = _blocks(documents[SUMMARY], "codex-current-policy", 1, SUMMARY)[0]
                self.assertIn(old, block)
                documents[SUMMARY] = documents[SUMMARY].replace(block, block.replace(old, new, 1), 1)
                self.assertTrue(documentation_errors(self.registry, documents))

    def test_markers_must_be_unique_complete_and_ordered(self) -> None:
        for name, marker in ((SUMMARY, "codex-current-policy"), ("README.md", "codex-policy-reference"), ("WORK_FLOW.ru.md", "codex-current-target-example")):
            begin, end = f"<!-- {marker}:begin -->", f"<!-- {marker}:end -->"
            for label, mutate in (
                ("missing", lambda text: text.replace(begin, "", 1)),
                ("duplicate", lambda text: text + "\n" + begin + "\n" + end),
                ("unclosed", lambda text: text.replace(end, "", 1)),
                ("out of order", lambda text: text.replace(begin, end, 1)),
            ):
                with self.subTest(document=name, mutation=label):
                    documents = dict(self.documents)
                    documents[name] = mutate(documents[name])
                    self.assertTrue(documentation_errors(self.registry, documents))

    def test_reference_link_and_contract_packaging_rules_detect_drift(self) -> None:
        for name in GUIDES:
            documents = dict(self.documents)
            block = _blocks(documents[name], "codex-policy-reference", 1, name)[0]
            if name in CONTRACT_GUIDES:
                changed = block.replace(SUMMARY_TARGET, f"[policy]({SUMMARY_TARGET})", 1)
            else:
                changed = block.replace(SUMMARY_TARGET, "missing.md#current-version-policy", 1)
            documents[name] = documents[name].replace(block, changed, 1)
            with self.subTest(document=name):
                self.assertTrue(documentation_errors(self.registry, documents))
        documents = dict(self.documents)
        documents["CODEX_WORKFLOW.md"] += "\n[registry](compatibility/codex-agents.json)\n"
        self.assertTrue(documentation_errors(self.registry, documents))
        for name in CONTRACT_GUIDES:
            with self.subTest(missing_source_context=name):
                documents = dict(self.documents)
                documents[name] = documents[name].replace(SOURCE_CONTEXT[name], "", 1)
                self.assertTrue(documentation_errors(self.registry, documents))

    def test_historical_versions_outside_markers_are_preserved_and_ignored(self) -> None:
        documents = dict(self.documents)
        for name in documents:
            documents[name] += "\nHistorical observation: codex-cli 0.155.1; --codex-version 0.159.0.\n"
        self.assertEqual([], documentation_errors(self.registry, documents))


if __name__ == "__main__":
    unittest.main()
