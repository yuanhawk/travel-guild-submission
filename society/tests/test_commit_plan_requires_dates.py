"""test_commit_plan_requires_dates.py — commit_plan() refuses to book a dateless plan.

Bug: a held plan with no real check-in dates on any leg could still be committed and
charged via /confirm — the frontend's date picker was advisory-only and the backend had
no independent check. Fix: commit_plan() now returns an honest cannot_satisfy terminal
(never calling _do_commit, never debiting) when neither `legs[].checkin` nor
`day_plans[].checkin` is present anywhere in the plan envelope. This is defense-in-depth
— the frontend also disables Confirm & Book in this case (see
web/e2e/date-required-booking.spec.ts) — guarding a direct API call that
bypasses the UI.

Harness reused from test_consent_split.py: _build_society + _PM_TRIP (which HAS real
dates, used here as the base to strip from) + _COMPLETE_OK/_FUND_OK merchant mocks.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests.test_trace_var0 import _PM_TRIP, _merchant_result
from tests.test_wallet_sim import _build_society

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


def _plan():
    orch = _build_society(complete_resp=(200, _COMPLETE_OK), fund_resp=(200, _FUND_OK))
    res = orch.negotiate(copy.deepcopy(_PM_TRIP), commit=False)
    return res, orch


def _strip_dates(envelope: dict) -> dict:
    """Deep-copy the envelope with every checkin/checkout removed from legs and day_plans
    — simulates a plan that was never given real trip dates."""
    env = copy.deepcopy(envelope)
    for leg in env.get("legs") or []:
        leg.pop("checkin", None)
        leg.pop("checkout", None)
    for dp in env.get("day_plans") or []:
        dp.pop("checkin", None)
        dp.pop("checkout", None)
    return env


def test_commit_plan_refuses_when_no_dates_anywhere():
    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = _strip_dates({k: v for k, v in res.items() if k != "_confirm_ctx"})
    assert not any(leg.get("checkin") for leg in envelope.get("legs") or []), \
        "fixture bug: dates must actually be stripped (else this test is vacuous)"

    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])

    assert booked.get("outcome") == "cannot_satisfy", booked
    assert booked.get("booking_ref") is None, "must NOT mint a booking without dates"
    assert "wallet" not in booked or (booked.get("wallet") or {}).get("debited") is not True, \
        "must NOT debit the wallet — no charge attempt should have happened at all"
    assert booked.get("idempotency_key") == ctx["idempotency_key"], \
        "the SAME key must be safe to retry once dates are set (never invalidated)"
    assert "date" in (booked.get("reason") or "").lower(), \
        "reason must be a human-readable sentence naming the actual problem, not a bare code"


def test_commit_plan_refuses_even_with_dates_on_day_plans_stripped_but_legs_kept_is_fine():
    """Sanity: dates on EITHER legs[] OR day_plans[] is sufficient — only the true
    both-missing case should block. Strip only day_plans dates, keep legs dates intact."""
    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}
    envelope = copy.deepcopy(envelope)
    for dp in envelope.get("day_plans") or []:
        dp.pop("checkin", None)
        dp.pop("checkout", None)
    assert any(leg.get("checkin") for leg in envelope.get("legs") or []), \
        "fixture bug: legs must still carry checkin (else this isn't testing the OR)"

    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])

    assert booked.get("outcome") == "success", booked
    assert booked.get("booking_ref"), "legs[].checkin alone must be enough to allow booking"


def test_commit_plan_with_real_dates_still_books_normally():
    """Regression guard: the normal with-dates path (unmodified _PM_TRIP, already has
    checkin on every leg) must be completely unaffected by the new check."""
    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}
    assert any(leg.get("checkin") for leg in envelope.get("legs") or []), \
        "fixture bug: _PM_TRIP must carry real dates"

    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])

    assert booked.get("outcome") == "success", booked
    assert booked.get("booking_ref"), booked
    assert (booked.get("wallet") or {}).get("debited") is True, booked
