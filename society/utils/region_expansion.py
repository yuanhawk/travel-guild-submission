"""
region_expansion.py — turn a REGIONAL request ("central japan", "eastern europe",
"tuscany", "northern vietnam") into a QUALIFIED, route-ordered set of bookable cities.

The free-text parser maps a region phrase to at most ONE gateway city, so a 2-week
"central japan" trip collapsed to a single city. This module expands a recognised
region into several real, bookable destinations.

DESIGN (curated registry — chosen with the user, 2026-06-26):
  - Each region phrase → a curated, ROUTE-ORDERED list of ANCHOR cities: the iconic
    must-visits, every one VERIFIED to exist in the booking catalog (honest — we never
    surface a city we can't book). Multi-country regions span their member countries
    (e.g. eastern europe = Prague/Kraków/Budapest/Vienna/...) so the trip isn't all one
    country. Data-richness/auto-ranking was rejected: the catalog's per-city lodging
    count is capped/near-uniform, so it can't rank Hanoi above Bac Ninh.
  - COUNT by trip length + PACE: ~3 nights/city for a leisure trip (default), 2 for an
    adventurous "see more places" request, 1 for an explicit stopover/whirlwind. The
    richest anchors lead, so a short trip still gets the headline cities.

Pure + deterministic: no LLM, no network, no clock/random. Returns a list of catalog
city slugs (lowercase) or [] when the phrase is not a recognised region. Anchors are
curated in a geographically sensible order (the transport agent re-sequences anyway).
"""

from __future__ import annotations

import os
import re

MAX_REGION_LEGS = 6   # never expand a single region past this
MIN_ANCHORS = 3       # import-time guard: every region must ship >= 3 verified anchors


# ---------------------------------------------------------------------------
# Curated registry. `anchors`: route-ordered, catalog-VERIFIED iconic cities.
# `iso2`/`countries`: provenance + scope (sub-national vs multi-country). Phrases are
# lowercase, matched whole-word; longest phrase wins. Cities that are not in the
# booking catalog were dropped at curation time (honest — see the region_expansion test).
# ---------------------------------------------------------------------------
_REGIONS: dict[str, dict] = {
    # ---- sub-national ----
    # fix-round-1: "northeast india" (the Seven Sisters — Assam/Meghalaya/etc.) is a
    # distinct region hundreds of km from Delhi; the generic directional N/S/E/W-only
    # registry (region_directional.json) has no intercardinal entry, so both "northeast
    # india" and "north east india" silently fell through to the country->gateway
    # (Delhi) or the wrong generic "east india" bucket. Both space/no-space forms map
    # to the SAME real, catalog-verified anchors so the wrong-region resolve is closed.
    "northeast india":   {"iso2": "IN", "anchors": ["guwahati", "shillong", "imphal", "agartala", "gangtok", "dibrugarh"]},
    "north east india":  {"iso2": "IN", "anchors": ["guwahati", "shillong", "imphal", "agartala", "gangtok", "dibrugarh"]},
    "northern vietnam":  {"iso2": "VN", "anchors": ["hanoi", "ha long", "haiphong", "lao cai"]},
    "central vietnam":   {"iso2": "VN", "anchors": ["da nang", "hoi an", "hue", "nha trang"]},
    "central japan":     {"iso2": "JP", "anchors": ["nagoya", "gifu", "kanazawa", "matsumoto", "nagano"]},
    "western japan":     {"iso2": "JP", "anchors": ["osaka", "kyoto", "nara", "kobe", "okayama", "hiroshima"]},
    "kansai":            {"iso2": "JP", "anchors": ["osaka", "kyoto", "nara", "kobe"]},
    "hokkaido":          {"iso2": "JP", "anchors": ["sapporo", "otaru", "hakodate", "asahikawa", "obihiro", "kushiro"]},
    "kyushu":            {"iso2": "JP", "anchors": ["fukuoka", "beppu", "kumamoto", "nagasaki", "kagoshima", "miyazaki"]},
    "tohoku":            {"iso2": "JP", "anchors": ["sendai", "aomori", "morioka", "akita", "hirosaki", "yamagata"]},
    "tuscany":           {"iso2": "IT", "anchors": ["florence", "siena", "arezzo", "pisa", "livorno"]},
    "sicily":            {"iso2": "IT", "anchors": ["palermo", "catania", "messina", "agrigento"]},
    "andalusia":         {"iso2": "ES", "anchors": ["seville", "cordoba", "granada", "marbella"]},
    "andalucia":         {"iso2": "ES", "anchors": ["seville", "cordoba", "granada", "marbella"]},
    "catalonia":         {"iso2": "ES", "anchors": ["barcelona", "girona", "figueres", "tarragona"]},
    "bavaria":           {"iso2": "DE", "anchors": ["munich", "regensburg", "nuremberg", "wurzburg", "augsburg"]},
    "provence":          {"iso2": "FR", "anchors": ["marseille", "aix-en-provence", "nice"]},

    # ---- multi-country (anchors span the member countries) ----
    "eastern europe": {"countries": ["PL", "CZ", "HU", "AT", "SK", "SI"],
                       "anchors": ["prague", "vienna", "bratislava", "budapest", "krakow", "ljubljana"]},
    "central europe": {"countries": ["DE", "AT", "CZ", "PL", "HU", "SK"],
                       "anchors": ["prague", "vienna", "munich", "budapest", "krakow", "bratislava"]},
    "the balkans":    {"countries": ["RS", "HR", "BA", "ME", "MK", "BG", "GR"],
                       "anchors": ["dubrovnik", "split", "sarajevo", "kotor", "belgrade", "skopje", "sofia"]},
    "balkans":        {"countries": ["RS", "HR", "BA", "ME", "MK", "BG", "GR"],
                       "anchors": ["dubrovnik", "split", "sarajevo", "kotor", "belgrade", "skopje", "sofia"]},
    "scandinavia":    {"countries": ["SE", "NO", "DK"],
                       "anchors": ["copenhagen", "oslo", "bergen", "gothenburg", "stockholm"]},
    # RC-3: "nordic"/"the nordics" extends Scandinavia to include Finland + Iceland,
    # matching the traveller's broader conception of the Nordic region.
    # All anchors verified bookable in catalog (2026-06-29 audit).
    "nordic":         {"countries": ["NO", "SE", "DK", "FI", "IS"],
                       "anchors": ["oslo", "stockholm", "copenhagen", "helsinki", "reykjavik"]},
    "the nordics":    {"countries": ["NO", "SE", "DK", "FI", "IS"],
                       "anchors": ["oslo", "stockholm", "copenhagen", "helsinki", "reykjavik"]},
    "the baltics":    {"countries": ["EE", "LV", "LT"], "anchors": ["tallinn", "riga", "vilnius"]},
    "baltics":        {"countries": ["EE", "LV", "LT"], "anchors": ["tallinn", "riga", "vilnius"]},
    "iberia":         {"countries": ["ES", "PT"], "anchors": ["lisbon", "porto", "madrid", "seville", "barcelona"]},
    "central america":{"countries": ["GT", "CR", "PA", "NI"],
                       "anchors": ["guatemala city", "san jose", "panama city", "leon"]},
    "southeast asia": {"countries": ["TH", "VN", "KH", "SG", "MY", "ID"],
                       "anchors": ["bangkok", "siem reap", "ho chi minh city", "singapore", "kuala lumpur", "bali"]},
}

# Merge the generated directional + multi-country registry (reference/region_directional.json):
# auto-derived N/S/E/W directional anchors for ~35 big countries (median lat/lon bands, ranked by
# population, catalog-filtered) + hand-overridden marquee directions + extra European multi-country
# groupings + short-form aliases ("north india"). Inline hand-curated named regions (above) WIN on
# any key collision. Every anchor in the file is catalog-bookable (filtered at generation time).
try:
    import json as _json
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "..", "reference", "region_directional.json"), encoding="utf-8") as _f:
        _REGIONS = {**_json.load(_f), **_REGIONS}   # inline (hand-curated) takes precedence
except Exception:
    pass


# ---------------------------------------------------------------------------
# Pace -> nights per city.
# ---------------------------------------------------------------------------
_FAST_CUES = re.compile(
    r"\b(whirlwind|fast[- ]?paced?|see (?:as much|everything|it all)|backpack\w*|"
    r"as many (?:cities|places|spots)|jam[- ]?packed|hop\w* around|on the go)\b", re.I)
_STOPOVER = re.compile(r"\b((?:1|one)[- ]night|stop[- ]?overs?|stopover)\b", re.I)


def nights_per_city(text: str) -> int:
    """3 nights/city for leisure (default); 2 for an adventurous 'see more places'
    request; 1 for an explicit stopover/whirlwind. Deterministic on the text."""
    t = text or ""
    if _STOPOVER.search(t):
        return 1
    if _FAST_CUES.search(t):
        return 2
    return 3


# ---------------------------------------------------------------------------
# Region detection + expansion.
# ---------------------------------------------------------------------------
_REGION_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in sorted(_REGIONS, key=lambda s: (-len(s), s))) + r")\b", re.I)


def scan_region(text: str) -> str | None:
    """Return the first (longest) recognised region phrase in *text*, else None."""
    low = (text or "").lower()
    m = _REGION_RE.search(low)
    if not m:
        return None
    # "iberia" the AIRLINE is not the region — don't hijack "Iberia airlines to Madrid" to Portugal.
    if m.group(0) == "iberia" and re.search(r"\biberia\s+(?:air|airline|airways|flight|express)", low):
        m2 = _REGION_RE.search(low, m.end())
        return m2.group(0) if m2 else None
    return m.group(0)


def region_cities(phrase: str) -> list[str]:
    """All curated anchor cities for a recognised region phrase (route order), else []."""
    r = _REGIONS.get((phrase or "").lower())
    return list(r["anchors"]) if r else []


def expand_region(text: str, nights: int, max_legs: int = MAX_REGION_LEGS) -> list[str]:
    """If *text* names a recognised region, return a route-ordered list of bookable
    anchor cities sized to the trip length + pace (headline cities first); else []."""
    phrase = scan_region(text)
    if not phrase:
        return []
    anchors = region_cities(phrase)
    if not anchors:
        return []
    npc = nights_per_city(text)
    if nights and nights > 0:
        k = int(round(nights / npc))
    else:
        k = 4   # nights unknown (date-only / unstated) → a sensible default region trip
    k = max(2, min(k, max_legs, MAX_REGION_LEGS, len(anchors)))   # >=2 so a region never collapses to 1
    return anchors[:k]


def _assert_regions_bookable() -> None:
    """Import-time guard: every region must ship >= MIN_ANCHORS catalog-bookable anchors.
    Loads the merchant catalog; never raises on a missing catalog (CI/segregated runs)."""
    try:
        import json
        cat = {(h.get("city") or "").lower() for h in json.load(
            open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "..", "ucp-merchant", "catalog.json"), encoding="utf-8")) if h.get("city")}
    except Exception:
        return  # catalog not available here — skip (the dedicated test enforces it)
    bad = {}
    for phrase, r in _REGIONS.items():
        missing = [c for c in r["anchors"] if c not in cat]
        if missing or len(r["anchors"]) < MIN_ANCHORS:
            bad[phrase] = missing or f"only {len(r['anchors'])} anchors"
    if bad:
        raise AssertionError(f"region_expansion: regions with unbookable/too-few anchors: {bad}")
