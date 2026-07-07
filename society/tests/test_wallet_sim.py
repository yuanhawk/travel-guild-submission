"""
test_wallet_sim.py — SIMULATED prepaid wallet (settlement-layer) Python tests.

Covers the Python half of the wallet seam:
  1. budget_agent._map_complete_response maps merchant 402 → insufficient_funds.
  2. The existing 403 price_exceeds_budget still maps to veto (REGRESSION guard —
     the additive 402 gate must not disturb the budget veto).
  3. The orchestrator surfaces a TERMINAL insufficient_funds (outcome
     cannot_satisfy, reason insufficient_funds) DISTINCT from a budget veto.
  4. The wallet DEBIT trace event fires on a committed booking that drew the
     wallet down; the CREDIT trace event fires on a recovery void/refund.
"""
import copy
import json
import os
import sys

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
from utils.recovery import RecoveryOrchestrator
from core.trace import CollectingTracer

from tests.test_trace_var0 import _SeqTransport, _merchant_result, _catalog_result, _PM_TRIP


# ---------------------------------------------------------------------------
# 1 + 2: _map_complete_response — 402 → insufficient_funds, 403 → veto.
# ---------------------------------------------------------------------------

def _agent() -> BudgetAgent:
    return BudgetAgent(merchant_transport=_SeqTransport({}))


def test_map_complete_402_insufficient_funds():
    agent = _agent()
    sc = {
        "status": "denied", "reason": "insufficient_funds", "id": "co_1",
        "total_cents": 54000, "wallet_balance_cents": 40000, "currency": "USD",
    }
    r = agent._map_complete_response(sc, 402, "co_1", 54000, "k1")
    assert r["decision"] == "insufficient_funds", r
    assert r["total_cents"] == 54000
    assert r["wallet_balance_cents"] == 40000


def test_map_complete_403_budget_still_veto():
    """REGRESSION: an over-budget-but-within-wallet 403 stays a veto, NOT 402."""
    agent = _agent()
    sc = {
        "status": "denied", "reason": "price_exceeds_budget", "id": "co_1",
        "total_cents": 90000, "budget_ceiling_cents": 80000, "currency": "USD",
    }
    r = agent._map_complete_response(sc, 403, "co_1", 90000, "k1")
    assert r["decision"] == "veto", r
    assert r["veto_reason"] == "price_exceeds_budget"
    assert r["budget_ceiling_cents"] == 80000


# ---------------------------------------------------------------------------
# 3: orchestrator surfaces a TERMINAL insufficient_funds distinct from a veto.
# ---------------------------------------------------------------------------

def _build_society(*, complete_resp, tracer=None, fund_resp=None):
    """Bookable Port-Moresby society with an overridable complete_checkout response."""
    PM_GRAND = {
        "hotel_id": "portmoresby-grand", "title": "Port Moresby Grand Hotel",
        "city": "port moresby", "review_score": 8.0, "star_rating": 4.0,
        "nights": 3, "total_cents": 54000, "amenities": ["pool", "wifi"],
        "provenance": "merchant", "area": "portmoresby",
    }
    CREATE_OK = _merchant_result({
        "id": "co_pm", "status": "incomplete", "user_id": "trace-test",
        "line_items": [], "total_cents": 54000, "currency": "USD",
        "buyer_consent": False, "booking_ref": "",
    })
    LOOKUP_OK = _merchant_result({
        "hotel": {"id": "portmoresby-grand", "review_score": 8.0, "star_rating": 4.0},
        "available": True, "total_cents": 54000, "nights": 3, "source": "mock",
    })
    # VULN-003: update_checkout response (silent server-side consent step).
    UPDATE_OK = _merchant_result({
        "id": "co_pm", "status": "incomplete", "total_cents": 54000,
        "buyer_consent": True, "currency": "USD",
    })
    budget_responses = {
        "create_checkout": (200, CREATE_OK),
        "update_checkout": (200, UPDATE_OK),
        "complete_checkout": complete_resp,
    }
    if fund_resp is not None:
        budget_responses["wallet_fund"] = fund_resp

    acc_transport = _SeqTransport({"search_catalog": _catalog_result([PM_GRAND])})
    budget_transport = _SeqTransport(budget_responses)
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
        tracer=tracer,
    )


def test_orchestrator_insufficient_funds_terminal():
    # complete_checkout → 402 insufficient_funds (within budget, over wallet).
    INSUFFICIENT = _merchant_result({
        "status": "denied", "reason": "insufficient_funds", "id": "co_pm",
        "total_cents": 54000, "wallet_balance_cents": 40000,
        "wallet_session_id": "trip-x", "currency": "USD",
    })
    orch = _build_society(complete_resp=(402, INSUFFICIENT))
    res = orch.negotiate(copy.deepcopy(_PM_TRIP))
    assert res.get("outcome") == "cannot_satisfy", res
    assert res.get("reason") == "insufficient_funds", res
    assert res.get("wallet_balance_cents") == 40000
    assert "wallet" in (res.get("detail") or "").lower()


def test_orchestrator_budget_veto_distinct_from_insufficient_funds():
    """A commit-time 403 budget veto is NOT insufficient_funds (re-plan, not wallet)."""
    VETO = _merchant_result({
        "status": "denied", "reason": "price_exceeds_budget", "id": "co_pm",
        "total_cents": 54000, "budget_ceiling_cents": 1, "currency": "USD",
    })
    orch = _build_society(complete_resp=(403, VETO))
    res = orch.negotiate(copy.deepcopy(_PM_TRIP))
    # The veto path re-plans/exhausts → cannot_satisfy but NOT reason insufficient_funds.
    assert res.get("reason") != "insufficient_funds", res


# ---------------------------------------------------------------------------
# 4: wallet DEBIT + CREDIT trace events fire.
# ---------------------------------------------------------------------------

def test_wallet_debit_trace_event_fires():
    COMPLETE_OK = _merchant_result({
        "id": "co_pm", "status": "complete", "user_id": "trace-test",
        "line_items": [], "total_cents": 54000, "currency": "USD",
        "buyer_consent": True, "booking_ref": "BK-trace-1",
        # SIMULATED prepaid wallet draw-down surfaced by the merchant.
        "wallet_session_id": "trip-x", "wallet_debit_cents": 54000,
        "wallet_balance_cents": 446000, "simulated": True,
    })
    FUND_OK = _merchant_result({
        "status": "ok", "wallet_session_id": "trip-x",
        "seed_cents": 500000, "balance_cents": 500000, "simulated": True, "note": "sim",
    })
    tracer = CollectingTracer()
    orch = _build_society(complete_resp=(200, COMPLETE_OK), tracer=tracer, fund_resp=(200, FUND_OK))
    res = orch.negotiate(copy.deepcopy(_PM_TRIP))
    assert res.get("outcome") == "success", res

    wallet_events = [e for e in tracer.events if e.type == "wallet"]
    ops = [e.data.get("op") for e in wallet_events]
    assert "seed" in ops, f"no wallet seed event: {ops}"
    debit = [e for e in wallet_events if e.data.get("op") == "debit"]
    assert debit, f"no wallet debit event: {ops}"
    assert debit[0].data.get("amount_cents") == 54000
    assert debit[0].data.get("balance_cents") == 446000
    assert debit[0].data.get("simulated") is True


def test_wallet_credit_trace_event_on_recovery_void():
    CANCEL_OK = _merchant_result({
        "status": "cancelled", "id": "co_pm", "cancelled": True,
        "released_booking_ref": "BK-old", "total_cents": 54000, "currency": "USD",
        "wallet_session_id": "trip-x", "wallet_credit_cents": 54000,
        "wallet_balance_cents": 500000, "simulated": True,
    })
    budget_transport = _SeqTransport({"cancel_checkout": (200, CANCEL_OK)})
    tracer = CollectingTracer()
    ro = RecoveryOrchestrator(
        budget_client=TestClient(BudgetAgent(merchant_transport=budget_transport).build_app()),
        tracer=tracer,
    )
    out = ro.cancel_recovery(checkout_id="co_pm", booking_ref="BK-old", user_id="u1")
    assert out["voided"] is True, out

    credit = [e for e in tracer.events if e.type == "wallet" and e.data.get("op") == "credit"]
    assert credit, f"no wallet credit event: {[e.type for e in tracer.events]}"
    assert credit[0].data.get("amount_cents") == 54000
    assert credit[0].data.get("balance_cents") == 500000
    assert credit[0].data.get("simulated") is True


def test_wallet_session_id_threaded_into_create_checkout():
    """The deterministic per-run session id reaches create_checkout (the keystone)."""
    COMPLETE_OK = _merchant_result({
        "id": "co_pm", "status": "complete", "user_id": "trace-test",
        "line_items": [], "total_cents": 54000, "currency": "USD",
        "buyer_consent": True, "booking_ref": "BK-1",
    })
    seen = {}

    class _CapturingTransport(httpx.BaseTransport):
        def handle_request(self, request):
            payload = json.loads(request.read())
            name = (payload.get("params") or {}).get("name", "")
            args = (payload.get("params") or {}).get("arguments", {})
            if name == "create_checkout":
                seen["wallet_session_id"] = (args.get("checkout") or {}).get("wallet_session_id")
                return httpx.Response(200, json=_merchant_result({
                    "id": "co_pm", "status": "incomplete", "total_cents": 54000,
                    "currency": "USD", "buyer_consent": False, "booking_ref": "",
                    "line_items": [], "user_id": "trace-test",
                }))
            if name == "complete_checkout":
                return httpx.Response(200, json=COMPLETE_OK)
            if name == "wallet_fund":
                return httpx.Response(200, json=_merchant_result({"status": "ok"}))
            return httpx.Response(500, json={"error": f"no mock for {name}"})

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
    orch = TravelOrchestrator(
        planner_client=TestClient(PlannerAgent().build_app()),
        accommodation_client=TestClient(AccommodationAgent(
            merchant_transport=_SeqTransport({"search_catalog": _catalog_result([PM_GRAND])})).build_app()),
        budget_client=TestClient(BudgetAgent(merchant_transport=_CapturingTransport()).build_app()),
        critic_client=TestClient(CriticAgent(
            merchant_transport=_SeqTransport({"lookup_catalog": (200, LOOKUP_OK)})).build_app()),
        transport_client=TestClient(TransportAgent().build_app()),
        risk_client=TestClient(RiskAgent().build_app()),
    )
    req = copy.deepcopy(_PM_TRIP)
    req["wallet_balance_cents"] = 500000
    res = orch.negotiate(req)
    assert res.get("outcome") == "success", res
    assert seen.get("wallet_session_id"), "wallet_session_id not threaded into create_checkout"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"[OK] {name}")
    print("\n[ALL] PASS")
