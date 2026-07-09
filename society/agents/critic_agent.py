"""
critic_agent.py — Critic/Verifier A2A agent (Travel Guild M3a).

Design contract: the internal design spec §3.6, §2, §4.1–4.3, §4.6, §10.6/#4/#9.

Agent Card skill: ``itinerary.verify``

The Critic is the hard gate before the single human consent.  It re-derives every
fact from merchant backend data — never trusts the assembled proposal blindly.

Input (data part, JSON):
    {
        "user_id":            str,
        "total_budget_cents": int,
        "planned_leg_count":  int  (optional — if provided, MISSING_LEG fires when
                                   assembled legs < planned_leg_count; backstop for
                                   partial-booking regression protection),
        "legs": [
            {
                "leg_id":      str,
                "city":        str,
                "area":        str  (optional),
                "checkin":     str  (YYYY-MM-DD),
                "checkout":    str  (YYYY-MM-DD),
                "adults":      int,
                "hotel_id":    str,
                "total_cents": int,
                "provenance":  str  (optional — "cache" | "live" | "merchant")
            }
        ]
    }

Output artifact (data part, JSON) — typed CriticResult:
    {
        "decision":            "verified" | "rejected",
        "quality_score":       float,       # 0.0–1.0 soft quality score
        "violations":          [            # empty on "verified"
            {
                "code":     str,            # see VIOLATION_CODES below
                "leg_id":   str | null,
                "detail":   str,
                "route_to": str             # "budget" | "planner" | "accommodation"
            }
        ],
        "reverified_total_cents": int,      # sum of merchant-re-priced leg totals
        "provenance":          "merchant",
        "transport_checked":   bool         # True when transport_result provided + all edges feasible
    }

Hard gates (reject on ANY violation — §2):
    MISSING_LEG          — a required leg has no hotel proposal, OR the itinerary
                           is empty (zero legs — unconditional, D4).
    PRICE_MISMATCH       — proposal's total_cents ≠ merchant's live price (anti-hallucination)
                           on a SUCCESSFUL 200 lookup.
    UNAVAILABLE          — merchant reports the hotel unavailable (HTTP 409 /
                           status=="unavailable", e.g. sold-out) — an availability/
                           coverage fault, distinct from PRICE_MISMATCH (D3/D4).
    OVER_CAPACITY        — adults > hotel.max_occupancy (the merchant checkout would
                           VOID with exceeds_room_capacity) (D4).
    DUPLICATE_LEG        — two legs share a leg_id (malformed itinerary) (D4).
    INVALID_DATE         — checkin >= checkout or dates malformed.
    DATE_GAP             — gap between consecutive legs (leg[k].checkout != leg[k+1].checkin).
    DATE_OVERLAP         — legs overlap.
    OVER_BUDGET          — sum of re-verified totals > total_budget_cents.
    MISSING_PROVENANCE   — leg carries no provenance tag (not traceable to backend).

Soft quality score (0–1, for ranking — not a gate):
    avg(review_score) / 10.0 × budget_utilization_fit
    budget_utilization_fit = 1.0 - |1.0 - reverified_total / total_budget| clamped to [0, 1]

Route-to logic (§4.3 conflict routing):
    PRICE_MISMATCH, MISSING_LEG    → "accommodation"  (re-propose)
    OVER_BUDGET                     → "budget"         (re-allocate)
    INVALID_DATE, DATE_GAP,         → "planner"        (fix global state)
    DATE_OVERLAP, MISSING_PROVENANCE

Transport feasibility gate (M3b — implemented):
    Accepts optional transport_result dict from TransportAgent.
    If provided with infeasible_edges → TRANSPORT_INFEASIBLE violation, reject.
    If provided with empty infeasible_edges → transport_checked=True.
    If absent → transport_checked=False (backward-compatible, no violation).

§4.6 reliability:
    - Typed output schema (all fields explicit).
    - All merchant calls use the same pattern as BudgetAgent / AccommodationAgent.
    - Never trusts proposal price; always re-verifies from lookup_catalog.
    - Returns honest rejection on any violation; never fabricates a "verified" result.

Runnable service: HOST / PORT env, defaults 0.0.0.0:9103.
(Port 9103 — CriticAgent takes the port previously used by AccommodationAgent
 in standalone mode; both coexist in tests via in-process TestClients.)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date
from typing import Any

import httpx
import uvicorn
from utils.ssrf_guard import validate_outbound_url

from agents.a2a_agent import (
    A2AAgent,
    _data_part,
    _new_artifact,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Merchant MCP endpoint
# ---------------------------------------------------------------------------

MERCHANT_MCP_URL = os.environ.get(
    "MERCHANT_MCP_URL",
    "http://ucp-merchant:8090/api/ucp/mcp",
)

# SSRF-001: validate at import time (same guard budget_agent.py and
# accommodation_agent.py apply to the identical env var — a security audit
# found this check previously existed ONLY in budget_agent.py, giving zero
# protection to this agent's separate process/container).
try:
    validate_outbound_url(MERCHANT_MCP_URL, param_name="MERCHANT_MCP_URL")
except ValueError:
    raise
except Exception as _exc:  # noqa: BLE001
    logger.warning("MERCHANT_MCP_URL validation skipped: %s", _exc)

_MERCHANT_TIMEOUT = float(os.environ.get("MERCHANT_TIMEOUT", "15"))


# ---------------------------------------------------------------------------
# Violation codes (§3.6 / §2 hard constraints)
# ---------------------------------------------------------------------------

MISSING_LEG          = "MISSING_LEG"
PRICE_MISMATCH       = "PRICE_MISMATCH"
UNAVAILABLE          = "UNAVAILABLE"            # D3/D4: 409 / status=="unavailable" (sold-out / coverage), NOT price
DUPLICATE_LEG        = "DUPLICATE_LEG"          # D4 #8: two legs share a leg_id (malformed input)
OVER_CAPACITY        = "OVER_CAPACITY"          # D4 #7: adults > hotel.max_occupancy (commit gate would VOID)
INVALID_DATE         = "INVALID_DATE"
DATE_GAP             = "DATE_GAP"
DATE_OVERLAP         = "DATE_OVERLAP"
OVER_BUDGET          = "OVER_BUDGET"
MISSING_PROVENANCE   = "MISSING_PROVENANCE"
TRANSPORT_INFEASIBLE = "TRANSPORT_INFEASIBLE"   # M3b: transport feasibility gate
COUNTERPARTY_BLOCKED = "COUNTERPARTY_BLOCKED"   # Fraud: insolvent/unknown supplier (N1)

# §4.3 conflict routing: code → responsible agent
_ROUTE_TO: dict[str, str] = {
    MISSING_LEG:          "accommodation",
    PRICE_MISMATCH:       "accommodation",
    UNAVAILABLE:          "accommodation",   # sold-out → re-propose a different hotel
    OVER_CAPACITY:        "accommodation",   # room too small → re-propose a bigger room/hotel
    DUPLICATE_LEG:        "planner",         # malformed leg ids → planner fixes global state
    INVALID_DATE:         "planner",
    DATE_GAP:             "planner",
    DATE_OVERLAP:         "planner",
    OVER_BUDGET:          "budget",
    MISSING_PROVENANCE:   "planner",
    TRANSPORT_INFEASIBLE: "planner",   # re-plan the itinerary order
    # A blocked/unknown counterparty can't be fixed by re-pricing — the
    # ACCOMMODATION/transport leg must be re-proposed against a solvent supplier.
    COUNTERPARTY_BLOCKED: "accommodation",
}

# ---------------------------------------------------------------------------
# Fraud commit-time re-check (defense-in-depth). The Critic INDEPENDENTLY
# re-derives each leg's counterparty solvency band from the SAME seeded table
# the Fraud agent uses, so a MISSED Fraud verdict (Fraud bypassed / a slip) can
# NEVER let an insolvent supplier through to commit. Imported lazily-safe at
# module load; if fraud_agent is unavailable the gate degrades to a no-op
# (backward-compatible — never blocks on an import error).
# ---------------------------------------------------------------------------
try:  # pragma: no cover - import guard
    from agents.fraud_agent import vet_counterparty as _fraud_vet_counterparty
    from agents.fraud_agent import RiskBand as _FraudRiskBand
except Exception:  # pragma: no cover
    _fraud_vet_counterparty = None  # type: ignore[assignment]
    _FraudRiskBand = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Typed result builders
# ---------------------------------------------------------------------------

def _violation(code: str, leg_id: str | None, detail: str) -> dict[str, Any]:
    return {
        "code": code,
        "leg_id": leg_id,
        "detail": detail,
        "route_to": _ROUTE_TO.get(code, "planner"),
    }


def _critic_result(
    *,
    decision: str,
    quality_score: float,
    violations: list[dict[str, Any]],
    reverified_total_cents: int,
    transport_checked: bool = False,
) -> dict[str, Any]:
    return {
        "decision": decision,
        "quality_score": round(max(0.0, min(1.0, quality_score)), 4),
        "violations": violations,
        "reverified_total_cents": reverified_total_cents,
        "provenance": "merchant",
        "transport_checked": transport_checked,
    }


# ---------------------------------------------------------------------------
# Merchant call helper — lookup_catalog
# ---------------------------------------------------------------------------

class _AvailabilityError(RuntimeError):
    """
    D3/D4: the merchant reported the hotel is NOT available for the leg
    (HTTP 409 / structuredContent status=="unavailable", e.g. sold-out).

    This is a COVERAGE/availability fault, NOT a price disagreement — it must
    map to UNAVAILABLE (route_to accommodation), never PRICE_MISMATCH (which is
    reserved for a price disagreement on a successful 200 lookup).
    """


def _lookup_catalog(
    client: httpx.Client,
    hotel_id: str,
    checkin: str,
    checkout: str,
    adults: int,
) -> dict[str, Any]:
    """
    Call the merchant MCP lookup_catalog tool for a specific hotel + dates.

    Returns the structured content dict, which includes:
        total_cents, nights, hotel (with review_score), available, source
    Raises RuntimeError on merchant error or not-found.
    """
    # SSRF-001 TOCTOU fix: re-validate immediately before every outbound call,
    # not just once at import time — a hostname that resolved benignly at
    # import time could be DNS-rebound to a metadata address by now.
    validate_outbound_url(MERCHANT_MCP_URL, param_name="MERCHANT_MCP_URL")
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {
            "name": "lookup_catalog",
            "arguments": {
                "meta": {"autonomy_level": "L2"},
                "product": {
                    "hotel_id": hotel_id,
                    "checkin": checkin,
                    "checkout": checkout,
                    "adults": adults,
                },
            },
        },
    }
    resp = client.post(MERCHANT_MCP_URL, json=payload, timeout=_MERCHANT_TIMEOUT)
    http_code = resp.status_code
    try:
        body = resp.json()
    except Exception:
        raise RuntimeError(
            f"Merchant lookup_catalog returned non-JSON (HTTP {http_code}): {resp.text[:200]}"
        )

    rpc_result = body.get("result", {})
    structured = rpc_result.get("structuredContent", {})
    if not isinstance(structured, dict):
        structured = {}

    if http_code == 404:
        # Hotel not in catalog
        raise RuntimeError(
            f"Hotel {hotel_id!r} not found in merchant catalog (HTTP 404)"
        )
    # D3/D4: sold-out / unavailable is HTTP 409 (StatusConflict) with
    # structuredContent status=="unavailable" and NO total_cents. This is an
    # AVAILABILITY/coverage fault — distinct from a price disagreement. Raise a
    # typed _AvailabilityError so the caller emits UNAVAILABLE, not PRICE_MISMATCH.
    if http_code == 409 or structured.get("status") == "unavailable":
        reason = structured.get("reason") or structured.get("status") or "unavailable"
        raise _AvailabilityError(
            f"Hotel {hotel_id!r} unavailable for {checkin}..{checkout} "
            f"(HTTP {http_code}, reason={reason!r})"
        )
    if http_code != 200:
        raise RuntimeError(
            f"Merchant lookup_catalog HTTP {http_code} for {hotel_id!r}: {body}"
        )

    if structured.get("error"):
        raise RuntimeError(
            f"Merchant lookup_catalog error for {hotel_id!r}: {structured['error']}"
        )
    return structured


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date | None:
    """Return a date or None if unparseable."""
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# CriticAgent
# ---------------------------------------------------------------------------

class CriticAgent(A2AAgent):
    """
    Critic/Verifier A2A agent (Travel Guild M3a).

    Implements the ``itinerary.verify`` skill: re-verifies the assembled
    candidate itinerary against the itinerary-assembly hard constraints (§2)
    — coverage, price integrity, dates, budget, provenance, transport
    feasibility, counterparty trust — by re-pricing every leg via the
    merchant backend.  Returns verified|rejected with a typed CriticResult
    artifact.

    Scope note: insurance, health/vaccine, and visa/compliance hard-vetoes
    are enforced upstream in the orchestrator (single enforcement point,
    not re-checked here) — Critic provides defense-in-depth specifically
    for the assembly-stage constraints listed above, not a second check on
    every hard-fail condition in the system.

    Never trusts proposal prices — always calls lookup_catalog per leg.
    Returns honest rejection on any violation; never fabricates a pass.

    Args:
        host:               Bind host for the ASGI server.
        port:               Bind port for the ASGI server.
        merchant_transport: Optional httpx.BaseTransport for testing.
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
            "name": "critic-agent",
            "description": (
                "Critic/Verifier specialist — hard gate before the single human consent. "
                "Re-verifies the assembled itinerary against the itinerary-assembly hard "
                "constraints (§2) by re-pricing every leg via the merchant backend "
                "(lookup_catalog). Insurance/health/visa hard-vetoes are enforced upstream "
                "in the orchestrator, not re-checked here. "
                "Never trusts proposal prices; catches fabricated/stale prices. "
                "Rejects on: missing leg, price mismatch, invalid/non-contiguous dates, "
                "over-budget sum, missing provenance. "
                "Returns a soft quality score (0–1) for ranking. "
                "Implements A2A skill 'itinerary.verify'. "
                "Part of the Travel Guild multi-agent pipeline (Track 3, M3a)."
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
                    "id": "itinerary.verify",
                    "name": "Verify Itinerary",
                    "description": (
                        "Given a fully assembled candidate itinerary "
                        "{user_id, total_budget_cents, legs[], transport_result (optional)}, "
                        "re-verify every leg via the merchant (lookup_catalog) and check "
                        "all hard constraints: "
                        "(1) Coverage — no missing leg; "
                        "(2) Price integrity — proposal total_cents matches merchant cents; "
                        "(3) Date validity + contiguity — checkin < checkout, no gaps/overlaps; "
                        "(4) Budget — re-verified sum <= total_budget_cents; "
                        "(5) Provenance present on each leg; "
                        "(6) [M3b] Transport feasibility — if transport_result provided, "
                        "    rejects on infeasible_edges (TRANSPORT_INFEASIBLE); "
                        "    sets transport_checked=True when all edges feasible. "
                        "Returns verified or rejected with violations[] and route_to per §4.3."
                    ),
                    "tags": [
                        "critic", "verifier", "itinerary", "gate", "anti-hallucination",
                        "price-integrity", "date-contiguity", "budget",
                    ],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "user_id": {"type": "string"},
                            "total_budget_cents": {"type": "integer"},
                            "planned_leg_count": {
                                "type": "integer",
                                "description": (
                                    "Optional total planned leg count from the Planner. "
                                    "If provided and len(legs) < planned_leg_count, emits "
                                    "MISSING_LEG immediately (backstop against partial-booking regression)."
                                ),
                            },
                            "legs": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "leg_id":      {"type": "string"},
                                        "city":        {"type": "string"},
                                        "area":        {"type": "string"},
                                        "checkin":     {"type": "string"},
                                        "checkout":    {"type": "string"},
                                        "adults":      {"type": "integer"},
                                        "hotel_id":    {"type": "string"},
                                        "total_cents": {"type": "integer"},
                                        "provenance":  {"type": "string"},
                                    },
                                    "required": [
                                        "leg_id", "city", "checkin", "checkout",
                                        "hotel_id", "total_cents",
                                    ],
                                },
                            },
                        },
                        "required": ["user_id", "total_budget_cents", "legs"],
                    },
                    "examples": [
                        (
                            '{"user_id":"u1","total_budget_cents":80000,"legs":['
                            '{"leg_id":"leg-0","city":"bali","checkin":"2025-10-01",'
                            '"checkout":"2025-10-04","adults":1,'
                            '"hotel_id":"bali-alaya-ubud","total_cents":49500,'
                            '"provenance":"live"}]}'
                        )
                    ],
                }
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("itinerary.verify", self._verify_handler)

    # ------------------------------------------------------------------
    # Skill handler
    # ------------------------------------------------------------------

    async def _verify_handler(self, message: dict, task: dict) -> dict:
        """
        itinerary.verify skill handler.

        Extracts the candidate itinerary, runs all hard-constraint checks
        (re-verifying prices from merchant), and returns a typed CriticResult.

        M3b: Accepts optional ``transport_result`` dict in the payload.
          - If provided and has infeasible_edges → adds TRANSPORT_INFEASIBLE violation.
          - If provided and empty infeasible_edges → sets transport_checked=True.
          - If absent → transport_checked=False (backward-compatible, no new violation).
        """
        payload = self._extract_payload(message)
        if payload is None:
            raise ValueError(
                "itinerary.verify requires a data part with JSON payload "
                "{user_id, total_budget_cents, legs}"
            )

        user_id: str = payload.get("user_id", "")
        total_budget_cents: int = int(payload.get("total_budget_cents", 0))
        legs: list[dict] = payload.get("legs", [])
        # Optional backstop: planned_leg_count fires MISSING_LEG if assembled legs < planned
        planned_leg_count_raw = payload.get("planned_leg_count")
        planned_leg_count: int | None = (
            int(planned_leg_count_raw) if planned_leg_count_raw is not None else None
        )
        # M3b: optional transport result from TransportAgent
        transport_result: dict | None = payload.get("transport_result")
        # Fraud: optional fresh-consent tokens {counterparty_id: token} that
        # override the insolvency gate for an explicitly-consented counterparty.
        consent_tokens = payload.get("counterparty_consent_tokens")
        if not isinstance(consent_tokens, dict):
            consent_tokens = None

        if not user_id:
            raise ValueError("user_id is required")
        if total_budget_cents <= 0:
            raise ValueError("total_budget_cents must be > 0")
        if not isinstance(legs, list):
            raise ValueError("legs must be a list")

        result_data = self._verify_itinerary(
            user_id=user_id,
            total_budget_cents=total_budget_cents,
            legs=legs,
            planned_leg_count=planned_leg_count,
            transport_result=transport_result,
            counterparty_consent_tokens=consent_tokens,
        )

        logger.info(
            "critic.verify: user=%s decision=%s quality=%.3f "
            "violations=%d reverified=%d¢",
            user_id,
            result_data["decision"],
            result_data["quality_score"],
            len(result_data["violations"]),
            result_data["reverified_total_cents"],
        )

        return _new_artifact(
            name="itinerary.verify.result",
            parts=[_data_part(result_data)],
        )

    # ------------------------------------------------------------------
    # Core verification logic
    # ------------------------------------------------------------------

    def _verify_itinerary(
        self,
        *,
        user_id: str,
        total_budget_cents: int,
        legs: list[dict],
        planned_leg_count: int | None = None,
        transport_result: dict | None = None,
        counterparty_consent_tokens: dict | None = None,
    ) -> dict[str, Any]:
        """
        Run all hard-constraint checks and return a typed CriticResult.

        Checks (in order):
            0. Planned leg count backstop — if planned_leg_count is provided and
               len(legs) < planned_leg_count, emit MISSING_LEG immediately.
               This is a defense-in-depth backstop against partial-booking regression:
               the Orchestrator's all-or-none rule (P0 fix) should prevent this from
               ever being reached, but the Critic fires it if called directly with
               a partial assembled list.
            1. Coverage — each leg has hotel_id.
            2. Per-leg date validity (checkin < checkout, parseable).
            3. Date contiguity — consecutive legs connect, no gaps/overlaps.
            4. Provenance present on each leg.
            5. Price integrity — re-verify each leg via lookup_catalog.
            6. Budget — sum of re-verified totals <= total_budget_cents.
            7. [M3b] Transport feasibility — if transport_result provided, reject on
               infeasible_edges; set transport_checked=True when all edges feasible.
               If transport_result absent → backward-compatible (transport_checked=False,
               no TRANSPORT_INFEASIBLE violation emitted).
        """
        violations: list[dict[str, Any]] = []

        # D4 #8: accumulate the re-verified total as a plain INTEGER SUM over the
        # legs LIST — NOT a dict keyed by leg_id. A dict collapses duplicate or
        # missing leg_ids (all default to "(unknown)") so two real legs would
        # overwrite each other and only one cost would count toward the budget,
        # silently understating OVER_BUDGET. The list-summed integer counts every
        # leg's re-verified cost regardless of leg_id collisions.
        reverified_total: int = 0

        # ------------------------------------------------------------------
        # Gate 0a — UNCONDITIONAL empty-itinerary guard (D4 #4, fail-open fix)
        # ------------------------------------------------------------------
        # An itinerary with ZERO legs has no booked accommodation at all and must
        # NEVER verify, regardless of planned_leg_count. The planned_leg_count
        # backstop below cannot fire on this case (len([]) < 0 is False, and
        # planned_leg_count may be 0 or absent), so the empty plan would otherwise
        # fall through to decision="verified" — the last hard gate before the
        # single irreversible human consent approving an empty plan. Emit
        # MISSING_LEG (route_to accommodation) unconditionally.
        if len(legs) == 0:
            violations.append(_violation(
                MISSING_LEG, None,
                "Itinerary has zero legs — no accommodation booked. "
                "An empty itinerary can never be verified.",
            ))
            logger.warning("critic.verify: MISSING_LEG — empty itinerary (zero legs)")

        # ------------------------------------------------------------------
        # Gate 0b — Duplicate leg_id detection (D4 #8, malformed-input flag)
        # ------------------------------------------------------------------
        # Two legs sharing a leg_id (or both missing it → "(unknown)") indicate
        # malformed input. With the dict-keyed accumulator this used to silently
        # collapse their costs; we now sum over the list AND flag the collision
        # so the planner fixes the global state.
        seen_leg_ids: dict[str, int] = {}
        for leg in legs:
            lid = leg.get("leg_id", "(unknown)")
            seen_leg_ids[lid] = seen_leg_ids.get(lid, 0) + 1
        for dup_lid in sorted(lid for lid, n in seen_leg_ids.items() if n > 1):
            violations.append(_violation(
                DUPLICATE_LEG, dup_lid,
                f"Leg id {dup_lid!r} appears {seen_leg_ids[dup_lid]} times — "
                f"duplicate/missing leg_id (malformed itinerary).",
            ))
            logger.warning(
                "critic.verify: DUPLICATE_LEG leg_id=%s count=%d",
                dup_lid, seen_leg_ids[dup_lid],
            )

        # ------------------------------------------------------------------
        # Gate 0 — Planned leg count backstop (defense-in-depth, P0 §Layer 2)
        # ------------------------------------------------------------------
        if planned_leg_count is not None and len(legs) < planned_leg_count:
            violations.append(_violation(
                MISSING_LEG, None,
                f"Assembled {len(legs)} legs but planned trip requires {planned_leg_count} legs "
                f"— partial booking rejected.",
            ))
            logger.warning(
                "critic.verify: MISSING_LEG backstop — assembled=%d legs but planned=%d legs",
                len(legs), planned_leg_count,
            )

        # ------------------------------------------------------------------
        # Gate 1 — Coverage: every leg must have a hotel_id
        # ------------------------------------------------------------------
        for leg in legs:
            leg_id = leg.get("leg_id", "(unknown)")
            if not leg.get("hotel_id"):
                violations.append(_violation(
                    MISSING_LEG, leg_id,
                    f"Leg {leg_id!r} has no hotel_id — missing accommodation proposal.",
                ))

        # ------------------------------------------------------------------
        # Gate 2 — Date validity per leg (checkin < checkout, parseable)
        # ------------------------------------------------------------------
        for leg in legs:
            leg_id = leg.get("leg_id", "(unknown)")
            checkin_s = leg.get("checkin", "")
            checkout_s = leg.get("checkout", "")
            ci = _parse_date(checkin_s)
            co = _parse_date(checkout_s)
            if ci is None or co is None:
                violations.append(_violation(
                    INVALID_DATE, leg_id,
                    f"Leg {leg_id!r}: unparseable date(s) checkin={checkin_s!r} "
                    f"checkout={checkout_s!r}.",
                ))
            elif ci >= co:
                violations.append(_violation(
                    INVALID_DATE, leg_id,
                    f"Leg {leg_id!r}: checkin {checkin_s} is not before checkout "
                    f"{checkout_s} — invalid date range.",
                ))

        # ------------------------------------------------------------------
        # Gate 3 — Date contiguity: consecutive legs must connect
        # ------------------------------------------------------------------
        # SEV-2 fix: sort legs by checkin date before the gap/overlap check.
        # The transport-reorder path can deliver legs in non-date order, which
        # causes false DATE_OVERLAP/DATE_GAP violations when checking array order.
        # We sort a copy here; the caller's list is unchanged (for other checks).
        legs_by_date = sorted(
            legs,
            key=lambda leg: (_parse_date(leg.get("checkin", "")) or date.min),
        )
        # Check leg[k].checkout == leg[k+1].checkin, no gap, no overlap.
        for k in range(len(legs_by_date) - 1):
            curr = legs_by_date[k]
            nxt = legs_by_date[k + 1]
            curr_id = curr.get("leg_id", f"leg-{k}")
            nxt_id = nxt.get("leg_id", f"leg-{k+1}")
            curr_co = curr.get("checkout", "")
            nxt_ci = nxt.get("checkin", "")
            curr_co_d = _parse_date(curr_co)
            nxt_ci_d = _parse_date(nxt_ci)
            if curr_co_d is None or nxt_ci_d is None:
                # Date parse errors already flagged by Gate 2; skip contiguity check
                continue
            if curr_co_d < nxt_ci_d:
                violations.append(_violation(
                    DATE_GAP, nxt_id,
                    f"Gap between leg {curr_id!r} (checkout {curr_co}) and "
                    f"leg {nxt_id!r} (checkin {nxt_ci}) — {(nxt_ci_d - curr_co_d).days} day(s) missing.",
                ))
            elif curr_co_d > nxt_ci_d:
                violations.append(_violation(
                    DATE_OVERLAP, nxt_id,
                    f"Overlap between leg {curr_id!r} (checkout {curr_co}) and "
                    f"leg {nxt_id!r} (checkin {nxt_ci}) — legs overlap by "
                    f"{(curr_co_d - nxt_ci_d).days} day(s).",
                ))

        # ------------------------------------------------------------------
        # Gate 4 — Provenance present (traceability requirement, §4.6 #5)
        # ------------------------------------------------------------------
        for leg in legs:
            leg_id = leg.get("leg_id", "(unknown)")
            prov = leg.get("provenance")
            if not prov or str(prov).strip() == "":
                violations.append(_violation(
                    MISSING_PROVENANCE, leg_id,
                    f"Leg {leg_id!r}: no provenance tag — cannot trace to a backend fact.",
                ))

        # ------------------------------------------------------------------
        # Gate 5 — Price integrity (anti-hallucination): re-price via merchant
        # ------------------------------------------------------------------
        # Build merchant client (may use injected test transport)
        client_kwargs: dict[str, Any] = {"timeout": _MERCHANT_TIMEOUT}
        if self._merchant_transport is not None:
            client_kwargs["transport"] = self._merchant_transport

        review_scores: list[float] = []

        with httpx.Client(**client_kwargs) as client:
            for leg in legs:
                leg_id = leg.get("leg_id", "(unknown)")
                hotel_id = leg.get("hotel_id", "")
                checkin_s = leg.get("checkin", "")
                checkout_s = leg.get("checkout", "")
                adults = int(leg.get("adults", 1))
                proposal_cents = int(leg.get("total_cents", 0))

                if not hotel_id:
                    # Already flagged by MISSING_LEG; conservatively count the
                    # proposal cost toward the budget (never understate spend).
                    reverified_total += proposal_cents
                    continue

                try:
                    lookup = _lookup_catalog(
                        client, hotel_id, checkin_s, checkout_s, adults
                    )
                    merchant_total: int = int(lookup.get("total_cents", 0))
                    # D4 #8: integer sum over the legs LIST (not a leg_id-keyed
                    # dict) — every leg's cost counts even on id collisions.
                    reverified_total += merchant_total

                    # Collect review score for soft quality scoring
                    hotel_data = lookup.get("hotel") or {}
                    rs = hotel_data.get("review_score")
                    if rs is not None:
                        review_scores.append(float(rs))

                    # D4 #7: enforce room max_occupancy vs adults at VERIFY time.
                    # The merchant checkout REJECTS adults > max_occupancy at
                    # commit (reason=exceeds_room_capacity, checkout.go:142); the
                    # lookup_catalog re-price does NOT enforce it, so without this
                    # the Critic would pass a booking the commit gate will VOID
                    # after the single human consent. We mirror the merchant
                    # check using the max_occupancy the lookup blob already
                    # carries. (>0 guard: 0/absent means "no cap declared".)
                    max_occ = int(hotel_data.get("max_occupancy", 0) or 0)
                    if max_occ > 0 and adults > max_occ:
                        violations.append(_violation(
                            OVER_CAPACITY, leg_id,
                            f"Leg {leg_id!r} hotel {hotel_id!r}: {adults} adult(s) "
                            f"exceed room max_occupancy {max_occ} — merchant would "
                            f"VOID at checkout (exceeds_room_capacity).",
                        ))
                        logger.warning(
                            "critic.verify: OVER_CAPACITY leg=%s hotel=%s "
                            "adults=%d max_occupancy=%d",
                            leg_id, hotel_id, adults, max_occ,
                        )
                    elif max_occ <= 0 and adults > 1:
                        # max_occupancy is a standard catalog field; if the lookup
                        # blob did NOT carry a usable value we CANNOT prove a
                        # multi-adult party fits the room. Silently treating absence
                        # as "no cap" would pass a booking the commit gate may VOID
                        # (exceeds_room_capacity) AFTER the single human consent.
                        # Conservative: FLAG when capacity is unprovable. (A solo
                        # guest fits any bookable room, so adults<=1 is safe to pass.)
                        violations.append(_violation(
                            OVER_CAPACITY, leg_id,
                            f"Leg {leg_id!r} hotel {hotel_id!r}: {adults} adults but "
                            f"room max_occupancy is unknown (absent from lookup) — "
                            f"cannot verify capacity; conservative flag.",
                        ))
                        logger.warning(
                            "critic.verify: OVER_CAPACITY (unprovable capacity) "
                            "leg=%s hotel=%s adults=%d", leg_id, hotel_id, adults,
                        )

                    # Price integrity check: proposal must match merchant cents exactly
                    if merchant_total != proposal_cents:
                        violations.append(_violation(
                            PRICE_MISMATCH, leg_id,
                            f"Leg {leg_id!r} hotel {hotel_id!r}: proposal says "
                            f"{proposal_cents}¢ but merchant live price is "
                            f"{merchant_total}¢ — fabricated or stale price rejected.",
                        ))
                        logger.warning(
                            "critic.verify: PRICE_MISMATCH leg=%s hotel=%s "
                            "proposal=%d¢ merchant=%d¢",
                            leg_id, hotel_id, proposal_cents, merchant_total,
                        )

                except _AvailabilityError as exc:
                    # D3/D4 #48: sold-out / 409 / status=="unavailable" is an
                    # AVAILABILITY/coverage violation, NOT a price disagreement.
                    # Count the proposal cost conservatively toward the budget so
                    # spend is never understated, and route to accommodation to
                    # re-propose a different (available) hotel.
                    reverified_total += proposal_cents
                    violations.append(_violation(
                        UNAVAILABLE, leg_id,
                        f"Leg {leg_id!r} hotel {hotel_id!r}: {exc} — "
                        f"availability/coverage failure, re-propose a different hotel.",
                    ))
                    logger.warning(
                        "critic.verify: UNAVAILABLE leg=%s hotel=%s: %s",
                        leg_id, hotel_id, exc,
                    )

                except Exception as exc:
                    # Merchant lookup failed (timeout / 5xx / non-JSON) — cannot
                    # re-verify the price. Treat as price mismatch (anti-
                    # hallucination conservative reject). Count the proposal cost
                    # conservatively toward the budget.
                    reverified_total += proposal_cents
                    violations.append(_violation(
                        PRICE_MISMATCH, leg_id,
                        f"Leg {leg_id!r} hotel {hotel_id!r}: merchant lookup_catalog "
                        f"failed — {exc}. Cannot re-verify price.",
                    ))
                    logger.error(
                        "critic.verify: lookup_catalog failed for leg=%s hotel=%s: %s",
                        leg_id, hotel_id, exc,
                    )

        # ------------------------------------------------------------------
        # Gate 6 — Budget: sum of re-verified totals must be <= total_budget_cents
        # ------------------------------------------------------------------
        # reverified_total was accumulated as an integer sum over the legs LIST
        # in Gate 5 (D4 #8) — duplicate/missing leg_ids no longer collapse costs.
        if reverified_total > total_budget_cents:
            violations.append(_violation(
                OVER_BUDGET, None,
                f"Re-verified package total {reverified_total}¢ exceeds "
                f"budget {total_budget_cents}¢ by "
                f"{reverified_total - total_budget_cents}¢.",
            ))
            logger.warning(
                "critic.verify: OVER_BUDGET reverified=%d¢ budget=%d¢",
                reverified_total, total_budget_cents,
            )

        # ------------------------------------------------------------------
        # Gate 6b — [Fraud] Counterparty-solvency re-check (commit-time, N1).
        # ------------------------------------------------------------------
        # Defense-in-depth: re-derive each leg's counterparty solvency band from
        # the SAME seeded fraud table the Fraud agent uses, INDEPENDENTLY of any
        # advisory verdict the orchestrator may (or may not) have obtained. A
        # blocked/unknown counterparty is rejected here unless an explicit fresh
        # consent token was supplied for it — so a missed Fraud verdict can never
        # slip an insolvent supplier through to the irreversible commit. The
        # counterparty id IS the catalog id (§19.1) — here the leg's hotel_id (or
        # an explicit counterparty_id), so no second namespace.
        # Backward-compatible: if fraud_agent is unavailable, this is a no-op.
        # Scoping (keeps S1–S5 byte-identical): a normal leg names only a catalog
        # `hotel_id` that has no fraud profile (most lodging) — that is NOT a fraud
        # signal and must NOT be blocked here (else every ordinary booking trips the
        # gate). The Critic re-check therefore fires when EITHER:
        #   (a) the counterparty has a KNOWN-blocked band (positive distress signal,
        #       from the seed table) — always block without fresh consent; OR
        #   (b) the leg carries an EXPLICIT `counterparty_id` (the planner/Fraud
        #       declared a vetted supplier) — then UNKNOWN degrades conservatively
        #       too (never silent-OK for a declared counterparty).
        # The Fraud agent itself always blocks UNKNOWN when explicitly vetting; this
        # is the commit-time defense-in-depth re-derivation, not a new policy.
        consent_tokens = counterparty_consent_tokens or {}
        if _fraud_vet_counterparty is not None:
            for leg in legs:
                leg_id = leg.get("leg_id", "(unknown)")
                explicit_cp = leg.get("counterparty_id")
                cp_id = explicit_cp or leg.get("hotel_id") or ""
                if not cp_id:
                    continue  # MISSING_LEG already covers a leg with no counterparty
                token = consent_tokens.get(cp_id)
                try:
                    fv = _fraud_vet_counterparty(cp_id, consent_token=token)
                except Exception as exc:  # pragma: no cover - never block on a bug
                    logger.error(
                        "critic.verify: fraud re-check raised for leg=%s cp=%s: %s",
                        leg_id, cp_id, exc,
                    )
                    continue
                band = fv.get("risk_band")
                # (a) known-blocked → always gated; (b) any non-committable band on
                # an EXPLICIT counterparty (incl. unknown) → gated. A bare hotel_id
                # with no profile (unknown, no explicit counterparty) is left alone.
                gate = (band == _FraudRiskBand.BLOCKED) or (
                    explicit_cp is not None and not fv.get("committable", True)
                )
                if gate and not fv.get("committable", True):
                    violations.append(_violation(
                        COUNTERPARTY_BLOCKED, leg_id,
                        f"Leg {leg_id!r} counterparty {cp_id!r}: solvency risk_band "
                        f"{band!r} (score "
                        f"{fv.get('solvency_score')}) — NOT committable without "
                        f"explicit fresh consent (pre-empts N1 supplier-insolvency "
                        f"VOID). {fv.get('gate_reason') or ''}".rstrip(),
                    ))
                    logger.warning(
                        "critic.verify: COUNTERPARTY_BLOCKED leg=%s cp=%s band=%s",
                        leg_id, cp_id, band,
                    )

        # ------------------------------------------------------------------
        # Gate 7 — [M3b] Transport feasibility gate
        # ------------------------------------------------------------------
        # If transport_result is provided, check for infeasible_edges.
        # If absent → backward-compatible (no violation, transport_checked=False).
        transport_checked = False
        if transport_result is not None:
            infeasible_edges = transport_result.get("infeasible_edges", [])
            if infeasible_edges:
                # Build a compact summary of what's infeasible
                edge_summary = "; ".join(
                    f"{e.get('from_leg','?')}→{e.get('to_leg','?')} "
                    f"({e.get('from_city','?')}→{e.get('to_city','?')}, "
                    f"reason={e.get('reason','unknown')})"
                    for e in infeasible_edges
                )
                violations.append(_violation(
                    TRANSPORT_INFEASIBLE, None,
                    f"Transport feasibility check failed — "
                    f"{len(infeasible_edges)} infeasible transfer(s): {edge_summary}",
                ))
                logger.warning(
                    "critic.verify: TRANSPORT_INFEASIBLE — %d infeasible edge(s)",
                    len(infeasible_edges),
                )
            elif transport_result.get("unverified_edges"):
                # #70: NO genuine-infeasible edge (so NO violation, NO reject) — but some hops
                # are UNVERIFIED (no seeded transfer time). Honest posture (mirrors the
                # orchestrator + compliance/health): surface as advisory, do NOT claim
                # transport_checked=True. We examined the route but could not fully verify it.
                transport_checked = False
                logger.info(
                    "critic.verify: %d unverified transfer(s) — advisory, NOT a violation; "
                    "transport_checked stays False (honest: not fully verified)",
                    len(transport_result.get("unverified_edges", [])),
                )
            else:
                # D4 #49: only assert transport_checked=True on a POSITIVE
                # feasibility signal from the transport agent — an explicit
                # "checked"/"feasible" flag, or a non-empty "edges" list it
                # actually examined. A malformed/empty {} (no edges examined)
                # must NOT flip this honesty signal to a false "checked" claim;
                # the mere ABSENCE of infeasible_edges is not proof of a check.
                positive_signal = (
                    bool(transport_result.get("checked"))
                    or bool(transport_result.get("feasible"))
                    or bool(transport_result.get("edges"))
                    or "infeasible_edges" in transport_result  # key present + empty == examined, none infeasible
                )
                if positive_signal:
                    transport_checked = True
                    logger.info(
                        "critic.verify: transport_checked=True (positive feasibility signal)"
                    )
                else:
                    logger.info(
                        "critic.verify: transport_result present but no positive "
                        "feasibility signal — transport_checked stays False"
                    )

        # ------------------------------------------------------------------
        # Soft quality score (not a gate — used for ranking)
        # ------------------------------------------------------------------
        # Component 1: avg review score normalised to [0, 1] (scores are 0–10)
        avg_review_norm = (
            (sum(review_scores) / len(review_scores)) / 10.0
            if review_scores else 0.5  # no data → neutral
        )
        # Component 2: budget utilisation fit — 1.0 when total == budget;
        #              degrades linearly as utilisation deviates in either direction.
        if total_budget_cents > 0:
            utilisation = reverified_total / total_budget_cents
            utilisation_fit = max(0.0, 1.0 - abs(1.0 - utilisation))
        else:
            utilisation_fit = 0.0

        quality_score = (avg_review_norm + utilisation_fit) / 2.0

        # ------------------------------------------------------------------
        # Decision
        # ------------------------------------------------------------------
        decision = "verified" if not violations else "rejected"
        logger.info(
            "critic.verify: decision=%s violations=%d "
            "reverified_total=%d¢ budget=%d¢ quality=%.3f transport_checked=%s",
            decision, len(violations), reverified_total,
            total_budget_cents, quality_score, transport_checked,
        )

        return _critic_result(
            decision=decision,
            quality_score=quality_score,
            violations=violations,
            reverified_total_cents=reverified_total,
            transport_checked=transport_checked,
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
    port = int(os.environ.get("PORT", 9103))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = CriticAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Critic agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)
    logger.info("Merchant MCP URL: %s", MERCHANT_MCP_URL)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
