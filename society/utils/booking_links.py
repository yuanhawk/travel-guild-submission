"""
booking_links.py — task #37: the reference / booking HANDOFF-LINK layer.

WHAT THIS IS
------------
A PURE, deterministic builder that turns the already-deterministic negotiation
``result`` into a block of HANDOFF links the UI can render. These links are
*constructed*, not *fetched* — they deep-link the traveler OUT to an
authoritative or meta-search destination (the hotel's official site, a flight
meta-search, a maps query, a government visa portal, a CDC/WHO health page).

THE HONESTY CONTRACT (#37 / §0 fail-honest)
-------------------------------------------
Every link carries a ``kind`` discriminator so the UI can NEVER imply a
confirmed booking:

    official_site | search_handoff | meta_search | maps |
    reference | gov_portal | cdc_who_portal | compare_note

  * Flight / meta-search links NEVER embed a fare, a price, or a flight number —
    they only hand the traveler to a search. We do not have live availability.
  * Insurance is a ``compare_note`` with ``booking_url: None`` — NO vendor plan
    is offered (the #45 boundary). We never sell or imply a specific policy.
  * The lodging UCP checkout (result["checkout_id"] / result["booking_ref"]) is
    a SEPARATE confirmed-commerce artifact and is deliberately NOT a
    booking_links entry — handoff links are advisory hand-outs, not the mandate.

THE VARIANCE-0 CONTRACT (var-0 — links go INTO result)
------------------------------------------------------
These links land in ``result`` and ``result`` must stay byte-identical across
re-runs (json.dumps(sort_keys=True)). So this module introduces ZERO
nondeterminism:

  * Query strings are built from an ORDERED LIST of (key, value) tuples — we
    NEVER iterate a dict to emit a URL.
  * Every dynamic URL segment is ``urllib.parse.quote(s, safe="")`` percent-
    encoded (so a venue name with & / ? / # / space is safe AND deterministic).
  * NO wall-clock, NO random, NO I/O, NO catalog load.

THE PROVENANCE WALL-CLOCK TRAP
------------------------------
``contracts.make_provenance(...)`` takes a ``fetched_at`` ISO string — it does
NOT itself read the wall clock, BUT a naive caller would pass ``date.today()``,
which would make ``result`` non-byte-identical across runs. These links are
CONSTRUCTED, not fetched, so there is no honest "fetched_at" wall-clock to
stamp. We therefore pass a STATIC sentinel date (_CONSTRUCTED_AT) that satisfies
``contracts.validate_provenance`` (it requires fetched_at to match
``^\\d{4}-\\d{2}-\\d{2}``) while carrying ZERO wall-clock. The provenance tier
is SEEDED and the source names the link as a deterministic construction, never a
fetch.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import quote

from core.contracts import make_provenance, SourceTier

# ---------------------------------------------------------------------------
# New kind for PROD affiliate handoff links (insurance).
# Registered here to keep ALLOWED_KINDS as the single source of truth.
# ---------------------------------------------------------------------------
KIND_AFFILIATE_HANDOFF = "affiliate_handoff"

# ---------------------------------------------------------------------------
# Allowed link kinds (the closed honesty discriminator set).
# ---------------------------------------------------------------------------
KIND_OFFICIAL_SITE = "official_site"
KIND_SEARCH_HANDOFF = "search_handoff"
KIND_META_SEARCH = "meta_search"
KIND_MAPS = "maps"
KIND_REFERENCE = "reference"
KIND_GOV_PORTAL = "gov_portal"
KIND_CDC_WHO_PORTAL = "cdc_who_portal"
KIND_COMPARE_NOTE = "compare_note"

ALLOWED_KINDS = frozenset({
    KIND_OFFICIAL_SITE,
    KIND_SEARCH_HANDOFF,
    KIND_META_SEARCH,
    KIND_MAPS,
    KIND_REFERENCE,
    KIND_GOV_PORTAL,
    KIND_CDC_WHO_PORTAL,
    KIND_COMPARE_NOTE,
    KIND_AFFILIATE_HANDOFF,  # PROD-only affiliate handoff link (booking_links.KIND_AFFILIATE_HANDOFF)
})

# Static, NON-wall-clock provenance timestamp (see "THE PROVENANCE WALL-CLOCK
# TRAP" above). These links are CONSTRUCTED deterministically — this is the
# project's frozen seed-date convention, NOT a real fetch time.
_CONSTRUCTED_AT = "2026-06-20"

# Accepted URL schemes for catalog-sourced website values (injection safety:
# reject javascript:/data:/vbscript: etc.). Note the trailing "://" so a value
# like "javascript:alert(1)" (no //) is rejected outright.
_SAFE_SCHEMES = ("http://", "https://")


# ===========================================================================
# Low-level deterministic primitives
# ===========================================================================

def _q(s: Any) -> str:
    """Percent-encode a single dynamic URL segment, fully (safe="").

    Deterministic and injection-safe: a name with & / ? / # / space becomes a
    stable %-encoded token. None / empty collapses to "".
    """
    if s is None:
        return ""
    return quote(str(s).strip(), safe="")


def _qs(pairs: list[tuple[str, Any]]) -> str:
    """Build a query string from an ORDERED LIST of (key, value) tuples.

    NEVER iterate a dict — the ordered list is what guarantees byte-identical
    output. Each key and value is independently %-encoded.
    """
    return "&".join(f"{_q(k)}={_q(v)}" for (k, v) in pairs)


def _safe_url(u: Any) -> str | None:
    """Return ``u`` iff it is a syntactically safe http/https URL, else None.

    Guards against a catalog ``website`` value carrying a javascript:/data: (or
    any non-web) scheme being surfaced as a clickable booking link.
    """
    if not isinstance(u, str):
        return None
    s = u.strip()
    low = s.lower()
    for scheme in _SAFE_SCHEMES:
        if low.startswith(scheme) and len(s) > len(scheme):
            return s
    return None


# ---------------------------------------------------------------------------
# Honest name-tier LABEL suffix (#222 fix — mirrors the "unreadable primary"
# badge from web/src/lib/itinerary.ts hasNonLatinScript()/
# namePresentation(), #168). Every label this module builds is a flat STRING
# baked verbatim into the link the UI renders (RightRail.svelte .link-label,
# Itinerary.svelte tooltip title/aria-label) — there is no live badge/
# local-name span downstream for these, so the same honesty indicator the
# frontend shows as a badge is appended here as plain text instead. This
# fires ONLY when there is genuinely no English name to prefer (a DATA gap —
# e.g. lodging has no name_en field in the schema at all, or a POI harvested
# without one) — never when name_en exists but was merely ignored (that's a
# precedence bug, fixed at each call site by preferring name_en directly, the
# same order as itinerary.ts's displayName()).
#
# Allowed ranges: Basic Latin + Latin-1 Supplement + Latin Extended-A/B +
# IPA Extensions + Spacing Modifier Letters (\u0020-\u02FF — widening the old
# \u024F cutoff to \u02FF covers real Latin-script orthographies that live
# just past Extended-B: Azerbaijani schwa (U+0259) and the Uzbek Latin
# okina/turned comma (U+02BB) — genuinely readable precomposed Latin
# letters, not decomposition artifacts, so NFC normalization alone can't fix
# them; the allow-list range itself has to widen — #233 root cause B),
# Combining Diacritical Marks (\u0300-\u036F — #234: NFC only composes a
# base+mark sequence into a precomposed codepoint WHERE ONE EXISTS; it does
# NOT exist for every mark, e.g. Turkish dotted-I (U+0130) case-folds to "i"
# + COMBINING DOT ABOVE (U+0307) with no precomposed "i-with-dot-above"
# letter, and some Lao/Vietnamese romanizations carry a bare macron/dot-below
# (U+0304/U+0323) the same way -- so NFC-normalizing first is not sufficient
# on its own; the residual combining mark must also be allow-listed, same
# fix shape as the #233 root-cause-B range widen. A combining mark can only
# ever ride on a preceding base letter, so allow-listing this block alone
# never masks a genuinely non-Latin string: any non-Latin base character
# it's attached to still fails the allow-list on its own), Latin Extended
# Additional (Vietnamese), whitespace, and the common ASCII / typographic
# punctuation real place names use. Kept in sync with hasNonLatinScript() in
# itinerary.ts — mirror any change there here too (see
# fix/nonlatin-punctuation-fp, #182).
_LATIN_ALLOWED_RE = re.compile(
    "^[\u0020-\u02FF\u0300-\u036F\u1E00-\u1EFF\u2013-\u2014\u2018-\u201F\u2026\\s\\-'.,()&/0-9]*$"
)
_UNREADABLE_SUFFIX = " (shown in original script \u2014 no English name available)"


_ISOLATE_START = "⁨"  # FIRST STRONG ISOLATE (FSI)
_ISOLATE_END = "⁩"  # POP DIRECTIONAL ISOLATE (PDI)


def _iso(s: Any) -> str:
    """Wrap a dynamic, potentially-RTL name in Unicode bidi isolate marks.

    Every label built here sandwiches an interpolated venue/hotel name
    between LTR prose ("Official site — <name>", "search <name> (not a
    confirmed booking)"). With no isolation, a raw RTL name (Hebrew/Arabic)
    embedded in that flat LTR string lets the bidi algorithm reorder the
    neutral characters that abut it (digits, parentheses, the em-dash) against
    the LTR base paragraph, visually scrambling the label (#233 root cause D).
    FSI ... PDI (U+2068 ... U+2069) isolates the wrapped run so its direction
    resolves from its own first strong character without touching the
    characters outside it, and works for LTR names too (a no-op visually).
    Applied ONLY to the label text, never to URL query segments (isolate
    marks must not leak into a percent-encoded query string).
    """
    text = str(s).strip()
    if not text:
        return text
    return f"{_ISOLATE_START}{text}{_ISOLATE_END}"


def _has_non_latin_script(s: str) -> bool:
    """True iff ``s`` contains a codepoint outside the Latin-script allow-list.

    ``s`` is NFC-normalized FIRST: real catalog data is not guaranteed to
    arrive precomposed (server.py NFC-normalizes ``title`` for its own
    Vietnamese matching, and the seed pipeline strips combining marks via
    NFKD elsewhere), so an NFD Vietnamese name — base vowel + horn (U+031B)
    + tone mark (U+0300 etc, Combining Diacritical Marks) — must not be
    misclassified as non-Latin just because it wasn't composed yet (#233
    root cause A).
    """
    return not _LATIN_ALLOWED_RE.match(unicodedata.normalize("NFC", s or ""))


def _with_unreadable_honesty(
    link: dict[str, Any], *, name: str | None, name_en: str | None,
) -> dict[str, Any]:
    """Mutate + return ``link``: append the honest "no English name available"
    suffix to its label iff there is genuinely no name_en AND the shown name
    is non-Latin script. No-op (never fabricates a translation) otherwise.
    """
    if not (name_en or "").strip() and _has_non_latin_script(name or ""):
        link["label"] = f"{link['label']}{_UNREADABLE_SUFFIX}"
    return link


def _provenance(source: str, source_url: str | None) -> dict[str, Any]:
    """A deterministic provenance envelope for a CONSTRUCTED link.

    Uses the static _CONSTRUCTED_AT (no wall-clock). Passes
    contracts.is_valid_provenance.
    """
    return make_provenance(
        source=source,
        fetched_at=_CONSTRUCTED_AT,
        tier=SourceTier.SEEDED.value,
        source_url=source_url,
        ttl=None,
    )


def _link(
    *,
    booking_url: str | None,
    kind: str,
    label: str,
    provenance_source: str,
    providers: list[dict[str, str]] | None = None,
    provenance_url: str | None = None,
) -> dict[str, Any]:
    """Assemble one link entry in the canonical schema (deterministic)."""
    return {
        "booking_url": booking_url,
        "kind": kind,
        "label": label,
        "providers": providers,
        "provenance": _provenance(provenance_source, provenance_url or booking_url),
    }


# ===========================================================================
# Per-kind builders
# ===========================================================================

def lodging_link(hotel_title: str, city: str, *, website: str | None = None) -> dict[str, Any]:
    """Official site if a safe website is known, else a Google Maps SEARCH.

    Never implies a confirmed booking (the UCP checkout is separate). The maps
    fallback is a SEARCH handoff — its label says "search".
    """
    safe = _safe_url(website)
    if safe is not None:
        return _link(
            booking_url=safe,
            kind=KIND_OFFICIAL_SITE,
            label=f"Official site — {_iso(hotel_title)} (not a confirmed booking)",
            provenance_source="booking_links: lodging official_site (constructed)",
        )
    query = " ".join(p for p in (str(hotel_title).strip(), str(city).strip()) if p)
    url = "https://www.google.com/maps/search/?" + _qs([("api", "1"), ("query", query)])
    return _link(
        booking_url=url,
        kind=KIND_MAPS,
        label=f"Find on Google Maps — search {_iso(hotel_title)} (not a confirmed booking)",
        provenance_source="booking_links: lodging maps search (constructed)",
    )


def flight_link(
    from_iata: str,
    to_iata: str,
    depart_date: str | None = None,
    return_date: str | None = None,
) -> dict[str, Any]:
    """A flight META-SEARCH handoff. NO fare, NO price, NO flight number.

    Providers (a SORTED list) deep-link Google Flights + Skyscanner. The
    Skyscanner yymmdd is derived by STRING-SLICING the YYYY-MM-DD (no date
    parsing → no wall-clock, no tz).
    """
    fr = (from_iata or "").strip().upper()
    to = (to_iata or "").strip().upper()

    # Google Flights: a plain search query (NO fares, NO flight numbers).
    if depart_date:
        gq = f"Flights from {fr} to {to} on {str(depart_date).strip()}"
    else:
        gq = f"Flights from {fr} to {to}"
    google_url = "https://www.google.com/travel/flights?" + _qs([("q", gq)])

    # Skyscanner deep path: /transport/flights/<from>/<to>/<yymmdd>/ — yymmdd by
    # STRING SLICING the ISO date (YYYY-MM-DD → YYMMDD), never parsed.
    sky_path = f"https://www.skyscanner.net/transport/flights/{_q(fr)}/{_q(to)}/"
    yymmdd = _yymmdd(depart_date)
    if yymmdd:
        sky_path += f"{_q(yymmdd)}/"
        ret_yymmdd = _yymmdd(return_date)
        if ret_yymmdd:
            sky_path += f"{_q(ret_yymmdd)}/"

    # providers: SORTED by name (deterministic).
    providers = sorted(
        [
            {"name": "Google Flights", "url": google_url},
            {"name": "Skyscanner", "url": sky_path},
        ],
        key=lambda p: p["name"],
    )
    label = f"Compare flights {fr} → {to} (meta-search — fares/availability not guaranteed)"
    return _link(
        booking_url=None,
        kind=KIND_META_SEARCH,
        label=label,
        providers=providers,
        provenance_source="booking_links: flight meta_search (constructed)",
        provenance_url=None,
    )


def _yymmdd(iso_date: str | None) -> str | None:
    """YYYY-MM-DD → YYMMDD by pure STRING SLICING (no date parsing).

    Returns None for anything that is not exactly a 10-char YYYY-MM-DD with
    dashes in the right places and digit fields (conservative — never raises).
    """
    if not isinstance(iso_date, str):
        return None
    s = iso_date.strip()
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return None
    yy, mm, dd = s[2:4], s[5:7], s[8:10]
    if not (yy.isdigit() and mm.isdigit() and dd.isdigit()):
        return None
    return yy + mm + dd


def restaurant_link(
    name: str,
    city: str,
    iso2: str,
    *,
    website: str | None = None,
) -> dict[str, Any]:
    """Official site if a safe website is known, else a maps SEARCH.

    CN seam: iso2 == "CN" → Amap (the operable maps provider inside China);
    otherwise Google Maps.
    """
    safe = _safe_url(website)
    if safe is not None:
        return _link(
            booking_url=safe,
            kind=KIND_OFFICIAL_SITE,
            label=f"Official site — {_iso(name)}",
            provenance_source="booking_links: restaurant official_site (constructed)",
        )
    return _maps_search(name, city, iso2, entity_label="restaurant")


def attraction_link(
    name: str,
    city: str,
    iso2: str,
    *,
    website: str | None = None,
    wikidata: str | None = None,
    wikipedia: str | None = None,
) -> dict[str, Any]:
    """Priority: official_site(website) → reference(wikipedia|wikidata) → maps.

    wikipedia comes as "<lang>:<Title>" (e.g. "en:Eiffel_Tower") OR a bare
    title → en. wikidata is a Q-id.
    """
    safe = _safe_url(website)
    if safe is not None:
        return _link(
            booking_url=safe,
            kind=KIND_OFFICIAL_SITE,
            label=f"Official site — {_iso(name)}",
            provenance_source="booking_links: attraction official_site (constructed)",
        )

    wiki_url = _wikipedia_url(wikipedia)
    if wiki_url is not None:
        return _link(
            booking_url=wiki_url,
            kind=KIND_REFERENCE,
            label=f"Reference — {_iso(name)} (Wikipedia)",
            provenance_source="booking_links: attraction wikipedia reference (constructed)",
        )

    qid = _wikidata_qid(wikidata)
    if qid is not None:
        return _link(
            booking_url="https://www.wikidata.org/wiki/" + _q(qid),
            kind=KIND_REFERENCE,
            label=f"Reference — {_iso(name)} (Wikidata)",
            provenance_source="booking_links: attraction wikidata reference (constructed)",
        )

    return _maps_search(name, city, iso2, entity_label="attraction")


def _maps_search(name: str, city: str, iso2: str, *, entity_label: str) -> dict[str, Any]:
    """Maps SEARCH handoff with the CN (Amap) seam. Deterministic."""
    query = " ".join(p for p in (str(name).strip(), str(city).strip()) if p)
    if (iso2 or "").strip().upper() == "CN":
        url = "https://www.amap.com/search?" + _qs([("query", query)])
        provider = "Amap"
    else:
        url = "https://www.google.com/maps/search/?" + _qs([("api", "1"), ("query", query)])
        provider = "Google Maps"
    return _link(
        booking_url=url,
        kind=KIND_MAPS,
        label=f"Find on {provider} — search {_iso(name)}",
        provenance_source=f"booking_links: {entity_label} maps search (constructed)",
    )


def _wikipedia_url(wikipedia: Any) -> str | None:
    """"<lang>:<Title>" or bare "<Title>" → https://<lang>.wikipedia.org/wiki/<Title>.

    Title spaces → underscores, then %-encoded. Returns None if no usable value.
    """
    if not isinstance(wikipedia, str):
        return None
    raw = wikipedia.strip()
    if not raw:
        return None
    if ":" in raw:
        lang, title = raw.split(":", 1)
        lang = lang.strip().lower()
        title = title.strip()
        if not lang or not title:
            return None
    else:
        lang, title = "en", raw
    seg = _q(title.replace(" ", "_"))
    if not seg:
        return None
    return f"https://{_q(lang)}.wikipedia.org/wiki/{seg}"


def _wikidata_qid(wikidata: Any) -> str | None:
    """A bare Q-id string, or None. (Validation is light — we just need a token.)"""
    if not isinstance(wikidata, str):
        return None
    q = wikidata.strip()
    return q or None


def visa_link(source_url: str | None) -> dict[str, Any] | None:
    """Thin gov_portal wrapper around the URL the COMPLIANCE verdict produced.

    Reuse, never re-derive. None when there is no source URL.
    """
    safe = _safe_url(source_url)
    if safe is None:
        return None
    return _link(
        booking_url=safe,
        kind=KIND_GOV_PORTAL,
        label="Official entry / visa portal (verify requirements yourself)",
        provenance_source="booking_links: visa gov_portal (handoff to compliance source)",
    )


def health_link(source_url: str | None) -> dict[str, Any] | None:
    """Thin cdc_who_portal wrapper around the URL the HEALTH verdict produced.

    Reuse, never re-derive. None when there is no source URL.
    """
    safe = _safe_url(source_url)
    if safe is None:
        return None
    return _link(
        booking_url=safe,
        kind=KIND_CDC_WHO_PORTAL,
        label="Travel-health guidance (CDC/WHO) — verify vaccinations yourself",
        provenance_source="booking_links: health cdc_who_portal (handoff to health source)",
    )


def insurance_note() -> dict[str, Any]:
    """A compare_note. NO vendor plan (the #45 boundary). booking_url is None.

    This is the UAT/seeded default. The PROD edition replaces it with an
    affiliate deeplink via ``_insurance_link_prod``; see ``_resolve_insurance_link``.
    """
    return _link(
        booking_url=None,
        kind=KIND_COMPARE_NOTE,
        label="Compare coverage independently (no vendor plan offered)",
        provenance_source="booking_links: insurance compare_note (no vendor plan, #45)",
    )


def _resolve_insurance_link(
    nationality: str | None = None,
    destination_iso2: str | None = None,
    trip_start: str | None = None,
    trip_end: str | None = None,
) -> dict[str, Any]:
    """Return the insurance handoff link for the current edition.

    UAT / no prod module → ``insurance_note()`` (compare_note, booking_url=None).
    PROD + tg_prod module present → affiliate deeplink keyed on nationality
    (via ``InsuranceLinkProvider.build_link``).

    VAR-0: this function is DISPLAY-ONLY. It must only be called from the
    display/booking-link layer, never from the deterministic plan path. Its output
    lands in ``result["booking_links"]["insurance"]`` only — explicitly excluded
    from the var-0 digest.

    GATING: ``load_prod_factory("make_insurance_provider")`` returns None unless
    ``TG_EDITION=prod`` AND the private ``tg_prod`` module is importable. Any
    error falls back to ``insurance_note()`` — the UAT path is always safe.
    """
    try:
        from providers.insurance_link import get_insurance_link_provider
        provider = get_insurance_link_provider()
        return provider.build_link(
            nationality=nationality,
            destination_iso2=destination_iso2,
            trip_start=trip_start,
            trip_end=trip_end,
        )
    except Exception:  # noqa: BLE001 — display-only; never fatal
        return insurance_note()


# ===========================================================================
# Verdict source-URL extraction (REUSE the URL the verdict already produced)
# ===========================================================================

def _first_verdict_source_url(verdict: Any, list_key: str) -> str | None:
    """Return the first non-empty source_url from verdict[list_key] entries.

    Each entry may carry a top-level "source_url" and/or a provenance dict
    {..., "source_url": ...}. We scan in the existing (deterministic) list
    order. Returns None if none is usable.
    """
    if not isinstance(verdict, dict):
        return None
    entries = verdict.get(list_key)
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("source_url")
        if _safe_url(candidate) is not None:
            return candidate
        prov = entry.get("provenance")
        if isinstance(prov, dict):
            candidate = prov.get("source_url")
            if _safe_url(candidate) is not None:
                return candidate
    return None


# ===========================================================================
# Leg/edge/day-plan correlation helpers (pure, deterministic)
# ===========================================================================

def _leg_index(legs: list[dict], leg_id: str) -> dict | None:
    """Find the leg dict whose leg_id matches (deterministic linear scan)."""
    for leg in legs:
        if isinstance(leg, dict) and leg.get("leg_id") == leg_id:
            return leg
    return None


def _iso2_for_leg(day_plans: list[dict], leg_id: str) -> str:
    """Derive a leg's iso2 from the matching day_plans entry; "" if unknown.

    Legs themselves may lack iso2; the day_plan carries it. Empty → caller
    falls back to the ROW (Google Maps) default.
    """
    for dp in day_plans:
        if isinstance(dp, dict) and dp.get("leg_id") == leg_id:
            return (dp.get("iso2") or "").strip().upper()
    return ""


# ===========================================================================
# THE PUBLIC ENTRY POINT
# ===========================================================================

def build_booking_links(
    result: dict,
    *,
    nationality: str | None = None,
) -> dict[str, Any]:
    """Build the booking_links block AND mutate per-entity links in place.

    PURE + deterministic: reads result["legs"], ["transport_edges"],
    ["day_plans"], ["compliance_verdict"], ["health_verdict"]. Iterates every
    list in its existing (deterministic) order and emits (k,v) pairs in a fixed
    order. NO wall-clock, NO random, NO I/O.

    Returns the booking_links block:
        {"lodging":[...], "transport":[...], "attractions":[...],
         "restaurants":[...], "visa":{}|None, "health":{}|None, "insurance":{}}

    Side effects (per the #37 plan): sets leg["booking_link"], and
    attraction["link"] / meal["link"] inside each day_plan entity.

    Parameters
    ----------
    result : dict
        The negotiation result (deterministic core + display overlays).
    nationality : str | None
        The traveler's home-country ISO-2 code (e.g. "SG", "US"). When provided
        AND ``TG_EDITION=prod``, the insurance entry becomes a nationality-keyed
        an affiliate deeplink instead of the UAT compare_note. The nationality
        is already part of the deterministic request; passing it here for the
        display-only insurance block does NOT perturb var-0.
    """
    legs = result.get("legs") or []
    if not isinstance(legs, list):
        legs = []
    edges = result.get("transport_edges") or []
    if not isinstance(edges, list):
        edges = []
    day_plans = result.get("day_plans") or []
    if not isinstance(day_plans, list):
        day_plans = []

    # -- lodging (one per leg, in leg order) -------------------------------
    lodging: list[dict[str, Any]] = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        hotel_name = leg.get("hotel_title") or leg.get("hotel_id") or ""
        lk = lodging_link(
            hotel_name,
            leg.get("city") or "",
            website=leg.get("website"),
        )
        # Lodging has NO name_en field in the schema at all (a DATA gap, not a
        # precedence bug — see the #168 hotel_title routing note in
        # Itinerary.svelte). Degrade honestly whenever hotel_name itself is
        # non-Latin script, instead of silently baking raw script into the
        # label with no indication (#222).
        _with_unreadable_honesty(lk, name=hotel_name, name_en=None)
        leg["booking_link"] = lk
        lodging.append(lk)

    # -- transport (one per edge, in edge order) ---------------------------
    # Edges lack dates — derive depart/return from the matching legs:
    #   depart_date = from_leg.checkout (when you leave the origin leg)
    #   return_date is left None (one-way handoff; we do not invent a return).
    transport: list[dict[str, Any]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        from_leg = _leg_index(legs, edge.get("from_leg"))
        depart_date = from_leg.get("checkout") if isinstance(from_leg, dict) else None
        fl = flight_link(
            edge.get("from_iata") or "",
            edge.get("to_iata") or "",
            depart_date=depart_date,
            return_date=None,
        )
        transport.append(fl)

    # -- attractions + restaurants (walk day_plans → days → entities) ------
    attractions: list[dict[str, Any]] = []
    restaurants: list[dict[str, Any]] = []
    for dp in day_plans:
        if not isinstance(dp, dict):
            continue
        city = dp.get("city") or ""
        iso2 = (dp.get("iso2") or "").strip().upper()
        for day in dp.get("days") or []:
            if not isinstance(day, dict):
                continue
            for att in day.get("attractions") or []:
                if not isinstance(att, dict):
                    continue
                # #222 fix: PREFER name_en, same order as the frontend's
                # displayName() (name_en || name) — the raw `name` was being
                # preferred here, which baked untranslated non-Latin script
                # verbatim into the (unbadged) link label the UI renders.
                al = attraction_link(
                    att.get("name_en") or att.get("name") or "",
                    city,
                    iso2,
                    website=att.get("website"),
                    wikidata=att.get("wikidata"),
                    wikipedia=att.get("wikipedia"),
                )
                # Residual DATA gap (no name_en harvested at all): degrade
                # honestly instead of silently showing raw script.
                _with_unreadable_honesty(al, name=att.get("name"), name_en=att.get("name_en"))
                att["link"] = al
                attractions.append(al)
            meals = day.get("meals")
            if isinstance(meals, dict):
                # Emit in the canonical meal-slot order (deterministic), never
                # by iterating the dict's runtime key order.
                for slot in _MEAL_SLOTS:
                    meal = meals.get(slot)
                    if not isinstance(meal, dict):
                        continue
                    # #222 fix: same name_en-preferred precedence as attractions above.
                    rl = restaurant_link(
                        meal.get("name_en") or meal.get("name") or "",
                        city,
                        iso2,
                        website=meal.get("website"),
                    )
                    _with_unreadable_honesty(rl, name=meal.get("name"), name_en=meal.get("name_en"))
                    meal["link"] = rl
                    restaurants.append(rl)

    # -- visa / health (reuse the verdict's source URL) --------------------
    visa = visa_link(
        _first_verdict_source_url(result.get("compliance_verdict"), "per_leg")
    )
    health = health_link(
        _first_verdict_source_url(result.get("health_verdict"), "per_destination")
    )

    # -- insurance: compare_note (UAT) or affiliate deeplink (PROD) -----
    # Derive first-leg destination iso2 for region-specific deeplink targeting.
    first_leg_iso2: str | None = None
    first_leg_start: str | None = None
    first_leg_end: str | None = None
    if legs:
        fl = next((l for l in legs if isinstance(l, dict)), None)
        if fl:
            first_leg_iso2 = (fl.get("dest_country") or fl.get("iso2") or "").upper() or None
            first_leg_start = fl.get("checkin") or None
            first_leg_end = fl.get("checkout") or None
    insurance = _resolve_insurance_link(
        nationality=nationality,
        destination_iso2=first_leg_iso2,
        trip_start=first_leg_start,
        trip_end=first_leg_end,
    )

    block: dict[str, Any] = {
        "lodging": lodging,
        "transport": transport,
        "attractions": attractions,
        "restaurants": restaurants,
        "visa": visa,
        "health": health,
        "insurance": insurance,
    }
    result["booking_links"] = block
    return block


# Canonical meal-slot order (mirrors day_planner_agent._MEAL_SLOTS). Kept local
# to keep this module pure (no day_planner import → no catalog/OSM load).
_MEAL_SLOTS = ("breakfast", "lunch", "tea", "dinner", "supper")
