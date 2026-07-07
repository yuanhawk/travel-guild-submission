"""Tester-reported bugs 3/4/5/6 — emergency-feed hardening (all verified real).

  Bug 4/5: `_country_matches` used an UNANCHORED substring fallback → "india" matched
           "British Indian Ocean Territory" (false IN) and "korea" matched "North Korea"
           (false KR) — false do-not-travel for the WRONG country.
  Bug 3:   monitoring tier was detected only via `resp.get("monitoring")`, an undocumented
           key — a spec-compliant client returning severity=="monitoring" silently
           downgraded a tracked storm to "clear".
  Bug 6:   `active_emergencies` was emitted per-leg with no iso2 dedup → a multi-leg
           same-country trip produced repeated identical entries.

All of this is the LIVE-feed overlay → off the var-0 / `_request_digest` path.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.emergency_feed import _country_matches
from orchestration.orchestrator import TravelOrchestrator


def _orch(client):
    o = TravelOrchestrator(emergency_client=client)
    o._trip_id = "t-hardening"
    o._emergency_request = {"check": True}
    return o


def _result(legs):
    # legs: list of (leg_id, city, iso2)
    return {"outcome": "success", "day_plans": [
        {"leg_id": lid, "city": c, "iso2": i, "region": "r", "days": []}
        for (lid, c, i) in legs]}


# ---- Bug 4 + 5: word-boundary + directional-country guard -------------------
def test_bug4_india_not_british_indian_ocean_territory():
    # empty affectedcountries forces the name fallback (the buggy path)
    assert _country_matches({"country": "British Indian Ocean Territory",
                             "affectedcountries": []}, "IN") is False


def test_bug5_korea_not_north_korea():
    assert _country_matches({"country": "North Korea", "affectedcountries": []}, "KR") is False
    assert _country_matches({"country": "Korea, Democratic People's Republic of",
                             "affectedcountries": []}, "KR") is False


def test_real_matches_preserved():
    # structured iso2 path (primary) — unchanged
    assert _country_matches({"country": "x", "affectedcountries": [{"iso2": "IN"}]}, "IN") is True
    # name fallback — legitimate matches still pass
    assert _country_matches({"country": "India", "affectedcountries": []}, "IN") is True
    assert _country_matches({"country": "Japan", "affectedcountries": []}, "JP") is True
    assert _country_matches({"country": "Thailand", "affectedcountries": []}, "TH") is True
    assert _country_matches({"country": "Vietnam", "affectedcountries": []}, "VN") is True
    assert _country_matches({"country": "South Korea", "affectedcountries": []}, "KR") is True
    assert _country_matches({"country": "Korea, Republic of", "affectedcountries": []}, "KR") is True


def test_multiword_un_iso_spellings_match_via_name_fallback():
    # Fix: word-boundary matching must NOT create a false-NEGATIVE for the
    # UN/ISO spellings GDACS actually emits, even with an EMPTY structured affectedcountries
    # (a missed do-not-travel is worse than the old false-positive).
    assert _country_matches({"country": "Viet Nam", "affectedcountries": []}, "VN") is True   # UN spelling (space)
    assert _country_matches({"country": "Vietnam", "affectedcountries": []}, "VN") is True
    assert _country_matches({"country": "Macao", "affectedcountries": []}, "MO") is True       # ISO/GDACS spelling
    assert _country_matches({"country": "Macau", "affectedcountries": []}, "MO") is True
    # no-false-positive property still holds for the substring trap
    assert _country_matches({"country": "British Indian Ocean Territory",
                             "affectedcountries": []}, "IN") is False


# ---- Bug 3: monitoring triggers on severity=="monitoring" (no 'monitoring' key) ----
def test_bug3_monitoring_via_severity_field():
    o = _orch(lambda q: {"active": False, "severity": "monitoring",
                         "hazard": "tropical_cyclone", "headline": "TC tracked", "source": "x"})
    r = _result([("leg-0", "naha", "JP")])
    o._maybe_check_active_emergencies(r)
    ae = r["active_emergencies"]
    assert len(ae) == 1 and ae[0]["status"] == "monitoring", ae  # was 'clear' pre-fix


def test_bug3_active_and_clear_unchanged():
    o = _orch(lambda q: {"active": True, "hazard": "wildfire", "severity": "red"})
    r = _result([("leg-0", "la", "US")])
    o._maybe_check_active_emergencies(r)
    assert r["active_emergencies"][0]["status"] == "active"
    o2 = _orch(lambda q: {"active": False})
    r2 = _result([("leg-0", "la", "US")])
    o2._maybe_check_active_emergencies(r2)
    assert r2["active_emergencies"][0]["status"] == "clear"


# ---- Bug 6: dedup active_emergencies by country -----------------------------
def test_bug6_same_country_collapses_to_one_highest_severity():
    def client(q):
        return ({"active": True, "hazard": "typhoon", "severity": "red"}
                if q.get("city") == "sapporo" else {"active": False})
    o = _orch(client)
    r = _result([("leg-0", "tokyo", "JP"), ("leg-1", "osaka", "JP"), ("leg-2", "sapporo", "JP")])
    o._maybe_check_active_emergencies(r)
    ae = r["active_emergencies"]
    assert len(ae) == 1, f"expected 1 JP entry, got {len(ae)}: {ae}"
    assert ae[0]["status"] == "active", ae  # highest severity wins across the legs


def test_bug6_multi_country_one_entry_each():
    o = _orch(lambda q: {"active": True, "hazard": "flood", "severity": "orange"})
    r = _result([("leg-0", "tokyo", "JP"), ("leg-1", "la", "US")])
    o._maybe_check_active_emergencies(r)
    assert len(r["active_emergencies"]) == 2, r["active_emergencies"]
