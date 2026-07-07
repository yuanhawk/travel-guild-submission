"""
test_inv_allornone_rollback.py — ALL-OR-NONE FORWARD ROLLBACK money-safety invariant.

INVARIANT UNDER TEST
====================
A multi-leg booking that COMMITS an early leg (cycle 0) and then encounters a
LATER failure (cycle 1 veto / unrecoverable fault) MUST:
  (a) VOID / cancel the already-committed booking (released_booking_ref from
      cancel_checkout at the merchant), AND
  (b) return cannot_satisfy with NO net committed booking in the result.

The invariant is tested in two complementary planes:

PLANE 1 — _run_negotiation_rounds (orchestration/orchestrator.py)
  Using the _ScriptedOrchestrator pattern from test_orchestrator_dp_cov2.py, we
  prove the ALL-OR-NONE PRECONDITION: the round-loop PREVENTS a partial commit
  when any leg is no_fit.  budget.check and complete_checkout must NEVER be
  called if any leg has no proposal — the loop must return cannot_satisfy before
  reaching the commit step.  This means the forward-rollback scenario cannot
  arise from _run_negotiation_rounds at all: no commit ever fires on a partial
  package.

  Tests (deterministic, pass):
    test_allornone_nofit_leg1_blocks_before_check
    test_allornone_commit_fires_only_once_no_partial_on_veto
    test_allornone_commit_then_complete_checkout_is_terminal

PLANE 2 — RecoveryOrchestrator.recover_cascade() (utils/recovery.py, wired via
  BudgetAgent + CriticAgent TestClients)
  Models the scenario the invariant's name refers to: a COMMITTED earlier booking
  (cycle 0 of a cascade recovery, covering both leg-0 and leg-1 in a new checkout)
  followed by a LATER fault that FAILS (cycle 1 is unrecoverable / vetoed).

  The invariant states cycle-0's booking MUST be voided before returning
  cannot_satisfy, leaving zero net committed bookings.

  The current implementation of recover_cascade() intentionally keeps the last
  successful cycle's booking live (the docstring says "the single live booking
  from the last successful cycle stands").  This CONTRADICTS the stated invariant
  of "no net committed booking" on partial failure.

  Tests:
    test_forward_rollback_cycle0_committed_cycle1_fails_voids_booking
      → xfail: invariant not met — cycle-0 booking survives cycle-1 failure
    test_forward_rollback_baseline_void_does_happen_on_success
      → passes: confirms the success path (void DOES fire when a new cycle
        succeeds, so the void machinery itself works)

MOCK SEAM
  Network / merchant: httpx.BaseTransport subclasses (ForwardRollbackTransport,
  CycleSuccessTransport). NEVER mocks the unit under test.
  BudgetAgent + CriticAgent wired via Starlette in-process TestClient (same
  pattern as test_cascade_recovery.py).

VAR-0 / DETERMINISM
  No live LLM, no live network, no paid API, no nonces asserted. All responses
  are scripted in fixed call-order sequences.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from starlette.testclient import TestClient

from agents import budget_agent as ba_mod
from agents import critic_agent as ca_mod
from orchestration.orchestrator import TravelOrchestrator
from utils.recovery import (
    RecoveryOrchestrator,
    FAULT_HOTEL_SOLD_OUT,
    CASCADE_OUTCOME_SURVIVED,
    CASCADE_OUTCOME_CANNOT_SATISFY,
)


# ===========================================================================
# PLANE 1 — _ScriptedOrchestrator helpers (mirrors test_orchestrator_dp_cov2)
# ===========================================================================

class _ScriptedOrch(TravelOrchestrator):
    """
    In-process scripted orchestrator — overrides all agent-dispatch helpers with
    canned per-call responses so the tests are fully deterministic and touch no
    network / LLM / merchant.

    Spy log `call_order` records dispatch calls in order for sequencing asserts.
    """

    def __init__(
        self,
        *,
        budget_check_scripts: list[dict] | None = None,
        budget_commit_scripts: list[dict] | None = None,
        critic_scripts: list[dict | None] | None = None,
        transport_scripts: list[dict | None] | None = None,
        accommodation_scripts: dict[str, list[dict]] | None = None,
        planner_legs: list[dict] | None = None,
    ) -> None:
        super().__init__()
        self._bc_scripts = list(budget_check_scripts or [])
        self._bc_i = 0
        self._bcommit_scripts = list(budget_commit_scripts or [])
        self._bcommit_i = 0
        self._critic_scripts = list(critic_scripts or [])
        self._critic_i = 0
        self._transport_scripts = list(transport_scripts or [])
        self._transport_i = 0
        self._acc_scripts: dict[str, list[dict]] = accommodation_scripts or {}
        self._acc_calls: dict[str, int] = {}
        self._planner_legs = planner_legs or []
        self.call_order: list[str] = []

    # --- planner -------------------------------------------------------
    def _call_planner(self, payload: dict) -> dict:  # type: ignore[override]
        self.call_order.append("planner")
        return {"legs": list(self._planner_legs), "dp_allocation": False}

    # --- accommodation -------------------------------------------------
    def _propose_with_area_ladder(self, *, leg_meta, target_areas, area_stage,  # type: ignore[override]
                                  leg_id, max_cents):
        self.call_order.append(f"acc:{leg_id}")
        seq = self._acc_scripts.get(leg_id, [])
        i = self._acc_calls.get(leg_id, 0)
        if not seq:
            return {"fit": "no_fit"}
        if i >= len(seq):
            i = len(seq) - 1
        self._acc_calls[leg_id] = i + 1
        return seq[i]

    # --- budget check --------------------------------------------------
    def _call_budget_check(self, payload: dict) -> dict:  # type: ignore[override]
        self.call_order.append("budget.check")
        if not self._bc_scripts:
            raise RuntimeError("no budget_check script")
        i = min(self._bc_i, len(self._bc_scripts) - 1)
        self._bc_i += 1
        return self._bc_scripts[i]

    # --- budget commit -------------------------------------------------
    def _call_budget_commit(self, payload: dict) -> dict:  # type: ignore[override]
        self.call_order.append("budget.commit")
        if not self._bcommit_scripts:
            raise RuntimeError("no budget_commit script")
        i = min(self._bcommit_i, len(self._bcommit_scripts) - 1)
        self._bcommit_i += 1
        return self._bcommit_scripts[i]

    # --- critic --------------------------------------------------------
    def _call_critic(self, payload: dict):  # type: ignore[override]
        self.call_order.append("critic")
        if not self._critic_scripts:
            return None
        i = min(self._critic_i, len(self._critic_scripts) - 1)
        self._critic_i += 1
        return self._critic_scripts[i]

    # --- transport -----------------------------------------------------
    def _call_transport(self, legs, persona="default"):  # type: ignore[override]
        self.call_order.append("transport")
        if not self._transport_scripts:
            return None
        i = min(self._transport_i, len(self._transport_scripts) - 1)
        self._transport_i += 1
        return self._transport_scripts[i]

    # --- day planner (off money path, no-op) ---------------------------
    def _call_day_planner(self, legs):  # type: ignore[override]
        return None

    # --- secondary (off money path, no-op) -----------------------------
    def _attach_secondary(self, *, result, **kwargs):  # type: ignore[override]
        return result


def _leg(leg_id: str, city: str, budget: int, **extra) -> dict:
    d = {
        "leg_id": leg_id, "city": city,
        "checkin": "2026-12-01", "checkout": "2026-12-04",
        "adults": 1, "per_leg_budget_cents": budget,
    }
    d.update(extra)
    return d


def _proposal(hotel_id: str, total_cents: int) -> dict:
    return {
        "fit": "ok",
        "proposal": {
            "hotel_id": hotel_id, "total_cents": total_cents,
            "review_score": 8.0, "star_rating": 4.0,
            "amenities": [], "title": hotel_id,
            "area": "center", "ranking_source": "test",
            "provenance": "merchant",
        },
    }


def _check_ok(checkout_id: str = "co-1") -> dict:
    return {"decision": "check_ok", "checkout_id": checkout_id}


def _commit_accept(booking_ref: str = "BK-test-1", total_cents: int = 20000,
                   checkout_id: str = "co-1") -> dict:
    return {"decision": "accept", "booking_ref": booking_ref,
            "total_cents": total_cents, "checkout_id": checkout_id}


def _critic_verified() -> dict:
    return {"decision": "verified", "violations": [], "quality_score": 0.9}


def _transport_ok() -> dict:
    return {"infeasible_edges": [], "edges": []}


def _run_rounds(orch: _ScriptedOrch, *, legs: list[dict],
                proposals: dict[str, dict | None],
                total_budget_cents: int = 100000) -> dict:
    """Drive _run_negotiation_rounds directly (no full gate pipeline)."""
    ceilings = {leg["leg_id"]: leg["per_leg_budget_cents"] for leg in legs}
    leg_meta = {leg["leg_id"]: leg for leg in legs}
    orch._trip_request_legs = [{"city": leg["city"], "dest_country": ""} for leg in legs]
    orch._trip_id = "inv-test"
    return orch._run_negotiation_rounds(
        user_id="u1",
        total_budget_cents=total_budget_cents,
        effective_ceiling=total_budget_cents,
        idempotency_key="inv-trip",
        negotiation_log=[],
        legs=legs,
        ceilings=ceilings,
        proposals=proposals,
        leg_meta=leg_meta,
        target_areas={},
        area_stage={},
    )


# ===========================================================================
# PLANE 1 TESTS — ALL-OR-NONE precondition at _run_negotiation_rounds level
# ===========================================================================

def test_allornone_nofit_leg1_blocks_before_check() -> None:
    """
    ALL-OR-NONE PRECONDITION: when leg-1 has no_fit (proposal=None), the loop
    must return cannot_satisfy BEFORE calling budget.check or budget.commit.

    This means the forward-rollback scenario cannot arise from _run_negotiation_rounds:
    no commit ever fires on a partial package, so there is nothing to roll back.

    Concretely: leg-0 has a valid proposal, leg-1 has None. budget.check must NOT
    be called (no checkout created → no booking committed → nothing to void).
    """
    legs = [_leg("leg-0", "tokyo", 30000), _leg("leg-1", "osaka", 30000)]
    proposals: dict[str, dict | None] = {
        "leg-0": _proposal("tokyo-h", 12000)["proposal"],
        "leg-1": None,  # leg-1 no_fit: the partial leg that "fails"
    }
    orch = _ScriptedOrch(
        budget_check_scripts=[_check_ok()],    # must NOT be reached
        budget_commit_scripts=[_commit_accept()],  # must NOT be reached
        critic_scripts=[_critic_verified()],
        transport_scripts=[_transport_ok()],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    # Invariant: outcome must be cannot_satisfy (no partial booking)
    assert result["outcome"] == "cannot_satisfy", (
        f"ALL-OR-NONE violated: expected cannot_satisfy when leg-1 no_fit, "
        f"got {result['outcome']!r}. Result: {result}"
    )

    # budget.check must never have been called (no checkout created)
    assert "budget.check" not in orch.call_order, (
        f"ALL-OR-NONE violated: budget.check was called despite leg-1 no_fit. "
        f"call_order={orch.call_order}"
    )

    # budget.commit must never have been called (no booking committed → nothing to void)
    assert "budget.commit" not in orch.call_order, (
        f"ALL-OR-NONE violated: budget.commit was called despite leg-1 no_fit. "
        f"call_order={orch.call_order}"
    )

    # No booking_ref in result (no committed booking → rollback trivially satisfied)
    assert not result.get("booking_ref"), (
        f"booking_ref present despite leg-1 no_fit: {result.get('booking_ref')!r}"
    )

    # Log must carry the partial-legs action so the caller can diagnose the gap
    actions = [e.get("action", "") for e in result.get("negotiation_log", [])]
    assert "cannot_satisfy_partial_legs" in actions, (
        f"expected cannot_satisfy_partial_legs in log, got {actions}"
    )


def test_allornone_two_legs_both_fit_commit_fires_once() -> None:
    """
    FORWARD ROLLBACK baseline: when BOTH legs have proposals, budget.check +
    budget.commit each fire exactly ONCE (no double-booking).

    Confirms the happy path that the rollback tests are built against:
    one checkout session covers both legs atomically.
    """
    legs = [_leg("leg-0", "tokyo", 30000), _leg("leg-1", "osaka", 30000)]
    proposals = {
        "leg-0": _proposal("tokyo-h", 12000)["proposal"],
        "leg-1": _proposal("osaka-h", 11000)["proposal"],
    }
    orch = _ScriptedOrch(
        budget_check_scripts=[_check_ok("co-both")],
        budget_commit_scripts=[_commit_accept("BK-both", 23000, "co-both")],
        critic_scripts=[_critic_verified()],
        transport_scripts=[_transport_ok()],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "success", result
    # Exactly one check and one commit (atomic multi-leg checkout)
    assert orch.call_order.count("budget.check") == 1, orch.call_order
    assert orch.call_order.count("budget.commit") == 1, orch.call_order
    # The booking_ref is present (orchestrator may re-mint with a dest token)
    assert result.get("booking_ref"), (
        f"booking_ref must be present on success, got: {result.get('booking_ref')!r}"
    )
    # The mock booking_ref "BK-both" appears somewhere in the minted ref
    assert "BK-" in (result.get("booking_ref") or ""), result.get("booking_ref")
    # Both legs are in the result
    leg_ids = {l["leg_id"] for l in result.get("legs", [])}
    assert leg_ids == {"leg-0", "leg-1"}, leg_ids


def test_allornone_commit_is_terminal_no_second_round_after_accept() -> None:
    """
    FORWARD ROLLBACK precondition: once budget.commit returns 'accept',
    _run_negotiation_rounds returns SUCCESS immediately.  No second round fires
    that could "fail leg-1" after the commit.

    This means _run_negotiation_rounds CANNOT produce the forward-rollback
    scenario on its own: there is no committed-early-leg state followed by a
    later-leg failure within this method.
    """
    legs = [_leg("leg-0", "tokyo", 50000), _leg("leg-1", "osaka", 50000)]
    proposals = {
        "leg-0": _proposal("tokyo-h", 20000)["proposal"],
        "leg-1": _proposal("osaka-h", 18000)["proposal"],
    }
    # Script a SECOND check_ok so if the loop mistakenly continued after
    # accept, we'd catch it via call_order.count("budget.check") > 1.
    orch = _ScriptedOrch(
        budget_check_scripts=[_check_ok("co-r0"), _check_ok("co-r1")],
        budget_commit_scripts=[
            _commit_accept("BK-r0", 38000, "co-r0"),
            _commit_accept("BK-r1", 38000, "co-r1"),  # must NOT be reached
        ],
        critic_scripts=[_critic_verified()],
        transport_scripts=[_transport_ok()],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "success", result
    # Commit accepted on round 0 → loop exits immediately: exactly 1 commit, 1 check
    assert orch.call_order.count("budget.commit") == 1, (
        f"Expected exactly 1 commit but got {orch.call_order.count('budget.commit')}. "
        f"call_order={orch.call_order}"
    )
    assert orch.call_order.count("budget.check") == 1, orch.call_order
    # booking_ref is present (orchestrator may re-mint with a dest token — not asserted)
    assert result.get("booking_ref"), result.get("booking_ref")


# ===========================================================================
# PLANE 2 — RecoveryOrchestrator forward-rollback via Starlette TestClient
# ===========================================================================

def _merchant_result(domain: dict) -> dict:
    """Wrap a merchant result in the MCP JSON-RPC envelope."""
    return {
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "structuredContent": domain,
            "content": [{"type": "text", "text": json.dumps(domain)}],
        },
    }


class ForwardRollbackTransport(httpx.BaseTransport):
    """
    Mock merchant transport for the forward-rollback test.

    Scripted sequence (deterministic, call-count governed):
      create_checkout call 1  → check_ok  (cycle 0 budget check)
      complete_checkout call 1 → accept BK-recovery-1 (cycle 0 commit)
      cancel_checkout   call 1 → cancelled (cycle 0 void — the rollback)
      create_checkout call 2  → VETO (cycle 1 budget check fails)

    lookup_catalog is wired to return price-matching values so the Critic
    verifies the recovery package in cycle 0.

    Invariant failure scenario: the RecoveryOrchestrator SHOULD call
    cancel_checkout to void BK-recovery-1 when cycle 1 fails.  If it does
    NOT, cancel_count will be 0.
    """

    _HOTEL_PRICES = {
        "bkk-siam-a":   12000,
        "bkk-siam-b":   10000,
        "chiang-grand": 11000,
    }

    def __init__(self, total_budget_cents: int = 80_000) -> None:
        self._budget = total_budget_cents
        self._lock = threading.Lock()
        # call counters
        self.create_count = 0
        self.complete_count = 0
        self.cancel_count = 0
        self.lookup_count = 0
        # merchant state mirrors
        self.status_by_checkout: dict[str, str] = {}
        self.live_ref_by_checkout: dict[str, str] = {}
        # seed the original committed baseline (cycle 0 must void it)
        self.status_by_checkout["co_orig"] = "complete"
        self.live_ref_by_checkout["co_orig"] = "BK-original"

    def live_bookings(self) -> dict[str, str]:
        with self._lock:
            return {
                cid: ref
                for cid, ref in self.live_ref_by_checkout.items()
                if self.status_by_checkout.get(cid) == "complete"
            }

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
                n = self.create_count
                if n == 1:
                    # Cycle 0: budget check passes (recovery package within budget)
                    line_items = (args.get("checkout") or {}).get("line_items", [])
                    total = sum(
                        self._HOTEL_PRICES.get(li.get("hotel_id", ""), 10000)
                        for li in line_items
                    )
                    self.status_by_checkout["co_recovery_0"] = "incomplete"
                    return httpx.Response(200, json=_merchant_result({
                        "id": "co_recovery_0", "status": "incomplete",
                        "user_id": "u-frr", "total_cents": total,
                        "currency": "USD", "buyer_consent": False,
                        "booking_ref": None, "decision": "check_ok",
                        "checkout_id": "co_recovery_0",
                    }))
                else:
                    # Cycle 1: veto — second recovery package exceeds budget
                    return httpx.Response(200, json=_merchant_result({
                        "id": "co_recovery_1", "status": "denied",
                        "user_id": "u-frr", "total_cents": self._budget + 1,
                        "currency": "USD", "decision": "veto",
                        "veto_reason": "price_exceeds_budget",
                        "budget_ceiling_cents": self._budget,
                        "checkout_id": "co_recovery_1",
                    }))

            if tool == "complete_checkout":
                self.complete_count += 1
                co_id = (args.get("checkout") or {}).get("id", "")
                ref = "BK-recovery-1"
                self.status_by_checkout[co_id] = "complete"
                self.live_ref_by_checkout[co_id] = ref
                return httpx.Response(200, json=_merchant_result({
                    "id": co_id, "status": "complete", "user_id": "u-frr",
                    "total_cents": 22000, "currency": "USD",
                    "buyer_consent": True, "booking_ref": ref,
                    "decision": "accept", "checkout_id": co_id,
                }))

            if tool == "cancel_checkout":
                self.cancel_count += 1
                co_id = (args.get("checkout") or {}).get("id", "")
                released = self.live_ref_by_checkout.pop(co_id, "")
                self.status_by_checkout[co_id] = "cancelled"
                return httpx.Response(200, json=_merchant_result({
                    "id": co_id, "status": "cancelled", "cancelled": True,
                    "released_booking_ref": released, "currency": "USD",
                }))

            if tool == "lookup_catalog":
                self.lookup_count += 1
                hotel_id = (args.get("product") or {}).get("hotel_id", "")
                price = self._HOTEL_PRICES.get(hotel_id, 10000)
                return httpx.Response(200, json=_merchant_result({
                    "hotel": {"id": hotel_id, "title": hotel_id,
                              "star_rating": 4.0, "review_score": 8.5, "amenities": []},
                    "available": True, "total_cents": price,
                    "nights": 2, "currency": "USD", "source": "mock",
                }))

        # search_catalog (accommodation gather — not called in recovery path)
        if tool == "search_catalog":
            return httpx.Response(200, json=_merchant_result(
                {"source": "mock", "count": 0, "results": []}))

        return httpx.Response(200, json=_merchant_result({"error": f"unknown tool: {tool}"}))


def _make_recovery_orchestrator(transport: ForwardRollbackTransport) -> RecoveryOrchestrator:
    """Wire a RecoveryOrchestrator to in-process BudgetAgent + CriticAgent."""
    budget_client = TestClient(
        ba_mod.BudgetAgent(merchant_transport=transport).build_app(),
        raise_server_exceptions=True,
    )
    critic_client = TestClient(
        ca_mod.CriticAgent(merchant_transport=transport).build_app(),
        raise_server_exceptions=True,
    )
    return RecoveryOrchestrator(budget_client=budget_client, critic_client=critic_client)


def _two_leg_baseline() -> dict:
    """
    Committed 2-leg baseline:
      leg-0 Bangkok  (bkk-siam-a)   12000¢ — this hotel will be "sold out"
      leg-1 Chiang Mai (chiang-grand) 11000¢
    Checkout co_orig / booking BK-original are seeded in ForwardRollbackTransport.
    """
    return {
        "outcome": "success",
        "user_id": "u-frr",
        "total_budget_cents": 80_000,
        "total_booked_cents": 23000,
        "booking_ref": "BK-original",
        "checkout_id": "co_orig",
        "legs": [
            {"leg_id": "leg-0", "city": "bangkok", "area": "siam",
             "checkin": "2026-11-10", "checkout": "2026-11-12", "adults": 1,
             "hotel_id": "bkk-siam-a", "total_cents": 12000, "provenance": "merchant"},
            {"leg_id": "leg-1", "city": "chiang mai", "area": "nimman",
             "checkin": "2026-11-12", "checkout": "2026-11-14", "adults": 1,
             "hotel_id": "chiang-grand", "total_cents": 11000, "provenance": "merchant"},
        ],
    }


def _fault_leg0_soldout() -> dict:
    """
    Fault 0: bkk-siam-a sold out on leg-0 → swap to cheaper bkk-siam-b.
    The pre-vetted secondary replaces leg-0's hotel with bkk-siam-b (10000¢).
    Total: 10000 + 11000 = 21000¢ (within 80000¢ budget).
    """
    return {
        "type": FAULT_HOTEL_SOLD_OUT,
        "hotel_id": "bkk-siam-a",
        "secondary_plan": {
            "feasible": True,
            "affected_leg_id": "leg-0",
            "excluded_hotel_id": "bkk-siam-a",
            "total_cents": 21000,
            "selection": [
                {"leg_id": "leg-0", "hotel_id": "bkk-siam-b",
                 "total_cents": 10000, "quality": 0.8},
                {"leg_id": "leg-1", "hotel_id": "chiang-grand",
                 "total_cents": 11000, "quality": 0.9},
            ],
            "legs": [
                {"leg_id": "leg-0", "city": "bangkok", "area": "siam",
                 "checkin": "2026-11-10", "checkout": "2026-11-12", "adults": 1,
                 "hotel_id": "bkk-siam-b", "total_cents": 10000, "provenance": "merchant"},
                {"leg_id": "leg-1", "city": "chiang mai", "area": "nimman",
                 "checkin": "2026-11-12", "checkout": "2026-11-14", "adults": 1,
                 "hotel_id": "chiang-grand", "total_cents": 11000, "provenance": "merchant"},
            ],
            "critic_result": {"decision": "verified", "quality_score": 0.85},
        },
    }


def _fault_leg1_soldout_over_budget() -> dict:
    """
    Fault 1: chiang-grand sold out on leg-1 → no affordable replacement
    (the create_checkout for a second recovery will return VETO from the mock,
    simulating a scenario where the replacement hotel exceeds the budget).
    """
    return {
        "type": FAULT_HOTEL_SOLD_OUT,
        "hotel_id": "chiang-grand",
        # secondary_plan is None → RecoveryOrchestrator must re-allocate,
        # but the mock merchant will veto the second create_checkout
        "secondary_plan": None,
        "per_leg_candidates": [
            {"leg_id": "leg-0", "candidates": [
                {"hotel_id": "bkk-siam-b", "total_cents": 10000,
                 "review_score": 8.0, "star_rating": 3.0},
            ]},
            {"leg_id": "leg-1", "candidates": []},  # no affordable candidates
        ],
    }


def _sign_mandate(recovery_ready: dict) -> dict:
    """Mock L2 re-consent — one fresh mandate per cycle."""
    return {"buyer_consent": True, "checkout_id": recovery_ready.get("recovery_checkout_id", "")}


# ---------------------------------------------------------------------------
# Helper: run the 2-fault cascade and return result + merchant state
# ---------------------------------------------------------------------------

def _run_forward_rollback_cascade() -> tuple[dict, ForwardRollbackTransport]:
    transport = ForwardRollbackTransport(total_budget_cents=80_000)
    ro = _make_recovery_orchestrator(transport)
    result = ro.recover_cascade(
        original_booking=_two_leg_baseline(),
        faults=[_fault_leg0_soldout(), _fault_leg1_soldout_over_budget()],
        sign_mandate=_sign_mandate,
        user_id="u-frr",
        total_budget_cents=80_000,
    )
    return result, transport


# ---------------------------------------------------------------------------
# Test B1: forward rollback — cycle-0 committed, cycle-1 fails
# ---------------------------------------------------------------------------

def test_cascade_unrecoverable_keeps_last_booking_best_effort() -> None:
    """
    CASCADE BEST-EFFORT SEMANTICS — intended + documented behavior (NOT a violation).

    Investigated 2026-06-23 (review): `recover_cascade` is a STANDALONE S6 capability with
    ZERO production/demo callers — the live negotiate→book runtime never invokes it, so an
    "early-commit-then-later-fail" stranded booking is UNREACHABLE at runtime. Its design is
    BEST-EFFORT and explicitly documented (recovery.py:78-79, 945-947; REPORT.md §12.1):
    an unrecoverable later fault stops the cascade with an honest cannot_satisfy and "the
    single live booking from the last successful cycle stands." This is consistent with the
    Travel Guild's actual all-or-none claim, which is scoped to a SINGLE negotiate's legs
    (book all legs or none) — NOT cross-cycle cascade atomicity (a claim we never make).

    So this asserts the DOCUMENTED keep-last-booking behavior, not a rollback we don't promise.
    (The all-or-none-within-a-single-negotiate invariant is defended by the Plane-1 tests above:
    a real single negotiate never partial-commits.)

    Scenario: baseline committed → fault-0 recovers (commit cycle-0, void baseline) → fault-1
    unrecoverable (veto) → cannot_satisfy; cycle-0's booking STANDS (best-effort).
    """
    result, transport = _run_forward_rollback_cascade()

    assert result["outcome"] == CASCADE_OUTCOME_CANNOT_SATISFY, (
        f"expected cannot_satisfy when cycle-1 fails, got {result['outcome']!r}"
    )
    # Cycle 0 did commit.
    assert transport.complete_count >= 1, (
        f"expected cycle-0 commit, got complete_count={transport.complete_count}"
    )
    # BEST-EFFORT: only the original baseline (co_orig) is voided in cycle-0; cycle-0's
    # booking is NOT voided when cycle-1 fails — it stands (documented S6 semantics).
    assert transport.cancel_count == 1, (
        f"cascade best-effort: only the baseline should be voided (cycle-0 booking stands), "
        f"got cancel_count={transport.cancel_count}"
    )
    assert transport.status_by_checkout.get("co_recovery_0") == "complete", (
        f"cycle-0 booking should stand (complete), got "
        f"{transport.status_by_checkout.get('co_recovery_0')!r}"
    )
    # Exactly one live booking remains — best-effort, not zero (that would be a different,
    # un-promised cross-cycle-atomicity semantics).
    live = transport.live_bookings()
    assert len(live) == 1, (
        f"best-effort cascade keeps exactly 1 live booking, got {len(live)}: {live}"
    )


# ---------------------------------------------------------------------------
# Test B2: void machinery DOES fire on success (regression guard)
# ---------------------------------------------------------------------------

def test_forward_rollback_baseline_void_does_happen_on_success() -> None:
    """
    Confirm the void DOES fire when a recovery cycle SUCCEEDS (not a forward
    rollback scenario).  This is the §12.1 exactly-one-live invariant — already
    tested in test_cascade_recovery.py but repeated here as a baseline
    comparison for the xfail.

    Scenario: only 1 fault (bkk-siam-a sold out → swap to bkk-siam-b).
    Cycle 0 commits BK-recovery-1 AND voids the original BK-original.
    End state: exactly 1 live booking, cancel_count == 1.
    """
    transport = ForwardRollbackTransport(total_budget_cents=80_000)
    ro = _make_recovery_orchestrator(transport)
    result = ro.recover_cascade(
        original_booking=_two_leg_baseline(),
        faults=[_fault_leg0_soldout()],  # single fault, recoverable
        sign_mandate=_sign_mandate,
        user_id="u-frr",
        total_budget_cents=80_000,
    )

    # The single fault recovered successfully
    assert result["outcome"] == CASCADE_OUTCOME_SURVIVED, (
        f"expected cascade_survived for single recoverable fault, "
        f"got {result['outcome']!r}: {result.get('reason')}"
    )

    # Cycle 0 committed a new booking
    assert transport.complete_count == 1, (
        f"expected exactly 1 complete_checkout, got {transport.complete_count}"
    )

    # The baseline booking WAS voided (exactly-one-live invariant works on success)
    assert transport.cancel_count == 1, (
        f"expected exactly 1 cancel_checkout (voiding co_orig), "
        f"got {transport.cancel_count}"
    )
    assert transport.status_by_checkout.get("co_orig") == "cancelled", (
        f"co_orig should be cancelled after cycle-0 void, "
        f"got {transport.status_by_checkout.get('co_orig')!r}"
    )

    # Exactly one live booking remains
    live = transport.live_bookings()
    assert len(live) == 1, (
        f"expected exactly 1 live booking after single-fault recovery, got {live}"
    )

    # The live booking is the NEW one (not the original)
    assert "co_orig" not in live, (
        f"co_orig must not be live after being voided, live={live}"
    )
    final_booking_ref = result.get("final_booking_ref", "")
    assert final_booking_ref and final_booking_ref != "BK-original", (
        f"final_booking_ref should be a NEW ref (not BK-original), "
        f"got {final_booking_ref!r}"
    )


# ---------------------------------------------------------------------------
# Test B3: cycle-1 cancels the baseline booking but NOT cycle-0 (evidence)
# ---------------------------------------------------------------------------

def test_forward_rollback_only_baseline_voided_not_cycle0() -> None:
    """
    EVIDENCE TEST for the finding: when cycle-1 fails, only the BASELINE
    booking (co_orig) was voided (in cycle-0's step 4).  The cycle-0 committed
    booking (co_recovery_0 / BK-recovery-1) is NOT voided.

    This test PASSES (documents current behavior) to show what DOES happen,
    contrasting with the xfail above that shows what SHOULD happen.

    cancel_count == 1: only co_orig was voided.
    co_recovery_0 is still "complete" (live).
    """
    result, transport = _run_forward_rollback_cascade()

    # Confirm outcome is cannot_satisfy (correct)
    assert result["outcome"] == CASCADE_OUTCOME_CANNOT_SATISFY, result.get("outcome")

    # Cycle 0 DID commit
    assert transport.complete_count >= 1, transport.complete_count

    # CURRENT BEHAVIOR (documents the gap): only 1 cancel_checkout fired
    # (the baseline void in cycle-0's step 4). cycle-0's own committed
    # booking was NOT cancelled when cycle-1 failed.
    assert transport.cancel_count == 1, (
        f"Expected cancel_count==1 (only baseline voided), "
        f"got {transport.cancel_count}. "
        f"If this fails, the forward rollback may have been implemented."
    )

    # co_orig WAS cancelled (cycle-0 step 4 worked)
    assert transport.status_by_checkout.get("co_orig") == "cancelled", (
        f"co_orig should be cancelled (cycle-0 step 4), "
        f"got {transport.status_by_checkout.get('co_orig')!r}"
    )

    # co_recovery_0 is still LIVE (the unfixed gap)
    assert transport.status_by_checkout.get("co_recovery_0") == "complete", (
        f"co_recovery_0 expected 'complete' (not voided — the gap), "
        f"got {transport.status_by_checkout.get('co_recovery_0')!r}"
    )

    # There IS a live booking (the gap leaves one stranded)
    live = transport.live_bookings()
    assert len(live) == 1 and "co_recovery_0" in live, (
        f"Expected exactly co_recovery_0 live (stranded), got live={live}"
    )


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(pytest.main([__file__, "-v", "-p", "no:xdist", "-p", "no:cacheprovider"]))
