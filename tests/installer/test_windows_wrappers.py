from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "nt", "native PowerShell wrapper coverage requires Windows")
class WindowsWrapperTests(unittest.TestCase):
    def run_wrapper(
        self,
        wrapper: str,
        switches: list[str],
        fake_exit_code: int = 0,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
        powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory(prefix="workflow wrapper test ") as directory:
            root = Path(directory)
            fake_bin = root / "fake bin"
            fake_bin.mkdir()
            capture_path = root / "captured.json"
            target = root / "target repository with spaces"
            target.mkdir()
            fake_python = fake_bin / "python.cmd"
            fake_python.write_text(
                "@echo off\r\n"
                "> \"%FAKE_CAPTURE%\" echo %~1\r\n"
                ">> \"%FAKE_CAPTURE%\" echo %~2\r\n"
                ">> \"%FAKE_CAPTURE%\" echo %~3\r\n"
                ">> \"%FAKE_CAPTURE%\" echo %~4\r\n"
                ">> \"%FAKE_CAPTURE%\" echo %~5\r\n"
                ">> \"%FAKE_CAPTURE%\" echo %~6\r\n"
                "exit /b %FAKE_EXIT_CODE%\r\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
            environment["FAKE_CAPTURE"] = str(capture_path)
            environment["FAKE_EXIT_CODE"] = str(fake_exit_code)
            command = [
                powershell,
                "-NoProfile",
                "-File",
                str(REPOSITORY_ROOT / wrapper),
                "-TargetRepository",
                str(target),
                *switches,
            ]
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            if not capture_path.exists():
                self.fail(
                    f"fake Python was not invoked; stdout={result.stdout!r}; stderr={result.stderr!r}"
                )
            captured = capture_path.read_text(encoding="utf-8").splitlines()
            return result, captured, str(target)

    def test_install_forwards_target_whatif_and_recover(self) -> None:
        result, captured, target = self.run_wrapper(
            "install.ps1",
            ["-WhatIf", "-Recover"],
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("install", captured[1])
        self.assertEqual(["--target", target, "--dry-run", "--recover"], captured[2:])

    def test_uninstall_forwards_arguments_and_propagates_exit(self) -> None:
        result, captured, target = self.run_wrapper(
            "uninstall.ps1",
            ["-WhatIf", "-Recover"],
            fake_exit_code=23,
        )

        self.assertEqual(23, result.returncode)
        self.assertEqual("uninstall", captured[1])
        self.assertEqual(["--target", target, "--dry-run", "--recover"], captured[2:])

    def test_native_wrappers_install_and_uninstall_repository_with_spaces(self) -> None:
        powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory(prefix="workflow native lifecycle ") as directory:
            root = Path(directory)
            fake_bin = root / "fake codex bin"
            fake_bin.mkdir()
            (fake_bin / "codex.cmd").write_text(
                "@echo off\r\necho codex-cli 0.155.1\r\n",
                encoding="utf-8",
            )
            target = root / "isolated repository with spaces"
            target.mkdir()
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")

            install = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(REPOSITORY_ROOT / "install.ps1"),
                    "-TargetRepository",
                    str(target),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(0, install.returncode, install.stderr)
            self.assertTrue((target / ".hybrid-codex-workflow-state.json").is_file())

            uninstall = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-File",
                    str(REPOSITORY_ROOT / "uninstall.ps1"),
                    "-TargetRepository",
                    str(target),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(0, uninstall.returncode, uninstall.stderr)
            self.assertEqual([], list(target.iterdir()))


if __name__ == "__main__":
    unittest.main()
