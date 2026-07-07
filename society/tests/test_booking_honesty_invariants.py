"""test_booking_honesty_invariants.py — consolidated cross-terminal invariants for the
booking/consent flow (commit_plan / /confirm).

Both the dateless-booking bug (#1) and the wallet-not-debiting-in-the-UI bug slipped
through because this class of invariant — "the money/consent envelope is honest and
correct for EVERY terminal outcome, not just the happy path" — had no single place that
checked it systematically, even though the codebase is normally rigorous about exactly
this (var-0, fail-conservative, insurance hard-veto all have dedicated tests). This file
is that place. It does NOT duplicate existing per-outcome coverage (see the "already
covered" notes below) — it adds the specific angles that were genuinely missing.

Reused harness: test_consent_split.py's _plan()/_build_society pattern.

Idempotency (repeat /confirm never double-charges): ALREADY COVERED —
test_server_cov2.py::test_debit_within_balance_and_idempotent_replay (replays the same
checkout_id, asserts exactly one debit ledger entry) and
test_cancel_trips_endpoints.py::test_double_cancel_already_cancelled_no_double_credit
(replays cancel, asserts no second credit). Not duplicated here.
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
_INSUFFICIENT = _merchant_result({
    "status": "denied", "reason": "insufficient_funds", "id": "co_pm",
    "total_cents": 54000, "wallet_balance_cents": 40000,
    "wallet_session_id": "trip-x", "currency": "USD",
})


def _plan(complete_resp=(200, _COMPLETE_OK)):
    orch = _build_society(complete_resp=complete_resp, fund_resp=(200, _FUND_OK))
    res = orch.negotiate(copy.deepcopy(_PM_TRIP), commit=False)
    return res, orch


# --------------------------------------------------------------------------
# Invariant 1 — a dateless plan can NEVER reach payment_status:'charged'.
# (test_commit_plan_requires_dates.py already proves the OUTCOME is honest;
# this proves the deeper guarantee — the merchant is never even called, so
# there is no window where a partial/ambiguous charge could occur.)
# --------------------------------------------------------------------------

def test_dateless_plan_never_reaches_the_merchant():
    calls: list[dict] = []

    class _RecordingTransport:
        """Wraps the real merchant call so we can prove it was never invoked."""
        def __call__(self, *a, **kw):
            calls.append({"args": a, "kwargs": kw})
            raise AssertionError("commit_plan must not call the merchant for a dateless plan")

    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = copy.deepcopy({k: v for k, v in res.items() if k != "_confirm_ctx"})
    for leg in envelope.get("legs") or []:
        leg.pop("checkin", None); leg.pop("checkout", None)
    for dp in envelope.get("day_plans") or []:
        dp.pop("checkin", None); dp.pop("checkout", None)

    # Sabotage _do_commit so the test FAILS LOUDLY if commit_plan ever reaches it —
    # a stronger proof than just checking the returned outcome.
    orch._do_commit = _RecordingTransport()  # type: ignore[method-assign]

    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])

    assert booked.get("payment_status") != "charged", booked
    assert calls == [], "the merchant must never be called for a dateless plan"


# --------------------------------------------------------------------------
# Invariant 2 — wallet balance is correct for every terminal, not just success.
# --------------------------------------------------------------------------

def test_success_terminal_wallet_matches_merchant_debit():
    """Already exercised in test_consent_split.py — restated here as the anchor case
    the other terminals below are contrasted against."""
    res, orch = _plan(complete_resp=(200, _COMPLETE_OK))
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}
    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])
    assert booked["payment_status"] == "charged"
    w = booked["wallet"]
    assert w["debited"] is True
    assert w["debit_cents"] == 54000
    assert w["balance_cents"] == 446000  # 500000 seed - 54000 debit


def test_insufficient_funds_terminal_wallet_untouched():
    """GAP CLOSED: test_consent_split.py's insufficient-funds test checked outcome/
    reason/booking_ref but never asserted the wallet balance in the RETURNED ENVELOPE
    matches the pre-commit (undebited) balance — only the merchant-mock's internal
    ledger was checked, at a different test layer (test_server_cov2.py)."""
    res, orch = _plan(complete_resp=(402, _INSUFFICIENT))
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}
    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])
    assert booked.get("outcome") == "cannot_satisfy"
    assert booked.get("payment_status") != "charged"
    assert booked.get("booking_ref") is None
    # The envelope must carry the UNDEBITED balance (what the merchant reported),
    # not silently omit it or echo a stale/charged figure.
    assert booked.get("wallet_balance_cents") == 40000


def test_commit_errored_terminal_never_marks_charged_and_stays_retryable():
    """GAP CLOSED: no existing test drove commit_plan through a genuine commit-layer
    error (merchant 5xx / transport exception) and checked the returned envelope. This
    is the 'needs_reconciliation' path — the riskiest one, since the true booking state
    is ambiguous; the envelope must never claim success and must preserve the SAME
    idempotency_key so the frontend's 'tap Confirm again, it's safe' retry (App.svelte
    confirmBook's 'reconcile' branch) is actually valid."""
    res, orch = _plan(complete_resp=(200, _COMPLETE_OK))
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}

    def _boom(**kw):
        raise RuntimeError("simulated merchant 5xx")
    orch._call_budget_commit = _boom  # type: ignore[method-assign]

    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])

    assert booked.get("outcome") == "cannot_satisfy", booked
    assert booked.get("payment_status") != "charged", booked
    assert booked.get("booking_ref") is None, booked
    assert booked.get("needs_reconciliation") is True, booked
    assert booked.get("idempotency_key") == ctx["idempotency_key"], \
        "the retry contract depends on the SAME key surviving a commit error"
