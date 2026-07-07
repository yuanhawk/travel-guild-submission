"""test_estimate_budget_range_no_candidates.py — Coverage for the no-candidates path.

orchestrator.py lines 1034–1037: when _gather_candidates_for_dp returns an empty list
for a leg, estimate_budget_range must return None without raising (not crash, not guess).
"""

from __future__ import annotations

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from orchestration.orchestrator import TravelOrchestrator


@pytest.fixture(scope="module")
def orch():
    return TravelOrchestrator.__new__(TravelOrchestrator)


_MINIMAL_REQUEST = {
    "legs": [
        {
            "leg_id": "L1",
            "city": "hypothetical-empty-city",
            "country": "XX",
            "checkin": "2027-03-01",
            "checkout": "2027-03-05",
            "nights": 4,
        }
    ],
    "nationality": "SG",
    "budget_cents": 0,
    "trip_type": "leisure",
}


def test_estimate_budget_range_returns_none_when_no_candidates(orch):
    """No catalog candidates for a leg → returns None, never raises."""
    with patch.object(orch, "_gather_candidates_for_dp", return_value=[]):
        # Also provide the _gather_reasons dict that estimate_budget_range checks
        orch._gather_reasons = {}  # type: ignore[attr-defined]
        result = orch.estimate_budget_range(_MINIMAL_REQUEST)
    assert result is None


def test_estimate_budget_range_none_does_not_raise(orch):
    """Confirm the no-candidates path is safe to call multiple times (idempotent)."""
    with patch.object(orch, "_gather_candidates_for_dp", return_value=[]):
        orch._gather_reasons = {}  # type: ignore[attr-defined]
        for _ in range(3):
            result = orch.estimate_budget_range(_MINIMAL_REQUEST)
            assert result is None
