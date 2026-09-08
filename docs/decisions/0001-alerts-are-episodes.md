# 0001 Alerts describe episodes, not frames

Date: 2026-09-07

## Status

Accepted

## Context

The first screening implementation raised one alert per frame per broken rule.
Run against three simulated orbits — 1,172 frames — it produced **1,324
alerts**, of which 709 were `SAFE_MODE` and 571 were `BATTERY_LOW`. The
spacecraft had four actual problems in that window.

A page-per-frame is the same as no paging at all: nobody reads the 300th
notification that the battery is still low, and the two `DATA_GAP` alerts that
described genuinely new events were buried underneath them.

## Decision

An alert represents a **condition episode**. Rules that describe a state the
spacecraft is *in* — `BATTERY_LOW`, `TEMP_HIGH`, `TEMP_LOW`, `SAFE_MODE`,
`ML_OUTLIER` — open one alert on the frame where the condition starts, escalate
that same row if severity worsens, and set `resolved_at` on the frame where it
clears. Only `DATA_GAP` remains one alert per frame, because losing frames
between two sequence numbers really is a single instant.

`ML_OUTLIER` was initially treated as a point event and was the loudest rule
left after the first fix: 142 alerts across three orbits, nearly all of them
consecutive frames from the same two excursions. As episodes, the same data
yields 28.

The open episode is found by querying `alerts` for the satellite and rule where
`resolved_at IS NULL`, so the state survives a worker restart with no extra
table.

Same data after the change: **60 alerts** across three orbits, of which 34 are
episodes and 26 are genuine per-frame downlink gaps — down from 1,324.

## Alternatives Considered

1. **Rate-limit alerts per rule per time window.** Simpler, but it answers
   "how often may we shout" rather than "what is happening", and it cannot tell
   an operator when a condition ended.
2. **Deduplicate in the dashboard.** Leaves the noise in the database, so every
   consumer — API, exports, any future paging integration — has to re-solve it.
3. **Alert only on the transition, never resolve.** Halves the noise but leaves
   every alert permanently open, so "what is currently wrong?" stays unanswerable.

## Consequences

Positive:

- `GET /api/v1/alerts?open_only=true` answers "what is wrong right now".
- An episode carries its own duration: `detected_at` → `resolved_at`.
- Severity escalation is visible in place rather than as a second alert.

Tradeoffs:

- The screener is stateful within a batch and must process a satellite's frames
  in sequence order, so frames cannot be screened in parallel per satellite.
- A condition that flickers across a batch boundary opens a second episode.
  Acceptable: the frames really did show recovery in between.

## Follow-Up

- Consider a minimum episode duration (hysteresis) if flicker proves common in
  real data.
