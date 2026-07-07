"""
test_city_region_bare_key_consistency.py — regression suite for the Task-1
(2026-07-06) bare-key city->region bug class, a SIBLING of the #236/#240
cross-country-homonym bug this repo already guards
(test_city_country_region_invariant.py).

THE BUG (discovered via benchmark/runners/stress_test.py, run against the
live seed tables): risk_agent.py's bare (non-iso2-qualified) _CITY_TO_REGION
table had DUPLICATE keys for the SAME real-world, SINGLE-COUNTRY city under
different spellings (accent-folded vs. native-accented, e.g. 'hofn' vs
'höfn') that silently disagreed on the region — one spelling matched the
verified-correct sub-region, the other was stale/wrong. Whichever spelling a
caller happened to pass (or an upstream accent-normalization step produced)
silently decided which — sometimes wrong — hazard data the traveler got.

A SEPARATE but related gap: region_for_city's bare-key fallback used to
still RETURN a region it already knew (via its own ambiguous/override-
mismatch bookkeeping) might be wrong for the actual city being queried,
merely logging a warning. That is worse than an honest "unknown" — assess_leg
treats an unresolved (None) region as a conservative FLAG, never a silent
"safe", so serving a confidently-wrong region is strictly worse than failing
safe. This suite locks in the fix: region_for_city now returns None (fails
safe) instead of a bare-key result it has already flagged as
ambiguous/known-mismatched, whenever the country-qualified composite lookup
didn't resolve it first.

CI-safe: no LLM, no network, no live agent app.
"""
from __future__ import annotations

from collections import defaultdict

from agents import risk_agent as ra


# ---------------------------------------------------------------------------
# 1. Systematic sweep: NO bare-key spelling group may disagree with itself,
#    except the genuinely cross-country homonym(s) already tracked via
#    _AMBIGUOUS_CITIES (san pedro: Belize vs. Ivory Coast has no single
#    "correct" bare answer by design -- that's what the composite table +
#    the fail-safe below are for).
# ---------------------------------------------------------------------------
_KNOWN_GENUINE_CROSS_COUNTRY_INCONSISTENCIES = frozenset({"san pedro"})


def test_no_new_bare_key_duplicate_spelling_inconsistency() -> None:
    """For every group of _CITY_TO_REGION keys that normalize (accent-fold +
    hyphen/space-collapse) to the SAME real city, every raw spelling in that
    group must map to the SAME region. A future hand-authored or mechanical
    (osm k-NN) addition that reintroduces this bug class -- adding an
    accented/hyphenated duplicate of an existing bare key with a DIFFERENT
    region -- fails this test immediately, instead of silently reintroducing
    a spelling-dependent wrong-region bug."""
    groups: dict[str, dict[str, str]] = defaultdict(dict)
    for raw, region in ra._CITY_TO_REGION.items():
        groups[ra._normalize_city_key(raw)][raw] = region

    inconsistent = {
        norm: keys for norm, keys in groups.items()
        if len(set(keys.values())) > 1 and norm not in _KNOWN_GENUINE_CROSS_COUNTRY_INCONSISTENCIES
    }
    assert not inconsistent, (
        f"{len(inconsistent)} normalized-city group(s) have inconsistent bare "
        f"_CITY_TO_REGION values across spellings (same bug class as "
        f"hofn/höfn, oswiecim/oświęcim): {inconsistent}"
    )


# ---------------------------------------------------------------------------
# 2. The specific cities the stress test caught (both spellings must now
#    resolve to the SAME, geographically-verified region).
# ---------------------------------------------------------------------------
_FIXED_CITY_SPELLINGS = [
    # (spelling-A, spelling-B, expected region)
    ("hofn", "höfn", "is-south"),                        # SE Iceland (Hornafjörður)
    ("oswiecim", "oświęcim", "pl-krakow"),                # ~50km from Kraków
    ("fussen", "füssen", "de-bavaria"),                   # southern Bavaria (Neuschwanstein)
    ("nimes", "nîmes", "fr-south"),                       # Gard/Occitanie, Mediterranean
    ("puerto jimenez", "puerto jiménez", "cr-osa"),       # Osa Peninsula
    ("vik", "vík", "is-south"),                           # Vík í Mýrdal, South Iceland
    ("stykkisholmur", "stykkishólmur", "is"),             # Snæfellsnes, WEST Iceland (no is-west)
    ("andalsnes", "åndalsnes", "no"),                     # Møre og Romsdal, NOT Lofoten
]


def test_fixed_city_spellings_resolve_consistently() -> None:
    for spelling_a, spelling_b, expected in _FIXED_CITY_SPELLINGS:
        got_a = ra.region_for_city(spelling_a)
        got_b = ra.region_for_city(spelling_b)
        assert got_a == expected, f"{spelling_a!r} should resolve to {expected!r}, got {got_a!r}"
        assert got_b == expected, f"{spelling_b!r} should resolve to {expected!r}, got {got_b!r}"


def test_fixed_city_spellings_resolve_via_assess_leg() -> None:
    """Same invariant through the real product surface (assess_leg), not just
    the internal lookup -- both spellings, no iso2 (this bug only ever bit a
    caller that didn't/couldn't thread a country)."""
    for spelling_a, spelling_b, expected in _FIXED_CITY_SPELLINGS:
        sig_a = ra.assess_leg(city=spelling_a, checkin="2026-06-01", checkout="2026-06-05")
        sig_b = ra.assess_leg(city=spelling_b, checkin="2026-06-01", checkout="2026-06-05")
        assert sig_a["region"] == expected, f"{spelling_a!r}: {sig_a['region']!r} != {expected!r}"
        assert sig_b["region"] == expected, f"{spelling_b!r}: {sig_b['region']!r} != {expected!r}"


# ---------------------------------------------------------------------------
# 3. region_for_city's fail-safe: a known-ambiguous/known-mismatched bare-key
#    hit must return None (never the known-wrong value) when no iso2 (or an
#    iso2 the composite table doesn't cover) is given -- but MUST still
#    resolve correctly when iso2 IS given and covered.
# ---------------------------------------------------------------------------
def test_ambiguous_city_fails_safe_without_iso2() -> None:
    # 'victoria': Canada (ca-west) vs. Hong Kong (hk) vs. Seychelles vs. Malta --
    # a real cross-country homonym with NO single correct bare answer.
    assert ra.region_for_city("victoria") is None
    assert ra.region_for_city("victoria", iso2="CA") == "ca-west"
    assert ra.region_for_city("victoria", iso2="HK") == "hk"


def test_ambiguous_city_fails_safe_through_assess_leg() -> None:
    """The conservative UNKNOWN-region path (never a silent 'safe') must be
    what a caller sees end-to-end when it can't supply a country for a known
    cross-country homonym."""
    sig = ra.assess_leg(city="victoria", checkin="2026-06-01", checkout="2026-06-05")
    assert sig["region"] is None
    assert sig["decisions"]["flag"] is True, "UNKNOWN region must ALWAYS conservatively flag"


def test_san_pedro_ambiguous_but_composite_covers_both_countries() -> None:
    assert ra.region_for_city("san pedro") is None
    assert ra.region_for_city("san pedro", iso2="BZ") == "bz"
    assert ra.region_for_city("san pedro", iso2="CI") == "ci"
    assert ra.region_for_city("san-pedro", iso2="CI") == "ci"


if __name__ == "__main__":
    test_no_new_bare_key_duplicate_spelling_inconsistency()
    test_fixed_city_spellings_resolve_consistently()
    test_fixed_city_spellings_resolve_via_assess_leg()
    test_ambiguous_city_fails_safe_without_iso2()
    test_ambiguous_city_fails_safe_through_assess_leg()
    test_san_pedro_ambiguous_but_composite_covers_both_countries()
    print("ALL BARE-KEY CITY->REGION CONSISTENCY TESTS PASSED")
