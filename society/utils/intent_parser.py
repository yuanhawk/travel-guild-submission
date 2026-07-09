"""
intent_parser.py — M-Agentic-1 intent parser for the Travel Guild.

Design contract: the internal design spec §M-Agentic-1, §10.11.

Converts free-text travel requests into validated trip_request dicts suitable
for TravelOrchestrator.negotiate(). Runs ONCE per request — never inside the
re-plan loop.

Pipeline:
  1. LLM Step:   qwen3-max (DashScope) parses free text → QUALITATIVE structure
                 only: ordered list of {city, vibe} legs. No nights, no dates,
                 no prices from the LLM (§10.11: hard numbers NEVER from LLM).
  2. DETERMINISTIC OVERRIDE: total_nights, total_budget_cents, adults are
                 extracted from the text by regex BEFORE the LLM and OVERRIDE
                 whatever the LLM might have returned for them.
  3. VARIANCE CLAMP: validates every field against catalog; normalises values.
  4. DETERMINISTIC NUMBERS: total_nights split evenly across legs (remainder to
                 last leg, reusing planner_agent.py logic); dates derived
                 contiguously from DEFAULT_START_DATE. All per-leg nights/dates
                 are deterministic — given the same text, the output is
                 byte-identical regardless of LLM sampling.
  5. Deterministic Fallback: regex/keyword parser if LLM fails after one retry.
  6. Returns validated trip_request OR {"needs_clarification": True, "reason": "..."}.

Variance guarantee (§10.11): the FULL correctness signature —
  total_budget_cents, total_nights, leg_count, ordered cities, ordered vibes,
  all per-leg (checkin, checkout) dates — is IDENTICAL across runs for the
  same input text. Only qualitative structure (which cities/vibes, in what
  order) may come from the LLM, but even that is extracted deterministically
  from text when unambiguous ("relax then beach" → [relax, beach] → 2 legs).

Security:
  - DASHSCOPE_API_KEY read from env — NEVER hardcoded.
  - gitleaks-safe: no raw API keys or secret patterns in code.
  - LLM output NEVER passed downstream unvalidated.
  - Prompt injection returns a safe deterministic fallback (no crash, no invented city).
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

import httpx

try:
    from utils import region_expansion
except ImportError:  # imported as a top-level module (society/ on sys.path)
    import region_expansion

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — from env, never hardcoded
# ---------------------------------------------------------------------------

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
MODEL = os.environ.get("SOCIETY_LLM_MODEL", "qwen3-max")  # cheap model for test sweeps

# Default start date for deterministic date derivation when only nights given
DEFAULT_START_DATE = "2026-10-01"

# Budget-estimate defaults (var-0): honest fallback duration when the user states
# no nights, and a sentinel budget large enough not to filter any catalog row.
DEFAULT_ESTIMATE_NIGHTS: int = 5
_ESTIMATE_SENTINEL_BUDGET_CENTS: int = 9_999_999_99

# ---------------------------------------------------------------------------
# Catalog constants — single source of truth for VARIANCE CLAMP
# ---------------------------------------------------------------------------
# The bookable-city set is DATA-DRIVEN from the merchant catalog (the same
# ucp-merchant/catalog.json the Go merchant embeds and the Python store loads),
# so the free-text front door covers EXACTLY what the system can actually book —
# no more "we have a Paris hotel but the NL door declines Paris". Canonical slug
# form is LOWERCASE: the merchant matches city case-insensitively (EqualFold),
# and risk_agent.region_for_city / city_coords.json are lowercase, so a lowercase
# slug is bookable + risk-assessable + coordinate-resolvable across all paths.

# Hard-coded SEED set used ONLY if the catalog is unreadable (defensive: the
# front door must still parse the core demo itineraries rather than crash). NOT
# the source of truth when the catalog loads.
_SEED_ALLOWED_CITIES: frozenset[str] = frozenset({
    "bali", "bangkok", "singapore", "kuala lumpur",
    "perth", "busselton", "margaret-river", "pemberton", "albany",
    "ravensthorpe", "esperance", "kalgoorlie", "northam",
    "gondar", "lalibela", "semera",
})

# Pure transit/connection HUBS that have NO bookable stay-inventory in the
# catalog. These are excluded from the front door so a user cannot "book a stay"
# in a city that exists only as a re-route waypoint (recovery.py).
#
# INVARIANT (the rule that prevents the Addis-class false-decline): a hub-only
# city is excluded ONLY IF it does not appear as a real catalog city. The OSM
# expansion can add genuine stay-inventory to a former pure-hub at any time;
# _load_catalog_cities reconciles this set against the loaded catalog and DROPS
# any hub city that now has bookable hotels, so a city with inventory is ALWAYS
# bookable. (Addis Ababa gained 159 Ethiopia hotels incl. a literal 'addis
# ababa' city, so it reconciles OUT of the hub-only set and becomes bookable.)
#
# This is a DECLARED candidate set; the EFFECTIVE hub-only set after catalog
# reconciliation is _HUB_ONLY_CITIES_EFFECTIVE (see _load_catalog_cities).
_HUB_ONLY_CITIES: frozenset[str] = frozenset({"addis ababa"})


def _load_catalog_cities() -> tuple[frozenset[str], dict[str, str], frozenset[str]]:
    """
    Load the bookable city set + slug map + country containers from the merchant
    catalog (single source of truth). Returns:
      (allowed_cities_lower, slug_map, country_containers_lower)

    - allowed_cities_lower: every catalog city, lowercased, minus hub-only cities.
    - slug_map: space-form → hyphen-slug for hyphenated catalog cities
      (e.g. "margaret river" → "margaret-river", "mont saint michel" → "mont-saint-michel")
      so a user typing the natural space form still resolves to the catalog slug.
    - country_containers_lower: every catalog country, lowercased — these are
      CONTEXT regions ("a trip in Indonesia, visit Bali"), never substitutable
      destinations, so they must not trip the unknown-destination honest-decline.

    Falls back to the SEED set if the catalog is unreadable (never crashes the
    front door).
    """
    path = os.path.join(os.path.dirname(__file__), "..", "..", "ucp-merchant", "catalog.json")
    cities: set[str] = set()
    slug_map: dict[str, str] = {}
    countries: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            catalog = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(
            "intent_parser: catalog.json unreadable (%s) — falling back to SEED "
            "city set (front door degraded to core demo itineraries)", e,
        )
        return _SEED_ALLOWED_CITIES, {"kl": "kuala lumpur", "margaret river": "margaret-river"}, frozenset()

    # Pass 1 — collect EVERY catalog city (no hub filtering yet) so we can
    # reconcile the hub-only candidate set against real inventory below.
    all_catalog_cities: set[str] = set()
    for hotel in catalog:
        city = str(hotel.get("city", "")).strip().lower()
        if city:
            all_catalog_cities.add(city)
        country = str(hotel.get("country", "")).strip().lower()
        if country:
            countries.add(country)

    # RECONCILE hub-only candidates against the catalog (Addis-class fix):
    # a declared hub that actually has bookable inventory is NOT a pure transit
    # hub anymore — drop it so it becomes a bookable destination. Only hubs with
    # ZERO catalog stay-inventory remain excluded. Future catalog growth can
    # therefore never resurrect a false "not in catalog" decline for a city that
    # in fact has hotels. (sorted() for a deterministic, var-0 log line.)
    effective_hub_only = frozenset(_HUB_ONLY_CITIES - all_catalog_cities)
    resurrected = sorted(_HUB_ONLY_CITIES & all_catalog_cities)
    if resurrected:
        logger.info(
            "intent_parser: hub-only cities now have catalog inventory → bookable: %s",
            resurrected,
        )

    # Pass 2 — build the bookable set, excluding only the EFFECTIVE hub-only set.
    for city in all_catalog_cities:
        if city in effective_hub_only:
            continue
        cities.add(city)
        if "-" in city:
            slug_map[city.replace("-", " ")] = city  # accept natural space form

    if not cities:  # empty/garbage catalog → degrade safely
        logger.warning("intent_parser: catalog.json had no cities — using SEED set")
        return _SEED_ALLOWED_CITIES, {"kl": "kuala lumpur"}, frozenset(countries)

    return frozenset(cities), slug_map, frozenset(countries)


ALLOWED_CITIES, _SPACE_FORM_SLUGS, _CATALOG_COUNTRIES = _load_catalog_cities()

# Derived catalog metrics — keep computed (not hardcoded) so they stay accurate as
# the catalog grows.  Used in user-facing decline messages (D6 #58).
_CATALOG_COUNTRY_COUNT: int = len(_CATALOG_COUNTRIES) or 168  # fallback if catalog unreadable


def _load_city_to_iso2() -> dict[str, str]:
    """
    Build a deterministic city (lowercase) → ISO-3166 alpha-2 map from the catalog
    and reference/country_name_to_iso2.json. Used to set dest_country on each leg
    so compliance/health gates engage for free-text trips.

    Falls back silently to empty dict if either file is unreadable — the gate will
    then see no dest_country and emit an unverified FLAG (never a silent pass).
    """
    catalog_path = os.path.join(os.path.dirname(__file__), "..", "..", "ucp-merchant", "catalog.json")
    iso2_path = os.path.join(os.path.dirname(__file__), "..", "..", "reference", "country_name_to_iso2.json")
    try:
        with open(catalog_path, encoding="utf-8") as f:
            catalog = json.load(f)
        with open(iso2_path, encoding="utf-8") as f:
            name_to_iso2: dict[str, str] = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning("intent_parser: city→ISO2 map unreadable (%s) — dest_country will be unset", e)
        return {}

    city_to_iso2: dict[str, str] = {}
    unresolved: list[str] = []
    for hotel in catalog:
        city = str(hotel.get("city", "")).strip().lower()
        country_name = str(hotel.get("country", "")).strip()
        if not city:
            continue
        iso2 = name_to_iso2.get(country_name)
        if iso2:
            city_to_iso2.setdefault(city, iso2)
        else:
            unresolved.append(f"{city!r}({country_name!r})")
    if unresolved:
        logger.warning(
            "intent_parser: %d city/country pairs have no ISO2 mapping: %s",
            len(unresolved), unresolved[:10],
        )
    return city_to_iso2


CITY_TO_ISO2: dict[str, str] = _load_city_to_iso2()

# Free-text city name → catalog city slug. Auto-generated space-forms for
# hyphenated catalog slugs, plus the manual "kl" abbreviation.
# "denpasar" is the capital of Bali; catalog stores all Bali hotels as city="bali".
CITY_SLUG_MAP: dict[str, str] = {
    **_SPACE_FORM_SLUGS,
    "kl": "kuala lumpur",
    "denpasar": "bali",
    "denpasar island": "bali",
}

# Multi-word free-text aliases detected BEFORE single-word scanning
# (longest-match-wins). Includes every hyphenated-slug space form so the scanner
# finds e.g. "margaret river" in prose and maps it to "margaret-river".
# Region / prefecture / island names travellers commonly type INSTEAD of a city, mapped to
# that region's primary gateway city (verified in the catalog). A real, common destination
# (e.g. "Hokkaido") should plan the region's main city, not be declined; the result shows the
# actual city booked, so it stays honest. Distinctive tokens only (low false-match risk).
_REGION_TO_CITY: dict[str, str] = {
    "hokkaido": "sapporo",
    "okinawa": "naha",
    "kansai": "osaka",
    "kyushu": "fukuoka",
    "tohoku": "sendai",
    "tuscany": "florence",
    "bavaria": "munich",
    "andalusia": "seville",
    "andalucia": "seville",
    "provence": "marseille",
    "catalonia": "barcelona",
}

# Well-known NEIGHBORHOODS / DISTRICTS travellers name INSTEAD of (or alongside) the
# parent city — "stay near Gion" means Kyoto; "near Namba" means Osaka. Each maps to its
# parent CATALOG city so the request plans the real bookable city instead of tripping the
# unknown-destination honest-decline. Same pattern as the Bali/Phuket district aliases
# below, generalised to the highest-traffic city districts and structured so it stays
# extensible (add a city block as real cases surface). Curated to DISTINCTIVE tokens only
# (low false-match risk); the parent slug is always a verified catalog city.
#
# INVARIANT: a district that is ITSELF a catalog city with its own bookable inventory
# (e.g. Tokyo's Shibuya / Shinjuku, NYC's Manhattan / Brooklyn) is DELIBERATELY OMITTED
# here so it resolves to itself, never gets rewritten to the parent. When both the parent
# and a district that maps to it are named ("Kyoto ... near Gion"), the parse-level city
# de-duplication (see parse_intent) collapses the repeat so no phantom duplicate leg forms.
_NEIGHBORHOOD_TO_CITY: dict[str, str] = {
    # Osaka districts
    "namba": "osaka", "dotonbori": "osaka", "umeda": "osaka",
    "shinsaibashi": "osaka", "tennoji": "osaka",
    # Kyoto districts
    "gion": "kyoto", "arashiyama": "kyoto", "higashiyama": "kyoto",
    "pontocho": "kyoto", "fushimi": "kyoto",
    # Tokyo districts (Shibuya / Shinjuku OMITTED — own catalog cities)
    "asakusa": "tokyo", "ginza": "tokyo", "akihabara": "tokyo",
    "roppongi": "tokyo", "harajuku": "tokyo", "ueno": "tokyo",
    # Seoul districts
    "myeongdong": "seoul", "gangnam": "seoul", "hongdae": "seoul", "itaewon": "seoul",
    # Bangkok districts
    "sukhumvit": "bangkok", "silom": "bangkok",
    # Paris districts
    "montmartre": "paris", "le marais": "paris",
}

CITY_ALIASES: dict[str, str] = {
    **_SPACE_FORM_SLUGS,
    **_REGION_TO_CITY,
    **_NEIGHBORHOOD_TO_CITY,
    "kl": "kuala lumpur",
    "denpasar island": "bali",  # longer form wins: _scan_city sorts aliases by length
    "denpasar": "bali",
    # Saigon — the common colloquial/historical name for Ho Chi Minh City; a trivial
    # alias-table gap found alongside BUG 3 (review). Maps to the more specific
    # "ho chi minh city" catalog entry (the same slug "Ho Chi Minh City" text resolves to).
    "saigon": "ho chi minh city",
    # Historical / colloquial city renames — the same alias-table gap class as Saigon.
    # Each value is a verified catalog slug; "rangoon"->"yangon" is deliberately EXCLUDED
    # because "yangon" is NOT in the catalog (would still decline, so aliasing it would
    # violate the alias->bookable-catalog invariant).
    "bombay": "mumbai",
    "calcutta": "kolkata",
    "madras": "chennai",
    "peking": "beijing",
    "constantinople": "istanbul",
    "byzantium": "istanbul",
    "krung thep": "bangkok",
    # fix-round-3: "Rio" — the ubiquitous everyday colloquial short name for
    # Rio de Janeiro (a fully supported, top-tier catalog city) had no alias
    # at all, so it was flagged as an unknown destination and dropped/
    # declined even though the real city is bookable.
    "rio": "rio de janeiro",
    # Bali districts — catalog stores all as city="bali"; so "Canggu" or "Ubud" in
    # a user message (including clarification follow-ups) must resolve to "bali", not decline.
    "canggu": "bali",
    "ubud": "bali",
    "seminyak": "bali",
    "kuta": "bali",
    "jimbaran": "bali",
    "nusa dua": "bali",
    "legian": "bali",
    "sanur": "bali",
    "kerobokan": "bali",
    "uluwatu": "bali",
    # Phuket beaches — same pattern
    "patong": "phuket",
    "kata beach": "phuket",
    "bang tao": "phuket",
    "kamala": "phuket",
}


def _build_city_regex() -> tuple[re.Pattern[str], dict[str, str]]:
    """
    Build ONE combined alternation regex for all city tokens (aliases + catalog
    cities), sorted longest-first so the regex engine matches the longest token
    on overlap (e.g. "margaret river" wins over stray "river").

    Built ONCE at module load (D6 #41 — avoid O(catalog) per-call regex
    compilation). Returns (compiled_pattern, token_to_slug_map).

    var-0: tokens are sorted by (-len, token) so ties resolve deterministically
    regardless of dict/frozenset iteration order.
    """
    token_to_slug: dict[str, str] = {}
    for alias, slug in CITY_ALIASES.items():
        token_to_slug[alias] = slug
    # #15b — bilingual alias spellings (local + anglicized) from city_aliases.json,
    # so EITHER spelling resolves to the same bookable slug (keep both spellings;
    # see memory bilingual-city-names). setdefault → hand aliases / catalog win.
    try:
        _alias_path = os.path.join(os.path.dirname(__file__), "..", "city_aliases.json")
        with open(_alias_path, encoding="utf-8") as _af:
            for _alias, _slug in json.load(_af).items():
                token_to_slug.setdefault(_alias, _slug)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass  # NIT#2: malformed alias JSON falls back to catalog-only, never crashes import
    for city in ALLOWED_CITIES:
        token_to_slug.setdefault(city, CITY_SLUG_MAP.get(city, city))

    # Sort longest-first with lexical tiebreak for determinism (D6).
    sorted_tokens = sorted(token_to_slug, key=lambda t: (-len(t), t))
    alternation = "|".join(re.escape(t) for t in sorted_tokens)
    pattern = re.compile(r"\b(?:" + alternation + r")\b", re.I)
    return pattern, token_to_slug


# Pre-built at module load — shared across _scan_city_sequence / _pair_cities_with_vibes.
_CITY_RE, _CITY_TOKEN_TO_SLUG = _build_city_regex()

# #2 — Common English words that are ALSO catalog city names. A bare occurrence
# ("surprise me", "on sale", "nice beaches", "a hot bath", "mobile phone") must NOT
# route to the city; require a destination CUE — a preceding preposition/verb ("fly
# to Surprise", "a week in Nice") OR a trailing ", <region>" ("Nice, France") — so the
# city only matches when genuinely named. (Live testing booked "...the Philippines,
# nice beaches" to Nice, France — so nice/bath/mobile are cue-required now. "split" is
# handled separately by _CONNECTIVE_CITY_VERBS; "york" stays bare-matchable. "reading"
# was ALSO bare-matchable historically, but it is a very common gerund in trip-vibe
# phrasing ("a quiet week of reading, somewhere warm") and silently resolved to
# Reading, UK — cue-required now, same as nice/bath/mobile; "reading, england" /
# "reading uk" / "trip to reading" still resolve via the normal cue path.
# 2026-07-04/05 batch: "sakura" (cherry-blossom SEASON word, "for the sakura season")
# fabricated a phantom Sakura, Chiba leg on Tokyo trips; "phoenix"/"providence"/
# "orange"/"concord"/"buffalo" are common nouns/idioms ("like a phoenix", "divine
# providence", "orange groves", "reached a concord", "buffalo wings"); "charlotte"/
# "jackson"/"hamilton"/"guadalupe" are everyday given names / cultural references most
# dangerous in party-composition text ("my daughter charlotte", "my son jackson", "see
# hamilton", "our lady of guadalupe"); "florence"/"victoria"/"valencia" are also genuine
# tourist cities but equally common given names / product nouns ("aunt florence", "my
# friend victoria", "valencia orange juice") — all reproduced live as spurious extra
# legs. Cue-required now, same pattern as nice/bath/mobile/reading; cued/regioned
# forms ("to Phoenix, Arizona", "Florence, Italy", "trip to Victoria") still resolve.)
_AMBIGUOUS_CITY_WORDS: frozenset[str] = frozenset({
    "surprise", "sale", "paradise", "enterprise", "independence",
    "normal", "why", "boring", "accident", "between", "hope", "general",
    "nice", "bath", "mobile", "reading",
    "sakura", "providence", "orange", "concord", "buffalo",
    # fix-round-2: "metro" (Metro, Lampung, Indonesia) is an everyday transport
    # noun ("take the metro", "the metro system") far more often than a real
    # destination mention — fabricated a phantom Metro, Indonesia leg on any
    # single-city trip that mentioned using the subway. Its "metro"->"city"
    # vibe synonym was removed at the same time (see _VIBE_SYNONYMS) so cueing
    # it here doesn't just trade one false positive for another.
    "metro",
    # adversarial-review (2026-07-06): "phoenix"/"charlotte"/"jackson"/
    # "hamilton"/"guadalupe"/"florence"/"victoria"/"valencia" were given a
    # SEPARATE, looser suppression rule for one night (real destinations that
    # are ALSO common given names/cultural references), on the theory that
    # requiring the same strict destination-cue as "nice"/"bath" over-
    # suppressed genuine bare bookings ("florence, 5 nights, $2000") and
    # silently dropped a city from an explicit multi-city list ("rome and
    # florence, 6 nights"). That looser rule went through FOUR rounds of
    # adversarial review; each round's fix for one fabricated-leg
    # class opened a new one (round 1: coordination/duration heuristics
    # fabricated legs on ordinary person-name prose -- "meeting Jackson and
    # then flying to Tokyo"; round 2: the narrative-verb-list missed "was"
    # as a legitimate copula and wrongly suppressed real trips; round 3
    # found the SAME class again from a different angle -- "we won't skip
    # florence" / Oxford commas / "&"/"or" coordination / spelled-out
    # durations). Diagnosis: an enumerated exception list
    # (which verbs count as "a person", which connectors count as "a list")
    # is structurally unclosable -- the exact lesson this file already
    # learned from the non-Latin-script deny-list saga, just in reverse.
    # Reverted to the strict, conservative rule these words shared with
    # nice/bath/surprise before that attempt: a bare mention needs a real
    # destination cue or trailing ", Region" to resolve. This reintroduces
    # the original bare-decline/silent-list-drop complaint, but an honest
    # decline is safer than a fabricated leg -- and closes every regression
    # class the four rounds found. A real fix needs an actual person/place
    # disambiguation signal (e.g. routing through proper NER, or requiring
    # the LLM assist path), not another regex exception list. Tracked as a
    # follow-up, not reopened as a same-night patch.
    "phoenix", "charlotte", "jackson", "hamilton", "guadalupe",
    "florence", "victoria", "valencia",
})
_DEST_CUE_WORDS = (
    r"to|in|at|near|from|via|visit|visiting|explore|exploring|see|stay|"
    r"staying|holiday|vacation|trip|fly|flying|land|arrive|arriving|go|going|"
    r"toward|towards|into|"
    # fix-round-2: "decided on Thailand" / "settled on Spain" — an equally
    # unambiguous travel-DECISION cue (root cause of a silent wrong-country
    # booking when a different country was merely discussed earlier in the
    # sentence as its topic, not the decided destination).
    r"decided\s+on|settled\s+on|"
    # fix-round-4: "book me Thailand" / "planning Thailand" — common
    # booking/action verbs, missing from this cue list, caused a bare-country
    # request phrased this way to fail the destination-use gate and hard-
    # decline, even though the IDENTICAL phrasing with a catalog CITY ("book
    # me Bali") resolves fine (city hits bypass this gate entirely). "book"
    # tolerates an optional intervening "me" so the cue still reaches the
    # country name directly.
    r"book(?:\s+me)?|planning"
)
_DEST_CUE_RE = re.compile(r"(?:\b(?:" + _DEST_CUE_WORDS + r")\s+)$", re.I)
# REGION-TOUR cue: a "tour the whole region" phrasing ("10 days AROUND hokkaido", "road trip
# THROUGHOUT kyushu") signals a multi-city regional request — NOT a single gateway. Scoped to
# region expansion only (see _region_used_as_destination), so it never alters the country→gateway
# path. Trailing-anchored like _DEST_CUE_RE so it reads as the immediate lead-in to the region.
_REGION_TOUR_CUE_RE = re.compile(
    r"\b(?:around|round|all\s+(?:around|over|round)|across|through(?:out)?|tour(?:ing)?|"
    r"road[- ]?trip\w*)\s*$", re.I)
# Regional qualifier ("northern", "south central", "northeast") prefixing a COUNTRY used as a
# destination region ("northern vietnam" → its gateway city). Stripped from the gate's pre-text
# ONLY when directly after a cue/duration/start (see _substitute_country_with_city).
_REGION_QUALIFIER = (
    r"(?:north|south)(?:[-\s]?(?:east|west))?(?:ern)?|(?:east|west)(?:ern)?|central|upper|lower")
# HONESTY EXCLUDE: directional phrases that name a DISTINCT country/feature, NOT a region of the
# matched gateway country — these must DECLINE, never resolve ("north korea" ≠ korea→Seoul;
# "south china sea" is not China). Small + explicit; expand as real cases surface.
_REGION_EXCLUDE_RE = re.compile(
    r"\b(?:north korea|south china sea|east china sea|northern ireland|north macedonia|"
    r"south sudan|east timor|west bank|west papua|north cyprus)\b", re.I)


# #2 — Country → primary gateway city. When a traveller names a COUNTRY but no city
# ("a week in the Philippines"), fall back to that country's main bookable city so a
# real, common destination is not declined. Curated gateways (capital or primary tourist
# hub), ALL verified bookable. Consulted ONLY when no city was found in the text, so it
# never overrides an explicit city; the result shows the actual city, so it stays honest.
COUNTRY_PRIMARY_CITY: dict[str, str] = {
    "philippines": "manila", "japan": "tokyo", "thailand": "bangkok", "vietnam": "hanoi",
    "indonesia": "bali", "malaysia": "kuala lumpur", "singapore": "singapore",
    "south korea": "seoul", "china": "beijing", "india": "delhi", "taiwan": "taipei",
    "cambodia": "phnom penh", "sri lanka": "colombo", "nepal": "kathmandu",
    "pakistan": "lahore",  # primary cultural-tourism gateway (Lahore Fort/Shalimar/Badshahi) — L3/L4 advisory FLAG, not excluded
    "france": "paris", "italy": "rome", "spain": "barcelona", "germany": "berlin",
    "united kingdom": "london", "netherlands": "amsterdam", "portugal": "lisbon",
    "greece": "athens", "turkey": "istanbul", "switzerland": "zurich", "austria": "vienna",
    "czech republic": "prague", "united states": "new york", "canada": "toronto",
    "mexico": "mexico city", "brazil": "rio de janeiro", "argentina": "buenos aires",
    "jamaica": "kingston",  # Caribbean nation — capital; also tier-1 seed priority so the cron grounds it early
    "australia": "sydney", "new zealand": "auckland", "egypt": "cairo",
    "morocco": "marrakech", "south africa": "cape town",
    "united arab emirates": "dubai", "qatar": "doha", "jordan": "amman",
    "israel": "tel aviv", "kenya": "nairobi",
    # RC-3b: Nordic country gateways — all verified bookable in catalog (2026-06-29).
    "norway": "oslo", "sweden": "stockholm", "denmark": "copenhagen",
    "finland": "helsinki", "iceland": "reykjavik",
}
_COUNTRY_NAME_ALIASES: dict[str, str] = {
    "uk": "united kingdom", "u.k.": "united kingdom", "britain": "united kingdom",
    "great britain": "united kingdom", "usa": "united states", "u.s.": "united states",
    "u.s.a.": "united states", "the states": "united states", "uae": "united arab emirates",
    "czechia": "czech republic", "korea": "south korea",
}
_COUNTRY_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(
        list(COUNTRY_PRIMARY_CITY) + list(_COUNTRY_NAME_ALIASES),
        key=lambda s: (-len(s), s))) + r")\b",
    re.I,
)

# fix-round-1: the DESTINATION-USE GATE below (_is_subject/_has_cue/_dur_before)
# only inspects the text BEFORE the matched country, so a country name that is
# also an English homonym used as a noun-MODIFIER of the following word ("turkey
# hunting" — the bird, "china shop"/"china painting" — crockery/porcelain) still
# gets substituted whenever a duration or cue happens to sit before it ("spent 3
# weeks turkey hunting..."). Curated, closed set of following nouns that turn the
# country word into an ordinary noun phrase rather than a destination — checked
# AFTER the country match, regardless of what precedes it.
_COUNTRY_HOMONYM_FOLLOWING_RE = re.compile(
    r"^\s*(?:hunting|sandwich(?:es)?|dinner|leg|breast|bacon|burger|meat|gravy|"
    r"shop|shops|cabinet|pattern|painting|plate|plates|dish|dishes|doll|dolls|"
    r"set|teacup|teacups|vase|vases)\b",
    re.I,
)


# Activity-implied COMPANION city: an activity, in a given gateway country, implies a specific
# well-known companion catalog city as an extra leg (a Vietnam "cruise" out of the Hanoi gateway
# is a Ha Long Bay cruise). HONESTY: the companion MUST be a real catalog city (enforced at module
# load below) and is added ONLY on an exact (gateway, activity) hit — never fabricated from a bare
# activity word. The activity token is NOT a vibe; the leg books AS the canonical ALLOWED_VIBE in
# the map value. Keyed (gateway_city_slug, activity_token).
_COMPANION_CITY_BY_ACTIVITY: dict[tuple[str, str], tuple[str, str]] = {
    ("hanoi", "cruise"): ("ha long", "cruise"),   # Ha Long Bay cruise (catalog: ha long ✓)
    # ("cairo", "cruise"): ("luxor", "culture"),  # Nile cruise — enable after catalog re-verify
}
# Honesty hard-stop: drop any entry whose companion is not a bookable catalog city, so a stale
# map can never fabricate a destination.
_COMPANION_CITY_BY_ACTIVITY = {
    k: v for k, v in _COMPANION_CITY_BY_ACTIVITY.items() if v[0] in ALLOWED_CITIES
}
# Distinct activity trigger tokens, sorted for deterministic scan order (var-0).
_COMPANION_ACTIVITY_TOKENS: tuple[str, ...] = tuple(
    sorted({_a for (_, _a) in _COMPANION_CITY_BY_ACTIVITY}))


def _country_primary_city(text: str) -> str | None:
    """If the text names a supported COUNTRY, return that country's primary gateway city,
    else None. Deterministic, longest-match-first. Callers use this ONLY when no city was
    found, so it never overrides an explicitly named city."""
    m = _COUNTRY_RE.search(text.lower())
    if not m:
        return None
    name = _COUNTRY_NAME_ALIASES.get(m.group(1), m.group(1))
    return COUNTRY_PRIMARY_CITY.get(name)


def _substitute_country_with_city(text: str) -> str:
    """Replace a named COUNTRY with its primary gateway city IN PLACE, so EVERY downstream
    text scan (vibe pairing, single-city leg build) sees a real city — setting the city list
    alone isn't enough, those scans re-read the text. Returns text unchanged if no supported
    country is named. Span positions match the original text (lower() preserves length)."""
    lowered = text.lower()
    # HONESTY EXCLUDE: a directional prefix can name a DISTINCT country/feature, not a region of
    # the matched gateway country ("north korea" ≠ korea→Seoul; "south china sea" is not China).
    # Such phrases must DECLINE — leave the text unchanged so no city is fabricated.
    if _REGION_EXCLUDE_RE.search(lowered):
        return text
    # fix-round-2: evaluate EVERY country mention in the text (not just the
    # first _COUNTRY_RE match) and prefer one with an explicit travel CUE or
    # a duration right before it over one that is merely the grammatical
    # SUBJECT of the sentence. A sentence can open by naming/discussing a
    # country as its topic without that being the destination at all
    # ("France is overrated, fly me to Portugal instead", "Japan's temples
    # are stunning, but let's go to Thailand") — the bare-subject heuristic
    # alone previously grabbed the FIRST such country regardless of a much
    # stronger, explicitly-cued mention of a DIFFERENT country later in the
    # same sentence, silently booking the wrong country.
    _strong_candidate = None
    _subject_candidate = None
    for m in _COUNTRY_RE.finditer(lowered):
        # DESTINATION-USE GATE: only substitute when the country is used as a destination —
        # either the trip SUBJECT (nothing but a leading article before it: "japan for a week",
        # "the philippines …") OR preceded by a travel cue, allowing one article in between
        # ("a week in thailand", "in the philippines", "visit turkey"). This keeps every country
        # usable as a real destination while NOT routing "my friend jordan" or "cook a turkey".
        pre = lowered[:m.start()].rstrip()
        pre_core = re.sub(r"\s+(the|a|an)$", "", pre)
        # Strip a trailing regional-qualifier chain ("northern", "south central", "northeast") ONLY
        # when it sits DIRECTLY after a destination cue, a duration, or the start — so "in northern
        # vietnam" / bare-subject "southern italy" / "8 days northern vietnam" resolve, but prose
        # where an article or other word precedes the qualifier ("from the central india branch",
        # "the lower japan office") is NOT mistaken for a destination (honesty: no fabricated city).
        _qm = re.search(
            r"(?P<keep>^|\b(?:" + _DEST_CUE_WORDS + r")\s+|\b\d+\s*(?:days?|nights?|weeks?)\s+)"
            r"(?:" + _REGION_QUALIFIER + r")(?:\s+(?:" + _REGION_QUALIFIER + r"))*$",
            pre_core)
        # When the qualifier matched, track where the qualifier starts in the ORIGINAL text
        # so we can strip it from the output too (not just from pre_core for the validity check).
        # Positions in pre_core correspond 1:1 to positions in text because:
        #   pre_core = lowered[:m.start()].rstrip() minus a trailing "the/a/an" article —
        #   and article removal never fires when _qm matches (qualifier occupies the trailing
        #   position instead), so len(pre_core) == len(lowered[:m.start()].rstrip()).
        #   rstrip() only removes from the right, preserving left-side indices.
        _qual_start_in_text: int | None = (
            _qm.start() + len(_qm.group("keep")) if _qm else None
        )
        if _qm:
            pre_core = (pre_core[:_qm.start()] + _qm.group("keep")).rstrip()
        # fix-round-3: a possessive GENITIVE ("Japan's temples are
        # stunning") makes the country a noun-MODIFIER of what follows, not
        # a destination — topical prose about a country, not a trip to it.
        # _COUNTRY_RE's \b boundary matches "japan" inside "japan's" (the
        # apostrophe is a non-word char), and a possessive at sentence start
        # otherwise satisfies the bare-subject heuristic below, so without
        # this check a purely topical remark got silently rewritten to the
        # country's gateway city and booked as a real trip.
        _is_possessive_topic = bool(re.match(r"['’]s\b", lowered[m.end():]))
        _is_subject = (
            pre_core.strip() in ("", "the", "a", "an") and not _is_possessive_topic
        )
        _has_cue = bool(_DEST_CUE_RE.search((pre_core + " ")[-25:]))
        # a duration phrase right before the country is also destination use ("8 days italy",
        # "two weeks vietnam") — but NOT "cook a turkey, 5 days" (there the duration trails).
        _dur_before = bool(re.search(
            r"(?:\d+|\b(?:a|one|two|three|four|few|couple(?:\s+of)?))\s*(?:days?|nights?|weeks?)$",
            pre_core))
        if not (_is_subject or _has_cue or _dur_before):
            continue
        # fix-round-1: the country name is a noun-modifier of the FOLLOWING word
        # ("turkey hunting", "china shop"/"china painting") — a common-noun use,
        # not a destination, regardless of what precedes it (a duration/cue before
        # the word is exactly what "spent 3 weeks turkey hunting" has).
        if _COUNTRY_HOMONYM_FOLLOWING_RE.match(lowered[m.end():]):
            continue
        name = _COUNTRY_NAME_ALIASES.get(m.group(1), m.group(1))
        city = COUNTRY_PRIMARY_CITY.get(name)
        if not city:
            continue
        candidate = (m, city, _qual_start_in_text)
        if _has_cue or _dur_before:
            # First (document-order) strongly-cued match wins outright.
            _strong_candidate = candidate
            break
        if _subject_candidate is None:
            _subject_candidate = candidate

    chosen = _strong_candidate or _subject_candidate
    if chosen is None:
        return text
    m, city, _qual_start_in_text = chosen
    # fix-round-4: an explicit "<Country> or <Country>" disjunction ("Thailand
    # or Vietnam") means the traveler has NOT decided between two options —
    # the loop above only ever considers the FIRST option a destination-use
    # candidate (the second is not travel-cued and is skipped), so this
    # silently committed to the first country with no signal that a choice
    # was made for the user. That is exactly the topic-vs-destination
    # consent failure this gate exists to prevent, so decline (leave the
    # text unchanged) instead of silently picking one.
    _or_m = re.match(r"\s+or\s+", lowered[m.end():])
    if _or_m and _COUNTRY_RE.match(lowered[m.end() + _or_m.end():]):
        return text
    # Strip the regional qualifier from the output text when one was found — otherwise
    # "9 days in Northern Vietnam" → "9 days in Northern hanoi" and _scan_unknown_place
    # flags "Northern" as an unknown destination (the original bug). When no qualifier was
    # matched, fall back to the full pre-text as before.
    _pre_end = _qual_start_in_text if _qual_start_in_text is not None else m.start()
    return text[:_pre_end] + city + text[m.end():]


# BUG 1 (review, most severe — silent wrong-country resolve): a catalog CITY can
# coincidentally share its exact NAME with a supported COUNTRY — "jamaica" is a real
# catalog entry for Queens, NY, but "jamaica" is ALSO a COUNTRY_PRIMARY_CITY key (the
# Caribbean nation, gateway Kingston). Because _scan_city_sequence finds the Queens
# catalog match FIRST, the country-fallback path (_substitute_country_with_city, which
# only fires when NO city was found) never gets consulted, and "a week in Jamaica,
# $3000" silently resolves to Queens, NY (dest_country defaults to US) instead of
# Kingston, Jamaica — a SILENT WRONG-COUNTRY resolve, not an honest decline.
#
# Fix: after the city scan, when a resolved catalog city's name EXACTLY equals (case-
# insensitive) a COUNTRY_PRIMARY_CITY key whose real gateway is a DIFFERENT city, prefer
# the country's gateway — UNLESS the text carries a cue that disambiguates toward the
# specific coincidentally-named catalog place (state/borough/US-context, e.g. "Jamaica,
# Queens" / "Jamaica, NY" / "Jamaica, USA"). The only such collision in the current
# catalog is a US neighbourhood-vs-country pair, so the cue set is US-context; extend it
# if the catalog grows more collisions.
_COUNTRY_CITY_DISAMBIGUATION_CUES: tuple[str, ...] = (
    "usa", "u.s.a", "u.s.", "united states", "the states",
    "new york", "nyc", "ny", "n.y.",
    "queens", "brooklyn", "bronx", "manhattan", "staten island", "borough",
)


def _disambiguate_country_city_collision(text: str, city_seq: list[str]) -> list[str]:
    """
    Resolve a catalog-city / country-name COLLISION (see BUG 1 comment above) toward the
    country's real gateway city, unless the text disambiguates toward the specific
    coincidentally-named catalog place.

    Conservative by construction: only touches a slug that is an EXACT
    COUNTRY_PRIMARY_CITY key AND whose gateway differs from itself (i.e. a genuine
    name collision, not e.g. "singapore" where city == country gateway already).
    Every other city is passed through untouched. var-0: pure function of text + city_seq.
    """
    if not city_seq:
        return city_seq
    lowered = text.lower()
    has_disambiguation_cue = any(
        re.search(r"\b" + re.escape(cue) + r"\b", lowered)
        for cue in _COUNTRY_CITY_DISAMBIGUATION_CUES
    )
    out: list[str] = []
    for slug in city_seq:
        gateway = COUNTRY_PRIMARY_CITY.get(slug)
        if gateway and gateway != slug and not has_disambiguation_cue:
            logger.info(
                "intent_parser: catalog city %r collides with country name — no "
                "disambiguating cue found, preferring country gateway %r",
                slug, gateway,
            )
            out.append(gateway)
        else:
            out.append(slug)
    return out


# ---------------------------------------------------------------------------
# Currency rider (§16.1 front-door currency fracture)
# ---------------------------------------------------------------------------
# LIGHT, SEEDED, provenance-tagged FX table. Closes the currency fracture at
# the front door so a non-USD budget ("AUD 3000", "฿50,000") normalises
# DETERMINISTICALLY to USD-cents. This is NOT the full provider FX seam (§18 /
# §19.5) — that is a later build. Rates are fixed/seeded for reproducibility
# (variance-0); an unknown currency is an HONEST-DECLINE, never a silent
# treat-as-USD.
#
# Provenance: indicative mid-market rates, captured 2026-01 (seeded snapshot).
# rate = USD per 1 unit of the foreign currency.
SEEDED_FX_USD_PER_UNIT: dict[str, float] = {
    "usd": 1.0,
    "aud": 0.66,   # Australian dollar  (WA corpus)
    "thb": 0.029,  # Thai baht          (Bangkok corpus)
    "sgd": 0.74,   # Singapore dollar
    "myr": 0.22,   # Malaysian ringgit  (KL corpus)
    "idr": 0.000063,  # Indonesian rupiah (Bali corpus)
    "etb": 0.018,  # Ethiopian birr     (Ethiopia corpus)
    "eur": 1.08,
    "gbp": 1.27,
}
FX_PROVENANCE = "seeded-2026-01 mid-market snapshot (intent_parser.SEEDED_FX_USD_PER_UNIT)"

# Currency symbol → ISO code. "$" is intentionally ambiguous and treated as USD
# at the front door (the dominant corpus + merchant currency); a user wanting
# AUD must write "AUD".
CURRENCY_SYMBOLS: dict[str, str] = {
    "$": "usd",
    "฿": "thb",
    "rp": "idr",
    "rm": "myr",
    "€": "eur",
    "£": "gbp",
    "br": "etb",
}

# ISO codes / words we recognise as currencies in free text (for decline of
# UNKNOWN currencies vs. silent USD treatment).
_KNOWN_CURRENCY_CODES: frozenset[str] = frozenset(SEEDED_FX_USD_PER_UNIT.keys())

ALLOWED_VIBES: frozenset[str] = frozenset({
    "culture",
    "beach",
    "surf",
    "relax",
    "city",
    "adventure",
    "luxury",
    "cruise",
    "hike",
})

# Vibe tokens that are ALSO kept as activity interests (so the day-planner still surfaces the
# matching trail/peak/viewpoint POIs). hiking/trekking map to the "hike" vibe AND drive the POI
# layer — same dual role as beach/surf. Without this they'd be silently dropped from interests.
_VIBE_ALSO_INTEREST: frozenset[str] = frozenset({"hiking", "trekking", "trek"})


# ---------------------------------------------------------------------------
# Activity intent capture — Phase 1 (interests field on legs)
#
# Capture rule: a CURATED, BOUNDED set of free-text activity tokens that are
# NOT consumed as a vibe and NOT part of the companion-city trigger vocabulary.
# Words are matched as whole words (word-boundary scan, lowercase). Tokens are
# deliberately sparse and low-false-positive — this is NOT an exhaustive NLP
# extraction; it only captures well-known activity keywords that have direct
# evidence in the POI catalog or the lexicon below.
#
# var-0: _scan_interests() sorts hits by (position, token) → always the same
# ordered list for the same text. No LLM, no set iteration, no floats.
# ---------------------------------------------------------------------------
_ACTIVITY_INTEREST_TOKENS: frozenset[str] = frozenset({
    # Catalog-verified (natural=hot_spring, amenity=place_of_worship, leisure=*)
    "onsen", "hot spring", "hot springs",
    "temple", "shrine", "pagoda",
    "garden", "park",
    # Cherry / blossom (name-level evidence in JP, TW catalogs)
    "hanami", "cherry blossom", "sakura",
    # Geophysical / adventure (natural=cave_entrance, natural=peak, natural=waterfall etc.)
    "cave", "caves", "ice cave",
    "aurora", "northern lights",
    "glacier", "volcano", "waterfall",
    "snorkeling", "diving", "scuba",
    "hiking", "trekking", "trek",
    # Cultural
    "museum", "gallery", "art",
    "castle", "ruins", "monastery",
    # Coastal / water
    "beach", "surfing", "surf",
    # Culinary
    "street food", "food tour",
    # Wellness
    "spa", "yoga",
})

# Multi-word tokens sorted longest-first for greedy matching (same pattern as
# the city scanner — avoids "hot" absorbing "hot spring").
_ACTIVITY_INTEREST_RE: re.Pattern[str] = re.compile(
    r"\b(?:"
    + "|".join(
        re.escape(t)
        for t in sorted(_ACTIVITY_INTEREST_TOKENS, key=lambda s: (-len(s), s))
    )
    + r")\b",
    re.I,
)


def _scan_interests(text: str) -> list[str]:
    """
    Scan *text* for free-text activity intent tokens NOT consumed as a vibe.

    Returns a deterministic, deduped, sorted list of lowercase interest tokens.
    The sort (alphabetical) removes any position dependency so two calls with
    the same text always produce byte-identical output (var-0).

    Rule: word-boundary match against _ACTIVITY_INTEREST_TOKENS. Multi-word
    tokens win over single-word prefixes (longest-first alternation in the RE).
    Tokens that map to an ALLOWED_VIBE are EXCLUDED — vibes are already handled
    by _scan_vibe_sequence; interests are the COMPLEMENTARY activity layer.
    """
    lowered = text.lower()
    found: set[str] = set()
    for m in _ACTIVITY_INTEREST_RE.finditer(lowered):
        token = m.group(0).strip()
        # Exclude tokens that are themselves an allowed vibe (or vibe synonym) —
        # those are already part of the vibe layer, not the interest layer.
        if (token in ALLOWED_VIBES or token in _VIBE_SYNONYMS) and token not in _VIBE_ALSO_INTEREST:
            continue
        found.add(token)
    # Deterministic: sorted alphabetically (no position order leaks into output).
    return sorted(found)


# ---------------------------------------------------------------------------
# Activity → catalog-evidence lexicon — Phase 2
#
# Maps a captured interest token to the catalog-level substrings that represent
# that activity in POI categories / names. Expansion is ONE-WAY (interest token
# → catalog terms); it can NEVER invent a POI. An unmapped token passes through
# unchanged (so the raw word is still tried as a substring).
#
# Catalog-verified target categories (from poi_catalog.json full scan):
#   natural=hot_spring, natural=spring, leisure=garden, leisure=park,
#   natural=cave_entrance, natural=peak, natural=waterfall, natural=volcano,
#   amenity=place_of_worship, tourism=museum, tourism=gallery,
#   historic=castle, historic=ruins, historic=monastery,
#   natural=beach, leisure=beach_resort, tourism=theme_park, tourism=zoo
#
# The map is BOUNDED and AUDITABLE — fewer than 20 entries. New entries require
# a catalog evidence comment (category or name substring verified present).
# ---------------------------------------------------------------------------
_ACTIVITY_CATALOG_TERMS: dict[str, tuple[str, ...]] = {
    # natural=hot_spring (~counted in catalog); "onsen" also appears in JP names.
    "onsen":          ("onsen", "hot_spring", "hot spring"),
    "hot spring":     ("hot_spring", "hot spring", "onsen"),
    "hot springs":    ("hot_spring", "hot spring", "onsen"),

    # Cherry / blossom — hanami happens in parks & gardens; rely on the leisure=garden/park
    # category + the SPECIFIC multi-word "cherry blossom"/"sakura" names. Bare "cherry"/"blossom"
    # are dropped — as whole words they collide with unrelated POIs ("Blossom Faith Church") and
    # would mislead; gardens/parks still catch the real hanami venues.
    "hanami":         ("cherry blossom", "cherry_blossom", "sakura", "garden", "park"),
    "cherry blossom": ("cherry blossom", "cherry_blossom", "sakura", "garden", "park"),
    "sakura":         ("sakura", "cherry_blossom", "garden", "park"),

    # Cave: natural=cave_entrance. "ice cave" is deliberately NOT mapped — a generic cave is not
    # an ice cave and the catalog has zero ice-cave/glacier evidence, so "ice cave" must fall
    # through to the honest "not in verified catalog" note rather than match e.g. "Cave Church".
    "cave":           ("cave",),
    "caves":          ("cave",),

    # Aurora — a sky phenomenon with NO bookable POI in OSM. Map only to the SPECIFIC phrases so
    # it does NOT collide with "Nova Aurora" churches / an "Aurora" cinema (bare "aurora" is a
    # common name word). In practice these match nothing → honest note, which is the truth.
    "aurora":         ("aurora borealis",),
    "northern lights": ("aurora borealis", "northern lights"),

    # Geophysical adventure: natural=peak, natural=waterfall, natural=volcano.
    "glacier":        ("glacier",),
    "volcano":        ("volcano",),
    "waterfall":      ("waterfall",),

    # Water activities — map ONLY to activity-specific terms. A generic beach, an aquarium, or the
    # bare word "marine" (collides with "Tokio Marine Hall") would mislead, so only the
    # dive/reef/snorkel terms remain; falls to the honest note when the city has no such POIs.
    "snorkeling":     ("snorkel", "reef"),
    "diving":         ("dive", "reef"),
    "scuba":          ("dive", "reef"),

    # Hiking: natural=peak, leisure=nature_reserve, tourism=viewpoint.
    "hiking":         ("trail", "peak", "nature_reserve", "viewpoint"),
    "trekking":       ("trail", "peak", "nature_reserve", "viewpoint"),
    "trek":           ("trail", "peak", "nature_reserve", "viewpoint"),

    # Cultural: already well-covered by vibe="culture" but gives concrete terms.
    "temple":         ("temple", "place_of_worship", "shrine", "pagoda"),
    "shrine":         ("shrine", "place_of_worship", "temple"),
    "pagoda":         ("pagoda", "temple", "place_of_worship"),
    "castle":         ("castle",),
    "ruins":          ("ruins", "archaeological_site"),
    "monastery":      ("monastery",),

    # Garden / park: leisure=garden, leisure=park.
    "garden":         ("garden",),
    "park":           ("park",),

    # Museum / gallery: tourism=museum, tourism=gallery, amenity=arts_centre.
    "museum":         ("museum",),
    "gallery":        ("gallery", "arts_centre"),
    "art":            ("gallery", "arts_centre", "museum"),

    # Beach / surf: natural=beach, leisure=beach_resort. (beach & surf are also vibes, usually
    # consumed by the vibe layer.) Surf maps only to surf-specific terms — a generic beach is not
    # a surf break, so it is excluded.
    "beach":          ("beach", "beach_resort"),
    "surfing":        ("surf",),
    "surf":           ("surf",),

    # Street food / food tour: market is the reliable signal; bare "food" is dropped (whole-word
    # collision with "Food Godown" mosque etc.). "street_food" rarely matches a category → note.
    "street food":    ("street_food", "market"),
    "food tour":      ("market",),

    # Wellness.
    "spa":            ("spa", "hot_spring", "wellness"),
    "yoga":           ("yoga", "wellness"),
}


def _expand_interests(interests: list[str]) -> list[str]:
    """
    Expand interest tokens via _ACTIVITY_CATALOG_TERMS into the catalog substrings
    they represent. Unmapped tokens pass through unchanged.

    Returns a deterministic, deduped, sorted list (var-0: no set iteration order
    leaks; sorted() makes the output order input-independent).

    This expansion is parser-side so legs carry expanded tokens into
    build_day_plan._interest_score; the day-planner never needs to know the map.
    """
    expanded: set[str] = set()
    for token in interests:
        mapped = _ACTIVITY_CATALOG_TERMS.get(token)
        if mapped:
            expanded.update(mapped)
        else:
            expanded.add(token)  # unmapped: pass raw token through
    return sorted(expanded)


def _interest_term_map(interests: list[str]) -> dict[str, list[str]]:
    """Map each ORIGINAL interest token to its expanded catalog terms.

    {original_interest: sorted[expanded_terms]}. Unmapped tokens map to
    themselves. Carried onto legs alongside the flat ``interests`` list so the
    day-planner can emit its honest unsatisfiable-activity note PER ORIGINAL
    INTEREST (flag only when NONE of an interest's terms have catalog evidence)
    instead of flagging individual expansion synonyms the user never typed.

    var-0: keys come from the already-sorted scan order; each value is sorted.
    """
    out: dict[str, list[str]] = {}
    for token in interests:
        mapped = _ACTIVITY_CATALOG_TERMS.get(token)
        out[token] = sorted(mapped) if mapped else [token]
    return out


# ---------------------------------------------------------------------------
# #26 Interactive Elicitation — frozen closed-set slot vocabulary.
#
# When a free-text trip_request is UNDERSPECIFIED (a load-bearing slot is empty
# such that no sensible plan can be formed), parse_intent returns a structured,
# machine-readable ``elicitation`` object ALONGSIDE the existing ``reason`` so a
# front can ask ONE targeted question instead of guessing or dead-ending in
# prose. The slot key is always one of these frozen string constants (mirrors
# insurance_agent.CoverageStatus) — an unrecognised gap degrades to the prose
# decline with NO slot invented (HONESTY: elicit-or-flag, never fabricate).
#
# DETERMINISM (var-0): _build_elicitation is a PURE function of its arguments
# plus module-load constants. Every emitted list is produced via sorted() (like
# the existing multi-area gate), so the same text yields a byte-identical
# envelope. No LLM, no wall-clock, no random, no set-iteration on the output.
#
# APPEND-ONLY: the elicitation object is a SUPERSET — it is only attached to
# branches that ALREADY return needs_clarification, and the existing ``reason``
# (and ``preference_prompt`` for the AREA case) is preserved verbatim. A
# fully-specified request returns a trip_request with NO elicitation key.
# ---------------------------------------------------------------------------
class ElicitationSlot:
    """Frozen closed-set of the load-bearing slots an elicitation can target."""

    DESTINATION = "destination"
    BUDGET = "budget"
    DURATION = "duration"
    AREA = "area"
    CURRENCY = "currency"
    TOO_MANY_CITIES = "too_many_cities"
    DATE = "date"   # RC-5(b): user asks WHEN to travel rather than stating a date


# Every slot the engine is allowed to emit. A gap that does not map to one of
# these is surfaced as a prose decline with NO ``elicitation`` key (the slot is
# omitted rather than invented), exactly like _normalise_vibe drops out-of-set
# tokens. Used by tests as the closed-set guard.
_ELICITATION_SLOTS: frozenset[str] = frozenset({
    ElicitationSlot.DESTINATION,
    ElicitationSlot.BUDGET,
    ElicitationSlot.DURATION,
    ElicitationSlot.AREA,
    ElicitationSlot.CURRENCY,
    ElicitationSlot.TOO_MANY_CITIES,
    ElicitationSlot.DATE,   # RC-5(b): date-inquiry clarification
})

# The three load-bearing slots whose presence/absence we report in every
# elicitation envelope as satisfied_slots / missing_slots. Frozen + sorted at
# emit time so the envelope is byte-identical for identical input.
_CORE_SLOTS: tuple[str, ...] = (
    ElicitationSlot.DESTINATION,
    ElicitationSlot.BUDGET,
    ElicitationSlot.DURATION,
)


def _build_elicitation(
    slot: str,
    question: str,
    *,
    choices: list[str] | None = None,
    examples: list[str] | None = None,
    has_destination: bool = False,
    has_budget: bool = False,
    has_duration: bool = False,
) -> dict | None:
    """
    Build a deterministic, machine-readable ElicitationRequest object.

    PURE function (var-0): output depends only on the arguments + the frozen
    _CORE_SLOTS constant. Choices/examples are emitted via sorted() so the
    envelope is byte-identical across runs for identical input.

    Returns None when ``slot`` is NOT in the frozen closed set — the caller then
    omits the elicitation key and surfaces the prose decline only (HONESTY:
    never invent a slot we do not recognise).

    Args:
        slot:            One of ElicitationSlot.* (else None is returned).
        question:        The single targeted question to ask the user.
        choices:         Optional small set of valid answers (sorted on emit).
        examples:        Optional example phrasings (sorted on emit).
        has_*:           Which load-bearing slots are already satisfied; used to
                         populate satisfied_slots / missing_slots.
    """
    if slot not in _ELICITATION_SLOTS:
        # Closed-set guard: unrecognised gap → no structured slot (never invent).
        return None

    _present = {
        ElicitationSlot.DESTINATION: has_destination,
        ElicitationSlot.BUDGET: has_budget,
        ElicitationSlot.DURATION: has_duration,
    }
    satisfied = sorted(s for s in _CORE_SLOTS if _present.get(s))
    missing = sorted(s for s in _CORE_SLOTS if not _present.get(s))

    elic: dict = {
        "slot": slot,
        "question": question,
        "satisfied_slots": satisfied,
        "missing_slots": missing,
    }
    if choices is not None:
        elic["choices"] = sorted(choices)
    if examples is not None:
        elic["examples"] = sorted(examples)
    return elic


# Synonym / fuzzy map for vibe normalisation (lowercase → canonical vibe)
_VIBE_SYNONYMS: dict[str, str] = {
    # culture
    "cultural": "culture",
    "historical": "culture",
    "temple": "culture",
    # fix-round-3: the plural "temples" (the more natural phrasing than the
    # singular) was entirely absent — the vibe scan uses a \bword-boundary\b
    # regex, so the singular "temple" entry cannot match "temples", silently
    # dropping an explicit, unambiguous culture signal (e.g. "temples in
    # Kyoto") even when explicitly bound to a city. Other vibe families
    # already carry their plurals ("beaches", "waves"), so this closes an
    # inconsistent lexical gap.
    # NOTE: "museum"/"museums" were deliberately NOT added here — unlike
    # "temple", "museum" is ALSO a distinct _ACTIVITY_INTEREST_TOKENS entry
    # with its own separately-tested contract (test_museum_captured expects
    # "Berlin museum week" to surface "museum" as an INTEREST, not be
    # silently consumed as a vibe). Promoting it to a vibe synonym here
    # would regress that existing behaviour; left for a follow-up that
    # reconciles the two vocabularies together.
    "temples": "culture",
    "art": "culture",
    # beach
    "beaches": "beach",
    "beachfront": "beach",
    "beachy": "beach",
    "seaside": "beach",
    "ocean": "beach",
    "sea": "beach",
    "coastal": "beach",
    # surf
    "surfing": "surf",
    "waves": "surf",
    "surfer": "surf",
    # relax
    "relaxing": "relax",
    "relaxed": "relax",
    "relaxation": "relax",
    "chill": "relax",
    "chilling": "relax",
    "spa": "relax",
    "peaceful": "relax",
    "quiet": "relax",
    "wellness": "relax",
    "forest": "relax",   # Bali Ubud = jungle/nature retreat
    "nature": "relax",
    "rainforest": "relax",
    # city
    "urban": "city",
    "downtown": "city",
    "citybreak": "city",
    # fix-round-2: "metro" REMOVED as a vibe synonym. It collides with the
    # extremely common transport noun ("take the metro", "the metro system")
    # far more often than it's used to mean "metropolitan/city" — and adding
    # "metro" to _AMBIGUOUS_CITY_WORDS (to stop it bare-matching as a phantom
    # Metro, Indonesia leg) would have UNMASKED this synonym, turning "take
    # the metro" into a spurious vibe=city instead. See _AMBIGUOUS_CITY_WORDS.
    "nightlife": "city",
    "shopping": "city",
    # adventure
    # hike — dedicated vibe for trail/trekking trips (also kept as interests via _VIBE_ALSO_INTEREST)
    "hiking": "hike",
    "trekking": "hike",
    "trek": "hike",
    "hikes": "hike",
    "hill walking": "hike",
    "rambling": "hike",
    "day hike": "hike",
    "jungle": "adventure",
    "outdoor": "adventure",
    "outdoors": "adventure",
    "active": "adventure",
    # luxury
    "luxurious": "luxury",
    "premium": "luxury",
    "five-star": "luxury",
    "5-star": "luxury",
    "upscale": "luxury",
    "high-end": "luxury",
    # cruise (Ha Long overnight cruises, river cruises, etc.)
    "cruising": "cruise",
    "cruises": "cruise",
    "river cruise": "cruise",
    "boat cruise": "cruise",
    "overnight cruise": "cruise",
}

# ---------------------------------------------------------------------------
# LLM system prompt — QUALITATIVE STRUCTURE ONLY (§10.11)
# ---------------------------------------------------------------------------
# CRITICAL: The LLM outputs ONLY qualitative structure (which cities/vibes,
# in what order). It MUST NOT output nights, dates, budget, or adults —
# those are determined deterministically from the text and OVERRIDE anything
# the LLM might return. This eliminates per-leg night/date variance entirely.

_LLM_SYSTEM_PROMPT = """You are a travel intent parser. Parse the user's travel request into PURE JSON only.

Output ONLY valid JSON with NO markdown, NO explanation, NO code blocks — just the raw JSON object.

IMPORTANT: Output ONLY the qualitative trip structure — which city/cities and vibe(s), in order.
Do NOT output budget, nights, dates, or adults — those are determined separately.

This is a MULTI-CITY parser. If the user names several cities (e.g.
"Bangkok then Kuala Lumpur then Singapore" or "Perth, Margaret River, Albany"),
output ONE leg per city, in the ORDER the user states them.

Required JSON schema:
{
  "legs": [
    {
      "city": <string, the destination city EXACTLY as the user names it, lowercased>,
      "vibe": <string, one of the allowed vibes below — omit if unclear>
    }
  ]
}

Allowed vibes: culture, beach, surf, relax, city, adventure, luxury

Rules:
- city: extract the destination city name the user actually wrote, lowercased
  (e.g. "Paris" → "paris", "Siem Reap" → "siem reap"). NEVER invent or substitute
  a city the user did not name. Do NOT translate, expand, or "correct" the name.
  The downstream system validates each city against its booking catalog — your job
  is faithful extraction, NOT validation. When unsure of the exact name, copy the
  user's spelling verbatim (lowercased).
- vibe MUST be one of the allowed vibes — or omit the vibe field
- Do NOT include budget, nights, dates, or adults in your output — omit those fields entirely
- MULTI-CITY: one leg per city, in stated order (e.g. "Bangkok, KL, Singapore" → 3 legs)
- If a single-city trip has multiple vibes ("relax then beach"), split into multiple legs
  with the SAME city, preserving the stated vibe order
- Leg count must be 1 to 5
- If you cannot identify ANY destination city, output:
  {"needs_clarification": true, "reason": "..."}.

Note: the deterministic layer re-derives city topology from the request text and
overrides your output when the text is unambiguous, so faithful extraction (not
guessing) is always the safe choice.
"""

# ---------------------------------------------------------------------------
# VARIANCE CLAMP helpers
# ---------------------------------------------------------------------------


def _parse_budget_cents(raw: Any) -> int | None:
    """
    Parse a budget value in various formats to integer cents.

    Accepts: int/float (treated as dollars), str like "$1,500" / "$1500" / "1500".
    Returns None if unparseable or non-positive.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val <= 0:
            return None
        # If the value looks like it's already in cents (very large number), keep as-is.
        # Heuristic: values >= 10000 without explicit dollar sign are ambiguous;
        # we treat them as dollars * 100 only if < 100000 (i.e., < $1000 as dollars would be < 10000¢).
        # Always trust the LLM to give us cents directly when field is total_budget_cents.
        return int(round(val))
    if isinstance(raw, str):
        # Strip currency symbols, commas, whitespace
        cleaned = re.sub(r"[$,\s]", "", raw)
        # Try to find a number
        m = re.search(r"[\d]+(?:\.\d+)?", cleaned)
        if not m:
            return None
        val = float(m.group(0))
        if val <= 0:
            return None
        # Heuristic: if the original contained "$" or "dollars"/"usd", it's dollars
        if "$" in str(raw) or re.search(r"\b(?:dollars?|usd)\b", str(raw), re.I):
            return int(round(val * 100))
        # Otherwise assume the LLM already gave cents (for total_budget_cents field)
        return int(round(val))
    return None


def _normalise_city(raw: Any) -> str | None:
    """
    Normalise a city to its lowercase catalog slug; None if not bookable.

    EXACT match (or a known space-form alias) only. The loose substring matching
    that this used to do is unsafe at full-catalog scale (463 cities): short
    names like "leh"/"vis"/"sur" would substring-match inside unrelated tokens
    and resolve to an arbitrary city (frozenset iteration order). The deterministic
    text scanner (_scan_city_sequence) is the topology authority now, so this only
    needs to validate a clean candidate token.
    """
    if not isinstance(raw, str):
        return None
    lowered = raw.strip().lower()
    if lowered in ALLOWED_CITIES:
        return lowered
    if lowered in CITY_SLUG_MAP:
        return CITY_SLUG_MAP[lowered]
    return None


def _normalise_vibe(raw: Any) -> str | None:
    """Normalise vibe to allowed set; return None if unrecognised (vibe is optional).

    var-0: substring fallback iterates sorted(ALLOWED_VIBES) so the first match
    is always lexicographically smallest when multiple vibes appear in the input —
    never hash-seed dependent (D6 #39).
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    lowered = raw.strip().lower()
    if lowered in ALLOWED_VIBES:
        return lowered
    # Check synonym map
    if lowered in _VIBE_SYNONYMS:
        canonical = _VIBE_SYNONYMS[lowered]
        logger.info("intent_parser: vibe clamped %r → %r (synonym)", raw, canonical)
        return canonical
    # Partial match: iterate sorted(ALLOWED_VIBES) for deterministic first-match
    # (D6 #39 — frozenset iteration is hash-seed dependent; sorted() is not).
    for vibe in sorted(ALLOWED_VIBES):
        if vibe in lowered:
            logger.info("intent_parser: vibe clamped %r → %r (substring)", raw, vibe)
            return vibe
    logger.info("intent_parser: vibe %r not in allowed set — dropping (vibe optional)", raw)
    return None


# Year-less dates anchor to the DEFAULT_START_DATE year so the deterministic core
# never reads the wall clock (var-0). The demo era is that year (2026).
_REFERENCE_YEAR = int(DEFAULT_START_DATE[:4])
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_RE = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
             r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
# A date-like phrase: ISO, "Month YYYY", "Month D[, YYYY]", "D Month [YYYY]", bare month.
# RC-5(a): "Month YYYY" alternative added BEFORE "Month D[...]" so "March 2027" binds the
# 4-digit group as the YEAR (not as day "20" + stray "27"), yielding 2027-03-01 not 2026-03-01.
# _parse_date correctly handles the matched "March 2027" string → date(2027, 3, 1).
_DATE_PHRASE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b" + _MONTH_RE + r"\s+\d{4}\b"                                    # "March 2027" (year, no day) — RC-5a
    r"|\b" + _MONTH_RE + r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?\b"  # "March 27" / "March 27, 2027"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH_RE + r"(?:,?\s*\d{4})?\b"
    r"|\b" + _MONTH_RE + r"\b",
    re.I,
)
# Cue words that precede an explicit start date.
_DATE_CUE_RE = re.compile(
    r"\b(?:start(?:s|ing)?|departing|depart(?:s)?|leav(?:e|es|ing)|begin(?:s|ning)?|"
    r"from|on|arriv(?:e|es|ing)|check[\s-]?in|in)\b[:\s]+(.{0,30})",
    re.I,
)

# RC-5(b): user asks WHEN to travel rather than stating a date.
# Fires ONLY when no explicit date is already present (checked in parse_intent).
# Deterministic / var-0: pure regex constant, no clock.
_DATE_INQUIRY_RE = re.compile(
    r"\b(?:"
    r"suggest\s+(?:a\s+)?(?:good\s+)?(?:time|date|dates?|travel\s+dates?)"
    r"|when\s+should\s+i\s+(?:go|travel|visit|book)"
    r"|best\s+time\s+(?:to\s+(?:go|visit|travel))?"
    r"|what(?:'?s)?\s+the\s+best\s+(?:time|season)"
    r"|when\s+is\s+the\s+best\s+time"
    r"|when\s+to\s+(?:go|travel|visit)"
    r"|what\s+time\s+of\s+(?:year|the\s+year)"
    r")\b",
    re.I,
)


# #honesty-fix (GAP 1, year-less-date-in-the-past): split into a raw parse that also
# reports whether the input text ITSELF specified a year, and a thin back-compat wrapper
# that discards that signal — same shim pattern as _scan_adults_raw/_scan_adults below.
# `had_year=False` is what lets `_scan_start_date_raw` know a resolved date's year was
# INFERRED (from `_REFERENCE_YEAR`) rather than stated, so it can roll a past-landing
# year-less date forward against `today` instead of silently anchoring it in the past.
def _parse_date_raw(raw: Any) -> tuple[str | None, bool]:
    """Parse a date to (ISO ``YYYY-MM-DD``, had_year).

    Accepts ISO, ``Month D[, YYYY]``, ``D Month [YYYY]`` and a bare ``Month`` (→ day 1).
    Year-less dates anchor to ``_REFERENCE_YEAR`` (no wall-clock read → var-0) and report
    ``had_year=False``. Returns (None, False) if unparseable.
    """
    if not isinstance(raw, str):
        return None, False
    s = raw.strip().lower()
    if not s:
        return None, False
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat(), True
    except ValueError:
        pass
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s).replace(",", " ")
    month = day = year = None
    for tok in s.split():
        if month is None and tok in _MONTHS:
            month = _MONTHS[tok]
        elif year is None and re.fullmatch(r"\d{4}", tok):
            year = int(tok)
        elif day is None and re.fullmatch(r"\d{1,2}", tok):
            day = int(tok)
    if month is None:
        return None, False
    try:
        return date(year or _REFERENCE_YEAR, month, day or 1).isoformat(), year is not None
    except ValueError:
        return None, False


def _parse_date(raw: Any) -> str | None:
    """Parse a date to ISO ``YYYY-MM-DD``. Thin back-compat wrapper over
    ``_parse_date_raw`` — see there for the ``had_year`` provenance signal used by
    ``_scan_start_date_raw`` (GAP 1 honesty fix). Returns None if unparseable.
    """
    return _parse_date_raw(raw)[0]


def _scan_start_date_raw(text: str, today: str | None = None) -> tuple[str | None, bool]:
    """Deterministically extract a trip START date from free text (var-0: pure fn of text + today).

    Resolves RELATIVE dates (today/tonight/tomorrow/day-after-tomorrow/next week/next month/
    next <weekday>/in N days) against the boundary-frozen ``today`` when provided; then a date
    following a start/from/departing cue; else the first date-like phrase. Bare
    ``may``/``march``/``august`` are honoured only when cue-anchored.

    Returns (iso_or_None, rolled_year). ``rolled_year`` is True when a YEAR-LESS date
    phrase (e.g. "starting March 5") resolved — via ``_REFERENCE_YEAR`` — to a date that
    would land in the PAST relative to ``today``, and was therefore rolled forward one
    year so the trip isn't silently anchored in the past (#honesty-fix GAP 1). Relative-
    date phrases (tomorrow, next week, ...) are computed directly off ``today`` and can
    never be year-less, so they always report ``rolled_year=False``.

    ``_scan_start_date`` is a thin back-compat wrapper that discards ``rolled_year`` —
    same shim pattern as ``_scan_adults_raw``/``_scan_adults``. No wall-clock read —
    ``today`` is an input (var-0).
    """
    if not isinstance(text, str) or not text.strip():
        return None, False
    low = text.lower()
    _base: date | None = None
    if today:
        try:
            _base = date.fromisoformat(today)
        except ValueError:
            _base = None
        if _base is not None:
            base = _base
            if re.search(r"\bday after tomorrow\b", low):
                return (base + timedelta(days=2)).isoformat(), False
            if re.search(r"\b(today|tonight|right now)\b", low):
                return base.isoformat(), False
            if re.search(r"\btomorrow\b", low):
                return (base + timedelta(days=1)).isoformat(), False
            # fix (in-N-days start-date hijack): an incidental "in N days" mention
            # elsewhere in the text — visa validity ("my visa expires in 90 days"),
            # a sale/deal/offer deadline ("the sale ends in 3 days"), or a refund/
            # cancellation/warranty/lease/subscription/membership window — was being
            # read as the TRIP'S start date with no context check at all, hijacking
            # `checkin` to today+N (not just a duration) and silently suppressing the
            # `assumed_start_date` honesty disclosure. Guarded with the same bounded-
            # window incidental-context check the nights/duration scanner already
            # uses. If this was the only date-ish phrase in the text, falling through
            # here (rather than returning) lets the existing "no date, assumed today"
            # disclosure path fire normally.
            _ind = re.search(r"\bin (\d{1,3}) days?\b", low)
            if _ind and not _is_incidental_context(low, _ind.start(), _ind.end()):
                return (base + timedelta(days=int(_ind.group(1)))).isoformat(), False
            if re.search(r"\bnext week\b", low):
                return (base + timedelta(days=7)).isoformat(), False
            if re.search(r"\bnext month\b", low):
                return date(base.year + base.month // 12, base.month % 12 + 1, 1).isoformat(), False
            _wd = re.search(r"\bnext (mon|tues|wednes|thurs|fri|satur|sun)day\b", low)
            if _wd:
                _names = {"mon": 0, "tues": 1, "wednes": 2, "thurs": 3, "fri": 4, "satur": 5, "sun": 6}
                _delta = (_names[_wd.group(1)] - base.weekday()) % 7 or 7
                return (base + timedelta(days=_delta)).isoformat(), False

    def _maybe_roll(iso: str, had_year: bool) -> tuple[str, bool]:
        """Roll a year-less resolved date forward one year if it lands before `today`."""
        if had_year or _base is None:
            return iso, False
        resolved = date.fromisoformat(iso)
        if resolved >= _base:
            return iso, False
        try:
            rolled = resolved.replace(year=resolved.year + 1)
        except ValueError:
            # Feb 29 rolling into a non-leap year — fall back to Mar 1 rather than crash.
            rolled = date(resolved.year + 1, 3, 1)
        return rolled.isoformat(), True

    for cue in _DATE_CUE_RE.finditer(text):
        dm = _DATE_PHRASE_RE.search(cue.group(1))
        if dm:
            iso, had_year = _parse_date_raw(dm.group(0))
            if iso:
                return _maybe_roll(iso, had_year)
    dm = _DATE_PHRASE_RE.search(text)
    if dm:
        # Bare month words that double as common English words / town names ("may" the
        # modal, "march"/"august" the verb/adjective/town) need an explicit cue (handled
        # above); without one, don't treat them as a date — UNLESS the surrounding text
        # is unambiguously naming a month rather than using the word as a verb/modal:
        # (a) it is the very first token of the request ("March, 7 nights in Tokyo" /
        #     "August works best, Kyoto, ..."), where a modal/verb reading has no
        #     subject to attach to; or (b) it directly follows "around"/"about"
        #     ("sometime around March", "go around March") — a phrasing that is never
        #     how the modal "may" or verb "march" would appear.
        # fix-round-1: closes the false "No travel dates were given" honesty-note gap
        # for exactly these three ambiguous months in natural comma-delimited phrasing.
        # fix-round-2: the round-1 rescue only covered "the very first token" and
        # "around/about" — very common natural hedging phrasings ("thinking May",
        # "planning May", "maybe May", "probably May", "looking at May", "leaning
        # towards May", "ideally May") still fell through to `return None, False`,
        # silently anchoring the trip to the Oct default with a false "no dates
        # given" note even though the user clearly named a month.
        if dm.group(0).strip().lower() in {"may", "march", "august"}:
            _pre_text = text[:dm.start()]
            _bare_month_rescued = (
                _pre_text.strip() == ""
                or re.search(
                    # fix-round-2: an optional trailing article ("a"/"an") after the
                    # hedge verb closes "planning A March trip" / "thinking AN August
                    # getaway" — the article is not itself a rescue signal (bare "a
                    # march" can mean a protest march), only a hedge-verb + article
                    # combination is.
                    # fix-round-3: commitment/intent verbs semantically identical to
                    # the ones already here ("shooting for"/"pushing for"/"targeting"/
                    # "prefer"/"want"/"let's do"/"let's say") were still missing —
                    # the very same "state a month with no subject to attach a modal/
                    # verb reading to" logic applies to them, just an incomplete
                    # enumeration of the surface forms.
                    # fix-round-4: two more residual gaps in this same rescue.
                    #   (1) the hedge-verb tail anchor required the verb to sit
                    #       IMMEDIATELY before the month (only an "a"/"an" article
                    #       tolerated in between) — but "thinking OF May", "free
                    #       FOR May"/"free DURING May", and "planning FOR May" all
                    #       insert a preposition the anchor did not tolerate, so
                    #       these extremely natural phrasings fell through.
                    #   (2) plain desire verbs ("would like"/"would love"/"like"/
                    #       "love") and the availability phrasing "free" were
                    #       missing from the verb list entirely, and a natural
                    #       date-window modifier ("early"/"late"/"mid") between the
                    #       verb/preposition and the month broke even an already-
                    #       whitelisted hedge verb ("shooting for early August").
                    r"\b(?:around|about|thinking|planning|maybe|probably|ideally|"
                    r"considering|looking\s+at|leaning\s+towards?|hoping\s+for|"
                    r"aiming\s+for|shooting\s+for|pushing\s+for|targeting|prefer|"
                    r"want|would\s+(?:like|love)|likes?|loves?|free|"
                    r"let'?s\s+do|let\s+us\s+do|let'?s\s+say|let\s+us\s+say)\b"
                    r"(?:\s+(?:of|for|during|on))?(?:\s+(?:a|an))?"
                    r"(?:\s+(?:early|late|mid))?\s*$",
                    _pre_text, re.I,
                ) is not None
                # fix-round-3: destination-first, comma-delimited phrasing
                # ("Tokyo, March, 7 nights, $3000") is the single most natural
                # way to state a trip, but the month sits right after a plain
                # trailing comma with no hedge verb at all. A comma directly
                # before the month closes the PRECEDING clause, so the month
                # cannot be read as a verb/modal continuing it (that reading
                # needs a subject in the SAME clause) — it unambiguously opens
                # a new, bare month-naming segment.
                or re.search(r",\s*$", _pre_text) is not None
            )
            if not _bare_month_rescued:
                return None, False
        iso, had_year = _parse_date_raw(dm.group(0))
        if iso:
            return _maybe_roll(iso, had_year)
        return None, False
    return None, False


def _scan_start_date(text: str, today: str | None = None) -> str | None:
    """Deterministically extract a trip START date from free text. Thin back-compat
    wrapper over ``_scan_start_date_raw`` — see there for the ``rolled_year`` provenance
    signal (GAP 1 honesty fix). Returns ISO or None (the caller falls back to
    DEFAULT_START_DATE)."""
    return _scan_start_date_raw(text, today)[0]


def _nights_from_dates(checkin: str, checkout: str) -> int | None:
    """Derive nights count from date strings."""
    ci = _parse_date(checkin)
    co = _parse_date(checkout)
    if ci is None or co is None:
        return None
    d1 = date.fromisoformat(ci)
    d2 = date.fromisoformat(co)
    n = (d2 - d1).days
    return n if n > 0 else None


def _dates_from_start_and_nights(start_iso: str, nights: int) -> tuple[str, str]:
    """Compute (checkin, checkout) from a start date and number of nights."""
    start = date.fromisoformat(start_iso)
    end = start + timedelta(days=nights)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# VARIANCE CLAMP: validate and normalise a raw parsed dict
# ---------------------------------------------------------------------------


def _clamp_and_validate(
    raw: dict,
    original_text: str,
    det_total_nights: int | None = None,
    det_budget_cents: int | None = None,
    det_adults: int | None = None,
    explicit_adults: bool = False,
    today: str | None = None,
    budget_provenance: str | None = None,
) -> dict | None:
    """
    Validate and normalise a raw dict (from LLM or fallback).

    Implements §10.11 variance-clamped hybrid: deterministic values for ALL
    numeric fields (nights, dates, budget, adults) — these OVERRIDE whatever
    the LLM may have returned.  The LLM only contributes qualitative structure
    (city, vibe in order).

    Priority for each numeric field:
      1. det_* arg (caller-supplied deterministic value from text scan) — WINS
      2. raw dict value (LLM output or fallback) — used only for genuinely
         ambiguous fields not found in text
      3. Catalog default

    The dict may contain either:
      (a) legs with only {city, vibe} — new qualitative-only format (post-fix)
      (b) legs with {city, vibe, nights} — old format, nights IGNORED (overridden)
      (c) legs with {city, vibe, checkin, checkout} — bypass format, dates used
          if and only if no det_total_nights was supplied AND no nights in text

    Does NOT add user_id (caller injects it).

    ``explicit_adults`` (#honesty-fix, silent-default-provenance): True when det_adults
    was derived from a real party-size signal in the text ("2 adults", "solo", "couple",
    "family of 4"...) rather than _scan_adults()'s own internal default-to-1. Drives the
    ``assumed_adults`` flag on the returned dict — mirrors ``assumed_start_date``.

    ``budget_provenance`` (#honesty-fix GAP 2): one of the ``_scan_budget_raw`` provenance
    tags (``code``/``word``/``symbol``/``bare_dollar``/``bare_number``) or None. Drives the
    ``assumed_currency`` flag — ONLY a ``bare_number`` budget (zero currency signal at all)
    is flagged; a bare ``$`` is a defensible, conventional USD assumption and is NOT flagged.

    Returns a valid trip_request dict or None if fundamentally invalid.
    Logs every clamping decision.
    """
    if not isinstance(raw, dict):
        logger.warning("intent_parser: clamp: input is not a dict")
        return None

    # ------------------------------------------------------------------
    # Budget — deterministic value OVERRIDES LLM
    # ------------------------------------------------------------------
    if det_budget_cents is not None and det_budget_cents > 0:
        budget_cents = det_budget_cents
        logger.info(
            "intent_parser: clamp: budget from text det=%d¢ (overrides LLM)", budget_cents
        )
        # #honesty-fix (GAP 2, silent-currency-assumption): only a genuinely bare NUMBER
        # (no "$", no currency word/code/symbol ANYWHERE in the text) silently picked USD
        # with zero currency signal. A bare "$" is a defensible, conventional assumption
        # and is intentionally left unflagged.
        _assumed_currency = "USD" if budget_provenance == "bare_number" else None
    else:
        # §10.11 hard rule: budget MUST come from deterministic text scan.
        # If the text scan missed the budget, NEVER trust the LLM's number.
        # Return None → caller converts to needs_clarification.
        logger.warning(
            "intent_parser: clamp: budget not found in text (det_budget_cents=None) — "
            "refusing to use LLM budget to prevent numeric variance/injection. "
            "Returning None → needs_clarification."
        )
        return None

    # ------------------------------------------------------------------
    # Adults — deterministic value OVERRIDES LLM
    # ------------------------------------------------------------------
    # #honesty-fix (silent-default-provenance): _scan_adults() ALWAYS returns an int
    # >= 1 (it applies its own conservative default of 1 when the text carries no
    # party-size signal), so det_adults is never None on the free-text path — this
    # branch is always taken. explicit_adults (from _scan_adults_raw, which returns
    # None on no signal) is what actually tells us whether that count is real text
    # evidence or the silent single-traveler fallback. Live/interactive callers go
    # through negotiate_from_text(), which reads the `assumed_adults` flag set below
    # and attaches an honest user-facing note instead of silently pricing lodging
    # occupancy / insurance / fees for a fabricated solo traveler.
    _assumed_adults = False
    if det_adults is not None and det_adults >= 1:
        adults = det_adults
        logger.info(
            "intent_parser: clamp: adults from text det=%d (overrides LLM)", adults
        )
        _assumed_adults = not explicit_adults
    else:
        adults = raw.get("adults", 1)
        if not isinstance(adults, int) or adults < 1:
            try:
                adults = max(1, int(adults))
                logger.info("intent_parser: clamp: adults coerced to %d", adults)
            except (TypeError, ValueError):
                adults = 1
                logger.info("intent_parser: clamp: adults fallback to 1")

    # #party-fix: explicitly-stated child count, carried onto every leg so hotel
    # selection / day planning see the real party (0 when none stated). The adult
    # count above was already reconciled against this in parse_intent (a stated
    # TOTAL headcount subtracts the kids; a bare "family + kids" implies 2 adults).
    _children_count = _scan_children(original_text) or 0

    # ------------------------------------------------------------------
    # Legs — extract qualitative structure (city + vibe order)
    # ------------------------------------------------------------------
    raw_legs = raw.get("legs")
    if not isinstance(raw_legs, list) or len(raw_legs) == 0:
        logger.warning("intent_parser: clamp: no legs in raw dict")
        return None

    # Leg count bound: 1–5
    if len(raw_legs) > 5:
        logger.warning(
            "intent_parser: clamp: %d legs exceeds max 5 — truncating", len(raw_legs)
        )
        raw_legs = raw_legs[:5]

    # Validate each leg's qualitative fields (city, vibe, interests)
    city_vibe_pairs: list[tuple[str, str | None]] = []
    # Interests are trip-level (same for all legs from the scanner).
    # Extract from the first leg that has them; all legs share the same list.
    _raw_interests: list[str] | None = None
    _raw_interest_map: dict[str, list[str]] | None = None
    # fix-round-1: _single_city_legs stashes any comma/"and"-joined vibe(s)
    # that were collapsed out of a single-city leg on leg["dropped_vibes"]
    # (additive, purely informational). city_vibe_pairs below only carries
    # (city, vibe) tuples, so this must be pulled out HERE before that
    # reduction discards it — same pattern as _children_count above.
    _dropped_vibes: list[str] = []
    for i, leg in enumerate(raw_legs):
        if not isinstance(leg, dict):
            logger.warning("intent_parser: clamp: leg[%d] is not a dict — skipping", i)
            continue
        if leg.get("dropped_vibes"):
            _dropped_vibes.extend(leg["dropped_vibes"])

        # City — mandatory
        raw_city = leg.get("city")
        city = _normalise_city(raw_city)
        if city is None:
            city = _scan_city(original_text)
            if city:
                logger.info(
                    "intent_parser: clamp: leg[%d] city %r not in catalog → "
                    "fallback from text: %r",
                    i, raw_city, city,
                )
            else:
                logger.warning(
                    "intent_parser: clamp: leg[%d] city %r not in catalog and "
                    "no city found in text — skipping leg",
                    i, raw_city,
                )
                continue

        # Vibe — optional (clamped to closed set)
        vibe = _normalise_vibe(leg.get("vibe"))
        city_vibe_pairs.append((city, vibe))

        # Interests — optional; pass through verbatim (already sorted/expanded by scanner).
        if _raw_interests is None and leg.get("interests"):
            _raw_interests = list(leg["interests"])
        # interest_map (original→terms) — carried through so the day-planner can
        # emit its honest note per ORIGINAL interest. Pass-through only (cannot be
        # reconstructed from the already-expanded flat list).
        if _raw_interest_map is None and isinstance(leg.get("interest_map"), dict):
            _raw_interest_map = dict(leg["interest_map"])

    if not city_vibe_pairs:
        logger.warning("intent_parser: clamp: no valid legs after city validation")
        return None

    leg_count = len(city_vibe_pairs)

    # ------------------------------------------------------------------
    # Dates / nights — ALL DETERMINISTIC (§10.11 hard rule)
    # ------------------------------------------------------------------
    # Priority: det_total_nights (from text) > bypass dates (checkin/checkout)
    # The LLM's per-leg "nights" field is ALWAYS ignored.
    #
    # Start date: honour a user-stated start ("starting Sep 10", "in September",
    # "2026-09-10") extracted DETERMINISTICALLY from the text; else fall back to
    # DEFAULT_START_DATE so dateless prompts (the benchmark scenarios) stay byte-identical.
    #
    # #honesty-fix (silent-date-fallback): DEFAULT_START_DATE exists purely to keep the
    # benchmark harness deterministic (it always calls parse_intent with dateless free
    # text). Live/
    # interactive callers go through negotiate_from_text(), which reads the
    # `assumed_start_date` flag set below and attaches an honest user-facing note
    # instead of silently pricing insurance / running season-sensitive risk & health
    # checks against a fabricated date. _explicit_start is None whenever no date could
    # be found in the free text — that is the var-0-safe signal we key off of.
    #
    # #honesty-fix (GAP 1, year-less-date-in-the-past): _scan_start_date_raw also reports
    # whether a YEAR-LESS date phrase ("starting March 5") had to be rolled forward a year
    # because, anchored on _REFERENCE_YEAR, it would have landed in the PAST relative to
    # `today`. Such a date IS explicit (the day/month came straight from the text) so it
    # must NOT be folded into `_assumed_start_date` (which means "no date given at all") —
    # it gets its own `assumed_date_year` flag, surfaced by negotiate_from_text via
    # `date_year_assumption_note`.
    _explicit_start, _rolled_year = _scan_start_date_raw(original_text, today)
    start_anchor = _explicit_start or DEFAULT_START_DATE
    _assumed_date_year = _rolled_year
    # True only once we've confirmed the fallback default is actually the value used
    # to build the first leg's checkin (not merely that the text lacked a date — a
    # bypass/structured caller may already supply real per-leg checkin/checkout).
    _assumed_start_date = False

    if det_total_nights is not None and det_total_nights >= 1:
        # PRIMARY PATH: deterministic total_nights → split evenly → contiguous dates
        logger.info(
            "intent_parser: clamp: using deterministic total_nights=%d for %d leg(s)",
            det_total_nights, leg_count,
        )
        validated_legs = _build_legs_with_dates(
            city_vibe_pairs, det_total_nights, adults, interests=_raw_interests,
            interest_map=_raw_interest_map, start_date=start_anchor,
            children=_children_count,
        )
        # This path always literally anchors leg 0's checkin on start_anchor.
        _assumed_start_date = _explicit_start is None
    else:
        # BYPASS PATH: no nights in text; try to use checkin/checkout from legs
        # (parse_intent_bypass populates these from a pre-structured request).
        # For the bypass case ONLY, we still enforce contiguity via cursor.
        logger.info(
            "intent_parser: clamp: no det_total_nights — using bypass date mode"
        )
        validated_legs = []
        cursor_date = date.fromisoformat(start_anchor)

        for i, ((city, vibe), raw_leg) in enumerate(zip(city_vibe_pairs, raw_legs)):
            leg_dict = raw_leg if isinstance(raw_leg, dict) else {}

            if "checkin" in leg_dict and "checkout" in leg_dict:
                checkin = _parse_date(leg_dict["checkin"])
                checkout = _parse_date(leg_dict["checkout"])
                if checkin is None or checkout is None or checkin >= checkout:
                    logger.warning(
                        "intent_parser: clamp: bypass leg[%d] invalid dates — skipping", i
                    )
                    continue
                # Enforce contiguity from cursor
                if i == 0:
                    cursor_date = date.fromisoformat(checkin)
                else:
                    nights = _nights_from_dates(checkin, checkout) or 1
                    checkin, checkout = _dates_from_start_and_nights(
                        cursor_date.isoformat(), nights
                    )
                cursor_date = date.fromisoformat(checkout)
            elif "nights" in leg_dict:
                # nights present in bypass: use it (but this is deterministic input)
                try:
                    nights = int(leg_dict["nights"])
                except (TypeError, ValueError):
                    nights = 0
                if nights < 1:
                    logger.warning(
                        "intent_parser: clamp: bypass leg[%d] nights=%r invalid — skipping",
                        i, leg_dict.get("nights"),
                    )
                    continue
                # Leg 0 reaching here with no explicit start date means cursor_date is
                # still the fallback start_anchor computed above — flag it honestly.
                if i == 0 and _explicit_start is None:
                    _assumed_start_date = True
                checkin, checkout = _dates_from_start_and_nights(cursor_date.isoformat(), nights)
                cursor_date = date.fromisoformat(checkout)
            else:
                logger.warning(
                    "intent_parser: clamp: bypass leg[%d] no dates/nights — skipping", i
                )
                continue

            out_leg: dict[str, Any] = {
                "city": city,
                "place_key": city,  # drives the health gate (see _build_legs_with_dates)
                "checkin": checkin,
                "checkout": checkout,
                "adults": adults,
                "children": _children_count,  # #party-fix: carry kids through to planning
            }
            if vibe is not None:
                out_leg["vibe"] = vibe
            if _raw_interests:
                out_leg["interests"] = _raw_interests
            if _raw_interest_map:
                out_leg["interest_map"] = _raw_interest_map
            iso2 = CITY_TO_ISO2.get(city)
            if iso2:
                out_leg["dest_country"] = iso2
            validated_legs.append(out_leg)

    if not validated_legs:
        logger.warning("intent_parser: clamp: no valid legs after date assignment")
        return None

    if _assumed_start_date:
        logger.info(
            "intent_parser: clamp: no start date in request — assumed DEFAULT_START_DATE=%s "
            "(honest note surfaced by negotiate_from_text for live callers)",
            start_anchor,
        )

    if _assumed_adults:
        logger.info(
            "intent_parser: clamp: no party-size signal in request — assumed adults=%d "
            "(honest note surfaced by negotiate_from_text for live callers)",
            adults,
        )

    if _assumed_date_year:
        logger.info(
            "intent_parser: clamp: year-less start date resolved to %s, which would have "
            "landed in the past — rolled the year forward "
            "(honest note surfaced by negotiate_from_text for live callers)",
            validated_legs[0]["checkin"] if validated_legs else start_anchor,
        )

    # #honesty-fix (GAP 3, silent-children-drop): the user EXPLICITLY stated children
    # ("2 adults and 2 kids"), but occupancy/insurance/fees are only ever priced for
    # `adults` — children are not a bookable party-size input anywhere downstream. This
    # is not a "silent default" like the two flags above (nothing was assumed — the text
    # WAS explicit) but a silent DROP of stated information, which is the same honesty
    # violation this pattern exists to catch. "family of N" phrasing is deliberately NOT
    # matched by _scan_children (it already prices all N as adults) — only explicit
    # kid/child count phrasing trips this flag.
    _ignored_children = _children_count or None
    if _ignored_children:
        logger.info(
            "intent_parser: clamp: %d child(ren) mentioned in request but not priced "
            "(honest note surfaced by negotiate_from_text for live callers)",
            _ignored_children,
        )

    return {
        "total_budget_cents": budget_cents,
        "adults": adults,
        # #party-fix: explicitly-stated child count carried through to planning
        # (0 when none). Distinct from `ignored_children` below, which is the
        # honesty flag that these kids are NOT priced into occupancy/insurance.
        "children": _children_count,
        "legs": validated_legs,
        # Honest-degradation flag (additive; None on every existing caller/fixture that
        # already supplies a date — see negotiate_from_text for how the live path
        # surfaces this to the user). Deterministic pure fn of (text, today) -> var-0 safe.
        "assumed_start_date": start_anchor if _assumed_start_date else None,
        # fix-round-3: when a start date WAS assumed, distinguish "the user
        # said nothing about timing at all" from "the user stated a SEASON/
        # HOLIDAY (e.g. 'this summer', 'around Christmas') that the
        # deterministic scanner cannot resolve to a specific calendar date".
        # None whenever no season/holiday word is present, or no date was
        # assumed at all — attach_assumption_notes uses this to word the
        # honesty note accurately instead of flatly contradicting the user.
        "assumed_start_date_season_hint": (
            _SEASON_HOLIDAY_HINT_RE.search(original_text).group(0).lower()
            if _assumed_start_date and _SEASON_HOLIDAY_HINT_RE.search(original_text)
            else None
        ),
        # #honesty-fix (GAP 1, silent-default-provenance): same pattern for party size — None on
        # every caller/fixture that already states a traveler count (explicitly or via a
        # bypass/structured request); the assumed count (always 1, the conservative
        # fallback) when the free text carried no party-size signal at all. See
        # negotiate_from_text for how the live path surfaces this to the user.
        "assumed_adults": adults if _assumed_adults else None,
        # #honesty-fix (GAP 1, year-less-date-in-the-past): True only when a year-less date
        # phrase resolved to a past date and was rolled forward a year (see
        # _scan_start_date_raw). None/False on every request that either gave a full date
        # or gave no date at all (that case is `assumed_start_date` above).
        "assumed_date_year": True if _assumed_date_year else None,
        # #honesty-fix (GAP 2, silent-currency-assumption): "USD" ONLY when the budget was
        # a bare number with ZERO currency signal in the text (see budget_provenance /
        # _scan_budget_raw). A bare "$" amount is a defensible convention and stays
        # unflagged.
        "assumed_currency": _assumed_currency,
        # #honesty-fix (GAP 3, silent-children-drop): count of explicitly-mentioned
        # children that are NOT folded into `adults` pricing. None when no child/kid
        # phrasing was found in the text.
        "ignored_children": _ignored_children if _ignored_children else None,
        # fix-round-3: True only when `ignored_children` is the conservative
        # "at least 1" UNQUANTIFIED estimate AND the user's own wording was
        # PLURAL ("the kids are coming") — lets attach_assumption_notes avoid
        # asserting a false precise "1 child(ren)" that contradicts the
        # user's own plural wording.
        "ignored_children_is_plural_estimate": (
            _children_count_is_plural_estimate(original_text)
            if _ignored_children else False
        ),
        # fix-round-1: comma/"and"-joined vibe(s) collapsed out of a single-city
        # leg (see _single_city_legs) — surfaced for attach_assumption_notes'
        # dropped_vibes_note. None when nothing was dropped.
        "dropped_vibes": _dropped_vibes if _dropped_vibes else None,
    }


# ---------------------------------------------------------------------------
# Deterministic fallback parser (regex / keyword)
# ---------------------------------------------------------------------------


def _scan_city(text: str) -> str | None:
    """Scan text for known city names. Returns first match as a catalog slug.

    var-0: sorted with tiebreak (-len, city) so equal-length names resolve in
    stable lexical order — never hash-seed dependent (D6 #14).
    """
    lowered = text.lower()
    # Aliases first (e.g. "kl", "margaret river"), then catalog cities; longer
    # match wins so multi-word names beat single-word substrings.
    for alias in sorted(CITY_ALIASES, key=lambda a: (-len(a), a)):
        if re.search(r"\b" + re.escape(alias) + r"\b", lowered):
            return CITY_ALIASES[alias]
    for city in sorted(ALLOWED_CITIES, key=lambda c: (-len(c), c)):
        if re.search(r"\b" + re.escape(city) + r"\b", lowered):
            return CITY_SLUG_MAP.get(city, city)
    return None


# Maximum number of legs/cities the front door supports (matches the LLM bound
# and the leg-count clamp). More than this → honest decline.
MAX_LEGS: int = 5


# ---------------------------------------------------------------------------
# Connective-verb disambiguation for catalog cities that are also planning verbs
# ---------------------------------------------------------------------------
# Some catalog city names double as multi-city DECOMPOSITION verbs in English —
# the worst offender is "split" (city of Split, Croatia vs. the verb "split the
# trip BETWEEN X and Y"). When 'split' is used connectively it is NOT a
# destination; treating it as one fabricates a spurious first leg (the live
# "Vestibul Palace, HR" bug). The disambiguation is purely CONTEXTUAL:
#
#   CONNECTIVE (NOT a city): 'split' followed — within a short window that may
#   contain filler like "the time"/"evenly"/"my days" — by a decomposition
#   preposition (between / across / among / over / into / amongst). e.g.
#   "split between Hanoi and Bangkok", "split across 3 cities", "split my time
#   over Bali and Bangkok".
#
#   DESTINATION (still a city): 'split' after a destination cue (in/to/visit/
#   explore/at) OR standalone (e.g. "Split, Croatia", "5 nights in Split").
#   A destination cue IMMEDIATELY before 'split' always wins, so "in Split"
#   resolves to the city even if a later "between" appears elsewhere.
#
# Conservative by construction: only spans matching the connective shape are
# suppressed; everything else (including every other catalog city) is untouched,
# so a genuinely-named Split is never lost and no unrelated city is affected.
_CONNECTIVE_CITY_VERBS: frozenset[str] = frozenset({"split"})

_DECOMP_PREP = r"(?:between|across|among|amongst|over|into)"
# Up to ~3 filler words may sit between the verb and the preposition
# ("split the time evenly between ..."). The filler is a CLOSED whitelist of
# quantity/time/distribution words, NOT arbitrary \w+ — otherwise a subject-
# position copula ("Split is between Croatia and the sea") would mis-suppress the
# real city Split. Kept small so it can't reach across a sentence either.
#
# Verified live bug: "split the cost between the two of us — 4 nights in Rome"
# fabricated a spurious first leg (the city Split, Croatia) because the object
# noun of "split" was a MONEY noun ("cost"), not a time/trip noun — the old
# whitelist only covered time/trip nouns, so "split the cost between ..." never
# matched this guard's filler span and fell through to the ordinary city scan.
# "split the bill/budget/tab/etc." is exactly as common a way to say "divide a
# payment between people" as "split the time", so the money-noun family belongs
# in the same closed whitelist for the same reason.
#
# adversarial-review: "split the dinner bill"/"split the taxi fare" still
# fabricated the fake Split-Croatia leg -- "dinner"/"taxi" (a modifier noun
# immediately before the recognized money noun) wasn't in the whitelist, so
# the filler span broke on that word. Added the common modifier-noun family
# that precedes a money word in this exact construction (meal/ride/lodging
# words) -- same closed-whitelist approach as the rest of this list, not a
# general \w+, for the same subject-position-copula reason given above.
_FILLER_WORD = (
    r"(?:the|my|our|your|its|time|trip|stay|holiday|vacation|days?|nights?|"
    r"weeks?|months?|evenly|equally|fairly|roughly|about|half|it|them|"
    r"everything|"
    r"costs?|bills?|budgets?|money|expenses?|fares?|prices?|tabs?|checks?|"
    r"dinner|lunch|breakfast|brunch|restaurant|taxi|cab|uber|lyft|grab|"
    r"parking|hotel|grocery|groceries|bar|drinks?|gas|fuel|"
    r"\d+)"
)
_FILLER = r"(?:\s+" + _FILLER_WORD + r"){0,3}"
# Destination cue IMMEDIATELY preceding the verb → it's the city, never connective.
# NOTE: 'to' is deliberately EXCLUDED here — inside a connective span the verb is
# always followed by a decomposition preposition, so "to split between X and Y"
# is the planning verb ("split [the trip]"), not the city Split. A real "to Split"
# (the city) never carries a trailing decomposition prep, so it won't form a span.
_DEST_CUE_BEFORE = r"(?:\bin\b|\bvisit\b|\bexplore\b|\bat\b)\s+$"

# fix-round-1: "between"/"over" (members of _DECOMP_PREP) are also the two most
# common English prepositions for naming a DATE/TIME WINDOW ("between April and
# May", "between the 10th and 15th", "over the long weekend", "over Easter",
# "over spring break") — not a two-city trip decomposition at all. Without this
# guard, "to Split between April and May" / "Split over the long weekend" wrongly
# suppressed the real city Split, producing a false "no supported destination"
# decline. Checked AFTER the decomposition preposition: if what follows it reads
# as a date/time window rather than a place list, this is NOT a connective use —
# the verb-shaped token is left alone so the ordinary city-cue logic decides it.
_DATE_WINDOW_AFTER_PREP_RE = re.compile(
    r"^\s*(?:the\s+)?"
    # fix-round-4: two more residual gaps in this same guard.
    #   (1) a date-window QUALIFIER ("early"/"late"/"mid") sitting BEFORE the
    #       month/season branches below ("between early June and late
    #       August") broke every branch, since none tolerated anything
    #       between the optional "the " and the month/day/season word —
    #       falsely suppressing a genuinely-named city (Split).
    #   (2) "the start of <month>" / "the end of <month>" puts the non-
    #       whitelisted noun "start"/"end" where a month/day is expected —
    #       same false suppression for this equally natural open-window
    #       phrasing.
    # Both are optional and purely additive: they only widen what this guard
    # RECOGNISES as a date/time window, never narrow it.
    r"(?:(?:early|late|mid)[\s-]*|(?:start|beginning|end)\s+of\s+)?"
    r"(?:"
    r"\d{1,2}(?:st|nd|rd|th)?\b"
    r"|" + _MONTH_RE + r"\b"
    r"|long\s+weekend\b|weekend\b"
    r"|easter\b|christmas\b|new\s+year'?s?\b|thanksgiving\b"
    r"|spring\s+break\b|summer\s+break\b|winter\s+break\b"
    r"|spring\b|summer\b|winter\b|fall\b|autumn\b|holidays?\b"
    # fix-round-2: weekday names ("between Monday and Friday") — one of the
    # most natural ways travelers state trip dates — were entirely absent,
    # so a weekday date window after "between"/"over" still matched the
    # connective shape and wrongly suppressed the real city (e.g. Split).
    r"|mondays?\b|tuesdays?\b|wednesdays?\b|thursdays?\b|fridays?\b|"
    r"saturdays?\b|sundays?\b"
    # fix-round-2: non-Western cultural/religious holiday windows ("over
    # Diwali", "over Ramadan") — already treated as holiday/time words
    # elsewhere in this module (the non-city allowlist), but missing here,
    # so this guard was internally inconsistent and still false-declined.
    r"|diwali\b|eid\b|ramadan\b"
    # fix-round-3: a BUDGET/MONEY range ("Split between $2000 and $3000",
    # "Split between 1500 and 3000 dollars") is at least as natural a thing
    # to state right after a city name as a date window, but neither a
    # currency-prefixed amount (the "$" blocked the old \d{1,2} branch) nor a
    # 3+-digit bare figure (that branch only matched 1-2 digits, a day-of-
    # month shape) was recognised — so a genuine "Split" city false-declined
    # as "no supported destination". A 1-2 digit day number is already
    # covered above; this adds a currency-symbol amount OR a 3+-digit bare
    # number (a budget figure, not a day-of-month).
    r"|[$€£]\s*\d[\d,]*(?:\.\d+)?\b"
    r"|\d{3}[\d,]*(?:\.\d+)?\b"
    # fix-round-3: a spelled-out DURATION ("Split across two weeks") means
    # "spread the stay across two weeks" — a single-city duration statement,
    # not a two-city decomposition — but was previously unrecognised unless
    # the number was a bare 1-2 digit numeral.
    r"|(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:days?|weeks?|months?)\b"
    # fix-round-4: a SPELLED-OUT budget range ("Split between two thousand
    # and three thousand dollars") is a money range, not a two-city
    # decomposition, but neither the currency-symbol/3+-digit numeric money
    # branch above (fix-round-3) nor the duration branch just above (number-
    # word + days/weeks/months) covers a spelled-out amount — a bare
    # word-number immediately before "thousand"/"hundred" is an unambiguous
    # money signal in this position.
    r"|(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety)[\s-]+"
    r"(?:hundred|thousand)\b"
    r")",
    re.I,
)

# fix-round-3: a traveler who states SEASONAL/HOLIDAY timing ("in spring",
# "this summer", "over the holidays", "around Christmas") instead of a
# concrete date is not silent about dates at all — the deterministic date
# scanner just cannot resolve a season/holiday word to a specific calendar
# date, so it falls back to the generic DEFAULT_START_DATE. Without this
# distinction the honesty note ends up flatly contradicting the user ("No
# travel dates were given...") even though they clearly stated timing. See
# `_clamp_and_validate` (sets assumed_start_date_season_hint) and
# `attach_assumption_notes` (uses it to word the note honestly).
_SEASON_HOLIDAY_HINT_RE = re.compile(
    r"\b(?:christmas|new\s+year'?s?|thanksgiving|easter|"
    r"spring\s+break|summer\s+break|winter\s+break|"
    r"spring|summer|winter|fall|autumn|holidays?|"
    r"diwali|eid|ramadan)\b",
    re.I,
)


def _connective_verb_spans(lowered: str) -> list[tuple[int, int]]:
    """
    Return (start, end) char spans of catalog-city tokens that are being used as
    multi-city DECOMPOSITION verbs (e.g. the 'split' in "split between X and Y"),
    so the city scanner can EXCLUDE them as destinations.

    Deterministic / var-0: spans derive purely from regex position over the input
    text (no set iteration selects a value); the verb set is iterated in sorted
    order so the scan is order-stable regardless of frozenset hashing.
    """
    spans: list[tuple[int, int]] = []
    for verb in sorted(_CONNECTIVE_CITY_VERBS):
        verb_re = re.compile(
            r"\b" + re.escape(verb) + r"\b" + _FILLER + r"\s+" + _DECOMP_PREP + r"\b",
            re.I,
        )
        for m in verb_re.finditer(lowered):
            # The verb token occupies the first len(verb) chars of the match.
            v_start, v_end = m.start(), m.start() + len(verb)
            # If a destination cue sits immediately before the verb, it's the
            # CITY (e.g. "in Split"), not a connective — do not suppress.
            if re.search(_DEST_CUE_BEFORE, lowered[:v_start]):
                continue
            # fix-round-1: the decomposition preposition is followed by a
            # DATE/TIME window ("between April and May", "over the long
            # weekend"), not a two-place list — this is not a decomposition
            # verb use at all, so leave the city token alone.
            if _DATE_WINDOW_AFTER_PREP_RE.search(lowered[m.end():m.end() + 30]):
                continue
            spans.append((v_start, v_end))
    return spans


# Negation-phrase patterns — a city mention immediately governed by a negation
# cue ("anywhere but Bangkok", "not going to Paris", "except Tokyo", "skip
# Bangkok") is a city the traveller has explicitly RULED OUT, not requested.
# Verified live bugs: "anywhere but Bangkok, 4 nights, $2000" was planning a
# trip TO Bangkok (the one city explicitly excluded), and "I am NOT going to
# Paris again. Rome this time, ..." put refused Paris as leg 1 and wanted Rome
# as leg 2 (or dropped it) — the scanner was polarity-blind, bare-matching
# every catalog city name regardless of negation around it.
#
# Matched the same "cue phrase immediately followed by a city token" way as
# _ORIGIN_CUE_RE/_origin_spans below (cue-adjacent, not a loose window scan,
# so filler/prose between the cue and a later, genuinely-wanted city is never
# mistaken for the negation's object). Unlike an origin span, a negated span
# is dropped OUTRIGHT by _scan_city_sequence_spans — never rescued as an
# origin or a staycation destination — because the user has explicitly
# refused it, not merely stated it as a home base.
#
# The "n't ..." alternatives sit OUTSIDE the leading \b group deliberately:
# "isn't"/"aren't"/"won't" are single tokens, so there is no word boundary
# immediately before the "n't" substring within them (both surrounding chars
# are word characters) — a leading \b there would never match "isn't going to
# Paris" at all.
# adversarial-review: the residual note on this fix originally said only
# VERBLESS negation forms ("Rome, but not Paris") leaked. That was wrong --
# fully-verbed forms leak too when the verb/cue isn't one of the ones listed
# below: "I don't want to go to Paris", "let's avoid Bangkok", "Rome rather
# than Paris", "Rome instead of Paris" all booked the refused city before
# these additions. Each new cue is adjacency-matched exactly like the
# existing ones (immediately followed by a real catalog city, via
# _CITY_RE.match not .search) so a cue with no city right after it is a
# no-op, same safety property as every other alternative here.
_NEGATION_CUE_RE = re.compile(
    r"(?:"
    r"\b(?:"
    r"anywhere\s+but"                    # "anywhere but Bangkok"
    r"|any\s*place\s+but"                # "anyplace but Bangkok"
    r"|somewhere\s+but"                  # "somewhere but Bangkok"
    r"|except\s+for"                     # "except for Tokyo"
    r"|except"                           # "except Tokyo"
    r"|excluding"                        # "excluding Tokyo"
    r"|skip(?:ping)?"                    # "skip Bangkok, just Chiang Mai"
    r"|avoid(?:ing)?"                    # "avoid Bangkok" / "avoiding Bangkok"
    r"|rather\s+than"                    # "Rome rather than Paris"
    r"|instead\s+of"                     # "Rome instead of Paris"
    r"|not\s+(?:going|travel(?:l)?ing|heading|flying|driving)\s+to"
    r"|not\s+visiting"
    r"|not\s+want(?:ing)?\s+to\s+(?:go|travel(?:l)?|head|fly|drive)\s+to"
    r"|never\s+(?:going|travel(?:l)?ing|heading|flying|driving)\s+to"
    r"|never\s+visiting"
    r")"
    r"|n't\s+(?:be\s+)?(?:going|travel(?:l)?ing|heading|flying|driving)\s+to"
    r"|n't\s+visiting"
    r"|n't\s+want(?:ing)?\s+to\s+(?:go|travel(?:l)?|head|fly|drive)\s+to"
    r")\s*",
    re.I,
)

# adversarial-review: "don't skip Rome" / "can't avoid Bangkok" is DOUBLE
# negation -- English negating a negation-cue verb flips it back to inclusion
# ("don't skip X" = definitely include X), the exact opposite of a bare "skip
# X"/"avoid X". A negator or difficulty-modal immediately before the cue must
# stop it from excluding the city that follows. Round-4 adversarial pass
# added the bare "not" and "won't" forms ("let's not skip Rome", "we won't
# skip Florence") -- arguably the single most common phrasing of this
# construction, and it was missing.
_DOUBLE_NEGATION_GUARD_RE = re.compile(
    r"(?:don'?t|do\s+not|doesn'?t|does\s+not|can'?t|cannot|can\s+not|"
    r"couldn'?t|could\s+not|won'?t|will\s+not|shouldn'?t|should\s+not|"
    r"wouldn'?t|would\s+not|no\s+way\s+to|no\s+reason\s+to|hard\s+to|"
    r"impossible\s+to|never|\bnot)\s*$",
    re.I,
)


def _negated_city_spans(lowered: str) -> list[tuple[int, int]]:
    """
    Return (start, end) char spans of catalog-city tokens that are the OBJECT
    of a negation cue ("anywhere but Bangkok", "not going to Paris", "except
    Tokyo", "skip Bangkok") — a city the traveller has explicitly ruled out.
    ``_scan_city_sequence_spans`` excludes these spans entirely (never as a
    destination, never rescued as an origin/staycation) so a refused city is
    never silently booked instead of (or ahead of) the one actually wanted.

    Deterministic / var-0: pure regex + module-load-time _CITY_RE scan over the
    lowercased input, matched cue-adjacent (``.match``, not ``.search`` over a
    window) exactly like ``_origin_spans`` — filler/prose between the cue and
    a later, genuinely-wanted city means no match, so a real destination is
    never dropped by accident.
    """
    spans: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def _add(city_start: int, city_end: int) -> None:
        key = (city_start, city_end)
        if key not in seen:
            seen.add(key)
            spans.append(key)

    for cue_m in _NEGATION_CUE_RE.finditer(lowered):
        # adversarial-review: "don't skip Rome"/"can't avoid Bangkok" is
        # DOUBLE negation — English "don't skip X" / "can't avoid X" means
        # definitely INCLUDE X, the opposite of what a bare "skip X"/"avoid X"
        # means. A negator or difficulty-modal immediately before the cue
        # flips its polarity back to inclusion, so it must not exclude the
        # city that follows.
        if _DOUBLE_NEGATION_GUARD_RE.search(lowered[max(0, cue_m.start() - 20):cue_m.start()]):
            continue
        pos = cue_m.end()
        city_m = _CITY_RE.match(lowered, pos)
        if city_m is None:
            # Fallback: skip one leading article ("except the Maldives").
            art_m = re.match(r"(?:a|an|the)\s+", lowered[pos:], re.I)
            if art_m:
                city_m = _CITY_RE.match(lowered, pos + art_m.end())
        if city_m:
            _add(city_m.start(), city_m.end())

    return spans


# RC-1: origin-phrase patterns — the traveller's HOME base or departure city,
# NOT a destination.  Matched as (cue phrase) + (city token) → the city span
# is excluded from _scan_city_sequence so it never becomes a trip leg.
# Patterns chosen to be unambiguous: they require a clear home/origin signal.
# "from <City> to" is handled separately (only when "to" follows the city).
# Deterministic / var-0: compiled once at import, pure regex over input.
_ORIGIN_CUE_RE = re.compile(
    r"\b(?:"
    r"start(?:ing)?\s+from"          # "starting from Stockholm"
    r"|located\s+in"                  # "located in Singapore"
    r"|based\s+in"                    # "based in Bangkok"
    r"|i\s*'?m\s+in"                 # "i'm in <City>" / "im in <City>"
    r"|i\s+am\s+in"                   # "i am in <City>"
    r"|i\s+live\s+in"                 # "i live in <City>"
    r"|living\s+in"                   # "living in <City>"
    r"|fly(?:ing)?\s+out\s+of"       # "flying out of Singapore"
    r"|depart(?:ing)?\s+from"        # "departing from <City>"
    r")\s*",
    re.I,
)


def _origin_spans(lowered: str) -> list[tuple[int, int]]:
    """
    Return (start, end) char spans of catalog-city tokens used as ORIGIN
    locations (home base or departure city, NOT a destination), so
    _scan_city_sequence can exclude them from the destination list.

    Matches named-cue patterns (``_ORIGIN_CUE_RE``) and the specific form
    ``from <City> to`` (suppressed only when ``to`` immediately follows).

    Deterministic / var-0: pure regex + module-load-time _CITY_RE scan over
    the lowercased input. Does NOT modify text; callers may stash the origin.
    No LLM, no clock, no random.
    """
    spans: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def _add(city_start: int, city_end: int) -> None:
        key = (city_start, city_end)
        if key not in seen:
            seen.add(key)
            spans.append(key)

    # fix-round-4: an origin-cued city is genuinely a home base ONLY for a
    # SINGLE hop ("starting from Stockholm, visiting Copenhagen" / "from
    # Singapore to Bangkok"). When the text continues as a CHAINED multi-city
    # route ("starting from Kyoto then Osaka then Hiroshima" / "from Tokyo to
    # Osaka to Kyoto"), the cued city is the itinerary's FIRST STOP, not a
    # home base — suppressing it as origin silently dropped roughly a third
    # of a domestic multi-city trip with no clarification or note. Detected
    # by checking whether TWO further cities (not just one) follow via "to"/
    # "then" hops right after the candidate origin city.
    def _is_chained_route(after_pos: int) -> bool:
        m1 = re.match(r"\s*(?:to|then)\s+", lowered[after_pos:])
        if not m1:
            return False
        pos2 = after_pos + m1.end()
        city_m1 = _CITY_RE.match(lowered, pos2)
        if not city_m1:
            art_m = re.match(r"(?:a|an|the)\s+", lowered[pos2:], re.I)
            if art_m:
                city_m1 = _CITY_RE.match(lowered, pos2 + art_m.end())
        if not city_m1:
            return False
        m2 = re.match(r"\s*(?:to|then)\s+", lowered[city_m1.end():])
        if not m2:
            return False
        pos3 = city_m1.end() + m2.end()
        city_m2 = _CITY_RE.match(lowered, pos3)
        if not city_m2:
            art_m2 = re.match(r"(?:a|an|the)\s+", lowered[pos3:], re.I)
            if art_m2:
                city_m2 = _CITY_RE.match(lowered, pos3 + art_m2.end())
        return city_m2 is not None

    # Phase 1: named-cue patterns → city IMMEDIATELY adjacent to the cue is
    # the origin.  RC-1 fix: use .match (anchored at cue end) instead of
    # .search over a 60-char window so that filler words between the cue and
    # a real destination city are never silently treated as origin suppression.
    # "i live in a small town, dreaming of Bali" → cue ends before "a small";
    # "a small" is not a city → no suppression → Bali stays as destination.
    # Multi-token city names beginning with an article ("the Hague") are tried
    # first without skipping; the article-skip fallback handles "located in the
    # Philippines" where only "philippines" is catalogued.
    for cue_m in _ORIGIN_CUE_RE.finditer(lowered):
        pos = cue_m.end()
        city_m = _CITY_RE.match(lowered, pos)
        if city_m is None:
            # Fallback: skip one leading article so "located in the Philippines"
            # still identifies philippines as origin.
            art_m = re.match(r"(?:a|an|the)\s+", lowered[pos:], re.I)
            if art_m:
                city_m = _CITY_RE.match(lowered, pos + art_m.end())
        if city_m and not _is_chained_route(city_m.end()):
            _add(city_m.start(), city_m.end())

    # Phase 2: "from <City> to" — only suppress when "to" directly follows
    # the city (i.e. clearly an origin, not a destination-direction marker).
    # Bare "from Paris" (no "to") is NOT suppressed: could mean "a trip from
    # [the Paris angle]" or a destination cue.
    # RC-1 fix: use .match (cue-adjacent) so "from work, Tokyo to Kyoto" does
    # NOT suppress Tokyo ("work" is not a city → no match → no suppression).
    for cue_m in re.finditer(r"\bfrom\s+", lowered):
        pos = cue_m.end()
        city_m = _CITY_RE.match(lowered, pos)
        if city_m is None:
            art_m = re.match(r"(?:a|an|the)\s+", lowered[pos:], re.I)
            if art_m:
                city_m = _CITY_RE.match(lowered, pos + art_m.end())
        if city_m:
            after = lowered[city_m.end(): city_m.end() + 20].lstrip()
            if re.match(r"\bto\b", after) and not _is_chained_route(city_m.end()):
                _add(city_m.start(), city_m.end())

    return spans


def _scan_origin_city(text: str) -> str | None:
    """Return the catalog slug for the first origin/home-base city in *text*,
    or None if no origin phrase is detected.  var-0: pure fn of text."""
    lowered = text.lower()
    for span_s, span_e in _origin_spans(lowered):
        for m in _CITY_RE.finditer(lowered):
            if m.start() == span_s and m.end() == span_e:
                tok = m.group(0)
                return _CITY_TOKEN_TO_SLUG.get(tok, tok)
    return None


# fix-round-1: companion/party-member NAMES that happen to collide with a
# catalog city ("with my son Austin", "my brother Warren", "my dog Milan",
# "uncle Tyler and aunt Eugene") were being bare-matched as real destination
# legs — including marquee real cities (Milan/Naples/Sydney) that CANNOT be
# blanket-denylisted the way _AMBIGUOUS_CITY_WORDS handles common nouns,
# because they are also genuine, frequently-requested trip destinations.
# Instead: a relation/companion cue word IMMEDIATELY before a token marks
# that token as a PERSON, not a place, regardless of catalog/denylist status.
# Deterministic / var-0: pure regex over input text, no LLM/clock/random.
_COMPANION_RELATION_WORDS = (
    # fix-round-4: "wife" (singular) was missing — only the irregular plural
    # "wives" was covered — so "my wife and Madison" had no relation cue for
    # "wife" at all and fell through to the bare "with" cue (which captures
    # the filler word "my" as its "name"), leaking the coordinated companion
    # name past the round-4 and-fix below.
    r"sons?|daughters?|husbands?|wife|wives|brothers?|sisters?|uncles?|aunts?|"
    r"nephews?|nieces?|cousins?|friends?|buddies|pals?|partners?|"
    r"fiance(?:e)?s?|moms?|dads?|mothers?|fathers?|grandmas?|grandpas?|"
    r"grandmothers?|grandfathers?|dogs?|cats?|pets?|"
    # fix-round-2: the single most common party/companion words were missing
    # (kids/children were the biggest gap — see #that finding), plus a batch of
    # other everyday relation words that collide with catalog cities the same way.
    r"kids?|children|child|boys?|girls?|twins?|babies|baby|toddlers?|"
    r"grandsons?|granddaughters?|boyfriends?|girlfriends?|spouses?|"
    r"colleagues?|coworkers?|co-workers?|roommates?|"
    # fix-round-4: "mate"/"mates" — one of the most common companion nouns
    # in AU/UK/SEA travel prose ("my mates Tyler and Warren") — was entirely
    # absent, so a name introduced by it got no companion cue and bare-
    # matched its catalog city as a phantom leg.
    r"mates?"
)
# fix-round-2: allow a "-in-law" suffix directly on the relation word
# ("mother-in-law", "sister-in-law") to be consumed as PART of the cue match.
# Previously \b(?:mothers?|...)\b matched only "mother" (the \b lands on the
# hyphen), leaving "-in-law Madison ..." as the "rest" text — which does NOT
# start with whitespace, so the name-capture regex failed to match and the
# in-law's name was never excluded, bare-matching as a phantom destination.
#
# fix-round-3: two more cue gaps closed here.
#   (1) the STANDALONE plural noun "in-laws" ("my in-laws Warren and Naples")
#       has no base relation word before it, so the suffix-only pattern above
#       never matched it at all — added as its own bare alternative.
#   (2) the single most common way a traveler names a companion is a BARE
#       "with <Name>" with NO relation word at all ("a week in Bali with
#       Austin and Madison"). "with" is not a destination cue anywhere in
#       this parser (see _DEST_CUE_WORDS — it is never used to introduce a
#       second city), so treating it as a companion cue here carries no risk
#       of suppressing a genuinely cued destination.
#
# fix-round-4: the "bringing <Name> along" idiom ("bringing Austin along")
# has no relation word or "with" at all, so the name got no companion cue
# and bare-matched its catalog city. "bringing" is likewise never used
# anywhere in this parser to introduce a second destination.
_COMPANION_CUE_RE = re.compile(
    r"\b(?:(?:" + _COMPANION_RELATION_WORDS + r")(?:-in-laws?)?|in-laws?|with|bringing)\b",
    re.I,
)
# Continuation for further names joined by "and" or listed by comma
# ("my daughters Madison and Sydney", "my sons Austin, Dallas and Memphis") —
# fix-round-2: was previously a single one-shot match (_COMPANION_AND_RE),
# which excluded only ONE further name and never handled a comma-separated
# enumeration, leaking every name after the 2nd as phantom destination legs.
# Now applied in a loop so an arbitrary-length list is fully excluded. An
# optional relation cue may still appear before a subsequent name ("uncle
# Tyler and aunt Eugene", but that case is already caught directly by the
# primary loop matching "aunt" as its own cue).
_COMPANION_CONT_RE = re.compile(
    r"\A(?:\s*,\s*(?:and\s+)?|\s+and\s+)(?:(?:my|our|the)\s+)?"
    r"(?:(?:" + _COMPANION_RELATION_WORDS + r")\s+)?"
    r"([A-Za-z][a-zA-Z']*)",
    re.I,
)


def _companion_name_spans(text: str) -> list[tuple[int, int]]:
    """
    Return (start, end) char spans of tokens that are travel-COMPANION names
    (immediately preceded by a relation/party cue such as son/daughter/
    husband/wife/brother/sister/uncle/aunt/friend/dog/cat/... or joined to one
    by "and"/comma-listed), so ``_scan_city_sequence_spans`` can exclude them
    even when the name happens to collide with a real catalog city.
    """
    spans: list[tuple[int, int]] = []
    for cue_m in _COMPANION_CUE_RE.finditer(text):
        rest = text[cue_m.end():]
        # fix-round-3: two adjacency gaps in the name capture.
        #   (1) a comma APPOSITIVE ("my son, Austin") leaves `rest` starting
        #       with "," rather than whitespace, so the old `\s+`-only anchor
        #       never matched at all and the name leaked through unexcluded.
        #   (2) a FILLER word between the cue and the real name ("my son
        #       named Austin", "my dog called Milan") made the old regex
        #       grab the filler ("named"/"called") as the "name", leaving the
        #       actual name one token later unexcluded.
        # fix-round-4: a THIRD gap — when the relation cue is immediately
        # followed by "and <Name>" with no name in between ("my wife and
        # Madison", i.e. the first companion IS the relation itself), this
        # regex used to greedily capture the literal token "and" as the
        # "name" and exclude only that, leaving the real coordinated name
        # (Madison/Austin/Geneva/Sydney) with no preceding cue at all — it
        # then bare-matched its colliding catalog city as a phantom leg.
        # Consuming an optional "and " before the name-capture group fixes
        # this without disturbing the "my son Jack and Warren" shape (no
        # leading "and" there, so this new optional group simply doesn't
        # match and Jack is captured exactly as before).
        name_m = re.match(
            r"[\s,]+(?:and\s+)?(?:(?:named|called)\s+)?([A-Za-z][a-zA-Z']*)", rest,
        )
        if not name_m:
            continue
        n_start = cue_m.end() + name_m.start(1)
        n_end = cue_m.end() + name_m.end(1)
        spans.append((n_start, n_end))
        # fix-round-2: keep consuming ", Name" / ", and Name" / " and Name"
        # continuations so a 3+-person enumeration is fully excluded, not
        # just the first name after the cue plus one "and"-joined name.
        cursor = n_end
        while True:
            cont_m = _COMPANION_CONT_RE.match(text[cursor:])
            if not cont_m:
                break
            c_start = cursor + cont_m.start(1)
            c_end = cursor + cont_m.end(1)
            spans.append((c_start, c_end))
            cursor = c_end
    return spans


# fix-round-1: "me[, Name]*[, and Name]" — a first-person travel-party
# enumeration ("just me and Jennifer", "for me, Emma, and Jack to Tokyo").
# Matched case-insensitively on "me" (its own sentence-initial "Me" included)
# so every companion name in the list can be excluded from unknown-place
# detection, not just the first one.
_PARTY_ENUM_RE = re.compile(
    r"\bme\b(?:\s*,\s*[A-Z][a-zA-Z]+)*(?:\s*,?\s*and\s+[A-Z][a-zA-Z]+)?",
    re.I,
)
_PARTY_ENUM_NAME_RE = re.compile(r"[A-Z][a-zA-Z]+")

# fix-round-4: mirror of _PARTY_ENUM_RE for the REVERSED ordering — a
# companion's name stated FIRST, coordinated with the speaker ("Austin and I
# want a week in Bali"). There is no relation/with/me cue before the name at
# all, so it bare-matched its colliding catalog city as a phantom leg placed
# BEFORE the genuine destination. Scoped to the very start of the text
# (optionally preceded only by whitespace) — this is the natural position for
# a coordinated-subject opener, and keeping the check anchored there means it
# can never suppress a genuinely-cued destination mentioned mid-sentence that
# happens to be followed later by "and I" ("flying to Austin and I can't
# wait").
_LEADING_NAME_AND_I_RE = re.compile(
    r"\A\s*([A-Za-z][a-zA-Z']*)\s+and\s+I\b", re.I,
)


def _leading_companion_and_i_span(text: str) -> tuple[int, int] | None:
    """
    Return the (start, end) char span of a companion's name when the text
    OPENS with "<Name> and I ..." (coordinated sentence subject, no relation/
    with cue) — a travel companion, not a destination. Returns None otherwise.
    """
    m = _LEADING_NAME_AND_I_RE.match(text)
    if not m:
        return None
    return (m.start(1), m.end(1))


# fix-round-3: a companion's given name used as the SENTENCE SUBJECT of a
# "joining"-style clause ("Austin is joining us", "Austin will join us") has
# no preceding relation/with cue at all, so it bare-matches a colliding
# catalog city the same way an un-cued companion name does. Mirrors the
# _PERSON_FOLLOWING_RE person-verb signal already used by
# _scan_unknown_place_spans, reused here so _scan_city_sequence_spans can
# exclude the same unambiguous person-verb context from real destination hits.
#
# fix-round-4: two more unambiguous "the name right after this IS a person"
# signals, folded into the same forward-lookahead check.
#   (1) a REVERSE-order appositive — the relation word follows the name
#       instead of preceding it ("Austin is my son", "Warren my brother") —
#       _companion_name_spans only ever scans forward from cue to name, so
#       this ordering was invisible to it entirely.
#   (2) a bare sentence subject under a verb outside the narrow joining-verb
#       whitelist above — a name-as-pet-copula ("Milan is our dog") or an
#       excited-companion idiom ("Tyler cannot wait").
_PERSON_VERB_FOLLOWING_RE = re.compile(
    r"^\s*(?:is\s+(?:joining|coming)\b|will\s+(?:show|join|meet|fly|arrive|come)\b|"
    r"flies?\s+in\b|flying\s+in\b|joins?\s+us\b)",
    re.I,
)

# fix-round-4: two more unambiguous "the name right after this IS a person"
# signals — a REVERSE-order appositive ("Austin is my son", "Warren my
# brother") and a bare sentence subject under a verb outside the narrow
# joining-verb whitelist above ("Milan is our dog", "Tyler cannot wait").
# Kept SEPARATE from _PERSON_VERB_FOLLOWING_RE (rather than folded into it)
# because, unlike that narrow joining-verb whitelist, these two patterns can
# plausibly follow a GENUINELY destination-cued city too ("in Paris my
# nephew Hamilton is joining us" — "Paris" is directly followed by "my
# nephew", which would otherwise false-fire and wipe out a real, cued
# destination). Gated at the call site so it only applies when the
# preceding token is NOT itself a destination cue.
_PERSON_VERB_FOLLOWING_UNCUED_RE = re.compile(
    r"^\s*(?:(?:is\s+)?my\s+(?:" + _COMPANION_RELATION_WORDS + r")\b|"
    r"is\s+(?:our|my|the)\s+(?:dog|cat|pet)s?\b|"
    r"(?:cannot|can'?t)\s+wait\b)",
    re.I,
)


def _party_enumeration_name_spans(text: str) -> list[tuple[int, int]]:
    """
    Return (start, end) char spans of capitalised NAME tokens inside a
    first-person travel-party enumeration ("me and Jennifer", "me, Emma, and
    Jack") — travel COMPANIONS, not unknown destinations.
    """
    spans: list[tuple[int, int]] = []
    for m in _PARTY_ENUM_RE.finditer(text):
        for nm in _PARTY_ENUM_NAME_RE.finditer(m.group(0)):
            if nm.group(0).lower() == "me":
                continue
            spans.append((m.start() + nm.start(), m.start() + nm.end()))
    return spans


# fix-round-1: an unambiguous signal that a STAYCATION is meant — the ORIGIN
# city named earlier in the sentence ("based in Bangkok") is also the
# destination ("planning a staycation there" / "want to spend a long weekend
# exploring it" / "want to explore the city" / "want a city break here").
# Deliberately narrow (closed vocabulary) so it only rescues the origin when
# no other destination exists at all — see the rescue pass in
# _scan_city_sequence_spans.
_STAYCATION_SIGNAL_RE = re.compile(
    r"\bstaycation\b"
    r"|\b(?:explor\w*|discover\w*|see\w*|visit\w*)\s+(?:it|here|there|the\s+city)\b"
    r"|\bcity\s+break\b.{0,25}\b(?:here|there)\b"
    r"|\b(?:here|there)\b.{0,25}\bcity\s+break\b"
    # fix-round-2: "here"/"there" anaphora to the sole named city, under a
    # non-whitelisted verb ("spend a long weekend here", "do a 5 day trip
    # here", "planning a week here") — the original whitelist only covered a
    # closed set of governing verbs (explore/discover/see/visit); an
    # unambiguous back-reference near ordinary TRIP-DURATION words (week/
    # weekend/trip/holiday/vacation/getaway/day/night) is just as clear a
    # staycation signal regardless of the verb. Scoped to a bounded distance
    # so it doesn't fire on an unrelated, far-away "here"/"there" in a long
    # sentence.
    r"|\b(?:here|there)\b.{0,30}\b(?:trip|holiday|vacation|getaway|"
    r"weeks?|weekends?|days?|nights?)\b"
    r"|\b(?:trip|holiday|vacation|getaway|weeks?|weekends?|days?|nights?)\b"
    r".{0,30}\b(?:here|there)\b"
    # fix-round-3: the whitelist above still required an explicit here/
    # there anaphora or a narrow governing verb (explore/discover/see/
    # visit) — but a traveler describing the ORIGIN city's own activities
    # ("a week of museums and pasta") or a bare trip descriptor ("a 7 night
    # getaway", "a 5 day trip", "a romantic week") with NO anaphora at all is
    # just as unambiguous a staycation request. This rescue is ONLY ever
    # consulted when the origin is the SOLE city named anywhere in the text
    # (see the "not hits" gate at the call site in
    # _scan_city_sequence_spans), so recognising any ordinary trip/duration
    # phrasing here can never steal a DIFFERENT, genuinely-named destination.
    r"|\b(?:want(?:s|ed)?|would\s+love|looking\s+for|planning|hoping\s+for|"
    r"love)\b.{0,40}\b(?:trip|getaway|vacation|holiday|tour|days?|nights?|"
    r"weeks?|weekends?)\b"
    r"|\b(?:a|an)\s+(?:\d+[\s-]*)?(?:day|days|night|nights|week|weeks)\b"
    r".{0,20}\b(?:trip|getaway|vacation|holiday|tour)\b",
    re.I,
)


def _fold_accents(s: str) -> str:
    """
    Return a SAME-LENGTH ASCII-folded copy of *s*: each accented Latin letter
    is replaced by its base ASCII letter ("í" -> "i", "í" in "Reykjavík" ->
    "Reykjavik"), one output character per input character, so char OFFSETS
    computed over the folded string still index correctly into the original.

    fix-round-1: fixes the mangled-ASCII-prefix false decline where a
    catalog city typed with its native diacritic ("Medellín", "Reykjavík")
    failed to resolve (the catalog only has the unaccented spelling), leaving
    the ASCII place-token regex to capture a truncated prefix up to the first
    non-ASCII letter ("Medell", "Reykjav") and flag THAT as an unsupported
    destination.
    """
    out_chars = []
    for ch in s:
        decomp = unicodedata.normalize("NFKD", ch)
        out_chars.append(decomp[0] if decomp else ch)
    return "".join(out_chars)


def _scan_city_sequence_spans(text: str) -> list[tuple[int, int, str]]:
    """
    Extract every RESOLVED catalog-city hit as (start, end, slug) char spans over
    *text*, in document order, with the same connective/origin/ambiguous-word
    exclusions as ``_scan_city_sequence``. This is the shared span-level core that
    ``_scan_city_sequence`` reduces to an ordered slug list, and that
    ``_scan_unknown_place`` (BUG 3 fix) consults to avoid flagging a place that was
    already resolved here as an "unknown destination".

    D6 #41 — uses _CITY_RE (one combined alternation compiled at module load)
    instead of re-compiling one regex per city token per call. The alternation
    is sorted longest-first, so the regex engine picks the longest token on
    overlap (leftmost-longest matching), eliminating the per-call O(catalog) loop.

    fix-round-1: the catalog itself is INCONSISTENT about diacritics — some
    entries keep the native accent ("malé"), others are plain ASCII
    ("medellin", "reykjavik"). So matching is done in TWO passes: first
    against the text AS-IS (catches "malé"-style entries, unchanged from
    before), then against an ASCII-accent-FOLDED copy (``_fold_accents``,
    same length so char offsets still line up) for any city NOT already
    matched — this catches a native diacritic spelling ("Reykjavík",
    "Medellín") whose catalog entry is plain ASCII. Previously the folded
    form was never tried at all, so the ASCII prefix up to the first
    diacritic leaked into the user-facing "not in the supported catalog"
    decline as a mangled fragment ("Medell", "Reykjav").
    """
    lowered = text.lower()
    _folded_lowered = _fold_accents(text).lower()

    # Spans where a catalog-city token is actually a multi-city decomposition
    # VERB ("split between X and Y"), not a destination. These are excluded from
    # the city hits so the connective 'split' does not fabricate a spurious leg,
    # while a genuinely-named Split ("in Split", "Split, Croatia") is untouched.
    connective_spans = _connective_verb_spans(lowered)

    # Spans where a catalog-city token is the OBJECT of a negation cue
    # ("anywhere but Bangkok", "not going to Paris", "except Tokyo", "skip
    # Bangkok") — a city the traveller has explicitly ruled out. Excluded
    # BEFORE origin/companion handling and never rescued (no staycation
    # fallback): a refused city must never become, or outrank, the actually-
    # wanted destination.
    _negated_spans = _negated_city_spans(lowered)

    # RC-1: Spans where a catalog-city token is used as an ORIGIN / home base
    # ("starting from Stockholm", "i'm in Singapore") — excluded so the origin
    # city is NOT taken as a destination. Optionally stashed in trip_request["origin"].
    _orig_spans = _origin_spans(lowered)

    # fix-round-1: spans where a catalog-city-shaped token is actually a
    # travel COMPANION's name ("with my son Austin", "my brother Warren",
    # "my dog Milan", "uncle Tyler and aunt Eugene") — excluded so party
    # members are never fabricated into phantom destination legs, even when
    # the name collides with a marquee real city (Milan/Naples/Sydney) that
    # cannot be blanket-denylisted. Matched against the ORIGINAL-case text
    # (relation cues are case-insensitive; names are Title-Case in practice).
    _companion_spans = _companion_name_spans(text)

    # fix-round-3: a companion name enumerated "me and X" style ("just me
    # and Austin") was already detected by _party_enumeration_name_spans, but
    # that span list was previously consulted ONLY by unknown-place detection
    # — never by this city-sequence core — so the enumerated companion still
    # bare-matched a colliding catalog city as a phantom destination leg.
    _party_spans = _party_enumeration_name_spans(text)

    # fix-round-4: a companion's name stated FIRST, coordinated with the
    # speaker ("Austin and I want a week in Bali") — no relation/with/me cue
    # precedes it at all, so it bare-matched its colliding catalog city as a
    # phantom leg placed before the real destination.
    _leading_and_i_span = _leading_companion_and_i_span(text)

    # Use the pre-built combined regex (D6 #41).  The alternation is sorted
    # longest-first so "margaret river" beats stray "river" on overlap.
    hits: list[tuple[int, int, str]] = []  # (start, end, slug)
    # fix-round-1: origin-city hits set aside here (not discarded outright) so
    # a STAYCATION request ("based in Bangkok, planning a staycation there")
    # can rescue the origin as the destination when it turns out to be the
    # ONLY city named at all — see the rescue pass after this loop.
    _origin_hits: list[tuple[int, int, str]] = []
    seen_spans: list[tuple[int, int]] = []

    def _scan_pass(src: str) -> None:
        for m in _CITY_RE.finditer(src):
            # _CITY_RE uses longest-first alternation, so overlaps don't occur for
            # the longest match. Skip any residual overlaps defensively (also
            # skips a span already matched by an earlier pass over a different
            # accent-folding of the text).
            if any(not (m.end() <= s or m.start() >= e) for s, e in seen_spans):
                continue
            # Skip a token used connectively (the 'split' in "split between ...").
            if any(m.start() == cs and m.end() == ce for cs, ce in connective_spans):
                continue
            # Skip a city token that is the OBJECT of a negation cue — a
            # REFUSED city, dropped outright (not set aside as an origin, not
            # eligible for the staycation rescue below).
            if any(m.start() == ns and m.end() == ne for ns, ne in _negated_spans):
                continue
            # RC-1: Skip a city token used as an origin / home-base location.
            if any(m.start() == os and m.end() == oe for os, oe in _orig_spans):
                tok = m.group(0)
                _origin_hits.append((m.start(), m.end(), _CITY_TOKEN_TO_SLUG.get(tok, tok)))
                seen_spans.append((m.start(), m.end()))
                continue
            # fix-round-1: Skip a city-shaped token that is really a companion's
            # given name (party-composition text), regardless of denylist status.
            if any(m.start() == cs and m.end() == ce for cs, ce in _companion_spans):
                continue
            # fix-round-3: Skip a city-shaped token that is a companion's name
            # inside a "me and X" first-person party enumeration.
            if any(m.start() == ps and m.end() == pe for ps, pe in _party_spans):
                continue
            # fix-round-4: Skip a city-shaped token that opens the text as a
            # "<Name> and I ..." coordinated sentence subject.
            if _leading_and_i_span is not None and (m.start(), m.end()) == _leading_and_i_span:
                continue
            # fix-round-3: Skip a city-shaped token used as the SUBJECT of an
            # unambiguous person-verb clause right after it ("Austin is
            # joining us", "Austin will join us") — a companion, not a place.
            if _PERSON_VERB_FOLLOWING_RE.match(src[m.end():m.end() + 40]):
                continue
            # fix-round-4: same "person, not a place" signal, but for the
            # two patterns that can also plausibly follow a GENUINELY
            # destination-cued city ("in Paris my nephew Hamilton is joining
            # us" — "Paris" is directly followed by "my nephew", which would
            # otherwise false-fire and wipe out a real, cued destination).
            # Only applied when the city is NOT itself immediately preceded
            # by a destination cue, so an actually-cued city always wins.
            if (
                not _DEST_CUE_RE.search(src[:m.start()])
                and _PERSON_VERB_FOLLOWING_UNCUED_RE.match(src[m.end():m.end() + 40])
            ):
                continue
            # fix-round-1: Skip a city-shaped token immediately followed by
            # "dollars" ("singapore dollars", "australian dollars") — this is a
            # CURRENCY name, not a destination, and was otherwise fabricating a
            # phantom leg (e.g. "budget 7000 singapore dollars" -> a bogus
            # Singapore leg) because the currency scanner consumes the amount
            # but leaves the currency-name token in the free text.
            if re.match(r"\s+dollars?\b", src[m.end():m.end() + 10]):
                continue
            tok = m.group(0)
            slug = _CITY_TOKEN_TO_SLUG.get(tok, tok)
            # #2 — ambiguous common-word city: keep ONLY with a destination cue
            # (preceding preposition/verb, or a trailing ", <region>"); otherwise it
            # is ordinary prose ("surprise me", "on sale"), not a destination.
            if tok in _AMBIGUOUS_CITY_WORDS:
                pre = src[max(0, m.start() - 25):m.start()]
                # trailing cue must be ", <Region>" — a comma then a CAPITALISED region word
                # ("Nice, France") in the ORIGINAL text, NOT a bare comma: "a new mobile, 5 days"
                # and "a hot bath, japan" are ordinary prose, not a destination.
                _region_cue = re.match(r",\s*[A-Z]", text[m.end():m.end() + 4]) is not None
                if not _DEST_CUE_RE.search(pre) and not _region_cue:
                    continue
            hits.append((m.start(), m.end(), slug))
            seen_spans.append((m.start(), m.end()))

    # Pass 1: text as-is — catches catalog entries that keep the native accent
    # ("malé"). Pass 2: accent-folded — catches a diacritic INPUT spelling
    # whose catalog entry is plain ASCII ("medellin", "reykjavik"); skips any
    # span pass 1 already claimed, so pass 1 always wins on conflict.
    _scan_pass(lowered)
    if _folded_lowered != lowered:
        _scan_pass(_folded_lowered)

    # fix-round-1: STAYCATION rescue — origin-city suppression (RC-1, above)
    # unconditionally drops the origin from the destination list, which is
    # correct when a real elsewhere-destination is also named ("i'm in
    # Singapore, flying to Bangkok") but wrong when the origin city is the
    # ONLY city named and the text unambiguously points back at it as the
    # destination ("staycation there", "explore it", "explore the city",
    # "city break here"). Only fires when no other destination was found at
    # all, so a genuine elsewhere-trip is never affected.
    if not hits and _origin_hits and _STAYCATION_SIGNAL_RE.search(text):
        hits = _origin_hits

    hits.sort(key=lambda h: h[0])
    return hits


def _scan_city_sequence(text: str) -> list[str]:
    """
    Extract an ORDERED, de-duplicated-by-position list of catalog cities from
    text, in the order they appear (document order).

    "Bangkok then KL then Singapore" → ["bangkok", "kuala lumpur", "singapore"].
    "Perth, Margaret River, Albany"   → ["perth", "margaret-river", "albany"].

    Each returned value is the canonical catalog city slug. Consecutive
    repeats of the SAME city are preserved only once per contiguous mention
    (so "bali, bali beach" stays a single bali leg here — multi-vibe single-city
    splitting is handled separately by _scan_vibe_sequence). Returns catalog
    slugs; may be empty.
    """
    ordered: list[str] = []
    for _s, _e, slug in _scan_city_sequence_spans(text):
        if ordered and ordered[-1] == slug:
            continue  # collapse a contiguous repeat of the SAME city
        ordered.append(slug)
    return ordered


# Heuristic stop-words that look like capitalised "place" tokens but are not
# cities — avoids false unknown-city declines on ordinary words.
_NON_CITY_TOKENS: frozenset[str] = frozenset({
    "i", "we", "my", "the", "a", "an", "trip", "travel", "holiday", "vacation",
    "days", "day", "nights", "night", "week", "weeks", "budget", "solo",
    "couple", "please", "book", "me", "for", "to", "then", "and", "with",
    "usd", "aud", "thb", "sgd", "myr", "idr", "etb", "eur", "gbp",
    "december", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november",
    # Common holiday/season words that can appear in destination-position text
    # without being place names (D6 #40 — avoid false unknown-city decline).
    "christmas", "easter", "new year", "diwali", "eid", "ramadan",
    "spring", "summer", "autumn", "fall", "winter",
    # RC-2: conversational filler tokens — these NEVER name a destination.
    # A bare "Ok sure, ..." or "Hi, I want to go to..." must not flag the
    # filler word as an unknown destination → cannot_satisfy.
    "ok", "okay", "sure", "hi", "hello", "hey", "thanks", "thank",
    "yes", "yeah", "no", "nope", "well", "so", "alright", "cheers",
    "kindly", "ill", "hmm", "maybe",
    # Live-test bug: common no-apostrophe contraction fragments ("Im", "Ive", ...)
    # sitting right after a routing/destination cue word ("to Im looking for
    # hike") were mistaken for an unknown capitalised place name, producing a
    # false "destination 'Im' is not in the supported catalog" decline.
    # _is_known() lowercases before comparing, so lowercase entries here match
    # both "Im" and "im". Deliberately closed/curated (first-person + common
    # subject-pronoun contractions) — NOT a general spell-checker/dictionary.
    "im", "ive", "id", "its", "youre", "theyre", "were", "cant", "wont",
    "dont", "isnt", "arent", "didnt", "doesnt", "youve", "theyve", "weve",
    "youll", "theyll", "shes", "hes", "thats", "whats", "lets",
})

# Calendar months — full names AND common 3-letter abbreviations (+ "sept").
# A capitalised month in a destination/routing slot ("...in Oct", "visit Jan")
# must NOT be mistaken for an unknown city (month-abbrev false-decline fix).
# Case-insensitive: _is_known lowercases before the check. This only suppresses
# the false unknown-PLACE decline — a real catalog city in the same string is
# still booked by _scan_city_sequence (catalog match), independent of this set.
# SAFETY: verified no catalog city/alias collides with any month token.
_MONTH_TOKENS: frozenset[str] = frozenset({
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul",
    "aug", "sep", "sept", "oct", "nov", "dec",
})

# Geographic CONTAINERS (countries / states / regions / islands) that our
# catalog cities live IN. These are CONTEXT words, not substitutable competing
# destinations: "a road trip in Western Australia, start in Perth..." names WA
# as the container of perth/margaret-river/albany — it must NOT trigger the
# unknown-destination honest-decline. Suppressing them here is SAFE: it only
# prevents a false decline; if the text contains no actual catalog city, the
# downstream "no recognisable city" decline still fires (never a silent collapse,
# never a substitution). NOTE (Stage D): when the catalog expands to 89 countries
# this set should be GENERATED from the country/region list, not hand-maintained.
# CONTINENTS / macro-regions: a traveller who says "first time in Africa, thinking
# Marrakech" or "see all of Japan" is naming the CONTAINER, not a bookable destination.
# A continent has no single gateway city, so — unlike a country — we do NOT substitute a
# city; we simply refuse to treat the continent token as an unknown destination. If the
# text ALSO names a supported city (Marrakech), that city is planned; if it names none,
# the normal "no recognisable city" decline still fires honestly (never a silent collapse).
_CONTINENT_REGION_TOKENS: frozenset[str] = frozenset({
    "africa", "north africa", "sub-saharan africa", "west africa", "east africa",
    "southern africa", "central africa",
    "north america", "south america", "central america", "latin america",
    "the americas", "americas",
    "oceania", "australasia", "antarctica",
    "middle east", "the middle east",
    # Named macro-deserts / feature-regions travellers cite as a "place" but which are
    # not a bookable city (the Sahara case).
    "sahara", "sahara desert", "the sahara",
})

_GEO_CONTAINER_TOKENS: frozenset[str] = frozenset({
    # Sub-national / regional containers + generic regions (not catalog countries).
    "western australia", "wa",
    "southeast asia", "south east asia", "se asia", "asia",
    "europe", "scandinavia", "patagonia", "the balkans", "balkans",
}) | _CONTINENT_REGION_TOKENS | _CATALOG_COUNTRIES  # containers/regions — not destinations


def _scan_unknown_place_spans(text: str) -> list[tuple[str, int, int]]:
    """
    Detect EVERY clearly-named destination that is NOT in the catalog, in document
    order, so a request can either honestly decline (no supported city at all) or
    GRACEFULLY DROP the unsupported leg(s) with a notice (when supported cities are
    also present — see parse_intent's partial-planning path).

    Heuristic: a "City → City" / "City, City" routing pattern, or a
    "in/to/visit <Capitalised Place>" phrase, where the place is not a known
    catalog city/alias. Returns each offending token WITH its char span
    ``(token, start, end)`` over *text* (the span lets callers detect a resolved
    catalog token swallowed by an unknown span — see _misresolved_city_slugs),
    or [] if no unknown place is confidently identified.

    Conservative by design: only fires on tokens that LOOK like a place name in
    a routing/destination position, so ordinary prose does not false-positive.

    D6 #40 — before declaring a token unknown, lowercase it and check it against
    ALLOWED_VIBES, _VIBE_SYNONYMS, and the month names in _NON_CITY_TOKENS so
    capitalised vibe words ("Relax", "Beach") or months ("Christmas", "December")
    following a routing cue do NOT trigger a false unknown-destination decline.

    BUG 3 fix (review): this function's place-token regex caps at TWO
    ASCII-only capitalised words and does not recognise non-ASCII letters, so a
    genuinely resolved multi-word/non-ASCII catalog city ("Ho Chi Minh City",
    "Malé") can extract a mangled PREFIX ("Ho Chi", "Mal") that matches nothing
    — falsely declining a destination _scan_city_sequence already resolved
    correctly. Rather than widen the place regex (which risks other
    regressions), any candidate span that OVERLAPS a span already resolved by
    _scan_city_sequence is skipped here (yielded to), not flagged as unknown.
    """
    known = set()
    for c in ALLOWED_CITIES:
        known.add(c)
    for a in CITY_ALIASES:
        known.add(a)

    def _is_known(tok: str) -> bool:
        t = tok.strip().lower()
        return (
            t in known
            or t in _NON_CITY_TOKENS
            or t in _MONTH_TOKENS          # months/abbrevs: "Oct","Jan",... (lowercased)
            or t in _GEO_CONTAINER_TOKENS  # container region, not a destination
            or t in CURRENCY_SYMBOLS       # "RM 8000" / "Rp 20m" — a currency, not a city
            or t in _KNOWN_CURRENCY_CODES
            or t in ALLOWED_VIBES          # D6 #40: "Relax", "Beach" etc. (lowercase check)
            or t in _VIBE_SYNONYMS         # D6 #40: "Relaxing", "Coastal" etc.
            or t == ""
        )

    # BUG 3: spans _scan_city_sequence already resolved as a real catalog city — a
    # candidate "unknown place" token overlapping one of these is a mangled fragment
    # of an already-resolved city, not a genuinely unknown destination.
    #
    # Fix: must require the CANDIDATE span to be fully CONTAINED WITHIN
    # a resolved span (not merely overlapping it). The Ho Chi Minh City case is
    # candidate ⊆ resolved (this function's 2-word-capped regex under-captures a
    # longer already-resolved name — "Ho Chi" sits entirely inside "ho chi minh
    # city"'s span). A plain overlap check also silently swallowed the OPPOSITE
    # shape — candidate ⊋ resolved, e.g. "Reading Pennsylvania": the resolved span
    # covers only "Reading", but the 2-word candidate span extends past it to
    # "Pennsylvania", which was never resolved and isn't a known trailing word.
    # Requiring full containment rejects that case (correctly re-declining it)
    # while still accepting the true mangled-prefix case.
    _resolved_spans = [(s, e) for s, e, _slug in _scan_city_sequence_spans(text)]

    def _overlaps_resolved(start: int, end: int) -> bool:
        return any(start >= s and end <= e for s, e in _resolved_spans)

    # fix-round-1: a companion/party-member's given NAME sitting after a
    # routing cue (",", "and") is structurally identical to an unknown place
    # ("Bangkok trip, me and Chloe" / "A getaway, just me and Jennifer") — this
    # function otherwise flags the friend as an unsupported destination and
    # either reports a false "dropped leg" notice (when a real city is also
    # present) or hard-declines the whole request naming the person as a
    # mistyped city. Scoped tightly to an explicit "me[, Name]*[, and Name]"
    # travel-party enumeration so ordinary unknown-place detection elsewhere
    # is untouched.
    _party_spans = _party_enumeration_name_spans(text)

    def _overlaps_party(start: int, end: int) -> bool:
        return any(start == s and end == e for s, e in _party_spans)

    # fix-round-2: a small, curated set of common English given names that
    # are NEVER also catalog cities — if one WERE also a real catalog city
    # (e.g. "Madison", "Sydney", "Victoria"), it would already resolve as
    # that city via `_is_known` upstream and never reach this unknown-place
    # path at all, so adding these here cannot suppress a genuine city
    # decline. Sitting bare after a routing cue with NO other textual signal
    # ("a trip to Emma", "flying out to Jennifer") is genuinely ambiguous
    # between a person and a mistyped/unsupported city, but confidently
    # accusing a common first name of being "not in the supported catalog"
    # is worse than a neutral "which destination?" — so these resolve to
    # "no destination named" instead of a false unknown-place decline.
    # fix-round-3: the 3-name allowlist was confirmed to be far too narrow —
    # every other common given name (verified: Sarah, Marco, David, Maria,
    # Chen, Ahmed, Olivia, Liam, Priya, Kenji) still fell through as a false
    # unknown-destination decline/drop. Widened with more everyday given
    # names spanning several naming traditions, same non-catalog-collision
    # reasoning as above (a name that WERE also a catalog city would already
    # resolve upstream and never reach this path).
    _COMMON_GIVEN_NAMES_NOT_DESTINATIONS: frozenset[str] = frozenset({
        "emma", "jennifer", "sophia",
        "sarah", "marco", "david", "maria", "chen", "ahmed", "olivia",
        "liam", "priya", "kenji", "james", "michael", "john", "robert",
        "jessica", "laura", "daniel", "anna", "lisa", "kevin", "ryan",
        # fix-round-4: still-fixed-size allowlist confirmed leaking on more
        # everyday given names from multiple naming traditions (Aditya, Ravi —
        # South Asian; Aiko — Japanese; Bjorn — Nordic) — same non-catalog-
        # collision reasoning as above.
        "aditya", "ravi", "aiko", "bjorn",
    })

    # fix-round-3: a named ATTRACTION/VENUE inside the city the user already
    # named ("Tokyo and Sensoji", "Rome and the Colosseum") is the venue arm
    # of the same non-catalog-proper-name class — the sight sits INSIDE the
    # already-named city, not a second destination, but was flagged and
    # dropped as an unsupported catalog miss. A small, curated set of
    # world-famous landmark names, same reasoning/pattern as the given-names
    # set above.
    _LANDMARK_WORDS_NOT_DESTINATIONS: frozenset[str] = frozenset({
        "sensoji", "meiji shrine", "colosseum", "louvre", "wat arun",
        "eiffel tower", "taj mahal", "great wall", "angkor wat",
        "forbidden city", "times square", "central park", "golden gate",
        "hagia sophia", "acropolis", "sistine chapel", "notre dame",
        "sagrada familia", "machu picchu",
        # fix-round-4: this allowlist-enumeration approach is inherently
        # incomplete — "wat arun" was covered but the equally iconic Bangkok
        # temple "wat pho" was not, and whole classes of real sub-city places
        # (neighbourhoods/districts) were entirely absent.
        "wat pho", "trastevere",
    })

    # A place-like token: a capitalised word, optionally a 2-word name
    # ("Margaret River"), but NOT immediately followed by a lowercase vibe/word
    # that would make it an adjective. We capture greedily then validate.
    # fix-round-4: also allow a hyphenated LOWERCASE continuation ("Senso-ji")
    # — a common transliteration spelling for landmark names — distinct from
    # the space/hyphen + CAPITALISED continuation above (multi-word proper
    # names like "Margaret River"). Without this, the regex captured only the
    # leading fragment ("Senso"), which then bypasses the curated landmark
    # allowlist below (keyed on the full name) and is flagged/dropped as an
    # unknown destination.
    place = r"[A-Z][a-zA-Z]+(?:[ -][A-Z][a-zA-Z]+|-[a-z]+)?"

    # A token sitting in a DESTINATION/ROUTING position is one that:
    #   (1) directly follows a routing/destination cue word, OR
    #   (2) directly follows a routing arrow / comma between place mentions.
    # We scan capitalised tokens after each cue and flag the first unknown one.
    cue = r"(?:->|→|–|—|;|,|\bthen\b|\bto\b|\bin\b|\bvisit\b|\bexplore\b|\bvia\b|\band\b)"
    # fix-round-2: a companion/person's given name sitting right after a BARE
    # routing cue ("and Marco will show us around", "then Emma flies in to
    # join us", "to propose to Jennifer") — not preceded by an explicit
    # relation word (_companion_name_spans) and not inside a "me and X" party
    # enumeration (_party_enumeration_name_spans) — still fell through as a
    # false unknown destination, either a confusing "dropped leg" notice or a
    # hard decline accusing the person's name of being a mistyped city. These
    # two narrow, curated signals catch a person by the unambiguous PERSON-
    # VERB context around the name, without touching ordinary destination
    # cues ("Bangkok then Tokyo", "fly to Paris").
    _PERSON_FOLLOWING_RE = re.compile(
        r"^\s*(?:will\s+(?:show|join|meet|fly|arrive|come)\b|"
        r"flies?\s+in\b|flying\s+in\b|is\s+coming\b|joins?\s+us\b)",
        re.I,
    )
    _PERSON_PRECEDING_CUE_RE = re.compile(r"\bpropos\w*\s*$", re.I)
    spans: list[tuple[str, int, int]] = []
    for m in re.finditer(rf"{cue}\s+({place})", text):
        tok = m.group(1)
        # BUG 3: yield to a span already resolved as a real catalog city — do not
        # flag a mangled fragment of it ("Ho Chi" of "Ho Chi Minh City") as unknown.
        if _overlaps_resolved(m.start(1), m.end(1)):
            continue
        # fix-round-1: yield to a companion's name inside a "me and/,..." party
        # enumeration — a person, not a destination.
        if _overlaps_party(m.start(1), m.end(1)):
            continue
        # fix-round-2: yield to a companion/person name signalled by an
        # unambiguous person-verb phrase right after it ("Marco will show us
        # around", "Emma flies in to join us") or a "propose to <Name>" cue
        # right before it.
        if _PERSON_FOLLOWING_RE.match(text[m.end(1):m.end(1) + 40]):
            continue
        if _PERSON_PRECEDING_CUE_RE.search(text[:m.start()]):
            continue
        # fix-round-2: a common given name with zero other context — see the
        # curated set above.
        if tok.strip().lower() in _COMMON_GIVEN_NAMES_NOT_DESTINATIONS:
            continue
        # fix-round-3: yield to a curated famous-landmark/attraction name —
        # a sight, not a second destination.
        # fix-round-4: also match with hyphens stripped, so a hyphenated
        # transliteration spelling ("Senso-ji") matches the same curated key
        # as its closed-up form ("Sensoji") — see the `place` regex comment.
        _tok_norm = tok.strip().lower()
        if (
            _tok_norm in _LANDMARK_WORDS_NOT_DESTINATIONS
            or _tok_norm.replace("-", "") in _LANDMARK_WORDS_NOT_DESTINATIONS
        ):
            continue
        # Drop a trailing capitalised word if it's actually a sentence-y token.
        if not _is_known(tok):
            # Guard: ignore if the whole multi-word token's first word is known
            # (e.g. "Margaret River" handled by alias) — _is_known covers that.
            spans.append((tok.strip(), m.start(1), m.end(1)))

    return spans


def _scan_unknown_places(text: str) -> list[str]:
    """Deduplicated unknown-destination tokens in document order (drops the char
    spans exposed by ``_scan_unknown_place_spans``). Preserves the list[str]
    contract used by parse_intent's decline/drop-notice path and by tests."""
    out: list[str] = []
    for tok, _s, _e in _scan_unknown_place_spans(text):
        if tok not in out:
            out.append(tok)
    return out


def _misresolved_city_slugs(text: str) -> set[str]:
    """Catalog slugs that resolved from a bare token sitting STRICTLY INSIDE a
    flagged-unknown destination span (candidate ⊋ resolved). "Reading Pennsylvania"
    is unknown, but its bare first word "Reading" independently resolves to Reading,
    England — a same-named mis-resolution. Planning that slug while claiming to have
    dropped "Reading Pennsylvania" would SILENTLY SUBSTITUTE a different city,
    violating the "I will not silently substitute a different city" invariant. The
    slug is returned here so parse_intent can remove it from the planned sequence
    (and, when nothing genuine remains, honestly decline the whole request).

    Only a slug whose EVERY resolved span is swallowed by an unknown span is
    returned: a city that ALSO has a legitimate standalone mention elsewhere in the
    same request is preserved (multi-city preservation is never weakened). Pure
    deterministic regex over *text* — var-0 safe."""
    unknown_spans = _scan_unknown_place_spans(text)
    if not unknown_spans:
        return set()
    inside: set[str] = set()
    outside: set[str] = set()
    for rs, re_end, slug in _scan_city_sequence_spans(text):
        swallowed = any(
            rs >= us and re_end <= ue and (rs > us or re_end < ue)
            for _tok, us, ue in unknown_spans
        )
        (inside if swallowed else outside).add(slug)
    return inside - outside


def _scan_unknown_place(text: str) -> str | None:
    """First clearly-named unsupported destination (or None). Thin wrapper over
    ``_scan_unknown_places`` — preserves the original single-token contract for
    callers/tests that only need to know IF an unknown place is present."""
    places = _scan_unknown_places(text)
    return places[0] if places else None


def _dedupe_cities_preserve_order(cities: list[str]) -> list[str]:
    """Collapse a city sequence to DISTINCT catalog slugs, preserving first-mention
    order. ``_scan_city_sequence`` only collapses *contiguous* repeats; this also
    removes a non-contiguous repeat that arises when a district alias resolves to a
    parent city already named elsewhere in the request ("Kyoto and Osaka ... near
    Gion and Namba" → Gion→kyoto / Namba→osaka would otherwise re-add kyoto/osaka).
    Multi-city preservation is unaffected: every DISTINCT supported city is kept."""
    seen: set[str] = set()
    out: list[str] = []
    for c in cities:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _scan_vibe(text: str) -> str | None:
    """Scan text for vibe keywords. Returns first canonical match."""
    lowered = text.lower()
    # Check direct vibe names first
    for vibe in ALLOWED_VIBES:
        if re.search(r"\b" + re.escape(vibe) + r"\b", lowered):
            return vibe
    # Check synonyms
    for synonym, canonical in _VIBE_SYNONYMS.items():
        if re.search(r"\b" + re.escape(synonym) + r"\b", lowered):
            return canonical
    return None


# Sentinel: a budget amount was clearly stated but in a currency we cannot
# normalise. Distinguishes "no budget at all" (→ needs_clarification: missing
# budget) from "non-USD budget in an unsupported currency" (→ honest currency
# decline). NEVER silently treated as USD.
UNKNOWN_CURRENCY: object = object()

# fix-round-1: a hand-curated set of well-known ISO currency CODES that are
# NOT in _KNOWN_CURRENCY_CODES (so we cannot convert them), matched
# case-INSENSITIVELY. This is deliberately a curated list (not a blanket
# case-insensitive [A-Za-z]{3}) so that ordinary lowercase 3-letter words
# ("for", "the", "and") sitting next to a number never misfire as a currency
# decline — only real currency-code-shaped tokens do. Fixes the case-sensitivity
# gap where "JPY 200000" honestly declined but "jpy 200000" silently priced as USD.
_UNSUPPORTED_CURRENCY_CODES: frozenset[str] = frozenset({
    "jpy", "cny", "rmb", "krw", "inr", "php", "vnd", "twd", "hkd", "rub",
    "brl", "mxn", "pkr", "bdt", "egp", "sar", "aed", "nzd", "cad", "chf",
    "zar", "try", "kwd", "qar", "npr", "lkr", "kes", "ngn", "pln", "ils",
    # fix-round-3: residual high-magnitude currencies still missing — a
    # lowercase code for any of these fell through to the bare_number
    # fallback and was silently priced 1:1 as USD (Hungarian forint is
    # ~360x over) even though the uppercase-code path already honestly
    # declines the SAME code shouted in caps ("HUF 500000").
    "huf", "czk", "nok", "dkk", "lak", "khr", "mmk", "uah", "ron", "crc",
})
# fix-round-1: spelled-out currency WORDS for the same unsupported currencies
# (yen/yuan/won/rupees/pesos/...), matched case-insensitively. These must be
# recognised as a currency BEFORE the weak bare_number fallback (step 6) so a
# stated-but-unsupported currency word is honestly declined (UNKNOWN_CURRENCY)
# instead of silently priced 1:1 as USD or mis-routed to the "no budget" slot.
_UNSUPPORTED_CURRENCY_WORDS: tuple[str, ...] = (
    r"yen", r"yuan", r"rmb", r"wons?", r"rupees?", r"pesos?", r"rubles?",
    r"dong", r"dirhams?", r"riyals?", r"rials?", r"liras?",
    # fix-round-2: compound "X dollars" currencies whose per-unit value is
    # nowhere close to USD 1:1 (Canadian/Hong Kong/New Zealand/Kiwi dollars) —
    # previously fell through to the bare_number step and were silently
    # priced 1:1 as USD (~7.8x over for HKD). Matched as multi-word phrases,
    # BEFORE the bare "dollars" word entry in step 2, so they never reach it.
    r"canadian\s+dollars?", r"hong\s+kong\s+dollars?",
    r"new\s+zealand\s+dollars?", r"kiwi\s+dollars?",
    # fix-round-2: other named non-dollar currencies missing from this list
    # (Brazilian real, South African rand, Israeli shekel, Polish zloty,
    # Bangladeshi taka, Swedish krona, Swiss franc, Nigerian naira) — same
    # silent-USD-or-mis-routed failure mode as yen/won/etc above. "reais"
    # (plural) is used rather than bare "real" to avoid colliding with the
    # common English adjective ("a real vacation").
    r"reais", r"rand", r"shekels?", r"zloty", r"taka", r"kronor|krona",
    r"swiss\s+francs?|francs?", r"naira",
    # fix-round-3: more spelled-out currency WORDS still missing (Hungarian
    # forint, Czech koruna, Norwegian/Danish krone, Lao kip, Cambodian riel,
    # Myanmar kyat, spoken East-African "shillings" — KES the CODE was
    # already covered but not the spoken word, Ukrainian hryvnia, Romanian
    # lei, the full word "renminbi" — yuan/rmb/cny codes were covered but not
    # this spoken form, Costa Rican colon) — same silent-1:1-USD failure mode.
    r"forints?", r"korunas?",
    # fix-round-4: "krone" (singular) and "krones?" (a non-word variant) were
    # covered, but NOT "kroner" — the actual standard Norwegian/Danish PLURAL
    # of krone, and the form a real Oslo/Copenhagen traveler would most
    # naturally type. Silently fell through to bare_number/USD (~7-11x
    # over-price) with a false "no currency stated" note.
    r"krones?|kroner", r"kips?", r"riels?", r"kyats?",
    r"shillings?", r"hryvnias?", r"leis?", r"renminbi", r"colon(?:es)?s?",
    # fix-round-4: more high-magnitude-variance currencies whose spelled-out
    # WORD form was missing entirely (no FX conversion path exists for any
    # of these), so each fell through to the bare_number/USD 1:1 fallback —
    # "dinar" spans Jordan/Kuwait/Tunisia/Iraq/Bahrain/Algeria with wildly
    # different per-unit values, so 1:1 USD is catastrophically wrong in
    # either direction (Jordan/Amman is a common destination).
    r"dinars?", r"tenge", r"levs?|leva", r"afghanis?", r"cedis?",
    # fix-round-4: colloquial money words with no ISO/word entry at all —
    # "quid" (British slang for GBP, a SUPPORTED currency: mis-prices AND
    # falsely denies a currency was named) and "bucks" (US slang for USD:
    # value coincidentally correct, but the "no currency stated" note still
    # falsely contradicts the user's own words).
    r"quid", r"bucks?",
)


def _convert_to_usd_cents(amount: float, currency: str) -> int | None:
    """
    Convert a foreign-currency amount to USD cents using the SEEDED FX table.

    Deterministic (fixed rates) → variance-0. Returns None for unknown currency
    or non-positive amount.

    PHASE-0 RECONCILE (§18 / §19.5): the actual conversion is now owned by the
    FX PROVIDER SEAM (fx_provider.convert_to_usd_cents), which wraps THIS module's
    SEEDED_FX_USD_PER_UNIT table in the §18 provider shape. We route through the
    seam rather than duplicating the math — the front-door currency rider and the
    provider seam share ONE FX implementation. The fallback below preserves the
    original behaviour byte-for-byte if the seam is somehow unavailable, so the
    merged multi-city behaviour can never regress.

    The import is lazy (inside the function) to avoid a circular import:
    fx_provider imports SEEDED_FX_USD_PER_UNIT from this module at load time.
    """
    if amount <= 0:
        return None
    try:
        from utils.fx_provider import convert_to_usd_cents as _seam_convert
        return _seam_convert(amount, currency)
    except Exception:  # pragma: no cover — defensive fallback (preserves prior math)
        rate = SEEDED_FX_USD_PER_UNIT.get(currency.lower())
        if rate is None:
            return None
        cents = int(round(amount * rate * 100))
        return cents if cents > 0 else None


# #honesty-fix (GAP 2, silent-currency-assumption): split into a raw scan that ALSO
# reports the provenance of a resolved budget's currency, and a thin back-compat wrapper
# that discards it — same shim pattern as _scan_adults_raw/_scan_adults and
# _parse_date_raw/_parse_date above. `provenance` distinguishes a bare "$" (a defensible,
# conventional USD assumption — NOT flagged) from a genuinely bare NUMBER with zero
# currency signal anywhere in the text (silently defaulted to USD — flagged via
# `assumed_currency` in _clamp_and_validate).
def _scan_budget_raw(text: str) -> tuple[int | None, object | None, str | None]:
    """Thin wrapper over `_scan_budget_raw_impl` that additionally honours an
    explicit mid-sentence self-correction of a bare-"$" budget — see
    `_BUDGET_CORRECTION_CUE_RE` below. Kept as a wrapper (rather than folded
    into the many-branch impl) so the correction check is a single, isolated
    override applied AFTER normal scanning, with zero risk of disturbing any
    of the impl's existing branches/precedence.
    """
    cents, err, provenance = _scan_budget_raw_impl(text)
    # fix-round-5 (verified live bug): "budget $2000, wait no, $3000" scanned
    # to $2000 — first-match-wins — even though "wait no" plainly marks $2000
    # as retracted in favour of the restated $3000. Scoped narrowly: only
    # overrides when the ORIGINAL match was the bare_dollar/bare_number "$"
    # provenance (the case the verified bug and its regression test cover);
    # a currency-code/word/symbol match is left alone rather than risking a
    # currency-mismatched override for a case that hasn't been observed.
    if provenance in ("bare_dollar", "bare_number"):
        corrected = _correction_override_budget_cents(text)
        if corrected is not None:
            return corrected, None, provenance
    return cents, err, provenance


def _correction_override_budget_cents(text: str) -> int | None:
    """Return the LAST "$<amount>" restated immediately after a correction cue
    ("actually", "wait no", "I mean", "scratch that", "make it"), in USD
    cents, or None if no such correction is present. Deliberately narrow —
    only the bare "$" form, matching the verified bug — NOT a general
    last-value-wins rule for every budget phrasing.
    """
    last: int | None = None
    for m in _BUDGET_CORRECTION_CUE_RE.finditer(text):
        raw = m.group(1).replace(",", "").strip()
        mult = 1
        if raw and raw[-1] in "kK":
            mult = 1_000
            raw = raw[:-1]
        elif raw and raw[-1] in "mM":
            mult = 1_000_000
            raw = raw[:-1]
        try:
            v = float(raw) * mult
        except ValueError:
            continue
        if v > 0:
            last = int(round(v * 100))
    return last


# fix-round-5: correction-cue phrases, each optionally chained ("actually,
# wait no, make it $3000"), immediately followed by a re-stated bare-"$"
# amount. Matches the verified live bug ("budget $2000, wait no, $3000").
_BUDGET_CORRECTION_CUE_RE = re.compile(
    r"(?:actually|wait,?\s*no|i\s*mean|scratch\s+that|make\s+it)"
    r"(?:[,\s]+(?:actually|wait,?\s*no|i\s*mean|scratch\s+that|make\s+it))*"
    r"[,\s]+\$\s*(\d[\d,]*(?:\.\d+)?[kKmM]?)\b",
    re.I,
)


def _scan_budget_raw_impl(text: str) -> tuple[int | None, object | None, str | None]:
    """
    Scan text for a budget and its currency, returning (usd_cents, error, provenance).

    Returns one of:
      (cents, None, provenance)    — a budget was found and normalised to USD cents.
                                     `provenance` is one of "code"/"word"/"symbol"/
                                     "bare_dollar"/"bare_number" — see below.
      (None, None, None)           — NO budget present in the text at all.
      (None, UNKNOWN_CURRENCY, None) — a budget amount was found, but in a currency
                                     we cannot convert (honest currency decline;
                                     NEVER silently treated as USD).

    Recognises (currency precedence: explicit code/symbol > bare "$"/"dollars"):
      "$1,500" / "1500 dollars" / "1500 USD"  → USD
      "AUD 3000" / "3000 AUD"                  → AUD (FX → USD)
      "฿50,000" / "THB 50000" / "50000 baht"  → THB (FX → USD)
      "RM 5000" / "5000 MYR" / "Rp 20,000,000" / "€2000" / "£1500" / "ETB 80000"
      A clearly-stated amount in an UNSUPPORTED currency (e.g. "JPY 200000",
      "₹150000")                              → UNKNOWN_CURRENCY (decline).

    `provenance`:
      "code"        — explicit ISO currency code, e.g. "AUD 3000" / "3000 AUD"
      "word"        — currency word, e.g. "2000 dollars" / "50000 baht"
      "symbol"      — a non-"$" currency symbol, e.g. "€2000" / "RM 5000"
      "bare_dollar" — a bare "$" symbol, e.g. "$2500" — a defensible, conventional
                      USD assumption (NOT flagged as an honesty-fix assumption)
      "bare_number" — a plain number with NO currency cue anywhere ("2500 budget") —
                      silently assumed USD with zero signal (flagged via
                      `assumed_currency` — see _clamp_and_validate)
    """
    # Trailing magnitude suffix (k=thousand, m=million) so "$2k" / "1.5k" / "$3m"
    # parse correctly instead of reading the bare digits ("$2k" -> $2 was a bug).
    #
    # D6 #13 — anchor the suffix DIRECTLY to the digits (no \s? gap) and require
    # a non-letter word boundary after it so "3 months" / "50 minutes" / "2 kids"
    # cannot absorb the leading 'm'/'k' of the next word and fabricate a budget.
    # "3k" → 3000  ✓;  "3 months" → 3 (bare digit, no suffix) ✓;  "$3m" → 3M ✓.
    num = r"([\d][\d,]*(?:\.\d+)?[kKmM]?)(?![A-Za-z])"

    def _val(s: str) -> float | None:
        s = s.replace(",", "").strip()
        mult = 1
        if s and s[-1] in "kK":
            mult = 1_000; s = s[:-1]
        elif s and s[-1] in "mM":
            mult = 1_000_000; s = s[:-1]
        try:
            v = float(s) * mult
            return v if v > 0 else None
        except ValueError:
            return None

    # ---- 1. Explicit ISO currency code adjacent to a number (either order) ----
    # Build an alternation of every code we KNOW (supported) so we can convert.
    known_codes = "|".join(sorted(_KNOWN_CURRENCY_CODES, key=len, reverse=True))
    for pat in (
        rf"\b({known_codes})\s*{num}",   # "AUD 3000"
        rf"{num}\s*\b({known_codes})\b",  # "3000 AUD"
    ):
        for m in re.finditer(pat, text, re.I):
            g = m.groups()
            code, amt = (g[0], g[1]) if g[0].isalpha() else (g[1], g[0])
            v = _val(amt)
            if v is not None:
                cents = _convert_to_usd_cents(v, code)
                if cents is not None:
                    return cents, None, "code"

    # ---- 2. Word forms: "dollars"/"baht"/"ringgit"/"rupiah"/"birr"/"euros"/"pounds"
    # The "dollars" form tolerates an optional "US"/"U.S."/"USD"/"American" qualifier
    # BETWEEN the amount and the word ("7000 US dollars", "7000 American dollars") — a
    # very common phrasing that otherwise slipped through (the number was no longer
    # directly adjacent to "dollars"), producing a spurious "no budget" clarification.
    word_to_code = {
        # fix-round-1: spoken forms of SUPPORTED currencies (AUD/SGD) whose
        # word forms were previously unrecognised — checked BEFORE the bare
        # "dollars" entry so "singapore dollars" / "aussie dollars" resolve
        # to SGD/AUD instead of falling through to a spurious "no budget"
        # decline (or, worse, a bare_number USD mis-price).
        r"(?:sgd|singapore)\s+dollars?": "sgd",
        r"(?:aud|australian|aussie)\s+dollars?": "aud",
        r"(?:us|u\.s\.?a?\.?|usd|american)?\s*dollars?": "usd",
        r"baht": "thb", r"ringgit": "myr",
        r"rupiah": "idr", r"birr": "etb", r"euros?": "eur", r"pounds?": "gbp",
    }
    for word, code in word_to_code.items():
        m = re.search(rf"{num}\s*{word}\b", text, re.I)
        if m:
            v = _val(m.group(1))
            if v is not None:
                cents = _convert_to_usd_cents(v, code)
                if cents is not None:
                    return cents, None, "word"

    # ---- 3. Currency SYMBOLS (multi-char first so "Rp"/"RM"/"Br" beat bare digits)
    for sym in sorted(CURRENCY_SYMBOLS, key=len, reverse=True):
        code = CURRENCY_SYMBOLS[sym]
        if sym == "$":
            continue  # handled below as the dominant default
        m = re.search(rf"{re.escape(sym)}\s*{num}", text, re.I)
        if m:
            v = _val(m.group(1))
            if v is not None:
                cents = _convert_to_usd_cents(v, code)
                if cents is not None:
                    return cents, None, "symbol"

    # ---- 3b. PREFIXED dollar symbols (C$/A$/NZ$/HK$/S$) — a country-prefixed
    # dollar sign is an EXPLICIT non-US-dollar signal, unlike a bare "$".
    # Previously the bare-"$" step below matched the "$3000" substring inside
    # "A$3000" etc, silently discarding the prefix and pricing 1:1 as USD with
    # NO disclosure note at all (bare_dollar is deliberately never flagged).
    # Handled here, BEFORE the bare-"$" step, so the prefix is honoured:
    # AUD/SGD (SUPPORTED currencies, in SEEDED_FX_USD_PER_UNIT) convert via
    # FX; CAD/NZD/HKD (not in the seeded FX table) honestly decline instead
    # of being silently mispriced.
    _PREFIXED_DOLLAR_CODES = {"c": "cad", "a": "aud", "nz": "nzd", "hk": "hkd", "s": "sgd"}
    m = re.search(rf"\b(c|a|nz|hk|s)\$\s*{num}", text, re.I)
    if m:
        prefix = m.group(1).lower()
        code = _PREFIXED_DOLLAR_CODES.get(prefix)
        v = _val(m.group(2))
        if v is not None and code is not None:
            cents = _convert_to_usd_cents(v, code)
            if cents is not None:
                return cents, None, "symbol"
            logger.warning(
                "intent_parser: budget in UNSUPPORTED prefixed-dollar currency "
                "%r — honest decline (NOT treated as USD)", code,
            )
            return None, UNKNOWN_CURRENCY, None

    # ---- 4. Bare "$" → USD (front-door default; dominant corpus + merchant ccy)
    m = re.search(rf"\${num}", text)
    if m:
        v = _val(m.group(1))
        if v is not None:
            return int(round(v * 100)), None, "bare_dollar"

    # ---- 5. UNKNOWN currency detection: an amount tagged with a 3-letter code
    #         or a foreign symbol we do NOT support → honest decline, never USD.
    m = re.search(rf"\b([A-Z]{{3}})\s*{num}\b", text)
    if not m:
        m = re.search(rf"{num}\s*\b([A-Z]{{3}})\b", text)
    if m:
        g = m.groups()
        code = g[0] if g[0].isalpha() else g[1]
        if code.lower() not in _KNOWN_CURRENCY_CODES:
            logger.warning(
                "intent_parser: budget in UNSUPPORTED currency %r — honest decline "
                "(NOT treated as USD)", code,
            )
            return None, UNKNOWN_CURRENCY, None
    # Foreign symbol we don't support (e.g. ₹, ¥, ₩)
    if re.search(rf"[₹¥₩]\s*{num}", text):
        logger.warning(
            "intent_parser: budget in unsupported currency symbol — honest decline"
        )
        return None, UNKNOWN_CURRENCY, None

    # ---- 5b. UNKNOWN currency detection, case-INSENSITIVE, curated list ----
    # fix-round-1: the step-5 check above only matches UPPERCASE 3-letter
    # codes, so a lowercase ISO code ("jpy 200000") silently fell through to
    # the bare_number fallback and was mispriced 1:1 as USD (~150x for JPY).
    # Matched only against a hand-curated set of real currency codes (never a
    # blanket case-insensitive [A-Za-z]{3}) so ordinary words next to a number
    # ("5000 for a trip") can never misfire as a currency decline.
    unsupported_codes = "|".join(sorted(_UNSUPPORTED_CURRENCY_CODES, key=len, reverse=True))
    m = re.search(rf"\b({unsupported_codes})\s*{num}\b", text, re.I)
    if not m:
        m = re.search(rf"{num}\s*\b({unsupported_codes})\b", text, re.I)
    if m:
        logger.warning(
            "intent_parser: budget in UNSUPPORTED currency %r (lowercase/mixed-case "
            "code) — honest decline (NOT treated as USD)", m.group(1),
        )
        return None, UNKNOWN_CURRENCY, None

    # ---- 5c. UNKNOWN currency detection: spelled-out WORDS ("yen", "yuan",
    #          "won", "rupees", "pesos", ...) for currencies we do not support.
    # fix-round-1: previously these fell all the way through to the weakest
    # bare_number fallback (mispriced 1:1 as USD, e.g. "200000 yen" -> $200,000)
    # or, when no budget cue was directly adjacent, to (None, None, None) —
    # indistinguishable from "no budget at all" and routed to the WRONG
    # elicitation slot (BUDGET instead of CURRENCY). Checking here, before
    # step 6, ensures a stated-but-unsupported currency word is always
    # honestly declined rather than silently mispriced or mis-routed.
    # fix-round-2: also check the WORD-then-NUMBER order ("spend won 800000",
    # "budget of yen 200000") symmetrically with step 5b's code-detection,
    # which already checks both orders. Previously only num-then-word matched,
    # so a word-first phrasing missed every branch and fell to (None, None,
    # None) — mis-routed to the BUDGET/duration clarification slot instead of
    # an honest CURRENCY decline.
    unsupported_words = "|".join(_UNSUPPORTED_CURRENCY_WORDS)
    if re.search(rf"{num}\s*(?:{unsupported_words})\b", text, re.I) or re.search(
        rf"\b(?:{unsupported_words})\s*{num}", text, re.I
    ):
        logger.warning(
            "intent_parser: budget in UNSUPPORTED currency word — honest decline "
            "(NOT treated as USD)"
        )
        return None, UNKNOWN_CURRENCY, None

    # ---- 5d. UNKNOWN currency NAMED elsewhere in the text (not adjacent to
    #          the number at all) — "budget 300000 (yen)", "keep it under
    #          300000, prices are in yen", "spend up to 500000, that is yen
    #          not dollars". Steps 5b/5c above require the code/word to sit
    #          immediately next to the number; when the user names the
    #          currency in a parenthetical or a separate clause, the amount
    #          still fell through to the weakest bare_number/USD assumption
    #          below WITH the honesty-inverting "no currency was stated" note
    #          — even though the user explicitly named a currency. "won" is
    #          deliberately excluded here (unlike step 5c) — it is also the
    #          common English past tense of "win" ("I won a prize"), so a
    #          match-anywhere-in-the-text scan for it would false-positive on
    #          ordinary sentences; it stays covered by the adjacency-scoped
    #          step 5c above.
    # fix-round-2: deliberately WORDS only, never the 3-letter CODES list —
    # several unsupported codes are also common English words when scanned
    # WITHOUT number-adjacency ("try" == TRY, "rub" == RUB, "cad" == an
    # archaic word), so an anywhere-in-text scan for codes would false-fire on
    # ordinary sentences. Currency WORDS are far less prone to this collision
    # ("won" is the one common-word exception, and is excluded below).
    # fix-round-3: the same common-word-collision risk applies to a few of
    # the newly-added currency words — "kip" (British slang for a nap/sleep),
    # "lei" (a flower necklace), and "colon"/"colons" (the punctuation mark /
    # body organ) — so they stay excluded from this anywhere-in-text scan the
    # same way "won" is, and remain covered by the number-adjacency-scoped
    # step 5c above.
    # fix-round-4: "bucks?" (a male deer / an NBA team / "pass the buck") and
    # "afghanis?" (also the common demonym for people from Afghanistan, "the
    # Afghanis were welcoming") are common-word collisions the same way
    # "won"/"kip"/"lei"/"colon" are — excluded from the anywhere-in-text scan,
    # still covered by the adjacency-scoped step 5c above.
    _ANYWHERE_SCAN_EXCLUDE = frozenset({
        r"wons?", r"kips?", r"leis?", r"colon(?:es)?s?", r"bucks?", r"afghanis?",
    })
    unsupported_words_anywhere = "|".join(
        w for w in _UNSUPPORTED_CURRENCY_WORDS if w not in _ANYWHERE_SCAN_EXCLUDE
    )
    # Gate on an actual number being present somewhere at all — otherwise this
    # step would misfire on trip-chat that merely mentions a currency by name
    # with no stated amount at all ("reading about yen history"), which is
    # correctly "no budget stated" (None, None, None), not a currency decline.
    if re.search(num, text) and re.search(
        rf"\b(?:{unsupported_words_anywhere})\b", text, re.I
    ):
        logger.warning(
            "intent_parser: budget number found but an UNSUPPORTED currency "
            "word is named elsewhere in the text — honest decline "
            "(NOT treated as USD)"
        )
        return None, UNKNOWN_CURRENCY, None

    # ---- 6. Plain-number budget context (assume USD — no currency cue at all) ----
    # Tolerate an optional approximator between the budget cue and the amount so
    # "budget about 7000" / "budget around 7k" / "spend roughly 3000" parse instead of
    # falling through to a spurious "no budget" clarification (approximators carry no
    # currency signal, so this stays a bare_number USD assumption).
    #
    # round-2 #date-budget-collision-fix: this is the WEAKEST signal (no currency cue
    # at all), so it must not consume the tail segment of an ISO date immediately
    # followed by a qualitative budget word ("2027-05-01, budget-friendly" -> the "01"
    # in "...01, budget..." was matched as "$1" because `num`'s char class tolerates a
    # THOUSANDS-GROUPING comma and therefore also swallowed the bare ", " before
    # "budget"). `_num_strict` is identical to `num` except it must END on a digit (or
    # k/m suffix), never a dangling comma, so a date fragment immediately before a
    # comma can never be mistaken for a stated amount.
    _num_strict = r"(\d(?:[\d,]*\d)?(?:\.\d+)?[kKmM]?)(?![A-Za-z])"
    _approx = r"(?:about|around|approx(?:\.|imately)?|roughly|~|up\s*to|nearly|close\s*to)\s+"
    # fix-round-1: OVER-capture guard — "spend 5 days" / "max 4 of us" / "limit 2
    # checked bags" / "budget 3 nights" put a DURATION/PARTY/luggage count, not a
    # dollar amount, directly after a budget cue word. Without this negative
    # lookahead the weakest (bare_number) signal silently grabbed that count as a
    # tiny cents budget, SATISFYING the budget slot (no clarification asked) and
    # then stranding the plan against an absurd $2-10 "budget".
    # (?!\d) blocks the regex engine from backtracking the optional inner digit
    # group down to a SHORTER run (e.g. "1" out of "10") just to dodge the
    # unit-word lookahead below with the leftover "0" — without it, "spend 10
    # days" would still match "1" (as $1) instead of correctly rejecting the
    # whole "10 days" span.
    # fix-round-2: the OVER-capture guard above only blacklisted DURATION/
    # PARTY/LUGGAGE nouns. A hotel STAR rating ("max 5 star hotels only") or an
    # activity/POI count ("up to 10 attractions", "up to 8 museums", "up to 6
    # tours", "budget 90 percent beaches") right after a budget cue word is the
    # exact same failure mode — the count/rating gets grabbed as an absurd
    # tiny cents "budget", SATISFYING the slot (no clarification asked) and
    # silently stranding the plan.
    _num_strict_budget = (
        r"(\d(?:[\d,]*\d)?(?:\.\d+)?[kKmM]?)(?!\d)(?![A-Za-z])"
        r"(?!\s*(?:days?|nights?|weeks?|months?|years?|minutes?|min|mins?|hours?|"
        r"of\s+us|people|persons?|pax|px|adults?|kids?|children|"
        r"(?:checked\s+)?bags?|suitcases?|"
        r"stars?|attractions?|museums?|tours?|activit(?:y|ies)|percent|%|"
        # fix-round-3: more non-money quantities that sit right after a
        # budget cue word the same way days/nights/stars/percent already do —
        # a proximity/layover/temperature/weight/step/flight/photo count, NOT
        # a dollar amount. Each of these was previously grabbed as an absurd
        # tiny cents "budget" (e.g. "within 20 min of the beach" -> $20),
        # SATISFYING the slot with no clarification and silently stranding
        # the plan against that absurd figure.
        r"layovers?|stops?|connections?|"
        r"degrees?|celsius|fahrenheit|"
        r"kgs?|kilograms?|kms?|kilometers?|kilometres?|miles?|meters?|metres?|"
        r"lbs?|pounds?|"
        r"steps?|flights?|calories?|photos?)\b)"
    )
    # fix-round-1: UNDER-capture fix — natural budget phrasings where the number
    # is not immediately adjacent to a bare cue word ("budget OF 3000", "budget IS
    # 3000", "my budget: 3000", "no more than 3000", "keep it around 3000",
    # "trying not to go over 3000", "aim for about 3k", "ballpark 2500", "up to 3k
    # total") previously fell through every pattern and produced a self-contradicting
    # "I could not find a budget" decline despite the literal word "budget" (or an
    # equally clear budget-cap phrase) being present. Widened cue-phrase set +
    # optional ":"/"-"/"is"/"of" connector between the cue and the amount.
    # fix-round-2: "my max IS 3000" needs the same optional "is"/"of" connector
    # that "budget" already gets; "stay within 3000" / "cap it at 3000" use cue
    # words ("within"/"cap ... at") that were entirely absent from this set —
    # both fell all the way through to a self-contradicting "I could not find
    # a budget" decline despite a plainly-stated numeric ceiling.
    # fix-round-3: more natural budget-ceiling phrasings still missing —
    # "cant spend more than"/"exceed" (a ceiling verb with no adjacent
    # "budget"/"limit" cue at all), "keep costs down to" (only "keep it
    # around/under" was covered, not this equally common variant), and
    # "in the region of"/"region of" (a British-English hedge for an
    # approximate ceiling). Each of these previously fell through every
    # pattern to a self-contradicting "I could not find a budget" decline
    # despite the user literally stating a numeric ceiling.
    # fix-round-4: more natural budget-ceiling phrasings still missing.
    #   - a bare "budget cap"/"cap" NOUN with no trailing "at" ("budget cap
    #     3000") — only the "cap(?:ped)?...at" VERB phrasing was covered, so
    #     the word "cap" sitting between "budget" and the number broke the
    #     "budget" cue's reach, and the "cap...at" cue required a trailing
    #     "at" this phrasing lacks — producing a SELF-CONTRADICTING decline
    #     (the literal word "budget" sits right next to the number).
    #   - "total of" / "afford" / "ceiling" / "nothing over" — unambiguous
    #     stated ceilings with no cue word from the set below at all.
    _budget_cue_phrases = (
        r"under|budget(?:\s+is|\s+of)?|budget\s+cap|cap(?:ped)?|"
        r"max(?:imum)?(?:\s+is)?|limit|spend|cost|"
        r"ballpark|up\s+to|total\s+of|(?:can|could)\s+afford|ceiling(?:\s+is)?|"
        r"no\s+more\s+than|more\s+than|nothing\s+over|exceed(?:ing)?|"
        r"not\s+(?:to\s+)?go(?:ing)?\s+over|"
        r"keep\s+it(?:\s+around|\s+under)?|keep\s+costs?\s+down\s+to|"
        r"aim\s+(?:for|at)|looking\s+at|"
        r"stay(?:ing)?\s+within|within|cap(?:ped)?(?:\s+it)?\s+at|at\s+most|"
        r"(?:in\s+the\s+)?region\s+of"
    )
    for pat in (
        rf"(?:{_budget_cue_phrases})\s*(?::|-)?\s*(?:{_approx})?{_num_strict_budget}",
        # fix-round-2: "3000 max" — the extremely common trailing-cue form —
        # previously matched neither pattern (only trailing "budget"/"limit"
        # were accepted), silently dropping a plainly-stated ceiling.
        rf"{_num_strict_budget}\s+(?:budget|limit|max(?:imum)?)\b",
        # fix-round-3: "3000 or less" — another common trailing-ceiling form.
        rf"{_num_strict_budget}\s+or\s+less\b",
    ):
        m = re.search(pat, text, re.I)
        if m:
            v = _val(m.group(1))
            if v is not None:
                return int(round(v * 100)), None, "bare_number"

    return None, None, None


def _scan_budget(text: str) -> tuple[int | None, object | None]:
    """Back-compat 2-tuple shim: scan text for a budget, returning (usd_cents, error).

    Drops the provenance signal — see ``_scan_budget_raw`` for that (GAP 2 honesty fix).
    Returns None both when no budget is present AND when the budget is in an
    unsupported currency (callers that need to distinguish use ``_scan_budget_raw``).
    """
    cents, err, _prov = _scan_budget_raw(text)
    return cents, err


def _scan_budget_cents(text: str) -> int | None:
    """
    Back-compat shim: scan text for a budget, returning USD cents or None.

    Returns None both when no budget is present AND when the budget is in an
    unsupported currency (callers that need to distinguish use _scan_budget_raw).
    """
    cents, _err, _prov = _scan_budget_raw(text)
    return cents


# #per-person-budget-fix (CRITICAL): "$1000 per person for 2 adults" was previously
# parsed as a TOTAL of $1000 — silently HALVING (or worse, for larger parties) the
# real party budget, since nothing anywhere in this module ever multiplied a stated
# dollar figure by party size. That is dangerous in both directions: understating the
# real budget risks false over-budget declines / wrong (too-cheap) hotel tiers, while
# if a caller ever treats total_budget_cents as a hard enforced ceiling (see
# orchestrator's insurance/lodging pre-commit DP gate) it can make a genuinely
# affordable trip look infeasible.
#
# Cue words recognised: "per person" / "per pax" / "per adult" / "per traveler(ler)" /
# "pp" / "a head" / "each" — matched ONLY when the cue sits directly after the budget
# amount (allowing at most one intervening currency word, e.g. "1500 USD per person"),
# so an unrelated "each" elsewhere in the sentence ("2 rooms, each with a view") can
# never be mistaken for a per-person budget qualifier. "each" is deliberately the
# TIGHTEST cue (no intervening word tolerated at all) since it is the most likely to
# collide with unrelated nouns.
_PER_PERSON_CURRENCY_WORD = (
    r"(?:usd|dollars?|aud|sgd|myr|thb|baht|idr|rupiah|etb|birr|eur|euros?|gbp|pounds?|"
    r"ringgit)"
)
_PER_PERSON_NUM = r"[\d][\d,]*(?:\.\d+)?[kKmM]?"
_PER_PERSON_CUE_LOOSE = (
    r"(?:per[\s-]+(?:person|pax|adult|head|traveler|traveller)|p\.?p\.?\b|a\s+head)"
)
_PER_PERSON_AFTER_RE = re.compile(
    rf"{_PER_PERSON_NUM}\s*(?:{_PER_PERSON_CURRENCY_WORD}\s*)?"
    rf"(?:{_PER_PERSON_CUE_LOOSE}|\beach\b)",
    re.I,
)
# Cue-before-amount phrasing ("per person budget of $500", "per person: 500 AUD").
_PER_PERSON_BEFORE_RE = re.compile(
    rf"\bper[\s-]+(?:person|pax|adult|head|traveler|traveller)\b"
    rf"[^.\n]{{0,20}}?{_PER_PERSON_NUM}",
    re.I,
)


def _scan_budget_is_per_person(text: str) -> bool:
    """True when the stated budget amount is qualified as PER-PERSON rather than a
    trip total ("$1000 per person", "USD 1500 per person", "$500pp", "$500 a head",
    "$1000 each"). Deterministic regex, var-0 safe — no price/live data, pure text
    scan. See the #per-person-budget-fix comment above for the false-positive
    guard rationale (why "each" requires zero intervening words)."""
    return bool(_PER_PERSON_AFTER_RE.search(text) or _PER_PERSON_BEFORE_RE.search(text))


# Sane upper bound for a date-range-DERIVED duration. No pre-existing per-night cap
# exists elsewhere in _scan_nights (explicit "N nights"/"N weeks" phrasing is trusted
# verbatim), but an explicit date range spanning this long is almost always a mis-parse
# (e.g. a stray year mismatch) rather than a genuine trip — 180 nights (~6 months)
# comfortably covers even round-the-world / extended-sabbatical requests.
_MAX_DATE_RANGE_NIGHTS = 180

# Join words/characters that can separate the two ends of an explicit date range.
_DATE_RANGE_JOIN_RE = re.compile(r"\b(?:to|through)\b|[-–—]", re.I)

# Same bare-month ambiguity guard `_scan_start_date_raw` applies ("may" the modal,
# "march"/"august" the verb/adjective) — without a day number attached these are too
# easy to false-positive on, so a bare hit on either side of the join word is rejected.
_AMBIGUOUS_BARE_MONTHS = {"may", "march", "august"}


def _scan_date_range_nights(text: str) -> int | None:
    """Fallback for `_scan_nights`: derive a night count from an explicit, unambiguous
    date RANGE ("April 10 to April 15", "March 3 through March 8") when no explicit
    night/day/week COUNT phrase is present anywhere in the text.

    This answers a different question than `_scan_start_date_raw` — "how many nights?"
    rather than "what is the start date?" — using the same two date-phrases as evidence.
    It does not re-decide (or duplicate) the start-date extraction that function already
    does correctly elsewhere in `parse_intent`'s pipeline.

    Conservative by construction: scans for a "to"/"through"/"-"/"–"/"—" join word and
    requires the date-like phrase immediately before and after it (only whitespace/comma
    allowed in between) to each independently parse as a date via `_parse_date_raw`.
    Unrelated "X to Y" phrasing ("drive to the airport", "connecting to my next flight")
    never matches, because neither side parses as a date there.

    Rejects a non-positive or implausibly large (`> _MAX_DATE_RANGE_NIGHTS`) computed
    span rather than guess — this also naturally covers the same-reference-year
    "December 20 to January 5" backwards-range case (both sides anchor to the same
    `_REFERENCE_YEAR` when year-less, so the span comes out negative and is rejected).
    `_scan_start_date_raw`'s `_maybe_roll` already solves year-rollover for a single
    START date; re-deriving that here for a full range is out of scope for this fix.

    Purely deterministic (var-0): no wall-clock read, no LLM.
    """
    for jm in _DATE_RANGE_JOIN_RE.finditer(text):
        before, after = text[: jm.start()], text[jm.end():]
        m_before = None
        for m in _DATE_PHRASE_RE.finditer(before):
            m_before = m  # keep the LAST (closest-to-join-word) match
        m_after = _DATE_PHRASE_RE.search(after)
        if not m_before or not m_after:
            continue
        # Nothing but whitespace/comma may sit between each date phrase and the join
        # word — otherwise this isn't a clean "<date> to/through/- <date>" range.
        if before[m_before.end():].strip(" ,") or after[: m_after.start()].strip(" ,"):
            continue
        if (
            m_before.group(0).strip().lower() in _AMBIGUOUS_BARE_MONTHS
            or m_after.group(0).strip().lower() in _AMBIGUOUS_BARE_MONTHS
        ):
            continue
        start_iso, _ = _parse_date_raw(m_before.group(0))
        end_iso, _ = _parse_date_raw(m_after.group(0))
        if not start_iso or not end_iso:
            continue
        nights = (date.fromisoformat(end_iso) - date.fromisoformat(start_iso)).days
        if nights <= 0 or nights > _MAX_DATE_RANGE_NIGHTS:
            continue
        return nights
    return _scan_date_range_nights_fallback(text)


# GAP-5/6 fallback (#202/daterange-02, daterange-03): two narrow, self-contained
# patterns for range shapes the strict `_DATE_PHRASE_RE`-pairing loop above never
# reaches, because one side of the range has no month token of its own attached —
# `_DATE_PHRASE_RE` (used elsewhere for single-date extraction, e.g.
# `_scan_start_date_raw`) is intentionally left untouched so this fix cannot regress
# any other date-parsing caller; both new regexes are scoped to this function only.
#
# Both patterns compute nights as a plain day-number SUBTRACTION (day2 - day1) rather
# than routing through `_parse_date_raw`/ISO dates — the month (and any year) is the
# same on both sides by construction of the phrase, so it cancels out and there is no
# year-rollover ambiguity to resolve.
#
# 1) "the 10th until/to/through the 15th of May" — a bare ordinal day (no month)
#    joined to a second side that carries the day via "Dth of Month" (day-then-month,
#    not the "Month D" shape `_DATE_PHRASE_RE` already handles). Requires the literal
#    "the" cue before each day number, which keeps this from ever firing on the
#    adversarial negatives ("drive to the airport", "connecting to my next flight") —
#    neither has a digit after "the".
_RANGE_DAY_THEN_DAY_OF_MONTH_RE = re.compile(
    r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)?\s+(?:to|through|until|til)\s+"
    r"the\s+(\d{1,2})(?:st|nd|rd|th)?\s+of\s+" + _MONTH_RE + r"\b",
    re.I,
)
#    2) "Apr 10-15" — compact "Month D-D" shorthand where the second (after-hyphen)
#    side omits the month entirely. Anchored on a month token directly before the
#    first day, so it cannot fire on unrelated hyphenated numbers ("$50-100", "a
#    9-5 job").
_RANGE_COMPACT_MONTH_DD_RE = re.compile(
    r"\b" + _MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?\s*[-–—]\s*(\d{1,2})(?:st|nd|rd|th)?\b",
    re.I,
)


def _scan_date_range_nights_fallback(text: str) -> int | None:
    """Second-chance patterns for date ranges where one side has no month token of
    its own — see the two regexes above for the exact shapes covered. Only called
    when the strict primary pass in `_scan_date_range_nights` finds nothing."""
    m = _RANGE_DAY_THEN_DAY_OF_MONTH_RE.search(text)
    if m:
        day1, day2 = int(m.group(1)), int(m.group(2))
        nights = day2 - day1
        if 0 < nights <= _MAX_DATE_RANGE_NIGHTS:
            return nights
    m = _RANGE_COMPACT_MONTH_DD_RE.search(text)
    if m:
        day1, day2 = int(m.group(1)), int(m.group(2))
        nights = day2 - day1
        if 0 < nights <= _MAX_DATE_RANGE_NIGHTS:
            return nights
    return None


# fix-round-3: "a/one week" is a genuine 7-night duration statement (see the
# fix-round-2 comment below) UNLESS it is immediately followed by "ago"/
# "from" — "a week ago", "a week from now/today/tomorrow/Monday" state WHEN a
# trip departs (or a past reference), never a trip LENGTH. The in-code
# assumption that departure-timing phrases "never start with a/one" was
# false; the trailing-word check here is what actually disambiguates them
# (the leading-"in" case is checked separately at the call site, since a
# fixed-width lookbehind can't span the variable "about"/"around" hedge).
_WEEK_DURATION_RE = re.compile(
    r"\b(?:a|one)\s+(?:\w+\s+)?week\b(?!\s+(?:ago\b|from\b))", re.I,
)

# fix-round-4: a small, bounded-proximity context signal that an "N days"/
# "N weeks" mention is INCIDENTAL (visa validity, booking lead time, a
# refund/cancellation window, or pre-trip prep time) rather than the trip's
# own length — see the guard in ``_scan_nights`` below.
#
# fix (in-N-days start-date hijack): the SAME incidental-context problem
# afflicts the "in N days" START-DATE scanner in ``_scan_start_date_raw`` —
# "my visa expires in 90 days. 4 nights in Tokyo" was resolving the trip's
# checkin to today+90 (visa-expiry hijacking the START DATE, not just a
# duration), and "the sale ends in 3 days" did the same for a promo window.
# Widened with expiry/commerce-deadline terms (expire/expiry, sale, deal,
# offer, warranty, lease, subscription, membership) so both scanners share
# one guard — see ``_is_incidental_context`` below.
_INCIDENTAL_DURATION_CONTEXT_RE = re.compile(
    r"\bvisa\b|\bin\s+advance\b|\brefund\b|\bcancellation\b|\bnotice\s+period\b|"
    r"\bwedding\s+prep|\bprep(?:aration)?\b|\bexpir(?:e|es|ed|y|ation)\b|\bsale\b|"
    r"\bdeal\b|\boffer\b|\bwarranty\b|\blease\b|\bsubscription\b|\bmembership\b",
    re.I,
)


def _is_incidental_context(text: str, start: int, end: int) -> bool:
    """Shared bounded-window guard: is the numeric phrase at ``text[start:end]``
    sitting inside an incidental (non-trip-duration, non-trip-start-date)
    context — visa validity, a sale/deal/offer deadline, a refund/cancellation/
    warranty/lease/subscription/membership window, booking lead time, or
    pre-trip prep? Used by both the nights/duration scanner and the "in N
    days" start-date scanner so a phrase like "my visa expires in 90 days" or
    "the sale ends in 3 days" never overrides the trip's real stated length
    or start date.
    """
    ctx = text[max(0, start - 20):end + 25]
    return _INCIDENTAL_DURATION_CONTEXT_RE.search(ctx) is not None


# fix-round-5: correction-cue phrases, each optionally chained ("actually,
# make it 5"), immediately followed by a re-stated bare duration NUMBER
# (optionally with a "nights"/"days" unit — the number alone, as in "make it
# 5", is what the verified live bug needs since it has no unit of its own).
# Matches the verified live bug ("2 nights in Rome... actually make it 5").
#
# Follow-up fix: the original follow-up fix
# used a negative lookahead enumerating party/pax/room nouns to exclude
# ("actually make it 4 adults" isn't a duration correction). An
# adversarial review pass rejected that as a NEW regression source: it's
# an enumerated DENY-list, the exact bug class this same file already learned
# (via the non-Latin-script rounds) can never be complete. Concrete breaks:
# "make it 5 of us" (missing "of us" from the list -- the file's own party
# regexes elsewhere DO recognize it), "make it 5 star hotels" / "make it 2
# twin rooms" (a modifier word between the number and the noun defeats
# adjacency-only matching), "make it 3 grand" (money, not covered at all).
#
# Fixed by inverting to an ALLOW-list instead: the bare-number branch (no
# explicit nights/days unit) only counts as a duration correction if the
# number is followed by nothing else at all -- end of string or punctuation.
# ANY trailing word (whatever it is -- party noun, adjective, unit of money,
# something not yet seen) disqualifies it, so there is no vocabulary list to
# keep falling behind. When an explicit "nights"/"days" unit IS stated
# ("actually make it 5 nights in Rome"), that's unambiguous regardless of
# what follows, so no such restriction applies to that branch.
_NIGHTS_CORRECTION_CUE_RE = re.compile(
    r"(?:actually|wait,?\s*no|i\s*mean|scratch\s+that|make\s+it)"
    r"(?:[,\s]+(?:actually|wait,?\s*no|i\s*mean|scratch\s+that|make\s+it))*"
    r"[,\s]+(\d+)"
    r"(?:"
    r"\s*(?:nights?|days?)\b"          # explicit unit -- always a duration, regardless of trailing text
    r"|"
    r"(?=\s*(?:[,.;:!?]|$))"           # bare number -- only a duration if nothing/punctuation follows
    r")",
    re.I,
)


def _correction_override_nights(text: str) -> int | None:
    """Return the LAST duration re-stated immediately after a correction cue
    ("actually", "wait no", "I mean", "scratch that", "make it"), or None if
    no such correction is present. Deliberately narrow — only the exact cue
    words verified in production, NOT a general last-value-wins rule (which
    would risk overriding a genuinely later, unrelated number elsewhere in
    the text). A bare number with no explicit nights/days unit only counts
    as a duration correction when nothing (or punctuation) follows it —
    "actually make it 4 adults"/"5 of us"/"2 twin rooms"/"3 grand" all have
    a trailing word and so are correctly left alone (occupancy, money, or
    anything else — not enumerated, just excluded by the lack of a clean
    boundary). See the regex's own comment for why this is an allow-list,
    not a party-noun deny-list.
    """
    last: int | None = None
    for m in _NIGHTS_CORRECTION_CUE_RE.finditer(text):
        n = int(m.group(1))
        if 0 < n <= _MAX_DATE_RANGE_NIGHTS:
            last = n
    return last


def _scan_nights(text: str) -> int | None:
    """Thin wrapper over `_scan_nights_impl` that additionally honours an
    explicit mid-sentence self-correction of the stated duration — see
    `_NIGHTS_CORRECTION_CUE_RE` above. Kept as a wrapper (rather than folded
    into the impl's candidate-collection logic) so the correction check is a
    single, isolated override applied AFTER normal scanning, with zero risk
    of disturbing the impl's existing early-return branches/precedence.
    """
    corrected = _correction_override_nights(text)
    if corrected is not None:
        return corrected
    return _scan_nights_impl(text)


def _scan_nights_impl(text: str) -> int | None:
    """Scan text for night/day count. Returns total nights or None.

    Handles numeric forms ("6 nights", "6 days" → 5) AND common word forms
    ("a week", "two weeks", "a fortnight", "long weekend", "weekend") so a
    perfectly satisfiable request like "a week in Bali under $2500" is not
    falsely declined for a missing duration.

    FALLBACK (tried only when none of the above explicit-count patterns match anything):
    an explicit date RANGE ("April 10 to April 15") also unambiguously implies a
    duration — see `_scan_date_range_nights`.
    """
    # Compact package notation "8d7n" / "8d/7n" / "8d 7n" (days+nights) → nights wins.
    m = re.search(r"\b(\d+)\s*d\s*[/\- ]?\s*(\d+)\s*n\b", text, re.I)
    if m:
        n = int(m.group(2))
        return n if 0 < n <= _MAX_DATE_RANGE_NIGHTS else None

    # fix-round-1: qualified "week" phrases meaning LESS than a full 7 nights
    # ("a work week" ~ 4-5 nights, "half a week" ~ 3-4 nights) — checked BEFORE
    # the numeric word-number weeks collector below, which would otherwise
    # match the "a week" embedded inside "half A WEEK" and over-count it to 7.
    _lowered_early = text.lower()
    if re.search(r"\bhalf\s+(?:a\s+)?week\b", _lowered_early):
        return 4
    # fix-round-4: "work-week" (hyphenated, the common dictionary spelling)
    # was not recognised — \s* matches whitespace but not a hyphen — while
    # "work week" and "workweek" both worked. Same treatment as the sibling
    # "week-?long" hyphen fix elsewhere in this scanner.
    if re.search(r"\bwork[\s-]*week\b", _lowered_early):
        return 5
    # fix (week-and-a-half undercount): "a week and a half" / "one and a half
    # weeks" / "1.5 weeks" is a genuine ~1.5-week (10.5-night) duration
    # statement, but was previously falling through to TWO different wrong
    # answers: (1) the bare "a/one week" catch-all further below matched the
    # embedded "a week" inside "a week and a half" and silently truncated it
    # to 7 nights (understating by 3-4 nights); (2) "1.5 weeks" was even
    # worse — the numeric "\d+\s*weeks?" collector's \b boundary fires
    # between "." and "5", so it matched just "5 weeks" out of "1.5 weeks"
    # and fabricated a wildly wrong 35-night trip. Checked here, before both
    # of those, and rounds 10.5 up to 11 nights (same round-half-up
    # convention as "half a week" -> 4 above and "fortnight" -> 14 below).
    if re.search(
        r"\b(?:a|one|1)\s+week\s+and\s+a\s+half\b"
        r"|\b(?:one|1)\s+and\s+a\s+half\s+weeks?\b"
        r"|\b1\.5\s*weeks?\b",
        _lowered_early,
    ):
        return 11

    # fix-round-1: collect EVERY explicit numeric duration signal (nights, compact
    # "Nn", days, numeric/word weeks) instead of returning on the first pattern that
    # matches. Previously "N nights" always won over "N weeks" regardless of which
    # appeared first or which was larger ("2 nights then 3 weeks in Japan" silently
    # collapsed a 3-week primary stay to 2 nights) — the LARGEST sane candidate now
    # wins, which is order-independent and honours the dominant stated duration.
    _candidates: list[int] = []
    # "6 nights" / "6 night"
    m = re.search(r"(\d+)\s*nights?\b", text, re.I)
    if m:
        _candidates.append(int(m.group(1)))
    # compact "7n" → 7 nights
    m = re.search(r"\b(\d+)\s*n\b", text, re.I)
    if m:
        _candidates.append(int(m.group(1)))
    # "6 days" → 5 nights ; compact "8d" → 7 nights (days - 1). The d-form requires a digit
    # immediately before, so currency codes ("3488 sgd") never match.
    # fix-round-2: split the spelled-out "days?" form (unambiguous) from the
    # BARE compact "d" form — the bare form alone also matches product/format
    # labels ("3D"/"4D"/"5D" movies, rides, printers), fabricating a fake
    # N-1-night duration out of "catch an IMAX 3D show" with no stated trip
    # length at all. Guarded with a negative lookahead over the common
    # cinema/format/product nouns that follow those labels.
    # fix-round-4: an INCIDENTAL "N days"/"N weeks" mention elsewhere in the
    # text — visa validity ("90 days visa-free", "60 day visa"), booking lead
    # time ("booked 30 days in advance"), or a refund/cancellation window
    # ("refund within 14 days") — was silently OVERRIDING the explicitly
    # stated trip length via max(), because every numeric "days"/"weeks"
    # phrase was collected as a candidate with no context check at all.
    # Scoped narrowly (a small bounded window around the match) so a
    # genuine trip-length "N days"/"N weeks" elsewhere in the same text is
    # never affected — only a match that itself sits inside one of these
    # unambiguous non-trip-duration contexts is skipped.
    def _is_incidental_duration(start: int, end: int) -> bool:
        return _is_incidental_context(text, start, end)

    for m in re.finditer(r"\b(\d+)\s*days?\b", text, re.I):
        if _is_incidental_duration(m.start(), m.end()):
            continue
        days = int(m.group(1))
        _candidates.append(max(1, days - 1))
        break
    else:
        m = re.search(
            r"\b(\d+)\s*d\b(?!\s*(?:show|shows|movie|movies|film|films|cinema|"
            r"ride|rides|print|printer|printers|tv|television|glasses|"
            r"experience|screening|screenings|attraction|attractions)\b)",
            text, re.I,
        )
        if m:
            days = int(m.group(1))
            _candidates.append(max(1, days - 1))
    lowered = text.lower()
    # Word-form durations. Numeric-week phrasing first ("two weeks", "3 weeks").
    _WORD_NUM = {
        "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
        "couple of": 2, "couple": 2, "few": 3,
    }
    # fix-round-3: both the digit and word-number "N week(s)" patterns below
    # (via the optional "s" in "weeks?") ALSO match departure-timing / past-
    # reference phrases ("2 weeks ago", "a week from now", "in about a
    # week") — the SAME false-positive class the later bare "a/one week"
    # catch-all guards against. Without excluding them here, this EARLIER
    # candidate-collection block fired and returned before that later guard
    # was ever reached, silently fabricating a duration out of pure timing
    # language. `_week_num_valid` mirrors the guard used for the bare "a/one
    # week" catch-all further below.
    def _week_num_valid(start: int, end: int) -> bool:
        if re.search(r"^\s*(?:ago\b|from\b)", lowered[end:]):
            return False
        if re.search(r"\bin\s+(?:about\s+|around\s+)?$", lowered[max(0, start - 20):start]):
            return False
        # fix-round-4: same incidental-duration guard as the "N days" collector
        # above ("booked 3 weeks in advance", "4 weeks of wedding prep").
        if _is_incidental_duration(start, end):
            return False
        return True

    for m in re.finditer(r"\b(\d+)\s*weeks?\b", lowered):
        if _week_num_valid(m.start(), m.end()):
            _candidates.append(int(m.group(1)) * 7)
            break
    for m in re.finditer(
        r"\b(a|an|one|two|three|four|couple of|couple|few)\s+weeks?\b", lowered,
    ):
        if _week_num_valid(m.start(), m.end()):
            _candidates.append(_WORD_NUM[m.group(1)] * 7)
            break
    if _candidates:
        # fix-round-1: cap at _MAX_DATE_RANGE_NIGHTS — the same ceiling the
        # date-range scan already enforces — so an absurd/typo'd duration
        # ("9999 nights") is dropped rather than silently accepted (a genuine
        # OTHER candidate in the same text, if any, still wins over decline).
        _sane = [c for c in _candidates if 0 < c <= _MAX_DATE_RANGE_NIGHTS]
        if _sane:
            return max(_sane)
        return None
    if re.search(r"\bfortnight\b", lowered):
        return 14
    if re.search(r"\blong\s+weekend\b", lowered):
        return 3
    if re.search(r"\bweekend\b", lowered):
        return 2
    # fix-round-2: the unqualified `\bweek\b` catch-all REMOVED — it matched any
    # sentence containing the standalone token "week" regardless of meaning,
    # fabricating a 7-night duration out of pure departure-TIMING phrases that
    # state no duration at all ("this week", "next week", "during the week",
    # "sometime this week", "sale ends this week"). "a week"/"one week" is a
    # genuine, unambiguous 7-night duration statement and is kept — including
    # with a single adjective in between ("a beach week", "a relaxing week"),
    # which is still an unambiguous duration statement, not a departure-timing
    # phrase (those never start with "a"/"one").
    # fix-round-3: that assumption was wrong for two common shapes — "a/one
    # week" immediately followed by "ago"/"from" ("a week ago", "a week from
    # now/today/tomorrow/Monday") states WHEN a trip departs (or a past
    # reference), never a trip LENGTH; and "a/one week" immediately preceded
    # by "in [about/around]" ("leaving in a week", "in about a week") is the
    # same departure-timing meaning. Both still start with "a"/"one" and were
    # silently fabricating a 7-night duration the user never stated.
    for _wm in _WEEK_DURATION_RE.finditer(lowered):
        _pre = lowered[max(0, _wm.start() - 20):_wm.start()]
        if re.search(r"\bin\s+(?:about\s+|around\s+)?$", _pre):
            continue
        return 7
    # fix-round-3: "week-long" (no leading article) is just as unambiguous a
    # 7-night duration statement as "a week-long" (which already worked via
    # the embedded "a week" match above) — but bare "week-long"/"weeklong"
    # with no article matched no pattern at all, silently falling through to
    # a false "how many nights?" clarification despite the user having
    # plainly stated a week-long trip. Unlike the removed bare "week"
    # catch-all, "week-long" is never a departure-timing phrase ("this
    # week-long"/"next week-long" is not idiomatic English), so this is safe
    # to recognise unconditionally.
    if re.search(r"\bweek-?long\b", lowered):
        return 7
    # Fallback: no explicit night/day/week count found anywhere above — check for an
    # explicit DATE RANGE instead (see `_scan_date_range_nights`).
    return _scan_date_range_nights(text)


# fix-round-4: `_scan_nights` returns None both for "no duration stated at all"
# AND for "a duration WAS stated but exceeds `_MAX_DATE_RANGE_NIGHTS`" (e.g.
# "200 nights") — the two are collapsed into the same None, so the caller's
# "I could not find a trip duration" message is factually false in the second
# case (a duration was found and understood, then rejected for being
# implausibly long). This lightweight, SEPARATE scan lets the caller tell the
# two apart to word the decline honestly, without changing `_scan_nights`'s
# return contract (every existing caller is untouched).
def _scan_nights_over_cap(text: str) -> int | None:
    """Return an explicitly-stated night/day/week duration that EXCEEDS
    ``_MAX_DATE_RANGE_NIGHTS``, or None if no candidate does. Mirrors the
    candidate-collection shape of ``_scan_nights`` (nights/days/weeks) closely
    enough to drive an honest over-length decline message; it deliberately
    does not need to replicate every guard there (timing/context false-
    positives), since a spurious over-cap number here only changes WORDING,
    never the underlying accept/decline decision.
    """
    over: list[int] = []
    m = re.search(r"(\d+)\s*nights?\b", text, re.I)
    if m:
        over.append(int(m.group(1)))
    m = re.search(r"\b(\d+)\s*days?\b", text, re.I)
    if m:
        over.append(max(1, int(m.group(1)) - 1))
    for m in re.finditer(r"\b(\d+)\s*weeks?\b", text, re.I):
        over.append(int(m.group(1)) * 7)
    over = [c for c in over if c > _MAX_DATE_RANGE_NIGHTS]
    return max(over) if over else None


# #honesty-fix (invalid-calendar-date): mirrors `_scan_nights_over_cap` immediately
# above — `_scan_date_range_nights` (and, through it, `_scan_nights`) returns the
# SAME None whether the text stated no date-like phrase at all OR stated one that
# names a day that does not exist in that month ("Feb 30", "April 31"). The
# caller's "I could not find a trip duration" message is factually false in the
# second case: a duration/date range WAS stated, it just failed calendar
# validation on one endpoint. This lightweight, separate scan lets the caller
# tell the two apart to word the decline honestly, without touching
# `_parse_date_raw`'s return contract (every existing caller stays untouched).
def _scan_invalid_calendar_date(text: str) -> str | None:
    """Return the raw text of the first date-like phrase (e.g. ``"Feb 30"``,
    ``"April 31"``) that names a day which does not exist in that month, or None
    if every date-like phrase in ``text`` either parses cleanly or has no
    explicit day of its own (a bare month like "in May" has no day to be wrong
    about, so it is never calendar-invalid).

    Deliberately mirrors the month/day tokenising in `_parse_date_raw` so the
    two agree on what counts as "invalid" — this scan just also reports WHICH
    phrase was invalid and whether it even had a day, distinctions
    `_parse_date_raw`'s (None, False) contract collapses away.
    """
    for dm in _DATE_PHRASE_RE.finditer(text):
        raw = dm.group(0)
        s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw.strip().lower()).replace(",", " ")
        month = day = year = None
        for tok in s.split():
            if month is None and tok in _MONTHS:
                month = _MONTHS[tok]
            elif year is None and re.fullmatch(r"\d{4}", tok):
                year = int(tok)
            elif day is None and re.fullmatch(r"\d{1,2}", tok):
                day = int(tok)
        if month is None or day is None:
            continue  # bare month ("in May") — no day stated, nothing to be invalid
        try:
            date(year or _REFERENCE_YEAR, month, day)
        except ValueError:
            return raw.strip()
    return None


# #honesty-fix (silent-default-provenance): split into a raw scan that returns None
# when the text carries NO explicit party-size signal, and a thin wrapper that applies
# the conservative default of 1 — mirrors the _scan_start_date (Optional) / start_anchor
# (defaulted) split used for the date fallback. _scan_adults_raw is the var-0-safe
# explicit/assumed signal parse_intent keys off of; _scan_adults keeps its original
# always-an-int contract so every existing caller is untouched.
# Companion words strong enough to imply "speaker + this person = 2 adults" in a
# travel-booking context. Spouse/partner words are the strongest signal (#192);
# a small, explicit set of close-relative words is included per #202/party-03 —
# deliberately NOT open-ended (no fuzzy relative matching) to keep the false-positive
# surface bounded and auditable.
_COMPANION_WORDS_RE = r"(?:wife|husband|spouse|partner|mom|mum|dad|mother|father|sister|brother)"

# "N couples" → 2*N adults (#202/party-04). Deliberately a SEPARATE pattern from the
# singular "\bcouple\b" check below rather than a shared regex, so the singular
# "a couple"/"my ... and I" inference (which returns a flat 2, not a multiplier) is
# untouched — this only fires on an explicit numeric prefix immediately before the
# plural "couples".
_N_COUPLES_RE = re.compile(r"\b(\d+)\s+couples\b", re.I)

# Small, closed word-to-number set shared by the "two people"/"for two" round-2
# #party-fix patterns below — deliberately bounded (two..six), matching the scope
# already used for the "<N> of us" word-number variant. ("six" added fix-round-1
# alongside the "adults" noun below — see _scan_adults_raw.)
# fix-round-2: extended through "twelve" — the cap used to stop at "six", so an
# explicitly-stated word-number party of seven or more ("seven adults", "eight
# adults", "ten people") matched neither the digit regex nor this set and silently
# defaulted the WHOLE stated party to 1 adult with no undercount disclosure.
# fix-round-3: extended through "twenty" — a spelled-out party ABOVE twelve
# ("fifteen adults", "twenty people") is uncommon for leisure but realistic for
# corporate offsites/retreats, and previously matched no branch at all, silently
# mispricing a 20-person booking as a single adult.
_PARTY_WORDNUM: dict[str, int] = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_PARTY_WORDNUM_ALT = (
    r"two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty"
)

# fix-round-4: compound spelled-out numerals ("twenty-five", "thirty-four",
# "forty-five") above twenty. The single-token patterns below (m1c/m2w) only
# match ONE word from _PARTY_WORDNUM_ALT — a hyphen/space in a compound
# numeral is a non-word boundary, so those patterns previously matched only
# the TRAILING unit word abutting the noun ("five adults" inside "twenty-five
# adults"), silently returning a plausible-looking WRONG non-default count
# (5 instead of 25) with no undercount disclosure. Deliberately does NOT
# include bare "thirty"/"forty"/etc. as their own standalone party sizes —
# that is the pre-existing, intentionally-bounded "thirty"->None->default-1
# gap; this only recognises a tens word when it is compounded with a unit.
_PARTY_TENS_WORDNUM: dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_PARTY_TENS_ALT = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_PARTY_UNIT_WORDNUM: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_PARTY_UNIT_ALT = "one|two|three|four|five|six|seven|eight|nine"


def _scan_adults_raw(text: str) -> int | None:
    """Scan text for an EXPLICITLY stated adult/party count. Returns None when no
    explicit signal is found in the text (caller decides how to default)."""
    lowered = text.lower()
    if re.search(r"\bsolo\b", lowered):
        return 1
    m_couples = _N_COUPLES_RE.search(lowered)
    if m_couples:
        return max(1, int(m_couples.group(1)) * 2)
    if re.search(r"\bcouple\b", lowered):
        return 2
    # "honeymoon" (round-2 #party-fix) — an unambiguous romantic-couple trip signal
    # (2 adults) even with zero numeric/companion-word party mention elsewhere in
    # the text. Checked early (a weak-but-unambiguous signal), same tier as "couple".
    if re.search(r"\bhoneymoon(?:er|ers)?\b", lowered):
        return 2
    # "2 adults" / "3 people" / "4 persons" / "2 of us" / "travelling with 3"
    # / "2 pax" / "2 px" — "pax" (and its clipped "px") is the standard travel-
    # industry abbreviation for headcount and is especially common in Southeast
    # Asian English (a core market for this product); previously unmatched here,
    # it silently fell through to the solo default AND (since explicit_adults is
    # derived from this same function returning non-None) caused a false
    # `assumed_adults` honesty-note claiming a headcount was "assumed" when the
    # user had in fact explicitly stated one.
    # (?!-) rejects hyphenated compounds ("2 adults-only villas") that are a
    # preference/property descriptor, not a stated party size — same false-positive
    # class the security review caught in _CHILDREN_RE (see #honesty-fix children-fix).
    m = re.search(r"(\d+)\s*(?:adults?|persons?|people|pax|px|of\s+us)(?!-)\b", lowered)
    if m:
        return max(1, int(m.group(1)))
    # word-number variant of "<N> of us" (#202/party-06) — "just the two of us" /
    # "the three of us". Deliberately a tiny closed set (two/three only, not a full
    # word-to-number parser) since larger word-number party sizes are vanishingly
    # rare in this phrasing and "one of us" is not a coherent party-size statement.
    m1b = re.search(r"\b(two|three)\s+of\s+us\b", lowered)
    if m1b:
        return {"two": 2, "three": 3}[m1b.group(1)]
    # round-2 #party-fix: WORD-number variant of "N people"/"N persons" — the digit
    # regex above only matches bare DIGITS ("2 people"), so prose like "two people"
    # fell through to the solo default. Small closed word-to-number set (two..six),
    # matching the scope already used for "<N> of us" just above.
    # fix-round-1: "adults?" added to the noun alternation — the digit regex above
    # only matches "2 adults" (bare digit), so "two adults"/"six adults" fell all
    # the way through to the solo default (1), silently mispricing lodging/insurance
    # for the whole stated party with no disclosure.
    # fix-round-4: compound numeral ("twenty-five adults") — checked BEFORE
    # the single-token m1c below, which would otherwise match only the
    # trailing unit word ("five adults") and silently return 5 for a
    # 25-person party. See _PARTY_TENS_WORDNUM/_PARTY_UNIT_WORDNUM above.
    m1c_compound = re.search(
        r"\b(" + _PARTY_TENS_ALT + r")[\s-]+(" + _PARTY_UNIT_ALT + r")\s*"
        r"(?:adults?|people|persons?)\b",
        lowered,
    )
    if m1c_compound:
        return (
            _PARTY_TENS_WORDNUM[m1c_compound.group(1)]
            + _PARTY_UNIT_WORDNUM[m1c_compound.group(2)]
        )
    m1c = re.search(
        r"\b(" + _PARTY_WORDNUM_ALT + r")\s*"
        r"(?:adults?|people|persons?)\b",
        lowered,
    )
    if m1c:
        return _PARTY_WORDNUM[m1c.group(1)]
    # round-2 #party-fix: "for two" / "for three" — "a trip for two", "Kyoto for two".
    # Guarded against the duration collision ("for two nights"/"for two weeks") with a
    # negative lookahead so a stated DURATION is never misread as a party size.
    m1d = re.search(r"\bfor\s+(two|three|four|five)\b(?!\s*(?:nights?|days?|weeks?))", lowered)
    if m1d:
        return _PARTY_WORDNUM[m1d.group(1)]
    # "family of 4" / "group of 5" / "party of 3"
    m2 = re.search(r"\b(?:family|group|party)\s+of\s+(\d+)\b", lowered)
    if m2:
        return max(1, int(m2.group(1)))
    # fix-round-3: WORD-number variant — "family of four" / "group of six" /
    # "party of three". The digit-only pattern above matched neither this nor
    # the generic word-number fallback (m1c, which requires an "adults?/
    # people/persons" noun, not "of <N>"), so a word-number party TOTAL fell
    # through to the solo default with no undercount disclosure — the whole
    # family silently priced as 1 adult.
    # fix-round-4: compound numeral variant ("group of twenty-five") — same
    # trailing-token-only bug as m1c_compound above, but here the m2w pattern
    # below would match the LEADING tens word ("twenty" of "twenty-five"),
    # silently returning 20 instead of 25. Checked first for the same reason.
    m2w_compound = re.search(
        r"\b(?:family|group|party)\s+of\s+("
        + _PARTY_TENS_ALT + r")[\s-]+(" + _PARTY_UNIT_ALT + r")\b",
        lowered,
    )
    if m2w_compound:
        return (
            _PARTY_TENS_WORDNUM[m2w_compound.group(1)]
            + _PARTY_UNIT_WORDNUM[m2w_compound.group(2)]
        )
    m2w = re.search(
        r"\b(?:family|group|party)\s+of\s+(" + _PARTY_WORDNUM_ALT + r")\b",
        lowered,
    )
    if m2w:
        return _PARTY_WORDNUM[m2w.group(1)]
    # "with my wife" / "my husband and I" / "and my partner" — no numeric token, but
    # naming a spouse/partner/close-relative implies the (implicit) speaker PLUS that
    # companion = 2 adults. Checked AFTER every numeric pattern above so an explicit
    # count ("3 adults including my wife") always wins over this inference — this only
    # fires when the text carries no numeric party-size signal at all.
    if re.search(rf"\bmy\s+{_COMPANION_WORDS_RE}\b", lowered):
        return 2
    # "husband and I" / "my sister and I" (#202/party-07) — same companion-word
    # semantics as the "my <companion>" check above, but without requiring the "my"
    # possessive immediately before the word. Anchored tightly to "<companion-word>
    # and i" so unrelated "X and I" phrasing ("waiting and I think...") never matches —
    # the word directly before "and I" must itself be one of the closed companion words.
    if re.search(rf"\b{_COMPANION_WORDS_RE}\s+and\s+i\b", lowered):
        return 2
    return None


def _scan_adults(text: str) -> int:
    """Scan text for adult count. Defaults to 1 when no explicit signal is found."""
    return _scan_adults_raw(text) or 1


# #honesty-fix (GAP 3, silent-children-drop): distinct from _scan_adults_raw — children
# are NEVER folded into party-size pricing (occupancy/insurance/fees only ever price
# `adults`), so detecting them is purely so the drop can be DISCLOSED (`ignored_children`
# in _clamp_and_validate), not to change any pricing behaviour. Deliberately does NOT
# match "family of N" phrasing — that's already handled by _scan_adults_raw and prices
# all N as adults, so there is nothing dropped/to disclose there.
_CHILDREN_RE = re.compile(
    # (?!-) rejects hyphenated compound adjectives ("child-friendly hotel",
    # "kid-friendly resort", "infant-safe room") that are NOT a headcount —
    # security review caught this false-positive fabricating a children_note when
    # no child is actually in the party.
    # fix-round-2: "babies"/"baby"/"newborns?"/"little\s+ones?" added —
    # previously absent from BOTH child regexes, so even an EXPLICIT count
    # ("2 babies", "two little ones") was invisible and silently
    # priced/disclosed as zero children.
    # fix-round-4: the word-number vocabulary here stopped at "five", while
    # _PARTY_WORDNUM_ALT (the ADULT scanner) was already extended through
    # "twenty" in earlier rounds. A genuine, explicitly spelled-out child
    # count of six or more ("six kids", "eight kids", "ten children") matched
    # neither this regex nor the unquantified fallback below (a leading
    # quantifier disqualifies that fallback), so it silently vanished —
    # children=0 with NO ignored_children disclosure, defeating the very
    # honesty guarantee this scanner exists to enforce. Reuses the same
    # six..twenty vocabulary already vetted for the adult scanner.
    r"\b(\d+|a|an|one|two|three|four|five|" + _PARTY_WORDNUM_ALT + r")\s+"
    r"(?:kids?|children|child|toddlers?|infants?|babies|baby|newborns?|"
    r"little\s+ones?)(?!-)\b",
    re.I,
)
_CHILDREN_WORD_NUM = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    **_PARTY_WORDNUM,
}

# fix-round-1: children MENTIONED but with no leading count at all ("our
# children", "the kids are coming", "bringing our children along") were
# invisible to _CHILDREN_RE (which requires a numeric/article quantifier
# immediately before the noun) — so no ignored_children disclosure ever
# fired, silently defeating the entire honesty guarantee _scan_children
# exists for (see GAP 3 comment above). This is a narrow, closed-vocabulary
# fallback (possessive/article + kid noun, no quantifier) that resolves to a
# conservative "at least 1" count purely so the disclosure fires — it does
# NOT claim to know the exact number.
# fix-round-2: two more gaps in this same fallback closed —
# (a) "toddlers?"/"infants?"/"babies"/"baby"/"newborns?" were present in the
#     QUANTIFIED regex above but missing here, so "our toddler"/"my infant"
#     were invisible even though "a toddler" correctly resolved to 1;
# (b) a possessive/article (my/our/the) was REQUIRED immediately before the
#     noun, so equally common phrasings that mention children with no
#     leading quantifier at all ("kids in tow", "a trip with children",
#     "travelling with kids", "bringing children to Rome") were invisible.
#     These extra alternatives are deliberately narrow, closed-vocabulary cue
#     phrases (with/bringing/in tow) — not a blanket "any mention of kids" —
#     so a negated statement like "no kids"/"without kids" still does not
#     misfire (neither "with" nor "bringing" nor "in tow" appears in them).
# fix-round-5: a single descriptive adjective sitting between the
# possessive and the child-noun ("our twin toddlers", "our young kids")
# defeated the strict adjacency the possessive branch required, so
# children silently resolved to 0 with NO ignored_children disclosure —
# the same honesty-guarantee failure this whole regex exists to prevent,
# and it cascades into the family-persona inference (a stated family of 4
# gets priced/planned as adults=1 when the child count is missed). Fix is
# a narrow, closed-vocabulary adjective slot (0-2 words) rather than a
# blanket "any word(s) in between", so it stays a targeted extension
# rather than a wildcard gap.
_CHILDREN_UNQUANTIFIED_RE = re.compile(
    r"\b(?:my|our|the)\s+"
    r"(?:(?:twin|twins|young|little|small|tiny|baby|toddler|infant|"
    r"newborn|older|younger|teenage|teenaged)\s+){0,2}"
    r"(?:kids?|children|child|toddlers?|infants?|babies|baby|newborns?|"
    r"little\s+ones?)(?!-)\b"
    # fix-round-3: the three NO-possessive branches below were hard-coded to
    # kids/children/child only, unlike the possessive branch above (and the
    # quantified _CHILDREN_RE) which both already include toddlers/infants/
    # babies/newborns/little-ones. So "travelling with toddlers", "bringing
    # infants", "babies in tow" were invisible — children=0 with NO
    # ignored_children disclosure, silently defeating the honesty guarantee
    # this whole regex exists for.
    r"|\bwith\s+(?:kids?|children|child|toddlers?|infants?|babies|baby|"
    r"newborns?|little\s+ones?)(?!-)\b"
    r"|\b(?:kids?|children|child|toddlers?|infants?|babies|baby|newborns?|"
    r"little\s+ones?)\s+in\s+tow\b"
    r"|\bbringing\s+(?:kids?|children|child|toddlers?|infants?|babies|baby|"
    r"newborns?|little\s+ones?)(?!-)\b",
    re.I,
)


def _scan_children(text: str) -> int | None:
    """Scan text for an EXPLICITLY stated child/kid/toddler/infant count.

    Returns None when no such signal is found in the text. Returns the count
    otherwise (word-numbers "a"/"one"/"two"... resolved; bare digits parsed directly;
    an unquantified mention like "our children" conservatively resolves to 1 so the
    ignored_children disclosure still fires instead of silently dropping to zero).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    m = _CHILDREN_RE.search(text)
    if m:
        tok = m.group(1).lower()
        if tok in _CHILDREN_WORD_NUM:
            return _CHILDREN_WORD_NUM[tok]
        try:
            n = int(tok)
            return n if n >= 1 else None
        except ValueError:
            return None
    if _CHILDREN_UNQUANTIFIED_RE.search(text):
        return 1
    return None


# fix-round-3: nouns that are grammatically PLURAL (implying "at least two")
# within the _CHILDREN_UNQUANTIFIED_RE vocabulary, vs the singular forms
# ("child", "toddler", "infant", "baby", "newborn", "little one") the same
# regex also matches. Used only by ``_children_count_is_plural_estimate`` to
# word the ignored_children honesty note accurately — never changes the
# conservative count ``_scan_children`` itself resolves an unquantified
# mention to.
_CHILDREN_PLURAL_WORDS_RE = re.compile(
    r"\b(?:kids|children|toddlers|infants|babies|newborns|ones)\b", re.I,
)


def _children_count_is_plural_estimate(text: str) -> bool:
    """
    True when the child-count `_scan_children` resolved for *text* is the
    conservative "at least 1" UNQUANTIFIED estimate AND the wording the user
    actually used was PLURAL ("the kids are coming", "with our children") —
    i.e. the user said "at least two", not "exactly one". Mirrors
    ``_scan_children``'s own precedence (an EXPLICIT quantified count from
    ``_CHILDREN_RE`` always wins and is never treated as an estimate here).
    """
    if not isinstance(text, str) or not text.strip():
        return False
    if _CHILDREN_RE.search(text):
        return False
    m = _CHILDREN_UNQUANTIFIED_RE.search(text)
    if not m:
        return False
    return bool(_CHILDREN_PLURAL_WORDS_RE.search(m.group(0)))


# A stated party HEADCOUNT phrase ("family of 4", "group of 5", "party of 3",
# "4 people", "3 persons", "3 pax", "the two of us") counts EVERYONE — adults +
# kids. When children are ALSO stated separately, the adults are the REMAINDER,
# not the full headcount (otherwise "family of 4 with 2 kids" would price 4
# adults + 2 kids = 6). "pax"/"px" added alongside "people"/"persons" for the
# same reason as _scan_adults_raw above — a common headcount abbreviation that
# must be treated identically, not just left to the adults-only scan.
# Distinct from "N adults" (which _scan_adults_raw already reads as adults-only).
_PARTY_TOTAL_RE = re.compile(
    r"\b(?:family|group|party)\s+of\s+(\d+)\b"
    r"|\b(\d+)\s*(?:people|persons?|pax|px)\b"
    r"|\b(two|three|four|five)\s+of\s+us\b"
    # fix-round-3: word-number "family of four"/"group of six" TOTAL — same
    # gap as the digit-only pattern above, kept as its own group so the
    # existing group(1)/(2)/(3) indices are untouched.
    r"|\b(?:family|group|party)\s+of\s+(" + _PARTY_WORDNUM_ALT + r")\b",
    re.I,
)
_PARTY_TOTAL_WORD_NUM = {"two": 2, "three": 3, "four": 4, "five": 5}

# Bare "family" / "families" mention (no explicit "of N" headcount) — used only in
# combination with an explicit child count to imply two accompanying adults (parents).
_FAMILY_RE = re.compile(r"\bfamil(?:y|ies)\b", re.I)


def _scan_party_total(text: str) -> int | None:
    """Scan for a TOTAL party headcount ("family of 4", "5 people", "the two of us").

    Returns None when no total-headcount phrase is present. Deliberately does NOT
    match "N adults" (that is an adults-only count read by _scan_adults_raw)."""
    if not isinstance(text, str) or not text.strip():
        return None
    m = _PARTY_TOTAL_RE.search(text)
    if not m:
        return None
    if m.group(1):
        try:
            return max(1, int(m.group(1)))
        except ValueError:
            return None
    if m.group(2):
        try:
            return max(1, int(m.group(2)))
        except ValueError:
            return None
    if m.group(3):
        return _PARTY_TOTAL_WORD_NUM.get(m.group(3).lower())
    if m.group(4):
        return _PARTY_WORDNUM.get(m.group(4).lower())
    return None


# Qualitative budget descriptor → price tier for lodging selection. Maps the words a
# user uses ("mid budget", "shoestring", "luxury") to a tier the accommodation selector
# turns into a star / per-night band. var-0 safe: a deterministic regex over the user's
# own words — NO price, catalog, or live data. "budget" alone is deliberately NOT matched
# (it collides with "my budget is $3000"); only qualitative budget PHRASES trip it.
_BUDGET_TIER_LUXURY_RE = re.compile(
    r"\b(?:luxur(?:y|ious)|high-?end|upscale|five-?star|5-?star|premium|opulent|splurge|lavish)\b",
    re.I,
)
_BUDGET_TIER_SHOESTRING_RE = re.compile(
    r"\b(?:shoestring|backpack(?:er|ing)?|dirt[- ]cheap|bare[- ]?bones)\b", re.I
)
_BUDGET_TIER_MID_RE = re.compile(
    r"\bmid[- ](?:range|budget|tier|priced?)\b|\bmid-?range\b|\b(?:moderate|modest|comfortable)\b",
    re.I,
)
_BUDGET_TIER_BUDGET_RE = re.compile(
    r"\bbudget[- ](?:friendly|conscious|trip|hotel|stay|accommodation|travel|option|minded)\b"
    r"|\bon\s+a\s+budget\b|\b(?:cheap|affordable|economical|inexpensive|frugal)\b",
    re.I,
)


def _scan_budget_tier(text: str) -> str | None:
    """Map a qualitative budget descriptor in the text to a lodging price tier.

    Returns one of "shoestring" / "budget" / "mid" / "luxury", or None when the text
    carries no qualitative budget signal. Precedence: an explicit luxury cue wins
    (users who say "luxury" mean it); then shoestring; then mid; then budget. Pure /
    deterministic / var-0 safe (regex over the user's words only)."""
    if not isinstance(text, str) or not text.strip():
        return None
    if _BUDGET_TIER_LUXURY_RE.search(text):
        return "luxury"
    if _BUDGET_TIER_SHOESTRING_RE.search(text):
        return "shoestring"
    if _BUDGET_TIER_MID_RE.search(text):
        return "mid"
    if _BUDGET_TIER_BUDGET_RE.search(text):
        return "budget"
    return None


# round-2 #budget-tier-plans-fix (CRITICAL): a QUALITATIVE tier with NO dollar amount
# ("2 adults, Kyoto, mid budget") previously bounced straight to
# needs_clarification[budget] — the tier IS a real budget signal, just not a number, and
# demanding a dollar figure when the user already told us the STYLE they want is a
# conversion-killing false decline. Sensible, conservative USD-cents-per-traveller-per-
# night bands, used ONLY to derive an IMPLIED total budget (tier x nights x party) when
# the text carries no numeric amount at all — an explicit dollar figure always wins and
# this table is never consulted when one is present. Deterministic / var-0 safe: pure
# arithmetic over the user's own words + already-parsed trip facts, no price/live/catalog
# data. Calibrated so a typical 2-adult trip's IMPLIED per-night lodging share (see
# orchestrator._resolve_per_night_cap, 45% of total) lands near the middle of that tier's
# star band rather than at either extreme.
_TIER_IMPLIED_USD_CENTS_PER_PERSON_NIGHT: dict[str, int] = {
    "shoestring": 3_500,   # ~$35/traveller/night — hostel/dorm-tier lodging + street food
    "budget": 7_000,       # ~$70/traveller/night — modest 2-3* lodging + casual meals
    "mid": 22_000,         # ~$220/traveller/night — comfortable 3-4* lodging + dining out
    "luxury": 40_000,      # ~$400/traveller/night — 5* lodging + fine dining
}


def _implied_budget_cents_from_tier(tier: str | None, nights: int | None, party: int) -> int | None:
    """Derive an IMPLIED total USD-cents budget from a qualitative tier x nights x
    party size, for use ONLY when the text stated a tier but no dollar amount at all.
    Returns None for an unrecognised tier. `nights` conservatively floors to 1 (a
    tier without a stated duration still deserves a plannable estimate); `party`
    floors to 1. Pure arithmetic — deterministic / var-0 safe."""
    per_person_night = _TIER_IMPLIED_USD_CENTS_PER_PERSON_NIGHT.get(tier or "")
    if not per_person_night:
        return None
    n = nights if (nights and nights > 0) else 1
    p = max(1, party)
    return per_person_night * n * p


# fix-round-2: some single-word vibe synonyms are ambiguous between naming a
# genuine trip VIBE and naming something else that merely shares the token —
# a flight cabin CLASS ("premium economy", not a luxury-trip descriptor) or
# the TRAVELERS themselves ("an active retirement couple", not a stated
# adventure-trip vibe). Rather than drop these synonyms outright (which would
# lose real "premium hotel"/"an active holiday" vibe signals), exclude only
# the specific trailing context that is never a vibe — an optional single
# filler word (e.g. "retirement") is tolerated between the token and the
# people-noun so "an active retirement couple" is still caught.
_VIBE_TOKEN_EXCLUDE_AFTER: dict[str, re.Pattern] = {
    "premium": re.compile(r"\s+economy\b", re.I),
    "active": re.compile(
        r"\s+(?:\w+\s+)?(?:family|families|couple|retirees?|retirement|"
        r"seniors?|travell?ers?|parents?|adults?|group|person|people)\b",
        re.I,
    ),
}

# fix-round-4: same ambiguous-synonym problem as above, but the disambiguating
# context sits BEFORE the token instead of after it.
#   "waves" -> surf false-fires on "heat/radio/sound/brain/crime waves" — none
#   of those name a surf trip vibe.
#   "art" -> culture false-fires on the idiom "state of the art"/"state-of-
#   the-art" (cutting-edge, not a culture-trip descriptor).
_VIBE_TOKEN_EXCLUDE_BEFORE: dict[str, re.Pattern] = {
    "waves": re.compile(r"(?:heat|radio|sound|brain|crime)\s*$", re.I),
    "art": re.compile(r"state[\s-]+of[\s-]+the[\s-]*$", re.I),
}


def _scan_vibe_sequence(text: str) -> list[str]:
    """
    Extract an ORDERED list of canonical vibes from text.

    Handles "relax then beach" → ["relax", "beach"] by scanning for vibe
    keywords in document order. Deduplicates consecutive identical vibes.

    Returns a list of canonical vibe strings (may be empty).
    """
    lowered = text.lower()

    # fix-round-1: a vibe word can be the TRAILING token of a legitimate
    # multi-word CITY name ("City" in "Mexico City"/"Panama City", "Beach" in
    # "Long Beach") — without masking those spans, the destination token is
    # re-read as a trip vibe, either silently overriding the user's real
    # stated vibe (single-city legs keep only the FIRST vibe hit) or
    # fabricating a phantom extra leg (with a temporal "then"). Resolved city
    # spans win; a vibe hit fully inside one is excluded here.
    _resolved_city_spans = [(s, e) for s, e, _slug in _scan_city_sequence_spans(text)]

    def _inside_resolved_city(pos: int) -> bool:
        return any(s <= pos < e for s, e in _resolved_city_spans)

    # Build a combined mapping: token → canonical vibe (direct names + synonyms)
    token_to_vibe: dict[str, str] = {}
    for vibe in ALLOWED_VIBES:
        token_to_vibe[vibe] = vibe
    for synonym, canonical in _VIBE_SYNONYMS.items():
        if synonym not in token_to_vibe:
            token_to_vibe[synonym] = canonical

    # Find all vibe tokens and their positions in document order
    hits: list[tuple[int, str]] = []  # (start_pos, canonical_vibe)
    for token, canonical in token_to_vibe.items():
        pattern = r"\b" + re.escape(token) + r"\b"
        _exclude_after = _VIBE_TOKEN_EXCLUDE_AFTER.get(token)
        _exclude_before = _VIBE_TOKEN_EXCLUDE_BEFORE.get(token)
        for m in re.finditer(pattern, lowered):
            if _inside_resolved_city(m.start()):
                continue
            if _exclude_after is not None and _exclude_after.match(lowered, m.end()):
                continue
            if _exclude_before is not None and _exclude_before.search(lowered[:m.start()]):
                continue
            hits.append((m.start(), canonical))

    if not hits:
        return []

    # Sort by position → preserves stated order ("relax then beach")
    hits.sort(key=lambda x: x[0])

    # Preserve ALL vibes in document order — NO dedup.
    # Distinct legs with the same vibe must be preserved (SEV-3 fix).
    # E.g. "beach, culture, beach" → ["beach", "culture", "beach"] (3 legs).
    ordered = [canonical for _, canonical in hits]

    return ordered


def _split_nights_evenly(total_nights: int, leg_count: int) -> list[int]:
    """
    Split total_nights evenly across leg_count legs.

    Uses integer floor for each leg; remainder added to the LAST leg.
    This replicates the planner_agent.py proportional split when all legs
    are equal-weight (equal nights), making the result deterministic.

    Examples:
        _split_nights_evenly(5, 2) → [2, 3]
        _split_nights_evenly(6, 2) → [3, 3]
        _split_nights_evenly(5, 3) → [1, 1, 3]  (floor=1 each; remainder 2 → last)
    """
    if leg_count <= 0:
        return []
    if leg_count == 1:
        return [total_nights]
    per_leg = total_nights // leg_count
    remainder = total_nights - per_leg * leg_count
    nights = [per_leg] * leg_count
    nights[-1] += remainder
    return nights


def _build_legs_with_dates(
    city_vibe_pairs: list[tuple[str, str | None]],
    total_nights: int,
    adults: int,
    interests: list[str] | None = None,
    interest_map: dict[str, list[str]] | None = None,
    start_date: str | None = None,
    children: int = 0,
) -> list[dict]:
    """
    Build fully-specified leg dicts from qualitative city/vibe pairs.

    Splits total_nights evenly across legs (deterministic); assigns
    contiguous dates starting from DEFAULT_START_DATE.  Per-leg nights and
    dates are ALWAYS deterministic — they do not come from the LLM.

    Args:
        city_vibe_pairs: Ordered list of (city, vibe_or_None).
        total_nights:    Deterministically extracted total night count.
        adults:          Deterministically extracted adult count.
        interests:       Optional pre-scanned/expanded activity interest tokens
                         (Phase 1/2). When present, attached to every leg verbatim.
                         Already sorted by caller (var-0).

    Returns:
        List of leg dicts with city, checkin, checkout, adults, nights,
        and optionally vibe/interests.  Never returns an empty list (caller checks).
    """
    leg_count = len(city_vibe_pairs)
    nights_split = _split_nights_evenly(total_nights, leg_count)

    legs: list[dict] = []
    cursor = date.fromisoformat(start_date or DEFAULT_START_DATE)
    for (city, vibe), nights in zip(city_vibe_pairs, nights_split):
        checkin = cursor.isoformat()
        cursor += timedelta(days=nights)
        checkout = cursor.isoformat()
        leg: dict[str, Any] = {
            "city": city,
            # place_key drives the HEALTH gate (its city→country→ISO2 slate
            # cascade). Free-text legs must set it or the health gate silently
            # skips them (cardinal-sin silent pass). City slug is the slate key.
            "place_key": city,
            "checkin": checkin,
            "checkout": checkout,
            "adults": adults,
            "children": children,  # #party-fix: carry kids through to planning (0 when none)
        }
        if vibe is not None:
            leg["vibe"] = vibe
        if interests:
            leg["interests"] = interests  # already sorted (var-0)
        if interest_map:
            leg["interest_map"] = interest_map
        iso2 = CITY_TO_ISO2.get(city)
        if iso2:
            leg["dest_country"] = iso2
        else:
            logger.info("intent_parser: no ISO2 for city %r — dest_country unset (gate will FLAG)", city)
        legs.append(leg)
    return legs


# Temporal SEQUENCE markers — only these split a single city into multiple
# date-separated stays. Vibes merely joined by "and"/"," describe ONE stay.
# fix-round-2: bare "\bnext\b" matched the overwhelmingly common date/
# preposition phrases "next week"/"next month"/"next year"/"next time"/
# "next to (the ...)"/"next door" — none of which are a temporal-SEQUENCE
# connector — wrongly splitting a single-city comma/"and"-joined multi-vibe
# request into phantom same-city stays (e.g. "relax and culture in Kyoto
# next week" -> two Kyoto legs instead of one). Excluded via a negative
# lookahead rather than removing "next" outright, so a genuine sequence use
# ("beach, next culture, next city") is unaffected.
_SEQUENCE_MARKER_RE = re.compile(
    r"\bthen\b|\bafter\s+that\b|\bafterwards?\b|\bfollowed\s+by\b|"
    r"\bnext\b(?!\s+(?:week|month|year|time|to|door))|->|→|;",
    re.I,
)


def _first_raw_vibe_word(text: str, canonical: str) -> str:
    """
    fix-round-3: return the literal token the user actually TYPED for a
    canonical vibe (e.g. "nightlife" for canonical "city"), so a dropped-
    vibe honesty note can quote the user's own word instead of an internal
    synonym-normalised token they never wrote. Falls back to the canonical
    string itself if (unexpectedly) no matching literal token is found.
    """
    token_to_vibe: dict[str, str] = {v: v for v in ALLOWED_VIBES}
    for synonym, canon in _VIBE_SYNONYMS.items():
        token_to_vibe.setdefault(synonym, canon)
    lowered = text.lower()
    best_pos: int | None = None
    best_tok: str | None = None
    for tok, canon in token_to_vibe.items():
        if canon != canonical:
            continue
        m = re.search(r"\b" + re.escape(tok) + r"\b", lowered)
        if m and (best_pos is None or m.start() < best_pos):
            best_pos = m.start()
            best_tok = text[m.start():m.end()]
    return best_tok if best_tok is not None else canonical


def _sequence_marker_between_vibes(text: str, vibe_seq: list[str]) -> bool:
    """
    fix-round-3: True only when a _SEQUENCE_MARKER_RE hit sits BETWEEN the
    first and second vibe mentions in *text* — not merely anywhere in the
    whole text (see the call site in ``_single_city_legs`` for the false-
    positive class this closes). Locates vibe-token positions the same way
    ``_scan_vibe_sequence`` does (direct vibe names + synonyms).
    """
    if len(vibe_seq) < 2:
        return False
    token_to_vibe: dict[str, str] = {v: v for v in ALLOWED_VIBES}
    for synonym, canonical in _VIBE_SYNONYMS.items():
        token_to_vibe.setdefault(synonym, canonical)
    lowered = text.lower()

    def _first_pos(canonical: str, start: int) -> int | None:
        best: int | None = None
        for tok, canon in token_to_vibe.items():
            if canon != canonical:
                continue
            m = re.search(r"\b" + re.escape(tok) + r"\b", lowered[start:])
            if m and (best is None or m.start() < best):
                best = m.start()
        return start + best if best is not None else None

    pos1 = _first_pos(vibe_seq[0], 0)
    if pos1 is None:
        return False
    pos2 = _first_pos(vibe_seq[1], pos1 + 1)
    if pos2 is None:
        return False
    return bool(_SEQUENCE_MARKER_RE.search(text[pos1:pos2]))


def _single_city_legs(
    city: str,
    text: str,
    vibe_seq: list[str],
    interests: list[str] | None = None,
) -> list[dict]:
    """
    Deterministic single-city leg topology — SHARED by the LLM and regex-fallback
    paths so both produce byte-identical legs (§10.11 var-0 across paths).

    A single city splits into one leg per vibe ONLY when the user expresses a
    temporal SEQUENCE ("relax then beach" → 2 stays). Vibes merely joined by
    "and"/"," ("beach and surf") describe a SINGLE stay → one leg (first vibe),
    not two same-city bookings. With no recognised vibe, a single bare leg.

    *interests* — optional pre-scanned activity tokens (Phase 1). When supplied,
    each leg gets an ``interests`` field with the expanded catalog terms (Phase 2).
    All legs in a single-city trip share the same interests (trip-level signal).
    var-0: interests is already sorted (from _scan_interests / _expand_interests).
    """
    expanded = _expand_interests(interests) if interests else None
    term_map = _interest_term_map(interests) if interests else None

    def _leg(city: str, vibe: str | None = None) -> dict:
        d: dict[str, Any] = {"city": city}
        if vibe is not None:
            d["vibe"] = vibe
        if expanded:
            d["interests"] = expanded
        if term_map:
            d["interest_map"] = term_map
        return d

    # fix-round-3: the split gate previously searched the ENTIRE text for a
    # sequence marker, so a stray "then"/";" sitting ANYWHERE — including an
    # unrelated trailing clause ("...then find me a nice hotel") or an
    # ordinary semicolon used as clause punctuation ("beach and culture; 7
    # nights, $3000") — flipped a comma/"and"-joined single-city multi-vibe
    # request (which this function's own docstring says should collapse to
    # ONE stay) into a fabricated second same-city stay. The marker must
    # actually sit BETWEEN two vibe mentions to mean a temporal sequence.
    if len(vibe_seq) >= 2 and _sequence_marker_between_vibes(text, vibe_seq):
        return [_leg(city, v) for v in vibe_seq]
    if vibe_seq:
        leg = _leg(city, vibe_seq[0])
        # fix-round-1: comma/"and"-joined multi-vibe requests for a single city
        # ("relax and culture") collapse to ONE leg (by design — see docstring),
        # but the un-kept vibe(s) were previously discarded with NO disclosure
        # anywhere in the result. Surface them additively so a caller can
        # honestly note "your other stated interest(s) weren't planned for" —
        # never silently vanish. Purely additive; does not change any existing
        # field or the single-leg topology.
        if len(vibe_seq) > 1:
            # fix-round-3: surface the literal word the user TYPED ("nightlife"),
            # not the internal canonical token it normalises to ("city") — an
            # honesty note that quotes a word the user never wrote undermines the
            # very disclosure it exists to provide.
            # fix-round-4: _scan_vibe_sequence deliberately does NOT dedup, so
            # two DIFFERENT typed words that are synonyms for the SAME canonical
            # vibe ("beach"+"seaside", "ocean"+"sea") produce vibe_seq entries
            # equal to the KEPT vibe (vibe_seq[0]) — nothing was actually
            # dropped (the single leg already covers it), but the old
            # unconditional slice reported a phantom drop, and worse, the note
            # would quote the kept vibe as an interest that "wasn't planned
            # for", directly contradicting itself. Exclude any vibe_seq entry
            # whose canonical equals the kept vibe before surfacing the drop.
            _dropped_canonical = [v for v in vibe_seq[1:] if v != vibe_seq[0]]
            if _dropped_canonical:
                leg["dropped_vibes"] = [
                    _first_raw_vibe_word(text, v) for v in _dropped_canonical
                ]
        return [leg]
    return [_leg(city)]


# Trip-continuation words that may legitimately FOLLOW a region phrase ("eastern europe FOR
# two weeks", "tuscany AND rome", "kansai ITINERARY"). Anything else directly after an
# ARTICLE + region ("the north india DESK", "the central japan OFFICE") is a noun the region
# merely modifies — prose, not a destination. Decline-by-default keeps the never-fabricate line.
_REGION_POST_OK = re.compile(
    r"(?:for|with|and|then|plus|or|over|during|this|next|in|on|by|to|at|&|"
    r"region|area|coast|coastline|countryside|peninsula|side|part|itinerary|trip|"
    r"holiday|holidays|vacation|tour|loop|circuit|route|leg|portion|getaway|adventure|"
    r"\d)\b")


def _region_used_as_destination(low: str, phrase: str) -> bool:
    """HONESTY GATE for region expansion: the region phrase must be used as a DESTINATION —
    the trip SUBJECT, or directly after a travel cue / a duration — NOT as a noun modifier, so
    "at the north india desk" / "the central japan office" never fabricate a trip. Mirrors the
    country destination-use gate (_substitute_country_with_city). `low` is the lower-cased text."""
    # Locate the phrase the SAME way scan_region did — word-boundary-anchored — so a region
    # embedded in a larger word ("iberia" inside "liberian") can't mis-anchor pre/post.
    m = re.search(r"\b" + re.escape(phrase) + r"\b", low)
    if not m:
        return False
    pre = low[:m.start()].rstrip()
    post = low[m.end():].lstrip()
    # Noun-modifier compound: "[article] <region> <bare-noun>" (the north india DESK) → prose.
    if re.search(r"\b(?:the|a|an)$", pre) and re.match(r"[a-z]", post) and not _REGION_POST_OK.match(post):
        return False
    # Destination position: subject / a trailing travel cue / a duration immediately before.
    pre_core = re.sub(r"\s+(?:the|a|an)$", "", pre)
    # fix-round-3: a possessive GENITIVE ("southern Italy's beaches are
    # gorgeous") makes the region phrase a noun-MODIFIER of what follows —
    # topical prose, not a destination — mirroring the same fix in the
    # country destination-use gate (_substitute_country_with_city). `post`
    # is already left-stripped, so the possessive "'s" would otherwise sit
    # immediately at its start with no separating word.
    _is_possessive_topic = post.startswith("'") or post.startswith("’")
    is_subject = (
        pre_core.strip() in ("", "the", "a", "an") and not _is_possessive_topic
    )
    has_cue = bool(_DEST_CUE_RE.search((pre_core + " ")[-25:]))
    # "tour the whole region" cue ("around hokkaido", "road trip through kyushu") → a multi-city
    # regional request; without this, "10 days around hokkaido" failed the gate and collapsed to one city.
    has_tour_cue = bool(_REGION_TOUR_CUE_RE.search(pre_core[-25:]))
    dur_before = bool(re.search(
        r"(?:\d+|\b(?:a|one|two|three|four|few|couple(?:\s+of)?))\s*(?:days?|nights?|weeks?)$",
        pre_core))
    return is_subject or has_cue or has_tour_cue or dur_before


# fix-round-1: "<direction> of <country>" ("the south of France", "the south
# of Italy") is at least as common/idiomatic as the adjacent "<direction>
# <country>" form the curated region registry keys on, but was never
# recognised — "of" simply isn't part of any registry key, so scan_region
# (and the destination-use gate above) missed it entirely and the whole
# request hard-declined despite the exact anchor cities existing for the
# sibling "southern France" phrasing. Normalising away the "of" (only between
# a direction word and what follows) lets every existing "<direction>
# <country>" registry key match unchanged — see _maybe_expand_region below.
_REGION_OF_RE = re.compile(r"\b(north|south|east|west)(ern)?\s+of\s+", re.I)


def _maybe_expand_region(text: str, city_seq: list[str]) -> list[str]:
    """REGION EXPANSION: a REGIONAL request ("central japan", "eastern europe", "northern
    vietnam") that the city scanner resolves to <=1 city → expand to curated, catalog-bookable
    anchor cities sized to the trip length + pace, so a 2-week regional trip is a multi-city
    itinerary rather than one gateway. Deterministic; an explicit multi-city list (>=2) is left
    untouched. Returns the (possibly expanded) sequence."""
    # fix-round-1: normalise "the south of France" -> "the south France" so it
    # matches the same registry key as "southern France" / "south France" —
    # see _REGION_OF_RE above. Purely additive (never removes a real match);
    # used for region-detection purposes only, not for city/duration scans.
    _region_text = _REGION_OF_RE.sub(r"\1\2 ", text)
    phrase = region_expansion.scan_region(_region_text)
    # fix-round-4: the substitution above always emits the BARE direction
    # form ("north vietnam") when the input itself has no "-ern" suffix
    # ("the north of vietnam") — but the region registry sometimes keys a
    # country ONLY under the "-ern" form ("northern vietnam", no "north
    # vietnam" entry at all, e.g. Vietnam), so a genuine directional
    # destination request hard-declined despite "northern vietnam" resolving
    # fine. Retry with the "-ern" form normalised instead, so either
    # registry-key shape resolves; purely additive, never removes a match
    # the bare form already found.
    if not phrase:
        _region_text_ern = _REGION_OF_RE.sub(
            lambda m: m.group(1) + (m.group(2) or "ern") + " ", text,
        )
        phrase_ern = region_expansion.scan_region(_region_text_ern)
        if phrase_ern:
            _region_text = _region_text_ern
            phrase = phrase_ern
    if not phrase or len(city_seq) > 1:
        return city_seq
    low = _region_text.lower()
    # HONESTY: a directional phrase naming a DISTINCT country/feature ("south china sea" is not
    # mainland China) OR a region used as a noun modifier / prose ("the north india desk") must
    # NOT be expanded into a fabricated trip — same destination-use discipline as the country path.
    if _REGION_EXCLUDE_RE.search(low) or not _region_used_as_destination(low, phrase):
        return city_seq
    expanded = region_expansion.expand_region(_region_text, _scan_nights(text) or 0)
    if len(expanded) > len(city_seq):
        return expanded
    return city_seq


def _regex_fallback(text: str) -> dict | None:
    """
    Deterministic regex/keyword parser for free text.

    Returns a raw dict (pre-clamp) or None if insufficient info.
    Does NOT produce legs with checkin/checkout or nights — those are
    computed deterministically in _clamp_and_validate. Produces only
    the qualitative leg structure (city + vibe).

    MULTI-CITY: uses _scan_city_sequence to detect an ordered city list
    ("Bangkok then KL then Singapore" → 3 legs, distinct cities). When the
    text names exactly ONE city, uses _scan_vibe_sequence to detect multi-vibe
    single-city trips ("relax then beach" → 2 legs, same city).
    """
    city_seq = _maybe_expand_region(text, _scan_city_sequence(text))
    # BUG 1 fix: this function re-derives its OWN city sequence from raw text (it does
    # not reuse parse_intent's det_city_seq), so the country/city-name collision fix
    # must be re-applied here too — otherwise the LLM-off path (this fallback runs
    # whenever the LLM is unavailable/fails, which is the entire deterministic test
    # surface) silently resolves "a week in Jamaica" to Queens, NY instead of Kingston.
    city_seq = _disambiguate_country_city_collision(text, city_seq)
    if not city_seq:
        logger.warning("intent_parser: regex_fallback: no city found in text")
        return None

    # fix-round-2: the SAME misresolved-slug guard parse_intent applies (see
    # its "HONESTY GUARD (disambiguation class)" comment) must be re-applied
    # HERE too — this function re-derives its own city sequence from raw text
    # (per the BUG-1-fix comment above) rather than reusing parse_intent's
    # det_city_seq, so the strip that guard performs never reached this,
    # always-available, LLM-off path. Without it, a landmark/venue whose
    # truncated 2-word prefix happens to also resolve to a SAME-named catalog
    # city ("Sydney Opera House" -> Sydney, "Westminster Abbey" -> Westminster,
    # US) planted a phantom leg to the WRONG city while the dropped-leg notice
    # simultaneously claimed the phrase was dropped — violating the "never
    # silently substitute a different city" invariant.
    _unknown_places_fb = _scan_unknown_places(text)
    if _unknown_places_fb:
        _misresolved_fb = _misresolved_city_slugs(text)
        if _misresolved_fb:
            city_seq = [c for c in city_seq if c not in _misresolved_fb]
            if not city_seq:
                logger.warning(
                    "intent_parser: regex_fallback: city sequence emptied by "
                    "misresolved-slug guard (no genuine supported city remains)"
                )
                return None

    budget_cents = _scan_budget_cents(text)
    if budget_cents is None:
        # round-2 #budget-tier-plans-fix: a qualitative tier ("mid budget",
        # "budget-friendly", "luxury", "cheap"...) with no dollar figure IS a real
        # budget signal — do not bail here. The caller (parse_intent) derives an
        # implied `det_budget_cents` from the tier and passes it into
        # _clamp_and_validate, whose det_budget_cents param ALWAYS overrides this
        # raw dict's total_budget_cents (see _clamp_and_validate's budget-priority
        # docstring), so 0 here is an inert placeholder that is never surfaced.
        if _scan_budget_tier(text):
            budget_cents = 0
            logger.info(
                "intent_parser: regex_fallback: no $ stated but a qualitative budget "
                "tier is present — continuing (the caller derives an implied budget "
                "from the tier rather than declining)"
            )
        else:
            logger.warning("intent_parser: regex_fallback: no budget found in text")
            return None

    adults = _scan_adults(text)

    # #per-person-budget-fix: mirrors parse_intent's own top-level fix (see
    # _scan_budget_is_per_person's module comment) — this raw dict's total_budget_cents
    # is normally overridden by parse_intent's own det_budget_cents (already multiplied),
    # but applying it here too keeps this function correct for any standalone caller
    # that trusts its returned total_budget_cents directly. Party = adults only (this
    # fallback does not track children separately).
    if budget_cents and _scan_budget_is_per_person(text):
        _fb_party = max(1, adults)
        logger.info(
            "intent_parser: regex_fallback: budget %d¢ stated PER-PERSON -> x%d party",
            budget_cents, _fb_party,
        )
        budget_cents = budget_cents * _fb_party

    # Companion-city expansion (e.g. Vietnam "cruise" → Ha Long) — applied here too so the
    # regex-fallback path stays byte-identical to the LLM path (var-0 across modes).
    city_seq, _comp_vibes = _expand_companion_legs(text, city_seq, MAX_LEGS)
    # Collapse a district-alias that resolved to a parent city already named elsewhere
    # ("Kyoto and Osaka ... near Gion and Namba") — mirrors parse_intent so the LLM-off
    # fallback path builds the SAME distinct-city legs (var-0 across modes).
    city_seq = _dedupe_cities_preserve_order(city_seq)

    # Activity intent capture (Phase 1): scan for free-text interest tokens BEFORE
    # leg construction so every leg gets the same trip-level interests list (var-0).
    _interests = _scan_interests(text)

    if len(city_seq) >= 2:
        # MULTI-CITY trip: one leg per city, in stated order. Pair each city
        # with the vibe that appears nearest after its mention, if any.
        legs: list[dict[str, Any]] = _pair_cities_with_vibes(
            text, city_seq, _comp_vibes, interests=_interests or None
        )
    else:
        # SINGLE-CITY trip: a temporal sequence ("relax then beach") splits into
        # multiple stays; "beach and surf" stays one. Shared with the LLM path.
        legs = _single_city_legs(
            city_seq[0], text, _scan_vibe_sequence(text), interests=_interests or None
        )

    return {
        "total_budget_cents": budget_cents,
        "adults": adults,
        "legs": legs,
    }


def _expand_companion_legs(
    text: str, city_seq: list[str], max_legs: int
) -> tuple[list[str], dict[str, str]]:
    """Append an activity-implied COMPANION city to the ordered city sequence.

    When a gateway city in *city_seq* is paired with an activity token in *text* that maps to a
    well-known companion catalog city (a Vietnam "cruise" out of the Hanoi gateway is a Ha Long Bay
    cruise), the companion is appended as an extra leg. Deterministic (var-0): frozen-dict lookup,
    word-boundary scan, fixed document order. HONEST: the companion must be a real catalog city and
    is added ONLY on an exact (gateway, activity) hit — never fabricated from a bare activity word,
    never standing alone, never past max_legs. Returns (expanded_seq, {companion_city: leg_vibe});
    a no-op returns the input sequence and an empty map.
    """
    if not _COMPANION_CITY_BY_ACTIVITY:
        return city_seq, {}
    lowered = text.lower()
    new_seq = list(city_seq)
    companion_vibes: dict[str, str] = {}
    for gw in city_seq:                                 # input gateways, document order (var-0)
        for activity in _COMPANION_ACTIVITY_TOKENS:     # sorted tuple (var-0)
            if len(new_seq) >= max_legs:
                break
            # Right-boundary excludes hyphenated non-activities ("cruise-control") since "-" is a
            # regex word boundary; left \b still anchors the start. Precision guard (audit).
            if not re.search(r"\b" + re.escape(activity) + r"(?![\w-])", lowered):
                continue
            entry = _COMPANION_CITY_BY_ACTIVITY.get((gw, activity))
            if entry is None:
                continue
            companion, vibe = entry
            if companion in ALLOWED_CITIES:
                if companion not in new_seq:
                    new_seq.append(companion)
                # CLAIM the activity vibe for the companion even if it's already in the sequence
                # (e.g. region expansion already added Ha Long) — so the gateway doesn't absorb it.
                companion_vibes.setdefault(companion, vibe)
    return new_seq, companion_vibes


def _pair_cities_with_vibes(
    text: str,
    city_seq: list[str],
    companion_vibes: dict[str, str] | None = None,
    interests: list[str] | None = None,
) -> list[dict]:
    """
    For a MULTI-CITY trip, pair each ordered city with the vibe keyword that
    appears closest AFTER it (and before the next city), if any.

    Deterministic: position-based, same text → same pairing → variance-0.

    D6 #41 — uses _CITY_RE and _CITY_TOKEN_TO_SLUG (module-load compiled)
    rather than rebuilding token_to_slug from scratch on every call.

    *interests* — optional pre-scanned activity tokens (Phase 1). When supplied,
    every leg gets an ``interests`` field with the expanded catalog terms (Phase 2).
    All legs share the same trip-level interests (the text rarely assigns activities
    to individual cities; they apply to the whole itinerary). var-0: expanded list
    is already sorted.
    """
    lowered = text.lower()
    companion_vibes = companion_vibes or {}
    # Position-pair only cities actually present in the text. Expansion-added companion cities
    # (e.g. Ha Long from a Vietnam cruise) are NOT in the text, so a degenerate position for them
    # would truncate a real city's vibe window — instead pair the real cities, then append the
    # companions afterward with their authoritative vibe (var-0; companions are a suffix already).
    real_seq = [c for c in city_seq if c not in companion_vibes]

    # Expand interests once for the whole trip (var-0: sorted by _expand_interests).
    expanded = _expand_interests(interests) if interests else None
    term_map = _interest_term_map(interests) if interests else None

    # Build slug→tokens map from the module-level table for position scanning.
    slug_to_tokens: dict[str, list[str]] = {}
    for tok, slug in _CITY_TOKEN_TO_SLUG.items():
        slug_to_tokens.setdefault(slug, []).append(tok)

    city_positions: list[int] = []
    # fix-round-3: tracks whether each city's position is a REAL text match
    # (True) vs the degenerate cursor fallback used for a region-expanded
    # anchor city that has no literal token in the text at all (False) — the
    # cities-then-vibes positional-pairing special case below must never
    # fire on those dummy sequential positions (see the region-expansion
    # regression this guards against).
    city_positions_found: list[bool] = []
    cursor = 0
    for slug in real_seq:
        best = None
        for tok in slug_to_tokens.get(slug, [slug]):
            m = re.search(r"\b" + re.escape(tok) + r"\b", lowered[cursor:])
            if m and (best is None or m.start() < best):
                best = m.start()
        pos = cursor + best if best is not None else cursor
        city_positions.append(pos)
        city_positions_found.append(best is not None)
        cursor = pos + 1

    # Vibe hits with positions.
    token_to_vibe: dict[str, str] = {v: v for v in ALLOWED_VIBES}
    for syn, canon in _VIBE_SYNONYMS.items():
        token_to_vibe.setdefault(syn, canon)
    vibe_hits: list[tuple[int, str]] = []
    for tok, canon in token_to_vibe.items():
        for m in re.finditer(r"\b" + re.escape(tok) + r"\b", lowered):
            vibe_hits.append((m.start(), canon))
    vibe_hits.sort()

    # A vibe already CLAIMED by an activity-implied companion (e.g. "cruise" → Ha Long) belongs to
    # that companion, NOT the gateway it was triggered from — so the gateway (Hanoi) takes the NEXT
    # unclaimed vibe ("culture"), giving the semantically-correct hanoi=culture / ha long=cruise.
    claimed_vibes = set(companion_vibes.values())

    # fix-round-1: EXPLICIT vibe->city bindings — "<vibe> [in/for/at/to] <City>", either
    # the prepositional form ("beach in Da Nang", pre-enumerated "culture in Bangkok")
    # or the bare adjective form ("a relaxing Hanoi trip") — ties a vibe DIRECTLY to a
    # named city regardless of city-listing order or the forward-window position below.
    # Without this: (a) a vibe sitting BEFORE its intended city was either swallowed by
    # an unrelated PRECEDING city's forward window (the last-listed city's window spans
    # to end-of-text and greedily grabs the first hit) or never captured at all; (b) a
    # trailing "vibe in City" clause written AFTER every city was already listed left the
    # earlier cities' (empty) forward windows with nothing. Reserved so no other city's
    # window scan can steal a vibe that is explicitly bound elsewhere.
    _vibe_alt = "|".join(sorted((re.escape(t) for t in token_to_vibe), key=len, reverse=True))
    _explicit_bind_vpos_to_city: dict[int, str] = {}
    if _vibe_alt:
        for slug in real_seq:
            if slug not in lowered:
                continue  # region-expanded city has no literal token to bind against
            for tok in slug_to_tokens.get(slug, [slug]):
                for m in re.finditer(
                    r"\b(" + _vibe_alt + r")\b\s*(?:in|for|at|to)?\s+" + re.escape(tok) + r"\b",
                    lowered,
                ):
                    _explicit_bind_vpos_to_city.setdefault(m.start(1), slug)

    # Region-expanded cities (from a "central japan"/"northern vietnam" request) are NOT literal in
    # the text, so they have no position window. Distribute the trip's unclaimed vibes to them in
    # order (cruise→Ha Long is companion-claimed, so Hanoi takes the next one, culture). var-0.
    _vibe_pool: list[str] = []
    for _, _vc in vibe_hits:
        if _vc not in claimed_vibes and _vc not in _vibe_pool:
            _vibe_pool.append(_vc)
    _used_vibes: set[str] = set()

    # fix-round-3: when EVERY real city is named before ANY (unclaimed,
    # unbound) vibe is stated at all ("Hanoi, Hue and Da Nang: culture,
    # history and beach"), the last city's unbounded forward window
    # (next_pos = len(lowered)+1) greedily grabs the FIRST such vibe while
    # every earlier city's window is empty — earlier legs silently lose
    # their vibe and the last leg is mislabeled with the wrong (first-
    # listed) vibe instead of the one positionally aligned to it. Detect
    # this shape and fall back to a straight proportional pairing: the Nth
    # city takes the vibe at floor(N * len(vibes) / len(cities)) — matching
    # how a reader would naturally align "City1, City2, City3: vibeA,
    # vibeB, vibeC", and naturally carrying a vibe forward to a later city
    # when there are fewer vibes than cities.
    _trailing_vibes: list[str] = [
        vcanon for vpos, vcanon in vibe_hits
        if vcanon not in claimed_vibes and vpos not in _explicit_bind_vpos_to_city
    ]
    # bug fix: a vibe word GLUED directly onto the LAST city's own tokens with no
    # separator ("ha long beach") is a compound reference to THAT city alone, not a
    # genuine trailing vibe LIST for proportional distribution across every city
    # ("Hanoi, Hue and Da Nang: culture, history and beach", where the vibes are
    # clearly separated from the cities by ": "). Distinguish the two by requiring
    # something other than pure whitespace between the end of the last city's own
    # token match and the first unclaimed/unbound trailing vibe — without this,
    # "hanoi then ha long beach" incorrectly proportionally-paired "beach" onto
    # BOTH hanoi and ha long instead of leaving hanoi's vibe unset (the plain
    # per-city window logic already gets this right on its own; this guard just
    # stops the override from firing when it shouldn't).
    # adversarial-audit follow-up: take the LONGEST matching token's end, not the
    # first one tried — mirrors _build_city_regex's own longest-first sort
    # (module docstring: "sorted longest-first so the regex engine matches the
    # longest token on overlap"). slug_to_tokens has no such ordering guarantee
    # (built by iterating CITY_ALIASES/city_aliases.json/ALLOWED_CITIES in
    # whatever order those collections yield), so for ~68 slugs where a
    # SHORTER alias happens to be listed before the full name (e.g. "rio"
    # before "rio de janeiro") breaking on the first match underestimated
    # _last_city_end, leaving the city's own trailing characters ("de janeiro")
    # in the "gap" — non-whitespace, so the glued-vibe guard failed to fire.
    _last_city_end = city_positions[-1] if city_positions else 0
    if real_seq:
        for _tok in slug_to_tokens.get(real_seq[-1], [real_seq[-1]]):
            _m = re.match(re.escape(_tok), lowered[city_positions[-1]:])
            if _m:
                _last_city_end = max(_last_city_end, city_positions[-1] + _m.end())
    _cities_then_vibes = bool(
        len(real_seq) >= 2 and _trailing_vibes and city_positions
        # Never fire on a region-expanded trip, where some/all "positions"
        # are the degenerate cursor fallback (no literal city token in the
        # text at all) rather than real text positions — see
        # city_positions_found above.
        and all(city_positions_found)
        and city_positions[-1] < min(
            vpos for vpos, vcanon in vibe_hits
            if vcanon not in claimed_vibes and vpos not in _explicit_bind_vpos_to_city
        )
        and not re.fullmatch(
            r"\s*",
            lowered[_last_city_end:min(
                vpos for vpos, vcanon in vibe_hits
                if vcanon not in claimed_vibes and vpos not in _explicit_bind_vpos_to_city
            )],
        )
    )
    # fix-round-4: the MIRROR shape — "all vibes then all cities" ("we want
    # culture and nightlife: Bangkok and Singapore") — was entirely
    # unhandled. Every vibe precedes the FIRST city's position, so it falls
    # before every city's forward window (pos < vpos < next_pos never
    # matches), the explicit vibe->city bind only fires when a vibe sits
    # textually adjacent to a city token, and _cities_then_vibes's guard is
    # the exact opposite ordering — so every leg's vibe was silently dropped
    # with no clarification. Same proportional Nth-city-takes-Nth-vibe
    # pairing as _cities_then_vibes above, just triggered by the reversed
    # ordering check.
    _vibes_then_cities = bool(
        len(real_seq) >= 2 and _trailing_vibes and city_positions
        and all(city_positions_found)
        and max(
            vpos for vpos, vcanon in vibe_hits
            if vcanon not in claimed_vibes and vpos not in _explicit_bind_vpos_to_city
        ) < city_positions[0]
    )

    legs: list[dict] = []
    for i, (slug, pos) in enumerate(zip(real_seq, city_positions)):
        next_pos = city_positions[i + 1] if i + 1 < len(city_positions) else len(lowered) + 1
        vibe = None
        # fix-round-2: an explicit binding for THIS city is checked FIRST, before
        # the forward-window scan below — it may sit anywhere in the text (commonly
        # BEFORE its own position — "relaxing Hanoi", or a trailing "beach in Bali"
        # clause written after every city was listed). Previously the forward-window
        # scan ran first and only skipped a vibe bound to a DIFFERENT city; it never
        # checked whether THIS city had its own explicit binding elsewhere that
        # should take precedence, so an unbound vibe sitting in this city's forward
        # window was greedily grabbed FIRST, silently discarding this city's own
        # explicit "<vibe> <City>" binding and mislabeling the leg.
        for vpos, vcanon in vibe_hits:
            if (_explicit_bind_vpos_to_city.get(vpos) == slug
                    and vcanon not in claimed_vibes and vcanon not in _used_vibes):
                vibe = vcanon
                break
        if vibe is None and (_cities_then_vibes or _vibes_then_cities):
            # fix-round-3: proportional trailing-vibe-list pairing (see above) —
            # deliberately does NOT consult `_used_vibes`, since re-using the
            # same vibe across two positionally-mapped cities (when there are
            # fewer vibes than cities) is the CORRECT behaviour here, not a
            # re-grab bug.
            _vidx = i * len(_trailing_vibes) // len(real_seq)
            if _vidx < len(_trailing_vibes):
                vibe = _trailing_vibes[_vidx]
        if vibe is None:
            for vpos, vcanon in vibe_hits:
                if not (pos < vpos < next_pos):
                    continue
                # fix-round-1: never re-hand out a vibe a previous leg already took —
                # the LAST city's window spans to end-of-text and would otherwise
                # greedily re-grab the trip's FIRST vibe hit even after it was used.
                if vcanon in claimed_vibes or vcanon in _used_vibes:
                    continue
                # fix-round-1: a vibe explicitly bound to a DIFFERENT city (see above)
                # is reserved for that city, even though it falls inside THIS city's
                # window ("a relaxing Hanoi trip then ... beach in Da Nang" — "beach"
                # sits inside Hanoi's window but is reserved for Da Nang).
                _bound_to = _explicit_bind_vpos_to_city.get(vpos)
                if _bound_to is not None and _bound_to != slug:
                    continue
                vibe = vcanon
                break
        if vibe is None and slug not in lowered:   # region-expanded city → next unused trip vibe
            vibe = next((v for v in _vibe_pool if v not in _used_vibes), None)
        leg: dict[str, Any] = {"city": slug}
        if vibe is not None:
            _used_vibes.add(vibe)
            leg["vibe"] = vibe
        if expanded:
            leg["interests"] = expanded
        if term_map:
            leg["interest_map"] = term_map
        legs.append(leg)
    # Append expansion-added companion legs (a suffix of city_seq) with their authoritative vibe.
    # They were excluded from the positional pairing above, so they never disturbed a real city's
    # vibe window; an explicitly-typed companion is NOT in companion_vibes and keeps its text vibe.
    for slug in city_seq:
        if slug in companion_vibes:
            companion_leg: dict[str, Any] = {"city": slug, "vibe": companion_vibes[slug]}
            if expanded:
                companion_leg["interests"] = expanded
            if term_map:
                companion_leg["interest_map"] = term_map
            legs.append(companion_leg)
    return legs


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _llm_call(free_text: str) -> str:
    """
    Call DashScope to parse free_text into JSON via the active model (model_router).

    Returns the raw response string (may or may not be valid JSON).
    Raises RuntimeError if the API call fails or key is missing.
    """
    if not DASHSCOPE_API_KEY:
        raise RuntimeError(
            "DASHSCOPE_API_KEY not set in environment — LLM call unavailable"
        )

    # Denial-of-wallet breaker (default OFF; utils/cost_breaker.py). When the daily
    # LLM cap is reached — or the kill-switch is set — raise so parse_intent's
    # existing retry loop degrades to the DETERMINISTIC parser: the SAME graceful
    # fallback used when no key is present. No paid DashScope call is made. When the
    # breaker is disabled (no cap env), allow() is a byte-identical no-op.
    try:
        from utils.cost_breaker import get_breaker
    except ImportError:  # flat-module import fallback (mirrors dashscope_chat below)
        from cost_breaker import get_breaker  # type: ignore[no-redef]
    if not get_breaker().allow("llm"):
        raise RuntimeError(
            "cost breaker: daily LLM cap reached — deterministic fallback"
        )

    try:
        from utils.model_router import dashscope_chat
    except ImportError:
        from model_router import dashscope_chat  # type: ignore[no-redef]

    body = {
        "enable_thinking": False,  # qwen3.x reasoning models: skip thinking → fast structured parse
        # NOTE: httpx.post itself can raise on transport errors (ConnectTimeout,
        # ReadTimeout, ConnectError — all httpx.RequestError). It MUST be inside
        # the try: parse_intent only catches RuntimeError, so any bare httpx
        # exception escaping here would crash the parser instead of degrading to
        # the deterministic fallback. A network hiccup is a fallback, not a crash.
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": free_text},
        ],
        # Request JSON output mode
        "response_format": {"type": "json_object"},
    }
    try:
        data = dashscope_chat("default", body, timeout=30.0)
        content = data["choices"][0]["message"]["content"]
        return content if isinstance(content, str) else json.dumps(content)
    except httpx.HTTPStatusError as e:
        # SECURITY: the raw upstream response body is logged (server-side only),
        # never embedded in the exception message — a future caller that surfaces
        # this message to a client (e.g. a bare `except Exception: ... str(exc)`
        # handler, a pattern already used elsewhere in this codebase) must not be
        # able to leak upstream diagnostic text (account/request details) verbatim.
        logger.warning(
            "DashScope API error HTTP %s: %s",
            e.response.status_code, e.response.text[:300],
        )
        raise RuntimeError(f"DashScope API error HTTP {e.response.status_code}")
    except httpx.RequestError as e:
        # Transport-level failure (timeout / connection / DNS). Degrade, don't die.
        raise RuntimeError(f"DashScope transport error: {type(e).__name__}: {e}")
    except (KeyError, IndexError, ValueError) as e:
        # ValueError covers resp.json() on a non-JSON body.
        raise RuntimeError(f"Unexpected DashScope response shape: {e}")


def _parse_llm_response(response_text: str) -> dict | None:
    """
    Parse LLM response text as JSON.

    Strips markdown code fences if present.
    Returns parsed dict or None if unparseable.
    """
    text = response_text.strip()
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return None
    except (json.JSONDecodeError, ValueError):
        logger.warning("intent_parser: LLM response is not valid JSON: %r", text[:200])
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def parse_intent(free_text: str, user_id: str = "guest", nationality: str | None = None, today: str | None = None) -> dict:
    """
    Parse free-text travel intent into a validated trip_request dict.

    Runs ONCE per request. Never inside the re-plan loop.

    Pipeline (§10.11 variance-clamped hybrid):
      0. DETERMINISTIC EXTRACTION: scan text for total_nights, total_budget_cents,
         adults, vibe_sequence BEFORE calling the LLM. These values are the
         authoritative source — they OVERRIDE any LLM-returned numeric fields.
      1. LLM call (qwen3-max via DashScope) → QUALITATIVE structure only:
         ordered legs as {city, vibe}. The LLM MUST NOT set nights/dates.
      2. One retry on parse failure.
      3. VARIANCE CLAMP: validate qualitative fields (city→catalog, vibe→closed set).
         Apply deterministic total_nights + budget + adults on top.
      4. Deterministic fallback if LLM fails — uses _scan_vibe_sequence for
         multi-vibe detection (e.g. "relax then beach" → 2 legs).
      5. Returns validated trip_request OR {"needs_clarification": True, "reason": "..."}.

    Args:
        free_text: Natural language trip description.
        user_id:   User identifier (default "guest").

    Returns:
        trip_request dict (ready for orchestrator.negotiate()) OR
        {"needs_clarification": True, "reason": "..."}
    """
    logger.info("utils.intent_parser.parse_intent: text=%r user_id=%r", free_text[:100], user_id)

    # RC-1: save the ORIGINAL text before any in-place substitution so
    # _scan_origin_city can always scan the user's actual wording.
    _orig_text = free_text

    # ------------------------------------------------------------------
    # Step 0: Deterministic extraction from text (runs FIRST, wins ALWAYS)
    # These values are extracted once here and passed as det_* overrides
    # into _clamp_and_validate so they can never be stomped by LLM output.
    # ------------------------------------------------------------------
    det_total_nights = _scan_nights(free_text)
    if det_total_nights is None or det_total_nights < 1:
        det_total_nights = None  # genuinely absent; LLM gap-fill allowed
        logger.info("intent_parser: total_nights not found in text — LLM may fill")
    else:
        logger.info("intent_parser: total_nights from text: %d", det_total_nights)

    # Budget + currency: distinguish (a) no budget, (b) supported-currency
    # budget normalised to USD cents, (c) unsupported-currency → honest decline.
    # #honesty-fix (GAP 2): _scan_budget_raw also reports `det_budget_provenance` — only
    # a "bare_number" budget (zero currency signal at all) gets flagged as an assumed
    # currency by _clamp_and_validate; a bare "$" stays unflagged (conventional).
    det_budget_cents, budget_err, det_budget_provenance = _scan_budget_raw(free_text)
    if budget_err is UNKNOWN_CURRENCY:
        logger.warning(
            "intent_parser: budget in unsupported currency — honest decline (no collapse)"
        )
        _currency_reason = (
            "cannot_satisfy: budget is in a currency I cannot price. Supported "
            "currencies: USD, AUD, THB, SGD, MYR, IDR, ETB, EUR, GBP. "
            "Please restate the budget in one of these (e.g. 'USD 3000')."
        )
        _elic = _build_elicitation(
            ElicitationSlot.CURRENCY,
            "Your budget is in a currency I cannot price. Which currency is it in?",
            choices=sorted(_KNOWN_CURRENCY_CODES),
            examples=["USD 3000", "AUD 3000"],
            # destination/duration may or may not be present, but the blocking
            # gap here is the currency; report all three core slots as missing
            # since the request hasn't been parsed past this gate.
        )
        return {
            "needs_clarification": True,
            "reason": _currency_reason,
            "elicitation": _elic,
        }
    if det_budget_cents and det_budget_cents > 0:
        logger.info("intent_parser: budget from text (USD-normalised): %d¢", det_budget_cents)
    else:
        det_budget_cents = None

    det_adults = _scan_adults(free_text)
    explicit_adults = _scan_adults_raw(free_text) is not None
    # #party-fix: children were previously scanned only to DISCLOSE the drop
    # (ignored_children). Now carry the count through so hotel selection / day
    # planning see the real party. When children are stated, reconcile the adult
    # count so the two never double-count the same people:
    #   - "family of 4 ... 2 kids"  → the "4" is the TOTAL → adults = 4 - 2 = 2.
    #   - bare "family ... 2 kids"   → no total stated → assume 2 accompanying
    #                                   adults (parents) rather than the silent 1.
    #   - "2 adults and 2 kids"      → adults stated explicitly → left untouched.
    det_children = _scan_children(free_text) or 0
    if det_children:
        _party_total = _scan_party_total(free_text)
        if _party_total is not None and _party_total > det_children:
            det_adults = _party_total - det_children
            explicit_adults = True
        elif not explicit_adults and _FAMILY_RE.search(free_text):
            det_adults = max(det_adults, 2)
            explicit_adults = True
    logger.info(
        "intent_parser: adults from text: %d (explicit=%s) children=%d",
        det_adults, explicit_adults, det_children,
    )

    # #per-person-budget-fix (CRITICAL): "$1000 per person for 2 adults" must become a
    # $2000 TOTAL budget, not $1000 — see _scan_budget_is_per_person's module comment
    # for the bug this closes. MUST run here (after adults/children are known), not
    # up in the budget-scan block above — parsing order in this function is budget
    # BEFORE adults (det_budget_cents at the top, det_adults/det_children just above),
    # so the party size needed for the multiply isn't available until now. Party
    # mirrors the tier-implied-budget convention just below (adults + children,
    # floored at 1) since children are already treated as real travelers to price
    # for elsewhere in this function. Never silent: `_assumed_budget_per_person`
    # carries the stated-per-person figure through to attach_assumption_notes so the
    # multiplication is disclosed, not hidden.
    _assumed_budget_per_person: int | None = None
    if det_budget_cents and _scan_budget_is_per_person(free_text):
        _budget_party = max(1, det_adults + det_children)
        _stated_per_person_cents = det_budget_cents
        det_budget_cents = det_budget_cents * _budget_party
        _assumed_budget_per_person = _stated_per_person_cents
        logger.info(
            "intent_parser: budget %d¢ stated PER-PERSON -> x%d party = %d¢ total",
            _stated_per_person_cents, _budget_party, det_budget_cents,
        )

    # round-2 #budget-tier-plans-fix (CRITICAL): a qualitative budget tier ("mid
    # budget", "budget-friendly", "luxury", "cheap"...) with NO dollar amount must
    # PLAN, not bounce to needs_clarification[budget] — the tier itself IS the
    # budget slot satisfied, just not as a number. Only fires when the text carries
    # NO numeric budget at all (an explicit dollar figure always wins — this never
    # overrides a real stated amount). Derives an implied total from tier x nights x
    # party so the trip proceeds with an honest, disclosed estimate instead of a false
    # decline. `assumed_budget_from_tier` mirrors the assumed_adults/assumed_currency
    # honesty-flag pattern (see attach_assumption_notes) — never a SILENT fabrication.
    _det_budget_tier = _scan_budget_tier(_orig_text)
    _assumed_budget_from_tier: str | None = None
    if det_budget_cents is None and _det_budget_tier:
        _implied_party = max(1, det_adults + det_children)
        _implied_cents = _implied_budget_cents_from_tier(
            _det_budget_tier, det_total_nights, _implied_party
        )
        if _implied_cents:
            det_budget_cents = _implied_cents
            det_budget_provenance = "tier_implied"
            _assumed_budget_from_tier = _det_budget_tier
            logger.info(
                "intent_parser: qualitative budget tier %r with no $ stated -> implied "
                "budget %d¢ (%s night(s) x %d traveller(s)) -- PLANNING, not "
                "declining",
                _det_budget_tier, _implied_cents,
                det_total_nights if det_total_nights else 1, _implied_party,
            )

    # Pre-scan ORDERED city sequence (multi-city front door) and vibe sequence.
    # Region expansion fires BEFORE the country→gateway substitution so a regional request
    # ("central japan") becomes its anchor cities instead of collapsing to one capital.
    _initial_city_scan = _scan_city_sequence(free_text)
    det_city_seq = _maybe_expand_region(free_text, _initial_city_scan)
    # BUG 1 fix: a resolved catalog city can coincidentally share its exact name with a
    # supported COUNTRY ("jamaica" the Queens, NY catalog city vs. Jamaica the Caribbean
    # nation) — prefer the country's real gateway unless the text disambiguates toward
    # the specific catalog place. Runs BEFORE the "no city found" country-fallback below
    # so it fires even though a (coincidentally-named) city WAS found.
    det_city_seq = _disambiguate_country_city_collision(free_text, det_city_seq)
    if not det_city_seq:
        # No city named — but a COUNTRY might be ("a week in the Philippines"). Substitute the
        # country with its primary gateway city IN THE TEXT so every downstream scan (vibe
        # pairing, leg build) sees a real city, then re-scan (#2). Only fires when the scan
        # found NO city, so it never overrides an explicitly named city.
        _aug = _substitute_country_with_city(free_text)
        if _aug != free_text:
            free_text = _aug
            det_city_seq = _scan_city_sequence(free_text)
            logger.info("intent_parser: country→city substitution → %r", free_text[:120])
    # Activity-implied companion city (e.g. Vietnam "cruise" → Ha Long): deterministic expansion of
    # the city sequence BEFORE the topology guard, so the guard rebuilds the extra leg from the
    # deterministic sequence (never from the LLM). var-0-safe; no-op when no (gateway,activity) hit.
    det_city_seq, _companion_vibes = _expand_companion_legs(free_text, det_city_seq, MAX_LEGS)
    # Collapse a district-alias that resolved to a parent city already named elsewhere
    # ("Kyoto and Osaka ... near Gion and Namba" → Gion→kyoto / Namba→osaka would else
    # re-add kyoto/osaka as phantom duplicate legs). Preserves every DISTINCT supported
    # city (multi-city preservation) while removing only exact repeats.
    det_city_seq = _dedupe_cities_preserve_order(det_city_seq)
    det_vibe_seq = _scan_vibe_sequence(free_text)
    # Phase 1: scan activity interest tokens ONCE (deterministic; var-0 sorted output).
    _det_interests = _scan_interests(free_text)
    logger.info(
        "intent_parser: city sequence=%r vibe sequence=%r interests=%r",
        det_city_seq, det_vibe_seq, _det_interests,
    )

    # ------------------------------------------------------------------
    # Honest-decline gate (§16.1): refuse rather than silently collapse.
    # ------------------------------------------------------------------
    # (a) An explicitly-named destination that is NOT in the catalog → decline.
    #     (Checked BEFORE accepting any catalog city we may also have found, so
    #     "Bali then Tokyo" declines on Tokyo rather than collapsing to Bali.)
    #
    # GATE TEXT: two resolution paths can leave the raw phrase in free_text, which
    # would cause false-positives in _scan_unknown_place:
    #
    #   Country path: _substitute_country_with_city rewrites free_text in-place
    #     (e.g. "Northern Vietnam" → "hanoi"), so scanning free_text directly is safe.
    #
    #   Region path (multi-country or sub-national): _maybe_expand_region expands the
    #     city list but does NOT rewrite free_text. A multi-country phrase ("eastern
    #     europe", "scandinavia") or directional phrase ("northern vietnam") stays raw
    #     in free_text and would false-positive the unknown-place scan. Strip the matched
    #     region phrase from free_text ONLY when cities were resolved from an empty
    #     initial scan (the region-expanded path). If the phrase is absent (country path
    #     already rewrote it), scan_region returns None and the strip is skipped.
    #
    # We NEVER blanket-skip the gate: a query like "eastern europe then Gondor" must
    # still trigger an honest decline on "Gondor" even though the first destination
    # resolved cleanly from a region.
    #
    # SAFETY COUPLING: excluded phrases ("north korea", "south china sea") never reach
    # the strip — _maybe_expand_region and _substitute_country_with_city both apply
    # _REGION_EXCLUDE_RE, so det_city_seq stays [] for them and the guard below is False
    # (they decline normally). Keep those two exclusion sites in sync with this gate.
    _gate_text = free_text
    if not _initial_city_scan and det_city_seq:
        _region_phrase = region_expansion.scan_region(free_text)
        if _region_phrase:
            _gate_text = re.sub(re.escape(_region_phrase), " ", free_text, count=1, flags=re.I)
    _unknown_places = _scan_unknown_places(_gate_text)
    # HONESTY GUARD (disambiguation class): a flagged-unknown span can SWALLOW a bare
    # token that independently resolved to a same-named catalog city — "Reading
    # Pennsylvania" is unknown, yet "Reading" resolved to Reading, England. Planning
    # that swallowed slug while reporting "Reading Pennsylvania" as dropped would
    # silently substitute the WRONG city. Strip such mis-resolved slugs from the
    # planned sequence BEFORE the partial-planning decision: if a genuine supported
    # city remains it is planned (and the unknown span honestly dropped); if nothing
    # remains, det_city_seq is now empty so the honest hard-decline below fires. Only
    # slugs with NO legitimate standalone mention are removed (multi-city preserved).
    if _unknown_places:
        _misresolved = _misresolved_city_slugs(_gate_text)
        if _misresolved:
            det_city_seq = [c for c in det_city_seq if c not in _misresolved]
    # GRACEFUL PARTIAL PLANNING (eval fix): an unsupported city/leg must NEVER abort a
    # trip that ALSO contains a supported city. "Tokyo → Takayama → Kyoto" plans
    # Tokyo + Kyoto and tells the user Takayama was dropped; "first time in Africa …
    # Marrakech" plans Marrakech. We ONLY hard-decline the unknown place when NO
    # supported city was resolved at all (nothing to plan) — honesty is preserved by
    # surfacing the dropped leg(s), not by refusing the whole trip.
    _dropped_legs: list[str] = []
    if _unknown_places:
        if det_city_seq:
            _dropped_legs = list(_unknown_places)
            logger.warning(
                "intent_parser: unsupported leg(s) %r dropped-with-notice; planning "
                "supported cities %r (graceful partial planning, no hard decline)",
                _dropped_legs, det_city_seq,
            )
        else:
            unknown_place = _unknown_places[0]
            logger.warning(
                "intent_parser: unknown destination %r and NO supported city — honest "
                "decline (nothing to plan)",
                unknown_place,
            )
            _unknown_reason = (
                f"cannot_satisfy: destination {unknown_place!r} is not in the "
                f"supported catalog. I will not silently substitute a different "
                f"city. {len(ALLOWED_CITIES)} cities are supported across "
                f"{_CATALOG_COUNTRY_COUNT} countries "
                f"(e.g. Bali, Tokyo, Singapore) — "
                f"name a major city; if a city is missing it has no bookable "
                f"inventory yet."
            )
            _elic = _build_elicitation(
                ElicitationSlot.DESTINATION,
                (
                    f"I can't find {unknown_place!r} in the supported catalog. "
                    f"Which destination did you mean?"
                ),
                examples=["Tokyo", "Paris", "Cairo", "Bangkok", "Marrakech", "Bali"],
                has_budget=det_budget_cents is not None,
                has_duration=det_total_nights is not None,
            )
            return {
                "needs_clarification": True,
                "reason": _unknown_reason,
                "elicitation": _elic,
            }

    # (b) More cities than the front door supports → decline (not truncate).
    if len(det_city_seq) > MAX_LEGS:
        logger.warning(
            "intent_parser: %d cities exceeds MAX_LEGS=%d — honest decline",
            len(det_city_seq), MAX_LEGS,
        )
        _too_many_reason = (
            f"cannot_satisfy: {len(det_city_seq)} cities requested but at most "
            f"{MAX_LEGS} legs are supported. Please split into shorter trips."
        )
        _elic = _build_elicitation(
            ElicitationSlot.TOO_MANY_CITIES,
            (
                f"You named {len(det_city_seq)} cities but I can plan at most "
                f"{MAX_LEGS} per trip. Which {MAX_LEGS} would you like to keep?"
            ),
            choices=[c.title() for c in det_city_seq],
            has_budget=det_budget_cents is not None,
            has_duration=det_total_nights is not None,
            has_destination=True,
        )
        return {
            "needs_clarification": True,
            "reason": _too_many_reason,
            "elicitation": _elic,
        }

    # ------------------------------------------------------------------
    # (c-RC5b) Date-inquiry gate: user asks WHEN to travel rather than
    # stating a date.  Fires ONLY when no explicit date is already present
    # in the text (a user who says "March 2027, best time?" already answered
    # their own question).  Placed BEFORE the multi-area gate so date
    # selection is resolved before area preference.
    # var-0: _DATE_INQUIRY_RE + _scan_start_date are both pure fns of text.
    # ------------------------------------------------------------------
    if _DATE_INQUIRY_RE.search(free_text) and not _scan_start_date(free_text, today):
        logger.info("intent_parser: date-inquiry gate fired — returning CLARIFY(DATE)")
        _elic = _build_elicitation(
            ElicitationSlot.DATE,
            "When would you like to travel? Sharing a rough window lets me suggest the best season.",
            examples=["March 2027", "October 2026", "summer 2027"],
            has_destination=bool(det_city_seq),
            has_budget=det_budget_cents is not None,
            has_duration=det_total_nights is not None,
        )
        return {
            "needs_clarification": True,
            "reason": (
                "needs_clarification: You've asked for a date suggestion — "
                "to recommend the best time to visit, please share a travel window "
                "or any constraints "
                "(e.g. 'March 2027', 'avoid rainy season', 'school holidays only')."
            ),
            "elicitation": _elic,
        }

    # ------------------------------------------------------------------
    # (c) Multi-area city, multi-day stay, no preference expressed →
    #     ask the user instead of silently booking one area.
    #
    # Gate fires when ALL of:
    #   • exactly ONE city in the request (multi-city trips already have
    #     a full area per city from the vibe pairing — no guess needed)
    #   • that city has ≥2 areas in AREA_CLOSED_SETS (single-area cities
    #     like bangkok/singapore have only one area — nothing to ask)
    #   • stay is multi-day: det_total_nights is not None and >= 5
    #     (short 1–4 night stays: one base is the obvious booking intent)
    #   • NO vibe/preference expressed: det_vibe_seq is empty
    #     (a stated vibe gives the planner enough signal to pick an area)
    #
    # Lazy-import destination_agent inside the function to avoid import
    # cycles (destination_agent depends on catalog data, not intent_parser).
    # If the import fails, fall through to current behavior — never crash.
    # ------------------------------------------------------------------
    if (
        len(det_city_seq) == 1
        and det_total_nights is not None
        and det_total_nights >= 5
        and not det_vibe_seq
    ):
        _single_city = det_city_seq[0]
        try:
            from agents.destination_agent import AREA_CLOSED_SETS as _ACS, SINGLE_AREA_CITIES as _SAC
            _city_areas = _ACS.get(_single_city)
            _is_single_area = _single_city in _SAC
            if (
                not _is_single_area
                and _city_areas is not None
                and len(_city_areas) >= 2
            ):
                _sorted_areas = sorted(_city_areas)
                _sorted_vibes = sorted(ALLOWED_VIBES)
                _city_display = _single_city.title()
                _example_vibe = "relax then beach then surf"
                _reason = (
                    f"I'd love to plan your {det_total_nights}-night trip to "
                    f"{_city_display}! What vibe(s) are you after?\n"
                    f"(Supported: {', '.join(_sorted_vibes)})\n\n"
                    f"Example: \"{det_total_nights + 1} days in {_city_display}, "
                    f"{_example_vibe}, "
                    f"${det_budget_cents // 100 if det_budget_cents else 'XXXX'}\" "
                    f"— once I know your vibe I'll match you with the right areas and hotels."
                )
                _preference_prompt = {
                    "city": _single_city,
                    "nights": det_total_nights,
                    "areas": _sorted_areas,
                    "vibes": _sorted_vibes,
                    "options": ["one base", "explore areas"],
                }
                logger.info(
                    "intent_parser: multi-area clarification gate fired for city=%r "
                    "nights=%d areas=%s",
                    _single_city, det_total_nights, _sorted_areas,
                )
                _elic = _build_elicitation(
                    ElicitationSlot.AREA,
                    (
                        f"Would you like to stay in ONE base area of {_city_display} "
                        f"or EXPLORE MULTIPLE areas, and what vibe(s) are you after?"
                    ),
                    choices=_sorted_areas,
                    examples=[f"{det_total_nights + 1} days in {_city_display}, {_example_vibe}"],
                    has_destination=True,
                    has_budget=det_budget_cents is not None,
                    has_duration=det_total_nights is not None,
                )
                return {
                    "needs_clarification": True,
                    "reason": _reason,
                    "preference_prompt": _preference_prompt,
                    "elicitation": _elic,
                }
        except Exception as _import_err:  # noqa: BLE001 — defensive; never crash
            # D6 #62: when area metadata cannot load for a single-city multi-day
            # no-vibe request, prefer a conservative needs_clarification (ask the
            # user which area/vibe) rather than silently planning one area.
            # Log at WARNING so the silent downgrade is observable in prod.
            logger.warning(
                "intent_parser: multi-area gate: could not import destination_agent "
                "for city=%r (%s) — asking user for preference (conservative)",
                det_city_seq[0] if det_city_seq else "?", _import_err,
            )
            _city_display = (det_city_seq[0] if det_city_seq else "").title()
            _sorted_vibes = sorted(ALLOWED_VIBES)
            _reason = (
                f"I'd love to plan your {det_total_nights}-night trip to "
                f"{_city_display}! Before I book, could you let me know:\n\n"
                f"1. Which area of {_city_display} would you prefer to stay in?\n\n"
                f"2. What vibe are you after? "
                f"(Supported: {', '.join(_sorted_vibes)})\n\n"
                f"Example: \"{det_total_nights + 1} days in {_city_display}, "
                f"relax, "
                f"${det_budget_cents // 100 if det_budget_cents else 'XXXX'}\" "
                f"— a vibe helps me pick the right area for you."
            )
            _elic = _build_elicitation(
                ElicitationSlot.AREA,
                (
                    f"Which area of {_city_display} would you prefer to stay in, "
                    f"and what vibe are you after?"
                ),
                choices=_sorted_vibes,
                has_destination=True,
                has_budget=det_budget_cents is not None,
                has_duration=det_total_nights is not None,
            )
            return {
                "needs_clarification": True,
                "reason": _reason,
                "elicitation": _elic,
            }

    # ------------------------------------------------------------------
    # Step 1: LLM parsing with one retry (qualitative only)
    # ------------------------------------------------------------------
    raw_dict: dict | None = None
    llm_error: str | None = None

    for attempt in range(2):  # max 1 retry
        try:
            response_text = _llm_call(free_text)
            logger.info(
                "intent_parser: LLM attempt %d response: %r",
                attempt + 1, response_text[:200],
            )
            parsed = _parse_llm_response(response_text)
            if parsed is not None:
                # Check if LLM signalled needs_clarification
                if parsed.get("needs_clarification"):
                    reason = parsed.get("reason", "LLM could not parse intent")
                    logger.info(
                        "intent_parser: LLM returned needs_clarification: %r", reason
                    )
                    # Still try deterministic fallback before giving up
                    raw_dict = None
                    llm_error = reason
                    break
                raw_dict = parsed
                break
            else:
                llm_error = f"LLM attempt {attempt + 1}: invalid JSON"
                logger.warning("intent_parser: %s", llm_error)
        except RuntimeError as e:
            llm_error = str(e)
            logger.warning("intent_parser: LLM call failed (attempt %d): %s", attempt + 1, e)
            if "DASHSCOPE_API_KEY not set" in llm_error:
                # No point retrying if key is missing
                break

    # ------------------------------------------------------------------
    # Step 2: VARIANCE CLAMP on LLM output
    # Inject deterministic overrides so numeric fields ALWAYS come from text.
    # ------------------------------------------------------------------
    validated: dict | None = None

    if raw_dict is not None:
        llm_legs = raw_dict.get("legs", [])

        # ------------------------------------------------------------------
        # MULTI-CITY GUARD (§16.1 fix): if the text names 2+ DISTINCT cities,
        # the text city sequence is authoritative — rebuild legs from it rather
        # than trusting an LLM that may have collapsed them to one city. This is
        # the variance clamp on leg TOPOLOGY (mirrors the numeric clamp).
        # ------------------------------------------------------------------
        if len(det_city_seq) >= 2:
            logger.info(
                "intent_parser: multi-city text (%r) — rebuilding legs from "
                "deterministic city sequence (LLM returned %d leg(s))",
                det_city_seq, len(llm_legs) if isinstance(llm_legs, list) else 0,
            )
            raw_dict = dict(raw_dict)
            raw_dict["legs"] = _pair_cities_with_vibes(
                free_text, det_city_seq, _companion_vibes,
                interests=_det_interests or None,
            )

        # SINGLE-CITY (§10.11 var-0): the leg TOPOLOGY for one catalog city is
        # deterministic from the text, NOT the LLM. The LLM nondeterministically
        # splits/merges legs for a single city — e.g. it coin-flips a "relax" leg
        # out of "...relaxed itinerary", yielding 1 leg on some runs and 2 on
        # others for byte-identical input. That breaks the var-0 thesis. So when
        # the text names exactly ONE catalog city we pin the topology to the
        # deterministic vibe scan, mirroring the regex-fallback single-city rule
        # EXACTLY (so the LLM and fallback paths agree byte-for-byte):
        #   >=1 vibe in text → one leg per vibe, in stated order
        #   0  vibes in text → a single bare leg
        elif len(det_city_seq) == 1:
            city = det_city_seq[0]
            rebuilt = _single_city_legs(
                city, free_text, det_vibe_seq, interests=_det_interests or None
            )
            llm_topo = [
                (l.get("city"), l.get("vibe"))
                for l in (llm_legs if isinstance(llm_legs, list) else [])
                if isinstance(l, dict)
            ]
            if llm_topo != [(l["city"], l.get("vibe")) for l in rebuilt]:
                logger.info(
                    "intent_parser: single-city topology pinned to text vibe seq %r "
                    "(LLM returned %d leg(s)) — var-0 guard",
                    det_vibe_seq, len(llm_topo),
                )
            raw_dict = dict(raw_dict)
            raw_dict["legs"] = rebuilt

        else:
            # TOPOLOGY GUARD (LOW fix / HONESTY): det_city_seq is EMPTY — the
            # deterministic text scan found NO catalog city in the request. This
            # is the conservative authority: if the text itself does not name a
            # catalog city, we MUST NOT trust the LLM's city, which may be
            # hallucinated or extracted from a location the user never stated.
            # A garbage or injection LLM city that happens to match a catalog
            # entry (e.g. "atlantis" → rejected, but a valid-but-wrong city)
            # must never bypass this guard.
            #
            # Action: discard raw_dict so the pipeline falls through to the
            # deterministic fallback (which also finds no city → returns None →
            # needs_clarification / honest decline). NEVER fail open.
            logger.warning(
                "intent_parser: topology guard: det_city_seq=[] but LLM returned "
                "%d leg(s) — refusing LLM city (hallucination guard). "
                "Dropping LLM output; deterministic fallback will produce "
                "needs_clarification.",
                len(llm_legs) if isinstance(llm_legs, list) else 0,
            )
            raw_dict = None

        if raw_dict is not None:
            validated = _clamp_and_validate(
                raw_dict,
                free_text,
                det_total_nights=det_total_nights,
                det_budget_cents=det_budget_cents,
                det_adults=det_adults,
                explicit_adults=explicit_adults,
                today=today,
                budget_provenance=det_budget_provenance,
            )
            if validated is None:
                logger.warning("intent_parser: LLM output failed variance clamp — using fallback")

    # ------------------------------------------------------------------
    # Step 3: Deterministic fallback
    # ------------------------------------------------------------------
    if validated is None:
        logger.info("intent_parser: running deterministic regex fallback")
        fallback_raw = _regex_fallback(free_text)
        if fallback_raw is not None:
            validated = _clamp_and_validate(
                fallback_raw,
                free_text,
                det_total_nights=det_total_nights,
                det_budget_cents=det_budget_cents,
                det_adults=det_adults,
                explicit_adults=explicit_adults,
                today=today,
                budget_provenance=det_budget_provenance,
            )
            if validated is not None:
                logger.info("intent_parser: deterministic fallback succeeded")
            else:
                logger.warning("intent_parser: deterministic fallback also failed clamp")
        else:
            logger.warning("intent_parser: deterministic fallback found insufficient info")

    # ------------------------------------------------------------------
    # Step 4: Return result
    # ------------------------------------------------------------------
    if validated is None:
        # Honest decline. Prefer a clean, user-facing reason that names the
        # actual gap (no recognisable city / no budget) over a raw LLM/transport
        # error string — never collapse or fabricate.
        # D6 #58: never leak llm_error (internal infra strings) to users;
        # never hardcode country count (use _CATALOG_COUNTRY_COUNT);
        # add a dedicated branch for "city found but no nights".
        # #26: alongside the prose ``reason``, emit a structured elicitation slot
        # following the SAME fixed precedence as the if/elif chain
        # (DESTINATION > BUDGET > DURATION). The generic catch-all maps to no
        # slot, so the elicitation key is omitted (HONESTY: never invent a slot
        # when we cannot name the gap). _build_elicitation is pure → var-0.
        _has_dest = bool(det_city_seq)
        _has_budget = det_budget_cents is not None
        _has_duration = det_total_nights is not None
        _elic: dict | None = None
        if not det_city_seq:
            reason = (
                "cannot_satisfy: I could not identify a supported destination in "
                f"your request. {len(ALLOWED_CITIES)} cities across "
                f"{_CATALOG_COUNTRY_COUNT} countries "
                "are supported (e.g. Bali, Tokyo, Singapore) "
                "— please name a major destination city."
            )
            _elic = _build_elicitation(
                ElicitationSlot.DESTINATION,
                "Where would you like to go?",
                examples=["Tokyo", "Paris", "Cairo", "Bangkok", "Marrakech", "Bali"],
                has_destination=False,
                has_budget=_has_budget,
                has_duration=_has_duration,
            )
        elif det_budget_cents is None:
            reason = (
                "cannot_satisfy: I could not find a budget in your request. "
                "Please state a budget (e.g. '$3000' or 'AUD 3000')."
            )
            _elic = _build_elicitation(
                ElicitationSlot.BUDGET,
                "What is your total budget for this trip?",
                examples=["$3000", "AUD 3000"],
                has_destination=_has_dest,
                has_budget=False,
                has_duration=_has_duration,
            )
        elif det_total_nights is None:
            # fix-round-4: distinguish "no duration stated at all" from "a
            # duration WAS stated but exceeds the plannable cap" — the latter
            # was previously given the SAME "I could not find a trip
            # duration" message, which is factually false (a duration was
            # found and understood, then rejected for being implausibly
            # long) and would loop a legitimate long-stay/sabbatical user who
            # re-states the same over-cap number.
            _over_cap_nights = _scan_nights_over_cap(free_text)
            if _over_cap_nights is not None:
                reason = (
                    f"cannot_satisfy: a {_over_cap_nights}-night trip is longer than "
                    f"I can plan for — please state {_MAX_DATE_RANGE_NIGHTS} nights "
                    "or fewer."
                )
                _elic = _build_elicitation(
                    ElicitationSlot.DURATION,
                    f"How many nights (up to {_MAX_DATE_RANGE_NIGHTS}) would you "
                    "like the trip to be?",
                    examples=["7 nights", "a week", "2 weeks"],
                    has_destination=_has_dest,
                    has_budget=_has_budget,
                    has_duration=False,
                )
            else:
                # #honesty-fix (invalid-calendar-date): distinguish "no duration/date
                # stated at all" from "a date range WAS stated but one endpoint names
                # an impossible calendar day" (e.g. "Feb 30 to Mar 4", "April 31") —
                # same fix-round-4 pattern as the over-cap branch above. The generic
                # "I could not find a trip duration" message is misleading here: the
                # user DID state a duration/date range, the actual problem is the
                # calendar date itself, so name that instead.
                _invalid_date = _scan_invalid_calendar_date(free_text)
                if _invalid_date is not None:
                    reason = (
                        f"cannot_satisfy: the date {_invalid_date} doesn't exist — "
                        "can you confirm your travel dates?"
                    )
                    _elic = _build_elicitation(
                        ElicitationSlot.DATE,
                        "What are your actual travel dates?",
                        examples=["April 10 to April 15", "10-15 May"],
                        has_destination=_has_dest,
                        has_budget=_has_budget,
                        has_duration=False,
                    )
                else:
                    # City and budget found but no duration stated — clean user-facing message.
                    reason = (
                        "cannot_satisfy: I could not find a trip duration in your request. "
                        "Please state how many nights (e.g. '7 nights', 'a week', '2 weeks')."
                    )
                    _elic = _build_elicitation(
                        ElicitationSlot.DURATION,
                        "How many nights would you like the trip to be?",
                        examples=["7 nights", "a week", "2 weeks"],
                        has_destination=_has_dest,
                        has_budget=_has_budget,
                        has_duration=False,
                    )
        else:
            # All three signals present but parse still failed — generic clean message.
            # Do NOT surface llm_error (internal infra string, e.g. API key errors).
            # No recognisable slot → no structured elicitation (prose decline only).
            reason = "cannot_satisfy: could not build a valid itinerary from the request — please rephrase."
        logger.warning("intent_parser: returning needs_clarification: %s", reason)
        _decline: dict = {"needs_clarification": True, "reason": reason}
        if _elic is not None:
            _decline["elicitation"] = _elic
        return _decline

    # Inject user_id and optional nationality
    validated["user_id"] = user_id
    if nationality:
        validated["nationality"] = nationality.strip().upper()

    # #budget-tier-fix: qualitative budget descriptor ("mid budget", "shoestring",
    # "luxury") → price tier for lodging selection, so a "mid budget" trip books a
    # mid-tier hotel instead of the most-expensive palace that still fits the total.
    # var-0 safe: deterministic regex over the user's own words (no price/live data).
    # Reuses `_det_budget_tier` (scanned earlier, before the budget-satisfied gate) so
    # the tier is never scanned twice from the same text.
    if _det_budget_tier:
        validated["budget_tier"] = _det_budget_tier
        logger.info("intent_parser: qualitative budget tier: %s", _det_budget_tier)

    # round-2 #budget-tier-plans-fix: honesty flag — the budget was DERIVED from the
    # qualitative tier (no dollar amount stated), never a silent fabrication. Mirrors
    # assumed_adults/assumed_currency; attach_assumption_notes turns this into a
    # user-facing "I estimated your budget..." disclosure.
    if _assumed_budget_from_tier:
        validated["assumed_budget_from_tier"] = _assumed_budget_from_tier

    # #per-person-budget-fix: honesty flag — the budget was MULTIPLIED from a stated
    # per-person figure by party size (never a silent doubling/tripling). Mirrors
    # assumed_budget_from_tier; attach_assumption_notes turns this into a user-facing
    # "budget stated as $X per person for N adults = $Y total" disclosure.
    if _assumed_budget_per_person:
        validated["assumed_budget_per_person"] = _assumed_budget_per_person

    # Graceful partial planning: record any unsupported leg(s) dropped from a trip that
    # still contains supported cities, so attach_assumption_notes can surface an honest
    # "I dropped X" disclosure. Never silent: the user is told which leg was omitted.
    if _dropped_legs:
        validated["dropped_legs"] = list(_dropped_legs)
        _multi = len(_dropped_legs) > 1
        _dropped_str = ", ".join(_dropped_legs)
        _planned_str = ", ".join(
            (l.get("city") or "").title() for l in validated.get("legs", []) if l.get("city")
        )
        validated["dropped_legs_note"] = (
            f"I couldn't find {_dropped_str} in the supported catalog, so I planned the "
            f"supported part of your trip ({_planned_str}) and left "
            f"{'those legs' if _multi else 'that leg'} out. Name a supported city to add "
            f"a leg, or continue with this plan."
        )

    # RC-1: stash origin/home-base city when detected (informational; does NOT
    # affect legs, budget, or dates — pure annotation on the trip_request).
    _origin_city = _scan_origin_city(_orig_text)
    if _origin_city:
        validated["origin"] = _origin_city
        logger.info("intent_parser: origin city stashed: %r", _origin_city)

    logger.info(
        "intent_parser: success — budget=%d¢ legs=%d total_nights=%d user_id=%r",
        validated["total_budget_cents"],
        len(validated["legs"]),
        sum(
            (date.fromisoformat(leg["checkout"]) - date.fromisoformat(leg["checkin"])).days
            for leg in validated["legs"]
        ),
        user_id,
    )
    return validated


def parse_intent_bypass(structured_request: dict) -> dict:
    """
    Back-compatibility shim for callers that already have a structured request.

    Validates and normalises the pre-structured request through the same
    VARIANCE CLAMP as parse_intent, then returns it.

    Args:
        structured_request: A pre-built trip_request dict.

    Returns:
        Validated trip_request dict OR {"needs_clarification": True, "reason": "..."}.
    """
    logger.info("utils.intent_parser.parse_intent_bypass: validating pre-structured request")

    if not isinstance(structured_request, dict):
        return {
            "needs_clarification": True,
            "reason": "structured_request must be a dict",
        }

    # Build a "raw" dict in the same shape the clamp expects.
    # The structured request already has checkin/checkout on legs, so use them directly.
    # We need to handle legs that may already have checkin/checkout.
    user_id = structured_request.get("user_id", "guest")

    raw = {
        "total_budget_cents": structured_request.get("total_budget_cents"),
        "adults": structured_request.get("adults", 1),
        "legs": [],
    }

    for leg in structured_request.get("legs", []):
        raw_leg: dict[str, Any] = {}
        # Pass through both formats; clamp will handle them
        if "city" in leg:
            raw_leg["city"] = leg["city"]
        if "vibe" in leg:
            raw_leg["vibe"] = leg["vibe"]
        if "nights" in leg:
            raw_leg["nights"] = leg["nights"]
        if "checkin" in leg:
            raw_leg["checkin"] = leg["checkin"]
        if "checkout" in leg:
            raw_leg["checkout"] = leg["checkout"]
        # adults per-leg is absorbed by the top-level adults field in clamp
        raw["legs"].append(raw_leg)

    # Use empty string as original text since we have structured input.
    # Pass structured budget as det_budget_cents so the §10.11 guard treats it as
    # a caller-supplied deterministic value (not an LLM number).
    bypass_budget = _parse_budget_cents(structured_request.get("total_budget_cents"))
    validated = _clamp_and_validate(raw, "", det_budget_cents=bypass_budget)
    if validated is None:
        return {
            "needs_clarification": True,
            "reason": "structured_request failed validation (city, budget, or dates invalid)",
        }

    validated["user_id"] = user_id
    return validated


# ---------------------------------------------------------------------------
# Task 3 entry point: parse free text → validated request → orchestrator
# ---------------------------------------------------------------------------


# Comfort/elderly persona cues. NOTE: bare "comfort(able)" is intentionally EXCLUDED — it
# false-positives on hotel-comfort phrasing ("comfortable hotels", the "Comfort Inn" brand). Only
# unambiguous mobility/age cues + explicit comfort-of-TRAVEL phrases trigger the comfort persona.
_COMFORT_CUES = re.compile(
    r"\b(elderly|seniors?|my (?:parents|grandparents|grandma|grandpa|mum|mom|dad|"
    r"grandmother|grandfather)|accessibility|accessible|mobility|wheelchair|"
    r"slow[- ]?paced|relaxed pace|take it easy|no rush|avoid flying|"
    r"prefer (?:the )?train|by train|overland|"
    r"comfortable (?:journey|travel|trip|ride)|comfort over speed|"
    r"priorit(?:y|is\w*|iz\w*) comfort)\b", re.I)


def _scan_persona(text: str) -> str:
    """Traveller persona from free text: "comfort" on an elderly/comfort/avoid-flying cue, else
    "default". Deterministic regex (var-0 for a given input). The transport gate maps "comfort" to a
    wider rail-preference range; everything else is unchanged. Hook for the fuller persona model (#35)."""
    return "comfort" if _COMFORT_CUES.search(text or "") else "default"


def build_estimate_request(
    free_text: str,
    *,
    user_id: str = "guest",
    nationality: str | None = None,
    today: str | None = None,
) -> dict | None:
    """Deterministically build a budget-ESTIMATE trip_request from free text — NO LLM call.

    Reproduces the variance-clamped topology + date logic of parse_intent (the cities,
    vibes, adults and start-date are pure regex scans; the LLM never contributes them),
    using a large SENTINEL budget so no catalog rows are filtered.  When the text states
    no duration, defaults to DEFAULT_ESTIMATE_NIGHTS and flags it via 'assumed_nights'.

    Returns a validated trip_request (with 'legs') ready for
    orchestrator.estimate_budget_range, or None when no catalog city is present.

    PURE/DETERMINISTIC (var-0): regex scans only → same text yields byte-identical legs,
    byte-identical to what parse_intent resolves for the same input.  Fast (no network/LLM).
    """
    # Mirror parse_intent lines 2481-2542 (deterministic scans only).
    det_total_nights = _scan_nights(free_text)
    if det_total_nights is None or det_total_nights < 1:
        det_total_nights = None

    det_adults = _scan_adults(free_text)
    explicit_adults = _scan_adults_raw(free_text) is not None

    det_city_seq = _maybe_expand_region(free_text, _scan_city_sequence(free_text))
    # BUG 1 fix — mirrors parse_intent: resolve a catalog-city / country-name collision
    # (e.g. "jamaica" the Queens, NY catalog city vs. Jamaica the Caribbean nation)
    # toward the country's real gateway before falling through to the no-city path.
    det_city_seq = _disambiguate_country_city_collision(free_text, det_city_seq)
    if not det_city_seq:
        _aug = _substitute_country_with_city(free_text)
        if _aug != free_text:
            free_text = _aug
            det_city_seq = _scan_city_sequence(free_text)
    if not det_city_seq:
        return None  # no catalog city → caller keeps the plain DESTINATION decline

    det_city_seq, _companion_vibes = _expand_companion_legs(free_text, det_city_seq, MAX_LEGS)
    if len(det_city_seq) > MAX_LEGS:
        det_city_seq = det_city_seq[:MAX_LEGS]  # estimate is best-effort: cap, don't decline
    det_vibe_seq = _scan_vibe_sequence(free_text)
    _det_interests = _scan_interests(free_text)

    # Default duration when absent — STATE the assumption (assumed_nights).
    _assumed = det_total_nights is None
    _est_nights = DEFAULT_ESTIMATE_NIGHTS if _assumed else det_total_nights

    # Rebuild leg topology deterministically (same authority as parse_intent's clamp).
    if len(det_city_seq) >= 2:
        _legs_raw = _pair_cities_with_vibes(
            free_text, det_city_seq, _companion_vibes, interests=_det_interests or None,
        )
    else:
        _legs_raw = _single_city_legs(
            det_city_seq[0], free_text, det_vibe_seq, interests=_det_interests or None,
        )

    validated = _clamp_and_validate(
        {"legs": _legs_raw},
        free_text,
        det_total_nights=_est_nights,
        det_budget_cents=_ESTIMATE_SENTINEL_BUDGET_CENTS,
        det_adults=det_adults,
        explicit_adults=explicit_adults,
        today=today,
    )
    if validated is None:
        return None
    validated["user_id"] = user_id
    if nationality:
        validated["nationality"] = nationality.strip().upper()
    if today and not validated.get("today"):
        validated["today"] = today
    validated["assumed_nights"] = _est_nights if _assumed else None
    return validated


# #honesty-fix (GAP 4): the assumed_*/ignored_* honest-note attachment logic used to live
# ONLY inline in negotiate_from_text — so /refine (server.py's conversational edit lane,
# which re-plans via orchestrator.negotiate() directly, NOT via negotiate_from_text) lost
# every disclosure note on a re-plan. Extracted here so BOTH entry points attach the SAME
# notes from the SAME trip_request provenance flags. Pure/additive: reads assumed_*/
# ignored_* off `req` (a validated trip_request — see _clamp_and_validate) and stamps the
# corresponding note fields onto `result` (an orchestrator.negotiate() return dict). No-op
# on a non-dict `result` or `req`. Mutates `result` in place AND returns it (convenience).
def attach_assumption_notes(req: dict, result: dict) -> dict:
    """Attach honest, user-facing assumption/drop notes to a negotiate() result.

    Reads the following provenance flags off `req` (all set by
    ``_clamp_and_validate`` / ``parse_intent``, all None when the corresponding value
    was genuinely explicit in the request):

      assumed_start_date  -> date_assumption_note        (no date given at all)
      assumed_date_year   -> date_year_assumption_note    (year-less date rolled fwd)
      assumed_adults      -> adults_assumption_note       (no party-size signal at all)
      assumed_currency    -> currency_assumption_note     (bare-number budget, no ccy cue)
      ignored_children    -> children_note                (children stated but not priced)
      assumed_budget_from_tier -> budget_tier_assumption_note (qualitative tier, no $ stated)
      assumed_budget_per_person -> budget_per_person_assumption_note (per-person $ x party)

    Each note, when its flag fires, is ALSO prepended to `result["reason"]` when that key
    is already a non-empty string (mirrors the pre-extraction behaviour exactly).
    """
    if not isinstance(result, dict) or not isinstance(req, dict):
        return result

    # Graceful partial planning: an unsupported leg was dropped from a trip that still
    # contained supported cities. Surface the honest "I dropped X" disclosure on the
    # result (and prepend to any existing reason) — never a silent omission.
    _dropped_legs = req.get("dropped_legs")
    if _dropped_legs:
        result["dropped_legs"] = list(_dropped_legs)
        _dropped_note = req.get("dropped_legs_note") or (
            f"I couldn't find {', '.join(_dropped_legs)} in the supported catalog, so "
            f"they were left out of this plan."
        )
        result["dropped_legs_note"] = _dropped_note
        if isinstance(result.get("reason"), str) and result["reason"]:
            result["reason"] = _dropped_note + " " + result["reason"]

    _assumed_start = req.get("assumed_start_date")
    if _assumed_start:
        # fix-round-3: a stated SEASON/HOLIDAY ("this summer", "around
        # Christmas") is timing the user DID give — the flat "no travel dates
        # were given" note contradicted them. Word the note honestly when
        # that hint is present; otherwise fall back to the original wording.
        _season_hint = req.get("assumed_start_date_season_hint")
        if _season_hint:
            _date_note = (
                f"You mentioned {_season_hint}, but I need exact travel dates to "
                f"precisely compute season/weather-based advice and the insurance "
                f"premium — so I assumed a start date of {_assumed_start} for now. "
                f"Tell me your real travel dates for accurate figures."
            )
        else:
            _date_note = (
                f"No travel dates were given, so I assumed a start date of "
                f"{_assumed_start} to compute season/weather-based advice and the "
                f"insurance premium. Tell me your real travel dates for accurate figures."
            )
        result["assumed_start_date"] = _assumed_start
        result["date_assumption_note"] = _date_note
        if isinstance(result.get("reason"), str) and result["reason"]:
            result["reason"] = _date_note + " " + result["reason"]

    # #honesty-fix (GAP 1): a YEAR-LESS date that would have landed in the past was
    # rolled forward a year. Distinct from assumed_start_date above — the user DID state
    # a real date, just not a year. Pull the actually-used (rolled) date from leg 0's
    # checkin, since that's always present on a validated trip_request.
    if req.get("assumed_date_year"):
        _rolled_date = ((req.get("legs") or [{}])[0] or {}).get("checkin", "")
        _year_note = (
            f"Your start date didn't include a year, and taken at face value it would "
            f"already be in the past — so I assumed {_rolled_date} (the next occurrence). "
            f"Tell me the real year for accurate figures."
        )
        result["assumed_date_year"] = True
        result["date_year_assumption_note"] = _year_note
        if isinstance(result.get("reason"), str) and result["reason"]:
            result["reason"] = _year_note + " " + result["reason"]

    _assumed_pax = req.get("assumed_adults")
    if _assumed_pax:
        _adults_note = (
            f"No traveler count was given, so I assumed a solo traveler (1 adult) to "
            f"price lodging, insurance, and fees. Tell me the real number of travelers "
            f"for accurate figures."
        )
        result["assumed_adults"] = _assumed_pax
        result["adults_assumption_note"] = _adults_note
        if isinstance(result.get("reason"), str) and result["reason"]:
            result["reason"] = _adults_note + " " + result["reason"]

    # #honesty-fix (GAP 2): a bare-number budget with zero currency signal silently
    # picked USD.
    _assumed_currency = req.get("assumed_currency")
    if _assumed_currency:
        _currency_note = (
            f"No currency was stated for your budget, so I assumed {_assumed_currency}. "
            f"Tell me the real currency (e.g. 'AUD 3000') for accurate figures."
        )
        result["assumed_currency"] = _assumed_currency
        result["currency_assumption_note"] = _currency_note
        if isinstance(result.get("reason"), str) and result["reason"]:
            result["reason"] = _currency_note + " " + result["reason"]

    # round-2 #budget-tier-plans-fix: a qualitative budget tier with no dollar amount
    # produced an ESTIMATED total budget (tier x nights x party) so the trip could plan
    # instead of declining. Disclose the estimate honestly — never silently presented
    # as if the user had stated a real number.
    _assumed_tier = req.get("assumed_budget_from_tier")
    if _assumed_tier:
        _est_cents = req.get("total_budget_cents")
        _est_usd = f"${_est_cents // 100:,}" if isinstance(_est_cents, int) else "an estimate"
        _tier_note = (
            f"You said '{_assumed_tier}' budget without a dollar figure, so I estimated "
            f"{_est_usd} total based on that style, your trip length, and party size. "
            f"Tell me a real number (e.g. '$3000') for accurate figures."
        )
        result["assumed_budget_from_tier"] = _assumed_tier
        result["budget_tier_assumption_note"] = _tier_note
        if isinstance(result.get("reason"), str) and result["reason"]:
            result["reason"] = _tier_note + " " + result["reason"]

    # #per-person-budget-fix: the stated budget was a PER-PERSON figure ("$1000 per
    # person"), multiplied by party size to get the real trip total — disclose the
    # arithmetic explicitly so the inference is visible, never silent.
    _assumed_per_person_cents = req.get("assumed_budget_per_person")
    if _assumed_per_person_cents:
        _total_cents = req.get("total_budget_cents")
        _party_n = req.get("adults", 1) + (req.get("children") or 0)
        _per_person_usd = f"${_assumed_per_person_cents // 100:,}"
        _total_usd = (
            f"${_total_cents // 100:,}" if isinstance(_total_cents, int) else "an estimate"
        )
        _per_person_note = (
            f"Your budget was stated as {_per_person_usd} per person for {_party_n} "
            f"traveler(s), so I used {_total_usd} total. Tell me a real total figure "
            f"if you meant something different."
        )
        result["assumed_budget_per_person"] = _assumed_per_person_cents
        result["budget_per_person_assumption_note"] = _per_person_note
        if isinstance(result.get("reason"), str) and result["reason"]:
            result["reason"] = _per_person_note + " " + result["reason"]

    # #honesty-fix (GAP 3): explicitly-stated children were dropped from party pricing.
    _ignored_children = req.get("ignored_children")
    if _ignored_children:
        # fix-round-3: an unquantified PLURAL mention ("the kids are coming")
        # means "at least two" — asserting the internal conservative "1"
        # count contradicts the user's own plural wording, undercutting the
        # very honesty this note exists to provide.
        if req.get("ignored_children_is_plural_estimate"):
            _children_note = (
                f"I priced lodging, insurance, and fees for {req.get('adults')} "
                f"adult(s) only — you mentioned children (plural) in your request, "
                f"but not how many, so they aren't priced separately yet; expect "
                f"higher real costs."
            )
        else:
            _children_note = (
                f"I priced lodging, insurance, and fees for {req.get('adults')} adult(s) "
                f"only — {_ignored_children} child(ren) mentioned in your request aren't "
                f"priced separately yet, so expect higher real costs."
            )
        result["ignored_children"] = _ignored_children
        result["children_note"] = _children_note
        if isinstance(result.get("reason"), str) and result["reason"]:
            result["reason"] = _children_note + " " + result["reason"]

    # fix-round-1: a comma/"and"-joined multi-vibe single-city request
    # ("relax and culture") collapses to one leg (_single_city_legs), and the
    # un-kept vibe(s) are surfaced via req["dropped_vibes"] (_clamp_and_validate)
    # — honestly disclose them here rather than let them silently vanish.
    _all_dropped_vibes = req.get("dropped_vibes")
    if _all_dropped_vibes:
        _kept_vibe = ((req.get("legs") or [{}])[0] or {}).get("vibe")
        _vibes_note = (
            f"I planned this trip around \"{_kept_vibe}\" only — your other stated "
            f"interest(s) ({', '.join(_all_dropped_vibes)}) weren't planned for as a "
            f"separate stay. Say \"then\" between vibes (e.g. \"relax then culture\") "
            f"if you want a stay for each."
        )
        result["dropped_vibes"] = list(_all_dropped_vibes)
        result["dropped_vibes_note"] = _vibes_note
        if isinstance(result.get("reason"), str) and result["reason"]:
            result["reason"] = _vibes_note + " " + result["reason"]

    return result


def negotiate_from_text(
    free_text: str,
    orchestrator: "Any",
    user_id: str = "guest",
    nationality: str | None = None,
    today: str | None = None,
    narrate: bool = False,
    wallet_balance_cents: int | None = None,
    live_emergency: dict | None = None,
    home_currency: str | None = None,
    interests: list | None = None,
    dietary: str | None = None,
    pace: str | None = None,
    avoid_lodging_types: list | None = None,
    prefer_lodging_types: list | None = None,
    dining_tier: str | None = None,
    commit: bool = True,
    merchant_user_id: str | None = None,
    memory_verified_user_id: str | None = None,
    real_user_id: str | None = None,
) -> dict:
    """
    Parse free text → validated request → orchestrator.negotiate().

    `commit` (#1 consent split): default True = atomic plan+book (byte-identical).
    False = PLAN-ONLY (held plan_ready envelope; the client confirms via /confirm).

    One-shot entry point. Never called inside the re-plan loop.

    Args:
        free_text:    Natural language trip description.
        orchestrator: TravelOrchestrator instance.
        user_id:      User identifier (default "guest").
        nationality:  Optional traveler nationality (ISO-3166 alpha-2). When
                      supplied, it is forwarded to the trip_request so the
                      compliance gate can engage even on free-text trips.
        today:        Optional ISO date captured at the I/O boundary; forwarded
                      as the single frozen run-date (D7) when the parsed request
                      doesn't already carry one. Keeps the gates off wall-clock.

    Returns:
        orchestrator.negotiate() result OR {"needs_clarification": True, "reason": "..."}.
    """
    req = parse_intent(free_text, user_id=user_id, nationality=nationality, today=today)
    if req.get("needs_clarification"):
        # Budget-guidance enrichment (Part A, ADDITIVE ONLY):
        # When the BUDGET slot is the missing gap AND city+duration are resolved,
        # call orchestrator.estimate_budget_range to surface an indicative range.
        # needs_clarification + elicitation remain UNTOUCHED; the range is appended
        # to reason and attached as budget_estimate. parse_intent's verbatim reason
        # prose is preserved byte-for-byte (test_reason_preserved_as_superset passes).
        elic = req.get("elicitation") or {}
        if (
            elic.get("slot") == ElicitationSlot.BUDGET
            and "destination" in (elic.get("satisfied_slots") or [])
        ):
            try:
                # Build the estimate request DETERMINISTICALLY — no 2nd LLM parse.
                # Fires for dest-only queries too (duration is optional; defaults to
                # DEFAULT_ESTIMATE_NIGHTS when absent and states the assumption).
                _req_for_est = build_estimate_request(
                    free_text, user_id=user_id, nationality=nationality, today=today,
                )
                if _req_for_est is not None and _req_for_est.get("legs"):
                    est = orchestrator.estimate_budget_range(_req_for_est)
                    if est is not None:
                        req["budget_estimate"] = est
                        # REFRAME: lead with the estimate (helpful guidance), keep
                        # the BUDGET elicitation so the user can still state a budget
                        # to proceed.  We do NOT auto-pick a budget (human-in-the-loop).
                        _assumed = _req_for_est.get("assumed_nights")
                        _assume_note = (
                            f"Assuming about {_assumed} nights (you didn't state a "
                            f"duration). "
                            if _assumed else ""
                        )
                        # #honesty-fix (silent-default-provenance): same treatment for
                        # party size — the estimate's "for {pax}" figure silently used
                        # the 1-adult fallback when no count was stated, which can skew
                        # the guidance range (rooms/insurance scale with pax).
                        _assumed_pax = _req_for_est.get("assumed_adults")
                        _assume_note += (
                            f"Assuming a solo traveler (you didn't say how many people "
                            f"are going). "
                            if _assumed_pax else ""
                        )
                        req["reason"] = (
                            _assume_note + est["message"] + " "
                            "Tell me your budget to plan the trip (e.g. '$3000' or "
                            "'AUD 3000'), or add dates/duration to refine this estimate."
                        )
            except Exception:  # noqa: BLE001
                # estimate enrichment is best-effort; never crash the clarification
                pass
        return req
    # Freeze the run date (D7): forward the boundary-captured `today` so the
    # health/compliance/risk gates use ONE frozen value instead of clocking off
    # wall-time deep in the determinism-critical path. A req["today"] from an
    # upstream structured caller always wins.
    if today and not req.get("today"):
        req["today"] = today
    # SIMULATED prepaid wallet — thread the (already-defaulted) balance into the
    # parsed request so the orchestrator seeds the per-run wallet from it. Only set
    # when supplied + absent on req (a structured upstream value always wins).
    if wallet_balance_cents is not None and req.get("wallet_balance_cents") is None:
        req["wallet_balance_cents"] = wallet_balance_cents
    if narrate:
        req["narrate"] = True  # #3 opt-in cosmetic itinerary narrative (firewalled, off the var-0 path)
    # #51 — OPT-IN LIVE active-emergency overlay. When supplied (board sets it only
    # when an EMERGENCY_FEED is configured), thread it into the request so the
    # orchestrator checks the live feed per leg. Absent → no key → var-0 no-op. A
    # structured upstream value always wins.
    if live_emergency is not None and req.get("live_emergency") is None:
        req["live_emergency"] = live_emergency
    # Slice 4 — demo-user profile prefs threaded as EXPLICIT request fields. var-0-safe:
    # home_currency is display-only (NOT in _request_digest); the persona-preset
    # interests/dietary/pace pre-fill EMPTY per-leg selections ONLY — a parsed/explicit
    # value always wins. Anonymous callers pass None → byte-identical to today.
    if home_currency and not req.get("home_currency"):
        req["home_currency"] = home_currency
    for _leg in (req.get("legs") or []):
        if not isinstance(_leg, dict):
            continue
        if interests and not _leg.get("interests"):
            _leg["interests"] = list(interests)
        if dietary and not _leg.get("dietary"):
            _leg["dietary"] = dietary
        if pace and not _leg.get("pace"):
            _leg["pace"] = pace
        if avoid_lodging_types and not _leg.get("avoid_lodging_types"):
            _leg["avoid_lodging_types"] = list(avoid_lodging_types)
        if prefer_lodging_types and not _leg.get("prefer_lodging_types"):
            _leg["prefer_lodging_types"] = list(prefer_lodging_types)
        if dining_tier and not _leg.get("dining_tier"):
            _leg["dining_tier"] = dining_tier
    # Phase 3b — traveller PERSONA from the free text (e.g. "trip for my elderly parents",
    # "prefer the train"). "comfort" makes the transport gate widen its rail-preference range
    # (rail/ferry over flying). Default "default" → byte-identical behaviour. A structured upstream
    # persona always wins. Deterministic (regex on the text) → var-0 for a given input.
    if "persona" not in req:
        req["persona"] = _scan_persona(free_text)
    # #161 — canonical Go-merchant checkout owner, computed at the server boundary
    # (utils.ucp_signing.merchant_checkout_owner) from the RAW request BEFORE any
    # anon uuid4 stamp. Rides along on req like wallet_balance_cents/live_emergency
    # — off _request_digest, so var-0 is untouched (see orchestrator.negotiate()).
    if merchant_user_id:
        req["merchant_user_id"] = merchant_user_id
    # M1 follow-up (security review) — see server.py's _memory_verified_user_id
    # docstring. Threaded ONLY when the caller (server.py) explicitly computed
    # it; `None` (direct/test callers that never pass this kwarg) leaves req
    # without the key so the orchestrator falls back to its pre-existing
    # merchant_user_id-based behavior, byte-identical to before this follow-up.
    if memory_verified_user_id is not None:
        req["memory_verified_user_id"] = memory_verified_user_id
    # C1 fix — the AUTHORITATIVE trip-row owner (server.py's raw pre-uuid4-stamp
    # capture). Threaded ONLY when the caller (server.py) explicitly computed it;
    # `None` (direct/test callers that never pass this kwarg) leaves req without
    # the key so orchestrator.negotiate() falls back to req["user_id"] instead
    # (byte-identical to before this fix for those callers).
    if real_user_id is not None:
        req["real_user_id"] = real_user_id
    result = orchestrator.negotiate(req, commit=commit)
    # #honesty-fix (GAP 4): attach every assumed_*/ignored_* honesty note present on `req`
    # to `result` — shared with server.py's /refine conversational edit lane so a
    # re-plan built on assumed/dropped data doesn't silently lose the disclosure.
    attach_assumption_notes(req, result)
    # B1: stamp the validated structured request onto the result so the server can persist it
    # (side-channel, stripped by _persist_and_sanitize_plan before reaching the client).
    if isinstance(result, dict):
        result["_trip_request"] = req
    return result
