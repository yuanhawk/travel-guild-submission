"""test_h3_sweep_confirm_race.py — H3 HIGH regression: the trips-store
background/overflow sweep must never delete a plan_ready row WHILE /confirm
is mid-flight (between its validation read and mark_booked), and mark_booked
must report (not silently swallow) a genuine row-vanished race.

CONTEXT (bug): store.py's sweep_expired() (called both by the 10-minute
background thread AND by save_plan()'s MAX_TRIPS overflow guard — the latter
with a cutoff of "now", i.e. it can delete EVERY user's un-expired plan_ready
row, not just the caller's own) ran with NO awareness of server.py's per-idk
trip lock (_state.trip_locks / _get_trip_lock) that already serializes
/confirm, /refine, and /replan against EACH OTHER. A sweep landing between
/confirm's row-read and its mark_booked write could delete the row the
merchant had just been (or was about to be) irreversibly charged for —
mark_booked's compare-and-set UPDATE then silently matched 0 rows (rowcount
was never checked), so /confirm still returned outcome:"success" + a real
booking_ref while the row vanished: GET /trips omits it, /cancel and a
retried /confirm both return unknown_plan, and the wallet debit is
unrecoverable through any API.

FIX:
  1. store.set_active_key_guard() lets server.py inject a callable returning
     the CURRENTLY-locked idempotency_keys (server._state.trip_locks);
     sweep_expired() excludes them from its DELETE. Wired in server._lifespan.
  2. store.mark_booked() now returns whether its UPDATE matched a row;
     server.py's /confirm checks this and — on the residual case where the
     row is still somehow gone — logs at ERROR and threads an honest
     `store_sync_warning` into the response instead of a bare silent success.

Tests:
  1. test_concurrent_sweep_during_confirm_does_not_delete_the_row — the
     PRIMARY fix: with the real trip-lock guard wired (as _lifespan does), a
     sweep racing a slow commit_plan() must skip the locked row entirely.
  2. test_sweep_without_guard_deletes_row_but_confirm_still_surfaces_it_honestly
     — with the guard explicitly disabled (simulating the pre-fix code path),
     the sweep DOES delete the row (proving the race is real), and /confirm's
     response — thanks to the mark_booked rowcount check — carries an honest
     store_sync_warning instead of a bare, misleading "success".
  3. test_mark_booked_returns_false_for_a_vanished_row — direct store-level
     unit check of the rowcount contract.
  4. test_sweep_excludes_locked_keys_directly — direct store-level unit check
     of set_active_key_guard()/sweep_expired()'s exclusion.
"""
from __future__ import annotations

import copy
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from orchestration import server
from orchestration.store import SqliteDashboardStore, set_store

_PLAN_IDK = "trip-h3-sweep-race-0001"
_OWNER = "owner-token-h3-sweep-race"

_ENVELOPE = {
    "outcome": "plan_ready",
    "idempotency_key": _PLAN_IDK,
    "payment_status": "held",
    "booking_ref": None,
    "package_total_with_fees_cents": 150000,
    "package_total_cents": 150000,
    "wallet": {"debited": False, "held_cents": 150000},
    "legs": [{"city": "tokyo"}],
    "day_plans": [],
}

# Ancient created_at so an age-based background sweep would consider this row
# "abandoned" — the overflow sweep (cutoff="now") would delete ANY plan_ready
# row regardless of age, so this also covers that call site.
_ANCIENT = "2000-01-01T00:00:00+00:00"


def _seed(store: SqliteDashboardStore) -> None:
    store.save_plan({
        "idempotency_key": _PLAN_IDK, "user_id": "", "owner_token": _OWNER,
        "checkout_id": "co-h3-sweep-race-001", "dest_token": "JP",
        "package_total_cents": 150000,
        "request": {"user_id": "", "legs": [{"city": "tokyo"}]},
        "envelope": copy.deepcopy(_ENVELOPE),
        "now": _ANCIENT,
    })


class _SlowCommitOrch:
    """commit_plan() blocks on an Event (released by the test) instead of a
    fixed sleep, so the race window is deterministic and the test never
    flakes on timing. Signals `commit_started` the instant it begins."""

    def __init__(self):
        self.commit_started = threading.Event()
        self.release_commit = threading.Event()

    def commit_plan(self, *, user_id, checkout_id, idempotency_key,
                    plan_envelope, dest_token, merchant_user_id=""):
        self.commit_started.set()
        self.release_commit.wait(timeout=30)
        return {
            "outcome": "success", "booking_ref": f"BK-{idempotency_key}",
            "idempotency_key": idempotency_key, "payment_status": "charged",
        }


class TestSweepExcludesInFlightConfirm(unittest.TestCase):
    """PRIMARY fix: a concurrent sweep must never delete a row /confirm is
    currently holding (via the real per-idk trip lock)."""

    def test_concurrent_sweep_during_confirm_does_not_delete_the_row(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed(store)
        orch = _SlowCommitOrch()
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch
            # Fresh per-idk lock maps for this app instance (mirrors _lifespan);
            # the sweep-exclusion guard is wired by _lifespan itself (real
            # server startup code path — NOT re-wired here, so this test
            # exercises the actual production wiring).
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}

            confirm_result: dict = {}

            def _do_confirm():
                confirm_result["r"] = client.post("/confirm", json={
                    "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                })

            t = threading.Thread(target=_do_confirm)
            t.start()
            self.assertTrue(
                orch.commit_started.wait(timeout=30),
                "confirm's commit_plan() never started",
            )
            # The exact race window the finding describes: a sweep (background
            # thread OR another user's MAX_TRIPS overflow guard) runs WHILE
            # /confirm is mid-commit, with a cutoff that would otherwise
            # delete this row (ancient created_at; here we also use "now" as
            # the cutoff to mirror the overflow sweep's most aggressive case).
            deleted = store.sweep_expired(older_than_iso=_iso_now())
            orch.release_commit.set()
            t.join(timeout=10)

        self.assertEqual(
            deleted, 0,
            "H3 regression: the concurrent sweep deleted a row /confirm was "
            "actively holding via the trip lock.",
        )
        confirm_outcome = confirm_result["r"].json()
        self.assertEqual(confirm_outcome.get("outcome"), "success")
        self.assertNotIn(
            "store_sync_warning", confirm_outcome,
            "no warning expected — the row was never actually lost",
        )
        row = store.get_plan(_PLAN_IDK)
        self.assertIsNotNone(
            row, "H3 regression: the trip row vanished after a successful /confirm")
        self.assertEqual(row["status"], "booked")
        self.assertEqual(row["booking_ref"], f"BK-{_PLAN_IDK}")


class TestMarkBookedHonestlySurfacesAVanishedRow(unittest.TestCase):
    """With the sweep-exclusion guard explicitly disabled (simulating the
    pre-fix code path), the race genuinely deletes the row — proving it is
    real — and /confirm's response must honestly surface that via
    store_sync_warning (the mark_booked rowcount check), never a bare silent
    success with no trace."""

    def test_sweep_without_guard_deletes_row_but_confirm_still_surfaces_it_honestly(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed(store)
        orch = _SlowCommitOrch()
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}
            # Simulate the PRE-FIX code path: no sweep-exclusion guard at all.
            store.set_active_key_guard(None)

            confirm_result: dict = {}

            def _do_confirm():
                confirm_result["r"] = client.post("/confirm", json={
                    "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                })

            t = threading.Thread(target=_do_confirm)
            t.start()
            self.assertTrue(orch.commit_started.wait(timeout=30))
            deleted = store.sweep_expired(older_than_iso=_iso_now())
            orch.release_commit.set()
            t.join(timeout=10)

        # The race is real: the un-guarded sweep genuinely deleted the row
        # while /confirm was mid-commit.
        self.assertEqual(deleted, 1)
        self.assertIsNone(store.get_plan(_PLAN_IDK))

        confirm_outcome = confirm_result["r"].json()
        # The merchant booking already happened — /confirm still reports it
        # (the charge is real; we cannot honestly claim otherwise) — but MUST
        # now surface the store-sync loss instead of a bare, misleading
        # "success" with no trace anywhere.
        self.assertEqual(confirm_outcome.get("outcome"), "success")
        self.assertIn(
            "store_sync_warning", confirm_outcome,
            "H3 regression: mark_booked's rowcount==0 case was silently "
            "swallowed — /confirm returned a bare success with no indication "
            "the trip record was lost.",
        )


class TestStoreLevelContracts(unittest.TestCase):
    """Direct unit coverage of the two store.py primitives this fix adds/
    changes, independent of the HTTP layer above."""

    def test_mark_booked_returns_false_for_a_vanished_row(self):
        store = SqliteDashboardStore(":memory:")
        ok = store.mark_booked(
            "trip-does-not-exist", booking_ref="BK-x",
            envelope={"outcome": "success"}, confirmed_at=_iso_now())
        self.assertFalse(ok)

    def test_mark_booked_returns_true_for_a_real_row(self):
        store = SqliteDashboardStore(":memory:")
        _seed(store)
        ok = store.mark_booked(
            _PLAN_IDK, booking_ref="BK-real",
            envelope=copy.deepcopy(_ENVELOPE), confirmed_at=_iso_now())
        self.assertTrue(ok)

    def test_sweep_excludes_locked_keys_directly(self):
        store = SqliteDashboardStore(":memory:")
        store.save_plan({
            "idempotency_key": "trip-h3-locked", "user_id": "u1",
            "package_total_cents": 1000,
            "request": {}, "envelope": {"outcome": "plan_ready"},
            "now": _ANCIENT,
        })
        store.save_plan({
            "idempotency_key": "trip-h3-unlocked", "user_id": "u2",
            "package_total_cents": 1000,
            "request": {}, "envelope": {"outcome": "plan_ready"},
            "now": _ANCIENT,
        })
        store.set_active_key_guard(lambda: {"trip-h3-locked"})
        deleted = store.sweep_expired(older_than_iso=_iso_now())
        self.assertEqual(deleted, 1)
        self.assertIsNotNone(store.get_plan("trip-h3-locked"))
        self.assertIsNone(store.get_plan("trip-h3-unlocked"))


def _iso_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


if __name__ == "__main__":
    unittest.main()
