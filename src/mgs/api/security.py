"""API-key authentication for the endpoints that change something.

What this is: a shared secret that stops an unauthenticated passer-by from
injecting telemetry, opening passes, or silencing alerts. It is the right shape
for machine-to-machine writes — a ground station front end has a key in its
config, not a login.

What it is not: per-operator identity. Every holder of the key is
indistinguishable in the audit trail, so `acknowledged_at` records *that*
someone acknowledged an alert and never *who*. Real operator accounts are a
different piece of work; see `docs/decisions/0005`.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import Depends, Header, HTTPException, status

from mgs.config import Settings, get_settings

log = logging.getLogger("mgs.api.security")

HEADER = "X-API-Key"


def configured_keys(settings: Settings) -> set[str]:
    return {key.strip() for key in settings.api_keys.split(",") if key.strip()}


def require_api_key(
    x_api_key: str | None = Header(default=None, alias=HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject the request unless it carries a configured key.

    With no keys configured the check passes and the API says so loudly at
    startup and on `/health`. That keeps a bare local checkout runnable while
    making an unauthenticated deployment something you can see rather than
    something you have to remember.
    """
    keys = configured_keys(settings)
    if not keys:
        return

    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing {HEADER}",
            headers={"WWW-Authenticate": HEADER},
        )

    # compare_digest, not ==, so a wrong key cannot be found one character at a
    # time by timing the rejection.
    if not any(secrets.compare_digest(x_api_key, known) for known in keys):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
