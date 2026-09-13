# Active Plan

**Schema:** `hybrid-codex/v1`  
**Plan ID:** `<id>`  
**Root owner:** `<session/agent>`  
**Updated:** `<ISO-8601 timestamp>`

Only the root orchestrator edits this file. Architects and workers propose updates in result packets.

## Objective

<Measurable outcome.>

## Scope

- <included work>

## Out of scope

- <excluded work>

## Fixed decisions

- <decision and reason>

## Compatibility assumptions

- <versions/schema assumptions and verification>

## Risk level

`Low | Medium | High`

Reason: <reason>

## Review requirement

`Required | Optional | Not required`

Reason: <reason>

## Execution and isolation strategy

- Mode: `serialized shared checkout | isolated worktrees`
- Base commit/status snapshot: <value>
- Parallelism justification: <value or none>
- Integration order: <ordered task IDs>
- Shared/generated-file risks: <value>

## Authorization boundaries

Allowed:

- <operation>

Prohibited:

- <operation>

Publishing mode: `none | commit-only | push-existing-commit | commit-and-push`

## Execution group

<Dependencies and ordering.>

## Task `<task-id>`: `<title>`

Status: `Not started | In progress | Blocked | Complete`  
Owner: `<role/agent>`  
Owned files: `<repository-relative paths>`  
Dependencies: `<task IDs or none>`  
Packet ID: `<id>`

### Context packet

<Minimal relevant context, symbols, and project-context sections.>

### Steps

1. <step>

### Acceptance criteria

- `<criterion-id>`: <testable requirement>

### Validation commands

- `<command>`

### Expected evidence

- `<criterion-id>`: <observable result>

### Authorization boundaries

Allowed:

- <task-specific operation>

Prohibited:

- <task-specific operation>

### Stop conditions

- <condition>

### Escalation conditions

- <evidence-based condition>

### Observed evidence

- `<criterion-id>`: <command/inspection, timestamp, exit code, sanitized outcome>

### Blocker

<blocker and remediation, or none>

### Resume point

<exactly where and how to continue>

## Integration evidence

<Ownership checks, integration sequence, conflicts, and post-integration checks.>

## Final completion gate

- [ ] Every task acceptance criterion has direct evidence.
- [ ] Independent validation passed when required.
- [ ] Review approved when required.
- [ ] No unexpected or unrelated changes are included.
- [ ] Requested publishing operation completed, or publishing mode is none.
- [ ] Handoff is current and secret-free.
