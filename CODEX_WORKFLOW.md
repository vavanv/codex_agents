# Codex Workflow Contract

**Contract version:** `1.0.0-draft`  
**Milestone:** Workflow contracts and project custom-agent preview  
**Status:** Contracts and opt-in TOML installation implemented

## 1. Purpose and invariants

This contract defines a risk-based multi-agent development workflow. It has five separate phases:

```text
planning != implementation != validation != review != publishing
```

The invariants are:

1. The root orchestrator owns scope, routing, state, integration, evidence acceptance, and final completion.
2. Workers own bounded tasks and files, never the global outcome.
3. Completion requires observed evidence.
4. Shared-checkout writes are serialized.
5. Parallel writers are isolated and integrated by the root.
6. Worker delegation depth is one; workers do not spawn workers.
7. Publishing authority is never inferred.
8. Plans, packets, and logs contain no secrets.

## 2. Compatibility and precedence

### 2.1 Validated compatibility matrix

| Workflow release | Codex CLI | Validation date | Status |
| --- | --- | --- | --- |
| v0.1 contracts | `0.154.0` | 2026-09-13 | Contracts-only installation; supported baseline |
| v0.2 agents preview | `0.154.0` | 2026-09-13 | Static TOML and lifecycle validation; live behavior pending |

Only listed versions are supported. The installer parses `codex --version`, matches an exact supported entry, and validates the source TOML catalog against `compatibility/codex-agents.json` before an opt-in agent installation. An unsupported or unparseable version is a no-write failure. Adding support requires tests for configuration parsing, instruction discovery, sandbox behavior, subagent spawning, and model availability.

The v0.1 schema uses:

```toml
[agents]
enabled = true
max_concurrent_threads_per_session = 2
```

The primary agent is excluded from that limit. `agents.max_threads` must not be emitted because it is a legacy alias. This package does not write a project `.codex/config.toml`; standalone `.codex/agents/*.toml` files are installed only when explicitly requested.

### 2.2 Configuration precedence

From highest to lowest: command-line/`--config` overrides, trusted project `.codex/config.toml` layers from project root toward the current directory, the selected user profile, user `~/.codex/config.toml`, cloud-managed defaults, system configuration, and built-in defaults. Organization requirements can constrain allowed values and cannot be weakened by lower layers.

The installer must never resolve a conflict by overwriting an unknown value. It must report the effective source and either merge a known managed key safely or stop.

### 2.3 Instruction precedence

Codex loads global guidance first, then one instruction file per directory from project root to the current directory; closer files win when instructions conflict. `AGENTS.override.md` takes precedence over `AGENTS.md` at the same level. Project instructions may specialize commands and architecture, but cannot grant publishing authority or weaken enforced security policy.

## 3. Roles

### Root orchestrator

Classifies work; chooses roles; defines task boundaries; validates architect proposals; exclusively writes plan/handoff state; integrates worker changes; accepts evidence; routes failures; and reports the final result. It may implement a tiny local change, but should use a bounded implementer when that improves clarity or isolation.

### Code explorer

Read-only discovery of modules, contracts, callers, tests, conventions, and risks. It reports concise findings and does not edit, install, generate, migrate, or run commands with persistent side effects.

### Quick implementer

Handles small, mechanical, well-understood changes, normally limited to one or two files. It performs a focused sanity check and returns a worker result packet.

### Implementer

Executes one bounded plan task within fixed decisions and owned files. It may add or update tests in its ownership. It does not redesign architecture, edit execution state, touch another worker's files, delegate, commit, or push.

### Luna escalation

Handles a bounded implementation problem only after concrete evidence shows repeated local failure or genuinely difficult algorithmic, concurrency, or type behavior inside a fixed architecture.

### Architect and deep architect

The architect proposes objectives, decisions, task boundaries, ownership, validation, risks, and plan/handoff changes. It never directly edits execution-state files. The deep architect is used only for a recorded consequential architecture blocker. Neither implements production code during planning.

### Code validator

Independently runs the approved validation commands in a read-only or disposable environment. It reports PASS, FAIL, or BLOCKED with direct evidence. It does not repair failures.

### Code reviewer

Performs read-only integration review of the plan, diff, evidence, public contracts, and risks. Its verdict is `APPROVE`, `REQUEST_CHANGES`, or `COMMENT`; findings include severity, location, impact, and a recommended fix.

### Commit/push operator

Performs only the explicitly authorized Git mode: commit-only, push-existing-commit, or commit-and-push. It inspects status and diffs, stages intentionally, never uses blanket staging without review, never bypasses hooks, and never force-pushes by default.

The catalog contains nine specialist agent configurations available through the opt-in installer flag: explorer, quick implementer, implementer, Luna escalation, architect, deep architect, validator, reviewer, and commit/push operator. The root orchestrator is the coordinating runtime role and is not counted among those nine configurations. TOML defaults are subject to parent-session permission and spawn overrides; they are not an immutable security boundary.

## 4. Classification and routing

### Class A: trivial

Use for a mechanical, understood change, normally one or two files, with no architecture, public-contract, persistence, security, or rollback concern.

```text
root -> quick implementer -> focused check -> complete
```

### Class B: normal

Use for ordinary multi-file features and bug fixes.

```text
root -> explorer if needed -> architect proposal -> root applies plan
     -> implementer -> validator -> root completes
```

### Class C: complex or high risk

Use for security, authentication, authorization, payments, migrations, concurrency, public APIs, cross-system changes, broad refactors, or multi-worker work.

```text
root -> explorer(s) -> architect proposal -> root applies plan
     -> implementer(s) -> validator -> reviewer -> root completes
```

Planning is required when any of these apply: more than two or three likely files, architecture decisions, public API or schema changes, multiple modules/services, security sensitivity, non-trivial test design, parallel implementation, or meaningful rollback.

## 5. State ownership

The root is the single authoritative writer of `plans/ACTIVE_PLAN.md`, `plans/HANDOFF.md`, and any execution ledger. Architects and workers return proposed state changes in their result packets. The root checks proposal consistency, updates files, and records why a proposal was rejected or modified.

Task statuses are exactly `Not started`, `In progress`, `Blocked`, and `Complete`. A task moves to `Complete` only after every acceptance criterion has direct evidence. A blocked task records the blocker, attempted checks, required remediation, and one safe resume point.

## 6. Context and result packets

Packets use UTF-8 YAML with schema version `hybrid-codex/v1`. Unknown fields are allowed for forward compatibility; missing required fields, unknown enum values, duplicate task IDs, or invalid types make a packet malformed. The root rejects malformed packets and requests one corrected response. If correction also fails, the task becomes `Blocked` with the malformed output retained only after redaction.

### 6.1 Context packet schema

```yaml
schema: hybrid-codex/v1
kind: task-context
packet_id: string
task_id: string
objective: string
owned_files: [string]
relevant_symbols: [string]
project_context_sections: [string]
fixed_decisions: [string]
constraints: [string]
ordered_steps: [string]
acceptance_criteria:
  - id: string
    requirement: string
validation_commands: [string]
expected_evidence: [string]
authorization:
  allowed: [string]
  prohibited: [string]
stop_conditions: [string]
escalation_conditions: [string]
```

All fields are required; lists may be empty only when the field genuinely does not apply. Paths must be repository-relative and normalized with `/`.

### 6.2 Worker result schema

```yaml
schema: hybrid-codex/v1
kind: worker-result
packet_id: string
task_id: string
result_kind: progress | complete | blocked
proposed_status: Not started | In progress | Blocked | Complete
owned_files: [string]
changed_files: [string]
completed_steps: [string]
commands:
  - command: string
    exit_code: integer | null
    outcome: string
evidence:
  - criterion_id: string
    observation: string
remaining_work: [string]
exact_continuation: string
blocker: string | null
assumptions: [string]
risks: [string]
integration_notes: [string]
proposed_state_changes: [string]
```

A `complete` result must propose `Complete`, have no blocker, and supply evidence for every criterion. A `blocked` result must propose `Blocked`, identify a blocker, and give an exact continuation. Changed files must be a subset of owned files unless the packet reports an ownership violation; an ownership violation is never auto-accepted.

## 7. Worktree and parallel integration strategy

The default is one implementation worker at a time in the user's checkout. Read-only exploration may run concurrently if its commands satisfy the read-only policy.

Parallel implementation is allowed only when:

1. tasks and dependencies are independent;
2. owned files, tests, fixtures, generated outputs, lockfiles, and shared configuration are disjoint;
3. each writer runs in an isolated Git worktree on a separate branch or detached HEAD;
4. the plan defines integration order and validation after integration;
5. no worker integrates its own work.

The root first snapshots status and base commit, then integrates one worker at a time. It checks the changed-file list against ownership before applying changes. Conflicts, unexpected deletions, generated artifacts, or ownership violations stop integration. The root does not guess conflict resolution; it routes the conflict to the owning task or revises the plan. If one worker fails, successful independent work remains unintegrated until the root confirms it is still valid without the failed dependency.

## 8. Read-only command safety

Read-only roles require `sandbox_mode = "read-only"` in their future agent definitions. They may use file listing/search, version/status inspection, diff inspection, and tests explicitly proven to write only in an isolated disposable directory.

They must not run dependency installation, formatters with write mode, code generation, migrations, servers, deployment tools, Git mutation, cache-clean commands, or tests that write to the repository, external databases, queues, cloud resources, or shared caches. When a validation command has unavoidable side effects, the root creates a disposable worktree/environment, declares those effects in the packet, and cleans it through an approved recovery procedure.

## 9. Validation, review, and evidence

Implementer checks are local sanity only. Normal and complex tasks require an independent validator after integration. High-risk and multi-worker tasks also require review after validation. A reviewer request for changes routes back to an implementer, followed by revalidation and rereview of affected findings.

`PASS` means every required command actually ran, returned the expected successful status, and supplied the expected evidence. Missing tools, credentials, services, or permissions produce `BLOCKED`, not PASS or an expensive reasoning escalation.

Evidence is stored in concise summaries in `ACTIVE_PLAN.md` and `HANDOFF.md`; optional detailed operational events may use `plans/execution-log.jsonl`. Store command, timestamp, exit code, relevant excerpt, task ID, and artifact path when applicable. Redact secrets before persistence. Never retain raw environment dumps, credentials, tokens, authorization headers, connection strings, private keys, or unnecessary personal data.

Active-plan evidence is kept while the task is active. On completion, move the final plan to `plans/completed/` according to the host project's retention policy; default retention is 90 days, after which evidence may be deleted while keeping a secret-free completion summary. Security or compliance policy may require a shorter or longer period and takes precedence.

## 10. Escalation

Escalation requires a recorded trigger, observed evidence, why more reasoning is needed, and what remains unresolved. Environment failures are blocked with remediation, not escalated. Architectural discoveries return to the architect; bounded hard implementation may use Luna escalation; consequential unresolved architecture may use the deep architect.

## 11. Authorization and publishing

Every task states allowed and prohibited operations. Workers may not silently substitute another environment, database, account, branch, service, or credential source.

Publishing modes are distinct:

- `commit-only`: create a local commit; do not push.
- `push-existing-commit`: push specified existing commit(s)/branch; do not create a new commit.
- `commit-and-push`: create the approved commit and push it to the approved remote/branch.

Authorization for one mode does not imply another. Ambiguous language such as "ship it" requires clarification. Force push, branch deletion, tag creation, release creation, deployment, and pull-request creation each require separate explicit authorization.

## 12. Installer transaction contract

The project-scoped installers and uninstallers support `-WhatIf`/`--dry-run`; reject unsupported Codex versions before installation writes; resolve paths without following unsafe symlinks; refuse links escaping the approved target repository; and never delete broad directories. Global Codex-home installation remains deferred; project custom-agent installation is opt-in and transactional.

Before mutation, compute pre-install hashes, create same-volume temporary files and backups, and write a transaction journal. Validate staged content, then replace files atomically where the platform supports it. Record pre-hash, installed hash, backup path, ownership, and transaction ID in the state manifest. Update the manifest last through an atomic replace.

On failure, rollback applied operations in reverse order and report every restored or unresolved path. Interrupted runs detect the journal and offer deterministic rollback or resume only after hashes match. Reinstall replaces only files whose current hash matches the prior installed hash. Uninstall removes only unchanged package-owned files, restores verified backups, preserves modified files, and reports manual cleanup. Symlinks/reparse points are never overwritten or traversed unless an explicit, validated policy permits the exact target.

## 13. License and attribution

This workflow is an original synthesis informed by Soluna and `subagents_configs`. Do not copy code or prose from a source without recording source URL, file, applicable license, and required notice. Material from a repository with an unknown or incompatible license may be studied as an idea but not copied or closely adapted. Generated distributions must include all required notices.

## 14. Definition of done for the contracts milestone

- Role responsibilities do not conflict.
- Root state ownership is unambiguous.
- Task and result packets are machine-parseable and versioned.
- Shared and isolated worktree policies cover integration failures.
- Read-only roles have command-side-effect restrictions.
- Commit and push authorities are separate.
- Compatibility is explicit and fail-closed.
- Installer recovery behavior is specified without implementing installers.
- Evidence retention and redaction are defined.
- All four project templates include the required fields.
