# Codex Multi-Agent Workflow

Contract-first workflow for using Codex with bounded specialist agents. The installer adds the workflow to one project; it does not change global Codex configuration.

## Start here

- [WORK_FLOW.md](WORK_FLOW.md) — authoritative step-by-step installation, operation, update, recovery, validation, and uninstall guide.
- [WORK_FLOW.ru.md](WORK_FLOW.ru.md) — Russian translation.
- [CODEX_WORKFLOW.md](CODEX_WORKFLOW.md) — workflow contract.
- [rules/](rules/) — routing, validation, escalation, and Git-safety rules.
- [agents/](agents/) — nine opt-in custom-agent definitions.
- [plan/plan_validated_finis.md](plan/plan_validated_finis.md) — current implementation, progress, and release status; [plan/plan.md](plan/plan.md) — original design reference.

If this file conflicts with the detailed guide, follow `WORK_FLOW.md` for installation and lifecycle steps and `CODEX_WORKFLOW.md` for workflow semantics.

## Current status

- Installation is contracts-only by default.
- `--with-custom-agents` (PowerShell: `-WithCustomAgents`) additionally installs the nine TOML definitions under the target project's `.codex/agents/`.
- Codex CLI `0.157.1` is the current gated static compatibility-registry version; historical `0.155.1` remains registered. Both have `runtimeValidated: false`. Python 3.11+ is required for custom-agent validation and installation.
- The `0.157.1` static migration passed the full 351-test repository suite with one skip. Two historical `0.155.1` same-fixture role captures and four `0.157.1` composite captures (`code_explorer`, `code_validator`, `sol_architect`, and `sol_architect_deep`) have bounded one-role acceptance. The other five roles and the full behavioral matrix remain unverified, so overall live validation is `NOT READY`. Current progress is tracked only in [plan/plan_validated_finis.md](plan/plan_validated_finis.md).
- Legacy `hybrid` names in state, markers, and compatibility identifiers are retained for existing installations; they are not the product branding.

## Prerequisites

- Codex CLI `0.157.1` (`codex --version`)
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

Run the package checks from this repository:

```powershell
python .\scripts\validate_agent_configs.py --codex-version 0.157.1
python .\scripts\verify_agent_runtime.py --target "C:\path\to\your-project"
```

The first command validates all nine TOMLs. The second may return overall `PASS` for matching installed files while discovery remains `UNVERIFIED`. Adding `--run-codex` runs a diagnostic probe, but its current unvalidated event adapter still returns overall/discovery `UNVERIFIED` with exit code 3 and cannot establish runtime PASS. For instruction-discovery and routing smoke tests, follow the validation checklist in [WORK_FLOW.md](WORK_FLOW.md).

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

Installation is project-scoped and idempotent. Existing unmanaged files are not silently replaced.

## Scope and limitations

Agent sandbox and model settings are defaults subject to the parent Codex session's controls, not an independent security boundary. Existing symlink, junction, and reparse-point ancestors are rejected, but pathname-based checks do not close an adversarial concurrent Windows ancestor-swap TOCTOU race; use only a trusted local filesystem until a reviewed handle-relative backend or threat-model decision resolves V0b. The workflow does not authorize commits, pushes, deployments, credential changes, or production-data changes by itself. See [WORK_FLOW.md](WORK_FLOW.md) and the rule documents for the complete safety and recovery behavior.
