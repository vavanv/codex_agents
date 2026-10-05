# Codex Multi-Agent Workflow: Project Installation and Operation

[Русская версия](WORK_FLOW.ru.md)

This guide explains how to install the workflow in one project, what the installer changes, how Codex uses the installed files, how to verify the result, and how to uninstall safely.

## What this installation provides

The current release installs project-specific workflow contracts. These contracts tell Codex how to classify work, when to plan, how to assign bounded implementation work, what evidence is required, when review is mandatory, and when Git publishing is allowed.

It provides:

- project instructions through `AGENTS.md`;
- routing, validation, escalation, and Git-safety rules;
- project context, task, plan, and handoff templates;
- a transactional installer and uninstaller;
- SHA-256 tracking of managed files;
- rollback for interrupted operations;
- an opt-in catalog of nine project-level custom agents;
- offline TOML/schema validation and optional live discovery diagnostics.

Contracts-only installation remains the default. Pass `-WithCustomAgents` on Windows or `--with-custom-agents` on Linux/macOS to validate and install `.codex/agents/*.toml` for the explorer, implementers, architects, escalation agent, validator, reviewer, and `commit_pusher` roles.

Custom agents remain opt-in; consult the current version policy for installation and evidence eligibility.

<!-- codex-policy-reference:begin -->
Current version policy: [registry-backed summary](codex_version.md#current-version-policy).
Release readiness remains **NOT_READY**; serving-backend/effective-policy
provenance remains **BLOCKED**. No registered version is runtime validated.
<!-- codex-policy-reference:end -->

Role isolation is also a layered guardrail, not an immutable security boundary: parent-session settings can override agent defaults, and filesystem read-only mode alone cannot prevent every external side effect.

With custom agents installed:

- Codex loads the project TOMLs in new sessions and can select the named roles with their configured models, reasoning efforts, sandboxes, and instructions;
- explorer, architect, validator, and reviewer TOMLs default to `read-only`;
- implementers, escalation, and `commit_pusher` default to `workspace-write` but remain restricted by ownership and authorization instructions;
- the root still owns routing, execution state, integration, and final evidence;
- static checks prove file/schema consistency, while the optional live diagnostic only collects candidate discovery evidence and cannot currently certify runtime discovery;
- behavioral permission claims remain unproven until the full disposable-project matrix is run and reviewed.

Installation, update, recovery, and uninstall remain project-scoped. No global Codex agent configuration is added or replaced, and a conflicting unmanaged project agent causes a fail-closed result before writes.

The local repository suite passed 396 tests with 1 skipped on 2026-10-01. The historical 0.159.3 run has accepted source-bound L0-L6 evidence: L5 covers nine roles and 18 unique sessions, and all five L6 challenges passed independent replay and review. The latest focused suite passed 54 tests independently. L8 task evidence is now present through a hash-bound native instruction manifest: explorer PASS; quick implementer FAIL because requested workspace-write was observed read-only in parent and child. Model and effort match; the remaining seven L8 cases were not run after that failure. L7 remains unaccepted; L9-L13 remain NOT RUN. Overall live validation remains **NOT READY** and `runtimeValidated: false`. The older run `eb2062b6-6307-4012-8806-a602d7fe9d57` is historical 0.159.0 evidence. See the [validation report](docs/validation/codex-0.159.3-windows.md) and [current plan](plan/plan_validated_finis.md).

## Installation scope

Installation is scoped to one target project. Nothing is written to the global Codex home directory.

```text
workflow package repository
        |
        | install.ps1 or install.sh
        v
your target project
  |-- AGENTS.md                         managed block added
  |-- .codex/agents/*.toml              optional custom agents
  |-- .codex-workflow/                  immutable contract copies
  |-- docs/ai/PROJECT_CONTEXT.md        editable project context
  |-- plans/TASK_TEMPLATE.md            task-packet reference
  `-- .hybrid-codex-workflow-state.json installation state
```

The `hybrid` text in internal state, journal, backup, schema, and marker identifiers is retained for compatibility with installations created before the current branding. Do not rename those identifiers manually.

## Prerequisites

Before installation, confirm:

1. The workflow package has been downloaded or cloned locally.
2. The target project directory already exists.
3. Python 3 is available; Python 3.11 or newer is required when installing custom agents.
4. Codex CLI is available on `PATH`.
5. Codex CLI matches a static-installation-eligible registry entry in the current version policy.
6. You have permission to write to the target project.

Windows PowerShell checks:

```powershell
python --version
codex --version
Test-Path -LiteralPath "C:\path\to\codex-multi-agent-workflow\install.ps1"
Test-Path -LiteralPath "C:\path\to\your-project"
```

Linux/macOS checks:

```bash
python3 --version
codex --version
test -f "/path/to/codex-multi-agent-workflow/install.sh"
test -d "/path/to/your-project"
```

Example output for the preferred static installation target:

<!-- codex-current-target-example:begin -->
```text
codex-cli 0.160.0
```
<!-- codex-current-target-example:end -->

The installer fails before changing project files if the Codex version is unsupported or cannot be determined.

## Install on Windows

Open PowerShell in the workflow package repository.

### Step 1: Preview the installation

```powershell
.\install.ps1 -TargetRepository "C:\path\to\your-project" -WhatIf
```

The preview performs prerequisite, version, source, path, symlink, state, and marker checks. It prints each planned operation without writing files.

Review the output. A new project normally reports planned installation of the contract files, project context, task template, managed `AGENTS.md` block, and state manifest.

### Step 2: Install

```powershell
.\install.ps1 -TargetRepository "C:\path\to\your-project"
```

To preview and install the custom-agent catalog:

```powershell
.\install.ps1 -TargetRepository "C:\path\to\your-project" -WithCustomAgents -WhatIf
.\install.ps1 -TargetRepository "C:\path\to\your-project" -WithCustomAgents
```

Success ends with:

```text
PASS: Codex Multi-Agent Workflow installed in C:\path\to\your-project
```

The target defaults to the current directory:

```powershell
Set-Location "C:\path\to\your-project"
C:\path\to\codex-multi-agent-workflow\install.ps1
```

### Step 3: Configure the project context

Open `docs/ai/PROJECT_CONTEXT.md` in the target project and replace the placeholders with real project information:

- applications and services;
- module boundaries;
- public APIs;
- authentication and authorization rules;
- build, test, lint, and type-check commands;
- expected command side effects;
- production-sensitive operations;
- coding conventions;
- files Codex must not modify.

Do not put secrets, tokens, passwords, private keys, or complete connection strings in this file.

### Step 4: Start a new Codex session

Codex builds its `AGENTS.md` instruction chain when a run or TUI session starts. Restart an existing session after installation.

```powershell
codex --cd "C:\path\to\your-project"
```

Official OpenAI documentation describes this project-level instruction discovery in [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

## Install on Linux or macOS

Open a shell in the workflow package repository.

### Step 1: Preview

```bash
bash ./install.sh --target "/path/to/your-project" --dry-run
```

### Step 2: Install

```bash
bash ./install.sh --target "/path/to/your-project"
```

To preview and install the custom-agent catalog:

```bash
bash ./install.sh --target "/path/to/your-project" --with-custom-agents --dry-run
bash ./install.sh --target "/path/to/your-project" --with-custom-agents
```

### Step 3: Configure project context

Edit:

```text
/path/to/your-project/docs/ai/PROJECT_CONTEXT.md
```

Replace its placeholders with verified commands, architecture, boundaries, and side effects.

### Step 4: Start a new session

```bash
codex --cd "/path/to/your-project"
```

## What the installer does internally

Installation follows these steps in order:

1. The PowerShell or Bash wrapper locates Python 3.
2. `scripts/workflow_manager.py` resolves the target to an absolute directory.
3. It rejects a filesystem root, a symlinked target, path traversal, or managed paths that traverse symlinks.
4. It verifies that all required package sources are ordinary files.
5. It runs `codex --version` and rejects unsupported versions.
6. With custom agents requested, it validates all nine TOMLs against `compatibility/codex-agents.json` before planning writes.
7. It checks for an interrupted transaction journal.
8. It loads and validates an existing state manifest when this is a reinstall.
9. It refuses an unmanaged `.codex-workflow/` directory because ownership is uncertain.
10. It rejects a conflicting unmanaged `.codex/agents/*.toml`; byte-identical pre-existing agents are preserved as project-owned.
11. It calculates SHA-256 hashes for source and installed files.
12. It plans new files, safe updates, and files that must be preserved because the user modified them.
13. It appends or updates one marked block in `AGENTS.md` without replacing surrounding project instructions.
14. It writes and strictly verifies the transaction journal before creating any durable preparation backup.
15. It creates required backups and verifies their ownership, paths, and hashes against the journal.
16. It checkpoints each action as `applying`, performs the change through same-directory temporary files and atomic replacement, verifies the resulting hash, and then checkpoints it as `applied`.
17. It writes the state manifest last, verifies it, and marks the transaction committed.
18. It performs committed cleanup; journal and backup cleanup is resumable if interrupted.

On a failure before commit, recovery rolls applied actions back in reverse order using verified evidence. Recovery of a committed transaction never rolls back installed state; it resumes and finishes cleanup. If rollback or cleanup cannot be resolved safely, the journal and supporting evidence remain in place for explicit recovery.

The path checks reject existing symbolic-link, junction, and reparse-point targets and ancestors. They are pathname-based, however, and do not close an adversarial concurrent Windows ancestor-swap TOCTOU race. Until a reviewed handle-relative Win32 backend is implemented or the threat model explicitly accepts this limitation, operate only on a trusted local filesystem and do not claim full adversarial Windows link safety.

## Files installed in your project

### `AGENTS.md`

The installer adds one block bounded by legacy compatibility markers:

```markdown
<!-- hybrid-codex-workflow:start -->
## Codex multi-agent workflow

- Read `.codex-workflow/CODEX_WORKFLOW.md` and the applicable files under `.codex-workflow/rules/` before routing or implementing work.
- Use `docs/ai/PROJECT_CONTEXT.md` for project-specific architecture, commands, and boundaries.
- If `.codex/agents/*.toml` is present, use the named role definitions for delegation; treat their sandbox values as defaults subject to parent-session controls.
- For non-trivial work, the root orchestrator exclusively owns `plans/ACTIVE_PLAN.md` and `plans/HANDOFF.md`.
- Preserve unrelated changes, require observed validation evidence, and never infer commit or push authorization.
<!-- hybrid-codex-workflow:end -->
```

If `AGENTS.md` already exists, its content remains in place and the block is appended. A reinstall updates an unchanged older branded block to the current wording. A modified managed block is preserved and reported instead of being overwritten.

### `.codex-workflow/`

This directory contains installer-owned copies:

```text
.codex-workflow/
  CODEX_WORKFLOW.md
  rules/
    ROUTING.md
    VALIDATION.md
    ESCALATION.md
    GIT_SAFETY.md
  templates/
    PROJECT_CONTEXT.md
    ACTIVE_PLAN.md
    HANDOFF.md
    TASK_TEMPLATE.md
  timeline/
    timeline_cli.py
    timeline_collector.py
    timeline_exporter.py
    timeline_privacy.py
    timeline_schema.py
```

Treat these as package-managed files. Put project-specific information in `docs/ai/` and `plans/`, not in `.codex-workflow/`.

### Optional execution timeline

The installer includes an explicit timeline recorder. It does not observe
Codex sessions automatically. Start and finish a turn around the stage you
want to record, then inspect or export the task ledger:

```powershell
python .\.codex-workflow\timeline\timeline_cli.py start --task-id <task-id> --stage IMPLEMENTATION --agent-role root --summary "Implement request validation"
python .\.codex-workflow\timeline\timeline_cli.py finish --task-id <task-id> --turn-id 0 --status SUCCESS --summary "Validation and implementation completed"
python .\.codex-workflow\timeline\timeline_cli.py show --task-id <task-id>
python .\.codex-workflow\timeline\timeline_cli.py show --task-id <task-id> --format json --output timeline.json
```

Use `--project <path>` before the subcommand when running outside the project
directory. The start command prints the turn ID to pass to finish. Records are
stored in `.codex-workflow-data/timeline/<task-id>.jsonl`; the Markdown, JSON and CSV
views can be regenerated. Omit observed model and usage fields unless directly
supported by a trusted source; absent values remain unavailable. Enter only
short sanitized summaries. Never include full prompts, hidden reasoning,
credentials, tokens or private rollout text. The redactor handles common
secret patterns but cannot identify every secret or remove all prompt content.
This explicit recorder makes no automatic-capture or complete-history claim.

### `.codex/agents/` (opt-in)

The custom-agent flag installs nine validated TOMLs:

```text
.codex/agents/
  code-explorer.toml
  quick-implementer.toml
  implementer.toml
  luna-escalation.toml
  sol-architect.toml
  sol-architect-deep.toml
  code-validator.toml
  code-reviewer.toml
  commit-pusher.toml
```

Each file defines `name`, `description`, `developer_instructions`, `model`, `model_reasoning_effort`, and `sandbox_mode`. Do not edit package-owned copies if you want automatic updates. Modified copies are preserved during update and uninstall for manual review.

### `docs/ai/PROJECT_CONTEXT.md`

This is an editable seed file. The installer creates it only when it does not already exist. Once you edit it, the uninstaller recognizes the changed hash and preserves it.

### `plans/TASK_TEMPLATE.md`

This is the project-accessible worker packet reference. It is also created only when absent and preserved after user modification.

### Active plans and handoffs

Installation does not create `plans/ACTIVE_PLAN.md` or `plans/HANDOFF.md`. They are needed only for non-trivial active work. When required, the root orchestrator copies the templates from `.codex-workflow/templates/`, fills them in, and remains their only writer.

### State and recovery files

- `.hybrid-codex-workflow-state.json` records the package version, target path, Codex version, managed files, hashes, ownership, created directories, backups, and managed-block state.
- `.hybrid-codex-workflow-transaction.json` exists only while a transaction is active or when interrupted recovery is required.
- `.hybrid-codex-workflow-backups/` stores exact transactional backups when existing managed content must change.

Do not manually edit these files.

## How it works for your project

When Codex starts in your project, it discovers the root `AGENTS.md`. The managed block tells Codex to read the workflow contracts and your project context before routing work. If custom agents were installed, a new Codex session also loads their TOMLs and can spawn the named roles. Existing sessions must be restarted after installation or update.

The TOML sandbox is the role's default. Parent-session permission choices and explicit spawn overrides can take precedence, so the root must still inspect commands, changes, and evidence rather than assuming absolute isolation.

The operating sequence is:

```text
Your request
    |
    v
Root reads AGENTS.md, contracts, and PROJECT_CONTEXT.md
    |
    v
Classify task: trivial, normal, or complex/high-risk
    |
    +-- trivial ------> focused implementation and check
    |
    +-- normal -------> plan -> implementation -> independent validation
    |
    `-- high-risk ----> exploration -> plan -> implementation
                        -> validation -> independent review
    |
    v
Root accepts evidence, integrates results, and reports completion
```

### Small task example

Request:

```text
Change the Save button label to Apply.
```

Expected behavior:

1. Classify as trivial if no hidden contract or security concern exists.
2. Avoid creating an unnecessary full plan.
3. Make the bounded change.
4. Run a focused relevant check.
5. Report the observed result.

### Normal task example

Request:

```text
Add a REST endpoint with unit and integration tests.
```

Expected behavior:

1. Read the project's API conventions and commands from `PROJECT_CONTEXT.md`.
2. Classify at least as normal.
3. Create root-owned `plans/ACTIVE_PLAN.md` and `plans/HANDOFF.md` from the installed templates.
4. Define scope, ownership, acceptance criteria, validation commands, and authorization boundaries.
5. Perform bounded implementation.
6. Run independent validation.
7. Mark the task complete only when each criterion has observed evidence.

### High-risk task example

Request:

```text
Change payment authorization and persistence behavior.
```

Expected behavior:

1. Classify as complex/high risk.
2. Explore relevant contracts and risks before planning.
3. Define strict ownership and rollback considerations.
4. Implement within the approved plan.
5. Run independent validation.
6. Require an independent final review.
7. Route review findings back through implementation and validation.

### Git publishing example

```text
Commit these changes.
```

This authorizes a local commit only. It does not authorize push, force push, pull-request creation, deployment, tags, or branch deletion. Those operations require separate explicit instructions.

## Verify the installation

### Verify installed files on Windows

Run from the target project:

```powershell
$RequiredFiles = @(
    "AGENTS.md",
    ".codex-workflow\CODEX_WORKFLOW.md",
    ".codex-workflow\rules\ROUTING.md",
    ".codex-workflow\rules\VALIDATION.md",
    ".codex-workflow\rules\ESCALATION.md",
    ".codex-workflow\rules\GIT_SAFETY.md",
    ".codex-workflow\templates\PROJECT_CONTEXT.md",
    ".codex-workflow\templates\ACTIVE_PLAN.md",
    ".codex-workflow\templates\HANDOFF.md",
    ".codex-workflow\templates\TASK_TEMPLATE.md",
    "docs\ai\PROJECT_CONTEXT.md",
    "plans\TASK_TEMPLATE.md",
    ".hybrid-codex-workflow-state.json"
)

$MissingFiles = $RequiredFiles | Where-Object { -not (Test-Path -LiteralPath $_) }

if ($MissingFiles.Count -eq 0) {
    "PASS: installation files are present"
} else {
    "FAIL: installation files are missing"
    $MissingFiles
}
```

If custom agents were requested, also validate the package and installed catalog from the workflow package repository:

<!-- codex-current-target-example:begin -->
```powershell
python .\scripts\validate_agent_configs.py --codex-version 0.160.0
python .\scripts\verify_agent_runtime.py --target "C:\path\to\your-project"
```
<!-- codex-current-target-example:end -->

The first command validates nine source TOMLs. The second compares installed definitions with those sources: matching installed content may produce overall `PASS`, while discovery remains `UNVERIFIED` because it was not requested.

For an optional live diagnostic in a disposable or non-critical project:

```powershell
python .\scripts\verify_agent_runtime.py --target "C:\path\to\your-project" --run-codex --evidence ".\agent-discovery.json"
```

Recorded migration checkpoint: on 0.160.0, `--run-codex` fails closed with UNSUPPORTED_RUNTIME_VERSION before discovery. The historical capture and behavioral scripts remain pinned to 0.159.3. The controlled migration passed 120 independent tests and review APPROVE; installed native/managed versions match, but provenance remains BLOCKED and no fresh L0-L17 is accepted. See the [migration report](docs/validation/codex-0.160.0-windows.md).

For the historical allowlisted versions, `--run-codex` is currently a diagnostic probe, not a runtime certification. The retained `0.159.0` fixture does not certify `0.159.3`; a probe alone cannot establish runtime PASS. Running this probe uses paid inference and needs separate authorization under the live-validation plan.

### Verify instruction discovery

Start a new read-only check:

```powershell
codex --cd "C:\path\to\your-project" --ask-for-approval never "Without editing files or running project commands, list the active AGENTS.md instruction files and summarize the Codex multi-agent workflow rules."
```

Expected response:

- the target project's `AGENTS.md` is active;
- the workflow contracts and project context are identified;
- the root owns plan and handoff state;
- observed validation evidence is required;
- commit does not imply push.

### Verify idempotency

Preview a reinstall:

```powershell
.\install.ps1 -TargetRepository "C:\path\to\your-project" -WithCustomAgents -WhatIf
```

An unchanged installation should report managed files and the `AGENTS.md` block as `UNCHANGED`. Running the real installer again should not duplicate the block or rewrite an unchanged state manifest.

## Update an existing installation

1. Update or replace the workflow package repository with the desired release.
2. Run the installer's dry-run against the same target project.
3. Review `UPDATE`, `UNCHANGED`, and `PRESERVED MODIFIED FILE` messages.
4. Run the installer normally, retaining `-WithCustomAgents` or `--with-custom-agents` when updating the custom catalog.
5. Restart Codex so it reloads project instructions.
6. Repeat the instruction-discovery check.

The installer updates only files that still match the hashes it previously installed. Modified files remain untouched. The previous managed version is backed up before an update.

## Recover an interrupted operation

Do not delete `.hybrid-codex-workflow-transaction.json`. It records action phases, hashes, and verified backups. A noncommitted transaction is recovered by rollback; a committed transaction is recovered by completing cleanup. If recovery cannot safely resolve an item, the journal and evidence are retained.

Preview recovery on Windows:

```powershell
.\install.ps1 -TargetRepository "C:\path\to\your-project" -Recover -WhatIf
```

Perform recovery:

```powershell
.\install.ps1 -TargetRepository "C:\path\to\your-project" -Recover
```

Linux/macOS:

```bash
bash ./install.sh --target "/path/to/your-project" --recover --dry-run
bash ./install.sh --target "/path/to/your-project" --recover
```

The install and uninstall wrappers use the same recovery engine, so either wrapper can recover the recorded transaction.

## Uninstall on Windows

Open PowerShell in the workflow package repository.

Use the PowerShell uninstaller for Windows targets. Do not pass a Windows path such as `C:\path\to\your-project` to `bash ./uninstall.sh`: Bash can consume the backslashes and resolve the target incorrectly. Even a converted `/mnt/c/...` path can fail the manifest safety check when the workflow was installed with a Windows path. Use the same PowerShell environment for uninstall that was used for install.

### Step 1: Preview uninstall

```powershell
.\uninstall.ps1 -TargetRepository "C:\path\to\your-project" -WhatIf
```

Review every `REMOVE` and `PRESERVED MODIFIED` message.

### Step 2: Uninstall

```powershell
.\uninstall.ps1 -TargetRepository "C:\path\to\your-project"
```

### Step 3: Review preserved content

If every managed file was unchanged, success ends with:

```text
PASS: Codex Multi-Agent Workflow uninstalled from C:\path\to\your-project
```

If project-owned or managed content changed, the uninstaller leaves it in place and retains the state manifest:

```text
WARN: uninstall preserved modified managed content; state manifest retained
```

Review preserved files manually. Do not delete project context, plans, or a modified `AGENTS.md` block without confirming their content is no longer needed.

### Step 4: Start a new Codex session

Codex instructions are loaded per run/session. Restart Codex after uninstall so the removed block is no longer present in the instruction chain.

## Uninstall on Linux or macOS

Preview:

```bash
bash ./uninstall.sh --target "/path/to/your-project" --dry-run
```

Uninstall:

```bash
bash ./uninstall.sh --target "/path/to/your-project"
```

Recover an interrupted uninstall:

```bash
bash ./uninstall.sh --target "/path/to/your-project" --recover
```

## What the uninstaller removes

For each file recorded in the state manifest, the uninstaller:

1. Resolves and validates the exact managed path.
2. Refuses symlink traversal or path escape.
3. Calculates the current SHA-256 hash.
4. Removes the file only if its hash matches the installed hash.
5. Preserves and reports the file when the hash differs.
6. Removes the managed `AGENTS.md` block only when its normalized block hash matches the recorded hash.
7. Preserves all surrounding `AGENTS.md` instructions.
8. Removes only empty directories that the installer created.
9. Removes the state manifest only when no modified managed content remains.

The uninstaller never recursively deletes `docs/`, `plans/`, the target repository, or another broad directory.

Package-owned custom agents are removed only when their hashes still match. Modified and byte-identical pre-existing project agents are preserved. `.codex/agents/` and `.codex/` are removed only when the installer created them and they are empty.

## Troubleshooting

### Unsupported Codex version

```text
FAIL: Unsupported Codex CLI ...
```

No installation files were changed. Use the validated Codex version or update the workflow compatibility matrix and tests before supporting another version.

### `.codex-workflow` exists without state

The installer cannot prove ownership. Move the directory aside after inspection or restore the matching `.hybrid-codex-workflow-state.json`. Do not force an overwrite.

### Unmanaged custom agent conflicts with a package agent

The installer will not overwrite the existing `.codex/agents/<name>.toml`. Compare it with the package definition, rename the project-owned role, or remove it after making a backup, then rerun the dry-run. A byte-identical file needs no action and is recorded as pre-existing.

### State manifest is malformed

Restore the manifest from a trusted backup. The uninstaller makes no uncertain deletions without valid state.

### Managed `AGENTS.md` block is duplicated or malformed

Inspect the start and end markers. Keep one complete block and preserve all unrelated project instructions. Then rerun the dry-run.

### Modified file is preserved during update or uninstall

This is expected safety behavior. Compare the modified file with the corresponding source under the workflow package or `.codex-workflow/templates/`, decide which content to retain, and resolve it manually.

### Interrupted transaction detected

Run recovery with `-Recover` or `--recover`. Preview recovery first. Do not delete the journal or backups before recovery completes.

### Codex does not mention the workflow

1. Confirm the managed block exists once in the target root `AGENTS.md`.
2. Confirm `.codex-workflow/CODEX_WORKFLOW.md` exists.
3. Start Codex from the correct project root or pass `--cd`.
4. Restart the session after changing instructions.
5. Ask Codex to list active instruction files without editing.

Codex discovers project instructions from the project root toward the current directory, with closer instruction files taking precedence. Nested `AGENTS.override.md` files may therefore supersede broader guidance.

### Codex does not discover custom agents

1. Confirm installation used `-WithCustomAgents` or `--with-custom-agents`.
2. Run `scripts/verify_agent_runtime.py` without `--run-codex` to verify installed content.
3. Confirm the project is trusted; untrusted projects may skip project-scoped `.codex/` configuration.
4. Start a new session from the target project root.
5. Run the optional `--run-codex` diagnostic only with paid-live authorization and inspect its sanitized evidence output. Treat diagnostic discovery as `UNVERIFIED` until the full `0.159.3` L0–L17 matrix is accepted; this is not runtime PASS.

## Recommended project adoption sequence

1. Install in a disposable or non-critical project first.
2. Complete `PROJECT_CONTEXT.md` accurately.
3. [Verify instruction discovery](#verify-instruction-discovery). From the target repository root, start a new Codex check using the read-only prompt in that section. Do not let the check edit files or run project commands. Confirm that it identifies the target `AGENTS.md`, workflow contracts and project context, root ownership of plan and handoff state, the direct-evidence requirement, and that commit does not imply push. This checks instruction discovery only; it does not establish custom-agent runtime PASS.
4. Try a text-only trivial-task classification.
5. Try a normal task that creates a plan and handoff.
6. Confirm validation evidence is recorded for the non-trivial task. Installation does not create `plans/ACTIVE_PLAN.md` or `plans/HANDOFF.md`; inspect them when the workflow creates them, and inspect `plans/execution-log.jsonl` only if the task uses that optional ledger. The root owns these files. Each task and acceptance criterion must map to an exact validation command, its exit code, a concise relevant observation or artifact path, an honest `PASS`, `FAIL`, or `BLOCKED` result, and a timestamp when the workflow records one. Evidence must be redacted and contain no secrets. Normal and complex work also needs independent-validator evidence; work requiring high-risk review needs the reviewer's verdict. Missing required evidence means the task is not complete.

   Read-only PowerShell inspection from the target root (run `Get-Content` only for paths reported as present):

   ```powershell
   if (Test-Path -LiteralPath '.\plans\ACTIVE_PLAN.md') {
       Get-Content -LiteralPath '.\plans\ACTIVE_PLAN.md'
   }
   if (Test-Path -LiteralPath '.\plans\HANDOFF.md') {
       Get-Content -LiteralPath '.\plans\HANDOFF.md'
   }
   if (Test-Path -LiteralPath '.\plans\execution-log.jsonl') {
       Get-Content -LiteralPath '.\plans\execution-log.jsonl'
   }
   ```

   POSIX equivalent:

   ```bash
   test -f plans/ACTIVE_PLAN.md && sed -n '1,240p' plans/ACTIVE_PLAN.md
   test -f plans/HANDOFF.md && sed -n '1,240p' plans/HANDOFF.md
   test -f plans/execution-log.jsonl && sed -n '1,240p' plans/execution-log.jsonl
   ```
7. Test commit-only authorization without pushing.
8. Preview uninstall and confirm modified project context is preserved.
9. Adopt in additional repositories only after the pilot behaves as expected.

## Related files

- [README.md](README.md) provides the concise setup reference.
- [CODEX_WORKFLOW.md](CODEX_WORKFLOW.md) is the canonical workflow contract.
- [rules/ROUTING.md](rules/ROUTING.md) defines classification and routing.
- [rules/VALIDATION.md](rules/VALIDATION.md) defines evidence requirements.
- [rules/ESCALATION.md](rules/ESCALATION.md) defines escalation gates.
- [rules/GIT_SAFETY.md](rules/GIT_SAFETY.md) defines worktree and publishing safety.
- [scripts/workflow_manager.py](scripts/workflow_manager.py) implements installation transactions.
- [scripts/validate_agent_configs.py](scripts/validate_agent_configs.py) validates the versioned TOML catalog.
- [scripts/verify_agent_runtime.py](scripts/verify_agent_runtime.py) verifies installed content and provides the optional live diagnostic probe.
- [plan/plan_validated_finis.md](plan/plan_validated_finis.md) records current implementation, progress, and release status; [plan/plan.md](plan/plan.md) retains the original design scope.
