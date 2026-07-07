"""
#51 — FLOOD seed-completeness for typhoon/monsoon sub-regions that carried a
cyclone/wet season but were MISSING the flood channel the same rains produce
(Manila/Luzon, Naha/Okinawa, tropical-N Australia, Vienna/Danube). Caught by
reference/flood_seismic_audit.py. Tests the honest under-warning fix AND the
deliberate arid-exclusion that prevents over-warning the Red Centre.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from agents import risk_agent as ra

FLAG = ra.FLOOD_FLAG_BP   # >= this surfaces a flood FLAG


def _maxflood(region):
    d = ra._FLOOD_BY_REGION_MONTH.get(region, {})
    return max(d.values()) if d else 0


# --------------------------------------------------------------------------
# The 6 flood-prone sub-regions now carry a flood season at >= FLAG level
# --------------------------------------------------------------------------
def test_typhoon_monsoon_subregions_now_have_flood():
    for region in ("ph-luzon", "jp-okinawa", "au-northeast", "au-northwest",
                   "au-vic", "at-vienna"):
        assert _maxflood(region) >= FLAG, (
            f"{region} should now carry a flood season >= FLAG ({FLAG})")
    print("[FLOOD] PASS — 6 flood-prone sub-regions now flag a flood season\n")


def test_headline_cities_resolve_to_a_flood_region():
    # The exact under-warned cities the audit flagged now resolve to a flooded region.
    cases = {"manila": "ph-luzon", "naha": "jp-okinawa", "melbourne": "au-vic",
             "vienna": "at-vienna", "cairns": "au-northeast"}
    for city, region in cases.items():
        assert ra.region_for_city(city) == region, city
        assert _maxflood(region) >= FLAG, f"{city} ({region}) still 0-flood"
    print("[FLOOD] PASS — Manila/Naha/Melbourne/Vienna/Cairns map to a flooded region\n")


# --------------------------------------------------------------------------
# HONESTY / no over-warn — arid regions DELIBERATELY excluded
# --------------------------------------------------------------------------
def test_arid_regions_stay_dry_no_overwarn():
    # The Red Centre / Kangaroo Island do not flood seasonally — a blanket
    # parent-inheritance would have over-warned them (the trap that ruled out
    # seismic proximity-inference). They must stay 0-flood.
    assert _maxflood("au-uluru") == 0, "au-uluru (arid) must NOT inherit flood"
    assert _maxflood("au-kangaroo") == 0, "au-kangaroo (arid) must NOT inherit flood"
    print("[FLOOD] PASS — arid au-uluru / au-kangaroo stay dry (no over-warn)\n")


# --------------------------------------------------------------------------
# var-0 / additive — off-season months carry NO flood signal
# --------------------------------------------------------------------------
def test_flood_is_seasonal_offseason_is_zero():
    # Victoria's cool-season pattern: a summer month (Feb=2) carries no flood bp,
    # so an off-season Melbourne trip stays byte-identical (additive, var-0).
    assert ra._FLOOD_BY_REGION_MONTH["au-vic"].get(2, 0) == 0
    # Okinawa: a winter month (Jan=1) carries no flood bp.
    assert ra._FLOOD_BY_REGION_MONTH["jp-okinawa"].get(1, 0) == 0
    print("[FLOOD] PASS — flood is seasonal; off-season months are 0 (var-0)\n")


# --------------------------------------------------------------------------
# var-0 through the CONSUMPTION path (assess_leg), not just the seed table
# --------------------------------------------------------------------------
def test_assess_leg_flood_in_season_vs_offseason_var0():
    # In-season (Aug) Manila surfaces a flood signal via the real assess_leg path...
    in_season = ra.assess_leg(city="manila", checkin="2026-08-10", checkout="2026-08-14")
    assert "flood" in in_season["reason_codes"]
    assert in_season["flood_index_bp"] >= FLAG
    # ...while an OFF-season (Apr) Manila trip carries NO flood signal — the seed is
    # additive only in its season, so the off-season leg stays byte-identical (var-0).
    off_season = ra.assess_leg(city="manila", checkin="2026-04-10", checkout="2026-04-14")
    assert "flood" not in off_season["reason_codes"]
    assert off_season["flood_index_bp"] == 0
    print("[FLOOD] PASS — assess_leg flags flood in-season, byte-identical off-season\n")


if __name__ == "__main__":
    for t in (test_typhoon_monsoon_subregions_now_have_flood,
              test_headline_cities_resolve_to_a_flood_region,
              test_arid_regions_stay_dry_no_overwarn,
              test_flood_is_seasonal_offseason_is_zero,
              test_assess_leg_flood_in_season_vs_offseason_var0):
        t()
    print("ALL #51 FLOOD SEED-COMPLETENESS TESTS PASSED")
