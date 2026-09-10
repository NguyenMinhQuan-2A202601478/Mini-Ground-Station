"""Resolving the operating limits that apply to one spacecraft.

Limits used to be a station-wide setting. They are still the default, but a
spacecraft can override any of them: a battery that has aged is not a battery
that is failing, and the floor that distinguishes the two belongs to that
satellite, not to the station that happens to be listening.

A `NULL` override means "use the station default", so a satellite row nobody
has edited behaves exactly as it did before this existed.
"""

from __future__ import annotations

from mgs.config import Settings
from mgs.models import Satellite
from mgs.schemas import Limits

FIELDS = ("battery_min_v", "battery_critical_v", "temp_max_c", "temp_min_c")


def station_defaults(settings: Settings) -> Limits:
    return Limits(**{field: getattr(settings, field) for field in FIELDS})


def resolve(satellite: Satellite | None, settings: Settings) -> Limits:
    """The limits to screen this spacecraft against."""
    defaults = station_defaults(settings)
    if satellite is None:
        return defaults
    return Limits(
        **{
            field: (
                override
                if (override := getattr(satellite, field)) is not None
                else getattr(defaults, field)
            )
            for field in FIELDS
        }
    )
