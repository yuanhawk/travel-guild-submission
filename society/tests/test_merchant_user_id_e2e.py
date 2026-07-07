"""test_merchant_user_id_e2e.py — #161 end-to-end proof that the canonical
merchant end-user identity genuinely reaches the Go merchant's request body,
for BOTH a real logged-in trip and an anonymous trip.

This is deliberately a LAYER ABOVE the existing #161 coverage:
  - test_ucp_signing.py unit-tests merchant_checkout_owner() in isolation.
  - test_budget_agent_cov3.py drives BudgetAgent directly with a hand-built
    payload and asserts the wire body carries checkout.user_id.
  - test_confirm_endpoint.py drives the HTTP /confirm route with a MOCKED
    orchestrator and asserts commit_plan(merchant_user_id=...) is called with
    the right value.

None of those exercises the REAL TravelOrchestrator (negotiate -> the
budget.check/commit dispatch funnel -> BudgetAgent -> the merchant HTTP wire)
end-to-end. This file does — with a capturing merchant transport that records
the actual `checkout.user_id` (and wallet `user_id`) bytes sent over the wire,
for:
  1. A real logged-in user (merchant_user_id == the verified user_id) —
     proves the identity is NOT silently dropped/skipped for real users.
  2. An anonymous user (merchant_user_id == the "anon:"+sha256 pseudo-id
     derived from owner_token) — proves the anon population gets a REAL,
     non-empty, non-secret-leaking identity, not a silent "" skip, and that
     it survives the plan (negotiate commit=False) -> confirm (commit_plan)
     boundary UNCHANGED (plan==confirm consistency, design spec A2).
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
from starlette.testclient import TestClient

from agents.planner_agent import PlannerAgent
from agents.accommodation_agent import AccommodationAgent
from agents.budget_agent import BudgetAgent
from agents.critic_agent import CriticAgent
from agents.risk_agent import RiskAgent
from agents.transport_agent import TransportAgent
from orchestration.orchestrator import TravelOrchestrator
from utils.ucp_signing import merchant_checkout_owner

from tests.test_trace_var0 import _merchant_result, _catalog_result, _PM_TRIP


# ---------------------------------------------------------------------------
# Capturing merchant transport — like test_trace_var0._SeqTransport, but also
# records the raw `arguments` body of every intercepted tool call so a test
# can assert exactly what user_id genuinely reached the merchant wire.
# ---------------------------------------------------------------------------

class _CapturingTransport(httpx.BaseTransport):
    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses: dict[str, list] = {}
        for tool, resp in responses.items():
            self._responses[tool] = list(resp) if isinstance(resp, list) else [resp]
        self._counts: dict[str, int] = {}
        self.captured: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            payload = json.loads(request.read())
        except Exception:
            return httpx.Response(400, text="bad request")
        params = payload.get("params") or {}
        tool = params.get("name", "")
        args = params.get("arguments") or {}
        with self._lock:
            self.captured.setdefault(tool, []).append(args)
            self._counts[tool] = self._counts.get(tool, 0) + 1
            idx = min(self._counts[tool] - 1, len(self._responses.get(tool, [])) - 1)
        resps = self._responses.get(tool)
        if not resps:
            return httpx.Response(500, json={"error": f"mock: no response for {tool!r}"})
        status, body = resps[max(idx, 0)]
        return httpx.Response(status, json=body)


def _build_society_with_budget_transport(budget_transport: httpx.BaseTransport) -> TravelOrchestrator:
    """Bookable Port-Moresby society (same fixture as test_wallet_sim._build_society)
    but with a caller-supplied budget merchant transport, so the test can capture
    the exact request bodies BudgetAgent sends."""
    PM_GRAND = {
        "hotel_id": "portmoresby-grand", "title": "Port Moresby Grand Hotel",
        "city": "port moresby", "review_score": 8.0, "star_rating": 4.0,
        "nights": 3, "total_cents": 54000, "amenities": ["pool", "wifi"],
        "provenance": "merchant", "area": "portmoresby",
    }
    LOOKUP_OK = _merchant_result({
        "hotel": {"id": "portmoresby-grand", "review_score": 8.0, "star_rating": 4.0},
        "available": True, "total_cents": 54000, "nights": 3, "source": "mock",
    })
    from tests.test_trace_var0 import _SeqTransport
    acc_transport = _SeqTransport({"search_catalog": _catalog_result([PM_GRAND])})
    critic_transport = _SeqTransport({"lookup_catalog": (200, LOOKUP_OK)})

    return TravelOrchestrator(
        planner_client=TestClient(PlannerAgent().build_app()),
        accommodation_client=TestClient(
            AccommodationAgent(merchant_transport=acc_transport).build_app()),
        budget_client=TestClient(
            BudgetAgent(merchant_transport=budget_transport).build_app()),
        critic_client=TestClient(
            CriticAgent(merchant_transport=critic_transport).build_app()),
        transport_client=TestClient(TransportAgent().build_app()),
        risk_client=TestClient(RiskAgent().build_app()),
    )


def _checkout_user_id(call: dict) -> Any:
    """Extract checkout.user_id from a captured create/update/complete_checkout
    call's arguments (the shape all three share — see budget_agent.py)."""
    return (call.get("checkout") or {}).get("user_id")


# ---------------------------------------------------------------------------
# 1. Real logged-in trip — the verified user_id must reach EVERY merchant call
#    (create_checkout, update_checkout, complete_checkout, wallet_fund).
# ---------------------------------------------------------------------------

def test_logged_in_real_trip_merchant_call_carries_verified_user_id():
    real_user_id = "user-real-9001"
    responses = {
        "create_checkout": (200, _merchant_result({
            "id": "co_pm161a", "status": "incomplete", "user_id": real_user_id,
            "line_items": [], "total_cents": 54000, "currency": "USD",
            "buyer_consent": False, "booking_ref": "",
        })),
        "update_checkout": (200, _merchant_result({
            "id": "co_pm161a", "status": "incomplete", "total_cents": 54000,
            "buyer_consent": True, "currency": "USD",
        })),
        "complete_checkout": (200, _merchant_result({
            "id": "co_pm161a", "status": "complete", "user_id": real_user_id,
            "line_items": [], "total_cents": 54000, "currency": "USD",
            "buyer_consent": True, "booking_ref": "BK-161-real",
            "wallet_session_id": "trip-x", "wallet_debit_cents": 54000,
            "wallet_balance_cents": 446000, "simulated": True,
        })),
        "wallet_fund": (200, _merchant_result({
            "status": "ok", "wallet_session_id": "trip-x",
            "seed_cents": 500000, "balance_cents": 500000, "simulated": True, "note": "sim",
        })),
    }
    transport = _CapturingTransport(responses)
    orch = _build_society_with_budget_transport(transport)

    # Mirrors server.py's /negotiate boundary: for a tier-1 logged-in caller,
    # merchant_checkout_owner() simply returns the real (session-verified)
    # user_id, computed BEFORE any anon uuid4 fallback would ever apply.
    expected = merchant_checkout_owner(user_id=real_user_id, owner_token=None)
    assert expected == real_user_id

    trip_request = copy.deepcopy(_PM_TRIP)
    trip_request["user_id"] = real_user_id
    trip_request["merchant_user_id"] = expected  # #161 — as server.py stamps it

    res = orch.negotiate(trip_request)  # commit=True (default): atomic plan + book
    assert res.get("outcome") == "success", res
    assert "161-real" in (res.get("booking_ref") or "")

    for tool in ("create_checkout", "update_checkout", "complete_checkout"):
        calls = transport.captured.get(tool) or []
        assert calls, f"{tool} was never called — cannot prove threading"
        for call in calls:
            got = _checkout_user_id(call)
            assert got == real_user_id, (
                f"{tool} carried checkout.user_id={got!r}, expected the "
                f"session-verified {real_user_id!r} — identity was dropped/mismatched"
            )

    fund_calls = transport.captured.get("wallet_fund") or []
    assert fund_calls, "wallet_fund was never called"
    assert fund_calls[0].get("user_id") == real_user_id, (
        f"wallet_fund carried user_id={fund_calls[0].get('user_id')!r}, "
        f"expected {real_user_id!r} — wallet ownership binding not threaded"
    )


# ---------------------------------------------------------------------------
# 2. Anonymous trip — the merchant must see a REAL, non-empty, non-skipped
#    pseudo-id (never "", never the raw owner_token, never the throwaway
#    internal uuid4), and it must survive plan (negotiate commit=False) ->
#    confirm (commit_plan) UNCHANGED.
# ---------------------------------------------------------------------------

def test_anonymous_trip_merchant_call_carries_real_nonskipped_pseudo_id():
    owner_token = "anon-secret-e2e-161"
    # Simulates server.py's uuid4() stamp for a body with no user_id — this is
    # the INTERNAL personalization id, deliberately NOT the merchant identity.
    internal_anon_user_id = "anon-uuid-8888-e2e"

    expected_mid = merchant_checkout_owner(user_id=None, owner_token=owner_token)
    assert expected_mid.startswith("anon:"), expected_mid
    assert expected_mid != "", "anon population must NOT map to a skipped/empty identity"
    assert expected_mid != owner_token, "the raw ownership secret must never reach the merchant"
    assert expected_mid != internal_anon_user_id, (
        "the merchant identity must be genuinely distinct from the throwaway "
        "internal uuid4 — proves this is a REAL derived id, not an accidental alias"
    )

    responses = {
        "create_checkout": (200, _merchant_result({
            "id": "co_pm161b", "status": "incomplete", "user_id": expected_mid,
            "line_items": [], "total_cents": 54000, "currency": "USD",
            "buyer_consent": False, "booking_ref": "",
        })),
        "update_checkout": (200, _merchant_result({
            "id": "co_pm161b", "status": "incomplete", "total_cents": 54000,
            "buyer_consent": True, "currency": "USD",
        })),
        "complete_checkout": (200, _merchant_result({
            "id": "co_pm161b", "status": "complete", "user_id": expected_mid,
            "line_items": [], "total_cents": 54000, "currency": "USD",
            "buyer_consent": True, "booking_ref": "BK-161-anon",
            "wallet_session_id": "trip-y", "wallet_debit_cents": 54000,
            "wallet_balance_cents": 446000, "simulated": True,
        })),
        "wallet_fund": (200, _merchant_result({
            "status": "ok", "wallet_session_id": "trip-y",
            "seed_cents": 500000, "balance_cents": 500000, "simulated": True, "note": "sim",
        })),
    }
    transport = _CapturingTransport(responses)
    orch = _build_society_with_budget_transport(transport)

    trip_request = copy.deepcopy(_PM_TRIP)
    trip_request["user_id"] = internal_anon_user_id
    trip_request["merchant_user_id"] = expected_mid  # #161 — as server.py stamps it for anon

    # --- PLAN phase (mirrors /negotiate {plan:true}) ---
    res = orch.negotiate(trip_request, commit=False)
    assert res.get("outcome") == "plan_ready", res
    ctx = res["_confirm_ctx"]
    # The SAME value must have been persisted into _confirm_ctx (this is what
    # server.py's _persist_and_sanitize_plan writes to row['merchant_user_id'],
    # and what a later /confirm reads back — the plan==confirm consistency
    # guarantee from design spec A2).
    assert ctx["merchant_user_id"] == expected_mid

    # create_checkout (the merchant CHECK phase) already carried the anon
    # pseudo-id at PLAN time.
    create_calls = transport.captured.get("create_checkout") or []
    assert create_calls, "create_checkout was never called at plan time"
    assert _checkout_user_id(create_calls[0]) == expected_mid

    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}

    # --- CONFIRM phase (mirrors /confirm reading the persisted row value) ---
    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"], merchant_user_id=ctx["merchant_user_id"],
    )
    assert booked.get("outcome") == "success", booked
    assert "161-anon" in (booked.get("booking_ref") or "")

    for tool in ("update_checkout", "complete_checkout"):
        calls = transport.captured.get(tool) or []
        assert calls, f"{tool} was never called at confirm time"
        for call in calls:
            got = _checkout_user_id(call)
            assert got, f"{tool} carried an EMPTY/skipped user_id — anon population unprotected"
            assert got == expected_mid, (
                f"{tool} carried checkout.user_id={got!r} at CONFIRM time, but "
                f"PLAN time used {expected_mid!r} — plan==confirm identity drifted"
            )
