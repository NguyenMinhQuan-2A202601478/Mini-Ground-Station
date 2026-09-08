# Agent tooling in this repository

Two things are installed, and they do different jobs.

## repository-harness — how to work here

[repository-harness](https://github.com/hoangnb24/repository-harness) installs a
repository protocol: the repository, not a chat log, is the system of record.

| Path | Role |
|---|---|
| `AGENTS.md` | the entrypoint — read this first |
| `CLAUDE.md` | imports `AGENTS.md` (Claude Code does not auto-load it) |
| `docs/WORKFLOW.md` | request shapes, planning, judgment, validation |
| `docs/README.md` | the documentation map |
| `docs/plans/active/` | durable plans, for work spanning sessions |
| `docs/decisions/` | choices later work must inherit |
| `.agents/skills/` | `encode-invariant`, `onboard-repository`, `improve-harness`, … |

The `<!-- HARNESS:BEGIN -->` / `<!-- HARNESS:END -->` blocks in `AGENTS.md` and
`CLAUDE.md` are upstream-managed. Project-specific guidance goes *outside* them
— see the "This Repository" section of `AGENTS.md`.

**Installed by file copy** from the upstream repository's declared payload
(`scripts/harness-install-files.txt`), not via the bootstrap script, so nothing
downloaded a binary. To pick up the updater (`scripts/bin/harness status|doctor|update`):

```bash
curl -fsSL "https://raw.githubusercontent.com/hoangnb24/repository-harness/main/scripts/install-harness.sh" | bash -s -- --merge
```

`--merge` preserves the files already here and adds only what is missing.

## graphify — how to find things here

[graphify](https://github.com/Graphify-Labs/graphify) maps the repository into a
knowledge graph you query instead of grepping. Code is parsed locally with
tree-sitter — no LLM, nothing leaves the machine.

```bash
graphify query "how does a telemetry frame become an alert"
graphify path "ingest_frame" "Alert"
graphify explain "screen_once"
graphify update .          # after changing code — AST only, no API cost
```

Output lives in `graphify-out/` (git-ignored): `graph.json` for queries,
`graph.html` to open in a browser, `GRAPH_REPORT.md` for the highlights.

Installed project-scoped:

```bash
uv tool install graphifyy
graphify install --project
```

That wrote `.claude/skills/graphify/`, the graphify section of `CLAUDE.md`, and
`.claude/settings.json` — a `PreToolUse` hook that nudges an assistant to check
the graph before grepping. Delete the hooks block in `.claude/settings.json` if
you would rather it stayed out of the way.

## Rebuilding the graph

The graph is a derived artifact and is not committed. After a clone:

```bash
graphify update . --no-cluster    # extract
graphify cluster-only . --no-label  # communities, report, graph.html
```

Drop `--no-label` to have an LLM name the communities.
