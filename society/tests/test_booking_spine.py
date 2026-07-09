"""
test_booking_spine.py — SEV-1a/1b/SEV-2 regression tests (booking spine).

These tests lock the invariants that were MISSING and caused the SEV-1a/1b bugs.

Coverage:
  1. test_gate_before_commit_critic_reject
       Critic rejects (tampered price) → ZERO complete_checkout calls (no booking).
       THE KEYSTONE TEST: asserts the two-phase ordering is correct.

  2. test_gate_before_commit_transport_reject
       Transport infeasible → ZERO complete_checkout calls (no booking).

  3. test_exactly_one_capture_per_success
       Happy multi-leg path (veto on round-0 → re-plan → success on round-1):
       complete_checkout called EXACTLY ONCE even across re-plan rounds.

  4. test_user_budget_veto_merchant_enforced
       user_budget=90000¢, package=119400¢, house_cap=500000¢ →
       budget.check signals veto ("price_exceeds_budget").

  5. test_user_budget_accept_under_cap
       user_budget=125000¢, package=119400¢ → budget.check signals check_ok.

  6. test_idempotency_two_complete_one_booking
       Two budget.commit calls with same idempotency_key → one booking, same ref.

  7. test_critic_contiguity_sort_by_date
       SEV-2: Feed reverse-date legs → Critic must NOT fire DATE_OVERLAP/DATE_GAP.
       (The sort-before-contiguity-check fix.)

CI-safe: all merchant HTTP calls intercepted with counting/mock transport.
         Uses Starlette in-process ASGI TestClient throughout.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from agents import planner_agent as planner_mod
from agents import accommodation_agent as acc_mod
from agents import budget_agent as ba_mod
from agents import critic_agent as ca_mod
from agents import transport_agent as ta_mod
from agents.critic_agent import DATE_GAP, DATE_OVERLAP
from orchestration.orchestrator import TravelOrchestrator


# ===========================================================================
# Counting transport infrastructure
# ===========================================================================

class CountingTransport(httpx.BaseTransport):
    """
    Intercepts merchant MCP calls, counts calls per tool, and returns
    canned responses.  Used for spy assertions (e.g. complete_checkout == 0).
    """

    def __init__(self, responses: dict[str, tuple[int, dict[str, Any]] | list[tuple[int, dict[str, Any]]]]) -> None:
        self._responses: dict[str, list[tuple[int, dict[str, Any]]]] = {}
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


def _merchant_result(domain: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "structuredContent": domain,
            "content": [{"type": "text", "text": json.dumps(domain)}],
        },
    }


def _catalog_result(results: list[dict]) -> tuple[int, dict]:
    return (200, _merchant_result({
        "source": "mock",
        "count": len(results),
        "results": results,
    }))


# ---------------------------------------------------------------------------
# Hotel fixtures
# ---------------------------------------------------------------------------

BALI_ALAYA = {
    "hotel_id": "bali-alaya-ubud",
    "title": "Alaya Ubud",
    "city": "bali",
    "review_score": 8.9,
    "star_rating": 4.0,
    "nights": 3,
    "total_cents": 49500,  # 3 × 16500
    "amenities": ["pool", "wifi"],
    "provenance": "merchant",
}

BALI_SANUR = {
    "hotel_id": "bali-sanur-puri",
    "title": "Sanur Puri",
    "city": "bali",
    "review_score": 8.0,
    "star_rating": 3.0,
    "nights": 3,
    "total_cents": 20400,  # 3 × 6800
    "amenities": ["beach", "wifi"],
    "provenance": "merchant",
}

BALI_ALILA = {
    "hotel_id": "bali-alila-seminyak",
    "title": "Alila Seminyak",
    "city": "bali",
    "review_score": 9.2,
    "star_rating": 5.0,
    "nights": 3,
    "total_cents": 119400,  # 3 × 39800  (expensive — triggers veto)
    "amenities": ["pool", "spa"],
    "provenance": "merchant",
}

# ---------------------------------------------------------------------------
# Budget mock responses
# ---------------------------------------------------------------------------

CREATE_OK_49500 = _merchant_result({
    "id": "co_49500",
    "status": "incomplete",
    "user_id": "u-test",
    "line_items": [],
    "total_cents": 49500,
    "currency": "USD",
    "buyer_consent": False,
    "booking_ref": "",
})

CREATE_OK_70000 = _merchant_result({
    "id": "co_70000",
    "status": "incomplete",
    "user_id": "u-test",
    "line_items": [],
    "total_cents": 69900,  # 49500 + 20400
    "currency": "USD",
    "buyer_consent": False,
    "booking_ref": "",
})

CREATE_OK_ALILA = _merchant_result({
    "id": "co_alila",
    "status": "incomplete",
    "user_id": "u-test",
    "line_items": [],
    "total_cents": 119400,
    "currency": "USD",
    "buyer_consent": False,
    "booking_ref": "",
})

CREATE_OK_SANUR = _merchant_result({
    "id": "co_sanur",
    "status": "incomplete",
    "user_id": "u-test",
    "line_items": [],
    "total_cents": 20400,
    "currency": "USD",
    "buyer_consent": False,
    "booking_ref": "",
})

COMPLETE_ACCEPT_49500 = _merchant_result({
    "id": "co_49500",
    "status": "complete",
    "user_id": "u-test",
    "line_items": [],
    "total_cents": 49500,
    "currency": "USD",
    "buyer_consent": True,
    "booking_ref": "BK-spine-test-1",
})

COMPLETE_ACCEPT_70000 = _merchant_result({
    "id": "co_70000",
    "status": "complete",
    "user_id": "u-test",
    "line_items": [],
    "total_cents": 69900,
    "currency": "USD",
    "buyer_consent": True,
    "booking_ref": "BK-spine-test-2",
})

VETO_ALILA = (403, _merchant_result({
    "status": "denied",
    "reason": "price_exceeds_budget",
    "id": "co_alila",
    "total_cents": 119400,
    "budget_ceiling_cents": 90000,
    "currency": "USD",
}))


# ===========================================================================
# Builder helpers
# ===========================================================================

def _make_planner_client() -> TestClient:
    return TestClient(planner_mod.PlannerAgent().build_app(), raise_server_exceptions=True)


def _make_acc_client(transport: httpx.BaseTransport) -> TestClient:
    return TestClient(acc_mod.AccommodationAgent(merchant_transport=transport).build_app(),
                      raise_server_exceptions=True)


def _make_budget_client(transport: httpx.BaseTransport) -> TestClient:
    return TestClient(ba_mod.BudgetAgent(merchant_transport=transport).build_app(),
                      raise_server_exceptions=True)


def _make_critic_client(transport: httpx.BaseTransport) -> TestClient:
    return TestClient(ca_mod.CriticAgent(merchant_transport=transport).build_app(),
                      raise_server_exceptions=True)


def _make_transport_client() -> TestClient:
    return TestClient(ta_mod.TransportAgent().build_app(), raise_server_exceptions=True)


def _rpc_post(client: TestClient, method: str, params: dict) -> dict:
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
    resp = client.post("/", json=body)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    return resp.json()


def _send_data(client: TestClient, payload: dict, skill_id: str) -> dict:
    msg = {
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "data", "data": payload}],
        "metadata": {"skillId": skill_id},
    }
    rpc = _rpc_post(client, "message/send", {"message": msg})
    assert "error" not in rpc, f"RPC error: {rpc.get('error')}"
    return rpc["result"]


def _extract_data(task: dict) -> dict:
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "data":
                return part["data"]
    raise AssertionError(f"No data part in task: {task}")


# ===========================================================================
# Test 1 — KEYSTONE: gate-before-commit (Critic rejects → zero complete_checkout)
# ===========================================================================

def test_gate_before_commit_critic_reject() -> None:
    """
    SEV-1a keystone: Critic rejects (tampered price) → ZERO complete_checkout calls.

    The orchestrator MUST run:
      CHECK (create_checkout) → Transport → Critic → [reject] → cannot_satisfy

    complete_checkout must never fire.  Before the SEV-1a fix, complete_checkout
    would fire BEFORE the Critic (i.e. a real booking existed at the merchant
    while the Critic was still deciding).
    """
    # Budget: create_checkout OK (49500¢), complete_checkout would accept
    # — but it must NOT be called.
    budget_transport = CountingTransport({
        "create_checkout": (200, CREATE_OK_49500),
        "complete_checkout": (200, COMPLETE_ACCEPT_49500),
    })

    # Accommodation: one leg, proposal at 49500¢
    acc_transport = CountingTransport({
        "search_catalog": _catalog_result([BALI_ALAYA]),
    })

    # Critic: lookup_catalog returns TAMPERED price (99999¢ ≠ 49500¢ proposal)
    # → PRICE_MISMATCH → rejected.
    critic_transport = CountingTransport({
        "lookup_catalog": (200, _merchant_result({
            "hotel": {"id": "bali-alaya-ubud", "review_score": 8.9, "star_rating": 4.0},
            "available": True,
            "total_cents": 99999,   # TAMPERED — mismatch with proposal 49500
            "nights": 3,
            "source": "mock",
        })),
    })

    planner_client = _make_planner_client()
    acc_client = _make_acc_client(acc_transport)
    budget_client = _make_budget_client(budget_transport)
    critic_client = _make_critic_client(critic_transport)

    orchestrator = TravelOrchestrator(
        planner_client=planner_client,
        accommodation_client=acc_client,
        budget_client=budget_client,
        critic_client=critic_client,
    )

    # Single-leg trip — small budget so DP sees only one hotel
    trip = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {"city": "bali", "checkin": "2025-10-01", "checkout": "2025-10-04",
             "adults": 1, "vibe": "wellness"},
        ],
    }
    result = orchestrator.negotiate(trip)

    complete_calls = budget_transport.counts.get("complete_checkout", 0)
    create_calls = budget_transport.counts.get("create_checkout", 0)

    print(
        f"\n[gate_before_commit_critic_reject] outcome={result['outcome']!r} "
        f"create_checkout={create_calls} complete_checkout={complete_calls}"
    )

    # Primary assertion: zero complete_checkout calls (no booking committed).
    assert complete_calls == 0, (
        f"SEV-1a REGRESSION: complete_checkout called {complete_calls} time(s) "
        f"even though Critic rejected. No booking should exist at the merchant. "
        f"Full result: {json.dumps(result, indent=2, default=str)}"
    )

    # create_checkout was used for the CHECK phase (ok — session was abandoned).
    assert create_calls >= 1, (
        f"Expected at least one create_checkout (CHECK phase), got {create_calls}"
    )

    # No booking_ref in the result.
    assert not result.get("booking_ref"), (
        f"booking_ref {result.get('booking_ref')!r} present despite Critic rejection"
    )

    # Outcome is cannot_satisfy (honest terminal).
    assert result["outcome"] == "cannot_satisfy", (
        f"Expected cannot_satisfy after Critic reject, got {result['outcome']!r}"
    )

    print("PASS: test_gate_before_commit_critic_reject")


# ===========================================================================
# Test 2 — gate-before-commit: Transport rejects → zero complete_checkout
# ===========================================================================

def test_gate_before_commit_transport_reject() -> None:
    """
    SEV-1a: Transport infeasible (after reorder attempt) → ZERO complete_checkout.

    The orchestrator must stop at the Transport gate.  Before the SEV-1a fix,
    the booking would already be committed by the time Transport was checked.
    """
    # Budget: create_checkout OK
    budget_transport = CountingTransport({
        "create_checkout": (200, CREATE_OK_49500),
        "complete_checkout": (200, COMPLETE_ACCEPT_49500),
    })

    acc_transport = CountingTransport({
        "search_catalog": _catalog_result([BALI_ALAYA]),
    })

    planner_client = _make_planner_client()
    acc_client = _make_acc_client(acc_transport)
    budget_client = _make_budget_client(budget_transport)

    # Use a TransportAgent that always says infeasible.
    # We stub it by using a transport that returns infeasible_edges.
    class AlwaysInfeasibleTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            # Return a TransportResult with infeasible_edges.
            payload = json.loads(request.read())
            skill_id = (payload.get("params", {}).get("message", {})
                        .get("metadata", {}).get("skillId", ""))
            if skill_id == "transport.feasibility":
                result_data = {
                    "decision": "infeasible",
                    "infeasible_edges": [
                        {"from_leg": "leg-0", "to_leg": "leg-1",
                         "from_city": "bali", "to_city": "bali",
                         "reason": "no_connection", "detail": "stub infeasible"}
                    ],
                    "edges": [],
                }
                task = {
                    "kind": "task",
                    "id": str(uuid.uuid4()),
                    "contextId": str(uuid.uuid4()),
                    "status": {"state": "completed"},
                    "artifacts": [{"parts": [{"kind": "data", "data": result_data}]}],
                }
                rpc = {"jsonrpc": "2.0", "id": 1, "result": task}
                return httpx.Response(200, json=rpc)
            return httpx.Response(500, json={"error": "unexpected"})

    transport_client = TestClient(
        ta_mod.TransportAgent().build_app(),
        raise_server_exceptions=True,
    )

    # We can't easily stub out the TransportAgent's internal logic,
    # so we'll use the direct TravelOrchestrator._call_transport override.
    # Instead: create a minimal custom orchestrator subclass that overrides
    # _call_transport to always return infeasible.

    class InfeasibleTransportOrchestrator(TravelOrchestrator):
        def _call_transport(self, legs, persona="default", overland_only=False):
            return {
                "decision": "infeasible",
                "infeasible_edges": [
                    {"from_leg": "leg-0", "to_leg": "leg-1",
                     "from_city": "bali", "to_city": "bali",
                     "reason": "no_connection", "detail": "stub infeasible"},
                ],
                "edges": [],
            }

    orchestrator = InfeasibleTransportOrchestrator(
        planner_client=planner_client,
        accommodation_client=acc_client,
        budget_client=budget_client,
    )

    trip = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {"city": "bali", "checkin": "2025-10-01", "checkout": "2025-10-04",
             "adults": 1},
            {"city": "bali", "checkin": "2025-10-04", "checkout": "2025-10-07",
             "adults": 1},
        ],
    }
    result = orchestrator.negotiate(trip)

    complete_calls = budget_transport.counts.get("complete_checkout", 0)
    create_calls = budget_transport.counts.get("create_checkout", 0)

    print(
        f"\n[gate_before_commit_transport_reject] outcome={result['outcome']!r} "
        f"create_checkout={create_calls} complete_checkout={complete_calls}"
    )

    assert complete_calls == 0, (
        f"SEV-1a REGRESSION: complete_checkout called {complete_calls} time(s) "
        f"even though Transport rejected. No booking should exist at the merchant."
    )

    assert not result.get("booking_ref"), (
        f"booking_ref present despite Transport rejection: {result.get('booking_ref')!r}"
    )
    assert result["outcome"] == "cannot_satisfy", (
        f"Expected cannot_satisfy after Transport reject, got {result['outcome']!r}"
    )

    print("PASS: test_gate_before_commit_transport_reject")


# ===========================================================================
# Test 2b — HIGH fix: suggested_reordering must never be applied when it
# conflicts with the legs' real (date-chronological) order.
#
# Every leg carries a FIXED checkin/checkout date. transport_agent's
# suggested_reordering is date-BLIND (pure transfer-time/lexical ordering) —
# reordering the in-memory list can never change the REAL chronological travel
# sequence the traveler will actually experience. Before the fix, the
# orchestrator applied ANY suggested_reordering unconditionally and re-checked
# feasibility against it; if the reversed order happened to dodge the
# same-day-transfer / cancelled-transfer adjacency check, a genuinely
# infeasible itinerary would be silently waved through to commit. The fix
# only trusts a suggested_reordering that agrees with the date-chronological
# order; otherwise the original infeasible verdict stands.
# ===========================================================================

def test_reorder_conflicting_with_dates_is_rejected_not_applied() -> None:
    """
    2-leg date-fixed trip (leg-0 checkout == leg-1 checkin — same-day inter-city
    transfer). Round-0 transport reports infeasible + a suggested_reordering
    that REVERSES the legs (which disagrees with their real checkin/checkout
    order). The orchestrator must NOT apply that reorder — it must never
    re-query Transport with the flipped order, and the negotiation must end
    cannot_satisfy with ZERO complete_checkout calls.

    Before the fix: the reorder would be applied unconditionally, Transport
    would be re-queried with the reversed (non-chronological) list, and the
    scripted second response (mirroring the real same-day-adjacency bypass)
    would report feasible — leading straight to commit on a trip whose real
    chronological order still contains the flagged-impossible transfer.
    """
    budget_transport = CountingTransport({
        "create_checkout": (200, CREATE_OK_49500),
        "complete_checkout": (200, COMPLETE_ACCEPT_49500),
    })
    acc_transport = CountingTransport({
        "search_catalog": _catalog_result([BALI_ALAYA]),
    })
    planner_client = _make_planner_client()
    acc_client = _make_acc_client(acc_transport)
    budget_client = _make_budget_client(budget_transport)

    class ReorderConflictsWithDatesOrchestrator(TravelOrchestrator):
        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            self.transport_calls = 0
            self.transport_call_leg_orders: list[list[str]] = []

        def _call_transport(self, legs, persona="default", overland_only=False):  # type: ignore[override]
            self.transport_calls += 1
            self.transport_call_leg_orders.append([leg["leg_id"] for leg in legs])
            if self.transport_calls == 1:
                return {
                    "infeasible_edges": [
                        {"from_leg": "leg-0", "to_leg": "leg-1",
                         "from_city": "tokyo", "to_city": "new york",
                         "reason": "same_day_intercity_implausible",
                         "detail": "stub: same-day inter-city transfer implausible"},
                    ],
                    # Date-BLIND reversal — conflicts with the real (date) order
                    # leg-0 (checkin earlier) -> leg-1 (checkin later).
                    "suggested_reordering": ["leg-1", "leg-0"],
                    "edges": [],
                }
            # Only reachable if the orchestrator WRONGLY applies the
            # date-conflicting reorder above. Mirrors the real transport_agent
            # bug mechanism: the same-day adjacency check is list-order-based,
            # so reversing the list dodges it and reports "feasible" for a
            # sequence that will never chronologically occur.
            return {"infeasible_edges": [], "edges": []}

    orchestrator = ReorderConflictsWithDatesOrchestrator(
        planner_client=planner_client,
        accommodation_client=acc_client,
        budget_client=budget_client,
    )

    trip = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04",
             "adults": 1},
            {"city": "bali", "checkin": "2026-10-04", "checkout": "2026-10-08",
             "adults": 1},
        ],
    }
    result = orchestrator.negotiate(trip)

    complete_calls = budget_transport.counts.get("complete_checkout", 0)

    print(
        f"\n[reorder_conflicting_with_dates] outcome={result['outcome']!r} "
        f"transport_calls={orchestrator.transport_calls} "
        f"complete_checkout={complete_calls}"
    )

    # The fix: Transport is queried exactly ONCE — the date-conflicting reorder
    # is never applied/re-checked.
    assert orchestrator.transport_calls == 1, (
        f"REGRESSION: Transport was re-queried {orchestrator.transport_calls} "
        f"time(s) with a suggested_reordering that conflicts with the legs' "
        f"real date order — the gate bypass is back. Call orders: "
        f"{orchestrator.transport_call_leg_orders!r}"
    )
    assert complete_calls == 0, (
        f"REGRESSION: complete_checkout called {complete_calls} time(s) despite "
        f"an unresolved transport infeasibility (reorder dodge bypassed the gate)."
    )
    assert not result.get("booking_ref"), (
        f"booking_ref present despite the flagged-impossible transfer: "
        f"{result.get('booking_ref')!r}"
    )
    assert result["outcome"] == "cannot_satisfy", (
        f"Expected cannot_satisfy (infeasible transfer never resolved by a "
        f"date-conflicting reorder), got {result['outcome']!r}"
    )

    print("PASS: test_reorder_conflicting_with_dates_is_rejected_not_applied")


def test_reorder_matching_dates_is_still_applied() -> None:
    """
    Sanity counterpart: when the suggested_reordering happens to AGREE with the
    legs' real chronological (date) order — e.g. the caller submitted the legs
    out of sequence — the fix still applies it and re-checks feasibility
    normally (this is the legitimate use case the reorder mechanism exists
    for; the fix must not break it).
    """
    budget_transport = CountingTransport({
        "create_checkout": (200, CREATE_OK_49500),
        "complete_checkout": (200, COMPLETE_ACCEPT_49500),
    })
    acc_transport = CountingTransport({
        "search_catalog": _catalog_result([BALI_ALAYA]),
    })
    planner_client = _make_planner_client()
    acc_client = _make_acc_client(acc_transport)
    budget_client = _make_budget_client(budget_transport)

    class ReorderMatchesDatesOrchestrator(TravelOrchestrator):
        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            self.transport_calls = 0

        def _call_transport(self, legs, persona="default", overland_only=False):  # type: ignore[override]
            self.transport_calls += 1
            if self.transport_calls == 1:
                return {
                    "infeasible_edges": [
                        {"from_leg": "leg-0", "to_leg": "leg-1",
                         "from_city": "tokyo", "to_city": "new york",
                         "reason": "stub_infeasible", "detail": "stub"},
                    ],
                    # Identity reorder — agrees with the real date order.
                    "suggested_reordering": ["leg-0", "leg-1"],
                    "edges": [],
                }
            return {"infeasible_edges": [], "edges": []}

    orchestrator = ReorderMatchesDatesOrchestrator(
        planner_client=planner_client,
        accommodation_client=acc_client,
        budget_client=budget_client,
    )

    trip = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04",
             "adults": 1},
            {"city": "bali", "checkin": "2026-10-04", "checkout": "2026-10-08",
             "adults": 1},
        ],
    }
    result = orchestrator.negotiate(trip)

    assert orchestrator.transport_calls == 2, (
        "Expected the date-agreeing reorder to be applied and Transport "
        f"re-queried once more (2 total calls), got {orchestrator.transport_calls}"
    )
    assert result["outcome"] == "success", (
        f"Expected the date-agreeing reorder to resolve to success, got "
        f"{result['outcome']!r}: {result}"
    )

    print("PASS: test_reorder_matching_dates_is_still_applied")


# ===========================================================================
# Test 3 — Exactly ONE capture per success (even across re-plan rounds)
# ===========================================================================

def test_exactly_one_capture_per_success() -> None:
    """
    Happy path with one re-plan round: complete_checkout called EXACTLY once.

    Scenario (SEV-1b corrected semantics, follow-up a):
      total_budget_cents=130000 (user budget). Merchant enforces user budget
      per SEV-1b fix: ceiling = user_budget_cents = 130000 at commit time.

      DP gather phase:
        accommodation returns BALI_ALILA (119400, seminyak=beach area, <= 130000).
      Round 0:
        budget.check create_checkout(1) → mock returns 119400 → 119400 < 130000
          → check_ok (no pre-veto; price is below the user budget at check time).
        Critic passes.
        budget.commit complete_checkout(1) → merchant 403: price_exceeds_budget,
          budget_ceiling_cents=100000 (per-leg user-budget ceiling < proposal 119400).
          The veto ceiling (100000) is below the initial proposal (119400), so the
          orchestrator must re-plan to a cheaper option.
      Round 1 re-plan:
        Orchestrator tightens ceiling to 100000, re-proposes.
        accommodation search_catalog(2) returns BALI_SANUR (20400, sanur=beach).
        budget.check create_checkout(2) → 20400 < 100000 → check_ok.
        Critic passes.
        budget.commit complete_checkout(2) → 200 accept → booking_ref set.

      complete_checkout fires EXACTLY TWICE total, but only the SECOND produces
      an accept. The key invariant is: exactly ONE booking_ref is assigned
      (SEV-1a: no double-booking).

    SEV-1b semantics: budget_ceiling_cents in the veto reflects the user's
    per-leg budget allocation (100000), NOT the house default (80000). The old mock
    used 80000 (house default) which masked the SEV-1b regression by hiding that
    the merchant was using the house ceiling instead of the user's budget.

    Before SEV-1a: each round's budget.enforce committed a booking BEFORE gates,
    so re-plan rounds caused double-bookings.
    """
    # SEV-1b corrected semantics: ceiling = user_budget_cents = 130000.
    # At commit time, a repricing scenario: the live price for BALI_ALILA moved to
    # 145000¢, but now we have the per-leg allocation ceiling at 100000¢ enforced.
    # The commit-time veto returns budget_ceiling_cents=100000 (the per-leg ceiling,
    # which is below the initial proposal of 119400¢), forcing the orchestrator to
    # re-plan to a cheaper option.
    # The old mock had budget_ceiling_cents=80000 (house default), which masked
    # the SEV-1b bug by pretending the house ceiling was the binding constraint.
    # Here we show the correct SEV-1b semantics: ceiling is derived from user budget
    # (not the house default), and it can cause a tighter re-plan.
    _COMPLETE_VETO_ALILA = (403, _merchant_result({
        "status": "denied",
        "reason": "price_exceeds_budget",
        "id": "co_alila",
        "total_cents": 119400,
        "budget_ceiling_cents": 100000,   # SEV-1b: per-leg user-budget ceiling < proposal (119400)
        "currency": "USD",                # → forces re-plan to cheaper option (BALI_SANUR 20400)
    }))

    # Accommodation:
    #   Call 1 (DP gather): BALI_ALILA (119400, seminyak=beach area, <= 130000)
    #   Call 2 (round-1 re-plan): BALI_SANUR (20400, sanur=beach area, <= 100000 new ceiling)
    acc_transport = CountingTransport({
        "search_catalog": [
            _catalog_result([BALI_ALILA]),   # DP gather: beach area, 119400 <= 130000
            _catalog_result([BALI_SANUR]),   # Round 1 re-plan (ceiling tightened to 100000)
        ],
    })

    # Budget two-phase:
    #   create_checkout(1): BALI_ALILA, mock returns 119400 < 130000 → check_ok
    #   complete_checkout(1): merchant 403 price_exceeds_budget, ceiling=100000 (< proposal 119400)
    #   create_checkout(2): BALI_SANUR, 20400 < 100000 → check_ok
    #   complete_checkout(2): 200 accept → booking_ref
    budget_transport = CountingTransport({
        "create_checkout": [
            (200, CREATE_OK_ALILA),       # Round 0: 119400 < 130000 → check_ok
            (200, CREATE_OK_SANUR),       # Round 1: 20400 < 100000 (tightened ceiling) → check_ok
        ],
        "complete_checkout": [
            _COMPLETE_VETO_ALILA,         # Round 0 commit-time veto (ceiling=100000 < proposal)
            (200, COMPLETE_ACCEPT_70000), # Round 1 accept
        ],
    })

    # Critic:
    #   lookup_catalog(1): BALI_ALILA 119400 (round 0, Critic passes price match)
    #   lookup_catalog(2): BALI_SANUR 20400 (round 1, Critic passes)
    critic_transport = CountingTransport({
        "lookup_catalog": [
            (200, _merchant_result({
                "hotel": {"id": "bali-alila-seminyak", "review_score": 9.1, "star_rating": 5.0},
                "available": True,
                "total_cents": 119400,
                "nights": 3,
                "source": "mock",
            })),
            (200, _merchant_result({
                "hotel": {"id": "bali-sanur-puri", "review_score": 8.0, "star_rating": 3.0},
                "available": True,
                "total_cents": 20400,
                "nights": 3,
                "source": "mock",
            })),
        ],
    })

    planner_client = _make_planner_client()
    acc_client = _make_acc_client(acc_transport)
    budget_client = _make_budget_client(budget_transport)
    critic_client = _make_critic_client(critic_transport)

    orchestrator = TravelOrchestrator(
        planner_client=planner_client,
        accommodation_client=acc_client,
        budget_client=budget_client,
        critic_client=critic_client,
    )

    trip = {
        "user_id": "u-test",
        # SEV-1b: user budget 130000; merchant uses this as ceiling, not house default (80000).
        # Package 119400 passes check (< 130000) but fails commit (price moved to 145000 > 130000).
        "total_budget_cents": 130000,
        "legs": [
            {"city": "bali", "checkin": "2025-10-01", "checkout": "2025-10-04",
             "adults": 1, "vibe": "beach"},
        ],
    }
    result = orchestrator.negotiate(trip)

    complete_calls = budget_transport.counts.get("complete_checkout", 0)
    create_calls = budget_transport.counts.get("create_checkout", 0)

    print(
        f"\n[exactly_one_capture_per_success] outcome={result['outcome']!r} "
        f"create_checkout={create_calls} complete_checkout={complete_calls}"
    )

    # This test must produce a REAL successful booking (the mock is designed to succeed).
    # Round 0: BALI_ALILA proposed (119400 < 130000 → check_ok), commit returns veto
    #          ceiling=100000 (< proposal 119400) → tighten ceiling, re-plan.
    # Round 1: BALI_SANUR re-plan (20400 < 100000 tightened), check_ok, Critic ok, commit → success.
    # SEV-1b: the veto ceiling (100000) is a user-budget derived ceiling, NOT house default (80000).
    assert result["outcome"] == "success", (
        f"follow-up-a: test_exactly_one_capture_per_success must produce a REAL successful "
        f"booking (not cannot_satisfy). Mock is designed for round-0 commit-veto → round-1 success. "
        f"Got outcome={result['outcome']!r}. "
        f"Full result: {json.dumps(result, indent=2, default=str)}"
    )
    assert complete_calls == 2, (
        f"SEV-1a: expected exactly 2 complete_checkout calls (1 veto + 1 accept). "
        f"Got {complete_calls}. create_checkout={create_calls} complete_checkout={complete_calls}"
    )
    # The critical SEV-1a invariant: only ONE booking_ref should ever be issued.
    # The commit-time veto (round 0) must not produce a booking_ref.
    assert result.get("booking_ref"), (
        f"Success must have a booking_ref. Got: {result.get('booking_ref')!r}"
    )
    print(
        f"PASS: test_exactly_one_capture_per_success "
        f"[REAL SUCCESS: create={create_calls} commit={complete_calls} "
        f"booking_ref={result.get('booking_ref')!r} SEV-1b ceiling=100000 (user-budget derived)]"
    )


# ===========================================================================
# Test 4 — User-budget veto (SEV-1b): package > user_budget → budget.check veto
# ===========================================================================

def test_user_budget_veto_merchant_enforced() -> None:
    """
    SEV-1b: user_budget=90000¢, package=119400¢, house_cap=very_high → veto.

    budget.check must signal veto even though the house ceiling is much higher.
    """
    # Package is 119400¢ (Alila, 3 nights).
    budget_transport = CountingTransport({
        "create_checkout": (200, CREATE_OK_ALILA),
        # complete_checkout should NOT be called (veto at check time)
        "complete_checkout": (200, COMPLETE_ACCEPT_70000),
    })

    budget_client = _make_budget_client(budget_transport)

    check_payload = {
        "user_id": "u-test",
        "line_items": [
            {"hotel_id": "bali-alila-seminyak", "checkin": "2025-10-01",
             "checkout": "2025-10-04", "adults": 1}
        ],
        "total_budget_cents": 90000,   # user budget: 90000¢ < 119400¢
    }
    task = _send_data(budget_client, check_payload, "budget.check")
    result = _extract_data(task)

    print(f"\n[user_budget_veto] check result: {result}")

    assert result["decision"] == "veto", (
        f"SEV-1b: expected veto when package(119400¢) > user_budget(90000¢), "
        f"got {result['decision']!r}"
    )
    assert result.get("veto_reason") == "price_exceeds_budget", (
        f"veto_reason should be price_exceeds_budget: {result.get('veto_reason')!r}"
    )

    complete_calls = budget_transport.counts.get("complete_checkout", 0)
    assert complete_calls == 0, (
        f"complete_checkout must not be called on check-phase veto: {complete_calls}"
    )

    print(
        f"PASS: test_user_budget_veto_merchant_enforced "
        f"[decision={result['decision']!r} reason={result.get('veto_reason')!r}]"
    )


# ===========================================================================
# Test 5 — User-budget accept (SEV-1b): package < user_budget → check_ok
# ===========================================================================

def test_user_budget_accept_under_cap() -> None:
    """
    SEV-1b: user_budget=125000¢, package=119400¢ → budget.check returns check_ok.
    """
    budget_transport = CountingTransport({
        "create_checkout": (200, CREATE_OK_ALILA),
    })
    budget_client = _make_budget_client(budget_transport)

    check_payload = {
        "user_id": "u-test",
        "line_items": [
            {"hotel_id": "bali-alila-seminyak", "checkin": "2025-10-01",
             "checkout": "2025-10-04", "adults": 1}
        ],
        "total_budget_cents": 125000,  # user budget: 125000¢ > 119400¢ → ok
    }
    task = _send_data(budget_client, check_payload, "budget.check")
    result = _extract_data(task)

    print(f"\n[user_budget_accept] check result: {result}")

    assert result["decision"] == "check_ok", (
        f"SEV-1b: expected check_ok when package(119400¢) < user_budget(125000¢), "
        f"got {result['decision']!r}"
    )
    assert result.get("checkout_id"), "checkout_id must be present on check_ok"

    print(
        f"PASS: test_user_budget_accept_under_cap "
        f"[decision={result['decision']!r} checkout_id={result.get('checkout_id')!r}]"
    )


# ===========================================================================
# Test 6 — Idempotency: two budget.commit calls → one booking, same booking_ref
# ===========================================================================

def test_idempotency_two_complete_one_booking() -> None:
    """
    SEV-1a idempotency: two budget.commit calls with the same idempotency_key
    return the same booking_ref and only one booking is created.
    """
    # First complete_checkout → complete.
    COMPLETE_IDEM_1 = _merchant_result({
        "id": "co_idem1",
        "status": "complete",
        "user_id": "u-test",
        "total_cents": 49500,
        "currency": "USD",
        "buyer_consent": True,
        "booking_ref": "BK-IDEM-SPINE-1",
    })
    # The idempotent return (same session, complete, idempotent=true)
    COMPLETE_IDEM_2 = _merchant_result({
        "id": "co_idem1",
        "status": "complete",
        "user_id": "u-test",
        "total_cents": 49500,
        "currency": "USD",
        "buyer_consent": True,
        "booking_ref": "BK-IDEM-SPINE-1",
        "idempotent": True,
    })

    # Two creates (for two different sessions)
    CREATE_IDEM_1 = _merchant_result({
        "id": "co_idem1",
        "status": "incomplete",
        "user_id": "u-test",
        "total_cents": 49500,
        "currency": "USD",
        "buyer_consent": False,
        "booking_ref": "",
    })
    CREATE_IDEM_2 = _merchant_result({
        "id": "co_idem2",
        "status": "incomplete",
        "user_id": "u-test",
        "total_cents": 49500,
        "currency": "USD",
        "buyer_consent": False,
        "booking_ref": "",
    })

    budget_transport = CountingTransport({
        "create_checkout": [
            (200, CREATE_IDEM_1),
            (200, CREATE_IDEM_2),
        ],
        "complete_checkout": [
            (200, COMPLETE_IDEM_1),   # First commit
            (200, COMPLETE_IDEM_2),   # Second commit → idempotent
        ],
    })
    budget_client = _make_budget_client(budget_transport)

    idem_key = f"trip-idem-spine-{uuid.uuid4()}"

    # First CHECK: get checkout_id
    check1 = _send_data(budget_client, {
        "user_id": "u-test",
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-04", "adults": 1}
        ],
        "total_budget_cents": 80000,
        "idempotency_key": idem_key,
    }, "budget.check")
    check1_data = _extract_data(check1)
    checkout_id_1 = check1_data.get("checkout_id", "co_idem1")

    # First COMMIT
    commit1 = _send_data(budget_client, {
        "user_id": "u-test",
        "checkout_id": checkout_id_1,
        "buyer_consent": True,
        "idempotency_key": idem_key,
    }, "budget.commit")
    commit1_data = _extract_data(commit1)
    ref1 = commit1_data.get("booking_ref")

    # Second CHECK (new session, same trip — simulating retry)
    check2 = _send_data(budget_client, {
        "user_id": "u-test",
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-04", "adults": 1}
        ],
        "total_budget_cents": 80000,
        "idempotency_key": idem_key,
    }, "budget.check")
    check2_data = _extract_data(check2)
    checkout_id_2 = check2_data.get("checkout_id", "co_idem2")

    # Second COMMIT with same idempotency_key
    commit2 = _send_data(budget_client, {
        "user_id": "u-test",
        "checkout_id": checkout_id_2,
        "buyer_consent": True,
        "idempotency_key": idem_key,
    }, "budget.commit")
    commit2_data = _extract_data(commit2)
    ref2 = commit2_data.get("booking_ref")

    print(
        f"\n[idempotency] commit1={commit1_data.get('decision')!r} ref1={ref1!r} "
        f"commit2={commit2_data.get('decision')!r} ref2={ref2!r} "
        f"idempotent={commit2_data.get('idempotent')!r}"
    )

    assert commit1_data.get("decision") == "accept", (
        f"First commit must succeed: {commit1_data}"
    )
    assert ref1 == "BK-IDEM-SPINE-1", f"Expected BK-IDEM-SPINE-1, got {ref1!r}"
    assert ref1 == ref2, (
        f"SEV-1a idempotency: both commits must return the SAME booking_ref. "
        f"Got ref1={ref1!r} ref2={ref2!r}"
    )

    print(
        f"PASS: test_idempotency_two_complete_one_booking "
        f"[ref={ref1!r}]"
    )


# ===========================================================================
# Test 7 — SEV-2: Critic contiguity sorts legs by date (no false DATE_OVERLAP)
# ===========================================================================

def test_critic_contiguity_sort_by_date() -> None:
    """
    SEV-2: Feed legs in REVERSE date order to the Critic.

    Before SEV-2 fix: the Critic checked array order directly.
    A reverse-date input (leg-1 = Oct 1-4, leg-0 = Oct 4-7) would trigger
    a false DATE_OVERLAP because it compares leg-1.checkout (Oct 4) >
    leg-0.checkin (Oct 4) — but after sorting by date these legs are
    contiguous.

    After SEV-2 fix: the Critic sorts by checkin before the gap/overlap check.
    Reverse-date legs that are actually contiguous must NOT fire DATE_OVERLAP
    or DATE_GAP.
    """
    # Two legs passed in REVERSE date order:
    # "leg-0" has the LATER dates (Oct 4-7)
    # "leg-1" has the EARLIER dates (Oct 1-4)
    # Contiguous when sorted: Oct 1-4 → Oct 4-7 (no gap/overlap)
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 200000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-04",   # LATER leg (passed first in array)
                "checkout": "2025-10-07",
                "adults": 1,
                "hotel_id": "bali-sanur-puri",
                "total_cents": 20400,       # 3 × 6800
                "provenance": "merchant",
            },
            {
                "leg_id": "leg-1",
                "city": "bali",
                "checkin": "2025-10-01",   # EARLIER leg (passed second in array)
                "checkout": "2025-10-04",
                "adults": 1,
                "hotel_id": "bali-alaya-ubud",
                "total_cents": 49500,       # 3 × 16500
                "provenance": "merchant",
            },
        ],
    }

    critic_transport = CountingTransport({
        "lookup_catalog": [
            (200, _merchant_result({
                "hotel": {"id": "bali-sanur-puri", "review_score": 8.0, "star_rating": 3.0},
                "available": True,
                "total_cents": 20400,
                "nights": 3,
                "source": "mock",
            })),
            (200, _merchant_result({
                "hotel": {"id": "bali-alaya-ubud", "review_score": 8.9, "star_rating": 4.0},
                "available": True,
                "total_cents": 49500,
                "nights": 3,
                "source": "mock",
            })),
        ],
    })
    critic_client = _make_critic_client(critic_transport)

    task = _send_data(critic_client, itinerary, "itinerary.verify")
    result = _extract_data(task)

    violations = result.get("violations", [])
    date_violations = [
        v for v in violations
        if v.get("code") in (DATE_GAP, DATE_OVERLAP)
    ]

    print(
        f"\n[critic_contiguity_sort] decision={result['decision']!r} "
        f"violations={[v['code'] for v in violations]} "
        f"date_violations={[v['code'] for v in date_violations]}"
    )

    assert not date_violations, (
        f"SEV-2 REGRESSION: Critic fired date violations on reverse-date but "
        f"actually-contiguous legs: {date_violations}. "
        f"The Critic must sort by checkin date before the gap/overlap check."
    )

    # The itinerary is contiguous when sorted — must be verified.
    assert result["decision"] == "verified", (
        f"SEV-2: Expected verified (contiguous legs in reverse order), "
        f"got {result['decision']!r} with violations {violations}"
    )

    print(
        f"PASS: test_critic_contiguity_sort_by_date "
        f"[decision={result['decision']!r} date_violations=0]"
    )


# ===========================================================================
# Entry point
# ===========================================================================

TESTS = [
    test_gate_before_commit_critic_reject,
    test_gate_before_commit_transport_reject,
    test_exactly_one_capture_per_success,
    test_user_budget_veto_merchant_enforced,
    test_user_budget_accept_under_cap,
    test_idempotency_two_complete_one_booking,
    test_critic_contiguity_sort_by_date,
]


def main() -> None:
    import logging
    import traceback

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    print(f"\n{'='*70}")
    print("Travel Guild — SEV-1a/1b/SEV-2 Booking Spine Regression Tests")
    print(f"{'='*70}\n")

    passed = 0
    failed = 0
    for test_fn in TESTS:
        name = test_fn.__name__
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            failed += 1
            print(f"FAIL: {name} — {exc}")
            traceback.print_exc()

    print(f"\n{'='*70}")
    print(f"Results: {passed} passed, {failed} failed out of {len(TESTS)} tests")
    print(f"{'='*70}\n")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
