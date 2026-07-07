"""test_l4_l5_l12_commit_total_and_supper.py — LOW regressions from the round-2
orchestrator-negotiation audit's final batch.

L4: total_cents was never threaded into the budget.commit payload from any of
the 4 orchestrator call sites (_run_negotiation_rounds x2, commit_plan,
_place_supper_order), so session_total_cents was always 0 server-side (see
budget_agent.py's _commit_handler docstring: the orchestrator is documented to
supply this so a needs_consent/needs_mandate message shows the real amount
instead of a misleading $0).

FIX: _do_commit gained an OPTIONAL `total_cents` kwarg, threaded unchanged
into commit_payload["total_cents"]; all 4 call sites now supply the
already-known priced total at that call site.

L5: _place_supper_order asserted "nothing was charged" on decision
commit_failed for every non-accept case uniformly, but commit_failed
specifically means a RAISED commit (merchant 5xx / dropped connection after
the request was sent) whose own documented contract is that merchant-side
state is AMBIGUOUS — the supper checkout may have actually completed.

FIX: commit_failed is now handled distinctly with needs_reconciliation:true,
mirroring the main booking path's honest handling; every OTHER non-accept
decision (a genuine DEFINITE merchant verdict) still reports the flat
"nothing was charged" (accurate for those cases).

L12: the supper order's budget.check payload omitted wallet_session_id
entirely (unlike the main hotel path), so complete_checkout took the
no-wallet code path — ordered:true + a charged_cents figure were reported but
the simulated wallet balance never actually moved, and insufficient-funds
could never fire for a supper order.

FIX: check_payload now threads wallet_session_id = self._wallet_session_id,
mirroring the hotel path's check_payload.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestration.orchestrator import TravelOrchestrator


class _SpyCommitOrch(TravelOrchestrator):
    """Bare orchestrator (no real agent clients) that records every
    budget.check / budget.commit payload it dispatches, without touching a
    real agent/merchant. Mirrors test_m8_wallet_fund_guard.py's _SpyFundOrch
    pattern."""

    def __init__(self):
        super().__init__()
        self.check_payloads: list[dict] = []
        self.commit_payloads: list[dict] = []
        # Scripted responses for the two calls _place_supper_order makes.
        self.check_response: dict = {"decision": "check_ok", "checkout_id": "co_supper_test"}
        self.commit_response: dict = {
            "decision": "accept", "checkout_id": "co_supper_test",
            "booking_ref": "bk_supper_test",
        }

    def _call_budget_check(self, payload: dict):  # type: ignore[override]
        self.check_payloads.append(payload)
        return dict(self.check_response)

    def _call_budget_commit(self, payload: dict):  # type: ignore[override]
        self.commit_payloads.append(payload)
        return dict(self.commit_response)


# ---------------------------------------------------------------------------
# L4 — _do_commit itself: the shared mechanism all 4 call sites route through.
# ---------------------------------------------------------------------------

def test_do_commit_threads_total_cents_into_commit_payload():
    orch = _SpyCommitOrch()
    orch._do_commit(user_id="u1", checkout_id="co1", idempotency_key="trip-abc",
                     total_cents=61500)
    assert len(orch.commit_payloads) == 1
    assert orch.commit_payloads[0].get("total_cents") == 61500, (
        "L4 REGRESSION: _do_commit did not thread total_cents into the "
        "budget.commit payload."
    )


def test_do_commit_omits_total_cents_when_not_supplied_var0_backcompat():
    """var-0/back-compat: a direct/test caller that never supplies
    total_cents (the default, None) must see the SAME payload shape as
    before this fix — no total_cents key at all."""
    orch = _SpyCommitOrch()
    orch._do_commit(user_id="u1", checkout_id="co1", idempotency_key="trip-abc")
    assert len(orch.commit_payloads) == 1
    assert "total_cents" not in orch.commit_payloads[0]


# ---------------------------------------------------------------------------
# L4 — commit_plan() (the /confirm production entrypoint) threads the held
# plan's priced total.
# ---------------------------------------------------------------------------

def test_commit_plan_threads_package_total_into_commit_payload():
    orch = _SpyCommitOrch()
    plan_envelope = {
        "legs": [{"checkin": "2026-10-01", "checkout": "2026-10-04"}],
        "day_plans": [],
        "package_total_with_fees_cents": 61500,
        "package_total_cents": 54000,
    }
    booked = orch.commit_plan(
        user_id="u1", checkout_id="co1", idempotency_key="trip-abc",
        plan_envelope=plan_envelope, dest_token="tokyo",
    )
    assert booked.get("outcome") == "success", booked
    assert len(orch.commit_payloads) == 1
    assert orch.commit_payloads[0].get("total_cents") == 61500, (
        "L4 REGRESSION: commit_plan() did not thread the held plan's priced "
        "total (package_total_with_fees_cents) into the commit payload."
    )


def test_commit_plan_falls_back_to_package_total_cents_when_no_fees_field():
    orch = _SpyCommitOrch()
    plan_envelope = {
        "legs": [{"checkin": "2026-10-01"}],
        "package_total_cents": 54000,
    }
    orch.commit_plan(
        user_id="u1", checkout_id="co1", idempotency_key="trip-abc",
        plan_envelope=plan_envelope, dest_token="tokyo",
    )
    assert orch.commit_payloads[0].get("total_cents") == 54000


# ---------------------------------------------------------------------------
# L4 / L5 / L12 — _place_supper_order (the supper checkout).
# ---------------------------------------------------------------------------

def _make_supper_order_orch(*, checkout_decision: str = "accept",
                            wallet_session_id: str = "trip-hotel-run-0001"):
    orch = _SpyCommitOrch()
    orch._wallet_session_id = wallet_session_id
    if checkout_decision != "accept":
        orch.commit_response = {"decision": checkout_decision, "checkout_id": "co_supper_test"}
    return orch


def test_place_supper_order_threads_total_cents_into_commit_payload():
    orch = _make_supper_order_orch()
    supper = {"line_item": {"food_id": "f1", "qty": 1}, "total_cents": 2500}
    patch = orch._place_supper_order(
        supper=supper, user_id="u1", idempotency_key="trip-hotel-run-0001",
        max_cents=None,
    )
    assert patch.get("ordered") is True, patch
    assert len(orch.commit_payloads) == 1
    assert orch.commit_payloads[0].get("total_cents") == 2500, (
        "L4 REGRESSION: _place_supper_order did not thread the supper "
        "total into the commit payload."
    )


def test_place_supper_order_threads_wallet_session_id_into_check_payload():
    """L12 regression: the supper checkout must be bound to the SAME
    simulated wallet the hotel checkout funded/debited this run, not left
    unbound (the no-wallet code path)."""
    orch = _make_supper_order_orch(wallet_session_id="trip-hotel-run-0001")
    supper = {"line_item": {"food_id": "f1", "qty": 1}, "total_cents": 2500}
    orch._place_supper_order(
        supper=supper, user_id="u1", idempotency_key="trip-hotel-run-0001",
        max_cents=None,
    )
    assert len(orch.check_payloads) == 1
    assert orch.check_payloads[0].get("wallet_session_id") == "trip-hotel-run-0001", (
        "L12 REGRESSION: supper budget.check payload did not carry "
        "wallet_session_id — the supper checkout is not genuinely "
        "wallet-bound and insufficient-funds can never fire for it."
    )


def test_place_supper_order_commit_failed_is_ambiguous_not_flat_denial():
    """L5 regression: commit_failed (a RAISED commit — merchant 5xx/dropped
    connection) means merchant-side state is AMBIGUOUS, not a definite
    'nothing was charged'."""
    orch = _make_supper_order_orch(checkout_decision="commit_failed")
    supper = {"line_item": {"food_id": "f1", "qty": 1}, "total_cents": 2500}
    patch = orch._place_supper_order(
        supper=supper, user_id="u1", idempotency_key="trip-hotel-run-0001",
        max_cents=None,
    )
    assert patch.get("ordered") is False, patch
    assert patch.get("needs_reconciliation") is True, (
        "L5 REGRESSION: commit_failed must surface needs_reconciliation "
        "(merchant-side state is ambiguous) — not a flat 'nothing was "
        "charged' denial."
    )
    assert "nothing was charged" not in (patch.get("order_note") or ""), (
        "L5 REGRESSION: commit_failed must not assert the definite "
        "negative 'nothing was charged' — it is not knowable."
    )


def test_place_supper_order_genuine_veto_still_reports_nothing_charged():
    """Regression guard: a genuine, DEFINITE non-accept decision (e.g. a
    commit-time veto — the merchant re-priced) is NOT ambiguous and must
    keep reporting the accurate 'nothing was charged' — only commit_failed
    gets the ambiguous/needs_reconciliation treatment."""
    orch = _make_supper_order_orch(checkout_decision="veto")
    supper = {"line_item": {"food_id": "f1", "qty": 1}, "total_cents": 2500}
    patch = orch._place_supper_order(
        supper=supper, user_id="u1", idempotency_key="trip-hotel-run-0001",
        max_cents=None,
    )
    assert patch.get("ordered") is False, patch
    assert not patch.get("needs_reconciliation"), patch
    assert "nothing was charged" in (patch.get("order_note") or ""), patch


if __name__ == "__main__":
    test_do_commit_threads_total_cents_into_commit_payload()
    test_do_commit_omits_total_cents_when_not_supplied_var0_backcompat()
    test_commit_plan_threads_package_total_into_commit_payload()
    test_commit_plan_falls_back_to_package_total_cents_when_no_fees_field()
    test_place_supper_order_threads_total_cents_into_commit_payload()
    test_place_supper_order_threads_wallet_session_id_into_check_payload()
    test_place_supper_order_commit_failed_is_ambiguous_not_flat_denial()
    test_place_supper_order_genuine_veto_still_reports_nothing_charged()
    print("ALL L4/L5/L12 TESTS PASSED")
