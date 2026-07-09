"""
test_retry_abandoned_legs.py — unit coverage for TravelOrchestrator._retry_abandoned_legs
(#112 fix: non-monotonic budget cliff).

Root cause (see orchestrator.py's #112-tagged comments in _run_negotiation_rounds):
the tighten-priciest-leg re-plan loop tries legs PRICIEST FIRST and `break`s on the
first one that re-fits at a tightened ceiling. Any OTHER leg that was tried-and-failed
(set to no_fit / None) EARLIER in that same pass was left permanently abandoned — even
though the leg that *did* succeed just shrank, freeing fresh ceiling headroom the
abandoned leg never got to use. The very next thing that ran was the ALL-OR-NONE check,
which saw the abandoned leg's `None` and returned cannot_satisfy immediately — a real AU
multi-region trip booked at $4000 but permanently declined at $5000-$20000 (see #112).

These tests exercise `_retry_abandoned_legs` DIRECTLY (isolated, in-memory, no live
LLM/network/merchant) by monkey-patching `_propose_with_area_ladder` on a bare
TravelOrchestrator() instance (all constructor args are optional — see __init__),
so the exact abandon-then-recover shape can be constructed deterministically without
threading a full negotiate() round trip.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.orchestrator import TravelOrchestrator  # noqa: E402


def _make_orch(fit_table):
    """fit_table: dict[leg_id] -> (true_cost, price_once_it_fits). A fake
    `_propose_with_area_ladder` returns fit="ok" once max_cents meets true_cost,
    else no_fit — mirroring the real ladder's "would this ceiling clear the leg's
    real minimum cost" semantics, without any live catalog/merchant call. Returns
    (orch, calls) where `calls` records every (leg_id, max_cents) the retry tried,
    so tests can assert not just the outcome but the EXACT headroom math."""
    orch = TravelOrchestrator()
    calls: list[tuple[str, int]] = []

    def fake_propose(*, leg_meta, target_areas, area_stage, leg_id, max_cents):
        calls.append((leg_id, max_cents))
        true_cost, price = fit_table[leg_id]
        if max_cents >= true_cost:
            return {"fit": "ok", "proposal": {"hotel_id": f"{leg_id}-hotel",
                                               "total_cents": price}}
        return {"fit": "no_fit", "reason_code": "no_fit"}

    orch._propose_with_area_ladder = fake_propose
    return orch, calls


def test_abandoned_leg_recovers_once_a_sibling_frees_headroom():
    """The #112 repro shape: sydney (priciest) is abandoned first at too tight a
    ceiling; darwin then tightens successfully to BELOW its stale-snapshot price,
    freeing real headroom. Retrying sydney against that fresh headroom (eff_ceiling
    5000 - darwin's real 1200 = 3800) now clears its 3000 true cost -> recovers."""
    orch, calls = _make_orch({
        "sydney": (3000, 3000),
        "darwin": (1000, 1200),
    })
    proposals = {"sydney": None, "darwin": {"total_cents": 1200}}
    recovered = orch._retry_abandoned_legs(
        proposals=proposals, abandoned_lids=["sydney"], eff_ceiling=5000,
        leg_meta={}, target_areas={}, area_stage={}, ceilings={},
        legs=["sydney", "darwin"],
    )
    assert recovered is True
    assert proposals["sydney"] is not None
    assert proposals["sydney"]["total_cents"] == 3000
    assert ("sydney", 3800) in calls, (
        f"expected sydney retried at the FRESH headroom (5000-1200=3800), got {calls}"
    )


def test_sequential_headroom_not_double_counted_across_two_abandoned_legs():
    """Two legs (A, B) abandoned in the same pass, plus an untouched sibling C.
    B's retry ceiling must reflect A's just-recovered REAL cost (not the stale
    pre-pass total, and not double-counting the freed room) — proving the retry
    loop recomputes other_costs off CURRENT `proposals` on every iteration."""
    orch, calls = _make_orch({
        "A": (5000, 5000),
        "B": (2500, 2500),
    })
    proposals = {"A": None, "B": None, "C": {"total_cents": 2000}}
    recovered = orch._retry_abandoned_legs(
        proposals=proposals, abandoned_lids=["A", "B"], eff_ceiling=10000,
        leg_meta={}, target_areas={}, area_stage={}, ceilings={},
        legs=["A", "B", "C"],
    )
    assert recovered is True
    assert proposals["A"]["total_cents"] == 5000
    assert proposals["B"]["total_cents"] == 2500
    a_call = next(c for c in calls if c[0] == "A")
    b_call = next(c for c in calls if c[0] == "B")
    assert a_call == ("A", 8000), f"A: other_costs=C(2000) -> new_max=10000-2000=8000, got {a_call}"
    assert b_call == ("B", 3000), (
        f"B: other_costs=C(2000)+A(5000, just recovered) -> new_max=10000-7000=3000, got {b_call}"
    )


def test_still_unfittable_leg_stays_abandoned_no_false_recovery():
    """A leg that genuinely cannot fit even at the fresh headroom must stay None —
    the retry must never FABRICATE a recovery. `recovered` reflects only what
    actually succeeded (guards against a false success on a genuinely infeasible
    trip, the mirror-image risk of the #112 fix)."""
    orch, calls = _make_orch({
        "sydney": (9_999_999, 9_999_999),  # impossible to fit under any real budget
        "darwin": (1000, 1200),
    })
    proposals = {"sydney": None, "darwin": {"total_cents": 1200}}
    recovered = orch._retry_abandoned_legs(
        proposals=proposals, abandoned_lids=["sydney"], eff_ceiling=5000,
        leg_meta={}, target_areas={}, area_stage={}, ceilings={},
        legs=["sydney", "darwin"],
    )
    assert recovered is False
    assert proposals["sydney"] is None


def test_mixed_recovery_only_flags_true_when_at_least_one_leg_recovers():
    """One abandoned leg recovers, the other genuinely doesn't — `recovered` must
    still be True (at least one leg improved), and each proposal reflects its own
    independent outcome (no all-or-nothing coupling inside the retry helper)."""
    orch, calls = _make_orch({
        "sydney": (3000, 3000),          # recovers
        "perth": (9_999_999, 9_999_999),  # stays unfittable
        "darwin": (1000, 1200),
    })
    proposals = {"sydney": None, "perth": None, "darwin": {"total_cents": 1200}}
    recovered = orch._retry_abandoned_legs(
        proposals=proposals, abandoned_lids=["sydney", "perth"], eff_ceiling=5000,
        leg_meta={}, target_areas={}, area_stage={}, ceilings={},
        legs=["sydney", "perth", "darwin"],
    )
    assert recovered is True
    assert proposals["sydney"] is not None and proposals["sydney"]["total_cents"] == 3000
    assert proposals["perth"] is None


def test_zero_or_negative_headroom_falls_back_to_even_split_floor():
    """When other_costs alone already meets/exceeds eff_ceiling (new_max <= 0), the
    retry must not propose a non-positive ceiling — it falls back to an even split
    of eff_ceiling across all legs (floor, min 1¢), matching the identical fallback
    used by the four tighten-priciest-leg re-plan loops this helper mirrors."""
    orch, calls = _make_orch({
        "sydney": (1, 1),  # trivially cheap so the fallback ceiling still clears it
    })
    proposals = {"sydney": None, "darwin": {"total_cents": 4900}, "perth": {"total_cents": 300}}
    orch._retry_abandoned_legs(
        proposals=proposals, abandoned_lids=["sydney"], eff_ceiling=5000,
        leg_meta={}, target_areas={}, area_stage={}, ceilings={},
        legs=["sydney", "darwin", "perth"],
    )
    # other_costs = 4900 + 300 = 5200 > eff_ceiling(5000) -> new_max <= 0 ->
    # fallback = max(1, floor(5000/3)) = 1666
    assert ("sydney", 1666) in calls, f"expected the even-split floor fallback, got {calls}"


def test_already_recovered_leg_is_skipped_on_a_later_retry_call():
    """If a caller invokes the retry helper twice in the same pass (defensive; the
    real call sites only call it once per pass) and an abandoned leg was already
    recovered, the second pass must skip it rather than re-querying/re-pricing it."""
    orch, calls = _make_orch({"sydney": (100, 100)})
    proposals = {"sydney": {"total_cents": 100}, "darwin": {"total_cents": 1000}}
    recovered = orch._retry_abandoned_legs(
        proposals=proposals, abandoned_lids=["sydney"], eff_ceiling=5000,
        leg_meta={}, target_areas={}, area_stage={}, ceilings={},
        legs=["sydney", "darwin"],
    )
    assert recovered is False, "already-fitted leg must not count as a fresh recovery"
    assert calls == [], "an already-recovered leg must be skipped, not re-priced"
