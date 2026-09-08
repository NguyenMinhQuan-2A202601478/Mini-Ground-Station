"""Element-set handling: the checksum is the only thing that catches a typo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mgs.simulator import tle as tle_module
from mgs.simulator.tle import BUNDLED_TLE, TLE, TLEError

GOOD = BUNDLED_TLE.text()


def test_the_bundled_element_set_is_valid():
    BUNDLED_TLE.validate()
    assert BUNDLED_TLE.catalog_number == 25544
    assert BUNDLED_TLE.name == "ISS (ZARYA)"


def test_a_two_line_set_without_a_name_still_parses():
    tle = TLE.parse(f"{BUNDLED_TLE.line1}\n{BUNDLED_TLE.line2}")
    assert tle.name == "UNNAMED"
    assert tle.catalog_number == 25544


def test_a_corrupted_digit_is_caught_by_the_checksum():
    """One wrong character is the failure mode; the checksum exists for it."""
    broken = BUNDLED_TLE.line1[:30] + "9" + BUNDLED_TLE.line1[31:]
    with pytest.raises(TLEError, match="checksum"):
        TLE(name="x", line1=broken, line2=BUNDLED_TLE.line2).validate()


def test_a_truncated_line_is_rejected():
    with pytest.raises(TLEError, match="characters"):
        TLE(name="x", line1=BUNDLED_TLE.line1[:40], line2=BUNDLED_TLE.line2).validate()


def test_swapped_lines_are_rejected():
    with pytest.raises(TLEError, match="starts with"):
        TLE(name="x", line1=BUNDLED_TLE.line2, line2=BUNDLED_TLE.line1).validate()


def test_two_lines_from_different_satellites_are_rejected():
    """Two halves of different element sets, each individually well-formed."""
    body = "2 25545" + BUNDLED_TLE.line2[7:68]
    other = body + str(tle_module._checksum(body))
    with pytest.raises(TLEError, match="different catalog numbers"):
        TLE(name="x", line1=BUNDLED_TLE.line1, line2=other).validate()


def test_too_few_lines_is_an_error():
    with pytest.raises(TLEError, match="expected 2 or 3 lines"):
        TLE.parse(BUNDLED_TLE.line1)


def test_the_epoch_decodes_to_a_real_date():
    epoch = BUNDLED_TLE.epoch
    assert epoch.tzinfo is UTC
    assert epoch.year == 2026
    # Column 19-20 is a two-digit year: 57 and above means the 1900s.
    old = TLE(
        name="x",
        line1=BUNDLED_TLE.line1[:18] + "99001.00000000" + BUNDLED_TLE.line1[32:],
        line2=BUNDLED_TLE.line2,
    )
    assert old.epoch.year == 1999


def test_staleness_is_measured_from_the_epoch():
    assert BUNDLED_TLE.age(BUNDLED_TLE.epoch + timedelta(days=3)).days == 3
    a_year_on = BUNDLED_TLE.epoch + timedelta(days=400)
    assert abs(BUNDLED_TLE.age(a_year_on).days) > tle_module.STALE_AFTER_DAYS


def test_round_tripping_text_preserves_the_element_set():
    assert TLE.parse(BUNDLED_TLE.text()) == BUNDLED_TLE


# --- resolution order -----------------------------------------------------


def test_an_explicit_file_wins(tmp_path):
    path = tmp_path / "sat.tle"
    path.write_text(GOOD)
    assert tle_module.load(path=path) == BUNDLED_TLE


def test_a_cached_element_set_is_used_when_present(tmp_path):
    cache = tmp_path / "tle"
    cache.mkdir()
    (cache / "25544.tle").write_text(GOOD)
    assert tle_module.load(catalog_number=25544, cache_dir=cache) == BUNDLED_TLE


def test_a_corrupt_cache_falls_back_instead_of_failing(tmp_path):
    cache = tmp_path / "tle"
    cache.mkdir()
    (cache / "25544.tle").write_text("not an element set at all")
    assert tle_module.load(catalog_number=25544, cache_dir=cache) == BUNDLED_TLE


def test_a_failed_download_falls_back_to_the_bundled_set(tmp_path, monkeypatch):
    """Offline, in CI, or behind a firewall, the simulator still flies."""

    def explode(catalog_number: int, timeout: float = 15.0):
        raise TimeoutError("no network here")

    monkeypatch.setattr(tle_module, "fetch", explode)
    assert tle_module.load(refresh=True, cache_dir=tmp_path / "tle") == BUNDLED_TLE


def test_a_download_is_cached_for_next_time(tmp_path, monkeypatch):
    cache = tmp_path / "tle"
    monkeypatch.setattr(tle_module, "fetch", lambda catalog_number, timeout=15.0: BUNDLED_TLE)

    tle_module.load(refresh=True, catalog_number=25544, cache_dir=cache)
    assert (cache / "25544.tle").read_text().strip() == GOOD.strip()


def test_load_never_touches_the_network_unless_asked(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("load() fetched without being asked to")

    monkeypatch.setattr(tle_module, "fetch", explode)
    assert tle_module.load(cache_dir=tmp_path / "tle") == BUNDLED_TLE


def test_epoch_of_the_bundled_set_is_close_to_release():
    """A guard against committing an element set that is already ancient."""
    assert BUNDLED_TLE.epoch > datetime(2026, 1, 1, tzinfo=UTC)
