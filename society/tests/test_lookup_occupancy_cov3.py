"""test_lookup_occupancy_cov3.py — regression: the in-process merchant transport's lookup_catalog
MUST carry max_occupancy inside the "hotel" blob.

The Critic reads lookup["hotel"]["max_occupancy"] (critic_agent.py:732/745) and treats its ABSENCE
as unprovable capacity → conservative OVER_CAPACITY flag for 2+ adults. The transport previously
dropped it, falsely declining EVERY couple (2 adults) even when the catalog row had occupancy=4.
The real Go merchant returns the full hotel struct, so this only affected the demo path.
"""

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


def _transport_with(catalog):
    # __new__ skips __init__ (avoids loading the 42k-hotel catalog); _lookup_catalog uses _catalog only.
    t = srv._LocalCatalogTransport.__new__(srv._LocalCatalogTransport)
    t._catalog = catalog
    t._food_catalog = []
    t._checkouts = {}
    t._co_seq = 0
    t._lock = threading.Lock()
    return t


class TestLookupCarriesOccupancy(unittest.TestCase):
    HOTEL = {"id": "test-hotel", "price_cents_per_night": 10000, "max_occupancy": 4,
             "review_score": 8.0, "star_rating": 4}

    def _lookup(self, hotel):
        t = _transport_with([hotel])
        return t._lookup_catalog({"product": {
            "hotel_id": hotel["id"], "checkin": "2026-10-01", "checkout": "2026-10-08"}})

    def test_hotel_blob_carries_max_occupancy(self):
        sc, status = self._lookup(self.HOTEL)
        self.assertEqual(status, 200)
        # the exact key path the Critic reads:
        self.assertEqual(sc["hotel"]["max_occupancy"], 4)

    def test_absent_catalog_occupancy_becomes_zero_not_missing(self):
        # a row with no occupancy → key present as 0 (the Critic's >0 guard handles 0; the point is
        # the key is never simply absent, which is the case the conservative flag punishes).
        h = dict(self.HOTEL); h.pop("max_occupancy")
        sc, _ = self._lookup(h)
        self.assertIn("max_occupancy", sc["hotel"])
        self.assertEqual(sc["hotel"]["max_occupancy"], 0)

    def test_two_adults_within_occupancy_is_provable(self):
        # mirror the Critic's check: max_occ(4) >= adults(2) → not over-capacity (couple books).
        sc, _ = self._lookup(self.HOTEL)
        max_occ = int(sc["hotel"].get("max_occupancy", 0) or 0)
        adults = 2
        self.assertTrue(max_occ > 0 and adults <= max_occ)


if __name__ == "__main__":
    unittest.main(verbosity=2)
