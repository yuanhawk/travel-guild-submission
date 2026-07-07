"""
test_planner_agent.py — Planner A2A agent hardening tests.

Covers the D2/#31/#46/#47 hardening contracts:

  D2  : Risk hazard directives injected by the orchestrator are EXTRACTED and
        PROPAGATED into the itinerary skeleton (per-leg risk_flags / avoid_window /
        buffer_connection_min + top-level risk_flagged / risk_advisories), never
        silently dropped.
  #31 : an UNEXPECTED DP allocator failure stamps dp_error=True (visible degraded
        fallback), never a silent proportional split.
  #46 : a per_leg_candidates set whose leg_ids don't match the planner's leg-{i}
        convention does NOT yield dp_allocation=True with silently-proportional
        legs — it drops to dp_allocation=False.
  #47 : proportional split uses pure INTEGER money math (no float on the money
        path) and the per-leg sum is exactly the budget.
  nights: _nights_between must FLAG (raise ValueError) for malformed dates,
        same-day checkout, and reversed dates — never silently fabricate 1 night.

All assertions are var-0: byte-identical reruns, integer money, sorted output.
"""

from __future__ import annotations

import asyncio

import pytest

from agents import planner_agent
from agents.planner_agent import PlannerAgent, _proportional_split, _nights_between


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------

def _make_agent() -> PlannerAgent:
    # Construct without binding the ASGI server (handler is exercised directly).
    return PlannerAgent(host="127.0.0.1", port=9102)


def _msg(payload: dict) -> dict:
    return {"parts": [{"kind": "data", "data": payload}]}


def _run_decompose(payload: dict) -> dict:
    """Invoke the plan.decompose handler and return the result skeleton dict."""
    agent = _make_agent()
    artifact = asyncio.run(
        agent._decompose_handler(_msg(payload), task={})
    )
    # artifact -> parts -> first data part -> data
    return artifact["parts"][0]["data"]


def _two_leg_payload(**extra) -> dict:
    base = {
        "user_id": "u1",
        "total_budget_cents": 80000,
        "legs": [
            {"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04",
             "adults": 1, "vibe": "culture"},
            {"city": "bali", "checkin": "2026-10-04", "checkout": "2026-10-08",
             "adults": 1, "vibe": "beach"},
        ],
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# #47 — integer money math
# ---------------------------------------------------------------------------

def test_proportional_split_is_integer_and_exact():
    # 3 nights + 4 nights = 7; budget 80000.
    alloc = _proportional_split([3, 4], 80000)
    assert all(isinstance(a, int) for a in alloc)
    assert sum(alloc) == 80000
    # leg0 = 3*80000//7 = 34285 ; remainder folded into last leg.
    assert alloc[0] == (3 * 80000) // 7
    assert alloc[1] == 80000 - alloc[0]


def test_proportional_split_no_float_drift_large_budget():
    # A budget that would expose float rounding if division were float-based.
    alloc = _proportional_split([1, 1, 1], 10_000_000_001)
    assert sum(alloc) == 10_000_000_001
    assert all(isinstance(a, int) for a in alloc)


def test_proportional_split_zero_nights_equal_split():
    alloc = _proportional_split([0, 0], 10000)
    assert alloc == [5000, 5000]


def test_handler_proportional_path_sum_exact():
    res = _run_decompose(_two_leg_payload())
    assert res["dp_allocation"] is False
    assert res["dp_error"] is False
    total = sum(leg["per_leg_budget_cents"] for leg in res["legs"])
    assert total == 80000


# ---------------------------------------------------------------------------
# D2 — Risk directives propagation
# ---------------------------------------------------------------------------

def test_risk_directives_propagated_per_leg_and_top_level():
    res = _run_decompose(_two_leg_payload(risk_directives={
        "avoid_window": True,
        "buffer_connection_min": 45,
        "flagged_legs": ["leg-1"],
        "reason_codes": ["ARMED_CONFLICT", "TRANSPORT_DISRUPTION"],
        "prefer_flexible_cancellation": True,
    }))
    # Top-level signal reaches the itinerary.
    assert res["risk_flagged"] is True
    assert res["risk_reason_codes"] == ["ARMED_CONFLICT", "TRANSPORT_DISRUPTION"]
    assert res["risk_advisories"]  # non-empty advisory texts
    # Advisories are sorted/deduped (var-0).
    assert res["risk_advisories"] == sorted(res["risk_advisories"])

    leg0, leg1 = res["legs"]
    # leg-0 is NOT named in flagged_legs → not flagged.
    assert leg0["risk_flags"] == []
    assert leg0["avoid_window"] is False
    assert leg0["buffer_connection_min"] == 0
    # leg-1 IS flagged → hazard reaches this leg.
    assert leg1["risk_flags"] == ["ARMED_CONFLICT", "TRANSPORT_DISRUPTION"]
    assert leg1["avoid_window"] is True
    assert leg1["buffer_connection_min"] == 45


def test_risk_trip_level_without_named_legs_applies_to_all():
    # Directive present but no flagged_legs named → conservative: apply to all legs.
    res = _run_decompose(_two_leg_payload(risk_directives={
        "avoid_window": True,
        "buffer_connection_min": 30,
        "flagged_legs": [],
        "reason_codes": ["CYCLONE"],
        "prefer_flexible_cancellation": False,
    }))
    assert res["risk_flagged"] is True
    for leg in res["legs"]:
        assert leg["risk_flags"] == ["CYCLONE"]
        assert leg["avoid_window"] is True
        assert leg["buffer_connection_min"] == 30


def test_no_risk_directives_means_clean_skeleton():
    res = _run_decompose(_two_leg_payload())
    assert res["risk_flagged"] is False
    assert res["risk_advisories"] == []
    assert res["risk_reason_codes"] == []
    for leg in res["legs"]:
        assert leg["risk_flags"] == []
        assert leg["avoid_window"] is False
        assert leg["buffer_connection_min"] == 0


def test_risk_propagation_is_var0_byte_identical():
    payload = _two_leg_payload(risk_directives={
        "avoid_window": True,
        "buffer_connection_min": 45,
        "flagged_legs": ["leg-0", "leg-1"],
        "reason_codes": ["B_CODE", "A_CODE"],
        "prefer_flexible_cancellation": True,
    })
    r1 = _run_decompose(payload)
    r2 = _run_decompose(payload)
    assert r1 == r2


# ---------------------------------------------------------------------------
# #31 — DP allocator unexpected error -> dp_error flag (not silent)
# ---------------------------------------------------------------------------

def test_dp_allocator_unexpected_error_flags_dp_error(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic allocator malfunction")

    from utils import allocator as allocator_mod
    monkeypatch.setattr(allocator_mod, "allocate", _boom)

    payload = _two_leg_payload(per_leg_candidates=[
        {"leg_id": "leg-0", "candidates": [
            {"hotel_id": "h0", "total_cents": 30000, "quality": 0.8, "review_score": 8.0}]},
        {"leg_id": "leg-1", "candidates": [
            {"hotel_id": "h1", "total_cents": 40000, "quality": 0.9, "review_score": 9.0}]},
    ])
    res = _run_decompose(payload)
    # Degraded, but NOT silent: dp_error stamped, proportional split used.
    assert res["dp_error"] is True
    assert res["dp_allocation"] is False
    assert sum(leg["per_leg_budget_cents"] for leg in res["legs"]) == 80000


def test_dp_value_error_still_raises_cannot_satisfy():
    # ValueError (infeasibility / cannot_satisfy) must still propagate, not flag.
    payload = _two_leg_payload(
        total_budget_cents=1000,  # far below any candidate cost
        per_leg_candidates=[
            {"leg_id": "leg-0", "candidates": [
                {"hotel_id": "h0", "total_cents": 30000, "quality": 0.8, "review_score": 8.0}]},
            {"leg_id": "leg-1", "candidates": [
                {"hotel_id": "h1", "total_cents": 40000, "quality": 0.9, "review_score": 9.0}]},
        ],
    )
    with pytest.raises(ValueError):
        _run_decompose(payload)


# ---------------------------------------------------------------------------
# #46 — leg_id mismatch validation
# ---------------------------------------------------------------------------

def test_leg_id_mismatch_drops_to_proportional():
    # Candidate leg_ids do NOT match the planner's leg-{i} convention (count equal).
    payload = _two_leg_payload(per_leg_candidates=[
        {"leg_id": "X-0", "candidates": [
            {"hotel_id": "h0", "total_cents": 30000, "quality": 0.8, "review_score": 8.0}]},
        {"leg_id": "X-1", "candidates": [
            {"hotel_id": "h1", "total_cents": 40000, "quality": 0.9, "review_score": 9.0}]},
    ])
    res = _run_decompose(payload)
    # Must NOT claim dp_allocation while legs silently get proportional budgets.
    assert res["dp_allocation"] is False
    for leg in res["legs"]:
        assert leg["dp_selected_hotel_id"] is None
        assert leg["dp_selected_total_cents"] is None
    assert sum(leg["per_leg_budget_cents"] for leg in res["legs"]) == 80000


def test_matching_leg_ids_use_dp():
    payload = _two_leg_payload(per_leg_candidates=[
        {"leg_id": "leg-0", "candidates": [
            {"hotel_id": "h0", "total_cents": 30000, "quality": 0.8, "review_score": 8.0}]},
        {"leg_id": "leg-1", "candidates": [
            {"hotel_id": "h1", "total_cents": 40000, "quality": 0.9, "review_score": 9.0}]},
    ])
    res = _run_decompose(payload)
    assert res["dp_allocation"] is True
    assert res["dp_error"] is False
    legs = {leg["leg_id"]: leg for leg in res["legs"]}
    assert legs["leg-0"]["dp_selected_hotel_id"] == "h0"
    assert legs["leg-0"]["per_leg_budget_cents"] == 30000
    assert legs["leg-1"]["dp_selected_hotel_id"] == "h1"
    assert legs["leg-1"]["per_leg_budget_cents"] == 40000


# ---------------------------------------------------------------------------
# _nights_between — fail-conservative: raise on invalid date ranges
# ---------------------------------------------------------------------------

def test_nights_between_valid_returns_correct_count():
    """Normal case: valid forward-facing dates return the exact night count."""
    assert _nights_between("2026-10-01", "2026-10-04") == 3
    assert _nights_between("2026-10-04", "2026-10-08") == 4
    assert _nights_between("2026-01-01", "2026-01-02") == 1


def test_nights_between_same_day_raises():
    """Same-day checkout (0 nights) must raise — never coerce to 1."""
    with pytest.raises(ValueError, match="invalid date range"):
        _nights_between("2026-10-01", "2026-10-01")


def test_nights_between_reversed_raises():
    """Reversed dates (checkout before checkin) must raise — never coerce to 1."""
    with pytest.raises(ValueError, match="invalid date range"):
        _nights_between("2026-10-04", "2026-10-01")


def test_nights_between_malformed_checkin_raises():
    """Malformed checkin string must raise — never coerce to 1."""
    with pytest.raises(ValueError, match="malformed date"):
        _nights_between("not-a-date", "2026-10-04")


def test_nights_between_malformed_checkout_raises():
    """Malformed checkout string must raise — never coerce to 1."""
    with pytest.raises(ValueError, match="malformed date"):
        _nights_between("2026-10-01", "bad")


def test_nights_between_empty_strings_raise():
    """Empty strings are malformed — must raise, never coerce to 1."""
    with pytest.raises(ValueError, match="malformed date"):
        _nights_between("", "")


def test_nights_between_none_like_raises():
    """None-like (empty string) date strings must raise — never coerce to 1."""
    with pytest.raises(ValueError):
        _nights_between("", "2026-10-04")


def test_handler_rejects_reversed_dates_in_leg():
    """
    If a leg carries reversed checkin/checkout dates, the handler must fail
    (raise ValueError → task fails), not silently produce a 1-night skeleton.
    """
    payload = {
        "user_id": "u1",
        "total_budget_cents": 80000,
        "legs": [
            # Valid leg
            {"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04",
             "adults": 1},
            # Reversed dates: checkout BEFORE checkin
            {"city": "bali", "checkin": "2026-10-08", "checkout": "2026-10-04",
             "adults": 1},
        ],
    }
    with pytest.raises(ValueError, match="invalid date range"):
        _run_decompose(payload)


def test_handler_rejects_same_day_checkout():
    """
    A leg with same-day checkin == checkout must cause the handler to raise,
    not produce a fabricated 1-night stay.
    """
    payload = {
        "user_id": "u1",
        "total_budget_cents": 80000,
        "legs": [
            {"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-01",
             "adults": 1},
        ],
    }
    with pytest.raises(ValueError, match="invalid date range"):
        _run_decompose(payload)


def test_handler_rejects_malformed_dates_in_leg():
    """
    Malformed date string in a leg must cause the handler to raise, not
    silently produce 1-night stays.
    """
    payload = {
        "user_id": "u1",
        "total_budget_cents": 80000,
        "legs": [
            {"city": "bali", "checkin": "not-a-date", "checkout": "2026-10-04",
             "adults": 1},
        ],
    }
    with pytest.raises(ValueError, match="malformed date"):
        _run_decompose(payload)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
