"""
day_planner_agent.py — Per-leg day-by-day activity & meal planner (Travel Guild, build #30).

Design contract: AGENT-SOCIETY-A2A-DESIGN.md §3.x (activity planning) + the
§AGENT-EXTENSION PATTERN at the bottom of society/insurance_agent.py.

Agent Card skill: ``activity.plan``

DETERMINISTIC — no LLM, no DashScope, no live POI APIs, NO wall-clock / random /
set-iteration on any output path. Every ranking uses explicit integer-or-string
sort keys with deterministic tiebreaks (var-0 byte-identical output).

HONESTY / fail-conservative: this agent NEVER fabricates POIs, opening hours, or
prices. It surfaces ONLY what the committed poi_catalog.json (an OSM presence-only
harvest cache) actually contains. An unknown city degrades to a conservative empty
plan with an explanatory note. Every surfaced attraction / meal carries a
provenance string. Missing fields pass through as null — never invented.

Input (data part, JSON):
    Wrapped dict form (canonical):
        {"legs": [
            {"leg_id": str, "city": str, "iso2": str, "country": str,
             "checkin": str, "checkout": str,
             "interests": [str]?, "dietary": [str]?, "pace": str?,
             "bad_weather_days": [int]?},
            ...
        ]}
    Also tolerated: a bare list of leg dicts (like transport_agent does).

Output artifact (data part, JSON):
    {"leg_plans": [DayPlanResult, ...]}

DayPlanResult shape (one per input leg):
    {
        "leg_id":                  str,
        "city":                    str,
        "iso2":                    str,
        "country":                 str,
        "catalog_hit":             bool,        # false → conservative miss
        "num_days":                int,
        "days": [                               # one entry per trip day
            {
                "day_index":   int,
                "bad_weather": bool,
                "attractions": [Attraction, ...],
                "meals": {
                    "breakfast": Meal | null,
                    "lunch":     Meal | null,
                    "tea":       Meal | null,
                    "dinner":    Meal | null,
                    "supper":    Meal | null,
                },
            },
            ...
        ],
        "unscheduled_attractions": [Attraction, ...],
        "provenance":              str,          # catalog-level provenance
        "notes":                   [str, ...],   # honest warnings
    }

Runnable service: HOST / PORT env, defaults 0.0.0.0:9110.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import unicodedata
from datetime import date, timedelta
from typing import Any

import uvicorn

from agents.a2a_agent import (
    A2AAgent,
    _data_part,
    _new_artifact,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provenance — the catalog is an OSM presence-only harvest cache. Stamped on
# every surfaced attraction / meal so a consumer is never misled into treating a
# harvested tag as a live / bookable fact. Dated from the catalog meta, but kept
# as a frozen constant string so the agent never reads a wall-clock.
# ---------------------------------------------------------------------------
_PROVENANCE = (
    "OSM poistore harvest 2026-06-20 (presence-only; verify hours/closures at booking)"
)

# Pace → max attractions per day. "relaxed"→2, "moderate"/default→3, "packed"→4.
_PACE_CAP = {"relaxed": 2, "moderate": 3, "packed": 4}
_DEFAULT_PACE_CAP = 3

# Travel-day reservation: usable sightseeing minutes in a full day (10 h baseline,
# a constant — NEVER a clock read), and which day index gets trimmed (always day 0,
# the ARRIVAL day; the first/origin leg is never trimmed via arrival_transport_minutes=0).
_USABLE_DAY_MINUTES = 600
_TRIM_DAY_INDEX = 0

# Weather-exposure ordering for a bad-weather day reorder (indoor first).
_EXPOSURE_RANK = {"indoor": 0, "mixed": 1, "outdoor": 2}

# Meal slots, in canonical (deterministic) order.
_MEAL_SLOTS = ("breakfast", "lunch", "tea", "dinner", "supper")

# Cuisine tokens that qualify a restaurant for the breakfast slot.
_BREAKFAST_CUISINES = frozenset({"coffee", "coffee_shop", "breakfast", "bakery"})
# Cuisine tokens that qualify a restaurant for the tea slot.
_TEA_CUISINES = frozenset({"coffee", "tea", "dessert", "cafe"})
# Substrings in opening_hours that signal a late-opening venue (supper).
_LATE_HOUR_TOKENS = ("22:", "23:", "24/7")

# #meal-quality-fix: global fast-food / chain-cafe brands that dominated dinners in the
# eval (KFC/McDonald's/Burger King/Domino's/Starbucks/Shake Shack ...). These are NOT
# what a traveller wants as their signature meal abroad, and they crowd out local
# venues because their catalog rows are unusually complete (website/hours/takeaway).
# We DE-WEIGHT them in meal selection (sink below independent venues) and treat them as
# chains for the per-leg ≤2 cap even when the city has only a single branch node — so a
# lone McDonald's no longer wins every dinner. Matched on the (lower-cased) venue name;
# there is no brand field in the data. NOT a filter: if a city has ONLY chains they can
# still fill slots (honest, never fabricated).
_GLOBAL_FASTFOOD_BRANDS = frozenset({
    "mcdonald's", "mcdonalds", "burger king", "kfc", "domino's", "dominos",
    "domino's pizza", "pizza hut", "starbucks", "subway", "shake shack",
    "wendy's", "wendys", "taco bell", "dunkin'", "dunkin", "dunkin' donuts",
    "popeyes", "chick-fil-a", "five guys", "in-n-out", "in-n-out burger",
    "papa john's", "papa johns", "little caesars", "hardee's", "carl's jr",
    "jollibee", "mos burger", "lotteria", "yoshinoya", "sukiya", "matsuya",
    "costa coffee", "tim hortons", "krispy kreme", "wingstop", "arby's",
    # #mealchain-fix: third-wave/specialty coffee CHAINS have the same dominance
    # problem as the classic fast-food brands above (unusually complete catalog
    # rows: website/hours/wifi tags) but were never caught by this list because
    # they read as "independent local cafe" by name alone.
    "blue bottle coffee", "%arabica", "verve coffee",
})


def _is_global_fastfood(restaurant: dict) -> bool:
    """True iff the venue's name is a known global fast-food / chain-cafe brand.

    Deterministic, name-based (no brand field exists). Strips a trailing branch tag
    ("McDonald's Shibuya" -> "mcdonald's") so branch nodes still match the brand."""
    name = _lower(restaurant.get("name_en") or restaurant.get("name") or "").strip()
    if not name:
        return False
    if name in _GLOBAL_FASTFOOD_BRANDS:
        return True
    # Branch suffix / prefix tolerance: any brand appearing as a whole-word token wins
    # ("KFC Siam", "Starbucks Reserve"). Uses the same word-boundary matcher as interests.
    return any(_token_hit(brand, name) for brand in _GLOBAL_FASTFOOD_BRANDS
               if " " not in brand and "'" not in brand and "-" not in brand) or \
        any(name.startswith(brand + " ") or name == brand for brand in _GLOBAL_FASTFOOD_BRANDS)


# ---------------------------------------------------------------------------
# Catalog loader — load ONCE at module import. Degrade conservative (empty dict)
# on ANY load/shape error so the agent never crashes the society at import time.
# ---------------------------------------------------------------------------

def _load_poi_catalog() -> dict[str, dict]:
    """Load society/poi_catalog.json once and return its ``cities`` mapping.

    Returns {} (degrade conservative, never crash) on any of:
    FileNotFoundError, ValueError (bad JSON), KeyError, TypeError. An empty
    catalog makes every city a conservative miss (catalog_hit=false)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "poi_catalog.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        cities = data.get("cities", {})
        if not isinstance(cities, dict):
            return {}
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return {}
    # #63 — merge the Gemini-grounded POI supplement (real attractions/restaurants for thin/missing
    # cities). Same ISO2:city keys. ADD a missing city; for an existing one, APPEND new-by-name items
    # so a thin/chain-heavy city (e.g. Tokyo cafés) gains variety. Provenance-tagged; never crashes.
    try:
        spath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "poi_supplement.json")
        with open(spath, encoding="utf-8") as fh:
            supp = json.load(fh)
        for key, blob in (supp.items() if isinstance(supp, dict) else []):
            if not isinstance(blob, dict) or not (blob.get("attractions") or blob.get("restaurants")):
                continue  # skip non-city blocks (e.g. a "meta" entry) — only real POI city entries
            if key not in cities:
                cities[key] = blob
                continue
            for field in ("attractions", "restaurants"):
                have = {(x.get("name_en") or x.get("name") or "").lower().strip()
                        for x in cities[key].get(field, [])}
                for item in blob.get(field, []) or []:
                    nm = (item.get("name_en") or item.get("name") or "").lower().strip()
                    if nm and nm not in have:
                        cities[key].setdefault(field, []).append(item)
                        have.add(nm)
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        pass
    return cities


_POI_CATALOG: dict[str, dict] = _load_poi_catalog()


# ---------------------------------------------------------------------------
# Pure-core helpers (no self, no wall-clock, no random)
# ---------------------------------------------------------------------------

def _lower(s: Any) -> str:
    """Lower-cased, stripped string view of any value (None → '')."""
    return str(s).strip().lower() if s is not None else ""


def _catalog_key(iso2: str, city: str) -> str:
    """Catalog lookup key: ``ISO2:city`` (upper iso2, lower city)."""
    return f"{(iso2 or '').strip().upper()}:{(city or '').strip().lower()}"


# #70: colloquial/state name -> POI-catalog city (mirrors the merchant cityCanonical).
_POI_CITY_ALIASES = {"penang": "george town"}


def _resolve_catalog_entry(iso2: str, city: str) -> tuple[dict | None, str]:
    """Tolerant POI lookup: bridge the 'X' vs 'X city' naming (cebu -> 'cebu city') + colloquial
    aliases, mirroring the merchant catalog's cityMatches so a city that BOOKS a hotel also gets
    a day-plan (the city-normalization gap, #70). Tries exact -> alias -> '+ city' -> '- city';
    returns (entry, key_used), or (None, exact_key) on a genuine miss (honest empty plan)."""
    exact = _catalog_key(iso2, city)
    if exact in _POI_CATALOG:
        return _POI_CATALOG[exact], exact
    c = (city or "").strip().lower()
    cands: list[str] = []
    if c in _POI_CITY_ALIASES:
        cands.append(_POI_CITY_ALIASES[c])
    if c and not c.endswith(" city"):
        cands.append(f"{c} city")          # cebu -> cebu city
    elif c.endswith(" city"):
        cands.append(c[: -len(" city")])   # "x city" -> x (reverse direction)
    # JP admin suffixes: the seed keys some Japanese cities by their worldcities name — "nara-shi"
    # (市=city), "...-ku" (区=ward), "...-machi"/"-cho" (町=town) — while the itinerary uses the bare
    # "nara". Bridge both directions; the ISO2 in the key keeps this from ever crossing countries.
    _jp_suf = ("-shi", "-ku", "-machi", "-cho", "-gun", "-mura", " shi")
    _matched = next((s for s in _jp_suf if c.endswith(s)), None)
    if _matched:
        cands.append(c[: -len(_matched)].rstrip(" -"))   # nara-shi -> nara
    elif c:
        cands.append(f"{c}-shi")                          # nara -> nara-shi (the city form)
    for cand in cands:
        k = _catalog_key(iso2, cand)
        if k in _POI_CATALOG:
            return _POI_CATALOG[k], k
    return None, exact


def _attraction_name_lower(a: dict) -> str:
    """Lower-cased English name (or local name) for stable string tiebreaks."""
    return _lower(a.get("name_en") or a.get("name") or "")


# #translation-fix: characters from scripts an English reader cannot read as a label
# (Arabic, Devanagari, Thai, Hangul, Hiragana/Katakana, CJK ...). Used to decide whether
# a surfaced venue label is still raw local script (so the UI can flag it) — NOT to strip
# or fabricate anything.
#
# #233 root cause C -> #234 -> #235 -> #236 each patched this as an ENUMERATED deny-list
# (unlike the allow-list detectors in booking_links.py / itinerary.ts), and each round
# found the SAME bug class again: a deny-list is guaranteed to omit any block not
# explicitly enumerated. #237 audited it once more and found 13 MORE real, live-catalog
# scripts still falling through (Balinese, Javanese, Meetei Mayek, Tai Tham/Lanna, New Tai
# Lue, Coptic, Cham, Ol Chiki, Sundanese, Runic, Arabic Presentation Forms A/B, CJK
# Ext-B+/supplementary plane) -- structural proof the enumerated approach can never be
# complete. Root-caused this time by switching to the same ALLOW-LIST architecture already
# used by _has_non_latin_script in booking_links.py / hasNonLatinScript in itinerary.ts:
# flag anything OUTSIDE Latin + common punctuation, rather than enumerating every non-Latin
# block that exists. Kept in sync with _LATIN_ALLOWED_RE in booking_links.py / itinerary.ts
# -- mirror any change there here too.
_LATIN_ALLOWED_RE = re.compile(
    "^[\u0020-\u02FF\u0300-\u036F\u1E00-\u1EFF\u2013-\u2014\u2018-\u201F\u2026\\s\\-'.,()&/0-9]*$"
)


def _has_nonlatin(text: str | None) -> bool:
    """True iff *text* contains a codepoint outside the Latin-script allow-list.

    ``text`` is NFC-normalized FIRST: real catalog data is not guaranteed to arrive
    precomposed (server.py NFC-normalizes ``title`` for its own Vietnamese matching, and
    the seed pipeline strips combining marks via NFKD elsewhere), so an NFD Vietnamese name
    -- base vowel + horn (U+031B) + tone mark (U+0300 etc, Combining Diacritical Marks) --
    must not be misclassified as non-Latin just because it wasn't composed yet (#233 root
    cause A).
    """
    if not text:
        return False
    return not _LATIN_ALLOWED_RE.match(unicodedata.normalize("NFC", text))


def _display_name(item: dict) -> str:
    """The label an English user should see for a POI/venue.

    Prefers the English/transliterated ``name_en`` when the catalog has it (e.g. Thai
    "ถนนเยาวราช" → "Yaowarat Road"); only falls back to the raw local ``name`` when NO
    English form exists (honest last resort — flagged via ``name_needs_translation`` at
    stamp time so the raw script is never silently presented as if it were English)."""
    en = (item.get("name_en") or "").strip()
    if en:
        return en
    return (item.get("name") or "").strip()


def _token_hit(token: str, hay: str) -> bool:
    """Word-boundary match of *token* in *hay* (both already lower-cased). Treats any non
    [a-z0-9] char — including '_' and ' ' in OSM categories — as a separator, so 'dive' does NOT
    match 'diversão', 'reef' not 'shareef', 'cherry' not 'mattancherry', 'market' not
    'supermarket'. Honest matching: a token only hits a genuinely-named POI/category, never a
    coincidental substring (the audit's never-mislead fix)."""
    if not token or not hay:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", hay) is not None


def _interest_score(attraction: dict, interests: list[str]) -> int:
    """Integer interest score for an attraction against sorted interest tokens.

    +2 if the token appears in the category, +1 if it appears in the
    English/local name. Tokens are matched as substrings (lower-cased). Summed
    over sorted(interests) so the result is order-independent → var-0."""
    if not interests:
        return 0
    cat = _lower(attraction.get("category"))
    name = _attraction_name_lower(attraction)
    score = 0
    for token in sorted(interests):
        t = _lower(token)
        if not t:
            continue
        if _token_hit(t, cat):
            score += 2
        if _token_hit(t, name):
            score += 1
    return score


def _completeness_index(restaurant: dict) -> int:
    """Lower = MORE complete (used directly as an ascending sort key).

    Restaurants have NO OSM notability signal, so we rank by DATA-COMPLETENESS
    (honest: this is NOT a quality ranking). Count present signal fields; negate
    so that a more-complete record sorts FIRST under an ascending key."""
    signals = ("cuisine", "website", "opening_hours", "diet", "wheelchair",
               "category", "outdoor_seating", "takeaway", "delivery")
    present = 0
    for f in signals:
        v = restaurant.get(f)
        if v:  # non-empty / truthy (empty list / "" / None do not count)
            present += 1
    return -present  # ascending key → most-complete first


def _slot_fit_score(restaurant: dict, slot: str) -> int:
    """Integer slot-fit score (higher = better) for a restaurant in a slot.

    breakfast / tea have explicit qualifying signals; lunch/dinner/supper fit
    anything (score 0). supper additionally PREFERS a late-open venue. The score
    is used as ``-score`` in the selection key so higher fit sorts first."""
    cat = _lower(restaurant.get("category"))
    cuisine = _lower(restaurant.get("cuisine"))
    cuisine_tokens = frozenset(t for t in cuisine.replace(",", ";").split(";") if t)
    if slot == "breakfast":
        if restaurant.get("breakfast"):
            return 2
        if cat == "cafe" or (cuisine_tokens & _BREAKFAST_CUISINES):
            return 1
        return 0
    if slot == "tea":
        if cat == "cafe" or (cuisine_tokens & _TEA_CUISINES):
            return 1
        return 0
    # #mealchain-fix: a cafe was previously tied at 0 with every real restaurant
    # for lunch/dinner/supper (no penalty at all), so it could win a dinner slot
    # purely on catalog-completeness/dedup tiebreaks — a cafe scoring identically
    # to a real restaurant for dinner. Mirror the breakfast/tea BONUS above but
    # INVERTED: a cafe now scores NEGATIVE for these three slots, so a real
    # restaurant always outranks a comparable cafe when both are candidates.
    cafe_penalty = -1 if cat == "cafe" else 0
    if slot == "supper":
        # supper fits anything, but PREFERS a late-open / always-on / off-prem venue.
        hours = _lower(restaurant.get("opening_hours"))
        late = any(tok in hours for tok in _LATE_HOUR_TOKENS)
        bonus = 1 if (late or restaurant.get("takeaway") or restaurant.get("delivery")) else 0
        return cafe_penalty + bonus
    # lunch / dinner — any venue fits, except a cafe is de-weighted (see above).
    return cafe_penalty


_DINING_TIER_RANK: dict[str, dict[str, int]] = {
    "authentic": {"restaurant": 2, "food_court": -1, "fast_food": -2},
    "cheap":     {"fast_food": 2, "food_court": 2, "cafe": 1, "restaurant": -1},
    "family":    {"bar": -2, "pub": -2},   # adult/alcohol-primary venues sink
}


def _dining_tier_rank(restaurant: dict, tier: str | None) -> int:
    """Deterministic SECONDARY reorder signal (never a filter): persona dining_tier
    ranks a restaurant's `category` up/down. Absent tier -> 0 for every restaurant
    -> no-op (var-0). Slot-fit remains the PRIMARY sort key wherever this is used."""
    if not tier:
        return 0
    return _DINING_TIER_RANK.get(_lower(tier), {}).get(_lower(restaurant.get("category")), 0)


def _supper_qualifies_late(restaurant: dict) -> bool:
    """True iff the restaurant is a genuinely late-open / always-on / off-premise
    venue. Used to decide whether supper can be filled at all (never invent one)."""
    hours = _lower(restaurant.get("opening_hours"))
    if any(tok in hours for tok in _LATE_HOUR_TOKENS):
        return True
    return bool(restaurant.get("takeaway") or restaurant.get("delivery"))


def _stamp_attraction(attraction: dict) -> dict:
    """Return a surfaced attraction dict: pass through known fields (missing → null)
    and stamp provenance. NEVER fabricates a missing field."""
    fields = (
        "name", "name_en", "category", "wikidata", "wikipedia", "heritage",
        "weather_exposure", "opening_hours", "fee", "wheelchair", "website",
        "image", "addr_housenumber", "addr_street", "addr_postcode",
        "addr_city", "lat", "lon",
    )
    out: dict[str, Any] = {f: attraction.get(f, None) for f in fields}
    out["provenance"] = attraction.get("provenance") or _PROVENANCE  # honest: keep Gemini tag if seeded
    # #translation-fix: surface an English/transliterated label (name_en) as the primary
    # display name; only fall back to raw local script when no English form exists, and
    # flag that case so the UI never presents Thai/CJK as if it were an English label.
    out["display_name"] = _display_name(attraction)
    if _has_nonlatin(out["display_name"]):
        out["name_needs_translation"] = True
    return out


def _stamp_meal(restaurant: dict, note: str | None = None) -> dict:
    """Return a surfaced meal dict: pass through known restaurant fields (missing →
    null) and stamp provenance. NEVER fabricates a missing field."""
    fields = (
        "name", "name_en", "category", "cuisine", "diet", "outdoor_seating",
        "takeaway", "delivery", "breakfast", "opening_hours", "wheelchair",
        "website", "addr_housenumber", "addr_street", "addr_postcode",
        "addr_city", "lat", "lon",
    )
    out: dict[str, Any] = {f: restaurant.get(f, None) for f in fields}
    out["provenance"] = restaurant.get("provenance") or _PROVENANCE  # honest: keep Gemini tag if seeded
    # #translation-fix: English/transliterated primary label (see _stamp_attraction).
    out["display_name"] = _display_name(restaurant)
    if _has_nonlatin(out["display_name"]):
        out["name_needs_translation"] = True
    if note is not None:
        out["note"] = note
    return out


# ---------------------------------------------------------------------------
# Notability ranking + junk filter — must-see attraction selection quality
#
# ROOT CAUSE this addresses: the committed poi_catalog.json attraction lists are
# NOT notability-ranked. The seed's only notability signal is a binary "has a
# wikidata tag?" (osm_poi_ingest.py rank_key) — but virtually EVERY named OSM node
# in a major city carries wikidata, so that bit is constant and the sort collapses
# to ALPHABETICAL by name, then the top-30 are kept. Result: Paris surfaces
# "(sans titre)" / "Anne de Bretagne" (statues, A–C) and never the Eiffel Tower.
# The Gemini-grounded must-see supplement (poi_supplement.json — real icons:
# Eiffel Tower, Louvre, Colosseum, Trevi) IS merged in at load, but it is APPENDED
# AFTER the alphabetical OSM nodes, and the old rank key ordered purely by that
# insertion index — so the icons sank below the junk and were never scheduled.
#
# FIX (deterministic, presence-only — NO live Places call; Places rating/review
# counts are NOT available at selection time, only later at /place_card). We rank
# by a cheap OSM/curation notability score and FILTER ornamental non-sightseeing
# nodes. Signals, in strength order: curated supplement > wikipedia article >
# heritage listing > recognised sightseeing category > wikidata (weak — nearly
# ubiquitous, so it never rescues an otherwise-junk node on its own).
# ---------------------------------------------------------------------------

# OSM subtypes (the value after '=') that are ornamental / non-sightseeing micro-nodes:
# they dominate an alphabetical dump but are never a city's must-see. Dropped UNLESS the
# node carries an independent notability signal (curated / wikipedia / heritage).
_JUNK_ATTRACTION_TYPES = frozenset({
    "artwork",   # tourism=artwork — statues, busts, "(sans titre)"
    "memorial",  # historic=memorial — plaques, steles
    "tomb",      # historic=tomb
    "cinema",    # amenity=cinema
    "office",    # amenity=office / office=*
})
# Name substrings that mark an un-presentable node regardless of type/signal.
_JUNK_NAME_TOKENS = ("(sans titre)", "sans titre", "untitled", "senza titolo", "ohne titel")
# Category keywords that mark a recognised sightseeing type (OSM `tourism=museum` and
# free-text Gemini `Museum`/`Landmark/...` both match by substring).
_HIGH_VALUE_TOKENS = (
    "attraction", "museum", "gallery", "landmark", "monument", "castle", "palace",
    "cathedral", "basilica", "temple", "shrine", "archaeolog", "ruins", "fort",
    "monastery", "theme_park", "theme park", "zoo", "aquarium", "viewpoint",
    "tower", "market", "observation",
    # round-2 #poi-clustering-fix: many major shrines/temples/churches/mosques (e.g.
    # Meiji Jingu) are tagged with the GENERIC OSM `amenity=place_of_worship` rather
    # than a specific shrine/temple/cathedral subtag, so they fell through this
    # category bonus entirely. A place of worship is a legitimate, common sightseeing
    # category worldwide (consistent with "temple"/"shrine"/"cathedral" already here).
    "place_of_worship",
)


def _osm_subtype(category: Any) -> str:
    """Value after '=' in an OSM category ('tourism=artwork' -> 'artwork'), lower-cased.
    Free-text (Gemini) categories like 'Landmark' pass through whole."""
    c = _lower(category)
    return c.split("=", 1)[1] if "=" in c else c


def _is_curated(a: dict) -> bool:
    """True iff the attraction came from the Gemini-grounded must-see supplement
    (provenance stamped at load). These are hand-picked city icons."""
    return _lower(a.get("provenance")).startswith("gemini")


def _has_notability_signal(a: dict) -> bool:
    """A signal strong enough to rescue an otherwise-ornamental node: curated must-see,
    a Wikipedia article, or a heritage listing. `wikidata` ALONE is deliberately excluded
    — nearly every named OSM node has one (that is exactly why the raw rank was junk)."""
    return bool(_is_curated(a) or a.get("wikipedia") or a.get("heritage"))


def _is_junk_attraction(a: dict) -> bool:
    """True iff the attraction must be dropped from a day plan: an untitled/no-name node,
    OR an ornamental / non-sightseeing micro-type (statue, plaque, tomb, cinema, office,
    mis-tagged shop) with NO independent notability signal. Curated / Wikipedia / heritage
    items always survive. Deterministic; never raises."""
    name = _attraction_name_lower(a)
    if not name or any(tok in name for tok in _JUNK_NAME_TOKENS):
        return True
    if _has_notability_signal(a):
        return False
    cat = _lower(a.get("category"))
    sub = _osm_subtype(cat)                          # value after '=' (e.g. 'artwork')
    key = cat.split("=", 1)[0] if "=" in cat else cat  # OSM key (e.g. 'shop', 'amenity')
    if sub in _JUNK_ATTRACTION_TYPES:
        return True
    if key in {"shop", "office"} or sub in {"shop", "office"}:  # e.g. the plant-shop mis-tag
        return True
    return False


def _notability_score(a: dict) -> int:
    """Integer must-see score (higher = more iconic). Deterministic, presence-only —
    no float, no live lookup.

    The curated supplement is one dominant TOP tier (flat 1000): every hand-picked
    city icon outranks every OSM node, and WITHIN the tier the catalog_index tiebreak
    preserves the supplement's own editorial priority (Eiffel Tower before a market) —
    the category bonus must not reshuffle curated icons against each other. Non-curated
    OSM nodes are then ranked by their own cheap signals: Wikipedia article > heritage
    listing > recognised sightseeing category > a lone wikidata tag (weak — near-
    ubiquitous, so it never lifts a node above a genuinely notable one)."""
    if _is_curated(a):
        return 1000         # hand-picked city must-see (Gemini-grounded supplement) — top tier
    score = 0
    if a.get("wikipedia"):
        score += 8          # has an encyclopaedia article
    if a.get("heritage"):
        score += 6          # official heritage listing
    if a.get("wikidata"):
        score += 2          # weak notability (nearly ubiquitous)
    cat = _lower(a.get("category"))
    if any(tok in cat for tok in _HIGH_VALUE_TOKENS):
        score += 4          # recognised sightseeing type (museum/landmark/monument/...)
    return score


# round-2 #poi-clustering-fix: descriptive fields whose PRESENCE correlates with an
# attraction being a properly-established, currently-operating tourist destination
# (a website, admission/fee info, published hours, a photo, accessibility info) —
# mirrors the completeness-index reasoning already used for restaurant ranking
# (`_completeness_index`), applied here as a SECONDARY tie-break only, never a
# substitute for the notability score above. Discriminates within a tied notability
# bucket (common: the catalog's `_notability_score` collapses many stub-wikipedia
# local landmarks to the SAME integer as a genuine global icon) — e.g. teamLab /
# Shibuya Sky (5 of 5 signals present) versus a small community museum (1-3 of 5).
_ATTRACTION_COMPLETENESS_FIELDS = ("website", "fee", "opening_hours", "image", "wheelchair")


def _attraction_completeness(a: dict) -> int:
    """Count of tourist-relevant descriptive fields present on *a* (0-5)."""
    return sum(1 for f in _ATTRACTION_COMPLETENESS_FIELDS if a.get(f))


def _ranked_attractions(attractions: list[dict], interests: list[str]) -> list[tuple[int, dict]]:
    """Return [(catalog_index, attraction), ...] in deterministic rank order.

    rank key = (-interest_score:int, -notability_score:int, -completeness:int,
    catalog_index:int, name_lower:str). All keys are integer/string — NEVER a float.
    Notability floats a city's must-see icons above obscure nodes (the
    alphabetical-dump fix); an explicit user interest still takes precedence;
    completeness (round-2) breaks notability TIES toward the more richly-documented,
    more likely globally-known venue; catalog_index/name give a total, stable
    tiebreak so identical inputs stay byte-identical (var-0)."""
    indexed = list(enumerate(attractions))
    indexed.sort(
        key=lambda pair: (
            -_interest_score(pair[1], interests),
            -_notability_score(pair[1]),
            -_attraction_completeness(pair[1]),
            pair[0],
            _attraction_name_lower(pair[1]),
        )
    )
    return indexed


# ---------------------------------------------------------------------------
# round-2 #dedup-fix: a single real-world attraction (often a long street/landmark
# tagged as several OSM way-segments/nodes — e.g. "Yaowarat Road" surfaced 4x in one
# Bangkok trip) can appear multiple times in the raw catalog list under the IDENTICAL
# name. Restaurants already collapse same-name entries (chain-branch dedup, see
# `_best_by_name` in build_day_plan); attractions never did. Collapse to ONE surviving
# entry per name here, BEFORE the junk filter / notability ranking, so a duplicate can
# never occupy two schedule slots (or a schedule slot AND the unscheduled list).
# ---------------------------------------------------------------------------

def _dedupe_attractions_by_name(attractions: list[dict]) -> list[dict]:
    """Collapse same-name duplicate attraction rows to ONE survivor, keeping the
    first-appearing item at the BEST rank among the duplicates: curated (score 1000)
    beats a wikipedia/heritage-backed row beats a bare OSM node, and any further tie
    breaks on the original position (earliest wins) — so a merged/duplicated pool
    picks the same survivor every time (var-0). Unnamed rows (no name/name_en) are
    left as-is (never collapsed by an empty key — `_is_junk_attraction` drops those
    separately on genuinely empty names)."""
    best_by_name: dict[str, tuple[int, dict]] = {}
    order: list[str] = []
    for idx, a in enumerate(attractions):
        key = _attraction_name_lower(a)
        if not key:
            # No name to key on — keep every such row (rare; junk-filtered later).
            order.append(f"__unnamed_{idx}__")
            best_by_name[f"__unnamed_{idx}__"] = (idx, a)
            continue
        cur = best_by_name.get(key)
        if cur is None:
            order.append(key)
            best_by_name[key] = (idx, a)
        else:
            cur_idx, cur_a = cur
            cand_rank = (-_notability_score(a), idx)
            cur_rank = (-_notability_score(cur_a), cur_idx)
            if cand_rank < cur_rank:
                best_by_name[key] = (idx, a)
    return [best_by_name[k][1] for k in order]


# ---------------------------------------------------------------------------
# round-2 #poi-clustering-fix: a booked city's catalog bucket can be geographically
# NARROW (the OSM nearest-city bucketing assigns each POI to whichever catalog city
# POINT is closest, so a big metro's icons scatter across many nearby buckets — e.g.
# Tokyo's own "tokyo" bucket is a small pocket near Yoyogi/Shinjuku, while teamLab
# lives in the "chuo"/"minato city" buckets and Shibuya Sky in "shibuya" — the day
# plan for a "Tokyo" trip never even loaded those buckets, so citywide marquee icons
# were absent regardless of where the booked hotel actually was).
#
# Fix: pool in attractions from "metro sibling" catalog keys — OTHER keys in the SAME
# country whose own attraction centroid is geographically close to the resolved
# city's centroid (i.e. genuinely the same metro area) — computed PURELY from the
# committed catalog's own static lat/lon fields (no live geocoding, no hotel/booking
# data of any kind -> the selection is by CITYWIDE notability, independent of hotel
# proximity, exactly as asked). This is data-driven rather than a hardcoded ward
# list: a ward/suburb bucket that happens to share a common name with an unrelated
# city's ward (this catalog has several — a real, verified hazard) sits far outside
# the radius and is correctly excluded; a genuine same-metro bucket sits well inside
# it. Restaurants are NOT pooled (meals are reasonably hotel-neighbourhood-scoped;
# only the "attractions cluster" defect is addressed here).
# ---------------------------------------------------------------------------

_METRO_MERGE_RADIUS_KM = 20.0
_EARTH_RADIUS_KM = 6371.0

# Memoized per-catalog-key sibling lookups (the catalog is loaded once and immutable
# at runtime, so this is safe to cache for the life of the process). Populated lazily
# on first use — computing this for every one of the ~5,000 catalog cities up front
# would be wasted work for cities nobody ever books.
_METRO_SIBLINGS_CACHE: dict[str, list[str]] = {}


def _entry_centroid(entry: dict | None) -> tuple[float, float] | None:
    """Mean (lat, lon) over an entry's attractions that carry BOTH coordinates.
    None when the entry is absent or has no geotagged attraction at all."""
    if not entry:
        return None
    pts = [
        (a["lat"], a["lon"]) for a in (entry.get("attractions") or [])
        if isinstance(a.get("lat"), (int, float)) and isinstance(a.get("lon"), (int, float))
    ]
    if not pts:
        return None
    n = len(pts)
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)


def _haversine_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lon) points. Pure arithmetic —
    no live lookup, var-0 safe."""
    lat1, lon1 = p1
    lat2, lon2 = p2
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _metro_sibling_keys(primary_key: str) -> list[str]:
    """OTHER catalog keys in the SAME country (iso2 prefix) whose own attraction
    centroid lies within `_METRO_MERGE_RADIUS_KM` of `primary_key`'s centroid — i.e.
    verified (by the catalog's OWN coordinates), not merely name-guessed, to be the
    same metro area. Memoized; returns [] when the primary entry is missing/geo-less
    (a conservative no-op, never a fabrication). Deterministic: sorted key iteration
    and a stable numeric distance comparison -> var-0 safe (no set-iteration leak)."""
    cached = _METRO_SIBLINGS_CACHE.get(primary_key)
    if cached is not None:
        return cached
    centroid = _entry_centroid(_POI_CATALOG.get(primary_key))
    siblings: list[str] = []
    if centroid is not None and ":" in primary_key:
        prefix = primary_key.split(":", 1)[0] + ":"
        for k in sorted(_POI_CATALOG):
            if k == primary_key or not k.startswith(prefix):
                continue
            other_centroid = _entry_centroid(_POI_CATALOG[k])
            if other_centroid is None:
                continue
            if _haversine_km(centroid, other_centroid) <= _METRO_MERGE_RADIUS_KM:
                siblings.append(k)
    _METRO_SIBLINGS_CACHE[primary_key] = siblings
    return siblings


def _citywide_attraction_pool(primary_key: str, primary_attractions: list[dict]) -> list[dict]:
    """The primary bucket's own attractions PLUS every metro-sibling bucket's
    attractions, deduplicated by name (round-2 #poi-clustering-fix + #dedup-fix
    combined — a merge with no dedup would just create MORE cross-bucket
    duplicates). Dedup runs UNCONDITIONALLY (a same-bucket duplicate — e.g. one long
    street/landmark tagged as several OSM segments, like "Yaowarat Road" surfacing 4x
    in one Bangkok trip — needs no sibling merge to occur), so this never returns the
    exact input list object; a city with no verified metro siblings is otherwise
    unaffected (same items, just deduplicated)."""
    pooled = list(primary_attractions)
    for sib in _metro_sibling_keys(primary_key):
        pooled.extend((_POI_CATALOG.get(sib) or {}).get("attractions") or [])
    return _dedupe_attractions_by_name(pooled)


def _meal_identity(restaurant: dict) -> str:
    """Stable identity for within-day meal dedup — the venue name as the traveller sees it (matches
    _stamp_meal's surfaced name_en/name precedence). Lowercased; never raises."""
    return _lower(restaurant.get("name_en") or restaurant.get("name") or "")


def _select_meal(
    candidates: list[tuple[int, dict]],
    slot: str,
    day_index: int,
    used: list[str] | None = None,
    history: list[str] | None = None,
    capped: set[str] | None = None,
    dining_tier: str | None = None,
    prefer: set[str] | None = None,
    dest_iso2: str | None = None,
    preferred_cuisine: str | None = None,
) -> dict | None:
    """Pick one restaurant for a slot, maximising VARIETY: avoid any venue chosen earlier the SAME day
    (`used`) AND any venue already used earlier in the TRIP (`history`). So a city with enough distinct
    venues never repeats a restaurant until every distinct one has been used — no more eating at the
    same place many days running. When every distinct venue has been used (pool thinner than the meal
    count), reuse the LEAST-RECENTLY-USED one so repeats are spaced as far apart as possible (honest
    reuse — never fabricated, flagged by the thin-pool note).

    Selection key = (-slot_fit_score, -dining_tier_rank, prefer, cuisine_mismatch, is_fastfood,
    catalog_index, name_lower). slot-fit is ALWAYS primary; dining_tier/cuisine bias are strictly
    SECONDARY reorders (never a filter — an absent tier/cuisine signal ranks every restaurant 0, a
    no-op). `dest_iso2`/`preferred_cuisine` (round-2 #cuisine-bias-fix) de-weight a restaurant whose
    cuisine is a KNOWN, DIFFERENT nationality than both the destination's own cuisine and any
    explicitly-stated cuisine interest — see `_cuisine_mismatch`. Deterministic: ordered
    `used`/`history` lists, integer last-use index, rank tiebreak → no set-iteration order leaks
    (var-0). Returns None only when there are no candidates."""
    if not candidates:
        return None
    used = used or []
    history = history or []
    capped = capped or set()   # chain names that hit the per-leg ≤2 cap → never re-pick
    prefer = prefer or set()   # venue identities that satisfy a stated food interest → float up
    seen_trip = set(history)
    ranked = sorted(
        candidates,
        key=lambda pair: (
            -_slot_fit_score(pair[1], slot),
            -_dining_tier_rank(pair[1], dining_tier),
            # #meal-quality-fix: a venue that fulfils a stated food interest ("street
            # food", "hawker") floats to the front (0 before 1) so the interest is
            # actually reflected in the meal plan, not just noted.
            0 if _meal_identity(pair[1]) in prefer else 1,
            # round-2 #cuisine-bias-fix: sink a confidently-mismatched cuisine (a known
            # DIFFERENT nationality's food than the destination/stated interest) below
            # local/requested/neutral options — never a filter, just a preference.
            _cuisine_mismatch(pair[1], dest_iso2, preferred_cuisine),
            # #meal-quality-fix: de-weight global fast-food / chain-cafe brands so an
            # independent local venue wins whenever fit/tier tie (which they do for
            # lunch/dinner where every venue fits). A chain still fills a slot when the
            # city has nothing else — never a filter, only a preference (0 before 1).
            _is_global_fastfood(pair[1]),
            pair[0],
            _lower(pair[1].get("name_en") or pair[1].get("name") or ""),
        ),
    )
    n = len(ranked)
    start = day_index % n
    # Pass 1 — FRESH: first venue (from the cross-day offset) not used today AND not used yet this trip.
    for k in range(n):
        cand = ranked[(start + k) % n][1]
        ident = _meal_identity(cand)
        if ident not in used and ident not in seen_trip and ident not in capped:
            return cand
    # Pass 2 — every distinct venue has been used: reuse the LEAST-RECENTLY-USED one that isn't used
    # today, so a repeat is spaced as far as possible. last_use: larger index = more recently used.
    last_use: dict[str, int] = {}
    for idx, ident in enumerate(history):
        last_use[ident] = idx
    best: tuple[tuple[int, int], dict] | None = None
    for ri, (_ci, cand) in enumerate(ranked):
        ident = _meal_identity(cand)
        if ident in used or ident in capped:
            continue
        key = (last_use.get(ident, -1), ri)   # oldest last-use first, then best rank
        if best is None or key < best[0]:
            best = (key, cand)
    if best is not None:
        return best[1]
    # Last resort (pool < intra-day slots): first non-capped venue from the offset, else None.
    for k in range(n):
        cand = ranked[(start + k) % n][1]
        if _meal_identity(cand) not in capped:
            return cand
    return None


# #meal-quality-fix: food/cuisine INTERESTS that a RESTAURANT fulfils (not an
# attraction). Without this, "street food" was matched only against attraction
# categories/names, always missed, and produced a misleading "no match found" note
# even in a city full of street-food venues. Maps the interest → (category token,
# cuisine/name tokens) that satisfy it. "" category means any restaurant satisfies.
_FOOD_INTEREST_SIGNALS: dict[str, tuple[str, tuple[str, ...]]] = {
    "street food": ("food_court", ("street_food", "hawker")),
    "street_food": ("food_court", ("street_food", "hawker")),
    "streetfood": ("food_court", ("street_food", "hawker")),
    "hawker": ("food_court", ("hawker", "street_food")),
    "food court": ("food_court", ()),
    "food_court": ("food_court", ()),
    "night market": ("food_court", ("street_food", "hawker")),
    "foodie": ("", ()),
    "food": ("", ()),
    "local food": ("", ()),
    "local cuisine": ("", ()),
    "cuisine": ("", ()),
    "dining": ("", ()),
}


# Street-food-style interests are additionally honoured by quick-casual LOCAL venues:
# most catalog cities have no explicit `food_court`/`street_food` tag, but a non-chain
# `fast_food` node is the honest nearest proxy for "street food" (quick, casual, local) —
# far better than falsely reporting "no match found" while the city is full of them.
_STREET_FOOD_INTERESTS = frozenset({
    "street food", "street_food", "streetfood", "hawker", "night market",
})


def _restaurant_matches_food_interest(restaurant: dict, interest: str) -> bool:
    """True iff the restaurant fulfils a stated FOOD interest ("street food", "foodie"...).

    Deterministic. A generic food interest ("" category) is satisfied by any restaurant;
    a specific one needs a matching category, cuisine/name token, or — for street-food
    interests — a non-chain quick-casual venue (the honest local proxy)."""
    key = _lower(interest).strip()
    sig = _FOOD_INTEREST_SIGNALS.get(key)
    if sig is None:
        return False
    cat_want, token_wants = sig
    cat = _lower(restaurant.get("category"))
    if cat_want == "":
        return True
    if cat_want and cat_want in cat:
        return True
    cz = _lower(restaurant.get("cuisine"))
    name = _lower(restaurant.get("name_en") or restaurant.get("name"))
    for w in token_wants:
        if _token_hit(w, cz) or _token_hit(w, name) or _token_hit(w, cat):
            return True
    # Street-food proxy: a non-chain quick-casual (fast_food) venue counts.
    if key in _STREET_FOOD_INTERESTS and "fast_food" in cat and not _is_global_fastfood(restaurant):
        return True
    return False


def _matches_cuisine(restaurant: dict, cuisine: str) -> bool:
    """True if the restaurant matches a requested cuisine token (cuisine field / category only —
    NOT the name, to avoid false 'pizza'-in-a-name matches). Used by the edit-lane cuisine filter."""
    want = _lower(cuisine).strip()
    if not want:
        return True
    cz = _lower(restaurant.get("cuisine"))
    tokens = {t.strip() for t in cz.replace(",", ";").split(";") if t.strip()}
    cat = _lower(restaurant.get("category"))
    return want in tokens or any(want in t for t in tokens) or want in cat


# ---------------------------------------------------------------------------
# round-2 #cuisine-bias-fix: a stated cuisine/interest ("street food" in Thailand) was
# not reflected in meal selection — a Thai street-food trip could still surface a
# Lebanese supper, because slot-fit/dining-tier/completeness ranking has NO cuisine
# awareness at all: a well-documented (many OSM tags) international restaurant often
# beats a scrappily-tagged local stall on the EXISTING completeness tie-break, even
# though it has nothing to do with the destination or the traveller's stated interest.
#
# Fix: a SECONDARY bias (never a filter — a mismatched venue still fills a slot when
# nothing else is available) that floats LOCAL-to-the-destination and
# explicitly-REQUESTED cuisines to the front, and sinks a cuisine that is a KNOWN,
# DIFFERENT nationality's cuisine than both of those. Cuisine tokens with no
# recognised nationality (regional/local/generic, or simply unknown) are neutral —
# never punished on a guess (honesty: only sink a POSITIVE, confident mismatch).
# ---------------------------------------------------------------------------

# OSM `cuisine` tokens that name a specific national/ethnic cuisine, mapped to the
# ISO2 country it is native to. Deliberately a modest, high-confidence set (the
# catalog's own most common nationality-cuisine tags) — used ONLY to detect a
# confident mismatch, never to guess at an unrecognised/regional token.
_CUISINE_NATIVE_ISO2: dict[str, str] = {
    "thai": "TH", "japanese": "JP", "chinese": "CN", "korean": "KR",
    "vietnamese": "VN", "indonesian": "ID", "filipino": "PH", "malaysian": "MY",
    "khmer": "KH", "lao": "LA", "burmese": "MM",
    "indian": "IN", "pakistani": "PK", "bangladeshi": "BD", "nepalese": "NP", "sri_lankan": "LK",
    "italian": "IT", "french": "FR", "spanish": "ES", "portuguese": "PT",
    "greek": "GR", "turkish": "TR", "lebanese": "LB", "moroccan": "MA", "arab": "AE",
    "mexican": "MX", "brazilian": "BR", "peruvian": "PE", "argentinian": "AR",
    "german": "DE", "russian": "RU", "polish": "PL",
    "ethiopian": "ET", "egyptian": "EG", "american": "US",
}
# Cuisine tokens that are NEVER a mismatch signal (too generic/regional to attribute
# to one nationality, or explicitly claiming to already be local/international).
_CUISINE_NEUTRAL_TOKENS = frozenset({
    "regional", "local", "international", "fusion", "asian", "western", "seafood",
    "grill", "barbecue", "steak_house", "pizza", "burger", "sandwich", "cafe",
    "coffee_shop", "dessert", "ice_cream", "bakery", "breakfast", "chicken",
})


def _restaurant_cuisine_tokens(restaurant: dict) -> frozenset[str]:
    """Lower-cased cuisine tokens on a restaurant (OSM `cuisine` is ';'/','-joined)."""
    cz = _lower(restaurant.get("cuisine"))
    return frozenset(t.strip() for t in cz.replace(",", ";").split(";") if t.strip())


def _scan_stated_cuisine(interests: list[str]) -> str | None:
    """First explicitly-named NATIONAL cuisine among the trip's stated interests
    (e.g. interest 'italian food' / 'italian' -> 'italian'), or None when no
    interest names a recognised nationality cuisine. Deterministic: scans
    `_CUISINE_NATIVE_ISO2` keys in sorted order so ties (an interest list naming two
    cuisines) resolve the same way every time (var-0)."""
    if not interests:
        return None
    lowered = [_lower(i) for i in interests]
    for token in sorted(_CUISINE_NATIVE_ISO2):
        if any(_token_hit(token, i) for i in lowered):
            return token
    return None


def _cuisine_mismatch(restaurant: dict, dest_iso2: str | None, preferred_cuisine: str | None) -> bool:
    """True iff *restaurant* carries a CONFIDENT, KNOWN-nationality cuisine that is
    neither the explicitly preferred/stated cuisine NOR native to the destination —
    the only case worth de-weighting (a genuine, positively-identified mismatch;
    unknown/generic/regional tokens are NEVER treated as a mismatch — honesty: never
    punish on a guess)."""
    tokens = _restaurant_cuisine_tokens(restaurant)
    if not tokens or tokens & _CUISINE_NEUTRAL_TOKENS:
        return False
    if preferred_cuisine and preferred_cuisine in tokens:
        return False
    dest = (dest_iso2 or "").strip().upper()
    for t in tokens:
        native = _CUISINE_NATIVE_ISO2.get(t)
        if native is None:
            continue  # unrecognised token — never guess a mismatch
        if native == dest:
            return False  # native to the destination — always fine
        if preferred_cuisine is None and dest:
            # A recognised nationality cuisine that is NOT the destination's own,
            # and the traveller didn't ask for anything specific -> a mismatch.
            return True
        if preferred_cuisine and t != preferred_cuisine:
            return True  # a different recognised cuisine than the one requested
    return False


def derive_bad_weather_days(
    region: str | None, checkin: str, n_days: int, *, threshold_bp: int = 3000
) -> list[int]:
    """PURE: high flood/cyclone-risk day indices for a leg, from the seasonal tables
    (risk_agent._FLOOD_BY_REGION_MONTH / _CYCLONE_BY_REGION_MONTH) keyed by the leg's
    sub-national `region` + each day's month. CONSERVATIVE: empty when region is unknown,
    not in the tables, or dates unparseable — NEVER flags on missing data. No wall-clock /
    random (date + timedelta only) → var-0-safe. threshold_bp=3000 = the AVOID tier."""
    if not region or n_days <= 0:
        return []
    try:
        from agents.risk_agent import _FLOOD_BY_REGION_MONTH, _CYCLONE_BY_REGION_MONTH
    except Exception:  # noqa: BLE001 — derivation is best-effort; missing table → no flags
        return []
    rk = _lower(region).strip()
    flood = _FLOOD_BY_REGION_MONTH.get(rk) or {}
    cyc = _CYCLONE_BY_REGION_MONTH.get(rk) or {}
    if not flood and not cyc:
        return []
    try:
        d0 = date.fromisoformat((checkin or "").strip())
    except (ValueError, TypeError):
        return []
    bad: list[int] = []
    for i in range(n_days):
        m = (d0 + timedelta(days=i)).month
        if max(int(flood.get(m, 0)), int(cyc.get(m, 0))) >= threshold_bp:
            bad.append(i)
    return bad


# ---------------------------------------------------------------------------
# Pure planning core — build_day_plan
# ---------------------------------------------------------------------------

def build_day_plan(
    city: str,
    iso2: str,
    country: str,
    checkin: str,
    checkout: str,
    num_days: int | None = None,
    interests: list[str] | None = None,
    dietary: list[str] | None = None,
    pace: str | None = None,
    bad_weather_days: list[int] | None = None,
    arrival_transport_minutes: int = 0,
    interest_map: dict[str, list[str]] | None = None,
    region: str | None = None,
    meal_cuisines: dict[str, str] | None = None,
    dining_tier: str | None = None,
    children: int | None = None,
) -> dict:
    """Build a deterministic per-leg day plan.

    Pure: no self, no wall-clock (date/timedelta only, never .today()/.now()),
    no random, no set-iteration on any output path. Byte-identical for identical
    inputs (var-0).

    On a catalog miss (unknown city) returns a conservative empty plan with
    catalog_hit=false and an explanatory note — NEVER fabricates POIs."""
    interests = list(interests or [])
    interest_map = dict(interest_map or {})
    dietary = list(dietary or [])
    _bw_supplied = bad_weather_days is not None   # explicit caller value wins over derivation
    bad_weather_days = list(bad_weather_days or [])
    meal_cuisines = {(_lower(k) or k): v for k, v in (meal_cuisines or {}).items() if v}
    notes: list[str] = []

    # round-2 #cuisine-bias-fix: an explicitly-named national cuisine among the
    # trip's stated interests ("Italian food") — passed into meal selection so it
    # (and the destination's own local cuisine, via `dest_iso2`) is preferred and a
    # KNOWN-different nationality is de-weighted (see `_cuisine_mismatch`).
    _preferred_cuisine = _scan_stated_cuisine(interests)

    # #party-fix: a party with children gets kid-appropriate dining — when the caller
    # has not already set an explicit dining_tier, bias meals toward the "family" tier
    # (sinks adult/alcohol-primary bars & pubs). Cheap, deterministic, honest signal.
    try:
        _children = int(children or 0)
    except (TypeError, ValueError):
        _children = 0
    if _children > 0 and not dining_tier:
        dining_tier = "family"
        notes.append(
            f"party includes {_children} child(ren); meals bias to family-friendly "
            "venues (bars/pubs de-prioritised) — not fabricated."
        )

    # --- num_days: prefer the checkin→checkout span; fall back conservatively. ---
    computed_days: int | None = None
    try:
        d_in = date.fromisoformat((checkin or "").strip())
        d_out = date.fromisoformat((checkout or "").strip())
        computed_days = max((d_out - d_in).days, 1)
    except (ValueError, TypeError):
        notes.append(
            f"could not parse checkin/checkout ({checkin!r}/{checkout!r}); "
            f"falling back to num_days={num_days if num_days else 1}"
        )
    if computed_days is not None:
        n_days = computed_days
    elif num_days is not None and num_days >= 1:
        n_days = int(num_days)
    else:
        n_days = 1

    # (a) Auto-derive bad-weather days from seasonal flood/cyclone risk when the caller did NOT
    # supply them (explicit user/leg value always wins). Conservative-empty off-season / unknown
    # region → no false flags. Pure seasonal lookup, no wall-clock → var-0.
    if not _bw_supplied:
        bad_weather_days = derive_bad_weather_days(region, checkin, n_days)

    # --- catalog lookup (#70: tolerant of the 'X' vs 'X city' naming so a booked city plans) ---
    entry, key = _resolve_catalog_entry(iso2, city)

    result: dict[str, Any] = {
        "leg_id": None,  # filled by the handler if present
        "city": city,
        "iso2": (iso2 or "").strip().upper(),
        "country": country,
        "catalog_hit": entry is not None,
        "num_days": n_days,
        "days": [],
        "unscheduled_attractions": [],
        "provenance": _PROVENANCE,
        "notes": notes,
    }

    if entry is None:
        # Conservative miss — empty days/meals, no fabricated POIs.
        notes.append(
            f"unknown city for activity planning: no POI catalog entry for "
            f"{key!r}; returning a conservative empty plan (no attractions/meals "
            f"fabricated). Verify locally."
        )
        result["days"] = [
            {
                "day_index": i,
                "bad_weather": i in bad_weather_days,
                "attractions": [],
                "meals": {slot: None for slot in _MEAL_SLOTS},
            }
            for i in range(n_days)
        ]
        return result

    # round-2 #poi-clustering-fix + #dedup-fix: pool in any verified metro-sibling
    # buckets (same country, geographically the same metro area per the catalog's OWN
    # coordinates — see `_citywide_attraction_pool`) so a big city's marquee icons
    # (scattered across nearby buckets by the harvest's nearest-city assignment)
    # compete for citywide notability ranking regardless of which bucket the booked
    # hotel happens to be nearest to; then dedupe by name so a duplicated row (e.g. one
    # long street/landmark tagged as several OSM segments) can never occupy multiple
    # schedule slots. A single-bucket city with no verified siblings is unaffected
    # (byte-identical to before — see `_metro_sibling_keys`' empty-list no-op).
    raw_attractions = _citywide_attraction_pool(key, list(entry.get("attractions") or []))
    restaurants = entry.get("restaurants") or []

    # #meal-quality-fix: venue identities that satisfy a stated FOOD interest (e.g.
    # "street food") — populated in the interest-evidence loop below, consumed by the
    # meal selector to bias those slots toward the matching venues.
    food_prefer_names: set[str] = set()

    # --- Junk filter: drop ornamental / non-sightseeing nodes (untitled artwork &
    # statues, memorial plaques, tombs, cinemas, offices, mis-tagged shops) that carry
    # no notability signal, so an alphabetical OSM dump can no longer crowd out a city's
    # real sights. Notable items (curated / wikipedia / heritage) always survive. ---
    attractions = [a for a in raw_attractions if not _is_junk_attraction(a)]
    dropped = len(raw_attractions) - len(attractions)
    if dropped:
        notes.append(
            f"filtered {dropped} low-notability node(s) not fit for a day plan "
            f"(untitled/ornamental artwork, memorial plaques, cinemas, etc.) from "
            f"{len(raw_attractions)} harvested; kept {len(attractions)} sightseeing "
            f"attraction(s)."
        )

    # --- Attraction ranking + day assignment (pace cap) ---
    ranked = _ranked_attractions(attractions, interests)
    cap = _PACE_CAP.get(_lower(pace), _DEFAULT_PACE_CAP)

    # Fill day 0,1,2,... up to cap per day in rank order; overflow → unscheduled.
    capacity = cap * n_days
    scheduled = ranked[:capacity]
    overflow = ranked[capacity:]

    # day_attractions[i] is the ordered list of (catalog_index, attraction) for day i.
    day_attractions: list[list[tuple[int, dict]]] = [[] for _ in range(n_days)]
    for pos, (cidx, a) in enumerate(scheduled):
        day_attractions[pos // cap].append((cidx, a))

    # --- Travel-day reservation: trim the ARRIVAL day (index 0) proportionally ---
    # arrival_transport_minutes=0 (default) → no trim, byte-identical to prior runs.
    # The ONLY division feeds a single round() of an int*int/int ratio — var-0 safe.
    travel_note = None
    mins = int(arrival_transport_minutes or 0)
    if mins > 0 and n_days >= 1 and day_attractions[_TRIM_DAY_INDEX]:
        full_count = len(day_attractions[_TRIM_DAY_INDEX])
        reserved = min(mins, _USABLE_DAY_MINUTES)
        new_count = max(1, round(full_count * (_USABLE_DAY_MINUTES - reserved) / _USABLE_DAY_MINUTES))
        if new_count < full_count:
            trimmed = day_attractions[_TRIM_DAY_INDEX][new_count:]
            day_attractions[_TRIM_DAY_INDEX] = day_attractions[_TRIM_DAY_INDEX][:new_count]
            overflow = trimmed + overflow      # trimmed-but-notable land first in unscheduled
            hours = round(mins / 60)
            travel_note = (f"Lighter day — arrival travel (~{hours}h in transit); "
                           f"trimmed from {full_count} to {new_count} attraction(s) to leave time for the journey.")

    # HONESTY (#54 sweep finding): flag whenever ANY scheduled day ends up with no
    # attraction — not just when len(attractions) < n_days. The pace-cap distribution
    # can leave later days empty even with enough total attractions (e.g. 4 attractions,
    # cap 3 → day0=3, day1=1, days 2-3 empty), which would otherwise be a SILENT hole.
    empty_days = sum(1 for da in day_attractions if not da)
    if empty_days:
        notes.append(
            f"{len(attractions)} known attraction(s) cover {n_days - empty_days} of "
            f"{n_days} day(s); {empty_days} day(s) have no scheduled attraction "
            f"(honest gap, not fabricated)."
        )

    # --- Phase 3: Honest unsatisfiable-activity advisory ---
    # After ranking, detect ORIGINAL interests with ZERO catalog evidence in this
    # leg's attraction set. An interest has evidence iff at least one attraction
    # matches ANY of its expanded catalog terms (token appears as a word-boundary
    # hit in the attraction's category or name_en/name). When there is no evidence,
    # emit an honest note — NEVER fabricate a POI to satisfy the request.
    #
    # Granularity: we report the ORIGINAL interest the user expressed (e.g.
    # "hanami"), flagged only when NONE of its expansion terms match. This avoids
    # flagging individual expansion synonyms (e.g. "sakura", "cherry blossom")
    # that the user never typed when the parent interest IS satisfied. The
    # interest_map {original -> [terms]} carries that grouping from the parser; if
    # it is absent (e.g. an already-expanded clamp-path leg), fall back to treating
    # each flat interest token as its own group (legacy per-token behaviour).
    # Deterministic: sorted keys → stable note text across identical inputs (var-0).
    def _interest_has_evidence(terms: list[str]) -> bool:
        for term in terms:
            t = _lower(term)
            if not t:
                continue
            for _ci, att in ranked:
                if _token_hit(t, _lower(att.get("category"))) or \
                        _token_hit(t, _attraction_name_lower(att)):
                    return True
        return False

    if interests or interest_map:
        # Group flat interests by their original interest. interest_map is the
        # authoritative grouping (an original maps to its expansion terms, or to
        # itself when unmapped). Invariant for parser-built legs: the union of all
        # map values == the flat interests set, so the second loop adds nothing.
        # The second loop is the SAFETY NET: it also covers (a) legacy/clamp legs
        # that carry flat interests but NO map, and (b) any caller whose map omits
        # a flat token — that token gets its own group so it is still honestly
        # checked rather than silently dropped (never-fabricate over tidiness).
        # All terms are lowercased on ingest so the `t in v` dedup is exact.
        groups: dict[str, list[str]] = {}
        for original, terms in interest_map.items():
            o = _lower(original)
            if o:
                vals = [_lower(t) for t in (terms or [original]) if _lower(t)]
                groups[o] = vals or [o]
        for token in interests:
            t = _lower(token)
            if t and t not in groups and not any(t in v for v in groups.values()):
                groups[t] = [t]

        unsatisfied: list[str] = []
        for original in sorted(groups):
            if _interest_has_evidence(groups[original]):
                continue
            # #meal-quality-fix: a FOOD interest ("street food", "foodie", "hawker") is
            # fulfilled by the RESTAURANT pool, not attractions — check there before
            # declaring "no match", and remember the matching venues so meal selection
            # can bias toward them below.
            if original in _FOOD_INTEREST_SIGNALS:
                _matched = [r for r in restaurants
                            if _restaurant_matches_food_interest(r, original)]
                if _matched:
                    for r in _matched:
                        food_prefer_names.add(_meal_identity(r))
                    continue
            unsatisfied.append(original)
        if unsatisfied:
            # Sort for deterministic note text (var-0).
            unsatisfied_label = ", ".join(sorted(unsatisfied))
            notes.append(
                f"activity interest(s) not present in verified POI catalog for {city!r}: "
                f"{unsatisfied_label} — no match found in {len(attractions)} known "
                f"attraction(s); cannot plan this activity (not fabricated)."
            )

    # --- Dietary filter (honest fallback) for restaurant pool ---
    diet_tokens = frozenset(_lower(d) for d in dietary if _lower(d))
    diet_fallback = False
    if diet_tokens:
        filtered = [
            r for r in restaurants
            if diet_tokens & frozenset(_lower(t) for t in (r.get("diet") or []) if _lower(t))
        ]
        if filtered:
            restaurant_pool = filtered
        else:
            # Fallback to the unfiltered pool BUT stamp the honesty note + per-meal note.
            restaurant_pool = restaurants
            diet_fallback = True
            diet_label = ", ".join(sorted(diet_tokens))
            notes.append(
                f"no restaurant with declared {diet_label} diet known; "
                f"meals fall back to unfiltered venues — verify suitability."
            )
    else:
        restaurant_pool = restaurants

    if not restaurants:
        notes.append(
            "no restaurants known for this city; all meal slots are null "
            "(not fabricated)."
        )

    # Collapse chain branches BEFORE indexing: a venue name (e.g. "Highlands Coffee") often has many
    # OSM branch-nodes in the pool, so a chain would otherwise dominate every meal slot AND every day
    # (the rotation just cycles through identically-named branches). Keep ONE entry per name — highest
    # completeness, then earliest original position. Deterministic: the survivors are re-emitted in
    # original-index order, so nothing leaks dict-iteration order into the output (var-0).
    _best_by_name: dict[str, tuple[int, int, dict]] = {}
    _branch_count: dict[str, int] = {}   # original branch-nodes per name → ≥2 marks a CHAIN
    for _idx, _r in enumerate(restaurant_pool):
        _key = _meal_identity(_r)
        if not _key:
            continue
        _branch_count[_key] = _branch_count.get(_key, 0) + 1
        _cand = (_completeness_index(_r), _idx, _r)
        _cur = _best_by_name.get(_key)
        if _cur is None or _cand[0] > _cur[0] or (_cand[0] == _cur[0] and _cand[1] < _cur[1]):
            _best_by_name[_key] = _cand
    _deduped = [t[2] for t in sorted(_best_by_name.values(), key=lambda t: t[1])]
    _chain_names = {nm for nm, c in _branch_count.items() if c >= 2}   # multi-branch = chain → ≤2 per leg/city
    # #meal-quality-fix: also cap KNOWN global fast-food / chain-cafe brands even when the
    # city has only ONE branch node of them (they otherwise escaped the ≥2-branch heuristic
    # and could still win multiple dinners). Same ≤2-per-leg cap applies.
    _chain_names |= {nm for nm in _best_by_name if _is_global_fastfood({"name": nm})}

    # Pre-index the (chain-collapsed) restaurant pool with completeness_index once (var-0 stable).
    meal_candidates: list[tuple[int, dict]] = [
        (_completeness_index(r), r) for r in _deduped
    ]

    # Honest note when the city has fewer DISTINCT restaurants than meal slots: after chain-collapse
    # the same venue(s) will recur across slots/days (honest reuse, never fabricated). Flagging it
    # beats a silently repetitive plan that reads like a bug.
    if 0 < len(meal_candidates) < len(_MEAL_SLOTS):
        notes.append(
            f"only {len(meal_candidates)} distinct restaurant(s) known for this city — some meal "
            f"slots reuse the same venue (limited dining data, not fabricated)."
        )

    diet_note = None
    if diet_fallback:
        diet_label = ", ".join(sorted(diet_tokens))
        diet_note = (
            f"no restaurant with declared {diet_label} diet known; "
            f"verify suitability"
        )

    # --- Per-day assembly ---
    supper_missing_days: list[int] = []
    meal_history: list[str] = []   # venue identities chosen across the WHOLE leg → cross-day variety
    chain_use: dict[str, int] = {}    # (b) per-LEG per-CHAIN-name count → hard cap ≤2 per city
    capped_chains: set[str] = set()   # chains at the cap → _select_meal skips them
    cuisine_unmet: set[str] = set()   # (c) slots whose requested cuisine had no match → honest note
    cap_left_gap = False              # (b) a non-supper slot left OPEN because the only venues were capped
    for i in range(n_days):
        is_bad = i in bad_weather_days
        day_atts = list(day_attractions[i])

        # Bad-weather reorder: indoor-first, WITHIN this day only (no reassignment). Default day
        # output is byte-identical to before; the fair-weather order is offered additively below.
        if is_bad:
            day_atts.sort(
                key=lambda pair: (
                    _EXPOSURE_RANK.get(_lower(pair[1].get("weather_exposure")), 1),
                    pair[0],
                    _attraction_name_lower(pair[1]),
                )
            )

        meals: dict[str, dict | None] = {}
        meal_pool_by_slot: dict[str, list[dict]] = {}   # up to 2 alternatives per slot → inline swap panel
        used_today: list[str] = []   # venues already chosen earlier today → avoid intra-day repeats
        for slot in _MEAL_SLOTS:
            if not meal_candidates:
                meals[slot] = None
                continue
            # (c) cuisine filter (edit-lane set_meal_cuisine): restrict this slot's pool to the
            # requested cuisine; honest fallback to the full pool (+ once-note) when nothing matches.
            slot_pool = meal_candidates
            want_cuisine = meal_cuisines.get(slot)
            if want_cuisine:
                filtered = [(ci, r) for (ci, r) in meal_candidates if _matches_cuisine(r, want_cuisine)]
                if filtered:
                    slot_pool = filtered
                else:
                    cuisine_unmet.add(want_cuisine)
            if slot == "supper":
                # supper PREFERS a late-open venue; never invent one. Restrict the
                # pool to genuinely-late candidates; if none qualify → null + note.
                late_candidates = [
                    (ci, r) for (ci, r) in slot_pool
                    if _supper_qualifies_late(r)
                ]
                if not late_candidates:
                    meals[slot] = None
                    supper_missing_days.append(i)
                    continue
                chosen = _select_meal(late_candidates, slot, i, used_today, meal_history, capped_chains,
                                       dining_tier=dining_tier, prefer=food_prefer_names,
                                       dest_iso2=iso2, preferred_cuisine=_preferred_cuisine)
                _alts_source = late_candidates
            else:
                chosen = _select_meal(slot_pool, slot, i, used_today, meal_history, capped_chains,
                                       dining_tier=dining_tier, prefer=food_prefer_names,
                                       dest_iso2=iso2, preferred_cuisine=_preferred_cuisine)
                _alts_source = slot_pool
            meals[slot] = _stamp_meal(chosen, diet_note) if chosen is not None else None
            # Collect up to 2 runner-up alternatives for the inline swap panel.
            chosen_name = _meal_identity(chosen) if chosen is not None else None
            alts = [
                {"name": r.get("name") or r.get("name_en"), "cuisine": r.get("cuisine")}
                for (_ci, r) in _alts_source
                if _meal_identity(r) != chosen_name
            ][:2]
            if alts:
                meal_pool_by_slot[slot] = alts
            if chosen is not None:
                ident = _meal_identity(chosen)
                used_today.append(ident)
                meal_history.append(ident)
                if ident in _chain_names:   # (b) count + cap chains at ≤2 across this leg (city)
                    chain_use[ident] = chain_use.get(ident, 0) + 1
                    if chain_use[ident] >= 2:
                        capped_chains.add(ident)
            elif slot != "supper" and slot_pool and capped_chains:
                # the only remaining venues were chain-capped → leave the slot OPEN rather
                # than over-repeating one chain (honest; flagged in a note below).
                cap_left_gap = True

        day: dict[str, Any] = {
            "day_index": i,
            "bad_weather": is_bad,
            "attractions": [_stamp_attraction(a) for (_ci, a) in day_atts],
            "meals": meals,
            "meal_pool": meal_pool_by_slot,
        }
        if is_bad:
            # (a) additive wet-weather toggle: the FAIR-weather (standard rank) ordering so the UI
            # can flip between the rain-ready default (indoor-first, above) and the fair-weather plan.
            day["fair_weather_attractions"] = [
                _stamp_attraction(a) for (_ci, a) in day_attractions[i]
            ]
        result["days"].append(day)

    if cuisine_unmet:
        notes.append(
            f"no restaurant matching requested cuisine(s) {sorted(cuisine_unmet)} for some "
            "meal slot(s); fell back to the best available venue (not fabricated)."
        )

    if supper_missing_days:
        notes.append(
            "no late-opening venue known for supper on day(s) "
            f"{supper_missing_days}; supper left empty (not fabricated)."
        )

    if cap_left_gap:
        notes.append(
            "a frequently-branching chain was limited to 2 appearances in this city; "
            "where no distinct alternative was available, that meal slot was left open "
            "rather than repeating the chain."
        )

    if travel_note is not None and result.get("days"):
        result["days"][_TRIM_DAY_INDEX]["travel_note"] = travel_note
        notes.append(travel_note)   # surface at leg level too

    result["unscheduled_attractions"] = [_stamp_attraction(a) for (_ci, a) in overflow]
    return result


# ---------------------------------------------------------------------------
# DayPlannerAgent — A2A wrapper
# ---------------------------------------------------------------------------

class DayPlannerAgent(A2AAgent):
    """
    Per-leg day-by-day activity & meal planner (Travel Guild, build #30).

    Implements the ``activity.plan`` skill.

    Fully deterministic — no LLM, no DashScope, no live POI APIs. Surfaces ONLY
    what the committed OSM poistore harvest cache contains; an unknown city
    degrades to a conservative empty plan. NEVER fabricates POIs/hours/prices.

    Args:
        host: Bind host for the ASGI server.
        port: Bind port for the ASGI server.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9110,
    ) -> None:
        self._host = host
        self._port = port
        super().__init__()

    # ------------------------------------------------------------------
    # A2AAgent protocol
    # ------------------------------------------------------------------

    def _build_card(self) -> dict:
        url = f"http://{self._host}:{self._port}"
        return {
            "name": "day-planner-agent",
            "description": (
                "Per-leg day-by-day activity & meal planner — deterministic, "
                "presence-only (no LLM, no live POI APIs). Surfaces attractions "
                "and meals from the committed OSM poistore harvest cache, ranked "
                "by wikidata-notability (attractions) / data-completeness "
                "(restaurants), assigned across trip days by pace, with a "
                "bad-weather indoor-first reorder and an honest dietary fallback. "
                "NEVER fabricates POIs/hours/prices; an unknown city degrades to a "
                "conservative empty plan with a note. Implements A2A skill "
                "'activity.plan'. Part of the Travel Guild multi-agent pipeline "
                "(Track 3, build #30)."
            ),
            "url": url,
            "version": "1.0.0",
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
            },
            "defaultInputModes": ["data"],
            "defaultOutputModes": ["data"],
            "skills": [
                {
                    "id": "activity.plan",
                    "name": "Day-by-day Activity & Meal Plan",
                    "description": (
                        "Given trip legs "
                        "[{leg_id, city, iso2, country, checkin, checkout, "
                        "interests?, dietary?, pace?, bad_weather_days?}], build a "
                        "deterministic per-day plan of attractions (notability-ranked, "
                        "interest-weighted, pace-capped, indoor-first on bad-weather "
                        "days) and 5 meal slots (breakfast/lunch/tea/dinner/supper). "
                        "Returns {leg_plans:[DayPlanResult, ...]}. Presence-only / "
                        "fail-conservative: never fabricates POIs/hours/prices; "
                        "unknown city → conservative empty plan. Fully deterministic."
                    ),
                    "tags": [
                        "activity", "itinerary", "day-plan", "meals", "poi",
                        "deterministic", "presence-only", "osm",
                    ],
                    "inputSchema": {
                        "type": ["array", "object"],
                        "oneOf": [
                            {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "leg_id":   {"type": "string"},
                                        "city":     {"type": "string"},
                                        "iso2":     {"type": "string"},
                                        "country":  {"type": "string"},
                                        "checkin":  {"type": "string"},
                                        "checkout": {"type": "string"},
                                    },
                                    "required": ["leg_id", "city"],
                                },
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "legs": {
                                        "type": "array",
                                        "items": {"type": "object"},
                                    },
                                },
                                "required": ["legs"],
                            },
                        ],
                    },
                    "examples": [
                        json.dumps([
                            {"leg_id": "leg-0", "city": "abu dhabi",
                             "iso2": "AE", "country": "United Arab Emirates",
                             "checkin": "2026-10-01", "checkout": "2026-10-04",
                             "interests": ["museum", "art"]},
                        ])
                    ],
                }
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("activity.plan", self._dayplan_handler)

    # ------------------------------------------------------------------
    # Skill handler
    # ------------------------------------------------------------------

    async def _dayplan_handler(self, message: dict, task: dict) -> dict:
        """
        activity.plan skill handler.

        Extracts the leg list, builds a deterministic day plan per leg, and
        returns a {leg_plans:[DayPlanResult, ...]} artifact.
        """
        payload = self._extract_payload(message)
        if payload is None:
            raise ValueError(
                "activity.plan requires a data part with a JSON list of legs "
                "[{leg_id, city, iso2, country, checkin, checkout, ...}] "
                "or a dict {'legs': [...]}"
            )

        # Accept both list and wrapped dict.
        if isinstance(payload, list):
            legs = payload
        elif isinstance(payload, dict):
            legs = payload.get("legs", [])
        else:
            raise ValueError(
                f"activity.plan payload must be a list or dict, got {type(payload).__name__}"
            )

        if not isinstance(legs, list):
            raise ValueError("legs must be a list")

        leg_plans: list[dict] = []
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            plan = build_day_plan(
                city=leg.get("city", ""),
                iso2=leg.get("iso2", ""),
                country=leg.get("country", ""),
                checkin=leg.get("checkin", ""),
                checkout=leg.get("checkout", ""),
                num_days=leg.get("num_days"),
                interests=leg.get("interests"),
                dietary=leg.get("dietary"),
                pace=leg.get("pace"),
                bad_weather_days=leg.get("bad_weather_days"),
                arrival_transport_minutes=leg.get("arrival_transport_minutes") or 0,
                interest_map=leg.get("interest_map"),
                region=leg.get("region"),                 # enables seasonal bad-weather derivation
                meal_cuisines=leg.get("meal_cuisines"),   # edit-lane set_meal_cuisine
                dining_tier=leg.get("dining_tier"),
                children=leg.get("children"),             # #party-fix: kid-appropriate signals
            )
            plan["leg_id"] = leg.get("leg_id")
            leg_plans.append(plan)

        result_data = {"leg_plans": leg_plans}

        logger.info(
            "activity.plan: %d leg(s) → %d plan(s), %d catalog hit(s)",
            len(legs),
            len(leg_plans),
            sum(1 for p in leg_plans if p.get("catalog_hit")),
        )

        return _new_artifact(
            name="activity.plan.result",
            parts=[_data_part(result_data)],
        )

    # ------------------------------------------------------------------
    # Input extraction helper (copied from transport_agent / CriticAgent)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_payload(message: dict) -> Any:
        """Extract the JSON payload from the first data or text part."""
        for part in message.get("parts", []):
            if part.get("kind") == "data":
                data = part.get("data")
                if isinstance(data, (dict, list)):
                    return data
                if isinstance(data, str):
                    try:
                        return json.loads(data)
                    except Exception:
                        pass
            elif part.get("kind") == "text":
                text = part.get("text", "")
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, (dict, list)):
                        return parsed
                except Exception:
                    pass
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    port = int(os.environ.get("PORT", 9110))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = DayPlannerAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Day-planner agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
