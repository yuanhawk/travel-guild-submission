"""
#44 — ORCHESTRATOR-level tests for the OPT-IN late-night supper hook
(_maybe_order_supper): the 2nd prepaid checkout KIND on the SAME UCP rails.

These exercise the hook directly on a TravelOrchestrator wired with a REAL
BudgetAgent backed by the in-process food-capable merchant transport
(_LocalCatalogTransport), so the order=true path drives a genuine
create_checkout -> complete_checkout against a food line {food_id, qty}.

Invariants proven:
  - APPEND-ONLY: no `supper` key unless the request opted in.
  - SELECTION-ONLY (no order flag): a supper is chosen + surfaced, NOTHING booked.
  - ORDER (order=true): a REAL prepaid food checkout completes on the budget rails.
  - HONESTY: an unavailable city / a budget veto never fabricates an order.
  - Off the itinerary path: never runs unless the trip already booked.
"""
import os
import sys
from uuid import uuid4

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from starlette.testclient import TestClient

from agents.budget_agent import BudgetAgent
from orchestration.orchestrator import TravelOrchestrator
from orchestration.server import _LocalCatalogTransport, _FOOD_DISCLOSURE


def _build_orch_with_food():
    """A minimal orchestrator: a real Budget agent + a food search, both wired to
    the SAME in-process food-capable merchant transport."""
    merchant = _LocalCatalogTransport()
    budget = BudgetAgent(merchant_transport=merchant)

    def food_search(query):
        body = {
            "jsonrpc": "2.0", "id": str(uuid4()),
            "method": "tools/call",
            "params": {"name": "search_catalog",
                       "arguments": {"meta": {"autonomy_level": "L2"}, "query": query}},
        }
        resp = merchant.handle_request(
            httpx.Request("POST", "http://ucp-merchant/api/ucp/mcp", json=body)
        )
        return resp.json().get("result", {}).get("structuredContent")

    orch = TravelOrchestrator(
        budget_client=TestClient(budget.build_app()),
        food_search=food_search,
    )
    # Per-run state the negotiate() entry would normally set.
    orch._trip_id = "t-supper"
    orch._trip_request_legs = [
        {"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04"},
    ]
    return orch


def test_not_opted_in_no_key():
    """No `supper` request → the hook adds NO key (byte-identical to today)."""
    orch = _build_orch_with_food()
    orch._supper_request = None
    result = {"outcome": "success"}
    orch._maybe_order_supper(result, user_id="u1", idempotency_key="trip-x")
    assert "supper" not in result
    print("[SUPPER] PASS — not opted in → no supper key\n")


def test_not_booked_no_key():
    """Trip did NOT book (cannot_satisfy) → never order supper, no key."""
    orch = _build_orch_with_food()
    orch._supper_request = {"order": True, "city": "bali"}
    result = {"outcome": "cannot_satisfy"}
    orch._maybe_order_supper(result, user_id="u1", idempotency_key="trip-x")
    assert "supper" not in result
    print("[SUPPER] PASS — unbooked trip → no supper\n")


def test_selection_only_books_nothing():
    """Opt-in WITHOUT order=true → selects + surfaces a supper, books NOTHING."""
    orch = _build_orch_with_food()
    orch._supper_request = {"city": "bali"}   # no order flag
    result = {"outcome": "success"}
    orch._maybe_order_supper(result, user_id="u1", idempotency_key="trip-x")
    sup = result["supper"]
    assert sup["available"] is True, sup
    assert sup["ordered"] is False, sup
    assert sup["selection_only"] is True, sup       # explicit UI marker (audit NIT)
    assert "supper_booking_ref" not in sup, sup
    # checkout-ready line item is surfaced for the UI, but NOT charged.
    assert sup["line_item"]["food_id"], sup
    assert sup["disclosure"] == _FOOD_DISCLOSURE, sup
    print("[SUPPER] PASS — selection-only surfaces pick, charges nothing\n")


def test_order_completes_real_checkout():
    """order=true → a REAL prepaid food checkout completes on the budget rails."""
    orch = _build_orch_with_food()
    orch._supper_request = {"order": True, "city": "bali", "max_cents": 5000}
    result = {"outcome": "success"}
    orch._maybe_order_supper(result, user_id="u1", idempotency_key="trip-x")
    sup = result["supper"]
    assert sup["available"] is True, sup
    assert sup["ordered"] is True, sup
    assert sup["supper_booking_ref"], sup
    assert sup["charged_cents"] == sup["total_cents"], sup
    # Deterministic pick: cheapest in-budget Bali supper (pad-thai-style set).
    print("[SUPPER] ordered ref=", sup["supper_booking_ref"],
          "charged=", sup["charged_cents"])
    print("[SUPPER] PASS — order=true completes a real 2nd prepaid checkout\n")


def test_two_different_supper_selections_get_different_idempotency_keys():
    """
    var-0 fix regression (same bug class as the main-booking _package_digest
    fix, task #49): the base idempotency_key is derived from the raw trip
    request only (no supper fields), so _place_supper_order used to build
    "{idempotency_key}-supper" for EVERY supper order regardless of which
    item/restaurant was actually chosen. Two calls that differ only in the
    chosen line_item would collide on that key; since the merchant's
    complete_checkout replay is NOT bound to checkout_id, the second
    (different) order would be silently replaced by the first order's
    booking_ref/total_cents. Assert the commit-path idempotency_key now
    differs when the line_item differs. Mocks check/commit directly (no real
    merchant round-trip needed -- this is purely about key derivation).
    """
    orch = TravelOrchestrator()
    seen_keys = []
    orch._call_budget_check = lambda payload: {"decision": "check_ok", "checkout_id": "co-x"}

    def _spy_do_commit(*, user_id, checkout_id, idempotency_key, total_cents=None):
        seen_keys.append(idempotency_key)
        return {"decision": "accept", "booking_ref": "BK-x", "checkout_id": checkout_id}

    orch._do_commit = _spy_do_commit

    orch._place_supper_order(
        supper={"line_item": {"food_id": "food-a", "qty": 1}, "total_cents": 3000},
        user_id="u1", idempotency_key="trip-x", max_cents=5000,
    )
    orch._place_supper_order(
        supper={"line_item": {"food_id": "food-b", "qty": 1}, "total_cents": 4000},
        user_id="u1", idempotency_key="trip-x", max_cents=5000,
    )

    assert len(seen_keys) == 2, seen_keys
    assert seen_keys[0] != seen_keys[1], (
        f"two DIFFERENT supper selections must not share a commit idempotency_key: {seen_keys}"
    )
    assert all(k.startswith("trip-x-supper:") for k in seen_keys), seen_keys
    print("[SUPPER] PASS — different supper selections get different idempotency keys\n")


def test_same_supper_selection_replayed_keeps_same_idempotency_key():
    """The flip side: a genuine retry of the SAME selection must still produce
    the SAME key (so the merchant's double-click protection still works)."""
    orch = TravelOrchestrator()
    seen_keys = []
    orch._call_budget_check = lambda payload: {"decision": "check_ok", "checkout_id": "co-x"}

    def _spy_do_commit(*, user_id, checkout_id, idempotency_key, total_cents=None):
        seen_keys.append(idempotency_key)
        return {"decision": "accept", "booking_ref": "BK-x", "checkout_id": checkout_id}

    orch._do_commit = _spy_do_commit
    same_item = {"line_item": {"food_id": "food-a", "qty": 1}, "total_cents": 3000}
    orch._place_supper_order(supper=same_item, user_id="u1", idempotency_key="trip-x", max_cents=5000)
    orch._place_supper_order(supper=same_item, user_id="u1", idempotency_key="trip-x", max_cents=5000)

    assert seen_keys[0] == seen_keys[1], seen_keys
    print("[SUPPER] PASS — identical supper selection retries keep the same idempotency key\n")


def test_unavailable_city_is_honest():
    """A city with no seeded supper → available=False, honest note, no order."""
    orch = _build_orch_with_food()
    orch._supper_request = {"order": True, "city": "nowhere-town"}
    result = {"outcome": "success"}
    orch._maybe_order_supper(result, user_id="u1", idempotency_key="trip-x")
    sup = result["supper"]
    assert sup["available"] is False, sup
    assert sup["ordered"] is False, sup
    assert "supper_booking_ref" not in sup, sup
    assert "nothing was charged" in sup.get("order_note", ""), sup
    print("[SUPPER] PASS — unavailable city is honest, books nothing\n")


def test_over_budget_vetoes_no_charge():
    """A max_cents below every supper total → no in-budget pick (available=False);
    HONESTY: nothing fabricated, nothing charged."""
    orch = _build_orch_with_food()
    orch._supper_request = {"order": True, "city": "bali", "max_cents": 1}
    result = {"outcome": "success"}
    orch._maybe_order_supper(result, user_id="u1", idempotency_key="trip-x")
    sup = result["supper"]
    assert sup["available"] is False, sup
    assert sup["ordered"] is False, sup
    print("[SUPPER] PASS — sub-budget ceiling vetoes cleanly, no charge\n")


def test_default_city_is_last_leg():
    """No explicit city → defaults to the LAST leg's city (trip destination)."""
    orch = _build_orch_with_food()
    orch._trip_request_legs = [
        {"city": "bangkok", "checkin": "2026-10-01", "checkout": "2026-10-03"},
        {"city": "bali", "checkin": "2026-10-03", "checkout": "2026-10-06"},
    ]
    orch._supper_request = {"city": None}   # opt-in, fall back to last leg = bali
    result = {"outcome": "success"}
    orch._maybe_order_supper(result, user_id="u1", idempotency_key="trip-x")
    sup = result["supper"]
    assert sup["available"] is True, sup
    assert sup["city"] == "bali", sup
    print("[SUPPER] PASS — supper city defaults to last leg\n")


if __name__ == "__main__":
    test_not_opted_in_no_key()
    test_not_booked_no_key()
    test_selection_only_books_nothing()
    test_order_completes_real_checkout()
    test_unavailable_city_is_honest()
    test_over_budget_vetoes_no_charge()
    test_default_city_is_last_leg()
    print("ALL SUPPER SOCIETY TESTS PASSED")
