"""
test_inv_insurance_hardveto_decline.py — #72 INSURANCE HARD-VETO DECLINE invariant.

INVARIANT: When a peril trip's lodging cost fits within the FULL budget but
lodging + insurance premium EXCEEDS the total budget, the society DECLINES
(outcome=cannot_satisfy, no booking_ref, reason mentions reserved fee/premium
envelope) PRE-commit — no merchant reservation is ever stranded.

Mirrors live_archetype_e2e.py A10/A10b (the #72 logic) but:
  - Uses a MOCK merchant transport (no live network, no paid API, var-0 safe).
  - Exercises negotiate() full-stack (Risk + Insurance real agents, mock HTTP seam
    only at the merchant boundary — same pattern as
    test_composition_wiring.test_insurance_exc_unrest1_composes_on_bookable_civil_unrest_trip).
  - Asserts the DECLINE specifically (not just that premium_estimate > 0 like
    test_insurance_agent.py does).

Key numbers (fully deterministic, derived from seeded risk + insurance agents):
  TOTAL_BUDGET_CENTS = 120_000  ($1200)
  HOTEL_PRICE_CENTS  = 110_000  ($1100)  — fits full budget, exceeds lodging budget
  PREMIUM_EST_CENTS  = 11_700   ($117)   — from InsuranceAgent at ceiling=120_000
  LODGING_BUDGET     = 108_300  ($1083)  — total - premium_est

  110_000 > 108_300  →  merchant's create_checkout total > lodging budget  →  veto
  110_000 ≤ 120_000  →  would have booked WITHOUT the #72 envelope reservation

Test structure:
  1. test_insurance_hardveto_decline_cannot_satisfy   — primary invariant assert
  2. test_insurance_hardveto_no_stranded_booking_ref  — no-stranding safety assert
     (budget veto fires PRE-commit; create_checkout was called but complete_checkout
     was NEVER called because the veto terminated the round before commit)
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

import httpx
import pytest

# Make the society package importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from starlette.testclient import TestClient

from agents.accommodation_agent import AccommodationAgent
from agents.budget_agent import BudgetAgent
from agents.critic_agent import CriticAgent
from agents.day_planner_agent import DayPlannerAgent
from agents.destination_agent import DestinationAgent
from agents.fraud_agent import FraudAgent
from agents.health_agent import HealthAgent
from agents.compliance_agent import ComplianceAgent
from agents.insurance_agent import InsuranceAgent
from agents.planner_agent import PlannerAgent
from agents.risk_agent import RiskAgent
from agents.transport_agent import TransportAgent
from orchestration.orchestrator import TravelOrchestrator


# ---------------------------------------------------------------------------
# Constants — fully deterministic from the seeded risk + insurance agents.
# Changing any of these triggers a test failure, which is intentional: they
# ARE the regression guard for the #72 hard-veto invariant.
# ---------------------------------------------------------------------------

# Total budget in cents ($1200).
TOTAL_BUDGET_CENTS: int = 120_000

# Hotel price in cents ($1100).
#   - Fits the full budget   (110_000 ≤ 120_000)  → would book WITHOUT #72
#   - Exceeds lodging budget (110_000 > 108_300)   → MUST decline WITH #72
HOTEL_PRICE_CENTS: int = 110_000

# Darwin February = cyclone / flood peril window.  The seeded RiskAgent emits
# reason_codes = ['natural_disaster', 'flood']; peril_crosswalk maps these to
# natural_disaster; InsuranceAgent computes premium_cents = 11_700 at ceiling
# 120_000 (9.75 bps × 120_000).  This is asserted below; if the seed or rate
# changes, the test fails deterministically and the constants must be updated.
EXPECTED_PREMIUM_CENTS: int = 11_700

# Derived: lodging budget left after reserving the premium.
EXPECTED_LODGING_BUDGET: int = TOTAL_BUDGET_CENTS - EXPECTED_PREMIUM_CENTS  # 108_300

# Trip dates: future February (cyclone season), well past any today=2026 guard.
CHECKIN: str = "2027-02-15"
CHECKOUT: str = "2027-02-18"


# ---------------------------------------------------------------------------
# Mock-merchant transport infrastructure (mirrors test_composition_wiring pattern)
# ---------------------------------------------------------------------------

class _CountingTransport(httpx.BaseTransport):
    """
    Intercept merchant MCP calls and return canned per-tool responses.
    List responses cycle through; the last element is repeated indefinitely.
    Thread-safe (lock on per-tool counters).
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses: dict[str, list] = {}
        for tool, resp in responses.items():
            self._responses[tool] = list(resp) if isinstance(resp, list) else [resp]
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
        tool = (payload.get("params") or {}).get("name", "")
        with self._lock:
            self._counts[tool] = self._counts.get(tool, 0) + 1
        resps = self._responses.get(tool)
        if not resps:
            return httpx.Response(500, json={"error": f"mock: no response for {tool!r}"})
        idx = min(self._counts[tool] - 1, len(resps) - 1)
        status, body = resps[idx]
        return httpx.Response(status, json=body)


def _merchant_result(domain: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "structuredContent": domain,
        "content": [{"type": "text", "text": json.dumps(domain)}],
    }}


def _catalog_result(results: list[dict]) -> tuple[int, dict]:
    return (200, _merchant_result({
        "source": "mock", "count": len(results), "results": results,
    }))


def _catalog_empty() -> tuple[int, dict]:
    return (200, _merchant_result({"source": "mock", "count": 0, "results": []}))


# ---------------------------------------------------------------------------
# Shared mock hotel fixture
#
# The hotel price is set to HOTEL_PRICE_CENTS = 110_000c, which:
#   - fits within TOTAL_BUDGET_CENTS (120_000) → lodging itself is affordable,
#   - exceeds EXPECTED_LODGING_BUDGET (108_300) → veto fires once the #72
#     envelope (premium = 11_700) is reserved in the budget ceiling passed to
#     the merchant.
# ---------------------------------------------------------------------------

_DARWIN_HOTEL = {
    "hotel_id": "darwin-cbd-mock",
    "title": "Darwin CBD Mock Hotel",
    "city": "darwin",
    "review_score": 7.5,
    "star_rating": 3.0,
    "nights": 3,
    "total_cents": HOTEL_PRICE_CENTS,
    "amenities": ["wifi", "pool"],
    "provenance": "merchant",
}

# create_checkout returns the hotel price at the (unreduced) total_cents so the
# budget agent's Python-side pre-veto fires when it compares against the
# LODGING budget ceiling (not the full budget).  complete_checkout is NEVER
# reached (the veto terminates the round pre-commit), so we set it to an
# intentionally-failing response to catch any accidental commit.
_CREATE_CHECKOUT_RESPONSE = _merchant_result({
    "id": "co-darwin-mock-1",
    "status": "incomplete",
    "user_id": "inv-hardveto",
    "line_items": [],
    "total_cents": HOTEL_PRICE_CENTS,   # 110_000 > EXPECTED_LODGING_BUDGET (108_300)
    "currency": "USD",
    "buyer_consent": False,
    "booking_ref": "",
})

_COMPLETE_CHECKOUT_SHOULD_NOT_BE_CALLED = _merchant_result({
    "id": "co-darwin-mock-1",
    "status": "error",
    "error": "INVARIANT VIOLATION: complete_checkout reached on a veto path",
})


def _build_peril_orch() -> TravelOrchestrator:
    """
    Build a TravelOrchestrator with:
      - Real agents: Planner, Destination, Risk, Insurance, Transport (fully
        seeded; these are the units under test for the invariant).
      - Mock merchant transport at the HTTP seam for Accommodation + Budget
        (so no live network / paid API is needed).
      - Critic NOT wired (the veto fires before the critic gate; omitting it
        keeps the mock surface minimal and the test focused on the #72 path).
    """
    acc_transport = _CountingTransport({
        "search_catalog": _catalog_result([_DARWIN_HOTEL]),
    })
    budget_transport = _CountingTransport({
        "create_checkout": (200, _CREATE_CHECKOUT_RESPONSE),
        # complete_checkout is deliberately last so any accidental call is
        # detected via the error status surfaced by the budget agent.
        "complete_checkout": (200, _COMPLETE_CHECKOUT_SHOULD_NOT_BE_CALLED),
    })

    return TravelOrchestrator(
        planner_client=TestClient(PlannerAgent().build_app()),
        accommodation_client=TestClient(
            AccommodationAgent(merchant_transport=acc_transport).build_app()),
        budget_client=TestClient(
            BudgetAgent(merchant_transport=budget_transport).build_app()),
        # Critic omitted intentionally: veto fires pre-critic; keeping mock
        # surface minimal and test focused on the #72 veto path.
        destination_client=TestClient(DestinationAgent().build_app()),
        risk_client=TestClient(RiskAgent().build_app()),
        insurance_client=TestClient(InsuranceAgent().build_app()),
        transport_client=TestClient(TransportAgent().build_app()),
    )


def _peril_trip(*, user_id: str = "inv-hardveto", budget: int = TOTAL_BUDGET_CENTS) -> dict:
    return {
        "user_id": user_id,
        "total_budget_cents": budget,
        "today": "2026-06-23",  # frozen; deterministic health/compliance gate dates
        "legs": [{
            "city": "darwin",
            "dest_country": "AU",
            "departure_date": CHECKIN,
            "checkin": CHECKIN,
            "checkout": CHECKOUT,
            "adults": 2,
            "vibe": "relax",
        }],
        "buyer_consent": True,
        "consent": True,
    }


# ---------------------------------------------------------------------------
# Test 1: Primary invariant — DECLINE when lodging + premium exceeds budget
# ---------------------------------------------------------------------------

def test_insurance_hardveto_decline_cannot_satisfy() -> None:
    """
    #72 HARD-VETO INVARIANT: a Darwin-Feb (peril) trip whose lodging FITS the full
    budget but lodging + insurance premium EXCEEDS total_budget_cents must be
    DECLINED (outcome=cannot_satisfy, no booking_ref) PRE-commit.

    Mechanism under test (end-to-end through negotiate()):
      1. RiskAgent emits natural_disaster + flood for Darwin-Feb (seeded).
      2. _estimate_insurance_premium_cents calls InsuranceAgent at ceiling=120_000
         → premium_est = 11_700c.
      3. lodging_budget_cents = 120_000 - 11_700 = 108_300.
      4. BudgetAgent.check: create_checkout returns 110_000c total;
         110_000 > 108_300 → veto (PRE-commit, no complete_checkout).
      5. Re-plan: accommodation ceiling tightened below 110_000 → no_fit
         (mock always returns the same hotel; the accommodation agent's own
         max_cents filter drops it as over-ceiling in the re-plan round).
      6. negotiate() returns cannot_satisfy with the #72 envelope reason.

    This test would FAIL (outcome=success) if #72 lodging_budget reservation
    were removed — proving the test meaningfully exercises the invariant.
    """
    orch = _build_peril_orch()
    result = orch.negotiate(_peril_trip())

    # --- Primary invariant: DECLINE, never a success ---
    assert result.get("outcome") == "cannot_satisfy", (
        f"#72 HARD-VETO INVARIANT BROKEN: expected cannot_satisfy but got "
        f"outcome={result.get('outcome')!r}. "
        f"The lodging ({HOTEL_PRICE_CENTS}c) fits the full budget ({TOTAL_BUDGET_CENTS}c) "
        f"but exceeds the lodging budget ({EXPECTED_LODGING_BUDGET}c) after reserving the "
        f"insurance premium ({EXPECTED_PREMIUM_CENTS}c). "
        f"The society must DECLINE pre-commit, not book. result={result}"
    )

    # --- No booking_ref (all-or-none: declined trips carry no committed booking) ---
    booking_ref = result.get("booking_ref")
    assert not booking_ref, (
        f"#72 NO-STRANDING: a declined trip must NOT carry a booking_ref. "
        f"Got booking_ref={booking_ref!r}. The merchant's create_checkout was called "
        f"(budget CHECK) but complete_checkout must NOT have been called (no commit). "
        f"result={result}"
    )
    checkout_id = result.get("checkout_id")
    assert not checkout_id, (
        f"#72 NO-STRANDING: a declined trip must NOT carry a checkout_id that "
        f"implies a committed session. Got checkout_id={checkout_id!r}. result={result}"
    )

    # --- Reason must mention the reserved fee/premium envelope ---
    reason = (result.get("reason") or "").lower()
    # The #72 envelope reason at line 4315-4320 of orchestrator.py reads:
    #   "... is reserved for required fees + insurance premium, leaving ..."
    assert "insurance premium" in reason or "reserved" in reason, (
        f"#72 HONEST REASON: the decline reason must mention the reserved "
        f"fee/premium envelope so the user understands WHY the budget was tight. "
        f"Got reason={result.get('reason')!r}. result={result}"
    )

    # --- Sanity: confirm the Risk agent actually emitted a peril for Darwin-Feb ---
    # (If it didn't, the premium estimate would be 0 and lodging_budget == full budget,
    # making this test vacuous / testing the wrong invariant.)
    risk_signals = result.get("risk_signals") or {}
    reason_codes = risk_signals.get("reason_codes") or []
    assert reason_codes, (
        f"#72 PRECONDITION: Risk must emit peril reason_codes for Darwin-Feb "
        f"(the peril is what triggers the insurance premium and #72 veto). "
        f"Got risk_signals={risk_signals!r}. result={result}"
    )

    print(
        f"[#72-INV] PASS — Darwin-Feb peril trip declined pre-commit as required: "
        f"outcome={result['outcome']}, risk_codes={reason_codes}, "
        f"reason={result.get('reason', '')[:120]!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: No-stranding — complete_checkout was NEVER called on the veto path
# ---------------------------------------------------------------------------

def test_insurance_hardveto_no_stranded_booking_ref() -> None:
    """
    #72 NO-STRANDING: the budget veto fires at the CHECK phase (create_checkout,
    no capture). complete_checkout — the IRREVERSIBLE booking step — must NEVER
    be called when the #72 veto terminates the round pre-commit.

    This test verifies that the mock's complete_checkout counter stays at zero,
    proving no booking was committed at the merchant before the decline was
    returned to the caller.

    Companion to A10b in live_archetype_e2e.py.
    """
    # Use a dedicated tracking transport to observe which merchant calls were made.
    complete_checkout_calls: list[str] = []

    class _TrackingTransport(httpx.BaseTransport):
        """Like _CountingTransport but records complete_checkout call bodies."""

        def __init__(self) -> None:
            self._create_count = 0
            self._lock = threading.Lock()

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            try:
                payload = json.loads(request.read())
            except Exception:
                return httpx.Response(400, text="bad request")
            tool = (payload.get("params") or {}).get("name", "")
            with self._lock:
                if tool == "complete_checkout":
                    # Record the call so the assertion can detect it
                    complete_checkout_calls.append(tool)
                    # Return an error so the test fails loudly if commit fires
                    return httpx.Response(200, json=_merchant_result({
                        "id": "", "status": "error",
                        "error": "SHOULD_NOT_REACH_COMPLETE_CHECKOUT_ON_VETO_PATH",
                    }))
                if tool == "create_checkout":
                    self._create_count += 1
                    return httpx.Response(200, json=_CREATE_CHECKOUT_RESPONSE)
                return httpx.Response(500, json={"error": f"unexpected tool {tool!r}"})

    tracking = _TrackingTransport()
    acc_transport = _CountingTransport({
        "search_catalog": _catalog_result([_DARWIN_HOTEL]),
    })

    orch = TravelOrchestrator(
        planner_client=TestClient(PlannerAgent().build_app()),
        accommodation_client=TestClient(
            AccommodationAgent(merchant_transport=acc_transport).build_app()),
        budget_client=TestClient(
            BudgetAgent(merchant_transport=tracking).build_app()),
        destination_client=TestClient(DestinationAgent().build_app()),
        risk_client=TestClient(RiskAgent().build_app()),
        insurance_client=TestClient(InsuranceAgent().build_app()),
        transport_client=TestClient(TransportAgent().build_app()),
    )

    result = orch.negotiate(_peril_trip(user_id="inv-hardveto-nostrand"))

    # The trip must decline (same invariant as test 1)
    assert result.get("outcome") == "cannot_satisfy", (
        f"#72 NO-STRANDING PRECONDITION: expected cannot_satisfy, got {result.get('outcome')!r}. "
        f"result={result}"
    )

    # complete_checkout must NOT have been called — the veto fires at CHECK
    # (create_checkout), which is the non-committing phase.  complete_checkout
    # is the irreversible booking step and must NEVER be reached on a veto.
    assert complete_checkout_calls == [], (
        f"#72 NO-STRANDING INVARIANT BROKEN: complete_checkout was called "
        f"{len(complete_checkout_calls)} time(s) on a veto path. "
        f"A declined trip must NOT strand a committed merchant reservation. "
        f"Calls: {complete_checkout_calls}"
    )

    print(
        f"[#72-NOSTRAND] PASS — complete_checkout was never called on the "
        f"veto/decline path (no stranded booking). outcome={result['outcome']}"
    )


# ---------------------------------------------------------------------------
# Test 3: Baseline sanity — verify the EXPECTED_PREMIUM_CENTS constant is correct.
#
# This test is not testing the orchestrator; it is a deterministic guard that
# catches any future calibration change in the insurance or risk agents that
# would invalidate the arithmetic in tests 1 and 2.
# ---------------------------------------------------------------------------

def test_hardveto_premium_constant_is_correct() -> None:
    """
    Guard: the EXPECTED_PREMIUM_CENTS constant matches what the real InsuranceAgent
    returns for the Darwin-Feb peril at TOTAL_BUDGET_CENTS ceiling. If the seeded
    premium rates or peril mapping change, this test fails first and tells the
    developer to update the test constants.
    """
    from utils.peril_crosswalk import risk_reasons_to_perils
    from agents.insurance_agent import assess_coverage

    # Darwin-Feb risk codes from the seeded RiskAgent (also verified in test 1)
    darwin_feb_codes = ["natural_disaster", "flood"]
    perils = risk_reasons_to_perils(darwin_feb_codes)
    peril_set = [p.value if hasattr(p, "value") else str(p) for p in perils]

    # Compute premium twice to confirm var-0
    a1 = assess_coverage(peril_set=peril_set, insured_trip_cost_cents=TOTAL_BUDGET_CENTS)
    a2 = assess_coverage(peril_set=peril_set, insured_trip_cost_cents=TOTAL_BUDGET_CENTS)
    prem1 = int(a1.get("premium_cents", 0) or 0)
    prem2 = int(a2.get("premium_cents", 0) or 0)

    assert prem1 == prem2, (
        f"Premium computation is NOT var-0 across two identical calls: "
        f"{prem1} vs {prem2}. Fix the non-determinism before testing the invariant."
    )
    assert prem1 == EXPECTED_PREMIUM_CENTS, (
        f"EXPECTED_PREMIUM_CENTS is stale: code returns {prem1} but constant is "
        f"{EXPECTED_PREMIUM_CENTS}. Update the test constants to match the current "
        f"seeded premium rate, then verify the hotel price ({HOTEL_PRICE_CENTS}) still "
        f"satisfies: hotel > lodging_budget ({TOTAL_BUDGET_CENTS - prem1}) "
        f"AND hotel <= total_budget ({TOTAL_BUDGET_CENTS})."
    )
    # Also verify the test's structural assumption holds with the current premium:
    lodging_budget = TOTAL_BUDGET_CENTS - prem1
    assert HOTEL_PRICE_CENTS > lodging_budget, (
        f"HOTEL_PRICE_CENTS ({HOTEL_PRICE_CENTS}) must exceed lodging_budget "
        f"({lodging_budget}) for the veto to fire. Update HOTEL_PRICE_CENTS."
    )
    assert HOTEL_PRICE_CENTS <= TOTAL_BUDGET_CENTS, (
        f"HOTEL_PRICE_CENTS ({HOTEL_PRICE_CENTS}) must fit the full budget "
        f"({TOTAL_BUDGET_CENTS}) to show that without #72 it would have booked. "
        f"Update HOTEL_PRICE_CENTS."
    )

    print(
        f"[#72-CONST] PASS — premium constants are correct and var-0: "
        f"budget={TOTAL_BUDGET_CENTS}, premium={prem1}, "
        f"lodging_budget={lodging_budget}, hotel={HOTEL_PRICE_CENTS}"
    )
