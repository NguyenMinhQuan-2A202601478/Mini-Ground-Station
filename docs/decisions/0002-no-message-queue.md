# 0002 The screening queue is a partial index, not a broker

Date: 2026-09-07

## Status

Accepted

## Context

The ingestion API and the anomaly worker are separate processes. Something has
to hand frames from one to the other, and the obvious reflex is a message
broker.

## Decision

`telemetry.screened_at` is the queue. The worker claims work with

```sql
SELECT ... FROM telemetry WHERE screened_at IS NULL
ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED
```

backed by `ix_telemetry_unscreened`, a partial index on rows where
`screened_at IS NULL`.

## Alternatives Considered

1. **Kafka / RabbitMQ / Redis Streams.** Another service to run, monitor, and
   recover, and the frames would still have to be written to PostgreSQL anyway.
2. **Celery with a Redis broker.** Same objection, plus a second serialisation
   format for the same rows.
3. **A `worker_state` table holding a cursor.** One row per satellite, and a
   crash between "advance cursor" and "write alerts" silently skips frames.

## Consequences

Positive:

- `SKIP LOCKED` gives at-least-once delivery at the row level.
- A crash mid-batch loses nothing: unscreened rows stay unscreened.
- Re-screening is safe because alert inserts are deduplicated by the database.
- One backing service to operate.

Tradeoffs:

- Screening is single-flight: an advisory lock lets one worker screen at a
  time, because episode reconciliation is a sequential fold over the stream
  (see `docs/ARCHITECTURE.md`). Extra worker processes buy redundancy, not
  throughput.
- Throughput is bounded by what one PostgreSQL can index. At a few hundred
  frames per pass this is not close, and the decision should be revisited if a
  fleet ever pushes sustained thousands of frames per second.
- Polling adds up to one `MGS_POLL_INTERVAL_SECONDS` of latency versus a push.
