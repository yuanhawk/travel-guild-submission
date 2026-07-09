"""
test_recovery_cov2.py — ADD-ONLY coverage tests for utils/recovery.py error/edge
branches (task #75 — total CI coverage initiative).

These are pure-logic, variance-0 unit tests that exercise the honest-failure and
fallback branches of RecoveryOrchestrator WITHOUT any live LLM / network / paid
merchant. The agent-call seams (_call_budget_check / _call_budget_commit /
_call_budget_cancel / _call_critic / _call_transport / _dispatch) are overridden
on the instance with deterministic stubs, so we drive each branch directly:

  - _dispatch: both client + url None -> RuntimeError
  - _call_critic: both client + url None -> None
  - recover():       hotel-not-in-package, secondary None/infeasible/mismatched,
                     no-candidate reallocate, reallocate-with-explicit-exclusion,
                     transport infeasible, Critic reject, budget.check raise,
                     budget veto.
  - commit_recovery(): non-ready input, budget.commit raise, merchant veto.
  - cancel_recovery(): empty checkout_id, budget.cancel raise, decision!=cancelled.
  - _recover_flight_cancel(): empty reroute legs, transport infeasible, budget
                     exceeded, budget.check raise, merchant veto.
  - recover_cascade(): unknown fault type, unrecoverable cycle, commit failure,
                     void-of-superseded fails (non-fatal continue).

NONE of these assert on a golden/benchmark number — they assert on outcome codes,
reason substrings, and call counts only.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from utils.recovery import (
    RecoveryOrchestrator,
    RECOVERY_OUTCOME_READY,
    RECOVERY_OUTCOME_COMMITTED,
    RECOVERY_OUTCOME_CANNOT_SATISFY,
    FAULT_HOTEL_SOLD_OUT,
    FAULT_FLIGHT_CANCEL,
    CASCADE_OUTCOME_SURVIVED,
    CASCADE_OUTCOME_CANNOT_SATISFY,
)

BUDGET = 100_000


# ---------------------------------------------------------------------------
# Deterministic stub orchestrator: override the agent-call seams in-process.
#
# Each seam can be set to a value (returned) or a callable (invoked with the
# payload). A value of `RAISE` makes the seam raise. This keeps every test a
# pure-logic exercise of recovery.py with NO real A2A / network.
# ---------------------------------------------------------------------------

class _Boom(Exception):
    pass


def _make_ro(
    *,
    check=None,
    commit=None,
    cancel=None,
    critic=None,
    transport=None,
):
    """Build a RecoveryOrchestrator with stubbed _call_* seams.

    Each kwarg is either a dict (returned), a callable (called with payload), or
    an Exception instance (raised). None leaves the seam returning a benign
    accept/None default suitable for a clean pass.
    """
    ro = RecoveryOrchestrator(budget_client=object())  # client present so _dispatch isn't reached

    def _wrap(spec, default):
        def seam(payload, *a, **k):
            target = spec if spec is not None else default
            if isinstance(target, BaseException):
                raise target
            if callable(target):
                return target(payload)
            return target
        return seam

    # budget.check default = check_ok with a fresh checkout id
    ro._call_budget_check = _wrap(  # type: ignore[assignment]
        check, {"decision": "check_ok", "checkout_id": "co_recovery"})
    ro._call_budget_commit = _wrap(  # type: ignore[assignment]
        commit, {"decision": "accept", "booking_ref": "BK-recovery"})
    ro._call_budget_cancel = _wrap(  # type: ignore[assignment]
        cancel, {"decision": "cancelled", "released_booking_ref": "BK-old"})
    # critic/transport default to None (gate absent → silent pass)
    ro._call_critic = _wrap(critic, None)        # type: ignore[assignment]
    ro._call_transport = _wrap(transport, None)  # type: ignore[assignment]
    return ro


def _booking(hotel_id="hotel-A"):
    return {
        "outcome": "success",
        "user_id": "u1",
        "total_budget_cents": BUDGET,
        "total_booked_cents": 40_000,
        "booking_ref": "BK-original",
        "checkout_id": "co_original",
        "legs": [
            {"leg_id": "leg-1", "city": "x", "area": "x",
             "checkin": "2026-11-24", "checkout": "2026-11-26", "adults": 1,
             "hotel_id": hotel_id, "total_cents": 40_000, "provenance": "merchant"},
        ],
    }


def _secondary(hotel_id="hotel-A", leg="leg-1", to="hotel-B", total=30_000, feasible=True):
    """A pre-vetted secondary that swaps `hotel_id`->`to` on `leg`."""
    return {
        "feasible": feasible,
        "affected_leg_id": leg,
        "excluded_hotel_id": hotel_id,
        "total_cents": total,
        "selection": [
            {"leg_id": "leg-1", "hotel_id": to, "total_cents": total, "quality": 0.7},
        ],
    }


# ===========================================================================
# _dispatch / _call_critic plumbing
# ===========================================================================

def test_dispatch_both_client_url_none_raises_runtimeerror():
    ro = RecoveryOrchestrator(budget_client=None, budget_url=None)
    with pytest.raises(RuntimeError):
        ro._call_budget_check({"user_id": "u1"})


def test_call_critic_both_client_url_none_returns_none():
    ro = RecoveryOrchestrator(critic_client=None, critic_url=None)
    assert ro._call_critic({"user_id": "u1"}) is None


# ===========================================================================
# recover() — early exits and fallbacks
# ===========================================================================

def test_recover_hotel_not_in_booking_early_exit():
    ro = _make_ro()
    out = ro.recover(
        original_booking=_booking(),
        unavailable_hotel_id="nonexistent-hotel",
        secondary_plan=None,
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "not in the booked package" in out["reason"]


def test_recover_no_candidates_cannot_satisfy():
    """secondary=None and per_leg_candidates=None → reallocate path bails out."""
    ro = _make_ro()
    out = ro.recover(
        original_booking=_booking(),
        unavailable_hotel_id="hotel-A",
        secondary_plan=None,
        per_leg_candidates=None,
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "no candidate sets available" in out["reason"]


def _candidates(exclude_filtered_ok=True):
    """per-leg candidate set: leg-1 has hotel-A (the faulted one) + hotel-B."""
    return [
        {"leg_id": "leg-1", "candidates": [
            {"hotel_id": "hotel-A", "total_cents": 40_000, "quality": 0.9},
            {"hotel_id": "hotel-B", "total_cents": 30_000, "quality": 0.7},
        ]},
    ]


def test_recover_secondary_plan_none_reallocates():
    """secondary=None but candidates present → allocate_secondary fallback works."""
    ro = _make_ro()
    out = ro.recover(
        original_booking=_booking(),
        unavailable_hotel_id="hotel-A",
        secondary_plan=None,
        per_leg_candidates=_candidates(),
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_READY
    assert out["source"] == "reallocate"


def test_recover_secondary_infeasible_reallocates():
    """secondary feasible=False → reallocate path taken (not the secondary)."""
    ro = _make_ro()
    out = ro.recover(
        original_booking=_booking(),
        unavailable_hotel_id="hotel-A",
        secondary_plan=_secondary(feasible=False),
        per_leg_candidates=_candidates(),
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_READY
    assert out["source"] == "reallocate"


def test_recover_secondary_covers_different_fault_reallocates():
    """secondary excludes a DIFFERENT hotel/leg than the fault → reallocate."""
    ro = _make_ro()
    # fault is on leg-1/hotel-A but secondary is keyed to leg-2/wrong-hotel
    sec = _secondary(hotel_id="wrong-hotel", leg="leg-2", to="hotel-B")
    out = ro.recover(
        original_booking=_booking(),
        unavailable_hotel_id="hotel-A",
        secondary_plan=sec,
        per_leg_candidates=_candidates(),
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_READY
    assert out["source"] == "reallocate"


def test_recover_allocate_secondary_infeasible_retry_with_exclusion():
    """allocate_secondary may return infeasible (only 1 candidate, == excluded
    baseline → no alternative); the explicit-exclusion fallback to allocate()
    then ALSO finds nothing → honest cannot_satisfy with the excluded-hotel
    fields populated (exercises lines 312-345)."""
    ro = _make_ro()
    # leg-1 has ONLY the faulted hotel-A: secondary can't avoid it, and the
    # explicit-exclusion allocate() leaves an empty candidate list → infeasible.
    only_faulted = [
        {"leg_id": "leg-1", "candidates": [
            {"hotel_id": "hotel-A", "total_cents": 40_000, "quality": 0.9},
        ]},
    ]
    out = ro.recover(
        original_booking=_booking(),
        unavailable_hotel_id="hotel-A",
        secondary_plan=None,
        per_leg_candidates=only_faulted,
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert out["excluded_hotel_id"] == "hotel-A"
    assert out["affected_leg_id"] == "leg-1"


def test_recover_transport_infeasible_edges_cannot_satisfy():
    ro = _make_ro(transport={"infeasible_edges": [{"from_leg": "leg-1", "to_leg": "leg-2"}]})
    out = ro.recover(
        original_booking=_booking(),
        unavailable_hotel_id="hotel-A",
        secondary_plan=_secondary(),
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "transport infeasible" in out["reason"]
    assert "leg-1→leg-2" in out["reason"]


def test_recover_critic_rejected_cannot_satisfy():
    ro = _make_ro(critic={
        "decision": "rejected",
        "violations": [{"code": "BUDGET", "leg_id": "leg-1", "detail": "too pricey"}],
    })
    out = ro.recover(
        original_booking=_booking(),
        unavailable_hotel_id="hotel-A",
        secondary_plan=_secondary(),
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "rejected by Critic" in out["reason"]
    assert "BUDGET" in out["reason"]


def test_recover_budget_check_exception_cannot_satisfy():
    ro = _make_ro(check=_Boom("connection reset"))
    out = ro.recover(
        original_booking=_booking(),
        unavailable_hotel_id="hotel-A",
        secondary_plan=_secondary(),
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "checkout creation failed" in out["reason"]
    assert "connection reset" in out["reason"]


def test_recover_budget_check_veto_cannot_satisfy():
    ro = _make_ro(check={"decision": "veto", "veto_reason": "exceeds limit"})
    out = ro.recover(
        original_booking=_booking(),
        unavailable_hotel_id="hotel-A",
        secondary_plan=_secondary(),
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "vetoed by merchant" in out["reason"]
    assert "exceeds limit" in out["reason"]


# ===========================================================================
# commit_recovery()
# ===========================================================================

def _ready():
    return {
        "outcome": RECOVERY_OUTCOME_READY,
        "recovery_checkout_id": "co_recovery",
        "idempotency_key": "recovery-fixed-key",
        "recovery_legs": [{"leg_id": "leg-1", "hotel_id": "hotel-B"}],
        "recovery_total_cents": 30_000,
        "swapped_from": "hotel-A",
        "swapped_to": "hotel-B",
        "affected_leg_id": "leg-1",
        "source": "secondary",
    }


def test_commit_recovery_non_ready_input_cannot_satisfy():
    ro = _make_ro()
    out = ro.commit_recovery({"outcome": "invalid_state"}, {"buyer_consent": True}, "u1")
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "non-ready" in out["reason"]


def test_commit_recovery_budget_commit_exception_cannot_satisfy():
    ro = _make_ro(commit=_Boom("merchant down"))
    out = ro.commit_recovery(_ready(), {"buyer_consent": True}, "u1")
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "commit failed" in out["reason"]
    assert "merchant down" in out["reason"]


def test_commit_recovery_merchant_veto_cannot_satisfy():
    ro = _make_ro(commit={"decision": "veto", "veto_reason": "mandate expired"})
    out = ro.commit_recovery(_ready(), {"buyer_consent": True}, "u1")
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "rejected by merchant" in out["reason"]
    assert "mandate expired" in out["reason"]


def test_commit_recovery_sets_top_level_buyer_consent_on_commit_payload():
    """
    CI-coverage-audit regression test (2026-07): the ONLY prior verification of
    this fix was e2e_py/live_recovery_commit_e2e.py, an ad-hoc script explicitly
    self-documented as "NOT IN THE CI SUITE" -- it needs a real merchant, so it
    never runs in CI. Every mocked commit_recovery test above stubs
    _call_budget_commit with a fixed dict that never inspects the outgoing
    payload, so a regression that silently drops the top-level `buyer_consent`
    key (reverting to nested-only ap2_mandate, which the merchant's HITL gate at
    the default L2 autonomy tier does NOT read) would pass every test in
    society/tests/ while breaking every real recovery commit against the live
    merchant. Capture the actual outgoing payload and assert on it directly.
    """
    seen_payload = {}

    def _capture_commit(payload):
        seen_payload.update(payload)
        return {"decision": "accept", "booking_ref": "BK-recovery"}

    ro = _make_ro(commit=_capture_commit)
    out = ro.commit_recovery(_ready(), {"buyer_consent": True}, "u1")
    assert out["outcome"] != RECOVERY_OUTCOME_CANNOT_SATISFY, out
    assert seen_payload.get("buyer_consent") is True, seen_payload


# ===========================================================================
# cancel_recovery()
# ===========================================================================

def test_cancel_recovery_empty_checkout_id_skipped():
    ro = _make_ro()
    out = ro.cancel_recovery(checkout_id="", booking_ref="BK123", user_id="u1")
    assert out["voided"] is False
    assert out["decision"] == "skipped"


def test_cancel_recovery_exception_error_response():
    ro = _make_ro(cancel=_Boom("cancel net error"))
    out = ro.cancel_recovery(checkout_id="co_old", booking_ref="BK123", user_id="u1")
    assert out["voided"] is False
    assert out["decision"] == "error"
    assert "cancel net error" in out["reason"]


def test_cancel_recovery_decision_not_cancelled_voided_false():
    ro = _make_ro(cancel={"decision": "unknown", "reason": "no such checkout"})
    out = ro.cancel_recovery(checkout_id="co_old", booking_ref="BK123", user_id="u1")
    assert out["voided"] is False
    assert out["decision"] == "unknown"


# ===========================================================================
# _recover_flight_cancel()
# ===========================================================================

def _reroute(total=30_000, legs=None):
    if legs is None:
        legs = [
            {"leg_id": "leg-1", "city": "hub", "checkin": "2026-11-24",
             "checkout": "2026-11-25", "adults": 1, "hotel_id": "hub-hotel",
             "total_cents": total, "provenance": "merchant"},
        ]
    return {"feasible": True, "legs": legs, "total_cents": total,
            "affected_leg_id": "leg-1", "reroute_label": "via hub"}


def test_recover_flight_cancel_empty_reroute_legs_cannot_satisfy():
    ro = _make_ro()
    out = ro._recover_flight_cancel(
        current_legs=[],
        from_city="A", to_city="B",
        reroute_option={"feasible": True, "legs": []},
        user_id="u1", total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "produced no legs" in out["reason"]


def test_recover_flight_cancel_transport_infeasible_cannot_satisfy():
    # _recover_flight_cancel calls _dispatch DIRECTLY (not _call_transport) and
    # only when a transport client/url is configured. So set transport_client and
    # override _dispatch to return an infeasible-edges result for the
    # transport.feasibility skill.
    ro = _make_ro()
    ro._transport_client = object()
    ro._dispatch = lambda client, url, payload, skill_id: (  # type: ignore[assignment]
        {"infeasible_edges": [{"from_leg": "leg-1", "to_leg": "leg-2"}]}
    )
    out = ro._recover_flight_cancel(
        current_legs=[],
        from_city="A", to_city="B",
        reroute_option=_reroute(),
        user_id="u1", total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "still" in out["reason"] and "infeasible" in out["reason"]


def test_recover_flight_cancel_budget_exceeded_cannot_satisfy():
    ro = _make_ro()
    out = ro._recover_flight_cancel(
        current_legs=[],
        from_city="A", to_city="B",
        reroute_option=_reroute(total=BUDGET + 1),
        user_id="u1", total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "exceeds budget" in out["reason"]


def test_recover_flight_cancel_checkout_exception_cannot_satisfy():
    ro = _make_ro(check=_Boom("checkout boom"))
    out = ro._recover_flight_cancel(
        current_legs=[],
        from_city="A", to_city="B",
        reroute_option=_reroute(),
        user_id="u1", total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "checkout creation failed" in out["reason"]


def test_recover_flight_cancel_merchant_veto_cannot_satisfy():
    ro = _make_ro(check={"decision": "veto", "veto_reason": "over ceiling"})
    out = ro._recover_flight_cancel(
        current_legs=[],
        from_city="A", to_city="B",
        reroute_option=_reroute(),
        user_id="u1", total_budget_cents=BUDGET,
    )
    assert out["outcome"] == RECOVERY_OUTCOME_CANNOT_SATISFY
    assert "vetoed by merchant" in out["reason"]
    assert "over ceiling" in out["reason"]


# ===========================================================================
# recover_cascade()
# ===========================================================================

def _sign(recovery_ready):
    return {"buyer_consent": True, "checkout_id": recovery_ready.get("recovery_checkout_id", "")}


def test_recover_cascade_unknown_fault_type_cannot_satisfy():
    ro = _make_ro()
    out = ro.recover_cascade(
        original_booking=_booking(),
        faults=[{"type": "unknown_type"}],
        sign_mandate=_sign,
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == CASCADE_OUTCOME_CANNOT_SATISFY
    assert "Unknown fault type" in out["reason"]
    assert out["failed_cycle_index"] == 0


def test_recover_cascade_unrecoverable_fault_cannot_satisfy():
    """First (and only) fault has no reroute → cycle returns cannot_satisfy →
    cascade stops with failed_cycle_index and the failing recovery_result."""
    ro = _make_ro()
    out = ro.recover_cascade(
        original_booking=_booking(),
        faults=[{"type": FAULT_FLIGHT_CANCEL, "from_city": "A", "to_city": "B",
                 "reroute_option": None}],
        sign_mandate=_sign,
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == CASCADE_OUTCOME_CANNOT_SATISFY
    assert out["failed_cycle_index"] == 0
    assert out["cycles"] == []
    assert "recovery_result" in out


def test_recover_cascade_commit_failure_cannot_satisfy():
    """recover() succeeds but commit_recovery() is vetoed by the merchant →
    cascade stops with the commit failure surfaced."""
    ro = _make_ro(commit={"decision": "veto", "veto_reason": "mandate stale"})
    out = ro.recover_cascade(
        original_booking=_booking(),
        faults=[{"type": FAULT_HOTEL_SOLD_OUT, "hotel_id": "hotel-A",
                 "secondary_plan": _secondary()}],
        sign_mandate=_sign,
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == CASCADE_OUTCOME_CANNOT_SATISFY
    assert out["failed_cycle_index"] == 0
    assert "commit_result" in out
    assert "Re-consent/commit failed" in out["reason"]


def test_recover_cascade_void_failure_non_fatal():
    """The baseline is 'committed' (has checkout_id/booking_ref) so cycle 1 tries
    to void it. cancel returns a non-cancelled decision (void fails) but the
    cascade SURVIVES (void failure is non-fatal); voided_checkout_id is empty."""
    ro = _make_ro(cancel={"decision": "unknown", "reason": "merchant lost it"})
    out = ro.recover_cascade(
        original_booking=_booking(),
        faults=[{"type": FAULT_HOTEL_SOLD_OUT, "hotel_id": "hotel-A",
                 "secondary_plan": _secondary()}],
        sign_mandate=_sign,
        user_id="u1",
        total_budget_cents=BUDGET,
    )
    assert out["outcome"] == CASCADE_OUTCOME_SURVIVED
    assert len(out["cycles"]) == 1
    # Void did NOT confirm → the cycle records an empty voided_checkout_id.
    assert out["cycles"][0]["voided_checkout_id"] == ""
    # But the new booking still committed and stands.
    assert out["cycles"][0]["booking_ref"] == "BK-recovery"


if __name__ == "__main__":
    import sys as _s
    raise SystemExit(pytest.main([__file__, "-q"] + _s.argv[1:]))
