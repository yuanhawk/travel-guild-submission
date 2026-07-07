"""
test_city_country_region_invariant.py — standing CI regression test for the
city->country region-mapping bug class (#236/#240, fixed in b2f26ce
"fix(risk/catalog): country-qualify city->region keying" + the 47b70bb
Salamanca follow-up).

THE BUG CLASS: risk_agent's city->region lookup used to be keyed on a bare,
un-country-qualified city name, so cross-country homonyms (hamilton NZ vs CA,
san jose CR vs US, cordoba ES vs AR, a 31-key Madrid/Barcelona cluster
mis-anchored to Mexico, etc.) silently inherited the WRONG country's
climate/seismic/advisory data. The fix added a composite (ISO2, city) lookup
(region_for_city(city, iso2=...)) consulted before the bare-key fallback.

THIS TEST mechanically re-derives the invariant that fix is supposed to
guarantee, straight from the data: for every distinct (city, country) pair
that actually appears in ucp-merchant/catalog.json + catalog_supplement.json
(the exact rows the product sells), resolving that city with its OWN catalog
country must never silently hand back a region belonging to some OTHER
country. A future catalog addition, a k-NN mis-anchor in
osm_city_regions.json, or a bad hand override would all be caught here without
anyone having to notice the wrong hazard data downstream.

CI-safe: no LLM, no network, no live agent app — pure data + region_for_city /
_region_iso2 (both pure functions). Loading both catalog files + iterating the
~7k distinct (city, country) pairs runs in well under a second.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents import risk_agent as ra

_UCP_MERCHANT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "ucp-merchant")
_CATALOG_PATH = os.path.join(_UCP_MERCHANT_DIR, "catalog.json")
_CATALOG_SUPPLEMENT_PATH = os.path.join(_UCP_MERCHANT_DIR, "catalog_supplement.json")
_ISO2_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "reference", "country_name_to_iso2.json")


def _load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _catalog_city_country_pairs() -> set[tuple[str, str]]:
    """Every distinct (city-lower, country-as-written-in-catalog) pair across
    BOTH catalog files. A city seeded under 2+ countries yields 2+ pairs here —
    exactly the homonym case the R1 fix (#236/#240) targeted."""
    pairs: set[tuple[str, str]] = set()
    for path in (_CATALOG_PATH, _CATALOG_SUPPLEMENT_PATH):
        for row in _load_json(path):
            city = str(row.get("city", "")).strip().lower()
            country = str(row.get("country", "")).strip()
            if city and country:
                pairs.add((city, country))
    return pairs


# ---------------------------------------------------------------------------
# Known-equivalent ISO2 groups: resolving into one member of a group when the
# catalog names another member of the SAME group is an intentional modeling
# choice, not a mis-route:
#   - UK constituent-nation names (England/Scotland/Wales/Northern Ireland) all
#     resolve to GB via reference/country_name_to_iso2.json, but risk_agent's
#     own region keys use a "uk-" prefix (e.g. "uk-south") rather than "gb-" —
#     same country, different prefix convention baked into the region keys.
#   - Hong Kong / Macau are seeded as their OWN hazard regions (hk / mo) even
#     where the catalog's country field says "China" (CN) — risk_agent
#     explicitly special-cases 'hong kong' -> 'hk' (see _CITY_TO_REGION.update
#     near the bottom of risk_agent.py); that is correct, deliberate behaviour.
# ---------------------------------------------------------------------------
_EQUIVALENT_ISO2_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"GB", "UK"}),
    frozenset({"CN", "HK", "MO"}),
)


def _iso2_equivalent(a: str, b: str) -> bool:
    if a == b:
        return True
    return any(a in group and b in group for group in _EQUIVALENT_ISO2_GROUPS)


# ---------------------------------------------------------------------------
# KNOWN RESIDUAL mismatches: pre-existing region mis-routes that THIS test
# discovered but which predate it and are not yet fixed (a different, still-
# open instance of the same #236/#240 bug class — not covered by the R1 /
# R1-followup hand overrides). Listed explicitly, not silently swept, so:
#   (a) this test is green today instead of blocking on unrelated pre-existing
#       data bugs it wasn't asked to fix,
#   (b) anyone reading this file sees exactly which (city, country) pairs are
#       still wrong, and to what,
#   (c) the check below only forgives the EXACT wrong region it names — if the
#       mis-route changes to some OTHER wrong country, or a brand-new city hits
#       this bug class, the test still fails. Fixing an entry here and
#       forgetting to delete it is harmless (an accidental correct fix still
#       passes); this list should shrink over time, not grow.
# Key: (city-as-lowered-in-catalog, catalog ISO2). Value: the WRONG ISO2
# region_for_city currently resolves to.
#
# R2 follow-up (#236/#240): the 13 entries that used to live here (BC-Canada
# cluster, Puerto Iguazú, Santa Maria, Madhyapur Thimi, Salem) were WRONG-
# COUNTRY composite mis-routes with an easy fix (reuse an existing region
# constant, same as the hamilton/san jose/cordoba/manzanillo hand overrides) —
# now hand-fixed directly in risk_agent.py's _CITY_REGION_BY_COUNTRY overrides,
# so they no longer belong in this forgiveness list. This dict is now empty;
# it stays in place (rather than being deleted) as the standing mechanism for
# any FUTURE residual this bug class turns up.
# ---------------------------------------------------------------------------
_KNOWN_RESIDUAL_MISMATCHES: dict[tuple[str, str], str] = {}


def test_catalog_city_country_region_invariant() -> None:
    """For every (city, country) pair in ucp-merchant/catalog.json +
    catalog_supplement.json, region_for_city(city, iso2=<that country's ISO2>)
    must either:
      - resolve to None (the conservative UNKNOWN fallback) -> PASS, or
      - resolve to a region whose own implied country (risk_agent's
        _region_iso2()) matches the catalog's country for that row, treating
        UK-nation-name and Hong Kong/Macau-vs-China as known-equivalent (see
        _EQUIVALENT_ISO2_GROUPS above).

    A resolved-but-WRONG-country region is the ONLY failure mode this test
    flags — exactly the #236/#240 bug class (b2f26ce/47b70bb)."""
    name_to_iso2 = _load_json(_ISO2_PATH)

    failures: list[str] = []
    for city, country in sorted(_catalog_city_country_pairs()):
        iso2 = name_to_iso2.get(country)
        if not iso2:
            # A country name the reference map can't resolve at all is a
            # DIFFERENT bug class (covered by
            # test_honesty_gate_coverage.py's country-resolution invariant);
            # this test can't determine an expected country for the row, so
            # it neither passes nor fails it here — just skips.
            continue
        iso2 = iso2.strip().upper()

        region = ra.region_for_city(city, iso2=iso2)
        if region is None:
            continue  # conservative UNKNOWN fallback — never a failure

        region_iso2 = ra._region_iso2(region)
        if _iso2_equivalent(region_iso2, iso2):
            continue

        if _KNOWN_RESIDUAL_MISMATCHES.get((city, iso2)) == region_iso2:
            continue  # pre-existing, tracked above — not a NEW regression

        failures.append(
            f"{city!r} (catalog country={country!r} -> {iso2}) resolved to "
            f"region={region!r} (implies {region_iso2}) — WRONG COUNTRY"
        )

    assert not failures, (
        f"{len(failures)} catalog (city, country) pairs resolve to a region "
        f"implying the WRONG country — the #236/#240 city->country "
        f"region-mapping bug class has regressed:\n" + "\n".join(failures)
    )
    print(
        f"[REGION-INVARIANT] PASS — every catalog (city, country) pair "
        f"resolves to either no region (conservative) or its own country's "
        f"region ({len(_KNOWN_RESIDUAL_MISMATCHES)} pre-existing residual "
        f"mis-routes tracked, not newly regressed)\n"
    )


def test_equivalence_classes_are_exercised() -> None:
    """Lock in the two known-equivalent-country carve-outs directly (not just
    via the blanket sweep above), so a future refactor of _iso2_equivalent
    can't silently stop covering the exact cases the task named."""
    # UK constituent nation vs risk_agent's 'uk-' region-key prefix.
    belfast_region = ra.region_for_city("belfast", iso2="GB")
    assert belfast_region is not None
    assert _iso2_equivalent(ra._region_iso2(belfast_region), "GB")

    # Hong Kong seeded as its own region even though the catalog's "hong kong"
    # city row is tagged country="China" (CN).
    hk_region = ra.region_for_city("hong kong", iso2="CN")
    assert hk_region is not None
    assert _iso2_equivalent(ra._region_iso2(hk_region), "CN")

    # Macau seeded as its own region under its own catalog country ("Macao").
    macau_region = ra.region_for_city("macau", iso2="MO")
    assert macau_region is not None
    assert _iso2_equivalent(ra._region_iso2(macau_region), "MO")

    print("[REGION-INVARIANT] PASS — UK-nation and Hong Kong/Macau-vs-China "
          "equivalence carve-outs verified directly\n")


if __name__ == "__main__":
    test_catalog_city_country_region_invariant()
    test_equivalence_classes_are_exercised()
    print("ALL CITY->COUNTRY REGION-MAPPING INVARIANT TESTS PASSED")
