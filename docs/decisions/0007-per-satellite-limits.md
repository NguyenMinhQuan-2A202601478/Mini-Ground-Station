# 0007 A satellites table, because limits belong to the spacecraft

Date: 2026-09-10

## Status

Accepted. Completes the follow-up left open in
[`0003`](0003-dashboard-reads-the-workers-limits.md).

## Context

`satellite_id` was bare text on all three tables with nothing behind it. The
schema document said why, and said what would change that:

> There is no `satellites` table yet because nothing needs one; when spacecraft
> metadata (TLE, operator, commissioning date) appears, it becomes a fourth
> table and these columns become FKs.

What actually needed it was none of those things. It was the **limits**.

`0003` put the operating limits in `Settings` and had the dashboard read them
from the API so a chart could never disagree with the rule that pages someone.
That was right, and it was station-wide. But a battery that has aged is not a
battery that is failing. A five-year-old spacecraft that now rests at 6.5 V is
healthy; a new one at 6.5 V is in trouble. Screening both against one number
means either paging constantly about the old one or missing the new one.

That distinction belongs to the spacecraft. There was nowhere to put it.

## Decision

A `satellites` table, keyed by the `satellite_id` the other tables already
carried, holding identity (`name`, `catalog_number`, `operator`) and **nullable
limit overrides**. `NULL` means "use the station default", so a row nobody has
edited screens exactly as before.

The worker resolves limits per satellite before screening its batch;
`GET /api/v1/summary` reports the resolved limits for the satellite in scope,
so the dashboard's threshold lines follow the same rule `0003` established.
`PUT /api/v1/satellites/{id}` edits them, and carries the station key like
every other write — changing a limit changes what someone gets paged for.

**Rows register themselves.** A frame from an unknown spacecraft inserts one
rather than being rejected. Telemetry that has already been received is not
something to throw away over a missing configuration row, and a pass already
opens itself on first contact for exactly the same reason.

`satellite_id` stays denormalised on all three tables as well as being a
foreign key. The worker still never joins to screen a frame.

## Alternatives Considered

1. **Keep limits station-wide.** Simplest, and it makes a multi-spacecraft
   station choose between a floor that is wrong for the old bird and one that
   is wrong for the new one.
2. **Reject telemetry from unregistered satellites.** The foreign key would
   enforce a real invariant. It would also drop data that has already been
   received, permanently, because of a missing row. Not a trade worth making
   in a system whose whole design is about not losing frames.
3. **A surrogate integer primary key.** Conventional, and it would mean
   rewriting `satellite_id` in three tables and migrating every existing row
   for no gain. The text identifier is stable, short, and already the natural
   key everywhere.
4. **Store the TLE here too.** Tempting — the API could then predict passes
   server-side. It would also create a second source of truth against the
   simulator's element-set file. Deferred until something actually reads it.
5. **Per-limit rows in a generic key/value table.** Flexible, unqueryable,
   uncheckable. Four nullable columns and two CHECK constraints say more.

## Consequences

Positive:

- Two spacecraft, two verdicts on identical telemetry. The tests screen 6.5 V
  as `BATTERY_LOW` for one satellite and as normal for another.
- The database refuses an inverted pair (`battery_critical_v` above
  `battery_min_v`), so a rule nothing can satisfy cannot be stored. The API
  checks first and answers 422; the constraint is the backstop.
- `GET /api/v1/satellites` now answers from a real table with counters and
  effective limits, instead of a `GROUP BY` over telemetry.
- The three foreign keys mean a spacecraft with history cannot be deleted.

Tradeoffs:

- Ingestion writes one extra `INSERT … ON CONFLICT DO NOTHING` per frame. It
  is an index probe on an already-hot primary key, and ingestion is a single
  transaction per batch regardless.
- Limits now live in two places — `Settings` for the station, the table for the
  exceptions. Reading the effective value means going through `mgs.limits`
  rather than reaching for `settings.battery_min_v` directly.
- A satellite that registers itself is a row nobody chose to create. A typo in
  a `satellite_id` becomes a spacecraft rather than an error.

## Follow-Up

- Fold the element set in here if the API ever predicts passes itself
  (alternative 4).
- A way to merge or retire a satellite registered by a typo.
