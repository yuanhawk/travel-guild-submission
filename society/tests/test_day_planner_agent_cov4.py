"""test_day_planner_agent_cov4.py — #105: day_planner_agent minor gap (70.7%).
Focuses on _matches_cuisine, derive_bad_weather_days error paths."""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from agents import day_planner_agent  # noqa: E402


class TestMatchesCuisine(unittest.TestCase):
    """Test _matches_cuisine: checks if restaurant matches a cuisine string."""

    def test_matches_cuisine_hit_on_cuisine_field(self):
        """Match on 'cuisine' field (lowercased)."""
        result = day_planner_agent._matches_cuisine(
            {"cuisine": "italian"}, "italian"
        )
        self.assertTrue(result)

    def test_matches_cuisine_hit_case_insensitive(self):
        """Matching is case-insensitive."""
        result = day_planner_agent._matches_cuisine(
            {"cuisine": "Italian"}, "italian"
        )
        self.assertTrue(result)

    def test_matches_cuisine_no_hit(self):
        """No match on different cuisine."""
        result = day_planner_agent._matches_cuisine(
            {"cuisine": "italian"}, "japanese"
        )
        self.assertFalse(result)

    def test_matches_cuisine_missing_cuisine_field(self):
        """Missing cuisine field → False."""
        result = day_planner_agent._matches_cuisine(
            {}, "italian"
        )
        self.assertFalse(result)


class TestDeriveBadWeatherDays(unittest.TestCase):
    """Test derive_bad_weather_days: returns high-risk day indices from season tables."""

    def test_derive_bad_weather_returns_list_of_indices(self):
        """Returns a list (even if empty)."""
        result = day_planner_agent.derive_bad_weather_days(
            None, "2026-07-01", 5, threshold_bp=3000
        )
        self.assertIsInstance(result, list)

    def test_derive_bad_weather_empty_region(self):
        """None/unknown region → empty list (no hazards to report)."""
        result = day_planner_agent.derive_bad_weather_days(
            None, "2026-07-01", 3, threshold_bp=3000
        )
        self.assertEqual(result, [])

    def test_derive_bad_weather_deterministic(self):
        """Same inputs → same output (no randomness)."""
        result1 = day_planner_agent.derive_bad_weather_days(
            "JP", "2026-07-01", 5, threshold_bp=3000
        )
        result2 = day_planner_agent.derive_bad_weather_days(
            "JP", "2026-07-01", 5, threshold_bp=3000
        )
        self.assertEqual(result1, result2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
