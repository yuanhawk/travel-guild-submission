"""test_poi_zero_coverage_guard.py — regression pins for task #B1: the
zero-POI-coverage fail-closed honesty guard.

Root cause: 36 of 196 countries have ZERO entries in poi_catalog.json, so the
day-planner returns catalog_hit=False + an EMPTY day-by-day plan (correctly, no
fabrication) — but the merchant hotel catalog often DOES book a real, charged
hotel for those same cities. The pre-fix behavior silently took real payment
while shipping a completely empty itinerary, and leaked a raw internal catalog
key ("no POI catalog entry for 'ID:bali'") straight into the user-facing UI.

This fix (a) surfaces a clean, hotel-aware advisory in the SAME response as the
booking (not a hard decline — a real hotel exists), (b) never leaks the internal
catalog key, and (c) gates on the leg actually being empty (catalog_hit=False AND
no attractions/meals), consistent with this codebase's narration rule that a
catalog MISS can still carry real sub-area POIs ("catalog_hit is NOT the gate").
"""
from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

os.environ.pop("DASHSCOPE_API_KEY", None)

from agents.day_planner_agent import build_day_plan  # noqa: E402
from orchestration.orchestrator import TravelOrchestrator  # noqa: E402

_TODAY = "2026-07-08"

# The single day of an empty (zero-coverage) plan, as build_day_plan emits it.
_EMPTY_DAY = {"day_index": 0, "attractions": [], "meals": {"lunch": None, "dinner": None}}


def _empty_miss_plan(city: str = "thimphu", leg_id: str = "leg-0") -> dict:
    return {
        "leg_id": leg_id, "city": city, "iso2": "BT", "catalog_hit": False,
        "days": [dict(_EMPTY_DAY)],
        "notes": ["some internal day-planner note"],
    }


def _fake_orch(day_plans: list[dict]):
    """A TravelOrchestrator whose negotiation returns a booked success envelope
    carrying the given day_plans (mirrors _success_result's shape)."""
    class _FakeDP(TravelOrchestrator):
        def _negotiate_dp(self, **kwargs):
            return {
                "outcome": "success", "booking_ref": "BK-test",
                "legs": [{"leg_id": p.get("leg_id"), "city": p.get("city"),
                          "star_rating": 3.0, "total_cents": 45000} for p in day_plans],
                "total_cents": 45000,
                "day_plans": day_plans,
            }
    o = _FakeDP()
    o._today = _TODAY
    return o


def _req(commit: bool):
    return {
        "user_id": "u1", "total_budget_cents": 50000,
        "legs": [{"city": "thimphu", "iso2": "BT", "checkin": "2026-10-01",
                  "checkout": "2026-10-04", "adults": 1}],
    }, commit


# ---------------------------------------------------------------------------
# The static predicate
# ---------------------------------------------------------------------------
class TestZeroCoveragePredicate(unittest.TestCase):
    def test_empty_miss_is_zero_coverage(self):
        self.assertTrue(TravelOrchestrator._is_zero_poi_coverage_leg(_empty_miss_plan()))

    def test_catalog_hit_true_is_not_zero_coverage(self):
        # A THIN catalog (hit=True) that under-covers a city is explicitly OUT of scope.
        p = _empty_miss_plan(); p["catalog_hit"] = True
        self.assertFalse(TravelOrchestrator._is_zero_poi_coverage_leg(p))

    def test_miss_with_real_subarea_content_is_not_zero_coverage(self):
        # Bali-style: catalog MISS but real restaurants present (POIs live under
        # sub-areas). "catalog_hit is NOT the gate" — must NOT warn "no data".
        p = _empty_miss_plan(city="bali")
        p["days"] = [{"day_index": 0, "attractions": [],
                      "meals": {"lunch": {"name": "Warung Made"}, "dinner": None}}]
        self.assertFalse(TravelOrchestrator._is_zero_poi_coverage_leg(p))

    def test_miss_with_real_attraction_is_not_zero_coverage(self):
        p = _empty_miss_plan()
        p["days"] = [{"day_index": 0, "attractions": [{"name": "Tiger's Nest"}],
                      "meals": {"lunch": None, "dinner": None}}]
        self.assertFalse(TravelOrchestrator._is_zero_poi_coverage_leg(p))

    def test_missing_catalog_hit_key_is_conservative_false(self):
        # A malformed plan with no catalog_hit key must not trip the warning.
        self.assertFalse(TravelOrchestrator._is_zero_poi_coverage_leg(
            {"leg_id": "l", "city": "x", "days": [dict(_EMPTY_DAY)]}))


# ---------------------------------------------------------------------------
# End-to-end through negotiate()
# ---------------------------------------------------------------------------
class TestZeroCoverageAdvisory(unittest.TestCase):
    def test_committed_booking_gets_hotel_aware_advisory(self):
        o = _fake_orch([_empty_miss_plan(city="thimphu")])
        req, commit = _req(commit=True)
        res = o.negotiate(req, commit=commit)
        advisories = res.get("advisories") or []
        hit = [a for a in advisories if "Thimphu" in a]
        self.assertTrue(hit, f"expected a Thimphu advisory, got {advisories!r}")
        self.assertIn("confirmed hotel booking", hit[0])

    def test_no_internal_catalog_key_leaks(self):
        o = _fake_orch([_empty_miss_plan(city="thimphu")])
        req, commit = _req(commit=True)
        res = o.negotiate(req, commit=commit)
        blob = " ".join(res.get("advisories") or [])
        # None of the raw internal diagnostic shapes may reach the user.
        for leak in ("catalog entry", "BT:", "no POI catalog", "conservative empty plan"):
            self.assertNotIn(leak, blob)

    def test_plan_only_uses_held_not_confirmed_wording(self):
        # commit=False → nothing charged yet → must NOT claim a "confirmed" booking.
        o = _fake_orch([_empty_miss_plan(city="thimphu")])
        req, _ = _req(commit=False)
        res = o.negotiate(req, commit=False)
        advisories = res.get("advisories") or []
        hit = [a for a in advisories if "Thimphu" in a]
        self.assertTrue(hit, f"expected a Thimphu advisory, got {advisories!r}")
        self.assertIn("held", hit[0])
        self.assertNotIn("confirmed hotel booking", hit[0])

    def test_covered_leg_gets_no_zero_coverage_advisory(self):
        covered = {"leg_id": "leg-0", "city": "paris", "catalog_hit": True,
                   "days": [{"day_index": 0, "attractions": [{"name": "Louvre"}],
                             "meals": {"lunch": {"name": "Bistro"}, "dinner": None}}],
                   "notes": []}
        o = _fake_orch([covered])
        req, commit = _req(commit=True)
        res = o.negotiate(req, commit=commit)
        blob = " ".join(res.get("advisories") or [])
        self.assertNotIn("day-by-day activity data", blob)

    def test_miss_with_real_content_lifts_its_own_note_not_false_warning(self):
        # A catalog MISS that still has real sub-area content must (a) NOT get the
        # "no activity data" warning and (b) still have its honest per-leg note
        # lifted normally (the note-lift skip is scoped to true zero-coverage legs).
        p = _empty_miss_plan(city="bali")
        p["days"] = [{"day_index": 0, "attractions": [],
                      "meals": {"lunch": {"name": "Warung Made"}, "dinner": None}}]
        p["notes"] = ["low attraction variety for this leg"]
        o = _fake_orch([p])
        req, commit = _req(commit=True)
        res = o.negotiate(req, commit=commit)
        blob = " ".join(res.get("advisories") or [])
        self.assertNotIn("day-by-day activity data", blob)
        self.assertIn("low attraction variety for this leg", blob)


# ---------------------------------------------------------------------------
# The day-planner's own user-facing note is clean (no raw catalog key)
# ---------------------------------------------------------------------------
class TestDayPlannerCleanMissNote(unittest.TestCase):
    def test_genuine_miss_note_is_human_readable_no_key_leak(self):
        # NB: uses a country still absent from poi_catalog.json. Bhutan/Thimphu was
        # the original example but is now seeded (task F6), so it is no longer a miss;
        # Comoros/Moroni remains zero-coverage. Any still-uncovered country works here.
        plan = build_day_plan(
            city="moroni", iso2="KM", country="Comoros",
            checkin="2026-10-01", checkout="2026-10-04",
        )
        self.assertIs(plan["catalog_hit"], False)
        note = " ".join(plan.get("notes", []))
        self.assertIn("unknown city", note)         # existing substring contract
        self.assertNotIn("KM:", note)               # no raw internal catalog key
        self.assertNotIn("no POI catalog entry", note)


if __name__ == "__main__":
    unittest.main()
