# 0006 Retention deletes old telemetry; partitioning is not available

Date: 2026-09-08

## Status

Accepted

## Context

`telemetry` is append-only and nothing removed anything. Measured on real data:
**2,280 bytes per frame** including indexes.

| Recording cadence | Frames/day/satellite | One year |
|---|---|---|
| 30 s (default) | 2,880 | ~2.3 GB |
| 1 s | 86,400 | ~70 GB |

The obvious answer is to partition by `recorded_at` and drop old partitions,
which turns a delete into a metadata operation.

**PostgreSQL does not allow it here.** Every unique constraint on a partitioned
table must contain the partition key:

```
ERROR:  unique constraint on partitioned table must include all partitioning columns
DETAIL:  UNIQUE constraint on table "part_probe" lacks column "at"
         which is part of the partition key.
```

So `UNIQUE (satellite_id, seq)` would have to become
`UNIQUE (satellite_id, seq, recorded_at)` — and that constraint is exactly what
makes ingestion idempotent. Widening it means the same frame, re-sent with a
different recorded timestamp, inserts twice. Partitioning would buy cheaper
deletes by giving up exactly-once ingestion.

## Decision

Delete rather than partition. `mgs-retention` applies a policy once and exits,
meant for cron or a scheduled container.

Two rules bound what it may remove:

1. **A frame an alert cites is evidence and is never deleted.** An alert
   reading "battery at 6.2 V on frame 4200" is worthless once frame 4200 is
   gone. The foreign key is already `ON DELETE RESTRICT`; the query simply
   never selects such a frame rather than discovering the constraint at run
   time.
2. **Unscreened frames are never deleted.** They are work in progress; losing
   telemetry nobody has examined is worse than keeping it.

Passes and alerts are not swept: they are the audit trail, they are small, and
they summarise the frames that have gone.

`MGS_RETENTION_DAYS` defaults to `0`, meaning keep everything. Silently
deleting a mission's telemetry because nobody set a variable would be
indefensible.

Deletes run in batches with a commit each, so a long purge frees space as it
goes instead of holding one transaction across the table. A new index on
`recorded_at` serves the sweep — the existing
`(satellite_id, recorded_at DESC)` cannot, because retention scans by age
across every satellite.

## Alternatives Considered

1. **Partition by `recorded_at`.** Blocked, as above.
2. **Partition and move idempotency into application logic.** Trades a
   database guarantee for code that has to be right on every path, to solve a
   problem that appears at a scale this project is nowhere near.
3. **Roll old telemetry up into summaries before deleting.** Genuinely useful
   and a larger feature: it needs a decision about which statistics survive.
   Nothing here forecloses it.
4. **Keep everything.** Fine at 2.3 GB a year, not fine at 70, and "fine until
   it isn't" is how a disk fills at three in the morning.

## Consequences

Positive:

- Storage is bounded by a policy someone chose, and every alert can still
  point at the frame that caused it.
- Repeating a purge is safe, and a dry run reports without touching anything.

Tradeoffs:

- Deleting rows is more expensive than dropping a partition, and leaves the
  space to be reclaimed by autovacuum rather than returned at once.
- Frames cited by alerts accumulate indefinitely. That is the intended
  behaviour, and it means retention does not give a hard upper bound on size.
- One more index to maintain on an append-only, write-heavy table.

## Follow-Up

- Revisit if a fleet ever pushes sustained thousands of frames per second: at
  that point the trade between partitioning and idempotency deserves a fresh
  look, most likely by giving frames a natural key that includes time.
