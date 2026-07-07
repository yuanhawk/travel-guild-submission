"""
test_cascade_recovery.py — MULTI-FAULT CASCADE RECOVERY unit tests (§12.1).

The marquee depth-add: the society must survive a SEQUENCE of sequential faults
(grounded in a real flight-cancellation cascade), recovering each within budget
with ONE fresh re-consent per cycle, and failing honestly (cannot_satisfy) when a
fault is genuinely unrecoverable — never an autonomous re-spend, never a partial
booking.

Tests (CI-safe, mock merchant, no LLM, no network):

  (a) test_two_fault_cascade_survives
        Fault 1 (hotel sold-out) → swap → fresh re-consent → booking_ref_1.
        Fault 2 (flight cancel) → re-route → fresh re-consent → booking_ref_2.
        Outcome: cascade_survived; 2 cycles; final booking != original.

  (b) test_cascade_deterministic_same_faults_same_result
        Same fault sequence on same baseline → identical recovery (variance-0).

  (c) test_cascade_re_consent_enforced_each_cycle
        sign_mandate is invoked exactly ONCE per recovered cycle (never autonomous).

  (d) test_cascade_within_budget_each_cycle
        Every cycle's recovery total ≤ budget (the merchant veto is re-applied).

  (e) test_cascade_unrecoverable_fault_cannot_satisfy
        A flight cancel with no in-budget re-route → cannot_satisfy at that cycle;
        no commit is attempted for the failing fault; prior cycles stand.

  (f) test_cascade_hotel_then_flight_order_matters
        The 2nd fault hits the NEW (already-recovered) state, not the original.

Design: §12.1 §12.8 §12.9 (the internal design spec).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from agents import budget_agent as ba_mod
from agents import critic_agent as ca_mod
from utils.recovery import (
    RecoveryOrchestrator,
    FAULT_HOTEL_SOLD_OUT,
    FAULT_FLIGHT_CANCEL,
    CASCADE_OUTCOME_SURVIVED,
    CASCADE_OUTCOME_CANNOT_SATISFY,
)

TOTAL_BUDGET_CENTS = 120_000  # $1200


# ---------------------------------------------------------------------------
# Mock merchant: per-cycle create_checkout + complete_checkout responses.
# A cascade calls create_checkout once and complete_checkout once per cycle,
# so we return a fresh checkout_id + booking_ref per call (proves freshness).
# ---------------------------------------------------------------------------

def _merchant_result(domain: dict) -> dict:
    return {
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "structuredContent": domain,
            "content": [{"type": "text", "text": json.dumps(domain)}],
        },
    }


class CascadeMerchantTransport(httpx.BaseTransport):
    """
    Sim-aware mock merchant for cascade tests.

    - create_checkout: computes total from seeded per-hotel rates, returns a
      fresh checkout_id (co_N). Vetoes (decision=veto) if total > budget.
    - complete_checkout: returns a fresh booking_ref (BK-N), decision=accept.
    - lookup_catalog: per-hotel totals (for the Critic re-verify).
    """

    HOTEL_TOTALS = {
        # Ethiopia cascade fixtures (2 nights each unless overridden by dates)
        "addis-addis-skylight": 22000,
        "addis-addis-guesthouse": 10400,
        "gondar-gondar-goha": 19600,
        "gondar-gondar-fasil": 9600,
        "lalibela-lalibela-maribela": 20400,
        "lalibela-lalibela-mountainview": 9200,
        "semera-semera-erta": 15600,
        "semera-semera-desert": 8400,
    }

    def __init__(self, budget_cents: int = TOTAL_BUDGET_CENTS) -> None:
        self._budget = budget_cents
        self._co_seq = 0
        self._bk_seq = 0
        self.create_count = 0
        self.complete_count = 0
        self.cancel_count = 0
        self._lock = threading.Lock()
        # checkout_id -> total_cents (so complete echoes the right total)
        self._totals: dict[str, int] = {}
        # checkout_id -> status ("complete"|"cancelled"); models the merchant's
        # session state so tests can assert exactly-one-live-booking.
        self.status_by_checkout: dict[str, str] = {}
        # checkout_id -> booking_ref while live (dropped on cancel).
        self.live_ref_by_checkout: dict[str, str] = {}

    # --- test helpers (merchant-side state mirrors) -------------------------
    def status_of(self, checkout_id: str) -> str:
        with self._lock:
            return self.status_by_checkout.get(checkout_id, "unknown")

    def live_bookings(self) -> dict[str, str]:
        """checkout_id -> booking_ref for every booking still 'complete' (live)."""
        with self._lock:
            return {cid: self.live_ref_by_checkout[cid]
                    for cid, st in self.status_by_checkout.items()
                    if st == "complete"}

    def _nights(self, ci: str, co: str) -> int:
        from datetime import date
        try:
            y1, m1, d1 = map(int, ci.split("-"))
            y2, m2, d2 = map(int, co.split("-"))
            n = (date(y2, m2, d2) - date(y1, m1, d1)).days
            return max(n, 1)
        except Exception:
            return 2

    def _total_for(self, line_items: list[dict]) -> int:
        # Per-night rate = HOTEL_TOTALS/2 (fixtures are quoted at 2 nights).
        total = 0
        for li in line_items:
            base2 = self.HOTEL_TOTALS.get(li.get("hotel_id", ""), 10000)
            per_night = base2 // 2
            n = self._nights(li.get("checkin", ""), li.get("checkout", ""))
            total += per_night * n
        return total

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            payload = json.loads(request.read())
        except Exception:
            return httpx.Response(400, text="bad request")
        params = payload.get("params") or {}
        tool = params.get("name", "")
        args = params.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}

        with self._lock:
            if tool == "create_checkout":
                self.create_count += 1
                self._co_seq += 1
                co_id = f"co_cascade_{self._co_seq}"
                line_items = (args.get("checkout") or {}).get("line_items", [])
                total = self._total_for(line_items)
                self._totals[co_id] = total
                decision = "check_ok" if total <= self._budget else "veto"
                domain = {
                    "id": co_id, "status": "incomplete", "user_id": "user-test",
                    "total_cents": total, "currency": "USD",
                    "buyer_consent": False, "booking_ref": None,
                    "decision": decision, "checkout_id": co_id,
                }
                if decision == "veto":
                    domain["veto_reason"] = "price_exceeds_budget"
                return httpx.Response(200, json=_merchant_result(domain))

            if tool == "complete_checkout":
                self.complete_count += 1
                self._bk_seq += 1
                co_id = (args.get("checkout") or {}).get("id", "")
                total = self._totals.get(co_id, 0)
                ref = f"BK-cascade-{self._bk_seq}"
                self.status_by_checkout[co_id] = "complete"
                self.live_ref_by_checkout[co_id] = ref
                domain = {
                    "id": co_id, "status": "complete", "user_id": "user-test",
                    "total_cents": total, "currency": "USD",
                    "buyer_consent": True, "booking_ref": ref,
                    "decision": "accept", "checkout_id": co_id,
                }
                return httpx.Response(200, json=_merchant_result(domain))

            if tool == "cancel_checkout":
                self.cancel_count += 1
                co_id = (args.get("checkout") or {}).get("id", "")
                released = self.live_ref_by_checkout.pop(co_id, "")
                already = None
                if self.status_by_checkout.get(co_id) == "cancelled":
                    already = "cancelled"
                elif co_id not in self.status_by_checkout:
                    already = "unknown"
                self.status_by_checkout[co_id] = "cancelled"
                domain = {
                    "id": co_id, "status": "cancelled", "cancelled": True,
                    "released_booking_ref": released, "currency": "USD",
                }
                if already:
                    domain["already"] = already
                return httpx.Response(200, json=_merchant_result(domain))

        if tool == "lookup_catalog":
            hotel_id = (args.get("product") or {}).get("hotel_id", "")
            ci = (args.get("product") or {}).get("checkin", "")
            co = (args.get("product") or {}).get("checkout", "")
            base2 = self.HOTEL_TOTALS.get(hotel_id, 10000)
            per_night = base2 // 2
            n = self._nights(ci, co) if ci and co else 2
            domain = {
                "hotel": {"id": hotel_id, "title": hotel_id, "star_rating": 3.0,
                          "review_score": 8.0, "amenities": []},
                "available": True, "total_cents": per_night * n, "nights": n,
                "currency": "USD", "source": "mock",
            }
            return httpx.Response(200, json=_merchant_result(domain))

        if tool == "search_catalog":
            return httpx.Response(200, json=_merchant_result(
                {"source": "mock", "count": 0, "results": []}))

        return httpx.Response(200, json=_merchant_result({"error": f"unknown tool {tool}"}))


def _make_orchestrator(budget_cents: int = TOTAL_BUDGET_CENTS):
    """RecoveryOrchestrator wired to in-process mock Budget + Critic agents."""
    merchant = CascadeMerchantTransport(budget_cents=budget_cents)
    # Seed the COMMITTED baseline (the cascade's original_booking carries a real
    # checkout_id/booking_ref). Cycle 1 must VOID it, so it has to be live first.
    merchant.status_by_checkout["co_original"] = "complete"
    merchant.live_ref_by_checkout["co_original"] = "BK-original"
    budget_client = TestClient(
        ba_mod.BudgetAgent(merchant_transport=merchant).build_app(),
        raise_server_exceptions=True,
    )
    critic_client = TestClient(
        ca_mod.CriticAgent(merchant_transport=merchant).build_app(),
        raise_server_exceptions=True,
    )
    ro = RecoveryOrchestrator(budget_client=budget_client, critic_client=critic_client)
    return ro, merchant


# ---------------------------------------------------------------------------
# Ethiopia cascade fixtures (flight-dependent historic route)
# ---------------------------------------------------------------------------

def _ethiopia_baseline() -> dict:
    """
    Baseline 3-leg Ethiopia trip (the trip BEFORE the cascade):
      leg-0 Gondar    (gondar-goha)
      leg-1 Lalibela  (lalibela-maribela)
      leg-2 Semera    (semera-erta)  — gateway to Danakil
    """
    return {
        "outcome": "success",
        "user_id": "user-test",
        "total_budget_cents": TOTAL_BUDGET_CENTS,
        "total_booked_cents": 55600,
        "booking_ref": "BK-original",
        "checkout_id": "co_original",
        "legs": [
            {"leg_id": "leg-0", "city": "gondar", "area": "gondar",
             "checkin": "2026-11-24", "checkout": "2026-11-26", "adults": 1,
             "hotel_id": "gondar-gondar-goha", "total_cents": 19600,
             "provenance": "merchant"},
            {"leg_id": "leg-1", "city": "lalibela", "area": "lalibela",
             "checkin": "2026-11-26", "checkout": "2026-11-28", "adults": 1,
             "hotel_id": "lalibela-lalibela-maribela", "total_cents": 20400,
             "provenance": "merchant"},
            {"leg_id": "leg-2", "city": "semera", "area": "semera",
             "checkin": "2026-11-28", "checkout": "2026-11-30", "adults": 1,
             "hotel_id": "semera-semera-erta", "total_cents": 15600,
             "provenance": "merchant"},
        ],
    }


def _hotel_fault_secondary() -> dict:
    """
    Fault 1: gondar-goha sold out on leg-0 → pre-vetted secondary swaps to the
    cheaper Fasil Lodge. (Models a hotel sold-out fault inside the cascade.)
    """
    return {
        "type": FAULT_HOTEL_SOLD_OUT,
        "hotel_id": "gondar-gondar-goha",
        "secondary_plan": {
            "feasible": True,
            "affected_leg_id": "leg-0",
            "excluded_hotel_id": "gondar-gondar-goha",
            "total_cents": 45600,  # 9600 + 20400 + 15600
            "selection": [
                {"leg_id": "leg-0", "hotel_id": "gondar-gondar-fasil", "total_cents": 9600, "quality": 0.6},
                {"leg_id": "leg-1", "hotel_id": "lalibela-lalibela-maribela", "total_cents": 20400, "quality": 0.9},
                {"leg_id": "leg-2", "hotel_id": "semera-semera-erta", "total_cents": 15600, "quality": 0.7},
            ],
            "legs": [
                {"leg_id": "leg-0", "city": "gondar", "area": "gondar",
                 "checkin": "2026-11-24", "checkout": "2026-11-26", "adults": 1,
                 "hotel_id": "gondar-gondar-fasil", "total_cents": 9600, "provenance": "merchant"},
                {"leg_id": "leg-1", "city": "lalibela", "area": "lalibela",
                 "checkin": "2026-11-26", "checkout": "2026-11-28", "adults": 1,
                 "hotel_id": "lalibela-lalibela-maribela", "total_cents": 20400, "provenance": "merchant"},
                {"leg_id": "leg-2", "city": "semera", "area": "semera",
                 "checkin": "2026-11-28", "checkout": "2026-11-30", "adults": 1,
                 "hotel_id": "semera-semera-erta", "total_cents": 15600, "provenance": "merchant"},
            ],
            "critic_result": {"decision": "verified", "quality_score": 0.8},
        },
    }


def _flight_fault_reroute(in_budget: bool = True) -> dict:
    """
    Fault 2: ET106-style cancel of the Addis→Semera-equivalent edge here modeled
    as the lalibela→semera flight cancelling. The pre-vetted re-route inserts an
    unplanned Addis overnight (hub) then onward to Semera.

    in_budget=False makes the re-route blow the budget (an unrecoverable fault).
    """
    # Re-routed legs: keep leg-0/leg-1, insert an Addis overnight, then Semera.
    overnight = "addis-addis-guesthouse" if in_budget else "addis-addis-skylight"
    overnight_total = 5200 if in_budget else 11000
    # Add a hefty extra so the over-budget variant truly exceeds $1200.
    semera_total = 15600 if in_budget else 120000
    legs = [
        {"leg_id": "leg-0", "city": "gondar", "area": "gondar",
         "checkin": "2026-11-24", "checkout": "2026-11-26", "adults": 1,
         "hotel_id": "gondar-gondar-fasil", "total_cents": 9600, "provenance": "merchant"},
        {"leg_id": "leg-1", "city": "lalibela", "area": "lalibela",
         "checkin": "2026-11-26", "checkout": "2026-11-28", "adults": 1,
         "hotel_id": "lalibela-lalibela-maribela", "total_cents": 20400, "provenance": "merchant"},
        # NEW unplanned overnight at the Addis hub (the reroute)
        {"leg_id": "leg-1b", "city": "addis ababa", "area": "addis ababa",
         "checkin": "2026-11-28", "checkout": "2026-11-29", "adults": 1,
         "hotel_id": overnight, "total_cents": overnight_total, "provenance": "merchant"},
        {"leg_id": "leg-2", "city": "semera", "area": "semera",
         "checkin": "2026-11-29", "checkout": "2026-12-01", "adults": 1,
         "hotel_id": "semera-semera-erta", "total_cents": semera_total, "provenance": "merchant"},
    ]
    total = sum(l["total_cents"] for l in legs)
    return {
        "type": FAULT_FLIGHT_CANCEL,
        "from_city": "lalibela",
        "to_city": "semera",
        "reroute_option": {
            "feasible": True,
            "legs": legs,
            "total_cents": total,
            "affected_leg_id": "leg-2",
            "reroute_label": "via Addis hub + unplanned overnight",
        },
    }


def _l2_sign_mandate(recovery_ready: dict) -> dict:
    """Mock 'one fresh re-consent' — the L2 buyer_consent shape for the demo path."""
    return {"buyer_consent": True, "checkout_id": recovery_ready.get("recovery_checkout_id", "")}


# ===========================================================================
# (a) Two-fault cascade survives
# ===========================================================================

def test_two_fault_cascade_survives() -> None:
    ro, merchant = _make_orchestrator()
    result = ro.recover_cascade(
        original_booking=_ethiopia_baseline(),
        faults=[_hotel_fault_secondary(), _flight_fault_reroute(in_budget=True)],
        sign_mandate=_l2_sign_mandate,
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )
    assert result["outcome"] == CASCADE_OUTCOME_SURVIVED, result
    assert result["fault_count"] == 2
    assert len(result["cycles"]) == 2
    # Each cycle minted a fresh booking_ref (no autonomous re-use).
    refs = [c["booking_ref"] for c in result["cycles"]]
    assert len(set(refs)) == 2, f"expected distinct refs per cycle: {refs}"
    assert result["final_booking_ref"] == refs[-1]
    assert result["final_booking_ref"] != "BK-original"
    # Cycle 1 = hotel swap, cycle 2 = flight reroute.
    assert result["cycles"][0]["fault_type"] == FAULT_HOTEL_SOLD_OUT
    assert result["cycles"][0]["source"] == "secondary"
    assert result["cycles"][1]["fault_type"] == FAULT_FLIGHT_CANCEL
    assert result["cycles"][1]["source"] == "reroute"
    # Final state includes the inserted Addis overnight (the reroute landed).
    final_cities = [l["city"] for l in result["final_legs"]]
    assert "addis ababa" in final_cities, final_cities

    # ---- EXACTLY-ONE-LIVE-BOOKING invariant (§12.1) ----------------------
    # Each cycle voids the immediately-superseded booking: cycle 0 voids the
    # baseline, cycle 1 voids cycle 0's booking. End state: ONLY the final
    # booking_ref is live; baseline + all intermediates are cancelled.
    assert result["cycles"][0]["voided_booking_ref"] == "BK-original", result["cycles"][0]
    assert result["cycles"][0]["voided_checkout_id"] == "co_original"
    assert result["cycles"][1]["voided_booking_ref"] == result["cycles"][0]["booking_ref"]
    assert result["cycles"][1]["voided_checkout_id"] == result["cycles"][0]["checkout_id"]

    # At the merchant: the baseline and cycle-0 booking are cancelled; only the
    # final booking is complete.
    assert merchant.status_of("co_original") == "cancelled"
    assert merchant.status_of(result["cycles"][0]["checkout_id"]) == "cancelled"
    assert merchant.status_of(result["final_checkout_id"]) == "complete"
    live = merchant.live_bookings()
    assert len(live) == 1, f"expected exactly ONE live booking, got {live}"
    assert result["final_checkout_id"] in live
    assert live[result["final_checkout_id"]] == result["final_booking_ref"]

    # CUMULATIVE real committed spend == final_total_cents (NOT the sum of
    # per-cycle totals — each cycle's total is the FULL itinerary cost).
    cumulative_live_spend = merchant._totals[result["final_checkout_id"]]
    assert cumulative_live_spend == result["final_total_cents"], (
        cumulative_live_spend, result["final_total_cents"])
    sum_of_cycle_totals = sum(c["recovery_total_cents"] for c in result["cycles"])
    assert sum_of_cycle_totals > result["final_total_cents"], (
        "sanity: per-cycle totals sum to MORE than the final (proves we'd have "
        "double-charged without the void)")


# ===========================================================================
# (a2) Void invariant — prior booking cancelled each cycle; one cancel per cycle
# ===========================================================================

def test_cascade_voids_superseded_booking_each_cycle() -> None:
    ro, merchant = _make_orchestrator()
    result = ro.recover_cascade(
        original_booking=_ethiopia_baseline(),
        faults=[_hotel_fault_secondary(), _flight_fault_reroute(in_budget=True)],
        sign_mandate=_l2_sign_mandate,
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )
    assert result["outcome"] == CASCADE_OUTCOME_SURVIVED
    # Two committed cycles → two voids (baseline, then cycle-0 booking).
    assert merchant.cancel_count == 2, merchant.cancel_count
    # commit-new FIRST then void-old: 2 commits, 2 cancels, never a zero-live gap.
    assert merchant.complete_count == 2, merchant.complete_count
    # The merchant holds exactly one live (complete) booking at the end.
    assert len(merchant.live_bookings()) == 1
    # Every prior ref (baseline + cycle 0) is cancelled; only the final stands.
    cancelled = [cid for cid, st in merchant.status_by_checkout.items() if st == "cancelled"]
    assert "co_original" in cancelled
    assert result["cycles"][0]["checkout_id"] in cancelled
    assert result["final_checkout_id"] not in cancelled


def test_cascade_baseline_not_committed_skips_void() -> None:
    """If the baseline was CHECKED-only (no committed checkout_id/booking_ref),
    cycle 1 has nothing to void — the void is conditional (no spurious cancel)."""
    ro, merchant = _make_orchestrator()
    # The baseline was CHECKED-only — clear the seeded live baseline too.
    merchant.status_by_checkout.pop("co_original", None)
    merchant.live_ref_by_checkout.pop("co_original", None)
    baseline = _ethiopia_baseline()
    baseline.pop("checkout_id", None)
    baseline.pop("booking_ref", None)
    result = ro.recover_cascade(
        original_booking=baseline,
        faults=[_hotel_fault_secondary(), _flight_fault_reroute(in_budget=True)],
        sign_mandate=_l2_sign_mandate,
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )
    assert result["outcome"] == CASCADE_OUTCOME_SURVIVED
    # Cycle 0 voids NOTHING (no committed baseline); cycle 1 voids cycle 0.
    assert result["cycles"][0]["voided_checkout_id"] == ""
    assert result["cycles"][1]["voided_checkout_id"] == result["cycles"][0]["checkout_id"]
    assert merchant.cancel_count == 1, merchant.cancel_count
    assert len(merchant.live_bookings()) == 1


# ===========================================================================
# (b) Deterministic: same faults → same result (variance-0)
# ===========================================================================

def test_cascade_deterministic_same_faults_same_result() -> None:
    def run() -> dict:
        ro, _ = _make_orchestrator()
        return ro.recover_cascade(
            original_booking=_ethiopia_baseline(),
            faults=[_hotel_fault_secondary(), _flight_fault_reroute(in_budget=True)],
            sign_mandate=_l2_sign_mandate,
            user_id="user-test",
            total_budget_cents=TOTAL_BUDGET_CENTS,
        )

    r1, r2 = run(), run()
    # Booking refs differ (fresh each run) but the SUBSTANTIVE recovery is identical.
    sig = lambda r: (
        r["outcome"], r["fault_count"],
        [(c["fault_type"], c["source"], c["recovery_total_cents"]) for c in r["cycles"]],
        [(l["leg_id"], l["hotel_id"], l["total_cents"]) for l in r["final_legs"]],
        r["final_total_cents"],
    )
    assert sig(r1) == sig(r2), f"non-deterministic cascade:\n{sig(r1)}\n{sig(r2)}"


# ===========================================================================
# (c) One fresh re-consent per cycle (never autonomous)
# ===========================================================================

def test_cascade_re_consent_enforced_each_cycle() -> None:
    ro, merchant = _make_orchestrator()
    calls: list[str] = []

    def counting_sign(recovery_ready: dict) -> dict:
        calls.append(recovery_ready.get("recovery_checkout_id", ""))
        return _l2_sign_mandate(recovery_ready)

    result = ro.recover_cascade(
        original_booking=_ethiopia_baseline(),
        faults=[_hotel_fault_secondary(), _flight_fault_reroute(in_budget=True)],
        sign_mandate=counting_sign,
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )
    assert result["outcome"] == CASCADE_OUTCOME_SURVIVED
    # Exactly one fresh re-consent per recovered cycle.
    assert len(calls) == 2, f"expected 2 re-consents, got {len(calls)}"
    # Each re-consent bound a DISTINCT fresh checkout (no re-use).
    assert len(set(calls)) == 2, f"re-consents must bind distinct checkouts: {calls}"
    # The merchant committed exactly once per cycle (no autonomous extra commit).
    assert merchant.complete_count == 2, merchant.complete_count


# ===========================================================================
# (d) Within budget each cycle
# ===========================================================================

def test_cascade_within_budget_each_cycle() -> None:
    ro, _ = _make_orchestrator()
    result = ro.recover_cascade(
        original_booking=_ethiopia_baseline(),
        faults=[_hotel_fault_secondary(), _flight_fault_reroute(in_budget=True)],
        sign_mandate=_l2_sign_mandate,
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )
    assert result["outcome"] == CASCADE_OUTCOME_SURVIVED
    for c in result["cycles"]:
        assert c["recovery_total_cents"] <= TOTAL_BUDGET_CENTS, c
    assert result["final_total_cents"] <= TOTAL_BUDGET_CENTS


# ===========================================================================
# (e) Unrecoverable fault → honest cannot_satisfy (no commit, prior cycles stand)
# ===========================================================================

def test_cascade_unrecoverable_fault_cannot_satisfy() -> None:
    ro, merchant = _make_orchestrator()
    result = ro.recover_cascade(
        original_booking=_ethiopia_baseline(),
        faults=[
            _hotel_fault_secondary(),                 # cycle 0: recoverable
            _flight_fault_reroute(in_budget=False),   # cycle 1: re-route blows budget
        ],
        sign_mandate=_l2_sign_mandate,
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )
    assert result["outcome"] == CASCADE_OUTCOME_CANNOT_SATISFY, result
    assert result["failed_cycle_index"] == 1
    assert result["failed_fault"]["type"] == FAULT_FLIGHT_CANCEL
    # The first fault DID recover (its cycle is recorded).
    assert len(result["cycles"]) == 1
    assert result["cycles"][0]["fault_type"] == FAULT_HOTEL_SOLD_OUT
    # Exactly ONE commit happened (cycle 0); the failing fault never committed.
    assert merchant.complete_count == 1, merchant.complete_count


def test_cascade_flight_cancel_no_reroute_cannot_satisfy() -> None:
    """A flight cancel with NO re-route option at all → honest cannot_satisfy."""
    ro, merchant = _make_orchestrator()
    result = ro.recover_cascade(
        original_booking=_ethiopia_baseline(),
        faults=[{
            "type": FAULT_FLIGHT_CANCEL,
            "from_city": "lalibela",
            "to_city": "semera",
            "reroute_option": None,
        }],
        sign_mandate=_l2_sign_mandate,
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )
    assert result["outcome"] == CASCADE_OUTCOME_CANNOT_SATISFY
    assert result["failed_cycle_index"] == 0
    assert merchant.complete_count == 0, "no commit on an unrecoverable first fault"


# ===========================================================================
# (f) The 2nd fault hits the NEW state (order/state-advance correctness)
# ===========================================================================

def test_cascade_state_advances_between_faults() -> None:
    ro, _ = _make_orchestrator()
    result = ro.recover_cascade(
        original_booking=_ethiopia_baseline(),
        faults=[_hotel_fault_secondary(), _flight_fault_reroute(in_budget=True)],
        sign_mandate=_l2_sign_mandate,
        user_id="user-test",
        total_budget_cents=TOTAL_BUDGET_CENTS,
    )
    assert result["outcome"] == CASCADE_OUTCOME_SURVIVED
    # After cycle 0 (hotel swap), leg-0 is the cheaper Fasil Lodge; the reroute in
    # cycle 1 was built on THAT state, so the final leg-0 is still fasil (the
    # swap persisted into the second recovery).
    leg0 = next(l for l in result["final_legs"] if l["leg_id"] == "leg-0")
    assert leg0["hotel_id"] == "gondar-gondar-fasil", result["final_legs"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("\nAll cascade recovery tests passed.")
