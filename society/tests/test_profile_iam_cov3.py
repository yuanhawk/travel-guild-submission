"""test_profile_iam_cov3.py — #40 IAM: anonymous-first profile hydration + the profile store.

Covers the pure hydration helper (anonymous = unchanged, fills only blanks, never overrides
explicit values, var-0) and the travel_memory profile store round-trip (save/load).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from utils.profile_hydrate import hydrate_trip_from_profile  # noqa: E402


class TestHydration(unittest.TestCase):
    PROFILE = {
        "nationality": "SG",
        "passports": ["SG", "GB"],
        "home_currency": "SGD",
        "dietary": "halal",
        "vibe": "culture",
        "held_vaccine_certs": ["yellow_fever"],
    }

    def test_anonymous_unchanged(self):
        req = {"city": "bali", "budget_cents": 300000}
        out = hydrate_trip_from_profile(req, None)
        self.assertEqual(out, req)
        out2 = hydrate_trip_from_profile(req, {})
        self.assertEqual(out2, req)

    def test_fills_only_blanks(self):
        req = {"city": "bali"}
        out = hydrate_trip_from_profile(req, self.PROFILE)
        self.assertEqual(out["passports"], ["SG", "GB"])
        self.assertEqual(out["home_currency"], "SGD")
        self.assertEqual(out["dietary"], "halal")
        self.assertEqual(out["city"], "bali")  # untouched

    def test_explicit_never_overridden(self):
        # The current request says JP nationality + USD — the profile must NOT clobber it.
        req = {"city": "tokyo", "nationality": "JP", "home_currency": "USD", "passports": ["JP"]}
        out = hydrate_trip_from_profile(req, self.PROFILE)
        self.assertEqual(out["nationality"], "JP")
        self.assertEqual(out["home_currency"], "USD")
        self.assertEqual(out["passports"], ["JP"])

    def test_does_not_mutate_input(self):
        req = {"city": "bali"}
        hydrate_trip_from_profile(req, self.PROFILE)
        self.assertNotIn("passports", req)  # original untouched

    def test_var0_byte_identical(self):
        import json
        req = {"city": "bali"}
        outs = {json.dumps(hydrate_trip_from_profile(req, self.PROFILE), sort_keys=True) for _ in range(5)}
        self.assertEqual(len(outs), 1)


class TestProfileStore(unittest.TestCase):
    def test_store_round_trip_and_anonymous_default(self):
        _MCP = os.path.join(os.path.dirname(_SOC), "mcp", "travel_memory")
        if _MCP not in sys.path:
            sys.path.insert(0, _MCP)
        try:
            from store import SqliteStore  # type: ignore
        except Exception as e:  # pragma: no cover - store import is env-dependent
            self.skipTest(f"travel_memory store unavailable: {e}")
        with tempfile.TemporaryDirectory() as d:
            st = SqliteStore(os.path.join(d, "mem.db"))
            # anonymous user -> no profile
            self.assertIsNone(st.get_profile("nobody"))
            prof = {"passports": ["SG", "GB"], "home_currency": "SGD"}
            st.upsert_profile("u1", prof)
            self.assertEqual(st.get_profile("u1"), prof)
            # upsert overwrites
            st.upsert_profile("u1", {"home_currency": "GBP"})
            self.assertEqual(st.get_profile("u1"), {"home_currency": "GBP"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
