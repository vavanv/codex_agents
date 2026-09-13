# Routing Rules

## Classification

Classify before delegation.

Use **Class A** only when the change is mechanical, architecture is known, scope is normally one or two files, rollback is obvious, and no public contract, persistence, security, concurrency, migration, or non-trivial test design is involved.

Use **Class B** for ordinary multi-file features and bug fixes with bounded architecture and moderate risk.

Use **Class C** when work affects authentication, authorization, security, payments, migrations, persistence semantics, concurrency, public APIs, multiple services, production-sensitive logic, broad refactors, difficult-to-test behavior, or parallel writers.

If uncertain between classes, choose the higher class and record why.

## Routes

```text
Class A: root -> quick implementer -> focused check
Class B: root -> explorer if needed -> architect -> implementer -> validator
Class C: root -> explorer(s) -> architect -> implementer(s) -> validator -> reviewer
```

The architect proposes plan content; the root validates and writes it. Skip the explorer when relevant files and contracts are already known. Use one implementer by default.

## Parallel routing gate

Parallel writers require all of the following:

- independent tasks and no unmet dependency between them;
- isolated Git worktrees;
- disjoint ownership including tests, fixtures, lockfiles, generators, and configuration;
- a declared integration order;
- an integration validation command;
- root capacity to inspect and integrate each result separately.

If any condition is missing, serialize implementation. Read-only agents may run concurrently only with side-effect-safe commands.

## Failure routing

| Evidence | Route |
| --- | --- |
| Local implementation defect | Same implementer with a focused correction packet |
| Wrong boundaries or unresolved public/data decision | Architect |
| Consequential architecture uncertainty with documented evidence | Deep architect |
| Repeated bounded algorithm/type/concurrency failure under fixed architecture | Luna escalation |
| Missing dependency, credential, service, permission, or broken tool | BLOCKED with remediation |
| Validation failure | Implementer, then independent revalidation |
| Review requests changes | Implementer, validation, then rereview |
| Ownership violation or integration conflict | Stop integration; root revises ownership/order or routes to owning task |

## Completion gate

The root may complete a task only when every acceptance criterion has accepted direct evidence, required validation passed, required review approved, integration is clean, and no publishing operation remains part of the user's request.
