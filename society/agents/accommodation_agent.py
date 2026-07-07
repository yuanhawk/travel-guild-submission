"""
accommodation_agent.py — Accommodation A2A agent (Travel Guild M2/M3).

Design contract: AGENT-SOCIETY-A2A-DESIGN.md §3.4, §4.6, §10.11.

Agent Card skill: ``accommodation.propose``

Input (data part, JSON):
    {
        "city":             str,
        "checkin":          str,
        "checkout":         str,
        "adults":           int,
        "max_cents":        int,
        "vibe":             str         (optional),
        "target_areas":     list[str]   (optional)   # M-Agentic-2: restrict to areas
        "preference_hint":  str         (optional)   # M-Agentic-3: free-text hint
        "prefer_lodging_types": list[str] (optional) # persona bundle: float these lodging_types to the head
        "avoid_lodging_types":  list[str] (optional) # persona bundle: sink these lodging_types to the tail
    }

M-Agentic-2 area filtering:
    When ``target_areas`` is supplied (from the Destination agent), candidates
    are filtered to hotels whose AREA — parsed deterministically from the hotel
    id prefix (``<city>-<area>-<name>``) — is in the target set.  Filtering
    happens AFTER the merchant search (real catalog rows only) and BEFORE
    ranking.  If no candidate is in the target areas, the result is ``no_fit``
    (the orchestrator's deterministic broaden/city-wide fallback then applies —
    NO LLM in the loop).  Single-area cities are unaffected (their one area
    always matches).

M-Agentic-3 LLM ranking (§10.11 variance-clamped hybrid):
    After the merchant search + area filter produce the REAL candidate list, an
    LLM (qwen3-max via DashScope) ranks those candidates by (vibe, preference).
    The ranking is STRICTLY clamped:
      - LLM output is treated as a permutation of the input hotel_ids ONLY.
      - Any hotel_id not in the real candidate set is DROPPED.
      - Any hotel_id the LLM omitted is APPENDED in deterministic
        review_score-desc order.
      - Result is ALWAYS exactly the input candidate set, reordered.
      - The LLM can NEVER invent a hotel, price, or availability.
    Pick logic (budget-safe, deterministic):
      - Select the highest-LLM-ranked candidate whose total_cents ≤ max_cents.
      - Budget safety is structural; never trust LLM ordering to be budget-safe.
    Deterministic fallback:
      - If LLM fails / returns garbage / empty → rank by review_score desc
        (current behaviour).  Worst-case = current deterministic floor.
    One-shot per leg:
      - Rank ONCE when candidates are first produced.
      - Orchestrator re-plan/ceiling-tightening re-filters the already-ranked
        list DETERMINISTICALLY.  NO LLM call inside the re-plan loop.
      - No compounding.

Output artifact (data part, JSON) — typed AccommodationProposal:
    {
        "type":       "accommodation_proposal",
        "city":       str,
        "checkin":    str,
        "checkout":   str,
        "adults":     int,
        "max_cents":  int,
        "fit":        "ok" | "no_fit",
        "proposal":   {                          # null when fit == "no_fit"
            "hotel_id":    str,
            "title":       str,
            "total_cents": int,
            "review_score": float,
            "star_rating": float,
            "amenities":   list[str],
            "ranking_source": "llm" | "fallback"
        } | null,
        "alternates": list[dict]                 # up to 2 further options, [] when no_fit
    }

On re-invocation with a lower max_cents the merchant search_catalog re-filters,
so cheaper options surface automatically — no special state needed.

§4.6 reliability:
  - Real catalog only: data comes directly from merchant search_catalog.
  - Explicit no_fit signal: never fabricates when nothing fits.
  - Typed result schema.
  - Accepts optional injected merchant_transport for testing (same pattern as BudgetAgent).
  - LLM ranking is variance-clamped: output ⊆ input candidate set always.
  - Budget safety is structural (max_cents check), never delegated to LLM.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any

import httpx
from utils import ucp_signing
import uvicorn

from agents.a2a_agent import (
    A2AAgent,
    _data_part,
    _new_artifact,
)
from agents.destination_agent import parse_area_from_hotel_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# #36 Accommodation Types (Tier A) — derive lodging_type from the id slug.
# ---------------------------------------------------------------------------
# The harvester (reference/osm_pbf_ingest.py:181) encodes the non-hotel OSM
# tourism= value as a slug segment in the id:
#     seg = "" if lodging_type == "hotel" else f"{slug(lodging_type)}-"
#     hid = f"{slug(name)}-{seg}{slug(nm)}"[:60]
# slug() folds "_" -> "-", so real catalog ids carry "-hostel-/-guest-house-/
# -apartment-/-motel-"; plain hotels carry NO segment.  This is a PURE string
# parse (no wall-clock/random/dict-iteration on the output path — the segment
# table is an ORDERED tuple), mirroring parse_area_from_hotel_id.
#
# HONESTY / fail-conservative: an id with no recognised segment is typed
# "hotel" (the correct OSM default — osm_pbf_ingest seg="" for tourism=hotel).
# Tier A NEVER claims resort/villa/chalet: resort was a derived boolean that
# was never slug-encoded, and villa/chalet are not in LODGING_TYPES at all, so
# claiming them would fabricate.  Type is REPORTED, never INVENTED.
_LODGING_TYPE_SEGMENTS: tuple[tuple[str, str], ...] = (
    ("-hostel-", "hostel"),
    ("-guest-house-", "guest_house"),  # slug("guest_house") == "guest-house"
    ("-guest_house-", "guest_house"),  # tolerate the underscore form too
    ("-apartment-", "apartment"),
    ("-motel-", "motel"),
)


def _lodging_type_from_id(hotel_id: str) -> str:
    """Derive the lodging type from a hotel id slug (Tier A, #36).

    Returns one of {hostel, guest_house, apartment, motel, hotel}.  Pure parse;
    default "hotel" when no recognised type segment is present (the OSM default).
    """
    if not hotel_id or not isinstance(hotel_id, str):
        return "hotel"
    hid = hotel_id.strip().lower()
    for seg, ltype in _LODGING_TYPE_SEGMENTS:
        if seg in hid:
            return ltype
    return "hotel"

# ---------------------------------------------------------------------------
# Merchant MCP endpoint
# ---------------------------------------------------------------------------

MERCHANT_MCP_URL = os.environ.get(
    "MERCHANT_MCP_URL",
    "http://ucp-merchant:8090/api/ucp/mcp",
)

_MERCHANT_TIMEOUT = float(os.environ.get("MERCHANT_TIMEOUT", "15"))

# ---------------------------------------------------------------------------
# Alternates cap (finding #32)
# ---------------------------------------------------------------------------
# The orchestrator's DP quality-candidate set is built from proposal + alternates,
# so larger values expose more quality diversity at scale (14.8k-hotel catalog).
# Default: 2 (backward-compat with orchestrator.py:676-719 which consumes
# proposal + up to 2 alternates).  Override via ACCOMMODATION_ALTERNATES_CAP env
# to scale with catalog size (e.g. "5" for a richer DP pool).
_ALTERNATES_CAP: int = max(0, int(os.environ.get("ACCOMMODATION_ALTERNATES_CAP", "2")))

# #76 — cap the LLM ranking INPUT. A megacity metro now surfaces ~200-350 candidates (ward
# aggregation), which times out the DashScope ranking call ("read operation timed out") and breaks
# LLM-on negotiate. We deterministically pre-rank the full set and only LLM-rank the top N; the
# remainder keeps that deterministic order. Static → var-0 (and LLM-off output is byte-identical:
# pre-rank(all) == head+tail, and the clamp fallback re-sorts the head into the same order).
_LLM_RANK_CAP: int = max(1, int(os.environ.get("ACCOMMODATION_LLM_RANK_CAP", "30")))

# ---------------------------------------------------------------------------
# M-Agentic-3: LLM ranking via DashScope
# ---------------------------------------------------------------------------
# Configuration — from env, never hardcoded.

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_RANKING_MODEL = os.environ.get("SOCIETY_LLM_MODEL", "qwen3-max")  # cheap model for test sweeps

# (public showcase repo: generic placeholder for the tuned production prompt --
# see the note in destination_agent.py's _LLM_SYSTEM_PROMPT. Schema unchanged.)
_RANKING_SYSTEM_PROMPT = """Rank the given candidate hotel IDs best-to-worst for the traveller's vibe/preference, using each candidate's lodging_type as a signal.

Output ONLY valid JSON: {"ranked_ids": [<hotel_id string>, ...]}. Include every provided ID exactly once, no invented IDs, no other fields -- ranking is a preference hint only, budget is enforced separately.
"""


def _build_ranking_user_prompt(
    candidates: list[dict],
    vibe: str | None,
    preference_hint: str | None,
) -> str:
    """Build the user prompt for the ranking LLM call."""
    hotel_summaries = []
    for c in candidates:
        hotel_summaries.append({
            "hotel_id": c.get("hotel_id", ""),
            "title": c.get("title", c.get("hotel_id", "")),
            "area": c.get("area") or "",
            "star_rating": c.get("star_rating", 0),
            "review_score": c.get("review_score", 0),
            "amenities": c.get("amenities") or [],
            "total_cents": c.get("total_cents", 0),
            # #36: deterministic lodging type so the ranker can honour a vibe
            # like "backpacker"/"family apartment".  Variance-clamped: the LLM
            # still only returns a permutation of real ids.
            "lodging_type": c.get("lodging_type") or "",
        })
    return json.dumps({
        "candidates": hotel_summaries,
        "vibe": vibe or "any",
        "preference_hint": preference_hint or "",
    })


def _llm_rank_hotels(
    candidates: list[dict],
    vibe: str | None,
    preference_hint: str | None,
) -> list[str] | None:
    """
    Call qwen3-max via DashScope to rank candidates.

    Returns a raw (UNVALIDATED) list of hotel_id strings, or None on failure.
    Never raises — errors are logged and None is returned (caller uses fallback).

    Security: DASHSCOPE_API_KEY is read from env; never hardcoded.
    gitleaks-safe: no raw API keys in code.
    """
    if not DASHSCOPE_API_KEY:
        logger.info(
            "accommodation.ranking: DASHSCOPE_API_KEY not set — using deterministic fallback"
        )
        return None

    try:
        from utils.model_router import dashscope_chat
    except ImportError:
        from model_router import dashscope_chat  # type: ignore[no-redef]

    body = {
        "enable_thinking": False,  # qwen3.x reasoning models: skip thinking → fast ranking
        "messages": [
            {"role": "system", "content": _RANKING_SYSTEM_PROMPT},
            {"role": "user", "content": _build_ranking_user_prompt(
                candidates, vibe, preference_hint
            )},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        data = dashscope_chat("default", body, timeout=30.0)
        content = data["choices"][0]["message"]["content"]
        raw_text = content if isinstance(content, str) else json.dumps(content)
    except Exception as exc:
        logger.warning("accommodation.ranking: LLM call failed: %s", exc)
        return None

    # Parse response
    try:
        text = raw_text.strip()
        # Strip markdown fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
        text = text.strip()
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            ranked = parsed.get("ranked_ids")
        elif isinstance(parsed, list):
            ranked = parsed
        else:
            ranked = None
        if isinstance(ranked, list):
            return [r for r in ranked if isinstance(r, str)]
        logger.warning(
            "accommodation.ranking: LLM response missing ranked_ids: %r", raw_text[:200]
        )
        return None
    except Exception as exc:
        logger.warning(
            "accommodation.ranking: LLM response parse failed: %s — %r",
            exc, raw_text[:200],
        )
        return None


def _clamp_ranking(
    llm_ids: list[str] | None,
    candidates: list[dict],
) -> tuple[list[dict], str]:
    """
    VARIANCE CLAMP: enforce that the final ranked list is exactly the input
    candidate set, reordered.

    Algorithm (§10.11 M-Agentic-3):
      1. Build the authoritative set from real candidates.
      2. If llm_ids is None/empty/garbage → deterministic fallback (review_score desc).
      3. Walk llm_ids in order: accept ids present in candidate set, drop unknowns.
      4. Append any candidate ids the LLM omitted, in deterministic review_score-desc order.
      5. Result is ALWAYS exactly len(candidates) entries — the complete input set, reordered.

    Returns (ranked_candidates: list[dict], source: "llm" | "fallback").
    """
    # Index real candidates by hotel_id
    candidate_by_id: dict[str, dict] = {}
    for c in candidates:
        hid = c.get("hotel_id", "")
        if hid:
            candidate_by_id[hid] = c

    if not llm_ids:
        # LLM returned nothing useful → deterministic fallback
        # D6 var-0: stable tiebreak on hotel_id so equal review_scores are
        # byte-identical across processes regardless of PYTHONHASHSEED.
        ranked = sorted(
            candidate_by_id.values(),
            key=lambda r: (-float(r.get("review_score", 0)), r.get("hotel_id", "")),
        )
        logger.info(
            "accommodation.ranking: LLM returned empty/None — deterministic fallback "
            "(review_score desc, hotel_id tiebreak), %d candidates",
            len(ranked),
        )
        return ranked, "fallback"

    # Step 3: accept LLM ids that are real; log and drop invented ones
    seen: set[str] = set()
    ranked: list[dict] = []
    for hid in llm_ids:
        if hid in candidate_by_id and hid not in seen:
            ranked.append(candidate_by_id[hid])
            seen.add(hid)
        elif hid not in candidate_by_id:
            logger.info(
                "accommodation.ranking: clamp dropped invented hotel_id %r (not in candidate set)",
                hid,
            )
        # duplicate ids in LLM output are silently skipped (seen check)

    # Step 4: append omitted candidates in deterministic review_score-desc order
    # D6 var-0: stable tiebreak on hotel_id for equal review_scores.
    omitted = sorted(
        (c for c in candidate_by_id.values() if c.get("hotel_id", "") not in seen),
        key=lambda r: (-float(r.get("review_score", 0)), r.get("hotel_id", "")),
    )
    if omitted:
        omitted_ids = [c.get("hotel_id") for c in omitted]
        logger.info(
            "accommodation.ranking: clamp appended %d omitted hotel(s) at tail "
            "(review_score-desc): %s",
            len(omitted), omitted_ids,
        )
        ranked.extend(omitted)

    source = "llm" if seen else "fallback"
    logger.info(
        "accommodation.ranking: clamped ranking: %d hotels, source=%s",
        len(ranked), source,
    )
    return ranked, source


def _occupancy_ok(c: dict, adults: int) -> bool:
    """
    Defense-in-depth max_occupancy re-validation (MED, mirrors merchant checkout
    + critic OVER_CAPACITY, server.py:141 / critic_agent.py:746).

    The merchant checkout REJECTS adults > max_occupancy at commit
    (exceeds_room_capacity).  When the catalog row carries a usable max_occupancy
    (>0) AND it cannot seat the party, this candidate must never be selected — it
    would VOID after the single human consent.

    "if available": absence (missing/<=0) is NOT treated as a hard fail here — that
    is the Critic's conservative-FLAG job (it flags unprovable capacity).  Dropping
    every catalog row that omits the field would manufacture false no_fits.  We only
    drop rows we can PROVE are over-capacity.

    Returns True if the row may be selected, False if it is provably over-capacity.
    """
    try:
        max_occ = int(c.get("max_occupancy") or 0)
    except (TypeError, ValueError):
        max_occ = 0
    if max_occ > 0 and adults > max_occ:
        logger.info(
            "accommodation.propose: skipping over-capacity candidate %s "
            "(adults=%d > max_occupancy=%d — merchant would VOID at checkout)",
            c.get("hotel_id", "?"), adults, max_occ,
        )
        return False
    return True


def _pick_within_ceiling(
    ranked_candidates: list[dict],
    max_cents: int,
    adults: int = 1,
) -> dict | None:
    """
    Deterministic pick: first candidate in LLM-ranked order whose total_cents ≤ max_cents.

    Budget safety is structural — we check every candidate regardless of LLM order.

    PRICE-UNKNOWN GUARD (finding #9): candidates with missing, null, or <=0 total_cents
    are SKIPPED — they must never be selected as a winner.  A row with no price signal
    is treated as price-unknown and dropped, not as "free" ($0).

    OVER-CAPACITY GUARD (MED defense-in-depth): candidates whose known max_occupancy
    cannot seat the party are SKIPPED — the merchant would VOID them at checkout.

    Returns the winning candidate dict or None if none fit.
    """
    for c in ranked_candidates:
        raw = c.get("total_cents")
        try:
            cents = int(raw)
        except (TypeError, ValueError):
            cents = 0
        if cents <= 0:
            logger.info(
                "accommodation.propose: skipping price-unknown candidate %s "
                "(total_cents=%r)",
                c.get("hotel_id", "?"), raw,
            )
            continue
        if not _occupancy_ok(c, adults):
            continue
        if cents <= max_cents:
            return c
    return None


def _rank_candidates(
    candidates: list[dict],
    vibe: str | None,
    preference_hint: str | None,
) -> tuple[list[dict], str]:
    """
    M-Agentic-3: LLM-rank the candidates list, then apply the variance clamp.

    ONE-SHOT: called once per leg when candidates are first produced.
    The orchestrator's re-plan loop NEVER calls this — it re-filters the
    already-ranked list deterministically.

    Returns (ranked_candidates, source) where source ∈ {"llm", "fallback"}.
    """
    # #76 — a megacity metro can surface ~300 candidates → the LLM ranking call times out. Deterministically
    # pre-rank (the SAME key the clamp fallback uses: review_score desc, hotel_id tiebreak) and LLM-rank only
    # the top _LLM_RANK_CAP; the remainder keeps that deterministic order. The strongest candidates (highest
    # review_score) are always in the head, so the booked pick still gets the LLM treatment.
    if len(candidates) > _LLM_RANK_CAP:
        pre = sorted(candidates, key=lambda r: (-float(r.get("review_score", 0)), r.get("hotel_id", "")))
        head, tail = pre[:_LLM_RANK_CAP], pre[_LLM_RANK_CAP:]
        llm_ids = _llm_rank_hotels(head, vibe, preference_hint)
        ranked_head, source = _clamp_ranking(llm_ids, head)
        return ranked_head + tail, source
    llm_ids = _llm_rank_hotels(candidates, vibe, preference_hint)
    return _clamp_ranking(llm_ids, candidates)


# ---------------------------------------------------------------------------
# Merchant call helper
# ---------------------------------------------------------------------------

def _search_catalog(
    client: httpx.Client,
    city: str,
    checkin: str,
    checkout: str,
    adults: int,
    max_cents: int,
    _meta_out: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Call the merchant MCP search_catalog tool and return the results list.

    Returns an empty list if no hotels fit or on error. If ``_meta_out`` is given, it is
    populated with response metadata — notably ``city_available_count`` (#70: city rows BEFORE
    the price filter), so the caller can tell a genuine inventory gap from an over-budget one.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {
            "name": "search_catalog",
            "arguments": {
                "meta": {"autonomy_level": "L2"},
                "query": {
                    "city": city,
                    "checkin": checkin,
                    "checkout": checkout,
                    "adults": adults,
                    "max_cents": max_cents,
                },
            },
        },
    }
    try:
        # #53 — sign the request (RFC 9421) IFF a client signing key is configured;
        # otherwise json=payload exactly as before (unsigned → byte-identical).
        _body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        _sig = ucp_signing.signed_headers("POST", MERCHANT_MCP_URL, _body)
        if _sig:
            resp = client.post(MERCHANT_MCP_URL, content=_body,
                               headers={"Content-Type": "application/json", **_sig},
                               timeout=_MERCHANT_TIMEOUT)
        else:
            resp = client.post(MERCHANT_MCP_URL, json=payload, timeout=_MERCHANT_TIMEOUT)
        http_code = resp.status_code
        body = resp.json()
    except Exception as exc:
        logger.error("accommodation.propose: merchant call failed: %s", exc)
        return []

    rpc_result = body.get("result", {})
    structured = rpc_result.get("structuredContent", {})
    results = structured.get("results", [])
    if _meta_out is not None:
        _meta_out["city_available_count"] = int(structured.get("city_available_count", 0) or 0)
    logger.info(
        "accommodation.propose: search_catalog city=%s max=%d¢ HTTP %d → %d results",
        city, max_cents, http_code, len(results),
    )
    return results if isinstance(results, list) else []


def _filter_by_area(
    results: list[dict[str, Any]],
    target_areas: list[str] | None,
    city: str,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Filter catalog rows to those whose parsed AREA is in target_areas.

    Area is parsed deterministically from the hotel id prefix
    (``<city>-<area>-<name>``) via parse_area_from_hotel_id.  When target_areas
    is empty/None, no filtering is applied (returns results unchanged).

    Rows whose area cannot be parsed are DROPPED when a target set is active
    (we never guess an area into the target set — that would leak variance into
    the area-is-vibe-appropriate correctness invariant).

    CONSERVATIVE NO-OP GUARD (findings #33/#36): when target_areas is supplied
    but the city's area closed-set is unknown (ALL hotel ids parse to None —
    i.e. area resolution for this city is not implemented), dropping every hotel
    would be a false no_fit.  Instead we detect this and return the full
    unfiltered list with area_set_unknown=True so the caller can log a flag
    without silently killing all candidates.

    Returns:
        (filtered_results, area_set_unknown)
        area_set_unknown=True means the filter was a no-op due to unknown area
        mapping; the caller MUST log an advisory flag.
    """
    if not target_areas:
        return results, False
    targets = {a.strip().lower() for a in target_areas if isinstance(a, str)}
    if not targets:
        return results, False

    # Single-area city fast-path: the merchant search already constrains by city
    # (+ its megacity wards), so every returned hotel IS in the city's one area.
    # Applying parse_area_from_hotel_id here would drop ward hotels whose catalog
    # area field is the ward name (e.g. "Bang Khae") rather than "bangkok" — a
    # false no_fit.  The search already did the right spatial filter; skip re-filtering.
    from agents.destination_agent import SINGLE_AREA_CITIES  # local to avoid circ-import
    city_lower = city.strip().lower() if city else ""
    if city_lower in SINGLE_AREA_CITIES and targets == {SINGLE_AREA_CITIES[city_lower]}:
        return results, False

    kept: list[dict[str, Any]] = []
    dropped_unknown_area: list[str] = []
    for r in results:
        # C3: the catalog carries an authoritative per-row "area" — use it as the
        # SOURCE OF TRUTH.  parse_area_from_hotel_id only falls back to id-parsing
        # when catalog_area is absent (legacy ids).
        area = parse_area_from_hotel_id(
            r.get("hotel_id", ""), city=city, catalog_area=r.get("area"),
        )
        if area is not None and area in targets:
            kept.append(r)
        else:
            if area is None:
                dropped_unknown_area.append(r.get("hotel_id", "?"))
            logger.info(
                "accommodation.propose: area filter dropped %s (area=%s, targets=%s)",
                r.get("hotel_id", "?"), area, sorted(targets),
            )

    # Conservative no-op: if we kept NOTHING and EVERY dropped row had an
    # unknown (None) area, the city's area system is simply not seeded — fall
    # back to the unfiltered list and flag the gap rather than returning no_fit.
    if not kept and dropped_unknown_area and len(dropped_unknown_area) == len(results):
        logger.warning(
            "accommodation.propose: area filter no-op for city=%r — all %d hotel(s) "
            "have unknown area (area closed-set not seeded for this city); "
            "returning full unfiltered list with area_set_unknown flag "
            "(findings #33/#36 conservative no-op)",
            city, len(results),
        )
        return results, True

    return kept, False


# ---------------------------------------------------------------------------
# AccommodationAgent
# ---------------------------------------------------------------------------

class AccommodationAgent(A2AAgent):
    """
    Accommodation A2A agent (Travel Guild M2).

    Implements the ``accommodation.propose`` skill: searches the merchant catalog
    for hotels fitting the per-leg budget cap and returns the top proposal plus
    alternates. Returns an explicit ``no_fit`` when nothing fits — never fabricates.

    On re-invocation with a lower max_cents the merchant re-filters, so cheaper
    options surface automatically (the veto-recovery path).

    Args:
        host:              Bind host for the ASGI server.
        port:              Bind port for the ASGI server.
        merchant_transport: Optional httpx.BaseTransport to inject for testing.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9103,
        merchant_transport: "httpx.BaseTransport | None" = None,
    ) -> None:
        self._host = host
        self._port = port
        self._merchant_transport = merchant_transport
        super().__init__()

    # ------------------------------------------------------------------
    # A2AAgent protocol
    # ------------------------------------------------------------------

    def _build_card(self) -> dict:
        url = f"http://{self._host}:{self._port}"
        return {
            "name": "accommodation-agent",
            "description": (
                "Proposes per-leg lodging within budget cap, area, and vibe preferences. "
                "Searches the live UCP merchant catalog (real catalog only — no fabricated "
                "prices). M-Agentic-3: applies LLM hotel ranking (qwen3-max, variance-"
                "clamped to the real candidate set) before selecting the best option. "
                "Returns top proposal + alternates, or explicit no_fit if nothing "
                "fits under max_cents. Re-invocation with lower max_cents surfaces cheaper "
                "options (drives veto-recovery). "
                "Implements A2A skill 'accommodation.propose'. "
                "Part of the Travel Guild multi-agent pipeline (Track 3, M2/M3)."
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
                    "id": "accommodation.propose",
                    "name": "Propose Accommodation",
                    "description": (
                        "Given {city, checkin, checkout, adults, max_cents, vibe?, "
                        "target_areas?, preference_hint?}, search the merchant catalog, "
                        "apply LLM hotel ranking (qwen3-max, variance-clamped), and return "
                        "the top-ranked hotel within max_cents plus up to 2 alternates. "
                        "Returns fit='no_fit' if no hotel fits. Real catalog only — "
                        "never fabricates prices or availability. LLM ranking is a "
                        "preference hint only; budget ceiling is enforced structurally."
                    ),
                    "tags": ["accommodation", "lodging", "catalog", "booking", "cents"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string"},
                            "checkin": {"type": "string"},
                            "checkout": {"type": "string"},
                            "adults": {"type": "integer"},
                            "max_cents": {"type": "integer"},
                            "vibe": {"type": "string"},
                            "target_areas": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "avoid_lodging_types": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "prefer_lodging_types": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "preference_hint": {"type": "string"},
                        },
                        "required": ["city", "checkin", "checkout", "max_cents"],
                    },
                    "examples": [
                        (
                            '{"city":"bali","checkin":"2025-10-01","checkout":"2025-10-04",'
                            '"adults":1,"max_cents":50000,"vibe":"culture"}'
                        ),
                    ],
                }
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("accommodation.propose", self._propose_handler)

    # ------------------------------------------------------------------
    # Skill handler
    # ------------------------------------------------------------------

    async def _propose_handler(self, message: dict, task: dict) -> dict:
        """
        accommodation.propose skill handler.

        Calls merchant search_catalog, applies M-Agentic-3 LLM ranking (variance-
        clamped), returns typed proposal.
        """
        payload = self._extract_payload(message)
        if payload is None:
            raise ValueError(
                "accommodation.propose requires a data part with JSON payload "
                "{city, checkin, checkout, max_cents, ...}"
            )

        city: str = payload.get("city", "")
        checkin: str = payload.get("checkin", "")
        checkout: str = payload.get("checkout", "")
        _raw_adults = payload.get("adults")
        if _raw_adults is None:
            # adults omitted: conservative clamp to 1 and log (finding #56).
            # We do NOT silently assume 1 without a trace — operators must be
            # able to audit the assumption.
            adults = 1
            logger.warning(
                "accommodation.propose: 'adults' not supplied — clamping to 1 "
                "(conservative minimum); caller should supply adults explicitly"
            )
        else:
            adults = int(_raw_adults)
            if adults <= 0:
                # Explicitly invalid value: raise rather than silently assume.
                raise ValueError(
                    f"accommodation.propose: 'adults' must be >= 1, got {_raw_adults!r}"
                )
        max_cents: int = int(payload.get("max_cents", 0))
        vibe: str | None = payload.get("vibe") or None
        preference_hint: str | None = payload.get("preference_hint") or None
        raw_target_areas = payload.get("target_areas")
        target_areas: list[str] | None = (
            [a for a in raw_target_areas if isinstance(a, str)]
            if isinstance(raw_target_areas, list)
            else None
        )
        raw_avoid_lodging_types = payload.get("avoid_lodging_types")
        avoid_lodging_types: list[str] | None = (
            [t for t in raw_avoid_lodging_types if isinstance(t, str)]
            if isinstance(raw_avoid_lodging_types, list)
            else None
        )
        raw_prefer_lodging_types = payload.get("prefer_lodging_types")
        prefer_lodging_types: list[str] | None = (
            [t for t in raw_prefer_lodging_types if isinstance(t, str)]
            if isinstance(raw_prefer_lodging_types, list)
            else None
        )

        if not city:
            raise ValueError("city is required")
        if not checkin or not checkout:
            raise ValueError("checkin and checkout are required")
        if max_cents <= 0:
            raise ValueError("max_cents must be > 0")

        # Call merchant catalog (+ M-Agentic-3 LLM ranking)
        result_data = self._call_catalog(
            city=city,
            checkin=checkin,
            checkout=checkout,
            adults=adults,
            max_cents=max_cents,
            vibe=vibe,
            target_areas=target_areas,
            preference_hint=preference_hint,
            avoid_lodging_types=avoid_lodging_types,
            prefer_lodging_types=prefer_lodging_types,
        )

        return _new_artifact(
            name="accommodation.propose.result",
            parts=[_data_part(result_data)],
        )

    # ------------------------------------------------------------------
    # Merchant integration
    # ------------------------------------------------------------------

    def _call_catalog(
        self,
        *,
        city: str,
        checkin: str,
        checkout: str,
        adults: int,
        max_cents: int,
        vibe: str | None,
        target_areas: list[str] | None = None,
        preference_hint: str | None = None,
        avoid_lodging_types: list[str] | None = None,
        prefer_lodging_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Search the merchant catalog and return a typed AccommodationProposal.

        M-Agentic-3: after merchant search + M-Agentic-2 area filter produce the
        REAL candidate list, apply LLM ranking (variance-clamped) then pick the
        highest-ranked candidate within max_cents.  Deterministic fallback if LLM
        fails.  NO LLM inside the orchestrator re-plan loop.
        """
        client_kwargs: dict[str, Any] = {"timeout": _MERCHANT_TIMEOUT}
        if self._merchant_transport is not None:
            client_kwargs["transport"] = self._merchant_transport

        _search_meta: dict[str, Any] = {}
        with httpx.Client(**client_kwargs) as client:
            results = _search_catalog(
                client, city, checkin, checkout, adults, max_cents, _meta_out=_search_meta
            )

        # M-Agentic-2: filter to the Destination-agent target areas (if any).
        # Deterministic — parses area from hotel id prefix; no LLM in the loop.
        # _filter_by_area now returns (filtered_list, area_set_unknown_flag) to
        # implement the conservative no-op guard (findings #33/#36): when the
        # city's area closed-set is not seeded, fall back to the full list and
        # flag the gap rather than silently dropping all candidates (→ false no_fit).
        area_set_unknown: bool = False
        # Persona-fit deterministic filter/boost state (avoid_lodging_types /
        # prefer_lodging_types). Declared here (not just at the enrichment site
        # below) so it is defined for every return path, including the early
        # no-results return before enrichment runs.
        lodging_type_relaxed: bool = False
        if target_areas:
            before = len(results)
            results, area_set_unknown = _filter_by_area(results, target_areas, city)
            logger.info(
                "accommodation.propose: area filter city=%s targets=%s %d → %d "
                "(area_set_unknown=%s)",
                city, target_areas, before, len(results), area_set_unknown,
            )

        base = {
            "type": "accommodation_proposal",
            "city": city,
            "checkin": checkin,
            "checkout": checkout,
            "adults": adults,
            "max_cents": max_cents,
        }

        if not results:
            # #70 honesty: empty after search (+ area filter). search_catalog pre-filters by
            # max_cents, so an empty list alone cannot tell a genuine inventory gap from an
            # over-budget shortfall. The merchant returns city_available_count (city rows
            # BEFORE the price filter): >0 → the city IS stocked, so the cause is the budget;
            # 0 → a genuine inventory gap. No extra merchant call.
            reason_code = (
                "over_budget" if _search_meta.get("city_available_count", 0) > 0
                else "no_inventory"
            )
            logger.info(
                "accommodation.propose: no_fit city=%s max=%d¢ reason=%s (city_avail=%s)",
                city, max_cents, reason_code, _search_meta.get("city_available_count"),
            )
            return {
                **base,
                "fit": "no_fit",
                "reason_code": reason_code,
                "proposal": None,
                "alternates": [],
                "area_set_unknown": area_set_unknown,
                "lodging_type_relaxed": lodging_type_relaxed,
            }

        # -----------------------------------------------------------------------
        # M-Agentic-3: LLM ranking — ONE-SHOT, VARIANCE-CLAMPED.
        #
        # _rank_candidates calls qwen3-max once to rank the real candidate list
        # by (vibe, preference_hint), then _clamp_ranking enforces:
        #   - Only real candidate ids survive.
        #   - Any id omitted by LLM is appended in deterministic review_score-desc
        #     order.
        #   - Result is always exactly the input set, reordered.
        #
        # This call is ONE-SHOT: it happens here, before the orchestrator's
        # re-plan loop.  The re-plan loop re-filters the result deterministically
        # by calling us again with a lower max_cents (the merchant re-filters the
        # catalog; we rank the new candidate set once more).  There is no LLM call
        # inside the orchestrator's re-plan iterations.
        # -----------------------------------------------------------------------

        # Enrich results with parsed area tag before passing to LLM (area is
        # informative context for the ranking prompt, not a constraint here).
        enriched: list[dict[str, Any]] = []
        for r in results:
            hid = r.get("hotel_id", "")
            enriched.append({
                **r,
                # C3: catalog per-row "area" is the source of truth; id-parse is a
                # fallback only when the catalog area is absent.
                "area": parse_area_from_hotel_id(
                    hid, city=city, catalog_area=r.get("area"),
                ),
                # #36: prefer an explicit catalog lodging_type if/when the field
                # is restored (Tier B); id-slug parse is the Tier-A fallback —
                # same catalog-field-first / id-parse-fallback pattern as area.
                "lodging_type": r.get("lodging_type") or _lodging_type_from_id(hid),
            })

        # Persona-fit deterministic filter (avoid_lodging_types): applied BEFORE
        # ranking so an excluded type never reaches the LLM/fallback ranking step.
        # Conservative — never manufacture a false no_fit by excluding every
        # candidate; relax (keep all) and flag instead.
        if avoid_lodging_types:
            avoid_set = {t.strip().lower() for t in avoid_lodging_types if isinstance(t, str)}
            filtered = [c for c in enriched if (c.get("lodging_type") or "hotel") not in avoid_set]
            if filtered:
                enriched = filtered
            else:
                lodging_type_relaxed = True  # would exclude ALL candidates -> relax, never manufacture a false no_fit

        ranked_candidates, ranking_source = _rank_candidates(enriched, vibe, preference_hint)

        # Persona-fit deterministic boost (prefer_lodging_types): stable sort so
        # preferred-type candidates float to the front while preserving each
        # partition's existing (ranked) relative order.
        if prefer_lodging_types:
            prefer_set = {t.strip().lower() for t in prefer_lodging_types if isinstance(t, str)}
            ranked_candidates = sorted(
                ranked_candidates,
                key=lambda c: 0 if (c.get("lodging_type") or "hotel") in prefer_set else 1,
            )  # stable sort: preserves relative order within each partition

        logger.info(
            "accommodation.propose: M-Agentic-3 ranking city=%s vibe=%s "
            "source=%s %d candidates",
            city, vibe, ranking_source, len(ranked_candidates),
        )

        # -----------------------------------------------------------------------
        # Deterministic pick: highest-LLM-ranked candidate within budget ceiling.
        # Budget safety is structural — we check every candidate regardless of
        # LLM order.  The LLM order is a preference hint; the ceiling is hard.
        # -----------------------------------------------------------------------
        winner = _pick_within_ceiling(ranked_candidates, max_cents, adults)

        if winner is None:
            # All candidates exceed max_cents OR all have unknown/zero price.
            logger.info(
                "accommodation.propose: no_fit city=%s max=%d¢ (all above ceiling "
                "or all price-unknown)",
                city, max_cents,
            )
            return {
                **base,
                "fit": "no_fit",
                # #70 honesty: inventory EXISTS but nothing fits the per-leg budget cap —
                # a genuine budget shortfall (distinct from no_inventory above).
                "reason_code": "over_budget",
                "proposal": None,
                "alternates": [],
                "area_set_unknown": area_set_unknown,
                "lodging_type_relaxed": lodging_type_relaxed,
            }

        def _hotel_dict(r: dict, source: str = ranking_source, compromised: bool = False) -> dict[str, Any]:
            hid = r.get("hotel_id", "")
            lodging_type = r.get("lodging_type") or _lodging_type_from_id(hid)
            out: dict[str, Any] = {
                "hotel_id": hid,
                "title": r.get("title", hid),
                "total_cents": int(r.get("total_cents", 0)),
                "review_score": float(r.get("review_score", 0)),
                "star_rating": float(r.get("star_rating", 0)),
                "amenities": r.get("amenities") or [],
                # M-Agentic-2: deterministic area tag for the area-is-vibe-
                # appropriate correctness invariant + Critic checks.  C3: prefer
                # the already-resolved enriched area (catalog-area-first); the
                # parse fallback also passes catalog_area so it never drops back
                # to id-parse when an authoritative catalog area is present.
                "area": r.get("area") or parse_area_from_hotel_id(
                    hid, city=city, catalog_area=r.get("area"),
                ),
                # #36: lodging type (hotel/hostel/guest_house/apartment/motel),
                # derived deterministically from the id slug (Tier A) — prefer the
                # already-enriched value, fall back to a re-parse.  Additive: an
                # existing consumer ignores this unknown key.
                "lodging_type": lodging_type,
                # M-Agentic-3: provenance of the ranking decision.
                "ranking_source": source,
            }
            # HONESTY: carry the merchant's unverified-lodging warning through to the proposal so it
            # reaches the Critic / booking gate / traveler. Without this the flag is silently
            # stripped here (it is set on the search row but _hotel_dict re-projects a fixed key set)
            # → a non-hotel surfaced as a city's only listing would be booked with no honesty marker.
            notes: list[str] = []
            if r.get("unverified_lodging"):
                out["unverified_lodging"] = True
                if r.get("note"):
                    notes.append(r["note"])
            # HONESTY (lodging-type compromise): when the avoid-list had to be relaxed
            # because EVERY candidate under budget was an avoided type, the winner (and
            # any surfaced alternate) is necessarily an avoided type served as a last
            # resort. Flag it on THIS dict (never silently) — mirrors the unverified_lodging
            # pattern above so both honesty markers survive the fixed-key re-projection.
            if compromised:
                out["lodging_type_compromise"] = True
                notes.append(
                    f"Only {lodging_type} was available under budget — outside your usual preference."
                )
            if notes:
                out["note"] = " ".join(notes)
            return out

        proposal = _hotel_dict(winner, compromised=lodging_type_relaxed)

        # Alternates: next _ALTERNATES_CAP ranked candidates within budget
        # (for transparency / DP quality-pool).  These are also budget-safe
        # picks from the LLM-ranked list.  Price-unknown rows (total_cents <= 0)
        # are excluded here too (same guard as _pick_within_ceiling, finding #9).
        # Cap is configurable via ACCOMMODATION_ALTERNATES_CAP env (finding #32).
        alternates: list[dict[str, Any]] = []
        winner_id = winner.get("hotel_id", "")
        for r in ranked_candidates:
            if r.get("hotel_id", "") == winner_id:
                continue
            try:
                r_cents = int(r.get("total_cents") or 0)
            except (TypeError, ValueError):
                r_cents = 0
            if r_cents <= 0:
                continue  # skip price-unknown alternates (finding #9)
            if not _occupancy_ok(r, adults):
                continue  # skip provably over-capacity alternates (MED defense-in-depth)
            if r_cents <= max_cents:
                # lodging_type_relaxed means EVERY candidate (including alternates) is an
                # avoided type — flag alternates the same honest way as the proposal.
                alternates.append(_hotel_dict(r, compromised=lodging_type_relaxed))
            if len(alternates) >= _ALTERNATES_CAP:
                break

        logger.info(
            "accommodation.propose: OK city=%s max=%d¢ → %s total=%d¢ "
            "(score=%.1f ranking=%s)",
            city, max_cents,
            proposal["hotel_id"], proposal["total_cents"],
            proposal["review_score"], ranking_source,
        )

        return {
            **base,
            "fit": "ok",
            "proposal": proposal,
            "alternates": alternates,
            "area_set_unknown": area_set_unknown,
            "lodging_type_relaxed": lodging_type_relaxed,
        }

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
    port = int(os.environ.get("PORT", 9103))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = AccommodationAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Accommodation agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)
    logger.info("Merchant MCP URL: %s", MERCHANT_MCP_URL)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
