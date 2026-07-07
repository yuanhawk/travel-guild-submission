"""hotel_geocode.py — Disk-cached Nominatim geocoder for hotel map pins.

ADDITIVE / var-0-safe: this module is ONLY used to attach hotel_lat/hotel_lon to
the already-frozen result envelope. It NEVER affects day_plans, booking bytes,
or the _request_digest path.

Request path: lookup_cached() only — pure disk read, deterministic, no network.
Live path: resolve_live() gated by GEOCODE_ENABLED=1, runs ONLY via /hotel_geo
  warm endpoint (off the negotiate() hot path), persists to disk.

Cache file: society/hotel_geo_cache.json (disk-persisted per crash-recovery note).
  Shape: { "<key>": {"lat": <float>|null, "lon": <float>|null,
                     "source": "nominatim"|"negative", "as_of": "<ISO date>"} }
  A null entry is a negative cache (unresolvable) — never re-hit Nominatim.

Centroid fallback: city_coords.json via transport_agent._CITY_COORDS (same seeded
  data, same determinism guarantee).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache file path
# ---------------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # society/
_CACHE_PATH = os.path.join(_BASE_DIR, "hotel_geo_cache.json")

# Module-level lazy-loaded cache dict (keyed by cache_key)
_cache: dict[str, dict] | None = None
_cache_lock = threading.Lock()

# Rate-limit state for Nominatim (1 req/s per OSM policy)
_last_nominatim_call: float = 0.0
_nominatim_lock = threading.Lock()

# GEO-005: Nominatim's usage policy requires a contact email in the User-Agent
# header. Set NOMINATIM_USER_AGENT_EMAIL to a real contact address before
# using this in production; the fallback below is a placeholder, not a real
# address to avoid using in requests.
_UA_EMAIL = os.environ.get("NOMINATIM_USER_AGENT_EMAIL", "contact@example.com")


# ---------------------------------------------------------------------------
# Cache key normalization
# ---------------------------------------------------------------------------

def cache_key(name: str, area: str, city: str, country: str) -> str:
    """Stable, deterministic cache key from (name, area, city, country)."""
    parts = [city.lower().strip(), country.lower().strip(),
             name.lower().strip(), area.lower().strip()]
    return "|".join(parts)


# ---------------------------------------------------------------------------
# Cache loading / saving
# ---------------------------------------------------------------------------

def _load_cache() -> dict[str, dict]:
    """Load the cache from disk (call once; module-level lazy init)."""
    if not os.path.exists(_CACHE_PATH):
        return {}
    try:
        with open(_CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        _log.warning("hotel_geocode: cache load failed (ignored): %s", exc)
    return {}


def _get_cache() -> dict[str, dict]:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = _load_cache()
    return _cache


def _save_cache(entry_key: str, entry: dict) -> None:
    """Atomically write a single new entry into the cache file."""
    global _cache
    with _cache_lock:
        c = _get_cache()
        c[entry_key] = entry
        try:
            tmp = _CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(c, fh, indent=2)
            os.replace(tmp, _CACHE_PATH)
        except Exception as exc:  # noqa: BLE001
            _log.warning("hotel_geocode: cache save failed (ignored): %s", exc)


# ---------------------------------------------------------------------------
# Public API — used in the request path (DISK ONLY, no network)
# ---------------------------------------------------------------------------

def lookup_cached(
    name: str, area: str, city: str, country: str
) -> Optional[tuple[float, float]]:
    """Pure disk read. Returns (lat, lon) on a positive cache hit, else None.

    A negative cache entry (lat=null) also returns None (no re-hit).
    NEVER makes a network call. Used in the hot negotiate() path.
    """
    key = cache_key(name, area, city, country)
    c = _get_cache()
    entry = c.get(key)
    if entry is None:
        return None
    lat = entry.get("lat")
    lon = entry.get("lon")
    if lat is None or lon is None:
        return None  # negative cache
    try:
        return (float(lat), float(lon))
    except (TypeError, ValueError):
        return None


def centroid(city: str) -> Optional[tuple[float, float]]:
    """Return the city centroid from city_coords.json (deterministic seeded data).

    Reuses the same _CITY_COORDS dict that intracity_transport and transport_agent
    use — same normalization, same determinism.
    """
    try:
        from agents.transport_agent import _CITY_COORDS, _canonical_city
        key = _canonical_city(city)
        coord = _CITY_COORDS.get(key)
        if coord:
            return (float(coord[0]), float(coord[1]))
    except Exception as exc:  # noqa: BLE001
        _log.warning("hotel_geocode: centroid lookup failed for %r: %s", city, exc)
    return None


# ---------------------------------------------------------------------------
# Live resolver — gated by GEOCODE_ENABLED=1, used ONLY via /hotel_geo endpoint
# ---------------------------------------------------------------------------

def resolve_live(
    name: str, area: str, city: str, country: str
) -> Optional[tuple[float, float]]:
    """Nominatim geocode (gated: GEOCODE_ENABLED=1 must be set).

    - Rate-limited to 1 req/s (OSM Nominatim policy).
    - Writes the result (positive or negative) to the cache.
    - Returns (lat, lon) on success, None on gate / error / no result.
    - NEVER raises.
    """
    if not os.environ.get("GEOCODE_ENABLED"):
        return None

    key = cache_key(name, area, city, country)

    # Check positive/negative cache first (avoid hitting Nominatim again)
    c = _get_cache()
    if key in c:
        entry = c[key]
        lat, lon = entry.get("lat"), entry.get("lon")
        if lat is not None and lon is not None:
            return (float(lat), float(lon))
        return None  # negative cache hit

    # 1 req/s throttle
    with _nominatim_lock:
        global _last_nominatim_call
        now = time.monotonic()
        elapsed = now - _last_nominatim_call
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        _last_nominatim_call = time.monotonic()

        try:
            import httpx
            query = f"{name}, {area}, {city}" if area else f"{name}, {city}"
            if country:
                query += f", {country}"
            resp = httpx.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 0},
                headers={
                    "User-Agent": f"TravelGuild/1.0 ({_UA_EMAIL})"
                },
                timeout=5.0,
            )
            resp.raise_for_status()
            results = resp.json()
        except Exception as exc:  # noqa: BLE001
            _log.warning("hotel_geocode: Nominatim request failed for %r: %s", name, exc)
            # Cache a negative entry so we don't retry immediately
            from datetime import date
            _save_cache(key, {"lat": None, "lon": None,
                               "source": "negative", "as_of": str(date.today())})
            return None

    if not results:
        from datetime import date
        _save_cache(key, {"lat": None, "lon": None,
                           "source": "negative", "as_of": str(date.today())})
        return None

    try:
        lat = float(results[0]["lat"])
        lon = float(results[0]["lon"])
    except (KeyError, ValueError, TypeError) as exc:
        _log.warning("hotel_geocode: Nominatim parse error for %r: %s", name, exc)
        from datetime import date
        _save_cache(key, {"lat": None, "lon": None,
                           "source": "negative", "as_of": str(date.today())})
        return None

    # GEO-002: reject and skip caching out-of-range coordinates
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        _log.warning(
            "geocode: out-of-range coordinates lat=%s lon=%s for %r — skipping",
            lat, lon, key,
        )
        return None  # caller already handles None

    from datetime import date
    _save_cache(key, {"lat": lat, "lon": lon,
                       "source": "nominatim", "as_of": str(date.today())})
    return (lat, lon)
