# mini-ground-station

A miniature satellite ground station. A simulated spacecraft flies a **real
orbit** — SGP4 against a published TLE — and downlinks telemetry during the
contact windows that geometry actually gives it; a FastAPI service ingests
those frames into PostgreSQL; a background worker screens them and raises
alerts.

```
simulated satellite  ──HTTP──>  FastAPI ingestion  ──>  PostgreSQL
   (orbit + health model)          (passes, telemetry)      │
                                                            ├──> anomaly worker ──> alerts
                                                            └──> query API ──> dashboard
```

The hardware and RF layers are simulated. The orbital mechanics are not, and
everything from the ingestion API downwards is the real thing.

## Layout

| Path | What it is |
|---|---|
| `docs/product/schema.md` | **Read this first** — the authoritative data model |
| `src/mgs/models.py` | SQLAlchemy tables: `passes`, `telemetry`, `alerts` |
| `src/mgs/ingest.py` | Idempotent ingestion service (shared by API and tests) |
| `src/mgs/api/` | FastAPI app and routes |
| `src/mgs/api/static/` | The dashboard — one HTML page, no build step |
| `src/mgs/worker/` | Threshold rules, statistical detector, screening loop |
| `src/mgs/simulator/` | SGP4 propagation, TLE handling, spacecraft health model |
| `migrations/` | Alembic migrations |
| `Dockerfile`, `docker-compose.yml` | One image, three processes, one command |
| `.github/workflows/ci.yml` | Lint, tests, and a container smoke run |

## Quick start — containers

Nothing to install but Docker:

```bash
make up          # database, migrations, API, worker
```

Then open **<http://localhost:8000/>**. Migrations run as their own container
that must exit cleanly before the API or the worker start, so nothing ever
races a half-applied schema.

Feed it some telemetry:

```bash
make stack-demo  # two orbits, screened, with a summary of what was found
```

`docker compose --profile demo up -d sim` runs the simulator continuously
instead. `make down` stops everything and keeps the data; `make logs` follows
all three processes.

## Quick start — local checkout

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
mgs-sim --duration 24h           # flies a day of real passes, compressed
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

## The orbit is real

A single ground station sees a low-Earth satellite for about **1% of the day**.
That is the fact the whole design turns on, and it is the one a toy circular
orbit cannot produce — the first version of this simulator had to shift the
ground track over the station to manufacture a pass every orbit, which quietly
deleted the problem.

Now positions come from SGP4 against a published element set, and a demo day
over Hanoi looks like this:

```
predicted pass  AOS 10:27:55   1.9 min  max elevation  5.5°
predicted pass  AOS 12:01:06   8.4 min  max elevation 55.7°
predicted pass  AOS 20:15:53   6.4 min  max elevation 13.8°
predicted pass  AOS 21:51:58   7.8 min  max elevation 27.0°
```

Four windows, two clusters, thirteen hours of silence in the middle — and pass
quality decides how much data comes down. In that run the 1.9-minute graze
cleared 235 frames off the recorder; the 8.4-minute overhead pass cleared 733.
When a silence outlasts the recorder, the oldest frames are overwritten and
show up on the ground as a `DATA_GAP`.

Everything geometric follows from the propagation: slant range, azimuth and
elevation, range rate, Doppler shift (±9 kHz at 437 MHz), eclipse, and received
power from free-space path loss. Reasoning:
[`decisions/0004`](docs/decisions/0004-real-orbit-propagation.md).

### Element sets

The repository ships a real ISS element set so the simulator, the tests, and CI
all run offline. It ages — SGP4 drifts about a kilometre a day from its epoch —
and the simulator says so when it is more than a fortnight old.

```bash
mgs-sim --fetch-tle                     # a current element set from Celestrak
mgs-sim --catalog-number 43013          # some other satellite
mgs-sim --tle-file ./mysat.tle          # your own
mgs-sim --station-lat 10.82 --station-lon 106.63 --station-id SAIGON-GS
```

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

## Tests and CI

```bash
make test                        # 61 tests; the integration ones need `make db-up`
make lint                        # ruff check + format --check
```

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs three jobs on every
push and pull request:

| Job | What it proves |
|---|---|
| **Lint** | `ruff check` and `ruff format --check` over `src` and `tests` |
| **Tests** | the whole suite against a real PostgreSQL service |
| **Container stack** | builds the image, brings the stack up, flies two orbits, screens them, and asserts frames, passes and alerts actually landed — then re-screens the same frames and asserts no alert was duplicated |

The third job is the one that earns its keep: it caught a crash and then a
duplicate-alert bug on re-screening that the unit tests missed, because both
only appear once an episode has been closed and replayed.

Dependencies are pinned in `uv.lock`, and both CI and the image install with
`uv sync --frozen`, so a build resolves nothing.

## Agent tooling

This repository carries [repository-harness](https://github.com/hoangnb24/repository-harness)
(`AGENTS.md`, `docs/WORKFLOW.md`, `.agents/skills/`) and a project-scoped
[graphify](https://github.com/Graphify-Labs/graphify) skill. See
[`docs/agents.md`](docs/agents.md).
