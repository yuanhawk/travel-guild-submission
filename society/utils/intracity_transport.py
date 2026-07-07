"""intracity_transport.py — Dynamic intra-city transport hops (Feature 2).

Pure, deterministic module: NO LLM, NO clock, NO random. All minutes use the same
5-min bucket rounding as transport_agent._fallback_transfer (int(round(raw/5.0))*5).

Imports real helpers from agents.transport_agent:
  _haversine_km, _CITY_COORDS, _canonical_city, nearest_airport

Data contract:
  - Day-plan attractions and meals carry real lat/lon from the POI catalog.
  - The booked hotel has NO coordinate anywhere in the result (only an area string).
    → Use the city centroid from _CITY_COORDS, labelled honestly (coord_basis='city_centroid').
    → Never reverse-geocode the area string (fabrication risk).
  - Every hop: estimate=True, label ends ', est.'
  - Hotel-endpoint hops: coord_basis='city_centroid', label includes
    '(from city centre — exact hotel location unavailable)'.

Var-0: pure function of the immutable inputs — no wall-clock, no random, no net.
The double-run harness in live_archetype_e2e.py guards determinism via day_plans in _VAR0_FIELDS.
"""

from __future__ import annotations

import copy
from typing import Any

# ---------------------------------------------------------------------------
# Import stable helpers from transport_agent (not re-implemented here)
# ---------------------------------------------------------------------------
from agents.transport_agent import (
    _CITY_COORDS,
    _canonical_city,
    _haversine_km,
    nearest_airport,
)

# Meal traversal order — mirrors day_planner_agent._MEAL_SLOTS (must stay in sync)
_MEAL_SLOTS = ("breakfast", "lunch", "tea", "dinner", "supper")

# ---------------------------------------------------------------------------
# Speed / floor constants — advisory, deterministic
# ---------------------------------------------------------------------------
_WALK_KMH = 4.8       # walking speed
_METRO_KMH = 22.0     # metro/bus including access time
_TAXI_KMH = 28.0      # taxi including hailing/traffic overhead

_WALK_THRESHOLD_KM = 1.5   # < 1.5 km → walk
_METRO_THRESHOLD_KM = 8.0  # 1.5–8 km → metro/bus; ≥ 8 km → taxi

_WALK_FLOOR_MIN = 5
_METRO_FLOOR_MIN = 10
_TAXI_FLOOR_MIN = 10


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _bucket(raw_min: float) -> int:
    """Round raw minutes to the nearest 5-minute bucket (mirrors transport_agent)."""
    return int(round(raw_min / 5.0)) * 5


def _hop_mode_minutes(km: float) -> tuple[str, int]:
    """Return (mode, int_minutes) for a given straight-line distance in km.

    mode is one of 'walk', 'metro', 'taxi'. Minutes are bucketed to 5-min
    granularity with per-mode floors. DETERMINISTIC — no clock/random.
    """
    if km < _WALK_THRESHOLD_KM:
        raw = km / _WALK_KMH * 60.0
        return "walk", max(_bucket(raw), _WALK_FLOOR_MIN)
    if km < _METRO_THRESHOLD_KM:
        raw = km / _METRO_KMH * 60.0
        return "metro", max(_bucket(raw), _METRO_FLOOR_MIN)
    raw = km / _TAXI_KMH * 60.0
    return "taxi", max(_bucket(raw), _TAXI_FLOOR_MIN)


def _coord_of_item(item: dict) -> tuple[float, float] | None:
    """Return (lat, lon) from an attraction or meal dict, or None if either is missing/null."""
    if not isinstance(item, dict):
        return None
    lat = item.get("lat")
    lon = item.get("lon")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _hotel_coord(
    city: str, leg: dict
) -> tuple[tuple[float, float] | None, str]:
    """Return (coord_or_None, source_label) for the hotel endpoint.

    Preference order (future-proof):
      1. Explicit lat/lon on the leg dict (if ever added).
      2. City centroid from _CITY_COORDS (canonical lookup).
      3. None (city not in coords map).

    Hotels carry NO coord in the current schema → path 1 is always None today.
    Path 2 is the standard path; the label is always 'city_centroid' on this path.
    """
    # Future-proof: prefer explicit coords if ever present on the leg
    lat = leg.get("lat")
    lon = leg.get("lon")
    if lat is not None and lon is not None:
        try:
            return (float(lat), float(lon)), "hotel_explicit"
        except (TypeError, ValueError):
            pass

    # Standard path: city centroid
    key = _canonical_city(city) if city else ""
    coord = _CITY_COORDS.get(key)
    if coord is not None:
        return coord, "city_centroid"

    return None, "unknown"


def _make_hop(
    from_name: str,
    to_name: str,
    from_kind: str,
    to_kind: str,
    from_coord: tuple[float, float],
    to_coord: tuple[float, float],
    coord_basis: str,
    hotel_endpoint: bool,
) -> dict:
    """Build a single hop dict from two known coordinates."""
    km = _haversine_km(from_coord, to_coord)
    mode, minutes = _hop_mode_minutes(km)
    label = f"~{minutes} min, {mode}, est."
    if hotel_endpoint:
        label += " (from city centre — exact hotel location unavailable)"
    return {
        "from": from_name,
        "to": to_name,
        "from_kind": from_kind,
        "to_kind": to_kind,
        "distance_km": round(km, 1),  # display-only; minutes come only from bucketed int
        "mode": mode,
        "minutes": minutes,
        "label": label,
        "coord_basis": coord_basis,
        "estimate": True,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_intracity_hops(
    day: dict,
    leg: dict,
    city: str,
) -> list[dict]:
    """Build intra-city hops for a single day: hotel→attractions→meals→hotel.

    Traversal order:
      hotel → attractions (in existing list order) → meals (in _MEAL_SLOTS order) → hotel

    A hop is SKIPPED if either endpoint has no coordinate (honest gap, NO fabricated minutes).

    Returns a list of hop dicts (may be empty).
    """
    hotel_coord, hotel_basis = _hotel_coord(city, leg)
    hotel_name = leg.get("hotel_title") or leg.get("hotel_id") or city

    # Build ordered list of (name, kind, coord_or_None)
    waypoints: list[tuple[str, str, tuple[float, float] | None]] = []

    # Attractions in existing list order
    for att in (day.get("attractions") or []):
        if not isinstance(att, dict):
            continue
        name = att.get("name_en") or att.get("name") or "attraction"
        waypoints.append((name, "attraction", _coord_of_item(att)))

    # Meals in _MEAL_SLOTS order (skip None slots)
    meals = day.get("meals") or {}
    if not isinstance(meals, dict):
        meals = {}
    for slot in _MEAL_SLOTS:
        meal = meals.get(slot)
        if not isinstance(meal, dict):
            continue
        name = meal.get("name_en") or meal.get("name") or slot
        waypoints.append((name, f"meal_{slot}", _coord_of_item(meal)))

    if not waypoints:
        return []

    # Full sequence: hotel + waypoints + hotel
    full_seq: list[tuple[str, str, tuple[float, float] | None]] = (
        [(hotel_name, "hotel", hotel_coord)]
        + waypoints
        + [(hotel_name, "hotel", hotel_coord)]
    )

    hops: list[dict] = []
    for i in range(len(full_seq) - 1):
        from_name, from_kind, from_coord = full_seq[i]
        to_name, to_kind, to_coord = full_seq[i + 1]

        # Skip hop if either endpoint has no coordinate (honest gap)
        if from_coord is None or to_coord is None:
            continue

        # Determine coord_basis and whether hotel is an endpoint
        is_hotel_hop = from_kind == "hotel" or to_kind == "hotel"
        if is_hotel_hop:
            basis = hotel_basis if hotel_basis != "unknown" else "city_centroid"
        else:
            basis = "poi_coords"

        hops.append(_make_hop(
            from_name=from_name,
            to_name=to_name,
            from_kind=from_kind,
            to_kind=to_kind,
            from_coord=from_coord,
            to_coord=to_coord,
            coord_basis=basis,
            hotel_endpoint=is_hotel_hop,
        ))

    return hops


# ---------------------------------------------------------------------------
# Inline item transport gaps (mealAnchoredTimeline order)
# ---------------------------------------------------------------------------

def _has_display_name(item: dict) -> bool:
    """True if the item has a non-empty displayable name.

    Mirrors the frontend ``displayName`` filter in itinerary.ts:
    ``(x?.name_en || x?.name) ?? ''``, trimmed.
    """
    name = item.get("name_en") or item.get("name")
    return bool(name and str(name).strip())


def _timeline_for_gaps(day: dict) -> list[dict]:
    """Build the day timeline in mealAnchoredTimeline order.

    Mirrors itinerary.ts ``mealAnchoredTimeline`` exactly:
      breakfast → morning attractions (first ⌈N/2⌉) → lunch →
      afternoon attractions (remaining) → tea → dinner → supper

    Only attractions with a displayable name are included (mirrors the
    ``displayName`` filter in the frontend — nameless entries are skipped,
    same as the frontend's ``att.filter(({a}) => displayName(a))``).

    Returns a list of raw item dicts (attractions + meals) in timeline order.
    """
    raw_atts = day.get("attractions") or []
    named_atts = [a for a in raw_atts if isinstance(a, dict) and _has_display_name(a)]
    # math.ceil(n / 2) without importing math: (n + 1) // 2
    mid = (len(named_atts) + 1) // 2

    meals = day.get("meals") or {}
    if not isinstance(meals, dict):
        meals = {}

    timeline: list[dict] = []

    def _push_meal(slot: str) -> None:
        m = meals.get(slot)
        if isinstance(m, dict) and _has_display_name(m):
            timeline.append(m)

    _push_meal("breakfast")
    timeline.extend(named_atts[:mid])
    _push_meal("lunch")
    timeline.extend(named_atts[mid:])
    _push_meal("tea")
    _push_meal("dinner")
    _push_meal("supper")

    return timeline


def build_item_transport_gaps(
    day: dict,
    leg: dict,
    city: str,
) -> list[dict | None]:
    """Build inline transport gaps for consecutive timeline items.

    Reconstructs the day timeline in mealAnchoredTimeline order (see
    ``_timeline_for_gaps``) and computes a gap for each consecutive pair
    using the existing helpers: ``_hop_mode_minutes``, ``_haversine_km``,
    and ``_coord_of_item``.

    Returns a list of N−1 entries where N = len(timeline items):
      ``{mode, minutes, estimate: True}`` — both endpoints have coordinates
      ``None``                             — either endpoint lacks coordinates
                                             (honest suppression, no fabrication)

    var-0 contract:
      - Pure function of committed POI coords only.
      - Integer 5-min bucketing via ``_bucket`` (same as the rest of this module).
      - NO clock, NO RNG, NO network, NO import-time IO.
      - Byte-identical output for byte-identical input.

    ``leg`` and ``city`` are accepted for API symmetry with the other public
    functions but are NOT used — item gaps are purely POI-to-POI (no hotel
    endpoint), so no centroid lookup is needed.
    """
    items = _timeline_for_gaps(day)
    n = len(items)
    if n < 2:
        return []

    gaps: list[dict | None] = []
    for i in range(n - 1):
        coord_a = _coord_of_item(items[i])
        coord_b = _coord_of_item(items[i + 1])
        if coord_a is None or coord_b is None:
            gaps.append(None)
        else:
            km = _haversine_km(coord_a, coord_b)
            mode, minutes = _hop_mode_minutes(km)
            gaps.append({"mode": mode, "minutes": minutes, "estimate": True})

    return gaps


def build_transfer_hops(
    leg: dict,
    city: str,
) -> list[dict]:
    """Build airport↔hotel transfer hops for a leg.

    Returns [] if no airport maps to this city or no hotel coord is available.
    Both outbound (hotel→airport) and inbound (airport→hotel) are emitted — the
    UI can filter/display whichever it needs (checkin = inbound, checkout = outbound).

    coord_basis: 'airport_real+hotel_centroid' (airport has real coords from OpenFlights).
    """
    ap = nearest_airport(city)
    if ap is None:
        return []

    iata, dist_km = ap   # dist_km = REAL city-centroid → OpenFlights-airport haversine

    hotel_coord, _ = _hotel_coord(city, leg)
    if hotel_coord is None:
        return []

    hotel_name = leg.get("hotel_title") or leg.get("hotel_id") or city

    # The hotel resolves to the city centroid (no per-hotel coord exists in the catalog), and
    # nearest_airport already returns the REAL centroid→airport distance — so that distance IS the
    # honest transfer estimate. (Audit FIX: the prior code measured centroid→centroid ≈ 0 km, which
    # produced a bogus "5-min walk to the airport" for every city. coord_basis stays truthful: a real
    # OpenFlights airport endpoint, a city-centroid hotel endpoint.)
    km = dist_km
    mode, minutes = _hop_mode_minutes(km)
    basis = "airport_real+hotel_centroid"
    label_suffix = " (from city centre — exact hotel location unavailable)"

    inbound = {
        "from": iata,
        "to": hotel_name,
        "from_kind": "airport",
        "to_kind": "hotel",
        "iata": iata,
        "distance_km": round(km, 1),
        "mode": mode,
        "minutes": minutes,
        "label": f"~{minutes} min, {mode}, est.{label_suffix}",
        "coord_basis": basis,
        "estimate": True,
        "direction": "inbound",
    }
    outbound = {
        "from": hotel_name,
        "to": iata,
        "from_kind": "hotel",
        "to_kind": "airport",
        "iata": iata,
        "distance_km": round(km, 1),
        "mode": mode,
        "minutes": minutes,
        "label": f"~{minutes} min, {mode}, est.{label_suffix}",
        "coord_basis": basis,
        "estimate": True,
        "direction": "outbound",
    }
    return [inbound, outbound]


def attach_intracity_transport(
    day_plans: list[dict],
    legs: list[dict],
    *,
    force: bool = False,
) -> None:
    """Mutate day_plans and legs in place — APPEND-ONLY, idempotent by default.

    - Sets day['intracity_hops'] on each day dict (replaces only if not already set → idempotent).
    - Sets day['item_transport_gaps'] on each day dict (same idempotent rule).
    - Sets leg['airport_transfer'] on each leg dict in `legs` (idempotent, always —
      not gated by `force` since /replan ops never touch the hotel, so this never
      goes stale).

    force=True recomputes intracity_hops/item_transport_gaps unconditionally, even
    if already present. Used by POST /replan after a structural edit (add_place /
    remove_place / swap_place / reflow_from / switch_variant) changes a day's
    attraction order or membership — without this, the idempotent skip below would
    leave the ORIGINAL (pre-edit) hops/gaps in the envelope forever, silently
    presenting stale distances/modes for the wrong item pairs. day-tab swap_days
    is unaffected either way (whole-day content moves as one unit, so a forced
    recompute reproduces byte-identical values — see op_swap_days's docstring).

    Never edits attractions or meals. Safe to call multiple times on the same objects.
    """
    # Build a lookup from leg_id → leg (for airport transfer)
    leg_by_id: dict[str, dict] = {}
    for leg in (legs or []):
        if isinstance(leg, dict) and leg.get("leg_id"):
            leg_by_id[leg["leg_id"]] = leg

    for dp in (day_plans or []):
        if not isinstance(dp, dict):
            continue
        city = dp.get("city", "")
        leg_id = dp.get("leg_id", "")
        leg = leg_by_id.get(leg_id, {})

        for day in (dp.get("days") or []):
            if not isinstance(day, dict):
                continue
            # Idempotent unless force=True (see docstring)
            if force or "intracity_hops" not in day:
                day["intracity_hops"] = build_intracity_hops(day, leg, city)
            # Inline item-to-item gaps (UI increment #3) — idempotent unless force=True
            if force or "item_transport_gaps" not in day:
                day["item_transport_gaps"] = build_item_transport_gaps(day, leg, city)

        # Airport transfer (idempotent) — mirror onto BOTH the leg and the day_plan, since the UI
        # renders the day_plan and needs the transfer alongside the per-day intracity hops. Same
        # deterministic value either way, so day_plans stays var-0 byte-identical.
        if "airport_transfer" not in dp:
            transfer = leg["airport_transfer"] if (leg and "airport_transfer" in leg) else build_transfer_hops(leg or {}, city)
            dp["airport_transfer"] = transfer
            if leg and "airport_transfer" not in leg:
                leg["airport_transfer"] = transfer
