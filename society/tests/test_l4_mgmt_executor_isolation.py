"""test_l4_mgmt_executor_isolation.py — L4 regression: /confirm and /cancel
must not queue behind concurrent /negotiate stream workers.

CONTEXT: /negotiate, /negotiate_text, /confirm, /cancel, /refine, and the
post-booking Telegram dispatch used to share ONE ThreadPoolExecutor
(max_workers=4). N concurrent long-running negotiations (LLM-on runs can
take ~110s) could occupy every worker, so a /confirm or /cancel for an
ALREADY-HELD/booked trip (normally a single sub-second merchant round-trip)
would queue behind ALL of them with no feedback — the POST returns 200
immediately (SSE-streamed elsewhere) so the client just hangs.

FIX: a SEPARATE, small `_state.mgmt_executor` now serves /confirm's _commit,
/cancel's _do_cancel, and the post-booking Telegram dispatch — isolated from
`_state.executor` (still used by /negotiate/_text stream workers and
/refine's re-plan). This test proves the isolation directly: saturate
`_state.executor` with blocking work (more tasks than its worker count) and
confirm a concurrent /confirm still completes promptly instead of queuing
behind it.
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

_PLAN_IDK = "trip-l4-mgmt-executor-0001"
_OWNER = "owner-token-l4-mgmt-executor"

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

_STRUCTURED_REQUEST = {
    "user_id": "", "total_budget_cents": 200000, "today": "2026-10-01",
    "legs": [{"city": "tokyo", "checkin": "2026-10-10", "checkout": "2026-10-12",
              "adults": 2, "pace": "moderate"}],
}


class _FastCommitOrch:
    """commit_plan returns immediately — used to measure /confirm's OWN
    latency, isolated from any negotiation cost."""

    def commit_plan(self, *, user_id, checkout_id, idempotency_key,
                    plan_envelope, dest_token, merchant_user_id=""):
        booked = copy.deepcopy(plan_envelope)
        booked["payment_status"] = "paid"
        booked["booking_ref"] = f"BK-{idempotency_key}"
        return {**booked, "outcome": "success", "booking_ref": f"BK-{idempotency_key}",
                "idempotency_key": idempotency_key, "payment_status": "paid"}


def _seed(store: SqliteDashboardStore) -> None:
    store.save_plan({
        "idempotency_key": _PLAN_IDK, "user_id": "", "owner_token": _OWNER,
        "checkout_id": "co-l4-mgmt-executor-001", "dest_token": "JP",
        "package_total_cents": 150000, "request": _STRUCTURED_REQUEST,
        "envelope": copy.deepcopy(_ENVELOPE),
    })


class TestConfirmDoesNotQueueBehindSaturatedNegotiateExecutor(unittest.TestCase):
    def test_confirm_completes_promptly_while_executor_is_saturated(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed(store)
        client = TestClient(server.build_app())
        with client:
            server._state.orch = _FastCommitOrch()
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}

            # Saturate the NEGOTIATE executor with blocking work — MORE tasks
            # than its worker count (4), so every worker is busy and more
            # would queue behind them on the old shared-executor design.
            release = threading.Event()
            started = threading.Barrier(4 + 1, timeout=5)

            def _blocking_task():
                try:
                    started.wait(timeout=5)
                except threading.BrokenBarrierError:
                    pass
                release.wait(timeout=10)

            futures = [server._state.executor.submit(_blocking_task) for _ in range(4)]
            started.wait(timeout=5)  # all 4 workers now occupied

            try:
                t0 = time.monotonic()
                resp = client.post("/confirm", json={
                    "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                })
                elapsed = time.monotonic() - t0
            finally:
                release.set()
                for f in futures:
                    f.result(timeout=5)

            body = resp.json()
            self.assertEqual(body.get("outcome"), "success", body)
            self.assertEqual(body.get("booking_ref"), f"BK-{_PLAN_IDK}")
            # L4 core assertion: /confirm did NOT queue behind the saturated
            # negotiate executor (which is fully occupied until `release` is
            # set) — it must complete via the SEPARATE mgmt_executor well
            # under the blocking tasks' hold duration.
            self.assertLess(
                elapsed, 5.0,
                f"/confirm took {elapsed:.2f}s while the negotiate executor was "
                "saturated — it queued behind it instead of using the isolated "
                "mgmt_executor (L4 regression).",
            )

    def test_confirm_and_cancel_use_a_separate_executor_instance(self):
        """Structural regression: mgmt_executor must be a DISTINCT
        ThreadPoolExecutor instance from the negotiate executor."""
        client = TestClient(server.build_app())
        with client:
            self.assertIsNot(server._state.mgmt_executor, server._state.executor)


if __name__ == "__main__":
    unittest.main()
