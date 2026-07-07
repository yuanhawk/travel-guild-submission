"""
test_contracts.py — PHASE-0 shared-contract tests (§19.1, §19.5).

Covers the coherent shared-contract set:
  - contracts.py        : enum closed-sets, provenance/money/identity validators,
                          frontier shape, N4 classifier, YF-cert handoff,
                          place keyspace, counterparty identity.
  - fx_provider.py      : the FX provider seam (deterministic USD-cents +
                          provenance; multi-city behaviour preserved).
  - peril_crosswalk.py  : Risk→Insurance crosswalk is total + deterministic.
  - line_item_assembler : deterministic order + idempotency (no double-charge).

All deterministic — no network, no LLM, variance-0.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from core import contracts as C  # noqa: E402
from utils import fx_provider as fx  # noqa: E402
from utils import peril_crosswalk as px  # noqa: E402
from utils.line_item_assembler import (  # noqa: E402
    PACKAGE_LEG,
    LineItemAssembler,
    LineItemKind,
    assemble_line_items,
)


# ===========================================================================
# Provenance envelope
# ===========================================================================


def test_provenance_valid():
    env = C.make_provenance(
        source="seeded-2026-01 FX snapshot",
        fetched_at="2026-01-01",
        tier=C.SourceTier.SEEDED,
        ttl=None,
    )
    assert C.is_valid_provenance(env)
    assert env["tier"] == "seeded"
    assert set(env) == {"source", "source_url", "fetched_at", "ttl", "tier"}


def test_provenance_rejects_missing_and_bad_tier():
    assert C.validate_provenance({}) != []
    bad = C.make_provenance("s", "2026-01-01", "not_a_tier")
    assert not C.is_valid_provenance(bad)
    bad2 = C.make_provenance("s", "Jan 1 2026", C.SourceTier.CACHE)
    assert not C.is_valid_provenance(bad2)  # fetched_at not ISO
    bad3 = C.make_provenance("", "2026-01-01", C.SourceTier.CACHE)
    assert not C.is_valid_provenance(bad3)  # empty source


# ===========================================================================
# Currency-cents tagging
# ===========================================================================


def test_money_shape_valid():
    m = C.make_money(
        native_cents=300000, currency="aud", usd_cents=198000,
        fx_rate=0.66, fx_source="seeded", fetched_at="2026-01-01",
    )
    assert C.is_valid_money(m)
    assert m["currency"] == "AUD"
    assert set(m) == {"native_cents", "currency", "fx", "usd_cents"}
    assert set(m["fx"]) == {"fx_rate", "fx_source", "fetched_at"}


def test_money_rejects_bad():
    assert C.validate_money({}) != []
    bad = C.make_money(300000, "AU", 1, 0.66, "s", "2026-01-01")  # bad ISO
    assert not C.is_valid_money(bad)
    bad2 = C.make_money(300000, "AUD", 1, -1.0, "s", "2026-01-01")  # bad rate
    assert not C.is_valid_money(bad2)


# ===========================================================================
# Peril + Risk reason-code closed sets
# ===========================================================================


def test_peril_closed_set():
    assert C.is_valid_peril("civil_unrest")
    assert C.is_valid_peril("war")
    assert not C.is_valid_peril("zombie_apocalypse")
    assert not C.is_valid_peril(123)
    # all_perils is stable/sorted
    assert C.all_perils() == tuple(sorted(C.all_perils()))


def test_risk_reason_closed_set():
    assert C.is_valid_risk_reason("civil_unrest")
    assert not C.is_valid_risk_reason("made_up")


# ===========================================================================
# Risk→Insurance crosswalk (total + deterministic) — DC0
# ===========================================================================


def test_crosswalk_total_over_reason_codes():
    table = px.crosswalk_table()
    reason_codes = {r.value for r in C.RiskReasonCode}
    assert set(table) == reason_codes, "crosswalk must be TOTAL over RiskReasonCode"
    for peril in table.values():
        assert C.is_valid_peril(peril)


def test_crosswalk_dc0_anchors():
    # DC0 depends on these exact mappings (war/unrest exclusion perils).
    assert px.risk_reason_to_peril("civil_unrest") == C.Peril.CIVIL_UNREST.value
    assert px.risk_reason_to_peril("armed_conflict") == C.Peril.WAR.value


def test_advisory_elevated_reason_code_and_peril():
    # D5: generic elevated advisory is a first-class reason code with the exact
    # value, and maps to the benign PERSONAL_LIABILITY proxy — NEVER WAR.
    assert C.RiskReasonCode.ADVISORY_ELEVATED.value == "advisory_elevated"
    assert C.is_valid_risk_reason("advisory_elevated")
    assert (
        px.risk_reason_to_peril("advisory_elevated")
        == C.Peril.PERSONAL_LIABILITY.value
    )
    assert px.risk_reason_to_peril("advisory_elevated") != C.Peril.WAR.value


def test_crosswalk_deterministic_and_rejects_unknown():
    for _ in range(5):
        assert px.risk_reason_to_peril("terrorism") == "terrorism"
    try:
        px.risk_reason_to_peril("not_a_code")
        assert False, "expected KeyError on unknown reason-code"
    except KeyError:
        pass


def test_crosswalk_list_dedups_stable():
    out = px.risk_reasons_to_perils(
        ["civil_unrest", "armed_conflict", "civil_unrest", "terrorism"]
    )
    assert out == ["civil_unrest", "war", "terrorism"]  # de-duped, first-appearance order


# ===========================================================================
# FX provider seam (§18) — deterministic USD-cents + provenance
# ===========================================================================


def test_fx_usd_identity():
    r = fx.normalize(150000, "USD")
    assert r.usd_cents == 150000
    assert r.fx_rate == 1.0
    assert not r.exhausted
    assert C.is_valid_money(r.to_money())
    assert C.is_valid_provenance(r.provenance())


def test_fx_aud_matches_multicity_seed():
    # AUD 3000 → native 300000¢ → 300000/100 * 0.66 * 100 = 198000 USD¢.
    r = fx.normalize(300000, "AUD")
    assert r.usd_cents == 198000
    assert r.tier == "seeded"
    assert "fx_rate" in r.to_emit() and r.to_emit()["usd_cents"] == 198000


def test_fx_unknown_currency_is_honest_decline_not_usd():
    r = fx.normalize(200000, "JPY")
    assert r.exhausted is True
    assert r.usd_cents == 0
    assert r.tier == "exhausted"  # NEVER silently treated as USD


def test_fx_deterministic():
    a = fx.normalize(50000, "THB").to_emit()
    b = fx.normalize(50000, "THB").to_emit()
    assert a == b  # variance-0


def test_fx_shim_matches_intent_parser_contract():
    # The reconciliation shim preserves the intent_parser._convert_to_usd_cents
    # contract: dollars in, USD cents out, None for unknown / non-positive.
    assert fx.convert_to_usd_cents(3000.0, "AUD") == 198000
    assert fx.convert_to_usd_cents(0, "AUD") is None
    assert fx.convert_to_usd_cents(200000.0, "JPY") is None


def test_intent_parser_routes_through_seam():
    # The merged multi-city behaviour must be IDENTICAL after re-pointing the
    # rider at the FX seam (no duplicate FX path, no regression).
    from utils import intent_parser as ip
    assert ip._convert_to_usd_cents(3000.0, "AUD") == 198000
    assert ip._convert_to_usd_cents(3000.0, "AUD") == fx.convert_to_usd_cents(3000.0, "AUD")
    assert ip._convert_to_usd_cents(200000.0, "JPY") is None


# ===========================================================================
# N4 critical-bag single-classifier contract
# ===========================================================================


def test_bag_classification_single_classifier():
    rec = C.make_bag_classification("insulin", C.BagClass.CRITICAL_MEDICAL, cabin_only=True)
    assert C.is_valid_bag_classification(rec)
    assert rec["classified_by"] == "risk"
    # Anyone forging a non-risk classifier is rejected (single-classifier invariant).
    rec["classified_by"] = "health"
    assert not C.is_valid_bag_classification(rec)


def test_bag_classification_closed_set():
    bad = C.make_bag_classification("thing", "not_a_class", cabin_only=False)
    assert not C.is_valid_bag_classification(bad)


# ===========================================================================
# YF-cert Health↔Compliance handoff
# ===========================================================================


def test_yf_handoff_two_halves():
    cost = fx.normalize(15000, "SGD").to_money()  # seeded SGD vaccine cost
    jab = C.make_jab_record("yellow_fever", cost, lead_time_days=10, validity_years=10)
    assert C.is_valid_jab_record(jab)
    assert jab["owned_by"] == "health"

    doc = C.make_entry_doc_requirement(
        "yellow_fever_certificate", "yellow_fever",
        mandatory_for_entry=True, place_key="gondar",
    )
    assert C.is_valid_entry_doc_requirement(doc)
    assert doc["owned_by"] == "compliance"
    # the two halves agree on the vaccine (the explicit handoff)
    assert jab["vaccine"] == doc["vaccine"]


def test_yf_handoff_rejects_wrong_owner():
    cost = fx.normalize(15000, "SGD").to_money()
    jab = C.make_jab_record("yellow_fever", cost, 10, 10)
    jab["owned_by"] = "compliance"  # Health must own the jab
    assert not C.is_valid_jab_record(jab)


# ===========================================================================
# Frontier-event shape (§16.9)
# ===========================================================================


def test_frontier_event_valid():
    ev = C.make_frontier_event(
        C.FrontierClass.HUMAN_AGENCY_LIMIT,
        enumerated_options=[
            {"option_id": "stay", "summary": "Remain; monitor advisory"},
            {"option_id": "exit", "summary": "Re-route out via Riyadh"},
        ],
        tradeoffs=[
            {"option_id": "stay", "pros": ["no cost"], "cons": ["safety risk"]},
            {"option_id": "exit", "pros": ["safer"], "cons": ["+$Δ"]},
        ],
    )
    assert C.is_valid_frontier_event(ev)
    assert ev["composed_by"] == "orchestrator"


def test_frontier_event_rejects_dangling_tradeoff_and_wrong_composer():
    ev = C.make_frontier_event(
        C.FrontierClass.ABSTRACTION_LIMIT,
        enumerated_options=[{"option_id": "a", "summary": "x"}],
        tradeoffs=[{"option_id": "ghost", "pros": [], "cons": []}],  # no such option
    )
    assert not C.is_valid_frontier_event(ev)
    ev2 = C.make_frontier_event(
        C.FrontierClass.PREDICTION_LIMIT,
        [{"option_id": "a", "summary": "x"}],
        [{"option_id": "a", "pros": [], "cons": []}],
    )
    ev2["composed_by"] = "concierge"  # Concierge presents, never composes
    assert not C.is_valid_frontier_event(ev2)


# ===========================================================================
# Place keyspace
# ===========================================================================


def test_place_key_canonicalization():
    assert C.canonical_place_key("Margaret River") == "margaret-river"
    assert C.canonical_place_key("Bali") == "bali"
    assert C.canonical_place_key("kuala lumpur") == "kuala-lumpur"
    # idempotent on already-canonical
    assert C.canonical_place_key("margaret-river") == "margaret-river"
    assert C.is_valid_place_key("margaret-river")
    assert not C.is_valid_place_key("Margaret River")


# ===========================================================================
# Counterparty ↔ catalog identity
# ===========================================================================


def test_counterparty_single_namespace():
    cp = C.make_counterparty("bali-alaya-ubud", C.CounterpartyKind.HOTEL)
    assert C.is_valid_counterparty(cp)
    assert cp["counterparty_id"] == cp["catalog_id"] == "bali-alaya-ubud"
    cp["counterparty_id"] = "other"  # second namespace not allowed
    assert not C.is_valid_counterparty(cp)


def test_counterparty_kind_ota():
    # D8: OTA/reseller is a first-class counterparty kind with the exact value.
    assert C.CounterpartyKind.OTA.value == "ota"
    cp = C.make_counterparty("ota-cheapfly-reseller", C.CounterpartyKind.OTA)
    assert C.is_valid_counterparty(cp)
    assert cp["kind"] == "ota"


# ===========================================================================
# Traveler / party identity (multi-nationality N5)
# ===========================================================================


def test_traveler_multi_nationality():
    t = C.make_traveler("t1", ["AU", "GB"])
    assert C.is_valid_traveler(t)
    assert C.is_multi_national(t)
    single = C.make_traveler("t2", ["US"])
    assert not C.is_multi_national(single)


def test_party_validation_and_dup_detection():
    party = C.make_party("p1", [
        C.make_traveler("t1", ["AU"]),
        C.make_traveler("t2", ["GB", "IE"]),
    ])
    assert C.is_valid_party(party)
    dup = C.make_party("p2", [C.make_traveler("t1", ["AU"]), C.make_traveler("t1", ["US"])])
    assert not C.is_valid_party(dup)
    badnat = C.make_party("p3", [C.make_traveler("t1", ["Australia"])])
    assert not C.is_valid_party(badnat)


# ===========================================================================
# Line-item assembler — deterministic order + IDEMPOTENCY (no double-charge)
# ===========================================================================


def _premium_money():
    return fx.normalize(12000, "USD").to_money()


def _visa_money():
    return fx.normalize(6200, "USD").to_money()  # Ethiopia eVisa $62 (§17)


def test_assembler_deterministic_order_regardless_of_insertion():
    a = LineItemAssembler()
    a.add_fee(source="insurance", kind=LineItemKind.PREMIUM, leg=PACKAGE_LEG,
              money=_premium_money())
    a.add_lodging(hotel_id="h0", leg="leg-0", checkin="2026-10-01", checkout="2026-10-03")
    a.add_fee(source="compliance", kind=LineItemKind.VISA, leg="leg-0", money=_visa_money())

    b = LineItemAssembler()
    b.add_lodging(hotel_id="h0", leg="leg-0", checkin="2026-10-01", checkout="2026-10-03")
    b.add_fee(source="compliance", kind=LineItemKind.VISA, leg="leg-0", money=_visa_money())
    b.add_fee(source="insurance", kind=LineItemKind.PREMIUM, leg=PACKAGE_LEG,
              money=_premium_money())

    assert a.assemble() == b.assemble()  # insertion-order-independent
    kinds = [it["kind"] for it in a.assemble()]
    assert kinds == ["lodging", "visa", "premium"]  # canonical kind order


def test_assembler_idempotent_no_double_charge():
    """THE cross-cutting money invariant: re-emitting the same fee key does not
    double-add (mirrors the §16.3 cascade lesson at the fee level)."""
    a = LineItemAssembler()
    a.add_lodging(hotel_id="h0", leg="leg-0", checkin="2026-10-01", checkout="2026-10-03")
    a.add_fee(source="compliance", kind="visa", leg="leg-0", money=_visa_money())

    items_once = a.assemble()
    total_once = a.total_usd_cents()

    # Re-emit the SAME visa fee (e.g. a retried Compliance agent / re-run leg).
    a.add_fee(source="compliance", kind="visa", leg="leg-0", money=_visa_money())
    items_twice = a.assemble()

    assert items_once == items_twice, "re-emission must not change the line_items"
    assert a.total_usd_cents() == total_once, "re-emission must not double-charge"
    # Exactly one visa entry survives.
    visa_items = [it for it in items_twice if it["kind"] == "visa"]
    assert len(visa_items) == 1


def test_assembler_different_legs_are_distinct():
    a = LineItemAssembler()
    a.add_fee(source="compliance", kind="visa", leg="leg-0", money=_visa_money())
    a.add_fee(source="compliance", kind="visa", leg="leg-1", money=_visa_money())
    items = a.assemble()
    assert len([it for it in items if it["kind"] == "visa"]) == 2  # distinct legs


def test_assembler_rejects_bad_money_and_kind():
    a = LineItemAssembler()
    try:
        a.add_fee(source="x", kind="not_a_kind", leg="leg-0", money=_visa_money())
        assert False
    except ValueError:
        pass
    try:
        a.add_fee(source="x", kind="visa", leg="leg-0", money={"bad": 1})
        assert False
    except ValueError:
        pass
    try:
        a.add_fee(source="x", kind="lodging", leg="leg-0", money=_visa_money())
        assert False, "lodging must go through add_lodging"
    except ValueError:
        pass


def test_assemble_functional_wrapper():
    items = assemble_line_items(
        lodging=[{"hotel_id": "h0", "leg": "leg-0", "checkin": "2026-10-01",
                  "checkout": "2026-10-03", "adults": 1}],
        fees=[{"source": "insurance", "kind": "premium", "leg": PACKAGE_LEG,
               "money": _premium_money()}],
    )
    assert [it["kind"] for it in items] == ["lodging", "premium"]
