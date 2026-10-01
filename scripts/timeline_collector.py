"""Timeline execution and token usage collector.

Instruments workflow execution, turns, nested agent delegations, usage metrics,
and crash recovery, safely writing to an append-only JSONL ledger.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence
import uuid

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from timeline_schema import (
    TIMELINE_SCHEMA_VERSION,
    TimelineEvent,
    TimelineValidationError,
    UsageMetrics,
    RetentionPolicy,
    parse_timeline_event,
    serialize_timeline_event,
    validate_timeline_event_dict,
    validate_timeline_sequence,
)
from codex_event_adapter import sanitize_text


def _utc_now_iso() -> str:
    """Return current UTC time in canonical ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TimelineCollector:
    """Manages recording, token accounting, and retention for task timeline events."""

    def __init__(
        self,
        task_id: str,
        ledger_path: Optional[Path | str] = None,
        retention_policy: Optional[RetentionPolicy] = None,
    ) -> None:
        if not task_id or not isinstance(task_id, str) or not task_id.strip():
            raise TimelineValidationError("task_id must be a non-empty string")

        self.task_id = task_id.strip()
        self.retention_policy = retention_policy or RetentionPolicy()

        if ledger_path is None:
            base_dir = Path(".codex-workflow") / "timeline"
            self.ledger_path = base_dir / f"{self.task_id}.jsonl"
        else:
            self.ledger_path = Path(ledger_path)

        self._active_turns: Dict[int, Dict[str, Any]] = {}

    def _ensure_ledger_dir(self) -> None:
        if not self.ledger_path.parent.exists():
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def read_events(self) -> List[TimelineEvent]:
        """Read and validate all events currently in the task ledger."""
        if not self.ledger_path.exists():
            return []

        events: List[TimelineEvent] = []
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    events.append(parse_timeline_event(line_str))
                except Exception as exc:
                    raise TimelineValidationError(
                        f"Corrupted ledger entry in {self.ledger_path}: {exc}"
                    ) from exc

        valid, errors = validate_timeline_sequence(events)
        if not valid:
            raise TimelineValidationError(
                f"Ledger sequence validation failed for {self.ledger_path}: {errors}"
            )

        return events

    def _append_event(self, event: TimelineEvent) -> None:
        """Atomically append an event to the ledger and apply retention if needed."""
        self._ensure_ledger_dir()
        serialized = serialize_timeline_event(event)
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(serialized + "\n")

    def prune_ledger(self) -> int:
        """Apply retention policy to prune old/excess events. Returns count pruned."""
        if not self.ledger_path.exists():
            return 0

        events = self.read_events()
        kept = self.retention_policy.prune_events(events)
        pruned_count = len(events) - len(kept)

        if pruned_count > 0:
            temp_path = self.ledger_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                for ev in kept:
                    f.write(serialize_timeline_event(ev) + "\n")
            # Atomic replacement
            os.replace(temp_path, self.ledger_path)

        return pruned_count

    def get_next_turn_id(self) -> int:
        """Compute the next sequential turn ID for this task."""
        events = self.read_events()
        if not events:
            return 0
        return max(e.turn_id for e in events) + 1

    def record_event(
        self,
        stage: str,
        agent_role: str,
        status: str,
        summary: str,
        *,
        turn_id: Optional[int] = None,
        prompt_id: Optional[str] = None,
        parent_agent_role: Optional[str] = None,
        session_id: Optional[str] = None,
        child_session_id: Optional[str] = None,
        model_configured: Optional[str] = None,
        effort_configured: Optional[str] = None,
        model_observed: Optional[str] = None,
        effort_observed: Optional[str] = None,
        timestamp_start: Optional[str] = None,
        timestamp_end: Optional[str] = None,
        duration_ms: Optional[int] = None,
        usage: Optional[UsageMetrics] = None,
        evidence_refs: Sequence[str] = (),
        retry_count: int = 0,
        handoff_to: Optional[str] = None,
    ) -> TimelineEvent:
        """Construct, sanitize, validate, and append a discrete timeline event."""
        assigned_turn = self.get_next_turn_id() if turn_id is None else turn_id
        start_ts = timestamp_start or _utc_now_iso()
        sanitized_summary = sanitize_text(summary, limit=500)

        # Enforce observed model/effort fallbacks: never substitute configured as observed
        obs_model = model_observed if model_observed is not None else "unavailable"
        obs_effort = effort_observed if effort_observed is not None else "unavailable"

        event_dict: Dict[str, Any] = {
            "schema_version": TIMELINE_SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "task_id": self.task_id,
            "turn_id": assigned_turn,
            "prompt_id": prompt_id,
            "stage": stage,
            "agent_role": agent_role,
            "parent_agent_role": parent_agent_role,
            "session_id": session_id,
            "child_session_id": child_session_id,
            "model_configured": model_configured,
            "effort_configured": effort_configured,
            "model_observed": obs_model,
            "effort_observed": obs_effort,
            "timestamp_start": start_ts,
            "timestamp_end": timestamp_end,
            "duration_ms": duration_ms,
            "status": status,
            "usage": usage.to_dict() if usage else None,
            "summary": sanitized_summary,
            "evidence_refs": list(evidence_refs),
            "retry_count": retry_count,
            "handoff_to": handoff_to,
        }

        event = validate_timeline_event_dict(event_dict)
        self._append_event(event)
        return event

    def start_turn(
        self,
        stage: str,
        agent_role: str,
        summary: str,
        *,
        prompt_id: Optional[str] = None,
        parent_agent_role: Optional[str] = None,
        session_id: Optional[str] = None,
        model_configured: Optional[str] = None,
        effort_configured: Optional[str] = None,
    ) -> TimelineEvent:
        """Start a new turn, recording its start time and in-progress status."""
        turn_id = self.get_next_turn_id()
        start_ts = _utc_now_iso()

        self._active_turns[turn_id] = {
            "stage": stage,
            "agent_role": agent_role,
            "start_ts": start_ts,
            "prompt_id": prompt_id,
            "parent_agent_role": parent_agent_role,
            "session_id": session_id,
            "model_configured": model_configured,
            "effort_configured": effort_configured,
        }

        return self.record_event(
            stage=stage,
            agent_role=agent_role,
            status="STARTED",
            summary=summary,
            turn_id=turn_id,
            prompt_id=prompt_id,
            parent_agent_role=parent_agent_role,
            session_id=session_id,
            model_configured=model_configured,
            effort_configured=effort_configured,
            timestamp_start=start_ts,
        )

    def complete_turn(
        self,
        turn_id: int,
        status: str,
        summary: str,
        *,
        child_session_id: Optional[str] = None,
        model_observed: Optional[str] = None,
        effort_observed: Optional[str] = None,
        usage: Optional[UsageMetrics] = None,
        evidence_refs: Sequence[str] = (),
        retry_count: int = 0,
        handoff_to: Optional[str] = None,
    ) -> TimelineEvent:
        """Complete an active turn, computing duration and attaching usage metrics."""
        turn_info = self._active_turns.pop(turn_id, None)
        end_ts = _utc_now_iso()

        if turn_info:
            stage = turn_info["stage"]
            agent_role = turn_info["agent_role"]
            start_ts = turn_info["start_ts"]
            prompt_id = turn_info["prompt_id"]
            parent_agent_role = turn_info["parent_agent_role"]
            session_id = turn_info["session_id"]
            model_configured = turn_info["model_configured"]
            effort_configured = turn_info["effort_configured"]

            start_dt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
            duration_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000))
        else:
            # Fallback if completing an untracked turn directly
            stage = "UNKNOWN"
            agent_role = "unknown"
            start_ts = end_ts
            prompt_id = None
            parent_agent_role = None
            session_id = None
            model_configured = None
            effort_configured = None
            duration_ms = 0

        return self.record_event(
            stage=stage,
            agent_role=agent_role,
            status=status,
            summary=summary,
            turn_id=turn_id,
            prompt_id=prompt_id,
            parent_agent_role=parent_agent_role,
            session_id=session_id,
            child_session_id=child_session_id,
            model_configured=model_configured,
            effort_configured=effort_configured,
            model_observed=model_observed,
            effort_observed=effort_observed,
            timestamp_start=start_ts,
            timestamp_end=end_ts,
            duration_ms=duration_ms,
            usage=usage,
            evidence_refs=evidence_refs,
            retry_count=retry_count,
            handoff_to=handoff_to,
        )

    def recover_interrupted_turns(self) -> List[TimelineEvent]:
        """Detect and close any dangling STARTED/RUNNING turns left by a crash or interruption."""
        events = self.read_events()
        if not events:
            return []

        # Find turns whose last state is STARTED or RUNNING
        turn_states: Dict[int, TimelineEvent] = {}
        for ev in events:
            turn_states[ev.turn_id] = ev

        recovered: List[TimelineEvent] = []
        now_ts = _utc_now_iso()

        for turn_id, last_ev in sorted(turn_states.items()):
            if last_ev.status in {"STARTED", "RUNNING"}:
                start_dt = datetime.fromisoformat(last_ev.timestamp_start.replace("Z", "+00:00"))
                now_dt = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
                duration_ms = max(0, int((now_dt - start_dt).total_seconds() * 1000))

                aborted_ev = self.record_event(
                    stage=last_ev.stage,
                    agent_role=last_ev.agent_role,
                    status="ABORTED",
                    summary="Interrupted execution recovered upon restart",
                    turn_id=last_ev.turn_id,
                    prompt_id=last_ev.prompt_id,
                    parent_agent_role=last_ev.parent_agent_role,
                    session_id=last_ev.session_id,
                    model_configured=last_ev.model_configured,
                    effort_configured=last_ev.effort_configured,
                    timestamp_start=last_ev.timestamp_start,
                    timestamp_end=now_ts,
                    duration_ms=duration_ms,
                    retry_count=last_ev.retry_count,
                )
                recovered.append(aborted_ev)

        return recovered
