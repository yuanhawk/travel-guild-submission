"""test_megacity_wards_cov3.py — #76 megacity ward aggregation in the in-process catalog transport.
A parent-megacity search ("tokyo") must also return its wards' lodging (OSM tags Tokyo hotels by ward,
leaving the parent near-empty), presented UNDER the parent with the ward as the area. A standalone
ward search ("nakano") is unchanged, and a non-megacity city is unaffected. Deterministic → var-0."""

from __future__ import annotations

import os
import sys
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from orchestration import server as srv  # noqa: E402


def _transport(catalog, wards):
    t = srv._LocalCatalogTransport.__new__(srv._LocalCatalogTransport)
    t._catalog = catalog
    t._food_catalog = []
    t._closed_ids = set()
    t._megacity_wards = {k: set(v) for k, v in wards.items()}
    t._checkouts = {}
    t._co_seq = 0
    t._lock = threading.Lock()
    return t


_CAT = [
    {"id": "t1", "city": "tokyo", "title": "Tokyo Central", "price_cents_per_night": 10000, "max_occupancy": 4},
    {"id": "n1", "city": "nakano", "title": "Nakano Inn", "price_cents_per_night": 8000, "max_occupancy": 4},
    {"id": "s1", "city": "shinjuku", "title": "Shinjuku Lodge", "price_cents_per_night": 9000, "max_occupancy": 4},
    {"id": "o1", "city": "osaka", "title": "Osaka Hotel", "price_cents_per_night": 7000, "max_occupancy": 4},
]
_WARDS = {"tokyo": ["nakano", "shinjuku"]}


def _ids(t, city):
    sc = t._search_catalog({"query": {"city": city, "checkin": "2026-10-01",
                                      "checkout": "2026-10-05", "adults": 2}})
    return sc["results"]


class TestMegacityWards(unittest.TestCase):
    def test_parent_aggregates_its_wards(self):
        t = _transport(_CAT, _WARDS)
        r = _ids(t, "tokyo")
        self.assertEqual({x["hotel_id"] for x in r}, {"t1", "n1", "s1"})  # tokyo + 2 wards, NOT osaka
        nakano = next(x for x in r if x["hotel_id"] == "n1")
        self.assertEqual(nakano["city"], "tokyo")     # presented under the parent
        self.assertEqual(nakano["area"], "nakano")    # ward becomes the area

    def test_standalone_ward_not_reparented(self):
        t = _transport(_CAT, _WARDS)
        r = _ids(t, "nakano")
        self.assertEqual({x["hotel_id"] for x in r}, {"n1"})   # only Nakano, not all of Tokyo
        self.assertEqual(r[0]["city"], "nakano")               # stays Nakano

    def test_non_megacity_city_unaffected(self):
        t = _transport(_CAT, _WARDS)
        r = _ids(t, "osaka")
        self.assertEqual({x["hotel_id"] for x in r}, {"o1"})   # not a megacity here → no aggregation

    def test_empty_wards_map_is_byte_identical(self):
        # absent map → no aggregation → byte-identical to the legacy single-city behavior (var-0)
        t = _transport(_CAT, {})
        self.assertEqual({x["hotel_id"] for x in _ids(t, "tokyo")}, {"t1"})

    def test_reparented_ward_hotel_keeps_its_own_price(self):
        # BOOKING INTEGRITY: a ward hotel served under "tokyo" must price at ITS rate, not the parent's
        t = _transport(_CAT, _WARDS)
        nakano = next(x for x in _ids(t, "tokyo") if x["hotel_id"] == "n1")
        self.assertEqual(nakano["total_cents"], 8000 * 4)  # Nakano's 8000/n × 4 nights, not Tokyo's

    def test_filters_apply_to_ward_hotels(self):
        # closed-id + budget filters must still exclude ward hotels in an aggregated parent search
        t = _transport(_CAT, _WARDS)
        t._closed_ids = {"n1"}
        self.assertNotIn("n1", {x["hotel_id"] for x in _ids(t, "tokyo")})  # closed ward excluded
        t2 = _transport(_CAT, _WARDS)
        sc = t2._search_catalog({"query": {"city": "tokyo", "checkin": "2026-10-01",
                                           "checkout": "2026-10-05", "adults": 2, "max_cents": 33000}})
        ids = {x["hotel_id"] for x in sc["results"]}
        self.assertIn("n1", ids)        # Nakano 8000×4=32000 ≤ 33000
        self.assertNotIn("s1", ids)     # Shinjuku 9000×4=36000 > 33000 → budget-cut (ward respects filter)


if __name__ == "__main__":
    unittest.main(verbosity=2)
