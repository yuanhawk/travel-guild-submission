"""test_cancel_trips_endpoints.py — POST /cancel + GET /trips + GET /trips/{key}.

Tests:
1.  test_cancel_booked_credits_wallet (cancel a booked trip → refund in response)
2.  test_cancel_unknown_plan → invalid_request
3.  test_cancel_plan_ready_not_booked → not_booked
4.  test_double_cancel_already_cancelled_no_double_credit (idempotent)
5.  test_trips_list_by_user_ordered_desc
6.  test_trips_list_empty_user
7.  test_trips_list_requires_user_id_400
8.  test_trips_detail_roundtrip (GET /trips/{key} returns the sanitised envelope)
9.  test_trips_detail_unknown_404
10. test_trips_detail_never_leaks_confirm_ctx_or_checkout_id

SECURITY (IDOR fix, was STORE-002 CVSS 9.1 CRITICAL): /cancel now REQUIRES the
two-tier session_token/owner_token ownership proof (see server.py's
_authorize_trip_action). TestCancelOwnership below is the regression lock —
mirrors test_prefs_session.py's `_login` + wrong-user pattern, but the gate
returns 403 `not_trip_owner` (not 401 — see server.py's `_forbidden`).

SECURITY (#155, read-IDOR follow-up): GET /trips/{key} now REQUIRES the SAME
two-tier ownership proof, threaded as query params (session_token / owner_token)
since this is a GET. Unlike /cancel, a DENIED read renders as 404 `not_found`
(not 403 `not_trip_owner`) — see server.py's trips_detail docstring for the
existence-oracle reasoning. TestTripsDetailOwnership below is the regression
lock, including the existence-oracle assertion that a denied-read 404 is
byte-identical to an unknown-key 404.

MIGRATION NOTE: the pre-fix fixtures below seeded trips under `_USER_ID =
"demo-cancel-user"`, a user_id that was never a real pre-seeded demo user. Once
the ownership gate landed, that user_id became un-actionable (POST /session/login
404s for an unknown user_id, so NO session_token could ever be minted for it —
every /cancel against a "demo-cancel-user" trip would 403 unconditionally,
breaking test_cancel_booked_credits_wallet, test_cancel_plan_ready_not_booked,
and test_double_cancel_already_cancelled_no_double_credit). Fixed by re-seeding
under a REAL demo user (`demo-mei`) and threading a real session_token via the
new `_login` helper into every /cancel call those three tests make.

MIGRATION NOTE (#155): test_trips_detail_roundtrip and
test_trips_detail_never_leaks_confirm_ctx_or_checkout_id previously seeded
their rows under non-real user_ids ("u-1", "u-safe") and read with no
identity at all — both now 404 under the #155 ownership gate (Tier 1: no
session_token can ever be minted for a non-seeded user_id). Fixed the same way
as the /cancel migration above: seeded under the real demo user `demo-mei` and
threaded a `session_token` query param via `_login`.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from orchestration import server
from orchestration.store import SqliteDashboardStore, set_store

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_PLAN_IDK = "trip-cancel-test-0001"
_BOOKED_IDK = "trip-cancel-test-booked-0001"
# MIGRATED (IDOR fix): was "demo-cancel-user", a non-seeded user_id that no
# session_token can ever be minted for (POST /session/login 404s) — see the
# module docstring's MIGRATION NOTE. Must be a REAL pre-seeded demo user.
_USER_ID = "demo-mei"

_PLAN_ENVELOPE = {
    "outcome": "plan_ready",
    "idempotency_key": _PLAN_IDK,
    "payment_status": "held",
    "booking_ref": None,
    "package_total_with_fees_cents": 75000,
    "wallet": {"debited": False, "held_cents": 75000},
    "legs": [{"city": "tokyo"}],
    "day_plans": [],
}

_BOOKED_ENVELOPE = {
    "outcome": "success",
    "idempotency_key": _BOOKED_IDK,
    "payment_status": "charged",
    "booking_ref": "BK-TEST-9001",
    "package_total_with_fees_cents": 75000,
    "wallet": {"debited": True, "debit_cents": 75000},
    "legs": [{"city": "tokyo"}],
    "day_plans": [],
}


def _seed_stores(store: SqliteDashboardStore) -> None:
    """Seed both a plan_ready row and a booked row."""
    store.save_plan({
        "idempotency_key": _PLAN_IDK,
        "user_id": _USER_ID,
        "checkout_id": "co-plan-001",
        "dest_token": "JP",
        "package_total_cents": 75000,
        "envelope": copy.deepcopy(_PLAN_ENVELOPE),
    })
    store.save_plan({
        "idempotency_key": _BOOKED_IDK,
        "user_id": _USER_ID,
        "checkout_id": "co-booked-001",
        "dest_token": "JP",
        "package_total_cents": 75000,
        "envelope": copy.deepcopy(_BOOKED_ENVELOPE),
    })
    store.mark_booked(
        _BOOKED_IDK, booking_ref="BK-TEST-9001",
        envelope=copy.deepcopy(_BOOKED_ENVELOPE),
        confirmed_at="2026-10-01T00:00:00+00:00",
    )


def _client_with_seeded_store():
    store = SqliteDashboardStore(":memory:")
    set_store(store)
    _seed_stores(store)
    client = TestClient(server.build_app())
    return client, store


def _login(c, user_id: str) -> str:
    """POST /session/login for a KNOWN demo user_id -> its session_token.
    Mirrors test_prefs_session.py's `_login` helper — the genuine session-
    possession proof required by the ownership gate on a logged-in trip."""
    r = c.post("/session/login", json={"user_id": user_id})
    assert r.status_code == 200, r.text
    return r.json()["session_token"]


# ---------------------------------------------------------------------------
# Mock local merchant for cancel tests.
# The cancel endpoint calls _state.local_merchant._cancel_checkout.
# In test mode, we inject a fake merchant via monkeypatching.
# ---------------------------------------------------------------------------

class _MockMerchant:
    """Fake in-process merchant that tracks _cancel_checkout calls."""
    def __init__(self):
        self.cancel_calls: list[dict] = []
        self._wallet_balance = 500000  # cents

    def _cancel_checkout(self, args: dict) -> dict:
        co_id = (args.get("checkout") or {}).get("id", "")
        self.cancel_calls.append({"checkout_id": co_id})
        self._wallet_balance += 75000  # simulate credit
        return {
            "id": co_id, "status": "cancelled",
            "wallet_credit_cents": 75000,
            "wallet_balance_cents": self._wallet_balance,
            "simulated": True,
        }


def _inject_mock_merchant(mock_m: _MockMerchant) -> None:
    """Inject a mock merchant into _state after app startup."""
    server._state.local_merchant = mock_m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCancelEndpoint(unittest.TestCase):

    # ---- 1. Cancel a booked trip → credits wallet ----
    def test_cancel_booked_credits_wallet(self):
        client, store = _client_with_seeded_store()
        mock_m = _MockMerchant()
        with client:
            _inject_mock_merchant(mock_m)
            token = _login(client, _USER_ID)  # IDOR fix: ownership proof required
            r = client.post("/cancel", json={
                "idempotency_key": _BOOKED_IDK,
                "user_id": _USER_ID,
                "session_token": token,
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "cancelled")
        self.assertEqual(body["idempotency_key"], _BOOKED_IDK)
        self.assertEqual(body["refunded_cents"], 75000)
        self.assertTrue(body["simulated"])
        # Store must be marked cancelled.
        row = store.get_plan(_BOOKED_IDK)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["refunded_cents"], 75000)

    # ---- 2. Cancel unknown plan → invalid_request ----
    def test_cancel_unknown_plan(self):
        client, store = _client_with_seeded_store()
        with client:
            r = client.post("/cancel", json={"idempotency_key": "trip-does-not-exist"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "invalid_request")
        self.assertEqual(body["reason"], "unknown_plan")

    # ---- 3. Cancel a plan_ready (not yet booked) → not_booked ----
    def test_cancel_plan_ready_not_booked(self):
        client, store = _client_with_seeded_store()
        with client:
            token = _login(client, _USER_ID)  # IDOR fix: ownership proof required
            r = client.post("/cancel", json={"idempotency_key": _PLAN_IDK,
                                             "user_id": _USER_ID, "session_token": token})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "invalid_request")
        self.assertEqual(body["reason"], "not_booked")

    # ---- 4. Double cancel → already_cancelled, no double credit ----
    def test_double_cancel_already_cancelled_no_double_credit(self):
        client, store = _client_with_seeded_store()
        mock_m = _MockMerchant()
        with client:
            _inject_mock_merchant(mock_m)
            token = _login(client, _USER_ID)  # IDOR fix: ownership proof required
            body = {"idempotency_key": _BOOKED_IDK, "user_id": _USER_ID, "session_token": token}
            r1 = client.post("/cancel", json=body)
            r2 = client.post("/cancel", json=body)
        self.assertEqual(r1.json()["outcome"], "cancelled")
        self.assertEqual(r2.json()["outcome"], "already_cancelled")
        # The mock merchant must have been called only ONCE (for the first cancel).
        self.assertEqual(len(mock_m.cancel_calls), 1, "Merchant cancel called twice — double refund!")

    # ---- 5. Missing idempotency_key → 400 ----
    def test_cancel_missing_idk_400(self):
        client, _ = _client_with_seeded_store()
        with client:
            r = client.post("/cancel", json={"user_id": _USER_ID})
        self.assertEqual(r.status_code, 400)


class TestTripsEndpoints(unittest.TestCase):

    # ---- 5. Trips list by user, ordered desc ----
    # MIGRATED (read-IDOR fix, Group D): GET /trips now REQUIRES a session_token
    # proving possession of a live session for user_id (see server.py trips_list's
    # SECURITY docstring) — threaded via `_login`, same pattern the rest of this
    # file already uses for /cancel and /trips/{key}.
    def test_trips_list_by_user_ordered_desc(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        # Seed 2 trips for the same user.
        store.save_plan({
            "idempotency_key": "trip-a",
            "user_id": _USER_ID,
            "package_total_cents": 50000,
            "envelope": {"outcome": "plan_ready"},
            "now": "2026-10-01T00:00:00+00:00",
        })
        store.save_plan({
            "idempotency_key": "trip-b",
            "user_id": _USER_ID,
            "package_total_cents": 80000,
            "envelope": {"outcome": "plan_ready"},
            "now": "2026-10-02T00:00:00+00:00",
        })
        client = TestClient(server.build_app())
        with client:
            token = _login(client, _USER_ID)  # IDOR fix: session proof required
            r = client.get(f"/trips?user_id={_USER_ID}", headers={"X-Session-Token": token})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        trips = body.get("trips") or []
        self.assertEqual(len(trips), 2)
        # trip-b (later) should come first (desc order).
        keys = [t["idempotency_key"] for t in trips]
        self.assertEqual(keys[0], "trip-b")
        self.assertEqual(keys[1], "trip-a")

    # ---- 6. Real user, no trips → empty list (not an error) ----
    # MIGRATED (read-IDOR fix): was seeded under a non-real "no-such-user-ever"
    # user_id with no session_token at all — that user_id can never mint a
    # session_token (POST /session/login 404s for an unknown user), so it can no
    # longer reach the empty-list branch at all. Re-pointed at a REAL demo user
    # (demo-alex) with zero seeded trips, authenticated via `_login`, to keep
    # testing the actual "empty list" behaviour rather than the auth gate
    # (which TestTripsListOwnership below covers explicitly).
    def test_trips_list_empty_user(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        client = TestClient(server.build_app())
        with client:
            token = _login(client, "demo-alex")
            r = client.get("/trips?user_id=demo-alex", headers={"X-Session-Token": token})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("trips"), [])

    # ---- 7. Missing user_id → 400 ----
    def test_trips_list_requires_user_id_400(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        client = TestClient(server.build_app())
        with client:
            r = client.get("/trips")
        self.assertEqual(r.status_code, 400)

    # ---- 8. GET /trips/{key} → sanitised envelope ----
    def test_trips_detail_roundtrip(self):
        # MIGRATED (#155): seeded under a real demo user + threaded session_token
        # query param — see module docstring's MIGRATION NOTE (#155).
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        env = {
            "outcome": "plan_ready",
            "idempotency_key": "trip-detail-01",
            "package_total_with_fees_cents": 90000,
            "legs": [{"city": "bali"}],
        }
        store.save_plan({
            "idempotency_key": "trip-detail-01",
            "user_id": "demo-mei",
            "envelope": env,
        })
        client = TestClient(server.build_app())
        with client:
            token = _login(client, "demo-mei")
            r = client.get("/trips/trip-detail-01", headers={"X-Session-Token": token})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("idempotency_key"), "trip-detail-01")
        self.assertEqual(body.get("outcome"), "plan_ready")
        self.assertEqual(body.get("package_total_with_fees_cents"), 90000)

    # ---- 9. Unknown key → 404 ----
    def test_trips_detail_unknown_404(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        client = TestClient(server.build_app())
        with client:
            r = client.get("/trips/trip-does-not-exist")
        self.assertEqual(r.status_code, 404)

    # ---- 10. /trips/{key} never leaks _confirm_ctx or checkout_id ----
    def test_trips_detail_never_leaks_confirm_ctx_or_checkout_id(self):
        """_confirm_ctx and checkout_id are stripped at save time by
        _persist_and_sanitize_plan. The /trips/{key} detail must echo
        only the already-sanitised envelope.

        MIGRATED (#155): seeded under a real demo user + threaded session_token
        query param — see module docstring's MIGRATION NOTE (#155)."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        # Save a plan with confirm_ctx still in the envelope (shouldn't happen
        # normally, but defence-in-depth check).
        env_with_ctx = {
            "outcome": "plan_ready",
            "idempotency_key": "trip-ctx-test",
            "_confirm_ctx": {"checkout_id": "co-secret-9999", "secret": "supersecret"},
            "checkout_id": "co-secret-9999",
            "package_total_with_fees_cents": 10000,
        }
        store.save_plan({
            "idempotency_key": "trip-ctx-test",
            "user_id": "demo-mei",
            "checkout_id": "co-secret-9999",
            "envelope": env_with_ctx,
        })
        client = TestClient(server.build_app())
        with client:
            token = _login(client, "demo-mei")
            r = client.get("/trips/trip-ctx-test", headers={"X-Session-Token": token})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        raw = r.text
        # Neither confirm_ctx nor checkout_id (the merchant handle) in the response.
        self.assertNotIn("_confirm_ctx", body)
        self.assertNotIn("co-secret-9999", raw)
        self.assertNotIn("checkout_id", body)


class TestCancelOwnership(unittest.TestCase):
    """SECURITY regression: /cancel ownership gate (was STORE-002, CVSS 9.1
    CRITICAL — IDOR). Mirrors test_prefs_session.py's `_login` + wrong-user
    pattern, but the gate returns 403 `not_trip_owner` (server.py's
    `_forbidden` — NOT 401, so the FE can render an honest refusal; see
    server.py's `_authorize_trip_action` docstring for the full two-tier
    session_token/owner_token model)."""

    _OWNER_IDK = "trip-cancel-own-001"       # logged-in owner (demo-mei), booked
    _ANON_IDK = "trip-cancel-anon-001"       # anonymous, booked, owner_token-bound
    _ANON_TOKEN = "anon-secret-cancel-A"

    def _seed(self, store: SqliteDashboardStore) -> None:
        # Tier 1: a real logged-in demo user's booked trip.
        owner_env = {**copy.deepcopy(_BOOKED_ENVELOPE), "idempotency_key": self._OWNER_IDK}
        store.save_plan({
            "idempotency_key": self._OWNER_IDK, "user_id": "demo-mei",
            "checkout_id": "co-cancel-own-001", "dest_token": "JP",
            "package_total_cents": 75000, "envelope": owner_env,
        })
        store.mark_booked(self._OWNER_IDK, booking_ref="BK-OWN-001",
                          envelope=owner_env, confirmed_at="2026-10-01T00:00:00+00:00")
        # Tier 2: an anonymous booked trip, bound to owner_token at creation.
        anon_env = {**copy.deepcopy(_BOOKED_ENVELOPE), "idempotency_key": self._ANON_IDK}
        store.save_plan({
            "idempotency_key": self._ANON_IDK, "user_id": "",
            "checkout_id": "co-cancel-anon-001", "dest_token": "JP",
            "package_total_cents": 75000, "envelope": anon_env,
            "owner_token": self._ANON_TOKEN,
        })
        store.mark_booked(self._ANON_IDK, booking_ref="BK-ANON-001",
                          envelope=anon_env, confirmed_at="2026-10-01T00:00:00+00:00")

    def _client(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        self._seed(store)
        return TestClient(server.build_app()), store

    # ---- 1. Logged-in owner + valid session_token -> 200, proceeds ----
    def test_owner_with_valid_session_token_succeeds(self):
        client, store = self._client()
        mock_m = _MockMerchant()
        with client:
            _inject_mock_merchant(mock_m)
            token = _login(client, "demo-mei")
            r = client.post("/cancel", json={"idempotency_key": self._OWNER_IDK,
                                             "session_token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["outcome"], "cancelled")
        self.assertEqual(store.get_plan(self._OWNER_IDK)["status"], "cancelled")

    # ---- 2. Logged-in trip, no session_token -> 403; no state change ----
    def test_owner_trip_no_session_token_403_no_state_change(self):
        client, store = self._client()
        mock_m = _MockMerchant()
        with client:
            _inject_mock_merchant(mock_m)
            r = client.post("/cancel", json={"idempotency_key": self._OWNER_IDK})
        self.assertEqual(r.status_code, 403)
        # #203: Tier 1 (logged-in trip) denials return `session_invalid`.
        self.assertEqual(r.json().get("reason"), "session_invalid")
        self.assertEqual(len(mock_m.cancel_calls), 0)
        self.assertEqual(store.get_plan(self._OWNER_IDK)["status"], "booked")

    # ---- 3. session_token minted for a DIFFERENT demo user -> 403 ----
    def test_owner_trip_wrong_user_session_token_403(self):
        client, store = self._client()
        mock_m = _MockMerchant()
        with client:
            _inject_mock_merchant(mock_m)
            other_token = _login(client, "demo-alex")   # a real user, but NOT the trip owner
            r = client.post("/cancel", json={"idempotency_key": self._OWNER_IDK,
                                             "session_token": other_token})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json().get("reason"), "session_invalid")  # #203: Tier 1
        self.assertEqual(len(mock_m.cancel_calls), 0)
        self.assertEqual(store.get_plan(self._OWNER_IDK)["status"], "booked")

    # ---- 4. Anon trip + correct owner_token -> proceeds ----
    def test_anon_trip_correct_owner_token_succeeds(self):
        client, store = self._client()
        mock_m = _MockMerchant()
        with client:
            _inject_mock_merchant(mock_m)
            r = client.post("/cancel", json={"idempotency_key": self._ANON_IDK,
                                             "owner_token": self._ANON_TOKEN})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["outcome"], "cancelled")
        self.assertEqual(store.get_plan(self._ANON_IDK)["status"], "cancelled")

    # ---- 5. Anon trip, wrong/missing owner_token -> 403; no state change ----
    def test_anon_trip_wrong_or_missing_owner_token_403_no_state_change(self):
        client, store = self._client()
        mock_m = _MockMerchant()
        with client:
            _inject_mock_merchant(mock_m)
            r_wrong = client.post("/cancel", json={"idempotency_key": self._ANON_IDK,
                                                    "owner_token": "not-the-right-secret"})
            r_missing = client.post("/cancel", json={"idempotency_key": self._ANON_IDK})
        self.assertEqual(r_wrong.status_code, 403)
        self.assertEqual(r_wrong.json().get("reason"), "not_trip_owner")
        self.assertEqual(r_missing.status_code, 403)
        self.assertEqual(r_missing.json().get("reason"), "not_trip_owner")
        self.assertEqual(len(mock_m.cancel_calls), 0)
        self.assertEqual(store.get_plan(self._ANON_IDK)["status"], "booked")

    # ---- 6. Cross-class: anon-only caller vs logged-in trip, and
    #         logged-in-only caller vs anon trip (no owner_token) -> both 403 ----
    def test_cross_class_callers_403(self):
        client, store = self._client()
        mock_m = _MockMerchant()
        with client:
            _inject_mock_merchant(mock_m)
            # Anon caller (only an owner_token, no session) against the LOGGED-IN trip.
            r1 = client.post("/cancel", json={"idempotency_key": self._OWNER_IDK,
                                              "owner_token": "some-random-anon-secret"})
            # Logged-in caller (valid session_token for a real user) against the
            # ANON trip, with no owner_token supplied.
            token = _login(client, "demo-mei")
            r2 = client.post("/cancel", json={"idempotency_key": self._ANON_IDK,
                                              "session_token": token})
        self.assertEqual(r1.status_code, 403)
        self.assertEqual(r1.json().get("reason"), "session_invalid")  # #203: r1 targets the Tier-1 (logged-in) trip
        self.assertEqual(r2.status_code, 403)
        self.assertEqual(r2.json().get("reason"), "not_trip_owner")  # r2 targets the Tier-2 (anon) trip
        self.assertEqual(len(mock_m.cancel_calls), 0)
        self.assertEqual(store.get_plan(self._OWNER_IDK)["status"], "booked")
        self.assertEqual(store.get_plan(self._ANON_IDK)["status"], "booked")

    # ---- 8 (targeted regression). /cancel already_cancelled by a non-owner
    #        -> 403, proving the gate precedes the already_cancelled branch ----
    def test_already_cancelled_probed_by_non_owner_is_403_not_already_cancelled(self):
        client, store = self._client()
        mock_m = _MockMerchant()
        with client:
            _inject_mock_merchant(mock_m)
            token = _login(client, "demo-mei")
            # First, the real owner cancels for real.
            r1 = client.post("/cancel", json={"idempotency_key": self._OWNER_IDK,
                                              "session_token": token})
            self.assertEqual(r1.json()["outcome"], "cancelled")
            # Now a non-owner probes the now-cancelled trip. If the ownership
            # gate did NOT precede the already_cancelled branch, this would leak
            # outcome='already_cancelled' (confirming the trip's booking state
            # to a stranger) instead of an honest 403.
            other_token = _login(client, "demo-alex")
            r2 = client.post("/cancel", json={"idempotency_key": self._OWNER_IDK,
                                              "session_token": other_token})
        self.assertEqual(r2.status_code, 403)
        self.assertEqual(r2.json().get("reason"), "session_invalid")  # #203: Tier 1
        self.assertNotEqual(r2.json().get("outcome"), "already_cancelled")


class TestTripsDetailOwnership(unittest.TestCase):
    """SECURITY regression (#155): GET /trips/{key} ownership gate — the read-only
    sibling of TestCancelOwnership above. Reuses the SAME two-tier
    _authorize_trip_action decision, but identity is threaded as QUERY PARAMS
    (this is a GET) and a DENIED read renders as 404 `not_found` — NOT the
    write endpoints' 403 `not_trip_owner` — to avoid an existence oracle (see
    server.py's trips_detail docstring). No mock merchant needed; this is
    read-only."""

    _OWNER_IDK = "trip-detail-own-001"   # logged-in owner (demo-mei), plan_ready
    _ANON_IDK = "trip-detail-anon-001"   # anonymous, plan_ready, owner_token-bound
    _LEGACY_ANON_IDK = "trip-detail-legacy-anon-001"  # anon, no owner_token (pre-rollout)
    _ANON_TOKEN = "anon-secret-read-A"

    def _seed(self, store: SqliteDashboardStore) -> None:
        # Tier 1: a real logged-in demo user's plan_ready trip.
        owner_env = {**copy.deepcopy(_PLAN_ENVELOPE), "idempotency_key": self._OWNER_IDK}
        store.save_plan({
            "idempotency_key": self._OWNER_IDK, "user_id": "demo-mei",
            "checkout_id": "co-detail-own-001", "dest_token": "JP",
            "package_total_cents": 75000, "envelope": owner_env,
        })
        # Tier 2: an anonymous trip, bound to owner_token at creation.
        anon_env = {**copy.deepcopy(_PLAN_ENVELOPE), "idempotency_key": self._ANON_IDK}
        store.save_plan({
            "idempotency_key": self._ANON_IDK, "user_id": "",
            "checkout_id": "co-detail-anon-001", "dest_token": "JP",
            "package_total_cents": 75000, "envelope": anon_env,
            "owner_token": self._ANON_TOKEN,
        })
        # Tier 2 legacy: an anon row with NO owner_token at all (pre-rollout state).
        legacy_env = {**copy.deepcopy(_PLAN_ENVELOPE), "idempotency_key": self._LEGACY_ANON_IDK}
        store.save_plan({
            "idempotency_key": self._LEGACY_ANON_IDK, "user_id": "",
            "checkout_id": "co-detail-legacy-001", "dest_token": "JP",
            "package_total_cents": 75000, "envelope": legacy_env,
        })

    def _client(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        self._seed(store)
        return TestClient(server.build_app()), store

    # ---- 1. Logged-in owner + valid session_token -> 200, envelope returned ----
    def test_owner_valid_session_token_200(self):
        client, _ = self._client()
        with client:
            token = _login(client, "demo-mei")
            r = client.get(f"/trips/{self._OWNER_IDK}", headers={"X-Session-Token": token})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body.get("idempotency_key"), self._OWNER_IDK)
        self.assertEqual(body.get("outcome"), "plan_ready")

    # ---- 2. Logged-in trip, no session_token -> 404 (not 403) ----
    def test_logged_in_trip_no_token_404(self):
        client, _ = self._client()
        with client:
            r = client.get(f"/trips/{self._OWNER_IDK}")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"error": "not_found", "idempotency_key": self._OWNER_IDK})
        # No envelope/private-field leakage in the denied response.
        self.assertNotIn("legs", r.text)
        self.assertNotIn("day_plans", r.text)
        self.assertNotIn("wallet", r.text)

    # ---- 3. session_token minted for a DIFFERENT demo user -> 404 ----
    def test_logged_in_trip_wrong_user_token_404(self):
        client, _ = self._client()
        with client:
            other_token = _login(client, "demo-alex")   # real user, NOT the trip owner
            r = client.get(f"/trips/{self._OWNER_IDK}", headers={"X-Session-Token": other_token})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"error": "not_found", "idempotency_key": self._OWNER_IDK})

    # ---- 4. Anon trip + correct owner_token -> 200, envelope returned ----
    def test_anon_trip_correct_owner_token_200(self):
        client, _ = self._client()
        with client:
            r = client.get(f"/trips/{self._ANON_IDK}", headers={"X-Owner-Token": self._ANON_TOKEN})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body.get("idempotency_key"), self._ANON_IDK)

    # ---- 5. Anon trip, wrong/missing owner_token -> 404 ----
    def test_anon_trip_wrong_owner_token_404(self):
        client, _ = self._client()
        with client:
            r_wrong = client.get(f"/trips/{self._ANON_IDK}", headers={"X-Owner-Token": "not-the-right-secret"})
            r_missing = client.get(f"/trips/{self._ANON_IDK}")
        self.assertEqual(r_wrong.status_code, 404)
        self.assertEqual(r_wrong.json(), {"error": "not_found", "idempotency_key": self._ANON_IDK})
        self.assertEqual(r_missing.status_code, 404)
        self.assertEqual(r_missing.json(), {"error": "not_found", "idempotency_key": self._ANON_IDK})

    # ---- 6. Legacy anon row (no stored owner_token) fails OPEN, per
    #         _authorize_trip_action's documented transient-rollout behaviour ----
    def test_legacy_anon_row_fails_open_200(self):
        client, _ = self._client()
        with client:
            r = client.get(f"/trips/{self._LEGACY_ANON_IDK}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("idempotency_key"), self._LEGACY_ANON_IDK)

    # ---- 7. Existence-oracle lock: a DENIED read for an EXISTING trip must be
    #         byte-identical to the 404 for a genuinely UNKNOWN key. This is the
    #         security-defining assertion for the read fix — the write endpoints
    #         assert 403 not_trip_owner; here we assert there is NO distinguishing
    #         signal at all between "exists, not yours" and "no such trip". ----
    def test_denied_read_is_indistinguishable_from_unknown_key(self):
        client, _ = self._client()
        with client:
            r_denied = client.get(f"/trips/{self._OWNER_IDK}")  # exists, no token
            r_unknown = client.get(f"/trips/{self._OWNER_IDK}-does-not-exist")
        self.assertEqual(r_denied.status_code, 404)
        self.assertEqual(r_unknown.status_code, 404)
        self.assertNotIn("not_trip_owner", r_denied.text)
        # Same shape: {"error": "not_found", "idempotency_key": <the key requested>}.
        self.assertEqual(set(r_denied.json().keys()), set(r_unknown.json().keys()))
        self.assertEqual(r_denied.json()["error"], r_unknown.json()["error"])


class TestTripsListOwnership(unittest.TestCase):
    """SECURITY (read-IDOR, sibling of #154/#155): GET /trips?user_id= must not
    return a user's trip history to a caller who cannot prove session
    possession. Demo user_ids are trivially guessable (demo_users.py); this was
    live-confirmed as HIGH severity — GET /trips?user_id=demo-mei with NO token
    returned HTTP 200 with the victim's full trip history (booking_ref, package
    totals, dates)."""

    def test_trips_list_denies_unauthenticated_caller(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        store.save_plan({
            "idempotency_key": "trip-victim",
            "user_id": _USER_ID,
            "package_total_cents": 99000,
            "envelope": {"outcome": "plan_ready"},
        })
        store.mark_booked("trip-victim", booking_ref="BK-SECRET-001",
                          envelope={"outcome": "success"},
                          confirmed_at="2026-09-01T10:00:00+00:00")
        client = TestClient(server.build_app())
        with client:
            r = client.get(f"/trips?user_id={_USER_ID}")
            self.assertNotEqual(r.status_code, 200,
                "IDOR: /trips returned victim data to an unauthenticated caller")
            self.assertNotIn("BK-SECRET-001", r.text)

    def test_trips_list_denies_wrong_users_session(self):
        """A caller holding a VALID session for a DIFFERENT demo user must not
        be able to read _USER_ID's trips by just passing user_id=_USER_ID."""
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        store.save_plan({
            "idempotency_key": "trip-victim-2",
            "user_id": _USER_ID,
            "package_total_cents": 99000,
            "envelope": {"outcome": "plan_ready"},
        })
        store.mark_booked("trip-victim-2", booking_ref="BK-SECRET-002",
                          envelope={"outcome": "success"},
                          confirmed_at="2026-09-01T10:00:00+00:00")
        client = TestClient(server.build_app())
        with client:
            other_token = _login(client, "demo-alex")  # a real, but WRONG, user's session
            r = client.get(f"/trips?user_id={_USER_ID}", headers={"X-Session-Token": other_token})
            self.assertNotEqual(r.status_code, 200)
            self.assertNotIn("BK-SECRET-002", r.text)

    def test_trips_list_allows_the_owner_with_session(self):
        store = SqliteDashboardStore(":memory:")
        set_store(store)
        store.save_plan({"idempotency_key": "trip-mine", "user_id": _USER_ID,
                         "package_total_cents": 50000,
                         "envelope": {"outcome": "plan_ready"}})
        client = TestClient(server.build_app())
        with client:
            token = _login(client, _USER_ID)
            r = client.get(f"/trips?user_id={_USER_ID}", headers={"X-Session-Token": token})
            self.assertEqual(r.status_code, 200, r.text)
            keys = [t["idempotency_key"] for t in r.json().get("trips", [])]
            self.assertIn("trip-mine", keys)


if __name__ == "__main__":
    unittest.main()
