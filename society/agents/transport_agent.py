"""
transport_agent.py — Transport/Logistics feasibility specialist (Travel Guild M3b).

Design contract: the internal design spec §3.x, §10.x.

Agent Card skill: ``transport.feasibility``

DETERMINISTIC — no LLM, no DashScope, no live timetables.
All transfer times are seeded advisory values; never fetched from external APIs.

Input (data part, JSON):
    List form:
        [{"leg_id": str, "city": str, "area": str,
          "checkin": str, "checkout": str}, ...]
    Or wrapped dict form:
        {"legs": [...]}

Output artifact (data part, JSON) — typed TransportResult:
    {
        "edges": [
            {
                "from_leg":         str,
                "to_leg":           str,
                "from_city":        str,
                "to_city":          str,
                "from_area":        str,
                "to_area":          str,
                "feasible":         bool,
                "mode":             str,   # "road" | "flight" | "same_area"
                "transfer_minutes": int,
                "reason":           str | null
            }
        ],
        "infeasible_edges":      [...],   # subset of edges where feasible=False
        "suggested_reordering":  [str] | null  # leg_ids in suggested order, or null
    }

Transfer model (seeded, HARD-CODED, advisory-grade):

  Bali intra-island area pairs use road-transfer minutes (symmetric).
  Inter-city pairs use flight times (symmetric, advisory).
  Same-day inter-city feasibility check: if transfer_minutes > 240 and
  leg[k].checkout == leg[k+1].checkin → infeasible.

Runnable service: HOST / PORT env, defaults 0.0.0.0:9104.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any

import uvicorn

from agents.a2a_agent import (
    A2AAgent,
    _data_part,
    _new_artifact,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seeded transfer model — HARD-CODED, advisory-grade, NEVER live timetables
# ---------------------------------------------------------------------------

# Bali intra-island area transfer times (minutes, road).
# The table is stored as a dict keyed by frozenset so both orderings hit the
# same value — guaranteeing symmetry at read time.
_BALI_AREAS = {
    "ubud", "seminyak", "kuta", "legian", "sanur",
    "nusadua", "jimbaran", "canggu",
}

_BALI_ROAD_MINUTES: dict[frozenset, int] = {
    frozenset({"ubud",     "seminyak"}):  75,
    frozenset({"ubud",     "kuta"}):      80,
    frozenset({"ubud",     "legian"}):    80,
    frozenset({"ubud",     "sanur"}):     55,
    frozenset({"ubud",     "nusadua"}):   70,
    frozenset({"ubud",     "jimbaran"}):  65,
    frozenset({"ubud",     "canggu"}):    90,
    frozenset({"seminyak", "kuta"}):      15,
    frozenset({"seminyak", "legian"}):    10,
    frozenset({"seminyak", "sanur"}):     40,
    frozenset({"seminyak", "nusadua"}):   50,
    frozenset({"seminyak", "jimbaran"}):  45,
    frozenset({"seminyak", "canggu"}):    20,
    frozenset({"kuta",     "legian"}):    10,
    frozenset({"kuta",     "sanur"}):     35,
    frozenset({"kuta",     "nusadua"}):   40,
    frozenset({"kuta",     "jimbaran"}):  35,
    frozenset({"kuta",     "canggu"}):    25,
    frozenset({"legian",   "sanur"}):     40,
    frozenset({"legian",   "nusadua"}):   45,
    frozenset({"legian",   "jimbaran"}):  40,
    frozenset({"legian",   "canggu"}):    25,
    frozenset({"sanur",    "nusadua"}):   30,
    frozenset({"sanur",    "jimbaran"}):  35,
    frozenset({"sanur",    "canggu"}):    55,
    frozenset({"nusadua",  "jimbaran"}):  20,
    frozenset({"nusadua",  "canggu"}):    60,
    frozenset({"jimbaran", "canggu"}):    50,
}

# Inter-city flight times (minutes, advisory).
_INTER_CITY_FLIGHTS: dict[frozenset, int] = {
    frozenset({"bali",      "bangkok"}):        360,
    frozenset({"bali",      "singapore"}):      240,
    frozenset({"bali",      "kuala lumpur"}):   210,
    frozenset({"bangkok",   "singapore"}):      150,
    frozenset({"bangkok",   "kuala lumpur"}):   100,
    frozenset({"singapore", "kuala lumpur"}):    60,

    # Ethiopia historic-route + Danakil circuit — FLIGHT-DEPENDENT (mountainous
    # terrain + security make internal flights the practical link). SEEDED from
    # the user's real 2023 trip; advisory minutes. These edges are the ones the
    # cascade fault cancels (ET120 Gondar→Lalibela, ET106 Addis→Semera).
    frozenset({"addis ababa", "gondar"}):       70,
    frozenset({"gondar",      "lalibela"}):      45,
    frozenset({"addis ababa", "lalibela"}):      60,
    frozenset({"addis ababa", "semera"}):        80,
    frozenset({"lalibela",    "semera"}):        90,
    frozenset({"gondar",      "semera"}):       110,
}

# ---------------------------------------------------------------------------
# Western Australia inter-TOWN ROAD legs — SEEDED from the user's real 2022 WA
# road trip (Perth-to-Perth loop, ~2816 km). PROVENANCE: structure + drive
# distances are REAL (the user's trip); see ucp-merchant/catalog.go provenance
# block + the internal design spec §13.
#
# Unlike the inter-CITY pairs above (which are flights), WA towns are connected
# by ROAD. We seed the real km between consecutive towns; minutes are advisory
# (km / 90 km·h⁻¹ highway average, rounded). These legs are ALWAYS feasible — a
# long drive is not infeasible, it just needs a buffer. The Transport agent
# flags "long_drive" advisories on the 391 km and 536 km legs so the society
# can buffer / sequence sensibly (exercises the §12.2 buffer behaviour).
# ---------------------------------------------------------------------------

# Real consecutive-leg drive distances (km), symmetric (keyed by frozenset).
_WA_ROAD_KM: dict[frozenset, int] = {
    frozenset({"perth",          "busselton"}):       233,
    frozenset({"busselton",      "margaret-river"}):   48,
    frozenset({"margaret-river", "pemberton"}):       139,
    frozenset({"pemberton",      "albany"}):          239,
    frozenset({"albany",         "ravensthorpe"}):    293,
    frozenset({"ravensthorpe",   "esperance"}):       188,
    frozenset({"esperance",      "kalgoorlie"}):      391,  # LONGEST single leg
    frozenset({"kalgoorlie",     "northam"}):         536,  # VERY LONG / "difficult" (via Coolgardie/Merredin)
    frozenset({"northam",        "perth"}):            97,  # loop close
}

# Advisory highway average (km/h) → minutes. Seeded constant, never live.
_WA_HIGHWAY_KMH = 90

# A WA road leg longer than this (km) gets a "long_drive" advisory: still
# feasible, but the society should add a rest/fuel buffer and avoid stacking it
# against a same-day check-in. 280 km cleanly separates the routine legs (≤293
# is borderline-long but routine here) from the genuinely long 391/536 km hauls.
_WA_LONG_DRIVE_KM = 350

# WA towns (mirror of destination_agent.WA_TOWNS) — used to recognise a leg pair
# as an intra-WA road transfer.
_WA_TOWNS: frozenset[str] = frozenset({
    "perth", "busselton", "margaret-river", "pemberton", "albany",
    "ravensthorpe", "esperance", "kalgoorlie", "northam",
})

# Same-day inter-city transfer is implausible when transfer_minutes > this.
_SAME_DAY_INTERCITY_THRESHOLD = 240

# Any edge (any mode, any dates) with transfer_minutes above this threshold
# gets a generic "long_transfer" advisory so the society can buffer multi-hour
# fallback flights / very long connections regardless of mode or date collision.
# 600 min (10 hours) cleanly separates routine long-haul flights from genuinely
# transcontinental or polar connections that need explicit itinerary buffering.
_LONG_TRANSFER_ADVISORY_MIN = 600


# ---------------------------------------------------------------------------
# Transfer lookup helpers
# ---------------------------------------------------------------------------

def _normalise(s: str) -> str:
    """Lowercase-strip a city/area name for lookup."""
    return s.strip().lower()


# #70: a TINY, data-VERIFIED map from a colloquial/short city name to the name used in
# city_coords / the OpenFlights air-network, so a leg that BOOKS a hotel (the merchant tolerates
# "cebu") also resolves a REAL flight (coords + airport are keyed "cebu city"). Applied ONLY in
# the coords/air-network lookups below — NOT in _normalise — so Bali-area / WA / ferry seed keys
# stay byte-identical. Do NOT alias island/region names (e.g. "palawan") to a city: those have no
# single airport-city and must stay honest-unverified, never a fabricated connection.
_CITY_CANONICAL = {
    "cebu": "cebu city",
}


def _canonical_city(s: str) -> str:
    """_normalise + a small verified colloquial->coords-key alias (#70). Lookup-path only."""
    n = _normalise(s)
    return _CITY_CANONICAL.get(n, n)


def _lookup_transfer(
    from_city: str,
    to_city: str,
    from_area: str,
    to_area: str,
) -> tuple[int, str]:
    """
    Return (transfer_minutes, mode) for the leg pair.

    Raises KeyError if no seeded value exists (unknown city pair).
    """
    fc = _normalise(from_city)
    tc = _normalise(to_city)
    fa = _normalise(from_area)
    ta = _normalise(to_area)

    # Empty/whitespace city on either leg → unknown, cannot route; caller flags infeasible.
    # This check MUST come before the fc==tc same-city branch so that two legs
    # with no city do not silently collapse to a trivial same-area/road transfer.
    if not fc or not tc:
        raise KeyError(
            f"missing city on leg: from_city={from_city!r} to_city={to_city!r}"
        )

    # Same city → intra-city road transfer (or same-area short-circuit)
    if fc == tc:
        if fa == ta:
            return 0, "same_area"
        # Bali intra-island: look up the road table
        key = frozenset({fa, ta})
        if key in _BALI_ROAD_MINUTES:
            return _BALI_ROAD_MINUTES[key], "road"
        # Unknown area pair within a city: default road 30 min (advisory)
        return 30, "road"

    # Inter-TOWN WA road leg (real seeded km → advisory minutes). Checked BEFORE
    # the flight table so WA towns route by road, not flight.
    wa_key = frozenset({fc, tc})
    if wa_key in _WA_ROAD_KM:
        km = _WA_ROAD_KM[wa_key]
        minutes = round(km / _WA_HIGHWAY_KMH * 60)
        return minutes, "road"

    # Inter-city: look up flight table
    city_key = frozenset({fc, tc})
    if city_key in _INTER_CITY_FLIGHTS:
        return _INTER_CITY_FLIGHTS[city_key], "flight"

    # Unknown inter-city pair: raise so the caller can flag it
    raise KeyError(f"No seeded transfer time for {fc!r} → {tc!r}")


# ---------------------------------------------------------------------------
# COORDINATE/DISTANCE FALLBACK (LP500 expansion). For inter-city pairs NOT in the
# seeded tables above, estimate a flight transfer from city coordinates so the
# 437 gateway cities are routable. CONSULTED ONLY after the frozen seed tables
# miss (the seeded pairs return earlier → byte-identical) and NEVER on the
# cancelled-edge path (that keeps its -1/"unknown" semantics → frozen snapshots
# stay byte-identical). Output is rounded to 5-min granularity so floating-point
# last-ULP variance cannot change the integer result (var-0 safe).
# ---------------------------------------------------------------------------
_FALLBACK_OVERHEAD_MIN = 90       # airport/check-in/transit overhead (advisory)
_FALLBACK_CRUISE_KMH = 800        # cruise speed for the estimate
# #76 — a SHORT un-seeded hop is overland (rail/road), NOT a flight. Flying ~30km (e.g. Osaka↔Nara,
# same KIX airport) is nonsensical; below this distance the fallback estimates an overland transfer.
_GROUND_TRANSFER_MAX_KM = 450     # below → overland (rail/HSR territory: Osaka-Nara-Tokyo, Paris-Lyon);
                                  # at/above → flight estimate (e.g. Manila-Cebu ~570km, cross-island)
_GROUND_OVERHEAD_MIN = 30         # station access / wait overhead
_GROUND_SPEED_KMH = 80            # conventional rail/road average for the estimate
# Phase 2 — HIGH-SPEED RAIL: on an HSR corridor (_rail_tier == "hsr") rail is far faster and wins
# door-to-door much further out than conventional. Door-to-door breakeven vs a short-haul flight
# (~90min airport overhead + ~800km/h) lands near ~1000km for ~250km/h HSR — so HSR rail is
# preferred up to _HSR_TRANSFER_MAX_KM (Madrid-Barcelona/Paris-Marseille/Rome-Milan rail; Beijing-
# Shanghai ~1068km just past it → flight, matching real behaviour). Conventional stays at 450km and
# 80km/h UNCHANGED (existing rail edges byte-identical → var-0).
_HSR_RAIL_KMH = 250               # effective HSR speed incl. stops (Shinkansen/CRH/TGV/AVE class)
_HSR_TRANSFER_MAX_KM = 1000       # HSR rail preferred up to here; beyond → flight
# Phase 3a — OVERNIGHT SLEEPER advisory (append-only; does NOT change mode/minutes/budget/dates).
_SLEEPER_MIN_KM = 350             # below: too short for an overnight sleeper to make sense
_SLEEPER_MAX_KM = 1900            # above: a sleeper would be >~1 night; advise flight instead
_SLEEPER_NIGHT_KMH = 90           # indicative overnight rail speed (for the ~hours estimate only)
_SLEEPER_CAVEAT = ("overnight sleeper train available on this corridor — can double as a night's "
                   "accommodation (saves ~1 hotel night); seeded vintage, no live schedule — "
                   "confirm availability at booking")
# Phase 3b — PERSONA weighting. A "comfort" traveller (e.g. an elderly couple prioritising comfort
# + flexibility over speed) prefers overland rail/ferry and tolerates a longer journey to avoid
# airports — so the overland rail-preference RANGE is widened by this factor (conventional 450→630km,
# HSR 1000→1400km, so e.g. Beijing-Shanghai HSR is rail for comfort but flight by default). Persona
# defaults to "default" everywhere → the default path is byte-identical (var-0). Hook for #35.
_COMFORT_RANGE_MULT = 1.4
_PERSONAS = frozenset({"default", "comfort"})
_EARTH_KM = 6371.0


def _load_city_coords() -> dict[str, tuple[float, float]]:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    coords: dict[str, tuple[float, float]] = {}
    try:
        with open(os.path.join(base, "city_coords.json"), encoding="utf-8") as fh:
            coords = {str(k).strip().lower(): (float(v[0]), float(v[1]))
                      for k, v in json.load(fh).items()}
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        coords = {}
    # #76 GAP-FILL: SimpleMaps World Cities (CC BY 4.0, https://simplemaps.com/data/world-cities)
    # centroids for cities NOT already mapped — extends airport-matching to ~42k more cities. Existing
    # entries are NEVER overwritten, and it's a static file → byte-identical determinism (var-0) holds.
    try:
        with open(os.path.join(base, "city_coords_worldcities.json"), encoding="utf-8") as fh:
            for k, v in json.load(fh).items():
                kk = str(k).strip().lower()
                if kk not in coords:
                    coords[kk] = (float(v[0]), float(v[1]))
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        pass
    return coords


_CITY_COORDS: dict[str, tuple[float, float]] = _load_city_coords()


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return _EARTH_KM * 2 * math.asin(math.sqrt(h))


def _fallback_transfer(from_city: str, to_city: str, persona: str = "default") -> tuple[int, str] | None:
    """Coordinate-distance flight estimate for an inter-city pair with no seeded
    value. Deterministic INTEGER minutes (5-min granularity). None if either city
    lacks coordinates (→ caller keeps the existing infeasible behavior)."""
    a = _CITY_COORDS.get(_canonical_city(from_city))
    b = _CITY_COORDS.get(_canonical_city(to_city))
    if a is None or b is None:
        return None
    km = _haversine_km(a, b)
    # A hop is overland (rail) ONLY when the two cities share a landmass — a hop SEPARATED BY WATER
    # (e.g. an island city with no nearby airport, so the air layer returned nothing and the dispatch
    # fell through here) must NOT be labelled "rail across the sea"; it falls through to the flight
    # estimate below (honesty: no train over water). Phase 2: the overland range + rail speed are
    # TIER-AWARE — HSR corridors are preferred to ~1000km at ~250km/h; conventional to 450km at 80km/h
    # (conventional path byte-identical to before → var-0).
    tier = _rail_tier(from_city, to_city)
    max_km = _overland_ceiling(tier, persona)
    if km <= max_km and _overland_possible(from_city, to_city):
        speed = _HSR_RAIL_KMH if tier == "hsr" else _GROUND_SPEED_KMH
        raw = _GROUND_OVERHEAD_MIN + km / speed * 60.0
        minutes = int(round(raw / 5.0)) * 5
        return max(minutes, 20), "rail"
    raw = _FALLBACK_OVERHEAD_MIN + km / _FALLBACK_CRUISE_KMH * 60.0
    minutes = int(round(raw / 5.0)) * 5     # 5-min buckets → ULP-stable
    return max(minutes, 30), "flight"


# ---------------------------------------------------------------------------
# LAND vs SEA gate (honesty: never assert "rail across open water"). A precomputed
# Natural Earth 1:50m land-polygon table (society/land_polygons.json, public domain)
# is sampled along the straight line between two city centroids; if a large enough
# FRACTION of samples fall in water, the pair is a sea crossing → rail is NOT a valid
# overland option (it stays flight/ferry). Coarse open-water detection only — narrow
# straits that the coarse polygon misreads as land, and sea crossings that have a real
# fixed RAIL link, are handled by the curated override sets below. var-0: static committed
# polygons, integer-rounded LINEAR sampling (no trig → no cross-platform libm drift),
# boolean even-odd winding; no clock/RNG/network on the request path.
# ---------------------------------------------------------------------------
_LANDSEA_SAMPLES       = 25     # sample points along the city-to-city line (inclusive of endpoints)
_LANDSEA_WATER_FRAC    = 0.20   # >= this fraction of samples in water → treat as a sea crossing
_LANDSEA_GRID_DECIMALS = 3      # round each sample lat/lon before the point-in-poly test → ULP/platform-stable
_LANDMASS_SNAP_KM      = 25     # snap a coastal city whose centroid falls just off the coarse coastline onto its landmass
_CAUSEWAY_SNAP_KM      = 20     # reach from a causeway/bridge-linked island city to its adjacent mainland ring

# Island cities with a fixed ROAD+RAIL link to the adjacent mainland (so they are overland-contiguous
# with it): Singapore via the Johor Causeway (KTM rail). Their effective landmass is the mainland.
_CAUSEWAY_TO_MAINLAND: frozenset = frozenset({"singapore"})

_LAYOVER_PENALTY_MIN   = 75     # added to any NON-nonstop flight (via-hub / routing-assumed connection)

# Sea crossings WITH a real fixed RAIL link (rail legitimate despite open water on the straight
# line): Channel Tunnel (London–Paris/Brussels/Amsterdam Eurostar), Øresund (Copenhagen–Malmö),
# Seikan (Aomori–Hakodate). Normalised, symmetric (frozenset of frozenset-pairs).
_FIXED_LINK_PAIRS: frozenset = frozenset({
    frozenset({"london", "paris"}), frozenset({"london", "brussels"}),
    frozenset({"london", "amsterdam"}), frozenset({"copenhagen", "malmo"}),
    frozenset({"copenhagen", "malmö"}), frozenset({"aomori", "hakodate"}),
})
# Fail-safe island cities: forced sea-side so a missing/corrupt polygon file (→ no geometry)
# still never fabricates rail to a well-known island. The polygon catches these when present;
# this is belt-and-suspenders for the honesty-critical cases.
_STRAIT_WATER_CITIES: frozenset = frozenset({
    "palermo", "catania", "messina", "agrigento", "trapani",         # Sicily
    "cagliari", "olbia", "sassari",                                  # Sardinia
    "palma", "ibiza", "mahon",                                       # Balearics
    "naha",                                                          # Okinawa
    "heraklion", "chania",                                           # Crete
    "valletta",                                                      # Malta
})


def _load_land_polygons():
    """Load the committed land-polygon table → list of [minlon, minlat, maxlon, maxlat, ring].
    Never raises on a missing file (CI/segregated runs) → empty list (the override sets + range
    check then carry the honesty)."""
    try:
        import json
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "land_polygons.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("rings", [])
    except Exception:
        return []


_LAND_RINGS = _load_land_polygons()


def _containing_ring(lat: float, lon: float) -> int | None:
    """Index of the first land ring CONTAINING (lat, lon), else None. Bbox-prefiltered even-odd
    ray cast over the static polygon table. Deterministic (fixed data + fixed arithmetic)."""
    x, y = lon, lat
    for idx in range(len(_LAND_RINGS)):
        minlon, minlat, maxlon, maxlat, ring = _LAND_RINGS[idx]
        if x < minlon or x > maxlon or y < minlat or y > maxlat:
            continue
        inside = False
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        if inside:
            return idx
    return None


def _point_on_land(lat: float, lon: float) -> bool:
    """True iff (lat, lon) lies inside any land ring."""
    return _containing_ring(lat, lon) is not None


def _nearest_ring_within(lat: float, lon: float, max_km: float,
                         exclude: int | None = None) -> int | None:
    """Index of the nearest land ring within max_km of (lat, lon) by minimum vertex distance
    (bbox-pruned), else None. SNAPS a coastal city whose centroid falls just off the coarse 1:50m
    coastline onto its adjacent landmass. `exclude` skips one ring (used to find the MAINLAND a
    causeway-linked island city connects to, past its own island ring). Deterministic: rounded-km
    comparison, lowest ring index wins ties."""
    deg = max_km / 111.0 + 0.05
    best: tuple[float, int] | None = None
    for idx in range(len(_LAND_RINGS)):
        if idx == exclude:
            continue
        minlon, minlat, maxlon, maxlat, ring = _LAND_RINGS[idx]
        if lon < minlon - deg or lon > maxlon + deg or lat < minlat - deg or lat > maxlat + deg:
            continue
        for vx, vy in ring:
            d = round(_haversine_km((lat, lon), (vy, vx)), 3)
            if best is None or (d, idx) < best:
                best = (d, idx)
    if best is None or best[0] > max_km:
        return None
    return best[1]


def _landmass_id(lat: float, lon: float) -> int | None:
    """Connected-landmass identity (a land-ring index) for a point: the containing ring, else the
    nearest ring within _LANDMASS_SNAP_KM (coastal snap), else None (unplaceable → open water)."""
    c = _containing_ring(lat, lon)
    if c is not None:
        return c
    return _nearest_ring_within(lat, lon, _LANDMASS_SNAP_KM)


def _city_landmass(norm: str, lat: float, lon: float) -> int | None:
    """Landmass identity for a NAMED city. A causeway/bridge-linked island city (Singapore via the
    Johor Causeway) is road+rail-contiguous with the adjacent mainland, so its effective landmass
    is that mainland (the nearest ring past its own island), NOT its island ring — otherwise a real
    overland route (Singapore→KL by KTM rail) would be mislabelled a sea crossing."""
    if norm in _CAUSEWAY_TO_MAINLAND:
        own = _containing_ring(lat, lon)
        m = _nearest_ring_within(lat, lon, _CAUSEWAY_SNAP_KM, exclude=own)
        if m is not None:
            return m
    return _landmass_id(lat, lon)


def _overland_possible(from_city: str, to_city: str) -> bool:
    """True iff two cities can plausibly be linked OVERLAND (rail/road). SAME-LANDMASS test: two
    cities are overland-linkable iff they sit on the SAME connected land component (Natural Earth
    ring). This — NOT a straight-line water-fraction — is the honest signal: a coastal corridor
    (Barcelona–Valencia, Lisbon–Porto) is ONE landmass even though the chord clips the sea, while
    an island hop (Naples–Palermo) is two. Order: fixed-link override (rail despite water) →
    curated island fail-safe (water) → same-landmass match → chord water-fraction fallback when an
    endpoint cannot be placed on land. Fail-SAFE: returns True when coords/geometry are unavailable
    (the caller already gates on coords + range; the curated sets carry the honesty-critical cases)."""
    a_norm = _normalise(from_city)
    b_norm = _normalise(to_city)
    if frozenset({a_norm, b_norm}) in _FIXED_LINK_PAIRS:
        return True
    if a_norm in _STRAIT_WATER_CITIES or b_norm in _STRAIT_WATER_CITIES:
        return False
    a = _CITY_COORDS.get(_canonical_city(from_city))
    b = _CITY_COORDS.get(_canonical_city(to_city))
    if a is None or b is None or not _LAND_RINGS:
        return True            # no geometry to judge → don't manufacture a water crossing
    la = _city_landmass(a_norm, a[0], a[1])
    lb = _city_landmass(b_norm, b[0], b[1])
    if la is not None and lb is not None:
        return la == lb        # same connected landmass → overland; different → sea crossing
    # An endpoint can't be placed on land (remote/offshore) → fall back to the chord water-fraction.
    n = _LANDSEA_SAMPLES
    water = 0
    for k in range(n):
        t = k / (n - 1)
        lat = round(a[0] + (b[0] - a[0]) * t, _LANDSEA_GRID_DECIMALS)
        lon = round(a[1] + (b[1] - a[1]) * t, _LANDSEA_GRID_DECIMALS)
        if not _point_on_land(lat, lon):
            water += 1
    return (water / n) < _LANDSEA_WATER_FRAC


# ---------------------------------------------------------------------------
# Phase 2 — HIGH-SPEED-RAIL tier classifier. A pair is HSR iff it is an explicitly curated HSR
# TRUNK city-pair (society/hsr_corridors.json); else conventional. We deliberately do NOT infer HSR
# from "same HSR country" — that over-claims non-trunk hops (Gifu-Kanazawa is a ~2h limited express,
# not Shinkansen). Drives the tier-aware overland range + rail-time speed. Static committed data;
# deterministic frozenset membership; never raises (missing file → no HSR → all conventional, the
# safe under-claim). Unlisted real HSR pairs default to conventional (honest under-claim).
# ---------------------------------------------------------------------------
def _load_hsr() -> frozenset:
    try:
        import json
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "hsr_corridors.json")
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return frozenset(frozenset({str(a).strip().lower(), str(b).strip().lower()})
                         for a, b in d.get("pairs", []) if a and b)
    except Exception:
        return frozenset()


_HSR_PAIRS = _load_hsr()


def _rail_tier(from_city: str, to_city: str) -> str:
    """'hsr' iff the pair is an explicitly curated HSR trunk corridor (society/hsr_corridors.json);
    else 'conventional'. We do NOT infer HSR from 'same HSR country' — that over-claims non-trunk
    hops (Gifu-Kanazawa is a ~2h limited express, not Shinkansen). Unlisted pairs default to
    conventional (honest under-claim). Only meaningful when the pair is overland-possible."""
    return "hsr" if frozenset({_normalise(from_city), _normalise(to_city)}) in _HSR_PAIRS else "conventional"


def _overland_ceiling(tier: str, persona: str = "default") -> int:
    """Distance ceiling (km) under which overland rail is preferred over flying — tier-aware
    (conventional 450 / HSR 1000), widened for a comfort persona (×_COMFORT_RANGE_MULT). persona
    'default' returns the base ceiling unchanged → byte-identical default path."""
    base = _HSR_TRANSFER_MAX_KM if tier == "hsr" else _GROUND_TRANSFER_MAX_KM
    return int(round(base * _COMFORT_RANGE_MULT)) if persona == "comfort" else base


def _load_sleeper() -> frozenset:
    try:
        import json
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "sleeper_corridors.json")
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return frozenset(frozenset({str(a).strip().lower(), str(b).strip().lower()})
                         for a, b in d.get("pairs", []) if a and b)
    except Exception:
        return frozenset()


_SLEEPER_PAIRS = _load_sleeper()


def _overnight_advisory(from_city: str, to_city: str) -> dict | None:
    """ADVISORY ONLY (Phase 3a): if the pair is a curated overnight-sleeper corridor within a sane
    distance band, return an advisory dict — an overnight sleeper that doubles as a night's lodging
    (saves ~1 hotel night). Does NOT change the edge mode/minutes/budget/dates; purely additive,
    vintage-caveated, no live schedule. None when not a sleeper corridor or out of band."""
    if frozenset({_normalise(from_city), _normalise(to_city)}) not in _SLEEPER_PAIRS:
        return None
    # Structural honesty guard: never advise an overnight TRAIN across water — only on the same
    # landmass (mirrors the rail mode gate). Belt-and-suspenders vs a mis-curated sea-crossing pair.
    if not _overland_possible(from_city, to_city):
        return None
    a = _CITY_COORDS.get(_canonical_city(from_city))
    b = _CITY_COORDS.get(_canonical_city(to_city))
    if a is None or b is None:
        return None
    km = _haversine_km(a, b)
    if km < _SLEEPER_MIN_KM or km > _SLEEPER_MAX_KM:
        return None
    return {"available": True, "approx_hours": int(round(km / _SLEEPER_NIGHT_KMH)),
            "saves_hotel_night": True, "note": _SLEEPER_CAVEAT}


def _short_overland_hop(from_city: str, to_city: str, persona: str = "default") -> bool:
    """True iff the two city centroids are within overland (rail/road) range
    (``_GROUND_TRANSFER_MAX_KM``) AND not separated by a water crossing — i.e. a hop a
    traveller would sensibly take by train, not fly. Used to SUPPRESS the air-network
    layer for short same-landmass hops: flying a ~130 km Honshu hop (Gifu→Kanazawa) is
    nonsensical, and when no nonstop exists the air graph can route through an absurd
    far-flung hub. An island hop (water crossing) keeps its flight/ferry option; a pair
    with missing coords returns False (no opinion → leave the air decision unchanged).
    Deterministic (static coords, no clock/RNG) → var-0 safe."""
    a = _CITY_COORDS.get(_canonical_city(from_city))
    b = _CITY_COORDS.get(_canonical_city(to_city))
    if a is None or b is None:
        return False
    max_km = _overland_ceiling(_rail_tier(from_city, to_city), persona)
    if _haversine_km(a, b) > max_km:
        return False
    return _overland_possible(from_city, to_city)


# ---------------------------------------------------------------------------
# AIR-NETWORK layer (OpenFlights, build #31). REAL route EXISTENCE plus the SET of
# operating carriers per route (a static REAL fact, OpenFlights ~2014 vintage) —
# still no schedules/flight-numbers/fares/stops/equipment/codeshare (PHANTOM-SAFE:
# a nonstop is labelled 'nonstop' iff it really exists in the OpenFlights graph, and
# 'operators' lists airlines KNOWN to fly it, vintage-caveated, NOT a live flight).
# When no direct route exists, we VERIFY a one-hop connection (src→hub→dst BOTH
# real routes). If verified: labelled 'connection (via hub)'. If NO one-hop hub
# is found: labelled 'connection (routing assumed)' — routing assumption only,
# NOT a confirmed itinerary (do not assert a specific connecting path).
# This AUGMENTS the city-centroid haversine fallback: consulted only AFTER the
# frozen seed tables miss, and ONLY when BOTH cities map to a real airport.
# A city with no mapped airport falls back to _fallback_transfer() unchanged
# (haversine path byte-identical → var-0 safe for those legs).
#
# var-0: static committed JSON, iteration over sorted candidates, integer 5-min
# rounding, no wall-clock / no randomness. Distances from REAL airport coords.
# ---------------------------------------------------------------------------

# Max distance (km) a city centroid may be from an airport for that airport to be
# the city's gateway. Generous enough for metro areas + secondary fields, tight
# enough that a city with no real nearby airport falls back to haversine instead
# of mis-mapping to a far-off field.
_NEAREST_AIRPORT_MAX_KM = 150.0


def _load_air_network() -> tuple[
    dict[str, dict],
    set[tuple[str, str]],
    dict[tuple[str, str], tuple[str, ...]],
    dict[str, str],
]:
    """Load from the committed air_network.json:
      - airports  {IATA: {name,city,country,lat,lon,icao,utc_offset,tz}}
      - route_set {(src,dst), ...}                          directed existence
      - route_carriers {(src,dst): (sorted carrier codes,)} REAL operating-carrier set
      - carrier_names {CODE: "Airline Name"}                code->name lookup

    Each route row is [src, dst, [carrier_code, ...]]; older 2-element rows (no
    carrier column) are tolerated (→ empty carrier tuple). Returns empty containers
    if the file is absent/corrupt (caller keeps the haversine fallback).

    PHANTOM-SAFE: carrier codes/names are a static REAL fact ('airlines known to
    operate this route', OpenFlights ~2014 vintage). No schedules/flight-numbers/
    fares are loaded — those were never written to the file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "air_network.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        airports = data.get("airports", {}) or {}
        carrier_names = {str(k): str(v) for k, v in (data.get("carriers", {}) or {}).items()}
        route_set: set[tuple[str, str]] = set()
        route_carriers: dict[tuple[str, str], tuple[str, ...]] = {}
        for row in data.get("routes", []) or []:
            if len(row) < 2:
                continue
            s, d = str(row[0]), str(row[1])
            route_set.add((s, d))
            codes = row[2] if len(row) > 2 and isinstance(row[2], list) else []
            # Stored sorted; sort defensively so output is deterministic regardless.
            route_carriers[(s, d)] = tuple(sorted(str(c) for c in codes))
        return airports, route_set, route_carriers, carrier_names
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return {}, set(), {}, {}


_AIRPORTS, _ROUTE_SET, _ROUTE_CARRIERS, _CARRIER_NAMES = _load_air_network()

# Vintage caveat surfaced on every operator-bearing edge. The carrier SET is a REAL
# static fact (which airlines flew the route in OpenFlights ~2014), NOT a live or
# guaranteed flight — schedules/times/fares/flight-numbers are intentionally absent.
_OPERATORS_CAVEAT = (
    "airlines known to operate this route (OpenFlights ~2014 vintage) — "
    "confirm current service at booking; NOT a live or guaranteed flight"
)


def route_operators(iata_src: str, iata_dst: str) -> list[str]:
    """Sorted list of airline NAMES known to operate the EXACT directed route
    src->dst (OpenFlights ~2014 vintage), or [] if the route is unknown or no
    carrier resolves to a name. Carrier codes with no name-table entry fall back
    to the bare code so the operator is never silently dropped.

    var-0: codes are stored sorted; names are sorted again here so the output is
    deterministic and stable regardless of name collisions."""
    src = (iata_src or "").strip().upper()
    dst = (iata_dst or "").strip().upper()
    if not src or not dst or src == dst:
        return []
    codes = _ROUTE_CARRIERS.get((src, dst))
    if not codes:
        return []
    return sorted(_CARRIER_NAMES.get(c, c) for c in codes)

# Route-connectivity (in+out degree) per airport, from the existence graph. Used to
# pick a city's PRIMARY GATEWAY (busiest in-radius airport) rather than the merely
# geometrically-nearest field — so London->LHR not LCY, Istanbul->IST not SAW, and
# flagship nonstops (LHR-JFK) aren't mislabelled 'connection'. Static -> var-0.
_AIRPORT_DEGREE: dict[str, int] = {}
for _s, _d in _ROUTE_SET:
    _AIRPORT_DEGREE[_s] = _AIRPORT_DEGREE.get(_s, 0) + 1
    _AIRPORT_DEGREE[_d] = _AIRPORT_DEGREE.get(_d, 0) + 1

# Precomputed adjacency for O(1) one-hop-hub lookup. Populated once at import
# time from the same _ROUTE_SET. Both directions are DIRECTED (per OpenFlights).
# var-0: static, no iteration over dicts on output paths, hub selection uses sorted().
_OUTBOUND: dict[str, set[str]] = {}   # src IATA → set of directly reachable IATAs
_INBOUND: dict[str, set[str]] = {}    # dst IATA → set of IATAs that fly directly to it
for _s, _d in _ROUTE_SET:
    _OUTBOUND.setdefault(_s, set()).add(_d)
    _INBOUND.setdefault(_d, set()).add(_s)


def nearest_airport(
    city: str | None = None,
    coords: tuple[float, float] | None = None,
) -> tuple[str, float] | None:
    """Return (IATA, distance_km) for the nearest air-network airport to a city or
    explicit (lat, lon), or None if no airport lies within _NEAREST_AIRPORT_MAX_KM
    (or the location cannot be resolved).

    Resolution order for the reference point: explicit ``coords`` wins; otherwise
    the city centroid from city_coords.json. var-0: candidates are scanned in
    sorted-IATA order so ties resolve deterministically; distance compared to a
    6-decimal-rounded value to avoid last-ULP flapping."""
    if coords is not None:
        ref = (float(coords[0]), float(coords[1]))
    elif city is not None:
        ref = _CITY_COORDS.get(_canonical_city(city))
        if ref is None:
            return None
    else:
        return None

    # Collect ALL airports within radius, then pick the PRIMARY GATEWAY = the most
    # route-connected one (highest degree), not the merely-closest field. Tiebreak by
    # distance then sorted IATA → deterministic / var-0. This stops a metro from
    # mapping to a minor field (London->LCY, Istanbul->SAW) and under-claiming real
    # hub nonstops; it never invents a route, so phantom-safety is preserved.
    candidates: list[tuple[str, float]] = []
    for iata in sorted(_AIRPORTS):
        ap = _AIRPORTS[iata]
        try:
            d = round(_haversine_km(ref, (ap["lat"], ap["lon"])), 6)
        except (KeyError, TypeError, ValueError):
            continue
        if d <= _NEAREST_AIRPORT_MAX_KM:
            candidates.append((iata, d))
    if not candidates:
        return None
    best_iata, best_km = min(
        candidates, key=lambda c: (-_AIRPORT_DEGREE.get(c[0], 0), c[1], c[0])
    )
    return best_iata, best_km


def air_route_exists(iata_a: str, iata_b: str) -> bool:
    """True iff a REAL nonstop route exists between the two airports in the
    OpenFlights graph (either direction → an air link exists). Carrier-agnostic.
    NEVER invents a route: returns False for any pair not in the graph."""
    a = (iata_a or "").strip().upper()
    b = (iata_b or "").strip().upper()
    if not a or not b or a == b:
        return False
    return (a, b) in _ROUTE_SET or (b, a) in _ROUTE_SET


# A one-hop hub is only NAMED if it is roughly EN ROUTE: its total detour
# (src→hub→dst) may not exceed this multiple of the direct great-circle distance.
# A sensible connecting hub adds a modest triangle (≤~1.5x); a gross backtrack
# (Bergen→Gothenburg "via Málaga", Nagoya→Komatsu "via Sapporo") is 5–10x and is
# rejected → the caller honestly labels it 'connection (routing assumed)' instead of
# asserting an absurd path. Connections only arise for direct hops >450 km (shorter
# hops go overland), so 2.5x leaves ample room for any real en-route hub.
_HUB_MAX_DETOUR_RATIO = 2.5


def find_one_hop_hub(iata_src: str, iata_dst: str) -> str | None:
    """Return the most EN-ROUTE IATA hub H such that BOTH src→H and H→dst exist as
    REAL nonstop routes in the OpenFlights graph, or None if no such single hub exists
    (or every shared hub is a gross backtrack — see _HUB_MAX_DETOUR_RATIO).

    This is a VERIFIED one-hop connection check — a hub is only returned if BOTH legs
    (src→hub AND hub→dst) genuinely exist in the route graph. NEVER invents a
    connection: returns None for any pair with no shared hub.

    var-0: candidates are the intersection of _OUTBOUND[src] and _INBOUND[dst], both
    computed from the static route set; the winning hub MINIMISES the great-circle
    detour src→hub→dst (rounded km, lexicographic IATA tiebreak → deterministic).
    Falls back to lexicographic min only when an endpoint airport lacks coordinates."""
    src = (iata_src or "").strip().upper()
    dst = (iata_dst or "").strip().upper()
    if not src or not dst or src == dst:
        return None
    # O(1) lookup via precomputed adjacency tables.
    src_out = _OUTBOUND.get(src, frozenset())
    dst_in  = _INBOUND.get(dst, frozenset())
    common  = src_out & dst_in
    if not common:
        return None
    sp = _AIRPORTS.get(src)
    dp = _AIRPORTS.get(dst)
    if not sp or "lat" not in sp or not dp or "lat" not in dp:
        return min(common)   # no coords → keep the old deterministic pick
    s_ll = (sp["lat"], sp["lon"])
    d_ll = (dp["lat"], dp["lon"])
    direct = _haversine_km(s_ll, d_ll)
    best: tuple[float, str] | None = None   # (detour_km, iata)
    for h in common:
        hp = _AIRPORTS.get(h)
        if not hp or "lat" not in hp:
            continue
        h_ll = (hp["lat"], hp["lon"])
        detour = round(_haversine_km(s_ll, h_ll) + _haversine_km(h_ll, d_ll), 6)
        if best is None or (detour, h) < best:
            best = (detour, h)
    if best is None:
        return min(common)
    # Reject a gross backtrack: name no hub → caller labels 'routing assumed'.
    if best[0] > _HUB_MAX_DETOUR_RATIO * direct:
        return None
    return best[1]


def _air_transfer(from_city: str, to_city: str) -> tuple[int, str, dict] | None:
    """Airport-graph flight estimate for an inter-city pair. Returns
    (minutes, mode, extra) where mode is always 'flight' and ``extra`` carries
    the air-network labels (from_iata, to_iata, air_option, [via_iata],
    [operators, operators_note]).

    On a 'nonstop' edge ``extra`` also carries 'operators' = sorted airline NAMES
    known to fly the exact directed route (OpenFlights ~2014 vintage) plus an
    'operators_note' vintage caveat. PHANTOM-SAFE: a static real fact, never a
    live/guaranteed flight; no schedules/times/fares/flight-numbers.

    ``air_option`` values (PHANTOM-SAFE — never an invented nonstop):
      'nonstop'               — a REAL direct route exists in the graph.
      'connection (via hub)'  — no direct route, but a VERIFIED one-hop path
                                 src→hub→dst exists; hub IATA in via_iata.
      'connection (routing assumed)' — no direct route AND no verified one-hop
                                 hub found. Multi-leg connection is the only
                                 realistic option, but the specific path is NOT
                                 verified — routing is an assumption. Treat as
                                 advisory only; do not assert a confirmed itinerary.

    Distance is computed from REAL airport coordinates.

    Returns None when EITHER city has no mapped airport → caller falls back to the
    existing city-centroid haversine behavior unchanged (var-0 safe)."""
    na = nearest_airport(from_city)
    nb = nearest_airport(to_city)
    if na is None or nb is None:
        return None
    src, dst = na[0], nb[0]
    if src == dst:
        # Both cities share one gateway airport → no air leg; let haversine handle it.
        return None
    a = (_AIRPORTS[src]["lat"], _AIRPORTS[src]["lon"])
    b = (_AIRPORTS[dst]["lat"], _AIRPORTS[dst]["lon"])
    direct_km = _haversine_km(a, b)
    # Time is computed from the ACTUAL routed distance + a layover for any non-nonstop hop —
    # a via-hub connection is NOT a straight line, so timing it on direct distance (the old bug)
    # made a connecting flight look as fast as a nonstop. A routing-assumed connection also pays
    # the layover so it is never modelled cheaper than a verified one (honesty monotonicity).
    if air_route_exists(src, dst):
        air_option = "nonstop"
        route_km = direct_km
        layover = 0
        extra: dict = {"from_iata": src, "to_iata": dst, "air_option": air_option}
        # Surface the REAL operating-carrier set for THIS exact directed route
        # (src->dst). Static fact, vintage-caveated, NOT a live/guaranteed flight.
        operators = route_operators(src, dst)
        if operators:
            extra["operators"] = operators
            extra["operators_note"] = _OPERATORS_CAVEAT
    else:
        hub = find_one_hop_hub(src, dst)
        layover = _LAYOVER_PENALTY_MIN
        if hub is not None:
            air_option = "connection (via hub)"
            hub_ap = _AIRPORTS.get(hub)
            if hub_ap and "lat" in hub_ap:
                hub_ll = (hub_ap["lat"], hub_ap["lon"])
                route_km = _haversine_km(a, hub_ll) + _haversine_km(hub_ll, b)
            else:
                route_km = direct_km
            extra = {"from_iata": src, "to_iata": dst, "air_option": air_option,
                     "via_iata": hub}
        else:
            # No verified one-hop path: label honestly as a routing assumption.
            # Do NOT assert a confirmed itinerary — the exact connection path is
            # unknown (may require >1 stop or indirect routing not in the graph).
            air_option = "connection (routing assumed)"
            route_km = direct_km
            extra = {"from_iata": src, "to_iata": dst, "air_option": air_option}
    raw = _FALLBACK_OVERHEAD_MIN + route_km / _FALLBACK_CRUISE_KMH * 60.0 + layover
    minutes = max(int(round(raw / 5.0)) * 5, 30)     # 5-min buckets → ULP-stable
    return minutes, "flight", extra


# ---------------------------------------------------------------------------
# FERRY / SEA-CROSSING layer (build #34). Hand-seeded inter-island / coastal
# ferry connectivity so a leg that crosses water (island hop) resolves to a
# REAL labelled ferry edge or an HONEST unverified gap — NEVER the phantom
# mode="flight" that _fallback_transfer would otherwise fabricate across water
# (no airline flies Bali->Gili). Mirrors the air-network posture exactly:
#
#   PHANTOM-SAFE: a pair is mode="ferry" iff it really exists in the seed; an
#   unseeded sea crossing whose endpoint is a known ferry/island city is flagged
#   feasible=False mode="water_crossing_unverified", never silently assumed.
#   Only ROUTE EXISTENCE + operator NAMES + an advisory crossing estimate are
#   claimed — NEVER live timetables/departure-times/fares (consistent with the
#   module docstring's NEVER-fetch-external contract).
#
#   var-0: static committed JSON loaded once at import; symmetric frozenset-keyed
#   lookups; integer minutes; no wall-clock / random / dict-iteration on the
#   output path. Missing/corrupt ferry_network.json -> empty containers (every
#   sea crossing then flags unverified = fail-conservative, the correct default).
#
#   APPEND-ONLY: consulted strictly AFTER the seed tables AND _air_transfer miss,
#   and gated to genuine ferry/island cities, so every existing road/flight edge
#   keeps its exact current shape and value (no ferry_* keys appear on them).
# ---------------------------------------------------------------------------

# Advisory crossing estimate (minutes) used when a seeded ferry route omits an
# explicit approx_minutes. Seeded constant, never live. Conservative single value.
_FERRY_DEFAULT_MIN = 90


def _load_ferry_network() -> tuple[
    dict[frozenset, tuple[int, tuple[str, ...]]],
    dict[str, str],
    frozenset[str],
]:
    """Load from the committed ferry_network.json:
      - ferry_routes  {frozenset({src,dst}): (approx_minutes, (operator_codes,))}
                       SYMMETRIC (frozenset key) — both directions sail.
      - operator_names {CODE: "Operator Name"}
      - island_cities  frozenset of every normalised city that appears in any
                       ferry route (known sea-crossing endpoints).

    Each route row is [src_city, dst_city, [op_code, ...], approx_minutes?];
    missing approx_minutes -> _FERRY_DEFAULT_MIN. City names are normalised
    (lowercased/trimmed) to match how legs carry `city`. Returns empty
    containers if the file is absent/corrupt (caller then flags every sea
    crossing as unverified = fail-conservative).

    PHANTOM-SAFE: operator codes/names are a static real fact (operators known to
    run the corridor, hand-seeded vintage). No schedules/times/fares are loaded —
    those were never written to the file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ferry_network.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        operator_names = {str(k): str(v) for k, v in (data.get("operators", {}) or {}).items()}
        ferry_routes: dict[frozenset, tuple[int, tuple[str, ...]]] = {}
        island_cities: set[str] = set()
        for row in data.get("routes", []) or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            src = _normalise(str(row[0]))
            dst = _normalise(str(row[1]))
            if not src or not dst or src == dst:
                continue
            codes = row[2] if len(row) > 2 and isinstance(row[2], list) else []
            # Sort codes defensively so output is deterministic regardless of seed order.
            code_tuple = tuple(sorted(str(c) for c in codes))
            try:
                minutes = int(row[3]) if len(row) > 3 and row[3] is not None else _FERRY_DEFAULT_MIN
            except (TypeError, ValueError):
                minutes = _FERRY_DEFAULT_MIN
            ferry_routes[frozenset({src, dst})] = (max(minutes, 5), code_tuple)
            island_cities.add(src)
            island_cities.add(dst)
        return ferry_routes, operator_names, frozenset(island_cities)
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return {}, {}, frozenset()


_FERRY_ROUTES, _FERRY_OPERATOR_NAMES, _FERRY_ISLAND_CITIES = _load_ferry_network()

# Vintage caveat surfaced on every ferry edge. The operator SET is a REAL static
# fact (operators known to run the corridor, hand-seeded vintage), NOT a live or
# guaranteed sailing — schedules/times/fares/frequency are intentionally absent.
_FERRY_CAVEAT = (
    "operators known to run this ferry corridor (hand-seeded vintage) — "
    "confirm current sailings/frequency/fares at booking; NOT a live or "
    "guaranteed sailing; crossing time is advisory only"
)


def ferry_operators(city_a: str, city_b: str) -> list[str]:
    """Sorted list of ferry-operator NAMES known to run the corridor between the
    two cities (symmetric), or [] if no seeded route exists. Codes with no
    name-table entry fall back to the bare code so an operator is never silently
    dropped. var-0: codes stored sorted; names sorted again here."""
    a = _normalise(city_a)
    b = _normalise(city_b)
    if not a or not b or a == b:
        return []
    entry = _FERRY_ROUTES.get(frozenset({a, b}))
    if entry is None:
        return []
    return sorted(_FERRY_OPERATOR_NAMES.get(c, c) for c in entry[1])


def _ferry_transfer(from_city: str, to_city: str) -> tuple[int, str, dict] | None:
    """Seeded ferry estimate for a sea-crossing pair. Returns
    (approx_minutes, "ferry", extra) where extra carries ferry_operators (sorted
    names, possibly empty), ferry_note (vintage caveat), and crossing ("city -> city").
    Returns None when the pair has no seeded ferry route -> caller decides between
    the unverified flag and the unchanged _fallback_transfer behaviour.

    PHANTOM-SAFE: mode is "ferry" iff the route really exists in the seed.
    var-0: frozenset-keyed lookup, integer minutes, no float/clock/random."""
    a = _normalise(from_city)
    b = _normalise(to_city)
    if not a or not b or a == b:
        return None
    entry = _FERRY_ROUTES.get(frozenset({a, b}))
    if entry is None:
        return None
    minutes = entry[0]
    extra: dict = {
        "ferry_operators": ferry_operators(from_city, to_city),
        "ferry_note": _FERRY_CAVEAT,
        "crossing": f"{a} -> {b}",
    }
    return minutes, "ferry", extra


def _is_water_crossing_unverified(from_city: str, to_city: str) -> bool:
    """True iff at least one endpoint is a KNOWN ferry/island city (appears in the
    ferry seed) AND the exact pair is NOT seeded as a ferry route. This is the
    HONESTY guard: a genuine sea-crossing endpoint with no known ferry must be
    surfaced as an unverified gap, never papered over by the haversine flight
    fallback (no airline flies the crossing). Distinct cities only.

    PHANTOM-SAFE / fail-conservative: never assumes a crossing is feasible."""
    a = _normalise(from_city)
    b = _normalise(to_city)
    if not a or not b or a == b:
        return False
    if frozenset({a, b}) in _FERRY_ROUTES:
        return False
    return a in _FERRY_ISLAND_CITIES or b in _FERRY_ISLAND_CITIES


def _compute_edge(
    leg_k: dict,
    leg_k1: dict,
    cancelled_transfers: set[tuple[str, str]] | None = None,
    persona: str = "default",
) -> dict[str, Any]:
    """
    Compute a single transfer edge between two consecutive legs.

    Returns a typed edge dict.

    ``cancelled_transfers`` is an optional set of directed (from_city, to_city)
    pairs (lower-cased) that the World Simulator has marked cancelled (a flight
    cancellation / missed connection, §12.1). A cancelled edge is FEASIBLE=False
    with mode="flight_cancelled" so the cascade-recovery loop re-routes around it.
    Default None preserves the legacy behaviour (no transfer faults) so existing
    tests/consumers are unchanged.
    """
    from_leg   = leg_k.get("leg_id", "")
    to_leg     = leg_k1.get("leg_id", "")
    from_city  = leg_k.get("city", "")
    to_city    = leg_k1.get("city", "")
    from_area  = leg_k.get("area", "")
    to_area    = leg_k1.get("area", "")
    checkout   = leg_k.get("checkout", "")
    checkin    = leg_k1.get("checkin", "")

    # Empty/whitespace city on either leg → infeasible edge (conservative unknown).
    # This guard MUST come before the cancelled-transfers check and the
    # fc==tc same-city branch in _lookup_transfer so that a pair of legs with
    # no city does NOT silently collapse to a same-area/road-30 FEASIBLE edge.
    fc_raw = from_city.strip()
    tc_raw = to_city.strip()
    if not fc_raw or not tc_raw:
        missing = []
        if not fc_raw:
            missing.append(f"from={from_leg!r}")
        if not tc_raw:
            missing.append(f"to={to_leg!r}")
        return {
            "from_leg":         from_leg,
            "to_leg":           to_leg,
            "from_city":        from_city,
            "to_city":          to_city,
            "from_area":        from_area,
            "to_area":          to_area,
            "feasible":         False,
            # #70: an EMPTY/missing city is a malformed leg (genuine problem), NOT a real
            # city-pair we merely lack data for — keep it genuine-infeasible (it also fails
            # upstream at accommodation). Only REAL unseeded routes are `unverified`.
            "mode":             "unknown",
            "transfer_minutes": -1,
            "reason":           f"missing city on leg: {', '.join(missing)}",
        }

    # Exogenous flight-cancellation fault (cascade): the merchant/World Simulator
    # has cancelled this directed edge. Visible-as-data; the Transport agent owns
    # the re-route. Checked first so a cancellation always wins over routing.
    if cancelled_transfers:
        if (_normalise(from_city), _normalise(to_city)) in cancelled_transfers:
            # Look up the nominal minutes/mode for context (best-effort).
            try:
                minutes, mode = _lookup_transfer(from_city, to_city, from_area, to_area)
            except KeyError:
                minutes, mode = -1, "flight"
            return {
                "from_leg":         from_leg,
                "to_leg":           to_leg,
                "from_city":        from_city,
                "to_city":          to_city,
                "from_area":        from_area,
                "to_area":          to_area,
                "feasible":         False,
                "mode":             "flight_cancelled",
                "transfer_minutes": minutes,
                "reason":           (
                    f"flight/transfer cancelled: {from_city!r}→{to_city!r} is no longer "
                    f"available (World Simulator fault). Re-route required."
                ),
                "cancelled":        True,
            }

    # Air-network labels populated only when the air layer routes this edge
    # (both cities map to a real airport). Absent otherwise → unchanged shape.
    air_extra: dict | None = None
    # Ferry labels populated only when the ferry layer routes this edge (seeded
    # sea crossing). Absent otherwise -> unchanged shape (append-only).
    ferry_extra: dict | None = None

    try:
        minutes, mode = _lookup_transfer(from_city, to_city, from_area, to_area)
    except KeyError:
        # No seeded value. Build #31: try the REAL air-network layer FIRST — when
        # BOTH cities map to an airport, use real airport coords for the distance
        # and label the air option nonstop/connection from the route-existence
        # graph (PHANTOM-SAFE: never an invented nonstop). When EITHER city has no
        # mapped airport, fall back to the existing city-centroid haversine
        # estimate UNCHANGED (var-0 byte-identical for those legs).
        _air = _air_transfer(from_city, to_city)
        # OVERLAND-FIRST for a short inter-city hop: even when both cities map to a real
        # airport, flying a sub-_GROUND_TRANSFER_MAX_KM same-landmass hop (e.g. Gifu→Kanazawa
        # ~130 km on Honshu) is nonsensical — rail/HSR dominates, and with no nonstop the air
        # graph can route through an absurd hub (NGO→CTS/Sapporo→KMQ). Drop the air option so
        # the overland (rail) estimate from _fallback_transfer wins below. A water crossing
        # (island hop) keeps its flight/ferry option. HONESTY: no phantom flight for a hop any
        # traveller would take by train. Seeded inter-city flights are unaffected (they resolve
        # in _lookup_transfer and never reach this branch).
        if _air is not None and _short_overland_hop(from_city, to_city, persona):
            _air = None
        if _air is not None:
            minutes, mode, air_extra = _air
        elif (_ferry := _ferry_transfer(from_city, to_city)) is not None:
            # Seeded inter-island / coastal ferry: a REAL labelled feasible edge.
            # Consulted AFTER the air layer (an island WITH an airport keeps its
            # flight option) and BEFORE _fallback_transfer (so the phantom
            # haversine "flight" across water never wins). PHANTOM-SAFE.
            minutes, mode, ferry_extra = _ferry
        elif _is_water_crossing_unverified(from_city, to_city):
            # Genuine sea-crossing endpoint (known ferry/island city) with NO
            # seeded ferry route and no air route: surface an HONEST gap instead
            # of fabricating a flight. feasible=False so it lands in
            # infeasible_edges and the orchestrator's conservative block handles
            # it (reorder -> _cannot_satisfy_result). HONESTY / fail-conservative.
            return {
                "from_leg":         from_leg,
                "to_leg":           to_leg,
                "from_city":        from_city,
                "to_city":          to_city,
                "from_area":        from_area,
                "to_area":          to_area,
                "feasible":         False,
                "unverified":       True,  # #70: unverified crossing → advisory, not hard block
                "mode":             "water_crossing_unverified",
                "transfer_minutes": -1,
                "reason":           (
                    f"unverified water crossing: {from_city!r} -> {to_city!r} "
                    f"appears to require a ferry/sea crossing but no known ferry "
                    f"route is seeded and no flight connects them. Not assumed "
                    f"feasible — confirm a sailing before relying on this leg."
                ),
            }
        else:
            _fb = _fallback_transfer(from_city, to_city, persona)
            if _fb is None:
                # Still unknown (no coords) — keep the conservative infeasible behavior.
                return {
                    "from_leg":         from_leg,
                    "to_leg":           to_leg,
                    "from_city":        from_city,
                    "to_city":          to_city,
                    "from_area":        from_area,
                    "to_area":          to_area,
                    "feasible":         False,
                    # #70: NO DATA ≠ infeasible. An unseeded city pair is UNVERIFIED (we lack
                    # the transfer time), not proof the trip is impossible. Flagged so the gates
                    # treat it as an honest advisory (like compliance/health unknown→FLAG),
                    # never a hard cannot_satisfy.
                    "unverified":       True,
                    "mode":             "unknown",
                    "transfer_minutes": -1,
                    "reason":           f"No seeded transfer time for {from_city!r} → {to_city!r}",
                }
            # Coordinate fallback succeeded → treat as a normal (feasible) flight edge.
            minutes, mode = _fb

    feasible = True
    reason: str | None = None
    long_drive = False
    distance_km: int | None = None

    fc_norm = _normalise(from_city)
    tc_norm = _normalise(to_city)

    # Same-day inter-city feasibility check (flights only)
    if fc_norm != tc_norm and mode == "flight":
        if checkout == checkin and minutes > _SAME_DAY_INTERCITY_THRESHOLD:
            feasible = False
            reason = (
                f"same-day inter-city transfer implausible: "
                f"{from_city!r}→{to_city!r} requires ~{minutes} min "
                f"but checkin and checkout are both {checkout!r}"
            )

    # WA inter-town ROAD long-drive advisory (still feasible — add a buffer).
    wa_key = frozenset({fc_norm, tc_norm})
    if wa_key in _WA_ROAD_KM:
        distance_km = _WA_ROAD_KM[wa_key]
        if distance_km >= _WA_LONG_DRIVE_KM:
            long_drive = True
            reason = (
                f"long drive: {from_city!r}→{to_city!r} is {distance_km} km "
                f"(~{minutes} min). Feasible, but add a rest/fuel buffer and "
                f"avoid stacking it against a same-day check-in."
            )

    # Generic long-transfer advisory: any mode, any date relationship.
    # Fires when transfer_minutes exceeds the threshold AND the edge is still
    # feasible AND no more specific reason has been set (WA long-drive or
    # infeasibility already produces a reason). This catches transcontinental
    # fallback flights (e.g. Sydney→London ~1365 min) so the society can
    # buffer multi-hour connections regardless of mode or exact date collision.
    if feasible and minutes > _LONG_TRANSFER_ADVISORY_MIN and reason is None:
        reason = (
            f"long transfer: {from_city!r}→{to_city!r} requires ~{minutes} min "
            f"({mode}). Add a buffer day between these legs."
        )

    edge = {
        "from_leg":         from_leg,
        "to_leg":           to_leg,
        "from_city":        from_city,
        "to_city":          to_city,
        "from_area":        from_area,
        "to_area":          to_area,
        "feasible":         feasible,
        "mode":             mode,
        "transfer_minutes": minutes,
        "reason":           reason,
    }
    # Additive fields for road legs (absent for legacy Bali/flight edges so
    # existing tests/consumers see the unchanged shape).
    if distance_km is not None:
        edge["distance_km"] = distance_km
        edge["long_drive"] = long_drive
    # Additive air-network labels (only when the air layer routed this edge).
    # PHANTOM-SAFE: air_option is 'nonstop' (real direct), 'connection (via hub)'
    # (verified one-hop), or 'connection (routing assumed)' (no verified path).
    # via_iata present only when a hub was verified. No carrier/flight number/fare.
    if air_extra is not None:
        edge["from_iata"] = air_extra["from_iata"]
        edge["to_iata"] = air_extra["to_iata"]
        edge["air_option"] = air_extra["air_option"]
        if "via_iata" in air_extra:
            edge["via_iata"] = air_extra["via_iata"]
        # Operating-carrier names for a nonstop directed route (vintage-caveated,
        # static fact). Present only when the exact route resolved to ≥1 carrier;
        # NEVER schedules/times/fares/flight-numbers (those are not in the data).
        if "operators" in air_extra:
            edge["operators"] = air_extra["operators"]
            edge["operators_note"] = air_extra["operators_note"]
    # Additive ferry labels (only when the ferry layer routed this edge). PHANTOM-
    # SAFE: mode=='ferry' means the route really exists in the seed; ferry_note
    # carries the vintage caveat; ferry_operators may be empty but is always a
    # static real fact. No schedules/times/fares are ever attached.
    if ferry_extra is not None:
        edge["ferry_operators"] = ferry_extra["ferry_operators"]
        edge["ferry_note"] = ferry_extra["ferry_note"]
        edge["crossing"] = ferry_extra["crossing"]
    # Phase 2: ADDITIVE rail tier on rail edges ("hsr" | "conventional"). Never a new `mode` string
    # (the orchestrator's airport-hop timing keys on mode=="flight"); purely informational so the
    # itinerary can show "high-speed rail". Absent on non-rail edges → append-only / var-0 safe.
    if mode == "rail" and feasible:
        edge["rail_tier"] = _rail_tier(from_city, to_city)
    # Phase 3a: ADDITIVE overnight-sleeper advisory on a feasible edge of a sleeper corridor —
    # regardless of the primary mode (a medium-long pair that otherwise flies can also be done as an
    # overnight sleeper that saves a hotel night). Advisory only; never mutates mode/minutes/budget.
    if feasible:
        ov = _overnight_advisory(from_city, to_city)
        if ov is not None:
            edge["overnight_rail"] = ov
    return edge


# ---------------------------------------------------------------------------
# Re-sequencing: deterministic greedy nearest-neighbour with city clustering
# ---------------------------------------------------------------------------

def _suggest_reordering(legs: list[dict]) -> list[str] | None:
    """
    Produce a suggested leg ordering that minimises total transfer time.

    Algorithm:
      1. Cluster same-city legs together.
      2. Within each cluster: greedy nearest-neighbour on intra-area transfer time.
      3. Order clusters by ascending inter-city transfer time (greedy chain).

    Returns a list of leg_ids in the suggested order, or None if no reordering
    makes sense (0 or 1 legs).

    NEVER changes dates or invents transfers — only re-orders leg_ids.
    """
    if len(legs) <= 1:
        return None

    # Step 1: Group by city (preserve original insertion order within groups)
    cities: dict[str, list[dict]] = {}
    for leg in legs:
        city = _normalise(leg.get("city", ""))
        cities.setdefault(city, []).append(leg)

    # Step 2: Within each city cluster, greedy nearest-neighbour on area transfer
    def _order_cluster(cluster: list[dict]) -> list[dict]:
        if len(cluster) <= 1:
            return list(cluster)
        remaining = list(cluster)
        # Start from the leg that has minimum total transfer to all others
        # (a simple centroid heuristic for the seed).
        def _total_transfer(leg: dict) -> int:
            total = 0
            for other in remaining:
                if other is leg:
                    continue
                try:
                    t, _ = _lookup_transfer(
                        leg.get("city", ""), other.get("city", ""),
                        leg.get("area", ""), other.get("area", ""),
                    )
                except KeyError:
                    t = 9999
                total += t
            return total

        start = min(remaining, key=_total_transfer)
        ordered = [start]
        remaining.remove(start)

        while remaining:
            last = ordered[-1]
            def _dist_to_last(leg: dict) -> int:
                try:
                    t, _ = _lookup_transfer(
                        last.get("city", ""), leg.get("city", ""),
                        last.get("area", ""), leg.get("area", ""),
                    )
                    return t
                except KeyError:
                    return 9999
            nearest = min(remaining, key=_dist_to_last)
            ordered.append(nearest)
            remaining.remove(nearest)

        return ordered

    ordered_clusters: dict[str, list[dict]] = {
        city: _order_cluster(cluster)
        for city, cluster in cities.items()
    }

    # Step 3: Order clusters by inter-city transfer time (greedy chain)
    city_list = list(ordered_clusters.keys())
    if len(city_list) == 1:
        # Single city — only return an order if it differs from the input
        ordered = ordered_clusters[city_list[0]]
        new_ids = [leg["leg_id"] for leg in ordered]
        orig_ids = [leg["leg_id"] for leg in legs]
        return new_ids if new_ids != orig_ids else None

    # Helper: inter-city distance estimate (minutes) for ordering purposes only.
    # Consults the seeded flight table first; falls back to the coordinate/haversine
    # estimate so that the ~437 expanded cities (outside the ~16-pair seed table)
    # produce real distances instead of the identical 9999 sentinel that previously
    # made the greedy chain a no-op for non-seeded pairs.
    def _inter_city_minutes(city_a: str, city_b: str) -> int:
        key = frozenset({city_a, city_b})
        seeded = _INTER_CITY_FLIGHTS.get(key)
        if seeded is not None:
            return seeded
        fb = _fallback_transfer(city_a, city_b)
        if fb is not None:
            return fb[0]
        return 9999  # No coords either — use sentinel as last resort

    # Pick a starting city: the one with the smallest total inter-city distance
    # to all other cities (greedy centroid heuristic).
    def _city_centrality(city: str) -> tuple[int, str]:
        total = sum(
            _inter_city_minutes(city, other)
            for other in city_list
            if other != city
        )
        return total, city  # stable tiebreak on city name (lexical)

    remaining_cities = list(city_list)
    start_city = min(remaining_cities, key=_city_centrality)
    city_order = [start_city]
    remaining_cities.remove(start_city)

    while remaining_cities:
        last_city = city_order[-1]
        def _inter_city_dist(c: str, _last: str = last_city) -> tuple[int, str]:
            return _inter_city_minutes(_last, c), c  # stable tiebreak on city name
        nearest_city = min(remaining_cities, key=_inter_city_dist)
        city_order.append(nearest_city)
        remaining_cities.remove(nearest_city)

    # Assemble final leg order
    result_legs: list[dict] = []
    for city in city_order:
        result_legs.extend(ordered_clusters[city])

    new_ids = [leg["leg_id"] for leg in result_legs]
    orig_ids = [leg["leg_id"] for leg in legs]
    return new_ids if new_ids != orig_ids else None


# ---------------------------------------------------------------------------
# Core feasibility check
# ---------------------------------------------------------------------------

def check_feasibility(
    legs: list[dict],
    cancelled_transfers: set[tuple[str, str]] | None = None,
    persona: str = "default",
) -> dict[str, Any]:
    """
    Run the full transport feasibility check on an ordered list of legs.

    Each leg must have: leg_id, city, area, checkin, checkout.

    ``cancelled_transfers`` is an optional set of directed (from_city, to_city)
    pairs (lower-cased) cancelled by the World Simulator (flight-cancellation
    fault, §12.1). Default None = no transfer faults (legacy behaviour).

    ``persona`` (Phase 3b) tunes the rail-vs-flight preference: "comfort" widens the overland
    rail-preference range (an elderly/comfort traveller takes the train further to avoid airports);
    "default" (the default) is byte-identical to the pre-persona behaviour (var-0). Unknown values
    fall back to default.

    Returns a typed TransportResult dict.
    """
    persona = persona if persona in _PERSONAS else "default"
    edges: list[dict[str, Any]] = []

    for k in range(len(legs) - 1):
        edge = _compute_edge(legs[k], legs[k + 1], cancelled_transfers, persona)
        edges.append(edge)

    # #70: split feasible=False edges into GENUINELY-infeasible vs UNVERIFIED (no-data).
    # An `unverified` edge (no seeded transfer time / unverified sea-crossing / missing city)
    # is absence-of-evidence, NOT evidence the trip is impossible — so it must NOT hard-block
    # (mirrors compliance/health/risk unknown→FLAG). `infeasible_edges` keeps only GENUINE
    # infeasibility (same-day-implausible / flight-cancelled) that legitimately rejects.
    unverified_edges = [e for e in edges if e.get("unverified")]
    infeasible_edges = [e for e in edges if not e["feasible"] and not e.get("unverified")]
    # Long-drive edges are FEASIBLE but flagged for buffering/sequencing (§12.2).
    long_drive_edges = [e for e in edges if e.get("long_drive")]
    # Cancelled edges are a subset of infeasible (flight-cancellation cascade).
    cancelled_edges = [e for e in edges if e.get("cancelled")]
    # Ferry edges are FEASIBLE labelled sea crossings (build #34). Additive subset,
    # surfaced so the itinerary render can show the ferry leg + operator caveat.
    ferry_edges = [e for e in edges if e.get("mode") == "ferry"]
    # Overnight-sleeper advisories (Phase 3a). Additive subset, surfaced so the itinerary render can
    # offer "overnight sleeper — saves ~1 hotel night" on the relevant legs. Advisory only.
    overnight_edges = [e for e in edges if e.get("overnight_rail")]

    # A reorder may still resolve a genuine OR unverified adjacency, so attempt it for either.
    if infeasible_edges or unverified_edges:
        suggested_reordering = _suggest_reordering(legs)
    else:
        suggested_reordering = None

    return {
        "edges":                edges,
        "infeasible_edges":     infeasible_edges,
        "unverified_edges":     unverified_edges,
        "long_drive_edges":     long_drive_edges,
        "cancelled_edges":      cancelled_edges,
        "ferry_edges":          ferry_edges,
        "overnight_edges":      overnight_edges,
        "suggested_reordering": suggested_reordering,
    }


# ---------------------------------------------------------------------------
# TransportAgent — A2A wrapper
# ---------------------------------------------------------------------------

class TransportAgent(A2AAgent):
    """
    Transport/Logistics feasibility specialist (Travel Guild M3b).

    Implements the ``transport.feasibility`` skill.

    Fully deterministic — no LLM, no DashScope, no live timetables.
    Uses a seeded transfer model for intra-Bali and inter-city transfers.

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
            "name": "transport-agent",
            "description": (
                "Transport/Logistics feasibility specialist — deterministic route checker "
                "with seeded transfer model (no LLM, no live timetables). "
                "Validates inter-leg transfers for Bali intra-island (road) and "
                "inter-city (flight) legs. Detects implausible same-day transfers. "
                "Produces a suggested re-ordering when infeasible edges are found. "
                "Implements A2A skill 'transport.feasibility'. "
                "Part of the Travel Guild multi-agent pipeline (Track 3, M3b)."
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
                    "id": "transport.feasibility",
                    "name": "Transport Feasibility Check",
                    "description": (
                        "Given an ordered list of trip legs "
                        "[{leg_id, city, area, checkin, checkout}], "
                        "check each consecutive transfer for feasibility using the "
                        "seeded transfer model. "
                        "Returns edges[], infeasible_edges[], and suggested_reordering "
                        "(list of leg_ids in optimal order, or null if no reordering needed). "
                        "Fully deterministic — no LLM, no external calls."
                    ),
                    "tags": [
                        "transport", "logistics", "feasibility", "routing",
                        "deterministic", "bali", "inter-city",
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
                                        "area":     {"type": "string"},
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
                            {"leg_id": "leg-0", "city": "bali",
                             "area": "ubud", "checkin": "2025-10-01",
                             "checkout": "2025-10-04"},
                            {"leg_id": "leg-1", "city": "bali",
                             "area": "seminyak", "checkin": "2025-10-04",
                             "checkout": "2025-10-08"},
                        ])
                    ],
                }
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("transport.feasibility", self._feasibility_handler)

    # ------------------------------------------------------------------
    # Skill handler
    # ------------------------------------------------------------------

    async def _feasibility_handler(self, message: dict, task: dict) -> dict:
        """
        transport.feasibility skill handler.

        Extracts the leg list, runs the deterministic feasibility check, and
        returns a typed TransportResult artifact.
        """
        payload = self._extract_payload(message)
        if payload is None:
            raise ValueError(
                "transport.feasibility requires a data part with a JSON list "
                "of legs [{leg_id, city, area, checkin, checkout}] "
                "or a dict {'legs': [...]}"
            )

        # Accept both list and wrapped dict
        if isinstance(payload, list):
            legs = payload
        elif isinstance(payload, dict):
            legs = payload.get("legs", [])
        else:
            raise ValueError(
                f"transport.feasibility payload must be a list or dict, got {type(payload).__name__}"
            )

        if not isinstance(legs, list):
            raise ValueError("legs must be a list")

        # Optional cancelled-transfer set (cascade flight-cancellation fault).
        # Accepted in the wrapped-dict form: {"legs": [...],
        #   "cancelled_transfers": [["gondar","lalibela"], ...]}.
        cancelled_transfers: set[tuple[str, str]] = set()
        if isinstance(payload, dict):
            for ct in payload.get("cancelled_transfers", []) or []:
                if isinstance(ct, (list, tuple)) and len(ct) == 2:
                    cancelled_transfers.add((str(ct[0]).strip().lower(), str(ct[1]).strip().lower()))
                elif isinstance(ct, dict) and "from" in ct and "to" in ct:
                    cancelled_transfers.add((str(ct["from"]).strip().lower(), str(ct["to"]).strip().lower()))

        # Optional persona ("comfort" widens the rail-preference range). Wrapped-dict form only;
        # absent / list payload → "default" (byte-identical legacy behaviour).
        persona = payload.get("persona", "default") if isinstance(payload, dict) else "default"
        result_data = check_feasibility(legs, cancelled_transfers or None, persona)

        logger.info(
            "transport.feasibility: %d legs → %d edges, %d infeasible, reorder=%s",
            len(legs),
            len(result_data["edges"]),
            len(result_data["infeasible_edges"]),
            result_data["suggested_reordering"] is not None,
        )

        return _new_artifact(
            name="transport.feasibility.result",
            parts=[_data_part(result_data)],
        )

    # ------------------------------------------------------------------
    # Input extraction helper (same pattern as CriticAgent / PlannerAgent)
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
    port = int(os.environ.get("PORT", 9104))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = TransportAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Transport agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
