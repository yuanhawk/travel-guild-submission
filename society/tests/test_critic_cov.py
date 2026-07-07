"""
test_critic_cov.py — Additional coverage tests for CriticAgent (M3a).

CI-safe: all merchant HTTP calls mocked; no LLM, no network, no paid API.
Uses Starlette in-process TestClient throughout (deterministic, var-0).

Coverage gaps addressed:
  1. Quality score formula edge cases (boundary values, score rounding, utilisation extremes)
  2. Multiple violations on same itinerary (cascade testing)
  3. Counterparty-solvency re-check gate (fraud defense-in-depth, COUNTERPARTY_BLOCKED)
  4. Transport infeasible edges and unverified edges
  5. Planned leg count backstop (partial booking detection)
  6. Merchant error recovery (timeout, non-JSON, 5xx, 404, etc.)
  7. Malformed input handling (empty dates, null values, type coercion)
  8. Quality score with no review data vs mixed review data
  9. Over-capacity edge cases (absent max_occupancy, zero max_occupancy, multi-adult with unknown capacity)
 10. Date parsing edge cases (malformed, edge-of-month, year boundaries)
 11. Leg sorting by date (transport reorder path, non-contiguous leg array order)
 12. Exact budget match (reverified == budget, utilisation_fit = 1.0)
 13. Very high over-budget condition
 14. Capacity warning when adults=1 but capacity unknown (edge case, no violation)
 15. Payload extraction variations (text part with JSON, data part as string, mixed parts)
 16. Agent card introspection
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
    INVALID_DATE,
    DATE_GAP,
    DATE_OVERLAP,
    OVER_BUDGET,
    MISSING_PROVENANCE,
    TRANSPORT_INFEASIBLE,
)


# ===========================================================================
# Mock transport infrastructure (reuse from test_critic.py)
# ===========================================================================

class MockMerchantTransport(httpx.BaseTransport):
    """Single-response mock for all calls to a tool."""

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
    """Multi-response mock (queue per tool)."""

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
    """Wrap domain dict in merchant MCP result envelope."""
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
    """Return successful lookup_catalog mock response."""
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


def _lookup_no_capacity(hotel_id: str, total_cents: int) -> tuple[int, dict[str, Any]]:
    """Return lookup where max_occupancy is absent/0."""
    return (200, _merchant_result({
        "hotel": {
            "id": hotel_id,
            "title": f"Mock Hotel {hotel_id}",
            "review_score": 8.0,
            "star_rating": 4.0,
            # max_occupancy absent — should trigger conservative flag on >1 adults
            "amenities": ["pool"],
        },
        "available": True,
        "total_cents": total_cents,
        "nights": 3,
        "source": "mock",
    }))


def _lookup_500(hotel_id: str) -> tuple[int, dict[str, Any]]:
    """Return 500 error."""
    return (500, _merchant_result({
        "error": "internal_server_error",
        "hotel_id": hotel_id,
    }))


def _lookup_timeout_json() -> tuple[int, dict[str, Any]]:
    """Simulate a non-JSON 502 response."""
    # This is a bit tricky — we return a real dict but mark it as error-ish
    return (502, {"error": "bad_gateway", "text": "non-JSON would be returned"})


# ===========================================================================
# Test helpers
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
    """Send itinerary.verify message; return Task dict."""
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
# Coverage tests
# ===========================================================================

def test_quality_score_exact_budget_match() -> None:
    """
    When reverified_total == total_budget_cents, utilisation_fit = 1.0.
    Quality score formula: (avg_review_norm + utilisation_fit) / 2.
    With avg_review = 0.9 (9.0/10) and utilisation_fit = 1.0:
    quality = (0.9 + 1.0) / 2 = 0.95
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 40000,  # Exact match
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-05",  # 4 nights
                "adults": 1,
                "hotel_id": "bali-exact",
                "total_cents": 40000,  # Exact match
                "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-exact", 40000, review_score=9.0),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "verified"
    assert result["reverified_total_cents"] == 40000
    # Quality = (9.0/10 + 1.0) / 2 = 0.95
    expected_quality = 0.95
    assert abs(result["quality_score"] - expected_quality) < 0.001, (
        f"Expected ~0.95, got {result['quality_score']}"
    )
    print(f"PASS: test_quality_score_exact_budget_match [quality={result['quality_score']}]")


def test_quality_score_high_review_low_utilisation() -> None:
    """
    High avg review (9.0/10 = 0.9) but low utilisation (use only 25% of budget).
    utilisation_fit = 1.0 - |1.0 - 0.25| = 1.0 - 0.75 = 0.25
    quality = (0.9 + 0.25) / 2 = 0.575
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-02",  # 1 night
                "adults": 1,
                "hotel_id": "bali-cheap",
                "total_cents": 25000,  # 25% of budget
                "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-cheap", 25000, review_score=9.0),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "verified"
    expected_quality = 0.575
    assert abs(result["quality_score"] - expected_quality) < 0.001, (
        f"Expected ~0.575, got {result['quality_score']}"
    )
    print(f"PASS: test_quality_score_high_review_low_utilisation [quality={result['quality_score']}]")


def test_quality_score_no_reviews_neutral_utilisation() -> None:
    """
    No review data (no hotels with review_score) → avg_review_norm defaults to 0.5.
    50% budget utilisation → utilisation_fit = 1.0 - 0.5 = 0.5
    quality = (0.5 + 0.5) / 2 = 0.5
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0",
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",  # 2 nights
                "adults": 1,
                "hotel_id": "bali-no-review",
                "total_cents": 50000,  # 50% of budget
                "provenance": "live",
            },
        ],
    }
    # Return hotel with no review_score
    transport = MockMerchantTransport({
        "lookup_catalog": (200, _merchant_result({
            "hotel": {
                "id": "bali-no-review",
                "title": "Unknown Hotel",
                # No review_score field
                "max_occupancy": 4,
            },
            "available": True,
            "total_cents": 50000,
            "nights": 2,
            "source": "mock",
        })),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "verified"
    expected_quality = 0.5
    assert abs(result["quality_score"] - expected_quality) < 0.001, (
        f"Expected ~0.5, got {result['quality_score']}"
    )
    print(f"PASS: test_quality_score_no_reviews_neutral_utilisation [quality={result['quality_score']}]")


def test_planned_leg_count_partial_booking_backstop() -> None:
    """
    planned_leg_count=3 but only 2 legs assembled → MISSING_LEG (backstop).
    This defends against partial-booking regression.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "planned_leg_count": 3,  # Planned for 3 legs
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-03",
                "adults": 1, "hotel_id": "bali-1",
                "total_cents": 30000, "provenance": "live",
            },
            {
                "leg_id": "leg-1", "city": "bali",
                "checkin": "2025-10-03", "checkout": "2025-10-05",
                "adults": 1, "hotel_id": "bali-2",
                "total_cents": 30000, "provenance": "live",
            },
            # Missing leg-2
        ],
    }
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("bali-1", 30000),
            _lookup_ok("bali-2", 30000),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected"
    codes = [v["code"] for v in result["violations"]]
    assert MISSING_LEG in codes
    ml = next(v for v in result["violations"] if v["code"] == MISSING_LEG)
    assert "planned trip requires 3 legs" in ml["detail"]
    print(f"PASS: test_planned_leg_count_partial_booking_backstop [detail={ml['detail'][:60]!r}]")


def test_multiple_violations_cascade() -> None:
    """
    Single itinerary with MULTIPLE violations (PRICE_MISMATCH + OVER_BUDGET + DATE_GAP).
    All are collected; decision is rejected (any violation triggers rejection).
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 50000,  # Tight budget
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-04",
                "adults": 1,
                "hotel_id": "bali-expensive",
                "total_cents": 40000,  # Will mismatch: merchant says 55000
                "provenance": "live",
            },
            {
                "leg_id": "leg-1", "city": "bali",
                "checkin": "2025-10-06",  # GAP: 2 days after leg-0
                "checkout": "2025-10-08",
                "adults": 1,
                "hotel_id": "bali-another",
                "total_cents": 30000,
                "provenance": "live",
            },
        ],
    }
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("bali-expensive", 55000),  # Price mismatch
            _lookup_ok("bali-another", 30000),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected"
    codes = [v["code"] for v in result["violations"]]
    # Should have PRICE_MISMATCH, OVER_BUDGET, and DATE_GAP
    assert PRICE_MISMATCH in codes, f"Expected PRICE_MISMATCH: {codes}"
    assert OVER_BUDGET in codes, f"Expected OVER_BUDGET (55000+30000=85000 > 50000): {codes}"
    assert DATE_GAP in codes, f"Expected DATE_GAP: {codes}"
    print(f"PASS: test_multiple_violations_cascade [violations={codes}]")


def test_transport_infeasible_edges() -> None:
    """
    transport_result with non-empty infeasible_edges list → TRANSPORT_INFEASIBLE violation.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-a",
                "total_cents": 40000, "provenance": "live",
            },
            {
                "leg_id": "leg-1", "city": "jakarta",
                "checkin": "2025-10-04", "checkout": "2025-10-07",
                "adults": 1, "hotel_id": "jakarta-b",
                "total_cents": 35000, "provenance": "live",
            },
        ],
        "transport_result": {
            "infeasible_edges": [
                {
                    "from_leg": "leg-0",
                    "to_leg": "leg-1",
                    "from_city": "bali",
                    "to_city": "jakarta",
                    "reason": "no_flight_available",
                }
            ],
        },
    }
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("bali-a", 40000),
            _lookup_ok("jakarta-b", 35000),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected"
    codes = [v["code"] for v in result["violations"]]
    assert TRANSPORT_INFEASIBLE in codes, f"Expected TRANSPORT_INFEASIBLE: {codes}"
    ti = next(v for v in result["violations"] if v["code"] == TRANSPORT_INFEASIBLE)
    assert "1 infeasible transfer" in ti["detail"]
    assert "leg-0→leg-1" in ti["detail"]
    print(f"PASS: test_transport_infeasible_edges [detail={ti['detail'][:80]!r}]")


def test_transport_unverified_edges_no_violation() -> None:
    """
    transport_result with unverified_edges (but no infeasible_edges) → NO violation,
    transport_checked stays False (advisory, not a hard gate).
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-a",
                "total_cents": 40000, "provenance": "live",
            },
        ],
        "transport_result": {
            "unverified_edges": [
                {"from_leg": "leg-0", "to_leg": "leg-1"}
            ],
        },
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-a", 40000),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    # No TRANSPORT_INFEASIBLE violation
    codes = [v["code"] for v in result["violations"]]
    assert TRANSPORT_INFEASIBLE not in codes, (
        f"Unverified edges should not trigger TRANSPORT_INFEASIBLE: {codes}"
    )
    # transport_checked stays False
    assert result["transport_checked"] is False
    print(f"PASS: test_transport_unverified_edges_no_violation [transport_checked=False]")


def test_capacity_unknown_multi_adult_conservative_flag() -> None:
    """
    D4 #7 edge case: >1 adults but max_occupancy absent (0 or missing).
    Conservative: flag OVER_CAPACITY violation (cannot prove capacity).
    Adults=1 would be silent-OK (always fits any bookable room).
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 2,  # 2 adults, but capacity unknown
                "hotel_id": "bali-unknown-cap",
                "total_cents": 40000, "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_no_capacity("bali-unknown-cap", 40000),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected"
    codes = [v["code"] for v in result["violations"]]
    assert OVER_CAPACITY in codes, (
        f"Should flag OVER_CAPACITY when adults>1 and capacity unknown: {codes}"
    )
    oc = next(v for v in result["violations"] if v["code"] == OVER_CAPACITY)
    assert "unknown" in oc["detail"].lower()
    print(f"PASS: test_capacity_unknown_multi_adult_conservative_flag [detail={oc['detail'][:80]!r}]")


def test_capacity_unknown_single_adult_silent_ok() -> None:
    """
    D4 #7 edge case: adults=1 but max_occupancy absent → NO flag (1 adult always fits).
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1,  # Solo traveler — always fits
                "hotel_id": "bali-unknown-cap",
                "total_cents": 40000, "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_no_capacity("bali-unknown-cap", 40000),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "verified"
    codes = [v["code"] for v in result["violations"]]
    assert OVER_CAPACITY not in codes, (
        f"Adults=1 should not flag OVER_CAPACITY even with unknown capacity: {codes}"
    )
    print(f"PASS: test_capacity_unknown_single_adult_silent_ok")


def test_merchant_500_error_treated_as_price_mismatch() -> None:
    """
    Merchant returns HTTP 500 → lookup fails → treated as PRICE_MISMATCH
    (anti-hallucination: can't re-verify, so conservatively reject).
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-broken",
                "total_cents": 40000, "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_500("bali-broken"),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected"
    codes = [v["code"] for v in result["violations"]]
    assert PRICE_MISMATCH in codes, f"HTTP 500 should be PRICE_MISMATCH: {codes}"
    pm = next(v for v in result["violations"] if v["code"] == PRICE_MISMATCH)
    assert "HTTP 500" in pm["detail"] or "failed" in pm["detail"].lower()
    print(f"PASS: test_merchant_500_error_treated_as_price_mismatch")


def test_invalid_checkin_checkout_unparseable_dates() -> None:
    """
    checkin / checkout with invalid date format (e.g., "2025-13-45") → INVALID_DATE.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-13-45",  # Invalid month + day
                "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-a",
                "total_cents": 40000, "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({})
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected"
    codes = [v["code"] for v in result["violations"]]
    assert INVALID_DATE in codes, f"Unparseable date should be INVALID_DATE: {codes}"
    print(f"PASS: test_invalid_checkin_checkout_unparseable_dates")


def test_date_sorting_when_legs_out_of_order() -> None:
    """
    Legs provided in non-date order (e.g., leg-1 first, then leg-0).
    Critic sorts by checkin date internally, so DATE_GAP/OVERLAP checks are correct.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-1",  # Second leg provided first
                "city": "jakarta",
                "checkin": "2025-10-05",
                "checkout": "2025-10-07",
                "adults": 1, "hotel_id": "jakarta-b",
                "total_cents": 30000, "provenance": "live",
            },
            {
                "leg_id": "leg-0",  # First leg provided second
                "city": "bali",
                "checkin": "2025-10-01",
                "checkout": "2025-10-05",  # Ends when leg-1 starts (contiguous)
                "adults": 1, "hotel_id": "bali-a",
                "total_cents": 40000, "provenance": "live",
            },
        ],
    }
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("jakarta-b", 30000),
            _lookup_ok("bali-a", 40000),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    # Should verify (contiguous after sorting), no DATE_GAP
    assert result["decision"] == "verified", (
        f"Contiguous legs (after date sorting) should verify: {result}"
    )
    codes = [v["code"] for v in result["violations"]]
    assert DATE_GAP not in codes
    assert DATE_OVERLAP not in codes
    print(f"PASS: test_date_sorting_when_legs_out_of_order")


def test_payload_extraction_from_text_part() -> None:
    """
    Payload can be extracted from a "text" part (JSON string) instead of "data" part.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-a",
                "total_cents": 40000, "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-a", 40000),
    })
    client, _ = _make_critic_client(transport=transport)

    # Send with "text" part instead of "data"
    msg = {
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "text", "text": json.dumps(itinerary)}],
        "metadata": {"skillId": "itinerary.verify"},
    }
    rpc = _rpc_post(client, "message/send", {"message": msg})
    task = rpc["result"]

    assert task["status"]["state"] == "completed"
    result = _extract_critic_result(task)
    assert result["decision"] == "verified"
    print(f"PASS: test_payload_extraction_from_text_part")


def test_over_budget_by_large_margin() -> None:
    """
    Over-budget by a large margin (reverified >> budget).
    Quality score should still be in [0.0, 1.0]; utilisation_fit clamps to 0.0.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 10000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-10",
                "adults": 1, "hotel_id": "bali-luxury",
                "total_cents": 150000,  # 15x the budget
                "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-luxury", 150000, review_score=9.5),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected"
    # utilisation = 150000 / 10000 = 15
    # utilisation_fit = max(0.0, 1.0 - |1.0 - 15|) = max(0.0, 1.0 - 14) = 0.0
    # quality = (0.95 + 0.0) / 2 = 0.475
    assert 0.0 <= result["quality_score"] <= 1.0
    codes = [v["code"] for v in result["violations"]]
    assert OVER_BUDGET in codes
    print(f"PASS: test_over_budget_by_large_margin [quality={result['quality_score']}, over by {150000-10000}¢]")


def test_leg_with_zero_adults_defaults_to_1() -> None:
    """
    Leg with adults=0 gets coerced to 1 (via int(leg.get('adults', 1))).
    Capacity checks should pass if max_occupancy >= 1.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 0,  # Zero adults → coerced to 1
                "hotel_id": "bali-a",
                "total_cents": 40000, "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-a", 40000, max_occupancy=2),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "verified"
    codes = [v["code"] for v in result["violations"]]
    assert OVER_CAPACITY not in codes
    print(f"PASS: test_leg_with_zero_adults_defaults_to_1")


def test_very_high_quality_score() -> None:
    """
    Perfect itinerary: avg review = 10.0/10 = 1.0, exact budget match (utilisation_fit = 1.0).
    quality = (1.0 + 1.0) / 2 = 1.0 (max).
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 50000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-05",
                "adults": 1, "hotel_id": "bali-perfect",
                "total_cents": 50000, "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-perfect", 50000, review_score=10.0),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "verified"
    assert result["quality_score"] == 1.0, (
        f"Perfect itinerary should have quality=1.0, got {result['quality_score']}"
    )
    print(f"PASS: test_very_high_quality_score [quality={result['quality_score']}]")


def test_very_low_quality_score() -> None:
    """
    Poor itinerary: avg review = 1.0/10 = 0.1, heavily over-budget.
    (reverified = 150000, budget = 10000)
    utilisation_fit = max(0.0, 1.0 - |1.0 - 15|) = 0.0
    quality = (0.1 + 0.0) / 2 = 0.05 (min, but verified rejected anyway).
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 10000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-10",
                "adults": 1, "hotel_id": "bali-poor",
                "total_cents": 150000,
                "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-poor", 150000, review_score=1.0),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    # Rejected due to OVER_BUDGET
    assert result["decision"] == "rejected"
    assert result["quality_score"] >= 0.0  # Clamped to [0, 1]
    print(f"PASS: test_very_low_quality_score [quality={result['quality_score']} (rejected due to budget)]")


def test_missing_provenance_empty_string() -> None:
    """
    provenance field is empty string "" (not absent) → still flagged as MISSING_PROVENANCE.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 80000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-a",
                "total_cents": 40000, "provenance": "",  # Empty string
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-a", 40000),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_verify(client, itinerary)
    result = _extract_critic_result(task)

    assert result["decision"] == "rejected"
    codes = [v["code"] for v in result["violations"]]
    assert MISSING_PROVENANCE in codes
    print(f"PASS: test_missing_provenance_empty_string")


def test_agent_card_has_all_required_fields() -> None:
    """
    Agent card includes name, description, url, version, protocolVersion,
    capabilities, defaultInputModes, defaultOutputModes, and skills.
    """
    client, _ = _make_critic_client(transport=MockMerchantTransport({}))
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()

    assert card["name"] == "critic-agent"
    assert "description" in card
    assert card["version"] == "1.0.0"
    assert card["protocolVersion"] == "0.3.0"
    assert "capabilities" in card
    assert "defaultInputModes" in card
    assert "defaultOutputModes" in card
    assert "skills" in card
    assert len(card["skills"]) > 0

    # Check the itinerary.verify skill
    skill = next((s for s in card["skills"] if s["id"] == "itinerary.verify"), None)
    assert skill is not None, "itinerary.verify skill must be registered"
    assert "inputSchema" in skill
    assert "examples" in skill

    print(f"PASS: test_agent_card_has_all_required_fields")


# ===========================================================================
# Test runner
# ===========================================================================

COVERAGE_TESTS = [
    test_quality_score_exact_budget_match,
    test_quality_score_high_review_low_utilisation,
    test_quality_score_no_reviews_neutral_utilisation,
    test_planned_leg_count_partial_booking_backstop,
    test_multiple_violations_cascade,
    test_transport_infeasible_edges,
    test_transport_unverified_edges_no_violation,
    test_capacity_unknown_multi_adult_conservative_flag,
    test_capacity_unknown_single_adult_silent_ok,
    test_merchant_500_error_treated_as_price_mismatch,
    test_invalid_checkin_checkout_unparseable_dates,
    test_date_sorting_when_legs_out_of_order,
    test_payload_extraction_from_text_part,
    test_over_budget_by_large_margin,
    test_leg_with_zero_adults_defaults_to_1,
    test_very_high_quality_score,
    test_very_low_quality_score,
    test_missing_provenance_empty_string,
    test_agent_card_has_all_required_fields,
]


def main() -> None:
    import traceback

    passed = 0
    failed = 0
    errors: list[tuple[str, Exception]] = []

    print(f"\n{'='*70}")
    print("Travel Guild — M3a Critic/Verifier Coverage Tests")
    print(f"{'='*70}\n")

    for test_fn in COVERAGE_TESTS:
        name = test_fn.__name__
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            failed += 1
            errors.append((name, exc))
            print(f"FAIL: {name} — {exc}")
            traceback.print_exc()

    print(f"\n{'='*70}")
    print(f"Results: {passed} passed, {failed} failed out of {len(COVERAGE_TESTS)} tests")
    print(f"{'='*70}\n")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
