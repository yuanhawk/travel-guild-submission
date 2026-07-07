"""test_item_transport_gaps.py — Unit tests for build_item_transport_gaps + _timeline_for_gaps.

var-0: all tests are pure/deterministic — no clock, no RNG, no network.

Test matrix (required by spec):
  - Correct timeline ORDER (mealAnchoredTimeline: breakfast→morning→lunch→afternoon→tea→dinner→supper)
  - N−1 length invariant
  - None on missing coords (honest suppression, no fabrication)
  - mode/minutes match _haversine_km + _hop_mode_minutes helpers
  - Idempotency (attach_intracity_transport twice → no change)
  - var-0 determinism (same input → same output on two independent calls)
  - Regression: intracity_hops unchanged (append-only, zero regression)
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from utils.intracity_transport import (  # noqa: E402
    _coord_of_item,
    _haversine_km,
    _hop_mode_minutes,
    attach_intracity_transport,
    build_item_transport_gaps,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────────────────────────────────────

def _att(name: str, lat: float, lon: float) -> dict:
    return {"name": name, "lat": lat, "lon": lon, "category": "monument"}


def _meal(name: str, lat: float, lon: float) -> dict:
    return {"name": name, "lat": lat, "lon": lon, "cuisine": "local"}


def _day(attractions=None, meals=None) -> dict:
    return {
        "day_index": 0,
        "attractions": attractions or [],
        "meals": meals or {},
    }


def _leg(city: str = "tokyo") -> dict:
    return {
        "leg_id": "leg-0",
        "city": city,
        "hotel_title": "Test Hotel",
        "checkin": "2026-10-01",
        "checkout": "2026-10-03",
    }


def _dp(city: str = "tokyo", days=None) -> dict:
    return {"leg_id": "leg-0", "city": city, "country": "Japan", "days": days or []}


# ─────────────────────────────────────────────────────────────────────────────
# Timeline ORDER
# ─────────────────────────────────────────────────────────────────────────────

class TestTimelineOrder(unittest.TestCase):
    """Verify mealAnchoredTimeline ordering:
    breakfast → morning attractions (⌈N/2⌉) → lunch → afternoon → tea → dinner → supper.
    """

    def test_breakfast_before_first_attraction(self):
        """breakfast appears before the first morning attraction in the gap list."""
        day = _day(
            attractions=[_att("Temple", 10.1, 10.1)],
            meals={"breakfast": _meal("Cafe", 10.0, 10.0)},
        )
        # Timeline: Cafe(breakfast) → Temple → 1 gap
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(len(gaps), 1)
        g = gaps[0]
        self.assertIsNotNone(g)
        # Verify gap matches haversine of (10.0, 10.0) → (10.1, 10.1)
        km = _haversine_km((10.0, 10.0), (10.1, 10.1))
        expected_mode, expected_min = _hop_mode_minutes(km)
        self.assertEqual(g["mode"], expected_mode)
        self.assertEqual(g["minutes"], expected_min)

    def test_morning_split_before_lunch_before_afternoon(self):
        """With 3 attractions: mid=2; order is breakfast→att1→att2→lunch→att3 = 5 items = 4 gaps."""
        day = _day(
            attractions=[
                _att("Att1", 10.0, 10.0),
                _att("Att2", 10.1, 10.1),
                _att("Att3", 10.2, 10.2),
            ],
            meals={
                "breakfast": _meal("BFast", 9.9, 9.9),
                "lunch": _meal("Lunch", 10.05, 10.05),
            },
        )
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(len(gaps), 4)

    def test_full_slot_order_all_present(self):
        """All slots present: breakfast→att1→att2→lunch→att3→tea→dinner→supper = 8 items = 7 gaps."""
        day = _day(
            attractions=[
                _att("Att1", 10.0, 10.0),
                _att("Att2", 10.1, 10.1),
                _att("Att3", 10.2, 10.2),
            ],
            meals={
                "breakfast": _meal("BF", 9.9, 9.9),
                "lunch": _meal("L", 10.05, 10.05),
                "tea": _meal("T", 10.15, 10.15),
                "dinner": _meal("D", 10.3, 10.3),
                "supper": _meal("S", 10.4, 10.4),
            },
        )
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(len(gaps), 7)

    def test_meal_slots_order_breakfast_before_dinner(self):
        """Meals appear in _MEAL_SLOTS order regardless of dict insertion order."""
        # breakfast gap appears before dinner gap
        day = _day(
            meals={
                "dinner": _meal("Dinner", 35.69, 139.76),
                "breakfast": _meal("BFast", 35.72, 139.78),
            }
        )
        # Timeline: BFast → Dinner = 2 items → 1 gap
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(len(gaps), 1)
        # The one gap is BFast(35.72,139.78) → Dinner(35.69,139.76)
        km = _haversine_km((35.72, 139.78), (35.69, 139.76))
        expected_mode, expected_min = _hop_mode_minutes(km)
        g = gaps[0]
        self.assertIsNotNone(g)
        self.assertEqual(g["mode"], expected_mode)
        self.assertEqual(g["minutes"], expected_min)

    def test_no_items_returns_empty(self):
        """Empty day (no attractions, no meals) → empty list."""
        day = _day()
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(gaps, [])

    def test_single_item_returns_empty(self):
        """Only one timeline item → empty list (no pairs, no gaps)."""
        day = _day(meals={"breakfast": _meal("Cafe", 10.0, 10.0)})
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(gaps, [])


# ─────────────────────────────────────────────────────────────────────────────
# N−1 length invariant
# ─────────────────────────────────────────────────────────────────────────────

class TestLengthInvariant(unittest.TestCase):
    def test_n_minus_1_length_for_various_attraction_counts(self):
        """gaps length is exactly N−1 for all attraction counts 0..5."""
        for n_att in range(0, 6):
            atts = [_att(f"A{i}", 10.0 + i * 0.01, 10.0) for i in range(n_att)]
            meals = {
                "breakfast": _meal("BF", 9.8, 9.8),
                "lunch": _meal("L", 10.0 + n_att * 0.005, 10.0),
                "dinner": _meal("D", 10.0 + n_att * 0.01 + 0.005, 10.0),
            }
            day = _day(attractions=atts, meals=meals)
            gaps = build_item_transport_gaps(day, _leg(), "tokyo")
            # Total items = 3 meals + n_att attractions
            expected_items = 3 + n_att
            if expected_items < 2:
                self.assertEqual(gaps, [], f"n_att={n_att}: expected [] got {gaps}")
            else:
                self.assertEqual(
                    len(gaps), expected_items - 1,
                    f"n_att={n_att}: expected {expected_items-1} gaps, got {len(gaps)}",
                )

    def test_nameless_attraction_excluded_from_count(self):
        """Nameless attractions are excluded (mirrors displayName filter) → N adjusted."""
        day = _day(
            attractions=[
                {"category": "tourism=unknown"},       # no name → excluded
                _att("Named", 10.0, 10.0),
            ],
            meals={"breakfast": _meal("Cafe", 9.9, 9.9)},
        )
        # Effective timeline: Cafe → Named = 2 items → 1 gap
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(len(gaps), 1)


# ─────────────────────────────────────────────────────────────────────────────
# None on missing coords
# ─────────────────────────────────────────────────────────────────────────────

class TestNoneOnMissingCoords(unittest.TestCase):
    def test_none_when_first_item_lacks_coords(self):
        """Gap is None when the first of a pair lacks lat/lon."""
        day = _day(
            attractions=[_att("Real", 10.0, 10.0)],
            meals={"breakfast": {"name": "No Coord Cafe"}},  # no lat/lon
        )
        # Timeline: NoCoordCafe → Real → 1 gap → None
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(len(gaps), 1)
        self.assertIsNone(gaps[0])

    def test_none_when_second_item_lacks_coords(self):
        """Gap is None when the second of a pair lacks lat/lon."""
        day = _day(
            attractions=[{"name": "No Coord Place"}],  # no lat/lon
            meals={"breakfast": _meal("Cafe", 10.0, 10.0)},
        )
        # Timeline: Cafe → NoCoordPlace → 1 gap → None
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(len(gaps), 1)
        self.assertIsNone(gaps[0])

    def test_none_does_not_suppress_independent_pairs(self):
        """A None gap from one pair does not suppress an adjacent independent pair."""
        day = _day(
            attractions=[
                _att("A", 10.0, 10.0),     # has coord
                {"name": "B"},              # no coord → gap A→B = None, gap B→C = None
                _att("C", 10.2, 10.2),     # has coord
            ],
        )
        # Timeline (no meals): A, B, C → 2 gaps
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(len(gaps), 2)
        self.assertIsNone(gaps[0])  # A→B: B has no coord
        self.assertIsNone(gaps[1])  # B→C: B has no coord

    def test_valid_gap_alongside_none_gaps(self):
        """A valid coord pair surrounded by None gaps is returned correctly."""
        day = _day(
            attractions=[
                {"name": "NoCoord1"},           # no coord
                _att("A", 35.7, 139.7),         # has coord → gap NoCoord1→A = None
                _att("B", 35.71, 139.71),        # has coord → gap A→B = valid
            ],
            meals={"dinner": {"name": "NoCoordDinner"}},  # no coord → gap B→NoCoordDinner = None
        )
        # Timeline: NoCoord1, A, B, NoCoordDinner → 3 gaps
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(len(gaps), 3)
        self.assertIsNone(gaps[0])        # NoCoord1→A: NoCoord1 has no coord
        self.assertIsNotNone(gaps[1])     # A→B: both have coords
        self.assertIsNone(gaps[2])        # B→NoCoordDinner: NoCoordDinner has no coord


# ─────────────────────────────────────────────────────────────────────────────
# mode/minutes match haversine helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestModeMinutesMatchHelpers(unittest.TestCase):
    def test_mode_and_minutes_derived_from_haversine(self):
        """Gap mode/minutes exactly match _hop_mode_minutes(_haversine_km(coord_a, coord_b))."""
        a = _att("A", 35.7, 139.7)
        b = _att("B", 35.71, 139.71)
        day = _day(attractions=[a, b])
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        self.assertEqual(len(gaps), 1)
        g = gaps[0]
        self.assertIsNotNone(g)
        coord_a = (35.7, 139.7)
        coord_b = (35.71, 139.71)
        km = _haversine_km(coord_a, coord_b)
        expected_mode, expected_minutes = _hop_mode_minutes(km)
        self.assertEqual(g["mode"], expected_mode)
        self.assertEqual(g["minutes"], expected_minutes)

    def test_estimate_always_true(self):
        """All returned gap dicts have estimate=True."""
        day = _day(attractions=[_att("A", 35.7, 139.7), _att("B", 35.75, 139.75)])
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        for g in gaps:
            if g is not None:
                self.assertTrue(g["estimate"], f"expected estimate=True, got {g}")

    def test_minutes_always_divisible_by_5(self):
        """All returned minutes are bucketed to 5-min increments (no fabricated precision)."""
        day = _day(
            attractions=[_att("X", 35.68, 139.77), _att("Y", 35.72, 139.82)],
            meals={"lunch": _meal("L", 35.70, 139.80)},
        )
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        for g in gaps:
            if g is not None:
                self.assertEqual(
                    g["minutes"] % 5, 0,
                    f"minutes {g['minutes']} not divisible by 5: {g}",
                )

    def test_walk_for_very_close_items(self):
        """Very close items (< 1.5 km apart) → walk mode."""
        a = _att("A", 35.700, 139.700)
        b = _att("B", 35.701, 139.701)  # ~140 m apart
        day = _day(attractions=[a, b])
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        g = gaps[0]
        self.assertIsNotNone(g)
        self.assertEqual(g["mode"], "walk")

    def test_taxi_for_distant_items(self):
        """Distant items (> 8 km apart) → taxi mode."""
        a = _att("A", 35.0, 139.0)
        b = _att("B", 35.1, 139.0)  # ~11 km apart (1° lat ≈ 111 km)
        day = _day(attractions=[a, b])
        gaps = build_item_transport_gaps(day, _leg(), "tokyo")
        g = gaps[0]
        self.assertIsNotNone(g)
        self.assertEqual(g["mode"], "taxi")


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────────────────────

class TestIdempotency(unittest.TestCase):
    def test_attach_sets_item_transport_gaps(self):
        """attach_intracity_transport sets item_transport_gaps on each day dict."""
        day = _day(attractions=[_att("X", 35.7, 139.7), _att("Y", 35.75, 139.75)])
        dp = _dp("tokyo", [day])
        attach_intracity_transport([dp], [_leg()])
        self.assertIn("item_transport_gaps", dp["days"][0])

    def test_attach_item_transport_gaps_idempotent(self):
        """Second attach call does not overwrite item_transport_gaps (idempotent guard)."""
        day = _day(attractions=[_att("X", 35.7, 139.7)])
        dp = _dp("tokyo", [day])
        leg = _leg()
        attach_intracity_transport([dp], [leg])
        after_first = json.dumps(dp["days"][0].get("item_transport_gaps"), sort_keys=True)
        # Mutate the field to detect if it's overwritten
        dp["days"][0]["item_transport_gaps"] = [{"sentinel": True}]
        attach_intracity_transport([dp], [leg])
        after_second = json.dumps(dp["days"][0].get("item_transport_gaps"), sort_keys=True)
        # Should NOT have been overwritten — the sentinel must survive
        self.assertIn("sentinel", after_second, "idempotency guard failed: field was overwritten")

    def test_full_idempotency_unchanged_json(self):
        """Calling attach_intracity_transport twice on the same objects → identical JSON."""
        day = _day(
            attractions=[_att("Senso-ji", 35.7148, 139.7967)],
            meals={"lunch": _meal("Ramen House", 35.7002, 139.7753)},
        )
        dp = _dp("tokyo", [day])
        leg = _leg()
        attach_intracity_transport([dp], [leg])
        snapshot_first = json.dumps(dp, sort_keys=True)
        attach_intracity_transport([dp], [leg])
        snapshot_second = json.dumps(dp, sort_keys=True)
        self.assertEqual(snapshot_first, snapshot_second,
                         "second attach_intracity_transport call must be a pure no-op")


# ─────────────────────────────────────────────────────────────────────────────
# var-0 determinism
# ─────────────────────────────────────────────────────────────────────────────

class TestVar0Determinism(unittest.TestCase):
    def test_same_input_same_output_twice(self):
        """Identical inputs produce byte-identical output on two independent calls (var-0)."""
        day_a = _day(
            attractions=[_att("Senso-ji", 35.7148, 139.7967)],
            meals={"lunch": _meal("Ramen", 35.7002, 139.7753)},
        )
        day_b = copy.deepcopy(day_a)
        leg = _leg("tokyo")

        result_a = build_item_transport_gaps(day_a, leg, "tokyo")
        result_b = build_item_transport_gaps(day_b, leg, "tokyo")

        self.assertEqual(
            json.dumps(result_a, sort_keys=True),
            json.dumps(result_b, sort_keys=True),
            "build_item_transport_gaps must be byte-identical for identical input (var-0)",
        )

    def test_attach_var0_across_deep_copies(self):
        """attach_intracity_transport on two deep-copied identical inputs → identical item_transport_gaps."""
        day = _day(
            attractions=[
                _att("Temple", 35.71, 139.80),
                _att("Garden", 35.72, 139.81),
            ],
            meals={
                "breakfast": _meal("Cafe", 35.70, 139.79),
                "dinner": _meal("Bistro", 35.73, 139.82),
            },
        )
        dp_a = _dp("tokyo", [day])
        dp_b = copy.deepcopy(dp_a)
        leg_a = _leg()
        leg_b = copy.deepcopy(leg_a)

        attach_intracity_transport([dp_a], [leg_a])
        attach_intracity_transport([dp_b], [leg_b])

        gaps_a = json.dumps(dp_a["days"][0].get("item_transport_gaps"), sort_keys=True)
        gaps_b = json.dumps(dp_b["days"][0].get("item_transport_gaps"), sort_keys=True)
        self.assertEqual(gaps_a, gaps_b,
                         "item_transport_gaps must be var-0 byte-identical across deep-copied runs")

    def test_build_is_pure_no_side_effects_on_input(self):
        """build_item_transport_gaps does not mutate the day dict."""
        day = _day(
            attractions=[_att("A", 35.7, 139.7)],
            meals={"lunch": _meal("L", 35.72, 139.72)},
        )
        snapshot_before = json.dumps(day, sort_keys=True)
        build_item_transport_gaps(day, _leg(), "tokyo")
        snapshot_after = json.dumps(day, sort_keys=True)
        self.assertEqual(snapshot_before, snapshot_after,
                         "build_item_transport_gaps must not mutate the input day dict")


# ─────────────────────────────────────────────────────────────────────────────
# Regression: intracity_hops unchanged (zero-regression append-only)
# ─────────────────────────────────────────────────────────────────────────────

class TestNoRegression(unittest.TestCase):
    def test_intracity_hops_still_attached(self):
        """intracity_hops is still set after adding item_transport_gaps (append-only)."""
        day = _day(attractions=[_att("X", 35.7, 139.7)])
        dp = _dp("tokyo", [day])
        attach_intracity_transport([dp], [_leg()])
        d = dp["days"][0]
        self.assertIn("intracity_hops", d, "intracity_hops must still be set")
        self.assertIn("item_transport_gaps", d, "item_transport_gaps must be added")

    def test_both_fields_present_simultaneously(self):
        """intracity_hops and item_transport_gaps can coexist; neither silences the other."""
        day = _day(
            attractions=[_att("A", 35.7, 139.7), _att("B", 35.75, 139.75)],
            meals={"lunch": _meal("L", 35.72, 139.72)},
        )
        dp = _dp("tokyo", [day])
        attach_intracity_transport([dp], [_leg()])
        d = dp["days"][0]
        self.assertIsInstance(d["intracity_hops"], list)
        self.assertIsInstance(d["item_transport_gaps"], list)
        # Both non-empty for this input
        self.assertGreater(len(d["intracity_hops"]), 0)
        self.assertGreater(len(d["item_transport_gaps"]), 0)

    def test_attractions_and_meals_unchanged_after_attach(self):
        """Attractions and meals are not mutated when item_transport_gaps is attached."""
        day = _day(
            attractions=[_att("A", 35.7, 139.7)],
            meals={"dinner": _meal("D", 35.72, 139.72)},
        )
        dp = _dp("tokyo", [day])
        before_att = json.dumps(dp["days"][0]["attractions"], sort_keys=True)
        before_meals = json.dumps(dp["days"][0]["meals"], sort_keys=True)
        attach_intracity_transport([dp], [_leg()])
        after_att = json.dumps(dp["days"][0]["attractions"], sort_keys=True)
        after_meals = json.dumps(dp["days"][0]["meals"], sort_keys=True)
        self.assertEqual(before_att, after_att, "attractions must not be mutated")
        self.assertEqual(before_meals, after_meals, "meals must not be mutated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
