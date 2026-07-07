"""test_city_suffix_normalization_cov3.py — #76 JP admin-suffix bridge (nara ↔ nara-shi).

The seed keys some Japanese cities by their worldcities name "nara-shi" (市 = city) while the
itinerary uses the bare "nara". Both the day-planner POI lookup (_resolve_catalog_entry) and the
in-process catalog/lodging search (_search_catalog) must bridge the suffix in both directions, and
leave non-Japanese / suffix-free cities byte-identical (var-0)."""

from __future__ import annotations

import os
import sys
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from agents import day_planner_agent as D  # noqa: E402
from orchestration import server as srv     # noqa: E402


class TestPOISuffix(unittest.TestCase):
    def setUp(self):
        self._orig = dict(D._POI_CATALOG)

    def tearDown(self):
        D._POI_CATALOG.clear()
        D._POI_CATALOG.update(self._orig)

    def test_bare_name_resolves_to_shi_key(self):
        D._POI_CATALOG.pop("JP:nara", None)
        D._POI_CATALOG["JP:nara-shi"] = {"attractions": [{"name_en": "Todai-ji"}], "restaurants": []}
        entry, key = D._resolve_catalog_entry("JP", "nara")
        self.assertEqual(key, "JP:nara-shi")     # nara -> nara-shi
        self.assertTrue(entry)

    def test_shi_key_resolves_to_bare(self):
        D._POI_CATALOG.pop("JP:nara-shi", None)
        D._POI_CATALOG["JP:nara"] = {"attractions": [{"name_en": "Todai-ji"}], "restaurants": []}
        entry, key = D._resolve_catalog_entry("JP", "nara-shi")
        self.assertEqual(key, "JP:nara")         # nara-shi -> nara (reverse)
        self.assertTrue(entry)

    def test_genuine_miss_still_misses(self):
        D._POI_CATALOG.pop("JP:atlantis", None)
        D._POI_CATALOG.pop("JP:atlantis-shi", None)
        entry, key = D._resolve_catalog_entry("JP", "atlantis")
        self.assertIsNone(entry)                 # no fabricated hit


def _transport(catalog):
    t = srv._LocalCatalogTransport.__new__(srv._LocalCatalogTransport)
    t._catalog = catalog
    t._food_catalog = []
    t._closed_ids = set()
    t._megacity_wards = {}
    t._checkouts = {}
    t._co_seq = 0
    t._lock = threading.Lock()
    return t


_CAT = [
    {"id": "a", "city": "nara-shi", "title": "Nara Grand", "price_cents_per_night": 10000, "max_occupancy": 4},
    {"id": "b", "city": "paris", "title": "Paris Hotel", "price_cents_per_night": 9000, "max_occupancy": 4},
]


class TestCatalogSuffix(unittest.TestCase):
    def _ids(self, t, city):
        return t._search_catalog({"query": {"city": city, "checkin": "2026-10-01",
                                            "checkout": "2026-10-05", "adults": 2}})["results"]

    def test_bare_query_finds_shi_lodging(self):
        t = _transport(_CAT)
        r = self._ids(t, "nara")
        self.assertEqual({x["hotel_id"] for x in r}, {"a"})  # finds nara-shi, not paris
        self.assertEqual(r[0]["city"], "nara")               # presented under the queried name
        self.assertEqual(r[0]["total_cents"], 10000 * 4)     # prices at the seeded rate (booking ok)

    def test_suffix_free_city_unaffected(self):
        t = _transport(_CAT)
        self.assertEqual({x["hotel_id"] for x in self._ids(t, "paris")}, {"b"})  # var-0: no spurious match

    def test_result_carries_state_key(self):
        t = _transport(_CAT)
        self.assertIn("state", self._ids(t, "paris")[0])  # city, state, country structure present


class TestStateResolver(unittest.TestCase):
    """_resolve_state: manual overrides + the suffix fallback work WITHOUT the worldcities file (so
    these hold in CI); the worldcities-backed lookups (e.g. nara->Nara) are verified on the dev box."""

    def test_manual_override(self):
        self.assertEqual(srv._resolve_state("burayu"), "Oromia")     # absent from worldcities
        self.assertEqual(srv._resolve_state("chuo city"), "Tokyo")   # override (suffix-free)

    def test_override_suffix_fallback(self):
        self.assertEqual(srv._resolve_state("ciudad camilo cienfuegos"), "Mayabeque")

    def test_unknown_city_no_fabrication(self):
        self.assertEqual(srv._resolve_state("zzznotacity"), "")      # honest empty, never invented


if __name__ == "__main__":
    unittest.main(verbosity=2)
