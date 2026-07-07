"""test_business_status_cov3.py — #63: Places business-status matching + the deterministic
closed-lodging filter (no network). The live Places call is verified out-of-band (Tokyo: Khaosan
→ UNVERIFIED, Park Hyatt/Mitsui → OPERATIONAL)."""

from __future__ import annotations

import os
import sys
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from utils import places_status as ps  # noqa: E402
from orchestration import server as srv  # noqa: E402


class TestDistinctiveMatch(unittest.TestCase):
    def test_strips_generic_and_city(self):
        # the hotel's distinctive tokens drop "hostel"/city → match on the real name part
        self.assertEqual(ps._distinctive("Khaosan Tokyo Ninja Hostel", "tokyo", "Japan"),
                         {"khaosan", "ninja"})
        self.assertEqual(ps._distinctive("Park Hyatt Tokyo", "tokyo", "Japan"), {"hyatt", "park"})
        # a generic-only name shares nothing distinctive with a different hotel
        self.assertFalse(ps._distinctive("Asakusa Hostel", "tokyo", "Japan")
                         & ps._distinctive("Khaosan Tokyo Ninja Hostel", "tokyo", "Japan"))

    def test_gate_off_returns_none(self):
        orig_en, orig_key = ps.PLACES_ENABLED, ps.PLACES_KEY
        ps.PLACES_ENABLED = False
        try:
            self.assertIsNone(ps.business_status("Some Hotel", "tokyo"))  # disabled → no call
        finally:
            ps.PLACES_ENABLED, ps.PLACES_KEY = orig_en, orig_key


def _transport_with(catalog, closed_ids):
    t = srv._LocalCatalogTransport.__new__(srv._LocalCatalogTransport)
    t._catalog = catalog
    t._food_catalog = []
    t._closed_ids = set(closed_ids)
    t._megacity_wards = {}
    t._checkouts = {}
    t._co_seq = 0
    t._lock = threading.Lock()
    return t


class TestClosedFilter(unittest.TestCase):
    CATALOG = [
        {"id": "x-open", "city": "tokyo", "title": "Open Hotel", "price_cents_per_night": 10000,
         "max_occupancy": 4},
        {"id": "x-closed", "city": "tokyo", "title": "Closed Hostel", "price_cents_per_night": 3000,
         "max_occupancy": 4},
    ]

    def _ids(self, transport):
        sc = transport._search_catalog({"query": {
            "city": "tokyo", "checkin": "2026-10-01", "checkout": "2026-10-05", "adults": 2}})
        return [r["hotel_id"] for r in sc.get("results", [])]

    def test_closed_lodging_excluded(self):
        t = _transport_with(self.CATALOG, {"x-closed"})
        ids = self._ids(t)
        self.assertIn("x-open", ids)
        self.assertNotIn("x-closed", ids)          # never recommended

    def test_no_status_map_keeps_all(self):
        # empty/absent status map → nothing excluded (unchecked cities unaffected → byte-identical)
        t = _transport_with(self.CATALOG, set())
        self.assertEqual(set(self._ids(t)), {"x-open", "x-closed"})


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestLodgingJunkFilter(unittest.TestCase):
    """Bug B (curated-blocklist redesign) — a reviewed set of non-hotel ids (admin / convention
    centre) is never BOOKED; everything else (incl. lookalike titles like 'Convention Center') is
    kept — 0 false positives; a sole-junk city is surfaced WITH an honesty flag, never silently."""

    def setUp(self):
        # Pin the curated blocklist to a known test set (bypass the JSON file) + restore after.
        self._saved = srv._LODGING_JUNK_IDS
        srv._LODGING_JUNK_IDS = frozenset({"junk-circuit-house", "junk-convention"})

    def tearDown(self):
        srv._LODGING_JUNK_IDS = self._saved

    CATALOG = [
        {"id": "junk-circuit-house", "city": "ranchi", "title": "Circuit House", "provenance": "osm",
         "price_cents_per_night": 3000, "max_occupancy": 4},
        {"id": "junk-convention", "city": "ranchi", "title": "Balanghai Convention Center",
         "provenance": "osm", "price_cents_per_night": 2800, "max_occupancy": 4},
        {"id": "real-osm", "city": "ranchi", "title": "Hotel Capitol Hill", "provenance": "osm",
         "price_cents_per_night": 8000, "max_occupancy": 4},
        # a REAL hotel whose title LOOKS junky — must NOT be dropped (the name-matcher false positive)
        {"id": "lookalike", "city": "ranchi", "title": "Marriott at the Convention Center",
         "provenance": "osm", "price_cents_per_night": 12000, "max_occupancy": 4},
        {"id": "grounded", "city": "ranchi", "title": "Radisson Blu Ranchi",
         "provenance": "gemini-2.5-flash-lite-grounded", "price_cents_per_night": 15000, "max_occupancy": 4},
    ]

    def _search(self, catalog, max_cents=0):
        t = _transport_with(catalog, set())
        return t._search_catalog({"query": {"city": "ranchi", "checkin": "2026-10-04",
                                            "checkout": "2026-10-05", "adults": 2, "max_cents": max_cents}})

    def test_curated_junk_ids_excluded(self):
        ids = [r["hotel_id"] for r in self._search(self.CATALOG)["results"]]
        self.assertNotIn("junk-circuit-house", ids)
        self.assertNotIn("junk-convention", ids)
        self.assertIn("real-osm", ids)

    def test_lookalike_title_not_dropped(self):
        # the KEY win over name-matching: a real hotel named "...Convention Center" stays (id not listed)
        ids = [r["hotel_id"] for r in self._search(self.CATALOG)["results"]]
        self.assertIn("lookalike", ids)
        self.assertIn("grounded", ids)

    def test_sole_junk_city_flagged_not_silent(self):
        # a city whose ONLY listing is curated-junk → surfaced (never no_fit) BUT with honesty flag
        only_junk = [self.CATALOG[0], self.CATALOG[1]]
        res = self._search(only_junk)["results"]
        self.assertTrue(res)                                  # never stranded to zero
        self.assertTrue(all(r.get("unverified_lodging") for r in res))
        self.assertTrue(all("confirm before booking" in (r.get("note") or "") for r in res))

    def test_normal_results_have_no_unverified_flag(self):
        # when real hotels exist, results carry NO unverified flag (byte-clean normal path)
        res = self._search(self.CATALOG)["results"]
        self.assertTrue(all("unverified_lodging" not in r for r in res))

    def test_var0_search_deterministic(self):
        a = [r["hotel_id"] for r in self._search(self.CATALOG)["results"]]
        b = [r["hotel_id"] for r in self._search(self.CATALOG)["results"]]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
