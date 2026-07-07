"""
test_orchestrator_partial.py — P0 partial-booking fix unit tests.

CI-safe: all merchant HTTP calls intercepted with mock transport.
         Uses Starlette in-process ASGI TestClient throughout.

Design contract: AGENT-SOCIETY-A2A-DESIGN.md §4.2, §4.3, §4.6 (all-or-none rule).

P0 Bug:
    The orchestrator previously computed fitted_legs (only legs WITH proposals) and
    ignored no_fit_legs.  When some legs had no_fit, it built line_items from only
    the fitted legs, called Budget with buyer_consent=True, got an "accept", and
    returned _success_result with only the partial legs.  For S3 (Bangkok+KL+Singapore
    on $700 budget), this meant KL was booked alone (1/3 legs) and called "success".

P0 Fix (Layer 1 — Orchestrator):
    After computing fitted_legs and no_fit_legs, if no_fit_legs is non-empty, the
    orchestrator returns _cannot_satisfy_result immediately with a clear reason.
    It NEVER calls Budget with a partial package.

P0 Fix (Layer 2 — Critic backstop):
    The Critic accepts an optional planned_leg_count.  If assembled legs < planned,
    it emits MISSING_LEG immediately (defense-in-depth).

Coverage:
  1. test_partial_no_fit_one_leg_returns_cannot_satisfy
         Leg-0 and Leg-2 return no_fit; Leg-1 returns a proposal.
         Orchestrator must return cannot_satisfy (NOT success).
         Verifies P0 Layer 1 fix (all-or-none rule).

  2. test_partial_no_fit_all_legs_returns_cannot_satisfy
         All legs return no_fit.
         Orchestrator must return cannot_satisfy.

  3. test_full_fit_all_legs_returns_success
         All three legs have proposals and budget accepts.
         Orchestrator must return success with booking_ref.
         Regression test — ensures the fix doesn't break the happy path.

  4. test_critic_missing_leg_backstop
         Critic called directly with planned_leg_count=3 but only 1 assembled leg.
         Critic must emit MISSING_LEG violation and return "rejected".
         Verifies P0 Layer 2 fix (Critic backstop).

  5. test_critic_planned_leg_count_matches_all_legs_no_violation
         Critic called with planned_leg_count=1 and 1 assembled leg (matches).
         No MISSING_LEG violation from count mismatch.

  6. test_critic_no_planned_leg_count_no_backstop_violation
         Critic called WITHOUT planned_leg_count.
         No count-mismatch MISSING_LEG fires (backward compat).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any

import httpx

# Resolve imports from society/
sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from agents import planner_agent as planner_mod
from agents import accommodation_agent as acc_mod
from agents import budget_agent as ba_mod
from agents import critic_agent as ca_mod
from agents.critic_agent import MISSING_LEG
from orchestration.orchestrator import TravelOrchestrator


# ===========================================================================
# Mock transport infrastructure
# (Same pattern as test_negotiation.py and test_critic.py)
# ===========================================================================

class MockMerchantTransport(httpx.BaseTransport):
    """
    Intercepts merchant MCP calls; returns canned responses keyed by tool name.
    """

    def __init__(self, responses: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self._responses = responses

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            payload = json.loads(request.read())
        except Exception:
            return httpx.Response(400, text="bad request")

        tool_name = (payload.get("params") or {}).get("name", "")
        if tool_name not in self._responses:
            return httpx.Response(
                500,
                json={"error": f"mock: no response for tool {tool_name!r}"},
            )
        status_code, body = self._responses[tool_name]
        return httpx.Response(status_code, json=body)


class SequencedMockTransport(httpx.BaseTransport):
    """
    Returns different responses per call (each call pops from the front of a
    per-tool queue).  Useful when the same tool is called multiple times with
    different expected outcomes across legs.
    """

    def __init__(
        self,
        sequences: dict[str, list[tuple[int, dict[str, Any]]]],
    ) -> None:
        self._sequences = {k: list(v) for k, v in sequences.items()}
        self._call_counts: dict[str, int] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            payload = json.loads(request.read())
        except Exception:
            return httpx.Response(400, text="bad request")

        tool_name = (payload.get("params") or {}).get("name", "")
        seq = self._sequences.get(tool_name)
        if seq is None:
            return httpx.Response(
                500,
                json={"error": f"mock: no sequence for tool {tool_name!r}"},
            )
        idx = self._call_counts.get(tool_name, 0)
        if idx >= len(seq):
            idx = len(seq) - 1  # Repeat last response if exhausted
        self._call_counts[tool_name] = idx + 1
        status_code, body = seq[idx]
        return httpx.Response(status_code, json=body)


# ===========================================================================
# Merchant response helpers
# ===========================================================================

def _merchant_result(domain: dict[str, Any]) -> dict[str, Any]:
    """Wrap domain dict in the merchant MCP result envelope."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "structuredContent": domain,
            "content": [{"type": "text", "text": json.dumps(domain)}],
        },
    }


def _catalog_result(results: list[dict]) -> tuple[int, dict]:
    """Wrap a list of hotel results in the catalog search envelope."""
    return (200, _merchant_result({
        "source": "mock",
        "count": len(results),
        "results": results,
    }))


def _catalog_empty() -> tuple[int, dict]:
    """Empty catalog result (no hotels fit) — triggers no_fit."""
    return _catalog_result([])


# ---------------------------------------------------------------------------
# Hotel fixtures (prices match catalog.go)
# ---------------------------------------------------------------------------

KL_BUKIT = {
    "hotel_id": "kl-bukit-bintang",
    "title": "Bukit Bintang Hotel",
    "city": "kuala lumpur",
    "review_score": 8.3,
    "star_rating": 4.0,
    "nights": 3,
    "total_cents": 16500,  # 5500 × 3
    "amenities": ["wifi", "pool"],
}

BKK_METRO = {
    "hotel_id": "bangkok-metro",
    "title": "Bangkok Metro Hotel",
    "city": "bangkok",
    "review_score": 8.5,
    "star_rating": 3.0,
    "nights": 3,
    "total_cents": 21600,  # 7200 × 3
    "amenities": ["wifi", "breakfast"],
}

SG_MARINA = {
    "hotel_id": "singapore-marina-bay",
    "title": "Marina Bay Hotel",
    "city": "singapore",
    "review_score": 8.0,
    "star_rating": 5.0,
    "nights": 4,
    "total_cents": 48000,  # 12000 × 4
    "amenities": ["pool", "spa", "concierge"],
}

# ---------------------------------------------------------------------------
# Budget mock responses
# ---------------------------------------------------------------------------

# Budget "accept" for KL-only booking (16500¢) — should NOT be reached post-P0 fix
_BUDGET_CREATE_KL_ONLY = _merchant_result({
    "id": "co_kl_only",
    "status": "incomplete",
    "user_id": "test-u1",
    "line_items": [],
    "total_cents": 16500,
    "currency": "USD",
    "buyer_consent": False,
    "booking_ref": "",
})

_BUDGET_COMPLETE_KL_ACCEPT = _merchant_result({
    "id": "co_kl_only",
    "status": "complete",
    "user_id": "test-u1",
    "line_items": [],
    "total_cents": 16500,
    "currency": "USD",
    "buyer_consent": True,
    "booking_ref": "BK-kl-partial",   # This booking_ref MUST NOT appear in cannot_satisfy result
})

# Budget "accept" for full 3-leg booking
_BUDGET_CREATE_FULL = _merchant_result({
    "id": "co_full",
    "status": "incomplete",
    "user_id": "test-u1",
    "line_items": [],
    "total_cents": 60000,   # 21600 + 16500 + 48000 is 86100, but use 60000 for mock
    "currency": "USD",
    "buyer_consent": False,
    "booking_ref": "",
})

_BUDGET_COMPLETE_FULL_ACCEPT = _merchant_result({
    "id": "co_full",
    "status": "complete",
    "user_id": "test-u1",
    "line_items": [],
    "total_cents": 60000,
    "currency": "USD",
    "buyer_consent": True,
    "booking_ref": "BK-full-3leg",
})


# ===========================================================================
# Builder helpers
# ===========================================================================

def _make_planner_client() -> TestClient:
    agent = planner_mod.PlannerAgent()
    return TestClient(agent.build_app(), raise_server_exceptions=True)


def _make_acc_client(transport: httpx.BaseTransport) -> TestClient:
    agent = acc_mod.AccommodationAgent(merchant_transport=transport)
    return TestClient(agent.build_app(), raise_server_exceptions=True)


def _make_budget_client(transport: httpx.BaseTransport) -> TestClient:
    agent = ba_mod.BudgetAgent(merchant_transport=transport)
    return TestClient(agent.build_app(), raise_server_exceptions=True)


def _make_critic_client(transport: httpx.BaseTransport) -> TestClient:
    agent = ca_mod.CriticAgent(merchant_transport=transport)
    return TestClient(agent.build_app(), raise_server_exceptions=True)


def _rpc_post(client: TestClient, method: str, params: dict) -> dict:
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
    resp = client.post("/", json=body)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    return resp.json()


def _send_data(client: TestClient, payload: dict, skill_id: str) -> dict:
    """Send a data-part message to a TestClient agent; return the Task dict."""
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


# 3-leg Bangkok → KL → Singapore trip request
_S3_TRIP = {
    "user_id": "test-u1",
    "total_budget_cents": 70000,
    "legs": [
        {"city": "bangkok",       "checkin": "2026-12-01", "checkout": "2026-12-04", "adults": 1, "vibe": "city"},
        {"city": "kuala lumpur",  "checkin": "2026-12-04", "checkout": "2026-12-07", "adults": 1, "vibe": "city"},
        {"city": "singapore",     "checkin": "2026-12-07", "checkout": "2026-12-11", "adults": 1, "vibe": "city"},
    ],
}


# ===========================================================================
# Test 1: Partial no_fit (legs 0 and 2 no_fit, leg 1 ok) → cannot_satisfy
# ===========================================================================

def test_partial_no_fit_one_leg_returns_cannot_satisfy() -> None:
    """
    P0 regression test (Layer 1 — Orchestrator all-or-none rule).

    Scenario: 3-leg Bangkok+KL+Singapore trip.
    - Leg-0 (Bangkok): no hotels fit within budget ceiling → no_fit
    - Leg-1 (KL):      hotel fits → proposal (KL-Bukit, 16500¢)
    - Leg-2 (Singapore): no hotels fit within budget ceiling → no_fit

    Pre-P0 bug: orchestrator books KL only (1/3 legs) and returns 'success'.
    Post-P0 fix: orchestrator must return 'cannot_satisfy' with zero booking_ref.
    """
    # Accommodation: leg-0=empty(no_fit), leg-1=KL fits, leg-2=empty(no_fit)
    acc_transport = SequencedMockTransport({
        "search_catalog": [
            _catalog_empty(),                   # Leg-0 Bangkok: no_fit
            _catalog_result([KL_BUKIT]),         # Leg-1 KL: ok
            _catalog_empty(),                   # Leg-2 Singapore: no_fit
        ],
    })

    # Budget should NEVER be called (the orchestrator must not proceed with partial legs)
    # If Budget IS called, the test will catch this via the booking_ref appearing in the result.
    budget_transport = MockMerchantTransport({
        "create_checkout": (200, _BUDGET_CREATE_KL_ONLY),
        "complete_checkout": (200, _BUDGET_COMPLETE_KL_ACCEPT),
    })

    planner_client = _make_planner_client()
    acc_client = _make_acc_client(acc_transport)
    budget_client = _make_budget_client(budget_transport)

    orchestrator = TravelOrchestrator(
        planner_client=planner_client,
        accommodation_client=acc_client,
        budget_client=budget_client,
        # No Critic client — testing the Orchestrator's own all-or-none guard
    )

    result = orchestrator.negotiate(_S3_TRIP)

    # PRIMARY ASSERTION: must be cannot_satisfy (all-or-none rule)
    assert result["outcome"] == "cannot_satisfy", (
        f"P0 REGRESSION: orchestrator returned {result['outcome']!r} instead of 'cannot_satisfy'. "
        f"Full result:\n{json.dumps(result, indent=2, default=str)}"
    )

    # No booking_ref (would indicate a partial booking was committed)
    assert not result.get("booking_ref"), (
        f"P0 REGRESSION: booking_ref {result.get('booking_ref')!r} present in cannot_satisfy result. "
        f"Orchestrator must not commit partial bookings."
    )

    # Reason must mention the unfilled legs. #70 made this stronger: the reason now NAMES the
    # un-satisfiable leg(s) and states a distinguished cause (no_inventory vs over_budget),
    # replacing the old generic 'partial/no hotel' phrasing.
    reason = result.get("reason", "")
    assert reason, "cannot_satisfy must include a reason"
    assert ("bangkok" in reason.lower() or "singapore" in reason.lower()), (
        f"cannot_satisfy reason must NAME the un-satisfiable leg(s): {reason!r}"
    )
    assert any(k in reason.lower() for k in ["lodging", "inventory", "budget", "cannot satisfy"]), (
        f"cannot_satisfy reason must state a cause: {reason!r}"
    )

    # Negotiation log must be present (transparency)
    assert "negotiation_log" in result

    print(
        f"PASS: test_partial_no_fit_one_leg_returns_cannot_satisfy "
        f"[outcome={result['outcome']!r} reason={result.get('reason','')[:80]!r}]"
    )


# ===========================================================================
# Test 2: All legs no_fit → cannot_satisfy (baseline coverage)
# ===========================================================================

def test_partial_no_fit_all_legs_returns_cannot_satisfy() -> None:
    """
    All legs return no_fit → orchestrator returns cannot_satisfy.
    This was already the pre-P0 behavior (when line_items is empty).
    Verifies the existing terminal path still works after the P0 change.
    """
    acc_transport = MockMerchantTransport({
        "search_catalog": _catalog_empty(),  # all legs → no_fit
    })
    budget_transport = MockMerchantTransport({})  # Budget never called

    planner_client = _make_planner_client()
    acc_client = _make_acc_client(acc_transport)
    budget_client = _make_budget_client(budget_transport)

    orchestrator = TravelOrchestrator(
        planner_client=planner_client,
        accommodation_client=acc_client,
        budget_client=budget_client,
    )

    result = orchestrator.negotiate(_S3_TRIP)

    assert result["outcome"] == "cannot_satisfy", (
        f"Expected 'cannot_satisfy', got {result['outcome']!r}"
    )
    assert not result.get("booking_ref"), "No booking_ref on all-legs-no_fit"
    assert result.get("reason"), "Must include a reason"

    print(
        f"PASS: test_partial_no_fit_all_legs_returns_cannot_satisfy "
        f"[reason={result.get('reason','')[:60]!r}]"
    )


# ===========================================================================
# Test 3: All legs fit + budget accepts → success (regression test)
# ===========================================================================

def test_full_fit_all_legs_returns_success() -> None:
    """
    All three legs have proposals and budget accepts → success.

    This is the happy-path regression test: ensures the P0 all-or-none fix
    does NOT break the successful 3-leg booking path.
    """
    # All 3 legs return proposals.
    # Each leg makes TWO search_catalog calls: Pass 1 (vibe-narrow) and
    # Pass 2 (city-wide) in _gather_candidates_for_dp — both passes happen
    # consecutively for each leg before moving to the next.  The city-wide
    # call returns the same hotel; hotel_id dedup suppresses duplicates in the
    # candidate set, so the booking outcome is unchanged.
    # Single-area cities (BKK, KL, SG) skip Pass 3 (cheapest-anchor sweep).
    acc_transport = SequencedMockTransport({
        "search_catalog": [
            _catalog_result([BKK_METRO]),   # Leg-0 Bangkok   — Pass 1 (vibe-narrow)
            _catalog_result([BKK_METRO]),   # Leg-0 Bangkok   — Pass 2 (city-wide, same hotel)
            _catalog_result([KL_BUKIT]),    # Leg-1 KL        — Pass 1 (vibe-narrow)
            _catalog_result([KL_BUKIT]),    # Leg-1 KL        — Pass 2 (city-wide, same hotel)
            _catalog_result([SG_MARINA]),   # Leg-2 Singapore — Pass 1 (vibe-narrow)
            _catalog_result([SG_MARINA]),   # Leg-2 Singapore — Pass 2 (city-wide, same hotel)
        ],
    })

    # Budget accepts immediately (total under 70000¢ in the mock)
    budget_transport = MockMerchantTransport({
        "create_checkout": (200, _BUDGET_CREATE_FULL),
        "complete_checkout": (200, _BUDGET_COMPLETE_FULL_ACCEPT),
    })

    # Critic mock: lookup_catalog re-prices each hotel and accepts
    # (prices must match mock accommodation proposals for Critic to pass)
    critic_lookup_responses = [
        # BKK_METRO lookup
        (200, _merchant_result({
            "total_cents": 21600,
            "nights": 3,
            "available": True,
            "source": "mock",
            "hotel": {"review_score": 8.5, "star_rating": 3.0},
        })),
        # KL_BUKIT lookup
        (200, _merchant_result({
            "total_cents": 16500,
            "nights": 3,
            "available": True,
            "source": "mock",
            "hotel": {"review_score": 8.3, "star_rating": 4.0},
        })),
        # SG_MARINA lookup
        (200, _merchant_result({
            "total_cents": 48000,
            "nights": 4,
            "available": True,
            "source": "mock",
            "hotel": {"review_score": 8.0, "star_rating": 5.0},
        })),
    ]
    critic_transport = SequencedMockTransport({
        "lookup_catalog": critic_lookup_responses,
    })

    planner_client = _make_planner_client()
    acc_client = _make_acc_client(acc_transport)
    budget_client = _make_budget_client(budget_transport)
    critic_client = _make_critic_client(critic_transport)

    # Use a higher budget so all 3 legs fit (BKK+KL+SG = 86100¢ total)
    trip_with_higher_budget = {**_S3_TRIP, "total_budget_cents": 100000}

    orchestrator = TravelOrchestrator(
        planner_client=planner_client,
        accommodation_client=acc_client,
        budget_client=budget_client,
        critic_client=critic_client,
    )

    result = orchestrator.negotiate(trip_with_higher_budget)

    assert result["outcome"] == "success", (
        f"P0 REGRESSION (happy path broken): expected 'success', got {result['outcome']!r}. "
        f"Full result:\n{json.dumps(result, indent=2, default=str)}"
    )
    assert result.get("booking_ref"), f"Expected booking_ref, got {result.get('booking_ref')!r}"

    # All 3 legs should be in the result
    booked_legs = result.get("legs", [])
    assert len(booked_legs) == 3, (
        f"Expected 3 booked legs, got {len(booked_legs)}: {booked_legs}"
    )

    print(
        f"PASS: test_full_fit_all_legs_returns_success "
        f"[booking_ref={result['booking_ref']!r} legs={len(booked_legs)}]"
    )


# ===========================================================================
# Test 4: Critic backstop — planned_leg_count=3 but only 1 assembled leg
# ===========================================================================

def test_critic_missing_leg_backstop() -> None:
    """
    P0 Layer 2: Critic MISSING_LEG backstop.

    When the Critic receives planned_leg_count=3 but only 1 assembled leg is
    passed, it must emit MISSING_LEG and return 'rejected' — even if the 1 leg
    is otherwise valid.

    This is the defense-in-depth backstop. The Orchestrator's all-or-none rule
    (Test 1) prevents this from happening normally, but the Critic fires it if
    called directly with a partial assembled list (regression protection).
    """
    # Critic merchant transport: only KL lookup is called (only 1 assembled leg)
    critic_transport = MockMerchantTransport({
        "lookup_catalog": (200, _merchant_result({
            "total_cents": 16500,
            "nights": 3,
            "available": True,
            "source": "mock",
            "hotel": {"review_score": 8.3, "star_rating": 4.0},
        })),
    })
    critic_client = _make_critic_client(critic_transport)

    # Call Critic directly with planned_leg_count=3 but only 1 assembled leg
    critic_payload = {
        "user_id": "test-u1",
        "total_budget_cents": 70000,
        "planned_leg_count": 3,     # ← 3 planned legs
        "legs": [
            # Only KL leg assembled (Bangkok and Singapore missing from packet)
            {
                "leg_id": "leg-1",
                "city": "kuala lumpur",
                "checkin": "2026-12-04",
                "checkout": "2026-12-07",
                "adults": 1,
                "hotel_id": "kl-bukit-bintang",
                "total_cents": 16500,
                "provenance": "merchant",
            },
        ],
    }

    task = _send_data(critic_client, critic_payload, "itinerary.verify")
    assert task["status"]["state"] == "completed", (
        f"Expected completed, got {task['status']['state']!r}: {task}"
    )
    result = _extract_data(task)

    # Must be rejected (not verified) — partial assembly
    assert result["decision"] == "rejected", (
        f"Critic backstop FAILED: got {result['decision']!r} instead of 'rejected'. "
        f"Critic must reject when assembled legs < planned_leg_count."
    )

    # Must include MISSING_LEG violation
    violations = result.get("violations", [])
    missing_leg_violations = [v for v in violations if v.get("code") == MISSING_LEG]
    assert missing_leg_violations, (
        f"Critic backstop FAILED: no MISSING_LEG violation in {violations!r}. "
        f"Expected MISSING_LEG when 1 assembled < 3 planned."
    )

    # MISSING_LEG must be routed to 'accommodation' (re-propose missing legs)
    for v in missing_leg_violations:
        assert v.get("route_to") == "accommodation", (
            f"MISSING_LEG violation should route to 'accommodation', got {v.get('route_to')!r}"
        )

    # Detail message must mention the count mismatch
    detail = missing_leg_violations[0].get("detail", "")
    assert "1" in detail and "3" in detail, (
        f"MISSING_LEG detail should mention 1 assembled and 3 planned, got: {detail!r}"
    )

    print(
        f"PASS: test_critic_missing_leg_backstop "
        f"[decision={result['decision']!r} "
        f"missing_leg_violations={len(missing_leg_violations)} "
        f"detail={detail[:80]!r}]"
    )


# ===========================================================================
# Test 5: Critic with planned_leg_count matching assembled count — no backstop violation
# ===========================================================================

def test_critic_planned_leg_count_matches_all_legs_no_violation() -> None:
    """
    When planned_leg_count equals the number of assembled legs, the Critic
    must NOT emit a count-mismatch MISSING_LEG.  Only 1 leg assembled AND
    planned_leg_count=1 → no count-mismatch violation.
    """
    critic_transport = MockMerchantTransport({
        "lookup_catalog": (200, _merchant_result({
            "total_cents": 16500,
            "nights": 3,
            "available": True,
            "source": "mock",
            "hotel": {"review_score": 8.3, "star_rating": 4.0},
        })),
    })
    critic_client = _make_critic_client(critic_transport)

    # 1 assembled leg, planned_leg_count=1 → counts match
    critic_payload = {
        "user_id": "test-u1",
        "total_budget_cents": 70000,
        "planned_leg_count": 1,   # ← matches assembled count
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "kuala lumpur",
                "checkin": "2026-12-04",
                "checkout": "2026-12-07",
                "adults": 1,
                "hotel_id": "kl-bukit-bintang",
                "total_cents": 16500,
                "provenance": "merchant",
            },
        ],
    }

    task = _send_data(critic_client, critic_payload, "itinerary.verify")
    assert task["status"]["state"] == "completed"
    result = _extract_data(task)

    # No count-mismatch MISSING_LEG violation (other violations possible but not this one)
    violations = result.get("violations", [])
    count_mismatch_violations = [
        v for v in violations
        if v.get("code") == MISSING_LEG and "planned" in v.get("detail", "").lower()
    ]
    assert not count_mismatch_violations, (
        f"Unexpected count-mismatch MISSING_LEG when planned=1 and assembled=1: "
        f"{count_mismatch_violations}"
    )

    print(
        f"PASS: test_critic_planned_leg_count_matches_all_legs_no_violation "
        f"[decision={result['decision']!r} violations={[v['code'] for v in violations]}]"
    )


# ===========================================================================
# Test 6: Critic without planned_leg_count — no backstop violation (backward compat)
# ===========================================================================

def test_critic_no_planned_leg_count_no_backstop_violation() -> None:
    """
    Critic called WITHOUT planned_leg_count (backward-compat path).
    No count-mismatch MISSING_LEG fires even if only 1 leg is assembled.
    The orchestrator fix (Layer 1) is the primary guard; planned_leg_count
    is optional for the Critic.
    """
    critic_transport = MockMerchantTransport({
        "lookup_catalog": (200, _merchant_result({
            "total_cents": 16500,
            "nights": 3,
            "available": True,
            "source": "mock",
            "hotel": {"review_score": 8.3, "star_rating": 4.0},
        })),
    })
    critic_client = _make_critic_client(critic_transport)

    # No planned_leg_count field
    critic_payload = {
        "user_id": "test-u1",
        "total_budget_cents": 70000,
        # "planned_leg_count": NOT PRESENT — backward compat
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "kuala lumpur",
                "checkin": "2026-12-04",
                "checkout": "2026-12-07",
                "adults": 1,
                "hotel_id": "kl-bukit-bintang",
                "total_cents": 16500,
                "provenance": "merchant",
            },
        ],
    }

    task = _send_data(critic_client, critic_payload, "itinerary.verify")
    assert task["status"]["state"] == "completed"
    result = _extract_data(task)

    # No count-mismatch MISSING_LEG (planned_leg_count was not supplied)
    violations = result.get("violations", [])
    count_mismatch = [
        v for v in violations
        if v.get("code") == MISSING_LEG and "planned" in v.get("detail", "").lower()
    ]
    assert not count_mismatch, (
        f"Count-mismatch MISSING_LEG fired without planned_leg_count — backward compat broken: "
        f"{count_mismatch}"
    )

    print(
        f"PASS: test_critic_no_planned_leg_count_no_backstop_violation "
        f"[decision={result['decision']!r} violations={[v['code'] for v in violations]}]"
    )


# ===========================================================================
# Test 7: Budget exhausted mid-trip — leg 0 fits, leg 1 cannot satisfy
# (first leg books, second leg no_fit because budget ceiling is exhausted)
# ===========================================================================

def test_budget_exhausted_second_leg_returns_cannot_satisfy() -> None:
    """
    GAP fill: "budget exhausted mid-trip" path.

    Scenario: 2-leg Bangkok → Singapore trip with a tiny budget (22000¢).
    - Leg-0 (Bangkok): hotel at 21600¢ fits under its per-leg ceiling.
    - Leg-1 (Singapore): hotel at 48000¢ far exceeds the remaining budget;
      accommodation search finds nothing under the leg-1 ceiling → no_fit.

    The orchestrator all-or-none rule must return cannot_satisfy (never
    book leg-0 alone).  The reason must reference the no_fit leg.

    This is distinct from test_partial_no_fit_one_leg (where leg-0 is the
    no_fit leg); here leg-0 succeeds but budget exhaustion blocks leg-1.
    """
    # Pass 1 + Pass 2 per leg (vibe-narrow + city-wide). Single-area cities skip Pass 3.
    acc_transport = SequencedMockTransport({
        "search_catalog": [
            _catalog_result([BKK_METRO]),   # Leg-0 Bangkok   — Pass 1 (vibe-narrow): fits
            _catalog_result([BKK_METRO]),   # Leg-0 Bangkok   — Pass 2 (city-wide)
            _catalog_empty(),               # Leg-1 Singapore  — Pass 1: nothing under ceiling
            _catalog_empty(),               # Leg-1 Singapore  — Pass 2: still nothing
        ],
    })

    # Budget must NOT be called (all-or-none: never commit partial)
    budget_transport = MockMerchantTransport({})

    planner_client = _make_planner_client()
    acc_client = _make_acc_client(acc_transport)
    budget_client = _make_budget_client(budget_transport)

    # Very tight budget: 22000¢ — Bangkok (21600¢) consumes almost all of it;
    # Singapore (48000¢) would need a ceiling of ~400¢ which finds nothing.
    tight_trip = {
        "user_id": "test-u1",
        "total_budget_cents": 22000,
        "legs": [
            {"city": "bangkok",   "checkin": "2026-12-01", "checkout": "2026-12-04",
             "adults": 1, "vibe": "city"},
            {"city": "singapore", "checkin": "2026-12-04", "checkout": "2026-12-07",
             "adults": 1, "vibe": "city"},
        ],
    }

    orchestrator = TravelOrchestrator(
        planner_client=planner_client,
        accommodation_client=acc_client,
        budget_client=budget_client,
    )

    result = orchestrator.negotiate(tight_trip)

    # PRIMARY: all-or-none → cannot_satisfy (never book leg-0 alone)
    assert result["outcome"] == "cannot_satisfy", (
        f"Budget-exhausted mid-trip must return cannot_satisfy (all-or-none); "
        f"got {result['outcome']!r}\n{json.dumps(result, indent=2, default=str)[:500]}"
    )

    # No booking_ref — no partial booking committed
    assert not result.get("booking_ref"), (
        f"No booking_ref allowed on budget-exhausted cannot_satisfy; "
        f"got {result.get('booking_ref')!r}"
    )

    # Reason must be present
    reason = result.get("reason", "")
    assert reason, "cannot_satisfy must include a reason describing the failure"

    # Negotiation log must be present (transparency)
    assert "negotiation_log" in result

    print(
        f"PASS: test_budget_exhausted_second_leg_returns_cannot_satisfy "
        f"[outcome={result['outcome']!r} reason={reason[:80]!r}]"
    )


# ===========================================================================
# Hardening pass — orchestrator-side cross-file contracts (D1, D3, D7, #15,
# #25, #26, #42, #63). These exercise the orchestrator's OWN side of each
# contract in isolation (no dependency on a parallel agent's fix).
# ===========================================================================

import pytest  # noqa: E402

from orchestration import orchestrator as orch_mod  # noqa: E402
from orchestration.orchestrator import _extract_task_data  # noqa: E402


def _task(state: str, *, data: dict | None = None, error: str | None = None) -> dict:
    """Build a minimal A2A Task envelope for the #42 state-contract tests."""
    t: dict[str, Any] = {"status": {"state": state}, "artifacts": []}
    if data is not None:
        t["artifacts"] = [{"parts": [{"kind": "data", "data": data}]}]
    if error is not None:
        t["metadata"] = {"error": error}
    return t


# --- #42: authoritative task-state success contract --------------------------

def test_task_state_completed_is_consumable() -> None:
    """completed → data part returned (normal success)."""
    out = _extract_task_data(_task("completed", data={"ok": True}), "x.skill")
    assert out == {"ok": True}


def test_task_state_input_required_is_consumable() -> None:
    """input-required → the budget consent HALT still carries a consumable
    data artifact the orchestrator handles downstream."""
    out = _extract_task_data(
        _task("input-required", data={"decision": "needs_consent"}), "budget.check"
    )
    assert out == {"decision": "needs_consent"}


@pytest.mark.parametrize("state", ["failed", "rejected", "canceled", "auth-required",
                                   "working", "submitted", ""])
def test_task_state_halted_or_failed_raises(state: str) -> None:
    """#42 — any state other than completed/input-required is a HARD STOP, even
    when a data artifact is present (state must not be trusted as success)."""
    with pytest.raises(RuntimeError):
        _extract_task_data(_task(state, data={"decision": "accept"}), "x.skill")


# --- D1: gate exception → conservative BLOCK (not None) ----------------------

class _RaisingOrch(TravelOrchestrator):
    """Stub whose agent calls always raise, to exercise the D1 fail-conservative
    branch without standing up real agents."""

    def _call_health(self, payload: dict) -> dict | None:  # type: ignore[override]
        raise RuntimeError("boom-health")

    def _call_compliance(self, payload: dict) -> dict | None:  # type: ignore[override]
        raise RuntimeError("boom-compliance")

    def _call_fraud(self, payload: dict) -> dict | None:  # type: ignore[override]
        raise RuntimeError("boom-fraud")


def test_health_gate_exception_returns_conservative_block() -> None:
    o = _RaisingOrch(health_client=object())  # truthy client → gate is wired
    o._today = "2026-06-19"  # D7: run-frozen today
    verdict = o._run_health_gate(
        {"today": "2026-06-19"},
        [{"place_key": "bali", "checkin": "2026-12-01"}],
    )
    assert verdict is not None, "D1: a carried health condition must NOT degrade to None"
    assert verdict.get("bookable") is False
    assert verdict.get("flag") is True
    assert "conservative block" in verdict.get("advisory", "")


def test_compliance_gate_exception_returns_conservative_block() -> None:
    o = _RaisingOrch(compliance_client=object())
    o._today = "2026-06-19"  # D7: run-frozen today
    verdict = o._run_compliance_gate(
        {"nationality": "US", "today": "2026-06-19"},
        [{"dest_country": "ID", "checkin": "2026-12-01"}],
        None,
    )
    assert verdict is not None
    assert verdict.get("bookable") is False
    assert verdict.get("flag") is True


def test_fraud_gate_exception_returns_non_committable_rollup() -> None:
    o = _RaisingOrch(fraud_client=object())
    verdict = o._run_fraud_gate(
        {"counterparties": [{"counterparty_id": "ota-x", "kind": "ota"}]}
    )
    assert verdict is not None
    assert verdict.get("rollup", {}).get("all_committable") is False
    assert "ota-x" in (verdict.get("rollup", {}).get("blocked_ids") or [])


def test_gate_not_applicable_still_returns_none() -> None:
    """No condition present → None (genuine pass-through), even on a raising stub."""
    o = _RaisingOrch(health_client=object(), compliance_client=object(),
                     fraud_client=object())
    assert o._run_health_gate({}, [{"city": "bali"}]) is None  # no place_key
    assert o._run_compliance_gate({}, [{"city": "bali"}], None) is None  # no dest_country
    assert o._run_fraud_gate({}) is None  # no counterparties


# --- #63: route summary preserves the -1 (unknown) sentinel -----------------

def test_route_summary_distinguishes_unknown_from_same_area() -> None:
    o = TravelOrchestrator()
    transport_result = {
        "infeasible_edges": [],
        "edges": [
            {"from_city": "a", "to_city": "b", "mode": "flight",
             "transfer_minutes": -1, "feasible": True, "to_leg": "leg-1"},
            {"from_city": "b", "to_city": "c", "mode": "walk",
             "transfer_minutes": 0, "feasible": True, "to_leg": "leg-2"},
            {"from_city": "c", "to_city": "d", "mode": "flight",
             "transfer_minutes": 125, "feasible": True, "to_leg": "leg-3"},
        ],
    }
    result = o._success_result(
        user_id="u", total_budget_cents=1000,
        budget_result={"total_cents": 1000, "booking_ref": "BK", "checkout_id": "co"},
        proposals={}, leg_meta={}, negotiation_log=[], rounds=1,
        critic_result=None, transport_result=transport_result,
    )
    route = result.get("route", [])
    assert any("~unknown" in hop for hop in route), "transfer -1 → ~unknown"
    assert any("same-area" in hop for hop in route), "transfer 0 → same-area (distinct)"
    assert any("~2h05m" in hop for hop in route), "positive minutes → duration"


# --- #26: commit exception → honest needs-reconciliation terminal -----------

def test_do_commit_exception_converts_to_typed_terminal() -> None:
    class _CommitRaises(TravelOrchestrator):
        def _call_budget_commit(self, payload: dict) -> dict:  # type: ignore[override]
            raise RuntimeError("merchant 5xx at commit")

    o = _CommitRaises()
    res = o._do_commit(user_id="u", checkout_id="co-1", idempotency_key="trip-1")
    assert res["decision"] == "commit_failed"
    assert res["idempotency_key"] == "trip-1"
    assert res["checkout_id"] == "co-1"

    out = TravelOrchestrator._commit_failed_result(
        budget_result=res, idempotency_key="trip-1",
        closest_total=999, negotiation_log=[], log_entry={},
    )
    assert out["outcome"] == "cannot_satisfy"
    assert out["needs_reconciliation"] is True
    assert out["idempotency_key"] == "trip-1"


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    tests = [
        test_partial_no_fit_one_leg_returns_cannot_satisfy,
        test_partial_no_fit_all_legs_returns_cannot_satisfy,
        test_full_fit_all_legs_returns_success,
        test_critic_missing_leg_backstop,
        test_critic_planned_leg_count_matches_all_legs_no_violation,
        test_critic_no_planned_leg_count_no_backstop_violation,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            print(f"FAIL: {test_fn.__name__}: {exc}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
