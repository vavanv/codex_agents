from __future__ import annotations

import json
import sys
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import workflow_manager as manager


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("fresh-process release barrier timed out")
        time.sleep(0.01)


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        return 2
    target = Path(argv[1])
    trigger = argv[2]
    ready = Path(argv[3])
    release = Path(argv[4])
    result = Path(argv[5])
    adapter: manager.RepositoryAdapter | None = None
    signaled = False
    outcome: dict[str, object]
    try:
        adapter = manager._repository_adapter_from_raw_target(str(target))
        if not isinstance(adapter, manager.WindowsRepositoryAdapter):
            raise RuntimeError("fresh-process helper did not acquire the Windows adapter")
        original_identity = adapter._filesystem.directory_identity

        def signal_once(path: str) -> None:
            nonlocal signaled
            if adapter.normalize(path) == trigger and not signaled:
                signaled = True
                ready.write_text("ready\n", encoding="utf-8")
                _wait_for(release)

        def barrier_identity(path: str) -> object:
            try:
                identity = original_identity(path)
            except OSError as error:
                if getattr(error, "winerror", None) in {2, 3}:
                    signal_once(path)
                raise
            signal_once(path)
            return identity

        with patch.object(
            adapter._filesystem,
            "directory_identity",
            side_effect=barrier_identity,
        ):
            with redirect_stdout(StringIO()):
                manager.recover(
                    target,
                    target / manager.JOURNAL_FILENAME,
                    False,
                    adapter=adapter,
                )
        if not signaled:
            raise RuntimeError("fresh-process identity barrier was not reached")
        outcome = {"ok": True}
        exit_code = 0
    except BaseException as error:
        outcome = {
            "ok": False,
            "errorType": type(error).__name__,
            "error": str(error),
        }
        exit_code = 1
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except BaseException as close_error:
                outcome = {
                    "ok": False,
                    "errorType": type(close_error).__name__,
                    "error": "fresh-process adapter close failed",
                }
                exit_code = 1
        result.write_text(
            json.dumps(outcome, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
