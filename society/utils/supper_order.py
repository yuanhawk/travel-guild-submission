"""
supper_order.py — #44 society-side helper for the FOOD_DELIVERY UCP checkout kind.

The Go merchant (ucp-merchant) added FOOD_DELIVERY as a 2nd real prepaid checkout
KIND on the SAME rails as hotels (RFC 9421 signed → HITL consent → integer-cents
budget veto → complete_checkout). This module is the DETERMINISTIC, OPT-IN
society-side selector that turns a late-night supper request into a single
checkout line item the existing Budget agent books verbatim (budget_agent forwards
line_items unchanged, so a {food_id, qty} line rides the hotel rails as-is).

DESIGN CONTRACT
---------------
- OPT-IN: the orchestrator only invokes this when the trip_request asks for it.
  When it is not invoked, itinerary output is byte-identical (APPEND-ONLY, mirrors
  the #45 coverage-gap and #30 day-planner hooks).
- HONESTY (fail-conservative): this module NEVER fabricates a supper. It selects
  ONLY among the real seeded rows the merchant returned. A city with no rows →
  available=False + an honest "no late-night supper available" note, never a made-
  up merchant/price. The merchant's own simulated-inventory disclosure is carried
  through verbatim (we do not invent provenance).
- var-0: pure function, integer cents only, deterministic total order (no
  wall-clock, no random, no dict-iteration on the output path). Same inputs →
  byte-identical selection + line item.

The LIVE merchant search (search_catalog kind=FOOD_DELIVERY) happens in the
orchestrator (like every other agent call); this module is pure over its result so
it is trivially testable and deterministic.
"""
from __future__ import annotations

from typing import Any

# Carried through to the user verbatim from the merchant; defined here as the
# canonical fallback so the society layer never SILENTLY drops the disclosure even
# if a caller forgets to pass the merchant's copy. Honesty: simulated inventory,
# real transaction, no partnership claimed.
SUPPER_DISCLOSURE = (
    "Food-delivery inventory is honestly simulated for the demo; the transaction "
    "is a real UCP prepaid checkout (RFC 9421 signed, HITL consent, integer-cents "
    "budget veto). NOT a live Ele.me integration; no Ele.me/AliCloud partnership "
    "claimed."
)


def _total_cents(row: dict[str, Any]) -> int:
    """Integer-cents total for a merchant food row = item price + delivery fee.

    The merchant already returns total_cents; we recompute from the parts as a
    defensive integer-only fallback (never trust a float, never fabricate)."""
    if isinstance(row.get("total_cents"), int):
        return int(row["total_cents"])
    return int(row.get("price_cents", 0)) + int(row.get("delivery_fee_cents", 0))


def _select_key(row: dict[str, Any]) -> tuple[int, str]:
    """Deterministic total order over food rows: cheapest first, then food_id as a
    stable tiebreak (food_id is unique in the seed → the order is total)."""
    return (_total_cents(row), str(row.get("food_id", "")))


def build_supper_order(
    city: str,
    food_results: list[dict[str, Any]] | None,
    *,
    max_cents: int | None = None,
    disclosure: str | None = None,
) -> dict[str, Any]:
    """
    Deterministically choose a single late-night supper for ``city`` from the REAL
    rows the merchant returned, and emit a checkout-ready line item.

    Args:
        city:         destination city the supper is for (echoed back; lower-cased
                      comparison is the merchant's job — we do not re-filter here).
        food_results: the merchant search_catalog(kind=FOOD_DELIVERY) "results"
                      list (real seeded rows). None/empty → honest "unavailable".
        max_cents:    optional integer-cents ceiling; rows whose (price+fee) total
                      exceeds it are excluded (the merchant may also pre-filter).
        disclosure:   the merchant's disclosure string, carried through verbatim;
                      falls back to SUPPER_DISCLOSURE so it is never dropped.

    Returns a dict:
        available True:  {available, city, selected, line_item, options_count,
                          total_cents, currency, disclosure}
        available False: {available: False, city, note, disclosure}

    HONESTY: with no in-budget real row, returns available=False — never invents a
    supper. var-0: deterministic selection; integer cents only.
    """
    disc = disclosure or SUPPER_DISCLOSURE
    rows = list(food_results or [])

    # Budget filter in integer cents (defensive; merchant may already have applied).
    if max_cents is not None:
        rows = [r for r in rows if _total_cents(r) <= int(max_cents)]

    if not rows:
        return {
            "available": False,
            "city": city,
            "note": f"No late-night supper available in {city} within the given constraints.",
            "disclosure": disc,
        }

    # Deterministic pick: cheapest in-budget real row, food_id tiebreak.
    rows.sort(key=_select_key)
    chosen = rows[0]
    total = _total_cents(chosen)

    return {
        "available": True,
        "city": city,
        "selected": {
            "food_id": chosen.get("food_id"),
            "merchant_name": chosen.get("merchant_name"),
            "item": chosen.get("item"),
            "delivery_window": chosen.get("delivery_window"),
            "diet": chosen.get("diet", []),
            "price_cents": int(chosen.get("price_cents", 0)),
            "delivery_fee_cents": int(chosen.get("delivery_fee_cents", 0)),
        },
        # The checkout line item Budget forwards verbatim to the merchant (qty=1).
        "line_item": {"food_id": chosen.get("food_id"), "qty": 1},
        "options_count": len(rows),
        "total_cents": total,
        "currency": chosen.get("currency", "USD"),
        "disclosure": disc,
    }
