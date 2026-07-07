"""test_m7_atomic_wallet_fund_guard.py — M7 regression: a retried identical
POST /negotiate in ATOMIC mode (commit=true, i.e. no `plan` in the request)
must not re-fund AND re-debit the wallet for a second live merchant booking.

CONTEXT: the prior MEDIUM/LOW batch's M8 fix gave `_fund_wallet` a guard
(`self._trip_lookup`) that skips wallet_fund when the idempotency_key already
has a booked/plan_ready row in the trips store. But ATOMIC-mode SUCCESSES are
NEVER persisted to the trips store at all — only plan_ready (plan-mode)
results are, via server.py's _persist_and_sanitize_plan. So a retried
identical POST /negotiate that omits `plan` (e.g. a client timeout-retry)
finds nothing via `trip_lookup`, `_fund_wallet` re-funds (create-OR-RESET)
and the run re-debits — ending with TWO completed merchant checkouts/two
booking_refs for one logical request, and the wallet ledger showing only the
SECOND debit (the first is silently erased by the fund-reset).

FIX: give atomic-mode bookings a lightweight idempotency record too (the
`atomic_bookings` store table + `mark_atomic_committed`/`was_atomic_committed`),
consulted by `_fund_wallet` via a new `self._atomic_committed_lookup` hook
(mirrors the `trip_lookup` pattern) ALONGSIDE the existing check — and stamped
by a new `self._atomic_commit_marker` hook, called once by negotiate() right
after an atomic-mode SUCCESS finalizes.
"""
from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestration.orchestrator import TravelOrchestrator
from orchestration.store import SqliteDashboardStore
from tests.test_trace_var0 import _PM_TRIP
from tests.test_wallet_sim import _build_society


# ---------------------------------------------------------------------------
# Store-level contract: mark_atomic_committed / was_atomic_committed.
# ---------------------------------------------------------------------------

def test_store_was_atomic_committed_false_for_an_unseen_key():
    store = SqliteDashboardStore(":memory:")
    assert store.was_atomic_committed("trip-never-seen") is False


def test_store_mark_then_was_atomic_committed_true():
    store = SqliteDashboardStore(":memory:")
    store.mark_atomic_committed("trip-atomic-1", booking_ref="BK-atomic-1")
    assert store.was_atomic_committed("trip-atomic-1") is True


def test_store_mark_atomic_committed_is_idempotent_on_repeat():
    """A repeat mark for the same key must never raise (defensive double-
    invocation) and must never lose the original booking_ref."""
    store = SqliteDashboardStore(":memory:")
    store.mark_atomic_committed("trip-atomic-2", booking_ref="BK-original")
    store.mark_atomic_committed("trip-atomic-2", booking_ref="BK-should-not-overwrite")
    assert store.was_atomic_committed("trip-atomic-2") is True


def test_store_atomic_committed_is_independent_of_the_trips_table():
    """The whole point of this table: it has NOTHING to do with the `trips`
    table (no row is ever created there for an atomic-mode booking)."""
    store = SqliteDashboardStore(":memory:")
    store.mark_atomic_committed("trip-atomic-3", booking_ref="BK-atomic-3")
    assert store.get_plan("trip-atomic-3") is None
    assert store.was_atomic_committed("trip-atomic-3") is True


# ---------------------------------------------------------------------------
# Orchestrator-level: _fund_wallet's new atomic_committed_lookup guard.
# ---------------------------------------------------------------------------

class _SpyFundOrch(TravelOrchestrator):
    """Records _call_budget_fund invocations without touching a real agent."""

    def __init__(self, *, trip_lookup=None, atomic_committed_lookup=None,
                atomic_commit_marker=None):
        super().__init__(
            trip_lookup=trip_lookup,
            atomic_committed_lookup=atomic_committed_lookup,
            atomic_commit_marker=atomic_commit_marker,
        )
        self.fund_calls: list[dict] = []

    def _call_budget_fund(self, payload: dict):  # type: ignore[override]
        self.fund_calls.append(payload)
        return {"status": "ok", "wallet_session_id": payload.get("wallet_session_id"),
                "balance_cents": payload.get("wallet_balance_cents")}


def _prime(orch: _SpyFundOrch, *, session_id: str = "trip-m7-test") -> None:
    orch._wallet_session_id = session_id
    orch._wallet_balance_cents = 200000


def test_no_atomic_committed_lookup_hook_is_backcompat_still_funds():
    """var-0: a bare orchestrator (both hooks None, e.g. every existing
    direct/test caller) behaves exactly as before this fix."""
    orch = _SpyFundOrch()
    _prime(orch)
    result = orch._fund_wallet()
    assert result is not None
    assert len(orch.fund_calls) == 1


def test_atomic_committed_lookup_false_still_funds():
    """A genuinely fresh idempotency_key (never atomically committed) funds
    normally."""
    orch = _SpyFundOrch(atomic_committed_lookup=lambda idk: False)
    _prime(orch)
    result = orch._fund_wallet()
    assert result is not None
    assert len(orch.fund_calls) == 1


def test_atomic_committed_lookup_true_skips_fund():
    """M7 core regression: a retried identical atomic POST whose
    idempotency_key already committed a real merchant booking must NOT
    re-fund/reset the wallet — trip_lookup alone (checking the trips STORE)
    has nothing to find for an atomic-mode success, so this second,
    independent hook must be the one that engages."""
    orch = _SpyFundOrch(
        trip_lookup=lambda idk: None,  # nothing in the trips store (atomic mode)
        atomic_committed_lookup=lambda idk: True,
    )
    _prime(orch)
    result = orch._fund_wallet()
    assert result is None
    assert orch.fund_calls == [], (
        "REGRESSION (M7): an already-committed atomic booking's wallet was "
        "re-funded/reset — a retried identical POST /negotiate would "
        "re-debit for a second live merchant checkout."
    )


def test_atomic_committed_lookup_raising_is_fail_safe_still_funds():
    """A broken/misbehaving hook must never crash a negotiation — degrades to
    the normal fund/reset path (logged, not fatal)."""
    def _raiser(idk):
        raise RuntimeError("store unavailable")

    orch = _SpyFundOrch(atomic_committed_lookup=_raiser)
    _prime(orch)
    result = orch._fund_wallet()
    assert result is not None
    assert len(orch.fund_calls) == 1


def test_both_hooks_present_trip_lookup_plan_ready_still_takes_priority():
    """Sanity: the two guards are independent/additive — a plan-mode held row
    (caught by trip_lookup, the M8-batch guard) still skips funding even when
    atomic_committed_lookup says False (this key was never atomically
    committed — it's a plan_ready row, a completely different lifecycle)."""
    row = {"idempotency_key": "trip-m7-test", "status": "plan_ready"}
    orch = _SpyFundOrch(
        trip_lookup=lambda idk: row,
        atomic_committed_lookup=lambda idk: False,
    )
    _prime(orch)
    result = orch._fund_wallet()
    assert result is None
    assert orch.fund_calls == []


# ---------------------------------------------------------------------------
# End-to-end: negotiate() itself calls the marker on a genuine atomic SUCCESS,
# and a real store-backed lookup makes _fund_wallet skip the retry.
# ---------------------------------------------------------------------------

def test_negotiate_atomic_success_stamps_the_marker_and_retry_skips_refund():
    store = SqliteDashboardStore(":memory:")
    COMPLETE_OK = {
        "jsonrpc": "2.0", "id": 1, "result": {
            "structuredContent": {
                "id": "co_pm", "status": "complete", "user_id": "trace-test",
                "line_items": [], "total_cents": 54000, "currency": "USD",
                "buyer_consent": True, "booking_ref": "BK-atomic-e2e",
                "wallet_session_id": "PLACEHOLDER", "wallet_debit_cents": 54000,
                "wallet_balance_cents": 446000, "simulated": True,
            },
            "content": [],
        },
    }
    FUND_OK = {
        "jsonrpc": "2.0", "id": 1, "result": {
            "structuredContent": {
                "status": "ok", "wallet_session_id": "PLACEHOLDER",
                "seed_cents": 500000, "balance_cents": 500000,
                "simulated": True, "note": "sim",
            },
            "content": [],
        },
    }
    orch = _build_society(complete_resp=(200, COMPLETE_OK), fund_resp=(200, FUND_OK))
    # Wire the M7 hooks directly to a real store (mirrors server.py's wiring,
    # without needing the full HTTP app).
    orch._atomic_commit_marker = (
        lambda idk, booking_ref: store.mark_atomic_committed(idk, booking_ref=booking_ref))
    orch._atomic_committed_lookup = lambda idk: store.was_atomic_committed(idk)

    trip = copy.deepcopy(_PM_TRIP)
    # The digest-based idempotency_key is deterministic — recompute it the
    # same way orchestrator.negotiate() does at entry, independent of run order.
    from orchestration.orchestrator import _request_digest
    expected_idk = f"trip-{_request_digest(trip)}"

    # Atomic mode: commit=True (no plan-only hold) — the exact path M7 covers.
    first = orch.negotiate(copy.deepcopy(trip), commit=True)
    assert first.get("outcome") == "success", first

    assert store.was_atomic_committed(expected_idk) is True, (
        "REGRESSION (M7): a genuine atomic-mode SUCCESS never stamped the "
        "committed marker — a retried identical POST /negotiate has nothing "
        "for _fund_wallet's guard to find."
    )

    # A retried IDENTICAL atomic POST must not re-fund (the guard should
    # engage and skip wallet_fund entirely this time). orch._wallet_session_id
    # is left set to this SAME digest key by the negotiate() call above.
    assert orch._wallet_session_id == expected_idk
    result = orch._fund_wallet()
    assert result is None, (
        "REGRESSION (M7): _fund_wallet did not skip re-funding an already "
        "atomically-committed idempotency_key."
    )
