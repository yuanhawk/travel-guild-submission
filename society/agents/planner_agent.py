"""
planner_agent.py — Planner A2A agent (Travel Guild M2).

Design contract: AGENT-SOCIETY-A2A-DESIGN.md §3.1, §4.2 R0, §2.1 (DP allocator).

Agent Card skill: ``plan.decompose``

Input (data part, JSON):
    {
        "user_id":            str,
        "total_budget_cents": int,
        "legs": [
            {"city": str, "checkin": str, "checkout": str, "adults": int, "vibe": str (optional)}
        ],
        "preferences": dict  (optional),

        # DP allocator extension (§2.1):
        # When per_leg_candidates is supplied, the Planner runs multiple-choice-knapsack
        # DP to find the globally budget-optimal hotel combination, using these pre-gathered
        # candidate lists.  When absent, falls back to proportional-split (old behaviour).
        "per_leg_candidates": [
            {
                "leg_id":     str,    # matches leg index order
                "candidates": [
                    {
                        "hotel_id":      str,
                        "total_cents":   int,
                        "quality":       float,   # pre-attached by orchestrator
                        "review_score":  float,
                        ...
                    }
                ]
            }
        ]  (optional)
    }

Output artifact (data part, JSON) — typed ItinerarySkeleton:
    {
        "type":               "itinerary_skeleton",
        "user_id":            str,
        "total_budget_cents": int,
        "total_nights":       int,
        "dp_allocation":      bool,   # True if DP was used; False if proportional fallback
        # Risk propagation (D2): the Risk agent's hazard directives, injected by
        # the orchestrator as `risk_directives`, are NEVER silently dropped — they
        # surface here so the hazard reaches the itinerary.
        "risk_flagged":       bool,   # True if any Risk directive applied to this trip
        "risk_advisories":    [str],  # human-readable hazard advisory texts (sorted, deduped)
        "risk_reason_codes":  [str],  # Risk reason codes carried from the rollup
        "dp_error":           bool,   # True if the DP allocator failed unexpectedly and
                                      #   the plan degraded to proportional split (NOT silent)
        "legs": [
            {
                "leg_id":               str,         # "leg-0", "leg-1", ...
                "city":                 str,
                "checkin":              str,
                "checkout":             str,
                "adults":               int,
                "nights":               int,
                "vibe":                 str | null,
                "per_leg_budget_cents": int,
                "hard_constraints":     {"max_cents": int},
                # Risk propagation (D2), per-leg:
                "risk_flags":           [str],  # reason codes if this leg is flagged, else []
                "avoid_window":         bool,   # True if a Risk avoid-window applies
                "buffer_connection_min": int,   # connection buffer to honour, else 0
                # DP-only: when DP allocation succeeded, these carry the
                # globally-optimal pre-selected hotel for this leg:
                "dp_selected_hotel_id": str | null,
                "dp_selected_total_cents": int | null,
                "dp_selected_quality": float | null,
            }
        ]
    }

Budget allocation strategy (§2.1):
  PRIMARY (USE_DP_ALLOCATOR=true, the default):
    Multiple-choice-knapsack DP over all candidate hotels, all legs.
    Globally budget-optimal (maximises Σ quality s.t. Σ cost ≤ budget).
    Deterministic — same inputs → same outputs always.
    Falls through to proportional-split if candidates not supplied.

  FALLBACK (USE_DP_ALLOCATOR=false, or no candidates supplied):
    Proportional-by-nights split (original behaviour).
    per_leg_i = floor(nights_i / total_nights * total_budget_cents)
    Remainder added to the last leg.

§4.6 reliability:
  - Typed output schema (all fields explicit).
  - Validates: user_id required, total_budget_cents > 0, legs non-empty.
  - Each leg gets a unique leg_id ("leg-N") for tracking across negotiation rounds.
  - DP allocator: deterministic, zero variance, O(n_legs × B × C) microseconds.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date, timedelta

import uvicorn

from agents.a2a_agent import (
    A2AAgent,
    _data_part,
    _new_artifact,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag: set USE_DP_ALLOCATOR=false to revert to proportional split
# (for backward-compatibility with existing tests/benchmark arms that don't
# supply per_leg_candidates, or to isolate the DP improvement in A/B tests).
# Default: true (DP is the new primary path).
# ---------------------------------------------------------------------------

_USE_DP_ALLOCATOR: bool = os.environ.get("USE_DP_ALLOCATOR", "true").lower() not in (
    "0", "false", "no", "off"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    """Parse YYYY-MM-DD date string."""
    return date.fromisoformat(s)


def _nights_between(checkin: str, checkout: str) -> int:
    """
    Return the number of nights between checkin and checkout dates.

    Raises ValueError for any invalid date range:
      - malformed / missing date strings
      - same-day checkout (0 nights)
      - reversed dates (checkout before checkin)

    HONESTY / fail-conservative: we never fabricate a 1-night stay from
    bad input.  The caller must supply valid forward-facing dates.
    """
    try:
        ci = _parse_date(checkin)
        co = _parse_date(checkout)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"_nights_between: malformed date(s) checkin={checkin!r} "
            f"checkout={checkout!r} — {exc}"
        ) from exc
    n = (co - ci).days
    if n <= 0:
        raise ValueError(
            f"_nights_between: invalid date range checkin={checkin!r} "
            f"checkout={checkout!r} — checkout must be strictly after checkin "
            f"(computed {n} nights)"
        )
    return n


def _proportional_split(
    nights_per_leg: list[int],
    total_budget_cents: int,
) -> list[int]:
    """
    Split total_budget_cents proportionally by nights per leg.

    Uses floor for each leg; remainder (to correct for integer truncation)
    is added to the last leg.

    This is the LEGACY/FALLBACK path. The primary path is the DP allocator.
    Preserved for backward-compat (USE_DP_ALLOCATOR=false) and as the
    per-leg ceiling when DP candidates are not supplied.
    """
    total_nights = sum(nights_per_leg)
    if total_nights == 0:
        # Edge case: split equally
        per = total_budget_cents // len(nights_per_leg)
        return [per] * len(nights_per_leg)

    # var-0: pure INTEGER money math (no float division/multiply on the money
    # path). alloc_i = nights_i * budget // total_nights; the integer-truncation
    # remainder is folded into the LAST leg so the sum is exact.
    allocations = [
        (n * total_budget_cents) // total_nights
        for n in nights_per_leg
    ]
    remainder = total_budget_cents - sum(allocations)
    # remainder is always >= 0 with integer floor division, but clamp defensively.
    allocations[-1] = max(0, allocations[-1] + remainder)
    return allocations


# ---------------------------------------------------------------------------
# PlannerAgent
# ---------------------------------------------------------------------------

class PlannerAgent(A2AAgent):
    """
    Planner A2A agent — decomposes a multi-leg trip into an itinerary skeleton
    with per-leg budget allocations.

    Authority: decompose, sequence, allocate budget.
    No spend, no data fabrication, no feasibility ruling.

    DP allocation (§2.1):
        When per_leg_candidates are supplied in the request, runs the
        multiple-choice-knapsack DP allocator to find the globally budget-optimal
        hotel combination.  The DP result pre-selects one hotel per leg and sets
        per_leg_budget_cents = that hotel's total_cents (exact, not approximate).
        The orchestrator uses these pre-selections as the first proposals, skipping
        the greedy proportional-split + per-leg-ceiling approach.

        When per_leg_candidates are NOT supplied (e.g., unit tests or old callers),
        falls back to proportional-split (identical to the old behaviour).

    Args:
        host: Bind host for the ASGI server.
        port: Bind port for the ASGI server.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9102,
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
            "name": "planner-agent",
            "description": (
                "Decomposes a multi-leg trip request into a structured itinerary skeleton "
                "with per-leg budget allocations. Authority: decompose, sequence, allocate "
                "budget. No spend, no data fabrication, no feasibility ruling. "
                "Primary path (§2.1): multiple-choice-knapsack DP for globally budget-optimal "
                "hotel selection when per_leg_candidates are supplied. "
                "Fallback: proportional-by-nights split (backward-compatible). "
                "Implements A2A skill 'plan.decompose'. "
                "Part of the Travel Guild multi-agent pipeline (Track 3, M2/DP)."
            ),
            "url": url,
            "version": "2.0.0",
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
                    "id": "plan.decompose",
                    "name": "Decompose Trip",
                    "description": (
                        "Given a trip request with total_budget_cents and legs[], "
                        "optionally per_leg_candidates (for DP allocation), "
                        "return an itinerary skeleton with per-leg budget ceilings "
                        "and (if DP ran) globally-optimal pre-selected hotels. "
                        "Each leg gets a leg_id for tracking across negotiation rounds."
                    ),
                    "tags": ["planning", "budget", "itinerary", "decompose", "dp", "knapsack"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "user_id": {"type": "string"},
                            "total_budget_cents": {"type": "integer"},
                            "legs": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "city": {"type": "string"},
                                        "checkin": {"type": "string"},
                                        "checkout": {"type": "string"},
                                        "adults": {"type": "integer"},
                                        "vibe": {"type": "string"},
                                    },
                                    "required": ["city", "checkin", "checkout"],
                                },
                            },
                            "preferences": {"type": "object"},
                            "per_leg_candidates": {
                                "type": "array",
                                "description": (
                                    "Pre-gathered hotel candidates per leg (from Accommodation). "
                                    "When supplied, DP allocation is used instead of proportional split."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "leg_id": {"type": "string"},
                                        "candidates": {"type": "array"},
                                    },
                                },
                            },
                            "risk_directives": {
                                "type": "object",
                                "description": (
                                    "Risk agent hazard roll-up injected by the orchestrator "
                                    "(avoid_window, buffer_connection_min, flagged_legs, "
                                    "reason_codes, prefer_flexible_cancellation). Propagated "
                                    "into the skeleton (risk_flagged / per-leg risk_flags) so "
                                    "the hazard is never silently dropped."
                                ),
                                "properties": {
                                    "avoid_window": {"type": "boolean"},
                                    "buffer_connection_min": {"type": "integer"},
                                    "flagged_legs": {"type": "array"},
                                    "reason_codes": {"type": "array"},
                                    "prefer_flexible_cancellation": {"type": "boolean"},
                                },
                            },
                        },
                        "required": ["user_id", "total_budget_cents", "legs"],
                    },
                    "examples": [
                        (
                            '{"user_id":"u1","total_budget_cents":80000,"legs":['
                            '{"city":"bali","checkin":"2025-10-01","checkout":"2025-10-04",'
                            '"adults":1,"vibe":"culture"},'
                            '{"city":"bali","checkin":"2025-10-04","checkout":"2025-10-08",'
                            '"adults":1,"vibe":"beach"}]}'
                        ),
                    ],
                }
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("plan.decompose", self._decompose_handler)

    # ------------------------------------------------------------------
    # Skill handler
    # ------------------------------------------------------------------

    async def _decompose_handler(self, message: dict, task: dict) -> dict:
        """
        plan.decompose skill handler.

        Extracts the trip request, validates it, and:
          - If per_leg_candidates are supplied AND USE_DP_ALLOCATOR=true:
            runs the multiple-choice-knapsack DP allocator (§2.1 primary path).
          - Otherwise: splits the budget proportionally by nights (legacy path).

        Returns a typed ItinerarySkeleton artifact.
        """
        payload = self._extract_payload(message)
        if payload is None:
            raise ValueError(
                "plan.decompose requires a data part with JSON payload "
                "{user_id, total_budget_cents, legs}"
            )

        user_id: str = payload.get("user_id", "")
        total_budget_cents: int = int(payload.get("total_budget_cents", 0))
        legs_input: list[dict] = payload.get("legs", [])
        preferences: dict = payload.get("preferences") or {}
        per_leg_candidates: list[dict] | None = payload.get("per_leg_candidates")

        # --- D2: extract Risk hazard directives (orchestrator-injected) ---
        # The orchestrator folds the Risk roll-up into the payload as
        # `risk_directives` ONLY when Risk produced a condition (avoid window /
        # connection buffer / flagged leg). The Planner MUST NOT silently drop
        # this hazard signal — it propagates it into the output skeleton so the
        # hazard reaches the itinerary (HONESTY thesis: flag, never omit).
        risk_directives: dict = payload.get("risk_directives") or {}
        risk_avoid_window: bool = bool(risk_directives.get("avoid_window"))
        risk_buffer_min: int = int(risk_directives.get("buffer_connection_min", 0) or 0)
        # flagged_legs is a list of leg_id strings (e.g. "leg-0") matching the
        # planner's own leg-{i} convention; use a set for membership but NEVER
        # iterate it to choose output (var-0).
        risk_flagged_legs: set[str] = {
            str(x) for x in (risk_directives.get("flagged_legs") or [])
        }
        risk_reason_codes: list[str] = [
            str(c) for c in (risk_directives.get("reason_codes") or [])
        ]
        risk_prefer_flexible: bool = bool(
            risk_directives.get("prefer_flexible_cancellation")
        )
        # Top-level: this trip carries a Risk hazard if any directive component
        # is present. (The orchestrator only injects risk_directives when a real
        # condition exists, but recompute defensively from the contents.)
        risk_flagged: bool = bool(
            risk_avoid_window
            or risk_buffer_min > 0
            or risk_flagged_legs
            or risk_reason_codes
        )

        # --- Validation ---
        if not user_id:
            raise ValueError("user_id is required")
        if total_budget_cents <= 0:
            raise ValueError("total_budget_cents must be > 0")
        if not legs_input:
            raise ValueError("legs must be a non-empty list")

        # --- Compute per-leg nights ---
        nights_per_leg = [
            _nights_between(leg.get("checkin", ""), leg.get("checkout", ""))
            for leg in legs_input
        ]
        total_nights = sum(nights_per_leg)

        # --- Try DP allocation if candidates supplied ---
        dp_used = False
        dp_error = False  # set True when DP fails UNEXPECTEDLY → degraded, NOT silent
        dp_allocator_result: dict | None = None

        if (
            _USE_DP_ALLOCATOR
            and per_leg_candidates
            and len(per_leg_candidates) == len(legs_input)
        ):
            try:
                from utils.allocator import allocate
                alloc_result = allocate(per_leg_candidates, total_budget_cents)
                if alloc_result["feasible"]:
                    dp_allocator_result = alloc_result
                    dp_used = True
                    logger.info(
                        "plan.decompose: DP allocation succeeded — "
                        "budget=%d¢ total_selected=%d¢ quality=%.3f legs=%d",
                        total_budget_cents,
                        alloc_result["total_cents"],
                        alloc_result["total_quality"],
                        len(alloc_result["selection"]),
                    )
                else:
                    # DP returned infeasible — propagate as cannot_satisfy signal
                    logger.info(
                        "plan.decompose: DP returned infeasible (budget=%d¢) — "
                        "raising to trigger cannot_satisfy",
                        total_budget_cents,
                    )
                    raise ValueError(
                        f"cannot_satisfy: DP precheck: minimum hotel costs exceed "
                        f"budget {total_budget_cents}¢ — no feasible combination exists."
                    )
            except ValueError:
                raise
            except Exception as exc:
                # #31: an UNEXPECTED allocator failure must NOT degrade silently.
                # Fall back to a proportional plan but STAMP the skeleton with an
                # explicit dp_error flag so the degraded path is visible to
                # downstream agents and the user (conservative: flag, never silent).
                logger.warning(
                    "plan.decompose: DP allocator raised unexpected error: %s — "
                    "falling back to proportional split (dp_error flagged)",
                    exc,
                )
                dp_used = False
                dp_error = True

        # --- #46: validate DP leg_id coverage BEFORE declaring success ---
        # The DP path keys selections on the allocator's leg_id; the planner's
        # own convention is "leg-{i}". If a caller supplies per_leg_candidates
        # whose leg_ids don't match (count equal but ids differ), the output must
        # NOT claim dp_allocation=True while legs silently get proportional
        # budgets and null dp fields. Require EVERY leg-{i} to be present in the
        # DP selection; if any is missing, drop to the honest proportional path.
        if dp_used and dp_allocator_result:
            dp_selection_ids = {
                s["leg_id"] for s in dp_allocator_result["selection"]
            }
            expected_ids = {f"leg-{i}" for i in range(len(legs_input))}
            if not expected_ids <= dp_selection_ids:
                missing = sorted(expected_ids - dp_selection_ids)
                logger.warning(
                    "plan.decompose: DP selection leg_id mismatch — legs %s not "
                    "covered by allocator selection; treating as proportional "
                    "(dp_allocation=False) so the artifact is not mislabelled.",
                    missing,
                )
                dp_used = False
                dp_allocator_result = None

        # --- Proportional split (legacy/fallback path) ---
        if not dp_used:
            allocations = _proportional_split(nights_per_leg, total_budget_cents)
        else:
            # Use DP-selected totals as the per-leg budget ceilings.
            # Each selected hotel's cost IS the per-leg allocation.
            # Coverage was validated above, so every leg-{i} is present.
            dp_by_leg_id: dict[str, dict] = {
                s["leg_id"]: s for s in dp_allocator_result["selection"]
            }
            allocations = [
                dp_by_leg_id[f"leg-{i}"]["total_cents"]
                for i in range(len(legs_input))
            ]

        # --- Build output skeleton ---
        dp_by_leg_id_for_output: dict[str, dict] = {}
        if dp_used and dp_allocator_result:
            dp_by_leg_id_for_output = {
                s["leg_id"]: s for s in dp_allocator_result["selection"]
            }

        skeleton_legs = []
        for i, (leg, nights, alloc) in enumerate(
            zip(legs_input, nights_per_leg, allocations)
        ):
            leg_id = f"leg-{i}"
            dp_sel = dp_by_leg_id_for_output.get(leg_id)
            # D2: per-leg Risk propagation. A leg is flagged if its leg_id is in
            # the Risk rollup's flagged_legs. The avoid-window / buffer signals
            # are trip-level in the directive, so we apply them to flagged legs
            # (and to every leg when the directive flags the whole trip without
            # naming legs — conservative: surface the hazard rather than drop it).
            leg_is_flagged = leg_id in risk_flagged_legs
            apply_trip_risk = leg_is_flagged or (
                risk_flagged and not risk_flagged_legs
            )
            leg_risk_flags = list(risk_reason_codes) if apply_trip_risk else []
            leg_out: dict = {
                "leg_id": leg_id,
                "city": leg.get("city", ""),
                "checkin": leg.get("checkin", ""),
                "checkout": leg.get("checkout", ""),
                "adults": int(leg.get("adults", 1)),
                "nights": nights,
                "vibe": leg.get("vibe") or None,
                "per_leg_budget_cents": alloc,
                "hard_constraints": {"max_cents": alloc},
                # D2 Risk propagation (per-leg): the hazard reaches the itinerary.
                "risk_flags": leg_risk_flags,
                "avoid_window": bool(apply_trip_risk and risk_avoid_window),
                "buffer_connection_min": (
                    risk_buffer_min if apply_trip_risk else 0
                ),
                # DP extension fields (null when proportional path)
                "dp_selected_hotel_id": dp_sel["hotel_id"] if dp_sel else None,
                "dp_selected_total_cents": dp_sel["total_cents"] if dp_sel else None,
                "dp_selected_quality": dp_sel["quality"] if dp_sel else None,
            }
            skeleton_legs.append(leg_out)

        # D2: build human-readable hazard advisory texts (sorted + deduped → var-0).
        risk_advisories: list[str] = []
        if risk_flagged:
            if risk_flagged_legs:
                risk_advisories.append(
                    "Risk flagged leg(s) "
                    + ", ".join(sorted(risk_flagged_legs))
                    + " — review hazard before booking."
                )
            else:
                risk_advisories.append(
                    "Risk flagged this trip — review hazard before booking."
                )
            if risk_avoid_window:
                risk_advisories.append(
                    "Risk avoid-window in effect — avoid the flagged travel window."
                )
            if risk_buffer_min > 0:
                risk_advisories.append(
                    f"Risk recommends a connection buffer of {risk_buffer_min} min."
                )
            if risk_prefer_flexible:
                risk_advisories.append(
                    "Risk recommends flexible-cancellation bookings."
                )
            for rc in sorted(set(risk_reason_codes)):
                risk_advisories.append(f"Risk reason code: {rc}")
        # Stable, deduped order (var-0: no set/dict iteration drives output).
        risk_advisories = sorted(dict.fromkeys(risk_advisories))

        if dp_error:
            logger.warning(
                "plan.decompose: returning degraded plan (dp_error=True) for "
                "user=%s — DP allocator failed; proportional split used.",
                user_id,
            )

        result = {
            "type": "itinerary_skeleton",
            "user_id": user_id,
            "total_budget_cents": total_budget_cents,
            "total_nights": total_nights,
            "dp_allocation": dp_used,
            # #31: visible degraded flag when the DP allocator failed unexpectedly.
            "dp_error": dp_error,
            # D2: Risk hazard signal propagated — NEVER silently dropped.
            "risk_flagged": risk_flagged,
            "risk_advisories": risk_advisories,
            "risk_reason_codes": list(risk_reason_codes),
            "legs": skeleton_legs,
        }

        logger.info(
            "plan.decompose: user=%s budget=%d¢ legs=%d total_nights=%d "
            "dp_used=%s split=%s",
            user_id, total_budget_cents, len(skeleton_legs), total_nights,
            dp_used, [a for a in allocations],
        )

        return _new_artifact(
            name="plan.decompose.result",
            parts=[_data_part(result)],
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
    port = int(os.environ.get("PORT", 9102))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = PlannerAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Planner agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)
    logger.info("DP allocator enabled: %s", _USE_DP_ALLOCATOR)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
