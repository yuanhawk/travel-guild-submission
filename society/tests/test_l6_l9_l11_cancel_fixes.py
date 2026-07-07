"""test_l6_l9_l11_cancel_fixes.py — LOW regressions from the round-2
orchestrator-negotiation audit's final batch, all on POST /cancel.

L6: /cancel treated ANY exception from the local merchant's cancel call as a
successful cancellation — the exception was converted to {"error": ...}, but
the handler never inspected that key: it computed refunded=0, marked the row
cancelled, and returned outcome:"cancelled", refunded_cents:0 regardless,
silently losing the refund path AND making the failure unretryable (the row's
new status='cancelled' trips the already_cancelled short-circuit).

FIX: an `error` key in the merchant call's result now short-circuits to an
honest cancel_errored/needs_reconciliation terminal — the row is NOT marked
cancelled, so a retry with the same idempotency_key remains possible.

L9: /confirm's _commit holds `orch_lock` for its ENTIRE blocking wait (it
must — commit_plan mutates shared singleton state on `_state.orch`); when
`orch_lock` is already held by a long negotiate() worker, a /confirm blocked
waiting on it still occupies one of mgmt_executor's few worker THREADS for
the whole wait — not just the lock. Two such blocked /confirm calls can
fully saturate a 2-worker mgmt_executor, and /cancel (which needs no lock at
all on the local-merchant path) would then queue behind them for a POOL
SLOT.

FIX: /cancel now runs on its OWN dedicated `cancel_executor` pool, split out
from `mgmt_executor` (mirrors L4's original executor-isolation pattern, one
layer down).

L11: concurrent double-/cancel on the same booked trip was not serialized by
the per-idk trip lock (unlike /confirm/refine/replan) — the merchant's own
pop-and-seen-map idempotency prevents an actual double wallet-credit, but the
LOSING request's mark_cancelled(refunded_cents=0) could commit to the row
BEFORE the winner's mark_cancelled(refunded_cents=N) runs, whose own
WHERE status='booked' guard then no-ops — permanently misreporting a real
refund as 0.

FIX: /cancel now wraps its whole validate-cancel-mark_cancelled sequence in
the SAME per-idk trip lock /confirm/refine/replan already use.
"""
from __future__ import annotations

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

from tests.test_cancel_trips_endpoints import (
    _BOOKED_IDK, _USER_ID, _client_with_seeded_store, _login, _inject_mock_merchant,
)


# ---------------------------------------------------------------------------
# L6 — a raised local-merchant exception must not be treated as a success.
# ---------------------------------------------------------------------------

class _RaisingMerchant:
    """Local merchant whose _cancel_checkout always raises (simulates a 5xx /
    dropped connection AFTER the merchant may have already processed it —
    merchant-side state is AMBIGUOUS, per the RAISED-commit contract this
    mirrors elsewhere in the codebase)."""
    def __init__(self):
        self.cancel_calls = 0

    def _cancel_checkout(self, args: dict) -> dict:
        self.cancel_calls += 1
        raise RuntimeError("merchant connection dropped")


class TestCancelMerchantErrorIsHonest(unittest.TestCase):
    def test_cancel_merchant_exception_is_not_reported_as_success(self):
        client, store = _client_with_seeded_store()
        merchant = _RaisingMerchant()
        with client:
            _inject_mock_merchant(merchant)
            token = _login(client, _USER_ID)
            r = client.post("/cancel", json={
                "idempotency_key": _BOOKED_IDK, "user_id": _USER_ID,
                "session_token": token,
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertNotEqual(
            body.get("outcome"), "cancelled",
            "L6 REGRESSION: a raised merchant exception was reported as a "
            "successful cancellation.",
        )
        self.assertTrue(body.get("needs_reconciliation"), body)
        row = store.get_plan(_BOOKED_IDK)
        self.assertEqual(
            row["status"], "booked",
            "L6 REGRESSION: the row was marked cancelled despite the merchant "
            "call having raised — a retry is now impossible (already_cancelled "
            "short-circuit) and the refund path is silently lost.",
        )

    def test_cancel_merchant_exception_leaves_the_row_retryable(self):
        """Once the honest error is surfaced, a SUBSEQUENT retry (e.g. after
        the transient merchant issue clears) must still be possible — proving
        the row was genuinely left in 'booked', not silently locked out."""
        client, store = _client_with_seeded_store()
        merchant = _RaisingMerchant()
        with client:
            _inject_mock_merchant(merchant)
            token = _login(client, _USER_ID)
            body = {"idempotency_key": _BOOKED_IDK, "user_id": _USER_ID,
                    "session_token": token}
            r1 = client.post("/cancel", json=body)
            self.assertTrue(r1.json().get("needs_reconciliation"), r1.json())

            # Swap in a working merchant and retry the SAME idempotency_key.
            from tests.test_cancel_trips_endpoints import _MockMerchant
            working = _MockMerchant()
            _inject_mock_merchant(working)
            r2 = client.post("/cancel", json=body)
        self.assertEqual(r2.json().get("outcome"), "cancelled", r2.json())
        self.assertEqual(r2.json().get("refunded_cents"), 75000)


# ---------------------------------------------------------------------------
# L9 — /cancel must not queue behind /confirm calls occupying mgmt_executor.
# ---------------------------------------------------------------------------

class TestCancelHasItsOwnExecutorPool(unittest.TestCase):
    def test_cancel_completes_promptly_while_mgmt_executor_is_saturated(self):
        client, store = _client_with_seeded_store()
        with client:
            from tests.test_cancel_trips_endpoints import _MockMerchant
            mock_m = _MockMerchant()
            _inject_mock_merchant(mock_m)

            # Saturate mgmt_executor (2 workers) with blocking tasks — mirrors
            # test_l4_mgmt_executor_isolation.py's technique, one layer down.
            release = threading.Event()
            started = threading.Barrier(2 + 1, timeout=5)

            def _blocking_task():
                try:
                    started.wait(timeout=5)
                except threading.BrokenBarrierError:
                    pass
                release.wait(timeout=10)

            futures = [server._state.mgmt_executor.submit(_blocking_task) for _ in range(2)]
            started.wait(timeout=5)  # both mgmt_executor workers now occupied

            try:
                token = _login(client, _USER_ID)
                t0 = time.monotonic()
                r = client.post("/cancel", json={
                    "idempotency_key": _BOOKED_IDK, "user_id": _USER_ID,
                    "session_token": token,
                })
                elapsed = time.monotonic() - t0
            finally:
                release.set()
                for f in futures:
                    f.result(timeout=5)

        self.assertEqual(r.json().get("outcome"), "cancelled", r.json())
        self.assertLess(
            elapsed, 5.0,
            f"/cancel took {elapsed:.2f}s while mgmt_executor was saturated — "
            "L9 REGRESSION: it queued behind /confirm-class work instead of "
            "using its own dedicated cancel_executor pool.",
        )

    def test_cancel_executor_is_a_distinct_pool_instance(self):
        """Structural regression: cancel_executor must be a DISTINCT
        ThreadPoolExecutor from both the negotiate executor and mgmt_executor."""
        client = TestClient(server.build_app())
        with client:
            self.assertIsNot(server._state.cancel_executor, server._state.executor)
            self.assertIsNot(server._state.cancel_executor, server._state.mgmt_executor)


# ---------------------------------------------------------------------------
# L11 — concurrent /cancel on the SAME trip must serialize via the trip lock.
# ---------------------------------------------------------------------------

class _SlowMockMerchant:
    """Local merchant whose _cancel_checkout blocks until released — lets the
    test control the exact interleaving of two concurrent /cancel calls."""
    def __init__(self):
        self.call_started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def _cancel_checkout(self, args: dict) -> dict:
        self.calls += 1
        self.call_started.set()
        self.release.wait(timeout=30)
        co_id = (args.get("checkout") or {}).get("id", "")
        return {"id": co_id, "status": "cancelled", "wallet_credit_cents": 75000,
                "wallet_balance_cents": 575000, "simulated": True}


class TestConcurrentCancelIsSerializedByTripLock(unittest.TestCase):
    def test_second_concurrent_cancel_never_reaches_the_merchant(self):
        client, store = _client_with_seeded_store()
        merchant = _SlowMockMerchant()
        with client:
            _inject_mock_merchant(merchant)
            token = _login(client, _USER_ID)
            results: dict = {}

            def _do_cancel(key: str):
                results[key] = client.post("/cancel", json={
                    "idempotency_key": _BOOKED_IDK, "user_id": _USER_ID,
                    "session_token": token,
                })

            t1 = threading.Thread(target=_do_cancel, args=("first",))
            t1.start()
            self.assertTrue(
                merchant.call_started.wait(timeout=5),
                "first cancel never reached the merchant",
            )
            # The SECOND concurrent /cancel starts WHILE the first is still
            # blocked mid-merchant-call (row still 'booked' in the DB — the
            # first hasn't written mark_cancelled yet). Pre-fix (no trip lock),
            # this races straight through to the merchant a SECOND time.
            t2 = threading.Thread(target=_do_cancel, args=("second",))
            t2.start()
            time.sleep(0.3)  # give t2 a chance to attempt (and, with the fix, block on) the lock
            merchant.release.set()
            t1.join(timeout=10)
            t2.join(timeout=10)

        self.assertEqual(
            merchant.calls, 1,
            "L11 REGRESSION: the second concurrent /cancel reached the "
            "merchant a SECOND time — it was not serialized behind the "
            "per-idk trip lock, and could race the winner's write with a "
            "mismatched refund.",
        )
        outcomes = sorted(r.json()["outcome"] for r in results.values())
        self.assertEqual(outcomes, ["already_cancelled", "cancelled"], results)
        winner = next(r.json() for r in results.values() if r.json()["outcome"] == "cancelled")
        self.assertEqual(winner["refunded_cents"], 75000)
        row = store.get_plan(_BOOKED_IDK)
        self.assertEqual(
            row["refunded_cents"], 75000,
            "L11 REGRESSION: the stored refund was overwritten/mismatched by "
            "the losing concurrent cancel.",
        )


if __name__ == "__main__":
    unittest.main()
