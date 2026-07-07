"""
test_supper_order.py — #44 deterministic, opt-in supper selection over the
FOOD_DELIVERY merchant rows. Pure unit tests: determinism, honesty (no fabrication),
integer-cents budget filter, checkout-ready line item.
"""
from __future__ import annotations

from utils import supper_order as so

# A small fixture mirroring the merchant search_catalog(kind=FOOD_DELIVERY) results.
ROWS = [
    {"food_id": "f-ramen", "merchant_name": "Midnight Ramen", "item": "Shoyu Ramen",
     "city": "tokyo", "price_cents": 890, "delivery_fee_cents": 400, "total_cents": 1290,
     "delivery_window": "23:00-05:00", "diet": [], "currency": "USD"},
    {"food_id": "f-veg", "merchant_name": "Green Bowl", "item": "Tofu Bowl",
     "city": "tokyo", "price_cents": 600, "delivery_fee_cents": 200, "total_cents": 800,
     "delivery_window": "22:00-02:00", "diet": ["vegan"], "currency": "USD"},
]


def test_selects_cheapest_deterministically():
    r = so.build_supper_order("tokyo", ROWS)
    assert r["available"] is True
    # cheapest total = f-veg (800) beats f-ramen (1290)
    assert r["selected"]["food_id"] == "f-veg"
    assert r["total_cents"] == 800
    assert r["line_item"] == {"food_id": "f-veg", "qty": 1}
    assert r["options_count"] == 2
    # deterministic: same inputs -> identical result
    assert so.build_supper_order("tokyo", ROWS) == r


def test_tiebreak_is_food_id():
    # two rows with identical totals -> lexicographically smaller food_id wins.
    rows = [
        {"food_id": "f-b", "total_cents": 700, "price_cents": 500, "delivery_fee_cents": 200},
        {"food_id": "f-a", "total_cents": 700, "price_cents": 500, "delivery_fee_cents": 200},
    ]
    r = so.build_supper_order("x", rows)
    assert r["selected"]["food_id"] == "f-a"


def test_budget_filter_integer_cents():
    # max_cents below the cheapest (800) -> nothing affordable -> honest unavailable.
    r = so.build_supper_order("tokyo", ROWS, max_cents=500)
    assert r["available"] is False
    assert "No late-night supper" in r["note"]
    # max_cents that admits only the veg bowl
    r2 = so.build_supper_order("tokyo", ROWS, max_cents=1000)
    assert r2["available"] is True and r2["selected"]["food_id"] == "f-veg"


def test_empty_is_honest_no_fabrication():
    for empty in (None, []):
        r = so.build_supper_order("reykjavik", empty)
        assert r["available"] is False
        assert "reykjavik" in r["note"]
        assert "line_item" not in r           # never emits a checkout for a phantom
        assert r["disclosure"]                # disclosure always carried


def test_disclosure_carried_through_verbatim():
    custom = "merchant-supplied disclosure text"
    r = so.build_supper_order("tokyo", ROWS, disclosure=custom)
    assert r["disclosure"] == custom
    # default falls back to the canonical honesty disclosure (simulated inventory).
    r2 = so.build_supper_order("tokyo", ROWS)
    assert "simulated" in r2["disclosure"].lower()
    assert "not a live ele.me" in r2["disclosure"].lower()


def test_total_recomputed_from_parts_when_total_absent():
    rows = [{"food_id": "f-x", "price_cents": 450, "delivery_fee_cents": 150}]  # no total_cents
    r = so.build_supper_order("hanoi", rows)
    assert r["total_cents"] == 600  # 450 + 150, integer cents
