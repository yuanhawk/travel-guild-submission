"""test_budget_estimate_cov3.py — unit tests for the deterministic budget-guidance core.

Pure-arithmetic helpers (no merchant/LLM): estimate_range, round_band, shortfall. Includes a
var-0 loop (byte-identical across repeated calls).
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from utils import budget_estimate as be  # noqa: E402


class TestEstimateRange(unittest.TestCase):
    def test_floor_mid_plus_fees(self):
        # 2 legs: floors 1000.00 + 800.00, mids 1500.00 + 1200.00, fees 300.00
        low, high = be.estimate_range([100000, 80000], [150000, 120000], 30000)
        self.assertEqual(low, 100000 + 80000 + 30000)   # 210000
        self.assertEqual(high, 150000 + 120000 + 30000)  # 300000

    def test_zero_fees(self):
        low, high = be.estimate_range([50000], [70000], 0)
        self.assertEqual((low, high), (50000, 70000))

    def test_none_fees_treated_as_zero(self):
        low, high = be.estimate_range([50000], [70000], None)
        self.assertEqual((low, high), (50000, 70000))

    def test_high_never_below_low_guard(self):
        # Pathological: mid < floor for a leg -> high clamped up to low.
        low, high = be.estimate_range([100000], [90000], 0)
        self.assertEqual(low, 100000)
        self.assertGreaterEqual(high, low)

    def test_mismatched_legs_raises(self):
        with self.assertRaises(ValueError):
            be.estimate_range([100000, 80000], [150000], 0)

    def test_empty_legs_just_fees(self):
        self.assertEqual(be.estimate_range([], [], 25000), (25000, 25000))


class TestRoundBand(unittest.TestCase):
    def test_low_floors_high_ceils_to_step(self):
        # 1804.xx -> 1800 ; 3201 -> 3300 at $100 step
        low, high = be.round_band(180412, 320001)
        self.assertEqual(low, 180000)
        self.assertEqual(high, 330000)

    def test_already_on_step_unchanged(self):
        self.assertEqual(be.round_band(200000, 300000), (200000, 300000))

    def test_non_degenerate_band(self):
        # low and high collapse to the same step -> high pushed one step up.
        low, high = be.round_band(200000, 200001)
        self.assertEqual(low, 200000)
        self.assertGreaterEqual(high, low + 10000)

    def test_custom_step(self):
        self.assertEqual(be.round_band(1234, 5678, step_cents=1000), (1000, 6000))

    def test_positive_low_below_step_never_zeros(self):
        # Regression guard for the $0-floor bug: an $84 floor (8400¢) with the default
        # $100 step must NOT round down to 0 — it falls back to the $10 grid → $80.
        low, high = be.round_band(8400, 49500)
        self.assertEqual(low, 8000, "positive sub-step low must not floor to 0")
        self.assertGreater(low, 0)
        self.assertLessEqual(low, 8400, "rounded low must not overstate the real floor")
        self.assertEqual(high, 50000)

    def test_tiny_positive_low_falls_to_dollar_grid(self):
        # A $0.50 floor (50¢): $100 and $10 grids both zero it → fall to the $1 grid.
        low, _ = be.round_band(50, 200000)
        self.assertGreater(low, 0)
        self.assertLessEqual(low, 50)

    def test_zero_low_stays_zero(self):
        # A genuinely-zero raw low (no lodging cost at all) stays 0 — the guard only
        # protects POSITIVE lows.
        low, _ = be.round_band(0, 50000)
        self.assertEqual(low, 0)


class TestShortfall(unittest.TestCase):
    def test_positive_gap(self):
        self.assertEqual(be.shortfall(212000, 180000), 32000)

    def test_budget_covers_plan_zero(self):
        self.assertEqual(be.shortfall(180000, 200000), 0)

    def test_exact_match_zero(self):
        self.assertEqual(be.shortfall(200000, 200000), 0)


class TestVar0(unittest.TestCase):
    def test_byte_identical_across_runs(self):
        outs = {
            (
                be.estimate_range([100000, 80000], [150000, 120000], 30000),
                be.round_band(180412, 320001),
                be.shortfall(212000, 180000),
            )
            for _ in range(5)
        }
        self.assertEqual(len(outs), 1, "helpers must be byte-identical across runs (var-0)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
