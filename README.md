# Codex Multi-Agent Workflow

Contract-first workflow for using Codex with bounded specialist agents. The installer adds the workflow to one project; it does not change global Codex configuration.

## Start here

- [WORK_FLOW.md](WORK_FLOW.md) â€” authoritative step-by-step installation, operation, update, recovery, validation, and uninstall guide.
- [WORK_FLOW.ru.md](WORK_FLOW.ru.md) â€” Russian translation.
- [Offline validation](docs/OFFLINE_VALIDATION.md) — repository-local static and synthetic checks while runtime provenance is blocked.
- [CODEX_WORKFLOW.md](CODEX_WORKFLOW.md) â€” workflow contract.
- [rules/](rules/) â€” routing, validation, escalation, and Git-safety rules.
- [agents/](agents/) â€” nine opt-in custom-agent definitions.
- [plan/plan_validated_finis.md](plan/plan_validated_finis.md) â€” current implementation, progress, and release status; [plan/plan.md](plan/plan.md) â€” original design reference.

If this file conflicts with the detailed guide, follow `WORK_FLOW.md` for installation and lifecycle steps and `CODEX_WORKFLOW.md` for workflow semantics.

## Current status

<!-- codex-policy-reference:begin -->
Current version policy: [registry-backed summary](codex_version.md#current-version-policy).
Release readiness remains **NOT_READY**; serving-backend/effective-policy
provenance remains **BLOCKED**. No registered version is runtime validated.
<!-- codex-policy-reference:end -->

- Installation is contracts-only by default.
- `--with-custom-agents` (PowerShell: `-WithCustomAgents`) additionally installs the nine TOML definitions under the target project's `.codex/agents/`.
- Python 3.11+ is required for custom-agent validation and installation.
- The local repository suite passed 396 tests with 1 skipped on 2026-10-01. The historical 0.159.3 run has accepted source-bound L0-L6 evidence: L5 covers nine roles and 18 unique sessions, and all five L6 challenges passed independent replay and review. The latest focused suite passed 54 tests independently. L8 task evidence is now present through a hash-bound native instruction manifest: explorer PASS; quick implementer FAIL because requested workspace-write was observed read-only in parent and child. Model and effort match; the remaining seven L8 cases were not run after that failure. L7 remains unaccepted; L9-L13 remain NOT RUN. Overall live validation remains **NOT READY** and `runtimeValidated: false`. The older run `eb2062b6-6307-4012-8806-a602d7fe9d57` is historical 0.159.0 evidence. See the [validation report](docs/validation/codex-0.159.3-windows.md) and [current plan](plan/plan_validated_finis.md).
- Legacy `hybrid` names in state, markers, and compatibility identifiers are retained for existing installations; they are not the product branding.

### Recorded migration checkpoint

The controlled 0.160.0 migration passed **120 independent tests** and separate
review **APPROVE**. Native installation succeeded with rollback preserved.
Launcher and managed backend report 0.160.0, but serving-backend linkage and
session effective-policy origin remain **BLOCKED**. No fresh 0.160.0 L0-L17
evidence is accepted; the historical capture/runtime gates remain closed to
0.160.0. Overall **NOT READY**. See the
[migration report](docs/validation/codex-0.160.0-windows.md).

## Prerequisites

- Codex CLI matching a static-installation-eligible registry entry in the current version policy
- Python 3 (3.11+ for custom agents)
- PowerShell 7 on Windows or a POSIX shell on Linux/macOS
- Write access to the target project
- Git is recommended for review and rollback

## Install

Preview first, then run the same command without the preview flag.

### Windows PowerShell

```powershell
.\install.ps1 -TargetRepository "C:\path\to\your-project" -WhatIf
.\install.ps1 -TargetRepository "C:\path\to\your-project"

# Include the opt-in custom-agent catalog
.\install.ps1 -TargetRepository "C:\path\to\your-project" -WithCustomAgents -WhatIf
.\install.ps1 -TargetRepository "C:\path\to\your-project" -WithCustomAgents
```

### Linux or macOS

```bash
bash ./install.sh --target "/path/to/your-project" --dry-run
bash ./install.sh --target "/path/to/your-project"

# Include the opt-in custom-agent catalog
bash ./install.sh --target "/path/to/your-project" --with-custom-agents --dry-run
bash ./install.sh --target "/path/to/your-project" --with-custom-agents
```

The target defaults to the current directory when omitted. For manual installation, interrupted transactions, or conflict recovery, use [WORK_FLOW.md](WORK_FLOW.md).

## Validate

Run the package checks from this repository. The concrete version below is the
preferred static installation target from the current version policy:

<!-- codex-current-target-example:begin -->
```powershell
python .\scripts\validate_agent_configs.py --codex-version 0.160.0
python .\scripts\verify_agent_runtime.py --target "C:\path\to\your-project"
```
<!-- codex-current-target-example:end -->

The first command validates all nine TOMLs. The second may return overall `PASS` for matching installed files while discovery remains `UNVERIFIED`. On versions denied by the discovery gate in the current policy, `--run-codex` fails closed with `UNSUPPORTED_RUNTIME_VERSION` before discovery. On historical allowlisted versions it runs a diagnostic probe whose unvalidated event adapter returns overall/discovery `UNVERIFIED` with exit code 3 and cannot establish runtime PASS. For instruction-discovery and routing smoke tests, follow the validation checklist in [WORK_FLOW.md](WORK_FLOW.md).

## Uninstall

Preview first, then remove unchanged managed content:

```powershell
.\uninstall.ps1 -TargetRepository "C:\path\to\your-project" -WhatIf
.\uninstall.ps1 -TargetRepository "C:\path\to\your-project"
```

```bash
bash ./uninstall.sh --target "/path/to/your-project" --dry-run
bash ./uninstall.sh --target "/path/to/your-project"
```

The uninstaller preserves modified files, surrounding `AGENTS.md` instructions, and project-owned documents. Recovery, manual cleanup, and post-uninstall checks are documented in [WORK_FLOW.md](WORK_FLOW.md).

## What is installed

- `.codex-workflow/` with contracts, rules, and templates.
- A marked workflow block in the target project's `AGENTS.md`.
- `docs/ai/PROJECT_CONTEXT.md` and `plans/TASK_TEMPLATE.md` only when absent.
- A state manifest and transactional backups when needed (legacy filenames retain `hybrid` for compatibility).
- `.codex/agents/*.toml` only when the custom-agent option is selected.

The package includes an explicit execution timeline recorder at
`.codex-workflow/timeline/timeline_cli.py`. Use `start`, `finish`, and `show`
to record and export selected task stages and turns. This is manual
recordkeeping; it does not capture Codex sessions automatically. Keep
summaries short and sanitized, and enter model or usage values only when
verified. See the [timeline instructions](WORK_FLOW.md#optional-execution-timeline).

Installation is project-scoped and idempotent. Existing unmanaged files are not silently replaced.

## Scope and limitations

Agent sandbox and model settings are defaults subject to the parent Codex session's controls, not an independent security boundary. Existing symlink, junction, and reparse-point ancestors are rejected, but pathname-based checks do not close an adversarial concurrent Windows ancestor-swap TOCTOU race; use only a trusted local filesystem until a reviewed handle-relative backend or threat-model decision resolves V0b. The workflow does not authorize commits, pushes, deployments, credential changes, or production-data changes by itself. See [WORK_FLOW.md](WORK_FLOW.md) and the rule documents for the complete safety and recovery behavior.

L7 offline fixture preparation is complete: **13 new tests and 54 behavioral regression tests PASS** under independent validation, with separate final review **APPROVE**. It preserves immutable calculator tests and accepts only the exact repair in a fresh temporary fixture. Live L7 remains **UNACCEPTED**, escalation **NOT_EXERCISED**, provenance preflight **BLOCKED**; no paid writer retry follows this check. See the [readiness audit](docs/validation/l7-offline-readiness-0.159.3.json).
