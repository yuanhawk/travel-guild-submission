"""
test_critic_agent_cov.py — Supplemental deterministic coverage for CriticAgent.

ADD-ONLY companion to test_critic.py / test_critic_cov.py. CI-safe / var-0:
all merchant HTTP calls are intercepted by an in-process httpx.BaseTransport
mock; no LLM, no network, no paid API, no real merchant. Uses the Starlette
in-process ASGI TestClient throughout (the house pattern).

This file deliberately targets the *uncovered* behaviours and drift risks in
agents/critic_agent.py that the existing two test files do NOT already pin:

  - Exact 4-decimal quality-score rounding (round(..., 4)) — guards a future
    silent change to round(..., 2) (drift risk #1).
  - Merchant HTTP 404 → PRICE_MISMATCH, preserving the "not found in catalog"
    detail (lines 268-272 → generic handler 808-822).
  - Merchant non-JSON 200 body → PRICE_MISMATCH ("non-JSON" detail) (256-261).
  - Merchant 200 with structuredContent that is NOT a dict (list/string/null)
    → reset to {} → total_cents defaults to 0 → PRICE_MISMATCH (265-266).
  - Merchant 200 with an explicit `error` field set → RuntimeError →
    PRICE_MISMATCH (288-291) — distinct from the 404/409 paths.
  - Multi-hotel (3 legs) review-score averaging into the quality score (966-969).
  - Transport `feasible` flag in isolation as a positive signal (line 947).
  - Transport unknown-key dict → transport_checked stays False (negative case
    for the positive-signal logic, drift risk #5).
  - _extract_payload: data part carrying a JSON *string* (1011-1015), and the
    data-before-text priority order when BOTH parts are present (1006-1023).

If any assertion here would require editing product code to pass, that is
recorded as a finding, NOT fixed (investigate-first policy).
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
    PRICE_MISMATCH,
)


# ===========================================================================
# Mock transport infrastructure (house pattern; mirrors test_critic.py)
# ===========================================================================

class MockMerchantTransport(httpx.BaseTransport):
    """Single canned response per tool name."""

    def __init__(self, responses: dict[str, tuple[int, Any]]) -> None:
        self._responses = responses

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            payload = json.loads(request.read())
        except Exception:
            return httpx.Response(400, text="bad request")
        tool_name = (payload.get("params") or {}).get("name", "")
        if tool_name not in self._responses:
            return httpx.Response(
                500, json={"error": f"mock: no response for tool {tool_name!r}"}
            )
        status_code, body = self._responses[tool_name]
        if isinstance(body, (dict, list)):
            return httpx.Response(status_code, json=body)
        # Raw text body (used for the non-JSON path)
        return httpx.Response(status_code, text=str(body))


class SequencedMockTransport(httpx.BaseTransport):
    """Different response per call (FIFO queue per tool name)."""

    def __init__(self, sequences: dict[str, list[tuple[int, Any]]]) -> None:
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
                500, json={"error": f"mock: no sequence for tool {tool_name!r}"}
            )
        idx = self._call_counts.get(tool_name, 0)
        if idx >= len(seq):
            idx = len(seq) - 1
        self._call_counts[tool_name] = idx + 1
        status_code, body = seq[idx]
        if isinstance(body, (dict, list)):
            return httpx.Response(status_code, json=body)
        return httpx.Response(status_code, text=str(body))


def _merchant_result(domain: Any) -> dict[str, Any]:
    """Wrap a domain object in the merchant MCP result envelope."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "structuredContent": domain,
            "content": [{"type": "text", "text": json.dumps(domain)
                         if isinstance(domain, (dict, list)) else str(domain)}],
        },
    }


def _lookup_ok(
    hotel_id: str,
    total_cents: int,
    review_score: float | None = 8.5,
    max_occupancy: int = 4,
) -> tuple[int, dict[str, Any]]:
    hotel: dict[str, Any] = {
        "id": hotel_id,
        "title": f"Mock Hotel {hotel_id}",
        "max_occupancy": max_occupancy,
    }
    if review_score is not None:
        hotel["review_score"] = review_score
    return (200, _merchant_result({
        "hotel": hotel,
        "available": True,
        "total_cents": total_cents,
        "nights": 3,
        "source": "mock",
    }))


# ===========================================================================
# Test helpers (house pattern)
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


def _send_message(client: TestClient, parts: list[dict]) -> dict:
    msg = {
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": parts,
        "metadata": {"skillId": "itinerary.verify"},
    }
    rpc = _rpc_post(client, "message/send", {"message": msg})
    assert "error" not in rpc, f"RPC error: {rpc.get('error')}"
    return rpc["result"]


def _send_verify(client: TestClient, payload: dict) -> dict:
    return _send_message(client, [{"kind": "data", "data": payload}])


def _extract_critic_result(task: dict) -> dict:
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "data":
                return part["data"]
    raise AssertionError(f"No data part in task: {task}")


# ===========================================================================
# 1. Exact 4-decimal quality-score rounding (drift risk #1)
# ===========================================================================

def test_quality_score_rounded_to_four_decimals() -> None:
    """
    Pin the EXACT round(..., 4) behaviour so a future change to round(..., 2)
    (or no rounding) is caught. We engineer a quality score with >4 decimal
    digits of precision and assert it is rounded to exactly 4 dp.

    Construction (line 978: quality = (avg_review_norm + utilisation_fit) / 2):
      review_score=7.0  -> avg_review_norm = 7.0/10 = 0.7
      reverified=33333, budget=100000 -> utilisation = 0.33333
        utilisation_fit = 1.0 - |1.0 - 0.33333| = 0.33333
      quality_raw = (0.7 + 0.33333) / 2 = 0.516665
      round(0.516665, 4) == 0.5167   (round-half-to-even / banker's: 0.51665? )
    We assert against Python's own round() of the analytic value so the test is
    independent of float wobble, AND assert it differs from the 2-dp rounding.
    """
    budget = 100000
    price = 33333
    review = 7.0
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": budget,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-r",
                "total_cents": price, "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-r", price, review_score=review),
    })
    client, _ = _make_critic_client(transport=transport)
    result = _extract_critic_result(_send_verify(client, itinerary))

    assert result["decision"] == "verified", result

    avg_review_norm = review / 10.0
    utilisation = price / budget
    utilisation_fit = max(0.0, 1.0 - abs(1.0 - utilisation))
    raw = (avg_review_norm + utilisation_fit) / 2.0
    expected_4dp = round(max(0.0, min(1.0, raw)), 4)

    qs = result["quality_score"]
    # Exact match to the product's 4-dp rounding contract.
    assert qs == expected_4dp, (
        f"quality_score must equal round(raw, 4)={expected_4dp}, got {qs}"
    )
    # The returned value must never carry more than 4 decimal places.
    s = repr(qs)
    if "." in s:
        decimals = s.split(".", 1)[1]
        assert len(decimals) <= 4, (
            f"quality_score {qs} has >4 decimals — round(...,4) contract broken"
        )
    print(f"PASS: test_quality_score_rounded_to_four_decimals [qs={qs}]")


# ===========================================================================
# 2. HTTP 404 → PRICE_MISMATCH, preserving "not found" detail
# ===========================================================================

def test_merchant_404_hotel_not_found_is_price_mismatch() -> None:
    """
    A merchant 404 (hotel absent from catalog) raises RuntimeError 'Hotel ...
    not found in merchant catalog (HTTP 404)' which is caught by the generic
    handler and surfaced as PRICE_MISMATCH (anti-hallucination conservative
    reject). The detail must preserve the 'not found' diagnostic.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-ghost",
                "total_cents": 40000, "provenance": "live",
            },
        ],
    }
    transport = MockMerchantTransport({
        "lookup_catalog": (404, _merchant_result({"error": "not_found"})),
    })
    client, _ = _make_critic_client(transport=transport)
    result = _extract_critic_result(_send_verify(client, itinerary))

    assert result["decision"] == "rejected", result
    pm = next(v for v in result["violations"] if v["code"] == PRICE_MISMATCH)
    assert pm["route_to"] == "accommodation"
    detail = pm["detail"].lower()
    assert "not found" in detail and "404" in pm["detail"], (
        f"404 detail must preserve the 'not found in catalog (HTTP 404)' message: "
        f"{pm['detail']!r}"
    )
    print(f"PASS: test_merchant_404_hotel_not_found_is_price_mismatch")


# ===========================================================================
# 3. Non-JSON 200 body → PRICE_MISMATCH ("non-JSON" detail)
# ===========================================================================

def test_merchant_non_json_body_is_price_mismatch() -> None:
    """
    A 200 response whose body is NOT JSON (e.g. an HTML/plain-text error page)
    triggers the json.loads except branch (256-261): RuntimeError 'returned
    non-JSON' → caught → PRICE_MISMATCH. The detail must preserve 'non-JSON'.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-html",
                "total_cents": 40000, "provenance": "live",
            },
        ],
    }
    # Raw (non-JSON) text body returned with HTTP 200.
    transport = MockMerchantTransport({
        "lookup_catalog": (200, "<html><body>502 Bad Gateway</body></html>"),
    })
    client, _ = _make_critic_client(transport=transport)
    result = _extract_critic_result(_send_verify(client, itinerary))

    assert result["decision"] == "rejected", result
    pm = next(v for v in result["violations"] if v["code"] == PRICE_MISMATCH)
    assert "non-JSON" in pm["detail"], (
        f"Detail must surface the non-JSON merchant body: {pm['detail']!r}"
    )
    print(f"PASS: test_merchant_non_json_body_is_price_mismatch")


# ===========================================================================
# 4. structuredContent NOT a dict → reset to {} → PRICE_MISMATCH
# ===========================================================================

def test_structured_content_not_a_dict_defensive_branch() -> None:
    """
    Lines 265-266: if structuredContent is not a dict it is reset to {}. With an
    empty structured blob, total_cents defaults to 0 (!= proposal) so the leg is
    flagged PRICE_MISMATCH against merchant '0¢'. Exercises the defensive branch
    with structuredContent given as a LIST.
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-list",
                "total_cents": 40000, "provenance": "live",
            },
        ],
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            # structuredContent is a LIST, not a dict — must be coerced to {}.
            "structuredContent": ["not", "a", "dict"],
            "content": [{"type": "text", "text": "[]"}],
        },
    }
    transport = MockMerchantTransport({"lookup_catalog": (200, body)})
    client, _ = _make_critic_client(transport=transport)
    result = _extract_critic_result(_send_verify(client, itinerary))

    # merchant_total defaults to 0; proposal=40000 → mismatch (not a crash).
    assert result["decision"] == "rejected", result
    pm = next(v for v in result["violations"] if v["code"] == PRICE_MISMATCH)
    assert "40000" in pm["detail"], pm["detail"]
    # reverified_total counts the merchant 0¢ for this leg (no crash, no AvailErr)
    assert result["reverified_total_cents"] == 0, result["reverified_total_cents"]
    print(f"PASS: test_structured_content_not_a_dict_defensive_branch")


# ===========================================================================
# 5. 200 with explicit error field → RuntimeError → PRICE_MISMATCH
# ===========================================================================

def test_merchant_200_with_error_field_is_price_mismatch() -> None:
    """
    Lines 288-291: a 200 with structuredContent.error truthy raises
    'Merchant lookup_catalog error ...' → caught → PRICE_MISMATCH. This is
    distinct from the 404 / 409 status checks (a successful HTTP code carrying
    an application-level error payload).
    """
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": 100000,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-04",
                "adults": 1, "hotel_id": "bali-err",
                "total_cents": 40000, "provenance": "live",
            },
        ],
    }
    # HTTP 200, no 'status'=='unavailable', but an explicit error field set.
    transport = MockMerchantTransport({
        "lookup_catalog": (200, _merchant_result({
            "error": "rate_limited",
            "hotel": {"id": "bali-err"},
        })),
    })
    client, _ = _make_critic_client(transport=transport)
    result = _extract_critic_result(_send_verify(client, itinerary))

    assert result["decision"] == "rejected", result
    pm = next(v for v in result["violations"] if v["code"] == PRICE_MISMATCH)
    assert "rate_limited" in pm["detail"] or "error" in pm["detail"].lower(), (
        f"Detail should surface the merchant error: {pm['detail']!r}"
    )
    print(f"PASS: test_merchant_200_with_error_field_is_price_mismatch")


# ===========================================================================
# 6. Multi-hotel (3 legs) review-score averaging into quality (966-969)
# ===========================================================================

def test_quality_score_averages_three_hotel_reviews() -> None:
    """
    Three legs with review scores 6.0, 8.0, 10.0 → avg = 8.0 → avg_review_norm
    = 0.8. With an exact budget match (utilisation_fit = 1.0), quality should be
    (0.8 + 1.0) / 2 = 0.9. Exercises the >1-entry averaging branch (line 967).
    """
    budget = 90000
    itinerary = {
        "user_id": "u-test",
        "total_budget_cents": budget,
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2025-10-01", "checkout": "2025-10-03",
                "adults": 1, "hotel_id": "h0",
                "total_cents": 30000, "provenance": "live",
            },
            {
                "leg_id": "leg-1", "city": "bali",
                "checkin": "2025-10-03", "checkout": "2025-10-05",
                "adults": 1, "hotel_id": "h1",
                "total_cents": 30000, "provenance": "live",
            },
            {
                "leg_id": "leg-2", "city": "bali",
                "checkin": "2025-10-05", "checkout": "2025-10-07",
                "adults": 1, "hotel_id": "h2",
                "total_cents": 30000, "provenance": "live",
            },
        ],
    }
    transport = SequencedMockTransport({
        "lookup_catalog": [
            _lookup_ok("h0", 30000, review_score=6.0),
            _lookup_ok("h1", 30000, review_score=8.0),
            _lookup_ok("h2", 30000, review_score=10.0),
        ],
    })
    client, _ = _make_critic_client(transport=transport)
    result = _extract_critic_result(_send_verify(client, itinerary))

    assert result["decision"] == "verified", result
    assert result["reverified_total_cents"] == 90000
    # avg review = (6+8+10)/3 = 8.0 → 0.8 ; util_fit = 1.0 → quality = 0.9
    assert abs(result["quality_score"] - 0.9) < 0.0005, (
        f"Expected ~0.9 from 3-hotel review average, got {result['quality_score']}"
    )
    print(f"PASS: test_quality_score_averages_three_hotel_reviews "
          f"[qs={result['quality_score']}]")


# ===========================================================================
# 7. Transport 'feasible' flag in isolation → transport_checked=True (947)
# ===========================================================================

def test_transport_feasible_flag_sets_checked() -> None:
    """
    transport_result={'feasible': True} (no infeasible_edges, no unverified_edges,
    no 'checked', no 'edges') must set transport_checked=True via the 'feasible'
    positive-signal branch (line 947).
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
        "transport_result": {"feasible": True},
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-a", 40000),
    })
    client, _ = _make_critic_client(transport=transport)
    result = _extract_critic_result(_send_verify(client, itinerary))

    assert result["transport_checked"] is True, result
    print(f"PASS: test_transport_feasible_flag_sets_checked")


# ===========================================================================
# 8. Transport unknown-key dict → transport_checked stays False (drift #5)
# ===========================================================================

def test_transport_unknown_key_keeps_checked_false() -> None:
    """
    Negative case for the positive-signal logic (945-960): a transport_result
    carrying ONLY an unrelated key (no checked/feasible/edges/infeasible_edges/
    unverified_edges) is NOT proof of a feasibility check — transport_checked
    must stay False. Guards against an inverted honesty signal (drift risk #5).
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
        "transport_result": {"some_unknown_key": True},
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-a", 40000),
    })
    client, _ = _make_critic_client(transport=transport)
    result = _extract_critic_result(_send_verify(client, itinerary))

    assert result["transport_checked"] is False, (
        f"Unknown-key transport_result must NOT claim a check: {result}"
    )
    print(f"PASS: test_transport_unknown_key_keeps_checked_false")


# ===========================================================================
# 9. _extract_payload: data part as JSON STRING (1011-1015)
# ===========================================================================

def test_payload_extraction_data_part_as_json_string() -> None:
    """
    A data part whose `data` value is a JSON *string* (not a dict) must be
    json.loads-parsed (lines 1011-1015), then verified normally.
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
    # data part value is a STRING, not a dict.
    task = _send_message(
        client, [{"kind": "data", "data": json.dumps(itinerary)}]
    )
    result = _extract_critic_result(task)
    assert result["decision"] == "verified", result
    print(f"PASS: test_payload_extraction_data_part_as_json_string")


# ===========================================================================
# 10. _extract_payload: data part takes priority over text part (1006-1023)
# ===========================================================================

def test_payload_extraction_data_part_priority_over_text() -> None:
    """
    When BOTH a data part and a text part are present, _extract_payload iterates
    parts in order and returns the first usable one. We put the GOOD itinerary
    in the data part (first) and a DECOY (different user_id / impossible budget)
    in the text part; the data part must win.
    """
    good = {
        "user_id": "u-good",
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
    decoy = {
        "user_id": "u-decoy",
        "total_budget_cents": 1,  # would force OVER_BUDGET if it were used
        "legs": [],               # would force MISSING_LEG if it were used
    }
    transport = MockMerchantTransport({
        "lookup_catalog": _lookup_ok("bali-a", 40000),
    })
    client, _ = _make_critic_client(transport=transport)
    task = _send_message(client, [
        {"kind": "data", "data": good},          # first → should win
        {"kind": "text", "text": json.dumps(decoy)},
    ])
    result = _extract_critic_result(task)

    # If the decoy text part had been used we'd see rejected (empty legs / budget=1).
    assert result["decision"] == "verified", (
        f"data part must take priority over text part: {result}"
    )
    assert result["reverified_total_cents"] == 40000
    print(f"PASS: test_payload_extraction_data_part_priority_over_text")


# ===========================================================================
# Test runner (mirrors the house standalone-runner convention)
# ===========================================================================

TESTS = [
    test_quality_score_rounded_to_four_decimals,
    test_merchant_404_hotel_not_found_is_price_mismatch,
    test_merchant_non_json_body_is_price_mismatch,
    test_structured_content_not_a_dict_defensive_branch,
    test_merchant_200_with_error_field_is_price_mismatch,
    test_quality_score_averages_three_hotel_reviews,
    test_transport_feasible_flag_sets_checked,
    test_transport_unknown_key_keeps_checked_false,
    test_payload_extraction_data_part_as_json_string,
    test_payload_extraction_data_part_priority_over_text,
]


def main() -> None:
    import traceback

    passed = failed = 0
    for fn in TESTS:
        try:
            fn()
            passed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL: {fn.__name__} — {exc}")
            traceback.print_exc()
    print(f"\nResults: {passed} passed, {failed} failed of {len(TESTS)}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
