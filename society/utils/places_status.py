"""places_status.py — #63: Google Places (New, v1) business-status check for CATALOG FRESHNESS.

Used by the OFFLINE batch enricher to stamp `business_status` into the catalog so the DETERMINISTIC
booking can exclude permanently-closed venues. var-0 is preserved: live Places calls run ONLY in the
offline enrichment; the booking reads the cached status baked into the static catalog. Gated by
PLACES_ENABLED + GOOGLE_PLACES_KEY (dormant until both set). NEVER raises.

Matching is the crux: a permanently-closed venue usually DROPS OUT of Places search, so a text query
returns OTHER nearby venues. We therefore match on the hotel's DISTINCTIVE name tokens (stripping
generic accommodation words + the city/country), and treat "no distinctive match" as UNVERIFIED — a
conservative closed/gone signal — rather than falsely trusting a same-word ("hostel") result.
"""

from __future__ import annotations

import os
import re

import httpx

PLACES_KEY = os.environ.get("GOOGLE_PLACES_KEY", "")
PLACES_ENABLED = os.environ.get("PLACES_ENABLED", "") == "1"
_URL = "https://places.googleapis.com/v1/places:searchText"

# generic accommodation / filler words that must NOT, alone, count as a name match
_GENERIC = frozenset({
    "hotel", "hostel", "inn", "motel", "lodge", "resort", "guesthouse", "guest", "house",
    "apartments", "apartment", "suites", "suite", "rooms", "room", "the", "and", "boutique",
    "backpackers", "bnb", "ryokan", "capsule", "stay", "place", "tokyo",  # city often re-added below
})


def _norm(s: str) -> set[str]:
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split() if len(t) >= 3}


def _distinctive(title: str, city: str, country: str) -> set[str]:
    """The hotel's distinctive name tokens: drop generic accommodation words + city/country tokens."""
    drop = _GENERIC | _norm(city) | _norm(country)
    return _norm(title) - drop


def business_status(title: str, city: str = "", country: str = "") -> str | None:
    """Return 'OPERATIONAL' / 'CLOSED_PERMANENTLY' / 'CLOSED_TEMPORARILY' for a catalog venue,
    'UNVERIFIED' when no Places result matches its distinctive name (closed/de-listed → vanished from
    search), or None when Places is unavailable/off/error. NEVER raises (offline batch use)."""
    if not (PLACES_ENABLED and PLACES_KEY):
        return None
    want = _distinctive(title, city, country)
    if not want:
        return None  # nothing distinctive to match on — don't guess
    query = f"{title} {city} {country}".strip()
    try:
        resp = httpx.post(
            _URL,
            headers={"X-Goog-Api-Key": PLACES_KEY, "Content-Type": "application/json",
                     "X-Goog-FieldMask": "places.displayName,places.businessStatus"},
            json={"textQuery": query, "maxResultCount": 6}, timeout=20.0,
        )
        places = resp.json().get("places", [])
    except Exception:
        return None
    for p in places:
        nm = (p.get("displayName") or {}).get("text", "")
        if _distinctive(nm, city, country) & want:  # distinctive-token overlap = a real match
            return p.get("businessStatus") or "OPERATIONAL"
    return "UNVERIFIED"  # no matching operational venue → conservatively flag (likely closed/gone)
