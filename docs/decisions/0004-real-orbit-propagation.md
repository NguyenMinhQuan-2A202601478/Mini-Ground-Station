# 0004 The orbit is propagated, not invented

Date: 2026-09-08

## Status

Accepted

## Context

The first simulator flew a hand-rolled circular orbit. It had to cheat to be
useful: `anchor_offset_deg` shifted the ground track so that the station got a
pass on *every* orbit. Without that shift the station saw nothing, because a
circular-orbit toy has no idea where the satellite actually is.

The cheat quietly deleted the most important fact about operating a ground
station. A single station sees a low-Earth satellite for about **1% of the
day**: a handful of passes, minutes each, in clusters separated by hours of
silence. Every real design pressure — the on-board recorder, the store-and-
forward downlink, deciding which pass is worth staffing — comes from that
silence. A simulator that hands you a pass every ninety minutes is not a
smaller version of the problem; it is a different problem.

## Decision

Propagate the real orbit with **SGP4** against a published **TLE**, via
Skyfield. Everything geometric now comes from that: sub-satellite point,
elevation, azimuth, slant range, range rate, contact windows, and Doppler.

- **Element sets** are resolved in order of trust: an explicit `--tle-file`, a
  fresh download with `--fetch-tle`, a previous download from the cache, and
  finally a real element set committed to the repository. The bundled one is
  the floor so the simulator, the tests, and CI all run offline.
- **Eclipse** uses the low-precision solar-position series from the
  Astronomical Almanac plus a cylindrical shadow test, rather than a planetary
  ephemeris. It is accurate to about 0.01°, and it avoids shipping 17 MB to
  answer "is the spacecraft in the Earth's shadow".
- **Received power** comes from the link geometry — free-space path loss at
  437 MHz plus an elevation-dependent atmospheric term — instead of a fudge
  factor scaled off elevation.
- **Simulated time** runs faster than the clock (`--time-scale`). Real geometry
  at real speed means waiting six hours between passes.

The spacecraft's battery, thermal, and fault behaviour is still a model. It is
now driven by the real orbital period and real eclipse timing.

## Alternatives Considered

1. **Keep the toy model.** Cheap, and every property worth demonstrating —
   backlog, gaps, pass quality — is one the toy cannot produce.
2. **`sgp4` alone, and hand-write the coordinate transforms.** Fewer
   dependencies, and TEME→ECEF→topocentric is exactly the kind of code that is
   subtly wrong for months. Skyfield is the tested version of that work.
3. **Fetch a TLE at startup, always.** Then the tests need a network, CI needs
   egress, and a Celestrak outage breaks the demo. Refreshing the element set
   is now a deliberate flag and a reviewable commit.
4. **Skyfield's `is_sunlit()`.** Correct, and it wants a 17 MB ephemeris to
   decide a question a 20-line shadow test answers well enough.

## Consequences

Positive:

- Contact windows are the real ones: four passes over Hanoi in the demo day,
  between 2 and 8 minutes, from 5.5° to 55.7° maximum elevation.
- Pass *quality* now matters, and shows: the 2-minute 5.5° graze cleared 235
  frames off the recorder; the 8-minute 55.7° pass cleared 733.
- The recorder can overflow during a long silence, which produces genuine data
  gaps rather than only simulated packet loss.
- The element set is perishable and says so: the simulator warns when the TLE
  is more than a fortnight from its epoch, because SGP4 drifts about a
  kilometre a day.

Tradeoffs:

- Skyfield, sgp4 and numpy are now required, not optional. Numpy was already
  pulled in by the `ml` extra; the orbit is not optional, so neither is it.
- A run's passes depend on the element set and the start time. Reproducing an
  exact run needs `--tle-file` and `--start`, not just `--seed`.
- The bundled TLE ages. It is fine for demonstrating geometry and useless for
  pointing a real antenna; `--fetch-tle` exists for that.

## Follow-Up

- Refresh the bundled element set when it drifts far from useful, or drop it in
  favour of a fetch-and-cache-only policy if the project ever gains a network
  dependency for other reasons.
