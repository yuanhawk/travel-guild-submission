"""
destination_agent.py — Destination / Local-expert A2A agent (M-Agentic-2).

Design contract: the internal design spec §3.3 (Destination/Local-expert),
§10.11 (variance-clamped hybrid), §10.6 rows 9/10.

Agent Card skill: ``destination.assess``

This is the SECOND clamped LLM edge of the Travel Guild (after M-Agentic-1
intent_parser).  It maps a trip leg's qualitative (city, vibe) to a ranked list
of REAL catalog AREAS for that city.  It mirrors the M-Agentic-1 clamp pattern
exactly:

    LLM call  →  closed-set validate (VARIANCE CLAMP)  →  vibe-strict intersection
                 → deterministic fallback

Variance discipline (§10.11):
  - The LLM contributes ONLY qualitative *ranking* within the strict
    vibe-appropriate closed set (BALI_VIBE_FALLBACK[vibe]).  It cannot invent
    an area, a city, a price, or a hotel, and it cannot add loosely-related
    areas that are outside the vibe-appropriate set.
  - Pipeline: (1) city-level catalog clamp drops invented areas; (2) vibe-strict
    intersection further restricts to BALI_VIBE_FALLBACK[vibe] — the LLM may
    only reorder within that set, never freelance outside it.
  - If the intersection is empty/invalid/unavailable, a DETERMINISTIC,
    vibe-keyed fallback area list is used — so the worst case equals today's
    deterministic behaviour (variance floor = current).
  - Runs ONE-SHOT per leg, NEVER inside the orchestrator's iterative re-plan
    (money) loop.
  - Invariant: returned areas ⊆ strict_map[vibe]  (BALI_VIBE_FALLBACK[vibe])
    for every known vibe.

Input (data part, JSON):
    {
        "city": str,            # one of the catalog cities (lowercase)
        "vibe": str | null      # optional qualitative vibe (closed set)
    }

Output artifact (data part, JSON) — typed AreaAssessment:
    {
        "type":       "area_assessment",
        "city":       str,
        "vibe":       str | null,
        "areas":      list[str],   # ranked, real-catalog areas (best first), non-empty
        "source":     "llm" | "fallback" | "city_single",
        "provenance": "catalog"
    }

Security:
  - DASHSCOPE_API_KEY read from env — NEVER hardcoded.
  - gitleaks-safe: no raw API keys or secret patterns in code.
  - LLM output NEVER passed downstream unvalidated (closed-set clamp).
  - Prompt injection / garbage → deterministic fallback (no crash, no invented area).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
import uvicorn

from agents.a2a_agent import (
    A2AAgent,
    _data_part,
    _new_artifact,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — from env, never hardcoded
# ---------------------------------------------------------------------------

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
MODEL = os.environ.get("SOCIETY_LLM_MODEL", "qwen3-max")  # cheap model for test sweeps

# ---------------------------------------------------------------------------
# Catalog area closed sets — single source of truth for the VARIANCE CLAMP.
#
# These MUST match the AREAS encoded in ucp-merchant/catalog.go hotel IDs
# (scheme: "<city>-<area>-<name>").  See parse_area_from_hotel_id() and
# accommodation_agent.AREA_CLOSED_SETS, kept in sync via test_destination_agent.
# ---------------------------------------------------------------------------

# Bali real catalog areas (from catalog.go hotel-id prefixes):
#   bali-ubud-*, bali-seminyak-*, bali-kuta-*, bali-legian-*, bali-sanur-*,
#   bali-nusadua-*, bali-jimbaran-*, bali-canggu-* (alaya=ubud, alila=seminyak,
#   como=canggu).
# Kept as frozenset for importers (D9 contract: AREA_CLOSED_SETS / SINGLE_AREA_CITIES
# importable with same shape). Internal iteration uses BALI_AREAS_ORDERED (sorted tuple)
# for var-0 determinism (D6).
BALI_AREAS: frozenset[str] = frozenset({
    "ubud",
    "seminyak",
    "kuta",
    "legian",
    "sanur",
    "nusadua",
    "jimbaran",
    "canggu",
})

# Sorted tuple for deterministic iteration (var-0, D6). Never iterate BALI_AREAS directly
# when the iteration order affects the returned value.
BALI_AREAS_ORDERED: tuple[str, ...] = tuple(sorted(BALI_AREAS))

# Western Australia road-trip towns (seeded from the user's real 2022 WA loop;
# see catalog.go provenance block + the internal design spec §13). Each town
# is a SINGLE-AREA city: town == city == area. Catalog ids follow
# "<town>-<town>-<name>" so parse_area_from_hotel_id resolves the town as its
# own area. Order mirrors the real Perth-to-Perth loop.
WA_TOWNS: tuple[str, ...] = (
    "perth", "busselton", "margaret-river", "pemberton", "albany",
    "ravensthorpe", "esperance", "kalgoorlie", "northam",
)

# Single-area cities — the city name IS the only area; no sub-area decomposition.
SINGLE_AREA_CITIES: dict[str, str] = {
    "bangkok": "bangkok",
    "singapore": "singapore",
    "kuala lumpur": "kuala lumpur",
    # WA towns: town is its own (only) area.
    **{town: town for town in WA_TOWNS},
}

# city → closed set of real catalog areas
AREA_CLOSED_SETS: dict[str, frozenset[str]] = {
    "bali": BALI_AREAS,
    "bangkok": frozenset({"bangkok"}),
    "singapore": frozenset({"singapore"}),
    "kuala lumpur": frozenset({"kuala lumpur"}),
    # Each WA town: single-area closed set = {town}.
    **{town: frozenset({town}) for town in WA_TOWNS},
}

# ---------------------------------------------------------------------------
# DETERMINISTIC FALLBACK — vibe → ranked Bali area list.
#
# Used verbatim when the LLM is unavailable / returns nothing valid.  Ranking
# order is the deterministic preference within the vibe.  Every entry is a real
# catalog area (subset of BALI_AREAS).  This is the variance FLOOR.
# ---------------------------------------------------------------------------

BALI_VIBE_FALLBACK: dict[str, list[str]] = {
    "beach":   ["seminyak", "legian", "kuta", "sanur", "nusadua", "jimbaran"],
    "culture": ["ubud"],
    "surf":    ["canggu"],
    "relax":   ["ubud", "sanur"],
    "luxury":  ["nusadua", "jimbaran"],
    # vibes without a bespoke mapping below fall back to the broad set
}

# Broad, vibe-appropriate-agnostic Bali set for unknown/None vibe — ranked by a
# stable, deterministic default preference. Used as the last deterministic step.
BALI_DEFAULT_RANKED: list[str] = [
    "seminyak", "ubud", "sanur", "legian", "kuta", "nusadua", "jimbaran", "canggu",
]

# Broadening map (§re-plan fallback, deterministic, NO LLM): when the narrow
# vibe area set yields no hotel within budget, widen to a still-vibe-appropriate
# superset before going city-wide.  Beach-family and relax-family widen to all
# coastal/quiet areas; culture/surf stay narrow then go city-wide.
BALI_VIBE_BROAD: dict[str, list[str]] = {
    "beach":   ["seminyak", "legian", "kuta", "sanur", "nusadua", "jimbaran", "canggu"],
    "relax":   ["ubud", "sanur", "nusadua", "jimbaran"],
    "luxury":  ["nusadua", "jimbaran", "seminyak", "ubud"],
    "culture": ["ubud", "sanur"],
    "surf":    ["canggu", "kuta", "legian"],
}

# ---------------------------------------------------------------------------
# Seasonal advisory — seeded, deterministic, provenance-grounded (§12.1, §12.2).
#
# A seasonal advisory is an exogenous "frequency-as-planning-data" signal the
# Destination agent surfaces when a leg's dates fall in a known high-risk
# window. It NEVER blocks booking (advisory-grade, like the transport buffer);
# the Critic/Planner may use it. Data is SEEDED + provenance-tagged (§13) — for
# WA: the real southern-WA bushfire season (high fire-danger Dec–Feb, frequent
# 40°C+ days), seeded from the user's real 2022 WA trip context. Live advisory
# feeds plug into the same Tier-2 LayeredProvider seam.
# ---------------------------------------------------------------------------

# (city_slug, country_lower) → list of {start_month, end_month, advisory dict}.
# Month window is inclusive and may wrap the year boundary (e.g. Dec→Feb).
# Keyed by (city, country) to avoid misattributing a WA advisory to globally-
# ambiguous town names (albany/perth/pemberton also exist in other countries).
# D6 / #57: key carries country so expansion to non-Australia cities is safe.
SEASONAL_ADVISORIES: dict[tuple[str, str], list[dict[str, Any]]] = {
    (town, "australia"): [
        {
            "start_month": 12,
            "end_month": 2,
            "flag": "bushfire_season",
            "severity": "high",
            "detail": (
                "Southern WA bushfire season (Dec–Feb): frequent 40°C+ days and "
                "elevated fire danger. Check DFES/Emergency WA alerts; carry water "
                "and a contingency for road/park closures."
            ),
            "provenance": "seeded:user-2022-wa-trip",
        }
    ]
    for town in WA_TOWNS
}

# City slug → default country-lower derived from SEASONAL_ADVISORIES keys.
# Used by assess() to fire seasonal advisories even when the caller (e.g. the
# orchestrator's _call_destination) does not pass an explicit country.  Building
# the map from SEASONAL_ADVISORIES keys guarantees zero false-positives: only
# cities that already have a seeded advisory entry are in the map; every other
# city stays advisory=None regardless.  Deterministic at module load (no I/O).
_CITY_COUNTRY_DEFAULTS: dict[str, str] = {
    city_slug: country_lower
    for (city_slug, country_lower) in SEASONAL_ADVISORIES
}


def _month_in_window(month: int, start_month: int, end_month: int) -> bool:
    """True if `month` falls in [start_month, end_month] inclusive, year-wrapping."""
    if start_month <= end_month:
        return start_month <= month <= end_month
    # Wrapping window (e.g. Dec(12)→Feb(2)): month >= start OR month <= end.
    return month >= start_month or month <= end_month


def seasonal_advisory(
    city: str,
    checkin: str | None,
    checkout: str | None,
    country: str | None = None,
) -> dict | None:
    """
    Return a seeded seasonal advisory for (city, country, date-window), or None.

    Deterministic: same city + country + dates → same advisory (variance-0).
    The advisory fires when EITHER the checkin or checkout month falls in a seeded
    high-risk window for the city+country pair. Dates are "YYYY-MM-DD";
    unparseable dates → no advisory.

    country is matched case-insensitively. When country is None/empty, the lookup
    is skipped (no advisory) to avoid misattributing a region-specific hazard to a
    globally-ambiguous city name (#57).
    """
    c = city.strip().lower()
    country_key = country.strip().lower() if country and isinstance(country, str) else None
    if not country_key:
        # Without a country we cannot safely attribute a region-specific advisory.
        return None
    windows = SEASONAL_ADVISORIES.get((c, country_key))
    if not windows:
        return None

    months: list[int] = []
    for d in (checkin, checkout):
        if isinstance(d, str) and len(d) >= 7:
            try:
                months.append(int(d[5:7]))
            except ValueError:
                continue
    if not months:
        return None

    for w in windows:
        if any(_month_in_window(m, w["start_month"], w["end_month"]) for m in months):
            return {
                "type": "seasonal_advisory",
                "city": c,
                "flag": w["flag"],
                "severity": w["severity"],
                "detail": w["detail"],
                "provenance": w["provenance"],
                "window": f"month {w['start_month']}–{w['end_month']}",
            }
    return None


def parse_area_from_hotel_id(
    hotel_id: str,
    city: str | None = None,
    catalog_area: str | None = None,
) -> str | None:
    """
    Parse the catalog AREA from a hotel id of the form "<city>-<area>-<name>".

    Examples (catalog.go):
        "bali-seminyak-oberoibali"  → "seminyak"
        "bali-nusadua-mulia"        → "nusadua"
        "bali-alaya-ubud"           → (alias) "ubud"   (city-suffix alias)
        "bangkok-metro"             → "bangkok"  (single-area city)
        "kl-bukit-bintang"          → "kuala lumpur"

    Strategy (D6 var-0 + #20 catalog-area-field-first):
      0. If catalog_area is supplied (the row's explicit "area" field), it is the
         SOURCE OF TRUTH — id-parsing (steps 1-4) is skipped entirely.  C3: for
         Bali hotel ids, the catalog area is also normalised to the closed-set token
         (spaces/underscores collapsed) so "Nusa Dua" → "nusadua", matching
         BALI_AREAS and the target_areas list from destination.assess.  Non-Bali
         catalog areas are returned stripped+lowercased only (no space collapse) so
         "A Coruña" stays "a coruña".
      1. Bali sub-area closed-set match — ONLY for ids whose first segment is "bali"
         (fixes #34: avoids misattributing a Bali area token to a non-Bali city).
         Iteration is over BALI_AREAS_ORDERED (sorted tuple) and match is picked by
         the FIRST matching segment position in the id — deterministic even when
         multiple area tokens appear (D6 var-0).
      2. Single-area cities: any id prefixed with the city / its alias.
      3. WA road-trip towns: longest-prefix match (already deterministic).
      4. If a city hint is given and it is single-area, use it.
      5. None.

    The closed-set match (step 1) only accepts tokens that are real catalog areas
    (variance clamp) and is now scoped to bali- ids (#34).
    """
    if not hotel_id or not isinstance(hotel_id, str):
        return None
    hid = hotel_id.strip().lower()

    # 0. Catalog's explicit "area" field is the SOURCE OF TRUTH (C3 / #20).
    #    Id-parsing (steps 1-4) is completely skipped when catalog_area is present.
    #    Normalisation:
    #      - Always: strip + lowercase.
    #      - Bali ids ONLY: also collapse internal spaces/underscores to match the
    #        BALI_AREAS token format ("Nusa Dua" → "nusadua", "nusa_dua" →
    #        "nusadua").  This ensures catalog rows with area="Nusa Dua" are
    #        correctly matched against target_areas=["nusadua"] from assess().
    #      - Non-Bali ids: space-collapse is NOT applied, so city/area names with
    #        legitimate spaces (e.g. "A Coruña") are preserved after lowercasing.
    if catalog_area and isinstance(catalog_area, str):
        raw_lower = catalog_area.strip().lower()
        if not raw_lower:
            pass  # empty after strip → fall through to id-based parsing
        elif hid.startswith("bali-") or hid.startswith("bali_"):
            # Bali: collapse spaces/underscores → matches BALI_AREAS closed-set tokens.
            return re.sub(r"[\s_]+", "", raw_lower) or None
        else:
            return raw_lower

    segments = re.split(r"[-_]", hid)

    # 1. Bali sub-area closed-set token match — scoped to bali- ids only (#34).
    #    Pick by FIRST matching segment position to be deterministic (#19 D6).
    if hid.startswith("bali-") or hid.startswith("bali_"):
        for seg in segments:          # iterate segments in id order (deterministic)
            if seg in BALI_AREAS:     # BALI_AREAS frozenset membership test is fine here
                return seg            # return first matching segment by position

    # 1b. Legacy alias: "bali-alaya-ubud" — "ubud" is last segment, already covered
    #     above since we iterate segments in order (ubud IS in BALI_AREAS).

    # 2. Single-area cities: any id prefixed with the city / its alias.
    #    "kl-*" alias → kuala lumpur.
    if hid.startswith("bangkok"):
        return "bangkok"
    if hid.startswith("singapore"):
        return "singapore"
    if hid.startswith("kl-") or hid.startswith("kualalumpur") or hid.startswith("kuala-lumpur"):
        return "kuala lumpur"

    # 3. WA road-trip towns: id is "<town>-<town>-<name>" (town slug may itself
    #    contain a hyphen, e.g. "margaret-river"). Match the longest WA town
    #    slug that prefixes the id so multi-word towns resolve before single
    #    segments. The town IS its own area (single-area city).
    for town in sorted(WA_TOWNS, key=len, reverse=True):
        if hid.startswith(town + "-"):
            return town

    # 4. If a city hint is given and it is single-area, use it.
    if city:
        c = city.strip().lower()
        if c in SINGLE_AREA_CITIES:
            return SINGLE_AREA_CITIES[c]

    return None


# ---------------------------------------------------------------------------
# LLM system prompt — QUALITATIVE RANKING WITHIN A CLOSED SET ONLY (§10.11)
# ---------------------------------------------------------------------------
# CRITICAL: The LLM may ONLY rank/reorder the vibe-appropriate areas we hand
# it.  These are already filtered to the strict vibe-appropriate set before the
# LLM call.  Output is validated against both the city closed set AND the strict
# vibe set; anything outside is dropped.  The LLM cannot expand the set.

# (public showcase repo: this prompt is a generic placeholder for the tuned
# production prompt, which is withheld as it's judged to be part of the
# competitive prompt-engineering surface, per this repo's stated redaction
# policy -- see README.md. The JSON schema/contract below is unchanged, so
# the LLM-on ranking edge still behaves identically; only the wording that
# was iterated on privately for ranking quality is generic here.)
_LLM_SYSTEM_PROMPT = """Rank the given areas best-to-worst for the requested vibe, choosing only from the provided allowed_areas list.

Output ONLY valid JSON: {"areas": [<area string>, ...]}. Use only allowed_areas values (exact spelling), best-first order, no invented areas, no other fields.
"""


def _build_user_prompt(city: str, vibe: str | None, allowed_areas: list[str]) -> str:
    return json.dumps({
        "city": city,
        "vibe": vibe or "any",
        "allowed_areas": allowed_areas,
    })


# ---------------------------------------------------------------------------
# LLM call (mirrors intent_parser._llm_call)
# ---------------------------------------------------------------------------


def _llm_call(city: str, vibe: str | None, allowed_areas: list[str]) -> str:
    """
    Call DashScope to rank areas for (city, vibe) via the active model (model_router).

    Returns the raw response string.  Raises RuntimeError if the API call fails
    or the key is missing (caller falls back deterministically).
    """
    if not DASHSCOPE_API_KEY:
        raise RuntimeError(
            "DASHSCOPE_API_KEY not set in environment — LLM call unavailable"
        )

    try:
        from utils.model_router import dashscope_chat
    except ImportError:
        from model_router import dashscope_chat  # type: ignore[no-redef]

    body = {
        "enable_thinking": False,  # qwen3.x reasoning models: skip thinking → fast vibe→area
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(city, vibe, allowed_areas)},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        data = dashscope_chat("default", body, timeout=30.0)
        content = data["choices"][0]["message"]["content"]
        return content if isinstance(content, str) else json.dumps(content)
    except httpx.HTTPStatusError as e:
        # SECURITY: log the raw upstream response body server-side only — never
        # embed it in the exception message (see intent_parser.py's matching fix).
        logger.warning(
            "DashScope API error HTTP %s: %s",
            e.response.status_code, e.response.text[:300],
        )
        raise RuntimeError(f"DashScope API error HTTP {e.response.status_code}")
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected DashScope response shape: {e}")


def _parse_llm_response(response_text: str) -> list[str] | None:
    """
    Parse the LLM response into a raw (UNVALIDATED) list of area strings.

    Strips markdown fences; accepts {"areas":[...]} or a bare JSON list.
    Returns the raw list or None if unparseable.
    """
    text = response_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("destination.assess: LLM response not valid JSON: %r", text[:200])
        return None

    if isinstance(parsed, dict):
        areas = parsed.get("areas")
    elif isinstance(parsed, list):
        areas = parsed
    else:
        return None

    if not isinstance(areas, list):
        return None
    return [a for a in areas if isinstance(a, str)]


# ---------------------------------------------------------------------------
# VARIANCE CLAMP — validate raw area list against the city's closed set
# ---------------------------------------------------------------------------


def _normalise_area_token(raw: str) -> str:
    """Normalise an area token: lowercase, strip, collapse spaces/underscores."""
    t = raw.strip().lower()
    t = re.sub(r"[\s_]+", "", t)  # "nusa dua" / "nusa_dua" → "nusadua"
    return t


# ---------------------------------------------------------------------------
# #74 catalog-derived area fallback. Every catalog city has REAL areas in
# destination_areas.json (city -> sorted real catalog areas, generated from
# ucp-merchant/catalog.json). The hardcoded AREA_CLOSED_SETS above keeps the
# curated demo cities (Bali sub-areas + vibe rankings); for ANY OTHER catalog
# city we fall back to its REAL catalog areas (e.g. Cairo downtown/garden city,
# Tbilisi old town/fabrika quarter) instead of the thin/empty "unknown city"
# path. No fabrication — the areas come straight from the booked catalog —
# and deterministic (the data file is a fixed snapshot), so var-0 holds.
# ---------------------------------------------------------------------------
_CATALOG_AREAS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "destination_areas.json"
)
try:
    with open(_CATALOG_AREAS_PATH, encoding="utf-8") as _caf:
        _CATALOG_DERIVED_AREAS: dict[str, frozenset[str]] = {
            k: frozenset(v) for k, v in json.load(_caf).items()
        }
except FileNotFoundError:  # pragma: no cover - the data file ships with the agent
    _CATALOG_DERIVED_AREAS = {}


def _closed_set_for(city: str) -> frozenset[str] | None:
    """The city's REAL catalog area closed-set: the hardcoded curated set
    (Bali sub-areas etc.) first, else the catalog-derived real areas (#74),
    else None (genuinely unknown city → honest empty, no fabrication)."""
    key = city.strip().lower()
    hard = AREA_CLOSED_SETS.get(key)
    if hard is not None:
        return hard
    derived = _CATALOG_DERIVED_AREAS.get(key)
    return derived if derived else None


def _clamp_areas(raw_areas: list[str], city: str) -> list[str]:
    """
    VARIANCE CLAMP: keep only areas that are in the city's REAL catalog closed
    set, preserving the LLM's order and de-duplicating.

    Any area outside the closed set is dropped (logged).  Returns a possibly
    empty list (caller applies the deterministic fallback when empty).
    """
    closed = _closed_set_for(city)
    if closed is None:
        logger.warning("destination.assess: clamp: unknown city %r — no closed set", city)
        return []

    # #74 fix: match on the NORMALISED token (so multi-word areas like "garden city" — which the
    # LLM may return as "gardencity" — still match) but OUTPUT the REAL catalog area, so the result
    # is the exact form the accommodation agent clamps hotels against (its lowercased catalog area).
    norm_to_real = {_normalise_area_token(a): a for a in closed}
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_areas:
        tok = _normalise_area_token(raw)
        real = norm_to_real.get(tok)
        if real is not None and real not in seen:
            out.append(real)
            seen.add(real)
        elif real is None:
            logger.info(
                "destination.assess: clamp: dropped out-of-catalog area %r (city=%s)",
                raw, city,
            )
    return out


# ---------------------------------------------------------------------------
# DETERMINISTIC FALLBACK
# ---------------------------------------------------------------------------


def _deterministic_fallback(city: str, vibe: str | None) -> tuple[list[str], str]:
    """
    Deterministic, vibe-keyed area list.  The variance FLOOR.

    Returns (areas, source) where source ∈ {"city_single", "fallback"}.
    Never returns an empty list for a known city.
    """
    c = city.strip().lower()

    # Single-area cities: the city IS the area.
    if c in SINGLE_AREA_CITIES:
        return [SINGLE_AREA_CITIES[c]], "city_single"

    if c == "bali":
        v = (vibe or "").strip().lower()
        if v in BALI_VIBE_FALLBACK:
            return list(BALI_VIBE_FALLBACK[v]), "fallback"
        # 'city' vibe or unknown → broad deterministic default (still real areas)
        return list(BALI_DEFAULT_RANKED), "fallback"

    # #74: any OTHER catalog city → its REAL catalog areas in a deterministic (sorted) order.
    # This makes the richness work on the LLM-OFF / CI / var-0 path (the default), not just when
    # the LLM happens to be available. Areas come straight from the booked catalog — no fabrication.
    derived = _closed_set_for(c)
    if derived:
        return sorted(derived), "catalog"

    # Unknown city: no closed set; return empty (caller surfaces unavailable).
    return [], "fallback"


def broaden_areas(city: str, vibe: str | None, current_areas: list[str]) -> list[str]:
    """
    DETERMINISTIC re-plan broadening (NO LLM).

    Given the currently-targeted areas that yielded no in-budget hotel, return a
    broader-but-still-vibe-appropriate area set for the city.  Used by the
    orchestrator's bounded re-plan fallback BEFORE going city-wide.

    Returns areas NOT already in current_areas where possible (so the caller
    makes progress), but always a valid subset of the catalog closed set.
    """
    c = city.strip().lower()
    if c in SINGLE_AREA_CITIES:
        return [SINGLE_AREA_CITIES[c]]
    if c != "bali":
        return list(current_areas)

    v = (vibe or "").strip().lower()
    broad = BALI_VIBE_BROAD.get(v)
    if broad is None:
        broad = list(BALI_DEFAULT_RANKED)
    # Keep broad order; ensure current areas first (they were ranked higher),
    # then append the new ones.
    cur = [a for a in current_areas if a in BALI_AREAS]
    merged = list(cur)
    for a in broad:
        if a not in merged:
            merged.append(a)
    return merged


def city_wide_areas(city: str) -> list[str]:
    """Full city-wide area set (last deterministic re-plan step)."""
    c = city.strip().lower()
    if c in SINGLE_AREA_CITIES:
        return [SINGLE_AREA_CITIES[c]]
    if c == "bali":
        return list(BALI_DEFAULT_RANKED)
    return []


# ---------------------------------------------------------------------------
# Core assess function (pure — testable without the A2A server)
# ---------------------------------------------------------------------------


def _vibe_strict_set(city: str, vibe: str | None) -> list[str] | None:
    """
    Return the strict vibe-appropriate area list for (city, vibe), or None if
    no strict set exists for this city/vibe combination.

    For Bali with a known vibe: returns BALI_VIBE_FALLBACK[vibe].
    For unknown vibe or non-Bali: returns None (caller uses full closed set).

    This is the STRICT SET the LLM is constrained to rank within.  The LLM
    receives only these areas as candidates and the result is intersected back
    against this same set, so the LLM can only reorder — never expand.
    """
    c = city.strip().lower() if isinstance(city, str) else ""
    if c != "bali":
        return None
    # Guard non-string vibe (#53 — vibe could be non-string if called directly)
    vibe_str = vibe if isinstance(vibe, str) else ""
    v = (vibe_str or "").strip().lower()
    if v in BALI_VIBE_FALLBACK:
        return list(BALI_VIBE_FALLBACK[v])
    return None


def assess(
    city: str,
    vibe: str | None,
    checkin: str | None = None,
    checkout: str | None = None,
    country: str | None = None,
) -> dict[str, Any]:
    """
    Map (city, vibe) → ranked real catalog areas.

    When checkin/checkout are supplied, a seeded seasonal advisory (if any) for
    the city + date-window is attached under the "advisory" key (None otherwise).
    The advisory is deterministic + provenance-tagged and NEVER blocks booking.

    Pipeline (§10.11, tightened vibe-appropriateness):
      1. Single-area city short-circuit (deterministic).
      2. Determine the strict vibe-appropriate set (BALI_VIBE_FALLBACK[vibe]).
         If a strict set exists, hand ONLY those areas to the LLM — the LLM
         can only reorder within the already-vibe-filtered set.
         If no strict set (unknown vibe / non-Bali), use full city closed set.
      3. LLM call (qwen3-max) ranks the provided areas (one-shot, one retry).
      4. VARIANCE CLAMP: city-level clamp (drops invented areas) THEN vibe-strict
         intersection (drops loosely-related areas the LLM might have smuggled in).
         Invariant: returned areas ⊆ strict_set (BALI_VIBE_FALLBACK[vibe]).
      5. DETERMINISTIC FALLBACK if intersection empty/invalid/unavailable.

    Returns a typed AreaAssessment dict (areas always non-empty for a known
    city).  For an unknown city, areas is [] and source is "fallback".
    """
    # Type-guard: city must be a string; vibe must be string-or-None (#53).
    # The handler already guards at the A2A boundary, but assess() is also called
    # directly (e.g. from tests, orchestrator shims). Coerce silently to avoid
    # crashing on truthy non-string values deep in the pipeline.
    if not isinstance(city, str):
        raise ValueError(f"assess(): city must be a str, got {type(city).__name__!r}")
    if not isinstance(vibe, (str, type(None))):
        vibe = None  # coerce non-string vibe to None (conservative fallback)

    c = city.strip().lower()

    # MED fix: derive country from _CITY_COUNTRY_DEFAULTS when the caller does
    # not pass one (e.g. the orchestrator's _call_destination payload omits
    # 'country').  _CITY_COUNTRY_DEFAULTS is built from SEASONAL_ADVISORIES keys
    # so the derivation is zero-false-positive: only cities with a seeded advisory
    # are in the map; for every other city the lookup returns None (no change).
    # An explicit caller-supplied country always takes precedence.
    _effective_country: str | None = (
        country
        if (country and isinstance(country, str) and country.strip())
        else _CITY_COUNTRY_DEFAULTS.get(c)
    )

    # Seeded seasonal advisory (deterministic; provenance-tagged; advisory-only).
    # Pass country so (city, country) keyed lookup avoids misattributing WA advisories
    # to globally-ambiguous city names (#57 fix).  Use _effective_country so the
    # advisory fires even when the caller omits the country field.
    _adv = seasonal_advisory(c, checkin, checkout, country=_effective_country)

    # 1. Single-area city short-circuit — no LLM needed, fully deterministic.
    if c in SINGLE_AREA_CITIES:
        return {
            "type": "area_assessment",
            "city": c,
            "vibe": vibe,
            "areas": [SINGLE_AREA_CITIES[c]],
            "source": "city_single",
            "provenance": "catalog",
            "advisory": _adv,
        }

    closed = _closed_set_for(c)
    if closed is None:
        # Unknown city — honest empty (no fabrication).
        logger.warning("destination.assess: unknown city %r", city)
        return {
            "type": "area_assessment",
            "city": c,
            "vibe": vibe,
            "areas": [],
            "source": "fallback",
            "provenance": "catalog",
            "advisory": _adv,
        }

    # 2. Determine the strict vibe-appropriate set.
    #    If BALI_VIBE_FALLBACK has an entry for this vibe, the LLM is given ONLY
    #    those areas — it cannot add loosely-related ones.  This is the primary
    #    vibe-appropriateness gate.  For unknown vibes, fall back to the full
    #    city closed set (no vibe constraint available).
    strict_set: list[str] | None = _vibe_strict_set(c, vibe)
    if strict_set is not None:
        # LLM sees only the vibe-appropriate strict set.
        allowed = list(strict_set)
        logger.info(
            "destination.assess: strict vibe set for city=%s vibe=%s → %s",
            c, vibe, allowed,
        )
    else:
        # No strict set (unknown vibe or non-Bali): full city closed set.
        allowed = sorted(closed)

    # 3. LLM ranking (one-shot, one retry on parse failure) — clamped after.
    clamped: list[str] = []
    for attempt in range(2):
        try:
            response_text = _llm_call(c, vibe, allowed)
            logger.info(
                "destination.assess: LLM attempt %d city=%s vibe=%s → %r",
                attempt + 1, c, vibe, response_text[:200],
            )
            raw_areas = _parse_llm_response(response_text)
            if raw_areas is not None:
                # 4a. City-level clamp: drops invented areas not in city catalog.
                city_clamped = _clamp_areas(raw_areas, c)
                # 4b. Vibe-strict intersection: if a strict set exists, keep only
                #     areas within it (LLM may only reorder, never expand the set).
                #     Preserves LLM's ordering for surviving items.
                if strict_set is not None:
                    strict_frozenset = frozenset(strict_set)
                    vibe_clamped = [a for a in city_clamped if a in strict_frozenset]
                    if len(vibe_clamped) < len(city_clamped):
                        dropped = [a for a in city_clamped if a not in strict_frozenset]
                        logger.info(
                            "destination.assess: vibe-strict intersection dropped %s "
                            "(city=%s vibe=%s strict_set=%s)",
                            dropped, c, vibe, list(strict_set),
                        )
                    clamped = vibe_clamped
                else:
                    clamped = city_clamped
                if clamped:
                    break
                logger.info(
                    "destination.assess: LLM areas all dropped by clamp — retry/fallback"
                )
        except Exception as e:  # noqa: BLE001 — LLM is one-shot & clamped; any
            # failure (RuntimeError, network, parse) falls back deterministically.
            logger.warning("destination.assess: LLM call failed (attempt %d): %s", attempt + 1, e)
            if "DASHSCOPE_API_KEY not set" in str(e):
                break  # no point retrying without a key

    # 5. Clamped + vibe-intersected LLM result wins if non-empty.
    if clamped:
        return {
            "type": "area_assessment",
            "city": c,
            "vibe": vibe,
            "areas": clamped,
            "source": "llm",
            "provenance": "catalog",
            "advisory": _adv,
        }

    # 5. DETERMINISTIC FALLBACK.
    areas, source = _deterministic_fallback(c, vibe)
    logger.info(
        "destination.assess: deterministic fallback city=%s vibe=%s → %s",
        c, vibe, areas,
    )
    return {
        "type": "area_assessment",
        "city": c,
        "vibe": vibe,
        "areas": areas,
        "source": source,
        "provenance": "catalog",
        "advisory": _adv,
    }


# ---------------------------------------------------------------------------
# DestinationAgent (A2A)
# ---------------------------------------------------------------------------


class DestinationAgent(A2AAgent):
    """
    Destination / Local-expert A2A agent (M-Agentic-2).

    Implements the ``destination.assess`` skill: maps a leg's (city, vibe) to a
    ranked list of REAL catalog areas, using qwen3-max behind a closed-set
    variance clamp + deterministic vibe-keyed fallback (§10.11).

    One-shot per leg; never invoked inside the orchestrator's re-plan loop.

    Args:
        host: Bind host for the ASGI server.
        port: Bind port for the ASGI server.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9104,
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
            "name": "destination-agent",
            "description": (
                "Local-expert: maps a leg's (city, vibe) to ranked REAL catalog areas. "
                "Uses qwen3-max for qualitative ranking behind a closed-set variance "
                "clamp (areas must be real catalog areas for the city) and a "
                "deterministic vibe-keyed fallback. Never invents areas, prices, or "
                "hotels. One-shot per leg, never inside the re-plan loop. "
                "Implements A2A skill 'destination.assess'. "
                "Part of the Travel Guild multi-agent pipeline (Track 3, M-Agentic-2)."
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
                    "id": "destination.assess",
                    "name": "Assess Destination Areas",
                    "description": (
                        "Given {city, vibe}, return a ranked list of REAL catalog "
                        "areas best-fitting the vibe. LLM ranks among a closed set of "
                        "real areas; output is clamped to the catalog; invalid/empty "
                        "output falls back to a deterministic vibe-keyed area list. "
                        "Real areas only — never fabricates."
                    ),
                    "tags": ["destination", "local-expert", "area", "vibe", "catalog"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string"},
                            "vibe": {"type": "string"},
                        },
                        "required": ["city"],
                    },
                    "examples": [
                        '{"city":"bali","vibe":"beach"}',
                        '{"city":"bali","vibe":"culture"}',
                    ],
                }
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("destination.assess", self._assess_handler)

    # ------------------------------------------------------------------
    # Skill handler
    # ------------------------------------------------------------------

    async def _assess_handler(self, message: dict, task: dict) -> dict:
        """destination.assess skill handler."""
        payload = self._extract_payload(message)
        if payload is None:
            raise ValueError(
                "destination.assess requires a data part with JSON payload {city, vibe}"
            )

        # Type-guard: city must be a non-empty string (#53 fix — truthy non-string
        # values like 123/{} would slip past `if not city` and then crash on .strip()).
        city_raw = payload.get("city", "")
        if not isinstance(city_raw, str) or not city_raw.strip():
            raise ValueError("city is required and must be a non-empty string")
        city: str = city_raw

        # vibe must be a string-or-None (#53 fix).
        vibe_raw = payload.get("vibe")
        vibe: str | None = vibe_raw if isinstance(vibe_raw, str) else None
        vibe = vibe or None  # normalise empty string to None

        checkin: str | None = payload.get("checkin") or None
        checkout: str | None = payload.get("checkout") or None
        country: str | None = payload.get("country") or None

        result = assess(city, vibe, checkin, checkout, country=country)

        logger.info(
            "destination.assess: city=%s vibe=%s → areas=%s source=%s",
            city, vibe, result["areas"], result["source"],
        )

        return _new_artifact(
            name="destination.assess.result",
            parts=[_data_part(result)],
        )

    # ------------------------------------------------------------------
    # Input extraction helper
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_payload(message: dict) -> dict | None:
        """Extract the JSON payload from the first data or text part."""
        for part in message.get("parts", []):
            if part.get("kind") == "data":
                data = part.get("data")
                if isinstance(data, dict):
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
                    if isinstance(parsed, dict):
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
    port = int(os.environ.get("PORT", 9104))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = DestinationAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Destination agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
