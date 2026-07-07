"""budget_estimate.py — deterministic budget-guidance helpers (var-0).

Pure integer arithmetic over USD cents (the internal accounting unit). NO LLM, clock,
network, or randomness — byte-identical across runs. Multi-currency DISPLAY (home/local/USD)
is layered on top by the currency-advisory module; these functions are the currency-agnostic
core.

The budget VETO stays restrictive; these helpers only GUIDE the user toward a workable number:
  - estimate_range:  user gave NO budget -> suggest a low-high band from the actual cheapest +
                     mid-tier lodging per leg plus the already-computed mandatory-fee envelope.
  - shortfall:       budget too low -> how much MORE is needed to reach the cheapest viable plan.

Inputs are ALREADY-COMPUTED integers (per-leg cheapest/mid total_cents from the same PASS-3
candidate gather the planner uses; fees pre-summed from the gates). This module never re-derives
prices or fees -> single source of truth, trivially deterministic + unit-testable without a merchant.
"""

from __future__ import annotations


def estimate_range(
    leg_floors_cents: list[int],
    leg_mids_cents: list[int],
    fees_cents: int,
) -> tuple[int, int]:
    """(low, high) USD-cent band for a trip with no stated budget.

    low  = Σ(cheapest lodging per leg) + mandatory fees
    high = Σ(mid-tier lodging per leg) + mandatory fees

    Pure. `leg_floors_cents`/`leg_mids_cents` must align 1:1 per leg.
    """
    if len(leg_floors_cents) != len(leg_mids_cents):
        raise ValueError("leg_floors_cents and leg_mids_cents must align 1:1 per leg")
    fees = int(fees_cents or 0)
    low = sum(int(x) for x in leg_floors_cents) + fees
    high = sum(int(x) for x in leg_mids_cents) + fees
    # mid >= floor per leg by construction, but guard so high is never below low.
    return low, max(low, high)


def round_band(low_cents: int, high_cents: int, step_cents: int = 10000) -> tuple[int, int]:
    """Round low DOWN and high UP to a stable display step (default $100) so the band reads
    cleanly ('$1,800-$3,200') and is byte-stable across runs. Pure integer arithmetic.

    HONESTY GUARD: a positive raw low must never floor to 0 (that would advertise a "free"
    floor for a real trip — the $0 Bali-floor bug). When flooring at `step` would zero out a
    positive low, fall back to the coarsest finer grid ($10, then $1) that still yields a
    positive floor, so the displayed low stays a real, non-zero number while remaining
    on-grid and byte-stable.
    """
    step = max(1, int(step_cents))
    raw_low = int(low_cents)
    low = (raw_low // step) * step
    if low == 0 and raw_low > 0:
        # Coarsest finer grid that keeps the low positive ($10 → $1).
        for finer in (1000, 100, 1):
            if finer < step and (raw_low // finer) * finer > 0:
                low = (raw_low // finer) * finer
                break
    high = -((-int(high_cents)) // step) * step  # ceil to step
    return low, max(high, low + step)  # never a degenerate (zero-width) band


def shortfall(min_feasible_total_cents: int, budget_cents: int) -> int:
    """How much MORE budget is needed to reach the cheapest viable plan.

    max(0, min_feasible_total - budget). 0 means the budget already covers the cheapest plan
    (so the caller should NOT surface a top-up nudge). Pure.
    """
    return max(0, int(min_feasible_total_cents) - int(budget_cents))
