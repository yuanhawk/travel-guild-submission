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

    def test_bali_alias_resolves_to_denpasar(self):
        """
        UX-audit regression (2026-07): the merchant hotel catalog books Bali
        hotels under city="bali" (e.g. bali-alaya-ubud) -- one of the site's
        own 6 featured landing-page destinations -- but poi_catalog.json only
        seeds Bali's POI/restaurant data under its capital city "denpasar".
        Without the alias, _resolve_catalog_entry("ID", "bali") returned None
        and every Bali trip silently booked a real hotel but got a completely
        EMPTY day-by-day plan with a raw internal error string leaked to the
        UI ("no POI catalog entry for 'ID:bali'").
        """
        D._POI_CATALOG.pop("ID:bali", None)
        D._POI_CATALOG["ID:denpasar"] = {
            "attractions": [{"name_en": "Uluwatu Temple"}], "restaurants": [],
        }
        entry, key = D._resolve_catalog_entry("ID", "bali")
        self.assertEqual(key, "ID:denpasar")
        self.assertTrue(entry)

    def test_jeju_island_alias_resolves_to_jeju_city(self):
        """
        UX-audit regression (2026-07): the site's own destination picker reads
        "Jeju Island" verbatim, but the catalog seeds it under "jeju city" --
        the bare "jeju" already bridges via the " city" suffix rule, but
        " island" is not a suffix that rule strips, so the exact phrase shown
        on the homepage failed the same way Bali did.
        """
        D._POI_CATALOG.pop("KR:jeju island", None)
        D._POI_CATALOG["KR:jeju city"] = {
            "attractions": [{"name_en": "Hallasan"}], "restaurants": [],
        }
        entry, key = D._resolve_catalog_entry("KR", "jeju island")
        self.assertEqual(key, "KR:jeju city")
        self.assertTrue(entry)

    def test_ulaanbaatar_alias_resolves_to_ulan_bator(self):
        """
        B2 audit regression (2026-07-08): the catalog seeds Mongolia's capital only under the
        older romanization "ulan bator", but "ulaanbaatar" -- the modern spelling virtually
        everyone types today -- is not bridged by any suffix rule, so it failed the same way
        Bali did: a real booking, an EMPTY day-by-day plan.
        """
        D._POI_CATALOG.pop("MN:ulaanbaatar", None)
        D._POI_CATALOG["MN:ulan bator"] = {
            "attractions": [{"name_en": "Gandantegchinlen Monastery"}], "restaurants": [],
        }
        entry, key = D._resolve_catalog_entry("MN", "ulaanbaatar")
        self.assertEqual(key, "MN:ulan bator")
        self.assertTrue(entry)

    def test_ndjamena_alias_resolves_to_apostrophe_spelling(self):
        """
        B2 audit regression (2026-07-08): the catalog seeds Chad's capital only as "n'djamena"
        (with an apostrophe), but "ndjamena" -- how most people would actually type it -- is not
        bridged by any suffix rule, so it failed the same way Bali did.
        """
        D._POI_CATALOG.pop("TD:ndjamena", None)
        D._POI_CATALOG["TD:n'djamena"] = {
            "attractions": [{"name_en": "Grande Mosquee de N'Djamena"}], "restaurants": [],
        }
        entry, key = D._resolve_catalog_entry("TD", "ndjamena")
        self.assertEqual(key, "TD:n'djamena")
        self.assertTrue(entry)

    def test_gondar_alias_resolves_to_gonder(self):
        """
        B2 audit regression (2026-07-08): the catalog seeds Ethiopia's historic city only under
        the alternate transliteration "gonder", but "gondar" -- the far more common English
        spelling -- is not bridged by any suffix rule, so it failed the same way Bali did.
        """
        D._POI_CATALOG.pop("ET:gondar", None)
        D._POI_CATALOG["ET:gonder"] = {
            "attractions": [{"name_en": "Fasil Ghebbi"}], "restaurants": [],
        }
        entry, key = D._resolve_catalog_entry("ET", "gondar")
        self.assertEqual(key, "ET:gonder")
        self.assertTrue(entry)


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
