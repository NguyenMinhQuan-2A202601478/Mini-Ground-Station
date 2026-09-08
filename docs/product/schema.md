# Data Schema — mini-ground-station

Authoritative description of the persisted model. Code in `src/mgs/models.py`
implements exactly this; migrations in `migrations/versions/` apply it.

## Domain in one paragraph

A satellite is only reachable while it flies over the ground station. That
window is a **pass** (from AOS — Acquisition of Signal — to LOS — Loss of
Signal). During a pass the spacecraft downlinks **telemetry** frames. A
background worker screens those frames and raises **alerts** when a value
breaks a rule or looks statistically anomalous.

Three tables, one per noun: `passes`, `telemetry`, `alerts`.

## Table: `passes`

One row per contact window. Created by the ingestion API on the first frame of
a pass (or explicitly by the simulator), closed at LOS.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `satellite_id` | text NOT NULL | e.g. `VNSAT-1`. Indexed. |
| `ground_station_id` | text NOT NULL | e.g. `HANOI-GS` |
| `aos_at` | timestamptz NOT NULL | start of window, **ground clock** |
| `los_at` | timestamptz NULL | NULL ⇒ pass still open |
| `max_elevation_deg` | double NULL | pass quality (0–90) |
| `status` | text NOT NULL | `active` \| `completed` \| `aborted` |
| `frame_count` | int NOT NULL DEFAULT 0 | denormalised counter, cheap dashboards |
| `created_at` | timestamptz NOT NULL DEFAULT now() | |

- `CHECK (los_at IS NULL OR los_at >= aos_at)` — a window cannot close before
  it opens. A pass is timed on the *ground* clock throughout; the frames it
  carries were recorded on the spacecraft's clock, possibly an orbit earlier.
- Index `ix_passes_satellite_aos (satellite_id, aos_at DESC)` — "last N passes".
- Partial unique `uq_passes_open (satellite_id) WHERE los_at IS NULL` — a
  satellite cannot have two open passes at once. This is the invariant that
  makes "attach frame to the current pass" unambiguous.

## Table: `telemetry`

One row per received frame. Append-only; never updated except `screened_at`.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `pass_id` | bigint FK → passes.id NULL | NULL = out-of-pass beacon |
| `satellite_id` | text NOT NULL | denormalised so the worker never joins |
| `seq` | int NOT NULL | on-board frame counter — gap detection |
| `recorded_at` | timestamptz NOT NULL | on-board clock |
| `received_at` | timestamptz NOT NULL DEFAULT now() | ground clock |
| `battery_voltage_v` | double NOT NULL | primary health metric |
| `battery_current_a` | double NULL | negative = discharging |
| `temperature_c` | double NOT NULL | |
| `lat_deg` | double NOT NULL | −90..90 |
| `lon_deg` | double NOT NULL | −180..180 |
| `alt_km` | double NOT NULL | |
| `signal_strength_dbm` | double NULL | link quality at the station |
| `mode` | text NOT NULL DEFAULT `NOMINAL` | `NOMINAL` \| `SAFE` \| `PAYLOAD` |
| `raw` | jsonb NULL | original payload, forward compatibility |
| `screened_at` | timestamptz NULL | set by the worker after scoring |

- Unique `uq_telemetry_sat_seq (satellite_id, seq)` — **idempotent ingestion**.
  A retried POST returns the existing row instead of duplicating it.
- Index `ix_telemetry_sat_recorded (satellite_id, recorded_at DESC)` — charts.
- Partial index `ix_telemetry_unscreened (id) WHERE screened_at IS NULL` — the
  worker's queue. This is why there is no fourth `worker_state` table: the
  cursor lives on the rows themselves, so a worker restart resumes exactly
  where it stopped and a crash never silently skips frames.

## Table: `alerts`

One row per detected problem.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `telemetry_id` | bigint FK → telemetry.id NULL | NULL for pass-level alerts |
| `pass_id` | bigint FK → passes.id NULL | |
| `satellite_id` | text NOT NULL | |
| `rule` | text NOT NULL | `BATTERY_LOW`, `TEMP_HIGH`, `TEMP_LOW`, `DATA_GAP`, `ML_OUTLIER` |
| `severity` | text NOT NULL | `info` \| `warning` \| `critical` |
| `metric` | text NULL | which field tripped, e.g. `battery_voltage_v` |
| `value` | double NULL | observed value |
| `threshold` | double NULL | limit that was crossed |
| `score` | double NULL | anomaly score (ML rules) |
| `message` | text NOT NULL | human-readable |
| `detected_at` | timestamptz NOT NULL DEFAULT now() | |
| `acknowledged_at` | timestamptz NULL | operator saw it |
| `resolved_at` | timestamptz NULL | condition cleared |
| `dedupe_key` | text NOT NULL | UNIQUE |

- Unique `uq_alerts_dedupe (dedupe_key)`. The key is
  `{satellite_id}:{rule}:{telemetry_id}` for frame-level rules and
  `{satellite_id}:{rule}:{pass_id}:{seq_gap}` for pass-level ones. Re-running
  the worker over the same data produces **no duplicate alerts**; the insert is
  `ON CONFLICT DO NOTHING`.
- Index `ix_alerts_open (satellite_id, detected_at DESC) WHERE resolved_at IS NULL`.

## Relationships

```
passes 1 ──< telemetry 1 ──< alerts
   └──────────────< alerts (pass-level)
```

`ON DELETE` is `RESTRICT` everywhere: telemetry is evidence and is not deleted
behind an alert that cites it.

## Decisions worth knowing

1. **Timestamps are all `timestamptz`, stored UTC.** Space work crosses
   timezones; naive datetimes are a bug factory.
2. **`satellite_id` is denormalised onto every table.** There is no
   `satellites` table yet because nothing needs one; when spacecraft metadata
   (TLE, operator, commissioning date) appears, it becomes a fourth table and
   these columns become FKs.
3. **Screening state is a column, not a table.** See `screened_at` above.
4. **Idempotency is enforced by the database, not by application logic.** Both
   ingestion and alerting are safe to retry.
