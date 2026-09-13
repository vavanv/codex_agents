# Hybrid Workflow Instructions

Use `CODEX_WORKFLOW.md` and the files under `rules/` as the operating contract for work in this repository.

## Root responsibilities

- Classify each request before delegating.
- Keep trivial, well-understood changes on the fast path.
- Create a bounded plan for non-trivial work.
- Own and exclusively edit `plans/ACTIVE_PLAN.md` and `plans/HANDOFF.md` when those files exist.
- Give workers only the context required for their assigned task.
- Accept completion only from direct evidence mapped to acceptance criteria.
- Preserve unrelated user changes.
- Keep implementation serialized in a shared checkout.
- Use parallel implementation only in isolated Git worktrees with disjoint file ownership and an explicit integration order.
- Run independent validation for normal and complex work.
- Require independent review for security, authentication, authorization, payments, migrations, persistence semantics, concurrency, public API changes, broad refactors, difficult-to-test changes, or multi-worker integration.
- Treat commit and push as separate operations requiring matching explicit user intent.

## Prohibited behavior

- Do not claim a check passed unless its successful result was observed.
- Do not let workers edit execution-state files or files owned by another worker.
- Do not allow recursive worker delegation.
- Do not widen scope, environment, account, credential source, or publishing authority silently.
- Do not place secrets or unredacted sensitive output in plans, handoffs, packets, or logs.
- Do not commit or push unless the user explicitly authorizes that exact operation.
- Do not invent custom agent TOMLs or global Codex configuration before their schemas, models, and behavioral tests are approved.

## Conflict handling

Follow the most specific applicable project instruction when it is stricter. If an instruction conflicts with the authorization, publishing, evidence, or secret-handling requirements in this workflow, stop and ask the user to resolve the conflict.
