"""
orchestrator.py — Travel Guild M3a negotiation conductor (M2 + Critic gate + DP allocator).

Design contract: the internal design spec §4.2, §4.3, §4.5, §4.6, §0.2, §3.6, §2.1.

The TravelOrchestrator drives the multi-agent negotiation:

  R0a. [NEW — §2.1] Destination.assess per leg → target_areas (ONE-SHOT, pre-loop).
  R0b. [NEW — §2.1] Accommodation gathers FULL candidate set per leg (all under
       total_budget_cents, area-filtered, M-Agentic-3 LLM-ranked).
  R0c. [NEW — §2.1] Calls Planner with per_leg_candidates → DP allocator picks
       globally budget-optimal hotel combination.  Planner sets per-leg ceilings
       = DP-selected hotel costs (exact, not proportional).
       → DP proposals become the FIRST proposals (skip greedy initial round).
  R1.  Budget.enforce (buyer_consent=True) → real merchant verdict.
       On ACCEPT → Critic gate (M3a) → Transport gate (M3b) → consent → book.
       On VETO   → tighten ceiling on priciest leg, re-propose (robustness layer).
       MAX_ROUNDS=3 cap; honest cannot_satisfy on exhaustion.

  DP + negotiation: the DP optimises against the catalog view; if the merchant's
  authoritative price differs and the package 403s, re-plan fires (re-propose
  with lower ceiling).  The DP near-optimal first proposal means re-plan RARELY
  fires — but the loop MUST remain (legitimacy backbone, §2.1).

  Backward-compat: when USE_DP_ALLOCATOR=false env var is set, the old
  proportional-split + greedy-per-leg path is used (for existing tests
  that don't supply candidates / don't need DP).  Both paths exercise the
  same Budget veto + re-plan loop; only the initial proposal generation differs.

M3a Critic gate (§3.6, §10.8):
  The Critic re-verifies the assembled package from merchant backend facts —
  never trusts the proposal's self-reported prices.  It checks:
    (1) Coverage         — every leg has a hotel proposal.
    (2) Price integrity  — proposal total_cents == merchant live price (anti-hallucination).
    (3) Date validity    — checkin < checkout on each leg.
    (4) Date contiguity — consecutive legs connect without gaps or overlaps.
    (5) Budget           — re-verified sum <= total_budget_cents.
    (6) Provenance       — each leg carries a real backend provenance tag.
  Violations are routed back to the responsible agent (§4.3); the gate re-runs
  within the existing MAX_ROUNDS cap.  On persistent rejection the orchestrator
  emits an honest terminal (no fabrication, no forced booking).

Single consent gate (§0.2):
  The Budget call with buyer_consent=True AFTER Critic verification IS the human
  consent gate.  The merchant verifies the package total server-side.
  The human approves once; the merchant completes the multi-item checkout.

Round-by-round negotiation log (demo artifact + M5 benchmark source):
    [
        {
            "round":               int,
            "ceilings":            {"leg-0": int, "leg-1": int, ...},
            "proposals":           {"leg-0": {...} | None, ...},
            "package_total_cents": int,
            "budget_result":       {...BudgetResult...},
            "critic_result":       {...CriticResult...} | None,   # M3a
            "action":              str  # "veto_received" | "critic_rejected" |
                                        # "accept" | "cannot_satisfy"
        },
        ...
    ]

§4.6 reliability:
  - Typed negotiation log entries.
  - MAX_ROUNDS = 3 (hard cap; §4.5).
  - Honest degradation: no fabrication; cannot_satisfy returned if convergence fails.
  - Idempotency key passed through to Budget for safe retry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import uuid
from datetime import date
from typing import Any

import httpx

from agents.destination_agent import (
    assess as _destination_assess_local,
    broaden_areas,
    city_wide_areas,
    SINGLE_AREA_CITIES,
)
from core.contracts import is_do_not_recommend_country
from utils import emergency_feed as _emergency_feed
from utils.intent_parser import CITY_TO_ISO2, normalize_country_to_iso2
from utils.intracity_transport import build_transfer_hops
from providers.pricing import get_price_provider, UnavailableResult as _PriceUnavailable

logger = logging.getLogger(__name__)

# Negotiation round cap (§4.5 guard)
MAX_ROUNDS = 3

# Cap on a single violation's `detail` text inside a cannot_satisfy decline
# summary. 80 was too tight: DATE_GAP/DATE_OVERLAP detail strings ("Gap
# between leg 'X' (checkout ...) and leg 'Y' (checkin ...) — N day(s)
# missing.") run ~120+ chars, and the day-count is the LAST thing in the
# string — an 80-char cut silently amputated the exact number the honesty-
# in-decline-reasons principle exists to surface. 400 comfortably covers every
# known violation detail template (DATE_GAP/DATE_OVERLAP, OVER_BUDGET,
# OVER_CAPACITY, PRICE_MISMATCH, etc.) even with long leg_id/hotel_id values,
# while still bounding the rare case where `detail` embeds an unbounded
# upstream exception string (e.g. a merchant lookup_catalog failure). This is
# a summary-length guard, not a UI truncation — never silent: `_fmt_violation`
# below appends an explicit "…" marker when it actually cuts something, so a
# reader can tell truncation happened instead of mistaking a cut sentence for
# a complete one.
_VIOLATION_DETAIL_MAX_CHARS = 400


def _fmt_violation(v: dict) -> str:
    """
    Render one Critic violation as `CODE (leg_id): detail` for a cannot_satisfy
    decline summary, bounding `detail` at _VIOLATION_DETAIL_MAX_CHARS.

    Honesty-in-decline-reasons: if truncation actually happens, append an
    explicit "…" so the cut is visible rather than silent (a bare slice reads
    as a complete, if oddly-phrased, sentence — the reader has no way to know
    a number got cut off the end). The full, untruncated violation is also
    still available to callers via critic_result["violations"] /
    result["critic_violations"] — this is only the short human-readable form.
    """
    detail = str(v.get("detail", ""))
    if len(detail) > _VIOLATION_DETAIL_MAX_CHARS:
        detail = detail[:_VIOLATION_DETAIL_MAX_CHARS].rstrip() + "…"
    return f"{v['code']} ({v.get('leg_id', 'pkg')}): {detail}"

# R0-decline (armed-conflict gate) — curated bare city-name collisions between
# a bookable catalog city and a same-named city in a DO_NOT_RECOMMEND_COUNTRIES
# member (2026-07 adversarial audit). CITY_TO_ISO2 is catalog-only by
# construction (it maps a city name to whichever country's catalog entry
# exists), so for a name shared with a country that has NO catalog inventory
# it can only ever resolve to the bookable side — silently missing the
# DO_NOT_RECOMMEND side entirely. This map plugs that hole: a BARE reference to
# one of these city names (no explicit dest_country to disambiguate) is treated
# conservatively (declined), rather than silently booked as the permissive
# country. Extend this list if a future catalog city is found to collide with
# a DO_NOT_RECOMMEND_COUNTRIES member's city of the same name.
_AMBIGUOUS_CITY_CONFLICT_COUNTRIES: dict[str, frozenset[str]] = {
    # Tripoli: catalog only has Tripoli, Lebanon (LB, bookable); Tripoli, Libya
    # (LY) is DO_NOT_RECOMMEND and carries no catalog inventory of its own.
    "tripoli": frozenset({"LY"}),
}

# SIMULATED prepaid wallet default seed (cents). SINGLE SOURCE — the server +
# board import this so the demo wallet defaults to $5,000 everywhere. A direct
# negotiate() caller that omits wallet_balance_cents is funded at this default
# WITHOUT perturbing the var-0 request digest (the digest defaults absent → 0).
DEMO_WALLET_DEFAULT_CENTS = 500000

# Feature flag: set USE_DP_ALLOCATOR=false to revert to old proportional-split
# + greedy-per-leg flow (backward-compat / A/B testing).
_USE_DP_ALLOCATOR: bool = os.environ.get("USE_DP_ALLOCATOR", "true").lower() not in (
    "0", "false", "no", "off"
)

# ---------------------------------------------------------------------------
# In-process call helpers (work with Starlette TestClient or httpx for URLs)
# ---------------------------------------------------------------------------

def _request_digest(trip_request: dict) -> str:
    """
    Stable 16-hex digest of a trip request's booking-relevant fields (#3/#4).

    DETERMINISTIC (var-0): same request → same digest. Used for the result-
    surfaced trip_id (so a declined/invalid output is byte-identical across
    reruns — the random uuid never reaches the result) AND as the default
    idempotency_key (so an identical re-POST after a lost response reuses the
    merchant session instead of double-booking). Pure function — NO wall-clock,
    NO random, sort_keys for stable serialization.
    """
    norm = {
        "user_id": trip_request.get("user_id", ""),
        "total_budget_cents": trip_request.get("total_budget_cents", 0),
        "nationality": trip_request.get("nationality", ""),
        "today": trip_request.get("today", ""),
        "legs": trip_request.get("legs", []),
        # SIMULATED prepaid wallet participates in request identity. Defaulted to 0
        # when ABSENT so a legacy caller (no wallet key) keeps a STABLE digest
        # within and across runs — only an explicit wallet balance perturbs it.
        # (The key itself is new, so digests differ from a pre-wallet build;
        # nothing pins historical digest values, so this is inert.)
        "wallet_balance_cents": trip_request.get("wallet_balance_cents", 0),
        # #54 — overland_only participates in request identity: unlike persona (a soft
        # preference, deliberately excluded), overland_only can change whether a leg
        # pair is even bookable, so two requests that differ ONLY in this flag must NOT
        # collide on the same idempotency_key/trip_id (that would risk reusing one
        # request's checkout session for the other). Defaulted to False when ABSENT so
        # a legacy caller (no key) keeps a stable digest — only an explicit True
        # perturbs it. (The key itself is new, so digests differ from a pre-#54 build;
        # nothing pins historical digest values, so this is inert.)
        "overland_only": bool(trip_request.get("overland_only", False)),
    }
    blob = json.dumps(norm, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _package_digest(proposals: dict[str, dict | None]) -> str:
    """
    Stable 12-hex digest of the FINAL negotiated package (hotel_id + total_cents
    per leg) at the moment of commit.

    Adversarial-audit finding (var-0 drift, task #49 re-plan clamp): the merchant's
    complete_checkout idempotency short-circuit (checkout.go 'SEV-1a') is keyed on
    the caller's idempotency_key alone, which defaults to a digest of the raw
    incoming trip_request (_request_digest) -- computed ONCE, before any veto/
    re-plan. Two negotiate() calls with byte-identical trip content therefore
    share that same base key even when one of them re-planned (veto -> cheaper
    hotel) and the other's local proposal is still the pre-veto one: the second
    call's complete_checkout gets idempotent-replayed with the FIRST call's real
    (different, cheaper) booking, but the orchestrator's own day_plans/proposals
    were never updated to match -- an internally inconsistent, dishonest result
    (reports a hotel that was never actually booked) and, when the two calls are
    compared byte-for-byte (var-0 self-check), a drift.
    Folding this package digest into the idempotency_key used for THIS SPECIFIC
    commit (not the earlier create_checkout/budget.check calls, which don't
    finalize anything) preserves the double-click/duplicate-POST protection this
    mechanism exists for (a genuine retry of the SAME request deterministically
    re-derives the SAME final package, hence the SAME refined key, hence still
    correctly replays) while ensuring that whenever a replay DOES occur, the
    replayed data matches what this call independently computed anyway -- no
    reconciliation gap, no drift.
    """
    norm = {
        lid: {"hotel_id": (p or {}).get("hotel_id"), "total_cents": (p or {}).get("total_cents")}
        for lid, p in sorted(proposals.items())
    }
    blob = json.dumps(norm, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Budget-tier lodging selection (#budget-tier-fix)
#
# The DP maximises hotel QUALITY subject to total_cents <= budget, so it picks the
# highest-quality (= most-expensive) combination that still fits — a "mid budget"
# trip with a generous total then books a 5-star palace that eats ~all of the
# budget. Fix: map the qualitative budget tier AND the per-night budget implied by
# total÷nights to a price tier, and drop candidates ABOVE that tier BEFORE the DP
# runs (with a relax-if-empty guard so a leg is never starved into a false no_fit).
# Deterministic / var-0 safe: derived purely from the user's stated budget + words.
# ---------------------------------------------------------------------------

# tier -> maximum star_rating allowed (a 5-star palace never survives a "mid" tier).
_TIER_STAR_MAX = {"shoestring": 2, "budget": 3, "mid": 4, "luxury": 5}
# round-2 #hotel-tier-band-fix: tier -> MINIMUM star_rating allowed. The ceiling ALONE
# let a tier collapse to its cheapest/lowest-quality extreme — the DP maximises
# review-score-quality subject to budget, and a highly-reviewed budget share-house can
# out-score a mediocre 3/4-star hotel on quality, so "mid budget" was floor-ing to a
# 2-star share house (a real product-quality bug: the ceiling-only filter never
# EXCLUDED the 2-star option, it just also allowed it in). Pairing a floor with the
# existing ceiling maps the tier to the correct BAND (budget->2-3*, mid->3-4*) and the
# DP now optimises WITHIN that band instead of at either extreme. luxury/shoestring
# intentionally have no floor: shoestring IS the bottom of the market, and luxury's
# existing ceiling-only (no-op-when-uncapped) behaviour is unchanged (round-1 contract).
_TIER_STAR_MIN = {"budget": 2, "mid": 3}
# #52 item 6b — DISCLOSURE-ONLY expectation floor (distinct from _TIER_STAR_MIN,
# which intentionally omits luxury/shoestring so a tight budget is never starved
# into a false no_fit). Used only to decide whether to tell the traveler their
# stated style and their stated budget disagree — never to filter/reject a
# candidate. No entry for "shoestring" (its whole premise is cheap, so there is
# no "too cheap for shoestring" mismatch to disclose).
_TIER_MISMATCH_DISCLOSURE_FLOOR = {"budget": 2, "mid": 3, "luxury": 4}
# tier -> per-night price ceiling in cents (backstop when star_rating is absent/0).
_TIER_PER_NIGHT_CAP_CENTS = {
    "shoestring": 9000, "budget": 18000, "mid": 38000, "luxury": None
}
# Fraction of the total budget assumed to go to LODGING when tightening a tier's
# per-night cap by the implied per-night. Conservative: the rest covers food /
# activities / local transport, so the implied per-night stays realistic.
_LODGING_BUDGET_SHARE = 0.45


def _resolve_budget_tier(trip_request: dict, total_budget_cents: int) -> str | None:
    """Resolve the lodging price tier for this trip from the user's stated words.

    Only an EXPLICIT qualitative tier ("mid budget" -> 'mid') drives the tier filter,
    so a trip that states no budget style behaves exactly as before (zero regression).
    The numeric per-night implied by total÷nights is NOT used to invent a tier from a
    bare budget (that would tighten every existing low-budget trip, and a generous bare
    budget maps to 'luxury' = no constraint anyway); it is instead used to TIGHTEN the
    chosen tier's price cap in _resolve_per_night_cap. Returns None when no qualitative
    tier is present (no filtering)."""
    qualitative = trip_request.get("budget_tier")
    if qualitative in _TIER_STAR_MAX:
        return qualitative
    return None


def _resolve_per_night_cap(
    tier: str, total_budget_cents: int, total_nights: int, *, skip_budget_tighten: bool = False
) -> int | None:
    """Effective per-night price cap for the tier: the tier's own ceiling, TIGHTENED by
    the per-night lodging budget implied by total÷nights (the numeric signal the eval
    asked for). Never LOOSENS the tier cap; None only for an unbounded luxury tier with
    no usable budget signal.

    ``skip_budget_tighten`` (round-2 #budget-tier-plans-fix): True when the total budget
    itself was an ESTIMATE derived from this same qualitative tier (no dollar figure was
    ever stated — see intent_parser._implied_budget_cents_from_tier), not an independent
    numeric signal from the user. Tightening the tier's cap using a number we ourselves
    invented FROM the tier is circular and can self-defeatingly starve the very tier it
    is supposed to serve (e.g. an implied 'luxury' total could imply a per-night cap
    below a genuine 5-star rate). In that case we trust ONLY the tier's own static
    band — never the derived number — leaving the star-rating band as the authority."""
    tier_cap = _TIER_PER_NIGHT_CAP_CENTS.get(tier)
    if skip_budget_tighten:
        return tier_cap
    if total_nights > 0 and total_budget_cents > 0:
        implied = int(total_budget_cents * _LODGING_BUDGET_SHARE / total_nights)
        if implied > 0:
            tier_cap = implied if tier_cap is None else min(tier_cap, implied)
    return tier_cap


def _filter_candidates_by_tier(
    candidates: list[dict], tier: str | None, nights: int,
    per_night_cap: int | None = None,
) -> list[dict]:
    """Keep candidates WITHIN the resolved price tier's BAND (by star_rating, backed by
    a per-night price cap) — round-2 #hotel-tier-band-fix: a ceiling alone let the DP's
    quality-maximiser float a tier down to its cheapest extreme (a highly-reviewed
    2-star share house beating a merely-decent 3/4-star hotel on review score), so a
    floor is now paired with the ceiling to map the tier to the correct BAND
    (budget->2-3*, mid->3-4*) and the DP selects WITHIN it, never at either extreme.
    RELAXES in stages if a bound would empty the leg — a tier preference must never
    manufacture a false no_fit: first drop the floor (ceiling-only, the round-1
    behaviour), then relax to the full candidate set. Deterministic / pure."""
    if not tier or not candidates:
        return candidates
    star_max = _TIER_STAR_MAX.get(tier)
    star_min = _TIER_STAR_MIN.get(tier)
    if per_night_cap is None:
        per_night_cap = _TIER_PER_NIGHT_CAP_CENTS.get(tier)
    if tier == "luxury" and per_night_cap is None:
        return candidates
    n = max(nights, 1)

    def _apply(min_bound: float | None) -> list[dict]:
        out: list[dict] = []
        for c in candidates:
            try:
                star = float(c.get("star_rating") or 0)
            except (TypeError, ValueError):
                star = 0.0
            # star_rating is the primary signal; when known (>0) it must be within tier.
            if star_max is not None and star > 0 and star > star_max:
                continue
            # round-2 #hotel-tier-band-fix: floor — a KNOWN star rating below the
            # tier's band is too basic for what the user asked for (never let "mid
            # budget" float to a 2-star share house just because it is the cheapest
            # survivor of a ceiling-only filter).
            if min_bound is not None and star > 0 and star < min_bound:
                continue
            # per-night price backstop: catches over-tier venues whose star is unknown.
            if per_night_cap is not None:
                try:
                    cents = int(c.get("total_cents") or 0)
                except (TypeError, ValueError):
                    cents = 0
                if cents > 0 and (cents // n) > per_night_cap:
                    continue
            out.append(c)
        return out

    kept = _apply(star_min)
    if not kept and star_min is not None:
        logger.info(
            "orchestrator: budget-tier band (%s) would empty leg — relaxing the floor "
            "(ceiling-only) before falling back to the full candidate set", tier,
        )
        kept = _apply(None)
    if not kept:
        logger.info(
            "orchestrator: budget-tier filter (%s) would empty leg — relaxing to full "
            "candidate set (never manufacture a false no_fit)", tier,
        )
        return candidates
    if len(kept) != len(candidates):
        logger.info(
            "orchestrator: budget-tier filter (%s): %d -> %d candidate(s)",
            tier, len(candidates), len(kept),
        )
    return kept


def _rpc_post_client(client: Any, method: str, params: dict) -> dict:
    """POST a JSON-RPC 2.0 message to an in-process Starlette TestClient."""
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    resp = client.post("/", json=body)
    return resp.json()


# #42 — AUTHORITATIVE task-state success contract at the transport seam.
# A skill's data artifact may ONLY be consumed when the task reached a state that
# legitimately CARRIES a consumable result in this society:
#   - "completed"      → normal success.
#   - "input-required" → the budget consent/mandate HALT, which intentionally
#                        appends a data artifact carrying the needs_consent /
#                        needs_mandate decision the orchestrator handles downstream.
# EVERY OTHER terminal/halt state (failed/rejected/canceled/auth-required, or an
# unexpected non-terminal like submitted/working) is a HARD STOP: raise rather
# than silently grab the first data part of a halted/failed task. This keeps the
# honesty signal (task state) and the data flag from disagreeing for any consumer.
_CONSUMABLE_TASK_STATES: frozenset[str] = frozenset({"completed", "input-required"})


def _extract_task_data(task: dict, skill_id: str) -> dict:
    """
    Enforce the #42 task-state success contract, then return the first data-part
    dict from the task's artifacts. Raises RuntimeError on any non-consumable
    state or when no data artifact is present.
    """
    state = (task.get("status") or {}).get("state", "")
    if state not in _CONSUMABLE_TASK_STATES:
        err = (task.get("metadata") or {}).get("error", "unknown")
        raise RuntimeError(
            f"Agent task not consumable ({skill_id}): state={state!r} "
            f"(expected one of {sorted(_CONSUMABLE_TASK_STATES)}); error={err}"
        )
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "data":
                return part["data"]
    raise RuntimeError(f"No data artifact in task for {skill_id}: {task}")


def _send_to_client(client: Any, payload: dict, skill_id: str) -> dict:
    """
    Send a typed A2A message to an in-process TestClient agent.

    Extracts and returns the first data-part dict from the artifact.
    Raises RuntimeError if no data artifact found or the task is not in a
    consumable state (#42 — only completed / input-required are consumable).
    """
    msg: dict[str, Any] = {
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "data", "data": payload}],
        "metadata": {"skillId": skill_id},
    }
    rpc = _rpc_post_client(client, "message/send", {"message": msg})
    if "error" in rpc:
        raise RuntimeError(f"A2A RPC error for {skill_id}: {rpc['error']}")
    task = rpc.get("result", {})
    return _extract_task_data(task, skill_id)


def _send_to_url(url: str, payload: dict, skill_id: str) -> dict:
    """
    Send a typed A2A message to an agent via HTTP URL.

    Extracts and returns the first data-part dict from the artifact. Raises
    RuntimeError if the task is not in a consumable state (#42 — only completed /
    input-required are consumable).
    """
    msg: dict[str, Any] = {
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "data", "data": payload}],
        "metadata": {"skillId": skill_id},
    }
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {"message": msg},
    }
    resp = httpx.post(url.rstrip("/") + "/", json=body, timeout=30.0)
    resp.raise_for_status()
    rpc = resp.json()
    if "error" in rpc:
        raise RuntimeError(f"A2A RPC error for {skill_id}: {rpc['error']}")
    task = rpc.get("result", {})
    return _extract_task_data(task, skill_id)


def _arrival_minutes_map(transport_result: dict | None) -> tuple[dict[str, int], dict[str, str]]:
    """Map arrival leg_id → (inter-city transfer_minutes, mode) from a transport result, for the
    travel-day reservation. Includes ONLY feasible edges with a positive integer transfer_minutes;
    sentinels (-1 unknown, 0 same-area), float/None minutes, and infeasible/cancelled edges are
    excluded (conservative — never trim a leg whose arrival we cannot honestly quantify).
    Pure + deterministic → unit-testable without the merchant backend."""
    minutes: dict[str, int] = {}
    mode: dict[str, str] = {}
    for edge in (transport_result or {}).get("edges") or []:
        to_leg = edge.get("to_leg")
        raw = edge.get("transfer_minutes")
        m = int(raw) if isinstance(raw, int) else -1
        if to_leg and m > 0 and edge.get("feasible", True):
            minutes[to_leg] = m
            mode[to_leg] = edge.get("mode") or ""
    return minutes, mode


def _arrival_transport_minutes(leg: dict, lid: str, city: str,
                               minutes_map: dict[str, int], mode_map: dict[str, str]) -> int:
    """Minutes the arrival day loses to inter-city transit for leg `lid`: the inter-city transfer
    PLUS the inbound airport→hotel hop — but the airport hop is added ONLY for a FLIGHT arrival
    (rail/road/ferry arrive in-city, so no airport leg). Returns 0 when there is no inbound edge
    (the origin leg) → the day-planner trim is then a no-op. Deterministic → unit-testable."""
    intercity = minutes_map.get(lid, 0)
    if intercity <= 0:
        return 0
    transfer_in = 0
    if mode_map.get(lid) == "flight":
        for hop in build_transfer_hops(leg, city):
            if hop.get("direction") == "inbound":
                transfer_in = int(hop.get("minutes") or 0)
                break
    return intercity + transfer_in


# ---------------------------------------------------------------------------
# TravelOrchestrator
# ---------------------------------------------------------------------------

class TravelOrchestrator:
    """
    Multi-agent negotiation conductor for the Travel Guild (M3a + DP §2.1).

    Accepts either in-process Starlette TestClients (for unit tests) or
    HTTP URLs (for live / integration runs). Mix-and-match is supported.

    The Critic gate (M3a) is wired between Budget envelope check and the
    single human consent step.  critic_client / critic_url are optional —
    when neither is provided the Critic gate is bypassed (backward-compatible
    with M2 unit tests that don't supply a Critic).

    The Transport gate (M3b) is wired between Budget accept and the Critic gate.
    transport_client / transport_url are optional — when neither is provided the
    Transport gate is bypassed (backward-compatible with M3a tests).

    DP allocation (§2.1):
      When USE_DP_ALLOCATOR=true (default), the orchestrator:
        1. Gathers FULL per-leg candidate sets before calling the Planner.
        2. Passes candidates to Planner → DP finds globally optimal combo.
        3. Uses DP pre-selections as initial proposals (skips greedy step).
      The Budget veto + re-plan loop remains as the robustness/legitimacy layer.

    Args:
        planner_url:          HTTP URL for the Planner agent (None = use client).
        accommodation_url:    HTTP URL for the Accommodation agent.
        budget_url:           HTTP URL for the Budget agent.
        critic_url:           HTTP URL for the Critic agent (M3a).
        transport_url:        HTTP URL for the Transport agent (M3b).
        destination_url:      HTTP URL for the Destination agent.
        planner_client:       Starlette TestClient for Planner (in-process).
        accommodation_client: Starlette TestClient for Accommodation.
        budget_client:        Starlette TestClient for Budget.
        critic_client:        Starlette TestClient for Critic (M3a, optional).
        transport_client:     Starlette TestClient for Transport (M3b, optional).
        destination_client:   Starlette TestClient for Destination.
    """

    def __init__(
        self,
        planner_url: str | None = None,
        accommodation_url: str | None = None,
        budget_url: str | None = None,
        critic_url: str | None = None,
        transport_url: str | None = None,
        destination_url: str | None = None,
        insurance_url: str | None = None,
        compliance_url: str | None = None,
        risk_url: str | None = None,
        health_url: str | None = None,
        fraud_url: str | None = None,
        day_planner_url: str | None = None,
        planner_client: Any = None,
        accommodation_client: Any = None,
        budget_client: Any = None,
        critic_client: Any = None,
        transport_client: Any = None,
        destination_client: Any = None,
        insurance_client: Any = None,
        compliance_client: Any = None,
        risk_client: Any = None,
        health_client: Any = None,
        fraud_client: Any = None,
        day_planner_client: Any = None,
        food_search: Any = None,
        dining_url: str | None = None,
        dining_client: Any = None,
        emergency_url: str | None = None,
        emergency_client: Any = None,
        narrator_client: Any = None,
        memory_client: Any = None,
        tracer: Callable | None = None,
        trip_lookup: Callable[[str], dict | None] | None = None,
        atomic_commit_marker: Callable[[str, str], None] | None = None,
        atomic_committed_lookup: Callable[[str], bool] | None = None,
        digest_booked_lookup: Callable[[str], bool] | None = None,
    ) -> None:
        self._planner_url = planner_url
        self._accommodation_url = accommodation_url
        self._budget_url = budget_url
        self._critic_url = critic_url
        self._transport_url = transport_url
        self._destination_url = destination_url
        # Insurance (L2) hook — APPEND-ONLY per the §AGENT-EXTENSION PATTERN
        # (insurance_agent.py). Optional + defaults to None, so every existing
        # call site + test is unchanged (backward-compatible by construction).
        self._insurance_url = insurance_url
        # #45 — OPT-IN user-policy for the coverage-GAP analyzer. Defaults to None,
        # so when a trip_request omits `user_policy` NOTHING runs and the result is
        # byte-identical to today. Set per-run in negotiate() from trip_request.
        self._user_policy: dict | None = None
        # Compliance (L2 / DC1) eligibility-gate hook — APPEND-ONLY per the
        # §AGENT-EXTENSION PATTERN (insurance_agent.py). Optional + defaults to
        # None, so every existing call site + test is unchanged (backward-
        # compatible by construction).
        self._compliance_url = compliance_url
        # Risk (L1 proactive, OFF the money path) signal-consolidator hook —
        # APPEND-ONLY per the §AGENT-EXTENSION PATTERN (insurance_agent.py).
        # Optional + defaults to None, so every existing call site + test is
        # unchanged (backward-compatible by construction). Risk emits PLANNING-
        # INPUT signals the Planner consumes ADDITIVELY: only scenarios that
        # CARRY a cyclone/delay condition change; S1–S5 stay byte-identical.
        self._risk_url = risk_url
        # Health (L2 / DC-health) vaccination + entry-cert-gate hook — APPEND-ONLY
        # per the §AGENT-EXTENSION PATTERN (insurance_agent.py). Optional + defaults
        # to None, so every existing call site + test is unchanged (backward-
        # compatible by construction). Health emits the UPFRONT vaccine cost as a
        # Budget line item, the mandatory-entry-cert gate, and the JAB half of the
        # §7 YF-cert handoff Compliance reads.
        self._health_url = health_url
        # Fraud (L2 / DC-fraud) counterparty-SOLVENCY-gate hook — APPEND-ONLY per
        # the §AGENT-EXTENSION PATTERN (insurance_agent.py). Optional + defaults to
        # None, so every existing call site + test is unchanged (backward-compatible
        # by construction). Fraud CONSTRAINS which counterparties Budget may commit:
        # a blocked/unknown counterparty is never committable without explicit fresh
        # consent (pre-empts N1 supplier insolvency). The Critic re-checks the SAME
        # seeded band at commit time so a missed verdict can't slip an insolvent
        # supplier through.
        self._fraud_url = fraud_url
        # Day-planner (build #30, OFF the money path) per-leg activity & meal
        # planner hook — APPEND-ONLY per the §AGENT-EXTENSION PATTERN
        # (insurance_agent.py). Optional + defaults to None, so every existing
        # call site + test is unchanged (backward-compatible by construction).
        # The day-planner is PRESENCE-ONLY (OSM harvest cache, fail-conservative):
        # it surfaces a day-by-day attraction/meal plan ADDITIVELY into the
        # success result; it never blocks a commit and a missing/failed verdict is
        # simply omitted (the itinerary is still valid without an activity plan).
        self._day_planner_url = day_planner_url
        self._planner_client = planner_client
        self._accommodation_client = accommodation_client
        self._budget_client = budget_client
        self._critic_client = critic_client
        self._transport_client = transport_client
        self._destination_client = destination_client
        self._insurance_client = insurance_client
        self._compliance_client = compliance_client
        self._risk_client = risk_client
        self._health_client = health_client
        self._fraud_client = fraud_client
        self._day_planner_client = day_planner_client
        # #44 — OPT-IN late-night supper on the SAME prepaid UCP rails as hotels.
        # `food_search` is a callable (query: dict) -> merchant structuredContent
        # dict | None (the search_catalog kind=FOOD_DELIVERY response). Defaults to
        # None, so when a trip_request omits `supper` NOTHING runs and the result is
        # byte-identical to today (APPEND-ONLY, mirrors the #45 / #30 hooks). The
        # food CHECKOUT reuses the EXISTING Budget client (the rails generalize).
        self._food_search = food_search
        # Set per-run in negotiate() from trip_request['supper'] (None when absent).
        self._supper_request: dict | None = None
        # #32 — OPT-IN LIVE restaurant reviews/ratings enrichment OVER the
        # deterministic meal plan. Provider seam (AMap-first / Google-fallback)
        # defaults to None (keys PENDING) → the enrichment hook reports an honest
        # 'live reviews unavailable' note and NEVER fabricates a rating/review.
        # APPEND-ONLY (mirrors the #44 food_search seam): a trip_request without
        # a `dining` dict leaves self._dining_request None → the hook is a no-op →
        # byte-identical output (day_plans stays var-0; reviews land in a SEPARATE
        # top-level key result['dining_reviews'], NEVER inside day_plans).
        self._dining_url = dining_url
        self._dining_client = dining_client
        # Set per-run in negotiate() from trip_request['dining'] (None when absent).
        self._dining_request: dict | None = None
        # #51 — OPT-IN LIVE active-emergency overlay. The seasonal risk model
        # (risk_agent) is a frozen climatology → ADVISORY ("take precautions").
        # An ACTIVE emergency (a declared wildfire/flood/cyclone evacuation) is a
        # live, time-varying fact that CANNOT live on the var-0 deterministic path.
        # So it is FIREWALLED behind a provider seam (mirrors the #32 dining /
        # reference-price layers): defaults None → the hook is a NO-OP → byte-
        # identical output. When configured + opted in AND the feed reports an active
        # emergency, it escalates to a "DO NOT TRAVEL" notice in a SEPARATE top-level
        # key result['active_emergencies'] — it NEVER mutates the deterministic risk
        # rollup, day_plans, or any avoid window (var-0 sacred). HONEST: feed
        # unavailable/failed → an honest 'live emergency status unavailable' note,
        # never a fabricated all-clear (silence is never safety).
        self._emergency_url = emergency_url
        self._emergency_client = emergency_client
        # Set per-run in negotiate() from trip_request['live_emergency'] (None absent).
        self._emergency_request: dict | None = None
        # #3 — OPT-IN cosmetic Google-style itinerary NARRATIVE (LLM). Defaults None → the hook is a
        # hard no-op → byte-identical (var-0 firewalled; mirrors the dining/emergency seams). The
        # LLM-on gate lives in the server wiring (passes this only when DASHSCOPE_API_KEY is set).
        self._narrator_client = narrator_client
        self._narrate_request: dict | None = None
        # #159 — genuine MCP client for the travel_memory server's memory-loop tools
        # (log_search WRITE + resolve_geographic_scope affinity READ; see
        # utils/memory_client.py). Defaults to None -> the seam is a hard no-op ->
        # byte-identical to today (mirrors the narrator/dining/emergency seams). Both
        # consumption points are OFF the var-0 booking/itinerary/price path: WRITE
        # fires only AFTER the result is frozen (a post-freeze side effect, like the
        # #64 telemetry emit); READ lands in a SEPARATE display-only
        # result['personalization'] key, never in day_plans or any priced field.
        self._memory_client = memory_client
        self._personalization: dict | None = None  # set per-run in negotiate()
        # #161 — canonical Go-merchant checkout owner (session_token/owner_token-
        # verified upstream by server.py _authorize_trip_action; see
        # utils/ucp_signing.merchant_checkout_owner). Set per-entrypoint (negotiate()
        # / commit_plan()) BEFORE any budget.check/commit/enforce dispatch, then
        # substituted for the outgoing merchant payload's "user_id" field by the
        # _call_budget_check/_call_budget_commit/_call_budget funnel methods below —
        # this is what checkout.go's END-USER OWNERSHIP check (task #161) verifies
        # against. Falls back to the internal `user_id` when unset/empty so direct/
        # test callers that never set it are byte-identical to today.
        self._merchant_user_id: str = ""
        # M1 follow-up (informational) — self._merchant_user_id above
        # mirrors _authorize_trip_action's TIER decision only; it is NOT session-
        # verified at plan-creation time (real verification happens later, at
        # /confirm). That's fine for checkout (re-verified there before anything
        # irreversible happens) but the travel_memory PERSONALIZATION channel
        # (_maybe_log_search / _maybe_read_affinity below) fires at negotiate()
        # time with no later gate, so it needs its OWN, properly-gated identity.
        # None (the default) means "server.py did not compute one" — direct/test
        # callers that invoke negotiate() without going through the real server
        # boundary fall back to self._merchant_user_id (byte-identical to before
        # this follow-up). A real string (INCLUDING "") means the server boundary
        # (see server.py _memory_verified_user_id) already made the call: "" is a
        # deliberate, safe REJECTION (unverified tier-1 claim) that must NEVER
        # fall through to any untrusted value.
        self._memory_user_id: str | None = None
        # HONESTY side-channel: {hotel_id -> note} for lodging the merchant flagged as unverified
        # (curated non-hotel / suspect-named OSM). Populated in _call_accommodation, consumed at final
        # leg assembly so the warning survives the downstream fixed-key re-projections. Reset per run.
        self._unverified_lodging: dict[str, str] = {}
        # Tracer side-channel (Var-0 sacred: no-op lambda by default).
        # A CollectingTracer or any callable may be supplied; it is never
        # called before or inside an agent call — only after it returns.
        # All emit sites are try/except guarded so a tracer bug can never
        # crash a negotiation.
        self._tracer: Callable = tracer if tracer is not None else (lambda *a, **kw: None)
        # M8 — OPTIONAL injected trips-store lookup (mirrors the `tracer` callback
        # pattern above: the orchestrator stays store-agnostic, never imports
        # orchestration.store itself; server.py wires this at construction). Given
        # an idempotency_key, returns the stored trip row dict (or None). Used
        # ONLY by _fund_wallet's fund-if-ALREADY-LIFECYCLED guard below — nowhere
        # else touches persistence. Defaults to None → _fund_wallet's guard is a
        # no-op and behavior is byte-identical to before this fix (var-0/back-
        # compat for direct callers/tests that construct a bare orchestrator).
        self._trip_lookup: Callable[[str], dict | None] | None = trip_lookup
        # M7 — OPTIONAL injected hooks (mirrors `trip_lookup` immediately above)
        # covering the ONE case that guard cannot see: an ATOMIC (commit=True,
        # i.e. no `plan` in the request) negotiate() SUCCESS is never persisted
        # to the trips store at all (only plan_ready/plan-mode results are, via
        # server.py's _persist_and_sanitize_plan) — so `trip_lookup` has nothing
        # to find for a retried identical atomic POST /negotiate, and
        # _fund_wallet's guard cannot engage: it re-funds AND re-debits the
        # wallet for what is, merchant-side, a SECOND live booking.
        #   atomic_commit_marker(idempotency_key, booking_ref) — called once,
        #     right after negotiate() finalizes an atomic-mode SUCCESS, to
        #     record a lightweight (non-trips-store) committed marker.
        #   atomic_committed_lookup(idempotency_key) -> bool — consulted by
        #     _fund_wallet ALONGSIDE `trip_lookup`'s existing check.
        # Both default to None → complete no-op, byte-identical to before this
        # fix for any caller that never wires them (var-0/back-compat).
        self._atomic_commit_marker: Callable[[str, str], None] | None = atomic_commit_marker
        self._atomic_committed_lookup: Callable[[str], bool] | None = atomic_committed_lookup
        # L10 — OPTIONAL injected hook (mirrors `trip_lookup`/`atomic_committed_
        # lookup` above): `trip_lookup` only ever checks the row stored under
        # THIS EXACT key. A derived-key row (idk-vN, minted by the M1/M4/M5/M6
        # consent-plan-mismatch fork guard in server.py) is a DIFFERENT store
        # key that nonetheless shares this run's digest-based wallet_session_id
        # (the wallet was bound to the digest at negotiate()-time, before any
        # fork ever happens — see get_booked_row_by_digest's docstring). If the
        # digest row is later cancelled/swept while the derived row is booked,
        # trip_lookup(digest_key) alone would miss that a SIBLING row sharing
        # this same wallet is booked, and _fund_wallet would reset/destroy that
        # booked trip's ledger. `digest_booked_lookup(digest) -> bool` closes
        # this by checking for ANY booked row under the given content digest,
        # not just the one at this exact key. Defaults to None → complete
        # no-op, byte-identical to before this fix (var-0/back-compat).
        self._digest_booked_lookup: Callable[[str], bool] | None = digest_booked_lookup
        self._trip_id: str = ""  # set at start of negotiate()
        # D7 — single frozen `today` for the whole run (set at negotiate() entry).
        self._today: str | None = None
        # SIMULATED prepaid wallet — frozen at negotiate() entry. The session id is
        # the deterministic per-run idempotency_key (var-0 keystone: identical input
        # → identical key → identical reset). DEMO default keeps direct callers /
        # legacy tests funded without changing the var-0 request identity.
        self._wallet_balance_cents: int = DEMO_WALLET_DEFAULT_CENTS
        self._wallet_session_id: str = ""
        # Circle Agentic Economy Prize: REAL (not simulated) USDC settlement
        # opt-in — "" (default) means no live settlement attempted (back-compat).
        # Set from trip_request in negotiate(), same lifecycle as
        # _wallet_session_id above. See _run_negotiation_rounds's check_payload.
        self._settlement_rail: str = ""
        # L2 — see the day-planner block in _run_negotiation_rounds + _success_result.
        self._day_plan_error: str | None = None

    # ------------------------------------------------------------------
    # Agent call dispatch
    # ------------------------------------------------------------------

    def _call_fraud(self, payload: dict) -> dict | None:
        """
        Call the Fraud agent (fraud.vet) — the deterministic counterparty-SOLVENCY
        gate (pre-empts fault class N1: supplier insolvency → VOID ticket). The
        returned roll-up tells the orchestrator which counterparties are committable:
          - 'committable_ids' / 'blocked_ids' — Budget commits only the cleared set;
          - a blocked/unknown counterparty is NEVER committable without explicit
            fresh consent (the gate; UNKNOWN → conservative block).
        ADVISORY here — the Critic INDEPENDENTLY re-checks the same seeded band at
        commit time so a missed verdict can't slip an insolvent supplier through.
        Returns None if no Fraud agent is configured (bypassed — backward-compatible).
        APPEND-ONLY hook per the §AGENT-EXTENSION PATTERN.

        payload: {counterparties[{counterparty_id|catalog_id, kind?, leg_id?}],
                  consent_tokens?}.
        """
        if self._fraud_client is not None:
            return _send_to_client(self._fraud_client, payload, "fraud.vet")
        if self._fraud_url:
            return _send_to_url(self._fraud_url, payload, "fraud.vet")
        return None  # No Fraud configured — bypass

    def _call_planner(self, payload: dict) -> dict:
        if self._planner_client is not None:
            return _send_to_client(self._planner_client, payload, "plan.decompose")
        if self._planner_url:
            return _send_to_url(self._planner_url, payload, "plan.decompose")
        raise RuntimeError("No planner client or URL configured")

    def _call_accommodation(self, payload: dict) -> dict:
        if self._accommodation_client is not None:
            result = _send_to_client(self._accommodation_client, payload, "accommodation.propose")
        elif self._accommodation_url:
            result = _send_to_url(self._accommodation_url, payload, "accommodation.propose")
        else:
            raise RuntimeError("No accommodation client or URL configured")
        # HONESTY side-channel: the unverified_lodging warning (a curated non-hotel surfaced as a
        # city's only listing, or a suspect-named OSM row) is set by the merchant but stripped by the
        # many fixed-key proposal/candidate/DP re-projections downstream. Capture it here — the single
        # chokepoint every accommodation response flows through — keyed by hotel_id, then re-stamp it at
        # final leg assembly (by id, which IS always preserved). Deterministic; never raises.
        try:
            rows = [result.get("proposal")] + list(result.get("alternates") or [])
            for h in rows:
                if isinstance(h, dict) and h.get("unverified_lodging") and h.get("hotel_id"):
                    self._unverified_lodging[str(h["hotel_id"])] = str(h.get("note") or "")
        except Exception:
            pass
        return result

    def _stamp_merchant_user_id(self, payload: dict) -> dict:
        """#161 — override the outgoing merchant payload's "user_id" with the
        canonical, session-verified merchant identity (self._merchant_user_id),
        set per-run by negotiate()/commit_plan() from utils.ucp_signing.
        merchant_checkout_owner(). This is the value checkout.go's END-USER
        OWNERSHIP check (checkoutSession.UserID) verifies against — never the
        raw internal `user_id` a payload happened to carry.

        No-op (payload returned unchanged) when self._merchant_user_id is unset,
        so direct/test callers that invoke _call_budget*/_do_commit without going
        through negotiate()/commit_plan() first stay byte-identical to today.
        """
        if self._merchant_user_id and isinstance(payload, dict) and "user_id" in payload:
            payload = dict(payload)
            payload["user_id"] = self._merchant_user_id
        return payload

    def _call_budget(self, payload: dict) -> dict:
        """Legacy budget.enforce dispatch (backward-compat for tests without Critic/Transport)."""
        payload = self._stamp_merchant_user_id(payload)
        if self._budget_client is not None:
            return _send_to_client(self._budget_client, payload, "budget.enforce")
        if self._budget_url:
            return _send_to_url(self._budget_url, payload, "budget.enforce")
        raise RuntimeError("No budget client or URL configured")

    def _call_budget_check(self, payload: dict) -> dict:
        """SEV-1a CHECK phase: budget.check (create_checkout, no capture)."""
        payload = self._stamp_merchant_user_id(payload)
        if self._budget_client is not None:
            return _send_to_client(self._budget_client, payload, "budget.check")
        if self._budget_url:
            return _send_to_url(self._budget_url, payload, "budget.check")
        raise RuntimeError("No budget client or URL configured")

    def _call_budget_commit(self, payload: dict) -> dict:
        """SEV-1a COMMIT phase: budget.commit (complete_checkout — irreversible booking)."""
        payload = self._stamp_merchant_user_id(payload)
        if self._budget_client is not None:
            return _send_to_client(self._budget_client, payload, "budget.commit")
        if self._budget_url:
            return _send_to_url(self._budget_url, payload, "budget.commit")
        raise RuntimeError("No budget client or URL configured")

    def _call_budget_cancel(self, payload: dict) -> dict:
        """H1 fix — VOID phase: budget.cancel (cancel_checkout — release a
        committed booking, §12.1 cascade). Mirrors _call_budget_commit's
        dispatch exactly, so a cancel genuinely reaches the merchant (local
        transport OR the real Go merchant over HTTP) instead of never being
        called at all."""
        payload = self._stamp_merchant_user_id(payload)
        if self._budget_client is not None:
            return _send_to_client(self._budget_client, payload, "budget.cancel")
        if self._budget_url:
            return _send_to_url(self._budget_url, payload, "budget.cancel")
        raise RuntimeError("No budget client or URL configured")

    def _call_budget_fund(self, payload: dict) -> dict | None:
        """
        SIMULATED prepaid wallet: budget.fund (wallet_fund — create-OR-RESET).

        Returns None when no Budget agent is configured (NO-OP unconfigured —
        mirrors _call_critic returning None so the wallet seam is fully bypassable
        and var-0 / backward-compatible).
        """
        if self._budget_client is not None:
            return _send_to_client(self._budget_client, payload, "budget.fund")
        if self._budget_url:
            return _send_to_url(self._budget_url, payload, "budget.fund")
        return None  # No Budget configured — wallet seam bypassed

    def _fund_wallet(self) -> dict | None:
        """
        SIMULATED prepaid wallet: create-OR-RESET the per-run wallet (var-0 keystone).

        Dispatches budget.fund keyed by the deterministic per-run session id so the
        wallet is reset-per-run (identical input → identical reset → identical
        ledger on replay). NO-OP (returns None) when no Budget agent is configured.
        NEVER raises — a wallet-funding failure must never crash a negotiation (the
        wallet is a best-effort settlement-layer sim seam, firewalled from var-0).

        M8 — because wallet_fund is genuinely create-OR-RESET (a deliberate var-0
        keystone: identical input → identical replay), unconditionally calling it
        on EVERY negotiate() is only safe for a FRESH idempotency_key. The
        idempotency_key is a deterministic digest of the request (orchestrator.
        _request_digest), so an identical re-POST (retry, second tab, double-
        submitted form) reuses the SAME wallet_session_id — if that key ALREADY
        has a booked or held (plan_ready) row in the trips store, resetting its
        wallet wipes the ledger/seen-map a later /cancel needs to compute a
        refund, and (in atomic/commit=true mode) re-funds+re-debits a session
        that already has a live booking. Guarded below via the OPTIONAL
        `self._trip_lookup` hook (server.py wires it to the trips store; None
        by default → byte-identical to before this fix for any caller that
        never sets it, e.g. direct/test callers with no store).
        """
        if not self._wallet_session_id:
            return None
        if self._trip_lookup is not None:
            try:
                existing_row = self._trip_lookup(self._wallet_session_id)
            except Exception as exc:  # noqa: BLE001 — the lookup is a side hook, never fatal
                logger.warning(
                    "orchestrator._fund_wallet: trip_lookup failed (%s) — "
                    "proceeding with normal fund/reset", exc,
                )
                existing_row = None
            if isinstance(existing_row, dict) and existing_row.get("status") in ("booked", "plan_ready"):
                logger.warning(
                    "orchestrator._fund_wallet: idempotency_key=%s already has a "
                    "%s trip row — SKIPPING wallet_fund (would reset/destroy the "
                    "existing wallet ledger/refund path for an identical re-POST).",
                    self._wallet_session_id, existing_row.get("status"),
                )
                return None
        # M7 — `trip_lookup` above only covers keys that have a trips-store ROW
        # (plan_ready/booked). An ATOMIC (commit=True) negotiate() SUCCESS is
        # never persisted to the trips store at all, so it has nothing to find
        # there — this second, independent hook is consulted for exactly that
        # case (see this method's M7 docstring paragraph and the
        # `_atomic_commit_marker`/`_atomic_committed_lookup` constructor args).
        if self._atomic_committed_lookup is not None:
            try:
                already_committed = self._atomic_committed_lookup(self._wallet_session_id)
            except Exception as exc:  # noqa: BLE001 — the lookup is a side hook, never fatal
                logger.warning(
                    "orchestrator._fund_wallet: atomic_committed_lookup failed (%s) — "
                    "proceeding with normal fund/reset", exc,
                )
                already_committed = False
            if already_committed:
                logger.warning(
                    "orchestrator._fund_wallet: idempotency_key=%s already has a "
                    "committed ATOMIC booking — SKIPPING wallet_fund (would "
                    "re-fund+re-debit the wallet for a second live merchant "
                    "checkout on an identical re-POST).",
                    self._wallet_session_id,
                )
                return None
        # L10 — see `_digest_booked_lookup`'s constructor-arg docstring: a
        # derived-key row sharing THIS run's digest-based wallet_session_id
        # may be booked under a DIFFERENT store key that `trip_lookup` above
        # (keyed on the exact wallet_session_id) can never see. Extract the
        # bare digest the same way save_plan/get_booked_row_by_digest do
        # (idempotency_key == f"trip-{digest}" for any caller that never
        # overrides it — the surfaced_trip_id convention in negotiate()).
        if self._digest_booked_lookup is not None:
            _digest = (
                self._wallet_session_id[len("trip-"):]
                if self._wallet_session_id.startswith("trip-")
                else self._wallet_session_id
            )
            try:
                derived_booked = self._digest_booked_lookup(_digest)
            except Exception as exc:  # noqa: BLE001 — the lookup is a side hook, never fatal
                logger.warning(
                    "orchestrator._fund_wallet: digest_booked_lookup failed (%s) — "
                    "proceeding with normal fund/reset", exc,
                )
                derived_booked = False
            if derived_booked:
                logger.warning(
                    "orchestrator._fund_wallet: idempotency_key=%s shares its wallet "
                    "session with a DERIVED-key row that is currently booked — "
                    "SKIPPING wallet_fund (would reset/destroy that booked trip's "
                    "shared wallet ledger).",
                    self._wallet_session_id,
                )
                return None
        try:
            _fund_payload: dict[str, Any] = {
                "wallet_session_id": self._wallet_session_id,
                "wallet_balance_cents": self._wallet_balance_cents,
            }
            # #161 — bind the SIMULATED wallet's end-user ownership dimension
            # (wallet.OwnerUserID in ucp-merchant/wallet.go) to the same canonical
            # merchant identity used for checkout, so wallet_get is owner-checked
            # too. Omitted (no key) when unset — back-compat, no-binding demo path.
            if self._merchant_user_id:
                _fund_payload["user_id"] = self._merchant_user_id
            return self._call_budget_fund(_fund_payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "orchestrator._fund_wallet: budget.fund failed (%s) — wallet seam degraded",
                exc,
            )
            return None

    def _call_critic(self, payload: dict) -> dict | None:
        """
        Call the Critic agent (itinerary.verify).

        Returns None if no Critic is configured (M3a gate bypassed —
        backward-compatible with M2 tests).
        """
        if self._critic_client is not None:
            return _send_to_client(self._critic_client, payload, "itinerary.verify")
        if self._critic_url:
            return _send_to_url(self._critic_url, payload, "itinerary.verify")
        return None  # No Critic configured — bypass gate

    def _call_transport(
        self, legs: list[dict], persona: str = "default", overland_only: bool = False,
    ) -> dict | None:
        """
        Call the Transport agent (transport.feasibility).

        legs: list of {leg_id, city, area, checkin, checkout} dicts.
        persona: optional traveller persona ("comfort" widens the rail-preference range); default
            "default" sends the legacy payload shape unchanged (var-0 / backward-compatible).
        overland_only: #54 — request-level HARD no-fly constraint. False (default) sends the
            legacy payload shape unchanged (var-0 / backward-compatible); True tells the
            Transport agent to reject any edge whose only resolvable option is a flight.

        Returns the TransportResult dict, or None if no Transport agent is
        configured (M3b gate bypassed — backward-compatible with M3a tests).
        """
        # Persona/overland_only ride as ADDITIVE payload fields only when non-default → a
        # default caller sends exactly {"legs": legs} as before (byte-identical wire payload).
        payload: dict[str, Any] = {"legs": legs}
        if persona != "default":
            payload["persona"] = persona
        if overland_only:
            payload["overland_only"] = True
        if self._transport_client is not None:
            return _send_to_client(
                self._transport_client, payload, "transport.feasibility"
            )
        if self._transport_url:
            return _send_to_url(
                self._transport_url, payload, "transport.feasibility"
            )
        return None  # No Transport configured — bypass gate

    def _call_day_planner(self, legs: list[dict]) -> dict | None:
        """
        Call the Day-planner agent (activity.plan).

        legs: list of {leg_id, city, iso2, country, checkin, checkout,
                       interests?, dietary?, pace?, bad_weather_days?} dicts.

        Returns the {"leg_plans": [...]} dict, or None if no Day-planner agent is
        configured (bypassed — backward-compatible). OFF the money path: the
        returned plan is ADDITIVE (attached to the success result); it never
        blocks a commit. APPEND-ONLY hook per the §AGENT-EXTENSION PATTERN.
        """
        if self._day_planner_client is not None:
            return _send_to_client(
                self._day_planner_client, {"legs": legs}, "activity.plan"
            )
        if self._day_planner_url:
            return _send_to_url(
                self._day_planner_url, {"legs": legs}, "activity.plan"
            )
        return None  # No Day-planner configured — bypass

    def _call_food_search(self, query: dict) -> dict | None:
        """
        #44 — search the merchant's SIMULATED food-delivery catalog
        (search_catalog kind=FOOD_DELIVERY). `query` carries {kind, city, diet?,
        delivery_window?, max_cents?}. Returns the merchant structuredContent dict
        ({results, disclosure, ...}) or None when no food_search is configured
        (bypassed — backward-compatible). NEVER raises: a search failure degrades
        to None so the OPT-IN supper hook reports an honest "unavailable" rather
        than crashing the (already-successful) booking. APPEND-ONLY.
        """
        if self._food_search is None:
            return None  # No food search configured — bypass
        try:
            return self._food_search(query)
        except Exception as exc:  # noqa: BLE001 — supper is opt-in & off the itinerary path
            logger.warning("orchestrator: food search failed (ignored): %s", exc)
            return None

    def _call_dining_reviews(self, query: dict) -> dict | None:
        """
        #32 — fetch LIVE restaurant reviews/ratings from the provider seam
        (AMap-first / Google-fallback). `query` carries {provider?, city, iso2,
        cuisine?, venues?}. Returns the provider structuredContent dict
        ({venues:[{name, rating?, review_count?, ...}], source, ...}) or None when
        no dining provider is configured (keys PENDING → bypassed, backward-
        compatible). NEVER raises: a provider/network failure degrades to None so
        the OPT-IN enrichment hook reports an honest 'live reviews unavailable'
        note rather than crashing the (already-successful) booking. NO live
        content is ever persisted (ToS). APPEND-ONLY (mirrors _call_food_search).
        """
        client = self._dining_client
        if client is None:
            return None  # No dining provider configured — bypass (keys pending)
        try:
            return client(query)
        except Exception as exc:  # noqa: BLE001 — dining is opt-in & off the itinerary path
            logger.warning("orchestrator: dining reviews fetch failed (ignored): %s", exc)
            return None

    def _call_narrator(self, payload: dict) -> dict | None:
        """#3 — call the cosmetic itinerary-narrative seam (LLM). `payload` is the grounded
        day-by-day structure (real attractions/restaurants only). Returns the LLM's structured
        narrative dict, or None when no narrator is configured / on any failure (NEVER raises) — so a
        narrator outage degrades to no-narrative and can never break the (already-successful) booking.
        The result is post-validated against the deterministic corpus before anything is surfaced."""
        client = self._narrator_client
        if client is None:
            return None  # no narrator configured (LLM-off / deterministic showcase) — bypass
        try:
            return client(payload)
        except Exception as exc:  # noqa: BLE001 — narrative is cosmetic & off the var-0 path
            logger.warning("orchestrator: itinerary narrator failed (ignored): %s", exc)
            return None

    def _maybe_narrate_itinerary(self, result: dict) -> None:
        """#3 — OPT-IN cosmetic Google-style day-by-day itinerary narrative OVER the deterministic
        plan. APPEND-ONLY / var-0: when trip_request omits `narrate` (or no narrator is wired, or the
        trip didn't book) NO key is added → byte-identical to today. NEVER mutates result['day_plans']
        or any booking field; the narrative lands in a SEPARATE key result['itinerary_narrative'].

        Honesty firewall (utils/itinerary_narration): the LLM may only describe REAL attractions /
        restaurants the day-planner selected — every surfaced highlight/dining chip is validated
        against the deterministic corpus and canonicalised to the real name, so a hallucinated place
        cannot reach the UI. Fail-conservative: a narrator outage → no narrative, never a fabrication.
        """
        if not self._narrate_request:
            return  # not opted in → byte-identical to today
        if result.get("outcome") != "success":
            return  # never narrate a trip that didn't book
        day_plans = result.get("day_plans")
        if not isinstance(day_plans, list) or not day_plans:
            return  # nothing real to narrate
        from utils.itinerary_narration import (
            build_narration_payload, narration_corpus, validate_narrative,
        )
        payload = build_narration_payload(day_plans, result.get("legs"))
        if not payload.get("legs"):
            return  # all catalog-miss legs → no grounded content → no narrative
        # Phase marker (side-channel only, like the agent tracer emits): the cosmetic AI
        # narrator is the slow tail (~10-15s) after the agents finish — surface it so the live
        # board shows "Generating AI summary…" with a timer instead of a dead spinner.
        try:
            self._tracer("phase", "narrator", trip_id=self._trip_id, summary="Generating AI summary…")
        except Exception:  # noqa: BLE001 — tracer must never break the booking
            pass
        raw = self._call_narrator(payload)
        try:
            self._tracer("phase", "narrator_done", trip_id=self._trip_id, summary="")
        except Exception:  # noqa: BLE001
            pass
        if not isinstance(raw, dict):
            return  # narrator unavailable / failed → no narrative (honest, never fabricated)
        validated = validate_narrative(raw, narration_corpus(payload))
        validated["disclosure"] = (
            "AI-written narrative over your booked plan; every place shown is verified "
            "against your deterministic itinerary."
        )
        result["itinerary_narrative"] = validated

    def _call_emergency_feed(self, query: dict) -> dict | None:
        """
        #51 — query the LIVE active-emergency feed seam (official disaster/evacuation
        declarations). `query` carries {city, iso2, region?, checkin?, checkout?}.
        Returns the provider dict ({active: bool, hazard?, severity?, headline?,
        advice?, source?, as_of?}) or None when no feed is configured (defaults None
        → bypassed → byte-identical). NEVER raises: a feed/network failure degrades
        to None so the OPT-IN hook reports an honest 'live emergency status
        unavailable' note rather than crashing the (already-successful) booking, and
        NEVER fabricates an all-clear. APPEND-ONLY (mirrors _call_dining_reviews)."""
        client = self._emergency_client
        if client is None:
            return None  # No emergency feed configured — bypass (var-0 no-op)
        try:
            return client(query)
        except Exception as exc:  # noqa: BLE001 — emergency overlay is opt-in & off the var-0 path
            logger.warning("orchestrator: emergency feed fetch failed (ignored): %s", exc)
            return None

    def _call_emergency_feed_batch(self, queries: list[dict]) -> list:
        """#51 perf/robustness fix (F1, 2026-07-06 adversarial audit) — fetch the
        live feed ONCE per trip-check call and reuse it for every leg, instead of
        `_call_emergency_feed`'s one-network-round-trip-per-leg. Each per-trip
        client call retries up to 2x5s + 0.3s backoff (~10.3s); calling it once
        per leg meant an N-leg trip could hold a worker thread (shared with
        unrelated bookings, only 4 wide) for up to N * ~10.3s during a GDACS
        outage.

        For the REAL gdacs client specifically (the one actually wired live via
        EMERGENCY_FEED=gdacs) this fetches the raw feature list ONCE
        (`emergency_feed.gdacs_fetch_events`) and filters it per leg IN-PROCESS
        (`emergency_feed._gdacs_match_leg`, no further network I/O). Any other
        configured client (the deterministic stub, or a prod-swapped live
        provider) keeps the existing one-call-per-leg behavior unchanged — this
        is a targeted fix for the actual live network client, not a change to
        the `client(query) -> dict | None` provider contract.

        Returns a list the same length as `queries`, each entry either the
        provider's dict or None (the existing 'unavailable' signal). A
        batch-level failure honestly degrades EVERY leg to None (never a
        fabricated all-clear) rather than raising — same fail-conservative
        contract as `_call_emergency_feed`.

        NOTE: the `client is None` / unconfigured case is deliberately NOT
        special-cased here — it falls through to the generic per-leg path
        below, which delegates to `_call_emergency_feed` (the single place that
        already handles "no client configured" → None). This keeps
        `_call_emergency_feed` the one source of truth for that check (tests
        patch it directly) instead of duplicating/bypassing its logic."""
        client = self._emergency_client
        if client is _emergency_feed.gdacs_emergency_client:
            try:
                fetched = _emergency_feed.gdacs_fetch_events()
            except Exception as exc:  # noqa: BLE001 — never let a batch fetch break the booking
                logger.warning("orchestrator: gdacs batch fetch failed (ignored): %s", exc)
                return [None] * len(queries)
            if fetched is None:
                # Fetch itself failed after its retry → honest 'unavailable' for
                # every leg (consistent with each leg independently failing the
                # same way, just without N redundant network round-trips).
                return [None] * len(queries)
            features, feed_as_of = fetched
            out = []
            for q in queries:
                try:
                    out.append(_emergency_feed._gdacs_match_leg(features, feed_as_of, q))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("orchestrator: gdacs per-leg match failed (ignored): %s", exc)
                    out.append(None)
            return out
        # Any other provider (stub / prod-swapped live client) → unchanged behavior.
        return [self._call_emergency_feed(q) for q in queries]

    # ------------------------------------------------------------------
    # #159 — travel_memory MCP client seam (the memory loop only: log_search WRITE +
    # resolve_geographic_scope affinity READ). See utils/memory_client.py for the
    # genuine mcp.client.session.ClientSession transport + honest-degradation
    # contract (any failure -> None, never raises).
    # ------------------------------------------------------------------

    def _maybe_read_affinity(self, user_id: str, legs_input: list[dict]) -> dict | None:
        """#159 IP-2 (READ) — cross-check the trip's first-leg city against the
        user's stored preference vector via a REAL MCP call_tool('resolve_geographic_
        scope', ...). Returns a small display-only hint dict, or None when no memory
        client is configured / the request has no resolvable city / the tool call
        failed / no affinity is on file. NEVER raises, NEVER blocks negotiate() —
        called BEFORE booking so a slow/dead memory server degrades to no hint, not a
        stalled trip (the client itself is timeout-bounded; see memory_client.py).
        Display-only: the caller attaches this (if any) to result['personalization'],
        a SEPARATE top-level key that never feeds ranking/area/price (var-0 sacred).

        M1 (IDOR): `user_id` here MUST be the session-verified identity — see the
        call site in negotiate(), which passes the properly-gated identity, never
        the raw untrusted trip_request user_id. M1 follow-up (security review): that
        call site prefers self._memory_user_id (session-verified — see its
        __init__ docstring) over self._merchant_user_id (only TIER-mirrored, not
        session-verified, at plan-creation time) when the server boundary set one.
        """
        if self._memory_client is None:
            return None
        if not isinstance(legs_input, list):
            return None
        first_city = ""
        for leg in legs_input:
            if isinstance(leg, dict) and isinstance(leg.get("city"), str) and leg["city"].strip():
                first_city = leg["city"].strip()
                break
        if not first_city:
            return None
        try:
            resp = self._memory_client.resolve_geographic_scope(
                prompt=first_city, user_id=user_id or ""
            )
        except Exception as exc:  # noqa: BLE001 — personalization is display-only & off var-0
            logger.warning("orchestrator: memory resolve_geographic_scope failed (ignored): %s", exc)
            return None
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            return None  # unresolved city / tool error — no hint (honest, no fabrication)
        affinity_city = resp.get("affinity_city")
        if not affinity_city or not isinstance(affinity_city, str):
            return None  # no preference on file yet (new user / no SEA history) — no-op
        return {
            "affinity_city": affinity_city,
            "resolved_city": resp.get("resolved_city"),
            "message": f"Welcome back — your recent trips favor {affinity_city.title()}.",
        }

    def _maybe_log_search(self, result: dict, trip_request: dict) -> None:
        """#159 IP-1 (WRITE) — for each booked leg, issue a REAL MCP call_tool
        ('log_search', {user_id, query, selection}) so the travel_memory server's
        21-dim EMA preference vector (store.py §10.3) learns from this trip. Called
        POST-FREEZE (after the result dict is fully built) so it can NEVER perturb
        the var-0 result bytes — identical firewall to the #64 telemetry emit.
        No-op unless a memory client is configured AND the trip booked. NEVER raises.

        M1 (IDOR): travel_memory's log_search tool trusts whatever user_id it is
        given with no ownership check of its own (mirrors the #161 Go-merchant
        gap). This is the ONE call site that decides which user's preference
        vector this trip's search gets written to, so it MUST use a
        properly-gated identity, never the raw caller-supplied result/
        trip_request user_id, which an untrusted client could set to someone
        else's id.

        M1 follow-up (informational): self._merchant_user_id alone
        is only TIER-mirrored (mirrors #161's checkout tier decision), not
        session-verified, at plan-creation time — real verification happens
        later, at /confirm. That is fine for checkout (re-verified there before
        anything irreversible happens) but this WRITE fires at negotiate() time
        with no later gate, so an unverified tier-1 claim must not reach it.
        Prefer self._memory_user_id (session-verified by the server boundary —
        see its __init__ docstring) when set; "" there is a deliberate denial
        and must NOT fall through to self._merchant_user_id or any untrusted
        value. Falls back to the OLD self._merchant_user_id-based chain only
        when self._memory_user_id is None (direct/test callers that invoke this
        helper without going through negotiate() first, or negotiate() runs
        that never set self._memory_user_id — byte-identical to pre-follow-up
        behavior for those callers).
        """
        if self._memory_client is None:
            return
        if not isinstance(result, dict) or result.get("outcome") != "success":
            return
        legs = result.get("legs")
        if not isinstance(legs, list):
            return
        user_id = (
            self._memory_user_id if self._memory_user_id is not None
            else (self._merchant_user_id or result.get("user_id") or trip_request.get("user_id") or "")
        )
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            nights = leg.get("nights")
            total_cents = leg.get("total_cents")
            price_cents_night = None
            if isinstance(total_cents, int) and isinstance(nights, int) and nights > 0:
                price_cents_night = total_cents // nights
            query = {
                "city": leg.get("city", ""),
                "checkin": leg.get("checkin", ""),
                "checkout": leg.get("checkout", ""),
            }
            selection = {
                "stars": leg.get("star_rating"),
                "price_cents_night": price_cents_night,
                "area": leg.get("area"),
            }
            try:
                self._memory_client.log_search(user_id=user_id, query=query, selection=selection)
            except Exception as exc:  # noqa: BLE001 — memory write is opt-in & off the var-0 path
                logger.warning("orchestrator: memory log_search failed (ignored): %s", exc)

    def _call_insurance(self, payload: dict) -> dict | None:
        """
        Call the Insurance agent (insurance.assess_coverage). Sequenced AFTER Risk
        (it consumes the peril_set / risk_reason_codes) and BEFORE final package
        assembly: the returned assessment carries a premium 'line_item' that flows
        into Budget's veto + single mandate like any other line item (Insurance
        proposes, Budget enforces), and excluded_perils_summary feeds the goal-shift
        signal. Returns None if no Insurance agent is configured (bypassed —
        backward-compatible). APPEND-ONLY hook per the §AGENT-EXTENSION PATTERN.

        payload: {peril_set[]?, risk_reason_codes[]?, policy_id?,
                  insured_trip_cost_cents?, leg?, region?}.
        """
        if self._insurance_client is not None:
            return _send_to_client(
                self._insurance_client, payload, "insurance.assess_coverage"
            )
        if self._insurance_url:
            return _send_to_url(
                self._insurance_url, payload, "insurance.assess_coverage"
            )
        return None  # No Insurance configured — bypass

    def _call_coverage_gap(self, payload: dict) -> dict | None:
        """
        #45 — call the Insurance agent's coverage-GAP analyzer
        (insurance.coverage_gap), which cross-checks the trip's perils against the
        TRAVELER's OWN declared policy. Reuses the SAME _insurance_client /
        _insurance_url transport (it is a second skill on the Insurance agent), but
        recommends NO vendor and mints no premium. OPT-IN: the orchestrator only
        invokes this when self._user_policy is set (trip_request['user_policy']).
        Returns None if no Insurance agent is configured (bypassed). APPEND-ONLY.

        payload: {peril_set[], user_policy{}, insured_trip_cost_cents?}.
        """
        if self._insurance_client is not None:
            return _send_to_client(
                self._insurance_client, payload, "insurance.coverage_gap"
            )
        if self._insurance_url:
            return _send_to_url(
                self._insurance_url, payload, "insurance.coverage_gap"
            )
        return None  # No Insurance configured — bypass

    def _call_compliance(self, payload: dict) -> dict | None:
        """
        Call the Compliance agent (compliance.check_eligibility) — the deterministic
        eligibility GATE. Sequenced EARLY (it is a precondition on the whole trip:
        an UNBOOKABLE-in-time visa makes the itinerary invalid regardless of price/
        coverage). The returned verdict carries:
          - verdict ("can_satisfy"/"cannot_satisfy") + bookable flag — a cannot_satisfy
            BLOCKS the itinerary (the gate never returns an unbookable-in-time trip),
          - 'line_items' (visa/eVisa fees) that flow into Budget's veto like any other
            line item (Compliance proposes, Budget enforces),
          - 'resequence' (depart-later date / visa-free alternative) for an honest block.
        Returns None if no Compliance agent is configured (bypassed — backward-
        compatible). APPEND-ONLY hook per the §AGENT-EXTENSION PATTERN.

        payload: {legs[{dest_country, departure_date}], nationality, today?, buffer_days?}.
        """
        if self._compliance_client is not None:
            return _send_to_client(
                self._compliance_client, payload, "compliance.check_eligibility"
            )
        if self._compliance_url:
            return _send_to_url(
                self._compliance_url, payload, "compliance.check_eligibility"
            )
        return None  # No Compliance configured — bypass

    def _call_risk(self, payload: dict) -> dict | None:
        """
        Call the Risk agent (risk.assess) — the L1 proactive signal CONSOLIDATOR.
        Sequenced ONE-SHOT pre-loop (like Destination.assess): it emits PLANNING-
        INPUT signals only (cyclone likelihood / median delay / seismic resilience
        + an avoid/buffer/flag decision). It is OFF the money path — NO fee, NO
        checkout, NO mandate — so it can NEVER block or alter a booking; the
        Planner CONSUMES the roll-up ADDITIVELY (avoid/buffer/flag) and the
        signals are attached to the result for surfacing. Returns None if no Risk
        agent is configured (bypassed — backward-compatible; S1–S5 byte-identical).
        APPEND-ONLY hook per the §AGENT-EXTENSION PATTERN.

        payload: {legs[{city, iso2?, checkin?, checkout?, mode?}]}.
        """
        if self._risk_client is not None:
            return _send_to_client(self._risk_client, payload, "risk.assess")
        if self._risk_url:
            return _send_to_url(self._risk_url, payload, "risk.assess")
        return None  # No Risk configured — bypass (S1–S5 unaffected)

    def _call_health(self, payload: dict) -> dict | None:
        """
        Call the Health agent (health.assess) — the vaccination + entry-cert GATE.
        Sequenced EARLY alongside Compliance (it is a precondition on the whole trip:
        a mandatory entry certificate that is UNBOOKABLE in time makes the itinerary
        invalid regardless of price/coverage). The returned verdict carries:
          - verdict ("can_complete"/"cannot_complete") + bookable flag — a
            cannot_complete BLOCKS the itinerary (the gate never returns a plan
            missing a mandatory entry cert; UNKNOWN slate → conservative flag),
          - 'line_items' (the UPFRONT vaccine cost) that flow into Budget's veto like
            any other line item (Health proposes, Budget enforces) — the "$1k the
            single agent forgets" correction,
          - 'jab_records' (the Health half of the §7 YF-cert handoff) Compliance reads
            to own the entry-DOCUMENT half.
        Returns None if no Health agent is configured (bypassed — backward-
        compatible; S1–S5 byte-identical). APPEND-ONLY hook per the §AGENT-EXTENSION
        PATTERN.

        payload: {legs[{place_key|city, departure_date}], today?, buffer_days?}.
        """
        if self._health_client is not None:
            return _send_to_client(self._health_client, payload, "health.assess")
        if self._health_url:
            return _send_to_url(self._health_url, payload, "health.assess")
        return None  # No Health configured — bypass (S1–S5 unaffected)

    def _call_destination(
        self,
        city: str,
        vibe: str | None,
        checkin: str | None = None,
        checkout: str | None = None,
    ) -> list[str]:
        """
        Call the Destination agent (destination.assess) → ranked target areas.

        Runs ONE-SHOT per leg, before the re-plan loop (§10.11).

        When checkin/checkout are supplied, the agent may also return a SEEDED
        seasonal advisory (e.g. the WA bushfire-season flag); the advisory is
        ADVISORY-ONLY (it never changes the area list or blocks booking), so we
        only log it here — the area-targeting contract is unchanged.

        Dispatch order:
          1. destination_client (in-process A2A TestClient) — full A2A path.
          2. destination_url (HTTP A2A) — live path.
          3. No Destination wired → DETERMINISTIC fallback area list (no LLM).
             This keeps M2/M3 unit tests (which don't supply a Destination)
             deterministic and backward-compatible.

        Returns a (possibly empty) list of real catalog areas.
        """
        payload = {"city": city, "vibe": vibe, "checkin": checkin, "checkout": checkout}
        try:
            if self._destination_client is not None:
                res = _send_to_client(self._destination_client, payload, "destination.assess")
            elif self._destination_url:
                res = _send_to_url(self._destination_url, payload, "destination.assess")
            else:
                # No Destination agent wired — deterministic fallback only.
                from agents.destination_agent import _deterministic_fallback
                areas, _src = _deterministic_fallback(city, vibe)
                return areas
        except Exception as exc:  # noqa: BLE001 — Destination is one-shot & advisory
            logger.warning(
                "orchestrator: destination.assess failed for city=%s vibe=%s: %s "
                "— using deterministic fallback",
                city, vibe, exc,
            )
            from agents.destination_agent import _deterministic_fallback
            areas, _src = _deterministic_fallback(city, vibe)
            return areas

        advisory = res.get("advisory") if isinstance(res, dict) else None
        if advisory:
            logger.info(
                "orchestrator: seasonal advisory for city=%s dates=%s→%s: [%s] %s (provenance=%s)",
                city, checkin, checkout,
                advisory.get("severity"), advisory.get("flag"), advisory.get("provenance"),
            )
        areas = res.get("areas", []) if isinstance(res, dict) else []
        return [a for a in areas if isinstance(a, str)]

    # ------------------------------------------------------------------
    # Budget range estimation (no confirmed budget yet, Part A guidance)
    # ------------------------------------------------------------------

    def estimate_budget_range(self, trip_request: dict) -> dict | None:
        """Estimate a low/high budget range for a trip whose budget is not yet known.

        Gathers the SAME candidate set the planner would use (via
        _gather_candidates_for_dp with a high sentinel ceiling) and computes:
          low  = Σ(cheapest candidate per leg) + enforced_envelope (0 when no gates)
          high = Σ(mid-tier candidate per leg) + enforced_envelope

        Multi-currency: formats the range in local destination currency + USD,
        deduping when they coincide, and appends exchange-timing guidance.

        Returns:
            {
                "low_cents": int,   # raw unrounded low
                "high_cents": int,  # raw unrounded high
                "low_rounded": int,
                "high_rounded": int,
                "message": str,     # multi-currency human-readable sentence
            }
        or None if legs are not fully resolved (missing city/checkin/checkout) or
        if no catalog inventory can be found for any leg.

        Deterministic (var-0): same inputs → same output. No LLM, no clock, no network.
        """
        import datetime as _dt
        from utils import budget_estimate
        from utils.currency_advisory import (
            currency_for_country,
            convert_usd_cents,
            exchange_timing_advice,
            decimals as ca_decimals,
            AS_OF,
        )
        from utils.intent_parser import CITY_TO_ISO2

        legs_input: list[dict] = trip_request.get("legs", [])
        if not legs_input:
            return None

        # Validate all legs have the required fields
        for leg in legs_input:
            if not (leg.get("city") and leg.get("checkin") and leg.get("checkout")):
                return None

        # High sentinel ceiling: gather ALL catalog rows (no budget filter)
        _SENTINEL_BUDGET = 9_999_999_99  # ~$99,999 — covers any realistic catalog

        # Ensure _gather_reasons dict exists (normally created by _gather_candidates_for_dp)
        if not hasattr(self, "_gather_reasons"):
            self._gather_reasons = {}

        target_areas: dict[str, list[str]] = {}
        area_stage: dict[str, int] = {}
        leg_floors: list[int] = []
        leg_mids: list[int] = []

        for i, leg in enumerate(legs_input):
            leg_id = f"leg-{i}"
            city = leg.get("city", "")
            vibe = leg.get("vibe")

            # Best-effort destination area lookup (advisory; failure is ok)
            try:
                areas = self._call_destination(
                    city, vibe,
                    checkin=leg.get("checkin"),
                    checkout=leg.get("checkout"),
                )
            except Exception:
                areas = []
            target_areas[leg_id] = areas

            cands = self._gather_candidates_for_dp(
                leg_id=leg_id,
                city=city,
                checkin=leg.get("checkin", ""),
                checkout=leg.get("checkout", ""),
                adults=int(leg.get("adults", 1)),
                vibe=vibe,
                target_areas=areas or None,
                total_budget_cents=_SENTINEL_BUDGET,
                target_areas_dict=target_areas,
                area_stage_dict=area_stage,
                prefer_lodging_types=leg.get("prefer_lodging_types"),
                avoid_lodging_types=leg.get("avoid_lodging_types"),
                dest_country=leg.get("dest_country"),
            )

            if not cands:
                logger.info(
                    "orchestrator: estimate_budget_range: no candidates for leg=%s city=%s — "
                    "cannot estimate",
                    leg_id, city,
                )
                return None

            # leg_floor = minimum total_cents
            valid_costs = [int(c["total_cents"]) for c in cands if c.get("total_cents")]
            if not valid_costs:
                return None

            leg_floors.append(min(valid_costs))

            # leg_mid = median by (total_cents, hotel_id) — tie-stable
            sorted_cands = sorted(
                (c for c in cands if c.get("total_cents")),
                key=lambda c: (int(c.get("total_cents", 0)), str(c.get("hotel_id", ""))),
            )
            mid_idx = len(sorted_cands) // 2
            leg_mids.append(int(sorted_cands[mid_idx].get("total_cents", min(valid_costs))))

        # Mandatory-fee envelope: 0 when no health/compliance/risk gates are wired
        # (typical for the estimate path). The full negotiate() enforces these separately.
        fees_cents = 0

        # Compute raw + rounded range
        low_cents, high_cents = budget_estimate.estimate_range(leg_floors, leg_mids, fees_cents)
        low_rounded, high_rounded = budget_estimate.round_band(low_cents, high_cents)

        # ------------------------------------------------------------------
        # Multi-currency formatting
        # ------------------------------------------------------------------
        # Currency symbols for display
        _SYMBOLS: dict[str, str] = {
            "USD": "US$", "SGD": "S$", "AUD": "A$", "CAD": "C$", "NZD": "NZ$",
            "HKD": "HK$", "TWD": "NT$", "EUR": "€", "GBP": "£", "JPY": "¥",
            "CNY": "¥", "IDR": "Rp ", "THB": "฿", "MYR": "RM ", "KRW": "₩",
            "VND": "₫", "INR": "₹", "PHP": "₱", "BRL": "R$", "MXN": "MX$",
            "CHF": "CHF ", "SEK": "kr", "NOK": "kr", "DKK": "kr",
        }

        def _fmt_minor(minor_units: int, iso: str) -> str:
            """Format minor units (from convert_usd_cents) as a display string."""
            dec = ca_decimals(iso)
            major = minor_units / 100
            if dec == 0:
                # Zero-decimal: show in thousands (k) or millions (M) for readability
                val = int(round(major))
                if val >= 1_000_000:
                    return f"{val / 1_000_000:.1f}M"
                elif val >= 10_000:
                    return f"{round(val / 1_000)}k"
                return f"{val:,}"
            else:
                return f"{int(round(major)):,}"

        def _currency_str(usd_cents: int, iso: str) -> str | None:
            val = convert_usd_cents(usd_cents, iso)
            if val is None:
                return None
            sym = _SYMBOLS.get(iso, iso + " ")
            return f"{sym}{_fmt_minor(val, iso)}"

        # Determine local currency from primary destination. #87: an EXPLICIT
        # dest_country on the leg (the traveller's own stated destination) is
        # authoritative and must win over a CITY_TO_ISO2 catalog guess (a bare
        # city name can collide with a same-named city in a different country,
        # e.g. 'victoria' -> CITY_TO_ISO2 'HK' vs an explicit Seychelles request).
        primary_city = (legs_input[0].get("city") or "").lower().strip()
        primary_dest_country = (legs_input[0].get("dest_country") or "").strip().upper()
        dest_iso2 = primary_dest_country or CITY_TO_ISO2.get(primary_city)
        local_iso: str | None = None
        if dest_iso2:
            local_iso = currency_for_country(dest_iso2.lower())

        # Home/display currency from the user profile (slice 4) when supplied, else USD.
        # Display-only (exchange-timing advice) → NOT in _request_digest, var-0-safe.
        home_iso = (trip_request.get("home_currency") or "USD").upper()

        # Build the range string
        range_parts: list[str] = []
        if local_iso and local_iso != "USD":
            loc_low = _currency_str(low_rounded, local_iso)
            loc_high = _currency_str(high_rounded, local_iso)
            usd_low = _currency_str(low_rounded, "USD")
            usd_high = _currency_str(high_rounded, "USD")
            if loc_low and loc_high:
                range_parts.append(f"{loc_low}–{loc_high}")
                if usd_low and usd_high:
                    range_parts.append(f"≈ {usd_low}–{usd_high}")
        else:
            # USD-only destination or unknown local currency
            usd_low = _currency_str(low_rounded, "USD")
            usd_high = _currency_str(high_rounded, "USD")
            if usd_low and usd_high:
                range_parts.append(f"{usd_low}–{usd_high}")
            elif not range_parts:
                range_parts.append(
                    f"US${low_rounded // 100:,}–US${high_rounded // 100:,}"
                )

        range_str = " / ".join(range_parts) or f"US${low_rounded // 100:,}–US${high_rounded // 100:,}"

        # Trip summary
        total_nights = sum(
            (_dt.date.fromisoformat(leg["checkout"]) - _dt.date.fromisoformat(leg["checkin"])).days
            for leg in legs_input
            if leg.get("checkin") and leg.get("checkout")
        )
        pax = int(legs_input[0].get("adults", 1))
        city_title = (legs_input[0].get("city") or "this destination").title()
        nights_str = f"{total_nights}-night " if total_nights else ""

        # Exchange timing guidance
        timing_str = ""
        if local_iso and local_iso != "USD":
            advice = exchange_timing_advice(local_iso, home_iso)
            timing_str = f" {advice['guidance']} ({advice['caveat']})"

        message = (
            f"A {nights_str}{city_title} trip for {pax} typically runs "
            f"{range_str} (indicative as-of {AS_OF}).{timing_str} "
            f"Please confirm a budget to proceed."
        )

        return {
            "low_cents": low_cents,
            "high_cents": high_cents,
            "low_rounded": low_rounded,
            "high_rounded": high_rounded,
            "message": message,
        }

    # ------------------------------------------------------------------
    # #93/#94: backfill dest_country onto leg_meta (Planner-skeleton legs)
    # ------------------------------------------------------------------

    def _enrich_leg_meta_dest_country(self, leg_meta: dict[str, dict]) -> None:
        """
        leg_meta is built as ``{leg["leg_id"]: leg for leg in legs}`` from the
        Planner agent's skeleton output (§ negotiate() build #30 comment,
        ~line 2498) — the skeleton carries city/checkin/checkout/adults/
        per_leg_budget_cents, but NEVER dest_country. That's why
        self._trip_request_legs (the ORIGINAL trip_request legs, set at the
        top of negotiate()) was introduced as a side-channel — but until now
        nothing consulted it for leg_meta's consumers.

        This backfills leg_meta[lid]["dest_country"] from
        self._trip_request_legs by leg-N index, mirroring the src_leg lookup
        pattern the day-planner activity_legs build already uses (the #87 fix
        site, ~line 6262). Mutates leg_meta's leg dicts IN PLACE (the same
        pattern used elsewhere for leg["hotel_lat"]/leg["day_plan"] etc.) —
        additive only, never overwrites an already-present dest_country.

        Makes dest_country available to:
          - _propose_with_area_ladder's acc_payload → Accommodation's country
            filter (#93 — a bare city search like "victoria" otherwise matches
            ANY country's catalog rows under that name).
          - _primary_dest_token / booking_ref minting (#94 — was reading a
            leg_meta key that was always absent, silently falling through to
            the CITY_TO_ISO2 guess every time).

        No-op (byte-identical) when self._trip_request_legs is unset/short,
        a leg_id doesn't match the "leg-N" convention, or dest_country was
        already supplied directly on the leg (defensive; never observed today
        since the skeleton doesn't carry the key at all).
        """
        trip_legs = getattr(self, "_trip_request_legs", None) or []
        for lid, lm in leg_meta.items():
            if not isinstance(lm, dict) or lm.get("dest_country"):
                continue
            if not isinstance(lid, str) or not lid.startswith("leg-"):
                continue
            try:
                idx = int(lid.split("-", 1)[1])
            except (ValueError, IndexError):
                continue
            if 0 <= idx < len(trip_legs):
                src_leg = trip_legs[idx] or {}
                dest_country = src_leg.get("dest_country")
                if dest_country:
                    lm["dest_country"] = dest_country

    # ------------------------------------------------------------------
    # Accommodation: gather FULL candidate set per leg (for DP, §2.1)
    # ------------------------------------------------------------------

    def _gather_candidates_for_dp(
        self,
        *,
        leg_id: str,
        city: str,
        checkin: str,
        checkout: str,
        adults: int,
        vibe: str | None,
        target_areas: list[str] | None,
        total_budget_cents: int,
        target_areas_dict: dict[str, list[str]] | None = None,
        area_stage_dict: dict[str, int] | None = None,
        avoid_lodging_types: list[str] | None = None,
        prefer_lodging_types: list[str] | None = None,
        dest_country: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Gather the FULL ranked candidate set for one leg (§2.1 DP pre-gather step).

        Calls Accommodation with max_cents=total_budget_cents (full ceiling) to
        retrieve ALL candidates that could fit in the total budget.  The DP then
        selects the globally optimal combination.

        The Accommodation agent:
          - Calls merchant search_catalog (real catalog rows only)
          - Applies M-Agentic-2 area filter (deterministic)
          - Applies M-Agentic-3 LLM ranking (variance-clamped, one-shot per leg)
          Returns: proposal (best candidate) + alternates (up to 2 more)

        We combine proposal + alternates into the full candidate list, then
        attach quality scores for the DP using the ranking_source field.

        Important: This call uses max_cents = total_budget_cents so we see ALL
        candidates the hotel catalog contains under the total trip budget.  The
        DP then decides which subset to use (one per leg) optimally.

        For DP feasibility the gather step uses a THREE-PASS approach:
          Pass 1 — vibe-narrow areas (from Destination LLM, stored in target_areas):
            highest-quality candidates, LLM-ranked for preference.
          Pass 2 — city-wide search with target_areas=None:
            retrieves candidates so the DP can find alternatives outside vibe areas.
          Pass 3 — cheapest-anchor sweep (DETERMINISTIC, §10.11 fix):
            sweeps progressively tighter ceilings to guarantee the globally
            cheapest hotel is always in the candidate set regardless of LLM
            ranking order.  Eliminates correctness variance on S4 (two beach
            legs where LLM ranking could exclude the cheapest surf/beach hotel).

        All three passes are MERGED (dedup by hotel_id).  Narrow-area candidates
        retain their LLM quality score (preferred); wide-area and cheap-anchor
        extras default to quality 0.0 (used only when necessary for feasibility).

        If no candidates are found at all (e.g. city has no hotels under budget),
        the same deterministic area-broaden ladder used in _propose_with_area_ladder
        is applied (narrow → broad → city-wide) so that DP candidate gathering
        has the same coverage as the re-plan loop.  target_areas_dict and
        area_stage_dict are updated in-place so later rounds inherit the broadened
        set (avoids redundant broadening in the veto/re-plan loop).
        """
        # #70 honesty: record WHY a leg yields zero candidates so the terminal cannot_satisfy
        # message can attribute it (no_inventory vs over_budget vs search_error) to THIS leg,
        # instead of always blaming the budget. Keyed by leg_id; overwritten each gather.
        if not hasattr(self, "_gather_reasons"):
            self._gather_reasons: dict[str, str] = {}
        cur_areas: list[str] | None = target_areas

        # ----------------------------------------------------------------
        # PASS 1: vibe-narrow / vibe-broad ladder (quality candidates)
        # ----------------------------------------------------------------
        while True:
            acc_payload: dict[str, Any] = {
                "city": city,
                "checkin": checkin,
                "checkout": checkout,
                "adults": adults,
                "max_cents": total_budget_cents,   # full budget ceiling for DP candidate gathering
                "vibe": vibe,
                "target_areas": cur_areas or None,
                "avoid_lodging_types": avoid_lodging_types,
                "prefer_lodging_types": prefer_lodging_types,
                # #93: thread the leg's explicit dest_country through so Accommodation
                # can filter merchant catalog rows by country too — a bare city search
                # (e.g. "victoria") otherwise matches ANY country's inventory under
                # that name and can propose the wrong country's hotel entirely.
                "dest_country": dest_country,
            }
            try:
                acc_result = self._call_accommodation(acc_payload)
            except Exception as exc:
                logger.warning(
                    "orchestrator: DP candidate gather failed for leg=%s: %s", leg_id, exc
                )
                self._gather_reasons[leg_id] = "search_error"
                return []

            if acc_result.get("fit") == "ok":
                # Found candidates — update shared dicts so re-plan rounds know
                # what area set was actually used.
                if target_areas_dict is not None and cur_areas is not None:
                    target_areas_dict[leg_id] = cur_areas
                break

            # no_fit → try area broadening (same ladder as _propose_with_area_ladder)
            stage = area_stage_dict.get(leg_id, 0) if area_stage_dict is not None else 2
            if stage >= 2:
                # Already city-wide; genuinely no_fit
                self._gather_reasons[leg_id] = acc_result.get("reason_code") or "no_inventory"
                return []

            if cur_areas is None:
                cur_areas = []
            if stage == 0:
                broadened = broaden_areas(city, vibe, cur_areas)
            else:  # stage == 1 → go city-wide
                broadened = city_wide_areas(city)

            if set(broadened) <= set(cur_areas):
                # Broadening didn't grow the set — no more options
                self._gather_reasons[leg_id] = acc_result.get("reason_code") or "no_inventory"
                return []

            logger.info(
                "orchestrator: DP gather area-broaden leg=%s stage %d→%d areas=%s (max=%d¢)",
                leg_id, stage, stage + 1, broadened, total_budget_cents,
            )
            cur_areas = broadened
            if area_stage_dict is not None:
                area_stage_dict[leg_id] = stage + 1
            # loop and retry with the broadened set

        if acc_result.get("fit") != "ok":  # pragma: no cover - defensive: the loop only break-exits
            # here when fit=="ok"; the no-fit cases already `return []` inside the loop. Kept as a
            # belt-and-suspenders guard against a future loop-invariant change.
            self._gather_reasons[leg_id] = acc_result.get("reason_code") or "no_inventory"
            return []

        # Collect proposal + alternates from PASS 1 as the quality candidate set
        candidates: list[dict[str, Any]] = []
        proposal = acc_result.get("proposal")
        if proposal:
            candidates.append(proposal)
        for alt in acc_result.get("alternates") or []:
            candidates.append(alt)

        # Attach quality scores for the DP (§2.1 M-Agentic-3 integration)
        # The candidates are already LLM-ranked by the Accommodation agent
        # (ranking_source = "llm" | "fallback").  The DP uses rank position
        # as quality so it optimises for LLM preference globally.
        ranking_source = proposal.get("ranking_source", "fallback") if proposal else "fallback"
        from utils.allocator import attach_quality_scores
        candidates_with_quality = attach_quality_scores(candidates, ranking_source)

        # ----------------------------------------------------------------
        # PASS 2: city-wide search — adds cheap alternatives so DP always
        # has access to the minimum-cost hotel in the city, regardless of
        # which vibe-areas the Destination LLM selected.  Extra candidates
        # get quality=0.0 (floor) so DP only picks them when narrow-area
        # candidates alone exceed the budget.
        #
        # We always try this pass because even when narrow areas succeed,
        # the narrow candidates might all be expensive and collectively
        # exceed the total budget across legs.  City-wide extras ensure
        # the DP's feasibility precheck has access to the cheapest options.
        # ----------------------------------------------------------------
        known_ids = {c["hotel_id"] for c in candidates_with_quality}
        try:
            wide_payload: dict[str, Any] = {
                "city": city,
                "checkin": checkin,
                "checkout": checkout,
                "adults": adults,
                "max_cents": total_budget_cents,
                "vibe": vibe,
                "target_areas": None,   # city-wide — no area restriction
                "avoid_lodging_types": avoid_lodging_types,
                "prefer_lodging_types": prefer_lodging_types,
                "dest_country": dest_country,   # #93: country filter, see PASS 1 above
            }
            wide_result = self._call_accommodation(wide_payload)
            if wide_result.get("fit") == "ok":
                wide_cands: list[dict[str, Any]] = []
                if wide_result.get("proposal"):
                    wide_cands.append(wide_result["proposal"])
                for alt in wide_result.get("alternates") or []:
                    wide_cands.append(alt)
                # Add any hotel not already in narrow-area results, with quality=0.0
                for wc in wide_cands:
                    if wc.get("hotel_id") not in known_ids:
                        candidates_with_quality.append({**wc, "quality": 0.0})
                        known_ids.add(wc["hotel_id"])
                        logger.info(
                            "orchestrator: DP gather WIDE-extra leg=%s → %s total=%d¢",
                            leg_id, wc.get("hotel_id"), wc.get("total_cents", 0),
                        )
        except Exception as exc:
            logger.warning(
                "orchestrator: DP gather PASS 2 city-wide failed leg=%s: %s", leg_id, exc
            )

        # ----------------------------------------------------------------
        # PASS 3: cheapest-anchor sweep (DETERMINISTIC — §10.11).
        #
        # The DP feasibility precheck requires the GLOBAL minimum-cost hotel
        # per leg.  Passes 1 and 2 use LLM ranking, which caps the returned
        # candidates at 3 (proposal + 2 alternates).  When the cheapest hotel
        # in the city is ranked 4th or lower by the LLM, it is excluded from
        # the candidate set, causing the DP precheck to falsely declare the
        # trip infeasible (correctness variance ≈50% on S4 — the thesis-
        # breaking regression fixed here).
        #
        # This pass sweeps cheapest-first by calling accommodation with a
        # progressively tightening ceiling (current_min - 1 cents) until no
        # cheaper hotel is found.  Each call is guaranteed to return a hotel
        # cheaper than any previously seen (merchant price ordering is real,
        # not LLM-influenced at this ceiling).  The sweep adds all new cheap
        # hotels with quality=0.0 so the DP only selects them when they are
        # strictly necessary for budget feasibility.
        #
        # Because the merchant catalog is small (≤18 hotels), the sweep
        # makes at most O(distinct_price_points) calls — bounded and fast.
        # This is purely a FEASIBILITY GUARD; preference optimisation is
        # handled by the LLM-ranked passes above.
        # ----------------------------------------------------------------
        # Pass 3 is only needed for cities with multiple hotels across areas
        # (e.g. Bali). Single-area cities (Bangkok, KL, Singapore) have exactly
        # one hotel each; the cheapest is always the only candidate returned by
        # Pass 1/2. Skipping Pass 3 for single-area cities avoids an extra
        # accommodation call that would exhaust the mock transport sequence in
        # unit tests.
        _is_single_area_city = city.strip().lower() in SINGLE_AREA_CITIES

        if candidates_with_quality and not _is_single_area_city:
            current_min_cents = min(
                int(c["total_cents"]) for c in candidates_with_quality
                if isinstance(c.get("total_cents"), (int, float)) and c["total_cents"] > 0
            )
            sweep_ceiling = current_min_cents - 1
            sweep_iters = 0
            max_sweep_iters = 10  # guard against unexpected large catalogs

            while sweep_ceiling > 0 and sweep_iters < max_sweep_iters:
                sweep_iters += 1
                try:
                    sweep_payload: dict[str, Any] = {
                        "city": city,
                        "checkin": checkin,
                        "checkout": checkout,
                        "adults": adults,
                        "max_cents": sweep_ceiling,
                        "vibe": vibe,
                        "target_areas": None,  # city-wide cheapest — no area filter
                        "avoid_lodging_types": avoid_lodging_types,
                        "prefer_lodging_types": prefer_lodging_types,
                        "dest_country": dest_country,   # #93: country filter, see PASS 1 above
                    }
                    sweep_result = self._call_accommodation(sweep_payload)
                    if sweep_result.get("fit") != "ok":
                        break  # no cheaper hotel exists
                    sweep_prop = sweep_result.get("proposal")
                    if not sweep_prop:
                        break
                    sweep_id = sweep_prop.get("hotel_id")
                    sweep_cents = int(sweep_prop.get("total_cents", 0))
                    if sweep_cents <= 0:
                        break
                    if sweep_id not in known_ids:
                        candidates_with_quality.append({**sweep_prop, "quality": 0.0})
                        known_ids.add(sweep_id)
                        logger.info(
                            "orchestrator: DP gather CHEAP-anchor leg=%s iter=%d "
                            "→ %s total=%d¢ (cheaper than prev_min=%d¢)",
                            leg_id, sweep_iters, sweep_id, sweep_cents, current_min_cents,
                        )
                    # Narrow the ceiling to find the next cheaper hotel
                    current_min_cents = min(current_min_cents, sweep_cents)
                    sweep_ceiling = current_min_cents - 1
                except Exception as exc:
                    logger.warning(
                        "orchestrator: DP gather CHEAP-anchor leg=%s iter=%d failed: %s",
                        leg_id, sweep_iters, exc,
                    )
                    break

            if sweep_iters > 0:
                logger.info(
                    "orchestrator: DP gather CHEAP-anchor leg=%s sweep_iters=%d "
                    "final_min=%d¢",
                    leg_id, sweep_iters, current_min_cents,
                )

        logger.info(
            "orchestrator: DP candidates leg=%s city=%s candidates=%d "
            "ranking_source=%s areas=%s",
            leg_id, city, len(candidates_with_quality), ranking_source,
            [c.get("area") for c in candidates_with_quality],
        )
        return candidates_with_quality

    # ------------------------------------------------------------------
    # Accommodation proposal with deterministic area-broaden ladder
    # (used in re-plan rounds — same as before)
    # ------------------------------------------------------------------

    def _propose_with_area_ladder(
        self,
        *,
        leg_meta: dict[str, dict],
        target_areas: dict[str, list[str]],
        area_stage: dict[str, int],
        leg_id: str,
        max_cents: int,
    ) -> dict:
        """
        Propose accommodation for one leg, applying the DETERMINISTIC area-broaden
        ladder on no_fit (NO LLM in this loop — §10.11):

            stage 0  vibe-narrow target areas (from Destination)
            stage 1  vibe-BROAD areas (deterministic superset, still vibe-appropriate)
            stage 2  city-wide areas (last resort within the city)

        The ladder only advances on no_fit; it never reaches an LLM and never
        leaves the city.  Updates target_areas[leg_id] / area_stage[leg_id] in
        place so subsequent rounds reuse the broadened set.

        Returns the accommodation result dict (fit ok|no_fit).
        """
        lm = leg_meta[leg_id]
        city = lm.get("city", "")
        vibe = lm.get("vibe")

        while True:
            acc_payload = {
                "city": city,
                "checkin": lm.get("checkin", ""),
                "checkout": lm.get("checkout", ""),
                "adults": lm.get("adults", 1),
                "max_cents": max_cents,
                "vibe": vibe,
                "target_areas": target_areas.get(leg_id) or None,
                "avoid_lodging_types": lm.get("avoid_lodging_types"),
                "prefer_lodging_types": lm.get("prefer_lodging_types"),
                # #93: dest_country backfilled onto leg_meta by
                # _enrich_leg_meta_dest_country (see negotiate()'s DP/greedy
                # paths) — country filter, mirrors _gather_candidates_for_dp.
                "dest_country": lm.get("dest_country"),
            }
            try:
                self._tracer("agent_started", "Accommodation", trip_id=self._trip_id,
                             summary=f"leg={leg_id} city={city} searching...")
            except Exception:  # noqa: BLE001
                pass
            # L3 — this call is used for the initial greedy proposal AND every
            # veto/critic-reject re-plan round (all pre-commit). An unguarded
            # failure here previously propagated out of the whole negotiation
            # as a raw server_error; mirror the DP gather path's guard
            # (_gather_candidates_for_dp above) and degrade to an honest
            # no_fit/search_error instead, letting the existing no_fit
            # handling (ALL-OR-NONE / re-plan-exhausted cannot_satisfy)
            # produce the honest terminal.
            try:
                acc_result = self._call_accommodation(acc_payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "orchestrator: accommodation search failed (re-plan ladder) "
                    "leg=%s: %s — degrading to honest no_fit.", leg_id, exc,
                )
                return {"fit": "no_fit", "reason_code": "search_error"}
            if acc_result.get("fit") == "ok":
                # Tracer: accommodation agent_completed (side-channel only)
                try:
                    proposal = acc_result.get("proposal") or {}
                    self._tracer(
                        "agent_completed",
                        "Accommodation",
                        trip_id=self._trip_id,
                        summary=f"leg={leg_id} fit=ok hotel={proposal.get('hotel_id','?')} total={proposal.get('total_cents',0)}¢",
                        data={"leg_id": leg_id, "fit": "ok",
                              "hotel_id": proposal.get("hotel_id"),
                              "title": proposal.get("title"),
                              "star_rating": proposal.get("star_rating"),
                              "review_score": proposal.get("review_score"),
                              "total_cents": proposal.get("total_cents", 0),
                              "area": proposal.get("area")},
                    )
                except Exception:  # noqa: BLE001
                    pass
                return acc_result

            # no_fit → try to broaden the area set deterministically.
            stage = area_stage.get(leg_id, 0)
            if stage >= 2:
                return acc_result  # already city-wide; genuinely no_fit

            cur = target_areas.get(leg_id) or []
            if stage == 0:
                broadened = broaden_areas(city, vibe, cur)
            else:  # stage == 1 → go city-wide
                broadened = city_wide_areas(city)

            area_stage[leg_id] = stage + 1

            # If broadening did not actually grow the set (e.g. single-area city),
            # there is nothing more to try — return the no_fit.
            if set(broadened) <= set(cur):
                return acc_result

            target_areas[leg_id] = broadened
            logger.info(
                "orchestrator: area-broaden leg=%s stage %d→%d areas=%s (max=%d¢)",
                leg_id, stage, stage + 1, broadened, max_cents,
            )
            # loop and retry with the broadened set

    # ------------------------------------------------------------------
    # Negotiation entry point
    # ------------------------------------------------------------------

    def negotiate(self, trip_request: dict, *, commit: bool = True) -> dict:
        """
        Run the full multi-agent negotiation for a trip request.

        `commit` (#1 consent split): True (default) = today's ATOMIC behavior —
        plan AND book in one shot, byte-identical to before. False = PLAN-ONLY:
        run every gate + the merchant CHECK (create_checkout, funds HELD) but
        STOP before the irreversible COMMIT — return a `plan_ready` held envelope
        (no booking_ref, wallet not debited) carrying an idempotency_key the client
        later POSTs to /confirm. var-0: `commit=True` callers are unaffected; the
        plan-only path is off the `_request_digest` keystone.

        trip_request shape:
            {
                "user_id":            str,
                "total_budget_cents": int,
                "legs": [
                    {"city": str, "checkin": str, "checkout": str,
                     "adults": int, "vibe": str (optional)}
                ],
                "preferences": dict (optional),
                "user_policy": dict (optional, #45) — the TRAVELER's OWN declared
                    insurance policy {policy_label, currency:"USD", declarations[],
                    global_exclusions[]}. OPT-IN: when ABSENT, the coverage-gap
                    analyzer NEVER runs and the result is byte-identical to today.
            }

        Returns either:
            {"outcome": "success", ...} on convergence + booking, or
            {"outcome": "cannot_satisfy", ...} on honest terminal failure.
        """
        user_id: str = trip_request.get("user_id", "")
        # C1 — the AUTHORITATIVE trip-row owner for persistence/authorization,
        # DISTINCT from `user_id` above (which may be a server-minted anon uuid4 —
        # see server.py's /negotiate and /negotiate_text). Set by the server
        # boundary from the RAW body BEFORE that uuid4 stamp (mirrors
        # merchant_user_id immediately below); "" means genuinely anonymous, and
        # MUST stay "" all the way into the stored row (a random per-request
        # uuid4 can never have a session, so persisting it as row['user_id']
        # would make _authorize_trip_action treat the trip as Tier-1 logged-in
        # and demand a session that can never exist — permanently locking out
        # the trip's own Tier-2 owner_token). Falls back to `user_id` for
        # direct/test callers that never set it (byte-identical to today).
        real_user_id: str = trip_request.get("real_user_id", trip_request.get("user_id", "")) or ""
        # #161 — canonical Go-merchant checkout owner. Rides along on trip_request
        # (like wallet_balance_cents/live_emergency) — NOT part of _request_digest,
        # so it never perturbs var-0. Set by the server boundary (utils.ucp_signing.
        # merchant_checkout_owner, mirroring _authorize_trip_action's tier decision)
        # BEFORE this trip_request is built; falls back to the internal `user_id`
        # for direct/test callers that never set it (byte-identical to today).
        self._merchant_user_id = (
            str(trip_request.get("merchant_user_id") or "").strip() or user_id
        )
        # M1 follow-up — see this attribute's __init__ docstring. `in` (not
        # `.get(...) or`) is deliberate: a server-computed "" (unverified tier-1
        # claim, deny) must be preserved verbatim, never coerced back to a
        # fallback value.
        self._memory_user_id = (
            trip_request["memory_verified_user_id"]
            if "memory_verified_user_id" in trip_request
            else None
        )
        total_budget_cents: int = int(trip_request.get("total_budget_cents", 0))
        # Internal RANDOM id — tracer side-channel / live-stream uniqueness ONLY.
        # It is NEVER surfaced in the returned result (keeps each live board run
        # distinct without leaking non-determinism into the var-0 output).
        trip_id = str(uuid.uuid4())
        self._trip_id = trip_id  # expose for tracer emit helpers
        # #4 — the RESULT-surfaced trip id is a DETERMINISTIC digest of the request
        # so declined/invalid outputs are byte-identical across reruns. #3 — the
        # idempotency_key defaults to the SAME stable digest (honoring an explicit
        # client-supplied key when present), so an identical re-POST after a lost
        # response reuses the merchant session instead of double-booking.
        surfaced_trip_id = f"trip-{_request_digest(trip_request)}"
        idempotency_key = str(trip_request.get("idempotency_key") or surfaced_trip_id)
        # SIMULATED prepaid wallet — freeze the seed + the per-run session id (the
        # deterministic idempotency_key, the var-0 keystone). Absent wallet_balance_cents
        # → DEMO default (direct callers stay funded without changing the var-0 digest).
        self._wallet_balance_cents = int(
            trip_request.get("wallet_balance_cents") or DEMO_WALLET_DEFAULT_CENTS
        )
        self._wallet_session_id = idempotency_key
        # Circle Agentic Economy Prize: REAL (not simulated) USDC settlement
        # opt-in — deliberately NOT part of _request_digest (like owner_token/
        # live_emergency): a payment-RAIL choice, not booking content, so it
        # must never perturb the deterministic trip_id/idempotency_key.
        self._settlement_rail = str(trip_request.get("settlement_rail") or "")
        # D7 — capture ONE frozen `today` at negotiate() entry and thread the SAME
        # value into every health/compliance/risk call for the WHOLE run. NO
        # wall-clock read here: the value is taken verbatim from trip_request (or
        # None when the caller omits it). Threading one value keeps the run
        # internally consistent and byte-identical across reruns; the determinism-
        # critical health gate RAISES on absence (caught → conservative block)
        # rather than silently clocking off date.today().
        self._today = trip_request.get("today")
        # #45 — OPT-IN coverage-gap user policy. When the request omits `user_policy`
        # this stays None and the coverage-gap analyzer is never invoked (the result
        # is byte-identical to today). No vendor catalog logic is affected.
        self._user_policy = trip_request.get("user_policy")
        # Insurance affiliate seam: capture nationality for the PROD insurance deeplink builder.
        # Display-only overlay — OFF the var-0 digest (nationality is already in
        # the deterministic request via _request_digest). UAT path is unaffected.
        self._nationality: str | None = (trip_request.get("nationality") or "").upper() or None
        # #1 CONSENT SPLIT — per-run plan-only flag. True → STOP before the
        # irreversible COMMIT (return a HELD plan_ready envelope). Default (commit=
        # True) keeps the atomic plan+book path byte-identical. OFF the var-0 digest.
        self._plan_only = not commit
        # #44 — OPT-IN late-night supper. When the request omits `supper` this stays
        # None and _maybe_order_supper is a no-op (the result is byte-identical to
        # today). A `supper` dict opts in; its `order:true` flag is the explicit
        # fresh consent for the SECOND prepaid checkout (see _maybe_order_supper).
        self._supper_request = trip_request.get("supper")
        # #32 — OPT-IN LIVE dining reviews. When the request omits `dining` this
        # stays None and _maybe_enrich_dining is a no-op (result byte-identical to
        # today). A `dining` dict opts in; the enrichment fires only when
        # dining.reviews is truthy AND a cuisine pref is given (see
        # _maybe_enrich_dining). NEVER touches the deterministic core.
        self._dining_request = trip_request.get("dining")
        # #51 — OPT-IN LIVE active-emergency overlay. Omitted → None →
        # _maybe_check_active_emergencies is a no-op (byte-identical, var-0). A
        # `live_emergency` dict (with `check` truthy) opts in; escalation lands in
        # result['active_emergencies'] only, NEVER on the deterministic risk path.
        self._emergency_request = trip_request.get("live_emergency")
        self._unverified_lodging = {}  # reset the per-run honesty side-channel (hotel_id -> note)
        # #3 itinerary narrative: default-ON when a narrator_client is wired (LLM-ON); OFF if the
        # request explicitly sets narrate=False. Still gated on self._narrator_client → LLM-OFF has
        # no client and the hook is a no-op → var-0 byte-identical with LLM-OFF unchanged.
        self._narrate_request = trip_request.get("narrate", True)

        # Robustness guard: invalid top-level request → graceful terminal outcome,
        # never an unhandled crash to the caller (found via adversarial stress).
        _legs = trip_request.get("legs")
        if not isinstance(_legs, list) or len(_legs) == 0:
            return {"outcome": "cannot_satisfy", "reason": "invalid_request",
                    "detail": "trip has no legs — nothing to plan", "trip_id": surfaced_trip_id}
        if total_budget_cents <= 0:
            return {"outcome": "cannot_satisfy", "reason": "invalid_request",
                    "detail": f"total_budget_cents must be > 0 (got {total_budget_cents})",
                    "trip_id": surfaced_trip_id}
        # Date-crash cluster (CRITICAL, adversarial finding): a same-day or
        # inverted checkin/checkout leg used to escape all the way down to
        # planner_agent._nights_between, which correctly raises ValueError
        # ("checkout must be strictly after checkin") — but that raise turned
        # into an A2A task state='failed', which _extract_task_data turned into
        # an uncaught RuntimeError, which propagated out of negotiate() as an
        # unhandled crash instead of an honest decline. Validate every leg's
        # dates HERE, before any planner call, using the exact same semantics
        # as _nights_between (checkout strictly after checkin) so this guard
        # and the deep validation always agree. Malformed/missing date strings
        # are just as fail-conservative: decline, never crash.
        for _leg_idx, _leg in enumerate(_legs):
            if not isinstance(_leg, dict):
                return {"outcome": "cannot_satisfy", "reason": "invalid_request",
                        "detail": f"leg {_leg_idx} is not a valid leg object",
                        "trip_id": surfaced_trip_id}
            _checkin = _leg.get("checkin")
            _checkout = _leg.get("checkout")
            try:
                _ci = date.fromisoformat(_checkin)
                _co = date.fromisoformat(_checkout)
            except (TypeError, ValueError) as _exc:
                return {"outcome": "cannot_satisfy", "reason": "invalid_request",
                        "detail": f"leg {_leg_idx} ({_leg.get('city', '?')}) has malformed "
                                  f"checkin/checkout — checkin={_checkin!r} "
                                  f"checkout={_checkout!r} ({_exc})",
                        "trip_id": surfaced_trip_id}
            if _co <= _ci:
                return {"outcome": "cannot_satisfy", "reason": "invalid_request",
                        "detail": f"leg {_leg_idx} ({_leg.get('city', '?')}) has an invalid date "
                                  f"range — checkout ({_co.isoformat()}) must be strictly after "
                                  f"checkin ({_ci.isoformat()})",
                        "trip_id": surfaced_trip_id}

        # ------------------------------------------------------------------
        # R0-decline: DO-NOT-RECOMMEND armed-conflict gate (EARLY TERMINAL).
        # Fires BEFORE any Destination/Risk/booking/pricing work. A leg whose
        # dest_country is in contracts.DO_NOT_RECOMMEND_COUNTRIES is an active
        # armed conflict → UNINSURABLE under EXC-WAR-2 → the society DECLINES
        # (HONESTY / fail-conservative). This is distinct from an ordinary L3/L4
        # advisory FLAG (which stays bookable, e.g. Russia/Moscow).
        #
        # CRITICAL: decline on SET MEMBERSHIP, not on missing inventory — a set
        # member (e.g. Ukraine, now 275 catalog hotels) must DECLINE even though
        # hotels exist; a naive "no inventory" path would NOT catch it.
        #
        # SELF-CONTAINED / fail-conservative on the PUBLIC entry point: the gate
        # must not depend on a caller supplying dest_country. When a leg omits (or
        # leaves empty) dest_country, RE-DERIVE the country from the leg's city via
        # the SAME map the parser uses (intent_parser.CITY_TO_ISO2), so a DIRECT
        # negotiate() call with a set-member city (e.g. 'kyiv') but no dest_country
        # STILL declines instead of booking (the audit MEDIUM: BK-LEAK). An
        # unresolved city yields no code → no FALSE decline (fail-conservative both
        # ways: a set-member city declines, an unknown city does not).
        #
        # var-0: membership is decided by contracts.is_do_not_recommend_country()
        # (THE canonical predicate — no inlined parallel check), on the upper-cased
        # ISO2 code; the blocked-country list in the message is sorted() for a
        # deterministic string.
        #
        # 2026-07 adversarial audit fix #1 — ISO3/full-name bypass: dest_country
        # was matched EXACTLY against the ISO2 set, so 'AFG' or 'Afghanistan'
        # (instead of 'AF') skipped the decline entirely (Kabul failed closed only
        # "by accident" — no catalog inventory; a covered country WITH inventory,
        # e.g. Ukraine, could plausibly have booked). Fix: normalize every
        # dest_country reference to ISO2 via intent_parser.normalize_country_to_iso2
        # — THE canonical country-reference normalizer (ISO2/ISO3/full-name → ISO2,
        # reused rather than re-derived here) — BEFORE the membership check.
        declined_countries: list[str] = []
        for leg in _legs:
            if not isinstance(leg, dict):
                continue
            # C4 — SELF-CONTAINED / no short-circuit: decline if EITHER the
            # caller-supplied dest_country OR the city-resolved CITY_TO_ISO2 country
            # is a DO_NOT_RECOMMEND member (OR the two). We must NOT skip the
            # city-resolution path just because a dest_country is present — a caller
            # could supply a bookable dest_country while naming a set-member city
            # (e.g. 'kyiv'), and that trip must STILL decline. Both candidate codes
            # are checked through the canonical predicate.
            candidates: list[str] = []
            dc_raw = leg.get("dest_country")
            dc = normalize_country_to_iso2(dc_raw) if isinstance(dc_raw, str) and dc_raw.strip() else ""
            if dc:
                candidates.append(dc)
            city = leg.get("city")
            city_key = city.strip().lower() if isinstance(city, str) and city.strip() else ""
            if city_key:
                city_dc = CITY_TO_ISO2.get(city_key)
                if city_dc:
                    candidates.append(city_dc)
                # 2026-07 adversarial audit fix #2 — ambiguous city-name collision:
                # a bare city name that names BOTH a bookable city AND a (same-
                # named) city in a DO_NOT_RECOMMEND country (e.g. "tripoli" is
                # Tripoli, Lebanon [catalog] AND Tripoli, Libya [DO_NOT_RECOMMEND,
                # no catalog inventory — so CITY_TO_ISO2, which is catalog-only by
                # construction, can NEVER surface the Libya side]) must NOT
                # silently resolve to the permissive country. Conservative rule:
                # a BARE city reference (no explicit dest_country to disambiguate)
                # is treated as potentially naming the DO_NOT_RECOMMEND side too —
                # decline rather than silently pick the bookable one. An explicit
                # dest_country on the leg counts as disambiguation (the caller
                # named a specific country) and skips this conservative add.
                if not dc:
                    candidates.extend(_AMBIGUOUS_CITY_CONFLICT_COUNTRIES.get(city_key, ()))
            for cand in candidates:
                if is_do_not_recommend_country(cand):
                    declined_countries.append(normalize_country_to_iso2(cand))
        if declined_countries:
            return self._do_not_recommend_block_result(
                declined_countries=sorted(set(declined_countries)),
                trip_id=surfaced_trip_id,
            )

        # Tracer: negotiate_started (side-channel only — no result dict mutation)
        try:
            # Per-leg stubs so the live board can seed per-leg cards with the
            # SAME canonical leg_id ("leg-{i}") the orchestrator uses downstream.
            leg_stubs = []
            for i, leg in enumerate(_legs):
                if not isinstance(leg, dict):
                    continue
                leg_stubs.append({
                    "leg_id": f"leg-{i}",
                    "city": leg.get("city", ""),
                    "checkin": leg.get("checkin", ""),
                    "checkout": leg.get("checkout", ""),
                })
            self._tracer(
                "negotiate_started",
                trip_id=self._trip_id,
                summary=f"user={user_id} budget={total_budget_cents}¢ legs={len(_legs)}",
                data={"user_id": user_id, "total_budget_cents": total_budget_cents,
                      "leg_count": len(_legs), "legs": leg_stubs},
            )
        except Exception:  # noqa: BLE001
            pass

        # SIMULATED prepaid wallet — create-OR-RESET the per-run wallet, then emit a
        # side-channel `seed` trace event (var-0-exempt by contract). Placed AFTER
        # negotiate_started so it never displaces the first event. NO-OP when no
        # Budget agent is configured; emit is fully try/except-guarded so a tracer or
        # funding bug can NEVER crash the negotiation or alter the var-0 result.
        _fund_result = self._fund_wallet()
        try:
            _seed_balance = self._wallet_balance_cents
            if isinstance(_fund_result, dict) and _fund_result.get("balance_cents") is not None:
                _seed_balance = _fund_result.get("balance_cents")
            self._tracer(
                "wallet", "Wallet", trip_id=self._trip_id,
                summary=f"wallet seeded ${self._wallet_balance_cents / 100:,.2f}",
                data={
                    "op": "seed",
                    "seed_cents": self._wallet_balance_cents,
                    "balance_cents": _seed_balance,
                    "wallet_session_id": self._wallet_session_id,
                    "simulated": True,
                },
            )
        except Exception:  # noqa: BLE001
            pass

        # #159 IP-2 — OPT-IN LIVE preference-affinity READ (genuine MCP call_tool
        # 'resolve_geographic_scope'). Computed EARLY (before any booking work) so a
        # slow/dead memory server can't delay the trip; attached LATE (only on a
        # success outcome, see below) so nothing on the money path ever sees it. A
        # no-op unless a memory client is configured; NEVER raises.
        # M1 (IDOR): pass the session-verified self._merchant_user_id (already set
        # above, mirrors #161), NOT the raw untrusted `user_id` — travel_memory's
        # resolve_geographic_scope tool does no ownership check of its own, so the
        # orchestrator is the only place this can be enforced.
        # M1 follow-up (security review) — self._merchant_user_id is only TIER-mirrored,
        # not session-verified, at plan-creation time (see self._memory_user_id's
        # __init__ docstring). Prefer the properly-gated self._memory_user_id when
        # the server boundary computed one; direct/test callers that never set it
        # fall back to self._merchant_user_id, byte-identical to before.
        _memory_id = (
            self._memory_user_id if self._memory_user_id is not None
            else self._merchant_user_id
        )
        self._personalization = self._maybe_read_affinity(_memory_id, _legs)

        negotiation_log: list[dict[str, Any]] = []
        # Effective spend ceiling — updated from first veto's budget_ceiling_cents
        # (the merchant may enforce a lower ceiling than the trip's total_budget_cents).
        effective_ceiling: int = total_budget_cents

        logger.info(
            "orchestrator.negotiate: start trip=%s user=%s budget=%d¢ dp=%s",
            trip_id, user_id, total_budget_cents, _USE_DP_ALLOCATOR,
        )

        # ------------------------------------------------------------------
        # R0a: Destination.assess per leg → target areas (ONE-SHOT, pre-loop).
        # §10.11: the vibe→area LLM edge runs once per leg, NEVER inside the
        # re-plan loop.
        # ------------------------------------------------------------------
        target_areas: dict[str, list[str]] = {}
        area_stage: dict[str, int] = {}
        legs_input: list[dict] = trip_request.get("legs", [])
        # Expose the ORIGINAL trip_request legs (index-aligned with leg-{i}) for the
        # day-planner block in _run_negotiation_rounds, whose in-scope `legs` is the
        # planner skeleton and does NOT carry interests/dietary/pace/dest_country.
        # Same per-negotiation self-state pattern as self._trip_id / self._today
        # (negotiate() is serialized under the server's orch_lock).
        self._trip_request_legs = legs_input
        for i, leg in enumerate(legs_input):
            leg_id = f"leg-{i}"
            try:
                self._tracer("agent_started", "Destination", trip_id=self._trip_id,
                             summary=f"leg={leg_id} city={leg.get('city','')}...")
            except Exception:  # noqa: BLE001
                pass
            areas = self._call_destination(
                leg.get("city", ""), leg.get("vibe"),
                leg.get("checkin"), leg.get("checkout"),
            )
            target_areas[leg_id] = areas
            area_stage[leg_id] = 0
            logger.info(
                "orchestrator: destination.assess leg=%s city=%s vibe=%s → areas=%s",
                leg_id, leg.get("city"), leg.get("vibe"), areas,
            )
            # Tracer: destination agent_completed (side-channel only, after call returns)
            try:
                self._tracer(
                    "agent_completed",
                    "Destination",
                    trip_id=self._trip_id,
                    summary=f"leg={leg_id} city={leg.get('city','')} areas={len(areas)}",
                    data={"leg_id": leg_id, "city": leg.get("city", ""),
                          "areas": areas, "vibe": leg.get("vibe")},
                )
            except Exception:  # noqa: BLE001
                pass

        # ------------------------------------------------------------------
        # R0a-risk: Risk.assess (ONE-SHOT, pre-loop) — L1 proactive signals.
        # ADDITIVE + OFF the money path: the result carries per-leg risk signals
        # + an avoid/buffer/flag roll-up the Planner consumes. Bypassed (None)
        # when no Risk agent is wired → S1–S5 are byte-identical (no new key).
        # ------------------------------------------------------------------
        risk_assessment = self._assess_risk_signals(legs_input)

        # #region-fix (2026-07 adversarial audit): expose leg_id -> Risk's
        # ALREADY-RESOLVED region (risk_agent.region_for_city) for the
        # day-planner payload block in _run_negotiation_rounds, whose in-scope
        # `legs`/`leg_meta` is the Planner skeleton and carries no region key.
        # Same per-negotiation self-state pattern as self._trip_request_legs
        # above (negotiate() is serialized under the server's orch_lock).
        # Without this, day_planner_agent.build_day_plan's bad-weather
        # contingency (derive_bad_weather_days(region, ...)) NEVER receives a
        # region on any real call — it always silently no-ops, despite passing
        # its own unit tests (which supply region directly).
        self._risk_region_by_leg: dict[str, str | None] = {
            pleg.get("leg_id"): pleg.get("region")
            for pleg in (risk_assessment.get("per_leg", []) if isinstance(risk_assessment, dict) else [])
            if isinstance(pleg, dict)
        }

        # ------------------------------------------------------------------
        # R0b-gates: CONTEXTUAL eligibility GATES (Health + Compliance), ONE-SHOT
        # pre-loop, EARLY (a precondition on the whole trip — an unbookable-in-time
        # visa/entry-cert makes the itinerary invalid regardless of price).
        #
        # CONTEXTUAL FIRING (the core invariant): each gate fires ONLY when its
        # condition is present in the trip_request (nationality + dest_country →
        # Compliance; place_key/dest_country → Health). A simple Bali/WA trip with
        # NO such field PASSES THROUGH (gate returns None) → S1–S5 byte-identical.
        #
        # Sequencing: Health runs FIRST so its jab_records feed the §7 YF-cert
        # interlock (Compliance reads the jab record for the entry certificate).
        # A cannot_complete / cannot_satisfy from EITHER gate BLOCKS the trip — the
        # society never returns an unbookable-in-time itinerary.
        # ------------------------------------------------------------------
        gate_fees: list[dict] = []  # FEE-INJECTION accumulator (visa/vaccine/premium)

        health_verdict = self._run_health_gate(trip_request, legs_input)
        if health_verdict is not None:
            # #70 budget scope: only ENTRY-REQUIRED vaccines the traveler does NOT already hold
            # are ENFORCED in the budget (mandatory_line_items). Recommended/situational + held
            # vaccines are surfaced separately (result["optional_health_estimate"]), NEVER in the
            # enforced total. (Full slate stays in health_verdict["line_items"] for display.)
            gate_fees.extend(health_verdict.get("mandatory_line_items") or [])
            # D1 — conservative default: a verdict that omits `bookable` cannot prove
            # bookability → treat as NOT bookable (block), never fail open.
            if not health_verdict.get("bookable", False):
                return self._gate_block_result(
                    gate="health",
                    verdict=health_verdict,
                    negotiation_log=negotiation_log,
                    gate_fees=gate_fees,
                    risk_assessment=risk_assessment,
                )

        compliance_verdict = self._run_compliance_gate(
            trip_request, legs_input, health_verdict
        )
        if compliance_verdict is not None:
            gate_fees.extend(compliance_verdict.get("line_items") or [])
            # D1 — conservative default: a verdict that omits `bookable` cannot prove
            # bookability → treat as NOT bookable (block), never fail open.
            if not compliance_verdict.get("bookable", False):
                return self._gate_block_result(
                    gate="compliance",
                    verdict=compliance_verdict,
                    negotiation_log=negotiation_log,
                    gate_fees=gate_fees,
                    risk_assessment=risk_assessment,
                )

        # ------------------------------------------------------------------
        # R0c-fraud: FRAUD advisory pre-commit GATE #1 (counterparty solvency).
        # CONTEXTUAL: fires ONLY when the trip_request carries counterparties /
        # carriers (the DC-fraud shape). Filters the committable set BEFORE the
        # money path; a blocked/unknown counterparty is never committable without
        # explicit fresh consent. The Critic re-checks the SAME seeded band at
        # commit time (the live gate #2). PASS-THROUGH (None) for S1–S5.
        # ------------------------------------------------------------------
        fraud_verdict = self._run_fraud_gate(trip_request)
        if fraud_verdict is not None:
            rollup = fraud_verdict.get("rollup", {})
            # D1 — conservative default: a rollup that omits `all_committable` cannot
            # prove every counterparty is committable → treat as NOT committable
            # (block), never fail open on an unverified/insolvent supplier.
            if not rollup.get("all_committable", False):
                return self._fraud_block_result(
                    fraud_verdict=fraud_verdict,
                    negotiation_log=negotiation_log,
                    gate_fees=gate_fees,
                    risk_assessment=risk_assessment,
                )

        # #72 INSURANCE HARD-VETO (all-or-none, PRE-commit): reserve the KNOWN enforced fees
        # (visa + entry-required-not-held vaccines = gate_fees) + an UPPER-BOUND insurance
        # premium from the lodging budget, so the merchant 403 rejects a trip that would bust
        # budget once fees+premium are added — BEFORE any booking commits (never strand a
        # reservation). Recommended/held vaccines are NOT in gate_fees → correctly excluded.
        # The premium estimate is an upper bound (priced at the budget), so the FINAL actual
        # total is always <= budget. Envelope 0 (no fees, no peril) → lodging_budget_cents ==
        # total_budget_cents → byte-identical (var-0).
        gate_fee_total = sum(
            int((li.get("money") or {}).get("usd_cents", 0) or 0) for li in gate_fees
        )
        premium_est = self._estimate_insurance_premium_cents(risk_assessment, total_budget_cents)
        enforced_envelope = gate_fee_total + premium_est
        lodging_budget_cents = max(1, total_budget_cents - enforced_envelope)
        if enforced_envelope:
            logger.info(
                "orchestrator: #72 envelope reserve — gate_fees=%d¢ premium_est=%d¢ → "
                "lodging_budget=%d¢ (user budget=%d¢)",
                gate_fee_total, premium_est, lodging_budget_cents, total_budget_cents,
            )

        if _USE_DP_ALLOCATOR:
            result = self._negotiate_dp(
                trip_request=trip_request,
                user_id=user_id,
                total_budget_cents=total_budget_cents,
                effective_ceiling=effective_ceiling,
                idempotency_key=idempotency_key,
                negotiation_log=negotiation_log,
                target_areas=target_areas,
                area_stage=area_stage,
                risk_assessment=risk_assessment,
                gate_fees=gate_fees,
                lodging_budget_cents=lodging_budget_cents,
            )
        else:
            result = self._negotiate_greedy(
                trip_request=trip_request,
                user_id=user_id,
                total_budget_cents=total_budget_cents,
                effective_ceiling=effective_ceiling,
                idempotency_key=idempotency_key,
                negotiation_log=negotiation_log,
                target_areas=target_areas,
                area_stage=area_stage,
                risk_assessment=risk_assessment,
                gate_fees=gate_fees,
                lodging_budget_cents=lodging_budget_cents,
            )

        # Surface the non-blocking gate verdicts + interlock evidence + injected
        # fees on the booking path too (only when a gate actually fired → no key
        # added for the no-condition S1–S5 trips, var-0 preserved).
        if isinstance(result, dict):
            if health_verdict is not None:
                result["health_verdict"] = health_verdict
                _ohe = self._build_optional_health_estimate(health_verdict, total_budget_cents)
                if _ohe:
                    result["optional_health_estimate"] = _ohe
            if compliance_verdict is not None:
                result["compliance_verdict"] = compliance_verdict
                if compliance_verdict.get("entry_cert_handoff"):
                    result["entry_cert_handoff"] = compliance_verdict["entry_cert_handoff"]
            if fraud_verdict is not None:
                result["fraud_verdict"] = fraud_verdict
            if gate_fees:
                self._inject_fees(result, gate_fees)
            # TOP-LEVEL surfacing of unverified eligibility/health flags. A flag
            # buried only inside *_verdict is effectively a silent pass for any
            # UI that renders top-level fields — so lift the advisory texts to a
            # single result["advisories"] list the front end must show.
            # #51/BUG8 — SEED from any advisories _success_result already set (e.g.
            # the day-planner-degraded note) rather than starting from an empty
            # list: this block's final `result["advisories"] = advisories` is a
            # REPLACE, not a merge, so starting empty would silently drop a
            # caveat that was already there whenever any of compliance/health/
            # risk/day-plan-notes also fire.
            advisories: list[str] = list(result.get("advisories") or [])
            if isinstance(compliance_verdict, dict):
                for leg in compliance_verdict.get("flagged_legs", []) or []:
                    adv = (leg or {}).get("flag_advisory")
                    if adv:
                        advisories.append(adv)
            if isinstance(health_verdict, dict):
                for d in health_verdict.get("flagged_destinations", []) or []:
                    adv = (d or {}).get("flag_advisory")
                    if adv:
                        advisories.append(adv)
            # D2 — SURFACE Risk's per-leg advisory texts + a top-level risk_flagged
            # bool alongside route/legs, so a Risk hazard flag is never buried only
            # inside risk_signals. CONTEXTUAL: only fires when a Risk agent ran and
            # produced flags (no key added for the no-Risk S1–S5 trips → var-0).
            if isinstance(risk_assessment, dict):
                rk_rollup = risk_assessment.get("rollup", {}) or {}
                rk_flagged_legs = rk_rollup.get("flagged_legs", []) or []
                result["risk_flagged"] = bool(rk_flagged_legs)
                # Iterate per_leg in its emitted (deterministic) list order — no
                # set/dict ordering drives the output.
                for pleg in risk_assessment.get("per_leg", []) or []:
                    if not isinstance(pleg, dict):
                        continue
                    for adv in pleg.get("advisory", []) or []:
                        detail = (adv or {}).get("detail")
                        if detail:
                            advisories.append(detail)
            # M5 — a WIRED-BUT-FAILED Risk call is otherwise indistinguishable
            # from "no Risk agent configured": no hazard advisories, no
            # risk_flagged, AND Insurance (which derives its perils solely from
            # the Risk rollup) silently never fires. Unlike Health/Compliance
            # (which fail CONSERVATIVE-BLOCK on their own call failure), Risk
            # stays advisory/non-fatal — it must never block the booking — but
            # the degradation itself must not be silent, since a real-money
            # line (insurance) is riding on it.
            if getattr(self, "_risk_degraded", False):
                result["risk_assessment_degraded"] = True
                advisories.append(
                    "Hazard/safety assessment could not be completed for this "
                    "trip (the Risk agent was unavailable) — no hazard "
                    "advisories or insurance premium could be computed for "
                    "this run. Please check travel advisories independently."
                )
            # #7 [3] — LIFT the day-planner's per-leg honest notes (dietary-fallback,
            # no-restaurant, low-attraction-variety, supper-unavailable) into the
            # surfaced advisories. The day-planner already flags these gaps in
            # leg.day_plan.notes, but a note buried only there is invisible to any UI
            # rendering top-level advisories — a silent gap. CONTEXTUAL: only legs
            # that produced a note contribute (a fully-covered leg adds nothing), so a
            # trip whose day plans are clean is unchanged; deterministic list order.
            #
            # #B1 — a ZERO POI-CATALOG COVERAGE leg (catalog_hit=False) is handled
            # SEPARATELY below with a dedicated, hotel-aware advisory instead of being
            # lifted verbatim here: the day-planner's own note is honest but scoped to
            # what IT knows (no POI/restaurant data for this city) — it has no
            # visibility into whether a REAL, CHARGED hotel booking exists for the same
            # leg (a separate merchant catalog, keyed differently — the Bali incident:
            # a genuinely empty day-by-day plan silently rode along with a real
            # payment). The orchestrator DOES have that context here, so it crafts the
            # one advisory that matters most for this leg instead of a generic note.
            for plan in result.get("day_plans", []) or []:
                if not isinstance(plan, dict) or self._is_zero_poi_coverage_leg(plan):
                    continue
                for note in plan.get("notes", []) or []:
                    if note:
                        advisories.append(str(note))
            # #B1 fail-closed (not fail-open) honesty guard: a leg whose day-planner
            # lookup was a genuine catalog miss (catalog_hit=False) AND which produced
            # a genuinely EMPTY day-by-day plan (no attractions and no meals at all —
            # what the traveler actually sees) still gets a real, charged hotel booking
            # from the separate merchant catalog. Gating on BOTH conditions (not
            # catalog_hit alone) is deliberate: this codebase's own narration layer
            # documents that a catalog MISS can still carry real sub-area POIs (the
            # Bali case — "catalog_hit is NOT the gate"), so firing on catalog_hit
            # alone would wrongly tell a traveler "we have no activity data" for a leg
            # that actually got a full itinerary. It also stays scoped to genuine
            # zero-coverage MISSES (not a THIN catalog that still partially covers a
            # city — a catalog_hit=True leg never triggers this). Least-disruptive
            # honest fix: this is a data-coverage gap, not a legal/safety blocker (a
            # real, bookable hotel exists and the rest of the trip is valid) — so it
            # surfaces as a clear advisory in THIS SAME response rather than declining
            # the booking outright, the same non-fatal-advisory pattern already used
            # above for risk-degraded, transport-unverified, and tier-mismatch gaps.
            # Deterministic list order (day_plans is already emitted in a stable order).
            # Wording depends on WHICH money-state this response describes: the
            # default atomic path (commit=True, self._plan_only False) has already
            # charged a real hotel booking by the time this response is built (the
            # exact Bali incident shape); the opt-in two-phase CONSENT SPLIT
            # (commit=False) only HOLDS a hotel selection pending a separate
            # /confirm — calling that a "confirmed" booking would itself be
            # dishonest, so it gets the accurate "held" phrasing instead (mirrors
            # the wallet's own "Held ... not yet charged" language above).
            _hotel_state = (
                "a hotel selection is held for you pending confirmation"
                if getattr(self, "_plan_only", False)
                else "you'll get a confirmed hotel booking"
            )
            for plan in result.get("day_plans", []) or []:
                if not isinstance(plan, dict) or not self._is_zero_poi_coverage_leg(plan):
                    continue
                city_label = str(plan.get("city") or "this destination").strip().title() or "this destination"
                advisories.append(
                    f"We don't have detailed day-by-day activity data for {city_label} "
                    f"yet — {_hotel_state}, but no curated itinerary "
                    f"(attractions/restaurants) for this leg. Please research things "
                    f"to do there independently."
                )
            # #51/BUG8 (Split->Vis) — lift the transport UNVERIFIED-edge caveats
            # (e.g. a water crossing with no seeded ferry route) into this SAME
            # top-level advisories list every other warning uses. Previously only
            # result["transport_unverified"] carried this — a key the front end
            # does not render — so a trip could book SUCCESSFULLY with a real
            # booking_ref while its only transfer was internally feasible=False
            # and the user never saw the caveat. CONTEXTUAL: only fires when the
            # Transport agent actually flagged an unverified edge.
            for edge in result.get("transport_unverified", []) or []:
                if not isinstance(edge, dict):
                    continue
                fc = (edge.get("from_city") or "?").title()
                tc = (edge.get("to_city") or "?").title()
                why = edge.get("reason") or "no verified transfer data for this leg"
                advisories.append(
                    f"Transport unverified: {fc} → {tc} — {why} Confirm "
                    f"this transfer is actually possible before relying on this "
                    f"itinerary."
                )
            # #52 item 6b — vibe/budget-tier mismatch disclosure. The user stated a
            # qualitative style tier ("luxury, $500"), but the tier filter's floor is
            # deliberately absent at the extremes (luxury/shoestring carry no
            # _TIER_STAR_MIN — see _filter_candidates_by_tier) precisely so a tight
            # budget is never starved into a false no_fit. That means a "luxury"
            # request whose budget can only afford a 2-star property was previously
            # booked with ZERO disclosure of the divergence. Surface it honestly
            # rather than silently downgrade — CONTEXTUAL: only checks legs when the
            # user actually stated a tier with a real expectation floor; a plain
            # trip (no tier) or a tier with no floor concept (shoestring) is
            # unaffected (var-0).
            _tier = trip_request.get("budget_tier")
            _tier_floor = _TIER_MISMATCH_DISCLOSURE_FLOOR.get(_tier or "")
            if _tier_floor and isinstance(result, dict):
                _mismatch = []
                for _lg in result.get("legs", []) or []:
                    if not isinstance(_lg, dict):
                        continue
                    try:
                        _star = float(_lg.get("star_rating") or 0)
                    except (TypeError, ValueError):
                        _star = 0.0
                    if _star > 0 and _star < _tier_floor:
                        _mismatch.append((_lg.get("city") or _lg.get("leg_id") or "?", _star))
                if _mismatch:
                    _detail = ", ".join(
                        f"{str(c).title()} ({s:g}-star)" for c, s in _mismatch
                    )
                    advisories.append(
                        f"You asked for a '{_tier}' style trip, but the stated "
                        f"budget could only afford: {_detail} — well below what "
                        f"'{_tier}' typically means. Increase your budget or relax "
                        f"the style if this divergence matters to you."
                    )
            if advisories:
                result["advisories"] = advisories

        # ------------------------------------------------------------------
        # R-post: INSURANCE coverage assessment (pre-commit, CONTEXTUAL). Fires
        # ONLY when Risk produced a peril signal (the perils come from Risk via
        # the peril_crosswalk) AND the booking succeeded. Insurance proposes a
        # premium line item; it is surfaced + (FEE-INJECTION) added to the result
        # package view. PASS-THROUGH (None) when no peril signal → S1–S5 unchanged.
        # ------------------------------------------------------------------
        result = self._apply_insurance(result, risk_assessment, total_budget_cents)

        # ADDITIVE: attach the Risk signals to the result (no-op when bypassed).
        result = self._attach_risk_signals(result, risk_assessment)

        # #37 — ADDITIVE: attach the reference/booking HANDOFF links. Pure +
        # deterministic (constructed from result data; NO wall-clock, NO random,
        # NO I/O). ONLY on a successful package — declined/cannot_satisfy paths
        # get NO booking links (a handoff link off a non-bookable plan would be
        # misleading). var-0: links are byte-identical functions of the already-
        # deterministic result.
        if isinstance(result, dict) and result.get("outcome") == "success":
            # Pass self._nationality for the PROD insurance affiliate deeplink (display-only;
            # nationality is already part of the deterministic request digest so
            # threading it here does NOT perturb var-0).
            self._attach_booking_links(result, nationality=getattr(self, "_nationality", None))

        # #159 IP-2 — ADDITIVE display-only personalization hint (genuine MCP
        # resolve_geographic_scope affinity READ, computed earlier so a slow/dead
        # memory server never delays booking). SEPARATE top-level key, never inside
        # day_plans/legs/price — mirrors dining_reviews/active_emergencies. Absent
        # (no key) when no memory client is configured, the city was unresolved, or
        # no preference is on file yet — var-0 sacred, never on a non-success outcome.
        if isinstance(result, dict) and result.get("outcome") == "success" and self._personalization:
            result["personalization"] = self._personalization

        # #101 — ADDITIVE indicative display-currency review. Pure deterministic
        # (seeded FX table; no clock/network/random). home_currency is OFF _request_digest
        # → var-0 holds. NEVER affects the USD veto; display only.
        if isinstance(result, dict) and result.get("outcome") == "success":
            try:
                self._attach_currency_review(result, trip_request)
            except Exception as exc:  # noqa: BLE001 — display-only, off the var-0 path
                logger.warning("orchestrator: _attach_currency_review failed (ignored): %s", exc)

        # #101 — ADDITIVE hotel geocode (city_centroid or cached Nominatim).
        # Attaches hotel_lat/hotel_lon/hotel_coord_basis to each leg for the map pin.
        # Cache-only in the request path (disk read, deterministic). Never mutates
        # day_plans or booking bytes — var-0 preserved.
        if isinstance(result, dict) and result.get("outcome") == "success":
            try:
                self._attach_hotel_geo(result)
            except Exception as exc:  # noqa: BLE001 — map-display only, off the var-0 path
                logger.warning("orchestrator: _attach_hotel_geo failed (ignored): %s", exc)

        # DISPLAY-ONLY price overlay — excluded from digest and day_plans
        if isinstance(result, dict) and result.get("outcome") == "success":
            try:
                self._maybe_attach_price_overlay(result)
            except Exception as exc:  # noqa: BLE001 — display-only overlay, off the var-0 path
                logger.warning("orchestrator: _maybe_attach_price_overlay failed (ignored): %s", exc)

        # #44 — OPT-IN late-night supper on the SAME prepaid UCP rails (2nd KIND).
        # APPEND-ONLY: a no-op unless trip_request supplied `supper` AND the trip
        # booked → S1–S5 (and every non-supper request) stay byte-identical.
        if isinstance(result, dict):
            try:
                self._maybe_order_supper(
                    result, user_id=user_id, idempotency_key=idempotency_key,
                )
            except Exception as exc:  # noqa: BLE001 — supper is opt-in & off the itinerary path
                logger.warning("orchestrator: _maybe_order_supper failed (ignored): %s", exc)

        # #32 — OPT-IN LIVE dining reviews/ratings OVER the deterministic meal
        # plan. APPEND-ONLY: a no-op unless trip_request supplied `dining` (with
        # reviews truthy + a cuisine pref) AND the trip booked → every non-dining
        # request stays byte-identical. NEVER mutates day_plans (var-0 preserved);
        # the live layer lands in a SEPARATE top-level key result['dining_reviews'].
        if isinstance(result, dict):
            try:
                self._maybe_enrich_dining(result)
            except Exception as exc:  # noqa: BLE001 — dining is opt-in & off the itinerary path
                logger.warning("orchestrator: _maybe_enrich_dining failed (ignored): %s", exc)

        # #51 — OPT-IN LIVE active-emergency overlay. APPEND-ONLY: a no-op unless
        # trip_request supplied `live_emergency` (with check truthy). NEVER mutates
        # the deterministic risk path; escalation lands in result['active_emergencies'].
        if isinstance(result, dict):
            try:
                self._maybe_check_active_emergencies(result)
            except Exception as exc:  # noqa: BLE001 — emergency overlay is opt-in & off the var-0 path
                logger.warning("orchestrator: _maybe_check_active_emergencies failed (ignored): %s", exc)

        # #3 — OPT-IN cosmetic Google-style itinerary NARRATIVE. APPEND-ONLY / var-0: a no-op unless
        # trip_request supplied `narrate` AND a narrator is wired AND the trip booked. Generated AFTER
        # the deterministic result is frozen; lands in result['itinerary_narrative']; NEVER touches
        # the booking / day_plans. A failure can never break the trip.
        if isinstance(result, dict):
            try:
                self._maybe_narrate_itinerary(result)
            except Exception as exc:  # noqa: BLE001 — narrative is cosmetic & off the var-0 path
                logger.warning("orchestrator: _maybe_narrate_itinerary failed (ignored): %s", exc)

        # #159 IP-1 — OPT-IN LIVE preference-memory WRITE (genuine MCP call_tool
        # 'log_search' per booked leg). POST-FREEZE side effect (result bytes are
        # already final) — mirrors the #64 telemetry emit firewall. A no-op unless a
        # memory client is configured AND the trip booked. NEVER touches result.
        if isinstance(result, dict):
            try:
                self._maybe_log_search(result, trip_request)
            except Exception as exc:  # noqa: BLE001 — memory is opt-in & off the var-0 path
                logger.warning("orchestrator: _maybe_log_search failed (ignored): %s", exc)

        # Tracer: negotiate_finished (side-channel only — after result is fully built)
        try:
            self._tracer(
                "negotiate_finished",
                trip_id=self._trip_id,
                summary=f"outcome={result.get('outcome', '?')}",
                data={"outcome": result.get("outcome"), "trip_id": self._trip_id},
            )
        except Exception:  # noqa: BLE001
            pass

        # #1 CONSENT SPLIT — a plan-only run built a success-shaped, FULLY post-processed
        # envelope (fees + insurance + booking-links + narrative all present) WITHOUT
        # committing or debiting. Flip it to a HELD plan_ready envelope for the client.
        # The atomic path (commit=True → self._plan_only False) NEVER enters here, so it
        # stays byte-identical (var-0 + every existing negotiate test unaffected).
        if getattr(self, "_plan_only", False) and isinstance(result, dict) \
                and result.get("outcome") == "success":
            result = self._to_plan_ready(
                result, idempotency_key=idempotency_key, user_id=user_id,
                real_user_id=real_user_id)
        elif isinstance(result, dict) and result.get("outcome") == "success" \
                and result.get("booking_ref") and self._atomic_commit_marker is not None:
            # M7 — this is the ATOMIC (commit=True) path's genuine SUCCESS —
            # never persisted to the trips store (only plan_ready results are,
            # via server.py's _persist_and_sanitize_plan). Record a lightweight
            # committed marker so a later identical re-POST's _fund_wallet
            # guard (atomic_committed_lookup, consulted alongside trip_lookup)
            # can detect it and skip re-funding/re-debiting the wallet for what
            # would otherwise be a second live merchant booking. Best-effort —
            # a marker failure is logged, never breaks the booking response.
            try:
                self._atomic_commit_marker(idempotency_key, result.get("booking_ref") or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "orchestrator: atomic_commit_marker failed (ignored) for "
                    "idempotency_key=%s: %s", idempotency_key, exc,
                )

        return result

    # ------------------------------------------------------------------
    # #1 CONSENT SPLIT — plan_ready envelope + /confirm commit
    # ------------------------------------------------------------------

    def _to_plan_ready(self, result: dict, *, idempotency_key: str, user_id: str,
                        real_user_id: str | None = None) -> dict:
        """Convert a success-shaped (but UNCOMMITTED) envelope into a HELD `plan_ready`
        one. Reuses the full success envelope (fees/insurance/links/narrative) for
        field-for-field parity with the eventual booked trip — only the booking-state
        fields differ. Nothing was charged: wallet balance is unchanged, the package
        total is HELD. The private `_confirm_ctx` lets the server persist the
        merchant checkout_id without ever echoing it to the client."""
        held = int(result.get("package_total_with_fees_cents")
                   or result.get("package_total_cents") or 0)
        checkout_id = getattr(self, "_plan_checkout_id", None) or result.get("checkout_id")
        dest_token = getattr(self, "_plan_dest_token", "") or "UNK"
        result["outcome"] = "plan_ready"
        result["payment_status"] = "held"
        result["booking_ref"] = None          # nothing booked yet
        result["idempotency_key"] = idempotency_key
        result["wallet"] = {
            "balance_cents": self._wallet_balance_cents,  # UNCHANGED — not debited
            "held_cents": held,
            "debited": False,
            "note": "Held against your SIMULATED prepaid wallet — not yet charged. "
                    "One /confirm books the trip and debits this amount.",
        }
        # PRIVATE server-only context (server strips before returning to the client).
        result["_confirm_ctx"] = {
            "checkout_id": checkout_id,
            "idempotency_key": idempotency_key,
            # C1 — the row's AUTHORITATIVE owner is `real_user_id` (raw, pre-uuid4-
            # stamp identity; "" for a genuinely anonymous trip), NOT `user_id`
            # (which may be a server-minted anon uuid4 that can never have a
            # session — see the docstring on `real_user_id` in negotiate()).
            # Falls back to `user_id` when the caller never set it (direct/test
            # callers of negotiate(), byte-identical to today).
            "user_id": real_user_id if real_user_id is not None else user_id,
            "dest_token": dest_token,
            # #161 — the SAME canonical identity that was stamped into create_checkout's
            # s.UserID this run. Persisted verbatim so a later /confirm's complete_checkout
            # claims the identical value (plan==confirm consistency; see server.py
            # _persist_and_sanitize_plan / confirm()).
            "merchant_user_id": self._merchant_user_id,
        }
        return result

    # ------------------------------------------------------------------
    # #101 — Additive display helpers (var-0 firewalled, display-only)
    # ------------------------------------------------------------------

    def _attach_currency_review(self, result: dict, trip_request: dict) -> None:
        """Attach an indicative display-currency conversion block to the result.

        Pure deterministic (seeded FX table, no clock/network/random).
        Only present when: home_currency != USD, total > 0, currency is seeded.
        NEVER a fabricated figure — unseedable currencies produce no block.
        var-0: home_currency is off _request_digest; attaches AFTER the deterministic
        core is frozen; does NOT touch day_plans or package_total_cents.
        """
        from utils.currency_advisory import (
            convert_usd_cents, currency_for_country, exchange_timing_advice,
            decimals, AS_OF,
        )
        from utils.intent_parser import CITY_TO_ISO2
        home_iso = (trip_request.get("home_currency") or "USD").upper()
        usd_cents = int(result.get("package_total_with_fees_cents")
                        or result.get("package_total_cents") or 0)
        if home_iso == "USD" or usd_cents <= 0:
            return  # USD users / no total → no indicative block (FE shows USD only)
        minor = convert_usd_cents(usd_cents, home_iso)  # None if unseeded
        if minor is None:
            return  # honest decline: unseeded currency → NO fabricated figure
        # local currency for exchange-timing advice (best-effort; advisory only).
        # #87: prefer the ORIGINAL request's EXPLICIT dest_country for the primary
        # (first booked) leg — it's authoritative — over a CITY_TO_ISO2 catalog
        # guess from the bare city name, which can collide across countries
        # (e.g. 'victoria' -> CITY_TO_ISO2 'HK' vs an explicit Seychelles request).
        primary_leg = (result.get("legs") or [{}])[0]
        primary_city = (primary_leg.get("city") or "").lower().strip()
        primary_leg_id = primary_leg.get("leg_id")
        primary_dest_country = ""
        for req_leg in trip_request.get("legs", []) or []:
            if not isinstance(req_leg, dict):
                continue
            if (req_leg.get("leg_id") == primary_leg_id
                    or (req_leg.get("city") or "").lower().strip() == primary_city):
                primary_dest_country = (req_leg.get("dest_country") or "").strip().upper()
                break
        primary_iso2 = primary_dest_country or CITY_TO_ISO2.get(primary_city)
        local_iso = currency_for_country(
            (primary_iso2 or "").lower()
        ) if primary_iso2 else None
        timing = exchange_timing_advice(local_iso, home_iso) if local_iso else None
        result["currency_review"] = {
            "display_currency": home_iso,
            "usd_cents": usd_cents,
            "indicative_minor_units": minor,
            "decimals": decimals(home_iso),
            "as_of": AS_OF,
            "indicative": True,
            "basis": "seeded_snapshot",
            "disclaimer": (
                f"Indicative only (seeded snapshot {AS_OF}); "
                f"the charge is in USD. Verify live rates before exchanging."
            ),
            **({"exchange_timing": timing} if timing else {}),
        }

    def _attach_hotel_geo(self, result: dict) -> None:
        """Attach hotel_lat/hotel_lon/hotel_coord_basis to each leg for the map pin.

        Cache-only in the request path (disk read, deterministic, no network).
        Falls back to city centroid (seeded city_coords.json) when not cached.
        NEVER mutates day_plans or booking bytes — additive only.
        """
        from utils.hotel_geocode import lookup_cached, centroid
        for leg in result.get("legs") or []:
            city = leg.get("city") or ""
            name = (leg.get("title") or leg.get("hotel_id") or "")
            area = leg.get("area") or ""
            country = leg.get("country") or leg.get("iso2") or ""
            coord = lookup_cached(name, area, city, country)  # disk only
            if coord:
                leg["hotel_lat"], leg["hotel_lon"] = coord
                leg["hotel_coord_basis"] = "geocoded"
            else:
                cen = centroid(city)
                if cen:
                    leg["hotel_lat"], leg["hotel_lon"] = cen
                    leg["hotel_coord_basis"] = "city_centroid"

    def _maybe_attach_price_overlay(self, result: dict) -> None:
        """Attach display-only price overlays to each lodging leg.

        DISPLAY-ONLY price overlay — excluded from digest and day_plans.
        Calls get_price_provider().best_price_for_lodging() per leg and attaches
        result.as_display_dict() under leg["price_overlay"]. NEVER touches
        total_cents, cost_basis, or any field that feeds the critic/validator/
        budget. The UAT SeededProvider returns UnavailableResult — var-0 unchanged
        (byte-identical to today).
        """
        provider = get_price_provider()
        for leg in result.get("legs") or []:
            try:
                pr = provider.best_price_for_lodging(
                    leg,
                    city=leg.get("city") or "",
                    checkin=leg.get("checkin") or "",
                    checkout=leg.get("checkout") or "",
                )
                leg["price_overlay"] = pr.as_display_dict()
            except Exception:  # noqa: BLE001 — per-leg fallback: surface unavailable, never propagate
                leg["price_overlay"] = _PriceUnavailable(
                    reason="price overlay unavailable"
                ).as_display_dict()

    def commit_plan(
        self,
        *,
        user_id: str,
        checkout_id: str,
        idempotency_key: str,
        plan_envelope: dict,
        dest_token: str,
        merchant_user_id: str = "",
    ) -> dict:
        """#1 CONSENT SPLIT — the ONE human consent: commit a previously-held plan.

        Calls _do_commit (complete_checkout) for the held checkout, then stamps the
        booking onto the stored plan envelope (full parity preserved). Idempotent:
        the merchant returns the same booking_ref + does NOT re-debit on a repeat key,
        and the caller (server) short-circuits a repeat /confirm via the trips store.
        Honest terminals on failure — NEVER marks booked on insufficient funds /
        commit error. Off the var-0 path (a stateful side effect, like a booking).
        Defense-in-depth: refuses to commit a plan with no real check-in dates on
        any leg (the frontend also disables Confirm & Book in this case; this guards
        a direct API call bypassing the UI).

        `merchant_user_id` (#161): the canonical, session-verified merchant identity
        persisted at plan time (server.py row['merchant_user_id']) — the SAME value
        that was stamped into create_checkout's s.UserID this trip. commit_plan is a
        standalone entrypoint (never routes through negotiate()), so it sets
        self._merchant_user_id itself, HERE, before any merchant dispatch — this
        instance is a long-lived singleton across requests (guarded by the server's
        orch_lock), so relying on a stale value from a prior negotiate() call would
        be a real cross-request identity bug. Defaults to `user_id` when omitted
        (back-compat for direct/test callers)."""
        self._merchant_user_id = (merchant_user_id or "").strip() or user_id
        legs = plan_envelope.get("legs") or []
        day_plans = plan_envelope.get("day_plans") or []
        has_dates = any(leg.get("checkin") for leg in legs) or any(dp.get("checkin") for dp in day_plans)
        if not has_dates:
            return {
                "outcome": "cannot_satisfy",
                "reason": "Trip dates must be set before booking — set them via chat or the date picker, then confirm again.",
                "idempotency_key": idempotency_key,
                "detail": "No checkin date found on any leg — booking blocked until dates are confirmed.",
            }
        # L4 — the already-known priced package total (held at plan time), so a
        # commit-time needs_consent/needs_mandate reply (which OMITS total_cents
        # from complete_checkout — the session was never actually completed)
        # still shows the real amount instead of a misleading $0. See
        # _do_commit's `total_cents` docstring paragraph.
        _commit_total = (plan_envelope.get("package_total_with_fees_cents")
                          or plan_envelope.get("package_total_cents"))
        bres = self._do_commit(
            user_id=user_id, checkout_id=checkout_id, idempotency_key=idempotency_key,
            total_cents=_commit_total)
        decision = bres.get("decision")
        if decision == "insufficient_funds":
            return {
                "outcome": "cannot_satisfy",
                "reason": "insufficient_funds",
                "idempotency_key": idempotency_key,
                "total_cents": bres.get("total_cents"),
                "wallet_balance_cents": bres.get("wallet_balance_cents"),
                "detail": "Trip exceeds the funded wallet balance — not booked.",
            }
        # M2 — mirror _run_negotiation_rounds's decision ladder (see
        # _needs_consent_terminal): a COMMIT-time needs_consent/needs_mandate is
        # the merchant asking for consent/mandate the caller hasn't (yet)
        # supplied — NOT a genuinely ambiguous/failed commit. Before this fix
        # every non-"accept" decision here (including this one) fell into the
        # generic "commit_errored"+needs_reconciliation catch-all below, which
        # told the client to retry the SAME idempotency_key — a retry that can
        # never succeed (the merchant will just ask for consent/mandate again)
        # and misstates the real, actionable cause.
        if decision in ("needs_consent", "needs_mandate"):
            message = (
                bres.get("consent_message")
                or bres.get("mandate_message")
                or (
                    "The merchant requires explicit buyer consent to complete "
                    "this booking."
                    if decision == "needs_consent" else
                    "The merchant requires an AP2 autonomy mandate to complete "
                    "this booking."
                )
            )
            return {
                "outcome": "needs_consent",
                "reason": decision,
                "detail": message,
                "consent_message": bres.get("consent_message"),
                "mandate_message": bres.get("mandate_message"),
                "checkout_id": bres.get("checkout_id", checkout_id),
                "idempotency_key": idempotency_key,
            }
        # M3 — a merchant verdict that is a DEFINITE (never-booked) outcome —
        # sold out / merchant-side unavailable — is not a re-price veto and not
        # an ambiguous commit failure; see budget_agent._map_complete_response's
        # "void"/"error" branches for where this decision originates. Honest
        # terminal, no needs_reconciliation (retrying the same key cannot
        # possibly succeed — the item is genuinely gone, not mispriced).
        if decision in ("unavailable", "cannot_price"):
            return {
                "outcome": "cannot_satisfy",
                "reason": decision,
                "idempotency_key": idempotency_key,
                "checkout_id": bres.get("checkout_id", checkout_id),
                "detail": (
                    f"The merchant could not complete this booking "
                    f"({bres.get('veto_reason', decision)}) — failing "
                    f"conservative rather than booking an unavailable "
                    f"selection. This will not succeed on retry."
                ),
            }
        # M2 — a genuine commit-time VETO (merchant re-priced between plan and
        # confirm — see the module docstring on the plan-to-confirm think-time
        # gap) is an honest, ACTIONABLE outcome (re-plan/re-submit at the new
        # price), not an ambiguous server-side state — must NOT carry
        # needs_reconciliation (that is reserved for a genuinely RAISED/failed
        # commit, the only case that legitimately falls through below).
        if decision == "veto":
            return {
                "outcome": "cannot_satisfy",
                "reason": "veto",
                "idempotency_key": idempotency_key,
                "checkout_id": bres.get("checkout_id", checkout_id),
                "budget_ceiling_cents": bres.get("budget_ceiling_cents"),
                "hard_max_cents": bres.get("hard_max_cents"),
                "detail": (
                    f"The merchant re-priced this booking between plan and "
                    f"confirm ({bres.get('veto_reason', 'price_changed')}) — "
                    f"not booked. Re-plan for the new price rather than "
                    f"retrying this same idempotency_key."
                ),
            }
        if decision == "commit_failed" or decision != "accept":
            return {
                "outcome": "cannot_satisfy",
                "reason": "commit_errored",
                "needs_reconciliation": True,
                "idempotency_key": idempotency_key,
                "detail": bres.get("detail") or bres.get("veto_reason")
                or "Commit could not be completed — retry with the same idempotency_key.",
            }
        # ACCEPTED — stamp the booking onto the held envelope (parity preserved).
        env = dict(plan_envelope)
        env["outcome"] = "success"
        env["payment_status"] = "charged"
        env["booking_ref"] = TravelOrchestrator._mint_booking_ref(
            bres.get("booking_ref"), dest_token or "UNK")
        env["checkout_id"] = checkout_id
        committed_total = bres.get("total_cents")
        if committed_total:
            env["total_booked_cents"] = committed_total
        env["wallet"] = {
            "balance_cents": bres.get("wallet_balance_cents"),
            "debited": True,
            "debit_cents": bres.get("wallet_debit_cents"),
            "note": "Charged to your SIMULATED prepaid wallet on confirm.",
        }
        # Circle Agentic Economy Prize: REAL (not simulated) USDC settlement result
        # — transaction_id, status, tx_hash, block_explorer_url, rail, network, note
        # (see ucp-merchant/circle_usdc.go's maybeCircleSettle). commit_plan
        # reconstructs `env` field-by-field rather than passing bres through
        # verbatim (see the wallet block above for the established pattern), so
        # this MUST be copied explicitly too or it is silently dropped here even
        # though budget_agent._map_complete_response already surfaced it on bres.
        # Absent when the booking didn't opt into settlement_rail="circle_usdc".
        if bres.get("circle_settlement"):
            env["circle_settlement"] = bres["circle_settlement"]
        env.pop("_confirm_ctx", None)
        self._emit_wallet_debit(bres)
        return env

    def cancel_plan(
        self,
        *,
        user_id: str,
        checkout_id: str,
        merchant_user_id: str = "",
        autonomy_level: str = "L2",
    ) -> dict:
        """H1 fix — VOID a booked trip's merchant checkout via the REAL
        budget.cancel A2A skill (BudgetAgent._cancel_merchant -> cancel_checkout),
        mirroring commit_plan's dispatch pattern. Before this method existed,
        server.py's /cancel had NO way to reach the merchant at all in
        Go-merchant deployment mode (UCP_MERCHANT_URL set, no in-process
        local_merchant to call directly) — it fabricated a synthetic
        {"status": "cancelled"} with zero merchant interaction, so the booking
        stayed live merchant-side and the wallet was never credited while the
        user was told "cancelled, refunded_cents: 0".

        Standalone entrypoint (never routes through negotiate()/commit_plan),
        so — like commit_plan — it sets self._merchant_user_id itself, HERE,
        before any merchant dispatch (this instance is a long-lived singleton
        across requests, guarded by the server's orch_lock; relying on a stale
        value from a prior call would be a real cross-request identity bug).
        Defaults to `user_id` when `merchant_user_id` is omitted (back-compat
        for direct/test callers).

        Returns the raw BudgetCancelResult dict:
          {"decision": "cancelled"|"not_owner"|"error", "checkout_id": ...,
           "wallet_credit_cents"?, "wallet_balance_cents"?, "reason"?}
        Never raises — a merchant/network failure degrades to
        {"decision": "error", ...} (mirrors _do_commit's honest-terminal
        pattern for the SAME class of failure)."""
        self._merchant_user_id = (merchant_user_id or "").strip() or user_id
        cancel_payload = {
            "user_id": user_id,
            "checkout_id": checkout_id,
            "autonomy_level": autonomy_level,
        }
        logger.info(
            "orchestrator.cancel_plan: checkout_id=%s", checkout_id,
        )
        try:
            return self._call_budget_cancel(cancel_payload)
        except Exception as exc:  # noqa: BLE001 — never an unhandled crash on cancel
            logger.error(
                "orchestrator.cancel_plan: cancel FAILED checkout_id=%s — %s",
                checkout_id, exc,
            )
            return {
                "decision": "error",
                "checkout_id": checkout_id,
                "reason": "cancel_errored",
                "detail": str(exc),
            }

    # ------------------------------------------------------------------
    # Risk (L1 proactive) — additive signal consumption
    # ------------------------------------------------------------------

    def _assess_risk_signals(self, legs_input: list[dict]) -> dict | None:
        """
        ONE-SHOT Risk.assess over the trip legs (pre-loop). Returns the Risk
        assessment dict (per_leg + rollup), or None when no Risk agent is wired
        (the default → S1–S5 byte-identical). Risk is OFF the money path: this
        NEVER blocks/alters the booking; it only produces additive signals.

        M5: a wired-but-FAILED Risk call also returns None here (Risk stays
        advisory/non-fatal — it must never block a booking), but that is
        otherwise byte-identical to "no Risk agent configured": no hazard
        advisories reach the user AND Insurance (which derives its perils
        solely from this rollup) is silently disabled. Since a real-money
        line (insurance) and safety advisories are at stake, that degradation
        must not be silent. `self._risk_degraded` is reset here every call
        (this instance is a long-lived per-request singleton) and set ONLY
        when the agent was actually wired and the call raised — negotiate()
        surfaces it as a top-level flag + honest advisory.
        """
        self._risk_degraded = False
        # #236/#240 R2 wiring fix: thread each leg's country through to Risk so
        # region_for_city's country-qualified composite lookup is actually
        # reached (without this, a cross-country homonym city — sevilla,
        # cordoba, san jose, hamilton, salamanca, ... — silently inherited the
        # WRONG country's hazard/advisory data on this, the only production
        # Risk call site). Same dest_country-or-CITY_TO_ISO2-fallback pattern
        # already used by the Health/Compliance gates just below in this file.
        def _leg_iso2(leg: dict) -> str | None:
            dest_country = leg.get("dest_country") or leg.get("country") or leg.get("iso2")
            if dest_country:
                return dest_country
            city = leg.get("city")
            if isinstance(city, str) and city.strip():
                return CITY_TO_ISO2.get(city.strip().lower())
            return None

        payload = {
            "legs": [
                {
                    "leg_id": f"leg-{i}",
                    "city": leg.get("city", ""),
                    "iso2": _leg_iso2(leg),
                    "checkin": leg.get("checkin"),
                    "checkout": leg.get("checkout"),
                    "mode": leg.get("mode", "flight"),
                }
                for i, leg in enumerate(legs_input)
            ]
        }
        # D7 — thread the SINGLE frozen run-`today` into Risk too, so every
        # specialist (health/compliance/risk) shares one consistent date.
        if self._today:
            payload["today"] = self._today
        try:
            self._tracer("agent_started", "Risk", trip_id=self._trip_id, summary="assessing legs...")
        except Exception:  # noqa: BLE001
            pass
        try:
            assessment = self._call_risk(payload)
        except Exception as exc:  # noqa: BLE001 — Risk is advisory; never fatal
            # M5 — reached ONLY when a Risk agent IS wired and the call itself
            # failed (an unconfigured agent returns None from _call_risk without
            # raising, never entering this branch) — so this is a genuine
            # degradation, not the normal not-wired bypass. Flag it for
            # negotiate() to surface honestly instead of silently proceeding
            # as if the trip were hazard-free.
            logger.warning("orchestrator: risk.assess failed (advisory; ignored): %s", exc)
            self._risk_degraded = True
            return None
        if assessment is None:
            return None
        rollup = assessment.get("rollup", {})
        logger.info(
            "orchestrator: risk.assess avoid=%s max_buffer=%dmin flagged=%s codes=%s",
            rollup.get("any_avoid_window"), rollup.get("max_buffer_connection_min", 0),
            rollup.get("flagged_legs"), rollup.get("all_reason_codes"),
        )
        # Tracer: risk agent_completed (side-channel only)
        try:
            flagged_legs = rollup.get("flagged_legs") or []
            reason_codes = rollup.get("all_reason_codes") or []
            self._tracer(
                "agent_completed",
                "Risk",
                trip_id=self._trip_id,
                summary="FLAG" if flagged_legs else "CLEAR",
                data={"reason_codes": reason_codes, "flagged_legs": flagged_legs},
            )
        except Exception:  # noqa: BLE001
            pass
        return assessment

    @staticmethod
    def _attach_booking_links(result: dict, *, nationality: str | None = None) -> dict:
        """
        #37 — ADDITIVE: build the reference/booking HANDOFF-link layer and attach
        it to the result. PURE + deterministic (no wall-clock, no random, no I/O):
        booking_links is a byte-identical function of the already-deterministic
        result, so var-0 is preserved. Also mutates per-entity links in place
        (leg["booking_link"], attraction["link"], meal["link"]).

        Honesty: every link carries a `kind` discriminator; flight/meta-search
        links never embed a fare/flight-number; UAT insurance is a compare_note
        with no vendor plan; PROD insurance is a nationality-keyed
        deeplink (TG_EDITION=prod only). The UCP checkout stays SEPARATE.

        Parameters
        ----------
        result : dict
            The negotiation result dict.
        nationality : str | None
            The traveler's home-country ISO-2 (e.g. "SG"). Passed from
            ``self._nationality`` at the negotiate() call site; defaults to None
            so the static call in tests (no nationality) remains byte-identical
            to the UAT compare_note behaviour.
        """
        from utils.booking_links import build_booking_links

        try:
            build_booking_links(result, nationality=nationality)
        except Exception as exc:  # noqa: BLE001 — handoff links are advisory; never fatal
            logger.warning(
                "orchestrator: build_booking_links failed (advisory; ignored): %s", exc
            )
        return result

    @staticmethod
    def _attach_risk_signals(result: dict, assessment: dict | None) -> dict:
        """
        ADDITIVE: attach Risk's PLANNING-INPUT signals to the negotiation result.

        No-op (byte-identical result) when no Risk agent is wired (assessment is
        None) — this is what keeps S1–S5 unchanged. When present, it adds a
        ``risk_signals`` block (avoid/buffer/flag roll-up + per-leg signals) the
        UI/Critic surfaces. Risk is OFF the money path: it adds NO line item, NO
        fee, and never changes the booking/total — signals only.
        """
        if assessment is None or not isinstance(result, dict):
            return result
        rollup = assessment.get("rollup", {})
        result["risk_signals"] = {
            "consolidator": "risk-agent",
            "any_avoid_window": rollup.get("any_avoid_window", False),
            "buffer_connection_min": rollup.get("max_buffer_connection_min", 0),
            "flagged_legs": rollup.get("flagged_legs", []),
            "reason_codes": rollup.get("all_reason_codes", []),
            "per_leg": assessment.get("per_leg", []),
        }
        return result

    @staticmethod
    def _risk_planning_directives(assessment: dict | None) -> dict | None:
        """
        Risk→DP/greedy consumption (CONTEXTUAL). Translate the Risk roll-up into
        PLANNING directives the Planner consumes ADDITIVELY (real buffering /
        avoidance when Risk signals a condition). Returns None when there is no
        condition (no avoid window, no connection buffer, no flagged leg) — which
        is what keeps S1–S5 byte-identical (no key added to the planner payload).
        """
        if assessment is None:
            return None
        rollup = assessment.get("rollup", {}) or {}
        any_avoid = bool(rollup.get("any_avoid_window"))
        buffer_min = int(rollup.get("max_buffer_connection_min", 0) or 0)
        flagged = list(rollup.get("flagged_legs", []) or [])
        if not any_avoid and buffer_min <= 0 and not flagged:
            return None  # no Risk condition → pass-through (no directives)
        return {
            "avoid_window": any_avoid,
            "buffer_connection_min": buffer_min,
            "flagged_legs": flagged,
            "reason_codes": list(rollup.get("all_reason_codes", []) or []),
            "prefer_flexible_cancellation": any_avoid,
        }

    # ------------------------------------------------------------------
    # CONTEXTUAL specialist GATES — composed into the live negotiate flow.
    # Each fires ONLY when its condition is present in the trip_request, and is
    # PASS-THROUGH (returns None / no-op) otherwise → S1–S5 byte-identical.
    # ------------------------------------------------------------------

    @staticmethod
    def _is_zero_poi_coverage_leg(plan: dict) -> bool:
        """#B1 — True iff this day-plan is a GENUINE zero-POI-coverage miss: the
        catalog lookup missed (catalog_hit is exactly False) AND the produced plan
        is actually empty (no attractions and no non-null meal on any day — exactly
        what the traveler sees as "Nothing scheduled this day yet").

        Both conditions are required on purpose. This codebase's narration layer
        documents (test_itinerary_narration_cov3 / test_itinerary_narrative_cov3)
        that a catalog MISS can still carry real sub-area POIs (the Bali case:
        "catalog_hit is NOT the gate"), so keying the "we have no activity data for
        this city" advisory on catalog_hit alone could dishonestly warn about a leg
        that actually received a full itinerary. The catalog_hit=False half also
        keeps this scoped to true misses, never a THIN catalog (catalog_hit=True)
        that merely under-covers a city.

        PURE read over the plan dict — no LLM, no clock, no new state (var-0)."""
        if not isinstance(plan, dict) or plan.get("catalog_hit") is not False:
            return False
        for day in plan.get("days", []) or []:
            if not isinstance(day, dict):
                continue
            if day.get("attractions"):
                return False
            meals = day.get("meals")
            if isinstance(meals, dict) and any(m for m in meals.values()):
                return False
        return True

    @staticmethod
    def _leg_water_hazard(risk_assessment: dict | None, leg_id: str) -> bool:
        """
        #52 item 8 — True iff Risk flagged an active HIGH/AVOID water-related
        hazard (a cyclone or flood AVOID window) for THIS specific leg. PURE
        lookup over risk_assessment["per_leg"] (see risk_agent.assess_leg) — no
        LLM, no new risk logic invented; just reads the signal Risk already
        computes. False (no filtering) whenever Risk is not wired, the leg is
        not found, or the leg's hazard is present but not at AVOID severity —
        this is a DEPRIORITIZATION signal, not a new block, so it stays
        deliberately conservative about when it fires.
        """
        if not isinstance(risk_assessment, dict):
            return False
        for sig in risk_assessment.get("per_leg", []) or []:
            if not isinstance(sig, dict) or sig.get("leg_id") != leg_id:
                continue
            if not sig.get("decisions", {}).get("avoid_window"):
                return False
            for adv in sig.get("advisory", []) or []:
                if (isinstance(adv, dict)
                        and adv.get("type") in ("cyclone_window", "flood_season")
                        and adv.get("severity") == "high"):
                    return True
            return False
        return False

    @staticmethod
    def _flag_unresolved_legs(
        verdict: dict | None,
        unresolved: list[tuple[int, str]],
        *,
        total_legs: int,
        flagged_key: str,
        id_key: str,
        reason_key: str,
        reason_value: str,
        ok_verdict: str,
        flagged_verdict: str,
        has_flags_key: str,
    ) -> None:
        """
        #52 (item 5) — a leg that never resolved to a place_key/dest_country (e.g.
        the diacritic-mismatch bug this fix also closes at the root — see
        intent_parser._load_city_to_iso2 / _ascii_fold_city) was previously just
        DROPPED from the legs sent to Health/Compliance and logged — the aggregate
        then read as fully clean (verdict=CAN_COMPLETE/ALLOW) when it was actually
        INCOMPLETE. Mutate `verdict` IN PLACE so an unresolved leg is honestly
        flagged (never silently equivalent to "assessed and clean"): appended to
        the same flagged_destinations/flagged_legs list the UI already renders,
        the headline verdict downgraded off its "all clear" value, and explicit
        legs_assessed/legs_total counters added. Does NOT newly block the booking
        (advisory-only, matching the existing "unverified" honesty pattern) — a
        genuine hard block still requires a real gate finding one.
        No-op when `verdict` is None (the call itself failed/errored — that path
        already fails conservative) or there is nothing unresolved.
        """
        if not unresolved or not isinstance(verdict, dict):
            return
        n = len(unresolved)
        for leg_idx, city in unresolved:
            verdict.setdefault(flagged_key, [])
            verdict[flagged_key].append({
                id_key: f"leg-{leg_idx}",
                reason_key: reason_value,
                "flag_advisory": (
                    f"Leg {leg_idx} ('{city}') could not be matched to a known "
                    f"country — this leg was NOT assessed ({n} of {total_legs} "
                    f"legs on this trip could not be assessed). Verify entry/"
                    f"health requirements for it independently before booking."
                ),
            })
        verdict[has_flags_key] = True
        if verdict.get("verdict") == ok_verdict:
            verdict["verdict"] = flagged_verdict
        verdict["unresolved_legs"] = [
            {"leg_index": i, "city": c} for i, c in unresolved
        ]
        verdict["legs_assessed"] = total_legs - n
        verdict["legs_total"] = total_legs

    def _run_health_gate(
        self, trip_request: dict, legs_input: list[dict]
    ) -> dict | None:
        """
        Health vaccination + entry-cert GATE (CONTEXTUAL). Fires ONLY when at
        least one leg carries a ``place_key`` — Health's primary, seeded slate key
        (the DC-health shape). A plain Bali/WA `city`-only leg, or a visa-only leg
        carrying just ``dest_country`` (the DC-compliance shape), has no place_key
        → None (pass-through). This keeps Health and Compliance independently
        contextual on their OWN condition keys. Returns the Health verdict
        (verdict/bookable/line_items/jab_records) or None.
        """
        if self._health_client is None and not self._health_url:
            return None
        # #70 SAFETY: Health must assess by DESTINATION, not require an explicit place_key.
        # The free-text path sets place_key=city, but a STRUCTURED/API trip carries only
        # {city, dest_country} — without firing on dest_country, a YF-endemic destination
        # booked via the structured path silently SKIPPED the vaccine/entry-cert mandate
        # (under-warn). Fire on place_key OR dest_country (mirrors the compliance gate) so the
        # structured/API path now gets Health assessed EXACTLY like the free-text path —
        # surfacing the same entry-cert/vaccine mandate (a correct enrichment, NOT byte-
        # identical for structured callers; that's the point). VAR-0: the pinned benchmark
        # scenarios carry no dest_country (bare city-only legs) so they don't activate Health
        # and stay byte-identical — verified (verify_report_numbers 86/86, trace_var0,
        # e2e_countries all green). Conservative-block on a failed assess is preserved below.
        health_active = any(
            leg.get("place_key") or leg.get("dest_country") for leg in legs_input
        )
        if not health_active:
            return None
        # #26 fail-open fix: once Health is active, EVERY leg is checked — a
        # city-only leg (no place_key/dest_country) is NOT silently skipped. Backfill
        # dest_country from the city via the SAME map the parser uses, so a
        # YF-mandatory leg named only by city (e.g. 'lagos') is still entry-checked.
        # We only ADD legs that resolve (place_key or a real dest_country); an
        # unresolvable city is logged, never a silent pass. This does NOT newly fire
        # Health for a plain trip (health_active already gated that), so S1–S5 are
        # unaffected; it only widens an ALREADY-firing gate to cover all its legs.
        health_legs = []
        health_unresolved: list[tuple[int, str]] = []  # #52 item 5 — see _flag_unresolved_legs
        for i, leg in enumerate(legs_input):
            place_key = leg.get("place_key")
            dest_country = leg.get("dest_country")
            if not place_key and not dest_country:
                city = leg.get("city")
                if isinstance(city, str) and city.strip():
                    dest_country = CITY_TO_ISO2.get(city.strip().lower())
            if place_key or dest_country:
                health_legs.append({
                    "leg_id": f"leg-{i}",
                    "place_key": place_key,
                    "dest_country": dest_country,
                    "departure_date": leg.get("departure_date") or leg.get("checkin"),
                })
            else:
                logger.warning(
                    "orchestrator: health gate active but leg %d city unresolved "
                    "to a country — entry requirements NOT verified for it", i,
                )
                health_unresolved.append((i, str(leg.get("city") or "")))
        if not health_legs:
            return None  # no health condition in this scenario → pass-through
        payload: dict[str, Any] = {"legs": health_legs}
        # D7 — thread the SINGLE frozen run-`today` (captured at negotiate() entry),
        # not a fresh per-gate read, so every gate in the run agrees on one date.
        if self._today:
            payload["today"] = self._today
        if trip_request.get("buffer_days") is not None:
            payload["buffer_days"] = trip_request["buffer_days"]
        # #70: opt-in held vaccine certs — a held entry-required cert clears the gate (no lead-
        # time block) and is excluded from the enforced budget. Default absent → unchanged.
        _held = trip_request.get("held_vaccine_certs")
        if isinstance(_held, (list, tuple)) and _held:
            payload["held_vaccine_certs"] = [str(c) for c in _held]
        # #52 — thread the traveler's stated nationality through as a REASONABLE
        # (not perfect) transit-origin proxy for leg 0's origin-conditional
        # mandates (e.g. yellow fever) when this trip has no earlier leg to derive
        # a real transit chain from. Same field Compliance already keys off; only
        # added when present, so a trip with no nationality is byte-identical.
        if trip_request.get("nationality"):
            payload["origin_nationality"] = trip_request["nationality"]
        try:
            self._tracer("agent_started", "Health", trip_id=self._trip_id, summary="checking health requirements...")
        except Exception:  # noqa: BLE001
            pass
        try:
            verdict = self._call_health(payload)
        except Exception as exc:  # noqa: BLE001
            # D1 — FAIL CONSERVATIVE, NOT OPEN. The trip CARRIES a health condition
            # (health_legs is non-empty), so a call failure must NOT degrade to None
            # ("no condition → pass-through"). Return a CONSERVATIVE BLOCKING verdict
            # so the trip fails to a flag/block, never silently books past an
            # unverified entry-cert hazard.
            logger.warning(
                "orchestrator: health.assess errored — CONSERVATIVE BLOCK: %s", exc
            )
            return {
                "bookable": False,
                "flag": True,
                "advisory": "health errored — conservative block",
            }
        # #52 item 5 — honestly flag any leg dropped above (never silently equivalent
        # to "assessed and clean"). No-op when nothing was dropped.
        self._flag_unresolved_legs(
            verdict, health_unresolved, total_legs=len(legs_input),
            flagged_key="flagged_destinations", id_key="leg_id",
            reason_key="gate_reason", reason_value="BLOCK_UNKNOWN_SLATE_CONSERVATIVE",
            ok_verdict="can_complete", flagged_verdict="can_complete_with_flags",
            has_flags_key="has_health_flags",
        )
        if verdict is not None:
            logger.info(
                "orchestrator: health gate verdict=%s bookable=%s fees=%d¢ jabs=%d",
                verdict.get("verdict"), verdict.get("bookable"),
                verdict.get("total_vaccine_cost_usd_cents", 0),
                len(verdict.get("jab_records") or []),
            )
            # Tracer: health agent_completed (side-channel only)
            try:
                _tier_ord = {"D": 0, "C": 1, "B": 2, "A": 3}
                _worst: str | None = None
                _evac = False
                for _d in (verdict.get("per_destination") or []):
                    _t = _d.get("medical_access_tier") if isinstance(_d, dict) else None
                    if _t and (_worst is None or _tier_ord.get(_t, 99) < _tier_ord.get(_worst, 99)):
                        _worst = _t
                    if isinstance(_d, dict) and _d.get("evacuation_recommended"):
                        _evac = True
                tier = _worst or "unknown"
                evac = _evac
                self._tracer(
                    "agent_completed",
                    "Health",
                    trip_id=self._trip_id,
                    summary=f"verdict={verdict.get('verdict')} tier={tier} evac={evac}",
                    data={"verdict": verdict.get("verdict"), "bookable": verdict.get("bookable"),
                          "medical_access_tier": tier, "evacuation_recommended": evac},
                )
            except Exception:  # noqa: BLE001
                pass
        return verdict

    def _run_compliance_gate(
        self,
        trip_request: dict,
        legs_input: list[dict],
        health_verdict: dict | None,
    ) -> dict | None:
        """
        Compliance visa lead-time eligibility GATE (CONTEXTUAL). Fires ONLY when
        the trip carries a nationality AND at least one leg has a dest_country
        (the DC-compliance shape). A plain city-only Bali/WA trip has no
        nationality/dest_country → None (pass-through).

        INTERLOCK (Health→Compliance): the Health jab_records are forwarded so
        Compliance can read the jab record for the YF entry-certificate half of
        the §7 handoff.
        """
        if self._compliance_client is None and not self._compliance_url:
            return None
        nationality = trip_request.get("nationality")
        # #70 multi-passport (opt-in): a list of passports the traveler holds. When present,
        # Compliance picks the best passport PER LEG. Gated on this NEW field so the single-
        # nationality path stays byte-identical (the payload key is added only when non-empty).
        _passports = trip_request.get("passports") or trip_request.get("nationalities")
        nationalities = (
            [str(n).strip().upper() for n in _passports if str(n).strip()]
            if isinstance(_passports, (list, tuple)) else []
        )
        # CONTEXTUAL FIRING (append-only): Compliance is "active" when the trip carries a
        # nationality (or a passports[] list) OR any leg already has a dest_country (the
        # DC-compliance opt-in). A plain city-only Bali/WA trip has none →
        # pass-through, S1–S5 byte-identical.
        compliance_active = bool(nationality) or bool(nationalities) or any(
            leg.get("dest_country") for leg in legs_input
        )
        if not compliance_active:
            return None
        # #26 fail-open fix: once Compliance is active (e.g. a nationality was
        # supplied), EVERY leg is visa-checked — a city-only leg is NOT silently
        # passed. Backfill dest_country from the city via the parser's map so a
        # visa-required leg named only by city is still checked; an unresolvable
        # city is logged, never a silent ALLOW. This only widens an ALREADY-active
        # gate to cover all its legs, so S1–S5 (neither nationality nor
        # dest_country) are untouched.
        comp_legs = []
        comp_unresolved: list[tuple[int, str]] = []  # #52 item 5 — see _flag_unresolved_legs
        for i, leg in enumerate(legs_input):
            dest_country = leg.get("dest_country")
            if not dest_country:
                city = leg.get("city")
                if isinstance(city, str) and city.strip():
                    dest_country = CITY_TO_ISO2.get(city.strip().lower())
            if dest_country:
                comp_legs.append({
                    "leg_id": f"leg-{i}",
                    "dest_country": dest_country,
                    "departure_date": leg.get("departure_date") or leg.get("checkin"),
                })
            else:
                logger.warning(
                    "orchestrator: compliance gate active but leg %d city unresolved "
                    "to a country — visa eligibility NOT verified for it", i,
                )
                comp_unresolved.append((i, str(leg.get("city") or "")))
        if not comp_legs:
            return None  # no dest_country resolvable on any leg → nothing to check
        # If nationality is absent, run with empty string → all legs get FLAG advisory
        # (unverified — never a silent pass-through, never a hard block for unknowns).
        payload: dict[str, Any] = {
            "legs": comp_legs,
            "nationality": nationality,
        }
        if nationalities:  # #70 multi-passport — only when opted in (byte-identical otherwise)
            payload["nationalities"] = nationalities
        # D7 — thread the SINGLE frozen run-`today` (captured at negotiate() entry).
        if self._today:
            payload["today"] = self._today
        if trip_request.get("buffer_business_days") is not None:
            payload["buffer_days"] = trip_request["buffer_business_days"]
        if trip_request.get("passport_expiry"):
            payload["passport_expiry"] = trip_request["passport_expiry"]
        # Health→Compliance jab interlock: forward the jab records so Compliance
        # owns the entry-DOCUMENT half against the SAME seeded jab record.
        if health_verdict is not None:
            payload["jab_records"] = health_verdict.get("jab_records") or []
        try:
            self._tracer("agent_started", "Compliance", trip_id=self._trip_id, summary="checking visa requirements...")
        except Exception:  # noqa: BLE001
            pass
        try:
            verdict = self._call_compliance(payload)
        except Exception as exc:  # noqa: BLE001
            # D1 — FAIL CONSERVATIVE, NOT OPEN. The trip CARRIES a compliance
            # condition (comp_legs is non-empty), so a call failure must NOT degrade
            # to None ("no condition → pass-through"). Return a CONSERVATIVE BLOCKING
            # verdict so the visa lead-time gate fails to a flag/block rather than
            # silently booking with ZERO entry/visa checking.
            logger.warning(
                "orchestrator: compliance.check errored — CONSERVATIVE BLOCK: %s", exc
            )
            return {
                "bookable": False,
                "flag": True,
                "advisory": "compliance errored — conservative block",
            }
        # #52 item 5 — honestly flag any leg dropped above (never silently equivalent
        # to "assessed and clean"). No-op when nothing was dropped.
        self._flag_unresolved_legs(
            verdict, comp_unresolved, total_legs=len(legs_input),
            flagged_key="flagged_legs", id_key="leg_id",
            reason_key="reason", reason_value="BLOCK_UNKNOWN_RULE_CONSERVATIVE",
            ok_verdict="can_satisfy", flagged_verdict="can_satisfy_with_flags",
            has_flags_key="has_eligibility_flags",
        )
        if verdict is not None:
            logger.info(
                "orchestrator: compliance gate verdict=%s bookable=%s visa_fee=%d¢ "
                "jab_interlock=%d",
                verdict.get("verdict"), verdict.get("bookable"),
                verdict.get("total_visa_fee_usd_cents", 0),
                len((health_verdict or {}).get("jab_records") or []),
            )
            # Tracer: compliance agent_completed (side-channel only)
            try:
                v = verdict.get("verdict", "?")
                bookable = verdict.get("bookable", True)
                self._tracer(
                    "agent_completed",
                    "Compliance",
                    trip_id=self._trip_id,
                    summary="PASS" if bookable else f"BLOCK verdict={v}",
                    data={"verdict": v, "bookable": bookable,
                          "total_visa_fee_usd_cents": verdict.get("total_visa_fee_usd_cents", 0)},
                )
            except Exception:  # noqa: BLE001
                pass
        return verdict

    def _run_fraud_gate(self, trip_request: dict) -> dict | None:
        """
        Fraud counterparty-solvency advisory GATE #1 (CONTEXTUAL, pre-commit).
        Fires when the trip carries explicit counterparties — a top-level
        ``counterparties`` list, ``carriers`` on a leg/trip (the DC-fraud
        shape), OR a per-LEG ``counterparty_id`` (a documented, unit-tested
        leg field — see critic_agent.py Gate 6b). A plain lodging-only
        Bali/WA trip carries none of these → None (pass-through). The live
        Critic re-checks the SAME seeded band at commit time (gate #2), so
        this advisory filter never replaces the commit check.

        BUG (2026-07 adversarial audit): this gate previously read ONLY the
        two top-level shapes above and never looked at a leg's own
        ``counterparty_id`` — so a watchlisted/insolvent carrier placed
        directly on a leg (rather than in the top-level ``counterparties``/
        ``carriers`` lists) produced ZERO fraud signal here. Harvesting it
        below is defense-in-depth #1 of 2 for that finding; #2 is threading
        the same leg-level counterparty_id through to the Critic's commit-time
        re-check (see the ``candidate_legs`` build in ``_run_negotiation_rounds``).

        SCOPE (organic vs. synthetic detection — read before assuming broader
        coverage): the solvency table this gate (and the Critic's re-check)
        consult is a SEEDED, hand-curated set of known-bad demo counterparty
        ids (see fraud_agent.py's ``_SOLVENCY_BY_COUNTERPARTY``) — there is no
        live registry/feed of real-world carrier insolvency. An UNKNOWN
        counterparty_id (i.e. every real, organic carrier/OTA that isn't in
        the seed table) is treated CONSERVATIVELY (blocked pending consent),
        never silently cleared — but that conservative-block is a fail-safe
        default, NOT a positive fraud finding. In practice this gate only ever
        actively FIRES a distress signal (BLOCKED/elevated) on the hand-seeded
        demo ids; it does not (today) detect fraud on an organic booking the
        way a live solvency feed would. This is a deliberate, documented scope
        limitation of the current build, not a claim of live fraud coverage.
        """
        if self._fraud_client is None and not self._fraud_url:
            return None
        counterparties: list[dict] = []
        for cp in trip_request.get("counterparties") or []:
            counterparties.append(cp)
        for c in trip_request.get("carriers") or []:
            counterparties.append({
                "counterparty_id": c.get("counterparty_id") or c.get("catalog_id"),
                "kind": c.get("kind", "transport"),
                "leg_id": c.get("leg_id"),
            })
        # BUG3.1 — leg-level counterparty_id (documented/unit-tested at the Critic,
        # never previously read here). fraud.vet() de-dups on the canonical id
        # itself, so a leg naming the same id as an existing top-level entry is
        # harmless to include again.
        for i, leg in enumerate(trip_request.get("legs") or []):
            if not isinstance(leg, dict):
                continue
            leg_cp_id = leg.get("counterparty_id")
            if isinstance(leg_cp_id, str) and leg_cp_id.strip():
                counterparties.append({
                    "counterparty_id": leg_cp_id,
                    "kind": leg.get("counterparty_kind") or "hotel",
                    "leg_id": leg.get("leg_id") or f"leg-{i}",
                })
        if not counterparties:
            return None  # no counterparty condition in this scenario → pass-through
        payload: dict[str, Any] = {"counterparties": counterparties}
        if trip_request.get("consent_tokens"):
            payload["consent_tokens"] = trip_request["consent_tokens"]
        try:
            self._tracer("agent_started", "Fraud", trip_id=self._trip_id, summary="vetting counterparties...")
        except Exception:  # noqa: BLE001
            pass
        try:
            verdict = self._call_fraud(payload)
        except Exception as exc:  # noqa: BLE001
            # D1 — FAIL CONSERVATIVE, NOT OPEN. The trip CARRIES counterparties
            # (counterparties is non-empty), so a call failure must NOT degrade to
            # None ("no condition → pass-through"). Return a CONSERVATIVE
            # NON-COMMITTABLE rollup so an unverified/insolvent supplier is never
            # silently committed.
            logger.warning(
                "orchestrator: fraud.vet errored — CONSERVATIVE BLOCK: %s", exc
            )
            return {
                "all_committable": False,
                "flag": True,
                "advisory": "fraud errored — conservative block",
                "rollup": {
                    "all_committable": False,
                    "blocked_ids": [c.get("counterparty_id") for c in counterparties],
                },
            }
        if verdict is not None:
            rollup = verdict.get("rollup", {})
            logger.info(
                "orchestrator: fraud gate #1 (advisory) n=%d all_committable=%s blocked=%s",
                rollup.get("n_counterparties", 0),
                rollup.get("all_committable"), rollup.get("blocked_ids"),
            )
            # Tracer: fraud agent_completed (side-channel only)
            try:
                all_ok = rollup.get("all_committable", True)
                blocked = rollup.get("blocked_ids") or []
                self._tracer(
                    "agent_completed",
                    "Fraud",
                    trip_id=self._trip_id,
                    summary="CLEAR" if all_ok else f"BLOCKED blocked={blocked}",
                    data={"all_committable": all_ok, "blocked_ids": blocked,
                          "n_counterparties": rollup.get("n_counterparties", 0)},
                )
            except Exception:  # noqa: BLE001
                pass
        return verdict

    def _estimate_insurance_premium_cents(
        self, risk_assessment: dict | None, ceiling_cents: int
    ) -> int:
        """#72 PRE-COMMIT upper-bound insurance premium for the budget HARD-VETO. Mirrors
        _apply_insurance's peril detection EXACTLY (same policy selection), but priced at
        insured_trip_cost_cents = ceiling (an UPPER bound on the booked lodging). The premium is
        monotonic in trip cost (compute_premium_cents bps >= 0), so this is >= the actual premium
        on the lower booked lodging → reserving it can never yield an over-budget success.
        Returns 0 when no Risk agent / no peril (insurance won't fire) → envelope 0 → var-0
        (no behaviour change for no-peril scenarios)."""
        if risk_assessment is None:
            return 0
        reason_codes = (risk_assessment.get("rollup", {}) or {}).get("all_reason_codes", [])
        if not reason_codes:
            return 0
        try:
            from utils.peril_crosswalk import risk_reasons_to_perils
            perils = risk_reasons_to_perils(reason_codes)
        except Exception as exc:  # noqa: BLE001
            # M4 — this used to be swallowed with NO log line at all; a failure
            # here silently zeros the #72 pre-commit envelope (see below).
            logger.warning(
                "orchestrator: #72 premium estimate — peril_crosswalk failed "
                "(envelope reserve degrades to 0, insurance may still fire "
                "post-commit for the real premium): %s", exc,
            )
            return 0
        if not perils:
            return 0
        payload = {
            "peril_set": [p.value if hasattr(p, "value") else str(p) for p in perils],
            "risk_reason_codes": list(reason_codes),
            "insured_trip_cost_cents": int(ceiling_cents),
        }
        try:
            assessment = self._call_insurance(payload)
        except Exception as exc:  # noqa: BLE001
            # M4 — see above: a TRANSIENT failure of THIS estimate call (distinct
            # from "no peril"/"no agent wired") must not disappear silently. It is
            # still safe to proceed with envelope=0 rather than block the trip
            # (Insurance is advisory/off the money path per design), but the
            # degraded envelope needs to be visible in logs so it can be
            # correlated with a later _apply_insurance over-budget flag.
            logger.warning(
                "orchestrator: #72 premium estimate — insurance.assess_coverage "
                "call failed (envelope reserve degrades to 0, insurance may "
                "still fire post-commit for the real premium): %s", exc,
            )
            return 0
        if assessment is None:
            return 0
        return int(assessment.get("premium_cents", 0) or 0)

    def _apply_insurance(
        self, result: dict, risk_assessment: dict | None,
        total_budget_cents: int | None = None,
    ) -> dict:
        """
        INSURANCE coverage assessment (CONTEXTUAL, pre-commit). Fires ONLY when
        Risk produced a peril signal (mapped from Risk reason codes via the
        peril_crosswalk) AND the booking succeeded. The premium is surfaced and
        (FEE-INJECTION) appended to the result package view via the assembler.
        PASS-THROUGH (no-op) when no peril signal / no Insurance agent wired.

        `total_budget_cents` (M4, optional/back-compat — None → no behaviour
        change): the #72 pre-commit envelope reserved an UPPER-BOUND estimate
        of this premium out of the lodging budget so the booked total could
        never exceed it — UNLESS that estimate call itself failed and silently
        degraded to 0 (see _estimate_insurance_premium_cents). If THIS
        (independent, post-booking) assessment then succeeds and its real
        premium pushes package_total_with_fees_cents over the user's budget,
        that must be surfaced HONESTLY (an over-budget flag + advisory) rather
        than silently absorbed — the booking already committed and cannot be
        undone, but the user must not be left thinking it was enforced.
        """
        if self._insurance_client is None and not self._insurance_url:
            return result
        if not isinstance(result, dict) or result.get("outcome") != "success":
            return result
        if risk_assessment is None:
            return result
        reason_codes = (risk_assessment.get("rollup", {}) or {}).get(
            "all_reason_codes", []
        )
        if not reason_codes:
            return result  # no peril signal → no insurance condition → pass-through
        try:
            from utils.peril_crosswalk import risk_reasons_to_perils
            perils = risk_reasons_to_perils(reason_codes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator: peril_crosswalk failed (ignored): %s", exc)
            return result
        if not perils:
            return result
        insured_cost = int(result.get("total_cents") or 0)
        payload = {
            "peril_set": [p.value if hasattr(p, "value") else str(p) for p in perils],
            "risk_reason_codes": list(reason_codes),
            "insured_trip_cost_cents": insured_cost,
        }
        try:
            self._tracer("agent_started", "Insurance", trip_id=self._trip_id, summary="assessing coverage...")
        except Exception:  # noqa: BLE001
            pass
        try:
            assessment = self._call_insurance(payload)
        except Exception as exc:  # noqa: BLE001
            # M6 — this branch is reached ONLY on a peril-flagged, already-
            # committed booking (the guards above already required outcome ==
            # "success" and a non-empty peril crosswalk). A silent pass-through
            # here previously left the user with risk_flagged=true, a reserved-
            # but-unspent #72 premium envelope, and NO insurance key, NO
            # premium line, and NO indication the assessment even ran — an
            # indistinguishable-from-clean-trip silent drop on a real-money
            # concern. The advisories list is already finalized by the time
            # this method runs (see the caller in negotiate()), so append/
            # create it directly on `result` here.
            logger.warning("orchestrator: insurance.assess failed (ignored): %s", exc)
            result["insurance_assessment_failed"] = True
            adv_note = (
                "Insurance coverage assessment could not be completed for "
                "this trip (flagged hazard perils are present) — no premium "
                "or excluded-perils summary is available for this booking. "
                "Please verify your own travel insurance coverage for this "
                "itinerary independently."
            )
            existing_advisories = result.get("advisories")
            if isinstance(existing_advisories, list):
                existing_advisories.append(adv_note)
            else:
                result["advisories"] = [adv_note]
            return result
        if assessment is None:
            return result
        premium_li = assessment.get("line_item")
        logger.info(
            "orchestrator: insurance premium=%d¢ excluded=%s",
            assessment.get("premium_cents", 0),
            assessment.get("excluded_perils_summary"),
        )
        # Tracer: insurance agent_completed (side-channel only, after call returns)
        try:
            self._tracer(
                "agent_completed",
                "Insurance",
                trip_id=self._trip_id,
                summary=f"premium={assessment.get('premium_cents', 0)}¢ excluded={assessment.get('excluded_perils_summary')}",
                data={"premium_cents": assessment.get("premium_cents", 0),
                      "peril_set": payload["peril_set"],
                      "excluded_perils_summary": assessment.get("excluded_perils_summary")},
            )
        except Exception:  # noqa: BLE001
            pass
        result["insurance"] = {
            "premium_cents": assessment.get("premium_cents", 0),
            "premium_money": assessment.get("premium_money"),
            "excluded_perils_summary": assessment.get("excluded_perils_summary"),
            # #52 item 1 — insurance_agent.assess_coverage() already computes this
            # (the closed-set default for ANY peril with no matching policy clause
            # — see CoverageStatus.UNDETERMINED) but it was being DROPPED here
            # before reaching the caller: the top-level result carried ONLY
            # excluded_perils_summary, so a peril the policy has NO clause for at
            # all (e.g. natural_disaster — no COVER/EXCLUDE/SUBLIMIT/CONDITION row
            # exists for it on either seeded policy) silently vanished from the
            # response. The user then saw an empty exclusions list and read "no
            # gaps" when the real coverage status for the very peril that
            # triggered the premium was UNKNOWN, not clean. Surface it honestly,
            # matching the same never-silently-drop bar excluded_perils_summary
            # already meets.
            "undetermined_perils": assessment.get("undetermined_perils"),
            "peril_set": payload["peril_set"],
            "line_item": premium_li,
        }
        # FEE-INJECTION: append the premium to the result's assembled package view.
        if premium_li is not None:
            self._inject_fees(result, [premium_li])

        # M4 — the #72 pre-commit envelope can only guard against an over-budget
        # commit when its premium ESTIMATE actually reflected this real premium.
        # If the estimate call failed and degraded to 0 (logged above), the
        # envelope reserved nothing, and THIS real premium can now push the
        # already-committed total over budget undetected. Since the booking is
        # already irreversible at this point, we cannot re-veto — but we MUST
        # surface it honestly instead of letting it pass silently.
        if total_budget_cents:
            final_total = result.get("package_total_with_fees_cents")
            if final_total is None:
                final_total = result.get("total_cents")
            if final_total is not None and int(final_total) > int(total_budget_cents):
                over_by = int(final_total) - int(total_budget_cents)
                logger.warning(
                    "orchestrator: #72 ENVELOPE MISS — committed total %d¢ "
                    "(incl. insurance premium) exceeds user budget %d¢ by %d¢ "
                    "(the pre-commit premium estimate likely failed/degraded "
                    "to 0 — see _estimate_insurance_premium_cents warnings).",
                    final_total, total_budget_cents, over_by,
                )
                result["insurance"]["premium_exceeds_budget"] = True
                result["insurance"]["over_budget_cents"] = over_by
                adv_note = (
                    f"Heads up: with the insurance premium included, this "
                    f"booked trip's total (${final_total / 100:,.2f}) is "
                    f"${over_by / 100:,.2f} over your stated budget "
                    f"(${total_budget_cents / 100:,.2f}). The pre-commit budget "
                    f"check could not price insurance in advance for this run."
                )
                existing_advisories = result.get("advisories")
                if isinstance(existing_advisories, list):
                    existing_advisories.append(adv_note)
                else:
                    result["advisories"] = [adv_note]

        # #45 — coverage-GAP cross-check over the USER's OWN policy. OPT-IN: runs
        # ONLY when trip_request supplied a user_policy. Reuses the SAME crosswalked
        # peril_set already computed above. Recommends NO vendor; mints no premium.
        # When self._user_policy is None NO key is added → byte-identical to today.
        if self._user_policy:
            gap_payload = {
                "peril_set": payload["peril_set"],
                "user_policy": self._user_policy,
                "insured_trip_cost_cents": insured_cost,
            }
            try:
                self._tracer(
                    "agent_started", "Insurance", trip_id=self._trip_id,
                    summary="coverage-gap cross-check (user policy)...",
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                gap_report = self._call_coverage_gap(gap_payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("orchestrator: insurance.coverage_gap failed (ignored): %s", exc)
                gap_report = None
            if gap_report is not None:
                result["coverage_gap"] = gap_report
                try:
                    self._tracer(
                        "agent_completed", "Insurance", trip_id=self._trip_id,
                        summary=(
                            f"coverage-gap: {len(gap_report.get('gaps', []))} gap(s), "
                            f"{len(gap_report.get('recommendations', []))} recommendation(s)"
                        ),
                        data={"gaps": gap_report.get("gaps", []),
                              "recommendations": gap_report.get("recommendations", [])},
                    )
                except Exception:  # noqa: BLE001
                    pass
        return result

    # ------------------------------------------------------------------
    # #44 — OPT-IN late-night supper (2nd prepaid checkout KIND, same rails)
    # ------------------------------------------------------------------

    def _maybe_order_supper(
        self, result: dict, *, user_id: str, idempotency_key: str
    ) -> None:
        """
        OPT-IN late-night supper on the SAME prepaid UCP rails as hotels (#44).

        Fires ONLY when the trip_request carried a `supper` request AND the trip
        actually BOOKED (outcome == success). When `supper` is absent NO key is
        added → byte-identical to today (APPEND-ONLY, mirrors #45 coverage-gap /
        #30 day-planner). You never "order supper" for a trip that didn't happen.

        `supper` request shape (all but the mode are optional):
            {"order": bool, "city": str?, "max_cents": int?,
             "diet": str?, "delivery_window": str?}

        Two honesty-preserving modes, selected by the `order` flag:
          - order absent/false → SELECTION ONLY: surfaces the deterministically
            chosen supper + a checkout-ready line item; books NOTHING, charges
            NOTHING.
          - order == true → the flag IS the EXPLICIT fresh consent for a SECOND
            prepaid checkout. A late-night supper is a distinct, user-initiated
            purchase, NOT part of the itinerary's single mandate — so it carries
            its own consent. The food line {food_id, qty} rides the EXISTING
            Budget rails (check → commit), proving the UCP rails generalize to a
            2nd KIND end-to-end (the #44 thesis).

        HONESTY / fail-conservative:
          - Never fabricates a supper: build_supper_order selects ONLY among REAL
            merchant rows; a city with no row → available=False + honest note.
          - A budget VETO / commit failure → ordered=False with the honest reason;
            NEVER a faked booking_ref, NEVER a silent charge.
          - The merchant's simulated-inventory disclosure is carried verbatim.
        var-0: build_supper_order is a pure deterministic selector; the city
        default + query are deterministic functions of the request/legs.
        """
        req = self._supper_request
        if not isinstance(req, dict) or not req:
            return  # not opted in → byte-identical to today
        if result.get("outcome") != "success":
            return  # never order supper for a trip that didn't book
        if getattr(self, "_plan_only", False):
            return  # PLAN-ONLY: the trip itself isn't booked yet — never place the 2nd
            # irreversible supper checkout / wallet debit before the human /confirm consent.
            # (commit_plan does not re-order supper, so plan↔booked parity is preserved;
            #  supper-at-confirm is a fair follow-up, not a money path.)

        # City: explicit request city, else the LAST leg's city (supper is at the
        # destination you END the trip in). Deterministic; no wall-clock/random.
        legs = getattr(self, "_trip_request_legs", None) or []
        default_city = ""
        if legs and isinstance(legs[-1], dict):
            default_city = (legs[-1].get("city") or "")
        city = (req.get("city") or default_city or "").strip()
        if not city:
            return  # nothing to scope the supper search to

        max_cents = req.get("max_cents")
        try:
            max_cents = int(max_cents) if max_cents is not None else None
        except (TypeError, ValueError):
            max_cents = None

        query: dict[str, Any] = {"kind": "FOOD_DELIVERY", "city": city}
        if req.get("diet"):
            query["diet"] = str(req["diet"])
        if req.get("delivery_window"):
            query["delivery_window"] = str(req["delivery_window"])
        if max_cents is not None:
            query["max_cents"] = max_cents

        search = self._call_food_search(query)
        rows = search.get("results") if isinstance(search, dict) else None
        disclosure = search.get("disclosure") if isinstance(search, dict) else None

        from utils.supper_order import build_supper_order
        supper = build_supper_order(
            city, rows, max_cents=max_cents, disclosure=disclosure
        )

        # SELECTION-ONLY unless the request EXPLICITLY consents to ordering.
        supper["ordered"] = False
        if bool(req.get("order")):
            if supper.get("available"):
                supper.update(self._place_supper_order(
                    supper=supper, user_id=user_id,
                    idempotency_key=idempotency_key, max_cents=max_cents,
                ))
            else:
                supper["order_note"] = (
                    "No supper available to order; nothing was charged."
                )
        elif supper.get("available"):
            # No `order` flag → SELECTION-ONLY. Mark it EXPLICITLY (audit NIT) so the
            # UI never has to infer this state from the absence of other fields.
            supper["selection_only"] = True
            supper["order_note"] = (
                "Selection only — set order=true to place the prepaid order."
            )

        result["supper"] = supper
        try:
            self._tracer(
                "agent_completed", "Supper", trip_id=self._trip_id,
                summary=(
                    f"city={city} available={supper.get('available')} "
                    f"ordered={supper.get('ordered')}"
                ),
                data={"city": city, "available": supper.get("available"),
                      "ordered": supper.get("ordered"),
                      "selected": supper.get("selected"),
                      "total_cents": supper.get("total_cents")},
            )
        except Exception:  # noqa: BLE001
            pass

    def _place_supper_order(
        self, *, supper: dict, user_id: str, idempotency_key: str,
        max_cents: int | None,
    ) -> dict:
        """
        Execute the REAL second prepaid checkout for the chosen supper on the
        EXISTING Budget rails (check → commit). The food line {food_id, qty} is
        forwarded by Budget UNCHANGED (it requires no hotel fields). A SEPARATE
        idempotency_key isolates the supper checkout from the itinerary booking
        (the two sessions never mix).

        Returns a dict patch merged onto the supper result:
          ordered True:  {ordered, supper_booking_ref, supper_checkout_id, charged_cents}
          ordered False: {ordered: False, order_note, order_veto_reason?}
                         — honest veto/failure; NO booking, NO charge.
        """
        line_item = supper.get("line_item") or {}
        total = int(supper.get("total_cents") or 0)
        # The supper's OWN budget envelope (the merchant veto ceiling): the request
        # ceiling if given, else the chosen total. The else-branch is INTENTIONAL
        # zero-slack: a food line is priced (item + delivery fee) from the SAME
        # static catalog the search read, with NO date-based recompute (unlike
        # hotels' nights×rate), so the commit total equals the search total exactly
        # — there is no re-pricing gap for the ceiling to absorb (audit NIT).
        food_budget = int(max_cents) if max_cents is not None else total
        # var-0 fix (same class as _package_digest, task #49): the base
        # idempotency_key is a digest of the RAW trip request only (no supper
        # fields), so two calls that differ ONLY in supper selection (city,
        # diet, item) would otherwise collide on "{idempotency_key}-supper"
        # against a fresh checkout_id each time -- the merchant's complete_checkout
        # replay is not bound to checkout_id, so the second, different supper
        # order gets silently replaced by the first order's booking_ref/total_cents.
        # Fold the actual chosen line_item into the key so two DIFFERENT supper
        # selections never collide, while a genuine retry of the SAME selection
        # still correctly replays.
        _supper_blob = json.dumps(line_item, sort_keys=True, separators=(",", ":"), default=str)
        supper_idem = f"{idempotency_key}-supper:{hashlib.sha256(_supper_blob.encode('utf-8')).hexdigest()[:12]}"

        check_payload = {
            "user_id": user_id,
            "line_items": [line_item],
            "total_budget_cents": food_budget,
            "idempotency_key": supper_idem,
            # L12 — bind the supper checkout to the SAME simulated wallet the
            # main hotel checkout already funded/debited this run (mirrors the
            # hotel path's check_payload, which threads this same key — see
            # the two-phase CHECK payload above). Without this, complete_checkout
            # takes the no-wallet code path: `ordered:true` + a charged_cents
            # figure are reported but the simulated wallet balance never
            # actually moves, and insufficient-funds can never fire for a
            # supper order regardless of the user's real balance.
            "wallet_session_id": self._wallet_session_id,
        }
        try:
            check = self._call_budget_check(check_payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator: supper budget.check failed (ignored): %s", exc)
            return {"ordered": False,
                    "order_note": "Supper checkout unavailable; nothing was charged."}
        if check.get("decision") != "check_ok":
            return {
                "ordered": False,
                "order_note": (
                    f"Supper not ordered (budget {check.get('decision', 'veto')}); "
                    f"nothing was charged."
                ),
                "order_veto_reason": check.get("veto_reason", ""),
            }

        # L4 — thread the already-known supper total (see _do_commit's
        # `total_cents` docstring paragraph).
        commit = self._do_commit(
            user_id=user_id, checkout_id=check.get("checkout_id", ""),
            idempotency_key=supper_idem, total_cents=total,
        )
        if commit.get("decision") == "commit_failed":
            # L5 — commit_failed specifically means a RAISED commit (merchant
            # 5xx / dropped connection after the request was sent) whose own
            # documented contract (_commit_failed_result / the #26 needs_
            # reconciliation pattern) is that merchant-side state is AMBIGUOUS
            # — the supper checkout may have actually completed. The main
            # booking path (commit_plan, _run_negotiation_rounds) honestly
            # surfaces this ambiguity via needs_reconciliation; this branch
            # previously asserted the definite negative "nothing was charged"
            # uniformly for every non-accept decision, which is simply not
            # knowable for THIS specific decision. Mirror the main path's
            # honest handling instead of a flat denial.
            return {
                "ordered": False,
                "needs_reconciliation": True,
                "order_note": (
                    "Supper order status is uncertain (the merchant commit "
                    "failed to respond) — it may or may not have been charged. "
                    "Please check your trip/wallet before reordering."
                ),
                "order_veto_reason": commit.get("veto_reason", "commit_errored"),
            }
        if commit.get("decision") != "accept":
            return {
                "ordered": False,
                "order_note": (
                    f"Supper not ordered (commit {commit.get('decision', 'failed')}); "
                    f"nothing was charged."
                ),
                "order_veto_reason": commit.get("veto_reason", ""),
            }
        return {
            "ordered": True,
            "supper_booking_ref": commit.get("booking_ref"),
            "supper_checkout_id": commit.get("checkout_id") or check.get("checkout_id"),
            "charged_cents": int(commit.get("total_cents") or total),
        }

    # ------------------------------------------------------------------
    # #32 — OPT-IN LIVE restaurant reviews enrichment (OFF the money path)
    # ------------------------------------------------------------------

    # 'live, not stored' provenance — mirrors day_planner_agent._PROVENANCE /
    # booking_links provenance_source. Live Places content is surfaced in-response
    # ONLY; it is NEVER written to poi_catalog.json or any cache (Places ToS).
    _DINING_LIVE_PROVENANCE = (
        "live provider lookup (not stored; ratings/reviews fetched at request time)"
    )

    @staticmethod
    def _dining_plan_venues(day_plans: list) -> list[dict]:
        """Collect the DETERMINISTIC plan's chosen restaurants (name + lat/lon),
        de-duplicated, in plan order. These are the ONLY venues the live layer may
        reconcile a provider rating onto (HONESTY: never invent a restaurant)."""
        venues: list[dict] = []
        seen: set = set()
        for plan in day_plans or []:
            if not isinstance(plan, dict):
                continue
            for day in plan.get("days", []) or []:
                if not isinstance(day, dict):
                    continue
                meals = day.get("meals") or {}
                for slot in ("breakfast", "lunch", "tea", "dinner", "supper"):
                    meal = meals.get(slot)
                    if not isinstance(meal, dict):
                        continue
                    name = meal.get("name")
                    if not name:
                        continue
                    key = (str(name).strip().lower(), meal.get("lat"), meal.get("lon"))
                    if key in seen:
                        continue
                    seen.add(key)
                    venues.append({
                        "name": name,
                        "name_en": meal.get("name_en"),
                        "lat": meal.get("lat"),
                        "lon": meal.get("lon"),
                        "cuisine": meal.get("cuisine"),
                    })
        return venues

    @staticmethod
    def _dining_match(plan_venue: dict, prov_venue: dict) -> bool:
        """HONESTY guard against fabrication-by-mislabel: a provider rating may be
        reconciled onto a plan venue ONLY when BOTH a name-token overlap AND a tight
        lat/lon proximity (~25 m, 0.00025 deg) hold. A loose match would silently
        attach the wrong rating to the wrong venue, so we require both."""
        def _tokens(v):
            out = set()
            for key in ("name", "name_en"):
                s = v.get(key)
                if isinstance(s, str):
                    out |= {t for t in s.strip().lower().split() if t}
            return out
        if not (_tokens(plan_venue) & _tokens(prov_venue)):
            return False
        pa_lat, pa_lon = plan_venue.get("lat"), plan_venue.get("lon")
        pb_lat, pb_lon = prov_venue.get("lat"), prov_venue.get("lon")
        if None in (pa_lat, pa_lon, pb_lat, pb_lon):
            return False
        try:
            return (abs(float(pa_lat) - float(pb_lat)) <= 0.00025
                    and abs(float(pa_lon) - float(pb_lon)) <= 0.00025)
        except (TypeError, ValueError):
            return False

    def _dining_surface_venue(self, prov_venue: dict, *, source: str,
                              matched: bool) -> dict:
        """Project a provider venue into the surfaced, honest shape. A rating is
        carried ONLY if the provider returned a numeric one; a MISSING rating →
        null (never 0, never a placeholder), mirroring _stamp_meal's pass-through
        of missing fields. Every item carries provider attribution + the
        'live, not stored' provenance and live=True."""
        rating = prov_venue.get("rating")
        rating = rating if isinstance(rating, (int, float)) and not isinstance(rating, bool) else None
        rc = prov_venue.get("review_count")
        rc = rc if isinstance(rc, int) and not isinstance(rc, bool) else None
        return {
            "name": prov_venue.get("name"),
            "rating": rating,
            "review_count": rc,
            "attribution": source,
            "live": True,
            "external": not matched,  # provider-only venue (not in the det. plan)
            "provenance": self._DINING_LIVE_PROVENANCE,
        }

    def _maybe_enrich_dining(self, result: dict) -> None:
        """
        #32 — OPT-IN LIVE restaurant reviews/ratings OVER the deterministic meal
        plan. APPEND-ONLY / var-0: when the trip_request omits `dining` (or its
        gate is not met) NO key is added → byte-identical to today. NEVER mutates
        result['day_plans'] (the deterministic core stays var-0); the live layer
        lands in a SEPARATE top-level key result['dining_reviews'].

        Gate (mirrors _maybe_order_supper's two guards):
          - self._dining_request must be a dict with truthy `reviews` AND a
            `cuisine` pref (the user-prompted interactive consent);
          - the trip must have BOOKED (outcome == success) — you only suggest
            dining for a trip that happened.

        HONESTY / fail-conservative:
          - The provider seam defaults None (keys pending) → an honest
            'live reviews unavailable (no provider configured)' note, NEVER a
            fabricated rating/review.
          - A rating is surfaced ONLY if the provider returned one; missing →
            null. A provider rating is reconciled onto a deterministic venue ONLY
            on a name-token + lat/lon match; provider-only venues are labelled
            external/live and never injected into day_plans.
          - No live content is ever persisted (Places ToS).
        """
        req = self._dining_request
        if not isinstance(req, dict) or not req:
            return  # not opted in → byte-identical to today
        if not req.get("reviews"):
            return  # opt-in present but reviews not requested → no-op
        cuisine = req.get("cuisine")
        if not (isinstance(cuisine, str) and cuisine.strip()):
            return  # cuisine pref is the interactive-consent gate → no-op
        if result.get("outcome") != "success":
            return  # never suggest dining for a trip that didn't book
        cuisine = cuisine.strip()

        out: list[dict] = []
        for plan in result.get("day_plans", []) or []:
            if not isinstance(plan, dict):
                continue
            leg_id = plan.get("leg_id")
            city = plan.get("city") or ""
            iso2 = (plan.get("iso2") or "").strip().upper()
            plan_venues = self._dining_plan_venues([plan])
            # AMap-first inside China, Google-fallback elsewhere (booking_links CN
            # seam concept). The actual provider source is whatever the seam
            # returns; this is only the PREFERRED dispatch hint.
            provider = "amap" if iso2 == "CN" else "google"
            query = {
                "provider": provider, "city": city, "iso2": iso2,
                "cuisine": cuisine,
                "venues": [{"name": v["name"], "lat": v["lat"], "lon": v["lon"]}
                           for v in plan_venues],
            }
            resp = self._call_dining_reviews(query)
            if not isinstance(resp, dict):
                # Provider unconfigured / failed → honest note, NEVER fabricate.
                out.append({
                    "leg_id": leg_id, "city": city, "cuisine": cuisine,
                    "provider": None, "venues": [],
                    "note": "live reviews unavailable (no provider configured)",
                })
                continue
            source = resp.get("source") or provider
            prov_venues = resp.get("venues") or []
            surfaced: list[dict] = []
            for pv in prov_venues:
                if not isinstance(pv, dict):
                    continue
                matched = any(self._dining_match(plv, pv) for plv in plan_venues)
                surfaced.append(self._dining_surface_venue(
                    pv, source=source, matched=matched))
            entry = {
                "leg_id": leg_id, "city": city, "cuisine": cuisine,
                "provider": source, "venues": surfaced,
            }
            if not surfaced:
                entry["note"] = "no live reviews returned for this cuisine"
            out.append(entry)

        # APPEND-ONLY: attach the SEPARATE top-level key only when there is
        # something to surface (a trip with no day_plans adds nothing).
        if out:
            result["dining_reviews"] = out

    # ------------------------------------------------------------------
    # #51 — OPT-IN LIVE active-emergency overlay (FIREWALLED off the var-0 path)
    # ------------------------------------------------------------------

    def _maybe_check_active_emergencies(self, result: dict) -> None:
        """
        #51 — OPT-IN LIVE active-emergency overlay. The seasonal risk model is a
        frozen climatology → ADVISORY. A DECLARED active emergency (wildfire/flood/
        cyclone evacuation) is a live, time-varying fact that CANNOT live on the
        var-0 deterministic path, so it is firewalled behind a provider seam.

        APPEND-ONLY / var-0: when trip_request omits `live_emergency` (or its `check`
        gate is falsy) NO key is added → byte-identical to today. NEVER mutates the
        deterministic risk rollup, day_plans, or any avoid window; the overlay lands
        in a SEPARATE top-level key result['active_emergencies'].

        HONESTY / fail-conservative:
          - No feed configured / feed error → an honest 'live emergency status
            unavailable' note per leg — NEVER a fabricated all-clear (silence is not
            safety).
          - An active declaration → a 'do not travel' escalation carrying the feed's
            hazard/severity/headline/advice/source verbatim (adhere to official
            evacuation guidance), never an LLM-invented emergency.
        """
        req = self._emergency_request
        if not isinstance(req, dict) or not req:
            return  # not opted in → byte-identical to today
        if not req.get("check"):
            return  # opt-in present but check not requested → no-op

        # The day-planner's leg_plans do NOT carry the trip dates, but result["legs"] do
        # (keyed by the same canonical "leg-{i}" index). Without them the per-leg query had
        # blank checkin/checkout, so the feed's window filter fell open and surfaced a storm
        # whose track never overlaps the trip (the out-of-season do-not-travel bug). Build a
        # leg_id → (checkin, checkout) map so the feed can date-filter correctly.
        leg_dates: dict[str, tuple] = {}
        for i, lg in enumerate(result.get("legs", []) or []):
            if isinstance(lg, dict):
                leg_dates[lg.get("leg_id") or f"leg-{i}"] = (
                    lg.get("checkin", ""), lg.get("checkout", ""))

        # F1 (2026-07-06 adversarial audit): build every leg's query FIRST, then fetch
        # the feed ONCE for the whole trip (_call_emergency_feed_batch) instead of once
        # PER LEG — an N-leg trip against the real network client previously fired N
        # independent round-trips (each with its own retry) for the identical feed.
        plans = [p for p in (result.get("day_plans", []) or []) if isinstance(p, dict)]
        queries: list[dict] = []
        for plan in plans:
            leg_id = plan.get("leg_id")
            city = plan.get("city") or ""
            iso2 = (plan.get("iso2") or "").strip().upper()
            ci, co = leg_dates.get(leg_id, ("", ""))
            queries.append({"city": city, "iso2": iso2, "region": plan.get("region"),
                            "checkin": plan.get("checkin") or ci,
                            "checkout": plan.get("checkout") or co})
        responses = self._call_emergency_feed_batch(queries)

        out: list[dict] = []
        for plan, resp in zip(plans, responses):
            leg_id = plan.get("leg_id")
            city = plan.get("city") or ""
            if not isinstance(resp, dict):
                # Feed unconfigured / failed → honest note, NEVER a fabricated all-clear.
                out.append({
                    "leg_id": leg_id, "city": city, "status": "unavailable",
                    "note": "live emergency status unavailable (no feed configured)",
                })
                continue
            if resp.get("active"):
                out.append({
                    "leg_id": leg_id, "city": city, "status": "active",
                    "hazard": resp.get("hazard"),
                    "severity": resp.get("severity"),
                    "headline": resp.get("headline"),
                    "advice": resp.get("advice")
                        or "Do not travel; adhere to official evacuation guidance.",
                    "source": resp.get("source"),
                    "as_of": resp.get("as_of"),
                    "notice": "DO NOT TRAVEL — active emergency declared for this leg.",
                })
            elif resp.get("monitoring") or resp.get("severity") == "monitoring":
                # Green-level storm being TRACKED near the leg — surfaced for awareness,
                # NOT a do-not-travel (distinct from 'active'). Honest low-severity tier.
                out.append({
                    "leg_id": leg_id, "city": city, "status": "monitoring",
                    "hazard": resp.get("hazard"),
                    "severity": resp.get("severity") or "monitoring",
                    "headline": resp.get("headline"),
                    "advice": resp.get("advice")
                        or "Storm being tracked — monitor official advisories.",
                    "source": resp.get("source"),
                    "as_of": resp.get("as_of"),
                    "note": "Storm being tracked near this leg — monitor official advisories "
                            "(not a do-not-travel).",
                })
            else:
                out.append({
                    "leg_id": leg_id, "city": city, "status": "clear",
                    "source": resp.get("source"), "as_of": resp.get("as_of"),
                    "note": "No active emergency reported for this leg at check time.",
                })

        if out:
            # Dedup by country: a multi-leg same-country trip (e.g. Tokyo/Osaka/Sapporo,
            # all JP) otherwise repeats identical active_emergencies entries. Each leg was
            # still queried with ITS OWN dates above (so a date-specific risk on any leg is
            # preserved); here we just collapse the display to ONE entry per iso2, keeping
            # the highest-severity status. iso2 missing → fall back to leg_id (stay distinct).
            leg_iso2 = {
                plan.get("leg_id"): (plan.get("iso2") or "").strip().upper()
                for plan in (result.get("day_plans", []) or []) if isinstance(plan, dict)
            }
            _rank = {"active": 3, "monitoring": 2, "clear": 1, "unavailable": 0}
            by_key: dict = {}
            order: list = []
            for e in out:
                key = leg_iso2.get(e.get("leg_id")) or e.get("leg_id")
                if key not in by_key:
                    by_key[key] = e
                    order.append(key)
                elif _rank.get(e.get("status"), -1) > _rank.get(by_key[key].get("status"), -1):
                    by_key[key] = e
            result["active_emergencies"] = [by_key[k] for k in order]

    @staticmethod
    def _inject_fees(result: dict, fees: list[dict]) -> None:
        """
        FEE-INJECTION: append gate-produced fee line items (visa/vaccine/premium)
        to the result's package view via the Phase-0 LineItemAssembler so they are
        deterministically ordered + idempotent (re-emission never double-charges).
        Lodging stays the merchant's authoritative line; fees are surfaced as the
        ``fee_line_items`` block + folded into ``package_total_with_fees_cents``.
        """
        if not fees:
            return
        from utils.line_item_assembler import LineItemAssembler
        from core.cost_basis import BASIS_DETERMINISTIC_ESTIMATE
        a = LineItemAssembler()
        existing = result.get("fee_line_items") or []
        for li in existing + fees:
            money = li.get("money")
            if money is None:
                continue
            a.add_fee(
                source=li.get("source", "society"),
                kind=li.get("kind"),
                leg=li.get("leg", "_package"),
                money=money,
                description=li.get("description", ""),
                # #42 PART A — fees are a deterministic estimate from seeded data
                # (not a live quote). Carry the basis onto the assembled entry.
                basis=li.get("basis", BASIS_DETERMINISTIC_ESTIMATE),
            )
        assembled = a.assemble()
        result["fee_line_items"] = assembled
        fee_total = a.total_usd_cents()
        result["fee_total_cents"] = fee_total
        result["package_total_with_fees_cents"] = int(result.get("total_cents") or 0) + fee_total

    @staticmethod
    def _build_optional_health_estimate(health_verdict: dict, total_budget_cents: int | None = None) -> dict | None:
        """#70: the vaccines EXCLUDED from the enforced budget — CDC-recommended/situational
        jabs + any entry-required cert the traveler ALREADY HOLDS (cost 0). Surfaced separately
        (not_in_budget) so the traveler sees them, but they NEVER count toward the budget veto or
        the 'within budget' decision. Returns None when there's nothing optional/held to show."""
        if not isinstance(health_verdict, dict):
            return None
        opt = health_verdict.get("optional_vaccine_estimate") or {}
        held = health_verdict.get("held_certs") or []
        if not opt and not held:
            return None
        budget_str = f"${total_budget_cents / 100:.0f}" if total_budget_cents else "your"
        return {
            "not_in_budget": True,
            "vaccine_cost_usd_cents": int(opt.get("total_usd_cents", 0) or 0),
            "recommended_items": opt.get("items", []),
            "held_certs": list(held),
            "label": (
                f"Recommended / already-held vaccines — NOT included in {budget_str} budget "
                f"(optional health add-on; held certs cost $0)"
            ),
        }

    def _gate_block_result(
        self,
        *,
        gate: str,
        verdict: dict,
        negotiation_log: list[dict],
        gate_fees: list[dict],
        risk_assessment: dict | None,
    ) -> dict:
        """
        Build an honest cannot_satisfy result from a blocking Health/Compliance
        gate verdict (the society never returns an unbookable-in-time itinerary).
        Surfaces the gate verdict + re-sequence + any fees, then attaches Risk
        signals (additive). Routed THROUGH negotiate → composed, not a direct call.
        """
        blocking = verdict.get("blocking_legs") or verdict.get("blocking") or []
        reason = (
            f"{gate.capitalize()} gate BLOCKED the trip (unbookable in time): "
            f"verdict={verdict.get('verdict')}; blocking={blocking}."
        )
        negotiation_log.append({
            "round": 0,
            "action": f"{gate}_gate_block",
            "gate": gate,
            "verdict": verdict.get("verdict"),
            "blocking": blocking,
        })
        result = self._cannot_satisfy_result(
            reason=reason,
            closest_total=0,
            negotiation_log=negotiation_log,
        )
        result[f"{gate}_verdict"] = verdict
        if gate == "health":
            _ohe = self._build_optional_health_estimate(verdict)
            if _ohe:
                result["optional_health_estimate"] = _ohe
        if verdict.get("resequence"):
            result["resequence"] = verdict["resequence"]
        if gate_fees:
            self._inject_fees(result, gate_fees)
        return self._attach_risk_signals(result, risk_assessment)

    def _do_not_recommend_block_result(
        self,
        *,
        declined_countries: list[str],
        trip_id: str,
    ) -> dict:
        """
        Honest, non-bookable TERMINAL for the armed-conflict DO-NOT-RECOMMEND
        gate (contracts.DO_NOT_RECOMMEND_COUNTRIES). DISTINCT from an L3/L4 flag:
        the trip is DECLINED, not flagged-and-booked.

        var-0: `declined_countries` is already sorted+deduped by the caller; the
        message is a deterministic function of that list. fail-conservative:
        bookable=False, no fabricated package.

        Routed through `_cannot_satisfy_result` so the terminal shape (outcome /
        reason / negotiation_log) stays consistent with the other gate blocks.
        """
        countries_str = ", ".join(declined_countries)
        advisory = (
            "DO-NOT-TRAVEL (active armed conflict). This destination is on the "
            "armed-conflict DO-NOT-RECOMMEND set, so the trip is DECLINED rather "
            "than booked. Travel insurance is NOT available: the EXC-WAR-2 war "
            "exclusion voids coverage for war/armed conflict, leaving the trip "
            f"uninsurable. Blocked country(ies): {countries_str}."
        )
        reason = (
            "DO-NOT-RECOMMEND gate DECLINED the trip (armed conflict → "
            f"uninsurable under EXC-WAR-2): {countries_str}."
        )
        negotiation_log: list[dict[str, Any]] = [{
            "round": 0,
            "action": "do_not_recommend_decline",
            "gate": "do_not_recommend",
            "declined_countries": declined_countries,
            "war_exclusion": "EXC-WAR-2",
        }]
        result = self._cannot_satisfy_result(
            reason=reason,
            closest_total=0,
            negotiation_log=negotiation_log,
        )
        # Explicit honest-terminal markers (distinct from a bookable L3/L4 flag).
        result["outcome"] = "declined"
        result["bookable"] = False
        result["declined"] = True
        result["trip_id"] = trip_id
        result["declined_countries"] = declined_countries
        result["advisory"] = advisory
        result["advisory_level"] = "DO_NOT_TRAVEL"
        result["war_exclusion"] = "EXC-WAR-2"
        result["insurance_available"] = False
        return result

    def _fraud_block_result(
        self,
        *,
        fraud_verdict: dict,
        negotiation_log: list[dict],
        gate_fees: list[dict],
        risk_assessment: dict | None,
    ) -> dict:
        """
        Build an honest cannot_satisfy result when the Fraud advisory gate finds a
        blocked/unknown counterparty with no committable alternative + no fresh
        consent. Routed THROUGH negotiate (composed). The live Critic re-check
        (gate #2) remains the commit-time backstop.
        """
        rollup = fraud_verdict.get("rollup", {})
        reason = (
            "Fraud gate BLOCKED commit: counterparty solvency — "
            f"blocked={rollup.get('blocked_ids')}; "
            f"requires_consent={rollup.get('requires_consent_ids')}. "
            "Never commit a blocked/unknown counterparty without explicit fresh consent."
        )
        negotiation_log.append({
            "round": 0,
            "action": "fraud_gate_block",
            "gate": "fraud",
            "blocked_ids": rollup.get("blocked_ids"),
            "requires_consent_ids": rollup.get("requires_consent_ids"),
        })
        result = self._cannot_satisfy_result(
            reason=reason,
            closest_total=0,
            negotiation_log=negotiation_log,
        )
        result["fraud_verdict"] = fraud_verdict
        if gate_fees:
            self._inject_fees(result, gate_fees)
        return self._attach_risk_signals(result, risk_assessment)

    # ------------------------------------------------------------------
    # NEW DP-first negotiation path (§2.1 primary)
    # ------------------------------------------------------------------

    def _negotiate_dp(
        self,
        *,
        trip_request: dict,
        user_id: str,
        total_budget_cents: int,
        effective_ceiling: int,
        idempotency_key: str,
        negotiation_log: list[dict[str, Any]],
        target_areas: dict[str, list[str]],
        area_stage: dict[str, int],
        risk_assessment: dict | None = None,
        gate_fees: list[dict] | None = None,
        lodging_budget_cents: int | None = None,
    ) -> dict:
        """
        DP-first negotiation flow (§2.1):

        1. Gather ALL candidates per leg (Accommodation with max=total_budget_cents).
        2. Call Planner with candidates → DP picks globally optimal combo.
        3. Use DP selections as initial proposals.
        4. Budget veto + re-plan loop (robustness/legitimacy layer, unchanged).

        Risk→DP consumption (CONTEXTUAL): when ``risk_assessment`` carries a
        condition (avoid window / connection-buffer signal), it is consumed
        ADDITIVELY into the Planner payload so the DP buffers/avoids. None /
        no-condition → byte-identical (S1–S5 unchanged).
        """
        legs_input: list[dict] = trip_request.get("legs", [])

        # ------------------------------------------------------------------
        # R0b: Gather full candidate sets per leg for DP
        # ------------------------------------------------------------------
        per_leg_candidates: list[dict[str, Any]] = []
        any_leg_has_candidates = True
        failed_legs: list[tuple[str, str]] = []  # #70: (leg_id, city) of legs with no candidates

        # #budget-tier-fix: resolve the lodging price tier ONCE from the user's stated
        # budget style ("mid budget" -> 'mid') so over-tier palaces are dropped before
        # the DP maximises quality-under-budget and books the priciest hotel that merely
        # "fits". The tier's per-night cap is tightened by the implied per-night budget.
        _budget_tier = _resolve_budget_tier(trip_request, total_budget_cents)
        _tier_per_night_cap: int | None = None
        if _budget_tier:
            _total_nights = 0
            for _lg in legs_input:
                try:
                    _di = date.fromisoformat((_lg.get("checkin") or "").strip())
                    _do = date.fromisoformat((_lg.get("checkout") or "").strip())
                    _total_nights += max((_do - _di).days, 0)
                except (ValueError, TypeError):
                    continue
            _tier_per_night_cap = _resolve_per_night_cap(
                _budget_tier, total_budget_cents, _total_nights,
                # round-2 #budget-tier-plans-fix: when the total itself was ESTIMATED
                # from this tier (no $ ever stated), don't let a number we invented
                # from the tier turn around and tighten that same tier's cap.
                skip_budget_tighten=bool(trip_request.get("assumed_budget_from_tier")),
            )
            logger.info(
                "orchestrator: DP gather budget tier=%s per_night_cap=%s",
                _budget_tier, _tier_per_night_cap,
            )

        for i, leg in enumerate(legs_input):
            leg_id = f"leg-{i}"
            city = leg.get("city", "")
            try:
                self._tracer("agent_started", "Accommodation", trip_id=self._trip_id,
                             summary=f"leg={leg_id} city={city} gathering candidates...", data={})
            except Exception:  # noqa: BLE001
                pass
            cands = self._gather_candidates_for_dp(
                leg_id=leg_id,
                city=city,
                checkin=leg.get("checkin", ""),
                checkout=leg.get("checkout", ""),
                adults=int(leg.get("adults", 1)),
                vibe=leg.get("vibe"),
                target_areas=target_areas.get(leg_id),
                total_budget_cents=total_budget_cents,
                target_areas_dict=target_areas,
                area_stage_dict=area_stage,
                avoid_lodging_types=leg.get("avoid_lodging_types"),
                prefer_lodging_types=leg.get("prefer_lodging_types"),
                dest_country=leg.get("dest_country"),
            )
            # #budget-tier-fix: drop over-tier candidates (relax-if-empty) so the DP
            # optimises quality WITHIN the tier instead of booking the priciest fit.
            if cands and _budget_tier:
                _leg_nights = 1
                try:
                    _d_in = date.fromisoformat((leg.get("checkin") or "").strip())
                    _d_out = date.fromisoformat((leg.get("checkout") or "").strip())
                    _leg_nights = max((_d_out - _d_in).days, 1)
                except (ValueError, TypeError):
                    _leg_nights = 1
                cands = _filter_candidates_by_tier(
                    cands, _budget_tier, _leg_nights, per_night_cap=_tier_per_night_cap
                )
            try:
                self._tracer("agent_completed", "Accommodation", trip_id=self._trip_id,
                             summary=f"leg={leg_id} candidates={len(cands)} city={city}",
                             data={"leg_id": leg_id, "candidates": len(cands), "city": city})
            except Exception:  # noqa: BLE001
                pass
            if not cands:
                any_leg_has_candidates = False
                failed_legs.append((leg_id, city))
                logger.info(
                    "orchestrator: DP candidate gather — leg=%s (%s) has no candidates "
                    "(%s) — cannot_satisfy",
                    leg_id, city, self._gather_reasons.get(leg_id, "unknown"),
                )
            per_leg_candidates.append({"leg_id": leg_id, "candidates": cands})

        if not any_leg_has_candidates:
            # One or more legs have no candidates → infeasible. #70 HONESTY: attribute the
            # failure to the SPECIFIC leg(s) and DISTINGUISH the cause (no inventory in our
            # catalog vs nothing under budget vs a lookup error) instead of always blaming the
            # budget — which mis-reported unstocked cities (e.g. boracay) as "over budget".
            _cause_phrase = {
                "no_inventory": "no lodging inventory in our catalog for this destination",
                "over_budget": f"no lodging fits the budget (total {total_budget_cents}¢)",
                "search_error": "the lodging lookup failed",
            }
            parts = [
                f"{fcity or fid}: {_cause_phrase.get(self._gather_reasons.get(fid, 'no_inventory'), 'no lodging available')}"
                for fid, fcity in failed_legs
            ]
            reason = "Cannot satisfy — " + "; ".join(parts) + "."
            return self._cannot_satisfy_result(
                reason=reason,
                closest_total=0,
                negotiation_log=negotiation_log,
            )

        # ------------------------------------------------------------------
        # R0c: Call Planner with per_leg_candidates → DP allocation
        # ------------------------------------------------------------------
        planner_payload = {
            **trip_request,
            "per_leg_candidates": per_leg_candidates,
        }
        # Risk→DP consumption (CONTEXTUAL): fold the avoid/buffer signal into the
        # Planner payload so the DP can buffer connections / avoid the flagged
        # window. No-op (no key) when Risk produced no condition → S1–S5 unchanged.
        risk_directives = self._risk_planning_directives(risk_assessment)
        if risk_directives:
            planner_payload["risk_directives"] = risk_directives
            logger.info(
                "orchestrator: Risk→DP consumption — directives=%s", risk_directives,
            )
        try:
            self._tracer("agent_started", "Planner", trip_id=self._trip_id, summary="building itinerary skeleton...")
        except Exception:  # noqa: BLE001
            pass
        try:
            skeleton = self._call_planner(planner_payload)
        except RuntimeError as exc:
            # Planner raised (e.g. DP infeasibility → cannot_satisfy)
            err_str = str(exc)
            if "cannot_satisfy" in err_str.lower():
                # Part B budget guidance: DP-infeasible is ALWAYS budget-driven
                # (candidates existed but Σ cheapest > budget). Compute shortfall.
                _dp_shortfall: int | None = None
                _dp_min_feasible: int | None = None
                try:
                    from utils import budget_estimate as _be
                    _lodging_min = sum(
                        min(
                            int(c["total_cents"]) for c in ld["candidates"]
                            if c.get("total_cents")
                        )
                        for ld in per_leg_candidates
                        if ld.get("candidates")
                    )
                    _enforced_envelope = total_budget_cents - (
                        lodging_budget_cents if lodging_budget_cents is not None
                        else total_budget_cents
                    )
                    _dp_min_feasible = _lodging_min + _enforced_envelope
                    _dp_shortfall = _be.shortfall(_dp_min_feasible, total_budget_cents)
                except Exception:  # noqa: BLE001 — shortfall is guidance; never crash booking path
                    pass
                return self._cannot_satisfy_result(
                    reason=err_str,
                    closest_total=0,
                    negotiation_log=negotiation_log,
                    budget_shortfall_cents=_dp_shortfall,
                    min_feasible_total_cents=_dp_min_feasible,
                )
            raise

        legs = skeleton.get("legs", [])
        dp_used: bool = skeleton.get("dp_allocation", False)
        logger.info(
            "orchestrator: planner returned %d legs dp_used=%s: %s",
            len(legs), dp_used,
            [(l["leg_id"], l["per_leg_budget_cents"]) for l in legs],
        )
        # Tracer: planner agent_completed (side-channel only)
        try:
            self._tracer(
                "agent_completed",
                "Planner",
                trip_id=self._trip_id,
                summary=f"legs={len(legs)} dp_used={dp_used}",
                data={"leg_count": len(legs), "dp_used": dp_used,
                      "ceilings": {l["leg_id"]: l["per_leg_budget_cents"] for l in legs}},
            )
        except Exception:  # noqa: BLE001
            pass

        # Working ceilings — mutable across rounds
        ceilings: dict[str, int] = {
            leg["leg_id"]: leg["per_leg_budget_cents"]
            for leg in legs
        }
        # Proposal state per leg (hotel_id, total_cents, etc.)
        proposals: dict[str, dict | None] = {leg["leg_id"]: None for leg in legs}
        # Map leg_id → leg metadata (city, checkin, checkout, adults)
        leg_meta: dict[str, dict] = {leg["leg_id"]: leg for leg in legs}
        # #94: leg_meta comes from the Planner skeleton, which does NOT carry
        # dest_country — backfill it from self._trip_request_legs so
        # _propose_with_area_ladder's country filter (#93) and
        # _primary_dest_token's booking_ref token (#94) both see it.
        self._enrich_leg_meta_dest_country(leg_meta)

        # ------------------------------------------------------------------
        # R0d: Use DP-selected hotels as initial proposals
        # The DP already picked the globally optimal hotel per leg; we use
        # these as the starting proposals directly.  No greedy per-leg loop needed.
        # ------------------------------------------------------------------
        if dp_used:
            # Build a lookup from candidate lists so we can retrieve full candidate info
            cands_by_leg: dict[str, dict[str, dict]] = {}
            for leg_cands in per_leg_candidates:
                lid = leg_cands["leg_id"]
                cands_by_leg[lid] = {
                    c["hotel_id"]: c for c in leg_cands["candidates"]
                }

            for leg in legs:
                leg_id = leg["leg_id"]
                dp_hotel_id = leg.get("dp_selected_hotel_id")
                dp_total = leg.get("dp_selected_total_cents")

                if dp_hotel_id and dp_total is not None:
                    # Retrieve full candidate info for this DP selection
                    full_cand = cands_by_leg.get(leg_id, {}).get(dp_hotel_id, {})
                    proposals[leg_id] = {
                        "hotel_id": dp_hotel_id,
                        "total_cents": dp_total,
                        "review_score": float(full_cand.get("review_score", 0)),
                        "star_rating": float(full_cand.get("star_rating", 0)),
                        "amenities": full_cand.get("amenities") or [],
                        "title": full_cand.get("title", dp_hotel_id),
                        "area": full_cand.get("area") or "",
                        "ranking_source": full_cand.get("ranking_source", "dp"),
                        "provenance": "merchant",
                        # HONESTY: re-attach the unverified-lodging warning by hotel_id (the DP rebuild
                        # above drops it; the side-channel survives every re-projection).
                        **({"unverified_lodging": True,
                            "note": self._unverified_lodging[str(dp_hotel_id)]}
                           if str(dp_hotel_id) in self._unverified_lodging else {}),
                    }
                    logger.info(
                        "orchestrator: DP initial proposal leg=%s → %s total=%d¢",
                        leg_id, dp_hotel_id, dp_total,
                    )
                else:
                    # DP did not select a hotel for this leg (fallback path)
                    proposals[leg_id] = None

            # For any legs without DP selection, fall back to greedy
            for leg in legs:
                leg_id = leg["leg_id"]
                if proposals[leg_id] is None:
                    acc_result = self._propose_with_area_ladder(
                        leg_meta=leg_meta,
                        target_areas=target_areas,
                        area_stage=area_stage,
                        leg_id=leg_id,
                        max_cents=ceilings[leg_id],
                    )
                    if acc_result.get("fit") == "ok":
                        proposals[leg_id] = acc_result["proposal"]
                        logger.info(
                            "orchestrator: fallback greedy proposal leg=%s → %s total=%d¢",
                            leg_id,
                            acc_result["proposal"]["hotel_id"],
                            acc_result["proposal"]["total_cents"],
                        )
        else:
            # DP not used (no candidates or disabled) — greedy initial proposals
            for leg in legs:
                leg_id = leg["leg_id"]
                acc_result = self._propose_with_area_ladder(
                    leg_meta=leg_meta,
                    target_areas=target_areas,
                    area_stage=area_stage,
                    leg_id=leg_id,
                    max_cents=ceilings[leg_id],
                )
                if acc_result.get("fit") == "ok":
                    proposals[leg_id] = acc_result["proposal"]
                    logger.info(
                        "orchestrator: greedy initial proposal leg=%s → %s total=%d¢",
                        leg_id,
                        acc_result["proposal"]["hotel_id"],
                        acc_result["proposal"]["total_cents"],
                    )
                else:
                    proposals[leg_id] = None

        # ------------------------------------------------------------------
        # Negotiation rounds (Budget veto + re-plan loop — unchanged)
        # This is the robustness/legitimacy layer: the DP optimises against the
        # catalog view; if the merchant's authoritative price differs and the
        # package 403s, re-plan fires.  The DP near-optimal first proposal means
        # re-plan RARELY fires — but the loop MUST remain.
        # ------------------------------------------------------------------
        result = self._run_negotiation_rounds(
            user_id=user_id,
            total_budget_cents=total_budget_cents,
            effective_ceiling=effective_ceiling,
            lodging_budget_cents=lodging_budget_cents,
            risk_assessment=risk_assessment,
            gate_fees=gate_fees,
            idempotency_key=idempotency_key,
            negotiation_log=negotiation_log,
            legs=legs,
            ceilings=ceilings,
            proposals=proposals,
            leg_meta=leg_meta,
            target_areas=target_areas,
            area_stage=area_stage,
            consent_tokens=trip_request.get("consent_tokens"),
            persona=(trip_request.get("persona") or "default"),
            overland_only=bool(trip_request.get("overland_only", False)),
        )

        # ------------------------------------------------------------------
        # L3-core §12.8/§12.9: compute + Critic-verify the pre-vetted SECONDARY
        # after the baseline converges.  Only done on a successful booking.
        # ------------------------------------------------------------------
        if result.get("outcome") == "success":
            result = self._attach_secondary(
                result=result,
                per_leg_candidates=per_leg_candidates,
                total_budget_cents=total_budget_cents,
                user_id=user_id,
                consent_tokens=trip_request.get("consent_tokens"),
            )

        return result

    # ------------------------------------------------------------------
    # LEGACY greedy negotiation path (USE_DP_ALLOCATOR=false)
    # ------------------------------------------------------------------

    def _negotiate_greedy(
        self,
        *,
        trip_request: dict,
        user_id: str,
        total_budget_cents: int,
        effective_ceiling: int,
        idempotency_key: str,
        negotiation_log: list[dict[str, Any]],
        target_areas: dict[str, list[str]],
        area_stage: dict[str, int],
        risk_assessment: dict | None = None,
        gate_fees: list[dict] | None = None,
        lodging_budget_cents: int | None = None,
    ) -> dict:
        """
        Legacy greedy negotiation flow (backward-compat when USE_DP_ALLOCATOR=false).
        Proportional-split Planner → greedy per-leg proposals → Budget veto + re-plan.
        Identical to the original negotiate() implementation.

        Risk→greedy consumption (CONTEXTUAL): when ``risk_assessment`` carries a
        condition, its avoid/buffer directives are folded into the Planner payload
        ADDITIVELY. None / no-condition → byte-identical (S1–S5 unchanged).
        """
        # R0: Planner decomposes trip → per-leg ceilings (proportional split)
        risk_directives = self._risk_planning_directives(risk_assessment)
        if risk_directives:
            planner_payload = {**trip_request, "risk_directives": risk_directives}
            logger.info(
                "orchestrator: Risk→greedy consumption — directives=%s", risk_directives,
            )
        else:
            planner_payload = trip_request
        try:
            self._tracer("agent_started", "Planner", trip_id=self._trip_id, summary="building itinerary skeleton (greedy)...")
        except Exception:  # noqa: BLE001
            pass
        skeleton = self._call_planner(planner_payload)
        legs = skeleton.get("legs", [])
        logger.info(
            "orchestrator: greedy planner returned %d legs: %s",
            len(legs),
            [(l["leg_id"], l["per_leg_budget_cents"]) for l in legs],
        )
        # Tracer: planner agent_completed (greedy path, side-channel only)
        try:
            self._tracer(
                "agent_completed",
                "Planner",
                trip_id=self._trip_id,
                summary=f"legs={len(legs)} dp_used=False (greedy path)",
                data={"leg_count": len(legs), "dp_used": False,
                      "ceilings": {l["leg_id"]: l["per_leg_budget_cents"] for l in legs}},
            )
        except Exception:  # noqa: BLE001
            pass

        ceilings: dict[str, int] = {
            leg["leg_id"]: leg["per_leg_budget_cents"]
            for leg in legs
        }
        proposals: dict[str, dict | None] = {leg["leg_id"]: None for leg in legs}
        leg_meta: dict[str, dict] = {leg["leg_id"]: leg for leg in legs}
        # #94: see the DP path's identical call above — the greedy path's
        # leg_meta comes from the same dest_country-less Planner skeleton.
        self._enrich_leg_meta_dest_country(leg_meta)

        # R1: Initial greedy accommodation proposals
        for leg in legs:
            leg_id = leg["leg_id"]
            acc_result = self._propose_with_area_ladder(
                leg_meta=leg_meta,
                target_areas=target_areas,
                area_stage=area_stage,
                leg_id=leg_id,
                max_cents=ceilings[leg_id],
            )
            if acc_result.get("fit") == "ok":
                proposals[leg_id] = acc_result["proposal"]
                logger.info(
                    "orchestrator: leg=%s initial proposal=%s total=%d¢ area=%s",
                    leg_id,
                    acc_result["proposal"]["hotel_id"],
                    acc_result["proposal"]["total_cents"],
                    acc_result["proposal"].get("area"),
                )
            else:
                proposals[leg_id] = None
                logger.info(
                    "orchestrator: leg=%s no_fit at ceiling=%d¢", leg_id, ceilings[leg_id]
                )

        return self._run_negotiation_rounds(
            user_id=user_id,
            total_budget_cents=total_budget_cents,
            effective_ceiling=effective_ceiling,
            lodging_budget_cents=lodging_budget_cents,
            risk_assessment=risk_assessment,
            gate_fees=gate_fees,
            idempotency_key=idempotency_key,
            negotiation_log=negotiation_log,
            legs=legs,
            ceilings=ceilings,
            proposals=proposals,
            leg_meta=leg_meta,
            target_areas=target_areas,
            area_stage=area_stage,
            consent_tokens=trip_request.get("consent_tokens"),
            persona=(trip_request.get("persona") or "default"),
            overland_only=bool(trip_request.get("overland_only", False)),
        )

    # ------------------------------------------------------------------
    # L3-core §12.8: Pre-vetted secondary computation + Critic verification
    # ------------------------------------------------------------------

    def _attach_secondary(
        self,
        *,
        result: dict,
        per_leg_candidates: list[dict],
        total_budget_cents: int,
        user_id: str,
        consent_tokens: dict | None = None,
    ) -> dict:
        """
        Compute the pre-vetted SECONDARY itinerary and Critic-verify it at plan time.

        Called after the baseline package converges + is booked successfully.
        The secondary is the DP next-best with the highest-cost leg's hotel excluded.

        Per §12.9 pressure-test: this is deterministic and provably in-budget when
        the baseline's pick is knocked out (same fault → same secondary, variance-0).

        Stores the secondary inside result["secondary_plan"].  On failure to find
        a valid secondary, stores result["secondary_plan"] = None (honest; the
        baseline booking is unaffected).

        Returns the (mutated) result dict.
        """
        from utils.allocator import allocate_secondary

        baseline_legs = result.get("legs", [])

        if not baseline_legs or not per_leg_candidates:
            result["secondary_plan"] = None
            logger.info("orchestrator._attach_secondary: no baseline legs or candidates")
            return result

        # Build the allocate_secondary input: baseline selection from result["legs"]
        baseline_selection = [
            {
                "leg_id": l["leg_id"],
                "hotel_id": l["hotel_id"],
                "total_cents": l["total_cents"],
                "quality": 0.0,  # quality not needed for secondary computation
            }
            for l in baseline_legs
        ]

        secondary = allocate_secondary(
            legs_with_candidates=per_leg_candidates,
            total_budget_cents=total_budget_cents,
            baseline_selection=baseline_selection,
        )

        if not secondary.get("feasible"):
            logger.info(
                "orchestrator._attach_secondary: DP secondary infeasible "
                "(affected=%s excluded=%s) — no secondary",
                secondary.get("affected_leg_id"), secondary.get("excluded_hotel_id"),
            )
            result["secondary_plan"] = None
            return result

        logger.info(
            "orchestrator._attach_secondary: secondary feasible — "
            "affected=%s excluded=%s total=%d¢ selection=%s",
            secondary.get("affected_leg_id"), secondary.get("excluded_hotel_id"),
            secondary.get("total_cents", 0),
            [(s["leg_id"], s["hotel_id"], s["total_cents"])
             for s in secondary.get("selection", [])],
        )

        # Critic-verify the secondary at plan time (§12.8).
        # Build the secondary leg list (merge original leg metadata + new hotels).
        original_leg_meta: dict[str, dict] = {l["leg_id"]: l for l in baseline_legs}
        secondary_selection_by_leg: dict[str, dict] = {
            s["leg_id"]: s for s in secondary.get("selection", [])
        }

        secondary_legs = []
        for orig_leg in baseline_legs:
            lid = orig_leg["leg_id"]
            sec_sel = secondary_selection_by_leg.get(lid)
            if sec_sel:
                secondary_legs.append({
                    "leg_id": lid,
                    "city": orig_leg.get("city", ""),
                    "area": sec_sel.get("area", orig_leg.get("area", "")),
                    "checkin": orig_leg.get("checkin", ""),
                    "checkout": orig_leg.get("checkout", ""),
                    "adults": orig_leg.get("adults", 1),
                    "hotel_id": sec_sel["hotel_id"],
                    "total_cents": sec_sel["total_cents"],
                    "provenance": "merchant",
                    # HONESTY: the recovery/secondary plan can DP-select a suspect/junk hotel too —
                    # re-stamp the unverified warning by hotel_id so a rebooked itinerary never
                    # silently books a non-hotel (same rationale as the primary leg assembly).
                    **({"unverified_lodging": True,
                        "note": self._unverified_lodging[str(sec_sel["hotel_id"])]}
                       if str(sec_sel.get("hotel_id")) in self._unverified_lodging else {}),
                })
            else:
                secondary_legs.append({
                    "leg_id": lid,
                    "city": orig_leg.get("city", ""),
                    "area": orig_leg.get("area", ""),
                    "checkin": orig_leg.get("checkin", ""),
                    "checkout": orig_leg.get("checkout", ""),
                    "adults": orig_leg.get("adults", 1),
                    "hotel_id": orig_leg.get("hotel_id", ""),
                    "total_cents": orig_leg.get("total_cents", 0),
                    "provenance": "merchant",
                    **({"unverified_lodging": True,
                        "note": self._unverified_lodging[str(orig_leg.get("hotel_id"))]}
                       if str(orig_leg.get("hotel_id")) in self._unverified_lodging else {}),
                })

        critic_payload = {
            "user_id": user_id,
            "total_budget_cents": total_budget_cents,
            "legs": secondary_legs,
            "planned_leg_count": len(secondary_legs),
        }
        # C1(b) — thread caller consent tokens into the secondary Critic check too,
        # so a VALID consent flows to the Critic on BOTH payloads (main + secondary)
        # and an invalid/garbage token never overrides a blocked counterparty.
        if consent_tokens:
            critic_payload["counterparty_consent_tokens"] = consent_tokens
        # CRITICAL — this call runs AFTER the baseline package has already been
        # committed (_do_commit) and wallet-debited (_emit_wallet_debit): it is
        # OFF the money path. An unguarded exception here (Critic down/timeout/
        # non-consumable task state) must NEVER propagate up through negotiate()
        # and be reported as cannot_satisfy on a trip that is already booked and
        # paid for. Degrade the SAME way as an explicit Critic rejection: no
        # secondary plan, baseline booking unaffected (mirrors the main-loop
        # Critic guard at the commit gate — "conservative block, no commit on
        # an unverified package" — except here there is nothing left to block).
        try:
            secondary_critic = self._call_critic(critic_payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "orchestrator._attach_secondary: Critic errored — %s "
                "— storing secondary as None (baseline booking unaffected)",
                exc,
            )
            result["secondary_plan"] = None
            return result

        if secondary_critic is not None and secondary_critic.get("decision") != "verified":
            violations = secondary_critic.get("violations", [])
            v_summary = "; ".join(
                f"{v['code']} ({v.get('leg_id','pkg')})" for v in violations
            )
            logger.warning(
                "orchestrator._attach_secondary: Critic rejected secondary — %s "
                "— storing as None (baseline booking unaffected)",
                v_summary,
            )
            result["secondary_plan"] = None
            return result

        if secondary_critic is not None:
            logger.info(
                "orchestrator._attach_secondary: Critic VERIFIED secondary "
                "quality=%.3f", secondary_critic.get("quality_score", 0.0),
            )

        # Store the verified secondary inside the result.
        result["secondary_plan"] = {
            **secondary,
            "legs": secondary_legs,
            "critic_result": secondary_critic,
        }
        result["has_secondary"] = True

        return result

    # ------------------------------------------------------------------
    # SEV-1a: COMMIT helper
    # ------------------------------------------------------------------

    def _do_commit(
        self,
        *,
        user_id: str,
        checkout_id: str,
        idempotency_key: str,
        total_cents: int | None = None,
    ) -> dict:
        """
        SEV-1a COMMIT phase: call budget.commit to complete_checkout.

        This is the SINGLE irreversible booking step.  It is called ONLY after
        the Transport gate and Critic gate have both passed.

        Passes buyer_consent=True (the single human consent, §0.2).
        Threads idempotency_key so repeat calls return the same booking_ref.

        L4 — `total_cents` (OPTIONAL, the already-known priced package/session
        total at this call site — e.g. from the prior budget.check response or
        the held plan envelope) is threaded through to budget.commit's payload
        as `total_cents` unchanged. _commit_handler (budget_agent.py) reads
        this into `session_total_cents` specifically so a requires_consent/
        requires_mandate response (which OMITS total_cents from complete_checkout
        itself — the session was never actually completed) can still surface
        the REAL amount to the consent/mandate message instead of a misleading
        $0. Every call site below already has this total available; omitting
        it (None, the default) is byte-identical to before this fix for any
        direct/test caller that doesn't pass it.
        """
        commit_payload = {
            "user_id": user_id,
            "checkout_id": checkout_id,
            "buyer_consent": True,
            "idempotency_key": idempotency_key,
        }
        if total_cents is not None:
            commit_payload["total_cents"] = int(total_cents)
        logger.info(
            "orchestrator._do_commit: checkout_id=%s idempotency_key=%s",
            checkout_id, idempotency_key,
        )
        # #26 — the COMMIT is the SINGLE irreversible booking step. A raised
        # RuntimeError here (merchant 5xx, network drop, failed-task A2A envelope)
        # must NOT propagate as an unhandled crash to the caller (the documented
        # 'never an unhandled crash' invariant) and must NOT be retried as a
        # garden-variety veto (ambiguous server-side booking state risks a
        # double-book). Convert it into a typed honest terminal carrying the
        # idempotency_key so a later reconciliation can recover the booking_ref.
        try:
            return self._call_budget_commit(commit_payload)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "orchestrator._do_commit: commit FAILED checkout_id=%s "
                "idempotency_key=%s — needs reconciliation: %s",
                checkout_id, idempotency_key, exc,
            )
            return {
                "decision": "commit_failed",
                "veto_reason": "commit_errored",
                "checkout_id": checkout_id,
                "idempotency_key": idempotency_key,
                "detail": str(exc),
            }

    # ------------------------------------------------------------------
    # #112 fix: recover legs abandoned mid re-plan pass.
    # ------------------------------------------------------------------

    def _retry_abandoned_legs(
        self,
        *,
        proposals: dict[str, Any],
        abandoned_lids: list[str],
        eff_ceiling: int,
        leg_meta: dict[str, dict],
        target_areas: dict[str, list[str]],
        area_stage: dict[str, int],
        ceilings: dict[str, int],
        legs: list,
    ) -> bool:
        """
        #112 fix (non-monotonic budget cliff): the tighten-priciest-leg re-plan
        loop above tries legs PRICIEST FIRST and `break`s on the first one that
        successfully re-fits at a tightened ceiling. Any OTHER leg that was
        tried-and-failed (set to no_fit / None) EARLIER in that same pass is
        left permanently abandoned — even though the leg that *did* succeed
        just shrank, which frees up fresh ceiling headroom for the abandoned
        leg(s). Without this retry, the very next thing that runs is the
        ALL-OR-NONE check at the top of the round loop, which sees the
        abandoned leg's `None` proposal and returns cannot_satisfy immediately
        — often well short of MAX_ROUNDS and with real headroom sitting
        unused (see the task's negotiation_log trace: leg squeezed to an
        unfittable ceiling using a STALE other-legs total, while the
        just-tightened leg's real (lower) cost would have freed enough room).

        Recomputes each abandoned leg's ceiling off the CURRENT `proposals`
        (which already reflects the just-tightened leg's real, lower cost)
        rather than the stale pre-pass snapshot, and retries
        `_propose_with_area_ladder` against that fresh headroom. Mutates
        `proposals` / `ceilings` in place. Returns True if at least one
        previously-abandoned leg now fits.
        """
        recovered = False
        for lid in abandoned_lids:
            if proposals.get(lid) is not None:
                continue  # already recovered by an earlier iteration of this retry pass
            other_costs = sum(
                p["total_cents"] for other_lid, p in proposals.items()
                if other_lid != lid and p is not None
            )
            new_max = eff_ceiling - other_costs
            if new_max <= 0:
                new_max = max(1, math.floor(eff_ceiling / max(len(legs), 1)))
            ceilings[lid] = new_max
            acc_result = self._propose_with_area_ladder(
                leg_meta=leg_meta,
                target_areas=target_areas,
                area_stage=area_stage,
                leg_id=lid,
                max_cents=new_max,
            )
            if acc_result.get("fit") == "ok":
                proposals[lid] = acc_result["proposal"]
                logger.info(
                    "orchestrator: #112 recovered abandoned leg=%s at freed "
                    "ceiling=%d¢ (headroom from a sibling leg's successful tighten)",
                    lid, new_max,
                )
                recovered = True
            else:
                logger.info(
                    "orchestrator: #112 retry did not recover abandoned leg=%s "
                    "at freed ceiling=%d¢ — still no_fit",
                    lid, new_max,
                )
        return recovered

    # ------------------------------------------------------------------
    # Shared: negotiation rounds (Budget veto + re-plan + Critic + Transport)
    # ------------------------------------------------------------------

    def _run_negotiation_rounds(
        self,
        *,
        user_id: str,
        total_budget_cents: int,
        effective_ceiling: int,
        idempotency_key: str,
        negotiation_log: list[dict[str, Any]],
        legs: list[dict],
        ceilings: dict[str, int],
        proposals: dict[str, dict | None],
        leg_meta: dict[str, dict],
        target_areas: dict[str, list[str]],
        area_stage: dict[str, int],
        consent_tokens: dict | None = None,
        lodging_budget_cents: int | None = None,
        risk_assessment: dict | None = None,
        gate_fees: list[dict] | None = None,
        persona: str = "default",
        overland_only: bool = False,
    ) -> dict:
        """
        Shared Budget veto + re-plan loop (used by both DP and greedy paths).

        SEV-1a TWO-PHASE BOOKING ORDER (the keystone fix):
          OLD (buggy): budget.enforce(buyer_consent=True) [COMMITS] → Transport → Critic
          NEW (fixed):
            1. budget.check  (create_checkout, no capture)        ← CHECK phase
            2. Transport gate (M3b)
            3. Critic gate   (M3a)
            4. budget.commit (complete_checkout, buyer_consent)   ← COMMIT (irreversible)

          A created-but-not-committed checkout from a rejected round is simply
          abandoned (never completed = no booking at the merchant).

        SEV-1b: total_budget_cents is passed to create_checkout as user_budget_cents
          so checkout.go enforces min(user_budget, BudgetHardMaxCents) at commit time.

        The loop MUST remain (legitimacy backbone, §2.1): the DP optimises
        against the catalog view; if the merchant's authoritative price differs
        → veto at CHECK → re-plan → new CHECK.  The DP near-optimal first
        proposal means re-plan RARELY fires.

        Backward-compat: when the budget agent does not support budget.check
        (e.g. legacy test stubs), the orchestrator falls back to the legacy
        budget.enforce path (buyer_consent=True in one call).  This happens when
        the budget.check call raises RuntimeError about the skill not found.
        """
        # Bug-2 fix #3 (insurance envelope false declines): `lodging_budget_cents`
        # (as passed in) reserved its insurance-premium component priced at
        # ceiling_cents=total_budget_cents (the user's FULL STATED BUDGET, an
        # upper bound taken before any proposal existed — see #72's docstring on
        # _estimate_insurance_premium_cents). That upper bound is correct but can
        # over-reserve substantially once REAL proposals exist this round,
        # artificially shrinking the ceiling sent to the merchant CHECK below what
        # the trip can actually afford — a false near-miss decline ("you're $3
        # short") even though trip + real premium genuinely fits. Recompute the
        # premium EVERY round off the REAL now-known package_total (this round's
        # proposed lodging cost) instead of the stale ceiling estimate; premium is
        # monotonic in trip cost and package_total <= total_budget_cents, so the
        # recomputed reserve is always <= the original (never loosens past a
        # genuinely-affordable total). No peril / no Risk agent → premium stays 0
        # both ways → byte-identical (var-0) for the common case.
        _gate_fee_total = sum(
            int((li.get("money") or {}).get("usd_cents", 0) or 0)
            for li in (gate_fees or [])
        )
        for round_num in range(MAX_ROUNDS + 1):
            # Legs with proposals contribute to the package
            fitted_legs = {lid: p for lid, p in proposals.items() if p is not None}
            no_fit_legs = {lid for lid, p in proposals.items() if p is None}

            # Build line_items for Budget
            line_items = []
            for lid, prop in fitted_legs.items():
                lm = leg_meta[lid]
                line_items.append({
                    "hotel_id": prop["hotel_id"],
                    "checkin": lm["checkin"],
                    "checkout": lm["checkout"],
                    "adults": lm.get("adults", 1),
                })

            package_total = sum(p["total_cents"] for p in fitted_legs.values())

            # --- ALL-OR-NONE RULE (P0 fix): if ANY leg has no_fit, do NOT proceed ---
            if no_fit_legs:
                no_fit_list = sorted(no_fit_legs)
                log_entry_partial: dict[str, Any] = {
                    "round": round_num,
                    "ceilings": dict(ceilings),
                    "proposals": {lid: (p.copy() if p else None) for lid, p in proposals.items()},
                    "no_fit_legs": no_fit_list,
                    "package_total_cents": package_total,
                    "budget_result": None,
                    "action": "cannot_satisfy_partial_legs",
                }
                negotiation_log.append(log_entry_partial)
                logger.warning(
                    "orchestrator: ALL-OR-NONE — legs %s have no_fit in round=%d; "
                    "cannot proceed with partial booking (planned=%d legs, fitted=%d legs)",
                    no_fit_list, round_num, len(legs), len(fitted_legs),
                )
                return self._cannot_satisfy_result(
                    reason=(
                        f"Partial booking rejected (all-or-none rule): "
                        f"legs {no_fit_list} have no hotel within their budget ceiling. "
                        f"Planned {len(legs)} legs; only {len(fitted_legs)} leg(s) have proposals. "
                        f"Ceilings: {dict(ceilings)}."
                    ),
                    closest_total=package_total,
                    negotiation_log=negotiation_log,
                )

            if not line_items:
                log_entry: dict[str, Any] = {
                    "round": round_num,
                    "ceilings": dict(ceilings),
                    "proposals": {lid: (p.copy() if p else None) for lid, p in proposals.items()},
                    "package_total_cents": 0,
                    "budget_result": None,
                    "action": "cannot_satisfy_no_proposals",
                }
                negotiation_log.append(log_entry)
                return self._cannot_satisfy_result(
                    reason=f"No hotels fit under any leg ceiling: {dict(ceilings)}",
                    closest_total=0,
                    negotiation_log=negotiation_log,
                )

            # ------------------------------------------------------------------
            # SEV-1a STEP 1: CHECK — create_checkout (no commit yet).
            # If the budget agent doesn't support budget.check, fall back to
            # legacy budget.enforce (buyer_consent=True).
            # ------------------------------------------------------------------
            use_two_phase = True
            check_transient_error = False  # L1 — set True only on a caught transient exc below
            # Bug-2 fix #3: re-price the insurance-premium reserve off THIS round's
            # real package_total (the real trip cost) instead of the stale
            # ceiling-priced estimate baked into `lodging_budget_cents` — see the
            # fix-#3 comment above the round loop. `package_total` (lodging only,
            # NO fees folded in) is deliberately the SAME basis _apply_insurance
            # uses post-commit (insured_cost = result["total_cents"], which never
            # includes fee_line_items — see _inject_fees), so this pre-commit
            # estimate and the real post-commit premium agree.
            if lodging_budget_cents is not None:
                _real_premium_est = self._estimate_insurance_premium_cents(
                    risk_assessment, package_total
                )
                _lodging_budget_for_check = max(
                    1, total_budget_cents - _gate_fee_total - _real_premium_est
                )
            else:
                _lodging_budget_for_check = total_budget_cents
            check_payload = {
                "user_id": user_id,
                "line_items": line_items,
                # #72: the merchant veto enforces the LODGING budget = user budget minus the
                # reserved enforced-fee envelope (visa + entry-required-not-held vaccines +
                # upper-bound insurance premium), so a trip that busts budget once fees are added
                # is 403'd PRE-commit (never booked → no stranded reservation). Falls back to the
                # full budget for any direct caller that doesn't pass it (var-0 / back-compat).
                # Bug-2 fix #3: `_lodging_budget_for_check` (recomputed every round off
                # the REAL package_total) replaces the stale, ceiling-priced
                # `lodging_budget_cents` here — see comment above.
                "total_budget_cents": _lodging_budget_for_check,  # SEV-1b
                "idempotency_key": idempotency_key,
                # SIMULATED prepaid wallet binding — the merchant persists this on the
                # session at create time so the commit-time debit / cancel-time credit
                # know which wallet. Empty → no wallet logic (back-compat).
                "wallet_session_id": self._wallet_session_id,
                # Circle Agentic Economy Prize: REAL (not simulated) USDC settlement
                # opt-in, threaded through exactly like wallet_session_id above — see
                # checkoutSession.SettlementRail in ucp-merchant/checkout.go. Empty →
                # no live settlement attempted (back-compat).
                "settlement_rail": self._settlement_rail,
            }
            try:
                try:
                    self._tracer("agent_started", "Budget", trip_id=self._trip_id,
                                 round=round_num, summary="checking budget...")
                except Exception:  # noqa: BLE001
                    pass
                check_result = self._call_budget_check(check_payload)
                check_decision = check_result.get("decision", "")
                # Tracer: budget agent_completed (side-channel only)
                try:
                    self._tracer(
                        "agent_completed",
                        "Budget",
                        trip_id=self._trip_id,
                        round=round_num,
                        summary=f"check decision={check_decision} total={package_total}¢",
                        data={"decision": check_decision, "package_total_cents": package_total,
                              "checkout_id": check_result.get("checkout_id")},
                    )
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:
                # L1 — distinguish a genuinely UNSUPPORTED budget.check skill (no
                # client/URL configured at all — _call_budget_check's own explicit
                # RuntimeError) from a TRANSIENT failure of an agent that DOES
                # support two-phase booking (network drop, merchant restart,
                # timeout, a failed/non-consumable task state). Misreporting the
                # latter as "unsupported, upgrade to v2.0.0" tells an operator to
                # upgrade an agent that is already up to date, and hides the real,
                # actionable (and often self-resolving) transient cause.
                check_unsupported = (
                    isinstance(exc, RuntimeError)
                    and "No budget client or URL configured" in str(exc)
                )
                logger.warning(
                    "orchestrator: budget.check failed (%s) — %s",
                    exc,
                    "no budget.check skill/agent configured" if check_unsupported
                    else "treating as a TRANSIENT budget-service failure "
                         "(failing closed; no legacy pre-gates fallback)",
                )
                use_two_phase = False
                check_result = None
                check_decision = ""
                check_transient_error = not check_unsupported

            if not use_two_phase:
                # Fail-closed (follow-up b SEV fix): if budget.check is unsupported,
                # we MUST NOT fall back to the pre-gates booking path (would re-introduce SEV-1a).
                # Return cannot_satisfy — the real budget agent supports two-phase.
                logger.error(
                    "orchestrator: budget.check not available — FAILING CLOSED "
                    "(refuse to book before gates)."
                )
                log_entry = {
                    "round": round_num,
                    "ceilings": dict(ceilings),
                    "proposals": {lid: (p.copy() if p else None) for lid, p in proposals.items()},
                    "package_total_cents": package_total,
                    "budget_result": None,
                    "action": (
                        "cannot_satisfy_budget_check_transient" if check_transient_error
                        else "cannot_satisfy_no_two_phase"
                    ),
                }
                negotiation_log.append(log_entry)
                return self._cannot_satisfy_result(
                    reason=(
                        "Budget service temporarily unavailable (budget.check "
                        "failed transiently). Failing conservative rather than "
                        "booking without a live budget check — please retry shortly."
                        if check_transient_error else
                        "budget.check (two-phase) not available. "
                        "Fail-closed: refusing pre-gates booking to prevent SEV-1a regression. "
                        "Upgrade budget agent to v2.0.0."
                    ),
                    closest_total=package_total,
                    negotiation_log=negotiation_log,
                )
            else:
                budget_result = check_result
                decision = "accept" if check_decision == "check_ok" else check_decision

            log_entry = {
                "round": round_num,
                "ceilings": dict(ceilings),
                "proposals": {lid: (p.copy() if p else None) for lid, p in proposals.items()},
                "no_fit_legs": sorted(no_fit_legs),  # var-0: stable order in returned log
                "package_total_cents": package_total,
                "budget_result": budget_result,
                "critic_result": None,
                "action": "",
            }

            # --- CHECK_OK / ACCEPT: budget envelope passed → Transport (M3b) → Critic (M3a) → COMMIT ---
            if decision == "accept":
                candidate_legs = []
                for lid, prop in fitted_legs.items():
                    lm = leg_meta[lid]
                    candidate_legs.append({
                        "leg_id": lid,
                        "city": lm.get("city", ""),
                        "area": prop.get("area") or lm.get("area", ""),
                        "checkin": lm.get("checkin", ""),
                        "checkout": lm.get("checkout", ""),
                        "adults": lm.get("adults", 1),
                        "hotel_id": prop.get("hotel_id", ""),
                        "total_cents": prop.get("total_cents", 0),
                        "provenance": prop.get("provenance") or "merchant",
                        **({"unverified_lodging": True,
                            "note": self._unverified_lodging[str(prop.get("hotel_id"))]}
                           if str(prop.get("hotel_id")) in self._unverified_lodging else {}),
                        # BUG3.2 (2026-07 adversarial audit, defense-in-depth): carry a
                        # leg-level counterparty_id (now threaded through by the Planner,
                        # see planner_agent.py) all the way into critic_payload["legs"]
                        # so the Critic's Gate 6b commit-time re-check — which reads
                        # leg.get("counterparty_id") — actually sees it. Without this the
                        # field was dropped here even after the Planner fix, and the
                        # Critic's re-check could never fire on it.
                        **({"counterparty_id": lm["counterparty_id"]}
                           if lm.get("counterparty_id") else {}),
                    })

                # --- M3b: Transport feasibility gate ---
                try:
                    self._tracer("agent_started", "Transport", trip_id=self._trip_id,
                                 round=round_num, summary="checking feasibility...")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    transport_result = self._call_transport(candidate_legs, persona, overland_only)
                except Exception as exc:  # noqa: BLE001
                    # M3 — a single Transport failure (network/timeout/failed task)
                    # must NOT abort the whole negotiation as a raw server_error
                    # (which leaks the internal exception string and strands the
                    # round's checkout). Fail conservative via the SAME honest
                    # terminal used for a present-but-malformed verdict below,
                    # mirroring the Critic/Budget gates' guarded-call pattern.
                    log_entry["action"] = "transport_error"
                    negotiation_log.append(log_entry)
                    logger.warning(
                        "orchestrator: TRANSPORT_ERROR round=%d — %s; conservative "
                        "block (no commit on an unverified-feasibility itinerary).",
                        round_num, exc,
                    )
                    return self._cannot_satisfy_result(
                        reason=(
                            "Transport feasibility could not be verified "
                            "(the Transport agent call failed). Failing "
                            "conservative rather than booking an "
                            "unverified-feasibility itinerary."
                        ),
                        closest_total=package_total,
                        negotiation_log=negotiation_log,
                    )
                # Tracer: transport agent_completed (side-channel only)
                try:
                    if transport_result is not None:
                        infeasible_edges = transport_result.get("infeasible_edges", [])
                        self._tracer(
                            "agent_completed",
                            "Transport",
                            trip_id=self._trip_id,
                            round=round_num,
                            summary="OK" if not infeasible_edges else f"INFEASIBLE edges={len(infeasible_edges)}",
                            data={"infeasible_edges": infeasible_edges,
                                  "edge_count": len(transport_result.get("edges", []))},
                        )
                except Exception:  # noqa: BLE001
                    pass

                if transport_result is not None:
                    # D1 (#25) — FAIL CONSERVATIVE on a present-but-malformed verdict.
                    # A transport_result that is present but does NOT carry the
                    # `infeasible_edges` key means the orchestrator could NOT actually
                    # read the feasibility verdict — it must NOT be treated as
                    # all-feasible (the old `.get(..., [])` default). Treat the missing
                    # key as a conservative feasibility failure (cannot_satisfy / flag).
                    if "infeasible_edges" not in transport_result:
                        log_entry["action"] = "transport_unverified"
                        negotiation_log.append(log_entry)
                        logger.warning(
                            "orchestrator: TRANSPORT_UNVERIFIED — transport_result "
                            "present but missing infeasible_edges; conservative block."
                        )
                        return self._cannot_satisfy_result(
                            reason=(
                                "Transport feasibility could not be verified "
                                "(malformed transport verdict missing infeasible_edges). "
                                "Failing conservative rather than booking an "
                                "unverified-feasibility itinerary."
                            ),
                            closest_total=package_total,
                            negotiation_log=negotiation_log,
                        )
                    infeasible = transport_result.get("infeasible_edges", [])
                    if infeasible:
                        suggested = transport_result.get("suggested_reordering")
                        if suggested:
                            leg_index = {leg["leg_id"]: leg for leg in candidate_legs}
                            reordered_legs = [
                                leg_index[lid] for lid in suggested if lid in leg_index
                            ]
                            reordered_ids = set(suggested)
                            for leg in candidate_legs:
                                if leg["leg_id"] not in reordered_ids:
                                    reordered_legs.append(leg)

                            # HIGH — every leg carries a FIXED checkin/checkout date;
                            # suggested_reordering is date-BLIND (transport_agent.
                            # _suggest_reordering orders purely by transfer-time/
                            # lexical centrality and NEVER touches dates). Reordering
                            # the list cannot change the REAL chronological travel
                            # sequence the user will actually experience — that is
                            # fixed by the dates. Re-running feasibility against a
                            # reorder that disagrees with the date-chronological
                            # order would silently launder a genuine infeasibility
                            # (same-day/cancelled transfer) into a false "feasible"
                            # verdict for a sequence that will never actually occur.
                            # Only trust the suggestion when it is ALSO consistent
                            # with the legs' real date order — i.e. it merely
                            # recovered legs that were submitted out of sequence.
                            date_order_ids = [
                                leg["leg_id"] for leg in sorted(
                                    candidate_legs,
                                    key=lambda l: (l.get("checkin", ""), l.get("checkout", "")),
                                )
                            ]
                            reordered_ids_seq = [leg["leg_id"] for leg in reordered_legs]
                            if reordered_ids_seq != date_order_ids:
                                logger.warning(
                                    "orchestrator: TRANSPORT suggested_reordering %s "
                                    "conflicts with the legs' real chronological "
                                    "(date) order %s — refusing to apply it; a "
                                    "date-fixed itinerary's real travel sequence "
                                    "cannot be changed by list reordering, so the "
                                    "flagged infeasibility stands.",
                                    suggested, date_order_ids,
                                )
                            else:
                                logger.info(
                                    "orchestrator: transport infeasible (%d edges) — "
                                    "suggested_reordering matches real chronological "
                                    "order, applying: %s",
                                    len(infeasible), suggested,
                                )
                                candidate_legs = reordered_legs
                                try:
                                    transport_result = self._call_transport(candidate_legs, persona, overland_only)
                                except Exception as exc:  # noqa: BLE001
                                    # M3 — same guard as the initial call above.
                                    log_entry["action"] = "transport_error"
                                    negotiation_log.append(log_entry)
                                    logger.warning(
                                        "orchestrator: TRANSPORT_ERROR (re-check after "
                                        "reorder) round=%d — %s; conservative block.",
                                        round_num, exc,
                                    )
                                    return self._cannot_satisfy_result(
                                        reason=(
                                            "Transport feasibility could not be "
                                            "re-verified after reorder (the Transport "
                                            "agent call failed). Failing conservative "
                                            "rather than booking an "
                                            "unverified-feasibility itinerary."
                                        ),
                                        closest_total=package_total,
                                        negotiation_log=negotiation_log,
                                    )
                                if transport_result is not None:
                                    infeasible = transport_result.get("infeasible_edges", [])

                        if infeasible:
                            edge_summary = "; ".join(
                                f"{e.get('from_leg','?')}→{e.get('to_leg','?')}"
                                for e in infeasible
                            )
                            log_entry["action"] = "transport_infeasible"
                            negotiation_log.append(log_entry)
                            logger.warning(
                                "orchestrator: TRANSPORT_INFEASIBLE — %d edge(s) remain "
                                "infeasible after reorder attempt: %s",
                                len(infeasible), edge_summary,
                            )
                            return self._cannot_satisfy_result(
                                reason=(
                                    f"Transport feasibility check failed after reorder attempt. "
                                    f"Infeasible transfers: {edge_summary}."
                                ),
                                closest_total=package_total,
                                negotiation_log=negotiation_log,
                            )
                        else:
                            logger.info(
                                "orchestrator: transport OK after reorder — proceeding to Critic"
                            )
                    else:
                        logger.info("orchestrator: transport OK — all edges feasible")

                # --- build #30: Day-planner (per-leg activity & meal plan) ---
                # OFF the money path: ADDITIVE day-by-day plan attached to the
                # success result. Never blocks a commit. Built from the (possibly
                # reordered) candidate_legs, enriched with iso2/country (the SAME
                # CITY_TO_ISO2 map the parser uses) + per-leg interests/dietary/
                # pace/bad_weather_days from trip_request. Guarded by try/except +
                # tracer so a day-planner failure degrades to "no plan" (the
                # itinerary is still valid without one). Bypassed (None) when no
                # Day-planner is wired → existing results are byte-identical.
                day_plan_result: dict | None = None
                # L2 — reset EVERY round this block runs (not just __init__): a
                # failure this round must not be reported as still-in-effect after
                # a later re-plan round's day-planner call succeeds, and a fresh
                # negotiate() on this long-lived orchestrator instance must not see
                # a stale flag from a PRIOR request. Distinguishes "Day-planner not
                # wired" (day_plan_result stays None, no flag) from "Day-planner IS
                # wired but the call raised" (see _success_result's honesty note).
                self._day_plan_error: str | None = None
                try:
                    # Build arrival_minutes_by_leg: intercity transfer_minutes from
                    # the transport result for each destination leg. Only feasible
                    # edges with a positive transfer_minutes are included; infeasible
                    # or cancelled edges are skipped (conservative: don't trim for
                    # legs whose transport status is uncertain).
                    arrival_minutes_by_leg, arrival_mode_by_leg = _arrival_minutes_map(transport_result)

                    activity_legs: list[dict] = []
                    for leg in candidate_legs:
                        lid = leg.get("leg_id", "")
                        city = leg.get("city", "")
                        # Per-leg planning prefs from the ORIGINAL trip_request leg
                        # (keyed by the leg-N index), defaulting to None/absent.
                        src_leg: dict = {}
                        if lid.startswith("leg-"):
                            try:
                                idx = int(lid.split("-", 1)[1])
                                if 0 <= idx < len(self._trip_request_legs):
                                    src_leg = self._trip_request_legs[idx] or {}
                            except (ValueError, IndexError):
                                src_leg = {}
                        # #70/#87: the leg's EXPLICIT dest_country is AUTHORITATIVE — it was
                        # parsed from the request the traveller actually made — and must win
                        # whenever present. CITY_TO_ISO2 is a catalog-derived GUESS from a bare
                        # city name and is only consulted as a fallback when dest_country is
                        # absent/unresolved (the #70 case: a structured trip that dropped
                        # dest_country produced ":city" keys, e.g. ":cebu", that missed the POI
                        # catalog -> empty day-plan). Getting this priority backwards is a
                        # money-path correctness bug: CITY_TO_ISO2['victoria'] == 'HK' (a Hong
                        # Kong district) silently overrode an explicit dest_country='SC'
                        # (Seychelles) request and booked a REAL Hong Kong hotel instead — see
                        # #87. dest_country may be "" (absent) or a valid ISO2; only a
                        # non-empty string counts as "present".
                        _explicit_dest_country = (src_leg.get("dest_country") or "").strip().upper()
                        iso2 = _explicit_dest_country or CITY_TO_ISO2.get(city.strip().lower())
                        activity_legs.append({
                            "leg_id": lid,
                            "city": city,
                            "iso2": iso2,
                            "country": src_leg.get("dest_country")
                            or src_leg.get("country") or "",
                            "checkin": leg.get("checkin", ""),
                            "checkout": leg.get("checkout", ""),
                            "interests": src_leg.get("interests"),
                            "interest_map": src_leg.get("interest_map"),
                            "dietary": src_leg.get("dietary"),
                            "pace": src_leg.get("pace"),
                            "dining_tier": src_leg.get("dining_tier"),
                            # #party-fix: carry the child count so the day planner can
                            # apply kid-appropriate signals (family dining bias + note).
                            "children": src_leg.get("children"),
                            "bad_weather_days": src_leg.get("bad_weather_days"),
                            # #region-fix (2026-07 adversarial audit): thread Risk's
                            # already-resolved region through so the day-planner's
                            # bad-weather contingency (derive_bad_weather_days) can
                            # actually auto-derive bad_weather_days when the caller
                            # didn't supply them explicitly above. See
                            # self._risk_region_by_leg, set in negotiate(). Defensive
                            # getattr (mirrors self._trip_request_legs's fallback
                            # elsewhere): a caller that invokes _negotiate_dp/
                            # _negotiate_greedy directly (bypassing negotiate()'s
                            # R0a-risk step, e.g. some unit tests) never set it —
                            # None is a legitimate "unresolved region" value the
                            # day-planner already treats conservatively (no bad-
                            # weather derivation), same as before this fix existed.
                            "region": getattr(self, "_risk_region_by_leg", {}).get(lid),
                            # Travel-day reservation: minutes the arrival day loses to inter-city
                            # transit (flight-only airport transfer); 0 for the origin leg.
                            "arrival_transport_minutes": _arrival_transport_minutes(
                                leg, lid, city, arrival_minutes_by_leg, arrival_mode_by_leg),
                            # #52 item 8 — an active HIGH/AVOID water-related hazard
                            # (cyclone/flood) for THIS leg, so the day-planner never
                            # recommends swimming/water sports in the same response
                            # that separately warns they may be curtailed.
                            "water_activity_hazard": self._leg_water_hazard(
                                risk_assessment, lid),
                        })
                    try:
                        self._tracer("agent_started", "DayPlanner",
                                     trip_id=self._trip_id, round=round_num,
                                     summary="planning activities & meals...")
                    except Exception:  # noqa: BLE001
                        pass
                    day_plan_result = self._call_day_planner(activity_legs)
                    try:
                        if day_plan_result is not None:
                            leg_plans = day_plan_result.get("leg_plans", [])
                            hits = sum(1 for p in leg_plans if p.get("catalog_hit"))
                            self._tracer(
                                "agent_completed", "DayPlanner",
                                trip_id=self._trip_id, round=round_num,
                                summary=f"plans={len(leg_plans)} catalog_hits={hits}",
                                data={"leg_count": len(leg_plans),
                                      "catalog_hits": hits,
                                      "leg_plans": leg_plans},
                            )
                    except Exception:  # noqa: BLE001
                        pass
                except Exception as exc:  # noqa: BLE001
                    # OFF the money path: a day-planner failure must NEVER block a
                    # commit. Degrade to "no activity plan" and continue.
                    logger.warning(
                        "orchestrator: DAY_PLANNER skipped (non-fatal): %s", exc
                    )
                    day_plan_result = None
                    # L2 — record the failure (distinct from "not configured") so
                    # _success_result can surface an honest advisory instead of a
                    # bare lodging itinerary indistinguishable from a deployment
                    # with no day-planner at all.
                    self._day_plan_error = str(exc)

                critic_payload = {
                    "user_id": user_id,
                    "total_budget_cents": total_budget_cents,
                    "legs": candidate_legs,
                    "planned_leg_count": len(legs),
                    "transport_result": transport_result,
                }
                # C1(b) — thread caller consent tokens into the Critic so a VALID
                # consent token (bound to a counterparty/catalog id == hotel_id and
                # consistent with the observed band) can override a blocked/unknown
                # counterparty at the commit-time re-check. The Critic only honors a
                # token that VALIDATES; an unrecognized/garbage token never overrides.
                if consent_tokens:
                    critic_payload["counterparty_consent_tokens"] = consent_tokens
                try:
                    self._tracer("agent_started", "Critic", trip_id=self._trip_id,
                                 round=round_num, summary="scoring itinerary quality...")
                except Exception:  # noqa: BLE001
                    pass
                # LOW — wrap the accept-branch Critic call so a Critic error degrades
                # CONSERVATIVELY to cannot_satisfy (HONESTY / never fail open at the
                # commit gate), matching the Transport/Budget gate failure handling.
                try:
                    critic_result = self._call_critic(critic_payload)
                except Exception as exc:  # noqa: BLE001
                    log_entry["action"] = "critic_error"
                    negotiation_log.append(log_entry)
                    logger.warning(
                        "orchestrator: CRITIC_ERROR round=%d — %s; conservative block "
                        "(no commit on an unverified package).",
                        round_num, exc,
                    )
                    return self._cannot_satisfy_result(
                        reason=(
                            "Critic verification could not be completed "
                            f"({exc}). Failing conservative rather than committing "
                            "an unverified itinerary."
                        ),
                        closest_total=package_total,
                        negotiation_log=negotiation_log,
                    )
                log_entry["critic_result"] = critic_result
                # Tracer: critic agent_completed (side-channel only)
                try:
                    if critic_result is not None:
                        c_decision = critic_result.get("decision", "?")
                        violations = critic_result.get("violations", [])
                        self._tracer(
                            "agent_completed",
                            "Critic",
                            trip_id=self._trip_id,
                            round=round_num,
                            summary=f"decision={c_decision} violations={len(violations)} quality={critic_result.get('quality_score', 0.0):.3f}",
                            data={"decision": c_decision,
                                  "quality_score": critic_result.get("quality_score", 0.0),
                                  "violation_count": len(violations)},
                        )
                except Exception:  # noqa: BLE001
                    pass

                if critic_result is None:
                    # No Critic configured — proceed to COMMIT (or PLAN-ONLY stop).
                    if getattr(self, "_plan_only", False):
                        # #1 CONSENT SPLIT — gates passed, funds HELD by the CHECK
                        # (budget_result == check_result). STOP before _do_commit:
                        # build the SAME success-shaped envelope from the CHECK so all
                        # downstream fee/insurance/narrative post-processing runs for
                        # field-for-field parity; negotiate() flips it to 'plan_ready'
                        # at the end. NO _do_commit, NO _emit_wallet_debit.
                        self._plan_checkout_id = budget_result.get("checkout_id")
                        self._plan_dest_token = TravelOrchestrator._primary_dest_token(
                            leg_meta, proposals)
                        log_entry["action"] = "plan_ready"
                        log_entry["budget_result"] = budget_result
                        negotiation_log.append(log_entry)
                        return self._success_result(
                            user_id=user_id,
                            total_budget_cents=total_budget_cents,
                            budget_result=budget_result,
                            proposals=proposals,
                            leg_meta=leg_meta,
                            negotiation_log=negotiation_log,
                            rounds=round_num,
                            critic_result=None,
                            transport_result=transport_result,
                            day_plan_result=day_plan_result,
                            day_plan_error=self._day_plan_error,
                            unverified_lodging=self._unverified_lodging,
                        )
                    if use_two_phase:
                        logger.info(
                            "orchestrator: no Critic, two-phase — committing checkout %s",
                            budget_result.get("checkout_id"),
                        )
                        # L4 — thread the already-known priced package total (see
                        # _do_commit's `total_cents` docstring paragraph).
                        # var-0 fix (task #49 re-plan): fold the FINAL package into
                        # the commit idempotency key (see _package_digest docstring).
                        budget_result = self._do_commit(
                            user_id=user_id,
                            checkout_id=budget_result.get("checkout_id", ""),
                            idempotency_key=f"{idempotency_key}:{_package_digest(proposals)}",
                            total_cents=package_total,
                        )
                        if budget_result.get("decision") == "commit_failed":
                            # #26 — commit RAISED. Do NOT re-plan (ambiguous booking
                            # state at the irreversible step); return an honest
                            # needs-reconciliation terminal threading idempotency_key.
                            return self._commit_failed_result(
                                budget_result=budget_result,
                                idempotency_key=idempotency_key,
                                closest_total=package_total,
                                negotiation_log=negotiation_log,
                                log_entry=log_entry,
                            )
                        if budget_result.get("decision") == "insufficient_funds":
                            # SIMULATED prepaid wallet 402 — TERMINAL (distinct from a
                            # budget veto; re-planning the priciest leg cannot help when
                            # the trip exceeds the FUNDED wallet balance).
                            return self._insufficient_funds_terminal(
                                budget_result=budget_result,
                                negotiation_log=negotiation_log,
                                log_entry=log_entry,
                            )
                        if budget_result.get("decision") in ("needs_consent", "needs_mandate"):
                            # M1 — the merchant is asking for consent/mandate, not a
                            # re-price; do NOT fall into the veto/re-plan branch below.
                            return self._needs_consent_terminal(
                                budget_result=budget_result,
                                idempotency_key=idempotency_key,
                                closest_total=package_total,
                                negotiation_log=negotiation_log,
                                log_entry=log_entry,
                            )
                        if budget_result.get("decision") in ("cannot_price", "unavailable"):
                            # M3 — a merchant verdict at COMMIT time that is a DEFINITE,
                            # never-booked outcome (session voided / item went
                            # unavailable between CHECK and COMMIT — see
                            # budget_agent._map_complete_response's "void"/"error"
                            # branches). Mirrors D3's CHECK-time handling above: this is
                            # NOT a re-price veto (nothing to tighten a leg against) and
                            # must not fall into the generic commit-time-veto/re-plan
                            # branch below, which would fabricate a price-based re-plan
                            # attempt for an item that is simply gone.
                            veto_reason = budget_result.get("veto_reason", "unavailable")
                            log_entry["action"] = f"commit_time_budget_{budget_result.get('decision')}"
                            log_entry["budget_result"] = budget_result
                            negotiation_log.append(log_entry)
                            logger.warning(
                                "orchestrator: COMMIT-TIME BUDGET %s round=%d reason=%s "
                                "— honest terminal",
                                budget_result.get("decision", "").upper(), round_num, veto_reason,
                            )
                            result = self._cannot_satisfy_result(
                                reason=(
                                    f"The merchant could not complete this booking "
                                    f"({veto_reason}) — failing conservative rather "
                                    f"than booking an unavailable selection."
                                ),
                                closest_total=package_total,
                                negotiation_log=negotiation_log,
                                day_plan_preview=day_plan_result,
                            )
                            result["idempotency_key"] = idempotency_key
                            result["checkout_id"] = budget_result.get("checkout_id", "")
                            return result
                        if budget_result.get("decision") != "accept":
                            # Commit-time veto (merchant re-priced) → treat as veto
                            decision = budget_result.get("decision", "veto")
                            veto_reason = budget_result.get("veto_reason", "unknown")
                            log_entry["action"] = "veto_received"  # commit-time veto treated as veto
                            log_entry["budget_result"] = budget_result
                            negotiation_log.append(log_entry)
                            logger.warning(
                                "orchestrator: COMMIT-TIME %s round=%d reason=%s",
                                decision.upper(), round_num, veto_reason,
                            )
                            # Treat commit-time veto the same as a check-time veto
                            # (fall through to the veto re-plan block below).
                            # We set decision and re-enter the veto handling path.
                            # Since we've already appended log_entry, continue to next round.
                            # Bug-2 fix #1 (core clamp): price_exceeds_hard_max vetoes
                            # populate hard_max_cents, NOT budget_ceiling_cents (see
                            # ucp-merchant/checkout.go) — reading only
                            # budget_ceiling_cents left effective_ceiling untightened
                            # for hard-max vetoes, so the re-plan loop kept re-pricing
                            # against the OLD too-high ceiling and exhausted MAX_ROUNDS
                            # into a false decline (books-at-low / declines-at-high).
                            # Fall back to hard_max_cents so BOTH veto shapes clamp to
                            # the real merchant-enforced ceiling.
                            merchant_ceiling = (
                                budget_result.get("budget_ceiling_cents")
                                or budget_result.get("hard_max_cents")
                            )
                            if merchant_ceiling and merchant_ceiling < effective_ceiling:
                                effective_ceiling = merchant_ceiling
                            # Re-plan: tighten the priciest leg (same as veto path)
                            fitted_by_cost = sorted(
                                fitted_legs.items(),
                                key=lambda kv: kv[1]["total_cents"],
                                reverse=True,
                            )
                            re_planned = False
                            abandoned_lids: list[str] = []
                            for target_lid, target_prop in fitted_by_cost:
                                other_costs = sum(
                                    p["total_cents"]
                                    for lid, p in fitted_legs.items()
                                    if lid != target_lid
                                )
                                new_max = effective_ceiling - other_costs
                                if new_max <= 0:
                                    new_max = max(1, math.floor(effective_ceiling / len(legs)))
                                if new_max >= target_prop["total_cents"]:
                                    continue
                                ceilings[target_lid] = new_max
                                acc_result = self._propose_with_area_ladder(
                                    leg_meta=leg_meta,
                                    target_areas=target_areas,
                                    area_stage=area_stage,
                                    leg_id=target_lid,
                                    max_cents=new_max,
                                )
                                if acc_result.get("fit") == "ok":
                                    proposals[target_lid] = acc_result["proposal"]
                                    re_planned = True
                                    break
                                else:
                                    proposals[target_lid] = None
                                    abandoned_lids.append(target_lid)
                            # #112 fix: a sibling leg's successful tighten (above) just
                            # freed real ceiling headroom (its cost dropped) — retry any
                            # leg abandoned earlier in THIS pass against that fresh
                            # headroom before falling through to the all-or-none check.
                            if re_planned and abandoned_lids:
                                self._retry_abandoned_legs(
                                    proposals=proposals,
                                    abandoned_lids=abandoned_lids,
                                    eff_ceiling=effective_ceiling,
                                    leg_meta=leg_meta,
                                    target_areas=target_areas,
                                    area_stage=area_stage,
                                    ceilings=ceilings,
                                    legs=legs,
                                )
                            # Bug-2 fix #2: surface the REAL merchant decline reason +
                            # amounts (previously discarded in favor of a generic
                            # "Cannot re-plan" message) — the same veto_reason /
                            # total_cents / ceiling values were already sitting in
                            # budget_result, just never reaching the caller.
                            _veto_detail = (
                                f" Merchant declined ({veto_reason}): priced at "
                                f"${budget_result.get('total_cents', 0) / 100:,.2f}, "
                                f"ceiling ${(merchant_ceiling or 0) / 100:,.2f}."
                            )
                            if not re_planned:
                                if round_num >= MAX_ROUNDS:
                                    return self._cannot_satisfy_result(
                                        reason=(
                                            f"Commit-time veto after {MAX_ROUNDS} rounds. "
                                            f"Cannot re-plan any leg to fit." + _veto_detail
                                        ),
                                        closest_total=package_total,
                                        negotiation_log=negotiation_log,
                                        day_plan_preview=day_plan_result,
                                    )
                            if round_num >= MAX_ROUNDS:
                                return self._cannot_satisfy_result(
                                    reason=(
                                        "Max rounds exceeded without convergence "
                                        "(commit-time veto)." + _veto_detail
                                    ),
                                    closest_total=package_total,
                                    negotiation_log=negotiation_log,
                                    day_plan_preview=day_plan_result,
                                )
                            continue

                    # Update log_entry to reflect the final committed result.
                    log_entry["budget_result"] = budget_result
                    log_entry["action"] = "accept"
                    negotiation_log.append(log_entry)
                    logger.info(
                        "orchestrator: ACCEPT (no Critic) round=%d total=%d¢ booking_ref=%s",
                        round_num, budget_result.get("total_cents", 0),
                        budget_result.get("booking_ref"),
                    )
                    # SIMULATED prepaid wallet — emit the side-channel DEBIT event.
                    self._emit_wallet_debit(budget_result)
                    return self._success_result(
                        user_id=user_id,
                        total_budget_cents=total_budget_cents,
                        budget_result=budget_result,
                        proposals=proposals,
                        leg_meta=leg_meta,
                        negotiation_log=negotiation_log,
                        rounds=round_num,
                        critic_result=None,
                        transport_result=transport_result,
                        day_plan_result=day_plan_result,
                        day_plan_error=self._day_plan_error,
                        unverified_lodging=self._unverified_lodging,
                    )

                critic_decision = critic_result.get("decision", "")
                logger.info(
                    "orchestrator: Critic decision=%s violations=%d quality=%.3f",
                    critic_decision,
                    len(critic_result.get("violations", [])),
                    critic_result.get("quality_score", 0.0),
                )

                if critic_decision == "verified":
                    # SEV-1a: COMMIT — the irreversible booking, AFTER all gates pass.
                    final_budget_result = budget_result
                    if getattr(self, "_plan_only", False):
                        # #1 CONSENT SPLIT — all gates (incl. Critic) passed, funds
                        # HELD by the CHECK. STOP before _do_commit; return the
                        # success-shaped envelope from the CHECK for parity (negotiate()
                        # flips to 'plan_ready'). NO _do_commit, NO _emit_wallet_debit.
                        self._plan_checkout_id = budget_result.get("checkout_id")
                        self._plan_dest_token = TravelOrchestrator._primary_dest_token(
                            leg_meta, proposals)
                        log_entry["action"] = "plan_ready"
                        log_entry["budget_result"] = budget_result
                        negotiation_log.append(log_entry)
                        return self._success_result(
                            user_id=user_id,
                            total_budget_cents=total_budget_cents,
                            budget_result=budget_result,
                            proposals=proposals,
                            leg_meta=leg_meta,
                            negotiation_log=negotiation_log,
                            rounds=round_num,
                            critic_result=critic_result,
                            transport_result=transport_result,
                            day_plan_result=day_plan_result,
                            day_plan_error=self._day_plan_error,
                            unverified_lodging=self._unverified_lodging,
                        )
                    if use_two_phase:
                        logger.info(
                            "orchestrator: CRITIC_VERIFIED — committing checkout %s "
                            "(round=%d, the irreversible step)",
                            budget_result.get("checkout_id"), round_num,
                        )
                        # L4 — thread the already-known priced package total (see
                        # _do_commit's `total_cents` docstring paragraph).
                        # var-0 fix (task #49 re-plan): fold the FINAL package into
                        # the commit idempotency key (see _package_digest docstring).
                        final_budget_result = self._do_commit(
                            user_id=user_id,
                            checkout_id=budget_result.get("checkout_id", ""),
                            idempotency_key=f"{idempotency_key}:{_package_digest(proposals)}",
                            total_cents=package_total,
                        )
                        if final_budget_result.get("decision") == "commit_failed":
                            # #26 — commit RAISED. Do NOT re-plan (ambiguous booking
                            # state at the irreversible step); return an honest
                            # needs-reconciliation terminal threading idempotency_key.
                            return self._commit_failed_result(
                                budget_result=final_budget_result,
                                idempotency_key=idempotency_key,
                                closest_total=package_total,
                                negotiation_log=negotiation_log,
                                log_entry=log_entry,
                            )
                        if final_budget_result.get("decision") == "insufficient_funds":
                            # SIMULATED prepaid wallet 402 — TERMINAL (see above).
                            return self._insufficient_funds_terminal(
                                budget_result=final_budget_result,
                                negotiation_log=negotiation_log,
                                log_entry=log_entry,
                            )
                        if final_budget_result.get("decision") in ("needs_consent", "needs_mandate"):
                            # M1 — the merchant is asking for consent/mandate, not a
                            # re-price; do NOT fall into the veto/re-plan branch below.
                            return self._needs_consent_terminal(
                                budget_result=final_budget_result,
                                idempotency_key=idempotency_key,
                                closest_total=package_total,
                                negotiation_log=negotiation_log,
                                log_entry=log_entry,
                            )
                        if final_budget_result.get("decision") in ("cannot_price", "unavailable"):
                            # M3 — a merchant verdict at COMMIT time that is a DEFINITE,
                            # never-booked outcome (session voided / item went
                            # unavailable between CHECK and COMMIT — see
                            # budget_agent._map_complete_response's "void"/"error"
                            # branches). Mirrors D3's CHECK-time handling and the
                            # two-phase call site above: NOT a re-price veto, must not
                            # fall into the generic commit-time-veto/re-plan branch
                            # below, which would fabricate a price-based re-plan
                            # attempt for an item that is simply gone.
                            veto_reason = final_budget_result.get("veto_reason", "unavailable")
                            log_entry["action"] = f"commit_time_budget_{final_budget_result.get('decision')}"
                            log_entry["budget_result"] = final_budget_result
                            negotiation_log.append(log_entry)
                            logger.warning(
                                "orchestrator: COMMIT-TIME BUDGET %s (after Critic verified) "
                                "round=%d reason=%s — honest terminal",
                                final_budget_result.get("decision", "").upper(), round_num, veto_reason,
                            )
                            result = self._cannot_satisfy_result(
                                reason=(
                                    f"The merchant could not complete this booking "
                                    f"({veto_reason}) — failing conservative rather "
                                    f"than booking an unavailable selection."
                                ),
                                closest_total=package_total,
                                negotiation_log=negotiation_log,
                                day_plan_preview=day_plan_result,
                            )
                            result["idempotency_key"] = idempotency_key
                            result["checkout_id"] = final_budget_result.get("checkout_id", "")
                            return result
                        if final_budget_result.get("decision") != "accept":
                            # Commit-time veto (merchant re-priced between check and commit).
                            # Treat same as a budget veto — will trigger re-plan.
                            commit_decision = final_budget_result.get("decision", "veto")
                            veto_reason = final_budget_result.get("veto_reason", "unknown")
                            log_entry["action"] = f"commit_time_{commit_decision}"
                            log_entry["budget_result"] = final_budget_result
                            negotiation_log.append(log_entry)
                            logger.warning(
                                "orchestrator: COMMIT-TIME %s (after Critic verified) "
                                "round=%d reason=%s — will re-plan",
                                commit_decision.upper(), round_num, veto_reason,
                            )
                            # Bug-2 fix #1 (core clamp) — see the mirror-image comment
                            # in the no-Critic branch above: price_exceeds_hard_max
                            # vetoes only populate hard_max_cents, not
                            # budget_ceiling_cents.
                            merchant_ceiling = (
                                final_budget_result.get("budget_ceiling_cents")
                                or final_budget_result.get("hard_max_cents")
                            )
                            if merchant_ceiling and merchant_ceiling < effective_ceiling:
                                effective_ceiling = merchant_ceiling
                            fitted_by_cost = sorted(
                                fitted_legs.items(),
                                key=lambda kv: kv[1]["total_cents"],
                                reverse=True,
                            )
                            re_planned = False
                            abandoned_lids: list[str] = []
                            for target_lid, target_prop in fitted_by_cost:
                                other_costs = sum(
                                    p["total_cents"]
                                    for lid, p in fitted_legs.items()
                                    if lid != target_lid
                                )
                                new_max = effective_ceiling - other_costs
                                if new_max <= 0:
                                    new_max = max(1, math.floor(effective_ceiling / len(legs)))
                                if new_max >= target_prop["total_cents"]:
                                    continue
                                ceilings[target_lid] = new_max
                                acc_result = self._propose_with_area_ladder(
                                    leg_meta=leg_meta,
                                    target_areas=target_areas,
                                    area_stage=area_stage,
                                    leg_id=target_lid,
                                    max_cents=new_max,
                                )
                                if acc_result.get("fit") == "ok":
                                    proposals[target_lid] = acc_result["proposal"]
                                    re_planned = True
                                    break
                                else:
                                    proposals[target_lid] = None
                                    abandoned_lids.append(target_lid)
                            # #112 fix: see the mirror-image comment in the no-Critic
                            # branch above — retry any leg abandoned earlier in THIS
                            # pass against the headroom the successful tighten just
                            # freed, before falling through to all-or-none/cannot_satisfy.
                            if re_planned and abandoned_lids:
                                self._retry_abandoned_legs(
                                    proposals=proposals,
                                    abandoned_lids=abandoned_lids,
                                    eff_ceiling=effective_ceiling,
                                    leg_meta=leg_meta,
                                    target_areas=target_areas,
                                    area_stage=area_stage,
                                    ceilings=ceilings,
                                    legs=legs,
                                )
                            if not re_planned or round_num >= MAX_ROUNDS:
                                # Bug-2 fix #2 — surface the real merchant reason +
                                # amounts instead of a generic "Cannot re-plan".
                                return self._cannot_satisfy_result(
                                    reason=(
                                        f"Commit-time veto after Critic verified, "
                                        f"round {round_num}. Cannot re-plan. "
                                        f"Merchant declined ({veto_reason}): priced at "
                                        f"${final_budget_result.get('total_cents', 0) / 100:,.2f}, "
                                        f"ceiling ${(merchant_ceiling or 0) / 100:,.2f}."
                                    ),
                                    closest_total=package_total,
                                    negotiation_log=negotiation_log,
                                    day_plan_preview=day_plan_result,
                                )
                            continue

                    # Update log_entry to reflect the final committed result.
                    log_entry["budget_result"] = final_budget_result
                    log_entry["action"] = "accept"
                    negotiation_log.append(log_entry)
                    logger.info(
                        "orchestrator: ACCEPT + CRITIC_VERIFIED round=%d total=%d¢ "
                        "booking_ref=%s quality=%.3f",
                        round_num, final_budget_result.get("total_cents", 0),
                        final_budget_result.get("booking_ref"),
                        critic_result.get("quality_score", 0.0),
                    )
                    # SIMULATED prepaid wallet — emit the side-channel DEBIT event.
                    self._emit_wallet_debit(final_budget_result)
                    return self._success_result(
                        user_id=user_id,
                        total_budget_cents=total_budget_cents,
                        budget_result=final_budget_result,
                        proposals=proposals,
                        leg_meta=leg_meta,
                        negotiation_log=negotiation_log,
                        rounds=round_num,
                        critic_result=critic_result,
                        transport_result=transport_result,
                        day_plan_result=day_plan_result,
                        day_plan_error=self._day_plan_error,
                        unverified_lodging=self._unverified_lodging,
                    )

                # Critic REJECTED
                violations = critic_result.get("violations", [])
                log_entry["action"] = "critic_rejected"
                negotiation_log.append(log_entry)
                logger.warning(
                    "orchestrator: CRITIC_REJECTED round=%d violations=%d",
                    round_num, len(violations),
                )

                if round_num >= MAX_ROUNDS:
                    return self._cannot_satisfy_result(
                        reason=(
                            f"Critic rejected itinerary after {MAX_ROUNDS} rounds: "
                            + "; ".join(_fmt_violation(v) for v in violations)
                        ),
                        closest_total=package_total,
                        negotiation_log=negotiation_log,
                        critic_result=critic_result,
                    )

                re_planned_critic = False
                acc_violation_legs = {
                    v["leg_id"]
                    for v in violations
                    if v.get("route_to") == "accommodation" and v.get("leg_id")
                }
                if acc_violation_legs:
                    # var-0 (#15): iterate in a DETERMINISTIC order. Each
                    # _propose_with_area_ladder mutates shared area_stage and consumes
                    # the next merchant response, so set-iteration (hash-seed) order
                    # would yield a different final booked package across reruns.
                    # sorted(...) matches the existing no_fit_legs pattern.
                    for target_lid in sorted(acc_violation_legs):
                        acc_result = self._propose_with_area_ladder(
                            leg_meta=leg_meta,
                            target_areas=target_areas,
                            area_stage=area_stage,
                            leg_id=target_lid,
                            max_cents=ceilings.get(target_lid, 0),
                        )
                        if acc_result.get("fit") == "ok":
                            proposals[target_lid] = acc_result["proposal"]
                            logger.info(
                                "orchestrator: critic re-plan (accommodation) leg=%s → %s %d¢",
                                target_lid,
                                acc_result["proposal"]["hotel_id"],
                                acc_result["proposal"]["total_cents"],
                            )
                            re_planned_critic = True
                        else:
                            proposals[target_lid] = None

                if not re_planned_critic:
                    reverified_total = critic_result.get("reverified_total_cents", package_total)
                    if reverified_total > total_budget_cents:
                        new_eff_ceiling = total_budget_cents
                        fitted_by_cost = sorted(
                            fitted_legs.items(),
                            key=lambda kv: kv[1]["total_cents"],
                            reverse=True,
                        )
                        abandoned_lids: list[str] = []
                        for target_lid, target_prop in fitted_by_cost:
                            other_costs = sum(
                                p["total_cents"]
                                for lid, p in fitted_legs.items()
                                if lid != target_lid
                            )
                            new_max = new_eff_ceiling - other_costs
                            if new_max <= 0:
                                new_max = max(1, math.floor(new_eff_ceiling / max(len(legs), 1)))
                            if new_max >= target_prop["total_cents"]:
                                continue
                            ceilings[target_lid] = new_max
                            acc_result = self._propose_with_area_ladder(
                                leg_meta=leg_meta,
                                target_areas=target_areas,
                                area_stage=area_stage,
                                leg_id=target_lid,
                                max_cents=new_max,
                            )
                            if acc_result.get("fit") == "ok":
                                proposals[target_lid] = acc_result["proposal"]
                                re_planned_critic = True
                                break
                            else:
                                proposals[target_lid] = None
                                abandoned_lids.append(target_lid)
                        # #112 fix: retry any leg abandoned earlier in THIS pass
                        # against the headroom the successful tighten just freed.
                        if re_planned_critic and abandoned_lids:
                            self._retry_abandoned_legs(
                                proposals=proposals,
                                abandoned_lids=abandoned_lids,
                                eff_ceiling=new_eff_ceiling,
                                leg_meta=leg_meta,
                                target_areas=target_areas,
                                area_stage=area_stage,
                                ceilings=ceilings,
                                legs=legs,
                            )

                if not re_planned_critic:
                    return self._cannot_satisfy_result(
                        reason=(
                            "Critic rejected itinerary and no re-plan found. "
                            "Violations: "
                            + "; ".join(_fmt_violation(v) for v in violations)
                        ),
                        closest_total=package_total,
                        negotiation_log=negotiation_log,
                        critic_result=critic_result,
                    )

                continue

            # --- VETO: over budget → negotiate ---
            if decision == "veto":
                veto_reason = budget_result.get("veto_reason", "unknown")
                log_entry["action"] = "veto_received"
                negotiation_log.append(log_entry)
                merchant_ceiling = budget_result.get("budget_ceiling_cents")
                if merchant_ceiling and merchant_ceiling < effective_ceiling:
                    effective_ceiling = merchant_ceiling
                    logger.info(
                        "orchestrator: merchant ceiling learned = %d¢ (was %d¢)",
                        effective_ceiling, total_budget_cents,
                    )
                logger.warning(
                    "orchestrator: VETO round=%d reason=%s total=%d¢ ceiling=%s",
                    round_num, veto_reason,
                    budget_result.get("total_cents", 0),
                    effective_ceiling,
                )

                if round_num >= MAX_ROUNDS:
                    return self._cannot_satisfy_result(
                        reason=(
                            f"Your ${total_budget_cents / 100:.0f} budget couldn't fit the trip "
                            f"after {MAX_ROUNDS} negotiation rounds. "
                            f"The closest option found costs ~${package_total / 100:.0f}. "
                            f"Try raising your budget or shortening the trip."
                        ),
                        closest_total=package_total,
                        negotiation_log=negotiation_log,
                    )

                # Re-plan: tighten the priciest leg
                fitted_by_cost = sorted(
                    fitted_legs.items(),
                    key=lambda kv: kv[1]["total_cents"],
                    reverse=True,
                )
                re_planned = False
                abandoned_lids: list[str] = []
                for target_lid, target_prop in fitted_by_cost:
                    other_costs = sum(
                        p["total_cents"]
                        for lid, p in fitted_legs.items()
                        if lid != target_lid
                    )
                    new_max = effective_ceiling - other_costs
                    if new_max <= 0:
                        new_max = max(1, math.floor(effective_ceiling / len(legs)))

                    if new_max >= target_prop["total_cents"]:
                        continue

                    ceilings[target_lid] = new_max
                    logger.info(
                        "orchestrator: re-planning leg=%s new_ceiling=%d¢",
                        target_lid, new_max,
                    )

                    acc_result = self._propose_with_area_ladder(
                        leg_meta=leg_meta,
                        target_areas=target_areas,
                        area_stage=area_stage,
                        leg_id=target_lid,
                        max_cents=new_max,
                    )
                    if acc_result.get("fit") == "ok":
                        proposals[target_lid] = acc_result["proposal"]
                        logger.info(
                            "orchestrator: re-plan leg=%s → %s total=%d¢",
                            target_lid,
                            acc_result["proposal"]["hotel_id"],
                            acc_result["proposal"]["total_cents"],
                        )
                        re_planned = True
                        break
                    else:
                        proposals[target_lid] = None
                        abandoned_lids.append(target_lid)
                        logger.info(
                            "orchestrator: re-plan leg=%s no_fit at new_ceiling=%d¢",
                            target_lid, new_max,
                        )

                # #112 fix: retry any leg abandoned earlier in THIS pass against
                # the headroom the successful tighten just freed, before falling
                # through to the exhausted-re-plan cannot_satisfy below.
                if re_planned and abandoned_lids:
                    self._retry_abandoned_legs(
                        proposals=proposals,
                        abandoned_lids=abandoned_lids,
                        eff_ceiling=effective_ceiling,
                        leg_meta=leg_meta,
                        target_areas=target_areas,
                        area_stage=area_stage,
                        ceilings=ceilings,
                        legs=legs,
                    )

                if not re_planned:
                    # #72: when an enforced-fee envelope was reserved, the lodging ceiling is
                    # BELOW the user's budget — say so honestly (don't misreport the reduced
                    # ceiling as "the budget"). Recommended/held vaccines are excluded (see #70
                    # optional_health_estimate), consistent with the budget-scope model.
                    _envelope = (
                        total_budget_cents - lodging_budget_cents
                        if lodging_budget_cents is not None else 0
                    )
                    _min_feasible = package_total + _envelope
                    _shortfall = max(0, _min_feasible - total_budget_cents)
                    if _envelope > 0:
                        reason = (
                            f"Your ${total_budget_cents / 100:.0f} budget is about "
                            f"${_shortfall / 100:.0f} short for this trip. "
                            f"The cheapest lodging found costs ~${package_total / 100:.0f}, "
                            f"plus ${_envelope / 100:.0f} reserved for required fees and insurance "
                            f"= ~${_min_feasible / 100:.0f} total. Try raising your budget."
                        )
                    else:
                        reason = (
                            f"Your ${total_budget_cents / 100:.0f} budget is about "
                            f"${_shortfall / 100:.0f} short — the cheapest available lodging "
                            f"for this trip is ~${package_total / 100:.0f}. "
                            f"Try raising your budget or shortening the trip."
                        )
                    return self._cannot_satisfy_result(
                        reason=reason,
                        closest_total=package_total,
                        negotiation_log=negotiation_log,
                        budget_shortfall_cents=_shortfall,
                        min_feasible_total_cents=_min_feasible,
                    )

                continue

            # --- D3: cannot_price / veto-with-no-price — Budget could NOT produce a
            # valid priced checkout (sold-out / merchant unavailable / empty id /
            # absent total). This is a HARD failure, NOT an accept. Map it to a
            # graceful honest terminal (never silently book an un-priced package).
            if decision in ("cannot_price", "unavailable"):
                veto_reason = budget_result.get("veto_reason", decision)
                log_entry["action"] = f"budget_{decision}"
                negotiation_log.append(log_entry)
                logger.warning(
                    "orchestrator: BUDGET %s round=%d reason=%s — honest terminal",
                    decision.upper(), round_num, veto_reason,
                )
                return self._cannot_satisfy_result(
                    reason=(
                        f"Budget could not price the package ({veto_reason}). "
                        "Failing conservative rather than booking an un-priced or "
                        "unavailable selection."
                    ),
                    closest_total=package_total,
                    negotiation_log=negotiation_log,
                )

            # Unexpected decision
            log_entry["action"] = f"unexpected_decision:{decision}"
            negotiation_log.append(log_entry)
            logger.error(
                "orchestrator: unexpected budget decision=%s round=%d",
                decision, round_num,
            )
            return self._cannot_satisfy_result(
                reason=f"Unexpected budget decision: {decision}",
                closest_total=package_total,
                negotiation_log=negotiation_log,
            )

        return self._cannot_satisfy_result(
            reason="Max rounds exceeded without convergence.",
            closest_total=0,
            negotiation_log=negotiation_log,
        )

    # ------------------------------------------------------------------
    # Result builders
    # ------------------------------------------------------------------

    @staticmethod
    def _primary_dest_token(leg_meta: dict[str, dict], proposals: dict[str, dict | None]) -> str:
        """
        Derive a DETERMINISTIC destination token for the booking_ref from the
        trip's PRIMARY destination (the first booked leg).

        Preference order (all var-0, no wall-clock, no random):
          1. The leg's dest_country (ISO2), if the caller supplied one.
          2. The leg's city resolved to ISO2 via the SAME map the parser uses
             (intent_parser.CITY_TO_ISO2) — e.g. 'tokyo' → 'JP', 'bali' → 'ID'.
          3. A slug of the city name when no ISO2 is known (fail-soft, still
             reflects the destination, e.g. 'port-au-prince').
          4. 'UNK' only when there is no destination signal at all.

        The token reflects the ACTUAL destination/region so a Tokyo booking and a
        Port Moresby booking get DIFFERENT refs (the old cosmetic 'co-local'
        prefix did not).
        """
        # First booked leg (proposals preserve leg order) = primary destination.
        primary_lid: str | None = None
        for lid, prop in proposals.items():
            if prop is not None:
                primary_lid = lid
                break
        if primary_lid is None:
            primary_lid = next(iter(leg_meta), None)
        lm = leg_meta.get(primary_lid, {}) if primary_lid is not None else {}

        dc = lm.get("dest_country")
        if isinstance(dc, str) and dc.strip():
            return dc.strip().upper()

        city = lm.get("city")
        if isinstance(city, str) and city.strip():
            iso2 = CITY_TO_ISO2.get(city.strip().lower())
            if iso2:
                return iso2.upper()
            # No ISO2 known — slugify the city so the ref still reflects the dest.
            slug = "".join(
                ch if ch.isalnum() else "-" for ch in city.strip().lower()
            )
            slug = "-".join(part for part in slug.split("-") if part)
            if slug:
                return slug
        return "UNK"

    @staticmethod
    def _mint_booking_ref(merchant_ref: str | None, dest_token: str) -> str | None:
        """
        Re-mint the merchant booking_ref into a DETERMINISTIC, destination-aware
        form: ``BK-<dest_token>-<suffix>``.

        The merchant returns a cosmetic ref (e.g. ``BK-co-local-1``) whose
        'co-local' prefix does NOT reflect the destination. We strip that cosmetic
        scaffolding and graft on the trip's real primary-destination token while
        PRESERVING the merchant's stable per-booking suffix — so the ref stays
        stable for the same trip + idempotency key (an idempotent commit returns
        the same merchant ref → the same suffix → the same final ref) and is
        unique per destination.

        var-0: pure function of (merchant_ref, dest_token). No wall-clock, no
        random. Returns None when the merchant issued no ref (no booking).
        """
        if not merchant_ref:
            return merchant_ref
        suffix = merchant_ref
        # Strip the leading 'BK-' merchant scaffolding.
        if suffix.startswith("BK-"):
            suffix = suffix[len("BK-"):]
        # Strip the cosmetic 'co-local-' prefix (the wrong/destination-blind tag).
        if suffix.startswith("co-local-"):
            suffix = suffix[len("co-local-"):]
        suffix = suffix.strip("-") or suffix
        return f"BK-{dest_token}-{suffix}"

    @staticmethod
    def _success_result(
        *,
        user_id: str,
        total_budget_cents: int,
        budget_result: dict,
        proposals: dict[str, dict | None],
        leg_meta: dict[str, dict],
        negotiation_log: list[dict],
        rounds: int,
        critic_result: dict | None = None,
        transport_result: dict | None = None,
        day_plan_result: dict | None = None,
        day_plan_error: str | None = None,
        unverified_lodging: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        _unverified = unverified_lodging or {}
        # #42 PART A — per-leg cost BASIS disclosure (additive strings only).
        # Lodging legs are PREPAID only when a real merchant checkout exists
        # (checkout_id truthy on the success path); otherwise basis is unknown.
        # Mirrors the booking_links local-import style (keeps cost_basis pure).
        from core.cost_basis import BASIS_UCP_PREPAID, BASIS_UNKNOWN, make_basis

        leg_basis = make_basis(
            BASIS_UCP_PREPAID if budget_result.get("checkout_id") else BASIS_UNKNOWN
        )

        legs_out = []
        for leg_id, prop in proposals.items():
            if prop is None:
                continue
            lm = leg_meta[leg_id]
            legs_out.append({
                "leg_id": leg_id,
                "city": lm.get("city", ""),
                "checkin": lm.get("checkin", ""),
                "checkout": lm.get("checkout", ""),
                "adults": lm.get("adults", 1),
                "nights": lm.get("nights", 1),
                "vibe": lm.get("vibe"),
                "hotel_id": prop["hotel_id"],
                "hotel_title": prop.get("title", prop["hotel_id"]),
                "total_cents": prop["total_cents"],
                "review_score": prop.get("review_score"),
                "star_rating": prop.get("star_rating"),
                "amenities": prop.get("amenities", []),
                "area": prop.get("area"),
                # #36 Tier A — additive lodging type (hotel/hostel/guest_house/
                # apartment/motel) derived from the id slug; None on legacy paths.
                "lodging_type": prop.get("lodging_type"),
                # HONESTY: surface the merchant's unverified-lodging warning (a curated non-hotel
                # booked as a city's only listing, or a suspect-named OSM row) on the leg so it reaches
                # the traveller — never silently booked as a verified hotel. Sourced from the side-
                # channel by hotel_id so it survives every upstream fixed-key re-projection.
                **({"unverified_lodging": True,
                    "note": _unverified[str(prop.get("hotel_id"))]}
                   if str(prop.get("hotel_id")) in _unverified else {}),
                # #42 PART A — basis disclosure (does not touch total_cents).
                "cost_basis": leg_basis["cost_basis"],
                "cost_basis_label": leg_basis["cost_basis_label"],
            })

        # BUG B fix — headline package_total_cents was None on success even though
        # every leg carried total_cents. Sum the per-leg total_cents into the
        # top-level INTEGER-cents headline (var-0: pure sum of the booked legs).
        # Any gate/insurance fees are folded in later by _inject_fees (which keys
        # off result["total_cents"]); summing legs here matches how the package
        # total is computed in the negotiation loop and avoids double-counting fees
        # that are NOT part of the lodging legs.
        package_total_cents = sum(int(leg["total_cents"]) for leg in legs_out)

        # #2 HONESTY — the HEADLINE total must be the merchant-AUTHORITATIVE
        # committed amount (what the user was actually charged), NOT the catalog/DP
        # proposal sum. When a Critic ran, price-integrity already forced these
        # equal; but on the Critic-BYPASSED backward-compat path they can diverge if
        # the merchant re-priced a leg within budget. Prefer the committed total and
        # surface an honest reconciliation note on any divergence — never show a
        # headline total the user wasn't charged. (Per-leg total_cents stays the
        # proposal breakdown; only the rolled-up headline is reconciled.) Fall back
        # to the proposal sum only when the merchant returned no committed total.
        committed_total = budget_result.get("total_cents")
        price_reconciliation: dict | None = None
        if isinstance(committed_total, int) and committed_total > 0:
            if committed_total != package_total_cents:
                price_reconciliation = {
                    "catalog_estimate_cents": package_total_cents,
                    "merchant_committed_cents": committed_total,
                    "note": (
                        "Headline total is the merchant-committed (charged) amount; "
                        "the catalog estimate differed and was reconciled to it."
                    ),
                }
            headline_total_cents = committed_total
        else:
            headline_total_cents = package_total_cents

        # BUG A fix — re-mint the merchant's destination-blind 'BK-co-local-N' ref
        # into a DETERMINISTIC destination-aware 'BK-<dest>-<suffix>' so a Tokyo
        # booking and a Port Moresby booking get DIFFERENT refs.
        dest_token = TravelOrchestrator._primary_dest_token(leg_meta, proposals)
        booking_ref = TravelOrchestrator._mint_booking_ref(
            budget_result.get("booking_ref"), dest_token
        )

        result: dict[str, Any] = {
            "outcome": "success",
            "user_id": user_id,
            "total_budget_cents": total_budget_cents,
            "total_booked_cents": budget_result.get("total_cents", 0),
            # #2 — headline = merchant-committed total (falls back to the per-leg
            # proposal sum only when no committed total was returned).
            "package_total_cents": headline_total_cents,
            # _inject_fees() reads result["total_cents"] to fold in gate/insurance
            # fees → seed it with the headline (merchant-committed) total so
            # package_total_with_fees_cents is correct (charged lodging + fees, no
            # double-count of fees already in legs). #2: on the divergent path this
            # is the committed total, so fees/premiums compute off the CHARGED base.
            # NOTE: this also feeds _attach_insurance's insured_trip_cost — previously
            # total_cents was absent on success so insurance assessed premiums on a $0
            # trip (latent bug); premiums are now correctly computed on the real legs
            # sum. Benchmark DC0 premiums (verify_report_numbers 86/86) confirm no
            # regression in the tested cases.
            "total_cents": headline_total_cents,
            "booking_ref": booking_ref,
            "checkout_id": budget_result.get("checkout_id"),
            "legs": legs_out,
            "negotiation_log": negotiation_log,
            "negotiation_rounds": rounds,
        }
        # #2 — surface the honest catalog-vs-committed reconciliation when they
        # diverged (Critic-bypassed re-price). Absent when they matched → no key,
        # var-0 preserved for the common (Critic-present) path.
        if price_reconciliation is not None:
            result["price_reconciliation"] = price_reconciliation
        if critic_result is not None:
            result["critic_quality_score"] = critic_result.get("quality_score")
            result["critic_transport_checked"] = critic_result.get("transport_checked", False)
        if transport_result is not None:
            # #70: transport_checked is the HONESTY signal — True only when EVERY edge is
            # genuinely verified-feasible. An UNVERIFIED edge (no seeded transfer time) does
            # NOT block the trip (it's surfaced as an advisory), but it must NOT let us claim
            # "all transport verified" either (that would overclaim). So require BOTH genuine-
            # infeasible AND unverified to be empty.
            result["transport_checked"] = (
                len(transport_result.get("infeasible_edges", [])) == 0
                and len(transport_result.get("unverified_edges", [])) == 0
            )
            result["transport_edges"] = transport_result.get("edges", [])
            # Additive: the UNVERIFIED hops (no data) the traveler must confirm at booking —
            # surfaced honestly alongside transport_edges, never silently dropped.
            result["transport_unverified"] = transport_result.get("unverified_edges", [])
            # #42 PART A — ONE-TIME honest transport pricing note. Transport
            # carries NO per-edge price line: durations are estimated (OpenFlights
            # ~2014 vintage) and fares are NOT priced — the traveler books
            # externally. Additive deterministic strings (no number asserted).
            result["transport_pricing"] = {
                "basis": "handoff",
                "basis_label": (
                    "Durations estimated (OpenFlights ~2014 vintage); "
                    "fares not priced — book externally"
                ),
            }
            # Surface route as human-readable strings — derived deterministically
            # from transport_edges (no LLM, no nondeterminism).
            # route: top-level list of readable hop strings, one per edge.
            # inbound_transport: per-leg readable string for the leg's incoming edge.
            route: list[str] = []
            inbound_by_leg: dict[str, str] = {}
            for edge in transport_result.get("edges", []):
                # #63 — PRESERVE the conservative-unknown sentinel. transfer_minutes
                # == -1 means truly-unknown (never knowable); 0 means same-area / no
                # transfer. Collapsing -1 → 0 would lose that distinction, so render
                # them as DISTINCT duration strings.
                raw_mins = edge.get("transfer_minutes")
                mins = int(raw_mins) if raw_mins is not None else -1
                if mins > 0:
                    h, m = divmod(mins, 60)
                    dur = f"~{h}h{m:02d}m" if h else f"~{m}m"
                elif mins == 0:
                    dur = "same-area"
                else:
                    dur = "~unknown"
                mode = edge.get("mode", "?")
                fc = (edge.get("from_city") or "").title()
                tc = (edge.get("to_city") or "").title()
                feasible = edge.get("feasible", True)
                feasible_tag = "" if feasible else " ⚠ infeasible"
                hop = f"{fc} → {tc} · {mode} · {dur}{feasible_tag}"
                route.append(hop)
                to_leg = edge.get("to_leg")
                if to_leg:
                    inbound_by_leg[to_leg] = hop
            if route:
                result["route"] = route
            # Annotate each leg with its inbound_transport string (all legs except first).
            for leg in result.get("legs", []):
                leg_id = leg.get("leg_id", "")
                if leg_id in inbound_by_leg:
                    leg["inbound_transport"] = inbound_by_leg[leg_id]

        # build #30: attach the ADDITIVE per-leg day-by-day activity & meal plan.
        # OFF the money path — present only when a Day-planner was wired AND it
        # returned a plan. Missing/None → omit entirely (never crash, the
        # itinerary is valid without an activity plan). Both the top-level
        # day_plans list and a per-leg leg["day_plan"] (keyed by leg_id, mirroring
        # inbound_transport) are surfaced.
        if day_plan_result is not None:
            leg_plans = day_plan_result.get("leg_plans", [])
            result["day_plans"] = leg_plans
            plan_by_leg = {
                p.get("leg_id"): p for p in leg_plans if p.get("leg_id")
            }
            for leg in result.get("legs", []):
                lid = leg.get("leg_id", "")
                if lid in plan_by_leg:
                    leg["day_plan"] = plan_by_leg[lid]
            # Attach deterministic intra-city transport hops (Feature 2).
            # APPEND-ONLY / idempotent: sets day['intracity_hops'] and leg['airport_transfer'].
            # Never edits attractions or meals. Hops live inside day_plans which is in
            # _VAR0_FIELDS → the double-run harness guards their determinism automatically.
            from utils.intracity_transport import attach_intracity_transport
            attach_intracity_transport(leg_plans, result.get("legs", []))
        elif day_plan_error:
            # L2 — a WIRED-BUT-FAILED Day-planner call (distinct from "no
            # Day-planner configured", which leaves day_plan_error None) must
            # not degrade to a bare lodging itinerary indistinguishable from a
            # deployment with no day-planner at all — surface it honestly.
            result["day_planner_degraded"] = True
            adv_note = (
                "Day-by-day activity and meal planning could not be completed "
                "for this trip (the Day-planner agent was unavailable) — this "
                "itinerary only covers lodging/transport. Please plan daily "
                "activities separately."
            )
            existing_advisories = result.get("advisories")
            if isinstance(existing_advisories, list):
                existing_advisories.append(adv_note)
            else:
                result["advisories"] = [adv_note]
        return result

    @staticmethod
    def _cannot_satisfy_result(
        *,
        reason: str,
        closest_total: int,
        negotiation_log: list[dict],
        critic_result: dict | None = None,
        budget_shortfall_cents: int | None = None,
        min_feasible_total_cents: int | None = None,
        day_plan_preview: dict | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "outcome": "cannot_satisfy",
            "reason": reason,
            "closest_package_total_cents": closest_total,
            "negotiation_log": negotiation_log,
        }
        if critic_result is not None:
            result["critic_violations"] = critic_result.get("violations", [])
        # Commit-time budget-veto decline (bug-2 cluster, fix #4): the day-by-day
        # activity/meal plan for this round was already fully computed before the
        # merchant's commit-time veto killed the trip — long segmented itineraries
        # (which hit hard-max/re-plan caps most often) hit this exact case. Rather
        # than throwing that work away on a bare rejection, attach it as a
        # non-authoritative preview. ADDITIVE ONLY / caller-scoped: only the
        # commit-time-veto decline call sites pass this kwarg — every other
        # cannot_satisfy call site is byte-identical (kwarg defaults to None).
        if day_plan_preview is not None:
            leg_plans = day_plan_preview.get("leg_plans", [])
            if leg_plans:
                result["day_plans_preview"] = leg_plans
        # Budget-guidance addition: only when BOTH fields are present AND shortfall > 0
        # (0 shortfall = budget covers the cheapest plan, no top-up needed).
        # ADDITIVE ONLY — all existing callers pass neither kwarg → byte-identical.
        if (
            budget_shortfall_cents is not None
            and min_feasible_total_cents is not None
            and budget_shortfall_cents > 0
        ):
            result["budget_shortfall_cents"] = budget_shortfall_cents
            result["min_feasible_total_cents"] = min_feasible_total_cents
            sf_usd = budget_shortfall_cents // 100
            mf_usd = min_feasible_total_cents // 100
            result["reason"] = (
                reason
                + f" Budget is approximately US${sf_usd:,} short of the cheapest viable"
                f" plan (~US${mf_usd:,}); consider increasing your budget."
            )
        return result

    def _emit_wallet_debit(self, budget_result: dict) -> None:
        """
        Emit the side-channel `wallet` DEBIT trace event after a committed booking
        (var-0-exempt by contract; fully guarded). Reads the draw-down surfaced on
        the accept result. NO-OP when the merchant ran no wallet (no wallet fields).
        """
        if not isinstance(budget_result, dict):
            return
        if not budget_result.get("wallet_session_id") and budget_result.get("wallet_debit_cents") is None:
            return
        try:
            self._tracer(
                "wallet", "Wallet", trip_id=self._trip_id,
                summary="wallet debited",
                data={
                    "op": "debit",
                    "amount_cents": budget_result.get("wallet_debit_cents"),
                    "balance_cents": budget_result.get("wallet_balance_cents"),
                    "checkout_id": budget_result.get("checkout_id"),
                    "booking_ref": budget_result.get("booking_ref"),
                    "simulated": True,
                },
            )
        except Exception:  # noqa: BLE001
            pass

    def _insufficient_funds_terminal(
        self,
        *,
        budget_result: dict,
        negotiation_log: list[dict],
        log_entry: dict,
    ) -> dict[str, Any]:
        """
        TERMINAL for the SIMULATED prepaid wallet 402 (insufficient_funds). DISTINCT
        from a budget veto (which is a re-plan trigger): the trip is WITHIN budget
        but exceeds the FUNDED wallet balance, so tightening the priciest leg won't
        help — return an honest cannot_satisfy. Emits a side-channel wallet/
        gate_blocked trace event (agent="Wallet").
        """
        total = int(budget_result.get("total_cents") or 0)
        bal = budget_result.get("wallet_balance_cents")
        log_entry["action"] = "insufficient_funds"
        log_entry["budget_result"] = budget_result
        negotiation_log.append(log_entry)
        try:
            self._tracer(
                "wallet", "Wallet", trip_id=self._trip_id,
                summary="insufficient funds — trip exceeds funded wallet",
                data={
                    "op": "gate_blocked",
                    "reason": "insufficient_funds",
                    "amount_cents": total,
                    "balance_cents": bal,
                    "checkout_id": budget_result.get("checkout_id"),
                    "simulated": True,
                },
            )
        except Exception:  # noqa: BLE001
            pass
        bal_usd = f"${(int(bal) if bal is not None else 0) / 100:,.2f}"
        return {
            "outcome": "cannot_satisfy",
            "reason": "insufficient_funds",
            "detail": f"trip ${total / 100:,.2f} exceeds funded wallet balance {bal_usd}",
            "total_cents": total,
            "wallet_balance_cents": bal,
            "checkout_id": budget_result.get("checkout_id", ""),
            "idempotency_key": budget_result.get("idempotency_key", ""),
            "closest_package_total_cents": total,
            "negotiation_log": negotiation_log,
        }

    @staticmethod
    def _needs_consent_terminal(
        *,
        budget_result: dict,
        idempotency_key: str,
        closest_total: int,
        negotiation_log: list[dict],
        log_entry: dict,
    ) -> dict[str, Any]:
        """
        M1 — Honest HALT for a COMMIT-time needs_consent / needs_mandate decision.

        The A2A seam (see the module docstring near _CONSUMABLE_TASK_STATES)
        promises that an input-required task carries a needs_consent /
        needs_mandate decision "the orchestrator handles downstream" — but
        nothing did: it fell into the generic "decision != accept" commit-time-
        veto branch below, which re-plans a CHEAPER hotel (a price-based
        resolution). Since the package is already within budget, no leg ever
        tightens, re_planned stays False, and the run terminated with a
        FABRICATED "cannot re-plan"/budget-shortfall reason instead of the real
        one — the merchant is asking for consent/mandate the caller hasn't (yet)
        supplied. This is NOT a re-plan trigger (re-pricing the itinerary cannot
        satisfy a consent requirement) — it is a distinct, honest halt that
        surfaces the real consent_message/mandate_message so the caller can
        re-submit with the requested consent/mandate, threading idempotency_key
        so the SAME held checkout is reused rather than re-priced from scratch.
        """
        decision = budget_result.get("decision", "needs_consent")
        message = (
            budget_result.get("consent_message")
            or budget_result.get("mandate_message")
            or (
                "The merchant requires explicit buyer consent to complete "
                "this booking."
                if decision == "needs_consent" else
                "The merchant requires an AP2 autonomy mandate to complete "
                "this booking."
            )
        )
        log_entry["action"] = decision  # "needs_consent" | "needs_mandate"
        log_entry["budget_result"] = budget_result
        negotiation_log.append(log_entry)
        return {
            "outcome": "needs_consent",
            "reason": decision,
            "detail": message,
            "consent_message": budget_result.get("consent_message"),
            "mandate_message": budget_result.get("mandate_message"),
            "checkout_id": budget_result.get("checkout_id", ""),
            "idempotency_key": idempotency_key,
            "closest_package_total_cents": closest_total,
            "negotiation_log": negotiation_log,
        }

    @staticmethod
    def _commit_failed_result(
        *,
        budget_result: dict,
        idempotency_key: str,
        closest_total: int,
        negotiation_log: list[dict],
        log_entry: dict,
    ) -> dict[str, Any]:
        """
        #26 — Honest terminal for a COMMIT that RAISED at the single irreversible
        booking step. Server-side booking state is AMBIGUOUS (the merchant may or
        may not have booked), so the orchestrator must NOT re-plan (double-book
        risk) and must NOT crash to the caller. It returns a needs-reconciliation
        outcome threading the idempotency_key so a later retry recovers the same
        booking_ref rather than creating a second booking.
        """
        log_entry["action"] = "commit_failed"
        log_entry["budget_result"] = budget_result
        negotiation_log.append(log_entry)
        return {
            "outcome": "cannot_satisfy",
            "reason": (
                "Commit failed at the irreversible booking step "
                f"({budget_result.get('detail', 'commit_errored')}). "
                "Booking state is unconfirmed — retry with the same "
                "idempotency_key to reconcile rather than re-book."
            ),
            "needs_reconciliation": True,
            "idempotency_key": idempotency_key,
            "checkout_id": budget_result.get("checkout_id", ""),
            "closest_package_total_cents": closest_total,
            "negotiation_log": negotiation_log,
        }
