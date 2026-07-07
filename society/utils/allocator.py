"""
allocator.py — Exact-optimal multiple-choice-knapsack allocator (Track-3 Travel Guild §2.1).

Design contract: the internal design spec §2.1 (complexity-reduction),
                 §3.1 (Planner authority), §10.11 (determinism/variance-0).

## Purpose

Replaces the proportional-budget-split + greedy-per-leg approach with a single
multiple-choice-knapsack DP that picks the globally budget-optimal hotel-per-leg
combination.

## API

    result = allocate(legs_with_candidates, total_budget_cents, bucket_cents=2000)

    legs_with_candidates: list of per-leg dicts, each with:
        {
            "leg_id":     str,
            "candidates": [
                {
                    "hotel_id":    str,
                    "total_cents": int,
                    "quality":     float,   # pre-computed quality score
                    ... (other fields passed through to selection output)
                },
                ...
            ]
        }

    Returns:
        {
            "feasible":       bool,
            "selection":      [{"leg_id", "hotel_id", "total_cents", "quality", ...}, ...],
            "total_cents":    int,
            "total_quality":  float,
        }

    On infeasibility (Σ min_cost > budget): returns feasible=False, selection=[],
    total_cents=0, total_quality=0.0. Zero DP work.

    The `bucket_cents` parameter is accepted for backward compatibility but is not
    used; the algorithm operates on exact cents with no discretization.

## Quality score convention (LLM-ranking integration)

The caller (Planner/orchestrator) pre-computes the `quality` field on each
candidate before calling `allocate()`.  Two modes:

  1. LLM-ranked candidates (LLM ranking active):
     `quality = (n_candidates - 1 - rank_index) / (n_candidates - 1)` (SEV-3 fix),
     normalised to [0,1] per-leg so legs with more candidates do not dominate
     the global DP sum.  Top-ranked = 1.0, worst-ranked = 0.0.

  2. Deterministic fallback (no LLM ranking):
     `quality = review_score` from the merchant catalog.
     The DP maximises average review score across legs.

In both cases the DP objective is deterministic given the candidate list.

## Algorithm

Exact-cost multiple-choice knapsack with Pareto-front pruning.

State: a dict mapping exact total cost in cents (int) to the best total quality
(float) achievable with that exact cost over legs processed so far.

Transition (for each leg i):
    new_states = {}
    for (prev_cost, prev_quality) in current_states:
        for candidate j of leg i:
            new_cost = prev_cost + candidate.total_cents
            if new_cost > budget: skip
            new_quality = prev_quality + candidate.quality
            if new_cost not in new_states or new_quality > new_states[new_cost]:
                new_states[new_cost] = new_quality

Pareto pruning: after each leg, remove any state (cost, q) that is dominated by
another state (cost', q') where cost' <= cost and q' >= q.  This keeps the state
set small — in practice O(candidates/leg) growth per leg, easily < 10,000 states
for our instances.

The backtrack table stores, for each (leg_index, cost) state, the (prev_cost,
candidate_index) tuple needed to reconstruct the selection.

Final answer: pick the state with maximum quality among all states with
cost <= budget.

Complexity: O(n_legs × |states| × candidates/leg) where |states| ≤ product of
candidates/leg after Pareto pruning.  For our instances (≤ 4 legs, ≤ 8
candidates/leg): |states| ≤ ~512 after pruning, typically far fewer.
Total ops ≤ 4 × 512 × 8 ≈ 16,384 — microseconds, exactly optimal, deterministic.

No discretization loss.  Every feasible combination that fits within budget is
considered with its exact cost.

## Determinism guarantees

- No randomness anywhere.
- Tie-breaking: for equal total_quality, the selection with LOWER total_cents is
  preferred.  Among equal (quality, cost), the first-encountered candidate wins,
  which respects LLM-ranked order (preference-stable).
- Fully reproducible: same inputs → same outputs always.

## Backward compatibility

The old `_proportional_split` logic in planner_agent.py is preserved and
available via `USE_DP_ALLOCATOR=false` env variable.  The Planner calls
`allocate_or_split()` which dispatches to DP or proportional based on the flag.
The `bucket_cents` parameter is accepted (no-op) so existing call sites need no
changes.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NEG_INF: float = float("-inf")

# ---------------------------------------------------------------------------
# Core: exact-cost multiple-choice knapsack with Pareto pruning
# ---------------------------------------------------------------------------


def allocate(
    legs_with_candidates: list[dict[str, Any]],
    total_budget_cents: int,
    bucket_cents: int = 2000,  # accepted for backward compat; not used
) -> dict[str, Any]:
    """
    Exact-optimal multiple-choice knapsack: pick exactly one hotel per leg to
    maximise total quality subject to total_cents <= total_budget_cents.

    Uses exact cent values (no bucketing/discretization) with Pareto-front
    pruning to keep the state space small.  Guaranteed to find the true
    global optimum on every instance.

    Args:
        legs_with_candidates: List of leg dicts, each containing:
            - "leg_id": str
            - "candidates": list of candidate dicts, each with:
                - "hotel_id": str
                - "total_cents": int  (TOTAL stay cost, not per-night)
                - "quality": float   (higher = better; see module docstring)
                - ... (any other fields; passed through to selection)
        total_budget_cents: Hard budget ceiling (inclusive).
        bucket_cents: Accepted for backward compatibility; not used.

    Returns a dict:
        {
            "feasible":      bool,
            "selection":     [{"leg_id", "hotel_id", "total_cents", "quality"}, ...],
            "total_cents":   int,
            "total_quality": float,
        }

    Feasibility:
        If Sigma(min total_cents per leg) > total_budget_cents, returns
        feasible=False with zero DP work (O(n_legs) precheck).
        If any leg has ZERO candidates, returns feasible=False immediately.
    """
    n_legs = len(legs_with_candidates)
    _EMPTY: dict[str, Any] = {
        "feasible": False,
        "selection": [],
        "total_cents": 0,
        "total_quality": 0.0,
    }

    if n_legs == 0:
        logger.info("allocator: no legs — trivially feasible (empty selection)")
        return {"feasible": True, "selection": [], "total_cents": 0, "total_quality": 0.0}

    if total_budget_cents <= 0:
        logger.info("allocator: total_budget_cents=%d <= 0 — infeasible", total_budget_cents)
        return _EMPTY

    # ------------------------------------------------------------------
    # Step 1: Validate and preprocess legs
    # ------------------------------------------------------------------

    # Validate each leg has at least one candidate with positive cost
    processed_legs: list[dict[str, Any]] = []
    for leg in legs_with_candidates:
        leg_id = leg.get("leg_id", "?")
        raw_cands = leg.get("candidates") or []
        # Filter out candidates with non-positive cost (sanity guard)
        cands = [
            c for c in raw_cands
            if isinstance(c.get("total_cents"), (int, float)) and int(c["total_cents"]) > 0
        ]
        if not cands:
            logger.info(
                "allocator: leg=%s has 0 valid candidates — infeasible", leg_id
            )
            return _EMPTY
        processed_legs.append({"leg_id": leg_id, "candidates": cands})

    # ------------------------------------------------------------------
    # Step 2: Min-cost feasibility precheck (O(n_legs))
    # ------------------------------------------------------------------

    min_total = sum(
        min(int(c["total_cents"]) for c in leg["candidates"])
        for leg in processed_legs
    )
    if min_total > total_budget_cents:
        logger.info(
            "allocator: INFEASIBLE — min_total=%dc > budget=%dc (precheck, zero DP work)",
            min_total, total_budget_cents,
        )
        return _EMPTY

    # ------------------------------------------------------------------
    # Step 3: Exact-cost DP with Pareto pruning
    #
    # dp_states: dict[int, float]  — maps exact cost (cents) to best quality
    # backtrack[i]: dict[int, tuple[int, int]]  — maps cost_after_leg_i to
    #               (cost_before_leg_i, candidate_index_j)
    # ------------------------------------------------------------------

    # Initial state: zero cost spent, zero quality, before any leg processed.
    dp_states: dict[int, float] = {0: 0.0}

    # backtrack[i][cost_after] = (cost_before, j)
    backtrack: list[dict[int, tuple[int, int]]] = [{} for _ in range(n_legs)]

    for i, leg in enumerate(processed_legs):
        cands = leg["candidates"]
        new_states: dict[int, float] = {}
        new_back: dict[int, tuple[int, int]] = {}

        for prev_cost, prev_quality in dp_states.items():
            for j, c in enumerate(cands):
                cost_cents = int(c["total_cents"])
                new_cost = prev_cost + cost_cents
                if new_cost > total_budget_cents:
                    continue
                new_quality = prev_quality + float(c.get("quality", 0.0))

                # Update if strictly better quality, or equal quality with lower cost
                # (tie-breaking: equal quality → prefer lower cost)
                if new_cost not in new_states:
                    new_states[new_cost] = new_quality
                    new_back[new_cost] = (prev_cost, j)
                elif new_quality > new_states[new_cost]:
                    new_states[new_cost] = new_quality
                    new_back[new_cost] = (prev_cost, j)
                # Equal quality: new_cost is already lower-or-equal to itself,
                # so the first write wins (stable: preserves LLM-ranked order).

        if not new_states:
            # No candidate fits for this leg within budget
            logger.info(
                "allocator: leg=%s all candidates exceed budget=%dc — infeasible",
                leg["leg_id"], total_budget_cents,
            )
            return _EMPTY

        # Pareto pruning: remove dominated states.
        # State (cost_a, q_a) is dominated by (cost_b, q_b) if cost_b <= cost_a and q_b >= q_a.
        # We keep only the Pareto-optimal front (lower cost AND higher quality).
        # Sort by cost ascending; keep running max of quality as we go.
        # A state is Pareto-optimal iff its quality is strictly higher than all
        # cheaper states' qualities.
        sorted_costs = sorted(new_states.keys())
        pruned_states: dict[int, float] = {}
        pruned_back: dict[int, tuple[int, int]] = {}
        best_q_so_far = _NEG_INF
        for cost in sorted_costs:
            q = new_states[cost]
            if q > best_q_so_far:
                pruned_states[cost] = q
                pruned_back[cost] = new_back[cost]
                best_q_so_far = q
            # else: dominated — skip (there exists a cheaper state with >= quality)

        dp_states = pruned_states
        backtrack[i] = pruned_back

    # ------------------------------------------------------------------
    # Step 4: Find the best state (max quality; tie-break: min cost)
    # ------------------------------------------------------------------

    best_cost = -1
    best_quality = _NEG_INF
    for cost, quality in dp_states.items():
        if quality > best_quality or (quality == best_quality and cost < best_cost):
            best_quality = quality
            best_cost = cost

    if best_cost < 0 or best_quality == _NEG_INF:
        logger.info(
            "allocator: DP found no feasible assignment (budget=%dc, min_total=%dc)",
            total_budget_cents, min_total,
        )
        return _EMPTY

    # ------------------------------------------------------------------
    # Step 5: Backtrack to recover selections
    # ------------------------------------------------------------------

    selection: list[dict[str, Any]] = []
    cur_cost = best_cost
    for i in range(n_legs - 1, -1, -1):
        leg = processed_legs[i]
        back_entry = backtrack[i].get(cur_cost)
        if back_entry is None:
            # Should not happen (DP ensured feasibility), but guard:
            logger.error(
                "allocator: backtrack failed at leg=%s cost=%d — returning infeasible",
                leg["leg_id"], cur_cost,
            )
            return _EMPTY
        prev_cost, j = back_entry
        cand = leg["candidates"][j]
        leg_id = leg["leg_id"]
        selection.append({
            "leg_id": leg_id,
            "hotel_id": cand.get("hotel_id", ""),
            "total_cents": int(cand.get("total_cents", 0)),
            "quality": float(cand.get("quality", 0.0)),
            # Pass through all extra fields (review_score, area, ranking_source, etc.)
            **{k: v for k, v in cand.items() if k not in {"hotel_id", "total_cents", "quality"}},
        })
        cur_cost = prev_cost

    # Reverse to restore original leg order
    selection.reverse()

    real_total = sum(s["total_cents"] for s in selection)
    logger.info(
        "allocator: exact-optimal — legs=%d budget=%dc total=%dc quality=%.3f "
        "pareto_states=%d selections=%s",
        n_legs, total_budget_cents, real_total, best_quality,
        len(dp_states),
        [(s["leg_id"], s["hotel_id"], s["total_cents"]) for s in selection],
    )

    return {
        "feasible": True,
        "selection": selection,
        "total_cents": real_total,
        "total_quality": best_quality,
    }


# ---------------------------------------------------------------------------
# Quality score derivation helpers (LLM-ranking integration)
# ---------------------------------------------------------------------------


def quality_from_rank(rank_index: int, n_candidates: int) -> float:
    """
    Derive a normalised quality score from LLM rank position.

    Returns a value in [0, 1]:
      - rank_index 0 (best rank) → 1.0
      - rank_index n_candidates-1 (worst rank) → 0.0 (or 1/n if only 1 candidate)

    Normalising to [0,1] per-leg means legs with more candidates don't dominate
    the global DP sum over legs with fewer candidates (SEV-3 fix).

    Args:
        rank_index:   0-based position in the LLM-ranked list (0 = best).
        n_candidates: Total number of candidates for this leg.

    Returns: float quality score in [0.0, 1.0].
    """
    if n_candidates <= 1:
        return 1.0
    # rank 0 → 1.0, rank n-1 → 0.0
    return float(n_candidates - 1 - rank_index) / float(n_candidates - 1)


def quality_from_review_score(review_score: float) -> float:
    """
    Derive a quality score from the merchant review_score (deterministic fallback).

    Used when LLM ranking is not available.
    The review_score from catalog.go is already a good quality proxy (0-10 scale).

    Args:
        review_score: Hotel review score (0-10 float).

    Returns: float quality score (same value — no transformation needed).
    """
    return float(review_score)


def attach_quality_scores(
    ranked_candidates: list[dict[str, Any]],
    ranking_source: str,
) -> list[dict[str, Any]]:
    """
    Attach `quality` scores to a list of (possibly LLM-ranked) candidates.

    This is the single integration point between LLM ranking output
    and the DP allocator.

    Convention:
      - ranking_source == "llm":      quality = rank-position score
                                      (top-ranked = n_candidates quality)
      - ranking_source == "fallback": quality = review_score
                                      (deterministic, no LLM dependence)

    Both paths are fully deterministic given the ranked list.

    Args:
        ranked_candidates: Already-ranked candidate list (from _clamp_ranking /
                          accommodation_agent._rank_candidates).  Order matters
                          for the "llm" path.
        ranking_source:   "llm" | "fallback" (from _clamp_ranking).

    Returns: New list of candidate dicts, each with a `quality` key added.
             The original dicts are NOT mutated.
    """
    n = len(ranked_candidates)
    result: list[dict[str, Any]] = []
    for i, cand in enumerate(ranked_candidates):
        if ranking_source == "llm":
            q = quality_from_rank(i, n)
        else:
            q = quality_from_review_score(float(cand.get("review_score", 0.0)))
        result.append({**cand, "quality": q})
    return result


# ---------------------------------------------------------------------------
# Brute-force reference (used ONLY in tests for DP==brute-force verification)
# ---------------------------------------------------------------------------


def allocate_brute_force(
    legs_with_candidates: list[dict[str, Any]],
    total_budget_cents: int,
) -> dict[str, Any]:
    """
    Brute-force enumerator: tries all combinations of one candidate per leg,
    returns the one with maximum total quality within budget.

    O(candidates_per_leg ^ n_legs) — only usable for small test instances.
    Used ONLY in unit tests to verify DP optimality.

    Same return schema as `allocate()`.
    """
    import itertools

    _EMPTY: dict[str, Any] = {
        "feasible": False,
        "selection": [],
        "total_cents": 0,
        "total_quality": 0.0,
    }

    if not legs_with_candidates:
        return {"feasible": True, "selection": [], "total_cents": 0, "total_quality": 0.0}

    # Extract candidate lists per leg
    candidate_lists = []
    for leg in legs_with_candidates:
        cands = [
            c for c in (leg.get("candidates") or [])
            if isinstance(c.get("total_cents"), (int, float)) and int(c["total_cents"]) > 0
        ]
        if not cands:
            return _EMPTY
        candidate_lists.append((leg.get("leg_id", "?"), cands))

    best_quality = _NEG_INF
    best_total = 0
    best_combo: list[dict] | None = None

    for combo in itertools.product(*[cands for _, cands in candidate_lists]):
        total_cost = sum(int(c["total_cents"]) for c in combo)
        if total_cost > total_budget_cents:
            continue
        total_qual = sum(float(c.get("quality", 0.0)) for c in combo)
        # Tie-breaking: equal quality -> prefer lower cost; equal cost -> first in product order
        if (total_qual > best_quality) or (total_qual == best_quality and total_cost < best_total):
            best_quality = total_qual
            best_total = total_cost
            best_combo = list(combo)

    if best_combo is None:
        return _EMPTY

    selection = []
    for (leg_id, _), cand in zip(candidate_lists, best_combo):
        selection.append({
            "leg_id": leg_id,
            "hotel_id": cand.get("hotel_id", ""),
            "total_cents": int(cand.get("total_cents", 0)),
            "quality": float(cand.get("quality", 0.0)),
        })

    return {
        "feasible": True,
        "selection": selection,
        "total_cents": best_total,
        "total_quality": best_quality,
    }


# ---------------------------------------------------------------------------
# L3-core: Secondary itinerary allocator (§12.8 / §12.9 pre-vetted Plan B)
# ---------------------------------------------------------------------------


def allocate_secondary(
    legs_with_candidates: list[dict[str, Any]],
    total_budget_cents: int,
    baseline_selection: list[dict[str, Any]],
    highest_cost_leg_id: str | None = None,
) -> dict[str, Any]:
    """
    Compute the pre-vetted SECONDARY itinerary for L3-core recovery.

    The secondary is the DP next-best with the chosen hotel on the highest-
    cost / highest-risk leg excluded.  This is the exact mechanism described
    in §12.8 and pressure-tested in §12.9:

      "pre-vetted Plan B = DP second-best for the high-risk leg,
       Critic-verified at plan time; only the affected leg swapped."

    Algorithm:
      1. Identify the highest-cost leg in the baseline (unless overridden).
      2. For that leg, exclude the baseline-selected hotel from the candidate list.
      3. Run the full DP allocator on the reduced candidate set.
      4. Return the result (feasible=True/False, selection, total_cents, etc.)
         plus meta-fields: `excluded_hotel_id`, `affected_leg_id`.

    Determinism guarantee: same baseline + same candidates → same secondary.
    This function is variance-0 (no LLM, no randomness).

    Args:
        legs_with_candidates: Full candidate set per leg (same as for allocate()).
        total_budget_cents:    User budget ceiling.
        baseline_selection:    The baseline DP selection (list of per-leg dicts).
        highest_cost_leg_id:   Override which leg's hotel to exclude.
                               When None, the highest-cost leg in the baseline
                               is chosen automatically.

    Returns a dict extending the standard allocate() schema:
        {
            "feasible":           bool,
            "selection":          [...],      # may overlap baseline except the excluded hotel
            "total_cents":        int,
            "total_quality":      float,
            "affected_leg_id":    str,        # the leg whose hotel was excluded
            "excluded_hotel_id":  str,        # the baseline hotel excluded from the DP
        }
    """
    _EMPTY: dict[str, Any] = {
        "feasible": False,
        "selection": [],
        "total_cents": 0,
        "total_quality": 0.0,
        "affected_leg_id": "",
        "excluded_hotel_id": "",
    }

    if not baseline_selection:
        logger.info("utils.allocator.secondary: empty baseline — no secondary possible")
        return _EMPTY

    # ------------------------------------------------------------------
    # Step 1: Identify the affected (highest-cost) leg in the baseline.
    # ------------------------------------------------------------------
    if highest_cost_leg_id:
        affected_leg_id = highest_cost_leg_id
    else:
        # Pick the leg with the highest cost in the baseline.
        best = max(baseline_selection, key=lambda s: int(s.get("total_cents", 0)))
        affected_leg_id = best["leg_id"]

    # Find what the baseline picked for the affected leg.
    baseline_hotel_for_leg: str | None = None
    for sel in baseline_selection:
        if sel["leg_id"] == affected_leg_id:
            baseline_hotel_for_leg = sel.get("hotel_id")
            break

    if not baseline_hotel_for_leg:
        logger.info(
            "utils.allocator.secondary: no baseline hotel found for leg=%s", affected_leg_id
        )
        return {**_EMPTY, "affected_leg_id": affected_leg_id}

    # ------------------------------------------------------------------
    # Step 2: Build the reduced candidate set: exclude baseline hotel on
    #         the affected leg only; all other legs unchanged.
    # ------------------------------------------------------------------
    reduced_legs: list[dict[str, Any]] = []
    for leg in legs_with_candidates:
        leg_id = leg.get("leg_id", "")
        if leg_id == affected_leg_id:
            # Exclude the baseline hotel from this leg's candidates.
            reduced_cands = [
                c for c in (leg.get("candidates") or [])
                if c.get("hotel_id") != baseline_hotel_for_leg
            ]
            if not reduced_cands:
                logger.info(
                    "utils.allocator.secondary: leg=%s has no remaining candidates "
                    "after excluding baseline hotel=%s — no secondary possible",
                    leg_id, baseline_hotel_for_leg,
                )
                return {
                    **_EMPTY,
                    "affected_leg_id": affected_leg_id,
                    "excluded_hotel_id": baseline_hotel_for_leg,
                }
            reduced_legs.append({"leg_id": leg_id, "candidates": reduced_cands})
        else:
            reduced_legs.append(leg)

    # ------------------------------------------------------------------
    # Step 3: Run DP on the reduced candidate set.
    # ------------------------------------------------------------------
    result = allocate(reduced_legs, total_budget_cents)

    # ------------------------------------------------------------------
    # Step 4: Annotate with meta-fields.
    # ------------------------------------------------------------------
    result["affected_leg_id"] = affected_leg_id
    result["excluded_hotel_id"] = baseline_hotel_for_leg

    logger.info(
        "utils.allocator.secondary: affected_leg=%s excluded=%s → feasible=%s "
        "total=%d¢ quality=%.3f selection=%s",
        affected_leg_id, baseline_hotel_for_leg,
        result["feasible"], result.get("total_cents", 0),
        result.get("total_quality", 0.0),
        [(s["leg_id"], s["hotel_id"], s["total_cents"])
         for s in result.get("selection", [])],
    )
    return result
