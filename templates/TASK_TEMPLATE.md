# Task Packet Template

Exchange task packets as UTF-8 YAML. All fields are required. Lists may be empty only when not applicable. Repository paths use `/` and are relative to the repository root.

## Context packet

```yaml
schema: hybrid-codex/v1
kind: task-context
packet_id: "<unique-id>"
task_id: "<task-id>"
objective: "<one bounded outcome>"
owned_files:
  - "path/to/file"
relevant_symbols:
  - "SymbolName"
project_context_sections:
  - "Commands"
fixed_decisions:
  - "<decision>"
constraints:
  - "<constraint>"
ordered_steps:
  - "<step>"
acceptance_criteria:
  - id: "AC-1"
    requirement: "<observable requirement>"
validation_commands:
  - "<command>"
expected_evidence:
  - "AC-1: <expected observation>"
authorization:
  allowed:
    - "<operation>"
  prohibited:
    - "<operation>"
stop_conditions:
  - "<condition>"
escalation_conditions:
  - "<trigger requiring evidence>"
```

## Worker result packet

```yaml
schema: hybrid-codex/v1
kind: worker-result
packet_id: "<context packet id>"
task_id: "<task-id>"
result_kind: "progress | complete | blocked"
proposed_status: "Not started | In progress | Blocked | Complete"
owned_files:
  - "path/to/file"
changed_files:
  - "path/to/file"
completed_steps:
  - "<step>"
commands:
  - command: "<exact command>"
    exit_code: 0
    outcome: "<sanitized observed result>"
evidence:
  - criterion_id: "AC-1"
    observation: "<direct evidence>"
remaining_work:
  - "<remaining work>"
exact_continuation: "<single precise resume instruction>"
blocker: null
assumptions:
  - "<assumption>"
risks:
  - "<risk>"
integration_notes:
  - "<base commit, generated-file, ordering, or compatibility note>"
proposed_state_changes:
  - "<change for root to apply to plan/handoff>"
```

## Validation rules

- `complete` requires `proposed_status: Complete`, no blocker, and evidence for every acceptance criterion.
- `blocked` requires `proposed_status: Blocked`, a non-null blocker, and an exact continuation.
- `changed_files` must be a subset of `owned_files`; otherwise report the ownership violation and stop.
- Commands record observed outcomes, not predictions.
- Remove or redact secrets before returning the packet.
- Unknown fields may be preserved, but missing required fields, invalid enums/types, duplicate IDs, or mismatched packet/task IDs make the packet malformed.
- The root requests one corrected packet after malformed output; a second malformed result blocks the task.
