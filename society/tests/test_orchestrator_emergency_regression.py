"""
test_orchestrator_emergency_regression.py — Regression tests for commits fef026d + 949e20b.

Covers:
  1. Mixed-leg trip: TW=active, JP=monitoring, ID=clear all land in active_emergencies.
  2. gate_blocked trace event is emitted on a 402 wallet denial.
  3. gate_blocked event carries the required kanban-consumed fields.
  4. gate_blocked is NOT emitted on a successful booking.
"""
from __future__ import annotations

import copy
import threading

import httpx
import pytest
from starlette.testclient import TestClient

from agents.accommodation_agent import AccommodationAgent
from agents.budget_agent import BudgetAgent
from agents.critic_agent import CriticAgent
from agents.planner_agent import PlannerAgent
from agents.risk_agent import RiskAgent
from agents.transport_agent import TransportAgent
from core.trace import CollectingTracer
from orchestration.orchestrator import TravelOrchestrator
from utils import emergency_feed as ef

# ---------------------------------------------------------------------------
# _SeqTransport — inline copy (must not import from test files)
# ---------------------------------------------------------------------------

class _SeqTransport(httpx.BaseTransport):
    """Returns canned merchant responses from a dict of tool → (status, body) or list thereof."""

    def __init__(self, responses: dict) -> None:
        self._responses: dict[str, list] = {}
        for tool, resp in responses.items():
            self._responses[tool] = list(resp) if isinstance(resp, list) else [resp]
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        import json as _json
        try:
            payload = _json.loads(request.read())
        except Exception:
            return httpx.Response(400, text="bad request")
        tool = (payload.get("params") or {}).get("name", "")
        with self._lock:
            self._counts[tool] = self._counts.get(tool, 0) + 1
            idx = min(self._counts[tool] - 1, len(self._responses.get(tool, [])) - 1)
        resps = self._responses.get(tool)
        if not resps:
            return httpx.Response(500, json={"error": f"mock: no response for {tool!r}"})
        status, body = resps[max(idx, 0)]
        return httpx.Response(status, json=body)


def _merchant_result(domain: dict) -> dict:
    import json as _json
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "structuredContent": domain,
        "content": [{"type": "text", "text": _json.dumps(domain)}],
    }}


def _catalog_result(items: list) -> tuple:
    return (200, _merchant_result({"source": "mock", "count": len(items), "results": items}))


# ---------------------------------------------------------------------------
# _result_with_legs — inline copy of the helper from test_emergency_feed.py
# ---------------------------------------------------------------------------

def _result_with_legs_mixed() -> dict:
    """Three legs: TW (active), JP (monitoring), ID (clear)."""
    return {"day_plans": [
        {"leg_id": "L1", "city": "taipei", "iso2": "TW", "region": "tw",
         "checkin": "2026-06-10", "checkout": "2026-06-14"},
        {"leg_id": "L2", "city": "tokyo", "iso2": "JP", "region": "jp",
         "checkin": "2026-06-15", "checkout": "2026-06-19"},
        {"leg_id": "L3", "city": "bali", "iso2": "ID", "region": "id-bali",
         "checkin": "2026-06-20", "checkout": "2026-06-24"},
    ]}


# ---------------------------------------------------------------------------
# _PM_TRIP — minimal Port Moresby trip request (mirrors test_trace_var0 / test_wallet_sim)
# ---------------------------------------------------------------------------
_PM_TRIP = {
    "user_id": "trace-test",
    "total_budget_cents": 120000,
    "wallet_balance_cents": 500000,
    "legs": [
        {"city": "port moresby", "checkin": "2026-03-01", "checkout": "2026-03-04",
         "adults": 1, "vibe": "city", "mode": "flight"},
    ],
}


# ---------------------------------------------------------------------------
# _build_society — inline copy of the helper from test_wallet_sim.py
# ---------------------------------------------------------------------------
_PM_GRAND = {
    "hotel_id": "portmoresby-grand", "title": "Port Moresby Grand Hotel",
    "city": "port moresby", "review_score": 8.0, "star_rating": 4.0,
    "nights": 3, "total_cents": 54000, "amenities": ["pool", "wifi"],
    "provenance": "merchant", "area": "portmoresby",
}
_LOOKUP_OK = _merchant_result({
    "hotel": {"id": "portmoresby-grand", "review_score": 8.0, "star_rating": 4.0},
    "available": True, "total_cents": 54000, "nights": 3, "source": "mock",
})
_CREATE_OK = _merchant_result({
    "id": "co_pm", "status": "incomplete", "user_id": "trace-test",
    "line_items": [], "total_cents": 54000, "currency": "USD",
    "buyer_consent": False, "booking_ref": "",
})
_FUND_OK = _merchant_result({
    "status": "ok", "wallet_session_id": "trip-x",
    "seed_cents": 500000, "balance_cents": 500000, "simulated": True, "note": "sim",
})


def _build_society(*, complete_resp, tracer=None):
    """Minimal Port Moresby society with an overridable complete_checkout response."""
    budget_transport = _SeqTransport({
        "create_checkout": (200, _CREATE_OK),
        "complete_checkout": complete_resp,
        "wallet_fund": (200, _FUND_OK),
    })
    acc_transport = _SeqTransport({"search_catalog": _catalog_result([_PM_GRAND])})
    critic_transport = _SeqTransport({"lookup_catalog": (200, _LOOKUP_OK)})

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


# ===========================================================================
# Mixed-leg trip regression
# ===========================================================================

def test_mixed_trip_all_statuses():
    """TW=active, JP=monitoring, ID=clear — all three land in active_emergencies."""
    orch = TravelOrchestrator(emergency_client=ef.stub_emergency_client)
    orch._emergency_request = {"check": True}
    result = _result_with_legs_mixed()
    orch._maybe_check_active_emergencies(result)

    assert "active_emergencies" in result, "active_emergencies key missing from result"
    by_city = {e["city"]: e for e in result["active_emergencies"]}

    # TW leg — active + DO NOT TRAVEL notice
    assert by_city["taipei"]["status"] == "active", by_city["taipei"]
    assert "DO NOT TRAVEL" in by_city["taipei"]["notice"], by_city["taipei"]

    # JP leg — monitoring, no 'notice' key, 'monitor' in note
    assert by_city["tokyo"]["status"] == "monitoring", by_city["tokyo"]
    assert "notice" not in by_city["tokyo"], \
        f"JP monitoring leg must not have 'notice' key: {by_city['tokyo']}"
    assert "monitor" in (by_city["tokyo"].get("note") or "").lower(), by_city["tokyo"]

    # ID leg — clear
    assert by_city["bali"]["status"] == "clear", by_city["bali"]

    # All three legs are present
    assert len(result["active_emergencies"]) == 3


# ===========================================================================
# gate_blocked trace event regression
# ===========================================================================

_INSUFFICIENT_RESP = _merchant_result({
    "status": "denied", "reason": "insufficient_funds", "id": "co_pm",
    "total_cents": 54000, "wallet_balance_cents": 40000,
    "wallet_session_id": "trip-x", "currency": "USD",
})


def test_gate_blocked_trace_event_emitted_on_402():
    """A 402 wallet denial must fire a wallet trace event with op=='gate_blocked'."""
    tracer = CollectingTracer()
    orch = _build_society(complete_resp=(402, _INSUFFICIENT_RESP), tracer=tracer)
    orch.negotiate(copy.deepcopy(_PM_TRIP))

    wallet_events = [e for e in tracer.events if e.type == "wallet"]
    gate_blocked = [e for e in wallet_events if e.data.get("op") == "gate_blocked"]
    assert gate_blocked, (
        f"expected a wallet/gate_blocked event but got wallet ops: "
        f"{[e.data.get('op') for e in wallet_events]}"
    )


def test_gate_blocked_event_has_required_fields():
    """The gate_blocked event must carry all fields consumed by the kanban handler."""
    tracer = CollectingTracer()
    orch = _build_society(complete_resp=(402, _INSUFFICIENT_RESP), tracer=tracer)
    orch.negotiate(copy.deepcopy(_PM_TRIP))

    gate_blocked = [
        e for e in tracer.events
        if e.type == "wallet" and e.data.get("op") == "gate_blocked"
    ]
    assert gate_blocked, "no gate_blocked event found"
    data = gate_blocked[0].data

    assert "op" in data, f"'op' missing from gate_blocked data: {data}"
    # Reconciled against main: commit 32770dd renamed total_cents → amount_cents in the
    # gate_blocked trace event to match kanban.html's data.amount_cents field read.
    # The test was written before that rename; update to reflect current correct behavior.
    assert "amount_cents" in data, f"'amount_cents' missing (renamed from total_cents in 32770dd): {data}"
    assert "balance_cents" in data, f"'balance_cents' missing: {data}"
    assert data.get("simulated") is True, \
        f"'simulated' must be True, got {data.get('simulated')!r}: {data}"
    assert data.get("reason") == "insufficient_funds", \
        f"'reason' must be 'insufficient_funds', got {data.get('reason')!r}: {data}"


def test_gate_blocked_not_emitted_on_success():
    """A successful booking (HTTP 200) must NOT emit a gate_blocked event."""
    COMPLETE_OK = _merchant_result({
        "id": "co_pm", "status": "complete", "user_id": "trace-test",
        "line_items": [], "total_cents": 54000, "currency": "USD",
        "buyer_consent": True, "booking_ref": "BK-regrtest-1",
        "wallet_session_id": "trip-x", "wallet_debit_cents": 54000,
        "wallet_balance_cents": 446000, "simulated": True,
    })
    tracer = CollectingTracer()
    orch = _build_society(complete_resp=(200, COMPLETE_OK), tracer=tracer)
    res = orch.negotiate(copy.deepcopy(_PM_TRIP))

    assert res.get("outcome") == "success", \
        f"expected success outcome for gate_blocked_not_emitted test, got: {res}"
    gate_blocked = [
        e for e in tracer.events
        if e.type == "wallet" and e.data.get("op") == "gate_blocked"
    ]
    assert not gate_blocked, \
        f"gate_blocked must NOT fire on a successful booking, but found: {gate_blocked}"


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"[OK] {_name}")
    print("\n[ALL] PASS")
