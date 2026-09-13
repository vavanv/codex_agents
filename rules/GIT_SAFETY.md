# Git and Worktree Safety Rules

## Before work

When the directory is a Git repository, record the current branch or detached state, base commit, `git status --short`, and relevant diff. Existing changes belong to the user unless explicitly identified otherwise. Never discard, overwrite, reformat, stage, or commit unrelated changes.

## Shared checkout

Only one implementation writer may operate in a shared checkout at a time. File ownership alone is insufficient isolation because tests, formatters, lockfiles, generated files, caches, and shared configuration can overlap.

Read-only activity may be concurrent only when its commands cannot mutate the checkout or external systems.

## Parallel implementation

Parallel writers require separate Git worktrees based on the recorded base commit and separate branches or detached HEADs. The plan must assign disjoint ownership, including tests and indirect outputs, and state the integration order.

Workers do not merge, cherry-pick, rebase, or resolve integration conflicts. Each returns its base commit, final commit/diff identity, and changed-file list. The root verifies ownership, integrates one result at a time, and runs the declared check after each integration plus final validation.

Stop integration on:

- an unexpected changed or deleted file;
- a base mismatch that invalidates assumptions;
- a merge/apply conflict;
- generated or lockfile overlap;
- missing worker evidence;
- a dependency on failed or unintegrated work.

The root routes the conflict back to the owning task or replans. It never guesses at conflict resolution. Successful independent work remains isolated until the root confirms it is valid and safe to integrate.

## Publishing authorization

Use exactly one explicit mode:

| Mode | Allowed | Not allowed |
| --- | --- | --- |
| commit-only | Stage reviewed files and create a local commit | Push |
| push-existing-commit | Push named existing commit(s)/branch to named remote/branch | Create or amend commits |
| commit-and-push | Create the reviewed commit and push to the named target | Any other branch/remote operation |

Before commit, inspect status, unstaged diff, and staged diff. Stage explicit intended paths; do not use `git add .` without a reviewed complete file list. Report commit hash and branch.

Before push, verify remote, source branch/commit, destination branch, and that authorization includes push. Normal push is the default. Force push, tag/release creation, pull-request creation, branch deletion, hook bypass, and history rewriting each require separate explicit authorization.

## Destructive operations

Do not use hard reset, broad clean, checkout-based discards, recursive deletion, or forced branch movement to solve an integration problem. Prefer a new worktree, patch, revert commit, or user-approved targeted recovery.
