# Agent Instructions

<!-- HARNESS:BEGIN -->
## Harness

Start with the requested outcome and use the repository as the system of record.
Read `docs/WORKFLOW.md` and only relevant product, design, plan, code, and
validation material.

- Answers, explanations, reviews, diagnoses, plans, and status reports are
  read-only. Inspect only what is needed; change nothing.
- For a bounded change, inspect affected behavior and proof, implement, and
  validate. No control-plane operation is required.
- Use one `docs/plans/active/` file when work spans sessions, coordinates
  contributors, has dependencies, or needs recovery. Move it to
  `docs/plans/completed/` only after validation.
- Before editing, identify repository authority for each new externally
  observable policy. If materially different choices remain open, stop before
  edits; configurable defaults are not authority.
- For architecture, reliability, security, or quality invariant work, read
  `docs/patterns/encoding-invariants.md` and enforce only accepted rules.
- Report reusable agent friction. Change guidance, tools, runbooks, or validation
  for that purpose only when explicitly asked to use `$improve-harness`.
- Also pause when product intent remains ambiguous, recovery is difficult,
  validation is weakened, or authority is insufficient.
- Claim completion only with executable or observable evidence. Report outcome,
  changes, validation, and unresolved risks.

Harness has no task database or orchestration lifecycle. Use repository plans
and behavior-level proof; do not create parallel control-plane state.
<!-- HARNESS:END -->

## This Repository

mini-ground-station ingests satellite telemetry, stores it in PostgreSQL, and
screens it for anomalies. Three processes, one database:

- `src/mgs/api/` — FastAPI ingestion and query surface, plus the dashboard
  page it serves from `src/mgs/api/static/`
- `src/mgs/worker/` — anomaly screening (threshold rules + statistical detector)
- `src/mgs/simulator/` — the simulated spacecraft that feeds the API

### Authority

- `docs/product/schema.md` is authoritative for the data model. `src/mgs/models.py`
  implements it and `migrations/versions/` applies it. A change to any one of the
  three is incomplete until all three agree.
- `docs/ARCHITECTURE.md` is authoritative for process boundaries — what runs
  where, and why screening is not done inside the request path.
- Thresholds are configuration (`Settings` in `src/mgs/config.py`), not policy.
  Changing a default limit changes what operators are woken up for: say so. The
  dashboard reads those limits from `/api/v1/summary` rather than hard-coding
  them (`docs/decisions/0003`); keep it that way.

### Validation

```bash
make test          # unit tests; integration tests need `make db-up`
make lint          # ruff check + format --check
make demo          # end-to-end: simulate 3 orbits, ingest, screen, summarise
```

Completion means the relevant command above ran and passed. Schema changes
additionally require `alembic upgrade head` against a live database.

### Judgment boundaries

Stop and ask before:

- adding, removing, or renaming a table or column — the schema doc is the
  contract, and telemetry is evidence that is never deleted;
- weakening a uniqueness constraint — `uq_telemetry_sat_seq` and
  `uq_alerts_dedupe` are what make ingestion and alerting safe to retry;
- changing alert severity thresholds or the episode model in
  `src/mgs/worker/screener.py` — both decide what a human gets paged for;
- putting two measures with different units on one chart. Three separate charts
  is deliberate: a shared y-axis across volts, degrees and dBm would invent a
  correlation the data does not contain.

### Navigation

A graphify knowledge graph is built for this repository. Prefer
`graphify query "<question>"` over grepping; see `docs/agents.md`.

