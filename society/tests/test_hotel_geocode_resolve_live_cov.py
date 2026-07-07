"""test_hotel_geocode_resolve_live_cov.py — #105: hotel_geocode.resolve_live() coverage.
Covers the live Nominatim geocoder branch (45.9% gap). Uses tempfile cache + mocked httpx/time."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from utils import hotel_geocode  # noqa: E402


class TestResolveLive(unittest.TestCase):
    def setUp(self):
        # Save original state
        self._orig_cache_path = hotel_geocode._CACHE_PATH
        self._orig_cache = hotel_geocode._cache
        self._orig_last_call = hotel_geocode._last_nominatim_call
        self._orig_enabled = os.environ.get("GEOCODE_ENABLED")

        # Set up temp cache file
        self._temp_cache_dir = tempfile.TemporaryDirectory()
        self._temp_cache_file = os.path.join(self._temp_cache_dir.name, "hotel_geo_cache.json")
        hotel_geocode._CACHE_PATH = self._temp_cache_file
        hotel_geocode._cache = None  # Force reload from temp file
        hotel_geocode._last_nominatim_call = 0.0

    def tearDown(self):
        # Restore original state
        hotel_geocode._CACHE_PATH = self._orig_cache_path
        hotel_geocode._cache = self._orig_cache
        hotel_geocode._last_nominatim_call = self._orig_last_call
        if self._orig_enabled is not None:
            os.environ["GEOCODE_ENABLED"] = self._orig_enabled
        else:
            os.environ.pop("GEOCODE_ENABLED", None)
        self._temp_cache_dir.cleanup()

    def test_resolve_live_returns_none_when_not_enabled(self):
        os.environ.pop("GEOCODE_ENABLED", None)
        result = hotel_geocode.resolve_live("Park Hotel", "", "tokyo", "Japan")
        self.assertIsNone(result)

    def test_resolve_live_accepts_any_nonempty_value_as_enabled(self):
        # The code checks: if not os.environ.get("GEOCODE_ENABLED")
        # So any non-empty value (including "0") is treated as enabled
        os.environ["GEOCODE_ENABLED"] = "0"  # Non-empty = enabled for this gate
        hotel_geocode._cache = {}
        mock_response = mock.MagicMock()
        mock_response.json.return_value = [{"lat": "35.6762", "lon": "139.7674"}]
        with mock.patch("httpx.get", return_value=mock_response), \
             mock.patch("utils.hotel_geocode.time.monotonic", return_value=1.0), \
             mock.patch("utils.hotel_geocode.time.sleep"):
            result = hotel_geocode.resolve_live("Park Hotel", "", "tokyo", "Japan")
            # Should attempt the call because GEOCODE_ENABLED is set (non-empty)
            self.assertEqual(result, (35.6762, 139.7674))

    def test_resolve_live_hits_positive_cache(self):
        os.environ["GEOCODE_ENABLED"] = "1"
        # Pre-populate cache with a positive entry
        key = hotel_geocode.cache_key("Park Hotel", "", "tokyo", "Japan")
        entry = {"lat": 35.6762, "lon": 139.7674, "source": "nominatim", "as_of": "2026-06-30"}
        hotel_geocode._cache = {key: entry}
        # Should return cached value without any network call
        result = hotel_geocode.resolve_live("Park Hotel", "", "tokyo", "Japan")
        self.assertEqual(result, (35.6762, 139.7674))

    def test_resolve_live_hits_negative_cache(self):
        os.environ["GEOCODE_ENABLED"] = "1"
        # Pre-populate cache with a negative (null) entry
        key = hotel_geocode.cache_key("FakeHotel", "", "fakecity", "FakeCountry")
        entry = {"lat": None, "lon": None, "source": "negative", "as_of": "2026-06-30"}
        hotel_geocode._cache = {key: entry}
        # Should return None for negative cache hit
        result = hotel_geocode.resolve_live("FakeHotel", "", "fakecity", "FakeCountry")
        self.assertIsNone(result)

    def test_resolve_live_success_nominatim_call(self):
        os.environ["GEOCODE_ENABLED"] = "1"
        hotel_geocode._cache = {}  # Empty cache
        mock_response = mock.MagicMock()
        mock_response.json.return_value = [
            {"lat": "35.6762", "lon": "139.7674", "name": "Park Hotel Tokyo"}
        ]
        with mock.patch("httpx.get", return_value=mock_response), \
             mock.patch("utils.hotel_geocode.time.monotonic", return_value=1.0), \
             mock.patch("utils.hotel_geocode.time.sleep"):
            result = hotel_geocode.resolve_live("Park Hotel", "", "tokyo", "Japan")
            self.assertEqual(result, (35.6762, 139.7674))
            # Verify cache was written
            key = hotel_geocode.cache_key("Park Hotel", "", "tokyo", "Japan")
            self.assertIn(key, hotel_geocode._cache)

    def test_resolve_live_nominatim_error_caches_negative(self):
        os.environ["GEOCODE_ENABLED"] = "1"
        hotel_geocode._cache = {}
        with mock.patch("httpx.get",
                        side_effect=Exception("connection error")), \
             mock.patch("utils.hotel_geocode.time.monotonic", return_value=1.0), \
             mock.patch("utils.hotel_geocode.time.sleep"):
            result = hotel_geocode.resolve_live("Park Hotel", "", "tokyo", "Japan")
            self.assertIsNone(result)
            # Verify negative cache entry was created
            key = hotel_geocode.cache_key("Park Hotel", "", "tokyo", "Japan")
            self.assertIn(key, hotel_geocode._cache)
            entry = hotel_geocode._cache[key]
            self.assertIsNone(entry.get("lat"))
            self.assertIsNone(entry.get("lon"))

    def test_resolve_live_empty_results_caches_negative(self):
        os.environ["GEOCODE_ENABLED"] = "1"
        hotel_geocode._cache = {}
        mock_response = mock.MagicMock()
        mock_response.json.return_value = []  # No results
        with mock.patch("httpx.get", return_value=mock_response), \
             mock.patch("utils.hotel_geocode.time.monotonic", return_value=1.0), \
             mock.patch("utils.hotel_geocode.time.sleep"):
            result = hotel_geocode.resolve_live("Nonexistent", "", "fakecity", "FakeCountry")
            self.assertIsNone(result)
            # Verify negative cache was written
            key = hotel_geocode.cache_key("Nonexistent", "", "fakecity", "FakeCountry")
            entry = hotel_geocode._cache[key]
            self.assertIsNone(entry.get("lat"))

    def test_resolve_live_parse_error_caches_negative(self):
        os.environ["GEOCODE_ENABLED"] = "1"
        hotel_geocode._cache = {}
        mock_response = mock.MagicMock()
        # Missing 'lat' key
        mock_response.json.return_value = [{"lon": "139.7674"}]
        with mock.patch("httpx.get", return_value=mock_response), \
             mock.patch("utils.hotel_geocode.time.monotonic", return_value=1.0), \
             mock.patch("utils.hotel_geocode.time.sleep"):
            result = hotel_geocode.resolve_live("Bad", "", "tokyo", "Japan")
            self.assertIsNone(result)
            # Verify negative cache was created
            key = hotel_geocode.cache_key("Bad", "", "tokyo", "Japan")
            entry = hotel_geocode._cache[key]
            self.assertIsNone(entry.get("lat"))

    def test_resolve_live_out_of_range_coordinates_not_cached(self):
        os.environ["GEOCODE_ENABLED"] = "1"
        hotel_geocode._cache = {}
        mock_response = mock.MagicMock()
        # Out of range latitude
        mock_response.json.return_value = [{"lat": "91.0", "lon": "139.7674"}]
        with mock.patch("httpx.get", return_value=mock_response), \
             mock.patch("utils.hotel_geocode.time.monotonic", return_value=1.0), \
             mock.patch("utils.hotel_geocode.time.sleep"):
            result = hotel_geocode.resolve_live("Bad", "", "tokyo", "Japan")
            self.assertIsNone(result)
            # Verify NO cache entry was created (out-of-range coords not cached)
            key = hotel_geocode.cache_key("Bad", "", "tokyo", "Japan")
            self.assertNotIn(key, hotel_geocode._cache)

    def test_resolve_live_rate_limiting_respected(self):
        os.environ["GEOCODE_ENABLED"] = "1"
        hotel_geocode._cache = {}
        mock_response = mock.MagicMock()
        mock_response.json.return_value = [{"lat": "35.6762", "lon": "139.7674"}]
        sleep_called = False
        def mock_sleep(duration):
            nonlocal sleep_called
            if duration > 0:
                sleep_called = True
        with mock.patch("httpx.get", return_value=mock_response), \
             mock.patch("utils.hotel_geocode.time.monotonic", side_effect=[0.0, 0.5]), \
             mock.patch("utils.hotel_geocode.time.sleep", side_effect=mock_sleep):
            result = hotel_geocode.resolve_live("Park Hotel", "", "tokyo", "Japan")
            self.assertEqual(result, (35.6762, 139.7674))
            # Should have slept because elapsed < 1.0
            self.assertTrue(sleep_called)

    def test_resolve_live_with_area_parameter(self):
        os.environ["GEOCODE_ENABLED"] = "1"
        hotel_geocode._cache = {}
        mock_response = mock.MagicMock()
        mock_response.json.return_value = [{"lat": "35.6762", "lon": "139.7674"}]
        with mock.patch("httpx.get", return_value=mock_response) as mock_get, \
             mock.patch("utils.hotel_geocode.time.monotonic", return_value=1.0), \
             mock.patch("utils.hotel_geocode.time.sleep"):
            result = hotel_geocode.resolve_live("Park Hotel", "Shibuya", "tokyo", "Japan")
            self.assertEqual(result, (35.6762, 139.7674))
            # Verify the query includes the area
            call_args = mock_get.call_args
            query = call_args[1]["params"]["q"]
            self.assertIn("Shibuya", query)


if __name__ == "__main__":
    unittest.main(verbosity=2)
