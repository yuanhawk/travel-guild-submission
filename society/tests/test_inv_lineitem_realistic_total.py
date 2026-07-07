"""
test_inv_lineitem_realistic_total.py — MONEY-SAFETY INVARIANT: line-item
realistic total (§19.5 / §16.3 / §17).

INVARIANT
---------
When LineItemAssembler receives a realistic fee set of:
  - eVisa fee           (compliance / visa   / leg-0)   = 8 200 cents  (~$82)
  - mandatory vaccine   (health     / vaccine / leg-0)   = 11 100 cents (~$111)
  - insurance premium   (insurance  / premium / _package)= 9 420 cents  (~$94.20)

the assembled result MUST satisfy ALL of:
  1. total_usd_cents() == 8200 + 11100 + 9420 == 28720 exactly (no rounding,
     no off-by-one, no double-charge, no partial drop).
  2. Each per-line usd_cents is preserved verbatim (8200, 11100, 9420) in the
     assembled list — the numbers that feed the budget veto (§17) arrive intact.
  3. The assembled list is ordered by canonical kind (visa < vaccine < premium)
     as documented by _KIND_ORDER, so the budget consumer always sees a
     predictable, stable shape.
  4. The canonical hotel_id for each fee is the synthetic addressable key
     ``fee:<source>:<kind>:<leg>`` — used as the idempotent address for
     re-emission guards (§16.3).
  5. Insertion-order independence: adding the same three fees in the reverse
     order yields the SAME assembled list and the SAME total.
  6. Re-emission idempotency at realistic magnitudes: re-emitting a fee with
     an UPDATED amount REPLACES (not adds to) the prior entry, so the total
     never double-charges.

WHY EXISTING TESTS ARE INSUFFICIENT
-------------------------------------
test_line_item_assembler_cov.py uses toy values (100, 500, 1000, 6200, 12000
cents) that do NOT reflect the real magnitudes an agent emits in a live trip
scenario. A magnitude-sensitive bug (e.g. silent int overflow, wrong usd_cents
key extraction at higher values, or an accidental cast-to-float rounding) would
pass toy-value tests while failing here.

var-0: pure Python, no I/O, no LLM, no network.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.contracts import make_money
from utils.line_item_assembler import (
    PACKAGE_LEG,
    LineItemAssembler,
    assemble_line_items,
)

# ---------------------------------------------------------------------------
# Realistic fee magnitudes (the exact values under test — do NOT change these
# without also updating the invariant comment above).
# ---------------------------------------------------------------------------

# ~$82 — Indonesia / Thailand eVisa single-entry (realistic 2026 range)
_VISA_EVISA_CENTS = 8_200

# ~$111 — Yellow Fever mandatory-entry vaccine
# (matches the DC-health benchmark re-baseline from task #71)
_VACCINE_MANDATORY_CENTS = 11_100

# ~$94.20 — 14-day comprehensive travel insurance premium
_INSURANCE_PREMIUM_CENTS = 9_420

# The exact expected total — arithmetic ground truth, computed once here so
# every test assertion references it rather than re-adding inline.
_EXPECTED_TOTAL_CENTS = _VISA_EVISA_CENTS + _VACCINE_MANDATORY_CENTS + _INSURANCE_PREMIUM_CENTS
# == 28_720


def _money(usd_cents: int) -> dict:
    """Build a valid make_money() tag for a USD amount at these magnitudes."""
    return make_money(
        native_cents=usd_cents,
        currency="USD",
        usd_cents=usd_cents,
        fx_rate=1.0,
        fx_source="seeded",
        fetched_at="2026-06-22",
    )


def _build_assembler_canonical_order() -> LineItemAssembler:
    """Add visa → vaccine → premium (canonical insertion order)."""
    a = LineItemAssembler()
    a.add_fee(
        source="compliance",
        kind="visa",
        leg="leg-0",
        money=_money(_VISA_EVISA_CENTS),
        description="Indonesia eVisa single-entry",
    )
    a.add_fee(
        source="health",
        kind="vaccine",
        leg="leg-0",
        money=_money(_VACCINE_MANDATORY_CENTS),
        description="Yellow Fever vaccine (mandatory entry requirement)",
    )
    a.add_fee(
        source="insurance",
        kind="premium",
        leg=PACKAGE_LEG,
        money=_money(_INSURANCE_PREMIUM_CENTS),
        description="Comprehensive travel insurance 14 days",
    )
    return a


def _build_assembler_reverse_order() -> LineItemAssembler:
    """Add premium → vaccine → visa (reverse of canonical insertion order)."""
    a = LineItemAssembler()
    a.add_fee(
        source="insurance",
        kind="premium",
        leg=PACKAGE_LEG,
        money=_money(_INSURANCE_PREMIUM_CENTS),
        description="Comprehensive travel insurance 14 days",
    )
    a.add_fee(
        source="health",
        kind="vaccine",
        leg="leg-0",
        money=_money(_VACCINE_MANDATORY_CENTS),
        description="Yellow Fever vaccine (mandatory entry requirement)",
    )
    a.add_fee(
        source="compliance",
        kind="visa",
        leg="leg-0",
        money=_money(_VISA_EVISA_CENTS),
        description="Indonesia eVisa single-entry",
    )
    return a


# ---------------------------------------------------------------------------
# INV-1: total_usd_cents() == exact arithmetic sum at realistic magnitudes
# ---------------------------------------------------------------------------


def test_realistic_total_equals_exact_arithmetic_sum():
    """
    total_usd_cents() must equal 8200 + 11100 + 9420 == 28720 exactly.

    Catches: any rounding, float contamination, or silent truncation that toy
    values (< 2000 cents) would not expose at typical IEEE-754 precision.
    """
    a = _build_assembler_canonical_order()
    total = a.total_usd_cents()
    assert total == _EXPECTED_TOTAL_CENTS, (
        f"total_usd_cents() returned {total}, expected {_EXPECTED_TOTAL_CENTS} "
        f"(visa={_VISA_EVISA_CENTS} + vaccine={_VACCINE_MANDATORY_CENTS} "
        f"+ insurance={_INSURANCE_PREMIUM_CENTS})"
    )


def test_realistic_total_is_integer_not_float():
    """
    total_usd_cents() must return a plain Python int, not a float.

    A silent cast to float (e.g. from an fx_rate multiplication path) can
    introduce sub-cent drift and would break the integer budget-veto comparison.
    """
    a = _build_assembler_canonical_order()
    total = a.total_usd_cents()
    assert isinstance(total, int), (
        f"total_usd_cents() returned type {type(total).__name__!r}, expected int; "
        f"float drift would corrupt the §17 budget veto"
    )


# ---------------------------------------------------------------------------
# INV-2: per-line usd_cents values are preserved verbatim in assembled list
# ---------------------------------------------------------------------------


def test_per_line_usd_cents_preserved_visa():
    """
    The visa line item must carry usd_cents == 8200 verbatim.

    The budget-veto path reads per-line usd_cents, not only the total.
    Corruption here silently wrong-vetos (or fails to veto) the package.
    """
    a = _build_assembler_canonical_order()
    items = a.assemble()
    visa_items = [it for it in items if it["kind"] == "visa"]
    assert len(visa_items) == 1, f"expected 1 visa item, got {len(visa_items)}"
    assert visa_items[0]["usd_cents"] == _VISA_EVISA_CENTS, (
        f"visa usd_cents={visa_items[0]['usd_cents']}, expected {_VISA_EVISA_CENTS}"
    )


def test_per_line_usd_cents_preserved_vaccine():
    """
    The vaccine line item must carry usd_cents == 11100 verbatim.

    At $111 this is the largest mandatory-fee contributor; a truncation or
    rounding here materially corrupts the §17 budget calculation.
    """
    a = _build_assembler_canonical_order()
    items = a.assemble()
    vaccine_items = [it for it in items if it["kind"] == "vaccine"]
    assert len(vaccine_items) == 1, f"expected 1 vaccine item, got {len(vaccine_items)}"
    assert vaccine_items[0]["usd_cents"] == _VACCINE_MANDATORY_CENTS, (
        f"vaccine usd_cents={vaccine_items[0]['usd_cents']}, expected {_VACCINE_MANDATORY_CENTS}"
    )


def test_per_line_usd_cents_preserved_premium():
    """
    The premium line item must carry usd_cents == 9420 verbatim.

    Insurance premium is a package-level fee (leg=PACKAGE_LEG); the per-line
    value feeds the insurance hard-veto (§72 task).
    """
    a = _build_assembler_canonical_order()
    items = a.assemble()
    premium_items = [it for it in items if it["kind"] == "premium"]
    assert len(premium_items) == 1, f"expected 1 premium item, got {len(premium_items)}"
    assert premium_items[0]["usd_cents"] == _INSURANCE_PREMIUM_CENTS, (
        f"premium usd_cents={premium_items[0]['usd_cents']}, expected {_INSURANCE_PREMIUM_CENTS}"
    )


def test_per_line_money_usd_cents_matches_toplevel_usd_cents():
    """
    item["usd_cents"] (the top-level extraction) must equal
    item["money"]["usd_cents"] (the canonical money tag) for every fee line.

    These are the two code paths the budget-veto logic may read; they must be
    in sync so neither path can produce a different veto decision.
    """
    a = _build_assembler_canonical_order()
    items = a.assemble()
    for item in items:
        if "money" in item:
            assert item["usd_cents"] == item["money"]["usd_cents"], (
                f"item kind={item['kind']!r}: top-level usd_cents={item['usd_cents']} "
                f"!= money['usd_cents']={item['money']['usd_cents']}"
            )


# ---------------------------------------------------------------------------
# INV-3: assembled list is in canonical kind order (visa < vaccine < premium)
# ---------------------------------------------------------------------------


def test_assembled_kind_order_visa_vaccine_premium():
    """
    The assembled list for this realistic fee set must be ordered
    [visa, vaccine, premium] — the _KIND_ORDER canonical sequence
    (kind orders 2, 4, 5).

    A wrong ordering means the budget consumer iterates fees in a non-
    deterministic or semantically-wrong sequence, which breaks the §19.5
    byte-identical-given-same-set guarantee.
    """
    a = _build_assembler_canonical_order()
    items = a.assemble()
    assert len(items) == 3, f"expected 3 items, got {len(items)}"
    kinds = [it["kind"] for it in items]
    assert kinds == ["visa", "vaccine", "premium"], (
        f"assembled kind order {kinds!r} != ['visa', 'vaccine', 'premium']"
    )


def test_assembled_count_is_exactly_three():
    """
    Exactly 3 items are assembled from 3 distinct (source, kind, leg) keys.

    Any duplication would indicate a double-charge bug (§16.3).
    """
    a = _build_assembler_canonical_order()
    items = a.assemble()
    assert len(items) == 3, (
        f"expected exactly 3 assembled items (visa + vaccine + premium), "
        f"got {len(items)}: {[it['kind'] for it in items]}"
    )
    # len(assembler) must also match
    assert len(a) == 3, f"len(assembler)={len(a)} != 3"


# ---------------------------------------------------------------------------
# INV-4: synthetic hotel_id (idempotency address) is correctly formed
# ---------------------------------------------------------------------------


def test_visa_synthetic_hotel_id():
    """Visa fee hotel_id must be 'fee:compliance:visa:leg-0'."""
    a = _build_assembler_canonical_order()
    items = a.assemble()
    visa_item = next(it for it in items if it["kind"] == "visa")
    assert visa_item["hotel_id"] == "fee:compliance:visa:leg-0", (
        f"visa hotel_id={visa_item['hotel_id']!r}"
    )


def test_vaccine_synthetic_hotel_id():
    """Vaccine fee hotel_id must be 'fee:health:vaccine:leg-0'."""
    a = _build_assembler_canonical_order()
    items = a.assemble()
    vaccine_item = next(it for it in items if it["kind"] == "vaccine")
    assert vaccine_item["hotel_id"] == "fee:health:vaccine:leg-0", (
        f"vaccine hotel_id={vaccine_item['hotel_id']!r}"
    )


def test_premium_synthetic_hotel_id():
    """Premium fee hotel_id must be 'fee:insurance:premium:_package'."""
    a = _build_assembler_canonical_order()
    items = a.assemble()
    premium_item = next(it for it in items if it["kind"] == "premium")
    assert premium_item["hotel_id"] == "fee:insurance:premium:_package", (
        f"premium hotel_id={premium_item['hotel_id']!r}"
    )


# ---------------------------------------------------------------------------
# INV-5: insertion-order independence at realistic magnitudes
# ---------------------------------------------------------------------------


def test_insertion_order_independence_assembled_list():
    """
    Adding fees in canonical vs reverse order produces the SAME assembled list.

    Insertion-order variance at the assembler level would mean the budget-veto
    input changes depending on agent timing — a non-deterministic money path.
    """
    items_canonical = _build_assembler_canonical_order().assemble()
    items_reverse = _build_assembler_reverse_order().assemble()
    assert items_canonical == items_reverse, (
        "assembled list differs by insertion order — non-deterministic money path:\n"
        f"  canonical: {[it['kind'] for it in items_canonical]}\n"
        f"  reverse:   {[it['kind'] for it in items_reverse]}"
    )


def test_insertion_order_independence_total():
    """
    total_usd_cents() == 28720 regardless of insertion order.

    Insertion order must NEVER affect the budget-veto sum.
    """
    total_canonical = _build_assembler_canonical_order().total_usd_cents()
    total_reverse = _build_assembler_reverse_order().total_usd_cents()
    assert total_canonical == _EXPECTED_TOTAL_CENTS, (
        f"canonical total={total_canonical}, expected {_EXPECTED_TOTAL_CENTS}"
    )
    assert total_reverse == _EXPECTED_TOTAL_CENTS, (
        f"reverse total={total_reverse}, expected {_EXPECTED_TOTAL_CENTS}"
    )
    assert total_canonical == total_reverse, (
        f"totals differ by insertion order: canonical={total_canonical} vs reverse={total_reverse}"
    )


# ---------------------------------------------------------------------------
# INV-6: re-emission idempotency — replace, never double-charge
# ---------------------------------------------------------------------------


def test_reemission_replaces_not_doubles_visa():
    """
    Re-emitting the visa fee with a CORRECTED amount (e.g. 8500 instead of 8200)
    REPLACES the prior entry — total reflects the NEW amount, not old+new.

    This is the §16.3 cascade-lesson invariant at the fee level.
    """
    a = _build_assembler_canonical_order()  # adds visa=8200
    # Simulate compliance agent re-emitting a corrected visa fee
    corrected_visa_cents = 8_500
    a.add_fee(
        source="compliance",
        kind="visa",
        leg="leg-0",
        money=_money(corrected_visa_cents),
        description="Indonesia eVisa single-entry (corrected amount)",
    )
    # Still 3 items — not 4
    assert len(a) == 3, (
        f"re-emission should not add a new item; got len={len(a)}"
    )
    items = a.assemble()
    assert len(items) == 3, f"assembled should still be 3 items, got {len(items)}"

    # Total must use the CORRECTED amount, not old+new
    expected_after_correction = corrected_visa_cents + _VACCINE_MANDATORY_CENTS + _INSURANCE_PREMIUM_CENTS
    total = a.total_usd_cents()
    assert total == expected_after_correction, (
        f"after re-emission total={total}, expected {expected_after_correction} "
        f"(would be {total - expected_after_correction} too high if doubling)"
    )
    # The visa line item reflects the corrected amount
    visa_item = next(it for it in items if it["kind"] == "visa")
    assert visa_item["usd_cents"] == corrected_visa_cents, (
        f"visa usd_cents after re-emission={visa_item['usd_cents']}, "
        f"expected corrected {corrected_visa_cents}"
    )


def test_reemission_does_not_corrupt_other_line_items():
    """
    Re-emitting the visa fee must not alter the vaccine or premium line items.

    Idempotency keying must be tight: a replacement of key (compliance, visa,
    leg-0) must not touch (health, vaccine, leg-0) or (insurance, premium, _pkg).
    """
    a = _build_assembler_canonical_order()
    a.add_fee(
        source="compliance",
        kind="visa",
        leg="leg-0",
        money=_money(8_500),
        description="Re-emitted visa",
    )
    items = a.assemble()
    vaccine_item = next(it for it in items if it["kind"] == "vaccine")
    premium_item = next(it for it in items if it["kind"] == "premium")
    assert vaccine_item["usd_cents"] == _VACCINE_MANDATORY_CENTS, (
        f"vaccine usd_cents mutated to {vaccine_item['usd_cents']} after visa re-emission"
    )
    assert premium_item["usd_cents"] == _INSURANCE_PREMIUM_CENTS, (
        f"premium usd_cents mutated to {premium_item['usd_cents']} after visa re-emission"
    )


# ---------------------------------------------------------------------------
# INV-7: functional wrapper assemble_line_items() reproduces the same result
# ---------------------------------------------------------------------------


def test_functional_wrapper_realistic_total():
    """
    assemble_line_items(fees=[...]) must yield the same total and per-line values
    as the class-based path, at realistic magnitudes.

    The functional wrapper is a convenience seam used by higher-level callers
    (e.g. Budget agent's one-call assembly). If it diverges from the class path
    it silently introduces a second money-calculation path with no parity guarantee.
    """
    items = assemble_line_items(
        fees=[
            {
                "source": "compliance",
                "kind": "visa",
                "leg": "leg-0",
                "money": _money(_VISA_EVISA_CENTS),
                "description": "Indonesia eVisa single-entry",
            },
            {
                "source": "health",
                "kind": "vaccine",
                "leg": "leg-0",
                "money": _money(_VACCINE_MANDATORY_CENTS),
                "description": "Yellow Fever vaccine (mandatory entry requirement)",
            },
            {
                "source": "insurance",
                "kind": "premium",
                "leg": PACKAGE_LEG,
                "money": _money(_INSURANCE_PREMIUM_CENTS),
                "description": "Comprehensive travel insurance 14 days",
            },
        ]
    )
    assert len(items) == 3, f"expected 3 items from functional wrapper, got {len(items)}"
    # Per-line values
    by_kind = {it["kind"]: it for it in items}
    assert by_kind["visa"]["usd_cents"] == _VISA_EVISA_CENTS
    assert by_kind["vaccine"]["usd_cents"] == _VACCINE_MANDATORY_CENTS
    assert by_kind["premium"]["usd_cents"] == _INSURANCE_PREMIUM_CENTS
    # Total (computed manually from the assembled list, as the wrapper has no total_usd_cents)
    total_from_wrapper = sum(it["usd_cents"] for it in items)
    assert total_from_wrapper == _EXPECTED_TOTAL_CENTS, (
        f"functional wrapper sum={total_from_wrapper}, expected {_EXPECTED_TOTAL_CENTS}"
    )


def test_functional_wrapper_matches_class_path_exactly():
    """
    assemble_line_items() must produce a list byte-identical to the class path
    for the same realistic inputs.
    """
    class_items = _build_assembler_canonical_order().assemble()
    wrapper_items = assemble_line_items(
        fees=[
            {
                "source": "compliance",
                "kind": "visa",
                "leg": "leg-0",
                "money": _money(_VISA_EVISA_CENTS),
                "description": "Indonesia eVisa single-entry",
            },
            {
                "source": "health",
                "kind": "vaccine",
                "leg": "leg-0",
                "money": _money(_VACCINE_MANDATORY_CENTS),
                "description": "Yellow Fever vaccine (mandatory entry requirement)",
            },
            {
                "source": "insurance",
                "kind": "premium",
                "leg": PACKAGE_LEG,
                "money": _money(_INSURANCE_PREMIUM_CENTS),
                "description": "Comprehensive travel insurance 14 days",
            },
        ]
    )
    assert class_items == wrapper_items, (
        "assemble_line_items() and LineItemAssembler.assemble() diverge for "
        "identical realistic inputs — dual money-path parity broken"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"[OK] {name}")
            except Exception as e:
                print(f"[FAIL] {name}: {e}")
                raise
    print("\n[ALL] PASS")
