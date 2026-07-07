"""test_itinerary_narrator_client.py — CI-safe unit tests for narrate() in itinerary_narrator.py.

Tests fence-stripping and max_tokens injection by monkeypatching httpx.post. No real network
calls; DASHSCOPE_API_KEY is injected via monkeypatch in each test. All tests are pure/deterministic.
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

import utils.itinerary_narrator as nar  # noqa: E402


def _make_resp(content_str: str) -> MagicMock:
    """Build a mock httpx response whose .json() mirrors the DashScope API shape."""
    mock = MagicMock()
    mock.json.return_value = {
        "choices": [{"message": {"content": content_str}}]
    }
    return mock


_SAMPLE_PAYLOAD = {"legs": [{"leg_id": "L0", "city": "Tokyo", "days": [
    {"day_number": 1, "attractions": [{"id": 1, "name": "Senso-ji Temple"}], "meals": []}
]}]}


class TestNarrateJsonFenceStripping(unittest.TestCase):
    """Verify that narrate() correctly parses responses wrapped in markdown code fences."""

    def test_plain_json_parsed(self):
        body = json.dumps({"legs": [{"leg_id": "L0"}]})
        with patch("httpx.post", return_value=_make_resp(body)), \
                patch.object(nar, "DASHSCOPE_API_KEY", "sk-test"):
            result = nar.narrate(_SAMPLE_PAYLOAD)
        self.assertIsInstance(result, dict)
        self.assertIn("legs", result)

    def test_json_fence_stripped(self):
        """```json\\n{...}\\n``` must be parsed correctly after fence removal."""
        body = "```json\n" + json.dumps({"legs": [{"leg_id": "L0"}]}) + "\n```"
        with patch("httpx.post", return_value=_make_resp(body)), \
                patch.object(nar, "DASHSCOPE_API_KEY", "sk-test"):
            result = nar.narrate(_SAMPLE_PAYLOAD)
        self.assertIsInstance(result, dict)
        self.assertIn("legs", result)

    def test_bare_fence_stripped(self):
        """``` (no json lang tag) fences also stripped."""
        body = "```\n" + json.dumps({"legs": []}) + "\n```"
        with patch("httpx.post", return_value=_make_resp(body)), \
                patch.object(nar, "DASHSCOPE_API_KEY", "sk-test"):
            result = nar.narrate(_SAMPLE_PAYLOAD)
        self.assertIsInstance(result, dict)

    def test_non_dict_returns_none(self):
        """A JSON array (not a dict) at top-level returns None."""
        body = json.dumps([1, 2, 3])
        with patch("httpx.post", return_value=_make_resp(body)), \
                patch.object(nar, "DASHSCOPE_API_KEY", "sk-test"):
            result = nar.narrate(_SAMPLE_PAYLOAD)
        self.assertIsNone(result)

    def test_malformed_json_returns_none(self):
        """Malformed JSON inside the fences degrades to None (no exception raised)."""
        body = "```json\n{not valid json\n```"
        with patch("httpx.post", return_value=_make_resp(body)), \
                patch.object(nar, "DASHSCOPE_API_KEY", "sk-test"):
            result = nar.narrate(_SAMPLE_PAYLOAD)
        self.assertIsNone(result)


class TestNarrateMaxTokens(unittest.TestCase):
    """Verify max_tokens is injected into the request body."""

    def test_default_max_tokens_in_body(self):
        """Default max_tokens scales with trip days; the 1-day sample payload -> 2500."""
        captured = {}

        def _capture(url, *, headers, json, timeout):
            captured["body"] = json
            return _make_resp('{"legs": []}')

        env_backup = os.environ.pop("SOCIETY_NARRATOR_MAX_TOKENS", None)
        try:
            with patch("httpx.post", side_effect=_capture), \
                    patch.object(nar, "DASHSCOPE_API_KEY", "sk-test"):
                nar.narrate(_SAMPLE_PAYLOAD)
        finally:
            if env_backup is not None:
                os.environ["SOCIETY_NARRATOR_MAX_TOKENS"] = env_backup

        self.assertEqual(captured["body"]["max_tokens"], 2500)  # 1-leg/1-day sample -> floor 2500

    def test_env_max_tokens_respected(self):
        """SOCIETY_NARRATOR_MAX_TOKENS env var overrides default."""
        captured = {}

        def _capture(url, *, headers, json, timeout):
            captured["body"] = json
            return _make_resp('{"legs": []}')

        with patch("httpx.post", side_effect=_capture), \
                patch.object(nar, "DASHSCOPE_API_KEY", "sk-test"), \
                patch.dict(os.environ, {"SOCIETY_NARRATOR_MAX_TOKENS": "800"}):
            # Re-read env inside narrate() — the env var is read at call time
            result = nar.narrate(_SAMPLE_PAYLOAD)

        # max_tokens comes from os.environ.get at call time in the body dict construction
        self.assertEqual(captured["body"]["max_tokens"], 800)

    def test_thinking_disabled_in_body(self):
        """enable_thinking must be False in every request body (repo-wide invariant)."""
        captured = {}

        def _capture(url, *, headers, json, timeout):
            captured["body"] = json
            return _make_resp('{"legs": []}')

        with patch("httpx.post", side_effect=_capture), \
                patch.object(nar, "DASHSCOPE_API_KEY", "sk-test"):
            nar.narrate(_SAMPLE_PAYLOAD)

        self.assertIs(captured["body"]["enable_thinking"], False)


class TestNarrateNoKey(unittest.TestCase):
    def test_no_key_no_http_call(self):
        """When DASHSCOPE_API_KEY is empty, no HTTP call is made and None is returned."""
        with patch("httpx.post") as mock_post, \
                patch.object(nar, "DASHSCOPE_API_KEY", ""):
            result = nar.narrate(_SAMPLE_PAYLOAD)
        mock_post.assert_not_called()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
