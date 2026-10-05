"""Offline L7 fixture readiness; never evidence of a live actor or escalation.

Use only within ``prepared_fixture()``. The pinned legacy test receives local
import bindings so its path insertion cannot change interpreter import state.
The two-file inventory is deliberately smaller than a live Git fixture.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import builtins
import hashlib
import io
from pathlib import Path
import stat
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest


SOURCE_PATH = "src/calculator.py"
TEST_PATH = "tests/test_calculator.py"
BASELINE_SOURCE = (
    "def add(left: int, right: int) -> int:\n"
    "    # Deliberately defective live-validation baseline.\n"
    "    return left - right\n"
).encode("utf-8")
REPAIRED_SOURCE = BASELINE_SOURCE.replace(b"return left - right", b"return left + right")
IMMUTABLE_TEST = (
    "from __future__ import annotations\n\n"
    "import sys\n"
    "import unittest\n"
    "from pathlib import Path\n\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n"
    "from calculator import add\n\n\n"
    "class CalculatorTests(unittest.TestCase):\n"
    "    def test_addition(self) -> None:\n"
    "        self.assertEqual(5, add(2, 3))\n\n\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
).encode("utf-8")
_OWNED_FIXTURE_TOKEN = object()


class FixtureRejected(ValueError):
    """The fixture no longer conforms to the pinned offline contract."""


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _ordinary(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise FixtureRejected("Symlink or reparse point in fixture path")


def _inventory(root: Path) -> dict[str, tuple[int, str | None]]:
    # Check ancestors too: a fixture must never traverse a reparse point.
    for ancestor in (root, *root.parents):
        _ordinary(ancestor)
    inventory: dict[str, tuple[int, str | None]] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in directory.iterdir():
            _ordinary(entry)
            relative = entry.relative_to(root).as_posix()
            metadata = entry.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                inventory[relative] = (metadata.st_mode, None)
                pending.append(entry)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise FixtureRejected("Hard-linked fixture file")
                inventory[relative] = (metadata.st_mode, _hash(entry.read_bytes()))
            else:
                raise FixtureRejected("Non-regular fixture entry")
    return inventory


@dataclass(frozen=True)
class OfflineReport:
    baseline_tests: int
    baseline_failures: int
    repaired_tests: int
    repaired_failures: int

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": "OFFLINE_VALIDATED",
            "actorSource": "offline-fixture",
            "l7Accepted": False,
            "runtimeValidated": False,
            "escalation": "NOT_EXERCISED",
            "baseline": {"tests": self.baseline_tests, "failures": self.baseline_failures, "errors": 0},
            "repaired": {"tests": self.repaired_tests, "failures": self.repaired_failures, "errors": 0},
            "changedFiles": [SOURCE_PATH],
            "hashes": {
                "baselineSource": _hash(BASELINE_SOURCE),
                "repairedSource": _hash(REPAIRED_SOURCE),
                "immutableTest": _hash(IMMUTABLE_TEST),
            },
        }


class OfflineFixture:
    """Created only by prepared_fixture; callers may inspect/alter its files."""

    def __init__(self, root: Path, *, _token: object = None):
        if _token is not _OWNED_FIXTURE_TOKEN:
            raise FixtureRejected("Only prepared_fixture may create owned fixtures")
        self._root = root
        self._initial = _inventory(root)
        if (set(self._initial) != {"src", "tests", SOURCE_PATH, TEST_PATH}
                or self._initial[SOURCE_PATH][1] != _hash(BASELINE_SOURCE)
                or self._initial[TEST_PATH][1] != _hash(IMMUTABLE_TEST)):
            raise FixtureRejected("Compiler inventory does not match pinned baseline")
        self._baseline_observed = False
        self._closed = False

    @property
    def root(self) -> Path:
        return self._root

    def _check(self, source: bytes) -> dict[str, tuple[int, str | None]]:
        if self._closed:
            raise FixtureRejected("Fixture context is closed")
        current = _inventory(self.root)
        expected = dict(self._initial)
        expected[SOURCE_PATH] = (expected[SOURCE_PATH][0], _hash(source))
        if current != expected:
            raise FixtureRejected("Fixture inventory or pinned bytes differ")
        return current

    def _execute(self, source: bytes) -> unittest.TestResult:
        before = self._check(source)
        calculator = ModuleType("calculator")
        calculator.__file__ = str(self.root / SOURCE_PATH)
        exec(compile(source, calculator.__file__, "exec"), calculator.__dict__)
        local_sys = SimpleNamespace(path=sys.path[:])

        def local_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 0 and name == "calculator":
                return calculator
            if level == 0 and name == "sys":
                return local_sys
            return builtins.__import__(name, globals, locals, fromlist, level)

        test_module = ModuleType("_l7_pinned_test")
        test_module.__file__ = str(self.root / TEST_PATH)
        test_module.__dict__["__builtins__"] = {**vars(builtins), "__import__": local_import}
        exec(compile(IMMUTABLE_TEST, test_module.__file__, "exec"), test_module.__dict__)
        suite = unittest.defaultTestLoader.loadTestsFromModule(test_module)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        if self._check(source) != before:
            raise FixtureRejected("Test execution changed fixture inventory")
        return result

    def observe_baseline(self) -> None:
        result = self._execute(BASELINE_SOURCE)
        if (result.testsRun != 1 or len(result.failures) != 1 or result.errors
                or result.skipped or result.expectedFailures or result.unexpectedSuccesses
                or "AssertionError: 5 != -1" not in result.failures[0][1]):
            raise FixtureRejected("Baseline did not produce the expected addition assertion")
        self._baseline_observed = True

    def apply_expected_repair(self) -> None:
        """Apply trusted bytes to demonstrate contract readiness, not actor behavior."""
        if not self._baseline_observed:
            raise FixtureRejected("Observe baseline before repair")
        self._check(BASELINE_SOURCE)
        (self.root / SOURCE_PATH).write_bytes(REPAIRED_SOURCE)

    def validate_repair(self) -> OfflineReport:
        if not self._baseline_observed:
            raise FixtureRejected("Recorded baseline observation is missing")
        result = self._execute(REPAIRED_SOURCE)
        if (result.testsRun != 1 or not result.wasSuccessful() or result.skipped
                or result.expectedFailures or result.unexpectedSuccesses):
            raise FixtureRejected("Repaired fixture did not pass the pinned test")
        return OfflineReport(1, 1, 1, 0)


@contextmanager
def prepared_fixture():
    """Compile pinned files in a newly allocated owned TemporaryDirectory only."""
    with tempfile.TemporaryDirectory(prefix="l7-offline-") as temporary:
        root = Path(temporary)
        # Never accept a supplied destination or reuse an existing fixture.
        for ancestor in (root, *root.parents):
            _ordinary(ancestor)
        for relative, content in ((SOURCE_PATH, BASELINE_SOURCE), (TEST_PATH, IMMUTABLE_TEST)):
            target = root / relative
            target.parent.mkdir(exist_ok=False)
            with target.open("xb") as output:
                output.write(content)
        fixture = OfflineFixture(root, _token=_OWNED_FIXTURE_TOKEN)
        try:
            yield fixture
        finally:
            fixture._closed = True
