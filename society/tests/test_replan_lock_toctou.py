"""test_replan_lock_toctou.py — follow-up to the security review of the
orchestrator-negotiation CRITICAL+HIGH fix pass.

Covers 2 issues the audit flagged in server.py's per-idk trip-lock fix:

1. LOW (advisory) — `_get_trip_lock`'s dict (`_state.trip_locks` /
   `_state.trip_lock_refs`) grew one entry per distinct idempotency_key ever
   seen and never shrank for the life of the process. Fixed via
   `_TripLockHandle`'s ref-counted eviction: once the last caller releases a
   given idk's lock, both dict entries for that idk are popped.
   TestTripLockDictIsEvicted below is the regression lock.

2. LOW (flagged "out of scope" by the audit, but the same lock mechanism now
   also covers it) — /confirm and /refine held the per-idk lock across their
   full read-validate-mutate-write sequence, but /replan did NOT: it read and
   wrote the SAME trip row's envelope with no lock at all, so a /replan
   racing a concurrent /confirm's commit (or a /refine's re-plan) could lose
   its edit — the confirm's mark_booked write (using the commit result it
   captured before /replan ever ran) would silently clobber whatever /replan
   had just persisted. /replan now holds the same `_get_trip_lock(idk)` for
   its entire read-validate-mutate-persist sequence, making it mutually
   exclusive with /confirm and /refine on the same idk.
   TestReplanConfirmRace below is the regression lock for the specific
   lost-update failure mode.
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

_PLAN_IDK = "trip-replan-lock-toctou-0001"
_OWNER = "owner-token-replan-lock-toctou"

_ATT_KEEP = {
    "name": "Senso-ji Temple", "name_en": "Senso-ji", "category": "temple",
    "wikidata": "Q844240", "lat": 35.71, "lon": 139.79, "provenance": "test",
}
_ATT_REMOVE = {
    "name": "Ueno Park", "name_en": "Ueno Park", "category": "park",
    "wikidata": None, "lat": 35.71, "lon": 139.77, "provenance": "test",
}

_ENVELOPE = {
    "outcome": "plan_ready",
    "idempotency_key": _PLAN_IDK,
    "payment_status": "held",
    "booking_ref": None,
    "package_total_with_fees_cents": 150000,
    "package_total_cents": 150000,
    "wallet": {"debited": False, "held_cents": 150000},
    "legs": [{"city": "tokyo"}],
    "day_plans": [
        {
            "city": "tokyo",
            "days": [
                {
                    "day_index": 0,
                    "bad_weather": False,
                    "attractions": [copy.deepcopy(_ATT_KEEP), copy.deepcopy(_ATT_REMOVE)],
                    "meals": {},
                },
            ],
            "unscheduled_attractions": [],
        }
    ],
}

_STRUCTURED_REQUEST = {
    "user_id": "", "total_budget_cents": 200000, "today": "2026-10-01",
    "legs": [{"city": "tokyo", "checkin": "2026-10-10", "checkout": "2026-10-12",
              "adults": 2, "pace": "moderate"}],
}

_REMOVE_OP = [{"op": "remove_place", "leg_index": 0, "day_index": 0,
               "position": 1, "attraction_ref": "Ueno Park"}]


def _seed(store: SqliteDashboardStore) -> None:
    store.save_plan({
        "idempotency_key": _PLAN_IDK, "user_id": "", "owner_token": _OWNER,
        "checkout_id": "co-replan-lock-toctou-001", "dest_token": "JP",
        "package_total_cents": 150000, "request": _STRUCTURED_REQUEST,
        "envelope": copy.deepcopy(_ENVELOPE),
    })


class _SlowCommitOrch:
    """commit_plan() sleeps (simulating a real multi-second commit) and
    signals `commit_started` the instant it begins — used to deterministically
    land a concurrent /replan inside the window between /confirm's row-read
    (still status='plan_ready') and its mark_booked write."""

    def __init__(self, *, commit_delay_s: float = 0.4):
        self.commit_started = threading.Event()
        self._commit_delay_s = commit_delay_s

    def commit_plan(self, *, user_id, checkout_id, idempotency_key,
                    plan_envelope, dest_token, merchant_user_id=""):
        self.commit_started.set()
        time.sleep(self._commit_delay_s)
        # Returns the SAME attractions the trip was seeded with (booking never
        # touches attractions) — if /replan's remove_place edit survives, the
        # final stored envelope must be missing _ATT_REMOVE despite this.
        booked = copy.deepcopy(plan_envelope)
        booked["payment_status"] = "paid"
        booked["booking_ref"] = f"BK-{idempotency_key}"
        return {**booked, "outcome": "success", "booking_ref": f"BK-{idempotency_key}",
                "idempotency_key": idempotency_key, "payment_status": "paid"}


class TestReplanConfirmRace(unittest.TestCase):
    """HIGH-adjacent TOCTOU regression: a /replan racing a concurrent /confirm
    on the SAME idempotency_key must never lose its edit to /confirm's
    mark_booked write."""

    def test_concurrent_replan_during_confirm_is_not_lost(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed(store)
        orch = _SlowCommitOrch(commit_delay_s=0.4)
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch
            # Fresh per-idk lock maps for this app instance (mirrors _lifespan).
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}

            confirm_result: dict = {}

            def _do_confirm():
                confirm_result["r"] = client.post("/confirm", json={
                    "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                })

            t = threading.Thread(target=_do_confirm)
            t.start()
            # Deterministically wait until /confirm's commit_plan() is actually
            # running (mid the simulated slow commit, row still 'plan_ready')
            # before firing the concurrent /replan — the exact race window.
            self.assertTrue(
                orch.commit_started.wait(timeout=5),
                "confirm's commit_plan() never started",
            )
            replan_resp = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                "ops": _REMOVE_OP,
            })
            t.join(timeout=10)

        confirm_outcome = confirm_result["r"].json()
        replan_outcome = replan_resp.json()

        self.assertEqual(confirm_outcome.get("outcome"), "success")
        # /replan must have been serialized AFTER /confirm's commit — it sees
        # the trip already booked and edits it via the "BOOKED trips ARE
        # editable" path (never re-negotiates or touches money).
        self.assertEqual(replan_outcome.get("outcome"), "plan_ready")
        self.assertTrue(replan_outcome.get("applied"))

        row = store.get_plan(_PLAN_IDK)
        self.assertEqual(row["status"], "booked")
        self.assertEqual(row["booking_ref"], f"BK-{_PLAN_IDK}")

        day0_attractions = row["envelope"]["day_plans"][0]["days"][0]["attractions"]
        names = [a.get("name") for a in day0_attractions]
        self.assertIn(
            "Senso-ji Temple", names,
            "the kept attraction vanished — envelope was clobbered, not merged",
        )
        self.assertNotIn(
            "Ueno Park", names,
            "TOCTOU REGRESSION: /replan's remove_place edit was silently lost — "
            "a concurrent /confirm's mark_booked write clobbered it because "
            "/replan ran unlocked against a stale pre-booking row read.",
        )


class TestTripLockDictIsEvicted(unittest.TestCase):
    """LOW (advisory) regression: _state.trip_locks / trip_lock_refs must not
    keep an entry for an idempotency_key forever once nothing is using it —
    otherwise both dicts grow one entry per distinct idk for the life of the
    process."""

    def _fresh_client(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed(store)
        client = TestClient(server.build_app())
        return client, store

    def test_confirm_evicts_its_idk_lock_entry(self):
        client, store = self._fresh_client()
        with client:
            server._state.orch = _SlowCommitOrch(commit_delay_s=0.0)
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}
            r = client.post("/confirm", json={
                "idempotency_key": _PLAN_IDK, "owner_token": _OWNER})
            self.assertEqual(r.json().get("outcome"), "success")
            self.assertNotIn(_PLAN_IDK, server._state.trip_locks)
            self.assertNotIn(_PLAN_IDK, server._state.trip_lock_refs)

    def test_replan_evicts_its_idk_lock_entry(self):
        client, store = self._fresh_client()
        with client:
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}
            r = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                "ops": _REMOVE_OP,
            })
            self.assertEqual(r.json().get("outcome"), "plan_ready")
            self.assertNotIn(_PLAN_IDK, server._state.trip_locks)
            self.assertNotIn(_PLAN_IDK, server._state.trip_lock_refs)

    def test_many_distinct_idks_do_not_accumulate(self):
        """Simulates a long-lived server seeing many distinct trips over time
        — the dicts must stay bounded (roughly O(in-flight requests)), not
        grow O(total trips ever seen)."""
        client, store = self._fresh_client()
        with client:
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}
            for i in range(25):
                idk = f"trip-replan-lock-sweep-{i:03d}"
                store.save_plan({
                    "idempotency_key": idk, "user_id": "", "owner_token": _OWNER,
                    "checkout_id": f"co-{idk}", "dest_token": "JP",
                    "package_total_cents": 150000, "request": _STRUCTURED_REQUEST,
                    "envelope": {**copy.deepcopy(_ENVELOPE), "idempotency_key": idk},
                })
                r = client.post("/replan", json={
                    "idempotency_key": idk, "owner_token": _OWNER, "ops": _REMOVE_OP,
                })
                self.assertEqual(r.json().get("outcome"), "plan_ready")
            self.assertEqual(len(server._state.trip_locks), 0)
            self.assertEqual(len(server._state.trip_lock_refs), 0)


if __name__ == "__main__":
    unittest.main()
