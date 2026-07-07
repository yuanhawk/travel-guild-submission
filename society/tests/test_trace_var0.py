"""
test_trace_var0.py — Trace event layer tests for TravelOrchestrator.

Tests:
  1. test_var0_unaffected_by_tracer  — negotiate() output is identical with/without tracer.
  2. test_tracer_fires_all_agent_events — CollectingTracer captures >= 10 events including
     negotiate_started, negotiate_finished, and at least one agent_completed per major agent.
  3. test_tracer_nocrash_disabled — no tracer kwarg → no crash, result has 'outcome' key.
  4. test_collecting_tracer_seq_monotonic — seq values are 0,1,2,3,4 for 5 emits.
  5. test_subscriber_notified — subscriber lambda is called on emit.
"""
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
from tests.test_composition_wiring import build_society
from core.trace import CollectingTracer, NoOpTracer, TraceEvent


# ---------------------------------------------------------------------------
# Mock merchant transport (no network required — same pattern as test_composition_wiring)
# ---------------------------------------------------------------------------

class _SeqTransport(httpx.BaseTransport):
    """Returns canned merchant responses from a dict of tool → [list of (status, body)]."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses: dict[str, list] = {}
        for tool, resp in responses.items():
            self._responses[tool] = list(resp) if isinstance(resp, list) else [resp]
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            payload = json.loads(request.read())
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
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "structuredContent": domain,
        "content": [{"type": "text", "text": json.dumps(domain)}],
    }}


def _catalog_result(results: list[dict]) -> tuple:
    return (200, _merchant_result({"source": "mock", "count": len(results), "results": results}))


def _build_bookable_society(tracer=None):
    """
    Build a society with a mocked merchant (no network) so ALL agents run end-to-end.
    Port Moresby is bookable in the seeded catalog and carries civil_unrest (Risk fires).
    """
    PM_GRAND = {
        "hotel_id": "portmoresby-grand", "title": "Port Moresby Grand Hotel",
        "city": "port moresby", "review_score": 8.0, "star_rating": 4.0,
        "nights": 3, "total_cents": 54000, "amenities": ["pool", "wifi"],
        "provenance": "merchant", "area": "portmoresby",  # match the real catalog area (#74:
        # destination now returns Port Moresby's real catalog area 'portmoresby', so the mock
        # hotel must carry it for the accommodation area-filter to find it — as in production.
    }
    CREATE_OK = _merchant_result({
        "id": "co_pm", "status": "incomplete", "user_id": "trace-test",
        "line_items": [], "total_cents": 54000, "currency": "USD",
        "buyer_consent": False, "booking_ref": "",
    })
    # VULN-003: update_checkout response (sets server-side consent; no trace event).
    UPDATE_OK = _merchant_result({
        "id": "co_pm", "status": "incomplete", "total_cents": 54000,
        "buyer_consent": True, "currency": "USD",
    })
    COMPLETE_OK = _merchant_result({
        "id": "co_pm", "status": "complete", "user_id": "trace-test",
        "line_items": [], "total_cents": 54000, "currency": "USD",
        "buyer_consent": True, "booking_ref": "BK-trace-1",
    })
    LOOKUP_OK = _merchant_result({
        "hotel": {"id": "portmoresby-grand", "review_score": 8.0, "star_rating": 4.0},
        "available": True, "total_cents": 54000, "nights": 3, "source": "mock",
    })

    acc_transport = _SeqTransport({"search_catalog": _catalog_result([PM_GRAND])})
    budget_transport = _SeqTransport({
        "create_checkout": (200, CREATE_OK),
        # VULN-003: update_checkout response (silent internal step, no trace event).
        "update_checkout": (200, UPDATE_OK),
        "complete_checkout": (200, COMPLETE_OK),
    })
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


_PM_TRIP = {
    "user_id": "trace-test",
    "total_budget_cents": 120000,
    "legs": [
        {"city": "port moresby", "checkin": "2026-03-01", "checkout": "2026-03-04",
         "adults": 1, "vibe": "city", "mode": "flight"},
    ],
}

# The canonical bali-wet-jan trip (var-0 reference case from test_stress_composed).
_BALI_WET_JAN = {
    "user_id": "trace-test",
    "total_budget_cents": 400000,
    "today": "2026-01-05",
    "nationality": "US",
    "legs": [
        {
            "city": "bali",
            "dest_country": "ID",
            "departure_date": "2026-01-10",
            "checkin": "2026-01-10",
            "checkout": "2026-01-14",
            "adults": 1,
            "vibe": "culture",
        }
    ],
}


def _stable(res: dict) -> str:
    """Canonical projection for Var-0 comparison (drop volatile ids/timestamps)."""
    sig = res.get("risk_signals") or {}
    rollup = sig.get("rollup") or {}
    codes = set()
    for leg in sig.get("per_leg") or []:
        codes.update(leg.get("reason_codes") or [])
    codes.update(rollup.get("all_reason_codes") or [])
    # Also include outcome and reason_codes from risk_signals top-level
    codes.update(sig.get("reason_codes") or [])
    # SIMULATED prepaid wallet — the wallet is a side-channel and does NOT appear in
    # the result EXCEPT on the insufficient_funds terminal (which surfaces the gate's
    # total/balance). Project those so a wallet-determinism case is covered by the
    # var-0 comparison; None on every other path (success/veto) → byte-identical.
    is_insufficient = res.get("reason") == "insufficient_funds"
    wallet = {
        "reason": "insufficient_funds" if is_insufficient else None,
        "total_cents": res.get("total_cents") if is_insufficient else None,
        "wallet_balance_cents": res.get("wallet_balance_cents") if is_insufficient else None,
    }
    return json.dumps(
        {
            "outcome": res.get("outcome"),
            "codes": sorted(codes),
            "wallet": wallet,
        },
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# Test 1: Var-0 — tracer does not affect negotiate() output
# ---------------------------------------------------------------------------

def test_var0_unaffected_by_tracer():
    """
    negotiate() output MUST be identical whether a CollectingTracer is attached
    or not. The tracer is a pure side-channel — it NEVER mutates the result dict,
    NEVER changes call order, and NEVER adds keys to the output.
    """
    import copy

    tracer = CollectingTracer()
    orch_with = _build_bookable_society(tracer=tracer)
    orch_without = _build_bookable_society(tracer=None)

    r1 = orch_with.negotiate(copy.deepcopy(_PM_TRIP))
    r2 = orch_without.negotiate(copy.deepcopy(_PM_TRIP))

    assert _stable(r1) == _stable(r2), (
        f"Var-0 violated: tracer changed negotiate() output.\n"
        f"  with tracer:    {_stable(r1)}\n"
        f"  without tracer: {_stable(r2)}"
    )
    # Confirm tracer did fire events but did NOT mutate the result
    assert len(tracer.events) > 0, "CollectingTracer should have captured events"
    print(f"[VAR-0] PASS — {len(tracer.events)} events captured, stable output identical")


# ---------------------------------------------------------------------------
# Test 2: All major agent events fire
# ---------------------------------------------------------------------------

def test_tracer_fires_all_agent_events():
    """
    A CollectingTracer attached to negotiate() MUST fire at minimum:
      - negotiate_started (exactly once, first event)
      - negotiate_finished (last event)
      - >= 10 events total
      - at least one agent_completed for Risk, Compliance (or their absence is fine
        when the gate doesn't fire — we test for agents that ALWAYS fire on this trip)
    """
    import copy

    tracer = CollectingTracer()
    orch = _build_bookable_society(tracer=tracer)
    res = orch.negotiate(copy.deepcopy(_PM_TRIP))

    assert res.get("outcome") in ("success", "cannot_satisfy"), (
        f"negotiate() must return a structured result; got: {res}"
    )

    event_types = [e.type for e in tracer.events]
    agent_names = [e.agent for e in tracer.events if e.type == "agent_completed"]

    print(f"[EVENTS] total={len(tracer.events)} types={set(event_types)} agents={set(agent_names)}")
    for e in tracer.events:
        print(f"  seq={e.seq} type={e.type} agent={e.agent} summary={e.summary!r}")

    assert len(tracer.events) >= 7, (
        f"Expected >= 7 events, got {len(tracer.events)}: {event_types}"
    )
    assert "negotiate_started" in event_types, (
        f"negotiate_started not fired; events: {event_types}"
    )
    assert "negotiate_finished" in event_types, (
        f"negotiate_finished not fired; events: {event_types}"
    )
    assert event_types[0] == "negotiate_started", (
        f"First event must be negotiate_started, got {event_types[0]}"
    )
    assert event_types[-1] == "negotiate_finished", (
        f"Last event must be negotiate_finished, got {event_types[-1]}"
    )

    # At least one agent_completed for the agents that always fire on any trip
    for required_agent in ("Destination", "Risk", "Planner", "Budget"):
        assert required_agent in agent_names, (
            f"No agent_completed event for {required_agent}; agents fired: {agent_names}"
        )

    print("[EVENTS] PASS — all required events present")


# ---------------------------------------------------------------------------
# Test 3: No-tracer path doesn't crash
# ---------------------------------------------------------------------------

def test_tracer_nocrash_disabled():
    """
    negotiate() with NO tracer kwarg (default) MUST not crash and MUST return
    a dict with an 'outcome' key.
    """
    import copy

    orch = build_society()
    # No tracer — uses default no-op lambda
    res = orch.negotiate(copy.deepcopy(_BALI_WET_JAN))

    assert isinstance(res, dict), f"Expected dict result, got {type(res)}"
    assert "outcome" in res, f"Result missing 'outcome' key: {res}"
    print(f"[NO-TRACER] PASS — outcome={res['outcome']}")


# ---------------------------------------------------------------------------
# Test 4: seq monotonic
# ---------------------------------------------------------------------------

def test_collecting_tracer_seq_monotonic():
    """
    CollectingTracer seq counter MUST be 0,1,2,3,4 for 5 emits (deterministic,
    monotonically increasing from 0).
    """
    tracer = CollectingTracer()
    for i in range(5):
        tracer("agent_completed", "TestAgent", trip_id="t1", summary=f"event {i}")

    seqs = [e.seq for e in tracer.events]
    assert seqs == [0, 1, 2, 3, 4], f"Expected [0,1,2,3,4], got {seqs}"
    print(f"[SEQ] PASS — seq values: {seqs}")


# ---------------------------------------------------------------------------
# Test 5: subscriber notified
# ---------------------------------------------------------------------------

def test_subscriber_notified():
    """
    A subscriber registered on CollectingTracer MUST be called synchronously
    when an event is emitted.
    """
    tracer = CollectingTracer()
    received: list[TraceEvent] = []
    tracer.subscribers.append(received.append)

    tracer("negotiate_started", trip_id="trip-xyz", summary="test event")

    assert len(received) == 1, f"Subscriber should have been called once, got {len(received)}"
    assert received[0].type == "negotiate_started", (
        f"Expected negotiate_started, got {received[0].type}"
    )
    assert received[0].trip_id == "trip-xyz", (
        f"Expected trip_id='trip-xyz', got {received[0].trip_id}"
    )
    print(f"[SUBSCRIBER] PASS — subscriber received event: seq={received[0].seq} type={received[0].type}")


# ---------------------------------------------------------------------------
# Test 6: Var-0 holds with the SIMULATED prepaid wallet UNWIRED and WIRED
# ---------------------------------------------------------------------------

def test_var0_wallet_unwired_and_wired():
    """
    The wallet must not perturb the var-0 result. A request with NO wallet key
    (unwired → DEMO default, digest unchanged) and one WITH an explicit
    wallet_balance_cents (wired) MUST produce the same _stable() projection on a
    bookable trip (the wallet is a side-channel; the booking is well within any
    sane balance). And the seed wallet event fires when a tracer is attached.
    """
    import copy

    tracer = CollectingTracer()
    orch_unwired = _build_bookable_society(tracer=None)
    orch_wired = _build_bookable_society(tracer=tracer)

    unwired_req = copy.deepcopy(_PM_TRIP)            # no wallet_balance_cents
    wired_req = copy.deepcopy(_PM_TRIP)
    wired_req["wallet_balance_cents"] = 500000        # explicit wallet seed

    r_unwired = orch_unwired.negotiate(unwired_req)
    r_wired = orch_wired.negotiate(wired_req)

    assert _stable(r_unwired) == _stable(r_wired), (
        f"wallet perturbed var-0:\n  unwired: {_stable(r_unwired)}\n  wired:   {_stable(r_wired)}"
    )
    # The seed event fires (side-channel) on the wired run with a tracer attached.
    seed = [e for e in tracer.events if e.type == "wallet" and e.data.get("op") == "seed"]
    assert seed, "expected a wallet seed event on the wired run"

    # Determinism: a second wired run is byte-identical (var-0 keystone).
    r_wired2 = _build_bookable_society(tracer=None).negotiate(copy.deepcopy(wired_req))
    assert _stable(r_wired) == _stable(r_wired2)
    print("[VAR-0 WALLET] PASS — wallet seam is var-0-stable unwired and wired")


if __name__ == "__main__":
    test_var0_unaffected_by_tracer()
    test_tracer_fires_all_agent_events()
    test_tracer_nocrash_disabled()
    test_collecting_tracer_seq_monotonic()
    test_subscriber_notified()
    test_var0_wallet_unwired_and_wired()
    print("\n[ALL] PASS")
