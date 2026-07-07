"""test_places_status_live_cov.py — #105: places_status.business_status() live call coverage (53.1% gap).
Covers the live Places API branch with mocked httpx.post."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from utils import places_status  # noqa: E402


class TestBusinessStatusLive(unittest.TestCase):
    def setUp(self):
        # Save original state
        self._orig_enabled = places_status.PLACES_ENABLED
        self._orig_key = places_status.PLACES_KEY
        self._orig_os_enabled = os.environ.get("PLACES_ENABLED")
        self._orig_os_key = os.environ.get("GOOGLE_PLACES_KEY")

    def tearDown(self):
        # Restore original state (module globals were already saved)
        places_status.PLACES_ENABLED = self._orig_enabled
        places_status.PLACES_KEY = self._orig_key
        if self._orig_os_enabled:
            os.environ["PLACES_ENABLED"] = self._orig_os_enabled
        else:
            os.environ.pop("PLACES_ENABLED", None)
        if self._orig_os_key:
            os.environ["GOOGLE_PLACES_KEY"] = self._orig_os_key
        else:
            os.environ.pop("GOOGLE_PLACES_KEY", None)

    def test_business_status_returns_none_when_disabled(self):
        places_status.PLACES_ENABLED = False
        result = places_status.business_status("Park Hotel", "tokyo", "Japan")
        self.assertIsNone(result)

    def test_business_status_returns_none_when_key_missing(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = ""
        result = places_status.business_status("Park Hotel", "tokyo", "Japan")
        self.assertIsNone(result)

    def test_business_status_returns_none_when_no_distinctive_tokens(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = "test-key"
        # A purely generic title with no distinctive tokens
        result = places_status.business_status("The Hotel", "tokyo", "Japan")
        self.assertIsNone(result)

    def test_business_status_returns_none_on_httpx_exception(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = "test-key"
        with mock.patch("httpx.post", side_effect=Exception("network error")):
            result = places_status.business_status("Park Hotel", "tokyo", "Japan")
            self.assertIsNone(result)

    def test_business_status_returns_operational_on_match(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = "test-key"
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "places": [
                {
                    "displayName": {"text": "Park Hyatt Tokyo"},
                    "businessStatus": "OPERATIONAL"
                }
            ]
        }
        with mock.patch("httpx.post", return_value=mock_response):
            result = places_status.business_status("Park Hotel", "tokyo", "Japan")
            self.assertEqual(result, "OPERATIONAL")

    def test_business_status_returns_operational_as_default_when_status_missing(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = "test-key"
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "places": [
                {
                    "displayName": {"text": "Park Hyatt Tokyo"}
                    # No businessStatus key
                }
            ]
        }
        with mock.patch("httpx.post", return_value=mock_response):
            result = places_status.business_status("Park Hotel", "tokyo", "Japan")
            self.assertEqual(result, "OPERATIONAL")

    def test_business_status_returns_closed_permanently_on_match(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = "test-key"
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "places": [
                {
                    "displayName": {"text": "Park Hyatt Tokyo"},
                    "businessStatus": "CLOSED_PERMANENTLY"
                }
            ]
        }
        with mock.patch("httpx.post", return_value=mock_response):
            result = places_status.business_status("Park Hotel", "tokyo", "Japan")
            self.assertEqual(result, "CLOSED_PERMANENTLY")

    def test_business_status_returns_unverified_when_no_match(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = "test-key"
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "places": [
                {
                    "displayName": {"text": "Different Hotel"},
                    "businessStatus": "OPERATIONAL"
                }
            ]
        }
        with mock.patch("httpx.post", return_value=mock_response):
            result = places_status.business_status("Park Hotel", "tokyo", "Japan")
            self.assertEqual(result, "UNVERIFIED")

    def test_business_status_returns_unverified_when_no_places(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = "test-key"
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {"places": []}
        with mock.patch("httpx.post", return_value=mock_response):
            result = places_status.business_status("NonexistentHotel", "fakecity", "FakeCountry")
            self.assertEqual(result, "UNVERIFIED")

    def test_business_status_returns_unverified_when_places_key_missing(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = "test-key"
        mock_response = mock.MagicMock()
        # Missing 'places' key entirely
        mock_response.json.return_value = {}
        with mock.patch("httpx.post", return_value=mock_response):
            result = places_status.business_status("Park Hotel", "tokyo", "Japan")
            self.assertEqual(result, "UNVERIFIED")

    def test_business_status_skips_places_without_displayname(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = "test-key"
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "places": [
                {"businessStatus": "OPERATIONAL"},  # No displayName
                {
                    "displayName": {"text": "Park Hotel Tokyo"},
                    "businessStatus": "OPERATIONAL"
                }
            ]
        }
        with mock.patch("httpx.post", return_value=mock_response):
            result = places_status.business_status("Park Hotel", "tokyo", "Japan")
            self.assertEqual(result, "OPERATIONAL")

    def test_business_status_distinctive_token_overlap_required(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = "test-key"
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "places": [
                {
                    "displayName": {"text": "Tokyo Hostel"},  # Different distinctive tokens
                    "businessStatus": "OPERATIONAL"
                }
            ]
        }
        with mock.patch("httpx.post", return_value=mock_response):
            # "Park Hotel" vs "Tokyo Hostel" — no distinctive overlap
            result = places_status.business_status("Park Hotel", "tokyo", "Japan")
            self.assertEqual(result, "UNVERIFIED")

    def test_business_status_sends_correct_headers(self):
        places_status.PLACES_ENABLED = True
        places_status.PLACES_KEY = "test-key"
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {"places": []}
        with mock.patch("utils.places_status.httpx.post", return_value=mock_response) as mock_post:
            # Use a name with distinctive tokens that survive generic filtering
            places_status.business_status("Park Plaza", "Tokyo", "Japan")
            # Verify the call
            self.assertTrue(mock_post.called)
            call_args = mock_post.call_args
            headers = call_args.kwargs["headers"]
            self.assertEqual(headers["X-Goog-Api-Key"], "test-key")
            self.assertIn("X-Goog-FieldMask", headers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
