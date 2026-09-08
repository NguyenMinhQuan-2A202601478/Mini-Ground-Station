"""Two-line element sets: loading, validating, and refreshing them.

A TLE is the real orbit description a ground station works from. It is also
perishable: SGP4 drifts by roughly a kilometre a day away from the element
set's epoch, so a month-old TLE will point the dish at empty sky.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("mgs.simulator.tle")

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={catalog_number}&FORMAT=TLE"

# A real element set, committed so the project runs — and its tests pass —
# without a network. `mgs-sim --fetch-tle` replaces it with a current one.
BUNDLED_TLE_TEXT = """ISS (ZARYA)
1 25544U 98067A   26250.49846501  .00005306  00000+0  10435-3 0  9991
2 25544  51.6306 252.7093 0004983 115.8922 244.2580 15.49018229584512"""

STALE_AFTER_DAYS = 14


class TLEError(ValueError):
    """The element set is malformed, or its checksum does not hold."""


@dataclass(frozen=True)
class TLE:
    name: str
    line1: str
    line2: str

    @classmethod
    def parse(cls, text: str) -> TLE:
        lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
        if len(lines) == 2:
            name, line1, line2 = "UNNAMED", lines[0], lines[1]
        elif len(lines) >= 3:
            name, line1, line2 = lines[0].strip(), lines[1], lines[2]
        else:
            raise TLEError(f"expected 2 or 3 lines, got {len(lines)}")

        tle = cls(name=name, line1=line1, line2=line2)
        tle.validate()
        return tle

    def validate(self) -> None:
        for number, line in ((1, self.line1), (2, self.line2)):
            if len(line) < 69:
                raise TLEError(f"line {number} is {len(line)} characters, expected at least 69")
            if line[0] != str(number):
                raise TLEError(f"line {number} starts with {line[0]!r}, expected {number!r}")
            expected = _checksum(line[:68])
            actual = line[68]
            if actual != str(expected):
                raise TLEError(
                    f"line {number} checksum is {actual!r}, computed {expected} — "
                    "the element set was corrupted in transit or in editing"
                )
        if self.catalog_number != int(self.line2[2:7]):
            raise TLEError("the two lines describe different catalog numbers")

    @property
    def catalog_number(self) -> int:
        return int(self.line1[2:7])

    @property
    def epoch(self) -> datetime:
        """Epoch from columns 19-32 of line 1: two-digit year, then day-of-year."""
        year = int(self.line1[18:20])
        year += 2000 if year < 57 else 1900  # the Space Age started in 1957
        day_of_year = float(self.line1[20:32])
        return datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=day_of_year - 1)

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or datetime.now(UTC)) - self.epoch

    @property
    def is_stale(self) -> bool:
        return abs(self.age().days) > STALE_AFTER_DAYS

    def warn_if_stale(self) -> None:
        days = self.age().total_seconds() / 86400
        if self.is_stale:
            log.warning(
                "TLE for %s is %.0f days from its epoch — SGP4 drifts about a "
                "kilometre a day, so predicted passes will be off. Refresh with "
                "--fetch-tle.",
                self.name,
                days,
            )
        else:
            log.info("TLE for %s, %.1f days from epoch %s", self.name, days, self.epoch.date())

    def text(self) -> str:
        return f"{self.name}\n{self.line1}\n{self.line2}\n"


def _checksum(body: str) -> int:
    """Modulo-10 sum of the digits, with every minus sign counting as one."""
    total = 0
    for char in body:
        if char.isdigit():
            total += int(char)
        elif char == "-":
            total += 1
    return total % 10


BUNDLED_TLE = TLE.parse(BUNDLED_TLE_TEXT)


def fetch(catalog_number: int, timeout: float = 15.0) -> TLE:
    """Download a current element set from Celestrak."""
    url = CELESTRAK_URL.format(catalog_number=catalog_number)
    log.info("fetching TLE for catalog number %d", catalog_number)
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed https URL
        body = response.read().decode("utf-8", errors="replace")
    if "No GP data found" in body:
        raise TLEError(f"Celestrak has no element set for catalog number {catalog_number}")
    return TLE.parse(body)


def load(
    *,
    path: Path | None = None,
    catalog_number: int = 25544,
    refresh: bool = False,
    cache_dir: Path | None = None,
) -> TLE:
    """Resolve which element set to fly, in order of how much it is trusted.

    An explicit file wins. Then a fresh download if one was asked for. Then a
    previous download still on disk. The bundled set is the floor, so the
    simulator always starts — offline, in CI, in a container with no egress —
    rather than failing on a network error.
    """
    if path is not None:
        tle = TLE.parse(path.read_text())
        log.info("TLE from %s", path)
        return tle

    cache = (cache_dir or Path(".cache/tle")) / f"{catalog_number}.tle"

    if refresh:
        try:
            tle = fetch(catalog_number)
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(tle.text())
            except OSError as exc:  # a read-only filesystem is not a failure
                log.debug("could not cache the element set: %s", exc)
            return tle
        except (urllib.error.URLError, TimeoutError, TLEError) as exc:
            log.warning("could not fetch a current TLE (%s); falling back", exc)

    if cache.is_file():
        try:
            tle = TLE.parse(cache.read_text())
            log.info("TLE from the cache at %s", cache)
            return tle
        except TLEError as exc:
            log.warning("cached TLE at %s is unusable (%s); falling back", cache, exc)

    log.info("using the bundled TLE for %s", BUNDLED_TLE.name)
    return BUNDLED_TLE
