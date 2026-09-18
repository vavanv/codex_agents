#!/usr/bin/env python3
"""Render an allowlisted report from a validated codex-live/v1 summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from live_validation_support import LiveValidationError, generate_report, sanitize_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = generate_report(arguments.summary, arguments.template, arguments.output)
    except (LiveValidationError, OSError) as error:
        print(f"REFUSED: {sanitize_text(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
