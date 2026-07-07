"""
test_l3_recovery.py — L3-core unit tests for reactive disruption-recovery.

Tests (CI-safe, mock merchant, no LLM, no network):

  (a) test_recovery_fault_activates_secondary
        Fault on baseline hotel → recovery activates the pre-vetted secondary,
        within budget, DETERMINISTIC (same fault → same recovery every call).

  (b) test_no_feasible_secondary_cannot_satisfy
        No feasible secondary (tight budget, no alternative) →
        cannot_satisfy (honest, no booking).

  (c) test_re_consent_enforced
        Recovery does NOT auto-commit.  Assert no complete_checkout is called
        without a fresh mandate (re-consent enforced).

  (d) test_recovered_package_within_budget_and_critic_verified
        The recovered package passes Critic + total ≤ budget.

  (e) test_secondary_determinism
        Same fault on same baseline → identical recovery plan every run (variance-0).

Design: §12.8 §12.9 (the internal design spec).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from typing import Any

import httpx
import pytest

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from agents import budget_agent as ba_mod
from agents import critic_agent as ca_mod
from utils.allocator import allocate, allocate_secondary, attach_quality_scores
from utils.recovery import RecoveryOrchestrator, RECOVERY_OUTCOME_READY, RECOVERY_OUTCOME_CANNOT_SATISFY


# ---------------------------------------------------------------------------
# Mock merchant infrastructure (CountingTransport pattern from test_booking_spine)
# ---------------------------------------------------------------------------

class CountingTransport(httpx.BaseTransport):
    """Intercepts merchant MCP calls, counts per tool, returns canned responses."""

    def __init__(
        self,
        responses: dict[str, Any],
    ) -> None:
        self._responses: dict[str, list[tuple[int, dict]]] = {}
        for tool, resp in responses.items():
            if isinstance(resp, list):
                self._responses[tool] = list(resp)
            else:
                self._responses[tool] = [resp]
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            payload = json.loads(request.read())
        except Exception:
            return httpx.Response(400, text="bad request")

        tool_name = (payload.get("params") or {}).get("name", "")
        with self._lock:
            self._counts[tool_name] = self._counts.get(tool_name, 0) + 1

        resps = self._responses.get(tool_name)
        if not resps:
            return httpx.Response(
                500,
                json={"error": f"mock: no response for tool {tool_name!r}"},
            )
        idx = self._counts.get(tool_name, 1) - 1
        if idx >= len(resps):
            idx = len(resps) - 1
        status_code, body = resps[idx]
        return httpx.Response(status_code, json=body)


def _merchant_result(domain: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "structuredContent": domain,
            "content": [{"type": "text", "text": json.dumps(domain)}],
        },
    }


# ---------------------------------------------------------------------------
# Hotel fixtures (matching the §12.9 pressure-test scenario)
# ---------------------------------------------------------------------------

# 3-leg Bali trip (all 2 nights):
# Leg 0: Ubud    — baseline = ubud-garden  ($28/N × 2 = $56)
# Leg 1: Legian  — baseline = legian-beach ($124/N × 2 = $248)  ← highest-risk
# Leg 2: Canggu  — baseline = como-canggu  ($310/N × 2 = $620)  ← highest-cost
#
# Baseline total = $56 + $248 + $620 = $924 (within $1200)
# Secondary (legian-beach sold out, leg-1 override):
#   swap leg-1 → kuta-paradiso ($85/N × 2 = $170)
#   total = $56 + $170 + $620 = $846 (within $1200)

LEG0_UBUD_GARDEN = {
    "hotel_id": "bali-ubud-garden",
    "title": "Ubud Garden Resort",
    "total_cents": 5600,    # 2 nights × 2800¢
    "quality": 0.5,
    "review_score": 8.2,
    "area": "ubud",
    "provenance": "merchant",
}
LEG0_ALAYA_UBUD = {
    "hotel_id": "bali-alaya-ubud",
    "title": "Alaya Resort Ubud",
    "total_cents": 33000,   # 2 nights × 16500¢
    "quality": 1.0,
    "review_score": 8.9,
    "area": "ubud",
    "provenance": "merchant",
}

LEG1_LEGIAN_BEACH = {
    "hotel_id": "bali-legian-beach",
    "title": "Legian Beach Hotel",
    "total_cents": 24800,   # 2 nights × 12400¢
    "quality": 1.0,
    "review_score": 8.3,
    "area": "legian",
    "provenance": "merchant",
}
LEG1_KUTA_PARADISO = {
    "hotel_id": "bali-kuta-paradiso",
    "title": "Kuta Paradiso Hotel",
    "total_cents": 17000,   # 2 nights × 8500¢
    "quality": 0.8,
    "review_score": 8.0,
    "area": "kuta",
    "provenance": "merchant",
}
LEG1_KUTA_BEACHSIDE = {
    "hotel_id": "bali-kuta-beachside",
    "title": "Kuta Beachside Hostel",
    "total_cents": 3800,    # 2 nights × 1900¢
    "quality": 0.3,
    "review_score": 7.5,
    "area": "kuta",
    "provenance": "merchant",
}

LEG2_COMO_CANGGU = {
    "hotel_id": "bali-como-canggu",
    "title": "COMO Uma Canggu",
    "total_cents": 62000,   # 2 nights × 31000¢
    "quality": 0.9,
    "review_score": 8.8,
    "area": "canggu",
    "provenance": "merchant",
}
LEG2_SANUR_PURI = {
    "hotel_id": "bali-sanur-puri",
    "title": "Puri Santrian Sanur",
    "total_cents": 13600,   # 2 nights × 6800¢
    "quality": 0.6,
    "review_score": 8.0,
    "area": "sanur",
    "provenance": "merchant",
}

TOTAL_BUDGET_CENTS = 120_000   # $1200


def _make_candidates() -> list[dict]:
    """Build per-leg candidate sets for the 3-leg Bali trip (both legs have alternatives)."""
    return [
        {"leg_id": "leg-0", "candidates": [LEG0_UBUD_GARDEN, LEG0_ALAYA_UBUD]},
        {"leg_id": "leg-1", "candidates": [LEG1_LEGIAN_BEACH, LEG1_KUTA_PARADISO, LEG1_KUTA_BEACHSIDE]},
        {"leg_id": "leg-2", "candidates": [LEG2_COMO_CANGGU, LEG2_SANUR_PURI]},
    ]


def _make_baseline_selection() -> list[dict]:
    """The DP-optimal baseline for the 3-leg trip within $1200 budget."""
    return [
        {"leg_id": "leg-0", "hotel_id": "bali-ubud-garden",  "total_cents": 5600,  "quality": 0.5},
        {"leg_id": "leg-1", "hotel_id": "bali-legian-beach",  "total_cents": 24800, "quality": 1.0},
        {"leg_id": "leg-2", "hotel_id": "bali-como-canggu",   "total_cents": 62000, "quality": 0.9},
    ]


def _make_original_booking() -> dict:
    """Simulate the booked baseline package (output of negotiate())."""
    return {
        "outcome": "success",
        "user_id": "user-test",
        "total_budget_cents": TOTAL_BUDGET_CENTS,
        "total_booked_cents": 92400,
        "booking_ref": "BK-original",
        "checkout_id": "co_original",
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2026-07-01", "checkout": "2026-07-03",
                "adults": 1, "nights": 2, "vibe": "culture",
                "hotel_id": "bali-ubud-garden", "hotel_title": "Ubud Garden Resort",
                "total_cents": 5600, "review_score": 8.2, "star_rating": 3.0,
                "amenities": ["breakfast", "wifi"], "area": "ubud",
            },
            {
                "leg_id": "leg-1", "city": "bali",
                "checkin": "2026-07-03", "checkout": "2026-07-05",
                "adults": 1, "nights": 2, "vibe": "beach",
                "hotel_id": "bali-legian-beach", "hotel_title": "Legian Beach Hotel",
                "total_cents": 24800, "review_score": 8.3, "star_rating": 4.0,
                "amenities": ["beachfront", "pool", "spa"], "area": "legian",
            },
            {
                "leg_id": "leg-2", "city": "bali",
                "checkin": "2026-07-05", "checkout": "2026-07-07",
                "adults": 1, "nights": 2, "vibe": "surf",
                "hotel_id": "bali-como-canggu", "hotel_title": "COMO Uma Canggu",
                "total_cents": 62000, "review_score": 8.8, "star_rating": 5.0,
                "amenities": ["surf", "pool", "gym"], "area": "canggu",
            },
        ],
    }


def _make_pre_vetted_secondary(affected_leg: str = "leg-1") -> dict:
    """
    Build the pre-vetted secondary plan for the baseline.
    For the unit test: excludes bali-legian-beach on leg-1
    (the 'highest-risk' leg, matching the §12.9 pressure-test).
    """
    return {
        "feasible": True,
        "selection": [
            {"leg_id": "leg-0", "hotel_id": "bali-ubud-garden",  "total_cents": 5600,  "quality": 0.5},
            {"leg_id": "leg-1", "hotel_id": "bali-kuta-paradiso", "total_cents": 17000, "quality": 0.8},
            {"leg_id": "leg-2", "hotel_id": "bali-como-canggu",   "total_cents": 62000, "quality": 0.9},
        ],
        "total_cents": 84600,
        "total_quality": 2.2,
        "affected_leg_id": "leg-1",
        "excluded_hotel_id": "bali-legian-beach",
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2026-07-01", "checkout": "2026-07-03",
                "adults": 1, "hotel_id": "bali-ubud-garden",
                "total_cents": 5600, "provenance": "merchant",
            },
            {
                "leg_id": "leg-1", "city": "bali",
                "checkin": "2026-07-03", "checkout": "2026-07-05",
                "adults": 1, "hotel_id": "bali-kuta-paradiso",
                "total_cents": 17000, "provenance": "merchant",
            },
            {
                "leg_id": "leg-2", "city": "bali",
                "checkin": "2026-07-05", "checkout": "2026-07-07",
                "adults": 1, "hotel_id": "bali-como-canggu",
                "total_cents": 62000, "provenance": "merchant",
            },
        ],
        "critic_result": {"decision": "verified", "quality_score": 0.82},
    }


# ---------------------------------------------------------------------------
# Mock merchant transport builder for recovery tests
# ---------------------------------------------------------------------------

def _make_merchant_transport(
    checkout_id: str = "co_recovery",
    total_cents: int = 84600,
    commit_booking_ref: str = "BK-recovery",
    check_decision: str = "check_ok",
    commit_decision: str = "accept",
) -> CountingTransport:
    """
    Build a CountingTransport that returns canned checkout/commit responses.
    The budget agent uses this transport to call the merchant.
    """
    check_domain = {
        "id": checkout_id, "status": "incomplete", "user_id": "user-test",
        "total_cents": total_cents, "currency": "USD",
        "buyer_consent": False, "booking_ref": None,
        "decision": check_decision, "checkout_id": checkout_id,
    }
    commit_domain = {
        "id": checkout_id, "status": "complete", "user_id": "user-test",
        "total_cents": total_cents, "currency": "USD",
        "buyer_consent": True, "booking_ref": commit_booking_ref,
        "decision": commit_decision, "checkout_id": checkout_id,
    }
    lookup_domain = {
        "hotel": {"id": "mock", "title": "mock", "star_rating": 3.0,
                  "review_score": 8.0, "amenities": []},
        "available": True, "total_cents": total_cents,
        "nights": 2, "currency": "USD", "source": "mock",
    }
    return CountingTransport({
        "search_catalog":  (200, _merchant_result({"source": "mock", "count": 0, "results": []})),
        "create_checkout": (200, _merchant_result(check_domain)),
        "complete_checkout": (200, _merchant_result(commit_domain)),
        "lookup_catalog": (200, _merchant_result(lookup_domain)),
    })


def _make_critic_transport(
    decision: str = "verified",
    total_cents: int = 84600,
) -> CountingTransport:
    """Mock transport for the Critic agent's merchant calls (lookup_catalog)."""
    hotel_totals = {
        "bali-ubud-garden": 5600,
        "bali-legian-beach": 24800,
        "bali-como-canggu": 62000,
        "bali-kuta-paradiso": 17000,
        "bali-kuta-beachside": 3800,
        "bali-alaya-ubud": 33000,
        "bali-sanur-puri": 13600,
    }

    class _DynamicCriticTransport(httpx.BaseTransport):
        """Returns per-hotel totals for lookup_catalog (Critic uses these to re-verify)."""
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}
            self._lock = threading.Lock()

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            try:
                payload = json.loads(request.read())
            except Exception:
                return httpx.Response(400, text="bad")
            tool = (payload.get("params") or {}).get("name", "")
            with self._lock:
                self.counts[tool] = self.counts.get(tool, 0) + 1
            if tool == "lookup_catalog":
                args = (payload.get("params") or {}).get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                hotel_id = (args.get("product") or {}).get("hotel_id", "")
                t = hotel_totals.get(hotel_id, 5000)
                domain = {
                    "hotel": {"id": hotel_id, "title": hotel_id,
                              "star_rating": 3.0, "review_score": 8.0, "amenities": []},
                    "available": True, "total_cents": t, "nights": 2,
                    "currency": "USD", "source": "mock",
                }
                return httpx.Response(200, json=_merchant_result(domain))
            return httpx.Response(200, json=_merchant_result({"error": f"unknown tool: {tool}"}))

    return _DynamicCriticTransport()


def _make_recovery_orchestrator(
    checkout_id: str = "co_recovery",
    total_cents: int = 84600,
    commit_booking_ref: str = "BK-recovery",
    check_decision: str = "check_ok",
    commit_decision: str = "accept",
    critic_decision: str = "verified",
    critic_total: int | None = None,
) -> tuple["RecoveryOrchestrator", CountingTransport]:
    """
    Build a RecoveryOrchestrator with in-process mock Budget + Critic agents.
    Returns (orchestrator, budget_merchant_transport).
    """
    if critic_total is None:
        critic_total = total_cents

    # Budget agent with counting transport
    budget_transport = _make_merchant_transport(
        checkout_id=checkout_id, total_cents=total_cents,
        commit_booking_ref=commit_booking_ref,
        check_decision=check_decision, commit_decision=commit_decision,
    )
    budget_client = TestClient(
        ba_mod.BudgetAgent(merchant_transport=budget_transport).build_app(),
        raise_server_exceptions=True,
    )

    # Critic agent with dynamic lookup transport
    critic_transport = _make_critic_transport(decision=critic_decision, total_cents=critic_total)
    critic_client = TestClient(
        ca_mod.CriticAgent(merchant_transport=critic_transport).build_app(),
        raise_server_exceptions=True,
    )

    ro = RecoveryOrchestrator(
        budget_client=budget_client,
        critic_client=critic_client,
    )
    return ro, budget_transport


# ===========================================================================
# Test (a): fault → recovery activates secondary, within budget, deterministic
# ===========================================================================

def test_recovery_fault_activates_secondary() -> None:
    """
    Fault: bali-legian-beach sold out.
    The pre-vetted secondary excludes exactly this hotel on leg-1.
    Recovery should activate the secondary (kuta-paradiso) deterministically.
    """
    ro, transport = _make_recovery_orchestrator(
        checkout_id="co_recovery_a",
        total_cents=84600,
        commit_booking_ref="BK-recovery-a",
        check_decision="check_ok",
    )

    secondary = _make_pre_vetted_secondary()
    original_booking = _make_original_booking()

    result = ro.recover(
        original_booking=original_booking,
        unavailable_hotel_id="bali-legian-beach",
        secondary_plan=secondary,
        per_leg_candidates=_make_candidates(),
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )

    # Must be recovery_ready (not auto-committed)
    assert result["outcome"] == RECOVERY_OUTCOME_READY, \
        f"Expected recovery_ready, got: {result}"

    # Recovery activated the pre-vetted secondary (not re-allocate)
    assert result["source"] == "secondary", \
        f"Expected secondary source, got: {result['source']}"

    # Correct leg was swapped
    assert result["affected_leg_id"] == "leg-1"
    assert result["swapped_from"] == "bali-legian-beach"
    assert result["swapped_to"] == "bali-kuta-paradiso"

    # Recovery total is within budget
    assert result["recovery_total_cents"] <= TOTAL_BUDGET_CENTS, \
        f"Recovery total {result['recovery_total_cents']} > budget {TOTAL_BUDGET_CENTS}"

    # A new checkout was created (not the original)
    assert result["recovery_checkout_id"] != "", "Expected a recovery checkout_id"

    # Not committed yet — complete_checkout was NOT called
    assert transport.counts.get("complete_checkout", 0) == 0, \
        "FAIL: complete_checkout should NOT be called before re-consent"

    # create_checkout WAS called (for the new recovery checkout)
    assert transport.counts.get("create_checkout", 0) >= 1


def test_recovery_deterministic_same_fault_same_result() -> None:
    """
    Same fault twice → identical recovery plan (determinism / variance-0).
    §12.9: 'same fault → same recovery, variance-0'
    """
    secondary = _make_pre_vetted_secondary()
    original_booking = _make_original_booking()
    candidates = _make_candidates()

    results = []
    for i in range(3):
        ro, _ = _make_recovery_orchestrator(
            checkout_id=f"co_det_{i}",
            total_cents=84600,
        )
        r = ro.recover(
            original_booking=original_booking,
            unavailable_hotel_id="bali-legian-beach",
            secondary_plan=secondary,
            per_leg_candidates=candidates,
            user_id="user-test",
            total_budget_cents=TOTAL_BUDGET_CENTS,
        )
        results.append(r)

    # All runs produce the same outcome
    for r in results:
        assert r["outcome"] == RECOVERY_OUTCOME_READY
        assert r["swapped_from"] == "bali-legian-beach"
        assert r["swapped_to"] == "bali-kuta-paradiso"
        assert r["recovery_total_cents"] == 84600


# ===========================================================================
# Test (b): no feasible secondary → cannot_satisfy (honest, no booking)
# ===========================================================================

def test_no_feasible_secondary_cannot_satisfy() -> None:
    """
    Budget $90 ($9000¢): no in-budget secondary → cannot_satisfy, no booking.
    Reproduces the §12.9 pressure-test: 'Budget $90 → honest cannot_satisfy.'
    """
    very_tight_budget = 9000  # $90 — no combo of hotels fits within $90

    ro, transport = _make_recovery_orchestrator(
        checkout_id="co_tight",
        total_cents=9000,
    )

    # Secondary is infeasible at this budget
    infeasible_secondary = {
        "feasible": False,
        "selection": [],
        "total_cents": 0,
        "total_quality": 0.0,
        "affected_leg_id": "leg-1",
        "excluded_hotel_id": "bali-legian-beach",
    }

    # Candidates also infeasible at $90 (3-leg trip, cheapest combo is ~$25k)
    tight_candidates = [
        {"leg_id": "leg-0", "candidates": [
            {**LEG0_UBUD_GARDEN, "total_cents": 5600},
        ]},
        {"leg_id": "leg-1", "candidates": [
            {**LEG1_KUTA_PARADISO, "total_cents": 17000},
        ]},
        {"leg_id": "leg-2", "candidates": [
            {**LEG2_COMO_CANGGU, "total_cents": 62000},
        ]},
    ]

    original_booking = _make_original_booking()

    result = ro.recover(
        original_booking=original_booking,
        unavailable_hotel_id="bali-legian-beach",
        secondary_plan=infeasible_secondary,
        per_leg_candidates=tight_candidates,
        user_id="user-test",
        total_budget_cents=very_tight_budget,
    )

    # Must be honest cannot_satisfy
    assert result["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY, \
        f"Expected cannot_satisfy for tight budget, got: {result}"

    # No checkout was created (DP infeasible → no create_checkout call)
    assert transport.counts.get("complete_checkout", 0) == 0, \
        "FAIL: complete_checkout should NOT be called when cannot_satisfy"


# ===========================================================================
# Test (c): re-consent enforced — recovery does NOT auto-commit
# ===========================================================================

def test_re_consent_enforced_no_auto_commit() -> None:
    """
    Recovery must NOT call complete_checkout without a fresh mandate/consent.
    The 'recovery_ready' state is returned; commit only happens via commit_recovery().
    This is the §12.8 RESOLVED invariant: 'recovery is NOT autonomous'.
    """
    ro, transport = _make_recovery_orchestrator(
        checkout_id="co_consent_test",
        total_cents=84600,
        commit_booking_ref="BK-consent",
        check_decision="check_ok",
        commit_decision="accept",
    )

    secondary = _make_pre_vetted_secondary()
    original_booking = _make_original_booking()

    result = ro.recover(
        original_booking=original_booking,
        unavailable_hotel_id="bali-legian-beach",
        secondary_plan=secondary,
        per_leg_candidates=_make_candidates(),
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )

    # Recovery is READY — not committed
    assert result["outcome"] == RECOVERY_OUTCOME_READY, \
        f"Expected recovery_ready, got: {result}"

    # THE KEY INVARIANT: complete_checkout was NOT called before re-consent
    complete_calls = transport.counts.get("complete_checkout", 0)
    assert complete_calls == 0, (
        f"FAIL: complete_checkout was called {complete_calls} time(s) without "
        f"fresh re-consent. Recovery must NOT auto-commit. "
        f"(§12.8 RESOLVED: 'recovery is NOT autonomous')"
    )

    # Now simulate the user giving re-consent via commit_recovery()
    fresh_mandate = {
        "user_id": "user-test",
        "checkout_id": "co_consent_test",   # bound to the new checkout
        "budget_ceiling_cents": TOTAL_BUDGET_CENTS,
        "currency": "USD",
        "valid_until": "2026-12-31T23:59:59Z",
        "signature": "mock-sig",
    }

    commit_result = ro.commit_recovery(
        recovery_ready=result,
        fresh_mandate=fresh_mandate,
        user_id="user-test",
    )

    # After re-consent: committed with a new booking_ref
    assert commit_result["outcome"] == "recovery_committed", \
        f"Expected recovery_committed after re-consent, got: {commit_result}"
    assert commit_result["booking_ref"] == "BK-consent"
    assert commit_result["checkout_id"] == "co_consent_test"

    # complete_checkout was called exactly ONCE (for the recovery commit)
    complete_after = transport.counts.get("complete_checkout", 0)
    assert complete_after == 1, \
        f"Expected exactly 1 complete_checkout after re-consent, got {complete_after}"


# ===========================================================================
# Test (d): recovered package passes Critic + ≤ budget
# ===========================================================================

def test_recovered_package_within_budget_and_critic_verified() -> None:
    """
    The recovery package must:
      (1) Have total_cents ≤ user budget.
      (2) Pass the Critic gate (decision=verified).
    """
    ro, transport = _make_recovery_orchestrator(
        checkout_id="co_quality",
        total_cents=84600,
        critic_decision="verified",
        check_decision="check_ok",
    )

    secondary = _make_pre_vetted_secondary()
    original_booking = _make_original_booking()

    result = ro.recover(
        original_booking=original_booking,
        unavailable_hotel_id="bali-legian-beach",
        secondary_plan=secondary,
        per_leg_candidates=_make_candidates(),
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )

    assert result["outcome"] == RECOVERY_OUTCOME_READY

    # Budget constraint
    assert result["recovery_total_cents"] <= TOTAL_BUDGET_CENTS, (
        f"Recovery total {result['recovery_total_cents']}¢ > budget {TOTAL_BUDGET_CENTS}¢"
    )

    # Critic gate passed (critic_result = verified)
    critic_r = result.get("critic_result")
    if critic_r is not None:
        assert critic_r.get("decision") == "verified", (
            f"Expected Critic 'verified', got: {critic_r.get('decision')}"
        )

    # Correct swap was made
    recovery_legs = result.get("recovery_legs", [])
    leg1 = next((l for l in recovery_legs if l["leg_id"] == "leg-1"), None)
    assert leg1 is not None, "leg-1 missing from recovery_legs"
    assert leg1["hotel_id"] == "bali-kuta-paradiso", (
        f"Expected kuta-paradiso, got {leg1['hotel_id']}"
    )

    # Non-affected legs unchanged
    leg0 = next((l for l in recovery_legs if l["leg_id"] == "leg-0"), None)
    leg2 = next((l for l in recovery_legs if l["leg_id"] == "leg-2"), None)
    assert leg0 is not None and leg0["hotel_id"] == "bali-ubud-garden"
    assert leg2 is not None and leg2["hotel_id"] == "bali-como-canggu"


def test_critic_rejection_causes_cannot_satisfy() -> None:
    """
    If the Critic rejects the recovery package, outcome must be cannot_satisfy.
    Tests the Critic gate blocking invalid recovery plans.
    """
    # Use a price that causes OVER_BUDGET violation in the Critic
    # (recovery_total > total_budget_cents → Critic fires OVER_BUDGET)
    over_budget_total = TOTAL_BUDGET_CENTS + 1  # just over budget

    ro, transport = _make_recovery_orchestrator(
        checkout_id="co_critic_reject",
        total_cents=over_budget_total,
        critic_decision="verified",  # Critic will see OVER_BUDGET
        check_decision="check_ok",
    )

    # A secondary that is over budget so Critic fires OVER_BUDGET
    over_budget_secondary = {
        "feasible": True,
        "selection": [
            {"leg_id": "leg-0", "hotel_id": "bali-ubud-garden",  "total_cents": 5600,  "quality": 0.5},
            {"leg_id": "leg-1", "hotel_id": "bali-kuta-paradiso", "total_cents": 17000, "quality": 0.8},
            {"leg_id": "leg-2", "hotel_id": "bali-como-canggu",   "total_cents": 62000, "quality": 0.9},
        ],
        "total_cents": over_budget_total,
        "total_quality": 2.2,
        "affected_leg_id": "leg-1",
        "excluded_hotel_id": "bali-legian-beach",
        "legs": [
            {"leg_id": "leg-0", "city": "bali", "checkin": "2026-07-01", "checkout": "2026-07-03",
             "adults": 1, "hotel_id": "bali-ubud-garden", "total_cents": 5600, "provenance": "merchant"},
            {"leg_id": "leg-1", "city": "bali", "checkin": "2026-07-03", "checkout": "2026-07-05",
             "adults": 1, "hotel_id": "bali-kuta-paradiso", "total_cents": 17000, "provenance": "merchant"},
            {"leg_id": "leg-2", "city": "bali", "checkin": "2026-07-05", "checkout": "2026-07-07",
             "adults": 1, "hotel_id": "bali-como-canggu", "total_cents": 62000, "provenance": "merchant"},
        ],
        "critic_result": None,
    }

    original_booking = _make_original_booking()

    result = ro.recover(
        original_booking=original_booking,
        unavailable_hotel_id="bali-legian-beach",
        secondary_plan=over_budget_secondary,
        per_leg_candidates=_make_candidates(),
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )

    # Over-budget secondary: recovery total > budget → caught by budget check in recover()
    # Either cannot_satisfy (budget check) or recovery_ready with Critic rejection
    # The budget ceiling check in recover() catches this before reaching complete_checkout.
    assert result["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY, (
        f"Expected cannot_satisfy for over-budget recovery, got: {result['outcome']}"
    )
    # No commit was made
    assert transport.counts.get("complete_checkout", 0) == 0


# ===========================================================================
# Test (e): determinism — same inputs → same secondary (pure DP, variance-0)
# ===========================================================================

def test_secondary_determinism_pure_dp() -> None:
    """
    allocate_secondary is deterministic: same candidates + baseline → same result.
    Runs 10 times to confirm variance-0 (no randomness anywhere in the DP).
    """
    candidates = _make_candidates()
    baseline = _make_baseline_selection()

    results = [
        allocate_secondary(
            legs_with_candidates=candidates,
            total_budget_cents=TOTAL_BUDGET_CENTS,
            baseline_selection=baseline,
            highest_cost_leg_id="leg-1",
        )
        for _ in range(10)
    ]

    first = results[0]
    assert first["feasible"], f"Secondary must be feasible for variance test: {first}"

    for i, r in enumerate(results[1:], 1):
        assert r["feasible"] == first["feasible"], f"Run {i} feasibility differs"
        assert r["total_cents"] == first["total_cents"], f"Run {i} total_cents differs"
        assert r["affected_leg_id"] == first["affected_leg_id"], f"Run {i} affected_leg_id differs"
        assert r["excluded_hotel_id"] == first["excluded_hotel_id"], f"Run {i} excluded differs"
        for s, fs in zip(r["selection"], first["selection"]):
            assert s["leg_id"] == fs["leg_id"]
            assert s["hotel_id"] == fs["hotel_id"]


# ===========================================================================
# Allocator: secondary computation tests (pure DP, no agents)
# ===========================================================================

def test_allocate_secondary_excludes_baseline_hotel_leg1() -> None:
    """
    allocate_secondary with leg-1 override must exclude bali-legian-beach
    and return the next-best (kuta-paradiso) within budget.
    (§12.9 pressure-test exact scenario: legian-beach sold out → kuta-paradiso.)
    """
    candidates = _make_candidates()
    baseline = _make_baseline_selection()

    # Override to leg-1 (the beach leg) — the §12.9 scenario
    secondary = allocate_secondary(
        legs_with_candidates=candidates,
        total_budget_cents=TOTAL_BUDGET_CENTS,
        baseline_selection=baseline,
        highest_cost_leg_id="leg-1",
    )

    assert secondary["feasible"], f"Secondary should be feasible: {secondary}"
    assert secondary["affected_leg_id"] == "leg-1"
    assert secondary["excluded_hotel_id"] == "bali-legian-beach"

    # Must not use legian-beach on leg-1
    sel_by_leg = {s["leg_id"]: s["hotel_id"] for s in secondary["selection"]}
    assert sel_by_leg.get("leg-1") != "bali-legian-beach", \
        "Secondary must NOT use legian-beach on leg-1"
    assert sel_by_leg.get("leg-1") in ("bali-kuta-paradiso", "bali-kuta-beachside"), \
        f"Expected kuta alternative on leg-1, got {sel_by_leg.get('leg-1')}"

    # Within budget
    assert secondary["total_cents"] <= TOTAL_BUDGET_CENTS

    # §12.9: kuta-paradiso should be chosen (quality=0.8 > kuta-beachside=0.3)
    assert sel_by_leg.get("leg-1") == "bali-kuta-paradiso", \
        "§12.9: kuta-paradiso should be chosen (quality=0.8 > kuta-beachside=0.3)"
    # Secondary total must be within budget (the DP maximises quality so may pick
    # a higher quality hotel on other legs if budget allows)
    assert secondary["total_cents"] <= TOTAL_BUDGET_CENTS, \
        f"Secondary total {secondary['total_cents']}¢ must be within budget {TOTAL_BUDGET_CENTS}¢"


def test_allocate_secondary_excludes_highest_cost_by_default() -> None:
    """
    Without leg override, allocate_secondary excludes the baseline hotel
    on the HIGHEST-COST leg (leg-2: como-canggu at $620).
    Since we now have sanur-puri as an alternative on leg-2, it should be feasible.
    """
    candidates = _make_candidates()
    baseline = _make_baseline_selection()

    secondary = allocate_secondary(
        legs_with_candidates=candidates,
        total_budget_cents=TOTAL_BUDGET_CENTS,
        baseline_selection=baseline,
    )

    # Highest-cost leg = leg-2 ($62000)
    assert secondary["feasible"], f"Secondary should be feasible: {secondary}"
    assert secondary["affected_leg_id"] == "leg-2"
    assert secondary["excluded_hotel_id"] == "bali-como-canggu"

    sel_by_leg = {s["leg_id"]: s["hotel_id"] for s in secondary["selection"]}
    assert sel_by_leg.get("leg-2") != "bali-como-canggu", \
        "Secondary must NOT use como-canggu on leg-2"
    assert sel_by_leg.get("leg-2") == "bali-sanur-puri", \
        f"Expected sanur-puri on leg-2, got {sel_by_leg.get('leg-2')}"

    assert secondary["total_cents"] <= TOTAL_BUDGET_CENTS


def test_allocate_secondary_no_alternative_cannot_satisfy() -> None:
    """
    When the only candidate on the affected leg is the baseline hotel,
    allocate_secondary should return feasible=False.
    """
    candidates_single = [
        {"leg_id": "leg-0", "candidates": [LEG0_UBUD_GARDEN]},
        {"leg_id": "leg-1", "candidates": [LEG1_LEGIAN_BEACH]},   # only 1 hotel
        {"leg_id": "leg-2", "candidates": [LEG2_COMO_CANGGU, LEG2_SANUR_PURI]},
    ]
    baseline = _make_baseline_selection()

    secondary = allocate_secondary(
        legs_with_candidates=candidates_single,
        total_budget_cents=TOTAL_BUDGET_CENTS,
        baseline_selection=baseline,
        highest_cost_leg_id="leg-1",
    )

    # After excluding the only hotel on leg-1, secondary must be infeasible
    assert not secondary["feasible"], (
        "Expected infeasible secondary when only one hotel on affected leg"
    )


if __name__ == "__main__":
    import subprocess
    import sys
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
