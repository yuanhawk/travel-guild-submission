"""test_consent_split.py — #1 CONSENT SPLIT (plan → review → /confirm).

Verifies the plan-only early-return + commit_plan, the held plan_ready envelope,
field-for-field parity with the eventual booked trip, idempotent/honest commit
terminals, and the unified trips store. The ATOMIC path (commit=True default) is
guarded by test_trace_var0 + the existing negotiate suite (byte-identical).

Harness reused from the wallet-sim tests: _build_society (mock merchant transports)
+ _PM_TRIP (a bookable Port-Moresby request).
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests.test_trace_var0 import _PM_TRIP, _merchant_result
from tests.test_wallet_sim import _build_society
from orchestration.store import SqliteDashboardStore

# A successful complete_checkout (the COMMIT) + a wallet fund, reused across tests.
_COMPLETE_OK = _merchant_result({
    "id": "co_pm", "status": "complete", "user_id": "trace-test",
    "line_items": [], "total_cents": 54000, "currency": "USD",
    "buyer_consent": True, "booking_ref": "BK-trace-1",
    "wallet_session_id": "trip-x", "wallet_debit_cents": 54000,
    "wallet_balance_cents": 446000, "simulated": True,
})
_FUND_OK = _merchant_result({
    "status": "ok", "wallet_session_id": "trip-x", "seed_cents": 500000,
    "balance_cents": 500000, "simulated": True, "note": "sim",
})


def _plan() -> dict:
    """A plan-only (commit=False) negotiate over the bookable PM society."""
    orch = _build_society(complete_resp=(200, _COMPLETE_OK), fund_resp=(200, _FUND_OK))
    res = orch.negotiate(copy.deepcopy(_PM_TRIP), commit=False)
    return res, orch


# --------------------------------------------------------------------------
# (a) PLAN-ONLY → held plan_ready, nothing booked or debited.
# --------------------------------------------------------------------------

def test_plan_only_returns_held_plan_ready():
    res, _ = _plan()
    assert res.get("outcome") == "plan_ready", res
    assert res.get("booking_ref") is None, "plan_ready must NOT carry a booking_ref"
    assert res.get("payment_status") == "held", res
    assert res.get("idempotency_key"), "plan_ready must carry the confirm handle"
    w = res.get("wallet") or {}
    assert w.get("debited") is False, "plan-only must NOT debit the wallet"
    assert w.get("held_cents", 0) > 0, "plan-only must surface the HELD amount"
    # The held amount mirrors the with-fees package total (full parity).
    assert w["held_cents"] == (res.get("package_total_with_fees_cents")
                               or res.get("package_total_cents"))
    # The deterministic itinerary spine is present (same as a booked trip).
    assert res.get("legs"), res
    # The private confirm context exists at the orchestrator boundary (server strips it).
    ctx = res.get("_confirm_ctx") or {}
    assert ctx.get("checkout_id") and ctx.get("idempotency_key") and ctx.get("dest_token")


def test_plan_only_does_not_place_or_charge_supper():
    """Money bug: _maybe_order_supper ran AFTER the plan-only early-return but
    was not _plan_only-guarded, so a {plan:true, supper:{order}} request placed + charged
    the 2nd irreversible checkout despite 'hold, don't book' (and then reported the trip's
    top-level wallet.debited:false). Guard: plan-only must short-circuit before stamping/
    charging supper. Contrast: the ATOMIC path DOES reach + stamp supper (so this isn't
    vacuous)."""
    trip = copy.deepcopy(_PM_TRIP)
    trip["supper"] = {"order": True}
    # Atomic (commit=True) reaches the supper path → result carries a `supper` block.
    orch_a = _build_society(complete_resp=(200, _COMPLETE_OK), fund_resp=(200, _FUND_OK))
    booked = orch_a.negotiate(copy.deepcopy(trip), commit=True)
    assert booked.get("outcome") == "success", booked
    assert booked.get("supper") is not None, "fixture must reach the supper path (else vacuous)"
    # Plan-only (commit=False) with the SAME supper request → NO supper stamped, NO debit.
    orch_p = _build_society(complete_resp=(200, _COMPLETE_OK), fund_resp=(200, _FUND_OK))
    res = orch_p.negotiate(copy.deepcopy(trip), commit=False)
    assert res.get("outcome") == "plan_ready", res
    assert res.get("supper") is None, "plan-only must NOT place/stamp a supper order"
    assert (res.get("wallet") or {}).get("debited") is False, "plan-only must NOT debit"


# --------------------------------------------------------------------------
# (b) /confirm (commit_plan) → success + booking_ref + wallet debited.
# --------------------------------------------------------------------------

def test_confirm_commits_and_debits():
    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}
    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])
    assert booked.get("outcome") == "success", booked
    assert booked.get("booking_ref"), "confirm must mint a real booking_ref"
    assert booked.get("payment_status") == "charged", booked
    w = booked.get("wallet") or {}
    assert w.get("debited") is True and w.get("debit_cents") == 54000, booked


# --------------------------------------------------------------------------
# (e) Plan envelope and the eventual booked envelope match field-for-field
#     (minus the booking-state fields).
# --------------------------------------------------------------------------

def test_plan_and_booked_envelopes_match():
    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}
    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=copy.deepcopy(envelope),
        dest_token=ctx["dest_token"])
    # Itinerary + money body identical (only outcome/booking_ref/payment_status/wallet differ).
    for key in ("legs", "day_plans", "package_total_cents",
                "package_total_with_fees_cents", "fee_line_items", "idempotency_key"):
        assert booked.get(key) == envelope.get(key), f"mismatch on {key}"


# --------------------------------------------------------------------------
# (d) Insufficient funds at confirm → honest 402-in-body, NOT booked.
# --------------------------------------------------------------------------

def test_confirm_insufficient_funds_terminal_not_booked():
    INSUFFICIENT = _merchant_result({
        "status": "denied", "reason": "insufficient_funds", "id": "co_pm",
        "total_cents": 54000, "wallet_balance_cents": 40000,
        "wallet_session_id": "trip-x", "currency": "USD",
    })
    orch = _build_society(complete_resp=(402, INSUFFICIENT), fund_resp=(200, _FUND_OK))
    res = orch.negotiate(copy.deepcopy(_PM_TRIP), commit=False)
    ctx = res["_confirm_ctx"]
    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"],
        plan_envelope={k: v for k, v in res.items() if k != "_confirm_ctx"},
        dest_token=ctx["dest_token"])
    assert booked.get("outcome") == "cannot_satisfy", booked
    assert booked.get("reason") == "insufficient_funds", booked
    assert booked.get("booking_ref") is None, "must NOT mint a booking on insufficient funds"


# --------------------------------------------------------------------------
# (f) ATOMIC path unchanged — commit defaults True → success + booking_ref.
# --------------------------------------------------------------------------

def test_atomic_path_still_books_by_default():
    orch = _build_society(complete_resp=(200, _COMPLETE_OK), fund_resp=(200, _FUND_OK))
    res = orch.negotiate(copy.deepcopy(_PM_TRIP))  # no commit kwarg = atomic
    assert res.get("outcome") == "success", res
    assert res.get("booking_ref"), res
    assert res.get("outcome") != "plan_ready"
    assert "_confirm_ctx" not in res, "atomic path must never carry the confirm context"


# --------------------------------------------------------------------------
# (g) Unified trips store — save_plan / get_plan / mark_booked / idempotent.
# --------------------------------------------------------------------------

def test_store_plan_lifecycle():
    s = SqliteDashboardStore(":memory:")
    s.save_plan({"idempotency_key": "trip-k", "user_id": "demo-1",
                 "checkout_id": "co-local-1", "dest_token": "PG",
                 "package_total_cents": 54000, "envelope": {"outcome": "plan_ready"}})
    row = s.get_plan("trip-k")
    assert row["status"] == "plan_ready" and row["checkout_id"] == "co-local-1"
    assert row["envelope"]["outcome"] == "plan_ready"
    s.mark_booked("trip-k", booking_ref="BK-PG-9",
                  envelope={"outcome": "success", "booking_ref": "BK-PG-9"},
                  confirmed_at="2026-06-27T00:00:00Z")
    row = s.get_plan("trip-k")
    assert row["status"] == "booked" and row["booking_ref"] == "BK-PG-9"
    # mark_booked is a no-op on an already-booked row (idempotent — never re-clobbers).
    s.mark_booked("trip-k", booking_ref="BK-OTHER",
                  envelope={"outcome": "success"}, confirmed_at="2026-06-27T01:00:00Z")
    assert s.get_plan("trip-k")["booking_ref"] == "BK-PG-9", "must not overwrite a booked row"
    assert [t["status"] for t in s.list_trips("demo-1")] == ["booked"]


def test_store_save_plan_never_clobbers_booked():
    s = SqliteDashboardStore(":memory:")
    s.save_plan({"idempotency_key": "trip-k", "user_id": "demo-1",
                 "package_total_cents": 1, "envelope": {"outcome": "plan_ready"}})
    s.mark_booked("trip-k", booking_ref="BK-1", envelope={"outcome": "success"},
                  confirmed_at="t")
    # A late duplicate plan POST must NOT revert a booked row to plan_ready.
    s.save_plan({"idempotency_key": "trip-k", "user_id": "demo-1",
                 "package_total_cents": 2, "envelope": {"outcome": "plan_ready"}})
    assert s.get_plan("trip-k")["status"] == "booked"
