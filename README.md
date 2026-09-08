# mini-ground-station

A miniature satellite ground station. A simulated spacecraft flies an orbit and
downlinks telemetry while it is in view; a FastAPI service ingests those frames
into PostgreSQL; a background worker screens them and raises alerts.

```
simulated satellite  ──HTTP──>  FastAPI ingestion  ──>  PostgreSQL
   (orbit + health model)          (passes, telemetry)      │
                                                            ├──> anomaly worker ──> alerts
                                                            └──> query API / dashboard
```

The hardware and RF layers are simulated. Everything from the ingestion API
downwards is the real thing.

## Layout

| Path | What it is |
|---|---|
| `docs/product/schema.md` | **Read this first** — the authoritative data model |
| `src/mgs/models.py` | SQLAlchemy tables: `passes`, `telemetry`, `alerts` |
| `src/mgs/ingest.py` | Idempotent ingestion service (shared by API and tests) |
| `src/mgs/api/` | FastAPI app and routes |
| `src/mgs/worker/` | Threshold rules, statistical detector, screening loop |
| `src/mgs/simulator/` | Orbit propagation and spacecraft health model |
| `migrations/` | Alembic migrations |

## Quick start

```bash
cp .env.example .env
docker compose up -d db          # PostgreSQL on localhost:5433
uv venv --python 3.12 && uv pip install -e '.[ml,dev]'
source .venv/bin/activate
alembic upgrade head             # create the three tables
```

Then run the three processes, each in its own terminal:

```bash
mgs-api                          # http://localhost:8000/docs
mgs-worker                       # screens telemetry, writes alerts
mgs-sim --orbits 3               # flies 3 orbits and downlinks
```

Watch what came out:

```bash
curl -s localhost:8000/api/v1/passes | jq
curl -s 'localhost:8000/api/v1/alerts?open_only=true' | jq
```

`make demo` does all of it in one shot.

## The three tables

- **`passes`** — one contact window, AOS to LOS. A partial unique index makes
  "at most one open pass per satellite" a database guarantee, so attaching a
  frame to the current pass is never ambiguous.
- **`telemetry`** — one downlinked frame, append-only. `UNIQUE (satellite_id,
  seq)` makes ingestion idempotent: a retried POST returns the original row.
  `screened_at` is the worker's cursor, which is why there is no fourth
  bookkeeping table.
- **`alerts`** — one detected problem, with a `dedupe_key` unique index, so
  re-running the worker over the same frames never duplicates an alert.

Full reasoning: [`docs/product/schema.md`](docs/product/schema.md).

## What the worker detects

| Rule | Trigger |
|---|---|
| `BATTERY_LOW` | voltage below `MGS_BATTERY_MIN_V`, critical below `MGS_BATTERY_CRITICAL_V` |
| `TEMP_HIGH` / `TEMP_LOW` | outside `MGS_TEMP_MIN_C … MGS_TEMP_MAX_C` |
| `SAFE_MODE` | spacecraft reported SAFE mode |
| `DATA_GAP` | on-board sequence numbers jumped — frames lost on the downlink |
| `ML_OUTLIER` | IsolationForest over a rolling window, z-score while cold-starting |

The threshold rules need no dependencies. The statistical layer needs the `ml`
extra; without it the worker degrades to z-score rather than failing.

## Tests

```bash
pytest                           # unit tests
pytest -m integration            # needs the database up
```

## Agent tooling

This repository carries [repository-harness](https://github.com/hoangnb24/repository-harness)
(`AGENTS.md`, `docs/WORKFLOW.md`, `.agents/skills/`) and a project-scoped
[graphify](https://github.com/Graphify-Labs/graphify) skill. See
[`docs/agents.md`](docs/agents.md).
