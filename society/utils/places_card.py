"""places_card.py — server-proxied Google Places (New, v1) detail card + autocomplete.

Used by POST /place_card (the map popup overlay). Architecture:
- GOOGLE_PLACES_KEY stays server-side ONLY (mirrors places_status.py). It is NEVER
  returned to the client, NEVER included in any field of the card response, and NEVER
  exposed in photo URLs. Photos are proxied via /place_photo (key added server-side).
- Gated: both PLACES_ENABLED=1 and GOOGLE_PLACES_KEY must be set or every call returns
  {"status":"unavailable"} (HTTP 200) — mirrors places_status.py:47.
- Cached: TTL in-process cache keyed by normalised query (mirrors _watchlist_cache
  pattern at server.py:831-837). TTL ~6 h; repeated board loads do not hammer Places.
- Least-privilege FieldMask: only the curated card fields are requested (no raw blob).
- NEVER raises: any failure degrades to an honest 'unavailable' (never fabricated).
- Off var-0: never called by negotiate()/_request_digest; no effect on booking/digest.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any

import httpx

_log = logging.getLogger("society.places_card")

# ---------------------------------------------------------------------------
# Config (read once at module import; mirrors places_status.py pattern)
# ---------------------------------------------------------------------------
PLACES_KEY = os.environ.get("GOOGLE_PLACES_KEY", "")
PLACES_ENABLED = os.environ.get("PLACES_ENABLED", "") == "1"

# Disk persistence paths — stored under the repo root's places_data/ so they
# survive server restarts and pre-warm runs persist to production.
_CACHE_DIR = os.environ.get(
    "PLACES_CACHE_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "places_data"),
)
_CARD_CACHE_PATH = os.path.join(_CACHE_DIR, "places_cache.json")
_PHOTO_DIR = os.path.join(_CACHE_DIR, "photos")

_PLACES_V1_SEARCH = "https://places.googleapis.com/v1/places:searchText"
_PLACES_V1_AUTO = "https://places.googleapis.com/v1/places:autocomplete"

# Least-privilege FieldMask for a detail card (no raw blob, no key-derivable fields).
_DETAIL_FIELD_MASK = (
    "places.displayName,"
    "places.formattedAddress,"
    "places.rating,"
    "places.userRatingCount,"
    "places.currentOpeningHours.openNow,"
    "places.photos.name,"
    "places.reviews.text,"
    "places.reviews.rating,"
    "places.reviews.authorAttribution.displayName"
)

# TTL for place detail cache (6 hours — place details are stable).
_CARD_TTL_S = 6 * 3600

# In-process cache: {cache_key: {"ts": float, "data": dict}}
_card_cache: dict[str, dict] = {}
_card_lock = threading.Lock()

# Opaque photo-ref store: {"opaque_id": places_photo_name}
# Server-side only — never sent to the client; the client gets /place_photo?ref=<opaque>
_photo_refs: dict[str, str] = {}
_photo_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Disk-cache bootstrap (loaded after function definitions below)
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    os.makedirs(_PHOTO_DIR, exist_ok=True)


def _load_disk_cache() -> None:
    """Populate in-process caches from disk on startup."""
    try:
        with open(_CARD_CACHE_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        cards = saved.get("cards") or {}
        refs = saved.get("photo_refs") or {}
        with _card_lock:
            _card_cache.update(cards)
        with _photo_lock:
            _photo_refs.update(refs)
        _log.info("places_card: loaded %d cached results + %d photo refs from disk", len(cards), len(refs))
    except FileNotFoundError:
        pass  # first run
    except Exception as exc:  # noqa: BLE001
        _log.warning("places_card: could not load disk cache: %s", exc)


def _flush_disk_cache() -> None:
    """Write in-process caches to disk (called under _card_lock or _photo_lock)."""
    try:
        _ensure_dirs()
        with _card_lock:
            cards_snapshot = dict(_card_cache)
        with _photo_lock:
            refs_snapshot = dict(_photo_refs)
        blob = {"version": 1, "cards": cards_snapshot, "photo_refs": refs_snapshot}
        tmp = _CARD_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, separators=(",", ":"))
        os.replace(tmp, _CARD_CACHE_PATH)
    except Exception as exc:  # noqa: BLE001
        _log.warning("places_card: disk flush failed: %s", exc)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _is_enabled() -> bool:
    """Both env vars must be set (gated + dormant by default)."""
    return bool(PLACES_ENABLED and PLACES_KEY)


def _unavailable(reason: str, source: str = "live:google_places") -> dict:
    return {"status": "unavailable", "source": source, "reason": reason}


def _breaker_ok() -> bool:
    """Denial-of-wallet breaker gate for a BILLABLE Google Places call (default OFF;
    utils/cost_breaker.py). Called only at the point a paid HTTP request is imminent
    (after the cache/enabled/ref checks), so cache hits and disabled/gated paths never
    consume budget. Returns True when the call may proceed; False when the daily Places
    cap is reached or the kill-switch is set → the caller degrades to 'unavailable'."""
    try:
        from utils.cost_breaker import get_breaker
    except ImportError:  # flat-module import fallback
        from cost_breaker import get_breaker  # type: ignore[no-redef]
    return get_breaker().allow("places")


def _guard_param(value: str, name: str, max_len: int = 300) -> str | None:
    """Return the sanitised param, or None if it fails the guard.

    Guards against SSRF/injection: rejects non-printable chars and values that look
    like URLs or paths (scheme:// or absolute paths). Truncates at max_len."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()[:max_len]
    # Reject anything that looks like a URL scheme or absolute path traversal.
    if re.search(r"(?i)(https?://|ftp://|file://|/\.\.)", v):
        _log.warning("places_card: rejected suspicious %s param: %r", name, v[:60])
        return None
    # Reject non-printable ASCII (control characters).
    if re.search(r"[\x00-\x1f\x7f]", v):
        _log.warning("places_card: rejected non-printable chars in %s", name)
        return None
    return v


# ---------------------------------------------------------------------------
# Opaque photo-ref helpers
# ---------------------------------------------------------------------------

def _mint_opaque_ref(places_photo_name: str) -> str:
    """Mint a stable opaque id for a Places photo name (SHA-256 truncated to 32 hex chars).
    The opaque id is safe to send to the browser; the Places photo name stays server-only."""
    opaque = hashlib.sha256(places_photo_name.encode()).hexdigest()[:32]
    with _photo_lock:
        is_new = opaque not in _photo_refs
        _photo_refs[opaque] = places_photo_name
    if is_new:
        _flush_disk_cache()
    return opaque


def resolve_photo_name(opaque: str) -> str | None:
    """Resolve an opaque photo ref back to the Places photo name (server-side only)."""
    with _photo_lock:
        return _photo_refs.get(opaque)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_key(mode: str, params: dict) -> str:
    blob = json.dumps({"mode": mode, **params}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:40]


def _cache_get(key: str) -> dict | None:
    now = time.time()
    with _card_lock:
        entry = _card_cache.get(key)
    if entry and (now - entry["ts"]) < _CARD_TTL_S:
        return entry["data"]
    return None


_CARD_CACHE_MAX = 500  # in-process cap; disk is unbounded (13 GB free on dev, OSS in prod)


def _cache_put(key: str, data: dict) -> None:
    with _card_lock:
        _card_cache[key] = {"ts": time.time(), "data": data}
        if len(_card_cache) > _CARD_CACHE_MAX:
            # Evict the oldest entry by timestamp (O(n) but rare and cheap at this size).
            oldest = min(_card_cache, key=lambda k: _card_cache[k]["ts"])
            del _card_cache[oldest]
    _flush_disk_cache()


# ---------------------------------------------------------------------------
# Places API helpers
# ---------------------------------------------------------------------------

def _places_headers() -> dict:
    """Auth + content headers. Key is added SERVER-SIDE ONLY; NEVER returned to client."""
    return {
        "X-Goog-Api-Key": PLACES_KEY,
        "Content-Type": "application/json",
    }


def _search_detail(name: str, city: str, country: str, lat: float | None, lon: float | None) -> dict:
    """Search Places for a place by text query; return a curated card dict or unavailable."""
    if not _is_enabled():
        return _unavailable("Places not enabled (PLACES_ENABLED=1 and GOOGLE_PLACES_KEY required)")

    query = f"{name} {city} {country}".strip()
    params_key = {"name": name, "city": city, "country": country}
    ck = _cache_key("detail", params_key)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    # Denial-of-wallet breaker: gate the BILLABLE Places call (default OFF → no-op).
    if not _breaker_ok():
        return _unavailable("temporarily unavailable (daily cost cap reached)")

    try:
        payload: dict[str, Any] = {"textQuery": query, "maxResultCount": 1}
        resp = httpx.post(
            _PLACES_V1_SEARCH,
            headers={**_places_headers(), "X-Goog-FieldMask": _DETAIL_FIELD_MASK},
            json=payload,
            timeout=15.0,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — never raise; honest unavailable
        _log.warning("places_card: detail fetch failed: %s", exc)
        return _unavailable("Places fetch failed (network/timeout)")

    places = data.get("places") or []
    if not places:
        result = _unavailable("no matching place found")
        return result

    p = places[0]
    display_name = (p.get("displayName") or {}).get("text") or name

    # Build photo list: return opaque proxied URLs, never the raw Places photo name.
    raw_photos = p.get("photos") or []
    photo_urls = []
    for ph in raw_photos[:3]:  # cap at 3 photos
        pn = (ph.get("name") or "").strip()
        if pn:
            opaque = _mint_opaque_ref(pn)
            photo_urls.append(f"/place_photo?ref={opaque}")

    # Reviews: return only the curated fields.
    raw_reviews = p.get("reviews") or []
    reviews = []
    for rv in raw_reviews[:5]:  # cap at 5
        text = (rv.get("text") or {}).get("text") or ""
        author = ((rv.get("authorAttribution") or {}).get("displayName") or "Anonymous")
        rating = rv.get("rating")
        if text:
            reviews.append({"author": author, "rating": rating, "text": text[:500]})

    opening = p.get("currentOpeningHours") or {}
    open_now = opening.get("openNow")

    card = {
        "display_name": display_name,
        "formatted_address": p.get("formattedAddress"),
        "rating": p.get("rating"),
        "user_rating_count": p.get("userRatingCount"),
        "open_now": open_now,
        "photos": photo_urls,
        "reviews": reviews,
    }
    result = {
        "status": "ok",
        "source": "live:google_places",
        "as_of": _now_iso(),
        "place": card,
    }
    # Cache only successful results.
    _cache_put(ck, result)
    return result


def _autocomplete(input_text: str, city: str) -> dict:
    """Places autocomplete for a partial query. Returns predictions list or unavailable."""
    if not _is_enabled():
        return _unavailable("Places not enabled")

    ck = _cache_key("autocomplete", {"input": input_text, "city": city})
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    # Denial-of-wallet breaker: gate the BILLABLE Places call (default OFF → no-op).
    if not _breaker_ok():
        return _unavailable("temporarily unavailable (daily cost cap reached)")

    try:
        payload: dict[str, Any] = {
            "input": f"{input_text} {city}".strip(),
        }
        resp = httpx.post(
            _PLACES_V1_AUTO,
            headers=_places_headers(),
            json=payload,
            timeout=10.0,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        _log.warning("places_card: autocomplete failed: %s", exc)
        return _unavailable("autocomplete fetch failed")

    suggestions = data.get("suggestions") or []
    preds = []
    for s in suggestions[:10]:
        place_pred = (s.get("placePrediction") or {})
        text_obj = place_pred.get("text") or {}
        text = text_obj.get("text") or ""
        if text:
            preds.append({"text": text})

    result = {"status": "ok", "predictions": preds}
    _cache_put(ck, result)
    return result


# ---------------------------------------------------------------------------
# Main entry point (called by the /place_card route handler)
# ---------------------------------------------------------------------------

def fetch_place_card(body: dict) -> tuple[dict, int]:
    """Process a /place_card request body; return (response_dict, http_status).

    400 for structurally-malformed bodies; 200 always for domain states (including
    unavailable). NEVER 500. NEVER returns the GOOGLE_PLACES_KEY in any field."""
    if not isinstance(body, dict):
        return {"error": "body must be a JSON object"}, 400

    mode = (body.get("mode") or "").strip().lower()
    if not mode:
        return {"outcome": "invalid_request", "reason": "mode is required (detail or autocomplete)"}, 400

    if mode == "detail":
        name_raw = body.get("name") or body.get("place") or ""
        name = _guard_param(str(name_raw), "name")
        if not name:
            return {"outcome": "invalid_request", "reason": "detail mode requires a non-empty 'name'"}, 400
        city = _guard_param(str(body.get("city") or ""), "city") or ""
        country = _guard_param(str(body.get("country") or ""), "country") or ""
        try:
            lat = float(body["lat"]) if "lat" in body else None
            lon = float(body["lon"]) if "lon" in body else None
        except (TypeError, ValueError):
            lat, lon = None, None
        result = _search_detail(name, city, country, lat, lon)
        return result, 200

    if mode == "autocomplete":
        input_raw = body.get("input") or ""
        input_text = _guard_param(str(input_raw), "input")
        if not input_text:
            return {"outcome": "invalid_request", "reason": "autocomplete mode requires a non-empty 'input'"}, 400
        city = _guard_param(str(body.get("city") or ""), "city") or ""
        result = _autocomplete(input_text, city)
        return result, 200

    return {"outcome": "invalid_request", "reason": f"unknown mode {mode!r}; use 'detail' or 'autocomplete'"}, 400


def fetch_place_photo(opaque_ref: str) -> bytes | None:
    """Fetch photo bytes for an opaque ref (server adds the Places key). Returns bytes
    or None if the ref is unknown / fetch failed. NEVER returns the key to the caller.

    Serves from disk cache (places_data/photos/<opaque>) when available so that:
    - repeated views cost nothing (no Places API quota consumed)
    - bytes survive server restarts (opaque ref → photo_name mapping is also persisted)
    - post-submission: swap _PHOTO_DIR for AliCloud OSS via PLACES_CACHE_DIR env
    """
    # 1. Serve from disk cache if available (free, fast, no quota).
    _ensure_dirs()
    disk_path = os.path.join(_PHOTO_DIR, opaque_ref)
    if os.path.isfile(disk_path):
        try:
            with open(disk_path, "rb") as fh:
                return fh.read()
        except Exception as exc:  # noqa: BLE001
            _log.warning("places_card: disk photo read failed (%s), re-fetching", exc)

    # 2. Places API is required for the first fetch or if disk read failed.
    if not _is_enabled():
        return None
    photo_name = resolve_photo_name(opaque_ref)
    if not photo_name:
        return None
    # Denial-of-wallet breaker: gate the BILLABLE Places photo fetch (default OFF →
    # no-op). On trip, return None → the /place_photo route serves an honest
    # {"status":"unavailable"} (HTTP 200), the SAME degrade as a key-not-set / fetch
    # failure. Disk-cached photos above are served free and never reach this gate.
    if not _breaker_ok():
        return None
    url = f"https://places.googleapis.com/v1/{photo_name}/media"
    try:
        resp = httpx.get(
            url,
            params={"maxWidthPx": 800},
            headers={"X-Goog-Api-Key": PLACES_KEY},
            follow_redirects=True,
            timeout=15.0,
        )
        if resp.status_code == 200:
            data = resp.content
            # Persist to disk so subsequent requests skip Google entirely.
            try:
                with open(disk_path, "wb") as fh:
                    fh.write(data)
            except Exception as exc:  # noqa: BLE001
                _log.warning("places_card: disk photo write failed: %s", exc)
            return data
    except Exception as exc:  # noqa: BLE001
        _log.warning("places_card: photo fetch failed: %s", exc)
    return None


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# Load persisted cache on first import (all functions defined above this point).
_load_disk_cache()
