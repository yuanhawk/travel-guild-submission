"""
test_orch_budget_guidance_cov3.py — Tests for the Budget & Currency Guidance feature.

Coverage plan (§5):
  1. Missing budget → range suggested (needs_clarification still True, budget_estimate
     present, low<=high, multi-currency string, NO booking).
  2. Budget too low (DP-infeasible) → budget_shortfall_cents > 0, min_feasible_total
     correct, message contains the gap.
  3. NOT-budget-driven failure (no_inventory) → NO shortfall attached.
  4. var-0: tests 1 and 2 are byte-identical across 3 runs.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from starlette.testclient import TestClient

from agents import planner_agent as planner_mod
from agents import accommodation_agent as acc_mod
from agents import budget_agent as ba_mod
from orchestration.orchestrator import TravelOrchestrator
from utils import intent_parser as ip

# ---------------------------------------------------------------------------
# Mock infrastructure (mirrors test_dp_allocator.py)
# ---------------------------------------------------------------------------


class MockMerchantTransport(httpx.BaseTransport):
    def __init__(self, responses: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self._responses = responses

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            payload = json.loads(request.read())
        except Exception:
            return httpx.Response(400, text="bad request")
        tool_name = (payload.get("params") or {}).get("name", "")
        if tool_name not in self._responses:
            return httpx.Response(500, json={"error": f"mock: no response for {tool_name!r}"})
        status_code, body = self._responses[tool_name]
        return httpx.Response(status_code, json=body)


class SequencedMockTransport(httpx.BaseTransport):
    def __init__(self, sequences: dict[str, list[tuple[int, dict[str, Any]]]]) -> None:
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
            return httpx.Response(500, json={"error": f"mock: no sequence for {tool_name!r}"})
        idx = self._call_counts.get(tool_name, 0)
        if idx >= len(seq):
            idx = len(seq) - 1
        self._call_counts[tool_name] = idx + 1
        status_code, body = seq[idx]
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
    return (200, _merchant_result({"source": "mock", "count": len(results), "results": results}))


# Hotel fixtures (Bali)
UBUD_GARDEN = {
    "hotel_id": "bali-ubud-garden",
    "title": "Ubud Garden Resort",
    "city": "bali",
    "review_score": 8.2,
    "star_rating": 3.0,
    "nights": 3,
    "total_cents": 8400,
    "amenities": ["breakfast", "wifi"],
}

UBUD_ALAYA = {
    "hotel_id": "bali-alaya-ubud",
    "title": "Alaya Resort Ubud",
    "city": "bali",
    "review_score": 8.9,
    "star_rating": 5.0,
    "nights": 3,
    "total_cents": 49500,
    "amenities": ["pool", "spa", "breakfast"],
}

KUTA_BEACHSIDE = {
    "hotel_id": "bali-kuta-beachside",
    "title": "Kuta Beachside Hostel",
    "city": "bali",
    "review_score": 7.5,
    "star_rating": 2.0,
    "nights": 4,
    "total_cents": 7600,
    "amenities": ["wifi", "breakfast"],
}

LEGIAN_BEACH = {
    "hotel_id": "bali-legian-beach",
    "title": "Legian Beach Hotel",
    "city": "bali",
    "review_score": 8.3,
    "star_rating": 4.0,
    "nights": 4,
    "total_cents": 49600,
    "amenities": ["beachfront", "pool", "spa"],
}


def _make_planner_client() -> TestClient:
    agent = planner_mod.PlannerAgent()
    return TestClient(agent.build_app(), raise_server_exceptions=True)


def _make_acc_client(transport: httpx.BaseTransport) -> TestClient:
    agent = acc_mod.AccommodationAgent(merchant_transport=transport)
    return TestClient(agent.build_app(), raise_server_exceptions=True)


def _make_budget_client(transport: httpx.BaseTransport) -> TestClient:
    agent = ba_mod.BudgetAgent(merchant_transport=transport)
    return TestClient(agent.build_app(), raise_server_exceptions=True)


def _make_orchestrator(acc_transport: httpx.BaseTransport,
                       budget_transport: httpx.BaseTransport | None = None) -> TravelOrchestrator:
    return TravelOrchestrator(
        planner_client=_make_planner_client(),
        accommodation_client=_make_acc_client(acc_transport),
        budget_client=_make_budget_client(budget_transport or MockMerchantTransport({})),
    )


def _no_llm():
    """Patch out DashScope so parse_intent runs the deterministic regex fallback."""
    return patch("utils.intent_parser.DASHSCOPE_API_KEY", "")


# ---------------------------------------------------------------------------
# Test 1: missing budget → range suggested (needs_clarification stays True)
# ---------------------------------------------------------------------------


def test_missing_budget_range_suggested() -> None:
    """
    negotiate_from_text with city+nights but NO budget → needs_clarification True,
    budget_estimate attached, low <= high, multi-currency string present, NO booking.
    """
    # Accommodation returns two candidates for the range computation
    acc_transport = MockMerchantTransport({
        "search_catalog": _catalog_result([UBUD_GARDEN, UBUD_ALAYA]),
    })

    orch = _make_orchestrator(acc_transport)

    with _no_llm():
        result = ip.negotiate_from_text(
            "6 nights Bali, beach, solo",
            orchestrator=orch,
            user_id="u-test",
            today="2026-07-01",
        )

    # Still needs_clarification — we never auto-planned
    assert result.get("needs_clarification") is True, (
        f"Expected needs_clarification=True, got: {result}"
    )

    # budget_estimate must be present
    est = result.get("budget_estimate")
    assert est is not None, (
        f"Expected budget_estimate in result, got keys: {list(result.keys())}"
    )

    # low < high — a STRICT band (the catalog has a cheap + an expensive row).
    assert est["low_cents"] < est["high_cents"], (
        f"low_cents={est['low_cents']} >= high_cents={est['high_cents']}"
    )

    # Floor must be the EXACT cheapest real catalog row (UBUD_GARDEN, 8400¢) — the
    # raw low is Σ(cheapest per leg) with no gate fees on this single-leg trip. This
    # is the regression guard for the $0-floor bug: the per-leg cheapest cost must be
    # carried through, not lost.
    assert est["low_cents"] == UBUD_GARDEN["total_cents"], (
        f"low_cents should equal the cheapest catalog row "
        f"({UBUD_GARDEN['total_cents']}¢), got {est['low_cents']}¢"
    )
    # High = mid-tier (single mock alternate, UBUD_ALAYA 49500¢).
    assert est["high_cents"] == UBUD_ALAYA["total_cents"], (
        f"high_cents should equal the mid candidate ({UBUD_ALAYA['total_cents']}¢), "
        f"got {est['high_cents']}¢"
    )

    # The DISPLAYED (rounded) low must be NON-ZERO for a real trip — a $84 floor must
    # never round down to $0. And the rounded band stays strictly ordered.
    assert est["low_rounded"] > 0, (
        f"rounded low must be non-zero for a real trip, got {est['low_rounded']}¢ "
        f"(the $0-floor bug)"
    )
    assert est["low_rounded"] < est["high_rounded"], (
        f"low_rounded={est['low_rounded']} not < high_rounded={est['high_rounded']}"
    )
    # Rounded low must not overstate the real floor (honest band: round DOWN).
    assert est["low_rounded"] <= est["low_cents"], (
        f"rounded low {est['low_rounded']}¢ overstates the real floor {est['low_cents']}¢"
    )

    # message is a non-empty string containing currency info
    assert isinstance(est["message"], str) and len(est["message"]) > 10, (
        f"message is short/missing: {est.get('message')!r}"
    )
    # The message mentions IDR (Bali → IDR local) AND the USD conversion.
    msg = est["message"]
    assert "Rp" in msg and "US$" in msg, (
        f"Expected both local (Rp) and USD currency info in message: {msg!r}"
    )
    # No "free" floor leaking into the displayed message (the $0-floor bug symptom).
    assert "Rp 0" not in msg and "US$0" not in msg, (
        f"message advertises a $0/Rp 0 floor (the bug): {msg!r}"
    )
    # Honesty caveat + as-of date must be present.
    assert "indicative as-of 2026-06" in msg, f"missing as-of caveat: {msg!r}"
    assert "verify the current rate" in msg, f"missing verify-rate caveat: {msg!r}"

    # reason must be appended (not replaced)
    reason = result.get("reason", "")
    assert "budget" in reason.lower(), (
        f"Original budget reason prose should still be in reason: {reason!r}"
    )
    # The estimate's message should be appended to reason
    assert est["message"] in reason or "typically runs" in reason, (
        f"Estimate message should appear in reason: {reason!r}"
    )

    # No booking reference (no negotiate() was called)
    assert "booking_ref" not in result, (
        f"No booking should have happened, got booking_ref={result.get('booking_ref')}"
    )

    print(
        f"PASS: test_missing_budget_range_suggested "
        f"[low={est['low_rounded']}¢ high={est['high_rounded']}¢ "
        f"msg={est['message'][:80]!r}]"
    )


# ---------------------------------------------------------------------------
# Test 2: budget too low (DP-infeasible) → shortfall surfaced
# ---------------------------------------------------------------------------


def test_budget_too_low_dp_infeasible_shortfall() -> None:
    """
    Budget just fits individual hotels but NOT their sum → DP infeasible →
    budget_shortfall_cents > 0, min_feasible_total_cents present.

    Scenario:
      budget = 40000¢ ($400)
      Leg-0: 1 hotel at 30000¢ — fits individually (< 40000), passes gather
      Leg-1: 1 hotel at 30000¢ — fits individually (< 40000), passes gather
      DP: min combo = 30000 + 30000 = 60000 > 40000 → INFEASIBLE
      shortfall = 60000 - 40000 = 20000¢ ($200)
    """
    MID_HOTEL_A = {
        "hotel_id": "bali-mid-a",
        "title": "Mid Hotel A",
        "city": "bali",
        "review_score": 8.0,
        "star_rating": 3.0,
        "nights": 3,
        "total_cents": 30000,
        "amenities": ["wifi"],
    }
    MID_HOTEL_B = {
        "hotel_id": "bali-mid-b",
        "title": "Mid Hotel B",
        "city": "bali",
        "review_score": 8.0,
        "star_rating": 3.0,
        "nights": 4,
        "total_cents": 30000,
        "amenities": ["wifi"],
    }

    acc_transport = SequencedMockTransport({
        "search_catalog": [
            # DP gather: leg-0 gets hotel A (30000¢ < budget 40000¢, passes)
            _catalog_result([MID_HOTEL_A]),
            # DP gather: leg-1 gets hotel B (30000¢ < budget 40000¢, passes)
            _catalog_result([MID_HOTEL_B]),
        ],
    })

    # Budget = 40000¢ — each hotel fits individually but 30000+30000=60000 > 40000
    orch = _make_orchestrator(acc_transport)
    result = orch.negotiate({
        "user_id": "u-test",
        "total_budget_cents": 40000,
        "legs": [
            {"city": "bali", "checkin": "2026-07-01", "checkout": "2026-07-04", "adults": 1},
            {"city": "bali", "checkin": "2026-07-04", "checkout": "2026-07-08", "adults": 1},
        ],
    })

    assert result["outcome"] == "cannot_satisfy", (
        f"Expected cannot_satisfy, got {result['outcome']!r}: {result}"
    )

    # Shortfall must be present and positive
    sf = result.get("budget_shortfall_cents")
    mf = result.get("min_feasible_total_cents")
    assert sf is not None, (
        f"budget_shortfall_cents missing from result keys: {list(result.keys())}"
    )
    assert sf > 0, f"Expected positive shortfall, got {sf}"
    assert mf is not None, f"min_feasible_total_cents missing"
    assert mf > 0, f"Expected positive min_feasible_total_cents, got {mf}"

    # min_feasible = Σ(leg floors) + 0 (no gate fees) = 30000 + 30000 = 60000
    assert mf == 60000, f"Expected min_feasible=60000, got {mf}"

    # shortfall = min_feasible - budget = 60000 - 40000 = 20000
    assert sf == 20000, f"Expected shortfall=20000, got {sf}"

    # reason must mention the gap
    reason = result.get("reason", "")
    assert "short" in reason.lower() or "budget" in reason.lower(), (
        f"reason should mention the gap: {reason!r}"
    )

    print(
        f"PASS: test_budget_too_low_dp_infeasible_shortfall "
        f"[shortfall={sf}¢ min_feasible={mf}¢]"
    )


# ---------------------------------------------------------------------------
# Test 3: NOT budget-driven (no_inventory) → NO shortfall
# ---------------------------------------------------------------------------


def test_not_budget_driven_no_shortfall() -> None:
    """
    search_catalog returns empty (no inventory) → cannot_satisfy with reason about
    no inventory; budget_shortfall_cents must NOT be in the result.
    """
    acc_transport = MockMerchantTransport({
        "search_catalog": _catalog_result([]),  # no hotels at all
    })

    orch = _make_orchestrator(acc_transport)
    result = orch.negotiate({
        "user_id": "u-test",
        "total_budget_cents": 500000,  # generous budget — not the problem
        "legs": [
            {"city": "bali", "checkin": "2026-07-01", "checkout": "2026-07-04", "adults": 1},
        ],
    })

    assert result["outcome"] == "cannot_satisfy", (
        f"Expected cannot_satisfy, got {result['outcome']!r}"
    )

    # NO shortfall should be attached (failure is no_inventory, not budget-driven)
    assert "budget_shortfall_cents" not in result, (
        f"budget_shortfall_cents should NOT be present for no_inventory failure: "
        f"{list(result.keys())}"
    )
    assert "min_feasible_total_cents" not in result, (
        f"min_feasible_total_cents should NOT be present for no_inventory failure: "
        f"{list(result.keys())}"
    )

    # The reason must attribute the failure to inventory, NOT nudge the (generous) budget.
    reason = result.get("reason", "")
    assert "inventory" in reason.lower() or "catalog" in reason.lower(), (
        f"no_inventory failure should attribute to inventory/catalog: {reason!r}"
    )
    assert "short of the cheapest" not in reason.lower(), (
        f"no top-up nudge should appear for a non-budget failure: {reason!r}"
    )

    print(
        f"PASS: test_not_budget_driven_no_shortfall "
        f"[reason={result['reason'][:60]!r}]"
    )


# ---------------------------------------------------------------------------
# Test 4: var-0 — byte-identical across 3 runs
# ---------------------------------------------------------------------------


def test_var0_byte_identical() -> None:
    """
    Run tests 1 and 2 three times each with frozen today; output must be byte-identical.
    """
    # --- var-0 for test 1 (missing budget range) ---
    def _run_missing_budget() -> dict:
        acc_transport = MockMerchantTransport({
            "search_catalog": _catalog_result([UBUD_GARDEN, UBUD_ALAYA]),
        })
        orch = _make_orchestrator(acc_transport)
        with _no_llm():
            return ip.negotiate_from_text(
                "6 nights Bali, beach, solo",
                orchestrator=orch,
                user_id="u-var0",
                today="2026-07-01",
            )

    r1 = _run_missing_budget()
    r2 = _run_missing_budget()
    r3 = _run_missing_budget()

    # Serialize to JSON for comparison — sort_keys for stability
    s1 = json.dumps(r1, sort_keys=True)
    s2 = json.dumps(r2, sort_keys=True)
    s3 = json.dumps(r3, sort_keys=True)

    assert s1 == s2, f"var-0 violated (run 1 != run 2) for missing-budget case"
    assert s2 == s3, f"var-0 violated (run 2 != run 3) for missing-budget case"
    assert r1.get("needs_clarification") is True, "Still needs_clarification"
    est1 = r1.get("budget_estimate")
    assert est1 is not None, "budget_estimate present"
    # Non-trivial var-0: the message AND the concrete floor are byte-stable and the
    # floor is a real non-zero value (not the degenerate $0 that would also be "stable").
    assert est1["message"] == r2["budget_estimate"]["message"] == r3["budget_estimate"]["message"], (
        "budget message must be byte-identical across runs"
    )
    assert est1["low_cents"] == UBUD_GARDEN["total_cents"] and est1["low_rounded"] > 0, (
        f"var-0 floor must be the real non-zero cheapest row, got {est1}"
    )

    # --- var-0 for test 2 (DP-infeasible shortfall) ---
    MID_A = {
        "hotel_id": "bali-mid-a",
        "title": "Mid Hotel A",
        "city": "bali",
        "review_score": 8.0,
        "star_rating": 3.0,
        "nights": 3,
        "total_cents": 30000,
        "amenities": ["wifi"],
    }
    MID_B = {
        "hotel_id": "bali-mid-b",
        "title": "Mid Hotel B",
        "city": "bali",
        "review_score": 8.0,
        "star_rating": 3.0,
        "nights": 4,
        "total_cents": 30000,
        "amenities": ["wifi"],
    }

    def _run_too_low() -> dict:
        acc = SequencedMockTransport({
            "search_catalog": [
                _catalog_result([MID_A]),
                _catalog_result([MID_B]),
            ],
        })
        orch = _make_orchestrator(acc)
        return orch.negotiate({
            "user_id": "u-var0",
            "total_budget_cents": 40000,
            "legs": [
                {"city": "bali", "checkin": "2026-07-01", "checkout": "2026-07-04", "adults": 1},
                {"city": "bali", "checkin": "2026-07-04", "checkout": "2026-07-08", "adults": 1},
            ],
        })

    t1 = _run_too_low()
    t2 = _run_too_low()
    t3 = _run_too_low()

    j1 = json.dumps(t1, sort_keys=True)
    j2 = json.dumps(t2, sort_keys=True)
    j3 = json.dumps(t3, sort_keys=True)

    assert j1 == j2, f"var-0 violated (run 1 != run 2) for DP-infeasible case"
    assert j2 == j3, f"var-0 violated (run 2 != run 3) for DP-infeasible case"
    assert t1.get("budget_shortfall_cents", 0) > 0, "shortfall present"

    print(
        f"PASS: test_var0_byte_identical "
        f"[missing_budget_hash={hash(s1)} dp_infeasible_hash={hash(j1)}]"
    )


# ---------------------------------------------------------------------------
# Tests 5-9: budget-suggestion UX fix (feat/budget-suggestion-fix)
#   5. Dest-only query → estimate fires with DEFAULT_ESTIMATE_NIGHTS assumption stated
#   6. No 2nd parse_intent call (deterministic helper, no LLM re-parse)
#   7. Existing dest+duration figures unchanged (golden regression)
#   8. Reason leads with estimate, NOT "cannot_satisfy"
#   9. var-0 byte-identical for dest-only case
# ---------------------------------------------------------------------------


def test_dest_only_estimate_fires_with_default_nights() -> None:
    """
    negotiate_from_text with city only (no budget, no duration) →
    estimate fires using DEFAULT_ESTIMATE_NIGHTS assumption, stated in reason.

    This is the core fix: before this fix, dest-only queries (e.g.
    "what is a good budget for Bali") returned a bare "cannot_satisfy" with
    no estimate. Now they get an estimate labeled with the assumed duration.
    """
    acc_transport = MockMerchantTransport({
        "search_catalog": _catalog_result([UBUD_GARDEN, UBUD_ALAYA]),
    })
    orch = _make_orchestrator(acc_transport)

    with _no_llm():
        result = ip.negotiate_from_text(
            "budget for Bali",
            orchestrator=orch,
            user_id="u-test",
            today="2026-07-01",
        )

    # Still needs_clarification — human must supply budget to proceed.
    assert result.get("needs_clarification") is True, (
        f"Expected needs_clarification=True, got: {result}"
    )

    # budget_estimate MUST be present (the fix).
    est = result.get("budget_estimate")
    assert est is not None, (
        f"Dest-only query must produce a budget_estimate: {list(result.keys())}"
    )
    assert est["low_cents"] < est["high_cents"], (
        f"low={est['low_cents']} not < high={est['high_cents']}"
    )

    # The elicitation slot must still be BUDGET (human consent preserved).
    elic = result.get("elicitation", {})
    assert elic.get("slot") == ip.ElicitationSlot.BUDGET, (
        f"Elicitation slot must remain BUDGET: {elic}"
    )

    # Reason must state the ASSUMPTION (n nights assumed) — honesty label.
    reason = result.get("reason", "")
    assert f"Assuming about {ip.DEFAULT_ESTIMATE_NIGHTS} nights" in reason, (
        f"Reason must state the assumed duration: {reason!r}"
    )

    # Reason must contain the estimate message.
    assert est["message"] in reason or "typically runs" in reason, (
        f"Estimate message should appear in reason: {reason!r}"
    )

    # Reason must still carry budget info (for the elicitation CTA).
    assert "budget" in reason.lower(), (
        f"Reason should reference budget: {reason!r}"
    )

    # No booking reference.
    assert "booking_ref" not in result, (
        f"No booking should have happened: booking_ref={result.get('booking_ref')}"
    )

    print(
        f"PASS: test_dest_only_estimate_fires_with_default_nights "
        f"[low={est['low_cents']}¢ high={est['high_cents']}¢ assumed={ip.DEFAULT_ESTIMATE_NIGHTS}n]"
    )


def test_dest_only_no_2nd_llm_call() -> None:
    """
    Fix (2): the estimate request is built DETERMINISTICALLY via
    build_estimate_request — negotiate_from_text must call parse_intent
    exactly ONCE (the initial parse), never a 2nd time for the sentinel re-parse.

    In LLM-off mode this also means exactly 0 LLM calls total; the parse_intent
    count proves the architectural fix regardless of the LLM key.
    """
    acc_transport = MockMerchantTransport({
        "search_catalog": _catalog_result([UBUD_GARDEN, UBUD_ALAYA]),
    })
    orch = _make_orchestrator(acc_transport)

    _original_parse = ip.parse_intent
    _call_count: list[int] = [0]

    def _counting_parse(*args: object, **kwargs: object) -> dict:
        _call_count[0] += 1
        return _original_parse(*args, **kwargs)

    with _no_llm(), patch.object(ip, "parse_intent", side_effect=_counting_parse):
        result = ip.negotiate_from_text(
            "budget for Bali",
            orchestrator=orch,
            user_id="u-test",
            today="2026-07-01",
        )

    assert _call_count[0] == 1, (
        f"negotiate_from_text must call parse_intent exactly ONCE "
        f"(the deterministic build_estimate_request adds no extra call), "
        f"got {_call_count[0]}"
    )
    # And the estimate still fires.
    assert result.get("budget_estimate") is not None, (
        "budget_estimate must still be present even without a 2nd parse_intent call"
    )

    print(
        f"PASS: test_dest_only_no_2nd_llm_call "
        f"[parse_intent calls={_call_count[0]} (expected 1)]"
    )


def test_existing_dest_plus_duration_figures_unchanged() -> None:
    """
    Golden regression: dest + duration ("6 nights Bali") figures must be
    byte-identical to the pre-fix path (the old sentinel re-parse produced
    the same legs as the new deterministic build_estimate_request).

    Asserts the EXACT same low_cents/high_cents as test_missing_budget_range_suggested
    to prove the architectural swap is transparent to the estimate output.
    """
    acc_transport = MockMerchantTransport({
        "search_catalog": _catalog_result([UBUD_GARDEN, UBUD_ALAYA]),
    })
    orch = _make_orchestrator(acc_transport)

    with _no_llm():
        result = ip.negotiate_from_text(
            "6 nights Bali, beach, solo",
            orchestrator=orch,
            user_id="u-test",
            today="2026-07-01",
        )

    assert result.get("needs_clarification") is True, f"Still needs_clarification: {result}"

    est = result.get("budget_estimate")
    assert est is not None, f"budget_estimate must be present: {list(result.keys())}"

    # Golden-lock: figures must match the cheapest and mid catalog rows.
    assert est["low_cents"] == UBUD_GARDEN["total_cents"], (
        f"low_cents changed: expected {UBUD_GARDEN['total_cents']}¢ "
        f"(UBUD_GARDEN), got {est['low_cents']}¢"
    )
    assert est["high_cents"] == UBUD_ALAYA["total_cents"], (
        f"high_cents changed: expected {UBUD_ALAYA['total_cents']}¢ "
        f"(UBUD_ALAYA), got {est['high_cents']}¢"
    )

    # Duration was explicit (6 nights) → NO assumption note in reason.
    reason = result.get("reason", "")
    assert "Assuming about" not in reason, (
        f"When duration is stated, no assumption note should appear: {reason!r}"
    )

    print(
        f"PASS: test_existing_dest_plus_duration_figures_unchanged "
        f"[low={est['low_cents']}¢ == UBUD_GARDEN, "
        f"high={est['high_cents']}¢ == UBUD_ALAYA]"
    )


def test_reason_leads_with_estimate_not_cannot_satisfy() -> None:
    """
    Fix (3): when an estimate is produced, reason must NOT start with
    "cannot_satisfy" — it should lead with helpful guidance.
    needs_clarification must remain True and the BUDGET elicitation intact.
    """
    acc_transport = MockMerchantTransport({
        "search_catalog": _catalog_result([UBUD_GARDEN, UBUD_ALAYA]),
    })
    orch = _make_orchestrator(acc_transport)

    with _no_llm():
        result = ip.negotiate_from_text(
            "budget for Bali",
            orchestrator=orch,
            user_id="u-test",
            today="2026-07-01",
        )

    reason = result.get("reason", "")
    assert not reason.startswith("cannot_satisfy"), (
        f"Reason must NOT start with 'cannot_satisfy' when an estimate is produced: "
        f"{reason[:100]!r}"
    )

    # Must still need clarification — no auto-proceed (human-in-the-loop).
    assert result.get("needs_clarification") is True, (
        f"needs_clarification must remain True: {result}"
    )

    # Elicitation BUDGET slot must be intact.
    elic = result.get("elicitation", {})
    assert elic.get("slot") == ip.ElicitationSlot.BUDGET, (
        f"BUDGET elicitation slot must be intact: {elic}"
    )

    # budget_estimate present.
    assert result.get("budget_estimate") is not None, (
        "budget_estimate must be present"
    )

    print(
        f"PASS: test_reason_leads_with_estimate_not_cannot_satisfy "
        f"[reason starts with: {reason[:60]!r}]"
    )


def test_var0_byte_identical_dest_only() -> None:
    """
    var-0 for the dest-only estimate: 3 runs with frozen today must produce
    byte-identical JSON output (deterministic regex, no LLM, no clock).
    """
    def _run_dest_only() -> dict:
        acc_transport = MockMerchantTransport({
            "search_catalog": _catalog_result([UBUD_GARDEN, UBUD_ALAYA]),
        })
        orch = _make_orchestrator(acc_transport)
        with _no_llm():
            return ip.negotiate_from_text(
                "budget for Bali",
                orchestrator=orch,
                user_id="u-var0",
                today="2026-07-01",
            )

    r1 = _run_dest_only()
    r2 = _run_dest_only()
    r3 = _run_dest_only()

    s1 = json.dumps(r1, sort_keys=True)
    s2 = json.dumps(r2, sort_keys=True)
    s3 = json.dumps(r3, sort_keys=True)

    assert s1 == s2, f"var-0 violated (run 1 != run 2) for dest-only case"
    assert s2 == s3, f"var-0 violated (run 2 != run 3) for dest-only case"
    assert r1.get("needs_clarification") is True, "Still needs_clarification"
    est1 = r1.get("budget_estimate")
    assert est1 is not None, "budget_estimate present"
    assert est1["message"] == r2["budget_estimate"]["message"] == r3["budget_estimate"]["message"], (
        "budget message must be byte-identical across dest-only runs"
    )
    assert est1["low_cents"] == UBUD_GARDEN["total_cents"] and est1["low_rounded"] > 0, (
        f"var-0 floor must be the real non-zero cheapest row, got {est1}"
    )

    print(
        f"PASS: test_var0_byte_identical_dest_only "
        f"[hash={hash(s1)} all 3 runs identical]"
    )


if __name__ == "__main__":
    test_missing_budget_range_suggested()
    test_budget_too_low_dp_infeasible_shortfall()
    test_not_budget_driven_no_shortfall()
    test_var0_byte_identical()
    test_dest_only_estimate_fires_with_default_nights()
    test_dest_only_no_2nd_llm_call()
    test_existing_dest_plus_duration_figures_unchanged()
    test_reason_leads_with_estimate_not_cannot_satisfy()
    test_var0_byte_identical_dest_only()
    print("\nAll tests passed.")
