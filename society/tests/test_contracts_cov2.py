"""
test_contracts_cov2.py — targeted coverage extension for core/contracts.py.

ADD-ONLY var-0 test file. Drives the uncovered ERROR branches of the §19.1
shared-contract validators (contracts.py) by feeding deliberately-malformed
records to each ``validate_*`` and asserting the specific error string the
branch emits. Pure deterministic Python — no network, no LLM, no paid API.

Uncovered line ranges targeted (per coverage gap brief):
  - validate_provenance            lines 136-145
  - validate_money (fx leg)        lines 214-226
  - validate_bag_classification    lines 429-437
  - validate_jab_record            lines 522-532
  - validate_entry_doc_requirement lines 546-557
  - validate_frontier_event        lines 657-678
  - validate_counterparty          lines 769-779
  - validate_traveler/_party       lines 830-867
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from core import contracts as C  # noqa: E402


# ---------------------------------------------------------------------------
# helpers: build a known-VALID record, then mutate one field per test so the
# only failing branch is the one under test (keeps each assertion precise).
# ---------------------------------------------------------------------------


def _good_money() -> dict:
    return C.make_money(
        native_cents=1000,
        currency="usd",
        usd_cents=1000,
        fx_rate=1.0,
        fx_source="seeded-2026-01 FX snapshot",
        fetched_at="2026-01-01",
    )


def _good_provenance() -> dict:
    return C.make_provenance(
        source="seeded table",
        fetched_at="2026-01-01",
        tier=C.SourceTier.SEEDED,
        source_url=None,
        ttl=None,
    )


def _good_bag() -> dict:
    return C.make_bag_classification("insulin", C.BagClass.CRITICAL_MEDICAL, True)


def _good_jab() -> dict:
    return C.make_jab_record("yellow_fever", _good_money(), 10, 10)


def _good_entry_doc() -> dict:
    return C.make_entry_doc_requirement(
        "yellow_fever_certificate", "yellow_fever", True, "lagos"
    )


def _good_frontier() -> dict:
    return C.make_frontier_event(
        C.FrontierClass.HUMAN_AGENCY_LIMIT,
        [{"option_id": "a", "summary": "stay"}],
        [{"option_id": "a", "pros": ["safe"], "cons": ["dull"]}],
    )


def _good_counterparty() -> dict:
    return C.make_counterparty("hotel-123", C.CounterpartyKind.HOTEL)


def _good_traveler() -> dict:
    return C.make_traveler("t1", ["USA"])


def _good_party() -> dict:
    return C.make_party("p1", [_good_traveler()])


# ===========================================================================
# 1. validate_provenance — lines 136-145
# ===========================================================================


def test_provenance_source_edge_cases():
    # baseline is valid (sanity — guarantees only the mutated branch fails)
    assert C.validate_provenance(_good_provenance()) == []

    empty = _good_provenance()
    empty["source"] = ""
    assert any("source must be a non-empty string" in e for e in C.validate_provenance(empty))

    ws = _good_provenance()
    ws["source"] = "   "
    assert any("source must be a non-empty string" in e for e in C.validate_provenance(ws))

    nonstr = _good_provenance()
    nonstr["source"] = 123
    assert any("source must be a non-empty string" in e for e in C.validate_provenance(nonstr))


def test_provenance_source_url_validation():
    # None is allowed (no error)
    ok = _good_provenance()
    ok["source_url"] = None
    assert not any("source_url" in e for e in C.validate_provenance(ok))

    # a valid string url is also fine
    ok2 = _good_provenance()
    ok2["source_url"] = "https://example.test/x"
    assert not any("source_url" in e for e in C.validate_provenance(ok2))

    bad = _good_provenance()
    bad["source_url"] = 42
    assert any("source_url must be a string or None" in e for e in C.validate_provenance(bad))


def test_provenance_fetched_at_iso_format():
    bad_fmt = _good_provenance()
    bad_fmt["fetched_at"] = "01/02/2026"  # MM/DD/YYYY — not ISO-8601
    assert any("fetched_at must be an ISO-8601" in e for e in C.validate_provenance(bad_fmt))

    nonstr = _good_provenance()
    nonstr["fetched_at"] = 20260101
    assert any("fetched_at must be an ISO-8601" in e for e in C.validate_provenance(nonstr))


def test_provenance_ttl_bounds():
    neg = _good_provenance()
    neg["ttl"] = -1
    assert any("ttl must be a non-negative int" in e for e in C.validate_provenance(neg))

    flt = _good_provenance()
    flt["ttl"] = 3.5
    assert any("ttl must be a non-negative int" in e for e in C.validate_provenance(flt))

    s = _good_provenance()
    s["ttl"] = "60"
    assert any("ttl must be a non-negative int" in e for e in C.validate_provenance(s))

    # None and zero are both valid
    zero = _good_provenance()
    zero["ttl"] = 0
    assert not any("ttl" in e for e in C.validate_provenance(zero))


def test_provenance_tier_closed_set():
    bad = _good_provenance()
    bad["tier"] = "not-a-tier"
    assert any("tier 'not-a-tier' not in" in e for e in C.validate_provenance(bad))

    # every enum member is accepted
    for t in C.SourceTier:
        env = C.make_provenance("s", "2026-01-01", t)
        assert C.validate_provenance(env) == []


# ===========================================================================
# 2. validate_money — lines 214-226 (the fx leg)
# ===========================================================================


def test_money_fx_dict_structure():
    assert C.validate_money(_good_money()) == []

    not_dict = _good_money()
    not_dict["fx"] = ["fx_rate", 1.0]
    assert any("fx must be a dict" in e for e in C.validate_money(not_dict))

    for missing in ("fx_rate", "fx_source", "fetched_at"):
        tag = _good_money()
        del tag["fx"][missing]
        errs = C.validate_money(tag)
        assert any(f"money.fx: missing required key {missing!r}" in e for e in errs)


def test_money_fx_rate_bounds():
    zero = _good_money()
    zero["fx"]["fx_rate"] = 0
    assert any("fx_rate must be a positive number" in e for e in C.validate_money(zero))

    neg = _good_money()
    neg["fx"]["fx_rate"] = -1.5
    assert any("fx_rate must be a positive number" in e for e in C.validate_money(neg))

    nonnum = _good_money()
    nonnum["fx"]["fx_rate"] = "1.0"
    assert any("fx_rate must be a positive number" in e for e in C.validate_money(nonnum))


def test_money_fx_source_content():
    empty = _good_money()
    empty["fx"]["fx_source"] = ""
    assert any("fx_source must be a non-empty string" in e for e in C.validate_money(empty))

    nonstr = _good_money()
    nonstr["fx"]["fx_source"] = 7
    assert any("fx_source must be a non-empty string" in e for e in C.validate_money(nonstr))


def test_money_cents_bounds():
    neg_native = _good_money()
    neg_native["native_cents"] = -1
    assert any("native_cents must be a non-negative int" in e for e in C.validate_money(neg_native))

    neg_usd = _good_money()
    neg_usd["usd_cents"] = -5
    assert any("usd_cents must be a non-negative int" in e for e in C.validate_money(neg_usd))


def test_money_currency_iso4217():
    bad = _good_money()
    bad["currency"] = "Usd"  # not 3 upper-case letters
    assert any("currency must be an upper-case ISO-4217 code" in e for e in C.validate_money(bad))

    bad2 = _good_money()
    bad2["currency"] = "US"
    assert any("currency must be an upper-case ISO-4217 code" in e for e in C.validate_money(bad2))


# ===========================================================================
# 3. validate_bag_classification — lines 429-437
# ===========================================================================


def test_bag_classification_item_content():
    assert C.validate_bag_classification(_good_bag()) == []

    empty = _good_bag()
    empty["item"] = ""
    assert any("item must be a non-empty string" in e for e in C.validate_bag_classification(empty))

    ws = _good_bag()
    ws["item"] = "   "
    assert any("item must be a non-empty string" in e for e in C.validate_bag_classification(ws))

    nonstr = _good_bag()
    nonstr["item"] = 99
    assert any("item must be a non-empty string" in e for e in C.validate_bag_classification(nonstr))


def test_bag_classification_bag_class_enum():
    bad = _good_bag()
    bad["bag_class"] = "super_critical"
    assert any("bag_class 'super_critical' not in" in e for e in C.validate_bag_classification(bad))

    for bc in C.BagClass:
        rec = C.make_bag_classification("x", bc, False)
        assert C.validate_bag_classification(rec) == []


def test_bag_classification_cabin_only_bool():
    for bad_val in ("yes", 1, None):
        rec = _good_bag()
        rec["cabin_only"] = bad_val
        assert any("cabin_only must be a bool" in e for e in C.validate_bag_classification(rec))


def test_bag_classification_classified_by_owner():
    rec = _good_bag()
    rec["classified_by"] = "planner"
    assert any("classified_by must be 'risk'" in e for e in C.validate_bag_classification(rec))


# ===========================================================================
# 4. validate_jab_record — lines 522-532
# ===========================================================================


def test_jab_vaccine_content():
    assert C.validate_jab_record(_good_jab()) == []

    for bad_val in ("", "  ", 5):
        rec = _good_jab()
        rec["vaccine"] = bad_val
        assert any("vaccine must be a non-empty string" in e for e in C.validate_jab_record(rec))


def test_jab_cost_validation_delegation():
    rec = _good_jab()
    bad_cost = _good_money()
    bad_cost["fx"]["fx_rate"] = 0  # triggers a nested money error
    rec["cost"] = bad_cost
    errs = C.validate_jab_record(rec)
    # delegated money errors are prefixed with 'jab_record.cost.'
    assert any(e.startswith("jab_record.cost.") for e in errs)
    assert any("fx_rate must be a positive number" in e for e in errs)


def test_jab_lead_time_bounds():
    neg = _good_jab()
    neg["lead_time_days"] = -1
    assert any("lead_time_days must be a non-negative int" in e for e in C.validate_jab_record(neg))

    flt = _good_jab()
    flt["lead_time_days"] = 3.0
    assert any("lead_time_days must be a non-negative int" in e for e in C.validate_jab_record(flt))


def test_jab_validity_years_bounds():
    zero = _good_jab()
    zero["validity_years"] = 0
    assert any(
        "validity_years must be a positive int or None" in e for e in C.validate_jab_record(zero)
    )

    neg = _good_jab()
    neg["validity_years"] = -2
    assert any(
        "validity_years must be a positive int or None" in e for e in C.validate_jab_record(neg)
    )

    flt = _good_jab()
    flt["validity_years"] = 10.0
    assert any(
        "validity_years must be a positive int or None" in e for e in C.validate_jab_record(flt)
    )

    # None is explicitly allowed (single-trip cert)
    none_ok = _good_jab()
    none_ok["validity_years"] = None
    assert not any("validity_years" in e for e in C.validate_jab_record(none_ok))


def test_jab_owned_by_owner():
    rec = _good_jab()
    rec["owned_by"] = "compliance"
    assert any("owned_by must be 'health'" in e for e in C.validate_jab_record(rec))


# ===========================================================================
# 5. validate_entry_doc_requirement — lines 546-557
# ===========================================================================


def test_entry_doc_document_vaccine_content():
    assert C.validate_entry_doc_requirement(_good_entry_doc()) == []

    for bad_val in ("", "  ", 9):
        doc = _good_entry_doc()
        doc["document"] = bad_val
        assert any(
            "document must be a non-empty string" in e
            for e in C.validate_entry_doc_requirement(doc)
        )

    for bad_val in ("", "  ", 9):
        vac = _good_entry_doc()
        vac["vaccine"] = bad_val
        assert any(
            "vaccine must be a non-empty string" in e
            for e in C.validate_entry_doc_requirement(vac)
        )


def test_entry_doc_mandatory_bool():
    for bad_val in ("true", 1, None):
        doc = _good_entry_doc()
        doc["mandatory_for_entry"] = bad_val
        assert any(
            "mandatory_for_entry must be a bool" in e
            for e in C.validate_entry_doc_requirement(doc)
        )


def test_entry_doc_place_key_canonical():
    bad = _good_entry_doc()
    bad["place_key"] = "Lagos City"  # has space + upper-case → not a canonical slug
    assert any(
        "place_key must be a valid canonical place key" in e
        for e in C.validate_entry_doc_requirement(bad)
    )

    # a correctly-slugged key passes (sanity on the positive branch)
    ok = _good_entry_doc()
    ok["place_key"] = "kuala-lumpur"
    assert not any("place_key" in e for e in C.validate_entry_doc_requirement(ok))


def test_entry_doc_owned_by_owner():
    doc = _good_entry_doc()
    doc["owned_by"] = "health"
    assert any("owned_by must be 'compliance'" in e for e in C.validate_entry_doc_requirement(doc))


# ===========================================================================
# 6. validate_frontier_event — lines 657-678
# ===========================================================================


def test_frontier_options_empty_list():
    ev = _good_frontier()
    ev["enumerated_options"] = []
    assert any(
        "enumerated_options must be a non-empty list" in e for e in C.validate_frontier_event(ev)
    )

    not_list = _good_frontier()
    not_list["enumerated_options"] = "a"
    assert any(
        "enumerated_options must be a non-empty list" in e
        for e in C.validate_frontier_event(not_list)
    )


def test_frontier_tradeoffs_not_list():
    ev = _good_frontier()
    ev["tradeoffs"] = {"option_id": "a"}
    assert any("tradeoffs must be a list" in e for e in C.validate_frontier_event(ev))


def test_frontier_option_dict_and_keys():
    # a non-dict option in the list
    ev = _good_frontier()
    ev["enumerated_options"] = ["not-a-dict"]
    ev["tradeoffs"] = []
    assert any("option[0] not a dict" in e for e in C.validate_frontier_event(ev))

    # an option dict missing a required key
    ev2 = _good_frontier()
    ev2["enumerated_options"] = [{"option_id": "a"}]  # missing 'summary'
    ev2["tradeoffs"] = [{"option_id": "a", "pros": [], "cons": []}]
    assert any("option[0] missing 'summary'" in e for e in C.validate_frontier_event(ev2))


def test_frontier_tradeoff_dict_and_keys():
    ev = _good_frontier()
    ev["tradeoffs"] = ["not-a-dict"]
    assert any("tradeoff[0] not a dict" in e for e in C.validate_frontier_event(ev))

    ev2 = _good_frontier()
    ev2["tradeoffs"] = [{"option_id": "a", "pros": []}]  # missing 'cons'
    assert any("tradeoff[0] missing 'cons'" in e for e in C.validate_frontier_event(ev2))


def test_frontier_tradeoff_option_id_consistency():
    ev = _good_frontier()
    # dangling reference: tradeoff points at an option that was never enumerated
    ev["tradeoffs"] = [{"option_id": "ghost", "pros": [], "cons": []}]
    errs = C.validate_frontier_event(ev)
    assert any("does not match any enumerated option" in e for e in errs)


# ===========================================================================
# 7. validate_counterparty — lines 769-779
# ===========================================================================


def test_counterparty_catalog_id_content():
    assert C.validate_counterparty(_good_counterparty()) == []

    for bad_val in ("", "   ", 123):
        rec = _good_counterparty()
        rec["catalog_id"] = bad_val
        # keep counterparty_id consistent so the ONLY failure is catalog_id content
        rec["counterparty_id"] = bad_val
        assert any(
            "catalog_id must be a non-empty string" in e for e in C.validate_counterparty(rec)
        )


def test_counterparty_id_equality():
    rec = _good_counterparty()
    rec["counterparty_id"] = "hotel-999"  # mismatch vs catalog_id
    assert any("counterparty_id must equal catalog_id" in e for e in C.validate_counterparty(rec))


def test_counterparty_kind_enum():
    bad = _good_counterparty()
    bad["kind"] = "airline"
    assert any("kind 'airline' not in" in e for e in C.validate_counterparty(bad))

    for k in C.CounterpartyKind:
        rec = C.make_counterparty("cid", k)
        assert C.validate_counterparty(rec) == []


# ===========================================================================
# 8. validate_traveler / validate_party — lines 830-867
# ===========================================================================


def test_traveler_id_content():
    assert C.validate_traveler(_good_traveler()) == []

    for bad_val in ("", "   ", 5):
        rec = _good_traveler()
        rec["traveler_id"] = bad_val
        assert any("traveler_id must be a non-empty string" in e for e in C.validate_traveler(rec))


def test_traveler_nationalities_list():
    empty = _good_traveler()
    empty["nationalities"] = []
    assert any("nationalities must be a non-empty list" in e for e in C.validate_traveler(empty))

    not_list = _good_traveler()
    not_list["nationalities"] = "USA"
    assert any(
        "nationalities must be a non-empty list" in e for e in C.validate_traveler(not_list)
    )


def test_traveler_nationality_iso_code():
    # lower-case, too short, too long, and a digit all fail the ^[A-Z]{2,3}$ rule.
    for bad in ("us", "U", "USAA", "U1"):
        rec = _good_traveler()
        rec["nationalities"] = [bad]
        assert any(
            "must be an ISO-3166 country code" in e for e in C.validate_traveler(rec)
        ), f"expected ISO error for {bad!r}"

    # non-string element
    rec = _good_traveler()
    rec["nationalities"] = [123]
    assert any("must be an ISO-3166 country code" in e for e in C.validate_traveler(rec))

    # alpha-3 is accepted
    ok = _good_traveler()
    ok["nationalities"] = ["GBR"]
    assert C.validate_traveler(ok) == []


def test_party_id_content():
    assert C.validate_party(_good_party()) == []

    for bad_val in ("", "   ", 7):
        rec = _good_party()
        rec["party_id"] = bad_val
        assert any("party_id must be a non-empty string" in e for e in C.validate_party(rec))


def test_party_travelers_list():
    empty = _good_party()
    empty["travelers"] = []
    assert any("travelers must be a non-empty list" in e for e in C.validate_party(empty))

    not_list = _good_party()
    not_list["travelers"] = "t1"
    assert any("travelers must be a non-empty list" in e for e in C.validate_party(not_list))


def test_party_duplicate_traveler_ids():
    rec = C.make_party(
        "p1", [C.make_traveler("dup", ["USA"]), C.make_traveler("dup", ["GBR"])]
    )
    errs = C.validate_party(rec)
    assert any("duplicate traveler_id 'dup'" in e for e in errs)


def test_party_traveler_validation_cascade():
    # an invalid nested traveler propagates with a 'party.travelers[i]:' prefix
    bad_traveler = {"traveler_id": "", "nationalities": []}
    rec = C.make_party("p1", [bad_traveler])
    errs = C.validate_party(rec)
    assert any(e.startswith("party.travelers[0]:") for e in errs)
    assert any("traveler_id must be a non-empty string" in e for e in errs)
