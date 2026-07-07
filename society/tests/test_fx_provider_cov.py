"""
test_fx_provider_cov.py — Comprehensive coverage of fx_provider deterministic behaviors.

Covers all uncovered code paths in utils/fx_provider.py:
  - supported_currencies() enumeration and sorting
  - FXResult.to_emit() method + dict shape validation
  - FXResult.to_money() and to_money() integration with contracts.make_money()
  - FXResult.provenance() method integration with contracts.make_provenance()
  - Rounding behavior: edge cases (sub-cent, boundary values)
  - Case-insensitivity of currency codes (strip + lower)
  - Negative native_cents clamped to 0 (deterministic decline)
  - All 8 supported currencies (USD, AUD, THB, SGD, MYR, IDR, ETB, EUR, GBP)
  - Large amount conversions (deterministic calculation)
  - Zero and near-zero conversions
  - FXResult object slots and invariants
  - convert_to_usd_cents() full parameter space (edge cases deferred to seam)

All deterministic — no network, no LLM, pure arithmetic.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from core import contracts as C
from utils import fx_provider as fx
from utils.intent_parser import SEEDED_FX_USD_PER_UNIT


# ===========================================================================
# supported_currencies() — enumeration + sorting
# ===========================================================================


def test_supported_currencies_returns_tuple_of_sorted_uppercase_codes():
    """supported_currencies() yields tuple of all ISO codes, uppercase, sorted."""
    result = fx.supported_currencies()
    assert isinstance(result, tuple)
    assert len(result) == len(SEEDED_FX_USD_PER_UNIT)
    assert result == tuple(sorted(result))  # sorted
    assert all(c.isupper() for c in result)  # all uppercase
    assert set(result) == {c.upper() for c in SEEDED_FX_USD_PER_UNIT}


def test_supported_currencies_includes_all_eight():
    """All 8 seeded currencies are exposed."""
    currencies = fx.supported_currencies()
    expected = {"USD", "AUD", "THB", "SGD", "MYR", "IDR", "ETB", "EUR", "GBP"}
    assert set(currencies) == expected


def test_supported_currencies_deterministic_order():
    """Multiple calls yield identical tuple (deterministic iteration)."""
    a = fx.supported_currencies()
    b = fx.supported_currencies()
    assert a == b  # byte-identical


# ===========================================================================
# FXResult.to_emit() — §19.5 provider emit shape
# ===========================================================================


def test_to_emit_shape_matches_spec():
    """to_emit() returns flat dict with exactly the spec'd keys."""
    r = fx.normalize(100000, "USD")
    emit = r.to_emit()
    assert isinstance(emit, dict)
    expected_keys = {
        "native_cents", "currency", "fx_rate", "fx_source",
        "fetched_at", "usd_cents", "tier", "exhausted",
    }
    assert set(emit) == expected_keys


def test_to_emit_values_match_result_fields():
    """to_emit() dict values match the FXResult fields."""
    r = fx.normalize(50000, "AUD")
    emit = r.to_emit()
    assert emit["native_cents"] == r.native_cents
    assert emit["currency"] == r.currency
    assert emit["fx_rate"] == r.fx_rate
    assert emit["fx_source"] == r.fx_source
    assert emit["fetched_at"] == r.fetched_at
    assert emit["usd_cents"] == r.usd_cents
    assert emit["tier"] == r.tier
    assert emit["exhausted"] == r.exhausted


def test_to_emit_exhausted_case():
    """to_emit() works for exhausted (unknown currency) case."""
    r = fx.normalize(100000, "XXX")
    emit = r.to_emit()
    assert emit["exhausted"] is True
    assert emit["usd_cents"] == 0
    assert emit["tier"] == "exhausted"


# ===========================================================================
# FXResult.to_money() — contracts.make_money() integration
# ===========================================================================


def test_to_money_is_valid_contract():
    """to_money() produces a valid contracts.make_money() shape."""
    r = fx.normalize(200000, "SGD")
    money = r.to_money()
    assert C.is_valid_money(money)


def test_to_money_includes_fx_nested_envelope():
    """to_money() embeds fx provenance in nested dict."""
    r = fx.normalize(150000, "MYR")
    money = r.to_money()
    assert "fx" in money
    assert isinstance(money["fx"], dict)
    assert "fx_rate" in money["fx"]
    assert "fx_source" in money["fx"]
    assert "fetched_at" in money["fx"]


def test_to_money_all_supported_currencies():
    """to_money() produces valid shape for all 8 supported currencies."""
    for code in fx.supported_currencies():
        r = fx.normalize(100000, code)
        money = r.to_money()
        assert C.is_valid_money(money), f"to_money() failed for {code}"


def test_to_money_exhausted_emits_unusable_money():
    """to_money() on an EXHAUSTED currency (unknown 'ZZZ' -> Tier-3 honest decline) emits an
    UNUSABLE money tag: usd_cents==0 AND contracts.is_valid_money()==False. The seam is NOT a
    silently-valid zero — callers MUST guard via .exhausted. (Prev version asserted only
    isinstance(dict), masking this real FX-seam contract.)"""
    r = fx.normalize(100000, "ZZZ")
    assert r.exhausted is True
    money = r.to_money()
    assert isinstance(money, dict)
    assert money["usd_cents"] == 0
    assert not C.is_valid_money(money), (
        "exhausted FX must yield an INVALID money tag (callers guard via .exhausted), "
        "not a silently-valid zero"
    )


# ===========================================================================
# FXResult.provenance() — contracts.make_provenance() integration
# ===========================================================================


def test_provenance_is_valid_contract():
    """provenance() produces a valid contracts.make_provenance() shape."""
    r = fx.normalize(100000, "IDR")
    prov = r.provenance()
    assert C.is_valid_provenance(prov)


def test_provenance_includes_source_and_fetched_at():
    """provenance() captures source and fetched_at."""
    r = fx.normalize(100000, "EUR")
    prov = r.provenance()
    assert prov["source"] == fx.FX_PROVENANCE
    assert prov["fetched_at"] == fx.FX_FETCHED_AT


def test_provenance_tier_matches_result():
    """provenance() tier field matches FXResult.tier."""
    r = fx.normalize(100000, "GBP")
    prov = r.provenance()
    assert prov["tier"] == r.tier


def test_provenance_source_url_none_for_seeded():
    """Seeded rates have no external source_url (internal table only)."""
    r = fx.normalize(100000, "USD")
    prov = r.provenance()
    assert prov["source_url"] is None


def test_provenance_ttl_none_for_seeded():
    """Seeded rates have no TTL (fixed constant, no expiry)."""
    r = fx.normalize(100000, "AUD")
    prov = r.provenance()
    assert prov["ttl"] is None


# ===========================================================================
# FXResult object slots + invariants
# ===========================================================================


def test_fxresult_has_slots():
    """FXResult uses __slots__ for memory efficiency."""
    r = fx.normalize(100000, "USD")
    assert hasattr(fx.FXResult, "__slots__")
    assert len(fx.FXResult.__slots__) == 8


def test_fxresult_cannot_add_arbitrary_attributes():
    """__slots__ prevents adding undefined attributes."""
    r = fx.normalize(100000, "USD")
    try:
        r.arbitrary_field = "nope"
        assert False, "expected AttributeError"
    except AttributeError:
        pass


def test_fxresult_all_fields_present():
    """All 8 slot fields are initialized."""
    r = fx.normalize(100000, "USD")
    assert hasattr(r, "native_cents")
    assert hasattr(r, "currency")
    assert hasattr(r, "fx_rate")
    assert hasattr(r, "fx_source")
    assert hasattr(r, "fetched_at")
    assert hasattr(r, "usd_cents")
    assert hasattr(r, "tier")
    assert hasattr(r, "exhausted")


# ===========================================================================
# All 8 supported currencies — conversion math
# ===========================================================================


def test_normalize_usd_identity():
    """USD 1000 → 100000 cents (identity, rate=1.0)."""
    r = fx.normalize(100000, "USD")
    assert r.usd_cents == 100000
    assert r.fx_rate == 1.0


def test_normalize_aud_matches_seed():
    """AUD conversion: rate=0.66 → AUD 100 (10000¢) → ~6600 USD¢."""
    r = fx.normalize(10000, "AUD")
    expected = int(round((10000 / 100.0) * 0.66 * 100))
    assert r.usd_cents == expected
    assert r.fx_rate == 0.66


def test_normalize_thb_matches_seed():
    """THB conversion: rate=0.029 → THB 1000 (100000¢) → ~290 USD¢."""
    r = fx.normalize(100000, "THB")
    expected = int(round((100000 / 100.0) * 0.029 * 100))
    assert r.usd_cents == expected
    assert r.fx_rate == 0.029


def test_normalize_sgd_matches_seed():
    """SGD conversion: rate=0.74."""
    r = fx.normalize(10000, "SGD")
    expected = int(round((10000 / 100.0) * 0.74 * 100))
    assert r.usd_cents == expected
    assert r.fx_rate == 0.74


def test_normalize_myr_matches_seed():
    """MYR conversion: rate=0.22."""
    r = fx.normalize(10000, "MYR")
    expected = int(round((10000 / 100.0) * 0.22 * 100))
    assert r.usd_cents == expected
    assert r.fx_rate == 0.22


def test_normalize_idr_matches_seed():
    """IDR conversion: rate=0.000063 (very small)."""
    r = fx.normalize(10000000, "IDR")  # 100,000 IDR
    expected = int(round((10000000 / 100.0) * 0.000063 * 100))
    assert r.usd_cents == expected
    assert r.fx_rate == 0.000063


def test_normalize_etb_matches_seed():
    """ETB conversion: rate=0.018."""
    r = fx.normalize(10000, "ETB")
    expected = int(round((10000 / 100.0) * 0.018 * 100))
    assert r.usd_cents == expected
    assert r.fx_rate == 0.018


def test_normalize_eur_matches_seed():
    """EUR conversion: rate=1.08."""
    r = fx.normalize(10000, "EUR")
    expected = int(round((10000 / 100.0) * 1.08 * 100))
    assert r.usd_cents == expected
    assert r.fx_rate == 1.08


def test_normalize_gbp_matches_seed():
    """GBP conversion: rate=1.27."""
    r = fx.normalize(10000, "GBP")
    expected = int(round((10000 / 100.0) * 1.27 * 100))
    assert r.usd_cents == expected
    assert r.fx_rate == 1.27


# ===========================================================================
# Case-insensitivity — currency code parsing
# ===========================================================================


def test_normalize_case_insensitive_lowercase():
    """normalize() accepts lowercase currency codes."""
    r1 = fx.normalize(100000, "usd")
    r2 = fx.normalize(100000, "USD")
    assert r1.usd_cents == r2.usd_cents
    assert r1.currency == r2.currency == "USD"


def test_normalize_case_insensitive_uppercase():
    """normalize() accepts uppercase currency codes."""
    r1 = fx.normalize(100000, "aud")
    r2 = fx.normalize(100000, "AUD")
    assert r1.usd_cents == r2.usd_cents


def test_normalize_case_insensitive_mixed_case():
    """normalize() accepts mixed-case currency codes (converted to uppercase)."""
    r1 = fx.normalize(100000, "ThB")
    r2 = fx.normalize(100000, "thb")
    assert r1.usd_cents == r2.usd_cents
    assert r1.currency == "THB"


def test_normalize_strips_whitespace():
    """normalize() strips leading/trailing whitespace from currency code."""
    r1 = fx.normalize(100000, "USD")
    r2 = fx.normalize(100000, "  USD  ")
    assert r1.usd_cents == r2.usd_cents
    assert r1.currency == "USD"


# ===========================================================================
# Negative native_cents — clamped to 0
# ===========================================================================


def test_normalize_negative_cents_clamped_to_zero():
    """normalize() clamps negative native_cents to 0 (honest decline)."""
    r = fx.normalize(-50000, "AUD")
    assert r.native_cents == 0  # clamped
    assert r.usd_cents == 0


def test_normalize_negative_unknown_currency_still_exhausted():
    """normalize() on negative unknown currency is exhausted (not USD)."""
    r = fx.normalize(-50000, "JPY")
    assert r.native_cents == 0
    assert r.exhausted is True
    assert r.usd_cents == 0


# ===========================================================================
# Zero native_cents
# ===========================================================================


def test_normalize_zero_cents_usd():
    """normalize(0, USD) → 0 USD cents (not missing/None)."""
    r = fx.normalize(0, "USD")
    assert r.usd_cents == 0
    assert not r.exhausted


def test_normalize_zero_cents_aud():
    """normalize(0, AUD) → 0 USD cents."""
    r = fx.normalize(0, "AUD")
    assert r.usd_cents == 0
    assert not r.exhausted


def test_normalize_zero_cents_unknown_currency():
    """normalize(0, unknown) → 0 USD cents, exhausted=True."""
    r = fx.normalize(0, "ZZZ")
    assert r.usd_cents == 0
    assert r.exhausted is True


# ===========================================================================
# Large amounts — no overflow or precision loss
# ===========================================================================


def test_normalize_large_amount_usd():
    """Large amounts (e.g. USD 1M) convert without overflow."""
    r = fx.normalize(100000000, "USD")  # $1M
    assert r.usd_cents == 100000000
    assert r.fx_rate == 1.0


def test_normalize_large_amount_aud():
    """Large AUD amounts convert correctly."""
    r = fx.normalize(100000000, "AUD")  # AUD $1M
    expected = int(round((100000000 / 100.0) * 0.66 * 100))
    assert r.usd_cents == expected


def test_normalize_large_idr_amount():
    """Large IDR amounts (tiny rate) convert correctly."""
    r = fx.normalize(1000000000, "IDR")  # IDR 10M
    expected = int(round((1000000000 / 100.0) * 0.000063 * 100))
    assert r.usd_cents == expected
    assert r.usd_cents > 0


# ===========================================================================
# Rounding behavior — boundary cases
# ===========================================================================


def test_normalize_rounding_standard():
    """FX conversion uses int(round(...)) — Python's rounding."""
    # 333 THB: (333 / 100) * 0.029 * 100 = 9.657, rounds to 10
    r = fx.normalize(333, "THB")
    assert r.usd_cents == 10


def test_normalize_rounding_boundary_50():
    """Rounding at .5 boundary uses Python's banker's rounding (round to even)."""
    # Python 3 uses banker's rounding (round half to even).
    # Example: 15 THB (rate 0.029) → 0.435 USD, should round.
    r = fx.normalize(1500, "THB")
    # (1500 / 100) * 0.029 * 100 = 43.5
    # Python's round(43.5) → 44 (banker's rounding)
    assert r.usd_cents == 44


def test_normalize_sub_cent_amounts():
    """Very small amounts may round to 0."""
    r = fx.normalize(1, "IDR")  # 0.01 IDR
    # (1 / 100) * 0.000063 * 100 = 0.0000063, rounds to 0
    assert r.usd_cents == 0


# ===========================================================================
# Tier field — SEEDED vs EXHAUSTED
# ===========================================================================


def test_normalize_successful_has_seeded_tier():
    """Known currency → tier='seeded'."""
    for code in fx.supported_currencies():
        r = fx.normalize(100000, code)
        assert r.tier == C.SourceTier.SEEDED.value


def test_normalize_unknown_currency_has_exhausted_tier():
    """Unknown currency → tier='exhausted'."""
    r = fx.normalize(100000, "ABC")
    assert r.tier == C.SourceTier.EXHAUSTED.value


def test_normalize_exhausted_tier_consistent_with_flag():
    """tier='exhausted' iff exhausted=True."""
    r1 = fx.normalize(100000, "USD")
    assert (r1.tier == "exhausted") == r1.exhausted

    r2 = fx.normalize(100000, "XXX")
    assert (r2.tier == "exhausted") == r2.exhausted


# ===========================================================================
# fx_source — FX_PROVENANCE constant
# ===========================================================================


def test_normalize_fx_source_is_provenance_string():
    """All results have fx_source = FX_PROVENANCE."""
    for code in fx.supported_currencies():
        r = fx.normalize(100000, code)
        assert r.fx_source == fx.FX_PROVENANCE


def test_normalize_unknown_currency_also_has_provenance():
    """Unknown currency results also emit the standard provenance."""
    r = fx.normalize(100000, "ZZZ")
    assert r.fx_source == fx.FX_PROVENANCE


# ===========================================================================
# fetched_at — FX_FETCHED_AT constant
# ===========================================================================


def test_normalize_fetched_at_is_constant():
    """All results have fetched_at = FX_FETCHED_AT."""
    for code in fx.supported_currencies():
        r = fx.normalize(100000, code)
        assert r.fetched_at == fx.FX_FETCHED_AT


def test_fetched_at_is_iso_date():
    """FX_FETCHED_AT follows ISO 8601 date format."""
    assert fx.FX_FETCHED_AT == "2026-01-01"


# ===========================================================================
# convert_to_usd_cents() — Front-door reconciliation shim
# ===========================================================================


def test_convert_to_usd_cents_positive_known():
    """convert_to_usd_cents(amount, code) → USD cents for known currency."""
    result = fx.convert_to_usd_cents(100.0, "AUD")
    assert isinstance(result, int)
    assert result > 0


def test_convert_to_usd_cents_usd_identity():
    """convert_to_usd_cents(100.0, USD) → 10000 cents."""
    result = fx.convert_to_usd_cents(100.0, "USD")
    assert result == 10000


def test_convert_to_usd_cents_aud_matches_intent_parser():
    """convert_to_usd_cents preserves original intent_parser contract."""
    from utils import intent_parser as ip
    amount = 500.0
    code = "AUD"
    result = fx.convert_to_usd_cents(amount, code)
    expected = ip._convert_to_usd_cents(amount, code)
    assert result == expected


def test_convert_to_usd_cents_zero_returns_none():
    """convert_to_usd_cents(0, code) → None (non-positive)."""
    result = fx.convert_to_usd_cents(0.0, "AUD")
    assert result is None


def test_convert_to_usd_cents_negative_returns_none():
    """convert_to_usd_cents(negative, code) → None."""
    result = fx.convert_to_usd_cents(-100.0, "USD")
    assert result is None


def test_convert_to_usd_cents_unknown_currency_returns_none():
    """convert_to_usd_cents(amount, unknown) → None."""
    result = fx.convert_to_usd_cents(100.0, "JPY")
    assert result is None


def test_convert_to_usd_cents_exhausted_result_returns_none():
    """If normalize().exhausted, shim returns None."""
    result = fx.convert_to_usd_cents(100.0, "ZZZ")
    assert result is None


def test_convert_to_usd_cents_case_insensitive():
    """convert_to_usd_cents() accepts any case for currency code."""
    r1 = fx.convert_to_usd_cents(100.0, "aud")
    r2 = fx.convert_to_usd_cents(100.0, "AUD")
    assert r1 == r2


def test_convert_to_usd_cents_large_amount():
    """Large amounts convert correctly."""
    result = fx.convert_to_usd_cents(1000000.0, "USD")
    assert result == 100000000


def test_convert_to_usd_cents_small_amount():
    """Small amounts convert correctly (may round to 0)."""
    result = fx.convert_to_usd_cents(0.0001, "AUD")
    # 0.0001 AUD → 0.00001 native cents (after rounding) → tiny USD
    assert result is None or result == 0  # either way, exhausted


def test_convert_to_usd_cents_tiny_positive_amount():
    """Tiny positive amount (≠ zero) still tries conversion."""
    result = fx.convert_to_usd_cents(0.001, "USD")
    # 0.001 USD → 0.1 cents, rounds to 0 → returns None
    assert result is None or result == 0


def test_convert_to_usd_cents_all_supported_currencies():
    """convert_to_usd_cents works for all 8 supported currencies."""
    for code in fx.supported_currencies():
        result = fx.convert_to_usd_cents(100.0, code)
        assert isinstance(result, int) and result > 0, f"Failed for {code}"


# ===========================================================================
# Determinism — reproducibility across runs
# ===========================================================================


def test_normalize_deterministic_same_input_same_output():
    """normalize() produces byte-identical results for same input."""
    for _ in range(5):
        r1 = fx.normalize(50000, "AUD")
        r2 = fx.normalize(50000, "AUD")
        assert r1.to_emit() == r2.to_emit()


def test_convert_to_usd_cents_deterministic():
    """convert_to_usd_cents() is deterministic."""
    for _ in range(5):
        result = fx.convert_to_usd_cents(200.0, "EUR")
        assert result == fx.convert_to_usd_cents(200.0, "EUR")


def test_supported_currencies_deterministic():
    """supported_currencies() returns identical tuple each time."""
    for _ in range(5):
        result = fx.supported_currencies()
        assert result == fx.supported_currencies()


# ===========================================================================
# Currency code normalization — edge cases
# ===========================================================================


def test_normalize_code_normalization_idempotent():
    """normalize() treats already-normalized codes identically."""
    r1 = fx.normalize(100000, "USD")
    r2 = fx.normalize(100000, "usd")
    r3 = fx.normalize(100000, "Usd")
    assert r1.to_emit() == r2.to_emit() == r3.to_emit()


def test_normalize_whitespace_stripping_idempotent():
    """Whitespace is stripped consistently."""
    r1 = fx.normalize(100000, "USD")
    r2 = fx.normalize(100000, "  USD  ")
    r3 = fx.normalize(100000, "\tUSD\n")
    assert r1.to_emit() == r2.to_emit() == r3.to_emit()


# ===========================================================================
# End-to-end: normalize() → to_emit() → to_money() → contracts validation
# ===========================================================================


def test_e2e_normalize_to_contracts_valid():
    """normalize() output passes all contract validators."""
    for code in fx.supported_currencies():
        r = fx.normalize(100000, code)
        assert C.is_valid_money(r.to_money())
        assert C.is_valid_provenance(r.provenance())


def test_e2e_shim_preserves_numeric_behavior():
    """convert_to_usd_cents() shim preserves numeric behavior of intent_parser."""
    from utils import intent_parser as ip
    test_cases = [
        (100.0, "AUD"),
        (50.0, "USD"),
        (1000.0, "EUR"),
    ]
    for amount, code in test_cases:
        seam = fx.convert_to_usd_cents(amount, code)
        original = ip._convert_to_usd_cents(amount, code)
        assert seam == original, f"Mismatch for {amount} {code}: {seam} vs {original}"
