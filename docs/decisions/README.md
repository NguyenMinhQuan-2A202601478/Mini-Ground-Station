# Decisions

Decision records preserve lasting product, architecture, compatibility,
security, data-ownership, and validation choices.

Use `docs/templates/decision.md`. Task-local choices stay in the active plan.

## Current Upstream Decisions

| Decision | Title |
| --- | --- |
| 0019 | Repository-Centered Default Workflow |
| 0020 | Installation Profiles And Knowledge Boundaries |
| 0024 | Rust Harness Core Maintenance CLI |
| 0025 | Latest-Release Self-Update And Human-Directed Conflicts |
| 0026 | Explicit Onboarding Skills In Default Core |
| 0027 | End Protocol V1 And Focus The Repository Protocol |
| 0028 | Authoritative Invariant Encoding |

These decisions describe upstream Harness. Installed consumers begin with an
empty decision index and add only real consumer choices.

## History

Superseded database lifecycle, story, trace, orchestration, and migration
decisions remain available through Git history. They are absent from the
current index so agents do not confuse historical authority with current
product behavior.

## Add A Decision When

- a lasting product or architecture choice changes;
- public compatibility or data ownership changes;
- security or recovery policy changes;
- validation is materially added, removed, or weakened; or
- the source-of-truth hierarchy changes.
