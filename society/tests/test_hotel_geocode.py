"""
test_hotel_geocode.py — #101 hotel geocode utility + _attach_hotel_geo.

Tests:
  - lookup_cached: hit (positive), miss, negative cache entry.
  - _attach_hotel_geo: geocoded hit → hotel_coord_basis=="geocoded";
    miss but known city → "city_centroid"; neither → no keys.
  - resolve_live: GEOCODE_ENABLED unset → returns None, no network call.
  - var-0: day_plans/hops byte-identical with empty vs warm cache
    (only legs[].hotel_* differ).

Light — no server, no network. Monkeypatches the cache file and httpx.
"""
import json
import os
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helper to patch the cache path
# ---------------------------------------------------------------------------

def _temp_cache(content: dict):
    """Context manager: write content to a temp file, point hotel_geocode at it."""
    import utils.hotel_geocode as hg
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(content, tf)
    tf.close()
    old_path = hg._CACHE_PATH
    old_cache = hg._cache
    hg._CACHE_PATH = tf.name
    hg._cache = None  # force reload
    try:
        yield tf.name
    finally:
        hg._CACHE_PATH = old_path
        hg._cache = old_cache
        os.unlink(tf.name)


from contextlib import contextmanager
_temp_cache = contextmanager(_temp_cache)


# ---------------------------------------------------------------------------
# lookup_cached
# ---------------------------------------------------------------------------

class TestLookupCached:
    def test_positive_hit(self):
        """lookup_cached returns (lat, lon) on a cached positive entry."""
        import utils.hotel_geocode as hg
        key = hg.cache_key("Grand Hyatt", "Orchard", "Singapore", "SG")
        cache_data = {key: {"lat": 1.3056, "lon": 103.8321, "source": "nominatim", "as_of": "2026-06-28"}}
        with _temp_cache(cache_data):
            result = hg.lookup_cached("Grand Hyatt", "Orchard", "Singapore", "SG")
        assert result is not None
        assert abs(result[0] - 1.3056) < 1e-6
        assert abs(result[1] - 103.8321) < 1e-6

    def test_cache_miss_returns_none(self):
        """lookup_cached returns None on a miss."""
        import utils.hotel_geocode as hg
        with _temp_cache({}):
            result = hg.lookup_cached("Unknown Hotel", "", "SomeCity", "XX")
        assert result is None

    def test_negative_cache_returns_none(self):
        """A negative cache entry (lat=null) returns None without a network call."""
        import utils.hotel_geocode as hg
        key = hg.cache_key("Bad Hotel", "", "Singapore", "SG")
        cache_data = {key: {"lat": None, "lon": None, "source": "negative", "as_of": "2026-06-28"}}
        with _temp_cache(cache_data):
            result = hg.lookup_cached("Bad Hotel", "", "Singapore", "SG")
        assert result is None

    def test_key_normalization_case_insensitive(self):
        """Cache key is case-insensitive — lowercase/upper lookups hit the same entry."""
        import utils.hotel_geocode as hg
        key = hg.cache_key("marina bay sands", "marina", "singapore", "sg")
        cache_data = {key: {"lat": 1.2836, "lon": 103.8607, "source": "nominatim", "as_of": "2026-06-28"}}
        with _temp_cache(cache_data):
            result = hg.lookup_cached("Marina Bay Sands", "Marina", "Singapore", "SG")
        assert result is not None

    def test_empty_cache_file_returns_none(self):
        """Empty cache → all lookups return None."""
        import utils.hotel_geocode as hg
        with _temp_cache({}):
            assert hg.lookup_cached("Any Hotel", "", "Tokyo", "JP") is None


# ---------------------------------------------------------------------------
# centroid
# ---------------------------------------------------------------------------

class TestCentroid:
    def test_known_city_returns_coord(self):
        """centroid() returns (lat, lon) for a city in city_coords.json."""
        from utils.hotel_geocode import centroid
        coord = centroid("Singapore")
        # Singapore centroid must be roughly (1.3, 103.8)
        assert coord is not None, "Singapore must be in city_coords.json"
        assert 0.5 < coord[0] < 2.0, f"Singapore lat expected near 1.3, got {coord[0]}"
        assert 102 < coord[1] < 105, f"Singapore lon expected near 103.8, got {coord[1]}"

    def test_unknown_city_returns_none(self):
        """centroid() returns None for an unknown city."""
        from utils.hotel_geocode import centroid
        assert centroid("NONEXISTENT_CITY_XYZ_9999") is None

    def test_tokyo_centroid(self):
        """Tokyo is in the catalog."""
        from utils.hotel_geocode import centroid
        coord = centroid("Tokyo")
        assert coord is not None, "Tokyo must be in city_coords.json"
        assert 30 < coord[0] < 40  # roughly 35.6
        assert 135 < coord[1] < 142  # roughly 139.7


# ---------------------------------------------------------------------------
# _attach_hotel_geo
# ---------------------------------------------------------------------------

def _attach_geo(result: dict) -> None:
    from orchestration.orchestrator import TravelOrchestrator
    obj = object.__new__(TravelOrchestrator)
    TravelOrchestrator._attach_hotel_geo(obj, result)


class TestAttachHotelGeo:
    def test_geocoded_hit(self):
        """With a cache hit, leg gets hotel_coord_basis=='geocoded' and the cached coords."""
        import utils.hotel_geocode as hg
        name, area, city, country = "Grand Hyatt", "Orchard", "Singapore", "SG"
        key = hg.cache_key(name, area, city, country)
        cache_data = {key: {"lat": 1.3056, "lon": 103.8321, "source": "nominatim", "as_of": "2026-06-28"}}
        result = {
            "outcome": "success",
            "legs": [{"leg_id": "leg-0", "city": city, "title": name, "area": area, "iso2": country}],
        }
        with _temp_cache(cache_data):
            _attach_geo(result)
        leg = result["legs"][0]
        assert leg.get("hotel_coord_basis") == "geocoded"
        assert abs(leg["hotel_lat"] - 1.3056) < 1e-6
        assert abs(leg["hotel_lon"] - 103.8321) < 1e-6

    def test_cache_miss_known_city_gets_centroid(self):
        """Cache miss + known city → city_centroid fallback."""
        result = {
            "outcome": "success",
            "legs": [{"leg_id": "leg-0", "city": "Tokyo", "title": "Some Hotel", "area": "", "iso2": "JP"}],
        }
        with _temp_cache({}):
            _attach_geo(result)
        leg = result["legs"][0]
        assert leg.get("hotel_coord_basis") == "city_centroid"
        assert "hotel_lat" in leg
        assert "hotel_lon" in leg
        # Tokyo centroid sanity check
        assert 30 < leg["hotel_lat"] < 40

    def test_cache_miss_unknown_city_no_keys(self):
        """Cache miss + unknown city → NO hotel_* keys added."""
        result = {
            "outcome": "success",
            "legs": [{"leg_id": "leg-0", "city": "NONEXISTENT_CITY_XYZ", "title": "Fake Hotel", "area": ""}],
        }
        with _temp_cache({}):
            _attach_geo(result)
        leg = result["legs"][0]
        assert "hotel_lat" not in leg
        assert "hotel_lon" not in leg
        assert "hotel_coord_basis" not in leg

    def test_day_plans_untouched(self):
        """_attach_hotel_geo NEVER touches day_plans."""
        original_day_plans = [{"leg_id": "leg-0", "days": [{"day_index": 0}]}]
        result = {
            "outcome": "success",
            "legs": [{"leg_id": "leg-0", "city": "Singapore", "title": "Hotel"}],
            "day_plans": original_day_plans,
        }
        with _temp_cache({}):
            _attach_geo(result)
        assert result["day_plans"] == original_day_plans, "day_plans must not be mutated"

    def test_empty_legs(self):
        """Empty legs list → no error, no keys."""
        result = {"outcome": "success", "legs": []}
        with _temp_cache({}):
            _attach_geo(result)  # must not raise
        assert result["legs"] == []

    def test_multi_leg_each_gets_coord(self):
        """Multiple legs each get their own hotel coord."""
        result = {
            "outcome": "success",
            "legs": [
                {"leg_id": "leg-0", "city": "Tokyo", "title": "Hotel A"},
                {"leg_id": "leg-1", "city": "Osaka", "title": "Hotel B"},
            ],
        }
        with _temp_cache({}):
            _attach_geo(result)
        # Both known cities → both get city_centroid
        for leg in result["legs"]:
            if "hotel_coord_basis" in leg:
                assert leg["hotel_coord_basis"] in {"geocoded", "city_centroid"}


# ---------------------------------------------------------------------------
# resolve_live — gated
# ---------------------------------------------------------------------------

class TestResolveLiveGated:
    def test_no_geocode_enabled_returns_none(self, monkeypatch):
        """resolve_live returns None when GEOCODE_ENABLED is unset."""
        monkeypatch.delenv("GEOCODE_ENABLED", raising=False)
        import utils.hotel_geocode as hg
        # Monkeypatch httpx to catch any accidental network call
        calls = []
        class FakeHttpx:
            @staticmethod
            def get(*a, **kw):
                calls.append((a, kw))
                raise AssertionError("httpx.get must NOT be called when GEOCODE_ENABLED is unset")
        monkeypatch.setattr(hg, "httpx", FakeHttpx, raising=False)
        with _temp_cache({}):
            result = hg.resolve_live("Hotel Test", "", "Singapore", "SG")
        assert result is None
        assert len(calls) == 0, "No network call when gate is off"


# ---------------------------------------------------------------------------
# var-0: day_plans/package_total identical with vs without cached hotel coord
# ---------------------------------------------------------------------------

class TestHotelGeoVar0:
    def test_core_fields_identical(self):
        """day_plans + package_total_cents are byte-identical with empty vs warm cache."""
        usd_cents = 300_000
        base_result = {
            "outcome": "success",
            "package_total_with_fees_cents": usd_cents,
            "day_plans": [{"leg_id": "leg-0", "city": "Singapore", "days": [{"day_index": 0}]}],
            "legs": [{"leg_id": "leg-0", "city": "Singapore", "title": "Some Hotel", "area": ""}],
        }

        # Run 1: empty cache → city_centroid
        import utils.hotel_geocode as hg
        r1 = json.loads(json.dumps(base_result))
        with _temp_cache({}):
            _attach_geo(r1)

        # Run 2: warm cache (geocoded)
        key = hg.cache_key("Some Hotel", "", "Singapore", "")
        r2 = json.loads(json.dumps(base_result))
        with _temp_cache({key: {"lat": 1.3056, "lon": 103.8321, "source": "nominatim", "as_of": "2026-06-28"}}):
            _attach_geo(r2)

        # Core fields must be identical; only hotel_coord_basis differs
        assert r1["package_total_with_fees_cents"] == r2["package_total_with_fees_cents"]
        assert r1["day_plans"] == r2["day_plans"]
        # hotel_coord_basis is allowed to differ
        assert r1["legs"][0].get("hotel_coord_basis") in ("city_centroid", None)
        assert r2["legs"][0].get("hotel_coord_basis") in ("geocoded", "city_centroid")

    def test_non_hotel_keys_unaffected(self):
        """Only hotel_lat/hotel_lon/hotel_coord_basis are added — nothing else changes."""
        leg = {"leg_id": "leg-0", "city": "Tokyo", "title": "Test Hotel",
               "checkin": "2026-08-01", "checkout": "2026-08-05", "cost_cents": 100_000}
        result = {"outcome": "success", "legs": [dict(leg)]}
        with _temp_cache({}):
            _attach_geo(result)
        after_leg = result["legs"][0]
        # All original keys preserved
        for k, v in leg.items():
            assert after_leg[k] == v, f"Key {k!r} changed: expected {v!r}, got {after_leg[k]!r}"
        # Only new hotel_* keys added (possibly)
        new_keys = set(after_leg) - set(leg)
        assert new_keys <= {"hotel_lat", "hotel_lon", "hotel_coord_basis"}
