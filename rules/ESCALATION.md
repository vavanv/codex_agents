# Escalation Rules

Escalation buys additional reasoning only when evidence shows reasoning is the limiting factor.

## Required record

Every escalation records:

```text
Escalation trigger:
Observed evidence:
Attempts already made:
Why additional reasoning is needed:
What remains unresolved:
Authorized scope:
```

## Bounded implementation escalation

Use Luna escalation when architecture and ownership are fixed but implementation remains difficult after at least one focused attempt and evidence gathering. Examples include a difficult algorithm, localized concurrency race, or complex type interaction.

The escalation receives only the failing task packet, relevant code, attempts, and evidence. It cannot widen scope, redesign architecture, delegate, commit, or push.

## Architecture escalation

Return to the normal architect when evidence shows incorrect task boundaries, a hidden cross-module dependency, or an unresolved API/data decision. Use the deep architect only when the normal architect records a consequential unresolved choice involving security architecture, migrations, concurrency design, compatibility, or cross-system coupling.

## Non-escalation blockers

Do not spend higher-cost reasoning on missing dependencies, missing credentials, unavailable services, permission denial, unsupported Codex versions, malformed environments, tool crashes, huge unfiltered logs, or vague/missing context. Mark the task BLOCKED with a concrete remediation and resume point.

## Exit conditions

An escalation ends with a bounded solution and evidence, a recommendation to replan, or a blocker. Repeated escalation without new evidence is prohibited.
