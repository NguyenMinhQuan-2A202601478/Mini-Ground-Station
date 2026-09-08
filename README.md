# mini-ground-station

A miniature satellite ground station. A simulated spacecraft flies an orbit and
downlinks telemetry while it is in view; a FastAPI service ingests those frames
into PostgreSQL; a background worker screens them and raises alerts.

```
simulated satellite  ──HTTP──>  FastAPI ingestion  ──>  PostgreSQL
   (orbit + health model)          (passes, telemetry)      │
                                                            ├──> anomaly worker ──> alerts
                                                            └──> query API ──> dashboard
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
| `src/mgs/api/static/` | The dashboard — one HTML page, no build step |
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

Then open **<http://localhost:8000/>** — the dashboard: live battery, temperature
and signal charts with the worker's own limits drawn on them, the passes it
heard, and the open alerts with an acknowledge button. Or from the shell:

```bash
curl -s localhost:8000/api/v1/passes | jq
curl -s 'localhost:8000/api/v1/alerts?open_only=true' | jq
curl -s 'localhost:8000/api/v1/telemetry/series?buckets=20' | jq
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

## The dashboard

One page at `/`, served by the same FastAPI app, no build step and no
dependencies to install. Three separate charts rather than one with three
y-axes: volts, degrees and dBm share no scale, and overlaying them would invent
a correlation that is not in the data.

- Threshold lines come from `GET /api/v1/summary`, which reports the worker's
  live limits — a line on a chart can never disagree with the rule that pages
  someone.
- Shaded bands mark the passes, so you can see the signal strength rise as the
  satellite climbs above the horizon.
- Charts are drawn from `GET /api/v1/telemetry/series`, which buckets and
  aggregates **in PostgreSQL**. A 900-pixel chart has no use for 50,000 rows.
- Every chart has a table view (the "Table view" button), a crosshair tooltip,
  and keyboard navigation with the arrow keys.

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
