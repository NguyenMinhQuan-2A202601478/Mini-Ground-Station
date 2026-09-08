# Architecture

Three processes and one database. They share the schema and nothing else.

```
┌──────────────────┐   HTTP POST /api/v1/telemetry
│  mgs-sim         │──────────────────────────────┐
│  simulated       │                              │
│  spacecraft      │   POST /api/v1/passes        │
└──────────────────┘──────────────────────────────┤
                                                  v
                                        ┌───────────────────┐
                                        │  mgs-api          │
                                        │  FastAPI          │
                                        │  ingest + query   │
                                        └─────────┬─────────┘
                                                  │ SQLAlchemy
                                                  v
                                        ┌───────────────────┐
                                        │  PostgreSQL       │
                                        │  passes           │
                                        │  telemetry        │
                                        │  alerts           │
                                        └─────────┬─────────┘
                                                  │ poll unscreened
                                                  v
                                        ┌───────────────────┐
                                        │  mgs-worker       │
                                        │  rules + detector │
                                        └───────────────────┘

The dashboard at `/` is served by mgs-api and reads the same query endpoints
anyone else would: /summary, /telemetry/series, /passes, /alerts.
```

## Why the worker is a separate process

Screening must not sit in the request path. A pass is minutes long and the
downlink does not wait: if an IsolationForest refit made a POST slow, the
ground station would drop frames it can never get back. Ingestion therefore
does the least possible work — validate, insert, return — and everything
analytical happens afterwards, driven by the `screened_at IS NULL` queue.

The consequence to accept: an alert appears seconds to minutes after the frame
that caused it. For spacecraft health on a store-and-forward link, where the
data itself is already an orbit old, that latency is free.

## Why there is no message queue

The queue is a partial index on a column. `fetch_unscreened` selects
`WHERE screened_at IS NULL ... FOR UPDATE SKIP LOCKED`, which gives at-least-once
delivery and crash recovery without a second piece of infrastructure to run and
reason about. Adding Kafka or Celery here would add operational surface without
changing a single guarantee.

Screening itself is **single-flight**, guarded by a PostgreSQL advisory lock
held for the batch's transaction. It has to be: deciding whether this frame
continues the episode the last one opened is a sequential fold over the stream,
so two workers splitting the frames between them would each see a fragment and
open an episode for it. Ingestion stays fully parallel; a worker that dies
mid-batch drops its connection, and the lock with it. Running a second worker
therefore buys redundancy, not throughput.

The limit of this choice: it is bounded by what one PostgreSQL can index. At a
few hundred frames per pass, it is not close.

## Two clocks

Telemetry carries `recorded_at` (the spacecraft's clock, when the measurement
was taken) and `received_at` (the ground clock, when the frame arrived). A
store-and-forward satellite records all the way round its orbit and dumps the
backlog during a contact window, so these differ by up to an orbit.

Everything that describes the spacecraft is timestamped in on-board time —
including alerts, whose `detected_at` is the frame's `recorded_at`, not the
moment the worker got round to screening it. Everything that describes the
ground station — a pass's `aos_at` and `los_at` — is ground time.

## Idempotency

Both write paths are safe to retry, and the database, not the application, is
what enforces it:

- `UNIQUE (satellite_id, seq)` on `telemetry` — a re-sent frame returns the
  original row instead of inserting a second copy.
- `UNIQUE (dedupe_key)` on `alerts` — re-screening the same frames produces no
  new alerts.

This matters because the simulator's downlink, like a real one, retries: a
timeout on the ground does not tell you whether the insert happened.

## Alerts are episodes

A battery under its floor for three hundred frames is one problem. The screener
opens an alert on the first frame of a condition, escalates the same row if the
condition worsens, and resolves it on the frame where it clears. Only rules that
describe a genuine instant — `DATA_GAP`, where frames were lost between two
sequence numbers — stay one alert per frame.

Without this, three orbits produced 1,324 alerts from 1,172 frames, which is the
same as producing none. With it, the same data yields around 60. See
[`decisions/0001-alerts-are-episodes.md`](decisions/0001-alerts-are-episodes.md).

## The dashboard aggregates in the database

`GET /api/v1/telemetry/series` buckets telemetry with `width_bucket` and returns
one row per bucket with avg/min/max. The alternative — ship every frame and let
the browser reduce it — makes the page slower every day the mission runs, and
the reduction has to happen somewhere regardless.

Returning avg *and* min/max per bucket is what keeps the downsampling honest: a
one-frame voltage collapse inside a bucket still shows, as the band around the
average, instead of being averaged out of existence.

## Detection in two tiers

`worker/rules.py` holds documented limits: an operator can point at the number
that was crossed. `worker/detector.py` adds an IsolationForest over a rolling
window for combinations nobody wrote a limit for, falling back to a per-feature
z-score when there is too little history or scikit-learn is not installed. The
statistical tier only ever *adds* findings; it can never suppress a threshold
rule.
