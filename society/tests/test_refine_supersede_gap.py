"""test_refine_supersede_gap.py — refined-away parent plan must not remain
independently bookable (cross-generation double-booking gap).

CONTEXT (booking-integrity, live-confirmed): after a successful /refine, a NEW
row (new idempotency_key) is persisted but the PARENT row stayed
status='plan_ready' — nothing superseded, locked, or cancelled it. The
simulated wallet is keyed per-run by idempotency_key (orchestrator.py, budget.fund
is create-OR-RESET), so each generation got its OWN fully-funded wallet — the
merchant's insufficient_funds gate did NOT bound the combined total across
generations. The same owner (stale tab, or a replayed /confirm with the still-
valid owner_token) could confirm BOTH generations: two live bookings + two full
debits for one trip intent.

FIX: a successful /refine now stamps the parent row's `superseded_by` column
with the child idempotency_key (store.py's mark_superseded); /confirm refuses
to book any row with a non-empty superseded_by (server.py).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from orchestration import server
from orchestration.store import SqliteDashboardStore, set_store

_PARENT_IDK = "trip-parent-0001"
_CHILD_IDK = "trip-child-0002"
_OWNER = "owner-token-supersede-test"

_REQUEST = {
    "user_id": "", "total_budget_cents": 500000, "today": "2026-10-01",
    "legs": [{"city": "tokyo", "place_key": "tokyo", "checkin": "2026-10-15",
              "checkout": "2026-10-19", "nights": 4, "adults": 2,
              "interests": ["food"]}],
}
_PARENT_ENVELOPE = {
    "outcome": "plan_ready", "idempotency_key": _PARENT_IDK,
    "package_total_with_fees_cents": 500000,
    "legs": [{"city": "tokyo", "checkin": "2026-10-15", "checkout": "2026-10-19"}],
    "day_plans": [],
}


class _BookingCountingOrch:
    """Orchestrator stub for /refine + /confirm: negotiate() always returns a
    fresh child plan (_CHILD_IDK); commit_plan() records every idempotency_key
    it is asked to irreversibly book."""

    def __init__(self):
        self.committed_keys: list[str] = []

    def negotiate(self, req, *, commit=True):
        return {
            "_trip_request": req, "outcome": "plan_ready",
            "idempotency_key": _CHILD_IDK,
            "package_total_with_fees_cents": 425000,
            "legs": req.get("legs") or [], "day_plans": [],
        }

    def commit_plan(self, *, user_id, checkout_id, idempotency_key,
                    plan_envelope, dest_token, merchant_user_id=""):
        self.committed_keys.append(idempotency_key)
        return {"outcome": "success", "booking_ref": f"BK-{idempotency_key}",
                "idempotency_key": idempotency_key, "payment_status": "charged"}


class _SlowNegotiateOrch(_BookingCountingOrch):
    """Same as _BookingCountingOrch, but negotiate() sleeps briefly (simulating
    a real multi-second re-plan) and signals `negotiate_started` the instant it
    begins — used to deterministically open the TOCTOU race window between a
    concurrent /refine and /confirm on the SAME idempotency_key."""

    def __init__(self, *, negotiate_delay_s: float = 0.4):
        super().__init__()
        self.negotiate_started = threading.Event()
        self._negotiate_delay_s = negotiate_delay_s

    def negotiate(self, req, *, commit=True):
        self.negotiate_started.set()
        time.sleep(self._negotiate_delay_s)
        return super().negotiate(req, commit=commit)


class TestRefinedParentNotBookable(unittest.TestCase):

    def test_confirm_on_refined_away_parent_is_rejected(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        store.save_plan({
            "idempotency_key": _PARENT_IDK, "user_id": "",
            "owner_token": _OWNER, "package_total_cents": 500000,
            "checkout_id": "chk-parent-0001",
            "request": _REQUEST, "envelope": dict(_PARENT_ENVELOPE),
        })
        orch = _BookingCountingOrch()
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch  # AFTER __enter__ (lifespan resets it)

            with patch("utils.followup_parser.parse_followup", return_value={
                "ops": [{"op": "budget_adjust", "direction": "cheaper", "pct": 0.15}],
                "unsupported": False, "reason": None,
            }):
                r = client.post("/refine", json={
                    "idempotency_key": _PARENT_IDK, "message": "make it cheaper",
                    "owner_token": _OWNER,
                })
            self.assertEqual(r.json().get("outcome"), "plan_ready")
            self.assertEqual(r.json().get("idempotency_key"), _CHILD_IDK)

            r_child = client.post("/confirm", json={
                "idempotency_key": _CHILD_IDK, "owner_token": _OWNER})
            self.assertEqual(r_child.json().get("outcome"), "success")

            r_parent = client.post("/confirm", json={
                "idempotency_key": _PARENT_IDK, "owner_token": _OWNER})

            self.assertNotEqual(
                r_parent.json().get("outcome"), "success",
                "refined-away parent plan was booked AFTER its child was booked "
                "— two live bookings + two full wallet debits for one trip intent")
            self.assertEqual(
                orch.committed_keys, [_CHILD_IDK],
                f"expected exactly one irreversible commit, got {orch.committed_keys}")

    def test_parent_store_row_carries_superseded_by_after_refine(self):
        """Direct store-level check: the parent row's superseded_by column is
        stamped with the child's idempotency_key immediately after a successful
        /refine (not just observable indirectly via /confirm's rejection)."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        store.save_plan({
            "idempotency_key": _PARENT_IDK, "user_id": "",
            "owner_token": _OWNER, "package_total_cents": 500000,
            "request": _REQUEST, "envelope": dict(_PARENT_ENVELOPE),
        })
        orch = _BookingCountingOrch()
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch
            with patch("utils.followup_parser.parse_followup", return_value={
                "ops": [{"op": "budget_adjust", "direction": "cheaper", "pct": 0.15}],
                "unsupported": False, "reason": None,
            }):
                client.post("/refine", json={
                    "idempotency_key": _PARENT_IDK, "message": "make it cheaper",
                    "owner_token": _OWNER,
                })
            parent_row = store.get_plan(_PARENT_IDK)
            self.assertEqual(parent_row.get("superseded_by"), _CHILD_IDK)
            # Parent status itself stays plan_ready (never silently booked/
            # cancelled) — superseded_by is the sole signal /confirm checks.
            self.assertEqual(parent_row.get("status"), "plan_ready")

    def test_refining_an_already_superseded_parent_is_locked(self):
        """A second /refine attempt on an already-superseded parent must not be
        allowed to branch off a second sibling generation (would reopen the
        same double-book class via a different path)."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        store.save_plan({
            "idempotency_key": _PARENT_IDK, "user_id": "",
            "owner_token": _OWNER, "package_total_cents": 500000,
            "request": _REQUEST, "envelope": dict(_PARENT_ENVELOPE),
        })
        orch = _BookingCountingOrch()
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch
            with patch("utils.followup_parser.parse_followup", return_value={
                "ops": [{"op": "budget_adjust", "direction": "cheaper", "pct": 0.15}],
                "unsupported": False, "reason": None,
            }):
                first = client.post("/refine", json={
                    "idempotency_key": _PARENT_IDK, "message": "make it cheaper",
                    "owner_token": _OWNER,
                })
                self.assertEqual(first.json().get("outcome"), "plan_ready")

                second = client.post("/refine", json={
                    "idempotency_key": _PARENT_IDK, "message": "make it even cheaper",
                    "owner_token": _OWNER,
                })
            self.assertEqual(second.json().get("outcome"), "plan_locked")
            self.assertEqual(second.json().get("reason"), "superseded")


class TestConcurrentConfirmRefineRace(unittest.TestCase):
    """HIGH — TOCTOU race regression (findings at server.py's /refine and
    /confirm handlers): a /confirm racing a concurrent /refine on the SAME
    idempotency_key must never both succeed.

    Before the fix: /refine validated status/superseded_by on the event loop,
    then awaited a multi-second executor re-plan, and only AFTERWARDS persisted
    the child + stamped the parent's superseded_by. /confirm independently
    checked the same row and dispatched its commit with no re-check. A /confirm
    landing inside /refine's re-plan window would pass its (stale) superseded_by
    check, book the parent, and /refine would then persist an independently
    confirmable child on top of it — two live bookings + two full wallet debits
    for one trip intent.

    Fix: both handlers now hold a per-idempotency_key asyncio.Lock
    (server._get_trip_lock) across their entire read-validate-mutate sequence,
    making /confirm and /refine on the SAME key mutually exclusive.
    """

    def test_concurrent_confirm_during_refine_is_rejected_not_double_booked(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        store.save_plan({
            "idempotency_key": _PARENT_IDK, "user_id": "",
            "owner_token": _OWNER, "package_total_cents": 500000,
            "checkout_id": "chk-parent-0001",
            "request": _REQUEST, "envelope": dict(_PARENT_ENVELOPE),
        })
        orch = _SlowNegotiateOrch(negotiate_delay_s=0.4)
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch
            # Fresh per-idk lock map for this app instance (mirrors _lifespan).
            server._state.trip_locks = {}

            refine_result: dict = {}

            def _do_refine():
                with patch("utils.followup_parser.parse_followup", return_value={
                    "ops": [{"op": "budget_adjust", "direction": "cheaper", "pct": 0.15}],
                    "unsupported": False, "reason": None,
                }):
                    refine_result["r"] = client.post("/refine", json={
                        "idempotency_key": _PARENT_IDK, "message": "make it cheaper",
                        "owner_token": _OWNER,
                    })

            t = threading.Thread(target=_do_refine)
            t.start()
            # Deterministically wait until /refine's negotiate() is actually
            # running (mid the simulated slow re-plan) before firing the
            # concurrent /confirm — this is the exact race window the finding
            # describes.
            self.assertTrue(
                orch.negotiate_started.wait(timeout=5),
                "refine's negotiate() never started",
            )
            confirm_resp = client.post("/confirm", json={
                "idempotency_key": _PARENT_IDK, "owner_token": _OWNER})
            t.join(timeout=10)

        refine_outcome = refine_result["r"].json()
        confirm_outcome = confirm_resp.json()

        self.assertEqual(refine_outcome.get("outcome"), "plan_ready")
        self.assertEqual(refine_outcome.get("idempotency_key"), _CHILD_IDK)

        # The race-landing /confirm on the PARENT must be rejected (superseded
        # by the time it actually runs), never raced into a successful booking.
        self.assertNotEqual(
            confirm_outcome.get("outcome"), "success",
            f"TOCTOU REGRESSION: a /confirm racing a concurrent /refine booked "
            f"the superseded parent — got {confirm_outcome!r}",
        )
        # The parent must NEVER be irreversibly committed via this race.
        self.assertNotIn(
            _PARENT_IDK, orch.committed_keys,
            f"TOCTOU REGRESSION: parent idempotency_key was committed — "
            f"{orch.committed_keys!r}",
        )
        # At most one irreversible commit exists (only fires if the caller
        # separately confirms the child — this race alone must commit nothing).
        self.assertLessEqual(len(orch.committed_keys), 1)


class _RevertCapableOrch:
    """H2 fix regression — a fake orchestrator whose negotiate() simulates a
    REAL _request_digest across a two-step refine chain: the FIRST re-plan
    mints a genuinely NEW key (forward_key); the SECOND re-plan (reverting to
    the FIRST generation's exact original parameters) re-mints THAT
    generation's deterministic idempotency_key (revert_key) — exactly what
    the real orchestrator.negotiate()'s digest-based idempotency_key does
    when the refined content genuinely reverts to an earlier generation's
    parameters (server.py:2600's `new_key = result.get("idempotency_key")`).
    """

    def __init__(self, *, forward_key: str, revert_key: str,
                 forward_total: int, revert_total: int):
        self.committed_keys: list[str] = []
        self._forward_key = forward_key
        self._revert_key = revert_key
        self._forward_total = forward_total
        self._revert_total = revert_total
        self._calls = 0

    def negotiate(self, req, *, commit=True):
        self._calls += 1
        key, total = (
            (self._forward_key, self._forward_total) if self._calls == 1
            else (self._revert_key, self._revert_total)
        )
        return {
            "_trip_request": req, "outcome": "plan_ready",
            "idempotency_key": key,
            "package_total_with_fees_cents": total,
            "legs": req.get("legs") or [], "day_plans": [],
        }

    def commit_plan(self, *, user_id, checkout_id, idempotency_key,
                    plan_envelope, dest_token, merchant_user_id=""):
        self.committed_keys.append(idempotency_key)
        return {"outcome": "success", "booking_ref": f"BK-{idempotency_key}",
                "idempotency_key": idempotency_key, "payment_status": "charged"}


class TestRevertToEarlierGenerationSupersedeCycle(unittest.TestCase):
    """H2 HIGH regression: /refine reverting to an earlier generation's EXACT
    parameters re-mints that generation's original idempotency_key (a
    deterministic digest) — this must NOT create a mutual superseded_by cycle
    (parent -> child AND child -> parent simultaneously), which would make
    BOTH generations permanently unconfirmable.

    Repro (mirrors the finding's empirical reproduction): negotiate "3 days
    bali $1000" -> key K0 (_PARENT_IDK). /refine "budget to $1200" -> plan_ready
    K1 (_CHILD_IDK), K0.superseded_by=K1. /refine on K1 back to "$1000" ->
    plan_ready with key==K0 again (the digest matches the original exactly).
    Before the fix: K0.superseded_by stayed K1 (save_plan's upsert never
    touches superseded_by) AND K1.superseded_by got stamped K0 — a MUTUAL
    cycle; /confirm on EITHER key refused with plan_superseded pointing at
    the other, forever.
    """

    def test_revert_to_parent_clears_stale_pointer_no_mutual_cycle(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        store.save_plan({
            "idempotency_key": _PARENT_IDK, "user_id": "",
            "owner_token": _OWNER, "package_total_cents": 500000,
            "checkout_id": "chk-parent-revert-0001",
            "request": _REQUEST, "envelope": dict(_PARENT_ENVELOPE),
        })
        orch = _RevertCapableOrch(
            forward_key=_CHILD_IDK, revert_key=_PARENT_IDK,
            forward_total=425000, revert_total=500000,
        )
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch

            # Step 1: refine the parent -> a genuinely NEW child key (K1).
            with patch("utils.followup_parser.parse_followup", return_value={
                "ops": [{"op": "budget_adjust", "direction": "higher", "pct": 0.2}],
                "unsupported": False, "reason": None,
            }):
                first = client.post("/refine", json={
                    "idempotency_key": _PARENT_IDK, "message": "raise budget to $1200",
                    "owner_token": _OWNER,
                })
            self.assertEqual(first.json().get("outcome"), "plan_ready")
            self.assertEqual(first.json().get("idempotency_key"), _CHILD_IDK)
            self.assertEqual(
                store.get_plan(_PARENT_IDK).get("superseded_by"), _CHILD_IDK)

            # Step 2: refine the child back down — the digest reverts to the
            # PARENT's original key (K0) again.
            with patch("utils.followup_parser.parse_followup", return_value={
                "ops": [{"op": "budget_adjust", "direction": "cheaper", "pct": 0.167}],
                "unsupported": False, "reason": None,
            }):
                second = client.post("/refine", json={
                    "idempotency_key": _CHILD_IDK, "message": "back to $1000",
                    "owner_token": _OWNER,
                })
            self.assertEqual(second.json().get("outcome"), "plan_ready")
            self.assertEqual(second.json().get("idempotency_key"), _PARENT_IDK)

            parent_row = store.get_plan(_PARENT_IDK)
            child_row = store.get_plan(_CHILD_IDK)

            # H2 regression: no mutual cycle — the revived parent must NOT
            # still point at the child.
            self.assertEqual(
                parent_row.get("superseded_by"), "",
                f"H2 regression: parent row still carries a STALE "
                f"superseded_by={parent_row.get('superseded_by')!r} after "
                f"reclaiming the current-generation slot — mutual cycle with "
                f"the child.",
            )
            # The child (now the OLD generation) correctly points at the
            # revived parent.
            self.assertEqual(child_row.get("superseded_by"), _PARENT_IDK)

            # The revived parent must be genuinely /confirm-able again.
            confirm_parent = client.post("/confirm", json={
                "idempotency_key": _PARENT_IDK, "owner_token": _OWNER})
            self.assertEqual(
                confirm_parent.json().get("outcome"), "success",
                f"H2 regression: the revived (current) generation could not "
                f"be confirmed: {confirm_parent.json()!r}",
            )
            self.assertEqual(orch.committed_keys, [_PARENT_IDK])

            # The old child generation correctly refuses to book (points at
            # the now-current parent) — never a silent no-op, never a crash.
            confirm_child = client.post("/confirm", json={
                "idempotency_key": _CHILD_IDK, "owner_token": _OWNER})
            self.assertEqual(confirm_child.json().get("outcome"), "cannot_satisfy")
            self.assertEqual(confirm_child.json().get("reason"), "plan_superseded")
            self.assertEqual(confirm_child.json().get("superseded_by"), _PARENT_IDK)


if __name__ == "__main__":
    unittest.main()
