# 0003 The dashboard draws the worker's own limits

Date: 2026-09-07

## Status

Accepted

## Context

The dashboard plots battery voltage and temperature with threshold lines. The
obvious implementation hard-codes 6.8 V and 60 °C in the page, because those
are the defaults in `.env.example`.

The worker reads its limits from `Settings`, which reads the environment. So an
operator who raises `MGS_BATTERY_MIN_V` for a degraded battery would get a
chart that draws a line where alerts *used* to fire — the chart and the pager
disagreeing, silently, in the one place where being wrong matters.

## Decision

`GET /api/v1/summary` returns a `limits` object built from the same `Settings`
instance the worker screens against, and the dashboard draws its threshold
lines from that response. Nothing about a limit is written twice.

## Alternatives Considered

1. **Hard-code the lines in the page.** One less field; two sources of truth.
2. **A separate `/limits` endpoint.** Correct, but the dashboard already calls
   `/summary` on every refresh, and a second request buys nothing.
3. **Store limits per satellite in the database.** The right answer once
   different spacecraft need different limits. Today there is one satellite and
   no table to hang them on; adding one now would be inventing a product
   decision that has not been made.

## Consequences

Positive:

- A threshold line and the rule that raises the alert cannot drift apart.
- Changing a limit is one environment variable, and both processes follow.

Tradeoffs:

- `/summary` now carries configuration as well as state. Acceptable while there
  is one set of limits for the whole station; when limits become per-satellite
  they move to their own table and their own endpoint.

## Follow-Up

- ~~Revisit when a second spacecraft with different limits appears — see
  alternative 3.~~ Done: [`0007`](0007-per-satellite-limits.md) adds the
  `satellites` table with nullable limit overrides. `/summary` still reports
  the limits the worker screens against; it now resolves them per spacecraft.
