# 0005 A shared key guards the writes; it is not operator identity

Date: 2026-09-08

## Status

Accepted

## Context

Every endpoint that changed something was open to anyone who could reach the
port: four write paths, no authentication anywhere in `src/mgs/api/routes/`.

Ranked by what an unauthenticated caller could actually do:

1. `POST /alerts/{id}/ack` — **resolve every open alert**. The board goes
   green while the spacecraft is draining its battery. Silencing the alarm is
   worse than never having raised it.
2. `POST /telemetry` — inject plausible frames. The statistical detector is
   fitted on the history it has screened, so a patient attacker can *teach it*
   that an anomaly is normal.
3. `POST /passes` — open a pass that the partial unique index then makes
   impossible for the real one to open.

## Decision

An `X-API-Key` header, checked with `secrets.compare_digest` against a
comma-separated list in `MGS_API_KEYS`, is required on all five writing
endpoints. Reads stay open: the dashboard is a read-only view of station
state, and gating it behind a key would only push the key into the page.

With no keys configured the check passes, the API logs a warning at startup,
and `/health` reports `"auth": "disabled"`. A bare `git clone` still runs; an
unauthenticated deployment is visible rather than silent. The packaged compose
stack sets a key by default, so what ships is authenticated.

The dashboard's acknowledge button asks the operator for the key on the first
401 and keeps it in `localStorage`.

## What this deliberately is not

**Operator identity.** Every holder of the key is the same principal, so
`acknowledged_at` records that *someone* acknowledged an alert and can never
record *who*. On a real station that is a gap: acknowledging an alert is a
human act with a name attached to it.

Building a fake version of that — a `user` column filled in by a client that
asserts its own identity — would be worse than the honest gap, because it
would read like an audit trail without being one. Real accounts mean sessions,
password or SSO handling, and a permission model, and that is its own piece of
work.

## Alternatives Considered

1. **Leave writes open, rely on network placement.** True for a station on an
   isolated LAN and false the first time someone port-forwards it for a demo.
2. **Require a key with no disabled mode.** Safer default; breaks `git clone
   && make demo` and every test, so the key would end up committed as a
   constant, which is the same exposure with more ceremony.
3. **mTLS.** Right answer for a real spacecraft link, and disproportionate for
   a project whose feed is a Python script on the same host.
4. **Full user accounts now.** The correct destination. Too large to bundle
   into "close the open write endpoints", and starting it badly is costly.

## Consequences

Positive:

- The three attacks above need the key.
- Several keys are accepted at once, so a feed's key can be rotated without an
  outage.
- `/health` answers "is this station open?" without a request to a write path.

Tradeoffs:

- One shared secret: it cannot be revoked per client, and every holder looks
  identical in the audit trail.
- The dashboard stores the key in `localStorage`, so it is readable by anyone
  at that browser profile.
- The compose default `dev-station-key` is public in this repository. It is
  named to be obvious, and the file says to change it.

## Follow-Up

- Operator accounts, so `acknowledged_at` can be joined to a person.
- Per-client keys with revocation, once there is more than one feed.
