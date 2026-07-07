"""
emergency_feed.py — LIVE active-emergency provider clients for the #51 overlay seam.

The seasonal risk model (risk_agent) is a FROZEN climatology → ADVISORY ("take
precautions"). A *declared, active* emergency (a tropical cyclone / flood / wildfire
with an official Orange/Red alert + evacuation) is a live, time-varying fact that
CANNOT live on the var-0 deterministic path. So it is FIREWALLED behind this
provider seam (orchestrator `_call_emergency_feed` / `_maybe_check_active_emergencies`):

  • Provider contract:  client(query) -> dict | None
      query  = {"city", "iso2", "region", "checkin", "checkout"}
      return =  {"active": bool, "monitoring"?: bool, "hazard"?, "severity"?,
                 "headline"?, "advice"?, "source"?, "as_of"?}
                 # monitoring tier triggers on monitoring==True OR severity=="monitoring"
                or None  (→ the consumer prints an honest "live emergency status
                unavailable" note — NEVER a fabricated all-clear; silence != safety).

  • These clients are wired ONLY when the board sets EMERGENCY_FEED (off by default →
    no client → the overlay is a var-0 no-op). They are NEVER on the deterministic
    risk rollup / day_plans / avoid-window and NEVER enter `_request_digest`.

Two clients:
  • stub_emergency_client  — deterministic demo provider (Typhoon Podul over southern
    Taiwan). Proves the escalation path with no external dependency.
  • gdacs_emergency_client — the REAL feed: GDACS (Global Disaster Alert &
    Coordination System), free, no API key. https://www.gdacs.org/
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import time

logger = logging.getLogger(__name__)

# Typical tropical-cyclone forecast horizon. An ACTIVE storm with a blank `todate` (no forecast
# end yet) must not be bounded at exactly its last-update timestamp — that would drop an alert for
# a trip starting a few days out. We extend the open end by this many days past datemodified so an
# imminent trip still overlaps, while a months-away trip is still (correctly) filtered out.
_TC_FORECAST_BUFFER_DAYS = 5

# Hazard mapping from GDACS eventtype → our hazard vocabulary.
_GDACS_HAZARD = {
    "TC": "tropical_cyclone",
    "FL": "flood",
    "WF": "wildfire",
}
# GDACS alertlevel → our severity. Orange/Red count as ACTIVE (do-not-travel).
_GDACS_SEVERITY = {"Red": "high", "Orange": "medium"}


def _classify(et: str, al: str):
    """Map a GDACS (eventtype, alertlevel) → (severity, active) or None (excluded).

    - Orange/Red TC/FL/WF      → ('medium'|'high', True)  — ACTIVE do-not-travel emergency.
    - Green TROPICAL CYCLONE    → ('monitoring', False)    — a storm being TRACKED (low/no
      current impact); SURFACED for awareness but NEVER escalated to do-not-travel.
    - Green flood/wildfire, any earthquake, anything else → None (excluded): Green FL/WF
      is high-volume routine noise; EQ is not forecastable (handled by the static
      seismic baseline, not this live overlay).
    """
    if et not in _GDACS_HAZARD:
        return None
    sev = _GDACS_SEVERITY.get(al)
    if sev is not None:
        return (sev, True)             # Orange/Red TC/FL/WF → active
    if al == "Green" and et == "TC":
        return ("monitoring", False)   # Green tropical cyclone → monitoring tier
    return None                        # Green FL/WF, EQ, etc. → excluded (noise)


# Severity → sort rank (high first, monitoring last).
_SEV_RANK = {"high": 3, "medium": 2, "monitoring": 1}


# ---------------------------------------------------------------------------
# STUB provider — deterministic demo (no network). Active TC over Taiwan only.
# ---------------------------------------------------------------------------
def stub_emergency_client(query: dict) -> dict | None:
    """Deterministic demo provider: an ACTIVE Typhoon Podul declaration for Taiwan
    (iso2 == "TW"), CLEAR for everywhere else. No clock/random — `as_of` is derived
    from the query's checkin (falls back to a fixed constant) so it is var-0 for a
    given input. Returns None only if the query is malformed (→ honest 'unavailable').
    """
    if not isinstance(query, dict):
        return None
    iso2 = (query.get("iso2") or "").strip().upper()
    as_of = (query.get("checkin") or "2026-06-26")[:10]
    if iso2 == "TW":
        return {
            "active": True,
            "hazard": "tropical_cyclone",
            "severity": "high",
            "headline": "Typhoon Podul — landfall over southern Taiwan (Kaohsiung/"
                        "Pingtung); CWA land + sea warnings in effect.",
            "advice": "Do not travel; adhere to Taiwan CWA warnings and official "
                      "evacuation guidance.",
            "source": "demo:stub",
            "as_of": as_of,
        }
    if iso2 == "JP":
        # Green-level TC offshore → MONITORING (being tracked), NOT do-not-travel.
        return {
            "active": False, "monitoring": True,
            "hazard": "tropical_cyclone", "severity": "monitoring",
            "headline": "Tropical storm offshore SE of Honshu — JMA tracking; "
                        "no warnings for the travel area yet.",
            "advice": "Storm being tracked — monitor JMA advisories.",
            "source": "demo:stub", "as_of": as_of,
        }
    return {"active": False, "source": "demo:stub", "as_of": as_of}


# ---------------------------------------------------------------------------
# GDACS provider — the REAL live feed.
# ---------------------------------------------------------------------------
_GDACS_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS4APP"


def _date10(s) -> str:
    """First 10 chars of an ISO datetime → 'YYYY-MM-DD' (or '' if unparseable).

    Validates via `date.fromisoformat` — a non-ISO string that merely happens to
    be >=10 chars (e.g. GDACS free-text like "TBD see advisory" or a human-typed
    "Jul 1 2026 12:00") must NOT pass through as a garbage pseudo-date: it would
    then sort lexicographically against real 'YYYY-MM-DD' strings in
    `_windows_overlap` (ASCII letters sort after digits) and could silently
    suppress a real overlap — a fail-CLOSED bug on a safety path. Returns the
    existing '' sentinel (already the fail-open value used throughout this file)
    so malformed input degrades exactly like missing input."""
    if not isinstance(s, str) or len(s) < 10:
        return ""
    candidate = s[:10]
    try:
        _dt.date.fromisoformat(candidate)
    except (ValueError, TypeError):
        return ""
    return candidate


def _date_plus_days(d10: str, n: int) -> str:
    """'YYYY-MM-DD' + n days → 'YYYY-MM-DD' (or '' if unparseable). Used to extend an
    open-ended event window by the cyclone forecast horizon."""
    try:
        return (_dt.date.fromisoformat(d10) + _dt.timedelta(days=n)).isoformat()
    except Exception:  # noqa: BLE001 — defensive; bad dates just yield no bound
        return ""


def _windows_overlap(ev_from: str, ev_to: str, trip_in: str, trip_out: str,
                     ev_modified: str = "", is_active: bool = True) -> bool:
    """True if the event window overlaps the trip window. Missing/blank trip dates →
    treat as overlapping (don't suppress an active alert on missing data).

    `is_active` (default True — fail-open if a caller omits it) marks whether the
    event is a currently-declared Orange/Red emergency (vs. e.g. a Green/monitoring
    event); it only gates the stale-`todate` extension below."""
    ti, to = _date10(trip_in), _date10(trip_out)
    ef, et = _date10(ev_from), _date10(ev_to)
    if not ti or not to:
        return True  # no trip window → don't filter out an active emergency
    # Standard interval overlap: event.from <= trip.out AND event.to >= trip.in.
    lo = ef or "0000-00-00"
    # An OPEN event end (blank todate, e.g. an in-progress storm) must NOT become "+infinity"
    # — that made a currently-active typhoon overlap a trip months away (the false do-not-travel
    # on an out-of-season date). Bound it to when GDACS last touched the event (datemodified),
    # falling back to its start, PLUS the cyclone forecast horizon so an imminent trip a few days
    # out is still covered (the dangerous false-all-clear direction). Truly-undated events keep
    # the open upper bound (fail-safe — never silently suppress an unbounded active event).
    if et:
        hi = et
        # A `todate` on a STILL-ACTIVE (Orange/Red) event can lag behind `datemodified` when
        # GDACS keeps updating a storm that hasn't been formally closed out yet — trusting a
        # stale `todate` alone can window out a genuinely still-live emergency (the false
        # all-clear direction). If the feed has touched the event MORE RECENTLY than its
        # stated end, extend the upper bound past that last update by the same forecast
        # horizon used for open-ended events. Genuinely closed/old events — where
        # `datemodified` is NOT more recent than `todate` — are completely unaffected: this
        # only ever widens the window, never narrows it.
        if is_active:
            dm = _date10(ev_modified)
            if dm and dm > et:
                extended = _date_plus_days(dm, _TC_FORECAST_BUFFER_DAYS)
                if extended:
                    hi = max(hi, extended)
    else:
        base = _date10(ev_modified) or ef
        # `or "9999..."` (not `if base else`) so BOTH a blank base AND a malformed-but-truthy
        # date — which _date_plus_days returns "" for — fail open, never silently suppressing an
        # unbounded active event ("silence != safety").
        hi = _date_plus_days(base, _TC_FORECAST_BUFFER_DAYS) or "9999-99-99"
    return lo <= to and hi >= ti


def _country_matches(props: dict, iso2: str) -> bool:
    """True if the GDACS event affects `iso2`. Prefers the structured
    `affectedcountries` (list of {iso2, iso3, countryname}); falls back to the
    top-level `country` name substring (handles 'Taiwan, Province of China')."""
    if not iso2:
        return False
    for c in props.get("affectedcountries") or []:
        if isinstance(c, dict) and (c.get("iso2") or "").strip().upper() == iso2:
            return True
    # Fallback: top-level country-name contains a known alias for the iso2.
    names = (props.get("country") or "").lower()
    aliases = _ISO2_NAME_ALIASES.get(iso2)
    if not aliases:
        return False
    # Directional-country guard: an alias can be a substring of a DIFFERENT country's
    # name (KR alias "korea" also appears in "North Korea") — reject those so e.g. a
    # North-Korea event never raises a false alert for a South-Korea (KR) trip.
    if any(neg in names for neg in _ISO2_NAME_NEGATIVE.get(iso2, ())):
        return False
    # Word-boundary match against ANY spelling variant: blocks substring false-positives
    # (e.g. "india" must NOT hit "British Indian Ocean Territory" → "indian") while still
    # matching multi-word UN/ISO spellings like "Viet Nam" / "Macao" via the alias tuple
    # (re.escape leaves spaces intact). A missed alert on a safety path is worse than the
    # old false-positive, so the fallback covers these even with no structured iso2.
    return any(re.search(r"\b" + re.escape(a) + r"\b", names) for a in aliases)

# Minimal iso2 → lowercase country-name alias for the name-substring FALLBACK only
# (the structured affectedcountries.iso2 match is primary). Kept short on purpose.
_ISO2_NAME_ALIASES = {
    "TW": ("taiwan",), "JP": ("japan",), "PH": ("philippines",),
    "VN": ("vietnam", "viet nam"),   # GDACS often emits the UN spelling "Viet Nam" (space)
    "CN": ("china",), "KR": ("korea",), "HK": ("hong kong",),
    "MO": ("macau", "macao"),        # ISO/GDACS spelling is "Macao"
    "TH": ("thailand",), "ID": ("indonesia",), "MY": ("malaysia",), "IN": ("india",),
    "US": ("united states",), "MX": ("mexico",), "AU": ("australia",),
}
# Negative guards: name tokens that denote a DIFFERENT country than the alias' iso2,
# to kill directional-name collisions in the substring fallback — a word boundary
# alone cannot separate "Korea" in "South Korea" from "North Korea".
_ISO2_NAME_NEGATIVE = {
    "KR": ("north korea", "democratic people", "dprk"),
    # GDACS's own canonical country string for Taiwan is literally
    # "Taiwan, Province of China" — the CN alias "china" word-matches inside it,
    # so a Taiwan-only typhoon (affectedcountries empty, falling back to this
    # name) would falsely raise a "Do not travel" for a mainland-China (CN) trip.
    "CN": ("taiwan",),
    # "United States Minor Outlying Islands" is a DISTINCT ISO entity (UM) from
    # the United States (US); "united states" word-matches as its prefix.
    "US": ("minor outlying",),
}
# Multi-word aliases (e.g. "viet nam", "macao") are matched whole-word so the name
# fallback covers UN/ISO spellings even when the structured affectedcountries path is
# empty — closes the "Viet Nam" / "Macao" false-negatives flagged in review.


def _gdacs_extract_features(data) -> list:
    """Defensive extraction of the GeoJSON `features` list from a fetched payload.
    Anything other than an actual list (missing key, wrong type — e.g. a
    malformed feed returning `{"features": 5}` or `{"features": "abc"}`)
    degrades to an empty list rather than raising when a caller iterates it."""
    features = data.get("features", []) if isinstance(data, dict) else []
    return features if isinstance(features, list) else []


def _gdacs_fetch_raw(*, _fetch=None):
    """Fetch (with one retry) the raw GDACS payload's `features` list.

    Returns `(features, None)` on success, or `(None, last_exc)` after a
    fetch/parse failure survives the retry. Shared low-level fetch+retry used by
    both `gdacs_fetch_events` (the per-trip client + the orchestrator's shared
    per-trip batch fetch) and `gdacs_active_watchlist` — one source of truth for
    the retry policy. Never raises."""
    data = None
    last_exc: Exception | None = None
    for attempt in range(2):  # one initial try + one retry
        try:
            if _fetch is not None:
                data = _fetch()
            else:
                import httpx
                resp = httpx.get(_GDACS_URL, timeout=5.0)
                resp.raise_for_status()
                data = resp.json()
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 — live feed is opt-in & off the var-0 path
            last_exc = exc
            if attempt == 0:
                time.sleep(0.3)  # short backoff before the single retry
    if last_exc is not None:
        return None, last_exc
    return _gdacs_extract_features(data), None


def gdacs_fetch_events(*, _fetch=None):
    """Fetch the raw GDACS feature list ONCE (standard one-retry policy) and
    pre-scan it for the feed's overall as-of date.

    Returns `(features, feed_as_of)` on success, or `None` on total
    fetch/parse failure (honest 'unavailable' signal — never a fabricated
    all-clear). Never raises.

    PERF/ROBUSTNESS FIX (2026-07-06 adversarial audit, F1): this is the shared
    fetch used both by `gdacs_emergency_client` for a single-leg query AND by
    the orchestrator's per-TRIP batch check (`_maybe_check_active_emergencies`
    / `_call_emergency_feed_batch`), which calls this exactly ONCE per trip and
    reuses the same `(features, feed_as_of)` for every leg via
    `_gdacs_match_leg` — instead of one independent HTTP round-trip (each with
    its own 2-attempt retry) PER LEG. An N-leg trip against the real network
    client previously could hold a worker thread for up to
    N * ~10.3s (2 attempts * 5s timeout + 0.3s backoff) during a GDACS outage."""
    features, last_exc = _gdacs_fetch_raw(_fetch=_fetch)
    if last_exc is not None:
        logger.warning("gdacs_fetch_events: fetch/parse failed after retry (%s) → unavailable",
                        last_exc)
        return None  # honest 'unavailable', never a fabricated all-clear
    feed_as_of = ""
    for f in features:
        if not isinstance(f, dict):
            continue
        try:
            p = f.get("properties") or {}
            if not isinstance(p, dict):
                continue
            dm = _date10(p.get("datemodified") or p.get("todate") or "")
            if dm > feed_as_of:
                feed_as_of = dm
        except Exception as exc:  # noqa: BLE001 — F4: one bad feature must not block the rest
            logger.warning("gdacs_fetch_events: skipping malformed feature (%s)", exc)
            continue
    return features, feed_as_of


def _gdacs_match_leg(features: list, feed_as_of: str, query: dict) -> dict:
    """Filter a pre-fetched GDACS feature list for ONE leg query
    (`{iso2, checkin, checkout}`) and return the same dict shape
    `gdacs_emergency_client` returns once it has reached the feed (active /
    monitoring / clear — never `None`; a fetch failure is the caller's
    responsibility to detect via `gdacs_fetch_events` returning `None` BEFORE
    calling this).

    Extracted out of `gdacs_emergency_client` (F1) so a single
    `gdacs_fetch_events()` call can be reused across every leg of a trip
    without re-fetching. Each feature's classification is individually
    exception-guarded (F4): a malformed-but-JSON-valid feature (e.g. a
    non-string `eventtype`/`alertlevel`, or a `properties` that isn't a dict)
    is skipped rather than aborting the whole match."""
    iso2 = (query.get("iso2") or "").strip().upper()
    best = None  # most severe matching event (high > medium > monitoring)
    for f in features:
        if not isinstance(f, dict):
            continue
        try:
            p = f.get("properties") or {}
            if not isinstance(p, dict):
                continue
            et = (p.get("eventtype") or "").strip().upper()
            al = (p.get("alertlevel") or "").strip().title()
            cls = _classify(et, al)
            if cls is None:
                continue  # Green FL/WF, EQ, etc. → not surfaced
            sev, is_active = cls
            if not _country_matches(p, iso2):
                continue
            if not _windows_overlap(p.get("fromdate", ""), p.get("todate", ""),
                                    query.get("checkin", ""), query.get("checkout", ""),
                                    p.get("datemodified", ""), is_active=is_active):
                continue
            rank = _SEV_RANK[sev]
            if best is None or rank > best[0]:
                best = (rank, p, et, sev, is_active)
        except Exception as exc:  # noqa: BLE001 — F4: skip the bad feature, don't abort the match
            logger.warning("_gdacs_match_leg: skipping malformed feature (%s)", exc)
            continue

    if best is not None:
        _, p, et, sev, is_active = best
        name = p.get("name") or p.get("eventname") or "active emergency"
        desc = p.get("description") or p.get("htmldescription") or ""
        headline = f"{name} — {desc}".strip(" —") if desc else name
        as_of = _date10(p.get("datemodified") or p.get("todate") or "") or feed_as_of
        if is_active:
            return {
                "active": True, "hazard": _GDACS_HAZARD[et], "severity": sev,
                "headline": headline,
                "advice": "Do not travel; adhere to official evacuation guidance.",
                "source": "live:gdacs", "as_of": as_of,
            }
        # Green TC → MONITORING: surfaced for awareness, NOT a do-not-travel (active=False).
        return {
            "active": False, "monitoring": True, "hazard": _GDACS_HAZARD[et],
            "severity": "monitoring", "headline": headline,
            "advice": "Storm being tracked — monitor official advisories.",
            "source": "live:gdacs", "as_of": as_of,
        }
    # Reached the feed, no match → honest CLEAR (carries the feed's own as_of).
    return {"active": False, "source": "live:gdacs", "as_of": feed_as_of}


def gdacs_emergency_client(query: dict, *, _fetch=None) -> dict | None:
    """REAL live provider: query GDACS for an active (Orange/Red) tropical-cyclone /
    flood / wildfire affecting the leg's country within the trip window.

    FIREWALL / HONESTY: any network or parse error → return None (the consumer then
    prints the honest 'unavailable' note — NEVER a fabricated active OR all-clear).
    Never raises. `_fetch` is an injection seam for tests (returns the GeoJSON dict).

    Retries once after a short backoff on a transient fetch/parse failure before
    giving up — same rationale and mechanism as gdacs_active_watchlist()'s retry
    (a dailies-review finding, 2026-07-06, showed this per-trip check's own single
    5s shot going 'unavailable' independently of the global watchlist).

    A thin wrapper over `gdacs_fetch_events()` + `_gdacs_match_leg()` — split out
    (F1 fix) so a single fetch can be shared across every leg of one trip; see
    the orchestrator's `_call_emergency_feed_batch`, which calls
    `gdacs_fetch_events` directly ONCE per trip instead of calling this function
    (and therefore fetching) once per leg."""
    if not isinstance(query, dict):
        return None
    fetched = gdacs_fetch_events(_fetch=_fetch)
    if fetched is None:
        return None  # honest 'unavailable', never a fabricated all-clear
    features, feed_as_of = fetched
    return _gdacs_match_leg(features, feed_as_of, query)


# ---------------------------------------------------------------------------
# GLOBAL WATCHLIST — ALL currently-active emergencies worldwide (NOT filtered to
# one country). Powers the always-on "Safety Watch" board tab, visible to all
# users. OFF the var-0 path (a separate endpoint, never in /negotiate).
#
# FUTURE: once per-user accounts / booked trips exist, scope proactive alerts to
# each user's booked destinations + travel-within-the-week (the preferred
# end-state). The global tab is the interim while there are no accounts.
# ---------------------------------------------------------------------------
def gdacs_active_watchlist(*, _fetch=None) -> dict:
    """All active (Orange/Red) TC/FL/WF events worldwide, deduped by country.

    Returns {status: "ok"|"unavailable", as_of, source, countries: [...]}.
    HONESTY: a network/parse error → status "unavailable" (NEVER an empty
    `countries` list, which would falsely imply an all-clear — "feed down" must be
    DISTINCT from "no active emergencies"). Never raises.

    Retries once after a short backoff on a transient fetch/parse failure before
    declaring 'unavailable' — a single 5s shot against a third-party feed is
    fragile for real users, not just recordings (raised by a dailies-review
    finding, 2026-07-06). Still honest: a failure is still never cached (see the
    caller's TTL cache, which only stores a successful fetch), so a genuine
    outage still surfaces as 'unavailable' and self-heals on the next request."""
    features, last_exc = _gdacs_fetch_raw(_fetch=_fetch)
    if last_exc is not None:
        logger.warning("gdacs_active_watchlist: fetch/parse failed after retry (%s) → unavailable",
                        last_exc)
        return {"status": "unavailable", "source": "live:gdacs", "as_of": "",
                "countries": []}

    feed_as_of = ""
    out: list[dict] = []
    seen: set = set()
    for f in features:
        if not isinstance(f, dict):
            continue
        # F4: one malformed-but-JSON-valid feature (a non-dict `properties`, a
        # non-string eventtype/alertlevel, etc.) must be SKIPPED, not abort the
        # whole loop — otherwise a single bad record would blow up the entire
        # global Safety Watch mid-iteration instead of just omitting that one entry.
        try:
            p = f.get("properties") or {}
            if not isinstance(p, dict):
                continue
            dm = _date10(p.get("datemodified") or p.get("todate") or "")
            if dm > feed_as_of:
                feed_as_of = dm
            et = (p.get("eventtype") or "").strip().upper()
            al = (p.get("alertlevel") or "").strip().title()
            cls = _classify(et, al)
            if cls is None:
                continue  # Green FL/WF, EQ, etc. → not surfaced
            sev, _ = cls  # 'high'|'medium' (active) or 'monitoring' (Green TC)
            name = p.get("name") or p.get("eventname") or "active emergency"
            desc = p.get("description") or ""
            # One row per affected country (so a multi-country TC lists each).
            acs = p.get("affectedcountries") or []
            if not acs:  # fall back to the top-level country name
                acs = [{"iso2": "", "countryname": p.get("country") or ""}]
            for c in acs:
                # F4: mirrors the per-trip client's isinstance(c, dict) guard — a
                # malformed affectedcountries entry (e.g. a bare string) must be
                # skipped, not raise (affectedcountries can itself be a non-list,
                # e.g. a dict, in which case iterating it yields its keys — plain
                # strings — which this guard also catches).
                if not isinstance(c, dict):
                    continue
                cn = (c.get("countryname") or "").strip()
                iso2 = (c.get("iso2") or "").strip().upper()
                key = (iso2 or cn, et, name)
                if not cn or key in seen:
                    continue
                seen.add(key)
                out.append({
                    "country": cn, "iso2": iso2,
                    "hazard": _GDACS_HAZARD[et], "severity": sev,
                    "headline": f"{name} — {desc}".strip(" —") if desc else name,
                    "source": "live:gdacs",
                    "as_of": _date10(p.get("datemodified") or p.get("todate") or "") or feed_as_of,
                })
        except Exception as exc:  # noqa: BLE001 — F4: skip the bad feature, don't abort the watchlist
            logger.warning("gdacs_active_watchlist: skipping malformed feature (%s)", exc)
            continue
    # Sort high → medium → monitoring, then by country (stable, readable).
    out.sort(key=lambda r: (-_SEV_RANK.get(r["severity"], 0), r["country"]))
    return {"status": "ok", "source": "live:gdacs", "as_of": feed_as_of, "countries": out}


def stub_active_watchlist(*, _fetch=None) -> dict:
    """Deterministic demo watchlist (for the demo video): the active Typhoon Podul
    over Taiwan + two illustrative entries. No clock/random."""
    return {
        "status": "ok", "source": "demo:stub", "as_of": "2026-06-26",
        "countries": [
            {"country": "Taiwan", "iso2": "TW", "hazard": "tropical_cyclone",
             "severity": "high",
             "headline": "Typhoon Podul — landfall over southern Taiwan (Kaohsiung/"
                         "Pingtung); CWA land + sea warnings in effect.",
             "source": "demo:stub", "as_of": "2026-06-26"},
            {"country": "Philippines", "iso2": "PH", "hazard": "tropical_cyclone",
             "severity": "medium",
             "headline": "Tropical Cyclone outer bands — Luzon; PAGASA Signal #2.",
             "source": "demo:stub", "as_of": "2026-06-26"},
            {"country": "Greece", "iso2": "GR", "hazard": "wildfire",
             "severity": "high",
             "headline": "Wildfire — active evacuations (demo).",
             "source": "demo:stub", "as_of": "2026-06-25"},
            {"country": "Japan", "iso2": "JP", "hazard": "tropical_cyclone",
             "severity": "monitoring",
             "headline": "Tropical storm offshore SE of Honshu — JMA tracking; "
                         "no warnings for the travel area yet.",
             "source": "demo:stub", "as_of": "2026-06-26"},
        ],
    }


def build_active_watchlist(mode: str):
    """Factory for the always-on watchlist. The watchlist is LIVE by default for
    all users (GDACS); EMERGENCY_FEED=stub swaps the deterministic demo set."""
    if (mode or "").strip().lower() == "stub":
        return stub_active_watchlist
    return gdacs_active_watchlist


def build_emergency_client(mode: str):
    """Factory: map the EMERGENCY_FEED env value → a provider callable (or None).
    Unknown / empty → None (the overlay stays a var-0 no-op)."""
    m = (mode or "").strip().lower()
    if m == "stub":
        return stub_emergency_client
    if m == "gdacs":
        return gdacs_emergency_client
    return None
