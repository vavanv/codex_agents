# Project Context

**Last verified:** YYYY-MM-DD  
**Owner:** <team or person>  
**Applies to:** <repository/path>

Do not store task history or secrets in this file. Verify commands and environment details before relying on them.

## Architecture

<System shape and major runtime boundaries.>

## Applications and services

| Name | Path | Purpose | Owner |
| --- | --- | --- | --- |
| <name> | <path> | <purpose> | <owner> |

## Module boundaries

<Allowed dependencies and ownership boundaries.>

## Important data models

<Models, invariants, and persistence ownership.>

## Public APIs

<Endpoints, events, schemas, compatibility promises.>

## Authentication and authorization

<Trust boundaries and test-safe guidance; no credentials.>

## External integrations

<Service names, contracts, and safe test doubles; no secrets.>

## Commands

| Purpose | Command | Expected side effects |
| --- | --- | --- |
| Build | `<command>` | <effects> |
| Unit test | `<command>` | <effects> |
| Integration test | `<command>` | <effects> |
| Lint | `<command>` | <effects> |
| Type-check | `<command>` | <effects> |
| Database/migration | `<command>` | <effects and authorization> |

## Deployment environments

<Environment names and boundaries. Do not include accounts or credentials.>

## Production-sensitive operations

<Operations requiring explicit authorization and recovery plans.>

## Coding conventions

<Repository-specific conventions.>

## Validation isolation

<Temporary directories, test databases, caches, and commands safe for read-only validation.>

## Git and generated files

<Lockfiles, generators, generated outputs, formatting scope, and worktree setup.>

## Files and directories agents must not modify

- `<path>`: <reason>

## Instruction precedence exceptions

<Nested instruction files and what they specialize.>
