"""test_itinerary_narrator_cov3.py — #3 Phase 3: the LLM narrator seam (no network here).

The real DashScope call is verified live (a Cebu payload → grounded narrative, 5/5 items kept). These
unit tests lock the seam's contract: no key → None (gate lives in the server), the prompt forbids
fabrication, and the request disables thinking (latency + the repo-wide invariant)."""

from __future__ import annotations

import inspect
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from utils import itinerary_narrator as nar  # noqa: E402


class TestNarratorSeam(unittest.TestCase):
    def test_no_key_returns_none(self):
        orig = nar.DASHSCOPE_API_KEY
        nar.DASHSCOPE_API_KEY = ""
        try:
            self.assertIsNone(nar.narrate({"legs": []}))  # no key → no call → None
        finally:
            nar.DASHSCOPE_API_KEY = orig

    def test_prompt_forbids_fabrication(self):
        p = nar._NARRATOR_SYSTEM_PROMPT.lower()
        self.assertIn("only", p)
        self.assertTrue("never invent" in p or "must not" in p, "prompt must forbid invention")
        self.assertIn("id", p)  # instructs the model to reference real ids

    def test_request_disables_thinking_and_json(self):
        src = inspect.getsource(nar)
        self.assertIn('"enable_thinking": False', src)            # latency guard + invariant
        self.assertIn('"response_format": {"type": "json_object"}', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
