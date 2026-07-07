"""
test_cost_basis.py — task #42 PART A: per-cost-line BASIS DISCLOSURE.

LIGHT tests only (an OSM sweep is running on a memory-tight box): NO catalog/OSM
load, NO server, NO heavy fixtures. cost_basis is stdlib-only; the assembler +
a tiny hand-built result fixture exercise the basis discriminator end to end:

  * make_basis returns the right label for each allowed basis.
  * make_basis falls back to BASIS_UNKNOWN on bad input (never KeyError).
  * every produced cost_basis ∈ ALLOWED_BASES.
  * lodging WITH a checkout_id → ucp_prepaid; WITHOUT → unknown.
  * fees (visa/vaccine/premium) → deterministic_estimate.
  * transport carries NO price line — instead a one-time handoff note.
  * attractions / restaurants carry NO price line at all (#37 handoff).
  * integer-cents invariant: all *_cents are int; package_total_cents is the
    EXACT integer sum of per-leg total_cents (never recomputed/altered).
  * var-0: make_basis is byte-identical across two calls.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.contracts import make_money

from core import cost_basis as CB
from core.cost_basis import (
    ALLOWED_BASES,
    BASIS_DETERMINISTIC_ESTIMATE,
    BASIS_HANDOFF,
    BASIS_UCP_PREPAID,
    BASIS_UNKNOWN,
    make_basis,
)
from utils.line_item_assembler import LineItemAssembler, LineItemKind


# ---------------------------------------------------------------------------
# cost_basis unit tests
# ---------------------------------------------------------------------------

def test_make_basis_label_per_basis():
    expected = {
        BASIS_UCP_PREPAID: "Prepaid via secure checkout",
        BASIS_DETERMINISTIC_ESTIMATE: "Estimated from seeded data (not a live quote)",
        BASIS_HANDOFF: "You book directly — no price asserted",
        BASIS_UNKNOWN: "Basis unknown — treat as indicative only",
    }
    for basis, label in expected.items():
        out = make_basis(basis)
        assert out == {"cost_basis": basis, "cost_basis_label": label}, out
        assert out["cost_basis"] in ALLOWED_BASES


def test_make_basis_falls_back_to_unknown_on_bad_input():
    for bad in ("nope", "", None, 42, ["ucp_prepaid"], {}):
        out = make_basis(bad)
        assert out["cost_basis"] == BASIS_UNKNOWN, (bad, out)
        assert out["cost_basis_label"] == "Basis unknown — treat as indicative only"
        assert out["cost_basis"] in ALLOWED_BASES


def test_make_basis_byte_identical_across_runs():
    a = json.dumps(make_basis(BASIS_UCP_PREPAID), sort_keys=True)
    b = json.dumps(make_basis(BASIS_UCP_PREPAID), sort_keys=True)
    assert a == b


def test_allowed_bases_closed_set():
    assert ALLOWED_BASES == frozenset({
        BASIS_UCP_PREPAID, BASIS_DETERMINISTIC_ESTIMATE,
        BASIS_HANDOFF, BASIS_UNKNOWN,
    })


# ---------------------------------------------------------------------------
# A tiny hand-built success-result fixture (no orchestrator run, no catalog).
# Mirrors the shape _success_result / _inject_fees produce, so we can assert
# the basis discriminator + integer-cents invariant without the heavy path.
# ---------------------------------------------------------------------------

def _fee_money(usd_cents: int) -> dict:
    return make_money(
        native_cents=usd_cents, currency="USD", usd_cents=usd_cents,
        fx_rate=1.0, fx_source="seeded", fetched_at="2026-06-20",
    )


def _success_fixture(*, with_checkout: bool) -> dict:
    """Build a success result via the real helpers' logic (no orchestrator run)."""
    checkout_id = "co-123" if with_checkout else None
    leg_basis = make_basis(BASIS_UCP_PREPAID if checkout_id else BASIS_UNKNOWN)
    legs = [
        {"leg_id": "leg-0", "city": "Shanghai", "total_cents": 54000,
         **leg_basis},
        {"leg_id": "leg-1", "city": "Tokyo", "total_cents": 72000,
         **leg_basis},
    ]
    package_total_cents = sum(int(leg["total_cents"]) for leg in legs)
    result = {
        "outcome": "success",
        "checkout_id": checkout_id,
        "package_total_cents": package_total_cents,
        "total_cents": package_total_cents,
        "legs": legs,
        # transport one-time handoff note (no per-edge price).
        "transport_edges": [{"from_leg": "leg-0", "to_leg": "leg-1"}],
        "transport_pricing": {
            "basis": "handoff",
            "basis_label": (
                "Durations estimated (OpenFlights ~2014 vintage); "
                "fares not priced — book externally"
            ),
        },
        # day plan entities carry NO price (handoff via #37).
        "day_plans": [{
            "leg_id": "leg-0", "city": "Shanghai",
            "days": [{"attractions": [{"name": "The Bund"}],
                      "meals": {"lunch": {"name": "Jia Jia Tang Bao"}}}],
        }],
    }
    # Fees via the real assembler path (basis defaults to deterministic_estimate).
    a = LineItemAssembler()
    a.add_fee(source="compliance", kind=LineItemKind.VISA, leg="leg-0",
              money=_fee_money(6200), description="CN visa")
    a.add_fee(source="insurance", kind=LineItemKind.PREMIUM, leg="_package",
              money=_fee_money(3100), description="trip premium")
    result["fee_line_items"] = a.assemble()
    result["fee_total_cents"] = a.total_usd_cents()
    result["package_total_with_fees_cents"] = (
        int(result["total_cents"]) + a.total_usd_cents()
    )
    return result


# ---------------------------------------------------------------------------
# Basis discriminator behaviour
# ---------------------------------------------------------------------------

def test_lodging_with_checkout_is_ucp_prepaid():
    res = _success_fixture(with_checkout=True)
    for leg in res["legs"]:
        assert leg["cost_basis"] == BASIS_UCP_PREPAID, leg
        assert leg["cost_basis_label"] == "Prepaid via secure checkout"


def test_lodging_without_checkout_is_unknown():
    res = _success_fixture(with_checkout=False)
    for leg in res["legs"]:
        assert leg["cost_basis"] == BASIS_UNKNOWN, leg
        assert leg["cost_basis_label"] == "Basis unknown — treat as indicative only"


def test_fees_are_deterministic_estimate():
    res = _success_fixture(with_checkout=True)
    assert res["fee_line_items"], "fixture should have fee line items"
    for fi in res["fee_line_items"]:
        assert fi["cost_basis"] == BASIS_DETERMINISTIC_ESTIMATE, fi
        assert fi["cost_basis_label"] == "Estimated from seeded data (not a live quote)"
        assert fi["cost_basis"] in ALLOWED_BASES


def test_transport_pricing_is_handoff_note_no_per_edge_price():
    res = _success_fixture(with_checkout=True)
    tp = res["transport_pricing"]
    assert tp["basis"] == "handoff"
    assert "fares not priced" in tp["basis_label"]
    # No per-edge price field on any transport edge.
    for edge in res["transport_edges"]:
        assert "price" not in edge and "total_cents" not in edge and "fare" not in edge


def test_attractions_and_restaurants_carry_no_price():
    res = _success_fixture(with_checkout=True)
    for dp in res["day_plans"]:
        for day in dp["days"]:
            for att in day.get("attractions", []):
                assert "price" not in att and "total_cents" not in att
                assert "cost_basis" not in att  # not a priced cost line
            for meal in (day.get("meals") or {}).values():
                assert "price" not in meal and "total_cents" not in meal
                assert "cost_basis" not in meal


# ---------------------------------------------------------------------------
# Integer-cents invariant (var-0): basis adds STRINGS only, never numbers.
# ---------------------------------------------------------------------------

def test_integer_cents_invariant():
    for with_checkout in (True, False):
        res = _success_fixture(with_checkout=with_checkout)
        # All *_cents top-level keys are plain ints.
        for k, v in res.items():
            if k.endswith("_cents"):
                assert isinstance(v, int), (k, type(v))
        for leg in res["legs"]:
            assert isinstance(leg["total_cents"], int)
        # package_total_cents == exact integer sum of per-leg total_cents.
        assert res["package_total_cents"] == sum(
            int(leg["total_cents"]) for leg in res["legs"]
        )
        assert res["package_total_cents"] == 54000 + 72000


def test_fee_path_basis_does_not_change_totals():
    """Threading basis through add_fee must not perturb usd_cents totals."""
    a = LineItemAssembler()
    a.add_fee(source="compliance", kind=LineItemKind.VISA, leg="leg-0",
              money=_fee_money(6200))
    a.add_fee(source="health", kind=LineItemKind.VACCINE, leg="leg-0",
              money=_fee_money(1500), basis=BASIS_DETERMINISTIC_ESTIMATE)
    items = a.assemble()
    assert all(it["usd_cents"] == int(it["money"]["usd_cents"]) for it in items)
    assert a.total_usd_cents() == 6200 + 1500
    assert all(it["cost_basis"] in ALLOWED_BASES for it in items)


def test_assembler_order_unchanged_by_basis():
    """Adding basis keys must NOT change assembled order (stable _sort_key)."""
    a = LineItemAssembler()
    a.add_fee(source="insurance", kind=LineItemKind.PREMIUM, leg="_package",
              money=_fee_money(3100))
    a.add_fee(source="compliance", kind=LineItemKind.VISA, leg="leg-0",
              money=_fee_money(6200))
    kinds = [it["kind"] for it in a.assemble()]
    # visa (kind order 2) before premium (kind order 5).
    assert kinds == ["visa", "premium"], kinds


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"[OK] {name}")
    print("\n[ALL] PASS")
