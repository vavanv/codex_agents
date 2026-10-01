"""User-readable timeline formatter and CLI exporter.

Generates rich Markdown reports with Mermaid sequence diagrams, structured JSON,
or CSV exports from task timeline ledgers, with strict secret scrubbing,
model-observability badges, token summaries, and coverage warnings.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from timeline_schema import (
    TimelineEvent,
    TimelineValidationError,
    UsageMetrics,
)
from timeline_collector import TimelineCollector


class TimelineExporter:
    """Formats timeline events into user-facing Markdown, JSON, or CSV."""

    def __init__(self, events: Sequence[TimelineEvent], task_id: Optional[str] = None) -> None:
        self.events = list(events)
        self.task_id = task_id or (self.events[0].task_id if self.events else "unknown-task")

    @classmethod
    def from_ledger(cls, task_id: str, ledger_path: Optional[Path | str] = None) -> TimelineExporter:
        collector = TimelineCollector(task_id, ledger_path=ledger_path)
        events = collector.read_events()
        return cls(events, task_id=task_id)

    def calculate_total_usage(self) -> UsageMetrics:
        """Aggregate token counts and monetary costs across all completed turns."""
        total_inp = 0
        total_out = 0
        total_cac = 0
        total_cost = 0.0
        has_usage = False

        for ev in self.events:
            if ev.usage:
                has_usage = True
                if ev.usage.input_tokens:
                    total_inp += ev.usage.input_tokens
                if ev.usage.output_tokens:
                    total_out += ev.usage.output_tokens
                if ev.usage.cached_tokens:
                    total_cac += ev.usage.cached_tokens
                if ev.usage.monetary_cost_usd:
                    total_cost += ev.usage.monetary_cost_usd

        if not has_usage:
            return UsageMetrics()

        return UsageMetrics(
            input_tokens=total_inp,
            output_tokens=total_out,
            cached_tokens=total_cac,
            monetary_cost_usd=round(total_cost, 6),
        )

    def calculate_total_duration_ms(self) -> int:
        """Compute total elapsed execution duration in milliseconds."""
        return sum(ev.duration_ms or 0 for ev in self.events)

    def has_coverage_gaps(self) -> bool:
        """Check if any turn has unobserved models/efforts or aborted/incomplete statuses."""
        for ev in self.events:
            if ev.model_observed in {"unavailable", "unverified"} or ev.effort_observed in {
                "unavailable",
                "unverified",
            }:
                return True
            if ev.status in {"ABORTED", "TIMEOUT", "CANCELLED", "FAILURE"}:
                return True
        return False

    def to_markdown(self) -> str:
        """Generate a user-facing Markdown report with diagram, table, and metrics."""
        lines: list[str] = []
        lines.append(f"# Task Execution Timeline: `{self.task_id}`\n")

        total_ms = self.calculate_total_duration_ms()
        duration_s = round(total_ms / 1000.0, 2)
        usage = self.calculate_total_usage()

        # Metrics Summary Card
        lines.append("## Executive Metrics\n")
        lines.append(f"- **Total Duration:** {duration_s}s ({total_ms} ms)")
        lines.append(f"- **Total Turns Recorded:** {len(self.events)}")
        if usage.input_tokens is not None or usage.output_tokens is not None:
            inp = usage.input_tokens or 0
            out = usage.output_tokens or 0
            cac = usage.cached_tokens or 0
            cost_str = f" (${usage.monetary_cost_usd:.4f})" if usage.monetary_cost_usd else ""
            lines.append(
                f"- **Token Usage:** {inp + out:,} total ({inp:,} input, {out:,} output, {cac:,} cached){cost_str}"
            )
        else:
            lines.append("- **Token Usage:** *Unavailable from runtime stream*")
        lines.append("")

        # Coverage Warning Banner
        if self.has_coverage_gaps():
            lines.append("> [!WARNING]")
            lines.append(
                "> **Partial Observability Detected:** One or more execution turns had unobserved model/effort settings, failures, or were interrupted. Observed metrics reflect recorded data only.\n"
            )

        # Mermaid Sequence Diagram
        if self.events:
            lines.append("## Execution Sequence\n")
            lines.append("```mermaid")
            lines.append("sequenceDiagram")
            lines.append("    autonumber")
            lines.append("    actor User as User / Root")

            # Collect unique roles
            roles = list(dict.fromkeys(ev.agent_role for ev in self.events if ev.agent_role))
            for role in roles:
                lines.append(f"    participant {role} as {role}")

            for ev in self.events:
                caller = ev.parent_agent_role or "User"
                target = ev.agent_role
                sanitized_msg = ev.summary.replace('"', "'").replace("\n", " ")
                if len(sanitized_msg) > 60:
                    sanitized_msg = sanitized_msg[:57] + "..."
                lines.append(f'    {caller}->>{target}: [{ev.stage}] {sanitized_msg}')
                if ev.status != "STARTED":
                    status_flag = "✓" if ev.status == "SUCCESS" else "✗"
                    lines.append(f'    {target}-->>{caller}: {status_flag} {ev.status} ({ev.duration_ms or 0}ms)')

            lines.append("```\n")

        # Chronological Event Table
        lines.append("## Chronological Turn Log\n")
        lines.append(
            "| Turn | Stage | Agent Role | Configured Model | Observed Model | Duration | Status | Tokens (In / Out) | Summary |"
        )
        lines.append(
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        )

        for ev in self.events:
            dur_str = f"{ev.duration_ms}ms" if ev.duration_ms is not None else "-"
            cfg_model = f"`{ev.model_configured}` ({ev.effort_configured})" if ev.model_configured else "-"
            obs_model = (
                f"`{ev.model_observed}` ({ev.effort_observed})"
                if ev.model_observed and ev.model_observed != "unavailable"
                else "*unavailable*"
            )
            token_str = (
                f"{ev.usage.input_tokens or 0:,} / {ev.usage.output_tokens or 0:,}"
                if ev.usage
                else "-"
            )
            clean_summary = ev.summary.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {ev.turn_id} | `{ev.stage}` | `{ev.agent_role}` | {cfg_model} | {obs_model} | {dur_str} | **{ev.status}** | {token_str} | {clean_summary} |"
            )

        lines.append("")

        # Evidence Artifacts
        all_refs = [ref for ev in self.events for ref in ev.evidence_refs]
        if all_refs:
            lines.append("## Evidence Artifacts\n")
            for ref in dict.fromkeys(all_refs):
                lines.append(f"- [`{ref}`]({ref})")
            lines.append("")

        return "\n".join(lines)

    def to_json(self) -> str:
        """Generate structured JSON representation of task timeline."""
        usage = self.calculate_total_usage()
        payload = {
            "task_id": self.task_id,
            "total_duration_ms": self.calculate_total_duration_ms(),
            "has_coverage_gaps": self.has_coverage_gaps(),
            "total_usage": usage.to_dict(),
            "event_count": len(self.events),
            "events": [ev.to_dict() for ev in self.events],
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    def to_csv(self) -> str:
        """Generate RFC-4180 CSV export of timeline events."""
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([
            "task_id",
            "turn_id",
            "stage",
            "agent_role",
            "parent_agent_role",
            "status",
            "model_configured",
            "effort_configured",
            "model_observed",
            "effort_observed",
            "timestamp_start",
            "timestamp_end",
            "duration_ms",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "cost_usd",
            "summary",
        ])

        for ev in self.events:
            inp = ev.usage.input_tokens if ev.usage else ""
            out_tok = ev.usage.output_tokens if ev.usage else ""
            cac = ev.usage.cached_tokens if ev.usage else ""
            cost = ev.usage.monetary_cost_usd if ev.usage else ""
            writer.writerow([
                ev.task_id,
                ev.turn_id,
                ev.stage,
                ev.agent_role,
                ev.parent_agent_role or "",
                ev.status,
                ev.model_configured or "",
                ev.effort_configured or "",
                ev.model_observed or "",
                ev.effort_observed or "",
                ev.timestamp_start,
                ev.timestamp_end or "",
                ev.duration_ms if ev.duration_ms is not None else "",
                inp,
                out_tok,
                cac,
                cost,
                ev.summary,
            ])

        return out.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="Export task timeline to Markdown, JSON, or CSV.")
    parser.add_argument("--task-id", required=True, help="Task ID to export timeline for")
    parser.add_argument("--ledger", help="Optional explicit path to task ledger JSONL file")
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "csv"],
        default="markdown",
        help="Export format (default: markdown)",
    )
    parser.add_argument("--output", help="Optional output destination file path")

    args = parser.parse_args()

    try:
        exporter = TimelineExporter.from_ledger(args.task_id, ledger_path=args.ledger)
        if args.format == "markdown":
            result = exporter.to_markdown()
        elif args.format == "json":
            result = exporter.to_json()
        elif args.format == "csv":
            result = exporter.to_csv()
        else:
            print(f"Unsupported format: {args.format}", file=sys.stderr)
            return 1

        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(result)
            print(f"Exported {args.format} timeline to {args.output}")
        else:
            print(result)

        return 0
    except Exception as exc:
        print(f"Timeline export failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
