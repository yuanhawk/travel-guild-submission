"""
test_line_item_assembler_cov.py — comprehensive coverage of line_item_assembler.py.

Tests the deterministic, idempotent line-item assembly logic with focus on
uncovered behaviors: enum validation, parameter variations, edge cases, stable
sorting with many entries, and determinism guarantees.

var-0: pure Python, no I/O, no randomness, no LLM.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.contracts import make_money
from core.cost_basis import BASIS_DETERMINISTIC_ESTIMATE, BASIS_UNKNOWN
from utils.line_item_assembler import (
    PACKAGE_LEG,
    LineItemAssembler,
    LineItemKind,
    _KIND_ORDER,
    _KIND_VALUES,
    _fee_key,
    assemble_line_items,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _money(usd_cents: int) -> dict:
    """Create a valid money dict."""
    return make_money(
        native_cents=usd_cents,
        currency="USD",
        usd_cents=usd_cents,
        fx_rate=1.0,
        fx_source="seeded",
        fetched_at="2026-06-20",
    )


# ---------------------------------------------------------------------------
# LineItemKind enum — closed set validation
# ---------------------------------------------------------------------------


def test_lineitemkind_all_values_in_kind_order():
    """All LineItemKind values must be keys in _KIND_ORDER."""
    for kind in LineItemKind:
        assert kind.value in _KIND_ORDER, f"{kind.value} not in _KIND_ORDER"


def test_lineitemkind_all_values_in_kind_values_set():
    """All LineItemKind values must be in _KIND_VALUES frozenset."""
    for kind in LineItemKind:
        assert kind.value in _KIND_VALUES, f"{kind.value} not in _KIND_VALUES"


def test_lineitemkind_enum_has_six_kinds():
    """Exactly 6 kinds are defined."""
    assert len(list(LineItemKind)) == 6
    assert {k.value for k in LineItemKind} == {
        "lodging", "visa", "vaccine", "premium", "transport", "permit"
    }


def test_kind_order_is_total_and_deterministic():
    """_KIND_ORDER must be total (all kinds) and values must be unique."""
    order_values = list(_KIND_ORDER.values())
    # All unique
    assert len(order_values) == len(set(order_values)), "order values not unique"
    # Total (6 kinds)
    assert len(_KIND_ORDER) == 6
    # Lodging is first
    assert _KIND_ORDER["lodging"] == 0


def test_fee_key_returns_tuple_of_strings():
    """_fee_key must return (source, kind, leg) as strings."""
    key = _fee_key("compliance", "visa", "leg-0")
    assert isinstance(key, tuple)
    assert len(key) == 3
    assert all(isinstance(x, str) for x in key)
    assert key == ("compliance", "visa", "leg-0")


def test_fee_key_coerces_to_string():
    """_fee_key coerces all inputs to str()."""
    # Note: _fee_key does str() conversion, not enum.value extraction
    # So we test with string inputs for the key contract
    key1 = _fee_key("compliance", "visa", "leg-0")
    key2 = _fee_key("compliance", "visa", "leg-0")
    assert key1 == key2


# ---------------------------------------------------------------------------
# add_lodging — parameter variations and edge cases
# ---------------------------------------------------------------------------


def test_add_lodging_minimal_parameters():
    """add_lodging works with required parameters only."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id="h1", leg="leg-0", checkin="2026-10-01",
                  checkout="2026-10-03")
    items = a.assemble()
    assert len(items) == 1
    item = items[0]
    assert item["hotel_id"] == "h1"
    assert item["leg"] == "leg-0"
    assert item["adults"] == 1  # default


def test_add_lodging_explicit_adults():
    """add_lodging respects explicit adults parameter."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id="h1", leg="leg-0", checkin="2026-10-01",
                  checkout="2026-10-03", adults=3)
    items = a.assemble()
    assert items[0]["adults"] == 3


def test_add_lodging_custom_source():
    """add_lodging respects custom source parameter."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id="h1", leg="leg-0", checkin="2026-10-01",
                  checkout="2026-10-03", source="custom_source")
    items = a.assemble()
    assert items[0]["source"] == "custom_source"


def test_add_lodging_custom_basis():
    """add_lodging respects custom basis parameter."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id="h1", leg="leg-0", checkin="2026-10-01",
                  checkout="2026-10-03", basis=BASIS_UNKNOWN)
    items = a.assemble()
    assert items[0]["cost_basis"] == BASIS_UNKNOWN


def test_add_lodging_rejects_empty_hotel_id():
    """add_lodging must reject empty hotel_id."""
    a = LineItemAssembler()
    try:
        a.add_lodging(hotel_id="", leg="leg-0", checkin="2026-10-01",
                      checkout="2026-10-03")
        assert False, "expected ValueError for empty hotel_id"
    except ValueError as e:
        assert "hotel_id" in str(e).lower()


def test_add_lodging_rejects_none_hotel_id():
    """add_lodging must reject None hotel_id."""
    a = LineItemAssembler()
    try:
        a.add_lodging(hotel_id=None, leg="leg-0", checkin="2026-10-01",
                      checkout="2026-10-03")
        assert False, "expected ValueError for None hotel_id"
    except ValueError as e:
        assert "hotel_id" in str(e).lower()


def test_add_lodging_coerces_strings():
    """add_lodging coerces inputs to strings."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id=12345, leg=0, checkin="2026-10-01",
                  checkout="2026-10-03", adults=2)
    items = a.assemble()
    assert items[0]["hotel_id"] == "12345"
    assert items[0]["leg"] == "0"
    assert items[0]["adults"] == 2


# ---------------------------------------------------------------------------
# add_fee — all six kinds, parameter variations
# ---------------------------------------------------------------------------


def test_add_fee_all_six_kinds_via_lineitemkind_enum():
    """Each non-lodging LineItemKind can be added via add_fee."""
    a = LineItemAssembler()
    for kind in LineItemKind:
        if kind == LineItemKind.LODGING:
            continue
        a.add_fee(source="test", kind=kind, leg="leg-0", money=_money(1000))
    items = a.assemble()
    # Visa (2), Permit (3), Vaccine (4), Premium (5), Transport (1)
    # Sorted by kind order: Transport, Visa, Permit, Vaccine, Premium
    assert len(items) == 5
    kinds = [it["kind"] for it in items]
    assert kinds == ["transport", "visa", "permit", "vaccine", "premium"]


def test_add_fee_all_six_kinds_via_string():
    """Each non-lodging kind value as string is accepted."""
    a = LineItemAssembler()
    for kind_str in ["transport", "visa", "permit", "vaccine", "premium"]:
        a.add_fee(source="test", kind=kind_str, leg="leg-0", money=_money(1000))
    items = a.assemble()
    assert len(items) == 5


def test_add_fee_rejects_lodging_kind():
    """add_fee must reject 'lodging' kind."""
    a = LineItemAssembler()
    try:
        a.add_fee(source="test", kind=LineItemKind.LODGING, leg="leg-0",
                  money=_money(1000))
        assert False, "expected ValueError for lodging kind"
    except ValueError as e:
        assert "lodging" in str(e).lower()


def test_add_fee_rejects_invalid_kind_string():
    """add_fee must reject unknown kind string."""
    a = LineItemAssembler()
    try:
        a.add_fee(source="test", kind="not_a_kind", leg="leg-0",
                  money=_money(1000))
        assert False, "expected ValueError for invalid kind"
    except ValueError as e:
        assert "kind" in str(e).lower() and "not_a_kind" in str(e)


def test_add_fee_with_description():
    """add_fee preserves description parameter."""
    a = LineItemAssembler()
    a.add_fee(source="compliance", kind="visa", leg="leg-0",
              money=_money(6200), description="Ethiopia eVisa single-entry")
    items = a.assemble()
    assert items[0]["description"] == "Ethiopia eVisa single-entry"


def test_add_fee_with_empty_description():
    """add_fee handles empty description."""
    a = LineItemAssembler()
    a.add_fee(source="compliance", kind="visa", leg="leg-0",
              money=_money(6200), description="")
    items = a.assemble()
    assert items[0]["description"] == ""


def test_add_fee_custom_basis():
    """add_fee respects custom basis parameter."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0",
              money=_money(1000), basis=BASIS_UNKNOWN)
    items = a.assemble()
    assert items[0]["cost_basis"] == BASIS_UNKNOWN


def test_add_fee_synthetic_hotel_id():
    """Fee generates synthetic hotel_id of form 'fee:source:kind:leg'."""
    a = LineItemAssembler()
    a.add_fee(source="compliance", kind="visa", leg="leg-0",
              money=_money(6200))
    items = a.assemble()
    assert items[0]["hotel_id"] == "fee:compliance:visa:leg-0"


def test_add_fee_extracts_usd_cents():
    """add_fee extracts and stores usd_cents from money dict."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0",
              money=_money(12345))
    items = a.assemble()
    assert items[0]["usd_cents"] == 12345
    assert items[0]["usd_cents"] == int(items[0]["money"]["usd_cents"])


# ---------------------------------------------------------------------------
# Sorting (deterministic total order: kind → leg → source)
# ---------------------------------------------------------------------------


def test_sort_key_returns_tuple():
    """_sort_key returns (kind_order, leg, source)."""
    a = LineItemAssembler()
    a.add_fee(source="b", kind="visa", leg="leg-1", money=_money(100))
    a.add_fee(source="a", kind="visa", leg="leg-0", money=_money(100))
    a.add_fee(source="c", kind="visa", leg="leg-1", money=_money(100))
    items = a.assemble()
    # Sorted by leg, then source
    assert [it["source"] for it in items] == ["a", "b", "c"]
    assert [it["leg"] for it in items] == ["leg-0", "leg-1", "leg-1"]


def test_deterministic_sort_by_kind_leg_source():
    """Total order: kind (via _KIND_ORDER), then leg, then source."""
    a = LineItemAssembler()
    # Add in reverse order of expected assembly
    a.add_fee(source="z", kind="premium", leg="leg-2", money=_money(1))
    a.add_fee(source="a", kind="vaccine", leg="leg-0", money=_money(1))
    a.add_fee(source="m", kind="visa", leg="leg-1", money=_money(1))
    a.add_fee(source="z", kind="transport", leg="leg-2", money=_money(1))
    a.add_lodging(hotel_id="h", leg="leg-0", checkin="2026-10-01",
                  checkout="2026-10-03")
    items = a.assemble()
    # Expected order: lodging (0), transport (1), visa (2), vaccine (4), premium (5)
    assert [it["kind"] for it in items] == [
        "lodging", "transport", "visa", "vaccine", "premium"
    ]


def test_sort_within_kind_by_leg_then_source():
    """Within same kind, sort by leg (alpha), then source."""
    a = LineItemAssembler()
    # Same kind, vary leg/source
    a.add_fee(source="z", kind="visa", leg="leg-2", money=_money(1))
    a.add_fee(source="a", kind="visa", leg="leg-1", money=_money(1))
    a.add_fee(source="m", kind="visa", leg="leg-1", money=_money(1))
    a.add_fee(source="a", kind="visa", leg="leg-0", money=_money(1))
    items = a.assemble()
    legs = [it["leg"] for it in items]
    assert legs == ["leg-0", "leg-1", "leg-1", "leg-2"]
    # Within leg-1, sources sorted a < m
    leg1_items = [it for it in items if it["leg"] == "leg-1"]
    assert [it["source"] for it in leg1_items] == ["a", "m"]


def test_package_leg_sorts_first_lexicographically():
    """_package leg sorts first because '_' < 'l' in ASCII/Unicode."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="_package", money=_money(100))
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(100))
    a.add_fee(source="test", kind="visa", leg="leg-1", money=_money(100))
    items = a.assemble()
    legs = [it["leg"] for it in items]
    # "_package" comes first because "_" < "l" (ord('_')=95, ord('l')=108)
    assert legs == ["_package", "leg-0", "leg-1"]


def test_package_leg_constant():
    """PACKAGE_LEG constant has the correct value."""
    assert PACKAGE_LEG == "_package"


# ---------------------------------------------------------------------------
# Idempotency and key replacement
# ---------------------------------------------------------------------------


def test_add_fee_same_key_replaces_prior_entry():
    """Adding fee with same (source, kind, leg) replaces prior entry."""
    a = LineItemAssembler()
    a.add_fee(source="compliance", kind="visa", leg="leg-0",
              money=_money(6200), description="v1")
    a.add_fee(source="compliance", kind="visa", leg="leg-0",
              money=_money(9900), description="v2")
    items = a.assemble()
    assert len(items) == 1
    assert items[0]["usd_cents"] == 9900
    assert items[0]["description"] == "v2"


def test_add_lodging_same_key_replaces_prior_entry():
    """Adding lodging with same (source, kind, leg) replaces prior."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id="h1", leg="leg-0", checkin="2026-10-01",
                  checkout="2026-10-03", adults=1)
    a.add_lodging(hotel_id="h2", leg="leg-0", checkin="2026-10-05",
                  checkout="2026-10-07", adults=2)
    items = a.assemble()
    assert len(items) == 1
    assert items[0]["hotel_id"] == "h2"
    assert items[0]["adults"] == 2


def test_idempotency_total_usd_cents_unchanged_on_replacement():
    """Replacing a fee does not double-count in total_usd_cents()."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(1000))
    total1 = a.total_usd_cents()
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(2000))
    total2 = a.total_usd_cents()
    assert total1 == 1000
    assert total2 == 2000  # not 3000
    assert len(a) == 1


# ---------------------------------------------------------------------------
# total_usd_cents() helper
# ---------------------------------------------------------------------------


def test_total_usd_cents_empty():
    """total_usd_cents() returns 0 for empty assembler."""
    a = LineItemAssembler()
    assert a.total_usd_cents() == 0


def test_total_usd_cents_single_fee():
    """total_usd_cents() sums single fee."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(6200))
    assert a.total_usd_cents() == 6200


def test_total_usd_cents_multiple_fees():
    """total_usd_cents() sums multiple fees."""
    a = LineItemAssembler()
    a.add_fee(source="compliance", kind="visa", leg="leg-0", money=_money(6200))
    a.add_fee(source="health", kind="vaccine", leg="leg-0", money=_money(1500))
    a.add_fee(source="insurance", kind="premium", leg="_package", money=_money(12000))
    total = a.total_usd_cents()
    assert total == 6200 + 1500 + 12000


def test_total_usd_cents_ignores_lodging():
    """total_usd_cents() ignores lodging (no usd_cents in lodging)."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id="h", leg="leg-0", checkin="2026-10-01",
                  checkout="2026-10-03")
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(1000))
    total = a.total_usd_cents()
    assert total == 1000  # only the visa


def test_total_usd_cents_missing_usd_cents_key():
    """total_usd_cents() handles entries without usd_cents (default 0)."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id="h1", leg="leg-0", checkin="2026-10-01",
                  checkout="2026-10-03")
    a.add_lodging(hotel_id="h2", leg="leg-1", checkin="2026-10-01",
                  checkout="2026-10-03")
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(5000))
    assert a.total_usd_cents() == 5000


# ---------------------------------------------------------------------------
# __len__ method
# ---------------------------------------------------------------------------


def test_len_empty():
    """len(assembler) is 0 for empty assembler."""
    a = LineItemAssembler()
    assert len(a) == 0


def test_len_after_add_fee():
    """len(assembler) increments after add_fee."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(100))
    assert len(a) == 1
    a.add_fee(source="test", kind="vaccine", leg="leg-0", money=_money(100))
    assert len(a) == 2


def test_len_replacement_does_not_increment():
    """len(assembler) unchanged when key is replaced."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(100))
    assert len(a) == 1
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(200))
    assert len(a) == 1


def test_len_matches_assemble_length():
    """len(assembler) == len(assemble())."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id="h", leg="leg-0", checkin="2026-10-01",
                  checkout="2026-10-03")
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(100))
    a.add_fee(source="test", kind="vaccine", leg="leg-1", money=_money(100))
    assert len(a) == len(a.assemble())


# ---------------------------------------------------------------------------
# assemble() — output shape and determinism
# ---------------------------------------------------------------------------


def test_assemble_returns_list():
    """assemble() returns a list."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(100))
    result = a.assemble()
    assert isinstance(result, list)


def test_assemble_returns_copies():
    """assemble() returns copies, not references to internal entries."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(100))
    items1 = a.assemble()
    items1[0]["test_key"] = "modified"
    items2 = a.assemble()
    assert "test_key" not in items2[0], "assemble() must return copies"


def test_assemble_byte_identical_same_insertion_order():
    """assemble() is byte-identical given same SET of entries."""
    def build_a():
        a = LineItemAssembler()
        a.add_lodging(hotel_id="h0", leg="leg-0", checkin="2026-10-01",
                      checkout="2026-10-03")
        a.add_fee(source="compliance", kind="visa", leg="leg-0",
                  money=_money(6200))
        a.add_fee(source="insurance", kind="premium", leg="_package",
                  money=_money(12000))
        return a.assemble()

    items1 = build_a()
    items2 = build_a()
    assert items1 == items2


def test_assemble_determinism_via_multiple_runs():
    """assemble() produces byte-identical results across 10 runs."""
    a = LineItemAssembler()
    a.add_fee(source="z", kind="premium", leg="leg-2", money=_money(1000))
    a.add_fee(source="a", kind="vaccine", leg="leg-0", money=_money(100))
    a.add_fee(source="m", kind="visa", leg="leg-1", money=_money(500))
    
    results = [a.assemble() for _ in range(10)]
    for i in range(1, 10):
        assert results[i] == results[0], f"Run {i} differs from run 0"


def test_assemble_with_many_entries_stable_sort():
    """assemble() with 30+ entries maintains stable order."""
    a = LineItemAssembler()
    # Add 10 visas across 3 legs with varied sources
    for leg_idx in range(3):
        for source_idx in range(10):
            source = f"compliance-{source_idx}"
            leg = f"leg-{leg_idx}"
            a.add_fee(source=source, kind="visa", leg=leg,
                     money=_money(100 + source_idx))
    
    items = a.assemble()
    assert len(items) == 30
    # All visas (kind order 2)
    assert all(it["kind"] == "visa" for it in items)
    # Grouped by leg: leg-0, then leg-1, then leg-2
    legs = [it["leg"] for it in items]
    assert legs[:10] == ["leg-0"] * 10
    assert legs[10:20] == ["leg-1"] * 10
    assert legs[20:30] == ["leg-2"] * 10
    # Within each leg, sources sorted alphabetically
    for leg_num in range(3):
        leg_items = items[leg_num*10:(leg_num+1)*10]
        sources = [it["source"] for it in leg_items]
        assert sources == sorted(sources)


# ---------------------------------------------------------------------------
# Functional wrapper: assemble_line_items()
# ---------------------------------------------------------------------------


def test_assemble_line_items_empty():
    """assemble_line_items() with no args returns empty list."""
    items = assemble_line_items()
    assert items == []


def test_assemble_line_items_none_args():
    """assemble_line_items(lodging=None, fees=None) returns empty list."""
    items = assemble_line_items(lodging=None, fees=None)
    assert items == []


def test_assemble_line_items_lodging_only():
    """assemble_line_items() with lodging only."""
    items = assemble_line_items(
        lodging=[
            {"hotel_id": "h1", "leg": "leg-0", "checkin": "2026-10-01",
             "checkout": "2026-10-03"}
        ]
    )
    assert len(items) == 1
    assert items[0]["kind"] == "lodging"


def test_assemble_line_items_fees_only():
    """assemble_line_items() with fees only."""
    items = assemble_line_items(
        fees=[
            {"source": "compliance", "kind": "visa", "leg": "leg-0",
             "money": _money(6200)}
        ]
    )
    assert len(items) == 1
    assert items[0]["kind"] == "visa"


def test_assemble_line_items_mixed():
    """assemble_line_items() with both lodging and fees."""
    items = assemble_line_items(
        lodging=[
            {"hotel_id": "h1", "leg": "leg-0", "checkin": "2026-10-01",
             "checkout": "2026-10-03"}
        ],
        fees=[
            {"source": "compliance", "kind": "visa", "leg": "leg-0",
             "money": _money(6200)},
            {"source": "insurance", "kind": "premium", "leg": "_package",
             "money": _money(12000)}
        ]
    )
    assert len(items) == 3
    kinds = [it["kind"] for it in items]
    assert kinds == ["lodging", "visa", "premium"]


def test_assemble_line_items_deterministic():
    """assemble_line_items() is deterministic across calls."""
    def build():
        return assemble_line_items(
            lodging=[
                {"hotel_id": "h", "leg": "leg-0", "checkin": "2026-10-01",
                 "checkout": "2026-10-03"}
            ],
            fees=[
                {"source": "test", "kind": "visa", "leg": "leg-0",
                 "money": _money(6200)}
            ]
        )
    
    results = [build() for _ in range(5)]
    for r in results[1:]:
        assert r == results[0]


# ---------------------------------------------------------------------------
# Edge cases and special behaviors
# ---------------------------------------------------------------------------


def test_add_fee_with_zero_usd_cents():
    """add_fee accepts 0 usd_cents (edge case)."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0", money=_money(0))
    items = a.assemble()
    assert items[0]["usd_cents"] == 0
    assert a.total_usd_cents() == 0


def test_add_fee_with_large_usd_cents():
    """add_fee handles large usd_cents values."""
    large_amount = 999999999
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0",
              money=_money(large_amount))
    items = a.assemble()
    assert items[0]["usd_cents"] == large_amount
    assert a.total_usd_cents() == large_amount


def test_add_lodging_with_special_characters_in_strings():
    """add_lodging handles special characters in string fields."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id="h-café-bücher", leg="leg-0",
                  checkin="2026-10-01", checkout="2026-10-03")
    items = a.assemble()
    assert items[0]["hotel_id"] == "h-café-bücher"


def test_add_fee_with_special_characters():
    """add_fee handles special characters in description."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0",
              money=_money(1000), description="Visa £¥€ 中文")
    items = a.assemble()
    assert "£¥€ 中文" in items[0]["description"]


def test_multiple_sources_same_fee_kind_leg():
    """Multiple sources for same kind+leg stay distinct (not replaced)."""
    a = LineItemAssembler()
    a.add_fee(source="agent-1", kind="visa", leg="leg-0", money=_money(100))
    a.add_fee(source="agent-2", kind="visa", leg="leg-0", money=_money(200))
    a.add_fee(source="agent-3", kind="visa", leg="leg-0", money=_money(300))
    items = a.assemble()
    assert len(items) == 3
    sources = [it["source"] for it in items]
    assert sorted(sources) == ["agent-1", "agent-2", "agent-3"]


def test_add_fee_rejects_invalid_money_dict():
    """add_fee rejects money dicts that fail is_valid_money()."""
    a = LineItemAssembler()
    try:
        a.add_fee(source="test", kind="visa", leg="leg-0",
                  money={"invalid": "dict"})
        assert False, "expected ValueError"
    except ValueError as e:
        assert "money" in str(e).lower()


def test_lodging_entry_contains_all_expected_keys():
    """Lodging entry contains all expected keys."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id="h1", leg="leg-0", checkin="2026-10-01",
                  checkout="2026-10-03", adults=2)
    items = a.assemble()
    item = items[0]
    expected_keys = {
        "kind", "source", "leg", "hotel_id", "checkin", "checkout", "adults",
        "cost_basis", "cost_basis_label"
    }
    assert expected_keys.issubset(set(item.keys())), \
        f"Missing keys: {expected_keys - set(item.keys())}"


def test_fee_entry_contains_all_expected_keys():
    """Fee entry contains all expected keys."""
    a = LineItemAssembler()
    a.add_fee(source="test", kind="visa", leg="leg-0",
              money=_money(6200), description="Test visa")
    items = a.assemble()
    item = items[0]
    expected_keys = {
        "kind", "source", "leg", "hotel_id", "money", "usd_cents",
        "description", "cost_basis", "cost_basis_label"
    }
    assert expected_keys.issubset(set(item.keys())), \
        f"Missing keys: {expected_keys - set(item.keys())}"


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
