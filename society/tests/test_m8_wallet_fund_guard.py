"""test_m8_wallet_fund_guard.py — M8 regression: re-negotiating the same
deterministic idempotency_key must not reset a booked/held trip's wallet
ledger.

CONTEXT: `_fund_wallet` dispatches budget.fund keyed by the deterministic
per-run idempotency_key; the merchant-side wallet_fund is a deliberate
create-OR-RESET (var-0 keystone — see test_server_cov2.py's
test_fund_is_reset_per_run_var0, which stays intact and unchanged by this
fix). Because the idempotency_key is a pure digest of the REQUEST, an
IDENTICAL re-POST (retry, second tab, double-submitted form) reuses the SAME
wallet_session_id. If that key already has a booked/held (plan_ready) row in
the trips store, resetting its wallet wipes the ledger/seen-map a later
/cancel needs to compute a refund (and, in atomic/commit=true mode, can
re-fund + re-debit a session that already has a live booking).

FIX: `_fund_wallet` now consults an OPTIONAL `self._trip_lookup` hook
(injected by server.py, mirroring the existing `tracer` callback pattern —
the orchestrator never imports orchestration.store itself) and SKIPS the
fund/reset call entirely when the looked-up row is already booked or
plan_ready. `trip_lookup=None` (the default for any bare/direct-constructed
orchestrator, e.g. existing tests) is a complete no-op — byte-identical to
before this fix (var-0/back-compat).

L10 — INVESTIGATED AND CONFIRMED REAL, follow-up fix below: `trip_lookup`
above only ever checks the row stored under THIS EXACT key
(wallet_session_id). A derived-key row (idk-vN, minted by the M1/M4/M5/M6
consent-plan-mismatch fork guard in server.py) is a DIFFERENT store key that
nonetheless shares THIS run's digest-based wallet_session_id (the wallet was
bound to the digest at negotiate()-time, BEFORE any fork ever happens — the
fork only occurs later, at persist time, in server.py). So if the digest row
is later cancelled/swept while the derived row is booked, a same-day
identical re-POST's `trip_lookup(digest_key)` alone would miss that a SIBLING
row sharing this wallet is booked, and `_fund_wallet` would reset/destroy
that booked trip's shared ledger — a later /cancel on the derived key would
then refund 0.

FIX: a second OPTIONAL hook, `digest_booked_lookup(digest) -> bool` (mirrors
`atomic_committed_lookup`'s injection pattern), checks for ANY row — under
ANY store key — sharing this run's content digest that is currently booked.
`digest_booked_lookup=None` (the default) is a complete no-op — byte-
identical to before this fix (var-0/back-compat).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestration.orchestrator import TravelOrchestrator


class _SpyFundOrch(TravelOrchestrator):
    """Records _call_budget_fund invocations without touching a real agent."""

    def __init__(self, *, trip_lookup=None, digest_booked_lookup=None):
        super().__init__(trip_lookup=trip_lookup, digest_booked_lookup=digest_booked_lookup)
        self.fund_calls: list[dict] = []

    def _call_budget_fund(self, payload: dict):  # type: ignore[override]
        self.fund_calls.append(payload)
        return {"status": "ok", "wallet_session_id": payload.get("wallet_session_id"),
                "balance_cents": payload.get("wallet_balance_cents")}


def _prime(orch: _SpyFundOrch, *, session_id: str = "trip-m8-test") -> None:
    orch._wallet_session_id = session_id
    orch._wallet_balance_cents = 200000


def test_no_trip_lookup_hook_is_backcompat_still_funds():
    """var-0: a bare orchestrator (trip_lookup=None, e.g. every existing direct/
    test caller) behaves exactly as before this fix — _fund_wallet always
    funds/resets."""
    orch = _SpyFundOrch(trip_lookup=None)
    _prime(orch)
    result = orch._fund_wallet()
    assert result is not None
    assert len(orch.fund_calls) == 1
    assert orch.fund_calls[0]["wallet_session_id"] == "trip-m8-test"


def test_trip_lookup_returns_none_still_funds():
    """A genuinely NEW idempotency_key (no existing row) funds normally."""
    orch = _SpyFundOrch(trip_lookup=lambda idk: None)
    _prime(orch)
    result = orch._fund_wallet()
    assert result is not None
    assert len(orch.fund_calls) == 1


def test_trip_lookup_plan_ready_row_skips_fund():
    """M8 core regression: an identical re-POST whose idempotency_key already
    has a HELD (plan_ready) row must NOT re-fund/reset that wallet."""
    row = {"idempotency_key": "trip-m8-test", "status": "plan_ready"}
    orch = _SpyFundOrch(trip_lookup=lambda idk: row)
    _prime(orch)
    result = orch._fund_wallet()
    assert result is None
    assert orch.fund_calls == [], "must NOT call budget.fund for an already-held trip"


def test_trip_lookup_booked_row_skips_fund():
    """M8 core regression: an identical re-POST whose idempotency_key already
    has a BOOKED row must NOT reset its wallet (would destroy the refund
    path for a later /cancel)."""
    row = {"idempotency_key": "trip-m8-test", "status": "booked"}
    orch = _SpyFundOrch(trip_lookup=lambda idk: row)
    _prime(orch)
    result = orch._fund_wallet()
    assert result is None
    assert orch.fund_calls == []


def test_trip_lookup_cancelled_row_does_not_skip():
    """A CANCELLED trip is not "still lifecycled" — re-funding under the same
    key (e.g. a legitimate deliberate re-run) is unaffected; the guard is
    scoped ONLY to booked/plan_ready."""
    row = {"idempotency_key": "trip-m8-test", "status": "cancelled"}
    orch = _SpyFundOrch(trip_lookup=lambda idk: row)
    _prime(orch)
    result = orch._fund_wallet()
    assert result is not None
    assert len(orch.fund_calls) == 1


def test_trip_lookup_raising_is_fail_safe_still_funds():
    """A broken/misbehaving trip_lookup hook must never crash a negotiation —
    it degrades to the normal fund/reset path (logged, not fatal)."""
    def _raiser(idk):
        raise RuntimeError("store unavailable")

    orch = _SpyFundOrch(trip_lookup=_raiser)
    _prime(orch)
    result = orch._fund_wallet()
    assert result is not None
    assert len(orch.fund_calls) == 1


# ---------------------------------------------------------------------------
# L10 — digest_booked_lookup: a DERIVED-key row sharing this run's wallet
# session must ALSO block the fund/reset, even when trip_lookup(exact key)
# finds nothing (or a non-lifecycled row) under the digest key itself.
# ---------------------------------------------------------------------------

def test_digest_booked_lookup_none_hook_is_backcompat_still_funds():
    """var-0: a bare orchestrator (digest_booked_lookup=None, e.g. every
    existing direct/test caller) behaves exactly as before this fix."""
    orch = _SpyFundOrch(trip_lookup=lambda idk: None, digest_booked_lookup=None)
    _prime(orch, session_id="trip-abc123")
    result = orch._fund_wallet()
    assert result is not None
    assert len(orch.fund_calls) == 1


def test_digest_row_cancelled_but_derived_row_booked_still_skips_fund():
    """L10 core regression: the digest-keyed row was cancelled/swept (so
    trip_lookup(digest key) finds nothing lifecycled), but a DERIVED-key row
    sharing this wallet is currently booked — the fund/reset must still be
    skipped, or it would destroy that booked trip's shared ledger."""
    orch = _SpyFundOrch(
        trip_lookup=lambda idk: None,  # digest-keyed row: gone (cancelled/swept)
        digest_booked_lookup=lambda digest: digest == "abc123",  # derived sibling IS booked
    )
    _prime(orch, session_id="trip-abc123")
    result = orch._fund_wallet()
    assert result is None, (
        "L10 REGRESSION: a booked DERIVED-key row sharing this digest's "
        "wallet must block wallet_fund even though the digest-keyed row "
        "itself is gone/non-lifecycled."
    )
    assert orch.fund_calls == []


def test_digest_booked_lookup_receives_the_bare_digest_not_the_trip_prefix():
    """The hook must be called with the BARE digest (matching store.py's
    `digest` column convention — trip_id.replace('trip-', '')), not the
    'trip-' prefixed wallet_session_id."""
    seen: list[str] = []

    def _lookup(digest: str) -> bool:
        seen.append(digest)
        return False

    orch = _SpyFundOrch(trip_lookup=lambda idk: None, digest_booked_lookup=_lookup)
    _prime(orch, session_id="trip-deadbeef42")
    orch._fund_wallet()
    assert seen == ["deadbeef42"], seen


def test_digest_booked_lookup_false_does_not_block_fund():
    """No sibling booked row sharing this digest → normal fund/reset proceeds."""
    orch = _SpyFundOrch(
        trip_lookup=lambda idk: None,
        digest_booked_lookup=lambda digest: False,
    )
    _prime(orch, session_id="trip-abc123")
    result = orch._fund_wallet()
    assert result is not None
    assert len(orch.fund_calls) == 1


def test_digest_booked_lookup_raising_is_fail_safe_still_funds():
    """A broken/misbehaving digest_booked_lookup hook must never crash a
    negotiation — it degrades to the normal fund/reset path (logged, not
    fatal), mirroring trip_lookup's own fail-safe contract."""
    def _raiser(digest):
        raise RuntimeError("store unavailable")

    orch = _SpyFundOrch(trip_lookup=lambda idk: None, digest_booked_lookup=_raiser)
    _prime(orch, session_id="trip-abc123")
    result = orch._fund_wallet()
    assert result is not None
    assert len(orch.fund_calls) == 1


# ---------------------------------------------------------------------------
# L10 — direct store-level unit coverage of get_booked_row_by_digest, the
# primitive server.py's digest_booked_lookup hook wraps.
# ---------------------------------------------------------------------------

def test_store_get_booked_row_by_digest_direct():
    from orchestration.store import SqliteDashboardStore

    store = SqliteDashboardStore(":memory:")

    # No row at all for this digest yet.
    assert store.get_booked_row_by_digest("deadbeef") is None

    # A plan_ready row sharing the digest does NOT count as booked.
    store.save_plan({
        "idempotency_key": "trip-deadbeef", "user_id": "", "digest": "deadbeef",
        "checkout_id": "co-1", "dest_token": "JP", "package_total_cents": 100000,
        "envelope": {"outcome": "plan_ready"},
    })
    assert store.get_booked_row_by_digest("deadbeef") is None

    # A DERIVED-key row (different idempotency_key!) sharing the SAME digest,
    # once booked, IS found by digest — even though its store key differs
    # entirely from the digest-based key above.
    store.save_plan({
        "idempotency_key": "trip-deadbeef-v1234abcde", "user_id": "",
        "digest": "deadbeef", "checkout_id": "co-2", "dest_token": "JP",
        "package_total_cents": 100000, "envelope": {"outcome": "plan_ready"},
    })
    store.mark_booked(
        "trip-deadbeef-v1234abcde", booking_ref="BK-1",
        envelope={"outcome": "success"}, confirmed_at="2026-10-01T00:00:00+00:00",
    )
    found = store.get_booked_row_by_digest("deadbeef")
    assert found is not None
    assert found["idempotency_key"] == "trip-deadbeef-v1234abcde"
    assert found["status"] == "booked"
