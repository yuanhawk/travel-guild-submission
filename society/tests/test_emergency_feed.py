"""
test_emergency_feed.py — #51 LIVE active-emergency overlay (provider clients,
per-trip escalation, global watchlist endpoint).

Stub-based (GDACS is live / non-deterministic, so we never assert against live
data — only against a MOCKED GeoJSON for the parse/map path). Confirms the
HONESTY invariants: feed-down is DISTINCT from all-clear, var-0 default path is
untouched, and the escalation only fires when opted-in with a configured feed.
"""
from __future__ import annotations

import os

import pytest

from utils import emergency_feed as ef
from orchestration.orchestrator import TravelOrchestrator


# Canned GDACS GeoJSON: a Red tropical cyclone over Taiwan (+ a Green event that
# must be ignored, and a non-matching Red flood elsewhere).
_CANNED = {
    "features": [
        {"properties": {
            "eventtype": "TC", "alertlevel": "Red", "name": "Tropical Cyclone PODUL-26",
            "description": "Podul over southern Taiwan", "fromdate": "2026-06-25T00:00:00",
            "todate": "2026-06-28T00:00:00", "datemodified": "2026-06-26T06:00:00",
            "country": "Taiwan",
            "affectedcountries": [{"iso2": "TW", "iso3": "TWN", "countryname": "Taiwan"}],
        }},
        {"properties": {  # Green TC → MONITORING tier (tracked, not do-not-travel)
            "eventtype": "TC", "alertlevel": "Green", "name": "Weak TC",
            "description": "offshore, no warnings", "datemodified": "2026-06-26T00:00:00",
            "affectedcountries": [{"iso2": "JP", "countryname": "Japan"}],
        }},
        {"properties": {  # Red flood elsewhere → in watchlist, not a TW match
            "eventtype": "FL", "alertlevel": "Red", "name": "Flood in Pakistan",
            "fromdate": "2026-06-20T00:00:00", "todate": "2026-06-30T00:00:00",
            "datemodified": "2026-06-26T00:00:00", "country": "Pakistan",
            "affectedcountries": [{"iso2": "PK", "countryname": "Pakistan"}],
        }},
        {"properties": {  # Green WILDFIRE → EXCLUDED (noise; only Green TC is surfaced)
            "eventtype": "WF", "alertlevel": "Green", "name": "Small wildfire",
            "affectedcountries": [{"iso2": "GR", "countryname": "Greece"}],
        }},
    ]
}


# --------------------------------------------------------------------------- #
# Stub provider
# --------------------------------------------------------------------------- #
def test_stub_active_for_taiwan_clear_elsewhere():
    tw = ef.stub_emergency_client({"iso2": "TW", "checkin": "2026-06-26"})
    assert tw["active"] is True
    assert tw["hazard"] == "tropical_cyclone"
    assert "Podul" in tw["headline"]
    assert tw["source"] == "demo:stub"
    assert ef.stub_emergency_client({"iso2": "ID", "checkin": "2026-05-01"})["active"] is False


def test_stub_deterministic():
    q = {"iso2": "TW", "checkin": "2026-06-26"}
    assert ef.stub_emergency_client(q) == ef.stub_emergency_client(q)


def test_stub_jp_green_tc_is_monitoring():
    jp = ef.stub_emergency_client({"iso2": "JP", "checkin": "2026-06-26"})
    assert jp["active"] is False and jp["monitoring"] is True
    assert jp["severity"] == "monitoring" and jp["hazard"] == "tropical_cyclone"


# --------------------------------------------------------------------------- #
# GDACS provider (mocked fetch — never hits the network)
# --------------------------------------------------------------------------- #
def test_gdacs_maps_red_tc_over_taiwan():
    r = ef.gdacs_emergency_client({"iso2": "TW", "checkin": "2026-06-26",
                                   "checkout": "2026-06-30"}, _fetch=lambda: _CANNED)
    assert r == {
        "active": True, "hazard": "tropical_cyclone", "severity": "high",
        "headline": "Tropical Cyclone PODUL-26 — Podul over southern Taiwan",
        "advice": "Do not travel; adhere to official evacuation guidance.",
        "source": "live:gdacs", "as_of": "2026-06-26",
    }


def test_gdacs_no_match_is_clear_not_active():
    r = ef.gdacs_emergency_client({"iso2": "ID", "checkin": "2026-06-26",
                                   "checkout": "2026-06-30"}, _fetch=lambda: _CANNED)
    assert r["active"] is False
    assert r["source"] == "live:gdacs"


def test_gdacs_green_tc_is_monitoring():
    # Japan has a GREEN TC → MONITORING tier: surfaced (being tracked) but NOT active
    # (must never trigger the do-not-travel escalation).
    r = ef.gdacs_emergency_client({"iso2": "JP"}, _fetch=lambda: _CANNED)
    assert r["active"] is False           # NOT a do-not-travel
    assert r["monitoring"] is True
    assert r["severity"] == "monitoring"
    assert r["hazard"] == "tropical_cyclone"
    assert r["source"] == "live:gdacs"


def test_gdacs_green_wildfire_excluded_not_monitoring():
    # Greece has only a GREEN WILDFIRE → excluded entirely (only Green TC is surfaced;
    # Green WF/FL would be high-volume noise). A GR query → plain clear, no monitoring.
    r = ef.gdacs_emergency_client({"iso2": "GR"}, _fetch=lambda: _CANNED)
    assert r["active"] is False
    assert "monitoring" not in r          # NOT surfaced as monitoring


def test_gdacs_error_returns_none_never_fabricates():
    def _boom():
        raise RuntimeError("network down")
    assert ef.gdacs_emergency_client({"iso2": "TW"}, _fetch=_boom) is None


def test_gdacs_emergency_client_retries_once_then_succeeds_on_transient_failure():
    # Same transient-hiccup class as the watchlist retry, this time on the
    # per-trip client (dailies-review found this surfaces "Feed unavailable" in
    # the Safety tab independently of the global watchlist badge).
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("gdacs slow")
        return _CANNED

    r = ef.gdacs_emergency_client({"iso2": "TW"}, _fetch=_flaky)
    assert r is not None
    assert r["active"] is True
    assert calls["n"] == 2


def test_gdacs_emergency_client_gives_up_after_exactly_one_retry():
    calls = {"n": 0}

    def _always_boom():
        calls["n"] += 1
        raise RuntimeError("network down")

    assert ef.gdacs_emergency_client({"iso2": "TW"}, _fetch=_always_boom) is None
    assert calls["n"] == 2  # initial try + exactly one retry, then give up


# --------------------------------------------------------------------------- #
# Global watchlist
# --------------------------------------------------------------------------- #
def test_watchlist_lists_active_plus_green_tc_monitoring():
    w = ef.gdacs_active_watchlist(_fetch=lambda: _CANNED)
    assert w["status"] == "ok"
    bysev = {c["iso2"]: c["severity"] for c in w["countries"]}
    assert bysev.get("TW") == "high" and bysev.get("PK") == "high"  # both Red → active
    assert bysev.get("JP") == "monitoring"   # Green TC → monitoring tier (surfaced)
    assert "GR" not in bysev                  # Green WILDFIRE → still excluded (noise)
    # Active (high/medium) sorted before monitoring.
    assert w["countries"][0]["severity"] == "high"
    assert w["countries"][-1]["severity"] == "monitoring"


def test_watchlist_feed_down_is_unavailable_not_empty_safe():
    def _boom():
        raise RuntimeError("gdacs 503")
    w = ef.gdacs_active_watchlist(_fetch=_boom)
    assert w["status"] == "unavailable"        # DISTINCT from "no active emergencies"
    assert w["countries"] == []


def test_watchlist_retries_once_then_succeeds_on_transient_failure():
    # A single transient hiccup (the dailies-review issue #2 class) must not be
    # reported as 'unavailable' if the very next attempt succeeds.
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("gdacs slow")
        return _CANNED

    w = ef.gdacs_active_watchlist(_fetch=_flaky)
    assert w["status"] == "ok"
    assert calls["n"] == 2  # one failed attempt, one successful retry


def test_watchlist_gives_up_after_exactly_one_retry():
    # A genuine outage (every attempt fails) must still be honestly 'unavailable'
    # — retrying must not become an unbounded/hanging loop.
    calls = {"n": 0}

    def _always_boom():
        calls["n"] += 1
        raise RuntimeError("gdacs 503")

    w = ef.gdacs_active_watchlist(_fetch=_always_boom)
    assert w["status"] == "unavailable"
    assert w["countries"] == []
    assert calls["n"] == 2  # initial try + exactly one retry, then give up


def test_stub_watchlist_has_podul():
    w = ef.stub_active_watchlist()
    assert w["status"] == "ok"
    assert any(c["iso2"] == "TW" and "Podul" in c["headline"] for c in w["countries"])
    # the demo set also carries a Green-TC MONITORING entry (mirrors the live tiering)
    assert any(c["severity"] == "monitoring" and c["hazard"] == "tropical_cyclone"
               for c in w["countries"])


def test_factories():
    assert ef.build_emergency_client("stub") is ef.stub_emergency_client
    assert ef.build_emergency_client("gdacs") is ef.gdacs_emergency_client
    assert ef.build_emergency_client("") is None
    assert ef.build_active_watchlist("stub") is ef.stub_active_watchlist
    assert ef.build_active_watchlist("") is ef.gdacs_active_watchlist


# --------------------------------------------------------------------------- #
# Orchestrator per-trip escalation (consumer side)
# --------------------------------------------------------------------------- #
def _result_with_legs():
    return {"day_plans": [
        {"leg_id": "L1", "city": "kaohsiung", "iso2": "TW", "region": "tw",
         "checkin": "2026-06-10", "checkout": "2026-06-14"},
        {"leg_id": "L2", "city": "bali", "iso2": "ID", "region": "id-bali",
         "checkin": "2026-06-15", "checkout": "2026-06-18"},
    ]}


def test_orchestrator_escalates_active_leg_when_opted_in():
    orch = TravelOrchestrator(emergency_client=ef.stub_emergency_client)
    orch._emergency_request = {"check": True}
    result = _result_with_legs()
    orch._maybe_check_active_emergencies(result)
    ae = {e["city"]: e for e in result["active_emergencies"]}
    assert ae["kaohsiung"]["status"] == "active"
    assert ae["kaohsiung"]["hazard"] == "tropical_cyclone"
    assert "DO NOT TRAVEL" in ae["kaohsiung"]["notice"]
    assert ae["bali"]["status"] == "clear"


def test_orchestrator_green_tc_leg_is_monitoring_not_active():
    # A leg whose country has a Green TC → status 'monitoring' (NOT 'active'/do-not-travel).
    orch = TravelOrchestrator(emergency_client=ef.stub_emergency_client)
    orch._emergency_request = {"check": True}
    result = {"day_plans": [
        {"leg_id": "L1", "city": "tokyo", "iso2": "JP", "region": "jp",
         "checkin": "2026-06-10", "checkout": "2026-06-14"},
    ]}
    orch._maybe_check_active_emergencies(result)
    e = result["active_emergencies"][0]
    assert e["status"] == "monitoring"        # distinct from 'active'
    assert e["severity"] == "monitoring"
    assert "notice" not in e                   # no DO-NOT-TRAVEL notice
    assert "monitor" in (e["note"] or "").lower()


def test_orchestrator_no_optin_is_byte_identical_noop():
    orch = TravelOrchestrator(emergency_client=ef.stub_emergency_client)
    orch._emergency_request = None            # not opted in
    result = _result_with_legs()
    orch._maybe_check_active_emergencies(result)
    assert "active_emergencies" not in result  # var-0 / back-compat


def test_orchestrator_feed_error_is_honest_unavailable_not_clear():
    def _boom(_q):
        raise RuntimeError("provider exploded")
    orch = TravelOrchestrator(emergency_client=_boom)
    orch._emergency_request = {"check": True}
    result = _result_with_legs()
    orch._maybe_check_active_emergencies(result)
    statuses = {e["status"] for e in result["active_emergencies"]}
    assert statuses == {"unavailable"}         # NEVER a fabricated all-clear


def test_orchestrator_no_client_unavailable_note():
    orch = TravelOrchestrator(emergency_client=None)
    orch._emergency_request = {"check": True}
    result = _result_with_legs()
    orch._maybe_check_active_emergencies(result)
    assert all(e["status"] == "unavailable" for e in result["active_emergencies"])


# --------------------------------------------------------------------------- #
# Bug B (2026-06-27): out-of-season storm must NOT raise a do-not-travel.
# User report: an October sapporo trip showed "DO NOT TRAVEL · Tropical Cyclone MEKKHALA-26"
# from a storm tracked in *June*. Two root causes: (1) an open `todate` became +infinity in
# the window filter; (2) the per-leg query had blank trip dates (day_plans carry no dates —
# they live on result['legs']), so the window filter fell open and matched any active storm.
# --------------------------------------------------------------------------- #
def test_windows_overlap_open_todate_is_bounded_not_infinite():
    # in-progress storm (blank todate) last modified in June → must NOT overlap an Oct trip ...
    assert ef._windows_overlap("2026-06-20", "", "2026-10-10", "2026-10-20", "2026-06-27") is False
    # ... but DOES overlap a concurrent June trip
    assert ef._windows_overlap("2026-06-20", "", "2026-06-22", "2026-06-26", "2026-06-27") is True
    # ... and DOES overlap an IMMINENT trip a few days out within the forecast horizon (the
    # honesty-critical case: never a false all-clear on an active storm just past its last update)
    assert ef._windows_overlap("2026-06-25", "", "2026-06-29", "2026-07-03", "2026-06-27") is True
    # no todate AND no modified → bounded by the start date (still won't haunt a later trip)
    assert ef._windows_overlap("2026-06-20", "", "2026-10-10", "2026-10-20", "") is False
    # truly-undated event (no end, no modified, no start) keeps the open bound (fail-safe)
    assert ef._windows_overlap("", "", "2026-10-10", "2026-10-20", "") is True
    # malformed-but-truthy base (passes _date10's length gate, fails date parse) must FAIL OPEN,
    # never silently suppress an active event (the honesty direction: silence != safety)
    assert ef._windows_overlap("", "", "2026-10-10", "2026-10-20", "2026-13-45") is True
    # blank TRIP dates still fail open — never suppress an alert on missing trip data
    assert ef._windows_overlap("2026-06-20", "2026-06-27", "", "", "2026-06-27") is True


_TYPHOON_JUNE = {"features": [{"properties": {
    "eventtype": "TC", "alertlevel": "Red", "name": "Tropical Cyclone MEKKHALA-26",
    "description": "Tropical cyclone", "fromdate": "2026-06-20T00:00:00",
    "todate": "2026-06-27T00:00:00", "datemodified": "2026-06-27T00:00:00",
    "country": "Japan", "affectedcountries": [{"iso2": "JP", "countryname": "Japan"}],
}}]}


def test_gdacs_out_of_season_storm_is_clear_not_active():
    # October trip vs a June typhoon → no active emergency in the trip window.
    r = ef.gdacs_emergency_client(
        {"iso2": "JP", "checkin": "2026-10-10", "checkout": "2026-10-20"},
        _fetch=lambda: _TYPHOON_JUNE)
    assert (r or {}).get("active") is False  # checked & clear, NOT a fabricated do-not-travel


def test_orchestrator_out_of_season_storm_uses_leg_dates_not_blank():
    # THE user bug: day_plans carry NO trip dates; they live on result['legs'] (here without an
    # explicit leg_id → matched by the canonical leg-{i} index). A June storm must read as CLEAR
    # for an October trip — previously the blank dates fell open and surfaced a false do-not-travel.
    client = lambda q: ef.gdacs_emergency_client(q, _fetch=lambda: _TYPHOON_JUNE)
    orch = TravelOrchestrator(emergency_client=client)
    orch._emergency_request = {"check": True}
    result = {
        "day_plans": [{"leg_id": "leg-0", "city": "sapporo", "iso2": "JP"}],   # NO dates
        "legs": [{"city": "sapporo", "checkin": "2026-10-10", "checkout": "2026-10-20"}],
    }
    orch._maybe_check_active_emergencies(result)
    assert result["active_emergencies"][0]["status"] == "clear"


def test_orchestrator_in_season_storm_still_flags_do_not_travel():
    # The fix must NOT silence a REAL overlap: a June storm for a June trip is still do-not-travel.
    client = lambda q: ef.gdacs_emergency_client(q, _fetch=lambda: _TYPHOON_JUNE)
    orch = TravelOrchestrator(emergency_client=client)
    orch._emergency_request = {"check": True}
    result = {
        "day_plans": [{"leg_id": "leg-0", "city": "sapporo", "iso2": "JP"}],
        "legs": [{"leg_id": "leg-0", "city": "sapporo",
                  "checkin": "2026-06-22", "checkout": "2026-06-26"}],
    }
    orch._maybe_check_active_emergencies(result)
    e = result["active_emergencies"][0]
    assert e["status"] == "active" and "DO NOT TRAVEL" in e["notice"]


# --------------------------------------------------------------------------- #
# /emergencies endpoint (stub feed)
# --------------------------------------------------------------------------- #
def test_emergencies_endpoint_stub(monkeypatch):
    monkeypatch.setenv("EMERGENCY_FEED", "stub")
    from starlette.testclient import TestClient
    from orchestration import server
    with TestClient(server.build_app()) as client:
        r = client.get("/emergencies")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "ok"
        assert any(c["iso2"] == "TW" for c in d["countries"])


# =========================================================================== #
# 2026-07-06 adversarial review — F1-F5 regression tests.
# =========================================================================== #

# --------------------------------------------------------------------------- #
# F1 — one fetch per trip-check call, not one per leg.
# --------------------------------------------------------------------------- #
def test_orchestrator_gdacs_batch_fetches_feed_once_per_trip_not_per_leg(monkeypatch):
    # Before the fix: _maybe_check_active_emergencies called _call_emergency_feed
    # (-> the client -> a fresh network fetch) once PER LEG. A 3-leg trip fired 3
    # independent HTTP round-trips for the IDENTICAL feed — each with its own
    # 2-attempt retry (~10.3s worst case), so an N-leg trip against a real GDACS
    # outage could hold a worker thread (shared with unrelated bookings, only 4
    # wide) for up to N x ~10.3s. Patch the actual network call (httpx.get, used
    # identically by both the pre-fix and post-fix code paths) to prove the fetch
    # now happens exactly ONCE for the whole trip regardless of leg count.
    import httpx as _httpx

    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return _CANNED

    def _fake_get(url, timeout=None):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(_httpx, "get", _fake_get)

    orch = TravelOrchestrator(emergency_client=ef.gdacs_emergency_client)
    orch._emergency_request = {"check": True}
    result = {"day_plans": [
        # _CANNED's Red TC over Taiwan runs 2026-06-25 .. 2026-06-28 — both TW legs
        # below overlap it, the PH leg does not (and _CANNED has no PH event anyway).
        {"leg_id": "L1", "city": "kaohsiung", "iso2": "TW",
         "checkin": "2026-06-24", "checkout": "2026-06-27"},
        {"leg_id": "L2", "city": "taipei", "iso2": "TW",
         "checkin": "2026-06-26", "checkout": "2026-06-29"},
        {"leg_id": "L3", "city": "manila", "iso2": "PH",
         "checkin": "2026-06-20", "checkout": "2026-06-25"},
    ]}
    orch._maybe_check_active_emergencies(result)
    assert calls["n"] == 1  # ONE network fetch for a 3-leg trip, not 3
    # Still correct per-leg query-wise (each leg's OWN iso2/dates are honored by the
    # shared-fetch match); the existing by-country DISPLAY dedup then collapses the
    # two TW legs into one row (unchanged pre-existing behavior — see the dedup
    # comment in _maybe_check_active_emergencies), leaving TW (active) + PH (clear).
    statuses = sorted(e["status"] for e in result["active_emergencies"])
    assert statuses == ["active", "clear"]


def test_orchestrator_gdacs_batch_fetch_failure_marks_every_leg_unavailable(monkeypatch):
    # A total fetch failure (after the retry) must degrade EVERY leg to the
    # honest 'unavailable' status, never a fabricated all-clear, and never a
    # partial/inconsistent picture across legs of the SAME trip.
    import httpx as _httpx

    def _fake_get(url, timeout=None):
        raise RuntimeError("gdacs down")

    monkeypatch.setattr(_httpx, "get", _fake_get)

    orch = TravelOrchestrator(emergency_client=ef.gdacs_emergency_client)
    orch._emergency_request = {"check": True}
    result = _result_with_legs()
    orch._maybe_check_active_emergencies(result)
    statuses = {e["status"] for e in result["active_emergencies"]}
    assert statuses == {"unavailable"}


# --------------------------------------------------------------------------- #
# F2 — a stale-but-lagging `todate` on a STILL-ACTIVE (Orange/Red) event, whose
# `datemodified` is more recent, must not window the event out as "expired".
# --------------------------------------------------------------------------- #
def test_windows_overlap_stale_todate_extends_when_still_active_and_recently_modified():
    # todate says the storm ended 2026-07-05, but datemodified (2026-07-06) proves
    # GDACS is STILL touching/updating it — a trip starting the very next day must
    # not be waved through as clear just because of a stale todate.
    assert ef._windows_overlap("2026-07-01", "2026-07-05", "2026-07-07", "2026-07-12",
                                "2026-07-06", is_active=True) is True


def test_windows_overlap_stale_todate_not_extended_for_genuinely_closed_event():
    # datemodified is NOT more recent than todate → a genuinely closed/old event
    # must be completely unaffected by the fix (still correctly filtered out).
    assert ef._windows_overlap("2026-07-01", "2026-07-05", "2026-07-07", "2026-07-12",
                                "2026-07-04", is_active=True) is False


def test_windows_overlap_stale_todate_extension_scoped_to_active_only():
    # Same stale-todate-but-recently-modified shape as the first test, but for a
    # non-active (e.g. Green/monitoring) event — the extension must NOT apply.
    assert ef._windows_overlap("2026-07-01", "2026-07-05", "2026-07-07", "2026-07-12",
                                "2026-07-06", is_active=False) is False


_STILL_ACTIVE_STALE_TODATE = {"features": [{"properties": {
    "eventtype": "TC", "alertlevel": "Orange", "name": "TC STILLACTIVE",
    "description": "storm still being tracked", "fromdate": "2026-07-01T00:00:00",
    "todate": "2026-07-05T18:00:00",        # stale — the feed kept updating past this
    "datemodified": "2026-07-06T12:00:00",  # newer than todate → genuinely still live
    "country": "Philippines",
    "affectedcountries": [{"iso2": "PH", "countryname": "Philippines"}],
}}]}


def test_gdacs_still_active_storm_with_stale_todate_flags_active_for_imminent_trip():
    r = ef.gdacs_emergency_client(
        {"iso2": "PH", "checkin": "2026-07-07", "checkout": "2026-07-12"},
        _fetch=lambda: _STILL_ACTIVE_STALE_TODATE)
    assert r["active"] is True


# --------------------------------------------------------------------------- #
# F3 — CN alias "china" must not word-match inside GDACS's own canonical Taiwan
# string ("Taiwan, Province of China"); ditto US vs. "...Minor Outlying Islands".
# --------------------------------------------------------------------------- #
def test_country_matches_cn_does_not_false_positive_on_taiwan():
    props = {"country": "Taiwan, Province of China", "affectedcountries": []}
    assert ef._country_matches(props, "CN") is False
    assert ef._country_matches(props, "TW") is True   # still correctly matches Taiwan itself


def test_country_matches_us_does_not_false_positive_on_minor_outlying_islands():
    props = {"country": "United States Minor Outlying Islands", "affectedcountries": []}
    assert ef._country_matches(props, "US") is False


def test_gdacs_taiwan_only_typhoon_is_not_active_for_china_trip():
    taiwan_typhoon = {"features": [{"properties": {
        "eventtype": "TC", "alertlevel": "Orange", "name": "Typhoon TEST",
        "description": "test", "fromdate": "2026-07-01T00:00:00",
        "todate": "2026-07-12T00:00:00", "datemodified": "2026-07-06T00:00:00",
        "country": "Taiwan, Province of China", "affectedcountries": [],
    }}]}
    r = ef.gdacs_emergency_client(
        {"iso2": "CN", "checkin": "2026-07-05", "checkout": "2026-07-10"},
        _fetch=lambda: taiwan_typhoon)
    assert r["active"] is False   # NOT a false do-not-travel for the mainland-China trip
    # The same feed still correctly flags a Taiwan (TW) trip.
    r_tw = ef.gdacs_emergency_client(
        {"iso2": "TW", "checkin": "2026-07-05", "checkout": "2026-07-10"},
        _fetch=lambda: taiwan_typhoon)
    assert r_tw["active"] is True


# --------------------------------------------------------------------------- #
# F4 — a single malformed-but-JSON-valid feature must be SKIPPED, never crash
# the whole loop (per-trip client) nor abort the whole watchlist mid-iteration.
# --------------------------------------------------------------------------- #
def test_gdacs_client_skips_feature_with_non_string_alertlevel():
    bad = {"features": [{"properties": {
        "eventtype": "TC", "alertlevel": 3, "name": "bad",
        "affectedcountries": [{"iso2": "PH", "countryname": "Philippines"}],
    }}]}
    r = ef.gdacs_emergency_client({"iso2": "PH"}, _fetch=lambda: bad)
    assert r is not None and r["active"] is False   # skipped, not a crash


def test_gdacs_client_skips_feature_with_non_dict_properties():
    bad = {"features": [{"properties": ["oops"]}]}
    r = ef.gdacs_emergency_client({"iso2": "PH"}, _fetch=lambda: bad)
    assert r is not None and r["active"] is False


def test_gdacs_client_features_not_a_list_degrades_safely():
    r = ef.gdacs_emergency_client({"iso2": "PH"}, _fetch=lambda: {"features": 5})
    assert r == {"active": False, "source": "live:gdacs", "as_of": ""}


def test_watchlist_skips_malformed_feature_without_aborting_the_rest():
    # One bad feature must not blow up the ENTIRE global Safety Watch mid-iteration
    # — the rest of the feed must still be processed and surfaced.
    mixed = {"features": [
        {"properties": {"eventtype": "TC", "alertlevel": 3, "name": "bad"}},  # malformed
        {"properties": {  # valid — must still be surfaced despite the bad entry above
            "eventtype": "TC", "alertlevel": "Red", "name": "Good Storm",
            "affectedcountries": [{"iso2": "PH", "countryname": "Philippines"}],
        }},
    ]}
    w = ef.gdacs_active_watchlist(_fetch=lambda: mixed)
    assert w["status"] == "ok"
    assert any(c["iso2"] == "PH" for c in w["countries"])


def test_watchlist_skips_non_dict_affectedcountries_entries():
    bad = {"features": [{"properties": {
        "eventtype": "TC", "alertlevel": "Orange", "name": "storm",
        "affectedcountries": ["Japan"],   # malformed: bare strings, not dicts
    }}]}
    w = ef.gdacs_active_watchlist(_fetch=lambda: bad)
    assert w["status"] == "ok"
    assert w["countries"] == []   # malformed entry skipped, no crash


def test_watchlist_features_not_a_list_degrades_safely():
    w = ef.gdacs_active_watchlist(_fetch=lambda: {"features": 5})
    assert w["status"] == "ok"
    assert w["countries"] == []


# --------------------------------------------------------------------------- #
# F5 — _date10 must validate real ISO dates, not just length; a non-date string
# must fail open ('') rather than pass through as a garbage pseudo-date.
# --------------------------------------------------------------------------- #
def test_date10_rejects_non_iso_garbage():
    assert ef._date10("TBD see advisory") == ""
    assert ef._date10("Jul 1 2026 12:00") == ""
    assert ef._date10("2026-13-45") == ""                      # invalid calendar date, same length
    assert ef._date10("2026-07-06T00:00:00") == "2026-07-06"   # still parses real ISO datetimes
    assert ef._date10("2026-07-06") == "2026-07-06"


def test_windows_overlap_malformed_fromdate_fails_open_not_suppressed():
    # Before the fix, _date10("TBD see advisory") returned the garbage first-10
    # chars ("TBD see ad"), which sorted AFTER real 'YYYY-MM-DD' strings (ASCII
    # 'T' > '2'), so `lo <= to` was False and a genuinely overlapping active
    # emergency was silently suppressed. Must fail OPEN.
    assert ef._windows_overlap("TBD see advisory", "2026-07-12",
                                "2026-07-05", "2026-07-10") is True
