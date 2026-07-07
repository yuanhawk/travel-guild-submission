"""
test_emergency_feed_regression.py — Targeted unit-level regression tests for the
private helpers (_classify, _windows_overlap, _country_matches, _date10) and
integration gaps introduced in commit fef026d (Green TC → 'monitoring' tier).

These complement the high-level scenario coverage in test_emergency_feed.py;
they do NOT duplicate those tests. No live network calls are made — all GDACS
paths use the _fetch injection seam.
"""
from __future__ import annotations

import pytest

from utils import emergency_feed as ef


# ---------------------------------------------------------------------------
# Shared canned fixture (extended beyond _CANNED in test_emergency_feed.py to
# exercise high/medium/monitoring co-existence for the same-country and
# ordering regression tests).
# ---------------------------------------------------------------------------
_CANNED_MIXED = {
    "features": [
        # Red TC over Japan — active, high severity
        {"properties": {
            "eventtype": "TC", "alertlevel": "Red", "name": "Super Typhoon KENJI",
            "description": "Direct hit on Honshu", "fromdate": "2026-06-24T00:00:00",
            "todate": "2026-06-29T00:00:00", "datemodified": "2026-06-26T12:00:00",
            "affectedcountries": [{"iso2": "JP", "iso3": "JPN", "countryname": "Japan"}],
        }},
        # Green TC also over Japan — monitoring tier (must NOT override the Red above)
        {"properties": {
            "eventtype": "TC", "alertlevel": "Green", "name": "Weak Tropical Depression",
            "description": "Disorganised offshore", "datemodified": "2026-06-26T06:00:00",
            "affectedcountries": [{"iso2": "JP", "countryname": "Japan"}],
        }},
        # Orange FL over Philippines — medium/active
        {"properties": {
            "eventtype": "FL", "alertlevel": "Orange", "name": "Flood Luzon",
            "description": "Severe flooding north Luzon", "fromdate": "2026-06-23T00:00:00",
            "todate": "2026-06-30T00:00:00", "datemodified": "2026-06-26T00:00:00",
            "affectedcountries": [{"iso2": "PH", "countryname": "Philippines"}],
        }},
        # Green TC over South Korea — monitoring, distinct country
        {"properties": {
            "eventtype": "TC", "alertlevel": "Green", "name": "Tracking Storm KR",
            "description": "Offshore, no warnings", "datemodified": "2026-06-25T00:00:00",
            "affectedcountries": [{"iso2": "KR", "countryname": "South Korea"}],
        }},
        # Green wildfire over Greece — must remain excluded entirely
        {"properties": {
            "eventtype": "WF", "alertlevel": "Green", "name": "Small WF GR",
            "affectedcountries": [{"iso2": "GR", "countryname": "Greece"}],
        }},
    ]
}


# ===========================================================================
# _classify unit tests
# ===========================================================================

def test_classify_red_tc_high_active():
    result = ef._classify("TC", "Red")
    assert result == ("high", True)


def test_classify_orange_tc_medium_active():
    result = ef._classify("TC", "Orange")
    assert result == ("medium", True)


def test_classify_green_tc_monitoring_not_active():
    # Key regression: Green TC must be ('monitoring', False) — NOT None, NOT active.
    result = ef._classify("TC", "Green")
    assert result is not None, "Green TC must not be excluded; it surfaces as monitoring"
    sev, is_active = result
    assert sev == "monitoring"
    assert is_active is False


def test_classify_red_fl_high_active():
    result = ef._classify("FL", "Red")
    assert result == ("high", True)


def test_classify_orange_fl_medium_active():
    result = ef._classify("FL", "Orange")
    assert result == ("medium", True)


def test_classify_green_fl_excluded():
    # Green flood → high-volume noise, excluded entirely.
    assert ef._classify("FL", "Green") is None


def test_classify_red_wf_high_active():
    result = ef._classify("WF", "Red")
    assert result == ("high", True)


def test_classify_green_wf_excluded():
    # Green wildfire → excluded (only Green TC is surfaced as monitoring).
    assert ef._classify("WF", "Green") is None


def test_classify_eq_eventtype_excluded():
    # EQ is not in _GDACS_HAZARD → None regardless of alert level.
    assert ef._classify("EQ", "Red") is None
    assert ef._classify("EQ", "Orange") is None
    assert ef._classify("EQ", "Green") is None


def test_classify_unknown_eventtype_excluded():
    assert ef._classify("VO", "Red") is None
    assert ef._classify("", "Red") is None
    assert ef._classify("UNKNOWN", "Orange") is None


def test_classify_unknown_alertlevel_non_tc_excluded():
    # A hypothetical 'Yellow' alert level is not in _GDACS_SEVERITY and not 'Green',
    # so for non-TC hazards it must be excluded.
    assert ef._classify("WF", "Yellow") is None
    assert ef._classify("FL", "Yellow") is None


def test_classify_title_case_alertlevel():
    # The gdacs client calls .title() before _classify, so 'red' → 'Red'.
    # Verify _classify itself handles the already-title-cased value correctly,
    # and that the raw lowercase form is handled by the caller (not _classify directly).
    assert ef._classify("TC", "Red") == ("high", True)   # title-cased → works
    assert ef._classify("TC", "red") != ("high", True)   # lowercase → _classify does NOT lower itself
    # lowercase 'red' falls through: not in _GDACS_SEVERITY{"Red","Orange"}, not == "Green"
    assert ef._classify("TC", "red") is None


def test_classify_all_orange_types():
    # Parametric-style: FL, TC, WF with Orange all return (medium, True).
    for et in ("FL", "TC", "WF"):
        result = ef._classify(et, "Orange")
        assert result == ("medium", True), f"Orange {et} should be ('medium', True), got {result}"


# ===========================================================================
# _windows_overlap unit tests
# ===========================================================================

def test_windows_overlap_exact_match():
    # Event window == trip window exactly.
    assert ef._windows_overlap("2026-06-25T00:00:00", "2026-06-28T00:00:00",
                               "2026-06-25", "2026-06-28") is True


def test_windows_overlap_event_contains_trip():
    # Event is wider than the trip — still overlaps.
    assert ef._windows_overlap("2026-06-20T00:00:00", "2026-07-05T00:00:00",
                               "2026-06-25", "2026-06-30") is True


def test_windows_overlap_trip_contains_event():
    # Trip is wider than the event — still overlaps.
    assert ef._windows_overlap("2026-06-26T00:00:00", "2026-06-27T00:00:00",
                               "2026-06-20", "2026-07-01") is True


def test_windows_overlap_adjacent_touching():
    # Event ends exactly on the day the trip starts — boundary overlap: lo<=to and hi>=ti.
    # "2026-06-25" (event ends) == "2026-06-25" (trip starts) → DOES overlap.
    assert ef._windows_overlap("2026-06-20T00:00:00", "2026-06-25T00:00:00",
                               "2026-06-25", "2026-06-30") is True


def test_windows_overlap_no_overlap_event_before_trip():
    # Event ends before trip starts.
    assert ef._windows_overlap("2026-06-10T00:00:00", "2026-06-20T00:00:00",
                               "2026-06-25", "2026-06-30") is False


def test_windows_overlap_no_overlap_event_after_trip():
    # Event starts after trip ends.
    assert ef._windows_overlap("2026-07-01T00:00:00", "2026-07-10T00:00:00",
                               "2026-06-25", "2026-06-30") is False


def test_windows_overlap_missing_trip_dates_always_overlap():
    # Safety invariant: missing trip dates → NEVER suppress an active alert.
    assert ef._windows_overlap("2026-06-25T00:00:00", "2026-06-28T00:00:00",
                               "", "") is True
    assert ef._windows_overlap("2026-06-25T00:00:00", "2026-06-28T00:00:00",
                               None, None) is True
    assert ef._windows_overlap("2026-06-25T00:00:00", "2026-06-28T00:00:00",
                               "2026-06-25", "") is True
    assert ef._windows_overlap("2026-06-25T00:00:00", "2026-06-28T00:00:00",
                               "", "2026-06-28") is True


def test_windows_overlap_missing_event_dates_treated_as_open_ended():
    # Missing event dates → sentinel lo="0000-00-00", hi="9999-99-99" → always overlaps
    # any trip window.
    assert ef._windows_overlap("", "", "2026-06-25", "2026-06-30") is True
    assert ef._windows_overlap(None, None, "2026-06-25", "2026-06-30") is True


# ===========================================================================
# _country_matches unit tests
# ===========================================================================

def test_country_matches_by_iso2_structured():
    props = {"affectedcountries": [{"iso2": "TW", "countryname": "Taiwan"}]}
    assert ef._country_matches(props, "TW") is True


def test_country_matches_iso2_case_insensitive():
    # Query iso2 is already upper-cased by the callers, but the field value itself
    # may vary; _country_matches does .strip().upper() on the field value.
    props = {"affectedcountries": [{"iso2": "tw", "countryname": "Taiwan"}]}
    assert ef._country_matches(props, "TW") is True


def test_country_matches_by_name_alias_fallback():
    # No affectedcountries → fall back to top-level `country` name substring.
    props = {"country": "Taiwan, Province of China"}
    assert ef._country_matches(props, "TW") is True


def test_country_matches_no_match():
    props = {"affectedcountries": [{"iso2": "PK", "countryname": "Pakistan"}]}
    assert ef._country_matches(props, "TW") is False


def test_country_matches_empty_iso2_query_returns_false():
    # Empty iso2 query → always False (no country to match against).
    props = {"affectedcountries": [{"iso2": "TW", "countryname": "Taiwan"}]}
    assert ef._country_matches(props, "") is False
    assert ef._country_matches(props, None) is False


# ===========================================================================
# Integration gap tests
# ===========================================================================

def test_gdacs_active_wins_over_monitoring_same_country():
    """JP has both a Red TC (active/high) AND a Green TC (monitoring) in _CANNED_MIXED.
    The per-country client must return the most severe match: active high, NOT monitoring."""
    r = ef.gdacs_emergency_client(
        {"iso2": "JP", "checkin": "2026-06-25", "checkout": "2026-06-28"},
        _fetch=lambda: _CANNED_MIXED,
    )
    assert r is not None
    assert r["active"] is True, "Red TC must win over Green TC for the same country"
    assert r["severity"] == "high"
    assert r["hazard"] == "tropical_cyclone"
    assert "monitoring" not in r or r.get("monitoring") is not True


def test_gdacs_watchlist_ordering_high_medium_monitoring():
    """Watchlist from _CANNED_MIXED should be sorted high → medium → monitoring."""
    w = ef.gdacs_active_watchlist(_fetch=lambda: _CANNED_MIXED)
    assert w["status"] == "ok"
    severities = [c["severity"] for c in w["countries"]]
    # All 'high' entries must precede all 'medium', which precede all 'monitoring'.
    rank = {"high": 3, "medium": 2, "monitoring": 1}
    for i in range(len(severities) - 1):
        assert rank[severities[i]] >= rank[severities[i + 1]], (
            f"Ordering violated at position {i}: {severities[i]} before {severities[i + 1]}"
        )
    # Spot-check: high (JP Red TC) appears before medium (PH Orange FL).
    iso2_order = [c["iso2"] for c in w["countries"]]
    assert iso2_order.index("JP") < iso2_order.index("PH"), (
        "JP (high) should appear before PH (medium) in sorted watchlist"
    )
    # Monitoring entries (JP Green TC deduplicated to high; KR Green TC) are last.
    assert severities[-1] == "monitoring"


def test_gdacs_orange_fl_still_active_unaffected_by_green_tc_change():
    """Regression: the Green TC → monitoring change must NOT affect Orange/Red FL logic.
    An Orange FL must still produce active=True, severity='medium'."""
    r = ef.gdacs_emergency_client(
        {"iso2": "PH", "checkin": "2026-06-24", "checkout": "2026-06-29"},
        _fetch=lambda: _CANNED_MIXED,
    )
    assert r is not None
    assert r["active"] is True, "Orange FL must still be active after the Green TC change"
    assert r["severity"] == "medium"
    assert r["hazard"] == "flood"
    assert r["source"] == "live:gdacs"
