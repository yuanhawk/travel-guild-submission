"""
test_currency_advisory_cov3.py — Unit tests for utils/currency_advisory.py

Covers:
  - CURRENCY_DATA structural integrity (valid tiers, positive rates, USD anchor)
  - Currency-correction assertions (TRY, IRR, ILS, SGD, SOS, SYP)
  - country->currency mapping spot-checks
  - convert_usd_cents: determinism + None on unseeded
  - exchange_timing_advice: tier routing + caveat always present + unseeded
  - var-0 loop: 3x identical results
"""

import unittest

from utils.currency_advisory import (
    AS_OF,
    COUNTRY_TO_CURRENCY,
    CURRENCY_DATA,
    TIER_HYPERINFLATION,
    TIER_STABLE,
    TIER_STRONG,
    TIER_WEAKENING,
    _VALID_TIERS,
    convert_usd_cents,
    currency_for_country,
    exchange_timing_advice,
    usd_per_unit,
)


class TestCurrencyDataIntegrity(unittest.TestCase):
    """Every CURRENCY_DATA entry must have a valid tier and a positive rate."""

    def test_all_entries_have_valid_tier(self):
        for iso, entry in CURRENCY_DATA.items():
            with self.subTest(iso=iso):
                self.assertIn(
                    entry["stability_tier"],
                    _VALID_TIERS,
                    f"{iso}: invalid tier '{entry['stability_tier']}'"
                )

    def test_all_entries_have_positive_rate(self):
        for iso, entry in CURRENCY_DATA.items():
            with self.subTest(iso=iso):
                self.assertGreater(
                    entry["usd_per_unit"],
                    0,
                    f"{iso}: usd_per_unit must be > 0, got {entry['usd_per_unit']}"
                )

    def test_usd_anchor_present(self):
        self.assertIn("USD", CURRENCY_DATA)
        self.assertAlmostEqual(CURRENCY_DATA["USD"]["usd_per_unit"], 1.0)
        self.assertEqual(CURRENCY_DATA["USD"]["stability_tier"], TIER_STRONG)

    def test_all_entries_have_required_fields(self):
        required = {"usd_per_unit", "stability_tier", "name", "rationale", "provenance"}
        for iso, entry in CURRENCY_DATA.items():
            with self.subTest(iso=iso):
                missing = required - set(entry.keys())
                self.assertEqual(
                    missing, set(),
                    f"{iso}: missing fields {missing}"
                )

    def test_no_byr_legacy_code(self):
        """BYR is a retired ISO code; only BYN (redenominated 2016) should be present."""
        self.assertNotIn("BYR", CURRENCY_DATA, "BYR is a retired code — must not be in dataset")
        self.assertIn("BYN", CURRENCY_DATA)

    def test_zar_not_duplicated(self):
        """ZAR had a duplicate entry in source data — after dedup only one entry present."""
        # Python dicts enforce uniqueness; if module loads, it's already deduped.
        # This test verifies ZAR IS present and valid.
        self.assertIn("ZAR", CURRENCY_DATA)
        self.assertGreater(CURRENCY_DATA["ZAR"]["usd_per_unit"], 0)


class TestCurrencyCorrections(unittest.TestCase):
    """Assert every correction is reflected in the seeded data."""

    def test_try_rate_updated(self):
        """TRY was ~0.0294/0.027 (stale); corrected to ~0.0215 (~46.5 TRY/USD)."""
        rate = CURRENCY_DATA["TRY"]["usd_per_unit"]
        self.assertLess(rate, 0.025, f"TRY usd_per_unit {rate} should be < 0.025 (corrected: ~0.0215)")
        self.assertGreater(rate, 0.018, f"TRY usd_per_unit {rate} seems too low")

    def test_irr_rate_corrected(self):
        """IRR was 0.0000238 (~42k/USD, defunct peg); corrected: ~0.00000073 (~1.375M/USD)."""
        rate = CURRENCY_DATA["IRR"]["usd_per_unit"]
        self.assertLess(rate, 0.000002, f"IRR usd_per_unit {rate} should be < 0.000002 (corrected: ~7.3e-7)")
        self.assertGreater(rate, 0.0, "IRR rate must be positive")

    def test_ils_tier_is_stable(self):
        """ILS was 'weakening'; Note: 'stable' (shekel +12-20% YoY, strongest since 1993)."""
        self.assertEqual(
            CURRENCY_DATA["ILS"]["stability_tier"],
            TIER_STABLE,
            "ILS tier should be 'stable' after correction"
        )

    def test_ils_rate_updated(self):
        """ILS was 0.272; corrected: ~0.334 (~2.99/USD Jun 2026)."""
        rate = CURRENCY_DATA["ILS"]["usd_per_unit"]
        self.assertAlmostEqual(rate, 0.334, places=3)

    def test_sgd_rate_updated(self):
        """SGD was 0.745; corrected: 0.775."""
        rate = CURRENCY_DATA["SGD"]["usd_per_unit"]
        self.assertAlmostEqual(rate, 0.775, places=3)

    def test_bnd_rate_matches_sgd(self):
        """BND is pegged 1:1 to SGD — must equal the corrected SGD rate."""
        self.assertAlmostEqual(
            CURRENCY_DATA["BND"]["usd_per_unit"],
            CURRENCY_DATA["SGD"]["usd_per_unit"],
            places=4,
            msg="BND must equal SGD rate (1:1 peg)"
        )

    def test_sos_rate_corrected(self):
        """SOS was 0.00174 (~575/USD); corrected: ~0.000036 (~27,000/USD)."""
        rate = CURRENCY_DATA["SOS"]["usd_per_unit"]
        self.assertLess(rate, 0.0001, f"SOS usd_per_unit {rate} should be < 0.0001 (corrected: ~3.6e-5)")
        self.assertGreater(rate, 0.0, "SOS rate must be positive")

    def test_syp_rate_corrected(self):
        """SYP was 0.000077 (pre-redenomination); corrected: ~0.0087 (new SYP ~115/USD)."""
        rate = CURRENCY_DATA["SYP"]["usd_per_unit"]
        self.assertGreater(rate, 0.005, f"SYP usd_per_unit {rate} should be > 0.005 (corrected: ~0.0087)")
        self.assertLess(rate, 0.02, f"SYP usd_per_unit {rate} should be < 0.02")

    def test_lak_tier_is_weakening(self):
        """LAK was 'hyperinflation'; Note: 'weakening' (rate flat 21-22k/USD, inflation ~7.7%)."""
        self.assertEqual(
            CURRENCY_DATA["LAK"]["stability_tier"],
            TIER_WEAKENING,
            "LAK tier should be 'weakening' after correction"
        )

    def test_pgk_rate_corrected(self):
        """PGK was 0.268; corrected: 0.224 (~4.46/USD Jun 2026)."""
        self.assertAlmostEqual(CURRENCY_DATA["PGK"]["usd_per_unit"], 0.224, places=3)

    def test_xpf_rate_corrected(self):
        """XPF was 0.00888; corrected: 0.00955 (EUR/USD ~1.14, fixed 119.33/EUR)."""
        self.assertAlmostEqual(CURRENCY_DATA["XPF"]["usd_per_unit"], 0.00955, places=5)

    def test_ves_rate_corrected(self):
        """VES was 0.000027 (~37,000/USD); corrected: ~0.0017 (~600/USD Jun 2026)."""
        rate = CURRENCY_DATA["VES"]["usd_per_unit"]
        self.assertGreater(rate, 0.001, f"VES usd_per_unit {rate} should be > 0.001 (corrected: ~0.0017)")
        self.assertLess(rate, 0.003, f"VES usd_per_unit {rate} seems too high")

    def test_htg_tier_is_weakening(self):
        """HTG was 'hyperinflation'; Note: 'weakening' (FX flat at ~130-131/USD in 2024-2026)."""
        self.assertEqual(
            CURRENCY_DATA["HTG"]["stability_tier"],
            TIER_WEAKENING,
            "HTG tier should be 'weakening' after correction"
        )

    def test_kes_tier_is_stable(self):
        """KES was 'weakening'; Note: 'stable' (KES recovered ~20% from 2024 low)."""
        self.assertEqual(
            CURRENCY_DATA["KES"]["stability_tier"],
            TIER_STABLE,
            "KES tier should be 'stable' after correction"
        )

    def test_ghs_tier_is_stable(self):
        """GHS was 'weakening'; Note: 'stable' (cedi was world's top performer in 2025)."""
        self.assertEqual(
            CURRENCY_DATA["GHS"]["stability_tier"],
            TIER_STABLE,
            "GHS tier should be 'stable' after correction"
        )

    def test_byn_not_byr(self):
        """BYR (dead ISO code) must be BYN (redenominated Jul 2016)."""
        self.assertNotIn("BYR", CURRENCY_DATA)
        self.assertIn("BYN", CURRENCY_DATA)
        self.assertAlmostEqual(CURRENCY_DATA["BYN"]["usd_per_unit"], 0.029, places=4)
        self.assertEqual(CURRENCY_DATA["BYN"]["stability_tier"], TIER_WEAKENING)

    def test_mmk_rationale_updated(self):
        """MMK rationale must mention the corrected CBM rate (~3,658/USD Jun 2026).
        The old 2,100/USD peg may appear as historical context but must be described as
        'abandoned' (not as the current rate), and 3,658 must be present as the current reference.
        """
        rationale = CURRENCY_DATA["MMK"]["rationale"]
        self.assertIn("3,658", rationale, "Corrected CBM rate ~3,658 should appear in MMK rationale")
        # If 2,100 appears, it must be in context of abandonment (historical note), not as current rate
        if "2,100" in rationale:
            self.assertIn("abandon", rationale.lower(),
                          "If 2,100 appears in MMK rationale it must be described as abandoned")


class TestCountryCurrencyMapping(unittest.TestCase):
    """COUNTRY_TO_CURRENCY spot-checks."""

    def test_indonesia(self):
        self.assertEqual(currency_for_country("id"), "IDR")

    def test_singapore(self):
        self.assertEqual(currency_for_country("sg"), "SGD")

    def test_japan(self):
        self.assertEqual(currency_for_country("jp"), "JPY")

    def test_turkey(self):
        self.assertEqual(currency_for_country("tr"), "TRY")

    def test_case_insensitive(self):
        self.assertEqual(currency_for_country("ID"), "IDR")
        self.assertEqual(currency_for_country("SG"), "SGD")
        self.assertEqual(currency_for_country("JP"), "JPY")

    def test_eurozone_germany(self):
        self.assertEqual(currency_for_country("de"), "EUR")

    def test_eurozone_france(self):
        self.assertEqual(currency_for_country("fr"), "EUR")

    def test_cfa_senegal(self):
        self.assertEqual(currency_for_country("sn"), "XOF")

    def test_east_caribbean(self):
        self.assertEqual(currency_for_country("ag"), "XCD")  # Antigua

    def test_unseeded_country_returns_none(self):
        self.assertIsNone(currency_for_country("zz"))
        self.assertIsNone(currency_for_country("xx"))


class TestUsdPerUnit(unittest.TestCase):
    """usd_per_unit helper."""

    def test_usd_is_one(self):
        self.assertEqual(usd_per_unit("USD"), 1.0)
        self.assertEqual(usd_per_unit("usd"), 1.0)

    def test_returns_none_for_unknown(self):
        self.assertIsNone(usd_per_unit("ZZZ"))
        self.assertIsNone(usd_per_unit("XYZ"))

    def test_case_insensitive(self):
        self.assertEqual(usd_per_unit("sgd"), usd_per_unit("SGD"))
        self.assertEqual(usd_per_unit("jpy"), usd_per_unit("JPY"))


class TestConvertUsdCents(unittest.TestCase):
    """convert_usd_cents: determinism and None on unseeded."""

    def test_usd_identity(self):
        """100 USD cents -> 100 USD minor units."""
        result = convert_usd_cents(100, "USD")
        self.assertEqual(result, 100)

    def test_sgd_conversion(self):
        """10000 USD cents (=$100) -> ~12,903 SGD minor units (at 0.775)."""
        result = convert_usd_cents(10000, "SGD")
        self.assertIsNotNone(result)
        # 10000 / 0.775 = 12903.2 -> 12903
        self.assertAlmostEqual(result, 12903, delta=10)

    def test_jpy_conversion(self):
        """10000 USD cents (=$100) -> ~1,615,000 JPY units (at 0.006191, ~161.5 JPY/USD).
        convert_usd_cents returns target units via round(usd_cents / rate).
        10000 / 0.006191 ≈ 1,615,248 JPY units.
        """
        result = convert_usd_cents(10000, "JPY")
        self.assertIsNotNone(result)
        # 10000 / 0.006191 ≈ 1,615,248
        self.assertAlmostEqual(result, 1_615_248, delta=500)

    def test_returns_none_for_unseeded(self):
        result = convert_usd_cents(10000, "ZZZ")
        self.assertIsNone(result)

    def test_zero_usd_cents(self):
        result = convert_usd_cents(0, "SGD")
        self.assertEqual(result, 0)

    def test_deterministic_same_result(self):
        """Same input always yields byte-identical result (var-0)."""
        r1 = convert_usd_cents(5000, "IDR")
        r2 = convert_usd_cents(5000, "IDR")
        r3 = convert_usd_cents(5000, "IDR")
        self.assertEqual(r1, r2)
        self.assertEqual(r2, r3)

    def test_result_is_integer(self):
        result = convert_usd_cents(9999, "EUR")
        self.assertIsInstance(result, int)

    def test_irr_conversion(self):
        """IRR correction check: large number of rials per USD cent."""
        result = convert_usd_cents(100, "IRR")  # 1 USD -> ~1,375,000 IRR major -> 137,500,000 minor
        self.assertIsNotNone(result)
        # 100 USD cents / 7.3e-7 ≈ 136,986,301
        self.assertGreater(result, 1_000_000)


class TestExchangeTimingAdvice(unittest.TestCase):
    """exchange_timing_advice: tier routing, guidance text, caveat always present."""

    def test_caveat_always_present(self):
        for iso in ["IDR", "CHF", "SGD", "IRR", "ZZZ"]:
            with self.subTest(iso=iso):
                result = exchange_timing_advice(iso)
                self.assertIn("caveat", result)
                self.assertTrue(len(result["caveat"]) > 10)
                self.assertIn(AS_OF, result["caveat"])

    def test_caveat_with_custom_home(self):
        result = exchange_timing_advice("IDR", home_iso="GBP")
        self.assertIn(AS_OF, result["caveat"])

    def test_hyperinflation_guidance_idr_is_weakening(self):
        """IDR is 'weakening' — guidance should say 'softened'."""
        result = exchange_timing_advice("IDR")
        self.assertEqual(result["tier"], "weakening")
        self.assertIn("softened", result["guidance"].lower())

    def test_strong_currency_chf(self):
        """CHF is 'strong' — guidance should mention 'locking in'."""
        result = exchange_timing_advice("CHF")
        self.assertEqual(result["tier"], "strong")
        self.assertIn("lock", result["guidance"].lower())

    def test_hyperinflation_irr(self):
        """IRR is 'hyperinflation' — guidance warns against pre-buying."""
        result = exchange_timing_advice("IRR")
        self.assertEqual(result["tier"], "hyperinflation")
        self.assertIn("small amounts", result["guidance"].lower())
        self.assertIn("USD", result["guidance"])

    def test_strong_currency_sgd(self):
        """SGD is 'strong' — guidance mentions locking in a rate ahead."""
        result = exchange_timing_advice("SGD")
        self.assertEqual(result["tier"], "strong")
        self.assertIn("lock", result["guidance"].lower())

    def test_unseeded_currency_returns_unknown(self):
        result = exchange_timing_advice("ZZZ")
        self.assertEqual(result["tier"], "unknown")
        self.assertIn("not seeded", result["guidance"].lower())
        self.assertIn("caveat", result)

    def test_case_insensitive(self):
        r1 = exchange_timing_advice("sgd")
        r2 = exchange_timing_advice("SGD")
        self.assertEqual(r1["tier"], r2["tier"])
        self.assertEqual(r1["guidance"], r2["guidance"])

    def test_guidance_names_local_currency(self):
        """Guidance text should mention the local currency code."""
        result = exchange_timing_advice("IRR")
        self.assertIn("IRR", result["guidance"])

    def test_guidance_names_home_currency(self):
        """Guidance text should mention the home currency code."""
        result = exchange_timing_advice("IRR", home_iso="SGD")
        self.assertIn("SGD", result["guidance"])


class TestVar0Loop(unittest.TestCase):
    """Var-0 guarantee: 3 identical calls produce byte-identical results."""

    def test_currency_for_country_var0(self):
        results = [currency_for_country("id") for _ in range(3)]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_convert_usd_cents_var0(self):
        results = [convert_usd_cents(10000, "SGD") for _ in range(3)]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_exchange_timing_advice_var0(self):
        results = [exchange_timing_advice("IDR") for _ in range(3)]
        for key in ("tier", "guidance", "caveat"):
            self.assertEqual(results[0][key], results[1][key])
            self.assertEqual(results[1][key], results[2][key])


if __name__ == "__main__":
    unittest.main()
