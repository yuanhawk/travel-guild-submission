"""test_itinerary_narrator.py — Narrator fix (A2): unit tests for the hardened content extraction.

Tests:
1. Good LLM response → narrate() returns a dict with non-empty 'overview'.
2. Empty content (reasoning model silent) → narrate() returns None (no stub).
3. Missing content key → narrate() returns None.
4. No API key → narrate() returns None immediately.
5. Invalid JSON in content → narrate() returns None.
6. Module has a logger.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

import utils.itinerary_narrator as nar


_CANNED_NARRATIVE = {
    "overview": "A warm journey through two vibrant cities.",
    "legs": [
        {
            "leg_id": "leg-1",
            "city": "tokyo",
            "summary": "Neon lights and ancient temples await.",
            "days": [
                {
                    "day_number": 1,
                    "title": "Temple Mornings",
                    "narrative": "Begin with a serene temple visit.",
                    "highlights": [{"id": 1, "name": "Senso-ji", "blurb": "A tranquil start."}],
                    "dining": [],
                }
            ],
        }
    ],
}


def _make_resp(content):
    """Build a mock httpx.Response for the given content string (or None for empty)."""
    resp = MagicMock()
    msg = {"reasoning_content": None}
    if content is None:
        msg["content"] = None
    else:
        msg["content"] = content
    resp.json.return_value = {
        "choices": [{"message": msg, "finish_reason": "stop"}]
    }
    return resp


class TestNarratorFix(unittest.TestCase):
    """A2: the hardened extraction path (no network, no key required)."""

    def setUp(self):
        self._orig_key = nar.DASHSCOPE_API_KEY
        nar.DASHSCOPE_API_KEY = "sk-test-fake-key"

    def tearDown(self):
        nar.DASHSCOPE_API_KEY = self._orig_key

    def test_good_content_returns_dict_with_overview(self):
        content_json = json.dumps(_CANNED_NARRATIVE)
        with patch("utils.itinerary_narrator.httpx.post") as mock_post:
            mock_post.return_value = _make_resp(content_json)
            result = nar.narrate({"legs": [{"days": [{}]}]})
        self.assertIsNotNone(result, "should return a dict on success")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["overview"], "A warm journey through two vibrant cities.")
        self.assertGreater(len(result.get("overview", "")), 10)

    def test_empty_content_returns_none(self):
        """Reasoning model leaves content empty → narrate() must return None, never a stub."""
        resp = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": "", "reasoning_content": "...thinking..."},
                         "finish_reason": "stop"}]
        }
        with patch("utils.itinerary_narrator.httpx.post") as mock_post:
            mock_post.return_value = resp
            result = nar.narrate({"legs": []})
        self.assertIsNone(result, "empty content must produce None, never a stub")

    def test_none_content_returns_none(self):
        with patch("utils.itinerary_narrator.httpx.post") as mock_post:
            mock_post.return_value = _make_resp(None)
            result = nar.narrate({"legs": []})
        self.assertIsNone(result)

    def test_invalid_json_returns_none(self):
        with patch("utils.itinerary_narrator.httpx.post") as mock_post:
            mock_post.return_value = _make_resp("not json at all }")
            result = nar.narrate({"legs": []})
        self.assertIsNone(result)

    def test_no_api_key_returns_none_without_network(self):
        orig = nar.DASHSCOPE_API_KEY
        nar.DASHSCOPE_API_KEY = ""
        try:
            with patch("utils.itinerary_narrator.httpx.post") as mock_post:
                result = nar.narrate({"legs": []})
                mock_post.assert_not_called()  # must not even attempt a call
            self.assertIsNone(result)
        finally:
            nar.DASHSCOPE_API_KEY = orig

    def test_module_has_logger(self):
        """A2: module must have a named logger (for latency attribution)."""
        self.assertIsInstance(nar.logger, logging.Logger)
        self.assertEqual(nar.logger.name, "utils.itinerary_narrator")

    def test_content_with_markdown_fences_strips_ok(self):
        fenced = "```json\n" + json.dumps(_CANNED_NARRATIVE) + "\n```"
        with patch("utils.itinerary_narrator.httpx.post") as mock_post:
            mock_post.return_value = _make_resp(fenced)
            result = nar.narrate({"legs": [{"days": [{}]}]})
        self.assertIsNotNone(result)
        self.assertEqual(result.get("overview"), _CANNED_NARRATIVE["overview"])

    def test_network_error_returns_none(self):
        import httpx as _httpx
        with patch("utils.itinerary_narrator.httpx.post") as mock_post:
            mock_post.side_effect = _httpx.ConnectTimeout("timed out", request=None)
            result = nar.narrate({"legs": []})
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
