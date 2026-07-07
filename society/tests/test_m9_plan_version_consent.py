"""test_m9_plan_version_consent.py — M9 regression: /replan mutating a held
(or booked) plan envelope under the same consent key with no re-consent.

CONTEXT (M9, medium): /confirm always re-reads the row's CURRENT envelope
before committing, so the server-side booked content is never stale — but a
same-owner /replan (a second tab's drag-drop edit) can change that content
under the SAME idempotency_key with NO re-consent stamp. A tab still showing
the OLD (pre-edit) content that clicks Confirm & Book silently books the NEW
content instead, with no signal anything changed. NOTE: 6bd40d2's per-idk
lock closes the narrow INTERLEAVED-race sub-case (a /replan racing mid a
/confirm's commit — see test_replan_lock_toctou.py's TestReplanConfirmRace),
but does NOT close this file's SEQUENTIAL scenario (replan fully completes,
THEN confirm runs) — the lock only serializes concurrent access, it does not
detect or refuse a legitimately-completed prior edit.

FIX: an OPT-IN `plan_version` content hash (over legs + day_plans only, not
money/booking fields) is stamped onto every plan_ready envelope
(_persist_and_sanitize_plan) and refreshed by /replan whenever it actually
applies an edit. /confirm accepts an optional `plan_version` in its body; if
supplied and it no longer matches the row's CURRENT envelope, /confirm
refuses with outcome="needs_reconsent" instead of silently booking the
changed content. Callers that never send `plan_version` are unaffected
(var-0 — this is why test 1 below asserts the pre-existing behavior is
unchanged for the no-plan_version case).

ALSO CLOSES L6 (low): "HITL consent is transmitted as a bare buyer_consent
boolean, not a verifiable artifact bound to the cart — the plan-binding
mandate exists only on the autonomous (L3) path." The AP2 mandate the Go
merchant verifies for L3 binds checkout_id/total/freshness/device key to the
cart, but the human L2 path (/confirm) only ever sent a plain `True` into
complete_checkout, with nothing server-side proving the human's consent was
actually given against the CURRENT plan content. `plan_version` is exactly
that missing artifact for the L2 path: a content-derived hash a client
captures when the review screen is rendered and must echo back for /confirm
to proceed — see TestL6ConsentArtifactBoundToCart below for the finding-
specific framing (same code path as M9, verified from the "consent must be
tied to a specific cart" angle rather than the "/replan raced /confirm"
angle).
"""
from __future__ import annotations

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from orchestration import server
from orchestration.store import SqliteDashboardStore, set_store

_PLAN_IDK = "trip-m9-plan-version-0001"
_OWNER = "owner-token-m9-plan-version"

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


class _MockCommitOrch:
    """commit_plan always succeeds — tracks calls so a test can assert it was
    (or was NOT) invoked."""

    def __init__(self):
        self.commit_plan_calls: list[str] = []

    def commit_plan(self, *, user_id, checkout_id, idempotency_key,
                    plan_envelope, dest_token, merchant_user_id=""):
        self.commit_plan_calls.append(idempotency_key)
        booked = copy.deepcopy(plan_envelope)
        booked["payment_status"] = "paid"
        booked["booking_ref"] = f"BK-{idempotency_key}"
        return {**booked, "outcome": "success", "booking_ref": f"BK-{idempotency_key}",
                "idempotency_key": idempotency_key, "payment_status": "paid"}


def _seed(store: SqliteDashboardStore) -> dict:
    store.save_plan({
        "idempotency_key": _PLAN_IDK, "user_id": "", "owner_token": _OWNER,
        "checkout_id": "co-m9-plan-version-001", "dest_token": "JP",
        "package_total_cents": 150000, "request": _STRUCTURED_REQUEST,
        "envelope": copy.deepcopy(_ENVELOPE),
    })
    return store.get_plan(_PLAN_IDK)


class TestConfirmWithoutPlanVersionIsUnaffected(unittest.TestCase):
    """var-0: a caller that never sends plan_version sees byte-identical
    behavior to before this fix — /confirm proceeds to commit normally even
    though the row carries no plan_version of its own yet (legacy/older row)."""

    def test_confirm_no_plan_version_field_still_books(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed(store)
        orch = _MockCommitOrch()
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}
            r = client.post("/confirm", json={
                "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
            })
        self.assertEqual(r.json().get("outcome"), "success", r.text)
        self.assertEqual(orch.commit_plan_calls, [_PLAN_IDK])


class TestReplanThenConfirmStalePlanVersionRejected(unittest.TestCase):
    """M9 core regression: a /replan that changes content under idk, followed
    (sequentially, no race) by a /confirm carrying the STALE plan_version
    captured before the edit, must be refused — NOT silently book the new
    content under the old consent."""

    def test_stale_plan_version_blocks_confirm_no_booking(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        row = _seed(store)
        stale_version = row["envelope"].get("plan_version", "")
        # A freshly-seeded row (built directly via store.save_plan, bypassing
        # _persist_and_sanitize_plan) has NO plan_version yet in this fixture —
        # compute what the client WOULD have captured had it gone through the
        # normal /negotiate → plan_ready path, by calling the same hash used
        # server-side.
        from orchestration.server import _plan_content_hash
        stale_version = _plan_content_hash(row["envelope"])
        self.assertTrue(stale_version)

        orch = _MockCommitOrch()
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}

            # A "second tab" edits the plan.
            replan_resp = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                "ops": _REMOVE_OP,
            })
            self.assertEqual(replan_resp.json().get("outcome"), "plan_ready")
            self.assertTrue(replan_resp.json().get("applied"))

            # The FIRST tab, still holding the pre-edit plan_version, clicks
            # Confirm & Book.
            confirm_resp = client.post("/confirm", json={
                "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                "plan_version": stale_version,
            })

        body = confirm_resp.json()
        self.assertEqual(body.get("outcome"), "needs_reconsent", body)
        self.assertEqual(body.get("reason"), "plan_content_changed", body)
        self.assertIn("current_plan_version", body)
        self.assertNotEqual(body["current_plan_version"], stale_version)
        # Must NOT have committed anything.
        self.assertEqual(orch.commit_plan_calls, [])
        row_after = store.get_plan(_PLAN_IDK)
        self.assertEqual(row_after["status"], "plan_ready", "must not book on stale plan_version")


class TestConfirmWithCurrentPlanVersionSucceeds(unittest.TestCase):
    """Positive case: /confirm with the CURRENT (post-edit) plan_version
    proceeds to commit normally — the guard only blocks an actual mismatch."""

    def test_current_plan_version_allows_confirm(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed(store)
        orch = _MockCommitOrch()
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}

            replan_resp = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                "ops": _REMOVE_OP,
            })
            current_version = replan_resp.json()["plan"]["plan_version"]
            self.assertTrue(current_version)

            confirm_resp = client.post("/confirm", json={
                "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                "plan_version": current_version,
            })
        self.assertEqual(confirm_resp.json().get("outcome"), "success", confirm_resp.text)
        self.assertEqual(orch.commit_plan_calls, [_PLAN_IDK])


class TestL6ConsentArtifactBoundToCart(unittest.TestCase):
    """L6 framing: buyer_consent alone (no plan_version) is a bare boolean with
    no cart-binding — that legacy behavior must still work (var-0/back-compat,
    since real clients aren't all upgraded at once). But a client that DOES
    mint and echo a plan_version now gets a genuine cart-bound consent check:
    consenting to cart A does not let a since-changed cart B silently commit
    under that same consent."""

    def test_legacy_buyer_consent_only_no_cart_binding_still_backcompat(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        _seed(store)
        orch = _MockCommitOrch()
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}
            # No plan_version supplied — bare-boolean consent path, exactly as
            # before this fix. Must still succeed (var-0 for un-upgraded callers).
            r = client.post("/confirm", json={
                "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
            })
        self.assertEqual(r.json().get("outcome"), "success", r.text)

    def test_consent_to_cart_a_does_not_authorize_committing_cart_b(self):
        """The core L6 property: once a client mints plan_version for cart A
        (what it reviewed) and cart B is what's now stored (post-edit), that
        consent artifact must NOT authorize committing cart B."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        row = _seed(store)
        from orchestration.server import _plan_content_hash
        consent_for_cart_a = _plan_content_hash(row["envelope"])

        orch = _MockCommitOrch()
        client = TestClient(server.build_app())
        with client:
            server._state.orch = orch
            server._state.trip_locks = {}
            server._state.trip_lock_refs = {}

            # Cart mutates to "cart B" (a genuine content edit).
            replan_resp = client.post("/replan", json={
                "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                "ops": _REMOVE_OP,
            })
            self.assertTrue(replan_resp.json().get("applied"))

            # Attempt to commit cart B using the consent artifact minted for
            # cart A.
            confirm_resp = client.post("/confirm", json={
                "idempotency_key": _PLAN_IDK, "owner_token": _OWNER,
                "plan_version": consent_for_cart_a,
            })

        self.assertEqual(confirm_resp.json().get("outcome"), "needs_reconsent")
        self.assertEqual(orch.commit_plan_calls, [], (
            "L6 REGRESSION: consent minted for one cart's content authorized "
            "committing a DIFFERENT cart's content under the same key."
        ))


if __name__ == "__main__":
    unittest.main()
