"""
test_critic.py — Unit tests for M3a Critic/Verifier agent (Travel Guild).

CI-safe: all merchant HTTP calls intercepted with mock transport (lookup_catalog).
         Uses Starlette in-process ASGI TestClient throughout.

Design contract: the internal design spec §3.6, §2, §4.1–4.3, §4.6.

Coverage:
  1. test_critic_verified_happy_path       — good package → "verified"
  2. test_critic_missing_leg              — hotel_id absent → MISSING_LEG rejected → accommodation
  3. test_critic_price_mismatch           — proposal cents ≠ merchant cents → PRICE_MISMATCH rejected → accommodation
  4. test_critic_non_contiguous_dates     — gap between legs → DATE_GAP rejected → planner
  5. test_critic_over_budget_sum          — re-verified total > budget → OVER_BUDGET rejected → budget
  6. test_critic_missing_provenance       — no provenance tag → MISSING_PROVENANCE rejected → planner
  7. test_critic_invalid_date_range       — checkin >= checkout → INVALID_DATE rejected → planner
  8. test_critic_quality_score_range      — quality_score in [0.0, 1.0] on verified result
  9. test_critic_overlap_dates            — leg dates overlap → DATE_OVERLAP rejected → planner
 10. test_critic_transport_checked_flag   — transport_checked always False (M3b hook)
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from agents import critic_agent as ca_mod
from agents.critic_agent import (
    MISSING_LEG,
    PRICE_MISMATCH,
    UNAVAILABLE,
    OVER_CAPACITY,
    DUPLICATE_LEG,
    INVALID_DATE,
    DATE_GAP,
    DATE_OVERLAP,
    OVER_BUDGET,
    MISSING_PROVENANCE,
)


# ===========================================================================
# Mock transport infrastructure
# ===========================================================================

class MockMerchantTransport(httpx.BaseTransport):
    """
    Intercepts merchant MCP calls; returns canned responses keyed by tool name.
    Matches the pattern used in test_negotiation.py.
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
    Returns different responses per call (queue per tool).
    Used to return different lookup results for leg-0 and leg-1.
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
            idx = len(seq) - 1
        self._call_counts[tool_name] = idx + 1
        status_code, body = seq[idx]
        return httpx.Response(status_code, json=body)


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


def _lookup_ok(
    hotel_id: str,
    total_cents: int,
    review_score: float = 8.5,
    star_rating: float = 4.0,
    max_occupancy: int = 4,
) -> tuple[int, dict[str, Any]]:
    """Return a successful lookup_catalog mock response."""
    return (200, _merchant_result({
        "hotel": {
            "id": hotel_id,
            "title": f"Mock Hotel {hotel_id}",
            "review_score": review_score,
            "star_rating": star_rating,
            "max_occupancy": max_occupancy,
            "amenities": ["pool", "wifi"],
        },
        "available": True,
        "total_cents": total_cents,
        "nights": 3,
        "source": "mock",
    }))


def _lookup_not_found(hotel_id: str) -> tuple[int, dict[str, Any]]:
    """Return a 404 lookup_catalog mock response."""
    return (404, _merchant_result({
        "error": "not_found",
        "hotel_id": hotel_id,
    }))


def _lookup_unavailable(hotel_id: str) -> tuple[int, dict[str, Any]]:
    """
    Return a sold-out lookup_catalog mock response — mirrors the merchant
    (catalog.go:131): HTTP 409 / structuredContent status=="unavailable",
    NO total_cents.
    """
    return (409, _merchant_result({
        "status": "unavailable",
        "reason": "hotel_sold_out",
        "hotel_id": hotel_id,
        "title": f"Mock Hotel {hotel_id}",
        "source": "mock",
    }))


# ===========================================================================
# Helper — build TestClient
# ===========================================================================

def _make_critic_client(
    transport: httpx.BaseTransport | None = None,
) -> tuple[TestClient, ca_mod.CriticAgent]:
    agent = ca_mod.CriticAgent(merchant_transport=transport)
    client = TestClient(agent.build_app(), raise_server_exceptions=True)
    return client, agent


def _rpc_post(client: TestClient, method: str, params: dict) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    return resp.json()


def _send_verify(client: TestClient, payload: dict) -> dict:
    """Send an itinerary.verify message; return the Task dict."""
    msg = {
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "data", "data": payload}],
        "metadata": {"skillId": "itinerary.verify"},
    }
    rpc = _rpc_post(client, "message/send", {"message": msg})
    assert "error" not in rpc, f"RPC error: {rpc.get('error')}"
    return rpc["result"]


def _extract_critic_result(task: dict) -> dict:
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "data":
                return part["data"]
    raise AssertionError(f"No data part in task: {task}")


# ===========================================================================
# Shared itinerary fixtures
# ===========================================================================

# A well-formed two-leg Bali itinerary where merchant confirms the prices.
GOOD_ITINERARY = {
    "user_id": "u-test",
    "total_budget_cents": 80000,
    "legs": [
        {
            "leg_id": "leg-0",
            "city": "bali",
            "checkin": "2025-10-01",
            "checkout": "2025-10-04",   # 3 nights
            "adults": 1,
            "hotel_id": "bali-alaya-ubud",
            "total_cents": 49500,       # 3 × 16500 — matches mock merchant
            "provenance": "live",
        },
        {
            "leg_id": "leg-1",
            "city": "bali",
            "checkin": "2025-10-04",    # contiguous with leg-0 checkout
            "checkout": "2025-10-08",   # 4 nights
            "adults": 1,
            "hotel_id": "bali-sanur-puri",
            "total_cents": 27200,       # 4 × 6800 — matches mock merchant
            "provenance": "cache",
        },
    ],
}

# Merchant responses matching GOOD_ITINERARY exactly
GOOD_LOOKUPS = SequencedMockTransport({
    "lookup_catalog": [
        _lookup_ok("bali-alaya-ubud", 49500, review_score=8.9),
        _lookup_ok("bali-sanur-puri", 27200, review_score=8.0),
    ],
})


# ===========================================================================
# Tests
# ===========================================================================

def test_critic_verified_happy_path() -> None:
    """Good package with all constraints satisfied → decision='verified'."""
    client, _ = _make_critic_client(transport=GOOD_LOOKUPS)
    task = _send_verify(client, GOOD_ITINERARY)

    assert task["status"]["state"] == "completed", (
        f"Expected completed, got {task['status']['state']!r}"
    )
    result = _extract_critic_result(task)

    assert result["decision"] == "verified", (
        f"Expected verified: {json.dumps(result, indent=2)}"
    )
    assert result["violations"] == [], f"Expected no violations: {result['violations']}"
    assert result["provenance"] == "merchant"
    assert result["transport_checked"] is False   # M3b hook
    assert result["reverified_total_cents"] == 49500 + 27200  # 76700
    assert 0.0 <= result["quality_score"] <= 1.0

    print(
        f"PASS: test_critic_verified_happy_path "
        f"[quality={result['quality_score']:.3f} "
        f"reverified={result['reverified_total_cents']}¢]"
    )


def test_critic_missing_leg() -> None:
    """
    A leg without hotel_id → MISSING_LEG violation → rejected → route_to=accommodation.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-04",
                "adults": 1,
                # hotel_id intentionally absent
                "total_cents": 49500,
                "provenance": "live",
            },
        ],
    }
    # lookup_catalog won't be called for missing hotel_id
    transport = MockMerchantTransport({})
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)

    assert task["status"]["state"] == "completed"
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected", f"Expected rejected: {result}"
    codes = [v["code"] for v in result["violations"]]
    assert MISSING_LEG in codes, f"Expected MISSING_LEG in {codes}"
    missing_v = next(v for v in result["violations"] if v["code"] == MISSING_LEG)
    assert missing_v["route_to"] == "accommodation", (
        f"Expected route_to=accommodation: {missing_v}"
    )

    print(
        f"PASS: test_critic_missing_leg "
        f"[violations={codes}]"
    )


def test_critic_price_mismatch() -> None:
    """
    Proposal says 99999¢ but merchant returns 49500¢ →
    PRICE_MISMATCH violation → rejected → route_to=accommodation.

    This is the anti-hallucination teeth (§3.6).
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 200000,  # budget not the issue here
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-04",
                "adults": 1,
                "hotel_id": "bali-alaya-ubud",
                "total_cents": 99999,   # TAMPERED — real merchant price is 49500
                "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-alaya-ubud", 49500, review_score=8.9),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)

    assert task["status"]["state"] == "completed"
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected", f"Expected rejected: {result}"
    codes = [v["code"] for v in result["violations"]]
    assert PRICE_MISMATCH in codes, f"Expected PRICE_MISMATCH in {codes}"
    pm_v = next(v for v in result["violations"] if v["code"] == PRICE_MISMATCH)
    assert pm_v["route_to"] == "accommodation", (
        f"Expected route_to=accommodation: {pm_v}"
    )
    # The detail must mention both prices so the violation is auditable
    assert "99999" in pm_v["detail"], f"Detail should mention proposal cents: {pm_v['detail']}"
    assert "49500" in pm_v["detail"], f"Detail should mention merchant cents: {pm_v['detail']}"

    print(
        f"PASS: test_critic_price_mismatch "
        f"[detail={pm_v['detail'][:80]!r}]"
    )


def test_critic_non_contiguous_dates() -> None:
    """
    Leg-0 checkout=2025-10-04, Leg-1 checkin=2025-10-06 → 2-day gap →
    DATE_GAP violation → rejected → route_to=planner.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-04",
                "adults": 1,
                "hotel_id": "bali-alaya-ubud",
                "total_cents": 49500,
                "provenance": "live",
            },
            {
                "leg_id": "leg-1",
                "city": "bali",
                "checkin": "2025-10-06",    # GAP: 2 days after leg-0 checkout
                "checkout": "2025-10-10",
                "adults": 1,
                "hotel_id": "bali-sanur-puri",
                "total_cents": 27200,
                "provenance": "cache",
            },
        ],
    }
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("bali-alaya-ubud", 49500),
            _lookup_ok("bali-sanur-puri", 27200),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)

    assert task["status"]["state"] == "completed"
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected", f"Expected rejected: {result}"
    codes = [v["code"] for v in result["violations"]]
    assert DATE_GAP in codes, f"Expected DATE_GAP in {codes}"
    gap_v = next(v for v in result["violations"] if v["code"] == DATE_GAP)
    assert gap_v["route_to"] == "planner", f"Expected route_to=planner: {gap_v}"
    assert gap_v["leg_id"] == "leg-1"

    print(
        f"PASS: test_critic_non_contiguous_dates "
        f"[leg_id={gap_v['leg_id']} detail={gap_v['detail'][:60]!r}]"
    )


def test_critic_buffer_day_gap_accepted() -> None:
    """
    #51/BUG7 — long-haul structural-deadlock fix. The SAME 1-day gap shape as
    test_critic_non_contiguous_dates, but this time transport_result carries a
    requires_buffer_day=True edge for exactly this leg pair (a known long-haul
    transfer, mirroring the A14 round-the-world archetype: tokyo->singapore
    checkout/checkin 1 day apart). The gap must be ACCEPTED — no DATE_GAP — because
    it is the transport rule's OWN required buffer day, not an unexplained gap.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-04",
                "adults": 1,
                "hotel_id": "bali-alaya-ubud",
                "total_cents": 49500,
                "provenance": "live",
            },
            {
                "leg_id": "leg-1",
                "city": "bali",
                "checkin": "2025-10-06",    # SAME 1-day-plus gap shape as the DATE_GAP test
                "checkout": "2025-10-10",
                "adults": 1,
                "hotel_id": "bali-sanur-puri",
                "total_cents": 27200,
                "provenance": "cache",
            },
        ],
        "transport_result": {
            "edges": [
                {
                    "from_leg": "leg-0", "to_leg": "leg-1",
                    "from_city": "bali", "to_city": "bali",
                    "feasible": True, "mode": "flight",
                    "transfer_minutes": 895,
                    "requires_buffer_day": True,
                }
            ],
            "infeasible_edges": [],
        },
    }
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("bali-alaya-ubud", 49500),
            _lookup_ok("bali-sanur-puri", 27200),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)

    assert task["status"]["state"] == "completed"
    result = _extract_critic_result(task)

    codes = [v["code"] for v in result["violations"]]
    assert DATE_GAP not in codes, f"Buffer-day gap must NOT be DATE_GAP: {codes} / {result}"
    assert result["decision"] == "verified", f"Expected verified: {result}"

    print("PASS: test_critic_buffer_day_gap_accepted")


def test_critic_unexplained_gap_still_rejected_even_with_transport_result() -> None:
    """
    #51/BUG7 — the exemption is NARROW: a transport_result that does NOT carry
    requires_buffer_day for this exact leg pair must still reject an unexplained
    gap as DATE_GAP (the fix must not accidentally waive every gap once any
    transport_result is present)."""
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-04",
                "adults": 1,
                "hotel_id": "bali-alaya-ubud",
                "total_cents": 49500,
                "provenance": "live",
            },
            {
                "leg_id": "leg-1",
                "city": "bali",
                "checkin": "2025-10-06",
                "checkout": "2025-10-10",
                "adults": 1,
                "hotel_id": "bali-sanur-puri",
                "total_cents": 27200,
                "provenance": "cache",
            },
        ],
        "transport_result": {
            "edges": [
                {
                    "from_leg": "leg-0", "to_leg": "leg-1",
                    "from_city": "bali", "to_city": "bali",
                    "feasible": True, "mode": "same_area",
                    "transfer_minutes": 30,
                    # no requires_buffer_day — this gap is unexplained
                }
            ],
            "infeasible_edges": [],
        },
    }
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("bali-alaya-ubud", 49500),
            _lookup_ok("bali-sanur-puri", 27200),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    codes = [v["code"] for v in result["violations"]]
    assert DATE_GAP in codes, f"Unexplained gap must still be DATE_GAP: {codes}"

    print("PASS: test_critic_unexplained_gap_still_rejected_even_with_transport_result")


def test_critic_over_budget_sum() -> None:
    """
    Re-verified totals sum to more than total_budget_cents →
    OVER_BUDGET violation → rejected → route_to=budget.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 10000,    # Intentionally tiny budget
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-04",
                "adults": 1,
                "hotel_id": "bali-alaya-ubud",
                "total_cents": 49500,   # Correct price — but over budget
                "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-alaya-ubud", 49500),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)

    assert task["status"]["state"] == "completed"
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected", f"Expected rejected: {result}"
    codes = [v["code"] for v in result["violations"]]
    assert OVER_BUDGET in codes, f"Expected OVER_BUDGET in {codes}"
    ob_v = next(v for v in result["violations"] if v["code"] == OVER_BUDGET)
    assert ob_v["route_to"] == "budget", f"Expected route_to=budget: {ob_v}"
    assert ob_v["leg_id"] is None  # Budget violation is package-level

    print(
        f"PASS: test_critic_over_budget_sum "
        f"[reverified={result['reverified_total_cents']}¢ budget=10000¢]"
    )


def test_critic_missing_provenance() -> None:
    """
    Leg has no provenance tag (fabricated data risk) →
    MISSING_PROVENANCE violation → rejected → route_to=planner.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-04",
                "adults": 1,
                "hotel_id": "bali-alaya-ubud",
                "total_cents": 49500,
                # provenance intentionally absent
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-alaya-ubud", 49500),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)

    assert task["status"]["state"] == "completed"
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected", f"Expected rejected: {result}"
    codes = [v["code"] for v in result["violations"]]
    assert MISSING_PROVENANCE in codes, f"Expected MISSING_PROVENANCE in {codes}"
    mp_v = next(v for v in result["violations"] if v["code"] == MISSING_PROVENANCE)
    assert mp_v["route_to"] == "planner", f"Expected route_to=planner: {mp_v}"

    print(
        f"PASS: test_critic_missing_provenance "
        f"[leg_id={mp_v['leg_id']}]"
    )


def test_critic_invalid_date_range() -> None:
    """
    checkin >= checkout on a leg → INVALID_DATE violation → rejected → route_to=planner.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-04",    # checkin AFTER checkout
                "checkout": "2025-10-01",
                "adults": 1,
                "hotel_id": "bali-alaya-ubud",
                "total_cents": 49500,
                "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-alaya-ubud", 49500),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)

    assert task["status"]["state"] == "completed"
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected", f"Expected rejected: {result}"
    codes = [v["code"] for v in result["violations"]]
    assert INVALID_DATE in codes, f"Expected INVALID_DATE in {codes}"
    iv_v = next(v for v in result["violations"] if v["code"] == INVALID_DATE)
    assert iv_v["route_to"] == "planner"

    print(
        f"PASS: test_critic_invalid_date_range "
        f"[detail={iv_v['detail'][:60]!r}]"
    )


def test_critic_quality_score_range() -> None:
    """
    Quality score is always in [0.0, 1.0] on a verified result.
    (Also serves as a second verified-pass assertion.)
    """
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("bali-alaya-ubud", 49500, review_score=8.9),
            _lookup_ok("bali-sanur-puri", 27200, review_score=8.0),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, GOOD_ITINERARY)
    result = _extract_critic_result(task)

    assert result["decision"] == "verified", f"Expected verified: {result}"
    qs = result["quality_score"]
    assert isinstance(qs, (int, float)), f"quality_score must be numeric: {qs!r}"
    assert 0.0 <= float(qs) <= 1.0, f"quality_score out of range: {qs}"

    print(f"PASS: test_critic_quality_score_range [quality_score={qs}]")


def test_critic_overlap_dates() -> None:
    """
    Leg-0 checkout=2025-10-06, Leg-1 checkin=2025-10-04 → 2-day overlap →
    DATE_OVERLAP violation → rejected → route_to=planner.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-06",
                "adults": 1,
                "hotel_id": "bali-alaya-ubud",
                "total_cents": 82500,   # 5 × 16500
                "provenance": "live",
            },
            {
                "leg_id": "leg-1",
                "city": "bali",
                "checkin": "2025-10-04",   # OVERLAP: 2 days before leg-0 checkout
                "checkout": "2025-10-08",
                "adults": 1,
                "hotel_id": "bali-sanur-puri",
                "total_cents": 27200,
                "provenance": "cache",
            },
        ],
    }
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("bali-alaya-ubud", 82500),
            _lookup_ok("bali-sanur-puri", 27200),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)

    assert task["status"]["state"] == "completed"
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected", f"Expected rejected: {result}"
    codes = [v["code"] for v in result["violations"]]
    assert DATE_OVERLAP in codes, f"Expected DATE_OVERLAP in {codes}"
    ov_v = next(v for v in result["violations"] if v["code"] == DATE_OVERLAP)
    assert ov_v["route_to"] == "planner"
    assert ov_v["leg_id"] == "leg-1"

    print(
        f"PASS: test_critic_overlap_dates "
        f"[detail={ov_v['detail'][:60]!r}]"
    )


def test_critic_transport_checked_flag() -> None:
    """
    transport_checked must always be False — M3b hook, not yet implemented.
    """
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("bali-alaya-ubud", 49500, review_score=8.9),
            _lookup_ok("bali-sanur-puri", 27200, review_score=8.0),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, GOOD_ITINERARY)
    result = _extract_critic_result(task)

    assert result["transport_checked"] is False, (
        f"transport_checked must be False (M3b hook): {result['transport_checked']!r}"
    )
    print("PASS: test_critic_transport_checked_flag")


# ===========================================================================
# Hardening pass — D4 #4/#8/#7/#48/#49 (critic) regression tests
# ===========================================================================

def test_critic_empty_itinerary_rejected() -> None:
    """
    D4 #4: An itinerary with ZERO legs must NEVER verify — emits MISSING_LEG
    (route_to=accommodation) UNCONDITIONALLY, regardless of planned_leg_count.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [],
    }
    transport = MockMerchantTransport({})
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)

    assert task["status"]["state"] == "completed"
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected", f"Empty itinerary must reject: {result}"
    codes = [v["code"] for v in result["violations"]]
    assert MISSING_LEG in codes, f"Expected MISSING_LEG for empty itinerary: {codes}"
    ml = next(v for v in result["violations"] if v["code"] == MISSING_LEG)
    assert ml["route_to"] == "accommodation"
    assert ml["leg_id"] is None
    print("PASS: test_critic_empty_itinerary_rejected")


def test_critic_empty_itinerary_with_planned_count_zero() -> None:
    """
    D4 #4: The backstop cannot fire on the empty case (len([]) < 0 is False, and
    planned_leg_count==0). The unconditional guard must still reject.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "planned_leg_count": 0,
        "legs": [],
    }
    client, _ = _make_critic_client(transport=MockMerchantTransport({}))
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected", (
        f"Empty itinerary with planned_leg_count=0 must reject: {result}"
    )
    assert MISSING_LEG in [v["code"] for v in result["violations"]]
    print("PASS: test_critic_empty_itinerary_with_planned_count_zero")


def test_critic_budget_sum_over_legs_list_not_dict() -> None:
    """
    D4 #8: Two real legs sharing a leg_id (or both missing it) must NOT collapse
    in the budget sum. Two 50000¢ legs vs a 60000¢ budget → 100000¢ total →
    OVER_BUDGET (a dict keyed by leg_id would hold only one and pass).
    Duplicate leg_ids are additionally flagged as DUPLICATE_LEG.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 60000,
        "legs": [
            {
                "leg_id": "leg-dup",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-04",
                "adults": 1,
                "hotel_id": "bali-alaya-ubud",
                "total_cents": 50000,
                "provenance": "live",
            },
            {
                "leg_id": "leg-dup",   # SAME leg_id — would collapse in a dict
                "city": "bali",
                "checkin": "2025-10-04",
                "checkout": "2025-10-07",
                "adults": 1,
                "hotel_id": "bali-sanur-puri",
                "total_cents": 50000,
                "provenance": "cache",
            },
        ],
    }
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("bali-alaya-ubud", 50000),
            _lookup_ok("bali-sanur-puri", 50000),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["reverified_total_cents"] == 100000, (
        f"Both legs must count toward the sum: {result['reverified_total_cents']}"
    )
    codes = [v["code"] for v in result["violations"]]
    assert OVER_BUDGET in codes, f"Expected OVER_BUDGET (100000 > 60000): {codes}"
    assert DUPLICATE_LEG in codes, f"Expected DUPLICATE_LEG: {codes}"
    assert result["decision"] == "rejected"
    dup = next(v for v in result["violations"] if v["code"] == DUPLICATE_LEG)
    assert dup["route_to"] == "planner"
    print("PASS: test_critic_budget_sum_over_legs_list_not_dict")


def test_critic_missing_leg_ids_dont_collapse_budget() -> None:
    """
    D4 #8: Two legs BOTH missing leg_id (default "(unknown)") must still sum
    independently toward the budget rather than collapsing to one cost.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 60000,
        "legs": [
            {
                "city": "bali", "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-alaya-ubud",
                "total_cents": 50000, "provenance": "live",
            },
            {
                "city": "bali", "checkin": "2025-10-04", "checkout": "2025-10-07",
                "adults": 1, "hotel_id": "bali-sanur-puri",
                "total_cents": 50000, "provenance": "cache",
            },
        ],
    }
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("bali-alaya-ubud", 50000),
            _lookup_ok("bali-sanur-puri", 50000),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["reverified_total_cents"] == 100000, (
        f"Both missing-id legs must count: {result['reverified_total_cents']}"
    )
    assert OVER_BUDGET in [v["code"] for v in result["violations"]]
    print("PASS: test_critic_missing_leg_ids_dont_collapse_budget")


def test_critic_over_capacity_rejected() -> None:
    """
    D4 #7: adults > hotel.max_occupancy → OVER_CAPACITY (route_to=accommodation),
    mirroring the merchant's commit-time exceeds_room_capacity check.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-04",
                "adults": 5,   # exceeds max_occupancy=4 below
                "hotel_id": "bali-alaya-ubud",
                "total_cents": 49500,
                "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-alaya-ubud", 49500, max_occupancy=4),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected", f"Over-capacity must reject: {result}"
    codes = [v["code"] for v in result["violations"]]
    assert OVER_CAPACITY in codes, f"Expected OVER_CAPACITY: {codes}"
    oc = next(v for v in result["violations"] if v["code"] == OVER_CAPACITY)
    assert oc["route_to"] == "accommodation"
    assert oc["leg_id"] == "leg-0"
    print("PASS: test_critic_over_capacity_rejected")


def test_critic_capacity_ok_when_within_max_occupancy() -> None:
    """D4 #7: adults <= max_occupancy → no OVER_CAPACITY violation."""
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 4, "hotel_id": "bali-alaya-ubud",
                "total_cents": 49500, "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-alaya-ubud", 49500, max_occupancy=4),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "verified", f"Expected verified: {result}"
    assert OVER_CAPACITY not in [v["code"] for v in result["violations"]]
    print("PASS: test_critic_capacity_ok_when_within_max_occupancy")


def test_critic_sold_out_is_unavailable_not_price_mismatch() -> None:
    """
    D3/D4 #48: a sold-out hotel (HTTP 409 / status=="unavailable") is an
    AVAILABILITY violation (UNAVAILABLE, route_to=accommodation), NOT a
    PRICE_MISMATCH.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-alaya-ubud",
                "total_cents": 49500, "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_unavailable("bali-alaya-ubud"),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected", f"Sold-out must reject: {result}"
    codes = [v["code"] for v in result["violations"]]
    assert UNAVAILABLE in codes, f"Expected UNAVAILABLE: {codes}"
    assert PRICE_MISMATCH not in codes, (
        f"Sold-out must NOT be PRICE_MISMATCH: {codes}"
    )
    uv = next(v for v in result["violations"] if v["code"] == UNAVAILABLE)
    assert uv["route_to"] == "accommodation"
    assert uv["leg_id"] == "leg-0"
    print("PASS: test_critic_sold_out_is_unavailable_not_price_mismatch")


def test_critic_transport_checked_only_on_positive_signal() -> None:
    """
    D4 #49: an empty/keyless transport_result {} must NOT flip transport_checked
    to True. A positive signal (checked / feasible / edges / explicit empty
    infeasible_edges) is required.
    """
    base = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-alaya-ubud",
                "total_cents": 49500, "provenance": "live",
            },
        ],
    }

    def _run(transport_result: dict) -> dict:
        transport = MockMerchantTransport({
            "lookup_catalog": _lookup_ok("bali-alaya-ubud", 49500),
        })
        client, _ = _make_critic_client(transport=transport)
        payload = {**base, "transport_result": transport_result}
        task = _send_verify(client, payload)
        return _extract_critic_result(task)

    # Empty/keyless {} → no positive signal → False
    r_empty = _run({})
    assert r_empty["transport_checked"] is False, (
        f"Empty transport_result must NOT set transport_checked: {r_empty}"
    )

    # Explicit empty infeasible_edges (key present → examined) → True
    r_examined = _run({"infeasible_edges": []})
    assert r_examined["transport_checked"] is True, (
        f"Explicit empty infeasible_edges should set transport_checked: {r_examined}"
    )

    # Explicit checked flag → True
    r_flag = _run({"checked": True})
    assert r_flag["transport_checked"] is True, r_flag

    print("PASS: test_critic_transport_checked_only_on_positive_signal")


# ===========================================================================
# Critic Agent Card smoke-test
# ===========================================================================

def test_critic_agent_card() -> None:
    """Agent Card served at /.well-known/agent-card.json with correct fields."""
    client, _ = _make_critic_client(transport=MockMerchantTransport({}))
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()

    assert card["name"] == "critic-agent"
    skill_ids = [s["id"] for s in card.get("skills", [])]
    assert "itinerary.verify" in skill_ids, f"Expected itinerary.verify skill: {skill_ids}"
    assert card["protocolVersion"] == "0.3.0"

    print(
        f"PASS: test_critic_agent_card "
        f"[skills={skill_ids}]"
    )


# ===========================================================================
# Test runner
# ===========================================================================

TESTS = [
    test_critic_verified_happy_path,
    test_critic_missing_leg,
    test_critic_price_mismatch,
    test_critic_non_contiguous_dates,
    test_critic_over_budget_sum,
    test_critic_missing_provenance,
    test_critic_invalid_date_range,
    test_critic_quality_score_range,
    test_critic_overlap_dates,
    test_critic_transport_checked_flag,
    test_critic_empty_itinerary_rejected,
    test_critic_empty_itinerary_with_planned_count_zero,
    test_critic_budget_sum_over_legs_list_not_dict,
    test_critic_missing_leg_ids_dont_collapse_budget,
    test_critic_over_capacity_rejected,
    test_critic_capacity_ok_when_within_max_occupancy,
    test_critic_sold_out_is_unavailable_not_price_mismatch,
    test_critic_transport_checked_only_on_positive_signal,
    test_critic_agent_card,
]


def main() -> None:
    import traceback

    passed = 0
    failed = 0
    errors: list[tuple[str, Exception]] = []

    print(f"\n{'='*64}")
    print("Travel Guild — M3a Critic/Verifier Unit Test Suite")
    print(f"{'='*64}\n")

    for test_fn in TESTS:
        name = test_fn.__name__
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            failed += 1
            errors.append((name, exc))
            print(f"FAIL: {name} — {exc}")
            traceback.print_exc()

    print(f"\n{'='*64}")
    print(f"Results: {passed} passed, {failed} failed out of {len(TESTS)} tests")
    print(f"{'='*64}\n")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
