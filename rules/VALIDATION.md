# Validation and Evidence Rules

## Validation layers

1. **Worker-local sanity:** focused checks used while implementing; never final evidence by itself for Class B or C.
2. **Independent validation:** a validator runs the plan's commands after integration.
3. **Integration review:** a reviewer examines the final diff, contracts, evidence, and risks for Class C and other required-review work.

## Validator isolation

The validator is source-read-only. A command is allowed only when it is demonstrably read-only or runs in a disposable environment whose permitted side effects are declared in the task packet. Tests must not mutate the user checkout, shared caches, external services, or persistent databases. Use isolated temporary paths, test databases, or worktrees when required.

Prohibited validator commands include dependency installation, write-mode formatting, code generation, migrations, deployment, Git mutation, production-service calls, and cleanup of unverified paths.

## Result format

```text
VALIDATION: PASS | FAIL | BLOCKED

Commands run:
Observed outcomes:
Failed checks:
Relevant error excerpts:
Acceptance criteria covered:
Likely affected task:
Recommended next action:
Side effects observed:
```

## PASS rule

PASS is valid only when all required commands ran, exit codes/results were observed, expected evidence was present, and no unexplained repository mutation occurred. Statements such as "should pass" or "looks correct" are not evidence.

FAIL means a check ran and demonstrated a product, test, integration, or contract failure. BLOCKED means the check could not run reliably because of the environment, permissions, dependencies, credentials, or unavailable services.

## Evidence records

For each criterion, retain the command or inspection, timestamp, exit code when applicable, concise observed outcome, and artifact location. Prefer summaries over raw output. Redact secrets before packets or files are written; if safe redaction is uncertain, omit the raw output and record only a sanitized diagnosis.

Never retain tokens, passwords, private keys, authorization headers, complete connection strings, raw environment dumps, or unrelated personal data.

Default retention is the active task plus 90 days in `plans/completed/`, subject to stricter host-project security or compliance policy. Handoffs contain only the minimum evidence needed to resume.

## Mutation check

When Git is available, compare status before and after independent validation. Unexpected changes make validation FAIL until explained and removed through an authorized, recoverable process. In a non-Git directory, use a declared file manifest or disposable environment instead.
