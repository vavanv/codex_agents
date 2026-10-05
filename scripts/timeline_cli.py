"""Record explicit workflow timeline entries and view sanitized task exports."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import re
import sys

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from timeline_collector import TimelineCollector
from timeline_exporter import TimelineExporter
from timeline_schema import ALLOWED_STAGES, ALLOWED_STATUSES, UsageMetrics


_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_AGENT_ROLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


def _task_id(value: str) -> str:
    if not _TASK_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("task ID must use 1-80 letters, digits, underscores, or hyphens")
    return value


def _agent_role(value: str) -> str:
    if not _AGENT_ROLE.fullmatch(value):
        raise argparse.ArgumentTypeError("agent role must use 1-80 letters, digits, dots, underscores, or hyphens")
    return value


def _finite_cost(value: str) -> float:
    cost = float(value)
    if not math.isfinite(cost):
        raise argparse.ArgumentTypeError("cost must be a finite number")
    return cost


def _project_root(value: str) -> Path:
    root = Path(value).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project path must be an existing directory")
    return root


def _ledger_path(root: Path, task_id: str) -> Path:
    path = root / ".codex-workflow-data" / "timeline" / f"{task_id}.jsonl"
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("timeline path resolves outside the project") from exc
    if resolved != path.absolute():
        raise ValueError("timeline directory or ledger must not traverse a link")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record and inspect an explicit project execution timeline.")
    parser.add_argument("--project", default=".", help="project root (default: current directory)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="append one completed stage or turn")
    record.add_argument("--task-id", required=True, type=_task_id)
    record.add_argument("--stage", required=True, choices=sorted(ALLOWED_STAGES))
    record.add_argument("--agent-role", required=True, type=_agent_role)
    record.add_argument("--status", required=True, choices=sorted(ALLOWED_STATUSES - {"STARTED", "RUNNING"}))
    record.add_argument("--summary", required=True, help="short sanitized outcome; never paste prompts or reasoning")
    record.add_argument("--turn-id", type=int, help="set only when correlating a known turn")
    record.add_argument("--prompt-id")
    record.add_argument("--parent-agent-role", type=_agent_role)
    record.add_argument("--session-id")
    record.add_argument("--child-session-id")
    record.add_argument("--model-configured")
    record.add_argument("--effort-configured")
    record.add_argument("--model-observed", help="only when directly observed; otherwise left unavailable")
    record.add_argument("--effort-observed", help="only when directly observed; otherwise left unavailable")
    record.add_argument("--duration-ms", type=int)
    record.add_argument("--input-tokens", type=int)
    record.add_argument("--output-tokens", type=int)
    record.add_argument("--cached-tokens", type=int)
    record.add_argument("--cost-usd", type=_finite_cost)
    record.add_argument("--retry-count", type=int, default=0)
    record.add_argument("--handoff-to", type=_agent_role)

    start = subparsers.add_parser("start", help="begin tracking one explicit workflow turn")
    start.add_argument("--task-id", required=True, type=_task_id)
    start.add_argument("--stage", required=True, choices=sorted(ALLOWED_STAGES))
    start.add_argument("--agent-role", required=True, type=_agent_role)
    start.add_argument("--summary", required=True, help="short description; never paste prompts or reasoning")
    start.add_argument("--prompt-id")
    start.add_argument("--parent-agent-role", type=_agent_role)
    start.add_argument("--session-id")
    start.add_argument("--model-configured")
    start.add_argument("--effort-configured")

    finish = subparsers.add_parser("finish", help="finish a previously started turn")
    finish.add_argument("--task-id", required=True, type=_task_id)
    finish.add_argument("--turn-id", required=True, type=int)
    finish.add_argument("--status", required=True, choices=sorted(ALLOWED_STATUSES - {"STARTED", "RUNNING"}))
    finish.add_argument("--summary", required=True, help="short sanitized outcome; never paste prompts or reasoning")
    finish.add_argument("--child-session-id")
    finish.add_argument("--model-observed", help="only when directly observed; otherwise left unavailable")
    finish.add_argument("--effort-observed", help="only when directly observed; otherwise left unavailable")
    finish.add_argument("--input-tokens", type=int)
    finish.add_argument("--output-tokens", type=int)
    finish.add_argument("--cached-tokens", type=int)
    finish.add_argument("--cost-usd", type=_finite_cost)
    finish.add_argument("--retry-count", type=int, default=0)
    finish.add_argument("--handoff-to", type=_agent_role)

    show = subparsers.add_parser("show", help="print or export the task timeline")
    show.add_argument("--task-id", required=True, type=_task_id)
    show.add_argument("--format", choices=("markdown", "json", "csv"), default="markdown")
    show.add_argument("--output", help="optional output file")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        root = _project_root(args.project)
        ledger = _ledger_path(root, args.task_id)
        collector = TimelineCollector(args.task_id, ledger_path=ledger)
        if args.command == "start":
            event = collector.start_turn(
                stage=args.stage,
                agent_role=args.agent_role,
                summary=args.summary,
                prompt_id=args.prompt_id,
                parent_agent_role=args.parent_agent_role,
                session_id=args.session_id,
                model_configured=args.model_configured,
                effort_configured=args.effort_configured,
            )
            print(f"Started {event.stage} turn {event.turn_id} for task {event.task_id}.")
            return 0

        if args.command == "finish":
            last_state: dict[int, str] = {}
            for recorded in collector.read_events():
                last_state[recorded.turn_id] = recorded.status
            if last_state.get(args.turn_id) not in {"STARTED", "RUNNING"}:
                raise ValueError(f"turn {args.turn_id} is not open for task {args.task_id}")
            usage_values = {
                "input_tokens": args.input_tokens,
                "output_tokens": args.output_tokens,
                "cached_tokens": args.cached_tokens,
                "monetary_cost_usd": args.cost_usd,
            }
            usage = UsageMetrics.from_dict(usage_values) if any(value is not None for value in usage_values.values()) else None
            event = collector.complete_turn(
                turn_id=args.turn_id,
                status=args.status,
                summary=args.summary,
                child_session_id=args.child_session_id,
                model_observed=args.model_observed,
                effort_observed=args.effort_observed,
                usage=usage,
                retry_count=args.retry_count,
                handoff_to=args.handoff_to,
            )
            print(f"Finished {event.stage} turn {event.turn_id} ({event.status}) for task {event.task_id}.")
            return 0

        if args.command == "record":
            if args.turn_id is not None and any(event.turn_id == args.turn_id for event in collector.read_events()):
                raise ValueError(f"turn {args.turn_id} already exists for task {args.task_id}")
            usage_values = {
                "input_tokens": args.input_tokens,
                "output_tokens": args.output_tokens,
                "cached_tokens": args.cached_tokens,
                "monetary_cost_usd": args.cost_usd,
            }
            usage = UsageMetrics.from_dict(usage_values) if any(value is not None for value in usage_values.values()) else None
            event = collector.record_event(
                stage=args.stage,
                agent_role=args.agent_role,
                status=args.status,
                summary=args.summary,
                turn_id=args.turn_id,
                prompt_id=args.prompt_id,
                parent_agent_role=args.parent_agent_role,
                session_id=args.session_id,
                child_session_id=args.child_session_id,
                model_configured=args.model_configured,
                effort_configured=args.effort_configured,
                model_observed=args.model_observed,
                effort_observed=args.effort_observed,
                duration_ms=args.duration_ms,
                usage=usage,
                retry_count=args.retry_count,
                handoff_to=args.handoff_to,
            )
            print(f"Recorded {event.stage} turn {event.turn_id} ({event.status}) for task {event.task_id}.")
            return 0

        exporter = TimelineExporter.from_ledger(args.task_id, ledger_path=ledger)
        rendered = {
            "markdown": exporter.to_markdown,
            "json": exporter.to_json,
            "csv": exporter.to_csv,
        }[args.format]()
        if args.output:
            destination = Path(args.output).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered, encoding="utf-8", newline="")
            print(f"Exported {args.format} timeline to {destination}")
        else:
            print(rendered)
        return 0
    except (OSError, ValueError) as exc:
        print(f"Timeline command failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Windows redirected streams otherwise use a legacy code page that cannot
    # encode timeline symbols or user summaries. CLI exports are UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
